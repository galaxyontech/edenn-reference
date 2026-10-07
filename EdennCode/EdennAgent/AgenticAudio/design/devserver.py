"""
Edenn agentic-audio DEV SERVER — run the REAL router + agent loop locally with
zero external infra (no Postgres, storage, providers, or API keys required).

    # from the repo root, on Python 3.11+ (your .venv):
    python EdennCode/EdennAgent/AgenticAudio/design/devserver.py
    # then open:  http://127.0.0.1:8800/?backend=real

What it wires up:
  • The REAL FastAPI router (``create_agentic_audio_router``) with the intent gate
    ON — so this exercises the actual agent loop, event stream, and snapshot code.
  • The same in-memory fakes the test-suite uses (repos / queue), so nothing
    external is needed.
  • An offline "dev director" LLM so the walkthrough runs WITHOUT API keys. If
    ``AZURE_API_KEY`` / ``AGENTIC_AUDIO_AZURE_*`` are set, it uses your real model
    instead — a fully live agent.
  • REAL video understanding: when API keys are present, the uploaded clip is run
    through the production scene/vision pipeline (``preview_pre_generation``)
    locally — genuine scenes, summary, category, language. Falls back to honest
    metadata-only if keys are absent or analysis fails. Disable with
    EDENN_DEV_REAL_ANALYZE=0.
  • A tiny background job-completer so generated candidates hydrate to a short
    placeholder track (the loop completes visibly). NOTE: music generation itself
    is a placeholder tone — the analysis + agent + editing flows are real, the
    rendered audio is not.
  • The new design-system frontend (``./app``) served same-origin.

This file is a DEV harness and lives entirely inside the agentic_audio folder.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from datetime import datetime
import tempfile
import wave
from urllib.parse import urlencode
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import re
import shutil
import subprocess
import time

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from EdennCode.EdennAgent.AgenticAudio.api import create_agentic_audio_router
from EdennCode.EdennAgent.AgenticAudio.persistence.collab import InMemoryCollabRepository
from EdennCode.EdennAgent.AgenticAudio.tools import AgenticAudioTools
from EdennCode.EdennAgent.AgenticAudio.tools.media import (
    bound_observation,
    candidate_music_source,
    candidate_music_volume,
    measure_window_signals,
    music_structure,
    music_take_signals,
    normalize_music_modelspec,
)
from EdennCode.EdennAgent.AgenticAudio.models import (
    DEFAULT_CANDIDATE_COUNT_BY_MODELSPEC,
    provider_for_modelspec,
)
from EdennCode.Deployment.async_pipeline_v2.models import JobStatus, new_id

# Shared in-memory fakes (also used by the test suite). Kept OUT of Testing/ so
# the standalone container image — which excludes tests — can boot this server.
from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
    _MemoryAgenticRepository,
    _MemoryAsyncRepository,
    _MemoryQueue,
    _fake_analyze,
    _fake_remix,
    _seed_source_video,
)


def _music_provider_key_present(modelspec: str) -> bool:
    """Does this dev box have the key for the modelspec's music provider?"""
    provider = provider_for_modelspec(normalize_music_modelspec(modelspec))
    if provider == "provider_c":
        # Mirror the studio-tier key pool: the legacy bare key or ANY numbered
        # key counts.
        if os.getenv("PROVIDER_C_API_KEY"):
            return True
        return any(re.match(r"^PROVIDER_C_API_KEY_\d+$", k) and (v or "").strip() for k, v in os.environ.items())
    if provider == "provider_b":
        # Mirror the enhanced-tier key pool: ANY numbered key counts (gaps
        # allowed — e.g. only _3.._9 set), plus the legacy/explicit names.
        if os.getenv("PROVIDER_B_API_KEY") or os.getenv("EDENN_ENHANCED_PROVIDER_B_API_KEY"):
            return True
        return any(re.match(r"^PROVIDER_B_API_KEY_\d+$", k) and (v or "").strip() for k, v in os.environ.items())
    if provider == "provider_a":
        return bool(os.getenv("PROVIDER_A_API_KEY"))
    return False


# This harness exists so its frontend can be edited and looked at; a browser
# holding on to any of it is always wrong here. Every file served from the
# console root answers with this.
_DEV_NO_STORE = {"Cache-Control": "no-store, must-revalidate"}


def _console_cache_headers(name: str, versioned: bool) -> dict[str, str]:
    """index.html must revalidate; a ?v=-stamped asset may be cached hard.

    Nothing set a cache directive at all. The document carries the ?v= stamps
    that bust every other file and had none of its own, so a browser applying
    heuristic freshness to it kept asking for yesterday's scripts after a deploy
    — and only a hard reload fixed that, which nothing in the product could ask
    the user to do. An asset fetched WITHOUT its version stamp is not cached
    hard: its URL would not change on the next deploy, and a year-long cache
    entry for it is the same bug wearing different clothes.
    """

    if name.endswith(".html") or not versioned:
        return {"Cache-Control": "no-cache"}
    return {"Cache-Control": "public, max-age=31536000, immutable"}


def rendered_modelspec(result: Any, requested: str) -> str:
    """The tier that ACTUALLY rendered, read off the workflow result.

    Echoing the request straight back made "asked for" and "got" equal by
    construction, which silently disarmed the honesty check downstream (it sets
    ``requested_modelspec`` only when the two differ, so it could never fire).
    The workflow re-resolves the tier on its own in at least one case — vocals in
    some languages route to a different tier — and the take would still have
    carried the label of what was asked for.
    """

    return normalize_music_modelspec(
        getattr(result, "used_music_model_spec", "") or requested
    )


def _real_music_enabled() -> bool:
    """Run REAL music generation for jobs whose provider key exists.
    Opt out with EDENN_DEV_REAL_MUSIC=0 to avoid provider cost while iterating."""
    return os.getenv("EDENN_DEV_REAL_MUSIC", "").strip() not in {"0", "false", "no"}


def _voiceover_key_present() -> bool:
    """The hosted speech provider backs the real voice-over synthesizer."""
    return bool(os.getenv("AZURE_API_KEY") or os.getenv("AGENTIC_AUDIO_AZURE_API_KEY"))


def _real_voiceover_enabled() -> bool:
    """Run REAL Azure-TTS voice-over in dev. Opt out with EDENN_DEV_REAL_VOICEOVER=0."""
    return os.getenv("EDENN_DEV_REAL_VOICEOVER", "").strip() not in {"0", "false", "no"}


def _sfx_key_present() -> bool:
    """The hosted audio provider backs the real text-to-sound-effect path."""
    return bool(os.getenv("PROVIDER_A_API_KEY"))


def _real_sfx_enabled() -> bool:
    """Run the REAL video→SFX pipeline in dev. Opt out with EDENN_DEV_REAL_SFX=0.
    Follows the same master switch as music so `EDENN_DEV_REAL_MUSIC=0` also
    silences SFX cost unless SFX is explicitly re-enabled."""
    explicit = os.getenv("EDENN_DEV_REAL_SFX", "").strip()
    if explicit:
        return explicit not in {"0", "false", "no"}
    return _real_music_enabled()

HERE = Path(__file__).resolve().parent
# The console moved out of design/app to the package's top-level frontend/.
APP_DIR = HERE.parent / "frontend"
PORT = int(os.getenv("EDENN_DEV_PORT", "8800"))
HOST = os.getenv("EDENN_DEV_HOST", "127.0.0.1")


# ---------------------------------------------------------------------------
# Offline "dev director" — stands in for the agent LLM so the walkthrough runs
# with no API keys. It reads the live state summary the agent feeds it and:
#   • proposes two directions once the modality (production plan) is chosen,
#   • otherwise idles politely. Card-driven steps (approve, finalize) need no LLM.
# ---------------------------------------------------------------------------
class DevDirectorClient:
    _USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    async def complete_messages(
        self, messages: list[dict[str, Any]], *, json_schema: Any = None, max_tokens: int | None = None
    ) -> tuple[dict[str, Any], dict[str, int]]:
        state = self._latest_state(messages)
        plan = state.get("production_plan") or {}
        layers = plan.get("layers") or []
        vo_only = ("voiceover" in layers) and ("music" not in layers)
        voiceover = (state.get("layers") or {}).get("voiceover") or {}
        proposals = state.get("proposals") or []
        # Voice-over-only session → go straight to a script draft, never music.
        if vo_only and not voiceover.get("script"):
            return {
                "thought": "Narration-only session — draft a script, no music.",
                "intent": "add_voiceover",
                "assistant_message": "Here's a script draft — tweak the voice or wording, then generate.",
                "action": {
                    "type": "call_tool",
                    "tool_name": "propose_script",
                    "tool_args": {
                        "script": "Discover the difference. Crafted to move with every cut — and made for you.",
                        "voice_id": "narrator_male",
                        "language": "en",
                    },
                },
            }, dict(self._USAGE)
        if not proposals and not vo_only:
            decision = {
                "thought": "Modality chosen — offer two concrete directions against the cuts.",
                "intent": "request_proposals",
                "assistant_message": "Here are a few directions for your video.",
                "action": {
                    "type": "propose",
                    "proposals": [
                        {
                            "proposal_id": "proposal_bright",
                            "title": "Bright & Uplifting",
                            "prompt": "Energetic pop-inspired score with light percussion, bright synths, and a motivating forward momentum — perfect for a short, upbeat video. Keep transitions smooth for easy narration placement.",
                            "modelspec": "edenn_enhanced",
                            "include_vocals": False,
                            "music_volume": 0.85,
                        },
                        {
                            "proposal_id": "proposal_minimal",
                            "title": "Minimal Tech Flow",
                            "prompt": "Clean electronic groove with subtle melodic layers and crisp accents — a balanced backdrop that leaves room for narration.",
                            "modelspec": "edenn_basic",
                            "include_vocals": False,
                            "music_volume": 0.8,
                        },
                    ],
                },
            }
            return decision, dict(self._USAGE)
        decision = {
            "thought": "",
            "intent": "other",
            "assistant_message": "Got it — pick a track above, or tell me what to change.",
            "action": {"type": "noop"},
        }
        return decision, dict(self._USAGE)

    @staticmethod
    def _latest_state(messages: list[dict[str, Any]]) -> dict[str, Any]:
        for message in reversed(messages):
            content = message.get("content") or ""
            if isinstance(content, str) and content.startswith("Current session state:"):
                try:
                    return json.loads(content.split("\n", 1)[1])
                except Exception:
                    return {}
        return {}


class RetryingLLM:
    """Wraps the LIVE model with a few retries. On persistent failure it RAISES,
    so the agent surfaces an honest error rather than silently swapping in canned
    output. With keys present you get the real model — never a placeholder."""

    def __init__(self, primary: Any, attempts: int = 3) -> None:
        self.primary = primary
        self.attempts = max(1, attempts)

    async def complete_messages(self, messages, *, json_schema=None, max_tokens=None):
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                return await self.primary.complete_messages(
                    messages, json_schema=json_schema, max_tokens=max_tokens
                )
            except Exception as exc:  # noqa: BLE001
                last = exc
                print(f"[devserver] live LLM call failed (attempt {attempt + 1}/{self.attempts}): {str(exc)[:140]}")
                await asyncio.sleep(0.6 * (attempt + 1))
        raise last if last is not None else RuntimeError("LLM call failed")


def build_llm_client() -> tuple[Any, str, bool]:
    """Live Azure model whenever keys are configured. The offline dev director is
    used ONLY when explicitly forced (EDENN_DEV_FORCE_DIRECTOR=1) or no keys exist
    — there is no silent fallback to a placeholder. Returns (client, label, live)."""
    forced = os.getenv("EDENN_DEV_FORCE_DIRECTOR", "").strip() in {"1", "true", "yes"}
    has_keys = bool(os.getenv("AGENTIC_AUDIO_AZURE_API_KEY") or os.getenv("AZURE_API_KEY"))
    if not forced and has_keys:
        try:
            from EdennCode.EdennAgent.AgenticAudio.agent import build_agentic_audio_agent_client

            return RetryingLLM(build_agentic_audio_agent_client()), "live LLM", True
        except Exception as exc:  # noqa: BLE001
            print(f"[devserver] could not build the live LLM client: {exc}")
            raise
    reason = "forced offline" if forced else "no API keys found"
    return DevDirectorClient(), f"offline dev director ({reason})", False


def _real_analysis_enabled() -> bool:
    """Run the genuine scene/vision pipeline locally when keys exist and we're not
    forced offline. The pipeline operates on the local file with no blob/storage
    (verified), so dev gets the same understanding production does. Opt out with
    EDENN_DEV_REAL_ANALYZE=0."""
    if os.getenv("EDENN_DEV_REAL_ANALYZE", "").strip() in {"0", "false", "no"}:
        return False
    if os.getenv("EDENN_DEV_FORCE_DIRECTOR", "").strip() in {"1", "true", "yes"}:
        return False
    return bool(os.getenv("AGENTIC_AUDIO_AZURE_API_KEY") or os.getenv("AZURE_API_KEY"))


def _metadata_only_observation(artifact, user_prompt: str, modelspec: str) -> dict[str, Any]:
    """Honest fallback: real file metadata, NULL for anything we can't know — so
    the UI never fabricates understanding."""
    md = dict(getattr(artifact, "metadata_json", None) or {})
    duration = float(md.get("duration") or md.get("duration_s") or 0.0)
    filename = md.get("filename") or ""
    title = Path(filename).stem if filename else "your video"
    return {
        "duration_s": duration,
        "width": md.get("width"),
        "height": md.get("height"),
        "video_title": title,
        "video_description": "",
        "scenes": [],                       # not analyzed in this fallback
        "detected_language": None,          # unknown — don't fabricate
        "detected_category": "VIDEO",
        "detected_include_vocals": None,    # unknown — don't claim "no dialogue"
        "detected_vocal_gender": None,
        "sanitized_prompt": user_prompt,
        "music_prompt": {},
        "suggested_modelspec": modelspec,
        "analysis_mode": "metadata-only (dev)",
    }


