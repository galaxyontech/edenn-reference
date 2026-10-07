from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


def _generation_stall_seconds() -> int:
    """A take normally renders within ~3 minutes; beyond this it's flagged stalled."""
    try:
        return int(os.getenv("AGENTIC_AUDIO_GEN_STALL_SECONDS", "180"))
    except ValueError:
        return 180

from EdennCode.Deployment.api_common import download_public_file_to_disk
from EdennCode.Deployment.api_video_generation import (
    LEGACY_MODEL_MAP,
    VALID_MUSIC_MODEL_SPECS,
)
from EdennCode.Deployment.async_pipeline_v2.models import JobStatus, TaskEnvelope, new_id
from EdennCode.Deployment.async_pipeline_v2.queue_names import namespaced_queue_name
from EdennCode.Deployment.async_pipeline_v2.result_paths import (
    result_audio_url,
    result_video_url,
)
from EdennCode.Deployment.async_pipeline_v2.video_source_preparation import (
    VideoSourcePreparationService,
)
from EdennCode.Deployment.recommendation_persistence import RecommendationAssetIds
from EdennCode.Util.MediaUtils.ffmpeg_utils import (
    compose_master_mix_on_video,
    compose_voiceover_mix_on_video,
    overlay_music_on_video,
    overlay_voiceover_on_video,
)

from ..models import (
    MAX_SPLICE_SEGMENTS,
    VOICE_CATALOG,
    AGENT_EDIT_KIND_CREATIVE_EDIT,
    AGENT_EDIT_KIND_EXTEND,
    AGENT_EDIT_KIND_REGENERATE,
    CREATIVE_EDIT_PROVIDERS,
    NATIVE_EXTEND_CONSUMER_AVAILABLE,
    NATIVE_EXTEND_PROVIDERS,
    MusicCandidateCard,
    provider_for_modelspec,
    voice_preset,
)


logger = logging.getLogger(__name__)

# Signature of an injectable analysis function (lets tests bypass the real
# LLM/scene pipeline). Receives the local video path + user prompt + modelspec
# and returns the observation dict that `analyze_video` exposes to the agent.
AnalyzeFn = Callable[..., Awaitable[dict[str, Any]]]

# Signature of an injectable re-mux function (lets tests bypass real ffmpeg /
# network I/O). Receives the candidate + new mix params and returns the
# `adjust_remix` result dict (remixed video URL + applied mix params).
RemixFn = Callable[..., Awaitable[dict[str, Any]]]

# Injectable multi-layer compose function (music + voice-over). Same idea as
# RemixFn but for `compose_mix`.
ComposeFn = Callable[..., Awaitable[dict[str, Any]]]


# Tiers whose pipeline accepts an explicit style direction. Mirrors the
# preprocessor's own gate (a request that sets verbose_instruction below this
# tier is REJECTED, not degraded), so the routing decision can be made before a
# job is ever enqueued.
VERBOSE_CAPABLE_MODELSPECS = frozenset({"edenn_enhanced", "edenn_studio"})


def candidate_music_volume(candidate: dict[str, Any], default: float = 0.85) -> float:
    """A take's music level, preserving a deliberate silence.

    ``float(candidate.get("music_volume") or 0.85)`` reads 0.0 as absent and
    turns a take the user muted back up — which is the one value nobody sets by
    accident.
    """

    value = (candidate or {}).get("music_volume")
    if value is None or isinstance(value, bool):
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# --------------------------------------------------------------------------- #
# Provenance: which measurements describe what a candidate sounds like NOW      #
# --------------------------------------------------------------------------- #
#
# A take gets re-presented for free — a re-cut window today, an envelope or a
# splice tomorrow — and every re-presentation leaves the previous render's
# measurements describing audio nobody can hear any more. Two writers touch
# these fields: the tool that re-presents, and the hydrate projection running
# on the 2.5s poll. Ownership between them cannot be folklore, because the
# projection's whole contract is "re-derive everything and converge", which
# silently undoes a tool that merely deletes a stale field.
#
# So every set of measurements is signed with the generation of audio it was
# taken from, and judgement only ever reads measurements stamped with the
# generation the candidate presents now. When nothing matches, the honest state
# is *unmeasured* — no report at all, rather than a confident one about a cut
# that no longer exists.

RENDER_EPOCH_KEY = "render_epoch"
TAKE_SIGNALS_KEY = "take_signals"


def candidate_render_epoch(candidate: Optional[dict[str, Any]]) -> int:
    """Which generation of audio a candidate currently presents.

    ``0`` is the paid render every candidate starts at; each free
    re-presentation advances it by one. A candidate written before provenance
    existed carries no key and reads as ``0`` — which is why
    :func:`take_signals_for_candidate` needs one more test before it trusts the
    render's own measurements.
    """

    try:
        return max(0, int((candidate or {}).get(RENDER_EPOCH_KEY) or 0))
    except (TypeError, ValueError):
        return 0


def stamped_take_signals(
    signals: Optional[dict[str, Any]], *, render_epoch: int
) -> dict[str, Any]:
    """Sign measurements with the generation of audio they were taken from.

    Additive: the stamp is a key beside the measurements, never a wrapper
    around them, because the card and the report layer read the measurement
    keys directly.
    """

    if not signals:
        return {}
    stamped = dict(signals)
    stamped[RENDER_EPOCH_KEY] = int(render_epoch)
    return stamped


def _re_presented_before_provenance(candidate: dict[str, Any]) -> bool:
    """True for a take re-cut back when candidates carried no epoch.

    Those rows sit at epoch 0 with the paid render's measurements still on the
    job — precisely the combination that would re-derive a fault the user
    already sculpted away, once, after this ships. The user-owned window is the
    tell: the matcher records ``source="matcher"``, and only a re-cut ever
    writes ``source="user"``.
    """

    window = candidate.get("window")
    return isinstance(window, dict) and str(window.get("source") or "") == "user"


