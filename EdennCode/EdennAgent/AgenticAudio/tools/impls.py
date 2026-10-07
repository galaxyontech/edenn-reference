"""Concrete agent-facing tools.

Each tool is a :class:`Tool` subclass whose ``run`` is the body of the former
``AgenticAudioAgent._tool_*`` method, now reaching through a :class:`ToolContext`
instead of the agent. ``build_tool_registry`` assembles the single registry the
reasoning loop dispatches through. Identity/metadata lives in ``models.TOOL_SPECS``.
"""

from __future__ import annotations

from typing import Any, Optional

from ..events import EventType
from ..models import (
    SCULPT_KIND_SPLICE,
    AGENT_EDIT_KIND_REGENERATE,
    AGENT_EDIT_KINDS,
    AGENT_PLAN_MODE_FULL_E2E,
    AGENT_PLAN_MODE_MUSIC_FIRST,
    AGENT_PLAN_MODES,
    AGENT_TOOL_ADJUST_REMIX,
    AGENT_TOOL_ANALYZE_VIDEO,
    AGENT_TOOL_APPROVE_DIRECTION,
    AGENT_TOOL_COMPOSE_MIX,
    AGENT_TOOL_EDIT_AUDIO,
    AGENT_TOOL_FINALIZE,
    AGENT_TOOL_GENERATE_CANDIDATES,
    AGENT_TOOL_GENERATE_SFX,
    AGENT_TOOL_GENERATE_VOICEOVER,
    AGENT_TOOL_PLAN_SFX,
    AGENT_TOOL_COMPARE_TAKES,
    AGENT_TOOL_SCULPT_AUDIO,
    AGENT_TOOL_PROPOSE_SCRIPT,
    AGENT_TOOL_SET_PRODUCTION_PLAN,
    SCULPT_KINDS,
    SCULPT_KIND_SHIFT_WINDOW,
    AUDIO_LAYER_MUSIC,
    AUDIO_LAYER_SFX,
    AUDIO_LAYER_VOICEOVER,
    AUDIO_LAYERS,
    DEFAULT_VOICE_ID,
    MAX_MESSAGE_CHARS,
    VOICE_CATALOG,
    AgenticAudioSessionPhase,
    AgenticMessageRole,
    AgenticSessionStatus,
    AgenticToolStatus,
    default_candidate_count_for_modelspec,
)
from .base import ApprovalRequiredError, Tool, ToolContext, ToolRegistry, ToolResult
from .media import (
    RENDER_EPOCH_KEY,
    TAKE_SIGNALS_KEY,
    candidate_music_source,
    candidate_render_epoch,
    estimated_speech_seconds,
    mix_alignment,
    music_alignment,
    normalize_music_envelope,
    normalize_splice_segments,
    snap_to_beat,
    normalize_music_modelspec,
    speech_units,
    stamped_take_signals,
    mix_stems,
    mix_still_describes,
)
from .media import UNSPACED_CHARS_PER_SECOND  # noqa: E402
from .spotting import (
    OWNER_SILENCE,
    OWNERS,
    build_spotting_sheet,
    free_windows,
    narration_conflicts,
    set_moment_owner,
    sfx_conflicts,
)

import logging
import math

logger = logging.getLogger(__name__)


# Bounds for mix parameters. Unbounded client values previously reached ffmpeg
# directly: a huge music_volume drove the encoder into a multi-minute NaN/Inf
# spin while the per-session turn lock was held, permanently deadlocking the
# session (adversarial unhappy-path review, P1). Every mix param is now coerced
# to a finite float and clamped to a sane range BEFORE any generation/mux.
# How many finished takes a comparison describes. Its result rides the state
# summary on every later step, so it has to stay small; the most recent takes are
# the ones a session is actually choosing between.
COMPARE_TAKES_LIMIT = 6

_MIX_PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "music_volume": (0.0, 2.0),
    "voiceover_volume": (0.0, 2.0),
    "duck_gain_db": (-60.0, 24.0),
    "voiceover_start_s": (0.0, 3600.0),
    "sfx_volume": (0.0, 2.0),
}


# The SLOW end of measured delivery, not the average. Rates from a real
# four-line read on the reference clip were 2.10 / 2.97 / 2.13 / 2.47 w/s once
# edge padding was trimmed. The check has to assume speech is slow, because
# under-estimating spoken length is precisely what lets an overrun through: at
# the 2.5 the prompt quotes, that same script "fits" at 15.8s and then actually
# ran to 17.4s on a 16.2s clip.
NARRATION_WORDS_PER_SECOND = 2.1
# The prompt's planning rate — quoted back to the model so the advice it gets
# here matches the advice it was given.
NARRATION_PLANNING_WORDS_PER_SECOND = 2.5
# Air the timeline resolver enforces between consecutive lines; a fit estimate
# that ignores it is short by 0.4s per joint.
NARRATION_MIN_GAP_S = 0.4
# Air after the last word, so the read does not collide with the final frame.
NARRATION_TAIL_MARGIN_S = 0.15


def _narration_overrun(
    segments: list[dict[str, Any]], *, duration_s: float
) -> dict[str, Any] | None:
    """Reject a narration plan that cannot physically fit the clip.

    The writer is told a word budget, and does not reliably keep to it: on a
    16.17s reference clip it produced 38 words against its own stated ceiling of
    24, and the overflowing tail was then amputated mid-phrase at mux — the last
    line lost its payoff, which is the one line that most needed to land.

    Estimating is enough to catch this. A line cannot be spoken faster than the
    rate above, so a plan whose final line is still running past the last frame
    is over budget no matter how the takes come back. Returning the per-line
    numbers (rather than a bare refusal) gives the model something it can act
    on, and costs nothing because no synthesis has happened yet.

    Returns None when the plan fits.
    """

    if not segments or duration_s <= 0:
        return None

    lines, prev_end = [], 0.0
    for i, seg in enumerate(segments):
        # Counted in whichever script the line is written in: whitespace words
        # are meaningless in Japanese/Chinese/Korean/Thai, where a whole
        # sentence is one "word" and the budget check silently passes anything.
        words, unit = speech_units(seg.get("text"))
        est = estimated_speech_seconds(
            seg.get("text"), words_per_second=NARRATION_WORDS_PER_SECOND)
        floor = prev_end + NARRATION_MIN_GAP_S if i else 0.0
        start = max(float(seg.get("start_s") or 0.0), floor)
        end = start + est
        prev_end = end
        lines.append({
            "id": seg.get("id"),
            "words": words,
            "unit": unit,
            "start_s": round(start, 2),
            "estimated_end_s": round(end, 2),
        })

    last_end = lines[-1]["estimated_end_s"]
    limit = duration_s - NARRATION_TAIL_MARGIN_S
    if last_end <= limit:
        return None

    total_words = sum(line["words"] for line in lines)
    # Budget from the SAME rate the check uses, minus the air between lines —
    # quoting a budget derived from a different rate produces the nonsense of
    # "38 words against a 40-word budget" on a plan that was just rejected.
    speakable_s = max(0.0, limit - (len(lines) - 1) * NARRATION_MIN_GAP_S)
    unit = lines[0]["unit"]
    per_second = (UNSPACED_CHARS_PER_SECOND if unit == "characters"
                  else NARRATION_WORDS_PER_SECOND)
    budget = int(speakable_s * per_second)

    # WHICH constraint binds decides what advice can possibly work. A script
    # well under the clip's budget that still overruns is not too long — it is
    # placed too late, and telling its writer to "cut at least 1 character" is
    # advice that cannot resolve the refusal no matter how often it is followed.
    # That is what the deployed console did: 22 characters against a stated
    # 73-character budget, refused three times running, each time asking for one
    # more character (live audit, 2026-08-30). Detect, gate, and then offer the
    # alternative — a warning that names no way out is just a wall.
    last = lines[-1]
    last_est = last["estimated_end_s"] - last["start_s"]
    latest_start = limit - last_est
    fits_at_cue = int(max(0.0, limit - last["start_s"]) * per_second)
    binding = "length" if total_words > budget else "placement"
    excess = max(1, total_words - budget)

    if binding == "length":
        instruction = (
            f"This narration does not fit the clip. Allowing for real delivery "
            f"pace and the {NARRATION_MIN_GAP_S}s of air between lines, the last "
            f"line ends near {last_end:.1f}s but the video is only "
            f"{duration_s:.1f}s — the tail would be cut off mid-phrase, and the "
            f"final line is the one that most needs to land. Across "
            f"{len(lines)} lines this clip carries about {budget} {unit}; this "
            f"script is {total_words}. Cut at least {excess} "
            f"{unit[:-1] if excess == 1 else unit}, or drop a line entirely — "
            f"fewer, shorter lines with air between them beat a crowded read. "
            f"Keep the moments you most want to hit, then call propose_script "
            f"again."
        )
    elif latest_start >= 0:
        instruction = (
            f"The script fits this clip; where you placed it does not. The last "
            f"line is cued at {last['start_s']:.1f}s with only "
            f"{max(0.0, limit - last['start_s']):.1f}s left before the final "
            f"frame — about {fits_at_cue} {unit} fit there, and that line is "
            f"{last['words']}. Either cue it at {latest_start:.1f}s or earlier "
            f"(the latest start that still lands it in time), or keep the cue "
            f"and cut that line to about {fits_at_cue} {unit}. Shortening the "
            f"OTHER lines will not help — the clip has room, this line does not."
        )
    else:
        instruction = (
            f"The last line cannot land on this clip at any cue: spoken at real "
            f"pace it runs {last_est:.1f}s, and only {limit:.1f}s of video "
            f"remain in total. Cut it to about {int(limit * per_second)} {unit} "
            f"or drop it, then call propose_script again."
        )

    return {
        "video_duration_s": round(duration_s, 2),
        "estimated_narration_end_s": round(last_end, 2),
        "total_words": total_words,
        "word_budget": budget,
        "cut_at_least_words": excess,
        # What actually binds, and the way out of it — so a caller can act on
        # this without re-deriving the arithmetic from the prose.
        "binding": binding,
        "last_line_start_s": last["start_s"],
        "latest_start_s": round(latest_start, 2),
        "fits_at_cue": fits_at_cue,
        "lines": lines,
        "instruction": instruction,
    }


# How many times the narration gate may refuse a draft while only the MODEL is
# told. Past this the user hears about it directly.
NARRATION_GATE_ATTEMPTS = 2


def _narration_refusal(
    ctx: Any,
    *,
    error: str,
    instruction: str,
    detail: dict[str, Any],
    user_note: str,
) -> Any:
    """Refuse a draft — and stop refusing silently once the model is stuck.

    Each refusal is instructive data the model is meant to act on, and for a
    couple of rounds that is exactly right. But on the deployed console a draft
    was refused three times running while the agent narrated success in detail —
    "I tightened the final line so it lands cleanly" — and nothing was ever
    saved. The user read a confident story and got an empty card. A gate the
    model can talk over is not a gate, so past the cap the refusal becomes a
    message the USER reads, carrying the same way out the model was given.
    """

    state = dict(ctx.require_session().state_json)
    attempts = int(state.get("narration_gate_attempts") or 0) + 1
    ctx.repository.record_tool_call(
        session_id=ctx.session_id,
        tool_name=AGENT_TOOL_PROPOSE_SCRIPT,
        status=AgenticToolStatus.FAILED,
        input_json={"blocked": error, "attempt": attempts},
        output_json=detail,
        finished=True,
    )
    events: list[Any] = []
    if attempts > NARRATION_GATE_ATTEMPTS:
        events = ctx.append_message(
            AgenticMessageRole.ASSISTANT,
            f"I couldn't fit a voice-over to this clip — {user_note}. Nothing was "
            f"recorded and nothing was spent. Tell me what to protect and I'll "
            f"draft it again: fewer lines, a shorter read, or moving the last "
            f"line earlier.",
        )
        attempts = 0  # a later, different draft starts clean
    state["narration_gate_attempts"] = attempts
    ctx.repository.update_session(ctx.session_id, state_json=state)
    return ToolResult(events=events, data={
        "error": error,
        "instruction": instruction,
        "detail": detail,
    })