def _meter_understanding(result: Any, *, artifact: Any, failed: bool = False) -> None:
    """Record what understanding a video cost.

    The single largest spend in the product and, until now, the only one with
    no record at all: one vision call per scene window, up to thirty, plus the
    prompt and summary calls around them. The pipeline measures its own tokens
    and hands them back on the result — the studio's boundary built an
    observation out of that same object and dropped the numbers.

    It is also unattributed to any turn: the session bootstrap runs it and
    returns before the loop records a turn, so a per-turn meter would never see
    it either.
    """

    from EdennCode.EdennAgent.AgenticAudio.persistence.usage import (
        Measurement,
        OUTCOME_DELIVERED,
        OUTCOME_FAILED_AFTER_SPEND,
        SITE_UNDERSTANDING,
        active_meter,
        analysis_key,
    )

    meter = active_meter()
    if not meter.enabled:
        return
    session_id = str(
        (dict(getattr(artifact, "metadata_json", None) or {})).get("agentic_session_id")
        or ""
    ) or None
    try:
        if failed or result is None:
            meter.record_once(
                meter_key=analysis_key(session_id or "orphan"),
                site=SITE_UNDERSTANDING,
                outcome=OUTCOME_FAILED_AFTER_SPEND,
                measurement=Measurement(note_code="analysis_failed_after_spend"),
                session_id=session_id,
            )
            return
        usage = getattr(result, "token_usage", None) or {}
        metadata = getattr(result, "video_metadata", None)
        duration = float(getattr(metadata, "duration", 0.0) or 0.0)
        meter.record_once(
            meter_key=analysis_key(session_id or "orphan"),
            site=SITE_UNDERSTANDING,
            outcome=OUTCOME_DELIVERED,
            measurement=Measurement(
                source_ms=round(duration * 1000) if duration > 0 else None,
                items=len(getattr(result, "scenes", None) or []),
                lm_input_tokens=usage.get("prompt_tokens"),
                lm_output_tokens=usage.get("completion_tokens"),
                primary_unit="source_ms",
            ),
            session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001 - never fail an analysis to record it
        print(f"[devserver] usage meter: understanding not recorded ({str(exc)[:120]})")


async def dev_analyze(*, artifact, user_prompt: str = "", modelspec: str = "edenn_basic"):
    """Real scene/vision understanding on the uploaded file when keys are present
    (the production ``preview_pre_generation`` runs locally with no blob), with an
    honest metadata-only fallback so the agent never fabricates understanding."""
    thumbnail_url = (dict(getattr(artifact, "metadata_json", None) or {})).get("thumbnail_url")
    local = getattr(artifact, "local_path", None)
    path = Path(local) if local else None
    if _real_analysis_enabled() and path and path.exists():
        try:
            from EdennCode.Deployment.workflows import VideoGenerationOrchestrator
            from EdennCode.EdennAgent.AgenticAudio.tools.media import AgenticAudioTools

            result = await VideoGenerationOrchestrator(storage=None).preview_pre_generation(
                video_path=path, user_prompt=user_prompt or "", modelspec=modelspec
            )
            observation = AgenticAudioTools._observation_from_pre_generation(result)
            _meter_understanding(result, artifact=artifact)
            from EdennCode.EdennAgent.AgenticAudio.tools.media import (
                attach_footage_signals,
            )
            attach_footage_signals(observation, path)
            observation["analysis_mode"] = "real (local pipeline)"
            observation["thumbnail_url"] = thumbnail_url
            print(f"[devserver] real analysis ✓ — {len(observation.get('scenes') or [])} scene(s), "
                  f"cuts={observation.get('cuts')} ({observation.get('cut_source')}), "
                  f"source_audio={observation.get('source_audio')}, "
                  f"category={observation.get('detected_category')!r}")
            return observation
        except Exception as exc:  # noqa: BLE001
            print(f"[devserver] real analysis FAILED ({str(exc)[:160]}) — falling back to metadata-only.")
            # The vision calls may already have been paid for before this
            # raised, and the metadata-only fallback that follows looks exactly
            # like a session that never analysed anything.
            _meter_understanding(None, artifact=artifact, failed=True)
    observation = _metadata_only_observation(artifact, user_prompt, modelspec)
    observation["thumbnail_url"] = thumbnail_url
    return observation


def probe_duration(path: Path) -> float | None:
    """Best-effort real duration via ffprobe; None if unavailable."""
    ff = shutil.which("ffprobe")
    if not ff:
        return None
    try:
        out = subprocess.run(
            [ff, "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def probe_dimensions(path: Path) -> tuple[int, int] | None:
    """Best-effort real pixel size via ffprobe; None if unavailable.

    Every uploaded source artifact was recorded as 1080x1920 regardless of the
    file — the audit's clip was 480x854. A fabricated shape is worse than a
    missing one: it is indistinguishable from a measurement, and anything
    downstream that frames or crops to it is working from fiction.
    """

    ff = shutil.which("ffprobe")
    if not ff:
        return None
    try:
        out = subprocess.run(
            [ff, "-v", "quiet", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        # Rotated videos make ffprobe emit a trailing separator; split and keep
        # the first two numbers rather than assuming exactly two fields.
        parts = [p for p in out.stdout.strip().replace("\n", "x").split("x") if p.strip()]
        width, height = int(parts[0]), int(parts[1])
        return (width, height) if width > 0 and height > 0 else None
    except Exception:
        return None


class _RealGenerationFailed(Exception):
    """A real generation ran and did not produce usable media.

    Raised only once an attempt is actually made — the not-attempted cases
    (no key, no source clip, nothing real to restyle) still return None and
    fall back to a badged placeholder, which is honest dev behaviour. An
    attempt that FAILED is different: completing the job with a stand-in tone
    presents a sine wave as the user's take for a generation that was paid for,
    with only ``placeholder: true`` to give it away.
    """


def make_placeholder_wav(path: Path, seconds: float = 6.0, freq: float = 220.0) -> None:
    """A quiet sine tone so generated candidates have a playable URL. The length
    matches the video so the placeholder *fits the cut* (dev only — the real
    pipeline renders a full track sized to the video)."""
    import math

    rate = 22050
    seconds = max(1.0, min(float(seconds), 600.0))  # clamp to something sane
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            # very low amplitude so it is unobtrusive in a demo
            val = int(1500 * math.sin(2 * math.pi * freq * (i / rate)))
            frames += int(val).to_bytes(2, "little", signed=True)
        w.writeframes(bytes(frames))


def extract_poster(video_path: Path, out_path: Path) -> bool:
    """Grab a representative still from the video (~1s in) so cards show a real
    thumbnail. Best-effort: returns False if ffmpeg is unavailable or fails."""
    ff = shutil.which("ffmpeg")
    if not ff:
        return False
    try:
        subprocess.run(
            [ff, "-y", "-ss", "1", "-i", str(video_path), "-frames:v", "1",
             "-vf", "scale=640:-2", str(out_path)],
            capture_output=True, timeout=20,
        )
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception:
        return False


# ---- upload guardrails ----------------------------------------------------
# Every upload is parsed by ffprobe and then handed to the analysis pipeline, so
# an unbounded one is both a disk problem and a compute problem. The numbers are
# deliberately generous for the product (a scored clip is seconds, not hours) and
# still small enough that a single request cannot fill the container.
MAX_UPLOAD_BYTES = int(os.getenv("EDENN_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
MAX_UPLOAD_SECONDS = float(os.getenv("EDENN_MAX_UPLOAD_SECONDS", "600"))
ALLOWED_UPLOAD_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
UPLOAD_CHUNK = 1024 * 1024


def build_app() -> FastAPI:
    # Structured logs, before anything else can emit one. The deployed console's
    # only telemetry was print(), which the platform collects as unsearchable
    # text with no request, session or user attached to it.
    from EdennCode.EdennAgent.AgenticAudio.api.observability import (
        configure_logging,
        install_request_context,
    )

    configure_logging()
    # Load the repo .env so AZURE_*/AGENTIC_AUDIO_AZURE_* are available for the
    # live LLM on a developer machine (no-op if there's no .env) — then SCRUB
    # the platform's database variables back out of this process.
    #
    # That file points PGHOST/DATABASE_URL at the platform's servers. Agentic
    # Audio itself never reads those names (config.py, enforced by a contract
    # test), but shared modules imported into this process might, and twice this
    # month that ambient path put studio writes on a production database. The
    # studio's own database is named by AGENTIC_AUDIO_PG_*, which this leaves
    # alone.
    try:
        from EdennCode.env import load_env

        load_env()
    except Exception:
        pass
    for _shared_db_var in (
        "PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGSSLMODE",
        "DATABASE_URL", "POSTGRES_HOST", "POSTGRES_DSN",
    ):
        os.environ.pop(_shared_db_var, None)

    workdir = Path(tempfile.mkdtemp(prefix="edenn_dev_"))
    media_dir = workdir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    make_placeholder_wav(media_dir / "placeholder.wav")  # default fallback tone
    media_url = "/dev/media/placeholder.wav"

    def placeholder_url_for(duration_s: float) -> str:
        """A placeholder track whose length matches the video (cached per second)."""
        secs = max(1, int(round(duration_s or 0)))
        name = f"placeholder_{secs}s.wav"
        path = media_dir / name
        if not path.exists():
            make_placeholder_wav(path, seconds=float(secs))
        return f"/dev/media/{name}"

    def publish_media(src: Path) -> str:
        """Copy a generated file (real music/video) into the served media dir and
        return its dev URL, so a candidate plays the ACTUAL render, not a tone."""
        dest = media_dir / f"{new_id('gen')}{src.suffix or '.bin'}"
        shutil.copyfile(str(src), str(dest))
        return f"/dev/media/{dest.name}"

    # Durable when the studio's OWN database is configured; in-memory otherwise.
    #
    # This is the line that decides whether a session survives a restart. The
    # deployment runs min-replicas 0, so "restart" is not an incident here — it
    # is every scale-from-zero. In-memory on that footprint means every user's
    # work quietly vanishes whenever the app goes idle, which is the difference
    # between a demo and a product. Handing the Postgres repositories in also
    # lights up the distributed turn lock and cross-replica fan-out, which read
    # the repository's client factory.
    from EdennCode.EdennAgent.AgenticAudio.config import (
        client_factory as _studio_db_factory,
        database_target as _studio_db_target,
    )

    from EdennCode.EdennAgent.AgenticAudio.persistence.media_store import (
        DurableMediaStore,
        set_active_store,
    )

    media_store = DurableMediaStore.configured()
    # One store per process: the API's delete path reaches for the same object
    # through active_store(), and a second instance would delete against a
    # different backend than the one that wrote the files.
    set_active_store(media_store)
    print(
        "[devserver] durable media: "
        + ("blob-backed (files survive a container recycle)"
           if media_store.active else
           "LOCAL ONLY — files die with this container")
    )
    _db_factory = _studio_db_factory()
    if _db_factory is not None:
        from EdennCode.EdennAgent.AgenticAudio.persistence.collab import (
            CollabRepository,
        )
        from EdennCode.EdennAgent.AgenticAudio.persistence.repositories import (
            AgenticAudioRepository,
        )

        agent_repo = AgenticAudioRepository(client_factory=_db_factory)
        agent_repo.ensure_schema()
        collab_repo = CollabRepository(client_factory=_db_factory)
        collab_repo.ensure_schema()
        print(f"[devserver] durable sessions: {_studio_db_target().describe()}")
    else:
        agent_repo = _MemoryAgenticRepository()
        collab_repo = InMemoryCollabRepository()
        print("[devserver] in-memory sessions (no AGENTIC_AUDIO_PG_HOST) — "
              "state does not survive a restart")
    def _restore_media_file(local_path: Any, url: Any) -> Optional[str]:
        """Put an artifact's bytes back where its row says they are.

        A row surviving a container recycle does not make the file survive one,
        and the path recorded in the row belongs to whichever container wrote
        it. So the NAME is what travels: the folder is chosen from the URL the
        row serves, and the durable media store supplies the bytes. Returns the
        usable path, or ``None`` — a path that is not there is worse than no
        path, because callers fall back to the URL on ``None`` and fail deep
        inside ffmpeg on a lie.
        """
        if not local_path:
            return None
        name = Path(str(local_path)).name
        kind = "uploads" if "/dev/uploads/" in str(url or "") else "media"
        folder = uploads_dir if kind == "uploads" else media_dir
        found = folder / name
        if not found.is_file():
            restored = media_store.restore(name, kind, folder)
            if restored is not None:
                found = restored
        return str(found) if found.is_file() else None

    def _restore_poster(metadata: Any) -> None:
        """The poster frame rides along, when the row names one."""
        thumb = str((metadata or {}).get("thumbnail_url") or "")
        if thumb.startswith("/dev/uploads/"):
            tname = Path(thumb.split("?")[0]).name
            if not (uploads_dir / tname).is_file():
                media_store.restore(tname, "uploads", uploads_dir)

    def _with_media_rehydration(base: type) -> type:
        """Give a job store a durable second chance at the media it points at.

        Two different things can be missing after a container recycle, and only
        one of them used to be handled. A missing ROW is what stranded every
        session on every recycle — "Source video artifact not found" — and the
        miss branch below restores it, and the file it names, from the durable
        store. A missing FILE is the failure that appears once rows themselves
        are durable: the lookup succeeds and hands back a path on a filesystem
        that no longer exists, which then fails later and somewhere else. Both
        are a cache miss here.

        Written as a mixin over whichever store is in use, because the in-memory
        one and the database-backed one need exactly the same second chance and
        a copy of this for each is a copy that drifts.
        """

        class _RehydratingAsyncRepo(base):  # type: ignore[misc, valid-type]
            def get_artifact(self, artifact_id: str) -> Any:
                found = super().get_artifact(artifact_id)
                if found is not None:
                    _restore_poster(getattr(found, "metadata_json", None))
                    local = _restore_media_file(
                        getattr(found, "local_path", None),
                        getattr(found, "url", None),
                    )
                    if local == getattr(found, "local_path", None):
                        return found
                    # Correct the answer for this caller without writing the
                    # row back: the stored path is still right for whichever
                    # container wrote it, and a write per read is a write per
                    # read.
                    return dataclasses.replace(found, local_path=local)
                row = media_store.restore_artifact(str(artifact_id))
                if not row or not isinstance(row.get("artifact"), dict):
                    return None
                art = dict(row["artifact"])
                job_row = row.get("job")
                if job_row and super().get_job(str(art.get("job_id"))) is None:
                    self.create_job(
                        job_id=job_row.get("job_id"),
                        job_type=job_row.get("job_type") or "asset_staging",
                        request_json=job_row.get("request_json") or {},
                        status=JobStatus.COMPLETED,
                        creator_user_id=job_row.get("creator_user_id"),
                    )
                # A registry row without its file is a promise we can't keep.
                _restore_poster(art.get("metadata_json"))
                art["local_path"] = _restore_media_file(
                    art.get("local_path"), art.get("url")
                )
                restored_artifact = self.add_artifact(**art)
                print(f"[devserver] durable media: rehydrated artifact {artifact_id}")
                return restored_artifact

        return _RehydratingAsyncRepo

    # One id for this process, stamped on every job it claims. Without it there
    # is no way to tell "a sibling replica is rendering this right now" from
    # "the container that was rendering this is gone", and those two need
    # opposite treatment: leave the first alone, tell the truth about the
    # second.
    runner_id = new_id("runner")

    if _db_factory is not None:
        from EdennCode.EdennAgent.AgenticAudio.persistence.jobs import (
            DurableJobRepository,
        )

        # The completer polls this store twice a second for the life of the
        # process. On the per-call factory that is a fresh connect + TLS
        # handshake + auth per read — thousands of throwaway connections an
        # hour against a managed database. The pooled factory reads the same
        # studio-namespace target and was shaped for exactly this.
        from EdennCode.EdennAgent.AgenticAudio.persistence.pool import (
            pooled_client as _pooled_client,
        )

        async_repo = _with_media_rehydration(DurableJobRepository)(
            client_factory=_pooled_client
        )
        async_repo.ensure_schema()
        print(f"[devserver] durable jobs: {_studio_db_target().describe()} — "
              "a render survives a restart")
    else:
        async_repo = _with_media_rehydration(_MemoryAsyncRepository)()
        print("[devserver] in-memory jobs (no AGENTIC_AUDIO_PG_HOST) — "
              "a render in flight dies with this process")
    # What each render consumes. Recorded beside the job in the studio's own
    # database, which is what lets the claim and the open be one transaction:
    # if the meter cannot be written the job is not claimed and nothing spends.
    # Inert on a laptop with no database, and it says so once rather than
    # warning on every render.
    from EdennCode.EdennAgent.AgenticAudio.persistence.usage import (
        Measurement,
        NO_SPEND,
        OUTCOME_DELIVERED,
        OUTCOME_FAILED_AFTER_SPEND,
        OUTCOME_NO_SPEND,
        OUTCOME_SPEND_UNKNOWN,
        SITE_MUSIC,
        SITE_MUSIC_RESTYLE,
        SITE_NARRATION,
        SITE_SFX_EVENTS,
        UsageMeter,
        job_key,
        set_active_meter,
    )

    meter = (
        UsageMeter(client_factory=_pooled_client)
        if _studio_db_target()
        else UsageMeter(enabled=False)
    )
    meter.announce()
    # The analysis path is a module-level function with no view of anything
    # built here, and it makes the largest single spend in the product. One
    # meter per process, reachable from both.
    set_active_meter(meter)

    #: Which invoice line a job type belongs to. Not the job type itself: an
    #: edit and a first take are the same job type through the same enqueue at
    #: the same cost, and one job type (sound effects) is two kinds of spend.
    _SITE_BY_JOB_TYPE = {
        "video_music": SITE_MUSIC,
        "audio_creative_edit": SITE_MUSIC_RESTYLE,
        "voiceover": SITE_NARRATION,
        "video_sfx": SITE_SFX_EVENTS,
    }

    def _meter_site(job: Any) -> Optional[str]:
        return _SITE_BY_JOB_TYPE.get(str(getattr(job, "job_type", "")))

    def _meter_key(job: Any) -> Optional[str]:
        site = _meter_site(job)
        return job_key(str(job.job_id), site=site) if site else None

    def _open_meter_for(job: Any, *, client: Any = None) -> None:
        """Open a usage row in the claim's own transaction.

        Raising here is deliberate and is the whole design: the claim rolls
        back, the job stays queued, and the next poll tries again. A render
        that waits is recoverable; a spend nobody recorded is not.
        """

        site = _meter_site(job)
        if site is None:
            return
        request = dict(getattr(job, "request_json", None) or {})
        requested = str(request.get("modelspec") or "").strip() or None
        meter.open(
            meter_key=job_key(str(job.job_id), site=site),
            site=site,
            job_id=str(job.job_id),
            session_id=getattr(job, "session_id", None),
            # The one click this belongs to: one approval enqueues a job per
            # take, so without this a customer sees several charges for one
            # press and nothing explains it.
            group_ref=str(
                request.get("agentic_proposal_id")
                or request.get("agentic_sfx_id")
                or request.get("agentic_parent_candidate_id")
                or ""
            ) or None,
            actor_user_id=getattr(job, "actor_user_id", None),
            creator_user_id=getattr(job, "creator_user_id", None),
            runner_id=runner_id,
            tier_requested=(
                requested
                if requested in DEFAULT_CANDIDATE_COUNT_BY_MODELSPEC
                else None
            ),
            deployment="standalone",
            client=client,
        )

    def _close_meter(
        job: Any, outcome: str, measurement: Optional[Any] = None, *, client: Any = None
    ) -> None:
        """Say what happened. Never allowed to fail the job it describes."""

        key = _meter_key(job)
        if key is None:
            return
        try:
            meter.close(
                meter_key=key, outcome=outcome, measurement=measurement, client=client
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[devserver] usage meter: could not close {job.job_id[:12]} "
                  f"({str(exc)[:120]}) — the spend is recorded as still open")

    queue = _MemoryQueue()
    source = _seed_source_video(async_repo)
    # The test-suite seed is a blob-only stub (https://cdn.test/...) with no local
    # file, which silently forces every no-upload session down the placeholder
    # path even in real-music mode. Synthesize a REAL 12s demo clip so the seeded
    # source behaves exactly like an upload (genuine analysis + real generation).
    demo_clip = media_dir / "demo_source.mp4"
    try:
        if not demo_clip.exists():
            subprocess.run(
                ["ffmpeg", "-y",
                 "-f", "lavfi", "-i", "testsrc2=size=480x854:rate=24:duration=12",
                 "-f", "lavfi", "-i", "sine=frequency=330:duration=12",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                 str(demo_clip)],
                check=True, capture_output=True, timeout=60,
            )
        demo_poster = media_dir / "demo_source_poster.jpg"
        meta = dict(source.metadata_json or {})
        meta["duration"] = 12.0
        if extract_poster(demo_clip, demo_poster):
            meta["thumbnail_url"] = "/dev/media/demo_source_poster.jpg"
        # The artifact dataclass is frozen — re-add under the same id (the
        # in-memory repo overwrites), now carrying the real local file.
        source = async_repo.add_artifact(
            artifact_id=source.artifact_id, job_id=source.job_id,
            artifact_type="source_video", role="input", container="user-uploads",
            blob_name=source.blob_name, url="/dev/media/demo_source.mp4",
            content_type="video/mp4", local_path=str(demo_clip), metadata_json=meta,
        )
        print("[devserver] seeded demo source now has a real local clip (real-music capable).")
    except Exception as exc:  # noqa: BLE001 - dev nicety; the placeholder path still works
        print(f"[devserver] demo clip synth failed ({str(exc)[:120]}) — no-upload flow stays placeholder.")

    # ---- REAL local remix/compose (replaces the test-suite fakes, whose
    # https://storage.test/... URLs made the mix preview unplayable in dev). All
    # dev media is local files behind /dev/media//dev/uploads, so we can run the
    # same ffmpeg helpers production uses and serve the actual render.
    def _persist_result_media(result: Any) -> None:
        """Push every /dev/media file a job result references to durable storage.

        The result dict is the one honest inventory of what a job produced —
        chasing every writer would miss one. Intermediates (per-line narration
        takes in volines_ dirs, temp remux workdirs) are deliberately not
        persisted: they are not referenced by any URL a session can serve.
        """
        seen: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, str) and value.startswith("/dev/media/"):
                name = Path(value.split("?")[0]).name
                if name in seen:
                    return
                seen.add(name)
                f = media_dir / name
                if f.is_file():
                    media_store.persist(f, "media")
                    side = f.with_suffix(f.suffix + ".alignment.json")
                    if side.is_file():
                        media_store.persist(side, "media")
            elif isinstance(value, dict):
                for v in value.values():
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)

        walk(result)

    def _local_media_file(url: Any) -> Path | None:
        """Resolve a dev-served URL (/dev/media/x, /dev/uploads/y) to its file.

        Local disk first; on a miss, the durable store — a container recycle
        wipes the disk, and the compose/remix pipelines resolve their inputs
        through here, so this line is what lets a resumed session keep working
        with the takes it already paid for."""
        if not url:
            return None
        name = Path(str(url).split("?")[0]).name
        for folder in (media_dir, workdir / "uploads"):
            p = folder / name
            if p.exists():
                return p
        for kind, folder in (("media", media_dir), ("uploads", workdir / "uploads")):
            restored = media_store.restore(name, kind, folder)
            if restored is not None:
                return restored
        return None

    def _source_video_path(artifact_id: Any) -> Path | None:
        art = async_repo.get_artifact(str(artifact_id)) if artifact_id else None
        local = getattr(art, "local_path", None)
        p = Path(local) if local else None
        return p if p and p.exists() else None

    async def _dev_remix(
        *, candidate: dict[str, Any], source_video_artifact_id: str,
        music_volume: float, preserve_original_audio: bool,
        music_envelope: Any = None,
    ) -> dict[str, Any]:
        """Music-only re-mux with real ffmpeg; falls back to the fake only when
        the local files can't be resolved (never leaves a dead URL silently)."""
        from EdennCode.Util.MediaUtils.ffmpeg_utils import overlay_music_on_video

        video = _source_video_path(source_video_artifact_id)
        # Ask the one resolver what this take actually plays. Reading audio_url
        # directly is what made a volume nudge silently throw away a window the
        # user had just re-cut: the cut and the seeked full track are different
        # music, and only this answer knows which one is on the card.
        music_url, music_start_s = candidate_music_source(candidate)
        music = _local_media_file(music_url)
        if not (video and music):
            print("[devserver] remix: local files unavailable — returning fake URL")
            return await _fake_remix(
                candidate=candidate, source_video_artifact_id=source_video_artifact_id,
                music_volume=music_volume, preserve_original_audio=preserve_original_audio,
            )
        out = media_dir / f"remix_{new_id('rm')}.mp4"
        await asyncio.to_thread(
            overlay_music_on_video, video, music, out,
            music_volume=float(music_volume),
            preserve_original_audio=bool(preserve_original_audio),
            music_start_s=float(music_start_s),
            music_envelope=[
                (float(a), float(b), float(g))
                for a, b, g in (music_envelope or [])
            ],
        )
        remix_result = {
            "status": "completed",
            "remixed_video_url": f"/dev/media/{out.name}",
            "music_volume": float(music_volume),
            "preserve_original_audio": bool(preserve_original_audio),
        }
        # Same hole as compose: a remixed take can be promoted to the final
        # deliverable, and this render never passes through a job, so nothing
        # else would ever push it to durable storage.
        _persist_result_media(remix_result)
        return remix_result

    async def _dev_window(
        *, candidate: dict[str, Any], source_video_artifact_id: str,
        window_start_s: float, segments: Any = None, music_envelope: Any = None,
    ) -> dict[str, Any]:
        """Re-cut a take from a different point in its own FULL track.

        Sources complete_audio_url, not the video-length cut: seeking into the
        cut would run the music out before the picture ends.
        """
        from EdennCode.Util.MediaUtils.ffmpeg_utils import (
            get_video_duration, overlay_music_on_video,
        )

        video = _source_video_path(source_video_artifact_id)
        full = _local_media_file(candidate.get("complete_audio_url"))
        if not (video and full):
            return {"status": "unavailable", "reason": "no_full_track",
                    "message": "The full track isn't available locally to re-cut."}
        start = max(0.0, float(window_start_s))
        video_dur = float(get_video_duration(video) or 0.0)
        full_dur = float(get_video_duration(full) or 0.0)
        if full_dur and not segments:
            # Clamp against the track even when the video length is unreadable:
            # seeking past the end produces a "completed" re-cut that is pure
            # silence, and a window shorter than the picture lets the mixer's
            # amix cut the narration off where the music stops.
            #
            # An arrangement is exempt because it does not seek: its pieces are
            # each bounded against the track already, and the assembled audio is
            # played from its own beginning.
            start = min(start, max(0.0, full_dur - (video_dur or 0.0)))
            if start >= full_dur:
                return {"status": "unavailable", "reason": "past_end_of_track",
                        "message": (
                            f"That point is past the end of this track "
                            f"({full_dur:.0f}s long) — pick an earlier moment."
                        )}
        out = media_dir / f"window_{new_id('wd')}.mp4"
        # Assemble first when the user arranged pieces, then mux the result the
        # same way a single window is muxed.
        source_audio = full
        if segments:
            from EdennCode.Util.MediaUtils.ffmpeg_utils import splice_audio_windows

            arranged = media_dir / f"arranged_{new_id('ar')}{full.suffix or '.m4a'}"
            await asyncio.to_thread(
                splice_audio_windows, full, arranged,
                windows=[(float(a), float(b)) for a, b in segments],
            )
            source_audio = arranged
            start = 0.0
        await asyncio.to_thread(
            overlay_music_on_video, video, source_audio, out,
            music_volume=candidate_music_volume(candidate),
            preserve_original_audio=bool(candidate.get("preserve_original_audio") or False),
            music_start_s=start,
            # Re-apply what the user already shaped: a re-cut renders a new file,
            # so an envelope that is not re-applied is an envelope discarded.
            music_envelope=list(music_envelope or []),
        )
        print(f"[devserver] re-cut take from {start:.1f}s of its full track ✓ -> /dev/media/{out.name}")
        # Measure the slice the viewer now hears, on the same render path that
        # produced it. This renderer and the library one BOTH have to do this:
        # the mix surface switches between them the moment narration or SFX
        # joins, and a re-cut measured on only one of them is a loop that
        # closes for some sessions and lies in the rest.
        signals = await asyncio.to_thread(
            measure_window_signals,
            full_track_path=source_audio,
            window_start_s=start,
            window_duration_s=video_dur,
        )
        window_result = {
            "status": "completed",
            # The arrangement is a piece of music in its own right; publish it
            # so every later render plays what the user arranged instead of the
            # untouched full track.
            **(
                {"arranged_audio_url": publish_media(source_audio)}
                if segments else {}
            ),
            "remixed_video_url": f"/dev/media/{out.name}",
            "window_start_s": round(start, 2),
            "full_duration_s": round(full_dur, 2) if full_dur else None,
        }
        if signals:
            window_result["take_signals"] = signals
        if segments:
            window_result["segments"] = [
                {"start_s": float(a), "duration_s": float(b)} for a, b in segments
            ]
        _persist_result_media(window_result)
        return window_result

    async def _dev_compose(**kwargs: Any) -> dict[str, Any]:
        """MASTER compose: every completed audio layer (music + narration + SFX)
        onto the source video with real ffmpeg, published to /dev/media so the
        mix preview actually plays."""
        from EdennCode.Util.MediaUtils.ffmpeg_utils import compose_master_mix_on_video

        params = {
            "music_volume": float(kwargs.get("music_volume", 0.85)),
            "voiceover_volume": float(kwargs.get("voiceover_volume", 1.0)),
            "voiceover_start_s": float(kwargs.get("voiceover_start_s", 0.0)),
            "duck_gain_db": float(kwargs.get("duck_gain_db", -9.0)),
            "sfx_volume": float(kwargs.get("sfx_volume", 1.0)),
            "preserve_original_audio": bool(kwargs.get("preserve_original_audio", False)),
        }
        video = _source_video_path(kwargs.get("source_video_artifact_id"))
        music = _local_media_file(kwargs.get("music_audio_url"))
        vo = _local_media_file(kwargs.get("voiceover_audio_url"))
        sfx = _local_media_file(kwargs.get("sfx_audio_url"))
        if not video or not (vo or sfx or music):
            # Honest state instead of a dead link: nothing local to compose in dev.
            return {
                "status": "planned",
                "message": "compose needs a local source video plus at least one audio layer in dev.",
                **params,
            }
        out = media_dir / f"mix_{new_id('mx')}.mp4"
        # Per-line ducking: the realized narration windows let the music dip under
        # each spoken line and recover between them. Without them ffmpeg falls back
        # to one flat duck across the whole narration span.
        vo_windows = kwargs.get("voiceover_segments") or None
        # The user's own level moves. Both renderers have to honour them or a
        # fade survives until the moment the session gains another layer.
        envelope = [
            (float(a), float(b), float(g))
            for a, b, g in (kwargs.get("music_envelope") or [])
        ]
        await asyncio.to_thread(
            compose_master_mix_on_video, video, out,
            music_path=music, voiceover_path=vo, sfx_path=sfx,
            music_start_s=float(kwargs.get("music_start_s") or 0.0),
            music_envelope=envelope,
            music_volume=params["music_volume"],
            voiceover_volume=params["voiceover_volume"],
            voiceover_start_s=params["voiceover_start_s"],
            duck_gain_db=params["duck_gain_db"],
            voiceover_segments=vo_windows,
            sfx_volume=params["sfx_volume"],
            preserve_original_audio=params["preserve_original_audio"],
        )
        layers = "+".join(n for n, p in (("music", music), ("vo", vo), ("sfx", sfx)) if p)
        ducking = f", {len(vo_windows)} duck windows" if vo_windows else ""
        print(f"[devserver] composed MASTER mix ({layers}{ducking}) ✓ -> /dev/media/{out.name}")
        # Listen back to the deliverable on the renderer that actually ships it.
        # The library twin does the same; a master checked on only one of them
        # is a check that holds for some sessions and not the rest.
        mix_signals = await asyncio.to_thread(music_take_signals, out)
        mix_result = {"status": "completed", "video_url": f"/dev/media/{out.name}", **params}
        if mix_signals:
            mix_result["mix_signals"] = mix_signals
        # The composed mix becomes final_artifact.video_url in durable session
        # state, but compose runs in-process — it never passes through a job, so
        # the job-completion persist never saw it and a container recycle stranded
        # the one file the session calls its deliverable.
        _persist_result_media(mix_result)
        return mix_result
    context = SimpleNamespace(
        settings=SimpleNamespace(
            workdir=workdir,
            async_v2_queue_namespace="edenn-dev",
            upload_container="user-uploads",
            output_container="generated-media",
            audio_container_name="generated-audio",
        ),
        storage=None,
    )
    tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=context.settings,
        analyze_fn=dev_analyze,   # real local scene/vision analysis (metadata-only fallback)
        remix_fn=_dev_remix,      # REAL ffmpeg re-mux on local dev files (fake only as fallback)
        compose_fn=_dev_compose,  # REAL ffmpeg multi-layer compose -> playable /dev/media URL
        window_fn=_dev_window,    # REAL ffmpeg re-cut from a new point in the full track
    )
    llm_client, llm_label, llm_live = build_llm_client()

    # A deployed box does not publish its own API surface to anonymous callers:
    # /docs, /redoc and /openapi.json answered without a credential and listed
    # every route. Local runs keep them — that is what they are for.
    _deployed = bool(
        os.getenv("CONTAINER_APP_NAME")
        or os.getenv("WEBSITE_HOSTNAME")
        or os.getenv("EDENN_PUBLIC_BASE_URL")
    )
    app = FastAPI(
        title="Edenn Agentic Audio — Dev Server",
        docs_url=None if _deployed else "/docs",
        redoc_url=None if _deployed else "/redoc",
        openapi_url=None if _deployed else "/openapi.json",
    )
    # Every request gets an id (honouring an inbound one) and a completion line
    # carrying status and duration, so a failed session can be found afterwards.
    install_request_context(app)
    agentic_router = create_agentic_audio_router(
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=tools,
            llm_client=llm_client,
            require_intent_gate=True,
            collab_repository=collab_repo,
    )
    app.include_router(agentic_router)
    # The drain loop asks this on shutdown; without it a deploy kills turns that
    # have already been paid for.
    app.state.agentic_planner = getattr(agentic_router, "agentic_audio_planner", None)

    # ---- Agentic creation (transformation flow) — ADDITIVE dev mount.
    # When EDENN_CREATION_MEDIA_DIR points at a folder of media (mp4 + cached
    # *.observation.json + wav tracks), a hermetic in-memory AUL store is seeded
    # at startup and the REAL creation service (resolve → plan → preview →
    # lock/render) is mounted, so the STABLE frontend can run the cut-shorts
    # flow live. Absent the env var this block is a no-op — the audio flow is
    # untouched either way.
    creation_media = os.environ.get("EDENN_CREATION_MEDIA_DIR", "").strip()
    if creation_media and Path(creation_media).is_dir():
        from EdennCode.EdennAgent.AssetLibrary import AssetIngestor, InMemoryAulRepository
        from EdennCode.EdennAgent.Creation import (
            CreationService,
            create_creation_router,
        )
        from EdennCode.EdennAgent.AssetLibraryApi import create_library_router

        aul_repo = InMemoryAulRepository()
        creation_ingestor = AssetIngestor(aul_repo)
        creation_service = CreationService(
            aul_repo, workdir=workdir / "creation",
            llm_client=llm_client if llm_live else None)
        app.include_router(create_creation_router(creation_service))
        app.include_router(create_library_router(aul_repo))

        @app.on_event("startup")
        async def _seed_creation_store() -> None:
            """Ingest the demo media (cached observations = no spend)."""

            media_root = Path(creation_media)
            for f in sorted(media_root.iterdir()):
                try:
                    if f.suffix == ".mp4" and "full" not in f.stem:
                        obs_file = f.with_suffix(".observation.json")
                        # Bounded on the way in, like the analysed path. A
                        # pre-generated observation reaches session state
                        # without ever passing through analyze_video, so
                        # bounding only there would leave this door open.
                        obs = (bound_observation(json.loads(obs_file.read_text()))
                               if obs_file.exists() else None)
                        await creation_ingestor.ingest(
                            f, name=f.stem.replace("_", " "),
                            observation=obs, with_signals=True)
                    elif f.suffix == ".wav":
                        await creation_ingestor.ingest(
                            f, kind="audio", name=f.stem.replace("_", " "),
                            generated=True)
                except Exception as exc:  # noqa: BLE001 - seed failures must not kill dev
                    print(f"[devserver] creation seed skipped {f.name}: {exc}")
            print(f"[devserver] creation store ready: "
                  f"{len(aul_repo.list_assets())} assets (transform flow live)")

    def _may_read_media(filename: str, caller: str | None) -> bool:
        """Whether this caller may read this file.

        A media file belongs to the sessions that reference it, so the question
        is answered by looking at the sessions this caller can already open —
        their own, and any they were invited to. Scanning from the CALLER's side
        rather than the file's means an unknown filename is simply not found,
        and the cost is bounded by how many sessions one person has rather than
        how many exist.

        With auth off every caller is an implicit owner, which is what the rest
        of this server already assumes on a laptop.
        """

        from EdennCode.EdennAgent.AgenticAudio.api.auth import auth_enabled

        if not auth_enabled():
            return True
        if not caller:
            return False

        session_ids: list[str] = [
            str(row.get("session_id"))
            for row in agent_repo.list_sessions(creator_user_id=caller, limit=200)
            if row.get("session_id")
        ]
        # Sessions shared WITH them count too — the same lookup the sessions
        # list uses. Without this an invited collaborator could open a session
        # and then not play anything in it.
        session_ids += [str(sid) for sid in collab_repo.sessions_for_user(caller)]

        for session_id in dict.fromkeys(session_ids):
            session = agent_repo.get_session(session_id)
            if session is None:
                continue
            if filename in json.dumps(session.state_json or {}, default=str):
                return True
        return False

    @app.get("/dev/media/{name}", include_in_schema=False)
    async def _media(
        name: str,
        authorization: str | None = Header(default=None),
        token: str | None = None,
    ) -> FileResponse:
        # Every generated take, mix and effects bed is served from here. It was
        # the one media route that never authenticated — during the live audit a
        # freshly generated take downloaded with no header and no token at all,
        # on a deployment that 401s anonymous callers everywhere else. Random
        # filenames are obscurity, not access control. The ?token= fallback is
        # the same concession /dev/uploads and the WebSocket already make, since
        # a <video> element cannot set a header.
        caller = await _require_caller(authorization, token)
        import mimetypes
        safe_name = Path(name).name  # no traversal
        # Authenticated is not authorised. Every take, mix and effects bed in
        # the deployment is served from this one route, and it asked only
        # whether the caller was SOMEBODY — so any valid token read any other
        # customer's footage and finished music, given a filename. Filenames
        # are obscurity, and they travel: in a shared link, a copied URL, a
        # support ticket.
        if not _may_read_media(safe_name, caller):
            # 404 rather than 403: a customer who cannot have this file should
            # not learn from us that it exists.
            raise HTTPException(status_code=404, detail="Not found.")
        target = media_dir / safe_name
        if not target.is_file():
            # The disk is per-container and containers get recycled; the
            # durable store is where a resumed session's takes actually live.
            restored = await asyncio.to_thread(
                media_store.restore, safe_name, "media", media_dir
            )
            if restored is None:
                raise HTTPException(status_code=404, detail="Not found.")
            target = restored
        media_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        return FileResponse(target, media_type=media_type)

    # Real video upload — same shape as the production POST /api/v2/assets/video,
    # so the frontend code is identical. Stores the file, registers a source
    # artifact in the in-memory repo, and returns its id for createSession.
    uploads_dir = workdir / "uploads"
    uploads_dir.mkdir(exist_ok=True)
    # Published on the app so operators (and tests) can find where this replica
    # is actually writing, without reaching into a closure.
    app.state.workdir = workdir
    app.state.uploads_dir = uploads_dir
    app.state.media_dir = media_dir
    # Published for the same reason the directories above are: an operator
    # answering "whose session is this?" on a live box, and the tests that
    # assert one customer cannot read another's media.
    app.state.agent_repository = agent_repo
    app.state.collab_repository = collab_repo
    app.state.async_repository = async_repo

    async def _require_caller(authorization: str | None, token: str | None = None) -> str | None:
        """Authenticate a non-router route the same way the router does.

        The devserver mounts these media routes itself, outside the agentic
        router, so they inherited none of its auth. That is how the deployed
        console ended up with an open upload endpoint and open media reads on a
        host that otherwise required a token.
        """
        from EdennCode.EdennAgent.AgenticAudio.api.auth import AuthError, resolve_caller

        try:
            caller = await resolve_caller(authorization=authorization, token=token)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return caller.principal if caller else None

    @app.post("/api/v2/assets/video", include_in_schema=False)
    async def stage_video_asset(
        video: UploadFile | None = File(default=None),
        video_url: str | None = Form(default=None),
        creator_user_id: str | None = Form(default=None),
        session_id: str | None = Form(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = await _require_caller(authorization)
        # The authenticated caller owns the upload; a form field never decides
        # whose asset this is.
        if principal:
            creator_user_id = principal
        asset_job = new_id("asset_job")
        name = (video.filename if video else "video.mp4") or "video.mp4"
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        # Clamp the client-supplied basename: a name near the OS NAME_MAX (255)
        # raised an unhandled ENAMETOOLONG on write → 500 instead of a clean
        # reject (adversarial review P2). Leave headroom for the job-id prefix.
        if len(safe) > 120:
            stem, dot, ext = safe.rpartition(".")
            ext = ext[:12] if dot else ""
            safe = ((stem or safe)[:100] + (("." + ext) if ext else "")) or "video.mp4"
        dest = uploads_dir / f"{asset_job}_{safe}"
        # Streamed to disk, never buffered whole: the previous read() put the
        # entire upload in RAM, so a large file (or a few concurrent ones) took
        # the process down rather than being rejected.
        if video is None:
            raise HTTPException(status_code=400, detail="Upload a video file — the request had no content.")
        suffix = Path(safe).suffix.lower()
        if suffix and suffix not in ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(
                status_code=415,
                detail=(
                    f"{suffix} isn't a video container we can score. "
                    f"Use one of: {', '.join(sorted(ALLOWED_UPLOAD_SUFFIXES))}."
                ),
            )
        written = 0
        try:
            with dest.open("wb") as fh:
                while True:
                    chunk = await video.read(UPLOAD_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                "That video is larger than "
                                f"{MAX_UPLOAD_BYTES // (1024 * 1024)}MB. "
                                "Trim it, or export it at a lower bitrate."
                            ),
                        )
                    fh.write(chunk)
        except HTTPException:
            dest.unlink(missing_ok=True)
            raise
        if not written:
            dest.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Upload a video file — the request had no content.")
        # Validate for real: a file ffprobe can't time is not a usable video —
        # reject it honestly instead of fabricating a duration and letting the
        # session bootstrap on garbage.
        duration = probe_duration(dest)
        if not duration or duration <= 0:
            dest.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail="That file doesn't decode as a video — please upload a playable video file.",
            )
        if duration > MAX_UPLOAD_SECONDS:
            dest.unlink(missing_ok=True)
            raise HTTPException(
                status_code=413,
                detail=(
                    f"That video is {int(duration)}s long; the limit is "
                    f"{int(MAX_UPLOAD_SECONDS)}s. Scoring works on a cut, not a full timeline."
                ),
            )
        content_type = (video.content_type if video else None) or "video/mp4"
        # Measured, or absent. Never invented.
        _probed = await asyncio.to_thread(probe_dimensions, dest)
        _dims = {"width": _probed[0], "height": _probed[1]} if _probed else {}
        url = f"/dev/uploads/{dest.name}"
        # A real poster frame so the source + candidate cards show a thumbnail.
        poster = uploads_dir / f"{dest.stem}_poster.jpg"
        thumbnail_url = f"/dev/uploads/{poster.name}" if extract_poster(dest, poster) else None
        async_repo.create_job(
            job_id=asset_job, job_type="asset_staging",
            request_json={"upload_filename": name}, status=JobStatus.COMPLETED,
            # Who uploaded it. Without this the artifact has no owner, and
            # "whose video is this" has no answer to check a session against.
            creator_user_id=creator_user_id,
        )
        # The upload and its poster go to durable storage NOW — a container
        # recycle between this response and the next turn used to strand the
        # session before it ever started.
        media_store.persist(dest, "uploads")
        if thumbnail_url:
            media_store.persist(poster, "uploads")
        artifact_id = f"{asset_job}:source_video:input"
        _artifact = async_repo.add_artifact(
            artifact_id=artifact_id, job_id=asset_job, artifact_type="source_video",
            role="input", container="user-uploads", blob_name=f"source/{safe}",
            url=url, content_type=content_type,
            # The real local file — lets the analysis pipeline resolve it directly
            # (no blob download) so dev runs genuine scene/vision understanding.
            local_path=str(dest),
            metadata_json={"duration": duration, **_dims,
                           "source_kind": "upload", "filename": name,
                           "thumbnail_url": thumbnail_url},
        )
        media_store.persist_artifact(
            _artifact, job=async_repo.get_job(asset_job)
        )
        return {
            "artifact_id": artifact_id, "job_id": asset_job, "artifact_type": "source_video",
            "status": "completed", "url": url, "content_type": content_type,
            "metadata": {"duration": duration, "filename": name, "thumbnail_url": thumbnail_url},
            "status_url": f"/api/v2/agentic/audio/sessions",
        }

    @app.get("/dev/uploads/{name}", include_in_schema=False)
    async def _serve_upload(
        name: str,
        authorization: str | None = Header(default=None),
        token: str | None = None,
    ) -> FileResponse:
        # A media URL is handed to <video>/<audio> elements, which cannot set a
        # header — hence the ?token= fallback, the same concession the WebSocket
        # makes. Unauthenticated, these were the user's own uploaded footage
        # served to anyone who guessed a filename.
        await _require_caller(authorization, token)
        target = (uploads_dir / name).resolve()
        if uploads_dir.resolve() not in target.parents:
            raise HTTPException(status_code=404, detail="Not found.")
        if not target.is_file():
            restored = await asyncio.to_thread(
                media_store.restore, Path(name).name, "uploads", uploads_dir
            )
            if restored is None:
                raise HTTPException(status_code=404, detail="Not found.")
            target = restored
        return FileResponse(target)

    # ---- liveness and readiness ------------------------------------------
    # Two questions, two answers. Liveness is "is this process still a process"
    # — a wedged container that answers it should be restarted, not left in
    # rotation. Readiness is "should traffic come here yet": it must be false
    # while the app is still starting, and false again once it is shutting down,
    # so a deploy drains instead of dropping requests mid-turn.
    @app.get("/healthz", include_in_schema=False)
    async def _healthz() -> dict[str, Any]:
        return {"status": "ok"}

    _READY_PROBE_TTL_S = 10.0
    _ready_probe: dict[str, Any] = {"at": 0.0, "ok": True, "reason": ""}

    def _probe_dependencies() -> tuple[bool, str]:
        """One cheap question to the things a turn cannot run without.

        Readiness used to read in-process flags only, so a replica whose
        database had gone away reported itself ready and kept being sent
        traffic it could not serve. Cached for a few seconds because a probe
        that runs on every health check becomes its own load, and health checks
        are the most frequent request a container gets.

        Never raises: a probe that can fail the endpoint it reports on turns a
        dependency blip into a replica restart.
        """

        now = time.time()
        if now - float(_ready_probe["at"]) < _READY_PROBE_TTL_S:
            return bool(_ready_probe["ok"]), str(_ready_probe["reason"])
        ok, reason = True, ""
        try:
            checker = getattr(agent_repo, "ping", None)
            if callable(checker):
                checker()
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"store: {str(exc)[:80]}"
        _ready_probe.update({"at": now, "ok": ok, "reason": reason})
        return ok, reason

    @app.get("/readyz", include_in_schema=False)
    async def _readyz(response: Response) -> dict[str, Any]:
        ready = bool(getattr(app.state, "ready", False))
        draining = bool(getattr(app.state, "draining", False))
        completer = getattr(app.state, "_completer", None)
        # A dead completer is a replica that accepts renders and never runs
        # them. It reported ready for the rest of its life.
        completer_alive = completer is None or not completer.done()
        depends_ok, depends_reason = (True, "")
        if ready and not draining:
            depends_ok, depends_reason = await asyncio.to_thread(_probe_dependencies)
        healthy = ready and not draining and completer_alive and depends_ok
        if not healthy:
            response.status_code = 503
        reason = ""
        if draining:
            reason = "draining"
        elif not ready:
            reason = "starting"
        elif not completer_alive:
            reason = "completer_died"
        elif not depends_ok:
            reason = depends_reason
        return {
            "ready": healthy,
            "draining": draining,
            # Named so a probe failure says WHY without another round trip.
            "reason": reason,
            "completer": "alive" if completer_alive else "dead",
        }

    @app.get("/dev/info", include_in_schema=False)
    async def _info() -> dict[str, Any]:
        return {
            "llm": getattr(app.state, "llm_status", llm_label),
            "llm_live": llm_live,
            "source_artifact": source.artifact_id,
            "intent_gate": True,
        }

    # ---- background job-completer: hydrate generated candidates ----
    inflight: set[str] = set()

    def _session_observation(job: Any) -> dict[str, Any]:
        sid = (job.request_json or {}).get("agentic_session_id")
        if not sid:
            return {}
        sess = agent_repo.get_session(sid)
        return (sess.state_json.get("observation") if sess else None) or {}

    def _session_duration(job: Any) -> float:
        return float(_session_observation(job).get("duration_s") or 0.0)

    def _session_cuts(job: Any) -> list[float]:
        """Real cuts, and only the trustworthy ones — a detector that fell back
        to frame differencing invents cuts on fast motion, and an invented cut
        would become a hard placement constraint."""
        obs = _session_observation(job)
        if obs.get("cut_source") != "pyscenedetect":
            return []
        return [float(c) for c in (obs.get("cuts") or [])]

    def _placeholder_result(job: Any) -> dict[str, Any]:
        # A clearly-marked placeholder tone (length-matched) — the UI badges it.
        dur = _session_duration(job)
        track = placeholder_url_for(dur) if dur else media_url
        return {"audio_url": track, "complete_audio_url": track, "video_url": None, "placeholder": True}

    async def _real_music_result(
        job: Any,
        modelspec_override: str | None = None,
        measured: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any] | None:
        """Run the REAL music pipeline on the local source video; return
        a candidate result with served URLs, or None to fall back to a placeholder."""
        req = job.request_json or {}
        modelspec = normalize_music_modelspec(modelspec_override or req.get("modelspec"))
        # The agentic tool enqueues a LINKED artifact id (job-scoped) whose dev
        # record has no local_path — the original artifact carries it. Try both.
        src = None
        for art_id in (req.get("source_video_artifact_id"), req.get("requested_source_video_artifact_id")):
            if not art_id:
                continue
            artifact = async_repo.get_artifact(art_id)
            local = getattr(artifact, "local_path", None)
            if local and Path(local).exists():
                src = Path(local)
                break
        if src is None:
            print(f"[devserver] real music: no local source video resolvable for job {job.job_id[:12]} — placeholder.")
            return None
        try:
            from EdennCode.Deployment.workflows import VideoGenerationOrchestrator
            # Re-route the fused prompt against the tier that will ACTUALLY
            # render: when a provider key is missing the substitute tier may not
            # accept an explicit style direction at all, and carrying the
            # enqueued flag across that substitution fails the job outright
            # instead of degrading it to the ordinary prompt.
            routed = AgenticAudioTools.route_style_prompt(
                req.get("music_style_prompt"),
                modelspec=modelspec,
                user_prompt=str(req.get("user_prompt") or ""),
            )
            # Say which prompt the model is being given. Whether a generation is
            # grounded in the footage is the difference between music that tracks
            # the video and music that merely matches its adjectives — and it was
            # silently the second one for a long time, on every deployment.
            grounding = (
                "video-grounded via explicit direction"
                if routed["verbose_instruction"]
                else (
                    "video-grounded via prompt"
                    if routed["music_style_prompt"]
                    else "ungrounded (no fused prompt)"
                )
            )
            print(
                f"[devserver] generating REAL music ({modelspec}, {grounding}) "
                f"for job {job.job_id[:12]} — this takes minutes…"
            )
            res = await VideoGenerationOrchestrator(storage=None).run(
                video_path=src,
                user_prompt=routed["user_prompt"],
                verbose_instruction=routed["verbose_instruction"],
                music_style_prompt=routed["music_style_prompt"],
                modelspec=modelspec,
                preserve_original_audio=bool(req.get("preserve_original_audio", False)),
                music_volume=float(req.get("music_volume", 0.85)),
                include_vocals=bool(req.get("include_vocals", False)),
                vocal_gender=req.get("vocal_gender", "female"),
            )
            # audio_url = the VIDEO-LENGTH CUT (what "play against your video"
            # means); the complete track stays available as complete_audio_url.
            # Previously the full 3+ minute track was exposed as the take itself.
            cut = getattr(res, "generated_music_path", None)
            full = getattr(res, "complete_generated_music_path", None) or cut
            audio_url = publish_media(Path(cut)) if cut else None
            complete_url = publish_media(Path(full)) if full and full != cut else audio_url
            video_url = publish_media(Path(res.remixed_video_path)) if getattr(res, "remixed_video_path", None) else None
            if not (audio_url or complete_url or video_url):
                raise _RealGenerationFailed(
                    "music generation returned no audio or video output")
            print(f"[devserver] real music ✓ job {job.job_id[:12]} (cut={bool(audio_url)} full={bool(complete_url)} video={bool(video_url)})")
            # Measure the take HERE, where the files are local. The listen-back
            # report is judged later on the read path, which runs on a 2.5s poll
            # inside a locked read-modify-write and must not touch a file.
            from EdennCode.EdennAgent.AgenticAudio.tools.media import music_take_signals

            take_signals = music_take_signals(
                Path(cut) if cut else None, Path(full) if full else None,
            )
            # Where the beats and the sections are, measured on the FULL track
            # because that is what a re-cut moves around inside. Unlike the
            # listen-back numbers this describes the TRACK, not the window, so
            # it survives every re-cut of it.
            structure = music_structure(Path(full) if full else None)
            if measured is not None:
                # What this take cost, in the dimensions that were free to
                # measure. Recorded per dimension rather than reduced to one
                # number: the tiers are charged differently upstream — one by
                # the length ASKED FOR, the others per generation — and a
                # dimension never written cannot be re-measured once the media
                # is deleted on the retention window.
                source_s = float((req.get("duration_s") or 0.0) or 0.0)
                rendered_tier = rendered_modelspec(res, modelspec)
                usage = getattr(res, "token_usage", None) or {}
                measured["measurement"] = Measurement(
                    delivered_ms=(
                        round(float(take_signals["cut_duration_s"]) * 1000)
                        if take_signals.get("cut_duration_s") else None
                    ),
                    produced_ms=(
                        round(float(take_signals["full_duration_s"]) * 1000)
                        if take_signals.get("full_duration_s") else None
                    ),
                    # The entry tier is billed on the length asked for, floored
                    # and capped by the request path, so a four-second clip buys
                    # ten seconds and the gap should be visible rather than
                    # mysterious.
                    requested_ms=(
                        max(10_000, min(300_000, round(source_s * 1000)))
                        if rendered_tier == "edenn_basic" and source_s > 0 else None
                    ),
                    # One per track generation, plus each extension. It does not
                    # count the lyric round-trips the vocal path also pays for.
                    provider_calls=int(
                        getattr(res, "generation_api_call_count", 1) or 1
                    ),
                    lm_input_tokens=usage.get("prompt_tokens"),
                    lm_output_tokens=usage.get("completion_tokens"),
                    tier=rendered_tier,
                    tier_requested=normalize_music_modelspec(req.get("modelspec")),
                    primary_unit="delivered_ms",
                    note_code=(
                        "call_count_excludes_lyric_calls"
                        if req.get("include_vocals") else None
                    ),
                    detail={"edit_kind": str(req.get("agentic_edit_kind") or "")}
                    if req.get("agentic_edit_kind") else {},
                )
            return {
                "audio_url": audio_url or complete_url or video_url,
                "complete_audio_url": complete_url or audio_url or video_url,
                "video_url": video_url,
                "placeholder": False,
                # What rendered, not what was asked for (see rendered_modelspec).
                "modelspec": rendered_modelspec(res, modelspec),
                "take_signals": take_signals,
                **({"music_structure": structure} if structure else {}),
                # WHERE in the full track this cut came from. The matcher picks
                # it and the orchestrator has always carried it; it was dropped
                # here, so no take knew its own window and there was nothing to
                # move relative to.
                "music_start_s": float(getattr(res, "music_start_s", 0.0) or 0.0),
                # The take's own handle at the provider that made it. The
                # orchestrator has carried these the whole time and this result
                # dropped them, which is why "make it longer" could never be an
                # extension OF THIS TRACK on the deployment that ships: the
                # agent's native-extend path requires a parent handle, found
                # none, and silently generated something new instead.
                **(
                    {"provider_audio_id": str(getattr(res, "provider_audio_id", "") or "")}
                    if getattr(res, "provider_audio_id", None) else {}
                ),
                **(
                    {"provider_task_id": str(getattr(res, "provider_task_id", "") or "")}
                    if getattr(res, "provider_task_id", None) else {}
                ),
            }
        except _RealGenerationFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[devserver] real music gen FAILED ({str(exc)[:200]}) — job marked failed.")
            raise _RealGenerationFailed(str(exc)) from exc

    def _narration_synthesizer() -> tuple[Any, str]:
        """The narration engine, by configuration.

        The hosted speech provider is the primary engine (owner decision,
        2026-08-26); the platform TTS deployment stays wired as the fallback and
        can be forced with AGENTIC_AUDIO_TTS_ENGINE=platform. Selection happens
        per render, not at startup, so a credential added to a running replica
        takes effect on the next job.
        """
        engine = os.getenv("AGENTIC_AUDIO_TTS_ENGINE", "").strip().lower()
        if engine != "platform" and os.getenv("PROVIDER_A_API_KEY", "").strip():
            from EdennCode.ModelFactory.VoiceOverModelFactory.hosted_speech_synthesizer import (
                HostedSpeechSynthesizer,
            )

            return HostedSpeechSynthesizer(), "hosted speech provider"
        from EdennCode.Deployment.async_pipeline_v2.workers.voiceover_worker import (
            DefaultVoiceoverSynthesizer,
        )

        return DefaultVoiceoverSynthesizer(), "platform TTS"

    async def _real_voiceover_result(
        job: Any, measured: Optional[dict[str, Any]] = None
    ) -> dict[str, Any] | None:
        """Synthesize the approved script with real Azure TTS, publish the audio.

        Segmented narration (video-informed): each segment renders with its OWN
        delivery direction, then the takes are assembled at their start offsets
        onto the video timeline — so WHEN and HOW each line lands matches the
        plan the director made from the footage.
        """
        req = job.request_json or {}
        script = str(req.get("script") or "").strip()
        segments = [s for s in (req.get("segments") or []) if isinstance(s, dict) and str(s.get("text") or "").strip()]
        if not script and not segments:
            return None
        try:
            synth, engine_label = _narration_synthesizer()
            base_instructions = str(req.get("tts_instructions") or "")
            # Count at the synthesis boundary, which is the ONLY place that is
            # right in both directions: the renderer copies a kept line off
            # disk for nothing, and re-reads a line that will not fit up to
            # twice more. Counting the lines that came back would miss both.
            synth_calls = 0
            synth_chars = 0
            _synthesize = synth.synthesize

            async def _counted_synthesize(*args: Any, **kwargs: Any) -> Any:
                nonlocal synth_calls, synth_chars
                synth_calls += 1
                synth_chars += len(str(kwargs.get("script") or ""))
                return await _synthesize(*args, **kwargs)

            out = media_dir / f"vo_{new_id('vo')}.wav"
            result_segments: list[dict[str, Any]] | None = None
            if segments:
                print(f"[devserver] synthesizing segmented voice-over ({len(segments)} lines, {engine_label}) for job {job.job_id[:12]}…")
                # Shared with the production worker — see narration_render. The
                # two copies drifting apart is what made every timing decision
                # vanish the moment a session ran anywhere but here.
                from EdennCode.EdennAgent.AgenticAudio.tools.narration_render import (
                    render_segmented_narration,
                )
                result_segments = await render_segmented_narration(
                    segments=segments,
                    synthesize=_counted_synthesize,
                    speed_capable=bool(getattr(synth, "supports_speed", True)),
                    voice=str(req.get("tts_voice") or "shimmer"),
                    base_instructions=base_instructions,
                    default_speed=float(req.get("speed") or 1.0),
                    workdir=media_dir / f"volines_{new_id('vl')}",
                    out_path=out,
                    video_duration_s=_session_duration(job),
                    cuts=_session_cuts(job),
                    avoid_windows=_session_observation(job).get("speech_windows") or [],
                    log=lambda m: print(f"[devserver] {m}"),
                    # Lines the caller says are unchanged and already recorded.
                    # Re-reading them off disk is what turns "re-read line 3"
                    # from a whole re-record into one line of synthesis.
                    reuse={
                        str(seg_id): Path(path)
                        for seg_id, path in (req.get("reuse_segment_audio") or {}).items()
                        if path and Path(path).exists()
                    },
                )
                if result_segments is None:
                    raise _RealGenerationFailed("no narration line synthesized")
            else:
                print(f"[devserver] synthesizing voice-over ({engine_label}) for job {job.job_id[:12]}…")
                await _counted_synthesize(
                    script=script,
                    voice=str(req.get("tts_voice") or "shimmer"),
                    instructions=base_instructions,
                    speed=float(req.get("speed") or 1.0),
                    out_path=out,
                )
            if not (out.exists() and out.stat().st_size > 1000):
                raise _RealGenerationFailed(
                    "voice-over synthesis produced no usable audio")
            url = f"/dev/media/{out.name}"
            print(f"[devserver] voice-over ✓ job {job.job_id[:12]} ({out.stat().st_size} bytes)")
            if measured is not None:
                from EdennCode.EdennAgent.AgenticAudio.tools.media import (
                    _peak_and_mean_db,  # noqa: F401 - probe lives beside the rest
                )
                from EdennCode.Util.MediaUtils import ffmpeg_utils

                try:
                    spoken_s = float(ffmpeg_utils.get_video_duration(out) or 0.0)
                except Exception:  # noqa: BLE001
                    spoken_s = 0.0
                kept = [
                    seg for seg in (result_segments or [])
                    if isinstance(seg, dict) and seg.get("reused")
                ]
                measured["measurement"] = Measurement(
                    delivered_ms=round(spoken_s * 1000) if spoken_s > 0 else None,
                    provider_calls=synth_calls,
                    items=len(result_segments or []) or 1,
                    items_reused=len(kept),
                    # Codepoints, not words: a word count is meaningless in
                    # Chinese, Japanese, Korean and Thai, where a whole sentence
                    # can be one "word".
                    text_chars=synth_chars,
                    primary_unit="delivered_ms",
                )
            return {
                "status": "completed", "audio_url": url, "complete_audio_url": url,
                "voice_id": req.get("voice_id"), "language": req.get("language"),
                "agentic_voiceover_id": req.get("agentic_voiceover_id"), "placeholder": False,
                **({"segments": result_segments} if result_segments else {}),
            }
        except _RealGenerationFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[devserver] voice-over TTS FAILED ({str(exc)[:200]}) — job marked failed.")
            raise _RealGenerationFailed(str(exc)) from exc

    def _local_for_dev_url(url: str | None) -> Path | None:
        """Map a served /dev/media/<name> URL back to its local file (no download)."""
        if not url:
            return None
        path = media_dir / str(url).rsplit("/", 1)[-1]
        return path if path.exists() else None

    async def _real_sfx_result(
        job: Any, measured: Optional[dict[str, Any]] = None
    ) -> dict[str, Any] | None:
        """Run the REAL video→SFX pipeline (hosted sound-effects provider) on the local
        source clip, publish the SFX-mixed video + audio. Each job = one variant;
        the caller-planned events steer the render via the user_prompt."""
        req = job.request_json or {}
        src = None
        src_public_url = ""
        for art_id in (req.get("source_video_artifact_id"), req.get("requested_source_video_artifact_id")):
            if not art_id:
                continue
            artifact = async_repo.get_artifact(art_id)
            local = getattr(artifact, "local_path", None)
            if local and Path(local).exists():
                src = Path(local)
                # The video-conditioned engine fetches the clip BY URL from
                # the vendor's side. First choice: a signed, expiring, read-only
                # URL on the blob copy — works from anywhere, and our API
                # tokens never ride in a URL handed to a third party. Fallback:
                # EDENN_PUBLIC_BASE_URL for open deployments. Neither present →
                # the text route carries the whole plan (quality decision, not
                # an outage).
                signed = media_store.vendor_read_url(src, "uploads")
                if signed:
                    src_public_url = signed
                else:
                    public_base = os.getenv("EDENN_PUBLIC_BASE_URL", "").strip().rstrip("/")
                    rel = str(getattr(artifact, "url", "") or "")
                    if public_base and rel.startswith("/"):
                        src_public_url = public_base + rel
                break
        if src is None:
            print(f"[devserver] real SFX: no local source video for job {job.job_id[:12]} — placeholder.")
            return None
        try:
            from EdennCode.EdennAgent.AgenticAudio.tools.sfx_render import (
                SfxRenderFailed,
                published_manifest,
                render_sfx,
            )

            def _resolve_reuse_audio(refs: Any) -> dict[str, str]:
                """Turn kept-effect references back into local files.

                New results record a kept effect as a served ``/dev/media/...``
                URL — the form that survives the container that rendered it
                (the durable media store holds the bytes). Older session state
                recorded raw local paths; those pass through and the shared
                render's existence check does the honest accounting for any
                that are gone.
                """
                resolved: dict[str, str] = {}
                for event_id, ref in (refs or {}).items():
                    text = str(ref or "")
                    if text.startswith("/dev/media/"):
                        name = Path(text.split("?")[0]).name
                        local = media_dir / name
                        if not local.is_file():
                            media_store.restore(name, "media", media_dir)
                        resolved[str(event_id)] = str(local)
                    elif text:
                        resolved[str(event_id)] = text
                return resolved

            run_dir = media_dir / f"sfx_run_{new_id('sfx')}"
            print(
                f"[devserver] generating REAL SFX for job {job.job_id[:12]} "
                f"({len(req.get('events') or [])} planned) — this takes a bit…"
            )
            if src_public_url:
                print("[devserver] SFX video-native routes armed "
                      f"({'signed blob read' if '?' in src_public_url else 'public base'})")

            out = await render_sfx(
                video_path=src,
                source_public_url=src_public_url,
                events=req.get("events") or [],
                summary=str(req.get("sfx_summary") or ""),
                ambience=str(req.get("sfx_ambience") or ""),
                # Jobs enqueued before plan authority existed carry no mode and
                # keep the engine path; the shared resolver owns that rule.
                spotting=req.get("sfx_spotting"),
                # Watch the footage, or write it from the plan's prompts. The
                # session asked for one; the render honours it or says why not.
                route=str(req.get("agentic_sfx_route") or "auto"),
                # Effects the user kept. This was carried from the tool into the
                # job payload and then never read, so fixing one hit in a bed of
                # twelve re-synthesised — and re-charged for — all twelve.
                reuse_event_audio=_resolve_reuse_audio(req.get("reuse_event_audio")),
                run_dir=run_dir,
                log=lambda message: print(f"[devserver] {message}"),
            )

            # The result carries per-event URLs, never server paths: a path is
            # useless to any other container and serving it to the browser is
            # the same server-path leak this repo has had once already. A kept
            # effect keeps the URL it was first published under — that URL
            # equality is how anything outside can verify "not generated
            # again"; a file already in the media dir keeps its name for the
            # same reason, and only freshly rendered files are copied in.
            reused_urls = {
                str(event_id): str(ref)
                for event_id, ref in (req.get("reuse_event_audio") or {}).items()
                if str(event_id) in out.reused_event_ids
                and str(ref or "").startswith("/dev/media/")
            }

            def _publish_event_audio(_event_id: str, path: Path) -> Optional[str]:
                if path.parent == media_dir:
                    return f"/dev/media/{path.name}"
                return publish_media(path)

            if measured is not None:
                # Count what was ATTEMPTED, not what came back marked rendered:
                # a per-event failure is swallowed inside the stage, so a
                # provider outage returns a full manifest of unrendered events
                # that were all attempted and all paid for.
                reused = set(out.reused_event_ids or ())
                attempted = [
                    event for event in (out.rendered_events or [])
                    if str(event.get("id") or "") not in reused
                ]
                measured["measurement"] = Measurement(
                    items=len(attempted),
                    # The reason the redo path exists: fixing one hit in a bed
                    # of twelve must not re-charge for the other eleven, and a
                    # meter that ignores reuse re-charges for eleven.
                    items_reused=len(reused),
                    provider_calls=len(attempted),
                    primary_unit="items",
                    detail={"spotting": str(out.spotting or "")},
                )
            rendered_events = published_manifest(
                out.rendered_events,
                publish=_publish_event_audio,
                known_urls=reused_urls,
            )

            video_url = publish_media(out.final_video_path) if out.final_video_path else None
            audio_url = publish_media(out.mixed_audio_path) if out.mixed_audio_path else None
            if not (video_url or audio_url):
                raise _RealGenerationFailed(
                    "sound-effect generation returned no audio or video output")
            print(f"[devserver] real SFX ✓ job {job.job_id[:12]} "
                  f"({len(out.rendered_events)} events, {len(out.reused_event_ids)} reused, "
                  f"video={bool(video_url)})")
            return {
                "status": "completed",
                "audio_url": audio_url or video_url,
                "complete_audio_url": audio_url or video_url,
                "video_url": video_url,
                "agentic_sfx_id": req.get("agentic_sfx_id"),
                # What actually rendered, so the plan card can be checked against
                # the take instead of taken on trust.
                "spotting": out.spotting,
                "rendered_events": rendered_events,
                "reused_event_ids": list(out.reused_event_ids),
                # Measured at render, where the files are local. The read path
                # judges from these numbers and probes nothing.
                "take_signals": dict(out.take_signals or {}),
                # Did the engine watch the footage, or read a description of
                # it? The session is entitled to know which it paid for.
                "watched_the_video": bool(out.watched_the_video),
                "not_watched_reason": str(out.not_watched_reason or ""),
                "placeholder": False,
            }
        except SfxRenderFailed as exc:
            print(f"[devserver] real SFX produced nothing usable ({exc}) — job marked failed.")
            raise _RealGenerationFailed(str(exc)) from exc
        except _RealGenerationFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[devserver] real SFX gen FAILED ({str(exc)[:200]}) — job marked failed.")
            raise _RealGenerationFailed(str(exc)) from exc

    async def _real_creative_edit_result(
        job: Any, measured: Optional[dict[str, Any]] = None
    ) -> dict[str, Any] | None:
        """Restyle the parent's REAL track (provider audio-to-audio), then re-mux onto
        the video. Only attempted when the parent is a real (non-placeholder) track."""
        req = job.request_json or {}
        src_audio = _local_for_dev_url(req.get("source_audio_url"))
        if not src_audio or "placeholder" in src_audio.name:
            return None  # nothing real to restyle → fall back to a placeholder
        art = async_repo.get_artifact(req.get("source_video_artifact_id"))
        vloc = getattr(art, "local_path", None)
        video_path = Path(vloc) if vloc else None
        if not (video_path and video_path.exists()):
            return None
        try:
            from EdennCode.Deployment.audio_edit_workflows import AudioCreativeEditOrchestrator
            from EdennCode.Util.MediaUtils.ffmpeg_utils import overlay_music_on_video
            modelspec = normalize_music_modelspec(req.get("modelspec"))
            # The studio restyle fetches the source track from the vendor's side,
            # so a local path is useless to it — the stage refuses outright
            # without a reachable URL, which is why a studio restyle failed every
            # time it was asked for. Mint the same signed, expiring, read-only
            # blob URL the SFX video-native route uses: it works from anywhere,
            # and our tokens never ride in a URL handed to a third party.
            source_provider_url = media_store.vendor_read_url(src_audio, "media") or ""
            print(
                f"[devserver] REAL creative-edit ({modelspec}, "
                f"source_url={'signed' if source_provider_url else 'none'}) "
                f"for job {job.job_id[:12]} — takes minutes…"
            )
            res = await AudioCreativeEditOrchestrator(storage=None).run(
                source_audio_path=src_audio,
                source_audio_provider_url=source_provider_url,
                user_prompt=req.get("user_prompt", ""),
                modelspec=modelspec,
                video_path=video_path,
            )
            edited = Path(res.edited_audio_path)
            remixed = media_dir / f"cedit_{new_id('ce')}.mp4"
            overlay_music_on_video(
                video_path, edited, remixed,
                music_volume=float(req.get("music_volume", 1.0) or 1.0),
                preserve_original_audio=bool(req.get("preserve_original_audio", False)),
            )
            audio_url = publish_media(edited)
            video_url = f"/dev/media/{remixed.name}" if remixed.exists() else None
            print(f"[devserver] creative-edit ✓ job {job.job_id[:12]}")
            if measured is not None:
                from EdennCode.Util.MediaUtils import ffmpeg_utils

                try:
                    edited_s = float(ffmpeg_utils.get_video_duration(edited) or 0.0)
                except Exception:  # noqa: BLE001
                    edited_s = 0.0
                measured["measurement"] = Measurement(
                    delivered_ms=round(edited_s * 1000) if edited_s > 0 else None,
                    # One call, even when it returns more than one variant.
                    provider_calls=1,
                    tier=normalize_music_modelspec(
                        getattr(res, "used_music_model_spec", "") or modelspec
                    ),
                    tier_requested=normalize_music_modelspec(req.get("modelspec")),
                    primary_unit="delivered_ms",
                )
            return {
                "status": "completed", "audio_url": audio_url, "complete_audio_url": audio_url,
                "video_url": video_url, "placeholder": False,
            }
        except _RealGenerationFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[devserver] creative-edit FAILED ({str(exc)[:200]}) — job marked failed.")
            raise _RealGenerationFailed(str(exc)) from exc

    # ---- pre-generated demo bundle (EDENN_DEV_PREGEN=<dir>) -----------------
    # Demo replay: agent turns stay 100% REAL (live LLM reasoning), but heavy
    # generation resolves from a bundle of PRE-GENERATED real artifacts after a
    # short, honest "working" beat — so a live demo never waits minutes. The
    # bundle's manifest.json maps job_type -> ordered entries (cycled):
    #   {"video_music": [{"audio_url_file": "m1.mp3", "video_url_file": "m1.mp4"}],
    #    "voiceover":   [{"audio_url_file": "vo.mp3", "segments": [...]}],
    #    "video_sfx":   [{"video_url_file": "sfx.mp4", "audio_url_file": "sfx.mp3"}]}
    pregen_counts: dict[str, int] = {}

    def _pregen_dir() -> str:
        return os.getenv("EDENN_DEV_PREGEN", "").strip()

    async def _pregen_result(job: Any) -> dict[str, Any] | None:
        bundle = Path(_pregen_dir())
        manifest_path = bundle / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:  # noqa: BLE001
            return None
        seq = manifest.get(job.job_type) or []
        if not seq:
            return None
        idx = pregen_counts.get(job.job_type, 0) % len(seq)
        pregen_counts[job.job_type] = idx + 1
        entry = dict(seq[idx])
        # A short, honest "rendering…" beat so the UI's live states are visible.
        await asyncio.sleep(float(os.getenv("EDENN_DEV_PREGEN_DELAY", "4")))
        out: dict[str, Any] = {"status": "completed", "placeholder": False}
        for key in ("audio_url", "video_url"):
            fname = entry.get(key + "_file")
            if fname and (bundle / fname).exists():
                url = publish_media(bundle / fname)
                out[key] = url
                if key == "audio_url":
                    out["complete_audio_url"] = url
        if entry.get("segments"):
            out["segments"] = entry["segments"]
        if isinstance(entry.get("extra"), dict):
            out.update(entry["extra"])
        if not (out.get("audio_url") or out.get("video_url")):
            return None
        print(f"[devserver] PREGEN {job.job_type} #{idx} -> served from bundle")
        return out

    def _job_could_spend(job: Any) -> bool:
        """Whether dispatching this job would have attempted a paid render.

        Mirrors the dispatch chain in ``_complete_job`` exactly — the two
        deciding differently is how a failure gets classified as harmless. Used
        to pick the honest outcome for an unexpected exception: a job that
        could never spend degrades to a badged placeholder (a laptop without
        ffmpeg still demos), and a job that COULD have spent must never be
        recorded as a completed take it did not make.
        """
        req = job.request_json or {}
        modelspec = req.get("modelspec")
        if job.job_type == "video_music":
            return _real_music_enabled() and (
                _music_provider_key_present(modelspec)
                or any(
                    _music_provider_key_present(m)
                    for m in ("edenn_studio", "edenn_basic", "edenn_enhanced")
                )
            )
        if job.job_type == "voiceover":
            return _real_voiceover_enabled() and _voiceover_key_present()
        if job.job_type == "audio_creative_edit":
            return _real_music_enabled() and _music_provider_key_present(modelspec)
        if job.job_type == "video_sfx":
            return _real_sfx_enabled() and _sfx_key_present()
        return False

    async def _complete_job(job: Any) -> None:
        # What this render consumed. Each real render path fills this in as it
        # measures — one box per job, so two renders finishing at once cannot
        # read each other's numbers. Left empty means nothing was measured,
        # which a closed row records as unmeasured rather than as free.
        measured_box: dict[str, Any] = {}
        measurement: Optional[Any] = None
        try:
            result = None
            req = job.request_json or {}
            modelspec = req.get("modelspec")
            # Demo bundle first (explicitly enabled), then real generation per job
            # type when the provider key is present; everything degrades to a
            # badged placeholder on miss or failure.
            if _pregen_dir() and job.job_type in {"video_music", "voiceover", "audio_creative_edit", "video_sfx"}:
                result = await _pregen_result(job)
            if result is not None:
                # A replayed demo bundle is byte-indistinguishable from a real
                # render (it even sets placeholder: False), so the fact that it
                # spent nothing has to be reported here rather than inferred
                # from what it produced.
                measurement = Measurement(
                    provider_calls=0, items=0, text_chars=0,
                    note_code="demo_bundle_replay",
                )
            elif job.job_type == "video_music" and _real_music_enabled():
                # A modelspec whose provider has no key on this box must not
                # silently degrade to a beep-tone placeholder while OTHER real
                # providers sit configured — render on the best available tier
                # instead (logged honestly; the result carries the tier used).
                effective = modelspec
                if not _music_provider_key_present(effective):
                    # Same chain the propose-time gate uses — the two deciding
                    # differently is how a label goes stale between screens.
                    effective = next(
                        (m for m in ("edenn_studio", "edenn_basic", "edenn_enhanced")
                         if _music_provider_key_present(m)),
                        None,
                    )
                    if effective:
                        print(f"[devserver] modelspec {modelspec} has no provider key — "
                              f"rendering with {effective} instead.")
                if effective:
                    result = await _real_music_result(
                        job, modelspec_override=effective, measured=measured_box
                    )
            elif job.job_type == "voiceover" and _real_voiceover_enabled() and _voiceover_key_present():
                result = await _real_voiceover_result(job, measured=measured_box)
            elif job.job_type == "audio_creative_edit" and _real_music_enabled() and _music_provider_key_present(modelspec):
                result = await _real_creative_edit_result(job, measured=measured_box)
            elif job.job_type == "video_sfx" and _real_sfx_enabled() and _sfx_key_present():
                result = await _real_sfx_result(job, measured=measured_box)
            if job.job_type in {"video_music", "audio_creative_edit", "voiceover", "video_sfx"}:
                # One honest line per media job so "why placeholder?" is never a mystery.
                print(
                    f"[devserver] job {job.job_id[:12]} type={job.job_type} modelspec={modelspec} "
                    f"real_music={_real_music_enabled()} key_present={_music_provider_key_present(modelspec)} "
                    f"-> {'REAL' if result else 'placeholder'}"
                )
            measurement = measured_box.get("measurement") or measurement
            final_result = result or _placeholder_result(job)
            if result is None:
                # Nothing real ran: a badged stand-in for a box with no keys.
                measurement = Measurement(
                    provider_calls=0, items=0, text_chars=0,
                    note_code="placeholder_render",
                )
            # The status write and the meter close are one transaction, and the
            # close is NOT gated on whether the status applied: a sweep that
            # already failed this row does not make the money come back. It
            # records spend_unknown instead of asserting a delivery the store
            # refused.
            with async_repo.transaction() as tx:
                _, applied = async_repo.update_job_status_checked(
                    job.job_id,
                    status=JobStatus.COMPLETED,
                    result_json=final_result,
                    client=tx,
                )
                spent_nothing = bool(
                    measurement is not None and measurement.provider_calls == 0
                )
                if not applied:
                    outcome = OUTCOME_SPEND_UNKNOWN
                elif spent_nothing:
                    outcome = OUTCOME_NO_SPEND
                else:
                    outcome = OUTCOME_DELIVERED
                _close_meter(
                    job, outcome, measurement if applied else None, client=tx
                )
            if applied:
                _persist_result_media(final_result)
        except _RealGenerationFailed as exc:
            # The attempt ran and produced nothing usable. Marking this COMPLETED
            # with a stand-in tone is what let a failed paid generation reach the
            # user as a sine-wave "take"; fail the job so the UI badges it.
            print(f"[devserver] job {job.job_id[:12]} type={job.job_type} -> FAILED ({str(exc)[:160]})")
            # The error text can carry upstream vendor identities (client error
            # strings, key-pool exhaustion messages); this result_json is served
            # to the browser in job snapshots, so scrub before persisting.
            from EdennCode.Deployment.error_codes import scrub_provider_names

            async_repo.update_job_status(
                job.job_id,
                status=JobStatus.FAILED,
                result_json={
                    "status": "failed",
                    "error": scrub_provider_names(str(exc)[:500]),
                    "placeholder": False,
                },
            )
            # Money was spent and the customer got nothing. That is a different
            # bill from a render that never started, and until now the two were
            # the same silence.
            _close_meter(
                job,
                OUTCOME_FAILED_AFTER_SPEND,
                measured_box.get("measurement") or measurement,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[devserver] job {job.job_id[:12]} completion error: {str(exc)[:160]}")
            if _job_could_spend(job):
                # A real attempt may have run — and may have been billed —
                # before this raised. Recording a stand-in tone as a COMPLETED
                # take would hide that forever, and against a durable job table
                # "forever" is now literal: no restart wipes the row, and a
                # terminal status refuses every later correction. Fail it, so
                # the UI badges it and offers the retry.
                from EdennCode.Deployment.error_codes import scrub_provider_names

                async_repo.update_job_status(
                    job.job_id,
                    status=JobStatus.FAILED,
                    result_json={
                        "status": "failed",
                        "error": scrub_provider_names(str(exc)[:500]),
                        "placeholder": False,
                    },
                )
                _close_meter(
                    job,
                    OUTCOME_FAILED_AFTER_SPEND,
                    Measurement(note_code="unexpected_error_after_claim"),
                )
            else:
                # Nothing could have been spent — placeholder mode. Degrading
                # to a badged stand-in keeps a laptop without keys or ffmpeg
                # demoing instead of failing.
                async_repo.update_job_status(
                    job.job_id,
                    status=JobStatus.COMPLETED,
                    result_json=_placeholder_result(job),
                )
                _close_meter(job, OUTCOME_NO_SPEND, NO_SPEND)
        finally:
            inflight.discard(job.job_id)

    #: How often the completer looks for work. Short on purpose: the turn that
    #: creates a job is waiting on this loop to start it, so a longer interval
    #: is latency the user watches. Against the database this is one indexed
    #: read of unfinished rows only — the finished ones are excluded by a
    #: partial index and never walked.
    _COMPLETER_POLL_S = 0.5
    #: A claimed job says it is still alive this often. Comfortably inside the
    #: window after which another process may call it abandoned.
    _HEARTBEAT_EVERY_S = 20.0
    #: How often retention runs, when a deployment turns it on. Once a day: the
    #: window is measured in days, and every pass is a destructive action
    #: against a live database.
    _RETENTION_EVERY_S = 24 * 3600.0
    #: How often the completer re-runs the interrupted-job sweep. Boot-only was
    #: a hole with two routine shapes in it: a crash-and-quick-restart arrives
    #: while the dead process's heartbeat is still fresh (so the boot sweep
    #: skips the row), and a rolling deploy kills a claimant while a SURVIVING
    #: replica — whose one sweep already ran — lives on. Either way the row sat
    #: in `processing` for as long as any process lived, which is the exact
    #: spinner-forever this store was built to end. The heartbeat threshold is
    #: what makes a periodic sweep safe: a live sibling speaks every 20s and a
    #: row is only called dead after 300s of silence.
    _SWEEP_EVERY_S = 60.0
    #: The dispatch loop enforces the SAME queued-age gate the sweep uses, so
    #: the two cannot disagree about whether an old row is work or debris.
    from EdennCode.EdennAgent.AgenticAudio.persistence.jobs import (
        DEFAULT_QUEUED_MAX_AGE_S as _QUEUED_MAX_AGE_S,
    )

    async def job_completer() -> None:
        """Run the jobs this process is responsible for, exactly once each.

        The loop has three jobs of its own, and they are separate on purpose:

        **Find** open work — a bounded read, so a long-lived store with tens of
        thousands of finished rows costs the same as an empty one.

        **Claim** it — ``queued → processing``, atomically, before anything
        spends provider credit. That write is what makes ``queued`` mean *not
        started*, which is the entire basis on which a later boot decides
        whether re-running a row would double-bill someone.

        **Say it is alive** — a claimed row that stops speaking is how a
        restart gets noticed at all. Silence has to mean something, so noise
        has to be maintained.
        """
        seen: dict[str, float] = {}
        loop = asyncio.get_event_loop()
        last_beat = loop.time()
        # Start a full interval out so the boot-time sweep is not doubled.
        last_sweep = loop.time()
        while True:
            now = loop.time()
            try:
                open_jobs = await asyncio.to_thread(
                    async_repo.list_open_jobs,
                    exclude_job_types=("asset_staging",),
                )
            except Exception as exc:  # noqa: BLE001 — a poll must never end the loop
                print(f"[devserver] job poll failed: {str(exc)[:160]}")
                await asyncio.sleep(_COMPLETER_POLL_S)
                continue

            # Forget jobs that have finished. Against a durable store this dict
            # would otherwise grow for the life of the process.
            live = {job.job_id for job in open_jobs}
            for stale in [job_id for job_id in seen if job_id not in live]:
                seen.pop(stale, None)

            for job in open_jobs:
                if job.job_type == "asset_staging":
                    continue
                # TERMINAL means terminal. This loop used to skip only COMPLETED,
                # and `inflight` is discarded in a finally — so a job that FAILED
                # was picked up again half a second later and generated again,
                # and again, for the life of the container. Every one of those
                # attempts spends real provider credit on a job the user has
                # already been told did not work. A failed generation is a thing
                # to retry deliberately, never a thing to retry in a loop.
                if job.status in (
                    JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED
                ):
                    continue
                if job.job_id in inflight:
                    continue
                # Anything not queued is already somebody's: this process has it
                # in `inflight`, or another replica is mid-render and its
                # heartbeat is what will eventually settle the question. Trying
                # to claim it every half second would be a write per tick for an
                # answer that cannot change here.
                if job.status != JobStatus.QUEUED:
                    continue
                # A queued row old enough that the sweep would abandon it must
                # not be dispatched, EVEN in the window between a boot whose
                # reconcile failed and the first in-loop sweep. The gate lives
                # at the dispatch decision, not only in the sweep, so the two
                # cannot disagree: starting yesterday's render on today's boot
                # is a surprise result and a surprise charge, and the sweep
                # will fail the row honestly on its next pass.
                created = getattr(job, "created_at", None)
                if created is not None:
                    age_s = (datetime.now(created.tzinfo) - created).total_seconds()
                    if age_s >= _QUEUED_MAX_AGE_S:
                        continue
                seen.setdefault(job.job_id, now)
                if now - seen[job.job_id] >= 2.0:  # brief "queued" beat, then complete
                    try:
                        claimed = await asyncio.to_thread(
                            lambda: async_repo.claim_job(
                                job.job_id,
                                runner_id=runner_id,
                                # In the claim's own transaction: no usage row,
                                # no claim, no spend.
                                on_claim=_open_meter_for,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        # The one repository call in this loop that had no net.
                        # An exception here didn't fail a job — it ended this
                        # task, silently, and the app served requests for the
                        # rest of its life while never completing anything.
                        print(f"[devserver] job claim failed: {str(exc)[:160]}")
                        continue
                    if claimed is None:
                        # Lost the race to another replica. Theirs to finish.
                        continue
                    inflight.add(claimed.job_id)
                    asyncio.create_task(_complete_job(claimed))

            if inflight and now - last_beat >= _HEARTBEAT_EVERY_S:
                last_beat = now
                try:
                    await asyncio.to_thread(
                        async_repo.touch_heartbeat,
                        tuple(inflight),
                        runner_id=runner_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[devserver] job heartbeat failed: {str(exc)[:160]}")

            # The sweep, again and again — not only at boot. This is also what
            # gives the boot-time call a second chance: it swallows its own
            # failure so a database blip cannot block a start, and before this
            # loop existed that swallow silently dropped every gate the sweep
            # carries for the life of the process. The residual race is judged
            # acceptable and fail-closed: a render whose process goes silent
            # for the full stale window and THEN finishes writes into a row the
            # sweep already failed, the checked update refuses, and the money
            # is honestly lost rather than the failure hidden.
            if now - last_sweep >= _SWEEP_EVERY_S:
                last_sweep = now
                try:
                    settled = await asyncio.to_thread(_reconcile_interrupted_jobs)
                    for kind, job_ids in settled.items():
                        if job_ids:
                            print(f"[devserver] sweep closed {len(job_ids)} "
                                  f"{kind} render(s): "
                                  f"{', '.join(j[:12] for j in job_ids)}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[devserver] interrupted-job sweep failed: {str(exc)[:160]}")
            await asyncio.sleep(_COMPLETER_POLL_S)

    def _refuse_to_sign_with_a_secret_that_dies_on_restart() -> None:
        """Share links and connection tickets must outlive the process.

        Both are signed with an explicit secret if one is set, otherwise with a
        hash of the static key map, otherwise with a random per-process key.
        That last fallback is invisible and, on a real deployment, wrong in two
        ways at once: every share link a customer sent breaks on the next
        restart, and with more than one replica a ticket minted by one is
        rejected by the next — a WebSocket that reconnects to the wrong
        instance and simply fails.

        It went unnoticed because the key map was always set. Retiring it for a
        verified identity — which is the whole point of the move — removes the
        derivation and lands the deployment on the random key.
        """

        if not _deployed_shape():
            return  # a laptop; a per-process secret is exactly right
        missing = [
            name
            for name in ("AGENTIC_AUDIO_SHARE_SECRET", "AGENTIC_AUDIO_TICKET_SECRET")
            if not os.getenv(name, "").strip()
        ]
        if not missing or os.getenv("AGENTIC_AUDIO_API_KEYS", "").strip():
            return
        raise RuntimeError(
            "Refusing to start: " + " and ".join(missing) + " must be set on a "
            "deployed instance. Without them share links are signed with a key "
            "that is regenerated on every restart, so every link already sent "
            "to a customer stops working, and connection tickets minted by one "
            "replica are rejected by another."
        )

    def _deployed_shape() -> bool:
        """Running somewhere other than a laptop, by the shape of the host."""

        return bool(
            os.getenv("CONTAINER_APP_NAME")
            or os.getenv("WEBSITE_HOSTNAME")
            or os.getenv("EDENN_PUBLIC_BASE_URL")
            or HOST not in {"127.0.0.1", "localhost", "::1"}
        )

    def _refuse_to_serve_publicly_without_auth() -> None:
        """Never let this reach the open internet unauthenticated.

        Everything here spends real provider credit — music, sound design and
        every line of narration — and the router's auth is OFF by default so
        local runs stay frictionless. That default is right for a laptop and
        dangerous on a public URL, where anyone who has the link can spend the
        keys. The standalone deployment shipped exactly that way.

        So the check is on the shape of the deployment rather than on anyone
        remembering: a loopback bind stays untouched, and a publicly-reachable
        one must either enable auth or say out loud that it means to be open.
        """

        from EdennCode.EdennAgent.AgenticAudio.api.auth import (
            _api_keys,
            auth_enabled,
            token_is_user_mode,
        )
        from EdennCode.EdennAgent.AgenticAudio.api.identity import (
            identity_configured,
        )

        if auth_enabled():
            # Auth ON is not the same as auth WORKING. With no way to
            # authenticate anyone the router refuses every request, so the app
            # would boot healthy and serve nothing but 401s. Say it here, at
            # startup, where someone is still watching the logs.
            #
            # A verified identity is now one of those ways, and the one the
            # product is moving to: a deployment that has retired the static
            # token map is CORRECTLY configured, and the old check called it
            # broken.
            if not (identity_configured() or _api_keys() or token_is_user_mode()):
                raise RuntimeError(
                    "Refusing to start: AGENTIC_AUDIO_REQUIRE_AUTH is on but "
                    "nothing can authenticate a caller, so every request would "
                    "be rejected. Configure identity "
                    "(AGENTIC_AUDIO_IDP_PROJECT_ID), or set the deprecated key "
                    "map (AGENTIC_AUDIO_API_KEYS='token:user,...'), or set "
                    "AGENTIC_AUDIO_TOKEN_IS_USER=1 for a local run."
                )
            _refuse_to_sign_with_a_secret_that_dies_on_restart()
            return
        if not _deployed_shape():
            return
        if os.getenv("EDENN_DEV_ALLOW_NO_AUTH", "").strip().lower() in {"1", "true", "yes", "on"}:
            print("[devserver] WARNING: serving publicly with NO AUTH — "
                  "anyone with the URL can spend provider credit.")
            return
        raise RuntimeError(
            "Refusing to start: this server is reachable beyond localhost with "
            "authentication disabled, and every generation it runs spends real "
            "provider credit. Set AGENTIC_AUDIO_REQUIRE_AUTH=1 (with "
            "AGENTIC_AUDIO_API_KEYS='token:user,...'), or set "
            "EDENN_DEV_ALLOW_NO_AUTH=1 if an open instance is genuinely intended."
        )

    def _reconcile_interrupted_renders() -> int:
        """Tell sessions the truth about work no store can account for.

        A session pointing at a job id that exists nowhere shows the candidate
        or layer as "queued" forever — a spinner with nothing behind it, on
        work the customer has already paid for. Nothing times it out and
        nothing reports it, because from the session's point of view the render
        simply never came back.

        This does not recover the money and does not pretend to: the generation
        may well have completed upstream after the process died. What it does is
        stop the lie. A render that cannot still be running is marked as
        interrupted, which is a thing the user can act on — retry it, or not.

        With a durable job store this is no longer the main defence — the job
        row itself now survives, carries why it stopped, and reaches the UI
        through ordinary hydration. What is left for this pass is the case the
        job table cannot answer: a session pointing at a job id that is in no
        store at all. That is every session written while the server ran in
        memory, and any row a retention sweep has since removed.
        """

        if not hasattr(agent_repo, "list_sessions"):
            return 0
        repaired = 0
        for row in agent_repo.list_sessions(limit=500):
            session_id = str(row.get("session_id") or "")
            if not session_id:
                continue
            session = agent_repo.get_session(session_id)
            if session is None:
                continue
            state = dict(session.state_json or {})
            touched = False

            def _orphaned(item: dict[str, Any]) -> bool:
                job_id = item.get("linked_job_id")
                return bool(
                    job_id
                    and item.get("status") in ("queued", "processing")
                    and async_repo.get_job(str(job_id)) is None
                )

            candidates = [dict(c) for c in (state.get("candidates") or [])]
            for candidate in candidates:
                if _orphaned(candidate):
                    candidate["status"] = "failed"
                    candidate["error"] = (
                        "This render was interrupted when the service restarted. "
                        "Nothing was delivered — generate it again when you are ready."
                    )
                    touched = True
            if touched:
                state["candidates"] = candidates

            layers = {k: dict(v) if isinstance(v, dict) else v
                      for k, v in (state.get("layers") or {}).items()}
            for name, layer in layers.items():
                if isinstance(layer, dict) and _orphaned(layer):
                    layer["status"] = "failed"
                    layer["error"] = (
                        "This render was interrupted when the service restarted."
                    )
                    touched = True
            if touched:
                state["layers"] = layers
                agent_repo.update_session(session_id, state_json=state)
                repaired += 1
        return repaired

    def _reconcile_interrupted_jobs() -> dict[str, list[str]]:
        """Close out renders that no live process is doing.

        Runs before the completer starts — so the loop never dispatches into a
        row this pass is about to settle — and then again from inside the loop
        every ``_SWEEP_EVERY_S``. Boot-only was a hole: a crash followed by a
        quick restart arrives while the dead process's heartbeat is still
        fresh, and a rolling deploy kills a claimant next to a surviving
        replica whose one sweep already ran. Either way the row sat in
        ``processing`` for as long as any process lived. What it must not do is re-run anything: a job
        found in ``processing`` was mid-render when its container died, and the
        provider call it was making may well have completed and been billed.
        Generating it again turns one paid render into two and hands the user a
        second take they did not ask for. Marking it failed is the honest
        outcome — the UI already offers a retry on a failed take, which puts
        the decision back where it belongs.
        """
        if not hasattr(async_repo, "fail_interrupted"):
            return {"interrupted": [], "abandoned": []}
        settled = async_repo.fail_interrupted(runner_id=runner_id)
        # An interrupted render was CLAIMED, so it opened a usage row and may
        # well have been billed upstream before its container died. Leaving the
        # row open would leave the spend permanently unaccounted for; closing it
        # as delivered would assert something nobody knows. It is recorded as
        # spend_unknown, which is an instruction to reconcile rather than a
        # judgement.
        interrupted = list(settled.get("interrupted") or [])
        if interrupted:
            try:
                meter.settle_for_jobs(
                    job_ids=interrupted,
                    outcome=OUTCOME_SPEND_UNKNOWN,
                    note_code="interrupted",
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[devserver] usage meter: interrupted rows not settled "
                      f"({str(exc)[:120]})")
        # An abandoned job was never claimed, so it never opened a row, and the
        # absence IS the record that nothing was spent. A row here means the
        # claim/open invariant is broken — say so rather than quietly fixing it.
        abandoned = list(settled.get("abandoned") or [])
        if abandoned:
            try:
                stray = meter.open_rows_for_jobs(abandoned)
                if stray:
                    print("[devserver] WARNING: usage rows exist for jobs that were "
                          f"never claimed: {', '.join(j[:12] for j in stray[:5])}")
            except Exception:  # noqa: BLE001
                pass
        return settled

    # Published so an operator can run either pass on demand — a container that
    # came back before its sessions were written is the case they exist for.
    app.state.reconcile_interrupted_renders = _reconcile_interrupted_renders
    app.state.reconcile_interrupted_jobs = _reconcile_interrupted_jobs
    app.state.runner_id = runner_id

    def _retention_mode() -> str:
        """off (default) | dry | apply.

        DEFAULT OFF, and deliberately. This is the one background task whose
        job is to destroy a customer's work, on a window read from an
        environment variable. A wrong value here is not a bug that shows up as
        an error — it is footage that is gone. Turning it on is an explicit
        operational decision, and "dry" exists so the first thing anyone does
        with a deletion job is watch it not delete anything.
        """

        mode = os.getenv("AGENTIC_AUDIO_RETENTION_SWEEP", "off").strip().lower()
        return mode if mode in {"dry", "apply"} else "off"

    async def retention_sweeper() -> None:
        """Keep the 7-day promise on a timer, if this deployment opted in.

        The promise has existed in code for a while and nothing ever called it,
        so the interval was real and the deletion never happened — the worst of
        both, since the policy is published and unkept.
        """

        from EdennCode.EdennAgent.AgenticAudio.persistence import retention

        mode = _retention_mode()
        apply = mode == "apply"
        print(f"[devserver] retention sweep: {mode} "
              f"(every {_RETENTION_EVERY_S / 3600:.0f}h, "
              f"{retention.retention_days()}d window)")
        while True:
            # Sleep FIRST: a restart loop must not turn into a deletion loop,
            # and nothing is so stale that it cannot wait one interval.
            await asyncio.sleep(_RETENTION_EVERY_S)
            try:
                result = await asyncio.to_thread(
                    retention.sweep,
                    agent_repo,
                    apply=apply,
                    job_repository=async_repo,
                    media_store=media_store,
                )
                print(f"[devserver] {result.summary}")
            except Exception as exc:  # noqa: BLE001 - a failed pass is not fatal
                print(f"[devserver] retention sweep failed: {str(exc)[:200]}")

    @app.on_event("startup")
    async def _startup() -> None:
        _refuse_to_serve_publicly_without_auth()
        try:
            settled = await asyncio.to_thread(_reconcile_interrupted_jobs)
            for kind, job_ids in settled.items():
                if job_ids:
                    print(f"[devserver] {len(job_ids)} {kind} render(s) closed out "
                          f"rather than re-run: {', '.join(j[:12] for j in job_ids)}")
        except Exception as exc:  # noqa: BLE001 — never block a boot on cleanup
            print(f"[devserver] interrupted-job reconcile skipped: {str(exc)[:160]}")
        try:
            repaired = await asyncio.to_thread(_reconcile_interrupted_renders)
            if repaired:
                print(f"[devserver] {repaired} session(s) had a render this "
                      "container never finished — marked interrupted rather "
                      "than left spinning")
        except Exception as exc:  # noqa: BLE001 — never block a boot on cleanup
            print(f"[devserver] interrupted-render reconcile skipped: {str(exc)[:160]}")
        app.state.ready = True
        app.state.draining = False
        app.state._completer = asyncio.create_task(job_completer())

        def _completer_died(task: "asyncio.Task[Any]") -> None:
            # Every path inside the loop has its own net, so this should never
            # fire — which is exactly why it must be observed. An unobserved
            # task exception here meant the app served requests for the rest
            # of its life while silently never completing another render.
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                print(f"[devserver] FATAL: job completer died: {str(exc)[:300]} — "
                      "renders will queue and never run until this process restarts")

        app.state._completer.add_done_callback(_completer_died)
        if _retention_mode() != "off":
            app.state._retention = asyncio.create_task(retention_sweeper())
        app.state.llm_status = llm_label
        # After the server is up, probe the LIVE model once so it's obvious in the
        # logs whether real calls actually work (no silent placeholder surprises).
        if llm_live:
            from EdennCode.EdennAgent.AgenticAudio.models import AGENT_DECISION_SCHEMA

            try:
                await llm_client.complete_messages(
                    [
                        {"role": "system", "content": "Reply with a noop action and a one-line assistant_message."},
                        {"role": "user", "content": "ping"},
                    ],
                    json_schema=AGENT_DECISION_SCHEMA,
                )
                app.state.llm_status = llm_label + " — reachable ✓"
            except Exception as exc:  # noqa: BLE001
                app.state.llm_status = llm_label + " — UNREACHABLE"
                print(f"  [!] live LLM probe FAILED: {str(exc)[:160]}")
                print("      calls will retry then error (no placeholder). Check network/keys.")
        print("=" * 64)
        print("  Edenn agentic-audio DEV SERVER")
        print(f"  LLM:        {app.state.llm_status}")
        print(f"  Open:       http://{HOST}:{PORT}/?backend=real")
        print("=" * 64)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        # Fail readiness FIRST: the probe is how the load balancer learns to stop
        # sending turns here. Cancelling the completer before that would drop
        # work the platform still believes this replica is accepting.
        app.state.draining = True
        app.state.ready = False

        # Then WAIT for whatever is mid-flight. A turn that is killed here has
        # usually already spent — the provider render is paid for and running —
        # so the user loses a take they were charged for, and the session is left
        # in whatever half-written state the kill interrupted. The grace period
        # is bounded because a deploy cannot wait forever on a stuck turn.
        deadline = float(os.getenv("EDENN_DRAIN_TIMEOUT_S", "25"))
        waited = 0.0
        step = 0.25
        while waited < deadline:
            planner = getattr(app.state, "agentic_planner", None)
            agent = getattr(planner, "agent", planner)
            in_flight = int(getattr(agent, "turns_in_flight", 0) or 0)
            if in_flight <= 0:
                break
            if waited == 0.0:
                print(f"[devserver] draining: waiting for {in_flight} turn(s)")
            await asyncio.sleep(step)
            waited += step
        planner = getattr(app.state, "agentic_planner", None)
        agent = getattr(planner, "agent", planner)
        remaining = int(getattr(agent, "turns_in_flight", 0) or 0)
        if remaining > 0:
            # Say it out loud: this is the case where a user is about to lose
            # work that was paid for.
            print(
                f"[devserver] drain timed out after {deadline:.0f}s with "
                f"{remaining} turn(s) still running — they will be interrupted."
            )

        task = getattr(app.state, "_completer", None)
        if task:
            task.cancel()

    # A bare "/" visit used to silently run the in-browser MOCK (the frontend only
    # auto-picks the real backend when served under /api/v2/…). This page IS backed
    # by a live API — default it to ?backend=real; the pill still toggles to mock.
    @app.get("/", include_in_schema=False)
    async def _root(request: Request) -> Any:
        if "backend" not in request.query_params:
            q = dict(request.query_params)
            q["backend"] = "real"
            return RedirectResponse("/?" + urlencode(q))
        return FileResponse(APP_DIR / "index.html", headers=_DEV_NO_STORE)

    # The console's assets at the root (same-origin as the API) — and ONLY those.
    # This was a blanket StaticFiles mount of the whole frontend directory, which
    # served the offline mock backend and the design notes sitting beside it to
    # anonymous callers, and bypassed the router's deliberate production block on
    # that mock: the block guards the OTHER path to the same files.
    #
    # Nothing this harness serves is cacheable. Starlette sends only ETag /
    # Last-Modified, and with no Cache-Control a browser is free to reuse a
    # cached copy WITHOUT revalidating. On a server whose whole point is editing
    # these files that is the wrong default and it fails confusingly — a stale
    # index.html against a fresh styles.css renders a broken layout that looks
    # like a code bug, and a stale js/ file silently shows yesterday's UI.
    #
    # That reasoning is right for a machine someone is editing on, and wrong for
    # the deployment: this same server IS the standalone product, where no-store
    # on a ?v=-stamped asset means every visitor re-downloads every file on
    # every load, and the whole point of the version stamp is that they should
    # not have to. So the answer depends on which of the two this is. The
    # document is never cached hard either way; only the stamped assets differ.
    from EdennCode.EdennAgent.AgenticAudio.api.router import (
        FRONTEND_ASSETS,
        _mock_backend_allowed,
    )

    @app.get("/{asset:path}", include_in_schema=False)
    async def _console_asset(asset: str, request: Request) -> FileResponse:
        name = asset or "index.html"
        if name not in FRONTEND_ASSETS:
            raise HTTPException(status_code=404, detail="Not found.")
        if name == "mock-backend.js" and not _mock_backend_allowed():
            raise HTTPException(status_code=404, detail="Not found.")
        target = (APP_DIR / name).resolve()
        if not target.is_file() or APP_DIR.resolve() not in target.parents:
            raise HTTPException(status_code=404, detail="Not found.")
        if _deployed:
            return FileResponse(
                target,
                headers=_console_cache_headers(
                    name, bool(request.query_params.get("v"))
                ),
            )
        return FileResponse(target, headers=_DEV_NO_STORE)

    return app


app = build_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
