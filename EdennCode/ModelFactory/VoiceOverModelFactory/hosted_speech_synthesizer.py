"""Narration speech synthesis via the hosted speech provider.

This is an ADAPTER BOUNDARY. The vendor's endpoint and request shapes live in
this one file; everything that leaves it — log lines, exception text, file
names, the class name callers import — is provider-neutral. Callers speak the
product's own vocabulary (a preset voice, a delivery instruction, a speed) and
get a WAV on disk back.

Interface-compatible with the platform-TTS synthesizer used by the voice-over
worker, so the two can be swapped by configuration:

    synthesize(script=..., voice=..., instructions=..., speed=..., out_path=...)

Design notes, in the order they will matter during an incident:

* Voices resolve through a STATIC map of the provider's premade voice ids —
  those ids are global constants, identical on every account. No listing call:
  the account's keys are scoped to synthesis only (listing returns
  missing_permissions), and a narration must not fail over an endpoint it
  never needed. Custom accounts can override the map with a JSON env variable.
* Delivery instructions are freeform text written by the director model. The
  provider's speech endpoint takes numeric settings, not prose, so the prose is
  mapped to settings by keyword. That is lossy and DELIBERATE: a wrong
  inflection is recoverable, an API error is not. The full instruction text is
  still sent as the segment's context so future models that accept prose can
  use it.
* The provider returns compressed audio; the pipeline expects PCM WAV at
  ``out_path``. Conversion happens here so no caller ever learns the wire
  format.
* Error text is scrubbed: whatever the provider's response body says, the
  exception a caller sees names no vendor.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_API_BASE = "https://api.provider-a.example.invalid"  # vendor detail; must not leak outward
_KEY_ENV = "PROVIDER_A_API_KEY"
_MODEL_ENV = "AGENTIC_AUDIO_SPEECH_MODEL"
_DEFAULT_MODEL = "eleven_multilingual_v2"

# The product's preset voices (models.VOICE_CATALOG) name engine voices like
# "shimmer"/"onyx". Each maps to one of the provider's PREMADE voices by id —
# global constants, the same on every account (live-verified 2026-08-26).
# AGENTIC_AUDIO_SPEECH_VOICE_MAP ({"shimmer": "<voice-id>", ...}) overrides
# entries for accounts that want their own voices, without a code change.
_VOICE_MAP_ENV = "AGENTIC_AUDIO_SPEECH_VOICE_MAP"
_PREMADE_VOICE_IDS: Dict[str, str] = {
    # warm_female — warm, friendly
    "shimmer": "21m00Tcm4TlvDq8ikWAM",
    # bright_female — bright, upbeat
    "nova": "MF3mGyEYCl7XYWbV9V6O",
    # calm_male — calm, authoritative
    "onyx": "pNInz6obpgDQGcFmaJgB",
    # narrator_male — cinematic narrator
    "echo": "JBFqnCBsd6RMkjVDRZzb",
    # neutral — clean, neutral
    "alloy": "ErXwobaYiN019PkySvjV",
    # gravel_trailer — gravel and coiled threat
    "ash": "N2lVS1w4EtoT3dr4eOWO",
    # velvet_female — smoky, late-night
    "coral": "XB0fDUnXU5powFXDhCwa",
    # worn_storyteller — raspy, lived-in
    "ballad": "yoZ06aMxZJJ28mfd3POQ",
    # commanding_female — assured, precise
    "sage": "AZnzlk1XvdvUeBnXmlld",
    # easy_male — conversational, natural
    "fable": "IKne3meq5aSn9XLyUdCD",
}
_DEFAULT_VOICE_KEY = "shimmer"

# Keyword → settings nudges. Base is a steady, natural read; instructions move
# it. Stability high = consistent/restrained; style high = expressive.
_BASE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75, "style": 0.1,
                  "use_speaker_boost": True}
_DIRECTION_RULES = (
    # "Restrained" is a PERFORMANCE, not an absence of one. The first mapping
    # sent stability to 0.68 — the provider's own docs call that territory
    # "monotonous voice with limited emotion" — and a listener called the
    # result detached and generic. Calm keeps a live mid-stability and enough
    # style to stay present.
    (re.compile(r"restrain|calm|measured|soft|warm|gentle|delicate|intimate|quiet|low|controlled|grave", re.I),
     {"stability": 0.55, "style": 0.25}),
    (re.compile(r"upbeat|bright|energetic|excited|punchy|lively|youthful", re.I),
     {"stability": 0.35, "style": 0.55}),
    (re.compile(r"cinematic|dramatic|intense|epic|trailer|gritty|tense", re.I),
     {"stability": 0.45, "style": 0.5}),
    (re.compile(r"authoritat|confident|assured|documentary|narrator", re.I),
     {"stability": 0.6, "style": 0.3}),
)

# ``provider_a`` must not match inside ``provider_audio_id``; the boundary is
# the same rule provider_vocabulary.py applies.
_SCRUB = re.compile(r"eleven\s*labs|(?<![a-z])provider_a(?![a-z])|eleven_[a-z0-9_]+|xi-api-key", re.I)

# The provider's expressive model line: takes inline bracketed delivery tags
# (the only engine whose read can follow the director's prose), accepts ONLY a
# stability setting, and silently ignores speed (measured 2026-08-26).
_EXPRESSIVE_MODEL_PREFIX = "eleven_v3"
# The renderer appends the per-line direction as "Delivery for this line: X."
_DELIVERY_IN_INSTRUCTIONS = re.compile(
    r"Delivery for this line:\s*(.+?)\.?\s*$", re.IGNORECASE
)


def _delivery_tag(instructions: str) -> str:
    """The director's per-line delivery as an inline tag, or empty.

    Kept short and stripped of brackets: the tag is direction, not content,
    and a malformed one would be read aloud.
    """

    m = _DELIVERY_IN_INSTRUCTIONS.search(instructions or "")
    phrase = (m.group(1) if m else "").strip().strip("[]").strip()
    if not phrase or len(phrase) > 60:
        return ""
    return f"[{phrase}]"


def _scrubbed(text: str) -> str:
    """Vendor markers out of anything that could reach a log or a client."""

    return _SCRUB.sub("the speech provider", text or "")


class HostedSpeechSynthesizer:
    """Speech synthesis through the hosted speech provider (adapter)."""

    def __init__(self, *, api_key: Optional[str] = None, model: Optional[str] = None,
                 timeout_s: float = 120.0) -> None:
        self._api_key = (api_key or os.getenv(_KEY_ENV, "")).strip()
        if not self._api_key:
            raise RuntimeError(
                "no speech-provider credential configured for narration TTS"
            )
        self._model = (model or os.getenv(_MODEL_ENV, "") or _DEFAULT_MODEL).strip()
        self._timeout_s = timeout_s

    # ------------------------------------------------------------------ #
    # public interface (matches the platform-TTS synthesizer)             #
    # ------------------------------------------------------------------ #

    @property
    def supports_speed(self) -> bool:
        """False on the expressive model line, which accepts a speed setting
        and silently ignores it (measured). The retake ladder checks this —
        re-rendering for a duration change that cannot happen is pure spend."""

        return not self._model.startswith(_EXPRESSIVE_MODEL_PREFIX)

    async def synthesize(self, *, script: str, voice: str, instructions: str,
                         speed: float, out_path: Path) -> Path:
        import asyncio

        return await asyncio.to_thread(
            self._synthesize_sync, script, voice, instructions, speed, out_path
        )

    # ------------------------------------------------------------------ #
    # internals                                                           #
    # ------------------------------------------------------------------ #

    def _synthesize_sync(self, script: str, voice: str, instructions: str,
                         speed: float, out_path: Path) -> Path:
        text = (script or "").strip()
        if not text:
            raise ValueError("empty narration script")
        self._guard_speed_support(speed)
        voice_id = self._resolve_voice(voice)
        if self._model.startswith(_EXPRESSIVE_MODEL_PREFIX):
            # The expressive line reads its direction inline: the per-line
            # delivery becomes a leading tag, and the only setting it honors
            # is stability — a live middle, so the tag has room to act.
            tag = _delivery_tag(instructions)
            payload: Dict[str, Any] = {
                "text": f"{tag} {text}".strip(),
                "model_id": self._model,
                "voice_settings": {"stability": 0.5},
            }
        else:
            payload = {
                "text": text,
                "model_id": self._model,
                "voice_settings": self._settings_for(instructions, speed),
            }
        body = json.dumps(payload).encode("utf-8")

        # The with-timestamps variant returns per-character start/end times in
        # the SAME call at the same price. Narration here is fitted into gaps
        # between cuts, so a measured duration beats any estimate; the raw-audio
        # endpoint would throw that measurement away.
        url = (f"{_API_BASE}/v1/text-to-speech/{voice_id}/with-timestamps"
               f"?output_format=mp3_44100_128")
        started = time.perf_counter()
        payload = self._post(url, body)
        audio, alignment = self._parse_timestamped(payload)
        logger.info(
            "narration speech synthesis took %.2fs (%d chars -> %d bytes)",
            time.perf_counter() - started, len(text), len(audio),
        )
        self._write_wav(audio, out_path)
        if alignment:
            # Sidecar, not return value: the synthesize() interface is shared
            # with the platform engine, which has no timing data to offer. A
            # caller that wants measured times reads <out>.alignment.json.
            sidecar = out_path.with_suffix(out_path.suffix + ".alignment.json")
            sidecar.write_text(json.dumps(alignment))
        return out_path

    def _guard_speed_support(self, speed: float) -> None:
        """The newest expressive model ACCEPTS a speed setting and silently
        ignores it (measured 2026-08-26: identical durations at 0.7/1.0/1.2,
        HTTP 200 every time). A model swap must not quietly re-introduce that
        timing bug, so a non-neutral speed on a model that drops it is loud."""
        try:
            wants_speed = abs(float(speed) - 1.0) > 1e-9
        except (TypeError, ValueError):
            wants_speed = False
        if wants_speed and self._model.startswith("eleven_v3"):
            logger.warning(
                "the configured speech model ignores speed adjustments; "
                "narration timing fit will be wrong — use the default model "
                "for fitted segments"
            )

    @staticmethod
    def _parse_timestamped(payload: bytes) -> tuple[bytes, Optional[Dict[str, Any]]]:
        """The timestamped endpoint returns JSON (base64 audio + alignment).
        Tolerate a raw-audio body too, so an endpoint rollback never breaks
        synthesis — timing data is an upgrade, not a dependency."""
        try:
            doc = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return payload, None
        audio = base64.b64decode(doc.get("audio_base64") or "")
        if not audio:
            raise RuntimeError("speech provider returned no audio payload")
        alignment = doc.get("alignment") or None
        return audio, alignment

    def _post(self, url: str, body: bytes, *, attempts: int = 3) -> bytes:
        last: Optional[str] = None
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(url, data=body, headers={
                "xi-api-key": self._api_key,
                "Content-Type": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                    payload = resp.read()
                    if resp.status == 200 and payload:
                        return payload
                    last = f"status {resp.status}, empty body"
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except Exception:  # noqa: BLE001
                    pass
                last = f"status {exc.code}: {_scrubbed(detail)}"
                # 4xx other than rate-limit will not improve on retry.
                if exc.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError) as exc:
                last = _scrubbed(str(exc))
            if attempt < attempts:
                time.sleep(1.5 * attempt)
        raise RuntimeError(f"speech provider request failed ({last})")

    def _resolve_voice(self, voice: str) -> str:
        wanted = (voice or "").strip().lower()
        overrides = self._voice_overrides()
        vid = overrides.get(wanted) or _PREMADE_VOICE_IDS.get(wanted)
        if vid:
            return vid
        logger.warning(
            "unknown narration voice %r; using the default preset", wanted,
        )
        return overrides.get(_DEFAULT_VOICE_KEY) or _PREMADE_VOICE_IDS[_DEFAULT_VOICE_KEY]

    @staticmethod
    def _voice_overrides() -> Dict[str, str]:
        raw = os.getenv(_VOICE_MAP_ENV, "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return {
                str(k).strip().lower(): str(v).strip()
                for k, v in parsed.items()
                if str(v).strip()
            }
        except (ValueError, AttributeError):
            logger.warning(
                "%s is not a JSON object of voice overrides; ignoring it",
                _VOICE_MAP_ENV,
            )
            return {}

    @staticmethod
    def _settings_for(instructions: str, speed: float) -> Dict[str, Any]:
        settings: Dict[str, Any] = dict(_BASE_SETTINGS)
        for pattern, nudge in _DIRECTION_RULES:
            if pattern.search(instructions or ""):
                settings.update(nudge)
                break  # first match wins; directions rarely mix well
        # The provider accepts 0.7–1.2; the pipeline's own resolver already
        # clamps harder, this is the seatbelt.
        try:
            s = float(speed)
        except (TypeError, ValueError):
            s = 1.0
        settings["speed"] = max(0.7, min(1.2, s))
        return settings

    @staticmethod
    def _write_wav(compressed: bytes, out_path: Path) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(compressed)
            tmp_path = Path(tmp.name)
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-i", str(tmp_path),
                 "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le",
                 str(out_path)],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0 or not out_path.exists():
                raise RuntimeError(
                    f"narration audio conversion failed: {proc.stderr[:200]}"
                )
        finally:
            tmp_path.unlink(missing_ok=True)


def _settings_for_test_hook(instructions: str, speed: Any) -> Dict[str, Any]:
    """Test seam for the direction mapping without instantiating the client."""

    return HostedSpeechSynthesizer._settings_for(instructions, speed)


__all__ = ["HostedSpeechSynthesizer"]