def _narration_over_source_audio(
    segments: list[dict[str, Any]], *, observation: dict[str, Any]
) -> dict[str, Any] | None:
    """Refuse a line written over the footage's own voice.

    The clearest failure this system can produce, and until the source track was
    scanned it could not perceive it at all: somebody speaks on camera, the agent
    writes a line across them, the render places it, and the alignment report
    calls the result clean.

    Only discrete activity counts. A clip whose audio runs end to end is a bed —
    music, room tone — and treating that as a no-go zone would ban narration from
    the whole video, so ``attach_source_audio`` reports it as continuous and this
    check has nothing to act on.

    Returns None when the read stays out of the footage's way.
    """

    windows = observation.get("speech_windows") or []
    if not segments or not windows:
        return None

    clashes = []
    for seg in segments:
        try:
            start = float(seg.get("start_s") or 0.0)
        except (TypeError, ValueError):
            continue
        end = start + estimated_speech_seconds(
            seg.get("text"), words_per_second=NARRATION_WORDS_PER_SECOND)
        for lo, hi in windows:
            try:
                lo, hi = float(lo), float(hi)
            except (TypeError, ValueError):
                continue
            overlap = min(end, hi) - max(start, lo)
            if overlap > 0.25:
                clashes.append({
                    "id": seg.get("id"),
                    "line": str(seg.get("text") or "")[:60],
                    "over": [round(lo, 2), round(hi, 2)],
                    "overlap_s": round(overlap, 2),
                })
                break
    if not clashes:
        return None

    free = []
    cursor = 0.0
    duration = float(observation.get("duration_s") or 0.0)
    for lo, hi in sorted((float(a), float(b)) for a, b in windows):
        if lo - cursor > 0.6:
            free.append([round(cursor, 2), round(lo, 2)])
        cursor = max(cursor, hi)
    if duration - cursor > 0.6:
        free.append([round(cursor, 2), round(duration, 2)])

    listed = "; ".join(
        f"{c['id']} over {c['over'][0]}-{c['over'][1]}s" for c in clashes)
    windows_txt = ", ".join(f"{a}-{b}s" for a, b in free) or "nowhere"
    return {
        "clashes": clashes,
        "free_windows": free,
        "instruction": (
            f"These lines talk over sound the footage is already making: "
            f"{listed}. Whoever is speaking on camera is the point of that "
            f"moment — narration on top of them competes with the thing the "
            f"viewer is watching. The clip is quiet during: {windows_txt}. Move "
            f"those lines into a quiet stretch, or drop them and let the footage "
            f"carry it (hold_silent records that as a decision). Then call "
            f"propose_script again."
        ),
    }


def _coerce_mix_param(name: str, value: Any, default: float) -> float:
    """Coerce a mix parameter to a finite, in-range float.

    Rejects non-numeric / NaN / Inf with a ValueError (mapped to a 400 by the
    API boundary) instead of letting it reach ffmpeg; clamps in-range values so a
    999 or -50 can never corrupt the mux or the persisted default.
    """

    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a number.")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number.")
    if not math.isfinite(num):
        raise ValueError(f"{name} must be a finite number.")
    lo, hi = _MIX_PARAM_BOUNDS.get(name, (float("-inf"), float("inf")))
    return max(lo, min(hi, num))


class AnalyzeVideoTool(Tool):
    name = AGENT_TOOL_ANALYZE_VIDEO

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        session = ctx.require_session()
        events = [ctx.event(EventType.TOOL_STARTED, {"tool_name": AGENT_TOOL_ANALYZE_VIDEO})]
        ctx.transition_to(AgenticAudioSessionPhase.OBSERVING)
        observation = await ctx.media.analyze_video(
            source_video_artifact_id=session.source_video_artifact_id,
            user_prompt=str(args.get("user_prompt") or ""),
            modelspec=normalize_music_modelspec(str(args.get("modelspec") or "edenn_basic")),
        )
        state = dict(session.state_json)
        state["observation"] = observation
        # Co-design is the default, not a mode: the layers are planned against
        # one shared moment list with an explicit owner each, every session, with
        # no card for the user to answer first. Best-effort — a sheet we could
        # not build must never fail the analysis turn.
        try:
            state["spotting_sheet"] = build_spotting_sheet(observation)
        except Exception:  # noqa: BLE001
            logger.warning("Spotting sheet unavailable for session %s", ctx.session_id)
            state["spotting_sheet"] = {"moments": [], "reliable": False}
        # Expose the source video itself to the client (P2 "watch" contract):
        # the canvas dock layers the muted source video under a take's audio, so
        # the user can WATCH a take against the picture without a per-take render.
        # Best-effort — a missing/odd artifact must never fail the analysis turn.
        try:
            artifact = ctx.media.get_source_video_artifact(session.source_video_artifact_id)
            meta = getattr(artifact, "metadata_json", None) or {}
            state["source_video"] = {
                "url": getattr(artifact, "url", None),
                "poster_url": meta.get("thumbnail_url"),
                "content_type": getattr(artifact, "content_type", None),
                "duration_s": meta.get("duration"),
            }
        except Exception:  # noqa: BLE001 - purely additive enrichment
            state.setdefault("source_video", None)
        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_ANALYZE_VIDEO,
            status=AgenticToolStatus.COMPLETED,
            input_json={"source_video_artifact_id": session.source_video_artifact_id},
            output_json=observation,
            linked_artifact_ids=[session.source_video_artifact_id],
            finished=True,
        )
        ctx.transition_to(AgenticAudioSessionPhase.PROPOSING, state_json=state
        )
        events.append(
            ctx.event(
                EventType.TOOL_COMPLETED,
                {"tool_name": AGENT_TOOL_ANALYZE_VIDEO, "output": observation},
            )
        )
        return ToolResult(events=events, data=observation)


class ApproveDirectionTool(Tool):
    name = AGENT_TOOL_APPROVE_DIRECTION

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        """Record an explicit user approval to spend on generation (cost gate).

        Light/in-process and auditable: it is the separately-logged step that
        unlocks ``generate_candidates``. Set by the deterministic ``/choices``
        proposal path too. ``approve_direction`` only records approval — it never
        spends.
        """

        session = ctx.require_session()
        proposal_id = str(args.get("proposal_id") or "") or None
        state = dict(session.state_json)
        state["approved_direction"] = True
        if proposal_id:
            state["approved_proposal_id"] = proposal_id
        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_APPROVE_DIRECTION,
            status=AgenticToolStatus.COMPLETED,
            input_json={"proposal_id": proposal_id},
            output_json={"approved_direction": True},
            finished=True,
        )
        ctx.repository.update_session(ctx.session_id, state_json=state)
        approval = {"approved": True, "proposal_id": proposal_id}
        return ToolResult(
            events=[ctx.event(EventType.DIRECTION_APPROVED, approval)], data=approval
        )


class GenerateCandidatesTool(Tool):
    name = AGENT_TOOL_GENERATE_CANDIDATES

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        session = ctx.require_session()
        # Cost gate (invariant #2): paid generation requires a prior explicit
        # approval — the deterministic /choices proposal path or approve_direction.
        if not session.state_json.get("approved_direction"):
            raise ApprovalRequiredError(
                "Music generation requires explicit user approval. Show the plan "
                "and have the user approve it (or pick a proposal) first."
            )
        try:
            proposal = ctx.resolve_proposal(session, args)
        except KeyError:
            # The model referenced a direction we never showed and gave no inline
            # plan. Degrade gracefully (ask which direction) instead of 404.
            return ToolResult(
                events=ctx.append_message(
                    AgenticMessageRole.ASSISTANT,
                    "I'm not sure which direction you mean — could you pick one of the "
                    "options I proposed, or describe the vibe you want?",
                ),
                data={"status": "needs_direction"},
            )
        events = [
            ctx.event(EventType.TOOL_STARTED, {"tool_name": AGENT_TOOL_GENERATE_CANDIDATES})
        ]
        ctx.transition_to(AgenticAudioSessionPhase.GENERATING_CANDIDATES)
        # Take count is model-dependent: Basic yields a single take; Enhanced and
        # Studio default to two so the user can A/B. An explicit count or the
        # proposal's candidate_count still wins.
        modelspec = normalize_music_modelspec(str(proposal.get("modelspec") or "edenn_basic"))
        model_default = default_candidate_count_for_modelspec(modelspec)
        requested = int(args.get("count") or proposal.get("candidate_count") or model_default)
        count = max(1, min(requested, ctx.max_candidates))

        def _announce(candidate_id: str, job_id: str, index: int) -> None:
            """Write the take into the session BEFORE its job row exists.

            The completer polls the job table, so the row IS the trigger. Jobs
            were created first and the takes written only after every one of
            them was enqueued — so a failure between take one and take two left
            a queued job that would run, spend, and have no take pointing at
            it. Nothing would ever show it to the user, and nothing would ever
            explain the charge.
            """

            ctx.repository.mutate_session_state(
                ctx.session_id,
                lambda current: {
                    **current,
                    "candidates": [
                        *(current.get("candidates") or []),
                        {
                            "candidate_id": candidate_id,
                            "proposal_id": proposal.get("proposal_id"),
                            "title": f"Take {index + 1}",
                            "status": "queued",
                            "linked_job_id": job_id,
                            "version": 1,
                        },
                    ],
                },
            )

        candidates = [
            candidate.model_dump(mode="json")
            for candidate in ctx.media.generate_music_candidates(
                session_id=ctx.session_id,
                source_video_artifact_id=session.source_video_artifact_id,
                proposal=proposal,
                count=count,
                creator_user_id=session.creator_user_id,
                actor_user_id=ctx.actor_user_id,
                # Ground generation in the bootstrap analysis: the fused prompt
                # carries the video's tempo/mood/instrumentation + timed scene arc,
                # not just the proposal prose (which alone yields unrelated music).
                observation=session.state_json.get("observation"),
                on_planned=_announce,
            )
        ]
        # Re-read: the placeholders above were written through the repository,
        # so the copy captured before them is stale.
        state = dict(ctx.require_session().state_json)
        state["selected_proposal_id"] = proposal.get("proposal_id")
        state["candidates"] = candidates
        linked_job_ids = [
            str(candidate["linked_job_id"])
            for candidate in candidates
            if candidate.get("linked_job_id")
        ]
        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_GENERATE_CANDIDATES,
            status=AgenticToolStatus.COMPLETED,
            input_json={"proposal": proposal},
            output_json={"candidates": candidates},
            linked_job_id=linked_job_ids[0] if linked_job_ids else None,
            linked_artifact_ids=[session.source_video_artifact_id],
            finished=True,
        )
        ctx.transition_to(AgenticAudioSessionPhase.AWAITING_CANDIDATE_CHOICE,
            linked_job_ids=linked_job_ids,
            state_json=state,
        )
        events.extend(
            [
                ctx.event(
                    EventType.TOOL_COMPLETED,
                    {"tool_name": AGENT_TOOL_GENERATE_CANDIDATES, "candidates": candidates},
                ),
                ctx.event(EventType.CANDIDATE_CARDS, {"candidates": candidates}),
                ctx.event(
                    EventType.PHASE_CHANGED,
                    {"phase": AgenticAudioSessionPhase.AWAITING_CANDIDATE_CHOICE},
                ),
            ]
        )
        return ToolResult(events=events, data={"candidates": candidates})


def _promote_mix_to_final(final_artifact: dict[str, Any], mix: dict[str, Any]) -> dict[str, Any]:
    """The composed mix IS the deliverable — said once, for both writers.

    finalize promotes it, and a later compose_mix REFRESHES it. Two independent
    copies of "the final file" is exactly how the deployment ended up serving a
    music-less mix from Export and the gallery while ``state.mix`` held the
    corrected one (live deployment, 2026-08-30).
    """

    return {
        **final_artifact,
        "video_url": mix["video_url"],
        "deliverable": "compose_mix",
        "mix": mix,
    }