def take_signals_for_candidate(
    candidate: Optional[dict[str, Any]],
    job_result: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """The measurements that describe this candidate's audio as it stands.

    Resolution order, and the reason each step exists:

    1. Measurements the candidate carries itself win when their stamp matches
       its current epoch — a re-presentation that measured its own output.
    2. A stamp that does *not* match means the audio moved on after the
       measuring: nothing is returned, because a stale report is worse than no
       report.
    3. With no measurements of its own, only a candidate still presenting its
       original paid render (epoch 0, never re-cut) may fall back to the job
       result's measurements.

    Returns ``None`` for "nothing describes this audio", which the report layer
    already renders as no report rather than an invented one.
    """

    item = candidate or {}
    epoch = candidate_render_epoch(item)

    own = item.get(TAKE_SIGNALS_KEY)
    if isinstance(own, dict) and own:
        try:
            own_epoch = int(own.get(RENDER_EPOCH_KEY) or 0)
        except (TypeError, ValueError):
            return None
        return own if own_epoch == epoch else None

    if epoch != 0 or _re_presented_before_provenance(item):
        return None

    rendered = (job_result or {}).get(TAKE_SIGNALS_KEY)
    return rendered if isinstance(rendered, dict) and rendered else None


def mix_stems(
    state: Optional[dict[str, Any]], *, candidate_id: Optional[str] = None
) -> dict[str, Any]:
    """The identity of the stems a master mix would be built from, right now.

    Deliberately identifiers and URLs rather than content: a stem that is
    re-rendered gets a new file and therefore a new URL, and a take that is
    re-cut from the same track keeps its URL but advances its render epoch. The
    two together catch every free edit that changes what the deliverable should
    sound like.

    ``candidate_id`` is passed explicitly rather than read from
    ``selected_candidate_id`` because composing happens BEFORE locking: a user
    composes a master out of a take they have not selected yet, and finalize is
    what selects it. Reading the selection here would stamp every mix against
    an empty one and then refuse every finalize that followed.
    """

    state = state or {}
    layers = state.get("layers") or {}

    candidate_id = str(
        candidate_id if candidate_id is not None
        else (state.get("selected_candidate_id") or "")
    )
    candidate = next(
        (
            dict(c)
            for c in (state.get("candidates") or [])
            if str(c.get("candidate_id")) == candidate_id
        ),
        None,
    )
    music_url, _music_start = candidate_music_source(candidate or {})

    voiceover = layers.get("voiceover") or {}
    sfx = layers.get("sfx") or {}
    selected_variant = str(sfx.get("selected_variant_id") or "")
    variant = next(
        (
            dict(v)
            for v in (sfx.get("variants") or [])
            if str(v.get("variant_id")) == selected_variant
        ),
        None,
    )

    return {
        "music_candidate_id": candidate_id,
        "music_epoch": candidate_render_epoch(candidate),
        "music_audio_url": str(music_url or ""),
        "voiceover_audio_url": str(voiceover.get("audio_url") or ""),
        "sfx_variant_id": selected_variant,
        "sfx_audio_url": str((variant or {}).get("audio_url") or ""),
    }


def mix_still_describes(
    mix: Optional[dict[str, Any]], state: Optional[dict[str, Any]]
) -> bool:
    """Whether a composed master still describes the stems it was built from.

    The sibling of :func:`comparison_still_describes`, one level up. Provenance
    stopped at the stem: a take knew whether its own measurements were stale,
    but the master mix built from it did not. So compose -> free re-cut ->
    finalize locked the PRE-cut master while the mix's listen report honestly
    described a file the user was no longer looking at. A single-line narration
    retake and a one-effect redo did the same thing through their own doors.

    Fails closed. A mix written before this stamp existed has no ``built_from``
    and is treated as no longer describing anything: the cost of being wrong
    that way is one free re-compose, and the cost of the other error is
    delivering the wrong file.
    """

    if not mix:
        return False
    stamped = mix.get("built_from")
    if not isinstance(stamped, dict) or not stamped:
        return False
    # Compare against the take this master was built from, not against whatever
    # is selected now: selecting a different take is a different question, and
    # the gate above this one already answers it.
    current = mix_stems(state, candidate_id=str(mix.get("music_candidate_id") or ""))
    return all(str(stamped.get(key, "")) == str(value) for key, value in current.items())


def comparison_still_describes(
    comparison: Optional[dict[str, Any]],
    candidates: Optional[list[dict[str, Any]]],
) -> bool:
    """Whether a stored comparison still describes the takes it named.

    A comparison is a table of measured durations, windows and faults, kept in
    durable state because the scratchpad is turn-local — so it outlives the
    takes it measured and is re-served on every reasoning step. A single free
    re-cut is enough to make a row wrong, and "go with the second one" then
    resolves against numbers that stopped being true several turns ago.

    Fails closed: a comparison with no stamps (written before provenance
    existed), naming a take that has since disappeared, or naming one whose
    epoch has moved, is treated as no longer describing anything. The cost of
    being wrong that way is one free re-run of a free tool.
    """

    if not comparison:
        return False
    stamps = comparison.get("candidate_epochs")
    if not isinstance(stamps, dict) or not stamps:
        return False

    by_id = {str(c.get("candidate_id")): c for c in (candidates or [])}
    for candidate_id, stamped_epoch in stamps.items():
        candidate = by_id.get(str(candidate_id))
        if candidate is None:
            return False
        try:
            if candidate_render_epoch(candidate) != int(stamped_epoch or 0):
                return False
        except (TypeError, ValueError):
            return False
    return True


#: Nobody wants a fade shorter than this; below it the move is a click.
MIN_FADE_S = 0.2
#: A fade cannot lift the music above the level the user set for the piece.
MAX_FADE_GAIN_DB = 0.0
#: ...nor drop it below effective silence, which is what a mute is for.
MIN_FADE_GAIN_DB = -60.0
#: Enough moves for a piece of short-form video; past this it is an arrangement.
MAX_ENVELOPE_POINTS = 12
#: A piece shorter than this is a stutter, not a section of music.
MIN_SPLICE_SEGMENT_S = 0.5


def normalize_splice_segments(
    segments: Optional[list[Any]],
    *,
    structure: Optional[dict[str, Any]] = None,
    full_duration_s: Optional[float] = None,
) -> list[tuple[float, float]]:
    """The pieces a take is assembled from, cleaned and snapped to the beat.

    ``(start_s, duration_s)`` in the full track's time, in the order given.
    Repeats are allowed and so is going backwards: both are ordinary things an
    arrangement does, and neither is a mistake to correct.

    Starts snap to the beat grid when one was found, because a join landing
    mid-beat is heard as an error even when the choice was right. Durations do
    NOT snap — the length of a piece is usually dictated by the picture it has
    to cover, and quantising it would move the very edit the user is placing.
    """

    cleaned: list[tuple[float, float]] = []
    for piece in (segments or [])[:MAX_SPLICE_SEGMENTS]:
        if isinstance(piece, dict):
            raw_start, raw_duration = piece.get("start_s"), piece.get("duration_s")
        elif isinstance(piece, (list, tuple)) and len(piece) == 2:
            raw_start, raw_duration = piece
        else:
            continue
        try:
            start = max(0.0, float(raw_start))
            duration = float(raw_duration)
        except (TypeError, ValueError):
            continue
        if not (duration > MIN_SPLICE_SEGMENT_S) or not math.isfinite(start):
            continue
        if full_duration_s:
            # A piece cannot start past the end of the track, and cannot run
            # past it either — the tail would be silence presented as music.
            if start >= float(full_duration_s) - MIN_SPLICE_SEGMENT_S:
                continue
            duration = min(duration, float(full_duration_s) - start)
        cleaned.append((snap_to_beat(start, structure), round(duration, 2)))
    return cleaned


def normalize_music_envelope(
    envelope: Optional[list[Any]],
) -> list[tuple[float, float, float]]:
    """The user's own level moves, cleaned into windows a mixer can render.

    Every mix volume in this product has been a single number for the whole
    timeline, so "bring the music down under the voice here and back after"
    could only be answered by turning the whole piece down. This is the shape
    that answers it: a list of ``(start_s, end_s, gain_db)``.

    Malformed entries are DROPPED rather than refused. A fade is a creative
    gesture the user will repeat and adjust, often several at once, and losing
    the whole set because one of them arrived backwards would be the wrong
    trade — the mixer is not going to be wrong about the ones that parsed.
    """

    cleaned: list[tuple[float, float, float]] = []
    for point in (envelope or [])[:MAX_ENVELOPE_POINTS]:
        if isinstance(point, dict):
            raw = (point.get("start_s"), point.get("end_s"), point.get("gain_db"))
        elif isinstance(point, (list, tuple)) and len(point) == 3:
            raw = tuple(point)
        else:
            continue
        try:
            start = max(0.0, float(raw[0]))
            end = float(raw[1])
            gain = float(raw[2])
        except (TypeError, ValueError):
            continue
        if not (end > start + MIN_FADE_S):
            continue
        if not all(map(math.isfinite, (start, end, gain))):
            continue
        cleaned.append((
            round(start, 2),
            round(end, 2),
            round(min(MAX_FADE_GAIN_DB, max(MIN_FADE_GAIN_DB, gain)), 1),
        ))
    return sorted(cleaned)


def candidate_music_source(
    candidate: Optional[dict[str, Any]],
) -> tuple[Optional[str], float]:
    """Which audio a re-presentation should play, and where to start it.

    A take whose window the user MOVED is that whole track seeked into, not the
    original cut. Every renderer that re-presents a take has to ask this
    question, and every one of them has to answer it identically — answering it
    separately in each is how a chosen window survived a compose and then
    silently reverted the moment the user nudged the volume.

    Returns ``(url, start_s)``. The url is ``None`` when the take has no audio
    to present yet. A window that cannot be read as a number is not a window:
    the cut is played from the top rather than the whole track from the top,
    which would be a different piece of music than the one on the card.
    """

    item = candidate or {}
    cut_url = item.get("audio_url")
    full_url = item.get("complete_audio_url")

    # An ARRANGEMENT is its own piece of music. A splice assembles pieces of the
    # full track into a new order and renders that; before this, only the muxed
    # video kept it, and `window.source == "user"` sent every later renderer
    # back to the full track from 0.0 — so the very next volume nudge, compose
    # or finalize quietly played something the user had not arranged. The
    # arranged file exists only because a splice made it, so its presence is
    # the whole test.
    arranged_url = item.get("arranged_audio_url")
    if arranged_url:
        return str(arranged_url), 0.0

    window = item.get("window") or {}
    if str(window.get("source") or "") == "user" and full_url:
        try:
            return str(full_url), max(0.0, float(window.get("start_s") or 0.0))
        except (TypeError, ValueError):
            pass

    if cut_url:
        return str(cut_url), 0.0
    return (str(full_url) if full_url else None), 0.0


def normalize_music_modelspec(value: str | None) -> str:
    normalized = (value or "edenn_basic").strip().lower() or "edenn_basic"
    normalized = LEGACY_MODEL_MAP.get(normalized, normalized)
    if normalized not in VALID_MUSIC_MODEL_SPECS:
        return "edenn_basic"
    return normalized


def _release_workdir(workdir: Path, *, keep: Optional[Path] = None) -> None:
    """Delete a compose working directory once its output has been handed off.

    Every mix downloads the music, the narration and the effects bed next to the
    video it renders, then writes a video-sized output beside them. Nothing ever
    removed any of it, so each mix cost several video-sized files on a container
    disk that is wiped on deploy and unmonitored until it is full.

    ``keep`` is the one file that is still the deliverable: with no blob storage
    configured the local path IS what the caller returns, so it stays and only
    the redundant inputs go. Once the output has been uploaded, nothing here is
    the last copy of anything.
    """
    import shutil

    try:
        if not workdir.exists():
            return
        if keep is None:
            shutil.rmtree(workdir, ignore_errors=True)
            return
        for child in workdir.iterdir():
            if child.resolve() == keep.resolve():
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - cleanup must never fail the render
        logger.debug("workdir cleanup failed for %s", workdir, exc_info=True)


#: Free-text the vision pipeline writes about the user's footage, and how much
#: of each is worth carrying. Generous on purpose: the prompt asks the agent to
#: ground its reasoning in observation specifics, and many-scene footage is
#: legitimately long, so a tight cap would starve the thing this text is for.
_OBSERVATION_TEXT_CAPS: dict[str, int] = {
    "video_title": 300,
    "video_description": 1200,
    "video_summary": 4000,
    "sanitized_prompt": 2000,
    "detected_category": 120,
    "detected_language": 60,
    "detected_vocal_gender": 40,
}
_SCENE_TEXT_CAPS: dict[str, int] = {
    "visual_summary": 800,
    "key_actions": 800,
    "mood": 200,
}
#: Anything else that arrives as a string.
_OBSERVATION_DEFAULT_CAP = 2000
_OBSERVATION_MAX_SCENES = 80


def _bounded_text(value: Any, cap: int) -> Any:
    """Bound one value, and leave anything that is not text alone."""

    if not isinstance(value, str):
        return value
    # Control characters are how a description tries to look like structure —
    # a fake blank line, a fake heading, a line that reads as a new speaker.
    cleaned = "".join(ch for ch in value if ch == "\n" or ch == "\t" or ch >= " ")
    return cleaned[:cap]


def bound_observation(observation: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Bound the vision pipeline's description of the user's footage.

    This text is written ABOUT a file the user uploaded, by a model reading
    that file, and it then rides inside the system-role state summary on every
    reasoning step — which is a higher privilege than anything the user
    themselves can write. Client payloads in this codebase are already bounded
    against hostile input; this path was not, and the asymmetry is the whole
    finding: not a decision anyone made, a surface nobody revisited.

    What this does is deliberately narrow. It bounds size and strips the control
    characters a description would use to imitate structure. It does NOT try to
    detect instructions in prose — that cannot be done reliably, and pretending
    otherwise would buy a false sense of safety. The part that actually closes
    injection-to-BEHAVIOUR is the prompt rule naming this text as a description
    of footage and never an instruction; injection-to-SPEND is already shut by
    the approval gates, which key on who invoked a tool rather than on anything
    in here.

    Cap-and-pass, never drop: unknown keys are carried through bounded, because
    the canvas and the logs read fields this function has no business knowing
    about (a thumbnail URL, an analysis mode, the source audio flag).
    """

    if not isinstance(observation, dict):
        return {}

    bounded: dict[str, Any] = {}
    for key, value in observation.items():
        if key == "scenes" and isinstance(value, list):
            scenes: list[Any] = []
            for scene in value[:_OBSERVATION_MAX_SCENES]:
                if not isinstance(scene, dict):
                    scenes.append(scene)
                    continue
                scenes.append({
                    scene_key: _bounded_text(
                        scene_value,
                        _SCENE_TEXT_CAPS.get(scene_key, _OBSERVATION_DEFAULT_CAP),
                    )
                    for scene_key, scene_value in scene.items()
                })
            bounded["scenes"] = scenes
            continue
        if key == "music_prompt" and isinstance(value, dict):
            bounded["music_prompt"] = {
                mp_key: _bounded_text(mp_value, _OBSERVATION_DEFAULT_CAP)
                for mp_key, mp_value in value.items()
            }
            continue
        bounded[key] = _bounded_text(
            value, _OBSERVATION_TEXT_CAPS.get(key, _OBSERVATION_DEFAULT_CAP)
        )
    return bounded


class AgenticAudioTools:
    def __init__(
        self,
        *,
        async_repository: Any,
        queue: Any = None,
        settings: Any = None,
        storage: Any = None,
        orchestrator: Any = None,
        analyze_fn: Optional[AnalyzeFn] = None,
        remix_fn: Optional[RemixFn] = None,
        compose_fn: Optional[ComposeFn] = None,
        window_fn: Optional[RemixFn] = None,
    ) -> None:
        self.async_repository = async_repository
        self.queue = queue
        self.settings = settings
        self.storage = storage
        self._orchestrator = orchestrator
        self._analyze_fn = analyze_fn
        self._remix_fn = remix_fn
        self._compose_fn = compose_fn
        # Its own seam, not remix_fn's: the injected remix function has a fixed
        # keyword signature, so forwarding a new argument through it would raise
        # at runtime rather than being ignored.
        self._window_fn = window_fn
        self._source_preparation: Optional[VideoSourcePreparationService] = None

    def _get_orchestrator(self) -> Any:
        if self._orchestrator is None:
            # Imported lazily so importing the module never pulls the full
            # video-music workflow stack (and its heavy deps) unless analysis
            # actually runs.
            from EdennCode.Deployment.workflows import VideoGenerationOrchestrator

            self._orchestrator = VideoGenerationOrchestrator(
                storage=self.storage,
                llm_image_container=getattr(self.settings, "llm_image_container", None),
                llm_image_sas_ttl_minutes=int(
                    getattr(self.settings, "llm_image_sas_ttl_minutes", 5) or 5
                ),
                llm_image_cleanup_delay_seconds=float(
                    getattr(self.settings, "llm_image_cleanup_delay_seconds", 2.0) or 2.0
                ),
            )
        return self._orchestrator

    def _get_source_preparation(self) -> VideoSourcePreparationService:
        if self._source_preparation is None:
            self._source_preparation = VideoSourcePreparationService(
                repository=self.async_repository,
                settings=self.settings,
                storage=self.storage,
                stage_name="agentic_audio_analyze",
                diagnostic_logger=logger,
            )
        return self._source_preparation

    async def analyze_video(
        self,
        *,
        source_video_artifact_id: str,
        user_prompt: str = "",
        modelspec: str = "edenn_basic",
    ) -> dict[str, Any]:
        """Run the understanding pipeline (intent → scenes → summary → prompt).

        Reuses ``VideoGenerationOrchestrator.preview_pre_generation`` (pipeline
        stages 0–3.1) and returns a flattened observation the agent reasons over.
        A test/injectable ``analyze_fn`` can replace the real LLM/scene pipeline.
        """

        artifact = self.get_source_video_artifact(source_video_artifact_id)
        normalized_modelspec = normalize_music_modelspec(modelspec)

        if self._analyze_fn is not None:
            # Injected analysis (tests / alternate implementations) owns source
            # resolution so it never depends on real file I/O.
            observation = await self._analyze_fn(
                artifact=artifact,
                user_prompt=user_prompt,
                modelspec=normalized_modelspec,
            )
        else:
            video_path = await self._get_source_preparation().resolve_source_video(artifact)
            result = await self._get_orchestrator().preview_pre_generation(
                video_path=Path(video_path),
                user_prompt=user_prompt or "",
                modelspec=normalized_modelspec,
            )
            observation = self._observation_from_pre_generation(result)
            self._meter_understanding(result)

            attach_footage_signals(observation, Path(video_path))

        observation.setdefault("source_video_artifact_id", source_video_artifact_id)
        # One chokepoint, after BOTH branches: the injected path is what the
        # standalone deploy runs, so bounding only the in-process one would
        # leave the shipped path unbounded.
        return bound_observation(observation)

    @staticmethod
    def _meter_understanding(result: Any, *, session_id: str = "") -> None:
        """Record what understanding this video cost.

        The same recorder the standalone path uses. Understanding is the
        largest single spend in the product — one vision call per scene window,
        up to thirty — and the boundary that builds an observation out of the
        pipeline's result has always dropped the token counts it was handed.
        """

        from ..persistence.usage import (
            Measurement,
            SITE_UNDERSTANDING,
            active_meter,
            analysis_key,
        )

        meter = active_meter()
        if not meter.enabled:
            return
        try:
            usage = getattr(result, "token_usage", None) or {}
            metadata = getattr(result, "video_metadata", None)
            duration = float(getattr(metadata, "duration", 0.0) or 0.0)
            meter.record_once(
                meter_key=analysis_key(session_id or "orphan"),
                site=SITE_UNDERSTANDING,
                measurement=Measurement(
                    source_ms=round(duration * 1000) if duration > 0 else None,
                    items=len(getattr(result, "scenes", None) or []),
                    lm_input_tokens=usage.get("prompt_tokens"),
                    lm_output_tokens=usage.get("completion_tokens"),
                    primary_unit="source_ms",
                ),
                session_id=session_id or None,
            )
        except Exception:  # noqa: BLE001 - never fail an analysis to record it
            logger.warning("usage meter: understanding not recorded", exc_info=True)

    @staticmethod
    def _observation_from_pre_generation(result: Any) -> dict[str, Any]:
        metadata = getattr(result, "video_metadata", None)
        duration = float(getattr(metadata, "duration", 0.0) or 0.0)
        scenes = [
            {
                "scene_index": getattr(scene, "scene_index", index),
                "start_timestamp": getattr(scene, "start_timestamp", None),
                "end_timestamp": getattr(scene, "end_timestamp", None),
                "visual_summary": getattr(scene, "visual_summary", ""),
                "key_actions": getattr(scene, "key_actions", ""),
                "mood": getattr(scene, "mood", ""),
            }
            for index, scene in enumerate(getattr(result, "scenes", None) or [])
        ]
        return {
            "duration_s": duration,
            "width": getattr(metadata, "width", None),
            "height": getattr(metadata, "height", None),
            "video_title": getattr(result, "video_title", ""),
            "video_description": getattr(result, "video_description", ""),
            "video_summary": getattr(result, "video_summary", None),
            "scenes": scenes,
            "detected_language": getattr(result, "user_requested_language", ""),
            "detected_category": getattr(result, "detected_category", ""),
            "detected_include_vocals": bool(getattr(result, "include_vocals", False)),
            "detected_vocal_gender": getattr(result, "vocal_gender", "female"),
            "sanitized_prompt": getattr(result, "sanitized_prompt", ""),
            "music_prompt": getattr(result, "music_prompt", None) or {},
            "suggested_modelspec": normalize_music_modelspec(
                getattr(result, "used_music_model_spec", "") or "edenn_basic"
            ),
        }

    def get_source_video_artifact(self, artifact_id: str) -> Any:
        artifact = self.async_repository.get_artifact(artifact_id)
        if artifact is None or getattr(artifact, "artifact_type", None) != "source_video":
            raise KeyError(f"Source video artifact not found: {artifact_id}")
        return artifact

    @staticmethod
    def fuse_music_style_prompt(
        proposal_prompt: str, observation: Optional[dict[str, Any]]
    ) -> Optional[str]:
        """Fuse the proposal's creative direction with the VIDEO-DERIVED structure
        already synthesized at session bootstrap (observation.music_prompt + scenes).

        The proposal prose alone reads like a generic score spec — the music model
        gets no tempo, no footage mood, and no timed arc, so the result doesn't
        track the video (the pipeline treats a detailed user prompt as an explicit
        style override and skips its own scene orchestration). This fusion keeps
        the chosen direction dominant while grounding it in what the video
        actually does and when.
        """
        if not observation:
            return None
        parts: list[str] = []
        if proposal_prompt:
            parts.append(proposal_prompt.strip()[:700])
        mp = observation.get("music_prompt") or {}
        facts: list[str] = []
        if mp.get("global_mood"):
            facts.append(f"footage mood: {str(mp['global_mood'])[:160]}")
        if mp.get("tempo_bpm"):
            facts.append(f"tempo ≈ {mp['tempo_bpm']} BPM")
        instruments = [str(i) for i in (mp.get("instruments") or [])][:10]
        if instruments:
            facts.append("instrumentation drawn from the footage: " + ", ".join(instruments))
        if facts:
            parts.append("Ground the score in the video — " + "; ".join(facts) + ".")
        duration = observation.get("duration_s")
        scenes = [s for s in (observation.get("scenes") or []) if isinstance(s, dict)]
        # The real analysis emits start_timestamp/end_timestamp/visual_summary
        # (tests and older shapes use start_s/end_s/label) — read both, and
        # sample evenly so the arc spans the whole video instead of stopping
        # at the first 8 scenes of a long cut.
        if len(scenes) > 8:
            step = (len(scenes) - 1) / 7
            scenes = [scenes[round(i * step)] for i in range(8)]
        entries: list[str] = []
        for s in scenes:
            start = s.get("start_s", s.get("start_timestamp")) or 0
            end = s.get("end_s", s.get("end_timestamp")) or 0
            label = s.get("label") or s.get("visual_summary") or s.get("mood") or ""
            if not label or float(end) <= 0:
                continue
            entries.append(f"{float(start):.0f}–{float(end):.0f}s {str(label)[:48]}")
        if entries:
            total = f" Total length ≈ {float(duration):.0f}s." if duration else ""
            parts.append(f"Follow the video's arc: {'; '.join(entries)}.{total}")
        if len(parts) <= 1:
            return None  # nothing video-derived to add — let the pipeline orchestrate
        return " ".join(parts)[:1800]

    @staticmethod
    def route_style_prompt(
        fused_style_prompt: Optional[str], *, modelspec: str, user_prompt: str
    ) -> dict[str, Any]:
        """Put the fused, video-grounded prompt where the rendering tier reads it.

        The fused prompt (direction + footage mood/tempo + a timed scene arc) is
        what makes the music track the video, and every enqueue has always
        carried it. But the pipeline only consumes ``music_style_prompt`` under
        ``verbose_instruction``, which additionally requires an EMPTY
        ``user_prompt`` and an enhanced/studio tier — and nothing ever set that
        flag, so the grounding was computed on every generation and then ignored
        on both deployments.

        Below the verbose tier the same text still has somewhere useful to go:
        the ordinary prompt, which the non-verbose path preprocesses. That is
        strictly better than dropping the video grounding on the floor.

        Returns the full slot set rather than mutating, because the decision has
        to be made twice: once at enqueue, and again by the standalone renderer
        when a missing provider key forces a different tier than the one asked
        for — where flipping the flag without re-routing would fail the job
        instead of degrading it.
        """

        text = (fused_style_prompt or "").strip()
        if not text:
            return {
                "user_prompt": user_prompt,
                "verbose_instruction": False,
                "music_style_prompt": None,
            }
        if normalize_music_modelspec(modelspec) in VERBOSE_CAPABLE_MODELSPECS:
            return {
                "user_prompt": "",
                "verbose_instruction": True,
                "music_style_prompt": text,
            }
        # Below the verbose tier the style slot is provably dead — the
        # orchestration stage reads it only under verbose_instruction — so the
        # fused text rides the ordinary prompt instead. It is still recorded in
        # its own slot: that field is the payload's record of what the session
        # fused, which the job history and the edit path both read back.
        return {
            "user_prompt": text,
            "verbose_instruction": False,
            "music_style_prompt": text,
        }

    def generate_music_candidates(
        self,
        *,
        session_id: str,
        source_video_artifact_id: str,
        proposal: dict[str, Any],
        count: int = 2,
        creator_user_id: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        observation: Optional[dict[str, Any]] = None,
        on_planned: Optional[Callable[[str, str, int], None]] = None,
    ) -> list[MusicCandidateCard]:
        proposal_id = str(proposal["proposal_id"])
        count = max(1, min(int(count or 2), 3))
        candidates: list[MusicCandidateCard] = []
        for index in range(count):
            candidate_id = f"candidate_{proposal_id}_{index + 1}"
            prompt = str(proposal.get("prompt") or "")
            if count > 1:
                prompt = f"{prompt} Variation {index + 1}: keep the same direction but explore a distinct arrangement."
            # The music model receives the FUSED prompt (direction + video structure);
            # the card keeps the human-readable direction text for the UI.
            fused = self.fuse_music_style_prompt(prompt, observation)
            # Mint the job id first and announce the take BEFORE the row the
            # completer acts on exists. The completer polls the job table, so
            # the row IS the trigger: created first, a failure before the
            # session write leaves a queued job that will run, bill, and have
            # no take pointing at it.
            planned_job_id = new_id("job")
            if on_planned is not None:
                on_planned(candidate_id, planned_job_id, index)
            linked_job_id = self._enqueue_video_music_candidate_job(
                job_id=planned_job_id,
                session_id=session_id,
                source_video_artifact_id=source_video_artifact_id,
                proposal=proposal,
                candidate_id=candidate_id,
                prompt=prompt,
                creator_user_id=creator_user_id,
                actor_user_id=actor_user_id,
                extra_payload={"music_style_prompt": fused} if fused else None,
            )
            candidates.append(
                MusicCandidateCard(
                    candidate_id=candidate_id,
                    proposal_id=proposal_id,
                    title=f"{proposal.get('title') or 'Music Candidate'} {index + 1}",
                    prompt=prompt,
                    modelspec=normalize_music_modelspec(str(proposal.get("modelspec") or "")),
                    include_vocals=bool(proposal.get("include_vocals", False)),
                    linked_job_id=linked_job_id,
                    status="queued" if linked_job_id else "planned",
                    provider=provider_for_modelspec(
                        normalize_music_modelspec(str(proposal.get("modelspec") or ""))
                    ),
                )
            )
        return candidates

    def hydrate_candidate_results(
        self,
        *,
        candidates: list[dict[str, Any]],
        observation: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        hydrated: list[dict[str, Any]] = []
        for candidate in candidates:
            item = dict(candidate)
            # Provider is derivable from the modelspec even before the job
            # finishes, so always set it.
            item.setdefault("provider", provider_for_modelspec(item.get("modelspec")))
            linked_job_id = item.get("linked_job_id")
            if linked_job_id:
                job = self.async_repository.get_job(str(linked_job_id))
                if job is not None:
                    result = getattr(job, "result_json", None) or {}
                    item["status"] = getattr(job, "status", item.get("status", "queued"))
                    # Candidates link to several job types whose results are shaped
                    # differently (video-music is blocked; creative-edit and the dev
                    # harness are flat), so read through the shape-tolerant accessors.
                    item["audio_url"] = result_audio_url(result)
                    item["video_url"] = result_video_url(result)
                    # The video-length CUT is the take; the complete track is the
                    # longer thing it was cut from. The job result carries both,
                    # and only the cut was ever copied across — so
                    # complete_audio_url was null on every candidate in every
                    # session and the full track was unreachable from a take.
                    complete = result.get("complete_audio_url")
                    if complete:
                        item["complete_audio_url"] = complete
                    # The beat grid and section map belong to the TRACK, so
                    # unlike the listen-back numbers they are not epoch-scoped:
                    # re-cutting a window does not change where the drop is.
                    structure = result.get("music_structure")
                    if structure and "music_structure" not in item:
                        item["music_structure"] = structure
                    # Listen back to the take, the way narration already does.
                    # Pure arithmetic over measurements taken at render time: no
                    # I/O here, because hydrate runs on the 2.5s poll inside a
                    # locked read-modify-write. A placeholder tone gets no report
                    # — a critic scoring a stand-in's flat energy is noise on a
                    # card already labelled a preview.
                    #
                    # Which measurements is a provenance question, not a
                    # freshness one: re-deriving from the render's signals
                    # whenever a report happens to be missing is what let a
                    # sculpted-away fault come back on the next poll, for good.
                    if not result.get("placeholder"):
                        report = music_alignment(
                            take_signals_for_candidate(item, result),
                            observation=observation or {},
                        )
                        if report:
                            item["listen_report"] = report
                    # Where this cut sits inside its own full track. Recorded
                    # only once, from the render: a later re-cut owns the window
                    # and must not be overwritten by the original match.
                    if "window" not in item and result.get("music_start_s") is not None:
                        try:
                            item["window"] = {
                                "start_s": round(float(result["music_start_s"]), 2),
                                "source": "matcher",
                            }
                        except (TypeError, ValueError):
                            pass
                    # Generation watchdog: a take normally renders within ~3 minutes.
                    # When a job sits queued/processing beyond the threshold, surface
                    # HOW LONG on the candidate so the UI can say "this looks stuck —
                    # check the backend / retry" instead of spinning forever. Read-only
                    # (no auto-fail): flag, don't kill, from a GET path.
                    if item["status"] in ("queued", "processing"):
                        created = getattr(job, "created_at", None)
                        if created is not None:
                            now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
                            age_s = int((now - created).total_seconds())
                            if age_s > _generation_stall_seconds():
                                item["stalled_seconds"] = age_s
                    # Honest labeling: dev harnesses mark stand-in tones with
                    # placeholder=True — carry it onto the candidate so the UI can
                    # badge it instead of presenting a tone as the user's real take.
                    if result.get("placeholder"):
                        item["placeholder"] = True
                    # The tier that rendered outranks the tier that was asked
                    # for: a render-time fallback (no provider key for the
                    # requested tier) is invisible unless the candidate says
                    # what actually played.
                    rendered_spec = str(result.get("modelspec") or "").strip()
                    if rendered_spec and rendered_spec != item.get("modelspec"):
                        item["requested_modelspec"] = item.get("modelspec")
                        item["modelspec"] = rendered_spec
                        item["provider"] = provider_for_modelspec(rendered_spec)
                    # Provider-native handles for downstream native edits. The
                    # video-music job result has never actually carried these
                    # (MusicGenerationStageOutput.task_id/.audio_id are declared but
                    # never assigned), so native extend always falls back to a
                    # regenerate today. Kept as a flat read: reconnecting the ids
                    # upstream is what would light this path up.
                    if result.get("provider_audio_id"):
                        item["provider_audio_id"] = result.get("provider_audio_id")
                    if result.get("provider_task_id"):
                        item["provider_task_id"] = result.get("provider_task_id")
            hydrated.append(item)
        return hydrated

    def hydrate_voiceover_layer(
        self, *, voiceover: dict[str, Any], observation: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """Hydrate a voice-over layer's audio_url/status from its linked job."""

        item = dict(voiceover or {})
        # The roster travels WITH the layer, so a reloaded session's picker
        # shows the same characters as a fresh card — without this, a reload
        # fell back to a stale hardcoded list in the frontend.
        item["voice_options"] = [
            {k: preset[k] for k in ("id", "name", "gender", "style")}
            for preset in VOICE_CATALOG
        ]
        linked_job_id = item.get("linked_job_id")
        if linked_job_id:
            job = self.async_repository.get_job(str(linked_job_id))
            if job is not None:
                result = getattr(job, "result_json", None) or {}
                item["status"] = getattr(job, "status", item.get("status", "queued"))
                audio_url = result_audio_url(result)
                if audio_url:
                    item["audio_url"] = audio_url
                # A stand-in tone must be badged as one. Candidates and SFX
                # variants already carry this; narration did not, so a fallback
                # tone arrived as a finished read with nothing to distinguish it.
                if result.get("placeholder"):
                    item["placeholder"] = True
                # The render resolves planned starts against the ACTUAL spoken
                # durations — surface the real placements (incl. duration_s), so
                # the UI's timed plan matches what the listener hears.
                if result.get("segments"):
                    planned = list(item.get("segments") or [])
                    item["segments"] = list(result["segments"])
                    item["alignment"] = narration_alignment(
                        item["segments"], planned=planned, observation=observation or {},
                    )
        return item

    def hydrate_sfx_layer(self, *, sfx: dict[str, Any]) -> dict[str, Any]:
        """Hydrate each SFX variant's status/audio_url/video_url from its job,
        and roll the layer status up from its selected (else latest) variant."""

        item = dict(sfx or {})
        variants = [dict(v) for v in (item.get("variants") or [])]
        for v in variants:
            linked_job_id = v.get("linked_job_id")
            if not linked_job_id:
                continue
            job = self.async_repository.get_job(str(linked_job_id))
            if job is None:
                continue
            result = getattr(job, "result_json", None) or {}
            v["status"] = getattr(job, "status", v.get("status", "queued"))
            audio_url = result_audio_url(result)
            if audio_url:
                v["audio_url"] = audio_url
            video_url = result_video_url(result)
            if video_url:
                v["video_url"] = video_url
            if result.get("placeholder"):
                v["placeholder"] = True
            # What the render actually placed, and who chose the moments. The
            # variant used to carry only URLs, so nothing downstream could tell
            # whether the take honoured the approved plan — which is the one
            # check the plan card exists to make possible. Rounded and ordered
            # by the renderer so re-hydrating an unchanged job stays byte-equal
            # and the 2.5s poll does not rewrite the session every tick.
            if result.get("spotting"):
                v["spotting"] = str(result["spotting"])
            # Which effects were carried forward instead of being generated
            # again. Without this nothing downstream can tell a one-hit redo
            # from a full re-render of the bed — which is exactly the state the
            # feature was in when the reuse map was silently dropped.
            reused = result.get("reused_event_ids")
            if isinstance(reused, list):
                v["reused_event_ids"] = [str(x) for x in reused]
            rendered = result.get("rendered_events")
            if isinstance(rendered, list):
                v["rendered_events"] = rendered
            # The bed was the one rendered artifact nobody listened back to.
            # These numbers were taken at render time; the report is pure
            # arithmetic over them, which is what makes it safe to compute on
            # the poll — no probe, no file, byte-stable between identical runs.
            # Whether the engine watched the footage or was handed a
            # description of it. A deployment with no reachable URL for the
            # clip degrades to prompt-written effects silently, and the
            # difference showed up in a log line and nowhere a user could see.
            if "watched_the_video" in result:
                v["watched_the_video"] = bool(result.get("watched_the_video"))
                reason = str(result.get("not_watched_reason") or "")
                if reason and not v["watched_the_video"]:
                    v["not_watched_reason"] = reason
            signals = result.get("take_signals")
            if isinstance(signals, dict) and signals:
                v[TAKE_SIGNALS_KEY] = signals
                report = sfx_listen_report(
                    signals,
                    watched_the_video=v.get("watched_the_video"),
                    not_watched_reason=v.get("not_watched_reason", ""),
                )
                if report:
                    v["listen_report"] = report
                # How the take compares to the plan that was approved. A diff,
                # not a verdict: under engine spotting the workflow chooses its
                # own moments by design, so treating divergence as a defect
                # would send the agent chasing a match it cannot reach.
                diff = sfx_render_diff(item.get("events"), rendered)
                if diff:
                    v["plan_diff"] = diff
        item["variants"] = variants
        # Layer status reflects the selected variant (or the latest) so the UI can
        # show a single rolled-up state.
        chosen = None
        sel = item.get("selected_variant_id")
        if sel:
            chosen = next((v for v in variants if v.get("variant_id") == sel), None)
        if chosen is None:
            # Nothing chosen yet: the first variant that actually FINISHED
            # becomes the working one. Deterministic (first in order, not
            # newest) so two hydrate passes over the same rows agree, which is
            # what keeps the 2.5s poll from rewriting the session every tick.
            finished = next(
                (
                    v for v in variants
                    if v.get("status") == "completed"
                    and (v.get("audio_url") or v.get("video_url"))
                ),
                None,
            )
            if finished is not None:
                item["selected_variant_id"] = finished.get("variant_id")
                chosen = finished
        if chosen is None and variants:
            chosen = variants[-1]
        if chosen is not None:
            item["status"] = chosen.get("status") or item.get("status")
        return item

    def compose_final_mix(
        self,
        *,
        selected_candidate: dict[str, Any],
    ) -> dict[str, Any]:
        linked_job_id = selected_candidate.get("linked_job_id")
        if not linked_job_id:
            return {
                "status": "planned",
                "message": "Selected candidate has no linked video-music job yet.",
            }
        job = self.async_repository.get_job(str(linked_job_id))
        result = getattr(job, "result_json", None) if job is not None else None
        if result:
            # The stored blob can carry a raw provider-named error from a
            # failed generation (legacy rows especially); this payload is
            # client-facing, so scrub the free-text error before embedding.
            if isinstance(result, dict) and result.get("error"):
                from EdennCode.Deployment.error_codes import scrub_provider_names

                result = dict(result)
                result["error"] = scrub_provider_names(str(result["error"]))
            return {
                "status": getattr(job, "status", "completed"),
                "linked_job_id": linked_job_id,
                "audio_url": result_audio_url(result),
                "video_url": result_video_url(result),
                "result": result,
            }
        return {
            "status": getattr(job, "status", "queued") if job is not None else "queued",
            "linked_job_id": linked_job_id,
            "message": "Final compose is waiting for the linked video-music job to complete.",
        }

    # ----- Phase 2: iterative editing ----------------------------------------

    def _refresh_signed_url(self, url: Optional[str]) -> Optional[str]:
        """Re-sign a possibly expired SAS URL for a blob in our storage account.

        Candidate/layer URLs are signed when their job completes; ffmpeg
        composes can run hours later, after the SAS expired (403 on download).
        When the URL points into our account and we can sign, mint a fresh
        read SAS from the blob path; anything else passes through unchanged.
        """

        if not url or self.storage is None or not getattr(self.storage, "enabled", False):
            return url
        if not hasattr(self.storage, "generate_sas_url"):
            return url
        try:
            from urllib.parse import unquote, urlparse

            parsed = urlparse(str(url))
            account_name = getattr(self.settings, "storage_account_name", None) or ""
            if not account_name or not parsed.netloc.startswith(f"{account_name}.blob."):
                return url
            container, _, blob_name = parsed.path.lstrip("/").partition("/")
            if not container or not blob_name:
                return url
            fresh = self.storage.generate_sas_url(
                container=container, blob_name=unquote(blob_name)
            )
            return fresh or url
        except Exception:  # noqa: BLE001 - refresh is best-effort; stored URL may still work
            return url

    async def adjust_remix(
        self,
        *,
        candidate: dict[str, Any],
        source_video_artifact_id: str,
        music_volume: float = 0.85,
        preserve_original_audio: bool = False,
        music_envelope: Optional[list[tuple[float, float, float]]] = None,
    ) -> dict[str, Any]:
        """Cheap re-mux: overlay the candidate's existing music on the source video.

        No new generation and no provider cost — this only re-runs ffmpeg with new
        mix parameters (music volume, keep/replace the original talking track) and
        registers a fresh remixed video. The actual media work is injectable via
        ``remix_fn`` so tests stay hermetic.
        """

        music_volume = float(music_volume)
        preserve_original_audio = bool(preserve_original_audio)

        if self._remix_fn is not None:
            result = await self._remix_fn(
                candidate=candidate,
                source_video_artifact_id=source_video_artifact_id,
                music_volume=music_volume,
                preserve_original_audio=preserve_original_audio,
                # The envelope belongs to the FILE, not to the card. Omitting it
                # here is what let a session show a fade the delivered video did
                # not have: the in-process branch below applies it, the injected
                # renderer accepts it, and only this call forgot to pass it — so
                # the defect was invisible everywhere except the deployment that
                # injects a renderer, which is the shipped one.
                music_envelope=list(music_envelope or []),
            )
            result.setdefault("music_volume", music_volume)
            result.setdefault("preserve_original_audio", preserve_original_audio)
            return result

        music_url, music_start_s = candidate_music_source(candidate)
        if not music_url:
            return {
                "status": "planned",
                "music_volume": music_volume,
                "preserve_original_audio": preserve_original_audio,
                "message": "Candidate has no generated audio to re-mix yet.",
            }

        artifact = self.get_source_video_artifact(source_video_artifact_id)
        video_path = await self._get_source_preparation().resolve_source_video(artifact)

        remix_id = new_id("remix")
        workdir = Path(getattr(self.settings, "workdir", "/tmp")) / "agentic_audio_remix" / remix_id
        workdir.mkdir(parents=True, exist_ok=True)
        music_path = workdir / "music.audio"
        await download_public_file_to_disk(
            url=str(self._refresh_signed_url(str(music_url))),
            destination=music_path,
            asset_label="audio",
        )
        output_path = workdir / "remixed_video.mp4"
        overlay_music_on_video(
            Path(video_path),
            music_path,
            output_path,
            music_volume=music_volume,
            preserve_original_audio=preserve_original_audio,
            music_start_s=music_start_s,
            music_envelope=list(music_envelope or []),
        )

        result: dict[str, Any] = {
            "status": "completed",
            "music_volume": music_volume,
            "preserve_original_audio": preserve_original_audio,
        }
        if self.storage is not None and getattr(self.storage, "enabled", False):
            container = getattr(self.settings, "output_container", "generated-media")
            blob_name = (
                f"agentic/remix/{candidate.get('candidate_id') or remix_id}/{remix_id}.mp4"
            )
            uploaded_blob = self.storage.upload_path(
                container=container,
                path=output_path,
                blob_name=blob_name,
                content_type="video/mp4",
            )
            result["blob_name"] = uploaded_blob
            if hasattr(self.storage, "generate_sas_url"):
                result["remixed_video_url"] = self.storage.generate_sas_url(
                    container=container, blob_name=uploaded_blob or blob_name
                )
            _release_workdir(workdir)
        else:
            result["remixed_video_local_path"] = str(output_path)
            _release_workdir(workdir, keep=output_path)
        return result

    async def sculpt_window(
        self,
        *,
        candidate: dict[str, Any],
        source_video_artifact_id: str,
        window_start_s: float,
        segments: Optional[list[tuple[float, float]]] = None,
        music_envelope: Optional[list[tuple[float, float, float]]] = None,
    ) -> dict[str, Any]:
        """Re-cut the take from a different point in its own full track.

        The matcher chose one window of a longer piece of music to sit under the
        video. That choice is sometimes just wrong — the drop lands after the cut,
        or the take opens on the tail of a phrase — and until now the only way to
        move it was to pay for a whole new generation. Nothing has to be
        generated: the full track is already on record, and both mixers already
        know how to seek into it.

        Free and instant, like adjust_remix, and injectable the same way. It gets
        its OWN injectable rather than reusing the remix one, whose signature is
        fixed at the call site.

        ``music_envelope`` is the mix's DURABLE envelope, handed in by the
        caller. A re-cut re-renders the file from scratch, so anything the user
        shaped earlier has to be re-applied or it is silently discarded — the
        fades survive on the card and vanish from the video.
        """

        try:
            start = max(0.0, float(window_start_s))
        except (TypeError, ValueError):
            raise ValueError("window_start_s must be a number.")

        cut_url = candidate.get("audio_url")
        full_url = candidate.get("complete_audio_url")
        if not full_url or full_url == cut_url:
            # The basic tier renders the video-length audio directly: there is no
            # longer track to move around inside. Say which case this is instead
            # of rendering an identical file and calling it a change.
            return {
                "status": "unavailable",
                "reason": "no_full_track",
                "message": (
                    "This take has no longer track behind it — its audio is exactly "
                    "the length of the video, so there is no other window to move to. "
                    "A new take on a higher tier would give one."
                ),
            }

        if self._window_fn is not None:
            result = await self._window_fn(
                candidate=candidate,
                source_video_artifact_id=source_video_artifact_id,
                window_start_s=start,
                segments=list(segments or []),
                music_envelope=list(music_envelope or []),
            )
            result.setdefault("window_start_s", start)
            return result

        artifact = self.get_source_video_artifact(source_video_artifact_id)
        video_path = await self._get_source_preparation().resolve_source_video(artifact)

        window_id = new_id("window")
        workdir = Path(getattr(self.settings, "workdir", "/tmp")) / "agentic_audio_window" / window_id
        workdir.mkdir(parents=True, exist_ok=True)
        music_path = workdir / "full.audio"
        await download_public_file_to_disk(
            url=str(self._refresh_signed_url(str(full_url))),
            destination=music_path,
            asset_label="audio",
        )

        from EdennCode.Util.MediaUtils import ffmpeg_utils

        video_duration = float(ffmpeg_utils.get_video_duration(Path(video_path)) or 0.0)
        full_duration = float(ffmpeg_utils.get_video_duration(music_path) or 0.0)
        if full_duration and video_duration:
            # Never seek so far in that the music runs out before the picture
            # does — that is the truncation failure mode, arriving through a new
            # door.
            start = min(start, max(0.0, full_duration - video_duration))

        output_path = workdir / "windowed_video.mp4"
        # An arrangement is assembled first, then treated exactly like any other
        # window: same mux, same measurement, same provenance. The only thing
        # that differs is where the audio came from.
        spliced_path: Optional[Path] = None
        if segments:
            from EdennCode.Util.MediaUtils.ffmpeg_utils import splice_audio_windows

            spliced_path = workdir / f"arranged{music_path.suffix or '.m4a'}"
            splice_audio_windows(music_path, spliced_path, windows=list(segments))
            music_path = spliced_path
            start = 0.0

        overlay_music_on_video(
            Path(video_path),
            music_path,
            output_path,
            music_volume=candidate_music_volume(candidate),
            preserve_original_audio=bool(candidate.get("preserve_original_audio") or False),
            music_start_s=start,
            music_envelope=list(music_envelope or []),
        )

        result: dict[str, Any] = {
            "status": "completed",
            "window_start_s": round(start, 2),
            "full_duration_s": round(full_duration, 2) if full_duration else None,
        }
        # Measure what the new window sounds like, here, while the track is
        # still on disk. The alternative is a candidate that carries the
        # PREVIOUS window's measurements — which is how a re-cut ends up
        # reporting the very fault it was asked to fix.
        signals = measure_window_signals(
            full_track_path=music_path,
            window_start_s=start,
            window_duration_s=video_duration,
        )
        if segments:
            result["segments"] = [
                {"start_s": seg_start, "duration_s": seg_dur}
                for seg_start, seg_dur in segments
            ]
            # Keep the arrangement itself, not only the video it was muxed into.
            # A later remix, compose or finalize re-renders from the candidate's
            # music source; without a durable file for the arrangement that
            # source is the unarranged full track, and the edit is silently
            # undone by the next thing the user does.
            if spliced_path is not None and spliced_path.exists():
                if self.storage is not None and getattr(self.storage, "enabled", False):
                    audio_container = getattr(
                        self.settings, "audio_container_name",
                        getattr(self.settings, "output_container", "generated-audio"),
                    )
                    arranged_blob = self.storage.upload_path(
                        container=audio_container,
                        path=spliced_path,
                        blob_name=(
                            f"agentic/arranged/"
                            f"{candidate.get('candidate_id') or window_id}/"
                            f"{window_id}{spliced_path.suffix or '.m4a'}"
                        ),
                        content_type="audio/mpeg",
                    )
                    if arranged_blob and hasattr(self.storage, "generate_sas_url"):
                        result["arranged_audio_url"] = self.storage.generate_sas_url(
                            container=audio_container, blob_name=arranged_blob
                        )
                else:
                    result["arranged_audio_local_path"] = str(spliced_path)
        if signals:
            result["take_signals"] = signals
        if self.storage is not None and getattr(self.storage, "enabled", False):
            container = getattr(self.settings, "output_container", "generated-media")
            blob_name = (
                f"agentic/window/{candidate.get('candidate_id') or window_id}/{window_id}.mp4"
            )
            uploaded_blob = self.storage.upload_path(
                container=container, path=output_path, blob_name=blob_name,
                content_type="video/mp4",
            )
            if hasattr(self.storage, "generate_sas_url"):
                result["remixed_video_url"] = self.storage.generate_sas_url(
                    container=container, blob_name=uploaded_blob or blob_name
                )
            _release_workdir(workdir)
        else:
            result["remixed_video_local_path"] = str(output_path)
            _release_workdir(workdir, keep=output_path)
        return result

    async def compose_mix(
        self,
        *,
        source_video_artifact_id: str,
        music_audio_url: Optional[str],
        voiceover_audio_url: Optional[str],
        sfx_audio_url: Optional[str] = None,
        music_volume: float = 0.85,
        voiceover_volume: float = 1.0,
        voiceover_start_s: float = 0.0,
        duck_gain_db: float = -9.0,
        sfx_volume: float = 1.0,
        voiceover_segments: Optional[list[tuple[float, float]]] = None,
        music_start_s: float = 0.0,
        preserve_original_audio: bool = False,
        music_envelope: Optional[list[tuple[float, float, float]]] = None,
    ) -> dict[str, Any]:
        """Layer EVERY completed audio layer (music, narration, SFX) onto the
        ORIGINAL video (cheap, re-runnable) — one master deliverable.

        No generation: this re-runs ffmpeg with the current mix parameters
        (layer volume ratios, ducking depth, and where the narration starts), so
        the user can iterate freely. Media work is injectable via ``compose_fn``
        for hermetic tests.
        """

        params = {
            "music_volume": float(music_volume),
            "voiceover_volume": float(voiceover_volume),
            "voiceover_start_s": float(voiceover_start_s),
            "duck_gain_db": float(duck_gain_db),
            "sfx_volume": float(sfx_volume),
            "preserve_original_audio": bool(preserve_original_audio),
        }
        # Not in `params`: those round-trip into state["mix"] and the tool-call
        # record as user-tunable knobs, and the envelope is a list of moments
        # that belongs on the mix in its own right — same reasoning as the
        # realized narration windows beside it.
        envelope = list(music_envelope or [])

        if self._compose_fn is not None:
            result = await self._compose_fn(
                source_video_artifact_id=source_video_artifact_id,
                music_audio_url=music_audio_url,
                voiceover_audio_url=voiceover_audio_url,
                sfx_audio_url=sfx_audio_url,
                # Explicitly, NOT via params: the realized narration windows are
                # not a user-tunable mix knob, and params round-trips into
                # state["mix"] and the tool-call record. Without this the injected
                # path (which is what the standalone deploy runs) fell back to one
                # flat duck across the whole narration while the in-process path
                # ducked per line — the shipped mix was quietly the worse one.
                voiceover_segments=voiceover_segments,
                music_start_s=float(music_start_s or 0.0),
                music_envelope=envelope,
                **params,
            )
            for key, value in params.items():
                result.setdefault(key, value)
            return result

        if not voiceover_audio_url and not sfx_audio_url:
            # Pure music-only sessions adjust via adjust_remix, not compose_mix;
            # with no narration and no SFX there is nothing to layer here.
            return {
                "status": "planned",
                "message": "compose_mix needs a voice-over or SFX layer (music optional).",
                **params,
            }

        artifact = self.get_source_video_artifact(source_video_artifact_id)
        video_path = await self._get_source_preparation().resolve_source_video(artifact)

        mix_id = new_id("mix")
        workdir = Path(getattr(self.settings, "workdir", "/tmp")) / "agentic_audio_mix" / mix_id
        workdir.mkdir(parents=True, exist_ok=True)

        async def _fetch(url: Optional[str], name: str) -> Optional[Path]:
            if not url:
                return None
            dest = workdir / name
            await download_public_file_to_disk(
                url=str(self._refresh_signed_url(str(url))),
                destination=dest,
                asset_label="audio",
            )
            return dest

        voiceover_path = await _fetch(voiceover_audio_url, "voiceover.audio")
        music_path = await _fetch(music_audio_url, "music.audio")
        sfx_path = await _fetch(sfx_audio_url, "sfx.audio")
        output_path = workdir / "mixed_video.mp4"
        # One MASTER graph for every layer subset (music ducked under the
        # narration, SFX bed at its own gain) — the single mixed deliverable.
        compose_master_mix_on_video(
            Path(video_path),
            output_path,
            music_path=music_path,
            voiceover_path=voiceover_path,
            sfx_path=sfx_path,
            music_volume=params["music_volume"],
            voiceover_volume=params["voiceover_volume"],
            voiceover_start_s=params["voiceover_start_s"],
            duck_gain_db=params["duck_gain_db"],
            voiceover_segments=voiceover_segments,
            music_start_s=float(music_start_s or 0.0),
            music_envelope=envelope,
            sfx_volume=params["sfx_volume"],
            preserve_original_audio=params["preserve_original_audio"],
        )

        result: dict[str, Any] = {"status": "completed", **params}
        # Listen back to the deliverable, here, while it is still on disk. Every
        # layer had a critic; the thing the layers were made for had none.
        mix_signals = music_take_signals(output_path)
        if mix_signals:
            result["mix_signals"] = mix_signals
        if self.storage is not None and getattr(self.storage, "enabled", False):
            container = getattr(self.settings, "output_container", "generated-media")
            blob_name = f"agentic/mix/{mix_id}.mp4"
            uploaded_blob = self.storage.upload_path(
                container=container, path=output_path, blob_name=blob_name, content_type="video/mp4"
            )
            result["blob_name"] = uploaded_blob
            if hasattr(self.storage, "generate_sas_url"):
                result["video_url"] = self.storage.generate_sas_url(
                    container=container, blob_name=uploaded_blob or blob_name
                )
            # Uploaded: nothing on disk here is the last copy of anything.
            _release_workdir(workdir)
        else:
            result["mixed_video_local_path"] = str(output_path)
            # No storage: the output IS the deliverable, but the downloaded
            # inputs beside it are copies of files that exist elsewhere.
            _release_workdir(workdir, keep=output_path)
        return result

    def edit_audio(
        self,
        *,
        session_id: str,
        source_video_artifact_id: str,
        parent_candidate: dict[str, Any],
        edit_kind: str,
        version: int,
        prompt: Optional[str] = None,
        extend_seconds: Optional[float] = None,
        creator_user_id: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        observation: Optional[dict[str, Any]] = None,
    ) -> MusicCandidateCard:
        """Branch a new candidate from ``parent_candidate`` via a fresh generation.

        ``regenerate`` re-runs generation with a tweaked prompt; ``extend``
        lengthens the track. Both reuse the existing ``video_music`` monolith
        enqueue path and return a versioned candidate linked to its parent.
        """

        parent_id = str(parent_candidate["candidate_id"])
        base_prompt = (prompt or parent_candidate.get("prompt") or "").strip()
        modelspec = normalize_music_modelspec(str(parent_candidate.get("modelspec") or ""))
        provider = parent_candidate.get("provider") or provider_for_modelspec(modelspec)
        candidate_id = f"{parent_id}_v{version}"
        parent_audio_url = parent_candidate.get("audio_url") or parent_candidate.get(
            "complete_audio_url"
        )

        # creative_edit needs an audio-to-audio provider AND an existing track to
        # restyle. When either is missing (a tier with no audio-to-audio path,
        # or the parent hasn't rendered yet) we fall back to a regenerate and
        # record the original ask.
        requested_edit_kind: Optional[str] = None
        effective_edit_kind = edit_kind
        if edit_kind == AGENT_EDIT_KIND_CREATIVE_EDIT and not (
            provider in CREATIVE_EDIT_PROVIDERS and parent_audio_url
        ):
            requested_edit_kind = AGENT_EDIT_KIND_CREATIVE_EDIT
            effective_edit_kind = AGENT_EDIT_KIND_REGENERATE

        # ----- creative_edit (audio-to-audio restyle via a dedicated job) -------
        if effective_edit_kind == AGENT_EDIT_KIND_CREATIVE_EDIT:
            edit_prompt = base_prompt or (
                "Reinterpret this track in a fresh style while keeping its core "
                "musical identity."
            )
            # Melody-guided restyles keep a LIGHTWEIGHT control prompt (the parent
            # audio is the main guide) — append only a compact footage grounding,
            # not the full scene arc.
            mp = (observation or {}).get("music_prompt") or {}
            grounding = []
            if mp.get("global_mood"):
                grounding.append(f"footage mood: {str(mp['global_mood'])[:120]}")
            if mp.get("tempo_bpm"):
                grounding.append(f"tempo ≈ {mp['tempo_bpm']} BPM")
            if grounding:
                edit_prompt = f"{edit_prompt} Stay aligned with the video — {'; '.join(grounding)}."
            linked_job_id = self._enqueue_audio_creative_edit_job(
                session_id=session_id,
                source_video_artifact_id=source_video_artifact_id,
                parent_candidate=parent_candidate,
                candidate_id=candidate_id,
                source_audio_url=str(self._refresh_signed_url(str(parent_audio_url))),
                edit_prompt=edit_prompt,
                modelspec=modelspec,
                creator_user_id=creator_user_id,
                actor_user_id=actor_user_id,
            )
            return MusicCandidateCard(
                candidate_id=candidate_id,
                proposal_id=str(parent_candidate.get("proposal_id") or parent_id),
                title=f"{parent_candidate.get('title') or 'Music Candidate'} (edit: creative_edit)",
                prompt=edit_prompt,
                modelspec=modelspec,
                include_vocals=bool(parent_candidate.get("include_vocals", False)),
                linked_job_id=linked_job_id,
                status="queued" if linked_job_id else "planned",
                parent_candidate_id=parent_id,
                version=version,
                edit_kind=AGENT_EDIT_KIND_CREATIVE_EDIT,
                music_volume=float(parent_candidate.get("music_volume") or 0.85),
                provider=provider,
            )

        # ----- regenerate / extend (reuse the video_music monolith path) --------
        if effective_edit_kind == AGENT_EDIT_KIND_EXTEND:
            target = f" to about {float(extend_seconds):.0f}s" if extend_seconds else ""
            derived_prompt = (
                f"{base_prompt} Extend the track{target}, continuing the same "
                "arrangement and energy without restarting."
            ).strip()
        else:  # regenerate (also the creative_edit fallback)
            derived_prompt = (
                base_prompt
                if prompt
                else (
                    f"{base_prompt} Regenerate a fresh variation with a distinct "
                    "arrangement while keeping the same overall direction."
                ).strip()
            )

        proposal = {
            "proposal_id": str(parent_candidate.get("proposal_id") or parent_id),
            "title": str(parent_candidate.get("title") or "Music Candidate"),
            "prompt": derived_prompt,
            "modelspec": modelspec,
            "include_vocals": bool(parent_candidate.get("include_vocals", False)),
            "vocal_gender": str(parent_candidate.get("vocal_gender") or "female"),
            "music_volume": float(parent_candidate.get("music_volume") or 0.85),
        }
        parent_provider_audio_id = parent_candidate.get("provider_audio_id")

        # Route an extend to the provider's native in-place extend when it both
        # supports it and we hold a track id; otherwise fall back to regenerating
        # a longer take. (Regenerate edits never extend in place.)
        extend_mode: Optional[str] = None
        extra_payload: dict[str, Any] = {
            "agentic_edit_kind": effective_edit_kind,
            "agentic_parent_candidate_id": parent_id,
        }
        if effective_edit_kind == AGENT_EDIT_KIND_EXTEND:
            if extend_seconds:
                extra_payload["extend_seconds"] = float(extend_seconds)
            # Three things have to be true, and the third is the one that was
            # missing: a provider that can extend, a handle on the parent track,
            # and something in this stack that actually performs the extension.
            native_capable = (
                NATIVE_EXTEND_CONSUMER_AVAILABLE
                and provider in NATIVE_EXTEND_PROVIDERS
                and bool(parent_provider_audio_id)
            )
            extend_mode = "native" if native_capable else "regenerate_fallback"
            extra_payload["agentic_extend_mode"] = extend_mode
            if native_capable:
                extra_payload["agentic_parent_provider"] = provider
                extra_payload["agentic_parent_provider_audio_id"] = parent_provider_audio_id
                if parent_candidate.get("provider_task_id"):
                    extra_payload["agentic_parent_provider_task_id"] = parent_candidate[
                        "provider_task_id"
                    ]

        fused_style_prompt = self.fuse_music_style_prompt(derived_prompt, observation)
        if fused_style_prompt:
            extra_payload["music_style_prompt"] = fused_style_prompt

        linked_job_id = self._enqueue_video_music_candidate_job(
            session_id=session_id,
            source_video_artifact_id=source_video_artifact_id,
            proposal=proposal,
            candidate_id=candidate_id,
            prompt=derived_prompt,
            creator_user_id=creator_user_id,
            actor_user_id=actor_user_id,
            extra_payload=extra_payload,
        )
        title_kind = requested_edit_kind or effective_edit_kind
        return MusicCandidateCard(
            candidate_id=candidate_id,
            proposal_id=str(proposal["proposal_id"]),
            title=f"{proposal['title']} (edit: {title_kind})",
            prompt=derived_prompt,
            modelspec=modelspec,
            include_vocals=bool(proposal["include_vocals"]),
            linked_job_id=linked_job_id,
            status="queued" if linked_job_id else "planned",
            parent_candidate_id=parent_id,
            version=version,
            edit_kind=effective_edit_kind,
            requested_edit_kind=requested_edit_kind,
            extend_mode=extend_mode,
            music_volume=float(proposal["music_volume"]),
            provider=provider,
        )

    def _enqueue_video_music_candidate_job(
        self,
        *,
        session_id: str,
        source_video_artifact_id: str,
        proposal: dict[str, Any],
        candidate_id: str,
        prompt: str,
        creator_user_id: Optional[str],
        actor_user_id: Optional[str] = None,
        extra_payload: Optional[dict[str, Any]] = None,
        job_id: Optional[str] = None,
    ) -> Optional[str]:
        if self.queue is None:
            return None
        source_artifact = self.get_source_video_artifact(source_video_artifact_id)
        job_id = job_id or new_id("job")
        linked_source_artifact_id = f"{job_id}:source_video:input"
        asset_ids = RecommendationAssetIds.create(job_id=job_id)
        modelspec = normalize_music_modelspec(str(proposal.get("modelspec") or "edenn_basic"))
        payload = {
            "source_video_artifact_id": linked_source_artifact_id,
            "requested_source_video_artifact_id": source_video_artifact_id,
            "modelspec": modelspec,
            "user_prompt": prompt,
            "include_vocals": bool(proposal.get("include_vocals", False)),
            "vocal_gender": str(proposal.get("vocal_gender") or "female"),
            "preserve_original_audio": False,
            "music_volume": float(proposal.get("music_volume") or 0.85),
            "water_mark": False,
            "compression_flag": True,
            "compression_max_height": 1280,
            "mode": "monolith",
            "max_attempts": 3,
            "agentic_session_id": session_id,
            "agentic_candidate_id": candidate_id,
            "video_id": asset_ids.video_id,
            "creative_id": asset_ids.creative_id,
            "primary_music_id": asset_ids.primary_music_id,
            "secondary_music_id": asset_ids.secondary_music_id,
            "selected_music_id": asset_ids.selected_music_id,
            "alignment_id": asset_ids.alignment_id,
            "job_received_timestamp": int(time.time()),
        }
        if extra_payload:
            payload.update(extra_payload)
        # Keep the session's own direction words in the record. On a verbose tier
        # the routing empties user_prompt, and the fused text keeps only the
        # first 700 characters of the direction — so without this the job row
        # would no longer hold what the user actually asked for.
        payload["agentic_direction_prompt"] = str(payload.get("user_prompt") or "")
        # Route the fused prompt into the slot this tier actually reads. Without
        # this the enqueue carried music_style_prompt that no deployment ever
        # consumed, and the music was generated from the bare direction text with
        # none of the footage grounding the session had just computed.
        payload.update(
            self.route_style_prompt(
                payload.get("music_style_prompt"),
                modelspec=modelspec,
                user_prompt=str(payload.get("user_prompt") or ""),
            )
        )
        self.async_repository.create_job(
            job_id=job_id,
            job_type="video_music",
            request_json=payload,
            session_id=session_id,
            creator_user_id=creator_user_id,
            actor_user_id=actor_user_id,
            priority=0,
            status=JobStatus.QUEUED,
        )
        if self.async_repository.get_artifact(linked_source_artifact_id) is None:
            metadata = dict(getattr(source_artifact, "metadata_json", None) or {})
            metadata["source_artifact_id"] = source_video_artifact_id
            metadata["source_asset_job_id"] = getattr(source_artifact, "job_id", None)
            metadata["linked_to_agentic_candidate_job"] = True
            self.async_repository.add_artifact(
                artifact_id=linked_source_artifact_id,
                job_id=job_id,
                artifact_type="source_video",
                role="input",
                container=getattr(source_artifact, "container", None),
                blob_name=getattr(source_artifact, "blob_name", None),
                url=getattr(source_artifact, "url", None),
                content_type=getattr(source_artifact, "content_type", None),
                local_path=getattr(source_artifact, "local_path", None),
                metadata_json=metadata,
            )
        task_id = new_id("task")
        queue_name = namespaced_queue_name("video-music-pipeline", settings=self.settings)
        self.queue.enqueue(
            TaskEnvelope(
                task_id=task_id,
                job_id=job_id,
                queue_name=queue_name,
                task_type="video_music_monolith",
                payload_json={
                    "source_video_artifact_id": linked_source_artifact_id,
                    "mode": "monolith",
                    "agentic_session_id": session_id,
                    "agentic_candidate_id": candidate_id,
                },
                priority=0,
                max_attempts=3,
                idempotency_key=f"{job_id}:agentic_video_music_monolith:v1",
            )
        )
        self.async_repository.add_event(
            job_id=job_id,
            event_type="job.created",
            stage_name="agentic_audio",
            message="Agentic audio candidate video-music job created.",
            payload_json={
                "task_id": task_id,
                "queue_name": queue_name,
                "task_type": "video_music_monolith",
                "agentic_session_id": session_id,
                "agentic_candidate_id": candidate_id,
            },
        )
        return job_id

    def _enqueue_audio_creative_edit_job(
        self,
        *,
        session_id: str,
        source_video_artifact_id: str,
        parent_candidate: dict[str, Any],
        candidate_id: str,
        source_audio_url: str,
        edit_prompt: str,
        modelspec: str,
        creator_user_id: Optional[str],
        actor_user_id: Optional[str] = None,
    ) -> Optional[str]:
        """Enqueue an audio-creative-edit job that restyles the parent's audio.

        The dedicated ``AudioCreativeEditWorker`` consumes this: it downloads the
        parent track + source video, runs the AudioCreativeEditWorkflow to restyle
        the audio, then re-muxes onto the source video. The job result mirrors the
        video_music contract (``audio_url``/``video_url``) so candidate hydration
        is unchanged.
        """

        if self.queue is None:
            return None
        # Validate the source video artifact exists (worker resolves it directly).
        self.get_source_video_artifact(source_video_artifact_id)
        job_id = new_id("job")
        asset_ids = RecommendationAssetIds.create(job_id=job_id)
        parent_id = str(parent_candidate.get("candidate_id") or "")
        payload = {
            "source_video_artifact_id": source_video_artifact_id,
            "source_audio_url": source_audio_url,
            "modelspec": modelspec,
            "user_prompt": edit_prompt,
            "include_vocals": bool(parent_candidate.get("include_vocals", False)),
            "vocal_gender": str(parent_candidate.get("vocal_gender") or "female"),
            "music_volume": float(parent_candidate.get("music_volume") or 0.85),
            "mode": "audio_creative_edit",
            "max_attempts": 3,
            "agentic_session_id": session_id,
            "agentic_candidate_id": candidate_id,
            "agentic_edit_kind": AGENT_EDIT_KIND_CREATIVE_EDIT,
            "agentic_parent_candidate_id": parent_id,
            "agentic_parent_provider": parent_candidate.get("provider"),
            "video_id": asset_ids.video_id,
            "creative_id": asset_ids.creative_id,
            "primary_music_id": asset_ids.primary_music_id,
            "selected_music_id": asset_ids.selected_music_id,
            "alignment_id": asset_ids.alignment_id,
            "job_received_timestamp": int(time.time()),
        }
        self.async_repository.create_job(
            job_id=job_id,
            job_type="audio_creative_edit",
            request_json=payload,
            session_id=session_id,
            creator_user_id=creator_user_id,
            actor_user_id=actor_user_id,
            priority=0,
            status=JobStatus.QUEUED,
        )
        task_id = new_id("task")
        queue_name = namespaced_queue_name(
            "audio-creative-edit-pipeline", settings=self.settings
        )
        self.queue.enqueue(
            TaskEnvelope(
                task_id=task_id,
                job_id=job_id,
                queue_name=queue_name,
                task_type="audio_creative_edit",
                payload_json={
                    "source_video_artifact_id": source_video_artifact_id,
                    "mode": "audio_creative_edit",
                    "agentic_session_id": session_id,
                    "agentic_candidate_id": candidate_id,
                },
                priority=0,
                max_attempts=3,
                idempotency_key=f"{job_id}:agentic_audio_creative_edit:v1",
            )
        )
        self.async_repository.add_event(
            job_id=job_id,
            event_type="job.created",
            stage_name="agentic_audio",
            message="Agentic audio creative-edit job created.",
            payload_json={
                "task_id": task_id,
                "queue_name": queue_name,
                "task_type": "audio_creative_edit",
                "agentic_session_id": session_id,
                "agentic_candidate_id": candidate_id,
            },
        )
        return job_id

    def enqueue_voiceover(
        self,
        *,
        session_id: str,
        source_video_artifact_id: str,
        script: str,
        voice_id: str,
        language: str = "",
        speed: float = 1.0,
        tone: str = "",
        segments: Optional[list[dict[str, Any]]] = None,
        video_duration_s: float = 0.0,
        cuts: Optional[list[float]] = None,
        speech_windows: Optional[list[list[float]]] = None,
        creator_user_id: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        reuse_segment_audio: Optional[dict[str, str]] = None,
    ) -> Optional[str]:
        """Enqueue a voice-over TTS job (Phase A2).

        The dedicated ``VoiceoverWorker`` consumes this: it TTS-es the approved
        script with the chosen preset voice and records the narration audio as a
        layer artifact. (Composing it onto the original video is A3.)
        """

        if self.queue is None:
            return None
        self.get_source_video_artifact(source_video_artifact_id)
        preset = voice_preset(voice_id)
        job_id = new_id("job")
        asset_ids = RecommendationAssetIds.create(job_id=job_id)
        voiceover_id = f"voiceover_{job_id}"
        # Tone steers TTS delivery/emotion; fall back to the preset's default.
        tone = (tone or "").strip()
        instructions = (
            f"{preset['instructions']} Tone: {tone}." if tone else preset["instructions"]
        )
        # Language was captured on the job and echoed back in the result, but
        # never reached synthesis — so asking for a Japanese read produced an
        # English one, with the request visible in the UI the whole time. The
        # synthesis boundary takes voice, instructions and speed and nothing
        # else, so the language belongs in the instructions. Built here rather
        # than in either renderer, so both the local path and the production
        # worker get it from one place.
        spoken_language = spoken_language_name(language)
        if spoken_language:
            instructions = (
                f"{instructions} Speak entirely in {spoken_language}, as a native "
                f"speaker would, with the natural rhythm and stress of that "
                f"language."
            )
        payload = {
            "mode": "voiceover",
            "script": script,
            "voice_id": preset["id"],
            "tts_voice": preset["voice"],
            "tts_instructions": instructions,
            "tone": tone,
            "speed": float(speed or 1.0),
            "language": language or "",
            # Timed, delivery-directed narration segments (video-informed). The
            # worker renders each with its own delivery and assembles them at
            # their start offsets onto the video timeline.
            "segments": list(segments or []),
            # Lines already recorded and unchanged, by id. The renderer copies
            # these in instead of synthesizing them again.
            **(
                {"reuse_segment_audio": dict(reuse_segment_audio)}
                if reuse_segment_audio else {}
            ),
            # The picture the plan is timed against. Without these the worker
            # can place lines relative to each other but not relative to the
            # video, so it cannot keep a line inside its shot or inside the clip.
            "video_duration_s": float(video_duration_s or 0.0),
            "cuts": list(cuts or []),
            # Where the footage speaks for itself; placement must not move a
            # line onto it while clearing a cut.
            "speech_windows": list(speech_windows or []),
            "source_video_artifact_id": source_video_artifact_id,
            "agentic_session_id": session_id,
            "agentic_voiceover_id": voiceover_id,
            "video_id": asset_ids.video_id,
            "creative_id": asset_ids.creative_id,
            "job_received_timestamp": int(time.time()),
        }
        self.async_repository.create_job(
            job_id=job_id,
            job_type="voiceover",
            request_json=payload,
            session_id=session_id,
            creator_user_id=creator_user_id,
            actor_user_id=actor_user_id,
            priority=0,
            status=JobStatus.QUEUED,
        )
        task_id = new_id("task")
        queue_name = namespaced_queue_name("voiceover-pipeline", settings=self.settings)
        self.queue.enqueue(
            TaskEnvelope(
                task_id=task_id,
                job_id=job_id,
                queue_name=queue_name,
                task_type="voiceover",
                payload_json={
                    "mode": "voiceover",
                    "agentic_session_id": session_id,
                    "agentic_voiceover_id": voiceover_id,
                },
                priority=0,
                max_attempts=3,
                idempotency_key=f"{job_id}:agentic_voiceover:v1",
            )
        )
        self.async_repository.add_event(
            job_id=job_id,
            event_type="job.created",
            stage_name="agentic_audio",
            message="Agentic audio voice-over job created.",
            payload_json={
                "task_id": task_id,
                "queue_name": queue_name,
                "task_type": "voiceover",
                "agentic_session_id": session_id,
                "agentic_voiceover_id": voiceover_id,
            },
        )
        return job_id

    # Back-compat alias: the agent used to reach for this private name directly.
    _enqueue_voiceover_job = enqueue_voiceover


    def enqueue_sfx(
        self,
        *,
        session_id: str,
        source_video_artifact_id: str,
        events: list[dict[str, Any]],
        summary: str = "",
        ambience: str = "",
        variant_id: str = "",
        creator_user_id: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        spotting: str = "plan",
        sfx_route: str = "auto",
        reuse_event_audio: Optional[dict[str, str]] = None,
    ) -> Optional[str]:
        """Enqueue a sound-effects render job (``video_sfx``).

        ``spotting`` decides who chooses the moments. ``"plan"`` renders the
        events the user approved, exactly where they put them. ``"engine"`` is
        the older behaviour — the plan is flattened into prose and the workflow
        spots the video itself, which is the right mode when nobody has said what
        they want but makes the plan card a suggestion box when they have.
        """

        if self.queue is None:
            return None
        self.get_source_video_artifact(source_video_artifact_id)
        job_id = new_id("job")
        asset_ids = RecommendationAssetIds.create(job_id=job_id)
        sfx_id = variant_id or f"sfx_{job_id}"
        payload = {
            "mode": "video_sfx",
            "events": list(events or []),
            # Effects already rendered and not being changed, by id.
            **(
                {"reuse_event_audio": dict(reuse_event_audio)}
                if reuse_event_audio else {}
            ),
            "sfx_summary": summary or "",
            "sfx_ambience": ambience or "",
            "sfx_spotting": str(spotting or "plan"),
            "source_video_artifact_id": source_video_artifact_id,
            "agentic_session_id": session_id,
            "agentic_sfx_id": sfx_id,
            # Which way to make this take: watch the footage, or write it from
            # the prompts the agent composed. Peers, and the session says which
            # one it asked for instead of the renderer deciding in silence.
            "agentic_sfx_route": str(sfx_route or "auto"),
            "video_id": asset_ids.video_id,
            "creative_id": asset_ids.creative_id,
            "job_received_timestamp": int(time.time()),
        }
        self.async_repository.create_job(
            job_id=job_id,
            job_type="video_sfx",
            request_json=payload,
            session_id=session_id,
            creator_user_id=creator_user_id,
            actor_user_id=actor_user_id,
            priority=0,
            status=JobStatus.QUEUED,
        )
        task_id = new_id("task")
        queue_name = namespaced_queue_name("sfx-pipeline", settings=self.settings)
        self.queue.enqueue(
            TaskEnvelope(
                task_id=task_id,
                job_id=job_id,
                queue_name=queue_name,
                task_type="video_sfx",
                payload_json={
                    "mode": "video_sfx",
                    "agentic_session_id": session_id,
                    "agentic_sfx_id": sfx_id,
                },
                priority=0,
                # SFX generation is deterministic-per-attempt but the render is a
                # single unit; one attempt (mirrors the multi-image contract).
                max_attempts=1,
                idempotency_key=f"{job_id}:agentic_sfx:v1",
            )
        )
        self.async_repository.add_event(
            job_id=job_id,
            event_type="job.created",
            stage_name="agentic_audio",
            message="Agentic audio SFX job created.",
            payload_json={
                "task_id": task_id,
                "queue_name": queue_name,
                "task_type": "video_sfx",
                "agentic_session_id": session_id,
                "agentic_sfx_id": sfx_id,
            },
        )
        return job_id


__all__ = ["AgenticAudioTools", "normalize_music_modelspec"]


def attach_cut_list(observation: dict[str, Any], video_path: Path) -> dict[str, Any]:
    """Record the video's REAL hard cuts on the observation, with their source.

    Scene boundaries are not the same thing as cuts, and the difference is the
    kind that ruins a take. Segmentation merges, splits and rounds them — on a
    16s reference clip it offered a boundary at 8.4s where the actual cut is at
    8.9s, plus three boundaries that are not cuts at all. A line written to end
    "just before the boundary" therefore lands half a second into the next shot.

    So the cut list is published separately from ``scenes``, and always with
    ``cut_source``: a detector that fell back to frame-difference heuristics
    invents cuts on fast motion, and an invented cut must never become a hard
    placement constraint. Callers should trust ``pyscenedetect`` and treat
    ``ffprobe_fallback`` as advisory.

    Never raises — detection is an enhancement, and analysis must not fail
    because a clip was awkward to scan.
    """

    from EdennCode.Util.MediaUtils import ffmpeg_utils

    try:
        import scenedetect  # noqa: F401
        have_pyscene = True
    except ImportError:
        have_pyscene = False

    cuts: list[float] = []
    source = "unavailable"
    try:
        if have_pyscene:
            # "content", not the "adaptive" default: adaptive smooths over the
            # cuts that matter most here. On the reference clip it reports only
            # 5.87 and 14.9 at every threshold, silently dropping the cuts at
            # 8.9 and 12.2 — and 12.2 is a real transition the sound-design pass
            # independently spotted. A cut list that misses half the cuts is
            # worse than none, because it reads as complete.
            cuts = ffmpeg_utils.detect_scene_cuts(
                Path(video_path), 0.3, detector="pyscenedetect",
                pyscene_method="content", min_scene_len_s=0.8,
            )
            source = "pyscenedetect"
        else:
            # The frame-difference fallback invents cuts on fast motion and, on
            # the reference clip, finds none at all — hence the honest label.
            cuts = ffmpeg_utils.detect_scene_cuts(Path(video_path), 0.3, detector="ffprobe")
            source = "ffprobe_fallback"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cut detection unavailable for %s: %s", video_path, exc)
        cuts, source = [], "unavailable"

    # t=0 is the start of the clip, not a cut to write around.
    observation["cuts"] = sorted({round(float(c), 3) for c in (cuts or []) if float(c) > 0.05})
    observation["cut_source"] = source
    return observation


# Scripts that do not put spaces between words. Counting whitespace tokens in
# these is not merely inaccurate, it is meaningless: a whole Japanese sentence
# comes back as one "word".
_UNSPACED_RANGES = (
    (0x3040, 0x30FF),   # hiragana + katakana
    (0x3400, 0x4DBF),   # CJK ext A
    (0x4E00, 0x9FFF),   # CJK unified
    (0xF900, 0xFAFF),   # CJK compatibility
    (0xAC00, 0xD7AF),   # hangul syllables
    (0x0E00, 0x0E7F),   # thai
)
# Measured on a real Japanese read: 13/2.43, 18/3.30 and 16/3.10 characters per
# second, so ~5.3. Deliberately slower here for the same reason the word rate is
# — under-estimating spoken length is what lets an over-long script through.
UNSPACED_CHARS_PER_SECOND = 4.8


def _is_unspaced(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _UNSPACED_RANGES)


def speech_units(text: str) -> tuple[int, str]:
    """How much speech this line is, and in what unit.

    Returns (count, unit) where unit is "characters" for scripts written without
    spaces and "words" otherwise, so callers can report a pace figure that means
    something in the language actually being spoken.
    """

    raw = str(text or "")
    unspaced = sum(1 for ch in raw if _is_unspaced(ch))
    if unspaced >= 4:
        # Count the punctuation too: it is pause time, which is duration.
        return sum(1 for ch in raw if not ch.isspace()), "characters"
    return len(raw.split()), "words"


def estimated_speech_seconds(text: str, *, words_per_second: float = 2.1) -> float:
    """Conservative spoken length of a line, in whichever script it is written.

    Every fit check in this pipeline was built on whitespace word counts, which
    silently degrade to nonsense in Japanese, Chinese, Korean and Thai — a live
    Japanese read reported 0.3 words/second and sailed through a budget check
    that thought the whole script took 1.4 seconds. It then overran the clip.
    """

    count, unit = speech_units(text)
    if not count:
        return 0.0
    if unit == "characters":
        return count / UNSPACED_CHARS_PER_SECOND
    return count / words_per_second


# Codes the analyzer reports, and the names a synthesiser can act on. "Speak
# entirely in en" is not an instruction; "Speak entirely in Japanese" is.
_LANGUAGE_NAMES = {
    "en": "English", "eng": "English",
    "ja": "Japanese", "jp": "Japanese", "jpn": "Japanese",
    "zh": "Chinese", "cmn": "Mandarin Chinese", "zh-cn": "Mandarin Chinese",
    "zh-tw": "Traditional Chinese", "ko": "Korean", "kor": "Korean",
    "es": "Spanish", "spa": "Spanish", "fr": "French", "fra": "French",
    "de": "German", "deu": "German", "it": "Italian", "ita": "Italian",
    "pt": "Portuguese", "por": "Portuguese", "ru": "Russian", "rus": "Russian",
    "ar": "Arabic", "ara": "Arabic", "hi": "Hindi", "hin": "Hindi",
    "th": "Thai", "tha": "Thai", "vi": "Vietnamese", "vie": "Vietnamese",
    "id": "Indonesian", "nl": "Dutch", "pl": "Polish", "tr": "Turkish",
}


def spoken_language_name(language: Optional[str]) -> str:
    """The language to instruct a read in, or "" to say nothing about it.

    Returns "" for English, which is the synthesiser's default — naming it adds
    a sentence of instruction that changes nothing. Also returns "" for a short
    token that is not a code we recognise: guessing wrong here produces a read
    in the wrong language, which is far worse than not asking.
    """

    raw = (language or "").strip()
    if not raw:
        return ""
    name = _LANGUAGE_NAMES.get(raw.lower())
    if name is None:
        if len(raw) <= 3 or raw.lower().count("-") == 1 and len(raw) <= 6:
            return ""          # an unrecognised code, not a language name
        name = raw             # already spelled out ("Japanese", "Brazilian Portuguese")
    return "" if name == "English" else name


_MEAN_VOLUME_DB_RE = r"mean_volume:\s*(-?[0-9.]+)"


def _window_loudness_db(path: Path, start_s: float, dur_s: float) -> Optional[float]:
    """Mean volume over one window, or None when it cannot be read."""

    import re
    import subprocess

    from EdennCode.Util.MediaUtils import ffmpeg_utils

    cmd = [
        ffmpeg_utils.resolve_ffmpeg_binary(), "-hide_banner", "-nostdin",
        "-ss", f"{max(0.0, start_s):.3f}", "-t", f"{max(0.05, dur_s):.3f}",
        "-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return None
    match = re.search(_MEAN_VOLUME_DB_RE, out.stderr or "")
    return round(float(match.group(1)), 1) if match else None


def _peak_and_mean_db(path: Path) -> tuple[Optional[float], Optional[float]]:
    """(max_volume, mean_volume) in dBFS for a whole file, or (None, None).

    One ffmpeg pass. The peak is what says "this is about to clip"; the mean is
    what says "this file is silence with a filename".
    """

    import re
    import subprocess

    from EdennCode.Util.MediaUtils import ffmpeg_utils

    cmd = [
        ffmpeg_utils.resolve_ffmpeg_binary(), "-hide_banner", "-nostdin",
        "-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    stderr = out.stderr or ""
    peak = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB", stderr)
    mean = re.search(_MEAN_VOLUME_DB_RE, stderr)
    return (
        round(float(peak.group(1)), 1) if peak else None,
        round(float(mean.group(1)), 1) if mean else None,
    )


#: Below this, a rendered effect is silence with a filename. Chosen well under
#: any real sound and well above the noise floor of an encoded file.
SILENT_EVENT_MEAN_DBFS = -60.0


def sfx_take_signals(
    rendered_events: list[dict[str, Any]],
    *,
    bed_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Measure a sound-effects take, at render time, where the files are local.

    The bed was the one rendered artifact nobody measured. "Rendered" meant the
    provider returned a path — not that anything is audible at the moment the
    plan asked for a sound. An effect that came back as silence looked exactly
    like an effect that worked, on the card, in the manifest and in the
    session, and the only way to find out was to play the video.

    Deterministic and rounded, because these values are copied into session
    state and a number that changes shape between identical runs rewrites the
    session on every hydrate poll.
    """

    signals: dict[str, Any] = {}
    levels: dict[str, float] = {}
    silent: list[str] = []
    unmeasured: list[str] = []
    for entry in rendered_events or []:
        event_id = str(entry.get("id") or "")
        path = str(entry.get("audio_path") or "")
        if not event_id:
            continue
        if not path or not Path(path).exists():
            unmeasured.append(event_id)
            continue
        _, mean = _peak_and_mean_db(Path(path))
        if mean is None:
            unmeasured.append(event_id)
            continue
        levels[event_id] = mean
        if mean <= SILENT_EVENT_MEAN_DBFS:
            silent.append(event_id)
    if levels:
        signals["event_level_dbfs"] = dict(sorted(levels.items()))
    if silent:
        signals["silent_event_ids"] = sorted(silent)
    if unmeasured:
        signals["unmeasured_event_ids"] = sorted(unmeasured)
    if bed_path and Path(bed_path).exists():
        peak, mean = _peak_and_mean_db(Path(bed_path))
        if peak is not None:
            signals["bed_peak_dbfs"] = peak
        if mean is not None:
            signals["bed_mean_dbfs"] = mean
    return signals


def sfx_listen_report(
    signals: Optional[dict[str, Any]],
    *,
    watched_the_video: Optional[bool] = None,
    not_watched_reason: str = "",
) -> dict[str, Any]:
    """What the effects bed sounds like, judged from what the render measured.

    Faults are mechanical and checkable. Nothing here guesses at craft: whether
    a sound SUITS a moment is not a thing arithmetic can answer, and a critic
    that pretends otherwise flattens the work it was meant to protect.
    """

    if not signals:
        return {}
    notes: list[str] = []
    observations: list[str] = []
    unchecked: list[str] = ["whether each sound suits its moment"]
    # Not a fault — prompt-written effects are a real product — but the user is
    # entitled to know which one they got, because it is the difference between
    # sound designed to the picture and sound designed to a description of it.
    if watched_the_video is False:
        detail = f" ({not_watched_reason})" if not_watched_reason else ""
        observations.append(
            "these effects were written from prompts, not from watching the "
            f"footage{detail}"
        )
        unchecked.append("how closely each sound tracks the movement on screen")

    silent = list(signals.get("silent_event_ids") or [])
    if silent:
        notes.append(
            f"{len(silent)} effect(s) came back silent: {', '.join(silent[:6])}"
        )
    unmeasured = list(signals.get("unmeasured_event_ids") or [])
    if unmeasured:
        unchecked.append(f"{len(unmeasured)} effect(s) could not be measured")

    peak = signals.get("bed_peak_dbfs")
    if isinstance(peak, (int, float)):
        if peak >= -0.5:
            notes.append("the effects bed peaks at or above full scale")
        elif peak >= -3.0:
            observations.append("the effects bed peaks close to full scale")
    else:
        unchecked.append("the level of the bed as a whole")

    levels = signals.get("event_level_dbfs") or {}
    if isinstance(levels, dict) and len(levels) >= 2:
        loudest = max(levels.values())
        quietest = min(levels.values())
        if loudest - quietest >= 24.0:
            observations.append(
                "the effects sit at very different levels "
                f"({quietest:.0f} to {loudest:.0f} dBFS)"
            )

    return {
        "clean": not notes,
        "notes": notes,
        "observations": observations,
        "unchecked": unchecked,
    }


def music_take_signals(
    cut_path: Optional[Path], full_path: Optional[Path] = None, *, windows: int = 9
) -> dict[str, Any]:
    """Measure a rendered take, at render time, where the files are local.

    This is the half of a listen-back report that CANNOT happen on the read
    path: hydration runs on a 2.5s poll inside a locked read-modify-write, so an
    ffmpeg probe there would hold a row open on every tick. Narration gets away
    with judging on hydrate only because its render already measured every take;
    music had nobody measuring at all.

    Never raises: a take that cannot be probed simply gets no report, which is
    the honest outcome — better than a fault invented from a failed measurement.
    """

    from EdennCode.Util.MediaUtils import ffmpeg_utils

    signals: dict[str, Any] = {}
    try:
        if cut_path and Path(cut_path).exists():
            cut = Path(cut_path)
            duration = float(ffmpeg_utils.get_video_duration(cut) or 0.0)
            if duration > 0:
                signals["cut_duration_s"] = round(duration, 2)
                active = ffmpeg_utils.detect_audio_activity(cut, duration_hint=duration)
                if active:
                    first_start = float(active[0][0])
                    last_end = float(active[-1][1])
                    signals["leading_silence_s"] = round(max(0.0, first_start), 2)
                    signals["trailing_silence_s"] = round(max(0.0, duration - last_end), 2)
                    gaps = [
                        round(float(nxt[0]) - float(cur[1]), 2)
                        for cur, nxt in zip(active, active[1:])
                        if float(nxt[0]) - float(cur[1]) > 1.5
                    ]
                    if gaps:
                        signals["internal_gaps_s"] = gaps
                else:
                    # No activity at all is itself the finding.
                    signals["leading_silence_s"] = round(duration, 2)
                    signals["trailing_silence_s"] = round(duration, 2)
                step = duration / max(1, windows)
                energy = [
                    _window_loudness_db(cut, index * step, step) for index in range(windows)
                ]
                if any(value is not None for value in energy):
                    signals["energy_windows_db"] = energy
        if full_path and Path(full_path).exists() and Path(full_path) != Path(cut_path or ""):
            full_duration = float(ffmpeg_utils.get_video_duration(Path(full_path)) or 0.0)
            if full_duration > 0:
                signals["full_duration_s"] = round(full_duration, 2)
    except Exception:  # noqa: BLE001 — a measurement is never worth a failed job
        logger.exception("music_take_signals failed; the take gets no listen report")
        return {}
    return signals


def measure_window_signals(
    *,
    full_track_path: Path,
    window_start_s: float,
    window_duration_s: float,
) -> dict[str, Any]:
    """Measure the audio a re-cut window actually presents.

    Both renderers of a re-cut mux the FULL track under the picture with a seek,
    so what the viewer hears is a slice that exists in no file of its own. This
    cuts that exact slice back out — :func:`extract_audio_window` mirrors the
    mux's seek, so the measured audio is the heard audio — and measures it.

    Without this the loop is open at its last step: the agent re-cuts a take to
    escape a dead tail and has no way to learn whether the new window is any
    better. Call it from a RENDER path, where the files are local and an ffmpeg
    probe is allowed; never from hydrate.

    Never raises. An unmeasurable re-cut is honestly unmeasured, which the
    report layer already renders as no report rather than a guess.
    """

    try:
        duration = float(window_duration_s)
        start = max(0.0, float(window_start_s))
    except (TypeError, ValueError):
        return {}
    if duration <= 0:
        return {}

    source = Path(full_track_path)
    cut_path = source.with_name(f"window_cut_{new_id('wc')}{source.suffix or '.m4a'}")
    try:
        from EdennCode.Util.MediaUtils import ffmpeg_utils

        ffmpeg_utils.extract_audio_window(
            source, cut_path, start_s=start, duration_s=duration
        )
        return music_take_signals(cut_path, source)
    except Exception:  # noqa: BLE001 — a measurement is never worth a failed re-cut
        logger.exception("window re-measurement failed; the re-cut goes unmeasured")
        return {}
    finally:
        try:
            cut_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("could not remove the temporary window cut %s", cut_path)


#: Beats kept for snapping. A video-length cut holds far fewer; the cap exists
#: so a full three-minute track cannot put a thousand floats in session state.
MAX_BEATS_KEPT = 256
#: Shorter than this is a moment, not a section.
MIN_SECTION_S = 4.0
#: Seconds of smoothing before the level is banded. Music moves constantly; a
#: section is a stretch that stays somewhere, not every dip between hits.
_SECTION_SMOOTH_S = 5
#: Sections are described relative to the track's OWN range, so a deliberately
#: quiet piece is not reported as one long lull.
_SECTION_BANDS = ("quiet", "mid", "full")


def _section_label(band: str, rising: Optional[bool]) -> str:
    """What to call a stretch of music, in words a person would use."""

    if band == "quiet":
        return "quiet"
    if band == "full":
        return "full"
    if rising is True:
        return "building"
    if rising is False:
        return "falling"
    return "steady"


def music_structure(audio_path: Optional[Path]) -> dict[str, Any]:
    """Where the beats and the sections are, measured at render time.

    Every editing verb in this product speaks in seconds, which means the user
    supplies seconds and the agent guesses. Music does not have seconds in it —
    it has beats and sections — so "make the chorus hit at 0:12" and "cut on the
    beat" were not requests this could act on, only paraphrase.

    This is the same bargain as the listen-back measurements beside it: run it
    where the file is local, store rounded numbers, and let the read path use
    them without touching a disk. Every value is rounded because it lands in
    session state, and session state is compared by equality on a 2.5s poll.

    Never raises. Music whose beat cannot be found is simply music this cannot
    snap to, and saying nothing is better than a confident grid that is wrong.

    One honest limitation, because it shows up in the reported tempo. Beat
    trackers make OCTAVE errors: a piece at 120 is often read as 60, especially
    when the material is sparse. The grid is still usable when that happens —
    the times it reports are real beats, just every other one — so snapping a
    cut to them remains musically right. ``tempo_bpm`` is therefore a
    description of the grid that was found, not a measurement to quote back to
    the user as fact.
    """

    if not audio_path or not Path(audio_path).exists():
        return {}
    try:
        import numpy as np

        from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.music_analyzer import (  # noqa: E501
            MusicAnalyzer,
        )

        analysis = MusicAnalyzer().analyze(str(audio_path))
        beats = [round(float(t), 2) for t in analysis.beat_times_s][:MAX_BEATS_KEPT]
        tempo = round(float(analysis.tempo_bpm), 1)

        structure: dict[str, Any] = {}
        if beats:
            structure["beat_times_s"] = beats
            structure["tempo_bpm"] = tempo
            # Every fourth beat, which is where a cut lands without sounding
            # like a mistake in almost all popular time.
            structure["downbeats_s"] = beats[::4]

        rms = np.asarray(analysis.rms_curve, dtype=float)
        if rms.size >= 4:
            hop_s = float(analysis.hop_length) / float(analysis.sr or 1)
            level_db = 20.0 * np.log10(np.maximum(rms, 1e-6))
            # One value per second, so a section boundary is a musical event
            # rather than a frame-level flicker.
            per_s = max(1, int(round(1.0 / max(hop_s, 1e-6))))
            trimmed = level_db[: (level_db.size // per_s) * per_s]
            if trimmed.size:
                seconds = trimmed.reshape(-1, per_s).mean(axis=1)
                # Smooth before banding. Music fluctuates second to second, and
                # banding the raw curve produced a map that flipped every few
                # seconds — which reads as structure while describing none.
                if seconds.size >= _SECTION_SMOOTH_S:
                    kernel = np.ones(_SECTION_SMOOTH_S) / _SECTION_SMOOTH_S
                    seconds = np.convolve(seconds, kernel, mode="same")
                low, high = float(seconds.min()), float(seconds.max())
                span = max(1e-6, high - low)
                bands = [
                    _SECTION_BANDS[min(2, int((value - low) / span * 3))]
                    for value in seconds
                ]
                # Runs of one band, then short ones folded into a neighbour.
                # Merging AFTER the fact rather than while walking means the
                # first run is treated like every other; a smoothing artifact at
                # the very start used to survive as its own one-second section.
                runs: list[list[int]] = []
                start = 0
                for index in range(1, len(bands) + 1):
                    if index == len(bands) or bands[index] != bands[start]:
                        runs.append([start, index])
                        start = index
                merged = True
                while merged and len(runs) > 1:
                    merged = False
                    for position, (run_start, run_end) in enumerate(runs):
                        if run_end - run_start >= MIN_SECTION_S:
                            continue
                        # Join whichever neighbour it is closer to in level.
                        before = runs[position - 1] if position else None
                        after = runs[position + 1] if position + 1 < len(runs) else None
                        target = before if after is None else (
                            after if before is None else (
                                before
                                if abs(seconds[before[0]:before[1]].mean()
                                       - seconds[run_start:run_end].mean())
                                <= abs(seconds[after[0]:after[1]].mean()
                                       - seconds[run_start:run_end].mean())
                                else after
                            )
                        )
                        target[0] = min(target[0], run_start)
                        target[1] = max(target[1], run_end)
                        runs.pop(position)
                        merged = True
                        break

                sections: list[dict[str, Any]] = []
                for run_start, run_end in runs:
                    window = seconds[run_start:run_end]
                    rising: Optional[bool] = None
                    if run_end - run_start >= 2:
                        if window[-1] > window[0] + 1.0:
                            rising = True
                        elif window[-1] < window[0] - 1.0:
                            rising = False
                    band = _SECTION_BANDS[
                        min(2, int((float(window.mean()) - low) / span * 3))
                    ]
                    sections.append({
                        "start_s": round(float(run_start), 2),
                        "end_s": round(float(run_end), 2),
                        "label": _section_label(band, rising),
                        "level_db": round(float(window.mean()), 1),
                    })
                if sections:
                    structure["sections"] = sections
        return structure
    except Exception:  # noqa: BLE001 — a grid nobody can find is not an error
        logger.exception("music_structure failed; this take simply has no grid")
        return {}


def snap_to_beat(
    time_s: float, structure: Optional[dict[str, Any]], *, tolerance_s: float = 0.35
) -> float:
    """Move a moment onto the nearest beat, when there is one close enough.

    Deliberately timid. A cut dragged half a bar to reach a beat is not the
    moment the user asked for any more, so beyond the tolerance the requested
    time wins and the music simply is not snapped.
    """

    beats = (structure or {}).get("beat_times_s") or []
    if not beats:
        return round(float(time_s), 2)
    nearest = min(beats, key=lambda beat: abs(float(beat) - float(time_s)))
    if abs(float(nearest) - float(time_s)) <= tolerance_s:
        return round(float(nearest), 2)
    return round(float(time_s), 2)


def music_alignment(
    signals: Optional[dict[str, Any]], *, observation: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """What the take actually sounds like against the video it was made for.

    A sibling of :func:`narration_alignment`, and it keeps that function's
    hard-won split: mechanical FAULTS go in ``notes``, craft goes in
    ``observations``. Music is where that discipline matters most — an energy
    arc that sits quiet for a third of the clip is a choice, and an agent handed
    it as a defect will regenerate a perfectly good take.

    Pure arithmetic over measurements taken at render time, with every float
    rounded, because this runs on the 2.5s poll and the write-churn guard
    compares dicts: an unstable value here rewrites the session forever.
    """

    if not signals:
        return {}

    obs = observation or {}
    notes: list[str] = []
    observations: list[str] = []

    # What this pass could NOT look at. A skipped check that reports clean is
    # indistinguishable from a check that passed, and both the finalize gate and
    # the prompt's "say what was checked" rule trusted `clean` on its own.
    unchecked: list[str] = []

    cut = signals.get("cut_duration_s")
    video = None
    try:
        video = float(obs.get("duration_s") or 0.0) or None
    except (TypeError, ValueError):
        video = None

    if not cut or not video:
        unchecked.append("whether the music covers the whole video")
    if cut and video and cut < video - 0.5:
        notes.append(
            f"the music stops {round(video - float(cut), 1)}s before the video ends"
        )
    lead = signals.get("leading_silence_s")
    if isinstance(lead, (int, float)) and lead > 0.75:
        notes.append(f"it opens with {round(float(lead), 1)}s of silence")
    tail = signals.get("trailing_silence_s")
    if isinstance(tail, (int, float)) and tail > 1.5:
        notes.append(f"it ends with {round(float(tail), 1)}s of silence")
    for gap in signals.get("internal_gaps_s") or []:
        notes.append(f"there is a {gap}s gap of near-silence in the middle")

    energy = [value for value in (signals.get("energy_windows_db") or []) if value is not None]
    if len(energy) >= 3:
        third = max(1, len(energy) // 3)
        opening = sum(energy[:third]) / third
        closing = sum(energy[-third:]) / third
        peak_at = energy.index(max(energy)) / max(1, len(energy) - 1)
        shape = "even"
        if closing - opening > 3.0:
            shape = "builds"
        elif opening - closing > 3.0:
            shape = "settles"
        elif 0.25 < peak_at < 0.75 and max(energy) - min(energy) > 4.0:
            shape = "peaks in the middle"
        observations.append(f"energy {shape} across the take")

    return {
        "clean": not notes,
        "notes": notes,
        "observations": observations,
        "unchecked": unchecked,
        "measured": {
            key: signals[key]
            for key in ("cut_duration_s", "full_duration_s", "leading_silence_s", "trailing_silence_s")
            if key in signals
        },
    }


def mix_alignment(
    signals: Optional[dict[str, Any]],
    *,
    observation: Optional[dict[str, Any]] = None,
    preserve_original_audio: bool = False,
) -> dict[str, Any]:
    """What the finished deliverable sounds like — the one artifact nobody heard.

    Every layer had a critic and the thing the layers were made for had none.
    The mix is what the user downloads and shares, and its historic failures are
    the loudest kind: a master truncated to the shortest audio stem, a tail of
    silence after the picture ends, a "final mix" that shipped with no music in
    it at all and was only discovered by playing the file.

    Deliberately coarse. Per-line duck depth is NOT measurable from the output —
    the ducked music is folded into one graph with everything else — so this
    does not pretend to judge the balance between stems. It judges the things a
    listener notices in the first two seconds and the last two.

    Pure arithmetic over measurements taken at compose time, every float
    rounded, because a report that changes shape between identical runs would
    rewrite the session on every poll.
    """

    if not signals:
        return {}

    obs = observation or {}
    notes: list[str] = []
    observations: list[str] = []
    unchecked: list[str] = []
    if not obs.get("duration_s"):
        unchecked.append("whether the mix covers the whole video")

    duration = signals.get("cut_duration_s")
    try:
        video = float(obs.get("duration_s") or 0.0) or None
    except (TypeError, ValueError):
        video = None

    # The truncation failure, which has shipped more than once: the master gets
    # cut to the shortest stem instead of running the length of the picture.
    if duration and video and float(duration) < video - 0.5:
        notes.append(
            f"the mix runs {round(video - float(duration), 1)}s short of the video"
        )
    if duration and video and float(duration) > video + 1.0:
        notes.append(
            f"the mix runs {round(float(duration) - video, 1)}s past the end of the video"
        )

    lead = signals.get("leading_silence_s")
    if isinstance(lead, (int, float)) and lead > 1.0:
        notes.append(f"it opens on {round(float(lead), 1)}s of silence")
    tail = signals.get("trailing_silence_s")
    if isinstance(tail, (int, float)) and tail > 1.5:
        notes.append(f"it ends on {round(float(tail), 1)}s of silence")

    # Nothing audible anywhere is the worst outcome and the easiest to miss:
    # a deliverable that looks finished in every field the UI reads.
    if (
        duration
        and isinstance(lead, (int, float))
        and isinstance(tail, (int, float))
        and float(lead) >= float(duration) - 0.05
    ):
        notes = [f"the mix is silent for its whole {round(float(duration), 1)}s"]

    energy = [v for v in (signals.get("energy_windows_db") or []) if v is not None]
    if len(energy) >= 3:
        loudest = max(energy)
        if loudest > -1.0:
            notes.append("it peaks close to clipping")
        elif loudest < -35.0:
            observations.append("the whole mix sits very quiet")
    if preserve_original_audio:
        observations.append("the video's own audio is kept under the mix")

    return {
        "clean": not notes,
        "notes": notes,
        "observations": observations,
        # This critic hears duration and silence. It cannot hear balance between
        # stems or true peak, and saying so is better than guessing: a critic
        # that invents faults it cannot measure pushes the agent to "fix" what
        # is already right.
        "unchecked": unchecked + ["balance between layers", "true peak level"],
        "measured": {
            key: signals[key]
            for key in ("cut_duration_s", "leading_silence_s", "trailing_silence_s")
            if key in signals
        },
    }


def sfx_render_diff(
    planned: Optional[list[dict[str, Any]]], rendered: Optional[list[dict[str, Any]]],
    *, tolerance_s: float = 0.25,
) -> dict[str, Any]:
    """How the render compares to the plan that was approved.

    Divergence is a DIFF, not a failure. Under engine spotting the workflow
    chooses its own moments by design, so calling that a defect would send the
    agent chasing a match it can never reach. Under plan spotting a drift is
    worth naming, because the user placed those moments.
    """

    # None means the render reported no manifest at all (a legacy job, a
    # placeholder). An EMPTY list is different and much worse: a real render that
    # placed nothing. Collapsing the two hid the failure the manifest exists to
    # expose.
    if rendered is None:
        return {}
    plan_rows = [row for row in (planned or []) if isinstance(row, dict)]
    render_rows = [row for row in rendered if isinstance(row, dict)]
    if not plan_rows and not render_rows:
        return {}

    def _start(row: dict[str, Any]) -> Optional[float]:
        try:
            return round(float(row.get("start_s")), 2)
        except (TypeError, ValueError):
            return None

    matched, moved, missing, silent = 0, [], [], []
    remaining = list(render_rows)
    for row in plan_rows:
        want = _start(row)
        if want is None:
            continue
        name = str(row.get("label") or row.get("id") or "an effect")
        hit = next(
            (r for r in remaining if _start(r) is not None
             and abs(_start(r) - want) <= tolerance_s),
            None,
        )
        if hit is None:
            missing.append(name)
            continue
        remaining.remove(hit)
        # A placed event that produced no audio is not a match. Per-event
        # generation failures are swallowed on purpose so one bad effect cannot
        # sink a batch — which means a provider outage returns a full manifest of
        # events that are all silent, and matching on time alone called that a
        # faithful render.
        if hit.get("rendered") is False:
            silent.append(name)
            continue
        drift = round(abs(_start(hit) - want), 2)
        matched += 1
        if drift > 0.05:
            moved.append(f"{name} moved {drift}s")

    diff = {
        "planned": len(plan_rows),
        "rendered": len(render_rows),
        "matched": matched,
        "moved": moved,
        "not_rendered": missing,
        "unplanned": len(remaining),
    }
    if silent:
        diff["silent"] = silent
    return diff


def narration_alignment(
    rendered: list[dict[str, Any]],
    *,
    planned: Optional[list[dict[str, Any]]] = None,
    observation: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """What the listener actually got, measured against what was planned.

    Nothing in this pipeline ever listened back. The agent enqueued a render and
    saw a status and a URL, so a line that drifted off its cue, or ended up
    running across a shot change, or came back at half the pace of its
    neighbour, was invisible until somebody played the file.

    No speech recognition is needed for any of that: the render already probes
    every take, so the realized windows are known exactly. Only word-level
    anchoring — which syllable lands on the beat — would need more, and that is
    deliberately out of scope here.

    Cheap, honest, and computed on every hydrate so the report is simply there
    rather than depending on anyone remembering to ask for it.
    """

    obs = observation or {}
    duration = float(obs.get("duration_s") or 0.0)
    # Cuts are trusted only from the real detector. When they are absent the
    # straddle check does not run at all — and used to report clean anyway,
    # which reads to the finalize gate exactly like a straddle-free take.
    cuts = ([float(c) for c in (obs.get("cuts") or [])]
            if obs.get("cut_source") == "pyscenedetect" else [])
    unchecked: list[str] = []
    if not cuts:
        unchecked.append("whether any line straddles a cut")
    if not duration:
        unchecked.append("whether narration overruns the video")
    planned_by_id = {
        str(p.get("id")): p for p in (planned or []) if isinstance(p, dict)
    }

    lines, rates = [], []
    units: set[str] = set()
    spoken_s = 0.0
    for seg in rendered or []:
        try:
            start = float(seg.get("start_s") or 0.0)
            dur = float(seg.get("duration_s") or 0.0)
        except (TypeError, ValueError):
            continue
        if dur <= 0:
            continue
        end = start + dur
        spoken_s += dur
        words, unit = speech_units(seg.get("text"))
        rate = words / dur if dur else 0.0
        rates.append(rate)
        units.add(unit)
        was = planned_by_id.get(str(seg.get("id")))
        drift = None
        if was is not None:
            try:
                drift = round(start - float(was.get("start_s") or 0.0), 2)
            except (TypeError, ValueError):
                drift = None
        crossed = [c for c in cuts if start < c < end - 0.01]
        over_source = next(
            (
                [round(float(a), 2), round(float(b), 2)]
                for a, b in (obs.get("speech_windows") or [])
                if min(end, float(b)) - max(start, float(a)) > 0.25
            ),
            None,
        )
        lines.append({
            "id": seg.get("id"),
            "window": [round(start, 2), round(end, 2)],
            "words": words,
            "unit": unit,
            "per_second": round(rate, 2),
            "drift_from_cue_s": drift,
            "crosses_cut": round(crossed[0], 2) if crossed else None,
            "over_source_audio": over_source,
            "overruns_video": bool(duration and end > duration + 0.05),
        })

    straddles = sum(1 for line in lines if line["crosses_cut"] is not None)
    overruns = sum(1 for line in lines if line["overruns_video"])
    talkovers = sum(1 for line in lines if line["over_source_audio"])
    moved = [line for line in lines
             if line["drift_from_cue_s"] is not None and abs(line["drift_from_cue_s"]) > 0.5]

    # Faults are mechanical: the read did something it was not asked to do.
    notes = []
    if straddles:
        notes.append(f"{straddles} line(s) still cross a shot change")
    if overruns:
        notes.append(f"{overruns} line(s) run past the end of the video")
    if talkovers:
        notes.append(
            f"{talkovers} line(s) talk over sound the footage is already making")
    if moved:
        notes.append(f"{len(moved)} line(s) moved more than 0.5s from their cue")

    # Craft is not. A pace that varies across lines with different directions is
    # an ARC, and the fix for the flat, undirected reads this pipeline used to
    # produce was more variation, not less — so reporting it as a defect would
    # push the agent to iron out exactly what it should be doing. Same for
    # coverage: sparse narration is the intent, never a gap to close.
    observations = []
    if len(rates) > 1 and len(units) == 1:
        # Reported in the unit of the script actually spoken: a Japanese read
        # measured in whitespace words reads as 0.3/sec, which is not a pace.
        observations.append(
            f"delivery pace ranges {min(rates):.1f}-{max(rates):.1f} "
            f"{units.pop()}/sec")
    if duration:
        observations.append(
            f"{round(spoken_s / duration * 100)}% of the clip carries speech")

    return {
        "lines": lines,
        "straddles": straddles,
        "overruns": overruns,
        "talkovers": talkovers,
        "spoken_s": round(spoken_s, 2),
        "coverage": round(spoken_s / duration, 2) if duration else None,
        "clean": not notes,
        "notes": notes,
        "observations": observations,
        "unchecked": unchecked,
    }


def _flag_straddles(
    out: list[dict[str, Any]], crossed: Any, dur: Any
) -> None:
    """Mark lines still crossing a cut after every legal repair was tried.

    Collision and overflow constraints outrank shot boundaries — a line pushed
    to avoid talking over its neighbour can land back across a cut, and that is
    the right trade. What must not happen is the caller believing it was fixed,
    so the survivors say so on the segment itself.
    """

    for seg in out:
        hits = crossed(float(seg["start_s"]), dur(seg))
        if hits:
            seg["crosses_cut"] = round(hits[0], 2)
        else:
            seg.pop("crosses_cut", None)


def attach_source_audio(observation: dict[str, Any], video_path: Path) -> dict[str, Any]:
    """Record where the footage is already making sound of its own.

    The system was deaf to its own input. ``preserve_original_audio`` is a bare
    boolean and nothing ever listened, so when someone spoke on camera the agent
    wrote a line straight over them, the render placed it, and the alignment
    report called the result clean — a fault the pipeline structurally could not
    see.

    This is presence, not recognition: loud-enough regions of the source track,
    from the detector already in the media utilities. No speech model, no new
    dependency. It cannot tell dialogue from a passing motorbike, which is why
    the windows are advisory for ranking and binding only for narration — not
    speaking over the footage's own voice is right in both cases.

    Never raises; a clip with no audio, or one that could not be scanned, simply
    reports nothing rather than failing the analysis.
    """

    duration = float(observation.get("duration_s") or 0.0)
    try:
        from EdennCode.Util.MediaUtils import ffmpeg_utils

        if not Path(video_path).exists():
            # Unreadable is not silent, and reporting it as silent would licence
            # narration to speak anywhere.
            raise FileNotFoundError(video_path)
        if not ffmpeg_utils.has_audio_stream(Path(video_path)):
            observation["source_audio"] = "silent"
            observation["speech_windows"] = []
            return observation
        regions = ffmpeg_utils.detect_audio_activity(
            Path(video_path), duration_hint=duration or None,
        )
        windows = [
            [round(float(a), 2), round(float(b), 2)]
            for a, b in (regions or [])
            if float(b) - float(a) >= 0.35
        ]
        # These windows only earn their keep as CONSTRAINTS, and a constraint
        # that covers most of the clip is not one — it just bans narration
        # everywhere. Real footage is the reason this is not a simple coverage
        # threshold: two reference clips came back as a single window over ~86%
        # of their runtime, which is obviously a bed (music, room tone, a mixed
        # track) rather than a moment to keep out of, yet sat under a 90% cut.
        #
        # So it is judged on what would be left: one window swallowing most of
        # the clip, or too little quiet remaining to place a line in, means the
        # track is continuous and there is nothing here to avoid. Presence
        # detection cannot tell dialogue from a soundtrack, and this is where
        # that limit is handled honestly rather than pretended away.
        covered = sum(b - a for a, b in windows)
        widest = max((b - a) for a, b in windows) if windows else 0.0
        if duration and (
            covered >= duration * 0.75
            or widest >= duration * 0.6
        ):
            observation["source_audio"] = "continuous"
            observation["speech_windows"] = []
            return observation
        observation["source_audio"] = "present" if windows else "silent"
        observation["speech_windows"] = windows
    except Exception as exc:  # noqa: BLE001
        logger.warning("Source-audio scan unavailable for %s: %s", video_path, exc)
        observation["source_audio"] = "unavailable"
        observation["speech_windows"] = []
    return observation


def attach_footage_signals(observation: dict[str, Any], video_path: Path) -> dict[str, Any]:
    """Every measured fact about the picture and its own audio, in one call.

    There are two analysis paths — the production pipeline and the local design
    server — and enriching the observation in each of them separately is how the
    local one ended up with cuts but no source-audio scan, which silently
    disarmed the talk-over gate: it had nothing to act on, so a line written
    straight across the footage's voice came back reported as clean.

    One entry point, so a signal added here reaches both by construction. This
    is the same drift that made the production renderer discard the timed plan.
    """

    attach_cut_list(observation, video_path)
    attach_source_audio(observation, video_path)
    return observation


def resolve_narration_timeline(
    segments: list[dict[str, Any]],
    *,
    video_duration_s: float,
    min_gap_s: float = 0.4,
    max_pull_back_s: float = 1.5,
    cuts: Optional[list[float]] = None,
    cut_clearance_s: float = 0.2,
    avoid_windows: Optional[list[list[float]]] = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Resolve PLANNED narration starts against the ACTUAL synthesized durations.

    The director plans starts before TTS runs, so it can't know how long each
    line takes to say. This pass keeps the plan wherever it already fits and
    repairs it where it doesn't:
      1. forward pass — a line never starts before the previous line has
         finished plus ``min_gap_s`` of air;
      2. tail recovery — if the last line would spill past the end of the clip,
         the OVERFLOWING TAIL ONLY is pulled earlier, by the least amount that
         makes it land, and never past ``max_pull_back_s`` from its cue.

    A planned start is a cue against the picture, so pulling a line earlier is a
    last resort, not a default: it moves the line off the moment it was written
    for. The repair therefore stops at the first line that already fits rather
    than cascading, and it never drags the whole read toward zero — a narration
    restacked from t=0 has no relationship to the footage left at all.

    Returns (adjusted segments, fits) — ``fits`` is False when the speech simply
    outlasts the clip. The sequence still comes back ordered and non-overlapping;
    the caller must surface the overflow rather than letting the mux clip it.

    Each segment needs ``start_s`` and ``duration_s``; ``start_s`` is adjusted
    in the returned copies.
    """

    out = [dict(s) for s in segments]
    if not out:
        return out, True

    def _dur(seg: dict[str, Any]) -> float:
        return max(0.0, float(seg.get("duration_s") or 0.0))

    # Remember the cues so the tail pass can bound how far it drifts from them.
    planned = [max(0.0, float(s.get("start_s") or 0.0)) for s in out]
    cut_list = sorted(float(c) for c in (cuts or []) if float(c) > 0.05)
    # Stretches the narration must not be moved onto, whatever else is true —
    # the footage's own voice. Snapping to clear a cut is a placement decision,
    # and a live run showed it sliding a line BACKWARDS onto someone speaking on
    # camera: the plan-time gate had passed, and the move undid it. A constraint
    # enforced at one stage and ignored at the next is not enforced.
    no_go = []
    for w in (avoid_windows or []):
        try:
            no_go.append((float(w[0]), float(w[1])))
        except (TypeError, ValueError, IndexError):
            continue

    def _hits_no_go(start: float, dur: float) -> bool:
        return any(min(start + dur, hi) - max(start, lo) > 0.25 for lo, hi in no_go)

    def _crossed(start: float, dur: float) -> list[float]:
        return [c for c in cut_list if start < c < start + dur - 0.01]

    # 0) snap to the shots. A line still being spoken when the picture changes
    # sounds like a mistake, and the writer cannot reliably avoid it: it plans
    # before the takes exist, so it is guessing at durations. Now that the real
    # ones are known, each line is nudged by the SMALLEST move that clears the
    # cut it crosses — either starting just after it, or finishing just before —
    # and only when that move is legal. This runs on the planned cues, before
    # collision repair, so a snap can still be overridden by the harder
    # constraints below rather than fighting them.
    if cut_list:
        for i, seg in enumerate(out):
            dur = _dur(seg)
            start = planned[i]
            crossing = _crossed(start, dur)
            if not crossing:
                continue
            if _hits_no_go(start, dur):
                # Already over the footage's voice: moving it for a cut would
                # only trade one fault for two. Leave it and let it be reported.
                continue
            cut = crossing[0]
            floor = 0.0 if i == 0 else planned[i - 1] + _dur(out[i - 1]) + min_gap_s
            ceil = video_duration_s - dur
            options = []
            after = cut + 0.05                      # begin on the new shot
            before = cut - cut_clearance_s - dur    # land clear of the change
            for cand in (after, before):
                if cand < floor - 0.001 or cand > ceil + 0.001 or cand < 0.0:
                    continue
                if _crossed(cand, dur):
                    continue                        # would only cross another
                if _hits_no_go(cand, dur):
                    continue                        # onto the footage's own voice
                options.append(cand)
            if options:
                planned[i] = min(options, key=lambda c: abs(c - start))

    # 1) forward: push late so lines never talk over each other.
    prev_end = 0.0
    for i, seg in enumerate(out):
        start = planned[i]
        if i > 0:
            start = max(start, prev_end + min_gap_s)
        seg["start_s"] = round(start, 2)
        prev_end = start + _dur(seg)

    if prev_end <= video_duration_s + 0.05:
        _flag_straddles(out, _crossed, _dur)
        return out, True

    # 2) tail recovery: walk back from the last line, pulling only what spills.
    latest_end = float(video_duration_s)
    for i in range(len(out) - 1, -1, -1):
        seg = out[i]
        start = float(seg["start_s"])
        overflow = (start + _dur(seg)) - latest_end
        if overflow <= 0.05:
            # This line already lands; everything before it was fine too.
            break
        floor = max(0.0, planned[i] - max_pull_back_s)
        if i > 0:
            prev = out[i - 1]
            floor = max(floor, float(prev["start_s"]) + _dur(prev) + min_gap_s)
        pulled = max(floor, start - overflow)
        seg["start_s"] = round(pulled, 2)
        if (pulled + _dur(seg)) > latest_end + 0.05:
            # Even at its floor this line overruns: report honestly rather than
            # shoving the lines before it off their cues to make room.
            _flag_straddles(out, _crossed, _dur)
            return out, False
        latest_end = pulled - min_gap_s

    _flag_straddles(out, _crossed, _dur)
    return out, True
