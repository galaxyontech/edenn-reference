"""Rendering an approved sound-effects plan, once, for both deployments.

This exists for the same reason ``narration_render`` does: the decisions here
were written inside the local design server, and the worker fleet had no copy of
them at all — no worker class, no role, nothing consuming the ``video_sfx`` task.
On the fleet the job simply sat in the queue while the API reported it queued.
Putting the decisions in one place is what stops the two from answering
differently once both run.

Four decisions live here, and each of them is the kind that is invisible when it
goes wrong:

**Who spotted the video.** ``plan`` renders the moments the user approved, where
they put them, and builds no analysis stage at all — so an approved render also
costs nothing in understanding calls. ``engine`` flattens the plan into prose and
lets the workflow spot the video itself, which is right when nobody has said what
they want and turns the plan card into a suggestion box when they have.

**Effects the caller already has.** Every effect is its own paid generation, so
re-synthesising a bed of twelve to fix one hit charges for eleven sounds the user
was happy with and returns subtly different ones. The reuse map was carried all
the way from the tool into the job payload and then dropped on the floor by the
only code that ran it.

**An empty bed is a failure, not a take.** Per-effect errors are swallowed inside
the workflow so one bad sound cannot sink a batch — which means a provider outage
returns a full set of silent events and a mux that still produces a video. That
must not reach a user as a completed take.

**What actually rendered.** The manifest is the only way anything downstream can
check the render against the plan card, which is the entire point of having a
plan card.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: Spotting modes. ``plan`` is the approved moments; ``engine`` re-spots.
SPOTTING_PLAN = "plan"
SPOTTING_ENGINE = "engine"


class SfxRenderFailed(RuntimeError):
    """The render ran and produced nothing a user could be given.

    Deliberately distinct from an exception out of the workflow: this is the
    case where everything "succeeded" and the result is unusable, which is the
    one that otherwise ships as a completed take.
    """


@dataclass(frozen=True)
class SfxRenderResult:
    """What a render produced, before either caller decides where to put it."""

    #: The source video with the effects bed muxed on, when one was produced.
    final_video_path: Optional[Path]
    #: The effects bed on its own.
    mixed_audio_path: Optional[Path]
    #: Which mode actually ran — not necessarily the one that was asked for.
    spotting: str
    #: Per-event record of what rendered and where it landed.
    rendered_events: list[dict[str, Any]] = field(default_factory=list)
    #: Effects taken from the reuse map instead of being generated again.
    reused_event_ids: tuple[str, ...] = ()
    #: What the render MEASURED, taken here because this is where the files
    #: are local. The read path cannot probe: hydration runs on a poll inside a
    #: locked read-modify-write, and an ffmpeg call there would hold a row open
    #: on every tick.
    take_signals: dict[str, Any] = field(default_factory=dict)
    #: Whether the engine WATCHED the footage or was handed a description of
    #: it. Two different products at two different prices, and until now the
    #: difference between them appeared in one log line and nowhere else: a
    #: deployment with no reachable URL for the clip silently produced
    #: prompt-written effects and called them video sound design.
    watched_the_video: bool = False
    #: Plain words for why it did not, when it did not.
    not_watched_reason: str = ""

    @property
    def placed_count(self) -> int:
        return sum(1 for event in self.rendered_events if event.get("rendered"))


def requested_duration(plan_row: dict[str, Any]) -> Optional[float]:
    """The duration a plan row asks for, or ``None`` when it names none.

    One accessor on purpose: the render stamps this into the manifest and the
    redo path compares against that stamp, and the two reading the row
    differently is how "unchanged" and "changed" swap places.
    """
    for key in ("duration_s", "duration"):
        if plan_row.get(key) is not None:
            try:
                return float(plan_row[key])
            except (TypeError, ValueError):
                return None
    return None


def resolve_spotting(
    requested: Any, *, events: list[dict[str, Any]], ambience: str
) -> str:
    """Which mode this job actually runs in.

    A job enqueued before plan authority existed carries no mode, and its event
    list may be empty — re-spotting is what it was built expecting, so absence
    means ``engine``. A plan with neither moments nor a bed has nothing to
    render from, so it falls back rather than producing silence.
    """
    spotting = str(requested or SPOTTING_ENGINE).strip().lower()
    if spotting == SPOTTING_PLAN and not (events or ambience):
        return SPOTTING_ENGINE
    return SPOTTING_PLAN if spotting == SPOTTING_PLAN else SPOTTING_ENGINE


def engine_prompt(summary: str, events: list[dict[str, Any]]) -> str:
    """The plan as prose, for the mode that does its own spotting.

    Not a substitute for the plan — the engine may ignore any of it. It is how a
    user's intent survives into a mode that was not given a plan to honour.
    """
    labels = "; ".join(
        f"{(event.get('label') or event.get('prompt') or 'effect')} "
        f"at {round(float(event.get('start_s') or 0), 1)}s"
        for event in events
        if isinstance(event, dict)
    )
    parts = [part for part in (summary, f"Sound effects: {labels}" if labels else "") if part]
    return " ".join(parts).strip()


def usable_reuse_map(
    reuse_event_audio: Any, *, log: Callable[[str], None] = logger.info
) -> dict[str, str]:
    """The subset of an already-rendered bed that is still on this disk.

    Reuse is recorded as a local path, which is true for as long as the process
    that wrote it is alive and never true on a different container. Rather than
    let that degrade silently into a full-price re-render, the ones that cannot
    be honoured are counted and said out loud — a re-render that costs eleven
    sounds more than the user expected should be visible in a log line.
    """
    if not isinstance(reuse_event_audio, dict):
        return {}
    usable: dict[str, str] = {}
    missing = 0
    for event_id, path in reuse_event_audio.items():
        if path and Path(str(path)).exists():
            usable[str(event_id)] = str(path)
        elif path:
            missing += 1
    if missing:
        log(
            f"sfx reuse: {missing} effect(s) the caller kept are not on this "
            "disk and will be generated again"
        )
    return usable


async def render_sfx(
    *,
    video_path: Path,
    source_public_url: str = "",
    events: list[dict[str, Any]],
    summary: str = "",
    ambience: str = "",
    spotting: Any = SPOTTING_ENGINE,
    route: str = "auto",
    reuse_event_audio: Optional[dict[str, str]] = None,
    run_dir: Path,
    log: Callable[[str], None] = logger.info,
) -> SfxRenderResult:
    """Run the sound-effects pipeline and return what it made.

    ``source_public_url`` is a signed, expiring, read-only URL the
    video-conditioned engine fetches the clip from. Absent, the text route
    carries the whole plan — a quality decision, not an outage.

    Raises :class:`SfxRenderFailed` when the run produced no usable output, so a
    caller cannot mistake an empty bed for a delivered take.
    """
    from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.planned_run import (
        PlannedSfxInput,
        PlannedSfxRun,
        rendered_event_manifest,
    )
    from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.video_sound_effect_workflow import (
        VideoSfxWorkflowOptions,
        VideoSoundEffectWorkflowE2E,
        VideoSoundEffectWorkflowE2EInput,
    )

    plan = [event for event in (events or []) if isinstance(event, dict)]
    ambience_prompt = str(ambience or "").strip()
    mode = resolve_spotting(spotting, events=plan, ambience=ambience_prompt)
    # The route the SESSION asked for. "auto" keeps the old behaviour — decide
    # from what is reachable — and the two named values are peers: one has the
    # engine watch the footage, the other writes each effect from the prompt
    # the agent composed after watching it itself.
    wanted_route = str(route or "auto").strip().lower()
    if wanted_route not in {"auto", "video_native", "text"}:
        wanted_route = "auto"
    options = VideoSfxWorkflowOptions(
        enable_ambience=bool(ambience_prompt), bed_route=wanted_route
    )

    if mode == SPOTTING_PLAN:
        reuse = usable_reuse_map(reuse_event_audio, log=log)
        log(
            f"sfx render: honouring the approved plan — {len(plan)} moment(s), "
            f"{len(reuse)} kept from a previous take"
        )
        out = await PlannedSfxRun().execute(
            PlannedSfxInput(
                video_path=str(video_path),
                uploaded_public_facing_url=source_public_url,
                events=plan,
                ambience_prompt=ambience_prompt,
                reuse_event_audio=reuse,
                run_dir=str(run_dir),
                options=options,
            )
        )
    else:
        # Re-spotting cannot honour a per-event reuse map: the events it will
        # choose are not the events those files belong to.
        reuse = {}
        log(f"sfx render: letting the engine spot the video ({len(plan)} hint(s))")
        out = await VideoSoundEffectWorkflowE2E().execute(
            VideoSoundEffectWorkflowE2EInput(
                video_path=str(video_path),
                uploaded_public_facing_url=source_public_url,
                user_prompt=engine_prompt(str(summary or "").strip(), plan) or None,
                run_dir=str(run_dir),
                options=options,
            )
        )

    generated = list(out.generated_sound_events or [])
    placed = [event for event in generated if event.active_audio_path]
    if generated and not placed:
        raise SfxRenderFailed("every planned sound effect came back empty")

    manifest = rendered_event_manifest(generated)
    if mode == SPOTTING_PLAN:
        # Stamp each entry with the duration the PLAN asked for, verbatim
        # (None when the row named none). Length is a generation parameter:
        # a later redo that reuses this effect must be able to prove the
        # moment still wants a sound of this length, and the rendered event's
        # own end time cannot answer that — it is clamped to the picture, so
        # comparing against it would regenerate every tail effect forever.
        requested = {
            str(row.get("id") or ""): requested_duration(row)
            for row in plan
        }
        for entry in manifest:
            if entry.get("id") in requested:
                entry["requested_duration_s"] = requested[entry["id"]]

    final_video = Path(out.final_video_path) if out.final_video_path else None
    mixed_audio = Path(out.mixed_audio_path) if out.mixed_audio_path else None
    if not (
        (final_video and final_video.exists()) or (mixed_audio and mixed_audio.exists())
    ):
        raise SfxRenderFailed(
            "sound-effect generation returned no audio or video output"
        )

    # Measure before anything is published: "rendered" has only ever meant the
    # provider returned a path, so an effect that came back as silence was
    # indistinguishable from one that worked — on the card, in the manifest and
    # in the session — and the only way to find out was to play the video.
    from .media import sfx_take_signals

    try:
        signals = sfx_take_signals(manifest, bed_path=mixed_audio)
    except Exception:  # noqa: BLE001 - a take that cannot be measured is still a take
        logger.warning("sfx render: could not measure the take", exc_info=True)
        signals = {}
    if signals.get("silent_event_ids"):
        log(
            "sfx render: "
            f"{len(signals['silent_event_ids'])} effect(s) came back silent"
        )

    watched = bool(getattr(out, "ambience_route", None) == "video_native")
    not_watched = "" if watched else str(
        getattr(out, "ambience_route_reason", "") or "the effects were written from prompts"
    )
    if not watched:
        asked = " (asked for)" if wanted_route == "video_native" else ""
        log(f"sfx render: the engine did not watch the footage{asked} — {not_watched}")

    return SfxRenderResult(
        final_video_path=final_video if final_video and final_video.exists() else None,
        mixed_audio_path=mixed_audio if mixed_audio and mixed_audio.exists() else None,
        spotting=mode,
        rendered_events=manifest,
        reused_event_ids=tuple(sorted(reuse)),
        take_signals=signals,
        watched_the_video=watched,
        not_watched_reason=not_watched,
    )


def published_manifest(
    rendered_events: list[dict[str, Any]],
    *,
    publish: Callable[[str, Path], Optional[str]],
    known_urls: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """The manifest as a job RESULT may carry it: URLs in, server paths out.

    ``audio_path`` is a filesystem path on whichever container rendered the
    effect. Persisting it into a job result serves it to every session viewer
    — the same server-path leak this repo has already had once — and it is
    useless to any other container anyway. What a result carries instead is
    ``audio_url``: a servable reference (``/dev/media/...`` on the standalone,
    a signed storage URL on the fleet) that a later redo can resolve back into
    bytes wherever it runs. That URL is also the reuse identity: a kept effect
    keeps its URL, so "was this generated again?" is answerable from the
    outside.

    ``publish`` turns one event's local file into that URL (or ``None`` when
    it cannot). ``known_urls`` short-circuits it for effects that already HAVE
    a URL — a reused effect must keep the URL it was first published under,
    not be re-uploaded into a fresh one that makes it look regenerated.
    """
    published: list[dict[str, Any]] = []
    for entry in rendered_events or []:
        row = {k: v for k, v in entry.items() if k != "audio_path"}
        event_id = str(entry.get("id") or "")
        path = str(entry.get("audio_path") or "")
        url = (known_urls or {}).get(event_id)
        if not url and path and Path(path).exists():
            url = publish(event_id, Path(path))
        row["audio_url"] = url or ""
        published.append(row)
    return published


__all__ = [
    "SPOTTING_ENGINE",
    "SPOTTING_PLAN",
    "SfxRenderFailed",
    "SfxRenderResult",
    "engine_prompt",
    "published_manifest",
    "render_sfx",
    "requested_duration",
    "resolve_spotting",
    "usable_reuse_map",
]