class FinalizeTool(Tool):
    name = AGENT_TOOL_FINALIZE

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        session = ctx.require_session()
        candidate_id = str(
            args.get("candidate_id") or session.selected_candidate_id or ""
        )
        candidate = (
            ctx.find_by_id(
                session.state_json.get("candidates") or [], "candidate_id", candidate_id
            )
            if candidate_id
            else None
        )
        if candidate is None:
            # Voice-over-only deliverable: no music candidate to select, but a
            # composed narration-over-video mix exists — that IS the final output.
            mix = session.state_json.get("mix") or {}
            if not candidate_id and mix.get("video_url"):
                return await self._finalize_voiceover_only(ctx, session, mix)
            if not candidate_id:
                raise ValueError("Select a candidate before composing the final mix.")
            raise KeyError(f"Candidate not found: {candidate_id}")

        # The composed mix is what actually ships, so it must carry the take
        # being locked. Promoting one built from a DIFFERENT take — or from no
        # music at all — is how a "final mix" reached a user with the music
        # missing entirely (live deployment, 2026-08-30), and a later recompose
        # did not rescue it. compose_mix is free and local, so the honest answer
        # is "recompose", never "ship what happens to be lying here".
        mix0 = session.state_json.get("mix") or {}
        mix_music_id = str(mix0.get("music_candidate_id") or "")
        if mix0.get("video_url") and mix_music_id != candidate_id:
            carries = (
                f"take {mix_music_id}" if mix_music_id else "no music at all"
            )
            ctx.repository.record_tool_call(
                session_id=ctx.session_id,
                tool_name=AGENT_TOOL_FINALIZE,
                status=AgenticToolStatus.FAILED,
                input_json={"blocked": "mix_does_not_carry_candidate"},
                output_json={
                    "mix_music_candidate_id": mix0.get("music_candidate_id"),
                    "candidate_id": candidate_id,
                },
                finished=True,
            )
            return ToolResult(events=[], data={
                "error": "mix_does_not_carry_candidate",
                "instruction": (
                    f"The composed mix carries {carries}, but you are locking "
                    f"{candidate_id}. Recompose the mix with "
                    f"candidate_id={candidate_id} first, then finalize — "
                    "composing is free."
                ),
                "detail": {
                    "mix_music_candidate_id": mix0.get("music_candidate_id"),
                    "candidate_id": candidate_id,
                },
            })

        # ...and it must be a mix worth shipping. The critic measured this
        # master when it was composed and wrote down what is wrong with it;
        # finalize never read that, so a deliverable the product itself had
        # already judged truncated — or silent throughout — could still be
        # locked in and handed over. Checking a report we already have is the
        # cheapest quality gate available, and skipping it makes the critic
        # decorative.
        #
        # Refused, not forbidden: the faults are mechanical, and a user may
        # genuinely accept one. What they may not do is accept it without being
        # told, so passing acknowledge_faults says they were.
        missing = [str(layer) for layer in (mix0.get("missing_layers") or [])]
        if missing and not bool(args.get("acknowledge_faults")):
            ctx.repository.record_tool_call(
                session_id=ctx.session_id,
                tool_name=AGENT_TOOL_FINALIZE,
                status=AgenticToolStatus.FAILED,
                input_json={"blocked": "mix_missing_requested_layer"},
                output_json={"missing_layers": missing},
                finished=True,
            )
            return ToolResult(events=[], data={
                "error": "mix_missing_requested_layer",
                "missing_layers": missing,
                "instruction": (
                    "This mix is missing "
                    + " and ".join(missing)
                    + ", which the user asked for. Either that layer never "
                    "finished or it failed — say which, offer to make it, and "
                    "recompose. Composing is free. If they want the piece "
                    "without it, finalize again with acknowledge_faults=true."
                ),
            })

        # ...and it must be the master the user last heard. Every free edit
        # since this mix was composed — a re-cut, a retaken line, a redone
        # effect — changed a stem this file was built from, and nothing
        # recomposed it. Locking here hands over the pre-edit render.
        # Only a composed MASTER can predate an edit. `state["mix"]` also holds
        # music-only mix parameters (a volume nudge writes an envelope there
        # with no video), and those are not a deliverable to be stale about —
        # the same precondition the carries-this-take gate above uses.
        if (
            mix0.get("video_url")
            and not mix_still_describes(mix0, session.state_json)
            and not bool(args.get("acknowledge_faults"))
        ):
            ctx.repository.record_tool_call(
                session_id=ctx.session_id,
                tool_name=AGENT_TOOL_FINALIZE,
                status=AgenticToolStatus.FAILED,
                input_json={"blocked": "mix_predates_edit"},
                output_json={
                    "built_from": mix0.get("built_from"),
                    "stems_now": mix_stems(
                        session.state_json,
                        candidate_id=str(mix0.get("music_candidate_id") or ""),
                    ),
                },
                finished=True,
            )
            return ToolResult(events=[], data={
                "error": "mix_predates_edit",
                "instruction": (
                    "Something changed after this mix was composed — a re-cut "
                    "take, a retaken line, or a redone effect — so the file "
                    "this would lock is not the one the user last heard. "
                    "Compose again (it is free) and then finalize. If they "
                    "want the older master anyway, finalize again with "
                    "acknowledge_faults=true."
                ),
            })

        mix_report = mix0.get("listen_report") or {}
        faults = list(mix_report.get("notes") or [])
        if faults and not bool(args.get("acknowledge_faults")):
            ctx.repository.record_tool_call(
                session_id=ctx.session_id,
                tool_name=AGENT_TOOL_FINALIZE,
                status=AgenticToolStatus.FAILED,
                input_json={"blocked": "mix_has_faults"},
                output_json={"faults": faults},
                finished=True,
            )
            return ToolResult(events=[], data={
                "error": "mix_has_faults",
                "faults": faults,
                "instruction": (
                    "The composed mix has something measurably wrong with it: "
                    + "; ".join(faults)
                    + ". Tell the user plainly and offer the fix — a mix that "
                    "runs short of the video wants recomposing, not locking. "
                    "If they hear it and want it anyway, finalize again with "
                    "acknowledge_faults=true."
                ),
            })

        events = [
            ctx.event(EventType.CANDIDATE_SELECTED, {"candidate_id": candidate_id}),
            ctx.event(EventType.TOOL_STARTED, {"tool_name": AGENT_TOOL_FINALIZE}),
        ]
        ctx.transition_to(AgenticAudioSessionPhase.COMPOSING,
            selected_candidate_id=candidate_id,
        )
        final_artifact = ctx.media.compose_final_mix(selected_candidate=candidate)
        # When a multi-layer mix exists (music + voice-over composed onto the
        # original video), that composed mix IS the deliverable — not the
        # music-only candidate. Prefer it.
        mix = session.state_json.get("mix") or {}
        if mix.get("video_url"):
            final_artifact = _promote_mix_to_final(final_artifact, mix)
        # A cheap slider remux (adjust_remix) muxes the chosen music onto the
        # video and records it on the candidate. When no compose_mix video
        # exists, that remuxed MP4 IS the deliverable — promote it so the final
        # card delivers the real balanced video, not the bare music stem.
        elif not final_artifact.get("video_url") and candidate.get("remixed_video_url"):
            final_artifact = {
                **final_artifact,
                "video_url": candidate["remixed_video_url"],
                "deliverable": "remixed_candidate",
            }
        completed = bool(final_artifact.get("video_url"))
        state = dict(session.state_json)
        state["selected_candidate_id"] = candidate_id
        state["final_artifact"] = final_artifact
        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_FINALIZE,
            status=AgenticToolStatus.COMPLETED,
            input_json={"candidate": candidate},
            output_json=final_artifact,
            linked_job_id=candidate.get("linked_job_id"),
            linked_artifact_ids=[session.source_video_artifact_id],
            finished=True,
        )
        ctx.transition_to((
                AgenticAudioSessionPhase.COMPLETED
                if completed
                else AgenticAudioSessionPhase.COMPOSING
            ),
            status=(
                AgenticSessionStatus.COMPLETED if completed else AgenticSessionStatus.ACTIVE
            ),
            state_json=state,
            finished=completed,
        )
        events.extend(
            [
                ctx.event(
                    EventType.TOOL_COMPLETED,
                    {"tool_name": AGENT_TOOL_FINALIZE, "final_artifact": final_artifact},
                ),
                ctx.event(EventType.FINAL_ARTIFACT, final_artifact),
                ctx.event(
                    EventType.PHASE_CHANGED,
                    {
                        "phase": (
                            AgenticAudioSessionPhase.COMPLETED
                            if completed
                            else AgenticAudioSessionPhase.COMPOSING
                        )
                    },
                ),
            ]
        )
        return ToolResult(events=events, data=final_artifact)

    async def _finalize_voiceover_only(
        self, ctx: ToolContext, session: Any, mix: dict[str, Any]
    ) -> ToolResult:
        """Finalize a narration-only session: the composed voice-over-over-video
        mix is the deliverable (no music candidate involved)."""

        final_artifact = {
            "status": "completed",
            "video_url": mix["video_url"],
            "deliverable": "voiceover_only",
            "mix": mix,
        }
        state = dict(session.state_json)
        state["final_artifact"] = final_artifact
        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_FINALIZE,
            status=AgenticToolStatus.COMPLETED,
            input_json={"deliverable": "voiceover_only"},
            output_json=final_artifact,
            linked_artifact_ids=[session.source_video_artifact_id],
            finished=True,
        )
        ctx.transition_to(
            AgenticAudioSessionPhase.COMPLETED,
            status=AgenticSessionStatus.COMPLETED,
            state_json=state,
            finished=True,
        )
        return ToolResult(
            events=[
                ctx.event(EventType.TOOL_STARTED, {"tool_name": AGENT_TOOL_FINALIZE}),
                ctx.event(
                    EventType.TOOL_COMPLETED,
                    {"tool_name": AGENT_TOOL_FINALIZE, "final_artifact": final_artifact},
                ),
                ctx.event(EventType.FINAL_ARTIFACT, final_artifact),
                ctx.event(
                    EventType.PHASE_CHANGED,
                    {"phase": AgenticAudioSessionPhase.COMPLETED},
                ),
            ],
            data=final_artifact,
        )


class SetProductionPlanTool(Tool):
    name = AGENT_TOOL_SET_PRODUCTION_PLAN

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        """Record how the session will sequence its audio layers (Phase A).

        Light/in-process: sets ``production_plan`` (mode + requested layers) and
        scaffolds the ``layers`` map. ``music_first`` is today's behavior;
        ``full_e2e`` records intent to produce every requested layer end-to-end.
        """

        session = ctx.require_session()
        mode = str(args.get("mode") or "").strip()
        if mode not in AGENT_PLAN_MODES:
            mode = AGENT_PLAN_MODE_MUSIC_FIRST
        # SFX is now a shipped layer (plan_sfx + generate_sfx). Keep every
        # recognized layer the model requests.
        requested = [
            layer for layer in (args.get("layers") or []) if layer in AUDIO_LAYERS
        ]
        # Music is the default spine, but a voice-over-only plan legitimately omits
        # it. Resolve whether to force a music layer from the MODALITY rather than a
        # blanket default (closes UX F1, which previously re-injected music on any
        # LLM-driven plan): an explicit ``force_music`` always wins; otherwise a
        # ``music_first`` plan whose explicit layer list omits music is a deliberate
        # narration-only plan (no music), while ``full_e2e`` always keeps the spine.
        if args.get("force_music") is not None:
            force_music = bool(args.get("force_music"))
        elif (
            args.get("layers")
            and AUDIO_LAYER_MUSIC not in requested
            and mode != AGENT_PLAN_MODE_FULL_E2E
        ):
            force_music = False
        else:
            force_music = True
        if force_music or AUDIO_LAYER_MUSIC in requested:
            layers = [AUDIO_LAYER_MUSIC] + [
                layer for layer in requested if layer != AUDIO_LAYER_MUSIC
            ]
        else:
            layers = list(requested) or [AUDIO_LAYER_VOICEOVER]
        seen: set[str] = set()
        layers = [l for l in layers if not (l in seen or seen.add(l))]

        state = dict(session.state_json)
        # The user's own choice outranks the model's.
        #
        # The intent gate writes the layer set the person actually ticked and
        # marks it `source: "user"`. The model then runs its turn and often calls
        # this tool again with its own idea of the plan — which silently deleted
        # the layer the user had just asked for (tick music + sound effects, get
        # a music-only session). A model-driven call may still ADD a layer as the
        # conversation grows, but it may not take away one the user chose.
        # (Same principle as spotting.py refusing to overwrite a user-owned
        # moment.)
        prev = state.get("production_plan") or {}
        # Provenance, not a model-claimed arg: "source": "user" counts only when
        # the call actually came through a user control (the choice endpoints
        # dispatch as "user"). A model could otherwise launder its own plan as
        # the user's by writing the magic string into its args.
        by_user = ctx.invoked_by == "user" and str(args.get("source") or "") == "user"
        kept_for_user: list[str] = []
        if prev.get("source") == "user" and not by_user:
            kept = [l for l in (prev.get("layers") or []) if l in AUDIO_LAYERS]
            kept_for_user = [l for l in kept if l not in layers]
            layers = kept + [l for l in layers if l not in kept]
            mode = prev.get("mode") or mode

        production_plan = {"mode": mode, "layers": layers}
        if by_user or prev.get("source") == "user":
            production_plan["source"] = "user"
        # Idempotent short-circuit: the loop sometimes re-asserts the same plan
        # several times in one turn — don't re-record a tool call / re-emit the
        # event / rewrite state for a no-op (observed 4x in one E2E turn).
        if state.get("production_plan") == production_plan:
            return ToolResult(events=[], data=self._plan_result(
                production_plan, kept_for_user
            ))
        state["production_plan"] = production_plan
        # Scaffold the layer map without disturbing any existing layer data.
        existing_layers = dict(state.get("layers") or {})
        existing_layers.setdefault("music", existing_layers.get("music"))
        if "voiceover" in layers:
            existing_layers.setdefault("voiceover", None)
        if "sfx" in layers:
            # A dict layer (parallel to voiceover); only scaffold when absent so a
            # legacy list value or an in-progress plan is never clobbered.
            if not isinstance(existing_layers.get("sfx"), dict):
                existing_layers["sfx"] = None
        state["layers"] = existing_layers

        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_SET_PRODUCTION_PLAN,
            status=AgenticToolStatus.COMPLETED,
            input_json={"mode": mode, "layers": layers, "source": production_plan.get("source")},
            output_json=production_plan,
            finished=True,
        )
        ctx.repository.update_session(ctx.session_id, state_json=state)
        events = [ctx.event(EventType.PRODUCTION_PLAN, production_plan)]
        return ToolResult(events=events, data=self._plan_result(
            production_plan, kept_for_user
        ))

    @staticmethod
    def _plan_result(
        production_plan: dict[str, Any], kept_for_user: list[str]
    ) -> dict[str, Any]:
        """The result the model reads back. Two lessons are encoded here.

        First: when the model tried to REMOVE a layer the user picked at the
        gate, the tool keeps it — but it used to keep it SILENTLY, so the model
        believed its narration-only plan had taken effect, announced it, saw
        reality disagree on the next turn, and re-called this tool identically,
        turn after turn (a live session stalled three turns straight on exactly
        this). A refusal the caller cannot see is indistinguishable from a bug.

        Second: setting the plan is bookkeeping, not the user's request. The
        model kept ending its turn right here, announcing what it would do
        next; the result now says in words that the turn is not done.
        """

        result = dict(production_plan)
        notes: list[str] = []
        if kept_for_user:
            notes.append(
                f"KEPT {', '.join(kept_for_user)}: the user chose these layers "
                "at the gate, and a model turn may add layers but never remove "
                "the user's. Do NOT call set_production_plan again to retry — "
                "the answer will be the same. Proceed with the plan as stored; "
                "an unwanted layer simply never generates unless the user "
                "approves it, and you may say exactly that."
            )
        notes.append(
            "The plan is stored — bookkeeping only, the user's request is NOT "
            "finished. Continue THIS turn with the next concrete step (for "
            "narration: propose_script with narration_segments). Do not end "
            "the turn to announce what you will do."
        )
        result["note"] = " ".join(notes)
        return result


class ProposeScriptTool(Tool):
    name = AGENT_TOOL_PROPOSE_SCRIPT

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        """Record a voice-over script draft (free) and show it for approval.

        The script may be the agent's own draft or text the user provided; either
        way it is stored as a draft layer and surfaced with the preset voice
        options. Generation only happens later, on explicit approval.
        """

        session = ctx.require_session()
        observation = session.state_json.get("observation") or {}
        duration_s = float(observation.get("duration_s") or 0.0)

        # Video-informed narration: timed, delivery-directed segments. When given,
        # they are the source of truth and the flat script is derived from them.
        raw_segments = args.get("narration_segments")
        segments: list[dict[str, Any]] = []
        if isinstance(raw_segments, list) and raw_segments:
            for i, seg in enumerate(raw_segments, start=1):
                if not isinstance(seg, dict):
                    continue
                text = str(seg.get("text") or "").strip()
                if not text:
                    continue
                try:
                    start_s = max(0.0, float(seg.get("start_s") or 0.0))
                except (TypeError, ValueError):
                    start_s = 0.0
                if duration_s:
                    start_s = min(start_s, max(0.0, duration_s - 0.5))
                segments.append(
                    {
                        "text": text,
                        "start_s": round(start_s, 2),
                        "delivery": str(seg.get("delivery") or "").strip(),
                    }
                )
            segments.sort(key=lambda s: s["start_s"])
            # Ids follow TIMELINE order (assigned after the sort), so seg_01 is
            # always the first line the viewer hears.
            for i, seg in enumerate(segments, start=1):
                seg["id"] = f"seg_{i:02d}"

            # Every problem at once. Rejecting one thing at a time makes the
            # writer spend a step per fault — a live session burned its whole
            # turn budget on four sequential refusals and ended with no script
            # at all, having been told about each problem only after fixing the
            # last. Gates that stack must report together.
            talkover = _narration_over_source_audio(
                segments, observation=observation,
            )
            overrun_now = _narration_overrun(segments, duration_s=duration_s)
            if talkover and overrun_now:
                combined = (
                    f"{talkover['instruction']}\n\nAND SEPARATELY: "
                    f"{overrun_now['instruction']}"
                )
                ctx.repository.record_tool_call(
                    session_id=ctx.session_id,
                    tool_name=AGENT_TOOL_PROPOSE_SCRIPT,
                    status=AgenticToolStatus.FAILED,
                    input_json={"blocked": "narration_over_source_audio+too_long"},
                    output_json={"talkover": talkover, "overrun": overrun_now},
                    finished=True,
                )
                return _narration_refusal(
                    ctx,
                    error="narration_plan_rejected",
                    instruction=combined,
                    detail={"talkover": talkover, "overrun": overrun_now},
                    user_note=(
                        "the lines would speak over voices already in the "
                        "footage, and the read runs past the last frame"
                    ),
                )
            if talkover:
                # Refused, not reported. Speaking over the footage's own voice
                # is the plainest mistake this system can make, and the one it
                # was blind to until the source track was scanned at all.
                return _narration_refusal(
                    ctx,
                    error="narration_over_source_audio",
                    instruction=talkover["instruction"],
                    detail=talkover,
                    user_note="the lines land on top of voices already in the footage",
                )

            overrun = overrun_now
            if overrun:
                # Soft gate, like the SFX treatment card: a raised error would
                # fail the whole user turn, whereas instructive data steers the
                # model into rewriting before a cent of synthesis is spent.
                return _narration_refusal(
                    ctx,
                    error="narration_too_long",
                    instruction=overrun["instruction"],
                    detail=overrun,
                    user_note=(
                        "the last line is cued too late to finish before the "
                        "video ends"
                        if overrun.get("binding") == "placement"
                        else "the script is longer than the clip can carry"
                    ),
                )

        # ---- Timing is part of the draft, not an afterthought -------------
        # A plain script renders as one continuous read parked at 0:00 — on a
        # 15s clip that read spoke straight through every cut and left the
        # rest dead air (live session, 2026-08-27). On footage long enough to
        # have structure, the agent must bring TIMED segments (start_s + text
        # + delivery per line, hold_silent for beats left alone) so the read
        # lands where the director meant it. GATED, not advised: a warning
        # alone never changes agent behaviour. A user typing their own flat
        # script into the card is explicit human intent and passes.
        if (
            not segments
            and ctx.invoked_by != "user"
            and duration_s > 12.0
        ):
            instruction = (
                f"This footage runs {duration_s:.0f}s — a flat script would "
                "render as one continuous read starting at 0:00, ignoring the "
                "cuts. Re-propose with narration_segments: a list of "
                "{start_s, text, delivery} lines placed on the moments that "
                "need words, and hold_silent for the beats deliberately left "
                "to the music. Keep it sparse — coverage is not the goal."
            )
            ctx.repository.record_tool_call(
                session_id=ctx.session_id,
                tool_name=AGENT_TOOL_PROPOSE_SCRIPT,
                status=AgenticToolStatus.FAILED,
                input_json={"blocked": "narration_needs_timing"},
                output_json={"duration_s": duration_s},
                finished=True,
            )
            return ToolResult(events=[], data={
                "error": "narration_needs_timing",
                "instruction": instruction,
            })

        # Co-design, narration side: report the sound-design moments this read
        # speaks over. Not a refusal — a line can sit deliberately under an
        # effect — but it must be a decision rather than a surprise in the mix.
        sheet0 = dict(session.state_json.get("spotting_sheet") or {})

        # Restraint, recorded. Narration is not meant to cover everything, and a
        # beat left alone on purpose should be legible as a decision rather than
        # inferred from a gap — which is also what lets the other layers know to
        # stay off it.
        held: list[dict[str, Any]] = []
        for entry in args.get("hold_silent") or []:
            if isinstance(entry, str):
                entry = {"moment_id": entry}
            if not isinstance(entry, dict):
                continue
            updated = set_moment_owner(
                sheet0,
                str(entry.get("moment_id") or ""),
                OWNER_SILENCE,
                source="narration",
                reason=str(entry.get("reason") or "").strip(),
            )
            if updated:
                held.append({"moment_id": updated.get("id"),
                             "t": updated.get("t"),
                             "reason": updated.get("reason", "")})

        # AFTER the holds are applied, not before. A live run declared the final
        # shot silent and then wrote a line running straight into it — and the
        # check missed it, because when it ran that moment was still owned by
        # narration. Declaring a beat silent and speaking over it in the same
        # breath is exactly the self-contradiction this layer exists to catch.
        vo_collisions = narration_conflicts(sheet0, segments)

        script = str(args.get("script") or "").strip()
        if segments:
            script = " ".join(s["text"] for s in segments)
        if not script:
            raise ValueError("propose_script requires a non-empty script.")
        # Cap unbounded script input before it reaches TTS (adversarial review P3).
        if len(script) > MAX_MESSAGE_CHARS:
            raise ValueError(
                f"Voice-over script is too long (max {MAX_MESSAGE_CHARS} characters)."
            )
        voice_id = str(args.get("voice_id") or DEFAULT_VOICE_ID)
        language = str(
            args.get("language") or observation.get("detected_language") or ""
        )
        tone = str(args.get("tone") or "").strip()
        voice_rationale = str(args.get("voice_rationale") or "").strip()

        state = dict(session.state_json)
        layers = dict(state.get("layers") or {})
        voiceover = dict(layers.get("voiceover") or {})
        voiceover.update(
            {
                "script": script,
                "voice_id": voice_id,
                "language": language,
                "tone": tone or voiceover.get("tone", ""),
                "status": "draft",
            }
        )
        # Segments REPLACE any prior plan when provided; an explicit flat script
        # (no segments) clears them so the render matches what's shown.
        if segments:
            voiceover["segments"] = segments
        elif args.get("script"):
            voiceover.pop("segments", None)
        if voice_rationale:
            voiceover["voice_rationale"] = voice_rationale
        voiceover["collisions"] = vo_collisions
        voiceover["held_silent"] = held
        # The roster travels WITH the layer from the DRAFT onward. It used to be
        # attached only by post-render hydration, so the picker a user chose from
        # BEFORE the first render fell back to a stale hardcoded list in the
        # console: the director cast a voice that was not in that list, the
        # select quietly fell to its first option, and the paid render spoke in a
        # voice neither the user nor the director had chosen — while the card
        # still showed the rationale for the discarded one.
        voiceover["voice_options"] = [
            {k: preset[k] for k in ("id", "name", "gender", "style")}
            for preset in VOICE_CATALOG
        ]
        layers["voiceover"] = voiceover
        if held:
            state["spotting_sheet"] = sheet0
        state["layers"] = layers
        # A draft got through: the gate's patience resets with it.
        state["narration_gate_attempts"] = 0
        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_PROPOSE_SCRIPT,
            status=AgenticToolStatus.COMPLETED,
            input_json={"voice_id": voice_id, "language": language},
            output_json={"script": script},
            finished=True,
        )
        ctx.repository.update_session(ctx.session_id, state_json=state)
        card = {
            "script": script,
            "voice_id": voice_id,
            "language": language,
            "tone": voiceover.get("tone", ""),
            "segments": voiceover.get("segments") or [],
            "voice_rationale": voiceover.get("voice_rationale", ""),
            "collisions": vo_collisions,
            "voice_options": voiceover["voice_options"],
        }
        events = [ctx.event(EventType.VOICEOVER_SCRIPT, card)]
        return ToolResult(events=events, data=card)


class GenerateVoiceoverTool(Tool):
    name = AGENT_TOOL_GENERATE_VOICEOVER

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        """Generate the voice-over (TTS) — gated behind an approved script.

        Structural cost gate (invariant #2): refuses unless a script has been
        drafted via propose_script and shown to the user first.
        """

        session = ctx.require_session()
        layers = dict(session.state_json.get("layers") or {})
        observation = session.state_json.get("observation") or {}
        voiceover = dict(layers.get("voiceover") or {})
        script = str(voiceover.get("script") or "").strip()
        if not script:
            raise ApprovalRequiredError(
                "Draft and confirm a voice-over script (propose_script) before "
                "generating the narration."
            )
        if ctx.invoked_by != "user":
            # "A script exists" is a precondition the agent satisfies by
            # itself — it drafted one and spent on it in the same turn, and a
            # user watched a paid render they never asked for. Recording money
            # moves only on the user's own click: the voice-over card's
            # Generate button (the choice endpoint), which is the one caller
            # that dispatches this tool as "user".
            raise ApprovalRequiredError(
                "generate_voiceover only runs from the user's own click on the "
                "voice-over card's Generate button. Present the draft and stop; "
                "do not call this tool yourself.",
                user_message=(
                    "The narration draft is ready on the voice-over card — "
                    "recording starts when you press Generate there. I won't "
                    "spend on the read without that click."
                ),
            )
        voice_id = str(args.get("voice_id") or voiceover.get("voice_id") or DEFAULT_VOICE_ID)
        language = str(args.get("language") or voiceover.get("language") or "")
        speed = float(args.get("speed") or 1.0)
        # Tone is adjustable: a new tone re-records the narration with new delivery.
        tone = str(args.get("tone") or voiceover.get("tone") or "").strip()

        # A retake names ONE line. The others are kept exactly as recorded —
        # which is the whole point: changing a word in line three should not
        # re-record, re-pay for, and subtly re-perform the other nine. A line
        # whose TEXT has changed is never kept, however it was asked for; the
        # user editing the words is precisely when a new recording is owed.
        retake_id = str(args.get("segment_id") or "").strip()
        reuse_segment_audio: dict[str, str] = {}
        if retake_id:
            for seg in (voiceover.get("segments") or []):
                if not isinstance(seg, dict):
                    continue
                seg_id = str(seg.get("id") or "")
                if not seg_id or seg_id == retake_id:
                    continue
                recorded = seg.get("audio_path")
                if recorded and str(seg.get("text") or "") == str(
                    (seg.get("rendered_text") or seg.get("text")) or ""
                ):
                    reuse_segment_audio[seg_id] = str(recorded)

        events = [
            ctx.event(EventType.TOOL_STARTED, {"tool_name": AGENT_TOOL_GENERATE_VOICEOVER})
        ]
        linked_job_id = ctx.media.enqueue_voiceover(
            reuse_segment_audio=reuse_segment_audio,
            session_id=ctx.session_id,
            source_video_artifact_id=session.source_video_artifact_id,
            script=script,
            voice_id=voice_id,
            language=language,
            speed=speed,
            tone=tone,
            # Intent only. The renderer writes REALIZED values (its chosen
            # per-line speed, measured duration, cut flags) back onto the
            # layer's segments; passing those into the next render made every
            # re-record START at the last render's sped-up values — the ladder
            # never ran again and the rush was inherited forever. A listener
            # heard it as "way too fast" on a read whose delivery said
            # "controlled".
            segments=[
                {k: seg.get(k) for k in ("id", "text", "start_s", "delivery")
                 if seg.get(k) is not None}
                for seg in (voiceover.get("segments") or [])
                if isinstance(seg, dict)
            ],
            # The picture the plan is timed against. Only trustworthy cuts go
            # through: the fallback detector invents them on fast motion, and an
            # invented cut would become a hard placement constraint.
            video_duration_s=float(observation.get("duration_s") or 0.0),
            cuts=(list(observation.get("cuts") or [])
                  if observation.get("cut_source") == "pyscenedetect" else []),
            speech_windows=list(observation.get("speech_windows") or []),
            creator_user_id=session.creator_user_id,
            actor_user_id=ctx.actor_user_id,
        )
        voiceover.update(
            {
                "voice_id": voice_id,
                "language": language,
                "speed": speed,
                "tone": tone,
                "linked_job_id": linked_job_id,
                "status": "queued" if linked_job_id else "planned",
            }
        )
        layers["voiceover"] = voiceover
        state = dict(session.state_json)
        state["layers"] = layers
        linked_job_ids = list(session.linked_job_ids or [])
        if linked_job_id:
            linked_job_ids.append(str(linked_job_id))
        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_GENERATE_VOICEOVER,
            status=AgenticToolStatus.COMPLETED,
            input_json={"voice_id": voice_id, "language": language, "speed": speed},
            output_json={"linked_job_id": linked_job_id},
            linked_job_id=linked_job_id,
            finished=True,
        )
        ctx.repository.update_session(
            ctx.session_id, linked_job_ids=linked_job_ids, state_json=state
        )
        events.extend(
            [
                ctx.event(
                    EventType.TOOL_COMPLETED,
                    {"tool_name": AGENT_TOOL_GENERATE_VOICEOVER, "voiceover": voiceover},
                ),
                ctx.event(EventType.VOICEOVER_GENERATING, voiceover),
            ]
        )
        return ToolResult(events=events, data=voiceover)


def _sfx_event_id(index: int) -> str:
    return f"sfx_event_{index:03d}"


def _describes_the_same_effect(
    plan_row: dict[str, Any], rendered: dict[str, Any]
) -> bool:
    """Is the moment under this id still the moment that sound was made for?

    Event ids are positional (``sfx_event_003``), so deleting or reordering a
    row silently moves every id after it onto a different moment. Reuse is
    keyed by id — which means an edited plan can staple one moment's sound onto
    another's, at full confidence, with nothing to see.

    Identity is the TEXT, not the timing. Moving a hit later is exactly the
    case where the sound should be reused rather than paid for again; changing
    what it should sound like is exactly the case where it should not be.

    Length is part of the identity too: the effect is generated AT a duration,
    so a plan edit that lengthens a hit and reuses the old audio delivers the
    old short clip followed by silence, flagged nowhere. The manifest records
    the duration the plan REQUESTED (verbatim, ``None`` included) rather than
    the one the render clamped to the picture — comparing against the clamped
    value would regenerate every tail-of-video effect on every redo, forever.

    Fails closed. A manifest written before the prompt or the requested
    duration was recorded cannot be checked, so its effects are generated
    again rather than guessed at.
    """
    from .sfx_render import requested_duration

    if "prompt" not in rendered or "requested_duration_s" not in rendered:
        return False
    label = str(plan_row.get("label") or "").strip()
    prompt = str(plan_row.get("prompt") or label).strip()
    if (
        str(rendered.get("label") or "").strip() != label
        or str(rendered.get("prompt") or "").strip() != prompt
    ):
        return False
    wanted = requested_duration(plan_row)
    recorded = rendered.get("requested_duration_s")
    if (wanted is None) != (recorded is None):
        return False
    if wanted is not None and abs(wanted - float(recorded)) > 0.01:
        return False
    return True


def reusable_event_audio(
    *,
    previous_variant: Optional[dict[str, Any]],
    plan_rows: list[dict[str, Any]],
    redo: set[str],
) -> dict[str, str]:
    """Effects to carry forward from the last take, by id.

    Every effect is its own paid generation, so re-rendering a bed of twelve to
    fix one charges for eleven sounds the user was happy with and returns
    subtly different ones. What is carried forward is everything that was not
    named for a redo AND still describes the same effect.
    """
    plan_by_id = {
        str(row.get("id") or ""): row
        for row in (plan_rows or [])
        if isinstance(row, dict)
    }
    carried: dict[str, str] = {}
    for rendered in ((previous_variant or {}).get("rendered_events") or []):
        if not isinstance(rendered, dict):
            continue
        event_id = str(rendered.get("id") or "")
        # A servable URL when the manifest has one (results persist those now
        # — they survive the container that rendered them), else the local
        # path older manifests recorded. The renderer resolves either back
        # into bytes where it runs.
        audio_ref = str(rendered.get("audio_url") or rendered.get("audio_path") or "")
        if not (event_id and audio_ref) or event_id in redo:
            continue
        plan_row = plan_by_id.get(event_id)
        if plan_row is None or not _describes_the_same_effect(plan_row, rendered):
            continue
        carried[event_id] = audio_ref
    return carried


class PlanSfxTool(Tool):
    name = AGENT_TOOL_PLAN_SFX

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        """Record a spotted sound-effect plan (free, editable) for approval.

        The agent spots the moments (a whoosh on a cut, an impact on the final
        frame) from the observed scenes; nothing is generated yet. Mirrors
        propose_script — the editable plan is shown, generation happens later on
        explicit approval. Existing variants are preserved (re-planning keeps the
        renders already made).
        """

        session = ctx.require_session()
        state = dict(session.state_json)

        # Treatment gate: "add sound effects" underdetermines register and
        # density — planning before the user answered the one treatment card
        # is how obtrusive SFX happens. The card answer lands in
        # state.sfx_treatment (see handle_choice); a `treatment` arg is the
        # explicit bypass for when the user's own message already specified
        # the effects (it is persisted, so the gate opens durably).
        #
        # Provenance discipline: a CARD-sourced answer is a deterministic user
        # choice. A model-authored inline arg can NEVER silently rewrite it as
        # if the user said so — when the gate is closed it lands as an honest
        # "revised" record that preserves what it replaced (the user's typed
        # pivot is the legitimate reason this path exists; a habitual overwrite
        # stays visible on the plan card instead of forging provenance).
        treatment = state.get("sfx_treatment")
        arg_treatment = args.get("treatment")
        if isinstance(arg_treatment, dict) and any(
            str(arg_treatment.get(k) or "").strip() for k in ("label", "register", "notes")
        ):
            register = str(arg_treatment.get("register") or "").strip()
            notes = str(arg_treatment.get("notes") or "").strip()
            label = str(arg_treatment.get("label") or "").strip() or (
                register or notes[:40] or "Custom treatment"
            )
            if not treatment:
                treatment = {
                    "label": label,
                    "register": register,
                    "notes": notes,
                    "source": "user_message",
                }
                state["sfx_treatment"] = treatment
            elif label != str(treatment.get("label") or ""):
                treatment = {
                    "label": label,
                    "register": register,
                    "notes": notes,
                    "source": "revised",
                    "revised_from": {
                        "label": treatment.get("label"),
                        "source": treatment.get("source"),
                    },
                }
                state["sfx_treatment"] = treatment
        if not treatment:
            # Soft gate: no exception (a hard error would fail the whole user
            # turn) — instructive data steers the model back to the card.
            ctx.repository.record_tool_call(
                session_id=ctx.session_id,
                tool_name=AGENT_TOOL_PLAN_SFX,
                status=AgenticToolStatus.FAILED,
                input_json={"blocked": "sfx_treatment_required"},
                output_json={},
                finished=True,
            )
            return ToolResult(
                events=[],
                data={
                    "error": "sfx_treatment_required",
                    "instruction": (
                        "No SFX plan was recorded. Ask the ONE treatment card "
                        'first (action clarify, topic "sfx_treatment": 2-4 '
                        "footage-grounded options fusing style register + "
                        "density, your pick marked recommended) — or pass "
                        "treatment={label, register, notes} when the user's "
                        "message already specified exactly what they want."
                    ),
                },
            )

        raw_events = args.get("sfx_events") or []
        if not isinstance(raw_events, list):
            raise ValueError("plan_sfx requires sfx_events to be a list.")
        obs = state.get("observation") or {}
        duration = float(obs.get("duration_s") or 0.0)
        events_plan: list[dict[str, Any]] = []
        for i, ev in enumerate(raw_events, start=1):
            if not isinstance(ev, dict):
                continue
            label = str(ev.get("label") or f"Effect {i}").strip()
            prompt = str(ev.get("prompt") or label).strip()
            try:
                start_s = max(0.0, float(ev.get("start_s") or 0.0))
            except (TypeError, ValueError):
                start_s = 0.0
            if duration:
                start_s = min(start_s, duration)
            event_plan = {
                "id": _sfx_event_id(i), "label": label, "prompt": prompt, "start_s": start_s
            }
            # A planned row is a moment, but how LONG the effect runs decides
            # whether it reads as a hit or a smear — and the renderer's own floor
            # is unusably short. Carry a stated duration through when there is
            # one; the planned render defaults the rest.
            for key in ("duration_s", "duration"):
                if ev.get(key) is not None:
                    try:
                        event_plan["duration_s"] = max(0.05, float(ev[key]))
                    except (TypeError, ValueError):
                        pass
                    break
            event_type = str(ev.get("event_type") or "").strip()
            if event_type:
                event_plan["event_type"] = event_type
            # Rows a person placed are not re-snapped to nearby motion; an
            # accepted transition suggestion says so and keeps its snap.
            authority = str(ev.get("timing_authority") or "").strip()
            if authority:
                event_plan["timing_authority"] = authority
            reason = str(ev.get("reason") or "").strip()
            if reason:
                event_plan["reason"] = reason
            events_plan.append(event_plan)
        prev_layers = state.get("layers") or {}
        prev_sfx = prev_layers.get("sfx") if isinstance(prev_layers.get("sfx"), dict) else {}
        ambience = str(
            args.get("sfx_ambience") or (prev_sfx or {}).get("ambience") or ""
        ).strip()
        # An ambience-only treatment is a legitimate plan with ZERO discrete
        # events — the continuous bed IS the sound design. Only an empty plan
        # with no bed either is an error.
        if not events_plan and not ambience:
            raise ValueError(
                "plan_sfx requires at least one sfx event, or sfx_ambience for "
                "an ambience-only plan."
            )

        # Density budget (soft, legible): ~1 discrete effect per 5s. The plan is
        # not trimmed — the cap is surfaced on the card so exceeding it is a
        # visible, user-approvable choice ("say 'denser'"), never a silent one.
        # Unknown duration gets a conservative default cap, never a cap fitted
        # to whatever was submitted (that would disable over_budget entirely).
        density_cap = max(1, round(duration / 5.0)) if duration else 3
        cap_note = (
            f"Capped at {density_cap} effect{'s' if density_cap != 1 else ''} "
            + (f"for {round(duration)}s" if duration else "(duration unknown)")
            + " — say 'denser' to override."
        )
        if not events_plan:
            cap_note = ""

        # Planning within a treatment settles the outstanding card, however the
        # gate was opened — a still-live treatment card over a recorded plan
        # invites a contradictory late tap (and confuses the state summary).
        pending = state.get("pending_clarification")
        if isinstance(pending, dict) and pending.get("topic") == "sfx_treatment":
            state["pending_clarification"] = None

        layers = dict(state.get("layers") or {})
        prev = layers.get("sfx")
        sfx = dict(prev) if isinstance(prev, dict) else {}
        sfx.update(
            {
                "status": "draft",
                "summary": str(args.get("sfx_summary") or sfx.get("summary") or "").strip(),
                "ambience": ambience,
                "events": events_plan,
                "variants": sfx.get("variants") or [],
                "selected_variant_id": sfx.get("selected_variant_id"),
                "treatment": treatment,
                # How this plan wants to be MADE: watched, or written from its
                # prompts. Part of the plan because it changes what the take
                # will be, the same way the treatment does — and because the
                # agent should be answering it rather than discovering it.
                "route": str(args.get("sfx_route") or sfx.get("route") or "auto"),
                "density_cap": density_cap,
                "cap_note": cap_note,
                "over_budget": len(events_plan) > density_cap,
            }
        )
        # Co-design: an accent landing under a spoken line either fights the
        # voice or disappears, and the mix cannot fix it afterwards.
        prev_vo = prev_layers.get("voiceover") if isinstance(prev_layers.get("voiceover"), dict) else {}
        vo_segments = (prev_vo or {}).get("segments") or []
        sheet = state.get("spotting_sheet") or {}
        collisions = sfx_conflicts(sheet, events_plan, vo_segments)
        if collisions and not args.get("allow_overlap"):
            # Reporting the clash was not enough on its own: a live session
            # printed the warning and spotted the effects there anyway. So this
            # gates, like the treatment card — soft, with the free stretches
            # included, because a planner told only "that's wrong" moves the
            # effect somewhere equally wrong.
            free = free_windows(sheet, vo_segments, duration_s=duration)
            listed = "; ".join(
                f"{c['event']} at {c['start_s']}s ({c['reason']})" for c in collisions
            )
            windows = ", ".join(f"{lo}-{hi}s" for lo, hi in free) or "none"
            ctx.repository.record_tool_call(
                session_id=ctx.session_id,
                tool_name=AGENT_TOOL_PLAN_SFX,
                status=AgenticToolStatus.FAILED,
                input_json={"blocked": "sfx_collides_with_narration"},
                output_json={"collisions": collisions, "free_windows": free},
                finished=True,
            )
            return ToolResult(events=[], data={
                "error": "sfx_collides_with_narration",
                "collisions": collisions,
                "free_windows": free,
                "instruction": (
                    f"These effects clash with the narration: {listed}. An accent "
                    f"under a spoken line either fights the voice or is lost, and "
                    f"the mix cannot repair it. Narration is clear during: "
                    f"{windows} — move each clashing effect into one of those "
                    f"stretches (the nearest cut inside one is usually right), or "
                    f"drop it. If the user genuinely wants an effect underneath "
                    f"the voice, call plan_sfx again with allow_overlap=true and "
                    f"say so out loud."
                ),
            })
        sfx["collisions"] = collisions
        sfx["collision_note"] = (
            f"{len(collisions)} of {len(events_plan)} effects sit under narration "
            f"deliberately." if collisions else ""
        )
        layers["sfx"] = sfx
        state["layers"] = layers
        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_PLAN_SFX,
            status=AgenticToolStatus.COMPLETED,
            input_json={"event_count": len(events_plan)},
            output_json={"summary": sfx.get("summary", "")},
            finished=True,
        )
        ctx.repository.update_session(ctx.session_id, state_json=state)
        events = [ctx.event(EventType.SFX_PLAN, sfx)]
        return ToolResult(events=events, data=sfx)


class GenerateSfxTool(Tool):
    name = AGENT_TOOL_GENERATE_SFX

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        """Render a SFX variant from the spotted plan — gated behind a plan.

        Structural cost gate (invariant #2): refuses unless plan_sfx has spotted
        at least one event first. Each call appends a NEW variant (so the user
        can A/B a couple of takes) and selects it optimistically.
        """

        session = ctx.require_session()
        layers = dict(session.state_json.get("layers") or {})
        prev = layers.get("sfx")
        sfx = dict(prev) if isinstance(prev, dict) else {}
        events_plan = sfx.get("events") or []
        # An ambience-only plan (zero discrete events, a continuous bed) is a
        # legitimate render target — only a plan with neither is blocked.
        if not events_plan and not str(sfx.get("ambience") or "").strip():
            raise ApprovalRequiredError(
                "Spot the sound moments (plan_sfx) before generating the effects."
            )
        variants = list(sfx.get("variants") or [])
        variant_index = len(variants) + 1
        variant_id = f"sfx_variant_{variant_index}"

        # Redo only the hits that were wrong. Every effect is its own paid
        # generation, so re-rendering a bed of twelve to fix one costs eleven
        # sounds the user was happy with — and returns subtly different ones.
        # The effects to redo are named; everything else is kept exactly.
        redo = {
            str(event_id) for event_id in (args.get("event_ids") or []) if event_id
        }
        reuse_event_audio: dict[str, str] = {}
        if redo:
            selected = str(sfx.get("selected_variant_id") or "")
            previous = next(
                (v for v in variants if str(v.get("variant_id")) == selected),
                variants[-1] if variants else None,
            )
            reuse_event_audio = reusable_event_audio(
                previous_variant=previous, plan_rows=events_plan, redo=redo
            )

        events = [ctx.event(EventType.TOOL_STARTED, {"tool_name": AGENT_TOOL_GENERATE_SFX})]
        linked_job_id = ctx.media.enqueue_sfx(
            reuse_event_audio=reuse_event_audio,
            session_id=ctx.session_id,
            source_video_artifact_id=session.source_video_artifact_id,
            events=events_plan,
            summary=str(sfx.get("summary") or ""),
            ambience=str(sfx.get("ambience") or ""),
            variant_id=variant_id,
            creator_user_id=session.creator_user_id,
            actor_user_id=ctx.actor_user_id,
            # The take's own route: what this call asked for, else what the
            # plan settled on, else let the render decide from what it can
            # reach. Recorded either way (see the plan's sfx_route).
            sfx_route=str(
                args.get("sfx_route") or sfx.get("route") or "auto"
            ),
        )
        variant = {
            "variant_id": variant_id,
            "label": f"SFX take {variant_index}",
            "status": "queued" if linked_job_id else "planned",
            "linked_job_id": linked_job_id,
            "audio_url": None,
            "video_url": None,
        }
        variants.append(variant)
        sfx.update(
            {
                "status": "queued" if linked_job_id else "planned",
                "variants": variants,
            }
        )
        # NOT selected here. A variant was marked chosen the moment it was
        # enqueued — before it rendered, before anyone could hear it — so the
        # card showed a pick the user had not made, and a compose running
        # before the render landed would have mixed a take with no audio. The
        # first one to FINISH becomes the working choice (below, on hydrate);
        # after that it takes a person to change it.
        layers["sfx"] = sfx
        state = dict(session.state_json)
        state["layers"] = layers
        linked_job_ids = list(session.linked_job_ids or [])
        if linked_job_id:
            linked_job_ids.append(str(linked_job_id))
        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_GENERATE_SFX,
            status=AgenticToolStatus.COMPLETED,
            input_json={"variant_id": variant_id, "event_count": len(events_plan)},
            output_json={"linked_job_id": linked_job_id},
            linked_job_id=linked_job_id,
            finished=True,
        )
        ctx.repository.update_session(
            ctx.session_id, linked_job_ids=linked_job_ids, state_json=state
        )
        events.extend(
            [
                ctx.event(
                    EventType.TOOL_COMPLETED,
                    {"tool_name": AGENT_TOOL_GENERATE_SFX, "sfx": sfx},
                ),
                ctx.event(EventType.SFX_GENERATING, sfx),
            ]
        )
        return ToolResult(events=events, data=sfx)


class ComposeMixTool(Tool):
    name = AGENT_TOOL_COMPOSE_MIX

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        """Layer music + voice-over onto the original video (cheap, re-runnable).

        Adjustable in place: the music/voice-over volume ratio, ducking depth, and
        where the narration starts. Parameters merge with the prior mix so the
        user can tweak one knob at a time. No generation, no cost.
        """

        session = ctx.require_session()
        state0 = session.state_json or {}
        candidates = list(state0.get("candidates") or [])
        try:
            candidate, candidate_id = ctx.resolve_candidate(session, candidates, args)
        except KeyError:
            candidate, candidate_id = None, None
        # A take whose window the user MOVED is that whole take, seeked into —
        # not the original cut. Every renderer has to agree on that, so the
        # question is asked in exactly one place.
        music_audio_url, music_start_s = (
            candidate_music_source(candidate) if candidate is not None else (None, 0.0)
        )
        # A session that HAS rendered music must never compose a mix without it.
        # Swallowing the resolution failure — two finished takes, none locked,
        # no candidate_id passed — composed a music-less "final mix" that
        # finalize then shipped: measured 45-53 dB under the music stem on the
        # live deployment (2026-08-30), and the only way a user found out was by
        # listening to the file. A take we cannot identify is a question to ask,
        # never a silence to ship.
        rendered = [
            c for c in candidates
            if isinstance(c, dict) and (c.get("audio_url") or c.get("complete_audio_url"))
        ]
        if rendered and not music_audio_url:
            # Instructive data, not a raise: a raise fails the whole user turn,
            # while the gate shape the narration and treatment paths already use
            # lets the model name the take and compose in the SAME turn.
            ids = [str(c.get("candidate_id")) for c in rendered]
            ctx.repository.record_tool_call(
                session_id=ctx.session_id,
                tool_name=AGENT_TOOL_COMPOSE_MIX,
                status=AgenticToolStatus.FAILED,
                input_json={"blocked": "music_candidate_unresolved"},
                output_json={"candidate_ids": ids},
                finished=True,
            )
            return ToolResult(events=[], data={
                "error": "music_candidate_unresolved",
                "instruction": (
                    f"This session has {len(ids)} rendered take(s) and none is "
                    "locked, so the mix would carry no music at all. Pass "
                    f"candidate_id (one of: {', '.join(ids)}), or lock a take "
                    "first, then compose."
                ),
                "detail": {"candidate_ids": ids},
            })
        voiceover = dict((state0.get("layers") or {}).get("voiceover") or {})
        voiceover_audio_url = voiceover.get("audio_url")
        # Segmented narration bakes its own offsets into the track, so the mix
        # must not shift it again.
        vo_has_segments = bool(voiceover.get("segments"))
        # The REALIZED line windows let the music duck per line and come back up
        # between them, instead of staying flattened across every pause. Only
        # rendered segments carry a measured duration; a still-planned one would
        # duck the wrong stretch, so it is skipped rather than guessed at.
        vo_windows: list[tuple[float, float]] = []
        for seg in voiceover.get("segments") or []:
            if not isinstance(seg, dict) or seg.get("duration_s") in (None, ""):
                continue
            try:
                start = max(0.0, float(seg.get("start_s") or 0.0))
                dur = max(0.0, float(seg["duration_s"]))
            except (TypeError, ValueError):
                continue
            if dur > 0:
                vo_windows.append((start, start + dur))

        # SFX layer: the selected (else latest completed) variant's rendered
        # effects bed joins the master mix.
        sfx_layer = (state0.get("layers") or {}).get("sfx")
        sfx_audio_url = None
        if isinstance(sfx_layer, dict):
            variants = [v for v in (sfx_layer.get("variants") or [])
                        if isinstance(v, dict) and v.get("status") == "completed" and v.get("audio_url")]
            chosen = next((v for v in variants
                           if v.get("variant_id") == sfx_layer.get("selected_variant_id")), None)
            chosen = chosen or (variants[-1] if variants else None)
            if chosen:
                sfx_audio_url = chosen.get("audio_url")

        prev = dict(state0.get("mix") or {})

        def _pick(key: str, default: Any) -> Any:
            if args.get(key) is not None:
                return args.get(key)
            return prev.get(key, default)

        # A fade list merges the way every other mix knob does: passing one
        # replaces it, passing none keeps what the mix already had. Clearing
        # them is an explicit empty list, which is why presence is what counts
        # rather than truthiness.
        prior_mix = state0.get("mix") or {}
        if "music_envelope" in (args or {}):
            music_envelope = normalize_music_envelope(args.get("music_envelope"))
        else:
            music_envelope = normalize_music_envelope(prior_mix.get("music_envelope"))
        music_volume = _coerce_mix_param("music_volume", _pick("music_volume", None), state0.get("last_mix_volume") or 0.85)
        voiceover_volume = _coerce_mix_param("voiceover_volume", _pick("voiceover_volume", None), 1.0)
        voiceover_start_s = _coerce_mix_param("voiceover_start_s", _pick("voiceover_start_s", None), 0.0)
        if vo_has_segments:
            voiceover_start_s = 0.0
        duck_gain_db = _coerce_mix_param("duck_gain_db", _pick("duck_gain_db", None), -9.0)
        sfx_volume = _coerce_mix_param("sfx_volume", _pick("sfx_volume", None), 1.0)
        preserve = bool(_pick("preserve_original_audio", False))

        events = [ctx.event(EventType.TOOL_STARTED, {"tool_name": AGENT_TOOL_COMPOSE_MIX})]
        result = await ctx.media.compose_mix(
            source_video_artifact_id=session.source_video_artifact_id,
            music_audio_url=music_audio_url,
            voiceover_audio_url=voiceover_audio_url,
            sfx_audio_url=sfx_audio_url,
            music_volume=music_volume,
            voiceover_volume=voiceover_volume,
            voiceover_start_s=voiceover_start_s,
            duck_gain_db=duck_gain_db,
            sfx_volume=sfx_volume,
            voiceover_segments=vo_windows or None,
            music_start_s=music_start_s,
            preserve_original_audio=preserve,
            music_envelope=music_envelope,
        )

        mix = {
            "music_candidate_id": candidate_id,
            # WHAT THIS MASTER WAS BUILT FROM. Provenance used to stop at the
            # stem: a take knew whether its own measurements were stale, the
            # master built from it did not. So compose -> free re-cut ->
            # finalize locked the pre-cut file while the mix's listen report
            # honestly described something the user was no longer looking at.
            # A one-line narration retake and a one-effect redo reached the
            # same place through their own doors.
            "built_from": mix_stems(state0, candidate_id=candidate_id),
            "music_volume": result.get("music_volume", music_volume),
            "voiceover_volume": result.get("voiceover_volume", voiceover_volume),
            "voiceover_start_s": result.get("voiceover_start_s", voiceover_start_s),
            "duck_gain_db": result.get("duck_gain_db", duck_gain_db),
            "sfx_volume": result.get("sfx_volume", sfx_volume),
            "sfx_included": bool(sfx_audio_url),
            # Layers the user ASKED for that are not in this master. Composing
            # while narration still renders is legitimate, so this is recorded
            # rather than refused here — but it has to be recorded, because a
            # mix missing a layer somebody asked for looks exactly like a mix
            # that is finished, and finalize is where that stops being
            # recoverable.
            "missing_layers": [
                layer for layer in
                ((state0.get("production_plan") or {}).get("layers") or [])
                if (layer == AUDIO_LAYER_VOICEOVER and not voiceover_audio_url)
                or (layer == AUDIO_LAYER_SFX and not sfx_audio_url)
                or (layer == AUDIO_LAYER_MUSIC and not music_audio_url)
            ],
            # The moves the user placed, on the mix rather than in params:
            # they are moments, not a knob, and the card renders them.
            "music_envelope": [
                {"start_s": start, "end_s": end, "gain_db": gain}
                for start, end, gain in music_envelope
            ],
            "preserve_original_audio": result.get("preserve_original_audio", preserve),
            "status": result.get("status"),
            "video_url": result.get("video_url"),
            "message": result.get("message"),
        }
        # Judge the deliverable. Every layer has had a critic since the listen-
        # back work; the artifact the user actually downloads and shares had
        # none, which is the same verification bias one level up. Measured on
        # the render path, judged here from those measurements.
        signals = result.get("mix_signals")
        if signals:
            mix["mix_signals"] = signals
            report = mix_alignment(
                signals,
                observation=state0.get("observation") or {},
                preserve_original_audio=bool(mix["preserve_original_audio"]),
            )
            if report:
                mix["listen_report"] = report
        state = dict(ctx.require_session().state_json)
        state["mix"] = mix
        state["last_mix_volume"] = mix["music_volume"]
        # A finalized session that is re-mixed must hand out the NEW file.
        # Export, the gallery and the share sheet all read final_artifact, so
        # leaving it pointing at the previous render is how a user who noticed
        # the fault and fixed it still shipped the broken one. Only refresh when
        # this mix carries the take that was locked — recomposing around a
        # different take is exploration, and finalize owns that decision.
        existing_final = dict(state.get("final_artifact") or {})
        if (
            # ANY existing final, not only one that already has a video: the
            # model often locks before it composes, and that finalize produces a
            # music-only artifact. Requiring a video here left the deliverable
            # pointing at the bare stem while a real composed mix sat beside it —
            # the same disagreement this refresh exists to prevent, reached from
            # the other direction (found driving the live server, 2026-08-30).
            existing_final
            and mix.get("video_url")
            and str(mix.get("music_candidate_id") or "")
            == str(state.get("selected_candidate_id") or "")
        ):
            state["final_artifact"] = _promote_mix_to_final(existing_final, mix)
        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_COMPOSE_MIX,
            status=AgenticToolStatus.COMPLETED,
            input_json={k: mix[k] for k in (
                "music_volume", "voiceover_volume", "voiceover_start_s", "duck_gain_db"
            )},
            output_json=result,
            linked_artifact_ids=[session.source_video_artifact_id],
            finished=True,
        )
        ctx.repository.update_session(ctx.session_id, state_json=state)
        events.extend(
            [
                ctx.event(
                    EventType.TOOL_COMPLETED, {"tool_name": AGENT_TOOL_COMPOSE_MIX, "mix": mix}
                ),
                ctx.event(EventType.MIX_UPDATED, mix),
            ]
        )
        return ToolResult(events=events, data=mix)


class AdjustRemixTool(Tool):
    name = AGENT_TOOL_ADJUST_REMIX

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        session = ctx.require_session()
        candidates = list(session.state_json.get("candidates") or [])
        try:
            candidate, candidate_id = ctx.resolve_candidate(session, candidates, args)
        except KeyError:
            # No explicit target and nothing selected yet (e.g. several candidates
            # in review). Rather than dead-ending with "no music track", fall back
            # to the most recent rendered candidate so the mix tweak still applies.
            rendered = [
                c
                for c in candidates
                if c.get("status") == "completed" and (c.get("audio_url") or c.get("video_url"))
            ]
            if not rendered:
                return ctx.no_target_message("adjust the mix on")
            candidate = dict(rendered[-1])
            candidate_id = str(candidate.get("candidate_id"))

        music_volume = _coerce_mix_param(
            "music_volume", args.get("music_volume"), candidate.get("music_volume", 0.85)
        )
        preserve_original_audio = bool(
            args.get(
                "preserve_original_audio",
                candidate.get("preserve_original_audio", False),
            )
        )

        # A music-only session should not have to add narration to get a fade.
        prior_mix = session.state_json.get("mix") or {}
        if "music_envelope" in (args or {}):
            music_envelope = normalize_music_envelope(args.get("music_envelope"))
        else:
            music_envelope = normalize_music_envelope(prior_mix.get("music_envelope"))

        events = [ctx.event(EventType.TOOL_STARTED, {"tool_name": AGENT_TOOL_ADJUST_REMIX})]
        result = await ctx.media.adjust_remix(
            candidate=candidate,
            source_video_artifact_id=session.source_video_artifact_id,
            music_volume=music_volume,
            preserve_original_audio=preserve_original_audio,
            music_envelope=music_envelope,
        )

        candidate["music_volume"] = result.get("music_volume", music_volume)
        candidate["preserve_original_audio"] = result.get(
            "preserve_original_audio", preserve_original_audio
        )
        if result.get("remixed_video_url"):
            candidate["remixed_video_url"] = result["remixed_video_url"]
        remixes = list(candidate.get("remixes") or [])
        remixes.append(
            {
                "music_volume": candidate["music_volume"],
                "preserve_original_audio": candidate["preserve_original_audio"],
                "remixed_video_url": candidate.get("remixed_video_url"),
                "status": result.get("status"),
            }
        )
        candidate["remixes"] = remixes

        updated_candidates = [
            candidate if str(item.get("candidate_id")) == candidate_id else item
            for item in candidates
        ]
        state = dict(session.state_json)
        state["candidates"] = updated_candidates
        # The fades go into the SAME write as the candidates. Writing them
        # separately first looked right and was not: the state rebuilt below
        # comes from the session read at the top of this call, so the second
        # write silently reverted the first — a read-modify-write clobbering
        # itself, a few lines apart.
        if music_envelope:
            state["mix"] = {
                **(state.get("mix") or {}),
                "music_envelope": [
                    {"start_s": start, "end_s": end, "gain_db": gain}
                    for start, end, gain in music_envelope
                ],
            }
        # Deterministic signal for session memory: the user's latest chosen mix.
        state["last_mix_volume"] = candidate["music_volume"]
        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_ADJUST_REMIX,
            status=AgenticToolStatus.COMPLETED,
            input_json={
                "candidate_id": candidate_id,
                "music_volume": candidate["music_volume"],
                "preserve_original_audio": candidate["preserve_original_audio"],
            },
            output_json=result,
            linked_artifact_ids=[session.source_video_artifact_id],
            finished=True,
        )
        ctx.repository.update_session(ctx.session_id, state_json=state)
        events.extend(
            [
                ctx.event(
                    EventType.TOOL_COMPLETED,
                    {"tool_name": AGENT_TOOL_ADJUST_REMIX, "output": result},
                ),
                ctx.event(EventType.CANDIDATE_CARDS, {"candidates": updated_candidates}),
            ]
        )
        return ToolResult(events=events, data=result)


class CompareTakesTool(Tool):
    """Put the takes side by side, with what is actually known about each.

    The agent could describe the takes it had generated only from their titles:
    the state summary carries ids, statuses and URLs, and nothing about how any
    of them SOUND. So "which of these is better?" got an answer assembled from
    the direction text — the same words for every take in the group.

    Free, and it writes its result to the session as well as returning it,
    because the scratchpad dies at the end of the turn: a comparison the user
    refers to two messages later has to still be on record.
    """

    name = AGENT_TOOL_COMPARE_TAKES

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        session = ctx.require_session()
        candidates = list(session.state_json.get("candidates") or [])
        wanted = [str(cid) for cid in (args.get("candidate_ids") or []) if cid]

        # A take that failed or is still rendering has no measurements, so it
        # would come back looking exactly like a clean finished one — the silent
        # wrong answer, in the tool whose whole job is telling takes apart.
        finished = [c for c in candidates if c.get("status") == "completed"]
        if wanted:
            by_id = {str(c.get("candidate_id")): c for c in finished}
            pool = [by_id[cid] for cid in wanted if cid in by_id]  # caller's order
            skipped = [
                {"candidate_id": str(c.get("candidate_id")), "status": c.get("status")}
                for c in candidates
                if str(c.get("candidate_id")) in wanted and c.get("status") != "completed"
            ]
        else:
            pool, skipped = list(finished), []
        # Only the most recent handful: this summary is re-served to the model on
        # every step of every turn, and a long session branches past a dozen takes.
        omitted = max(0, len(pool) - COMPARE_TAKES_LIMIT)
        pool = pool[-COMPARE_TAKES_LIMIT:]

        if not pool:
            return ToolResult(data={
                "error": "nothing_to_compare",
                "instruction": (
                    "No finished takes to compare yet. Wait for the takes to render, "
                    "or name candidate_ids that have finished."
                ),
                "skipped": skipped,
                "known_candidate_ids": [str(c.get("candidate_id")) for c in finished],
            })

        selected = str(session.selected_candidate_id or "")
        rows: list[dict[str, Any]] = []
        for candidate in pool:
            report = candidate.get("listen_report") or {}
            measured = report.get("measured") or {}
            window = candidate.get("window") or {}
            rows.append({
                "candidate_id": str(candidate.get("candidate_id")),
                "status": candidate.get("status"),
                "title": candidate.get("title"),
                "version": candidate.get("version"),
                "parent_candidate_id": candidate.get("parent_candidate_id"),
                "edit_kind": candidate.get("edit_kind"),
                "locked": str(candidate.get("candidate_id")) == selected,
                # What it sounds like, not what it was asked to be.
                "duration_s": measured.get("cut_duration_s"),
                "full_track_s": measured.get("full_duration_s"),
                "window_start_s": window.get("start_s"),
                "faults": list(report.get("notes") or []),
                "shape": list(report.get("observations") or []),
                "direction": str(candidate.get("prompt") or "")[:160],
            })

        summary = {
            "takes": rows,
            "compared": len(rows),
            "any_faults": any(row["faults"] for row in rows),
        }
        if skipped:
            summary["skipped"] = skipped
        if omitted:
            summary["omitted"] = omitted
        # Stamp WHICH takes this described. The summary is re-served on every
        # later step, and a re-cut or a new branch makes "the second one" mean
        # something else — a table that cannot be checked against the takes on
        # record is worse than no table.
        summary["candidate_ids"] = [row["candidate_id"] for row in rows]
        # ...and WHICH generation of each take's audio the row measured. A free
        # re-cut advances that number, which is the moment these durations,
        # windows and faults stop describing the take the row names. The state
        # summary checks these stamps and drops the whole table rather than
        # re-serve a row that has quietly gone stale.
        summary["candidate_epochs"] = {
            str(candidate.get("candidate_id")): candidate_render_epoch(candidate)
            for candidate in pool
        }

        if session.state_json.get("last_comparison") == summary:
            # The model is told to call this before any comparing question, so
            # the same comparison arrives repeatedly. Re-writing the session,
            # re-recording the call and re-broadcasting for a no-op is the
            # mistake set_production_plan already learned not to make.
            return ToolResult(data=summary)

        state = dict(session.state_json)
        # Durable, because the scratchpad is turn-local: without this the model
        # forgets the comparison the moment the turn ends, and the user's next
        # message ("go with the second one") has nothing to resolve against.
        state["last_comparison"] = summary
        ctx.repository.update_session(ctx.session_id, state_json=state)
        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_COMPARE_TAKES,
            status=AgenticToolStatus.COMPLETED,
            input_json={"candidate_ids": wanted},
            output_json=summary,
            finished=True,
        )
        return ToolResult(
            events=[ctx.event(
                EventType.TOOL_COMPLETED,
                {"tool_name": AGENT_TOOL_COMPARE_TAKES, "output": summary},
            )],
            data=summary,
        )


class SculptAudioTool(Tool):
    """Re-shape a take the session already paid for. Free, instant, repeatable.

    The line this draws is the one users already learned from adjust_remix versus
    edit_audio: if it asks a provider for new audio it belongs there, and if it
    re-presents audio we already have it belongs here.
    """

    name = AGENT_TOOL_SCULPT_AUDIO

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        session = ctx.require_session()
        candidates = list(session.state_json.get("candidates") or [])
        kind = str(args.get("sculpt_kind") or SCULPT_KIND_SHIFT_WINDOW).strip()
        if kind not in SCULPT_KINDS:
            raise ValueError(
                f"Unsupported sculpt_kind '{kind}'. Supported: {', '.join(sorted(SCULPT_KINDS))}."
            )
        try:
            candidate, candidate_id = ctx.resolve_candidate(session, candidates, args)
        except KeyError:
            rendered = [
                c for c in candidates
                if c.get("status") == "completed" and (c.get("audio_url") or c.get("video_url"))
            ]
            if not rendered:
                return ctx.no_target_message("re-cut")
            candidate = dict(rendered[-1])
            candidate_id = str(candidate.get("candidate_id"))

        start = _coerce_mix_param(
            "window_start_s", args.get("window_start_s"),
            float((candidate.get("window") or {}).get("start_s") or 0.0),
        )
        # Land on a beat when one is close. A cut that arrives mid-beat reads as
        # a mistake even when the moment was right, and the user asking for
        # "around 0:45" does not mean 45.000.
        start = snap_to_beat(start, candidate.get("music_structure"))

        # An arrangement: several pieces of the take's own track, in the order
        # the user chose. Starts snap to the beat; lengths do not, because a
        # piece's length is usually dictated by the picture it has to cover.
        structure = candidate.get("music_structure") or {}
        segments = normalize_splice_segments(
            args.get("segments"),
            structure=structure,
            full_duration_s=(candidate.get("window") or {}).get("full_duration_s")
            or (structure.get("sections") or [{}])[-1].get("end_s"),
        )
        if kind == SCULPT_KIND_SPLICE and not segments:
            raise ValueError(
                "A splice needs segments: a list of {start_s, duration_s} taken "
                "from this take's own track, in the order they should play."
            )
        if kind != SCULPT_KIND_SPLICE:
            segments = []

        # Carry the mix's durable envelope into the re-cut. Without it a user who
        # faded the music and then moved the window gets their fade deleted by a
        # free edit that never said it would touch the levels.
        durable_envelope = normalize_music_envelope(
            (session.state_json.get("mix") or {}).get("music_envelope")
        )

        events = [ctx.event(EventType.TOOL_STARTED, {"tool_name": AGENT_TOOL_SCULPT_AUDIO})]
        result = await ctx.media.sculpt_window(
            candidate=candidate,
            source_video_artifact_id=session.source_video_artifact_id,
            window_start_s=start,
            segments=segments,
            music_envelope=durable_envelope,
        )

        if result.get("status") == "completed":
            # Persist the window ON THE CANDIDATE, not as a call argument: the
            # mix surface switches renderers the moment narration or SFX joins,
            # and a window that lived only in the call would silently reset
            # exactly when the session got richer.
            window = dict(candidate.get("window") or {})
            window["start_s"] = result.get("window_start_s", start)
            window["source"] = "user"
            # The arrangement travels with the take, beside its window. A plain
            # re-cut clears it: moving the window of an unarranged track must
            # not keep playing a previous arrangement.
            arranged = result.get("arranged_audio_url") or result.get(
                "arranged_audio_local_path"
            )
            if result.get("segments") and arranged:
                candidate["arranged_audio_url"] = str(arranged)
            elif not result.get("segments"):
                candidate.pop("arranged_audio_url", None)
            if result.get("segments"):
                # The arrangement IS the take now; record what it was built
                # from so a later edit can reason about it rather than guess.
                window["segments"] = result["segments"]
            else:
                window.pop("segments", None)
            if result.get("full_duration_s"):
                window["full_duration_s"] = result["full_duration_s"]
            candidate["window"] = window
            if result.get("remixed_video_url"):
                candidate["remixed_video_url"] = result["remixed_video_url"]
            # A re-cut is a new generation of this take's audio, so everything
            # measured about the old window now describes something nobody can
            # hear. Advancing the epoch is what makes that legible to the
            # hydrate projection: deleting the stale report on its own was
            # silently undone 2.5 seconds later, and the fault the user had just
            # sculpted away came back for the rest of the session.
            epoch = candidate_render_epoch(candidate) + 1
            candidate[RENDER_EPOCH_KEY] = epoch
            signals = result.get(TAKE_SIGNALS_KEY)
            if signals:
                candidate[TAKE_SIGNALS_KEY] = stamped_take_signals(
                    signals, render_epoch=epoch
                )
                # Judge the new window now, so the card the user is looking at
                # is about the audio they just asked for. Hydrate recomputes
                # this from the same measurements with the same function, so
                # the two agree and the poll's write-churn guard stays quiet.
                report = music_alignment(
                    signals, observation=session.state_json.get("observation") or {}
                )
                if report:
                    candidate["listen_report"] = report
                else:
                    candidate.pop("listen_report", None)
            else:
                # The re-cut could not be measured. Carrying the previous
                # window's numbers would be worse than carrying none.
                candidate.pop(TAKE_SIGNALS_KEY, None)
                candidate.pop("listen_report", None)
            remixes = list(candidate.get("remixes") or [])
            remixes.append({
                "sculpt_kind": kind,
                "window_start_s": window["start_s"],
                "remixed_video_url": candidate.get("remixed_video_url"),
                "status": result.get("status"),
            })
            candidate["remixes"] = remixes

        updated_candidates = [
            candidate if str(item.get("candidate_id")) == candidate_id else item
            for item in candidates
        ]
        state = dict(session.state_json)
        state["candidates"] = updated_candidates
        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_SCULPT_AUDIO,
            status=AgenticToolStatus.COMPLETED,
            input_json={"candidate_id": candidate_id, "sculpt_kind": kind,
                        "window_start_s": start},
            output_json=result,
            linked_artifact_ids=[session.source_video_artifact_id],
            finished=True,
        )
        ctx.repository.update_session(ctx.session_id, state_json=state)
        events.extend([
            ctx.event(
                EventType.TOOL_COMPLETED,
                {"tool_name": AGENT_TOOL_SCULPT_AUDIO, "output": result},
            ),
            ctx.event(EventType.CANDIDATE_CARDS, {"candidates": updated_candidates}),
        ])
        return ToolResult(events=events, data=result)


class EditAudioTool(Tool):
    name = AGENT_TOOL_EDIT_AUDIO

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        session = ctx.require_session()
        candidates = list(session.state_json.get("candidates") or [])
        try:
            parent, parent_id = ctx.resolve_candidate(session, candidates, args)
        except KeyError:
            return ctx.no_target_message("edit")

        edit_kind = str(args.get("edit_kind") or AGENT_EDIT_KIND_REGENERATE)
        if edit_kind not in AGENT_EDIT_KINDS:
            raise ValueError(f"Unsupported edit_kind: {edit_kind!r}")
        version = ctx.next_version(candidates, parent_id, parent)

        events = [ctx.event(EventType.TOOL_STARTED, {"tool_name": AGENT_TOOL_EDIT_AUDIO})]
        ctx.transition_to(AgenticAudioSessionPhase.GENERATING_CANDIDATES)
        new_candidate = ctx.media.edit_audio(
            session_id=ctx.session_id,
            source_video_artifact_id=session.source_video_artifact_id,
            parent_candidate=parent,
            edit_kind=edit_kind,
            version=version,
            prompt=(str(args["prompt"]) if args.get("prompt") else None),
            extend_seconds=args.get("extend_seconds"),
            creator_user_id=session.creator_user_id,
            actor_user_id=ctx.actor_user_id,
            observation=session.state_json.get("observation"),
        ).model_dump(mode="json")

        updated_candidates = candidates + [new_candidate]
        state = dict(session.state_json)
        state["candidates"] = updated_candidates
        linked_job_ids = list(session.linked_job_ids or [])
        if new_candidate.get("linked_job_id"):
            linked_job_ids.append(str(new_candidate["linked_job_id"]))
        ctx.repository.record_tool_call(
            session_id=ctx.session_id,
            tool_name=AGENT_TOOL_EDIT_AUDIO,
            status=AgenticToolStatus.COMPLETED,
            input_json={"parent_candidate_id": parent_id, "edit_kind": edit_kind},
            output_json={"candidate": new_candidate},
            linked_job_id=new_candidate.get("linked_job_id"),
            linked_artifact_ids=[session.source_video_artifact_id],
            finished=True,
        )
        ctx.transition_to(AgenticAudioSessionPhase.AWAITING_CANDIDATE_CHOICE,
            linked_job_ids=linked_job_ids,
            state_json=state,
        )
        events.extend(
            [
                ctx.event(
                    EventType.TOOL_COMPLETED,
                    {"tool_name": AGENT_TOOL_EDIT_AUDIO, "candidate": new_candidate},
                ),
                ctx.event(EventType.CANDIDATE_CARDS, {"candidates": updated_candidates}),
                ctx.event(
                    EventType.PHASE_CHANGED,
                    {"phase": AgenticAudioSessionPhase.AWAITING_CANDIDATE_CHOICE},
                ),
            ]
        )
        return ToolResult(events=events, data={"candidate": new_candidate})


def build_tool_registry() -> ToolRegistry:
    """Assemble the single registry the reasoning loop dispatches through.

    One instance per tool (tools are stateless — all state flows via ToolContext).
    """

    return ToolRegistry(
        [
            AnalyzeVideoTool(),
            ApproveDirectionTool(),
            GenerateCandidatesTool(),
            FinalizeTool(),
            AdjustRemixTool(),
        SculptAudioTool(),
        CompareTakesTool(),
            EditAudioTool(),
            SetProductionPlanTool(),
            ProposeScriptTool(),
            GenerateVoiceoverTool(),
            PlanSfxTool(),
            GenerateSfxTool(),
            ComposeMixTool(),
        ]
    )


__all__ = [
    "AdjustRemixTool",
    "AnalyzeVideoTool",
    "ApproveDirectionTool",
    "ComposeMixTool",
    "EditAudioTool",
    "FinalizeTool",
    "GenerateCandidatesTool",
    "GenerateVoiceoverTool",
    "PlanSfxTool",
    "GenerateSfxTool",
    "ProposeScriptTool",
    "SetProductionPlanTool",
    "build_tool_registry",
]
