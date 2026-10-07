"""Audio treatments that synthesize a voice layer (AGENTIC_CREATION.md §3).

``rephrase_original`` is the new primitive: the source's own speech is
transcribed locally, rewritten by the model to fit the short's arc (message
preserved, hook first, close landed), and spoken in the HOUSE VOICE — a
deliberate, honest redub. Voice cloning is a later, gated step.

Every capability is injectable (transcriber / model client / TTS), so the
whole treatment is hermetically testable; the defaults resolve to the local
the local speech recogniser model and the same synthesizer the voiceover worker uses.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

from pydantic import BaseModel, Field

from ..Recompose.audio import synthesize_narration
from ..Recompose.domain import RecomposePlan, new_id
from .domain import HOUSE_VOICE

logger = logging.getLogger(__name__)


class TranscriptSpan(BaseModel):
    """One transcribed speech span of the source, with a 0..1 confidence."""

    start_s: float
    end_s: float
    text: str
    confidence: float = Field(ge=0.0, le=1.0)


class SourceTranscript(BaseModel):
    """The source's speech content: spans plus convenience aggregates."""

    spans: list[TranscriptSpan] = Field(default_factory=list)
    language: Optional[str] = None

    @property
    def full_text(self) -> str:
        """All span texts joined in order."""

        return " ".join(s.text.strip() for s in self.spans).strip()

    @property
    def word_count(self) -> int:
        return len(self.full_text.split())

    @property
    def mean_confidence(self) -> float:
        """Duration-weighted mean confidence (0 when there is no speech)."""

        total = sum(max(s.end_s - s.start_s, 1e-6) for s in self.spans)
        if not self.spans or total <= 0:
            return 0.0
        return sum(s.confidence * max(s.end_s - s.start_s, 1e-6)
                   for s in self.spans) / total


class Transcriber(Protocol):
    """Anything that can turn a media file into a :class:`SourceTranscript`."""

    def transcribe(self, path: Path) -> SourceTranscript: ...


class SpeechRecognitionTranscriber:
    """Local ASR via the local speech recogniser (no network, no spend).

    The model loads lazily on first use and is cached per instance; per-span
    confidence is ``exp(avg_logprob)`` discounted by the no-speech probability,
    which tracks the "content-level accuracy" the rephrase gate needs (word
    timing precision is NOT required here — the lyric-work lesson).
    """

    def __init__(self, model_size: str = "base", device: str = "cpu",
                 compute_type: str = "int8") -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._model: Any = None
        self._cache: dict[str, SourceTranscript] = {}

    def transcribe(self, path: Path) -> SourceTranscript:
        """Transcribe ``path`` (cached per file path for the instance's life)."""

        key = str(path)
        if key in self._cache:
            return self._cache[key]
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_size, device=self.device,
                                       compute_type=self.compute_type)
        segments, info = self._model.transcribe(key, vad_filter=True)
        spans = [
            TranscriptSpan(
                start_s=float(seg.start), end_s=float(seg.end),
                text=seg.text.strip(),
                confidence=max(0.0, min(1.0, math.exp(float(seg.avg_logprob))
                                        * (1.0 - float(seg.no_speech_prob)))))
            for seg in segments if seg.text.strip()
        ]
        transcript = SourceTranscript(
            spans=spans, language=getattr(info, "language", None))
        self._cache[key] = transcript
        logger.info("transcribed %s: %d spans, %d words, conf %.2f",
                    Path(key).name, len(spans), transcript.word_count,
                    transcript.mean_confidence)
        return transcript


REPHRASE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"script": {"type": "string"}},
    "required": ["script"],
    "additionalProperties": False,
}

REPHRASE_SYSTEM = """You rewrite a video's ORIGINAL spoken content into a short
voice-over for a fast cut of that same footage. You get the source transcript,
the cut's creative direction, and a duration. Rules: preserve the original
message and any factual claims — rephrase, never invent; open with the
strongest hook from the material; land the final sentence before the end;
roughly 2.2 words per second of duration, never more; plain speakable prose,
no headings or stage directions. Respond with JSON only."""


@dataclass(frozen=True)
class RephraseResult:
    """A synthesized rephrase: the script, its audio file, and provenance."""

    script: str
    audio_path: Path
    voice: str
    source_confidence: float
    source_words: int


@dataclass
class RephraseOriginalTreatment:
    """transcribe → rewrite for the arc → house-voice TTS.

    ``synthesize`` returns ``None`` when the ASR gate fails (too little or too
    unreliable speech) — the caller falls back to ``new_narration`` and says
    so, rather than redubbing garbage. All three capabilities are injectable.

    Attributes:
        llm_client: ``complete_messages`` contract (json_schema supported).
        transcriber: any :class:`Transcriber`; defaults to local speech_recognition.
        tts_fn: optional TTS override passed through to
            :func:`synthesize_narration` (tests inject a stub).
        min_confidence: duration-weighted ASR confidence gate.
        min_words: minimum source words worth rephrasing.
    """

    llm_client: Any
    transcriber: Transcriber = field(default_factory=SpeechRecognitionTranscriber)
    tts_fn: Optional[Any] = None
    voice: str = HOUSE_VOICE
    min_confidence: float = 0.45
    min_words: int = 8

    async def synthesize(self, *, voice_source_path: Path, plan: RecomposePlan,
                         duration_s: float, workdir: Path,
                         ) -> Optional[RephraseResult]:
        """Produce the rephrased narration audio for one short, or ``None``.

        Args:
            voice_source_path: the media file whose speech carries the message.
            plan: the planned cut (its hypothesis/passages steer the rewrite).
            duration_s: the short's duration — sets the word budget.
            workdir: where the narration wav lands.

        Returns:
            A :class:`RephraseResult`, or ``None`` when the gate refuses
            (callers must fall back to new narration and say so).
        """

        transcript = self.transcriber.transcribe(Path(voice_source_path))
        if (transcript.word_count < self.min_words
                or transcript.mean_confidence < self.min_confidence):
            logger.info(
                "rephrase gate refused: words=%d conf=%.2f (need ≥%d @ ≥%.2f)",
                transcript.word_count, transcript.mean_confidence,
                self.min_words, self.min_confidence)
            return None

        script = await self._rewrite(transcript, plan, duration_s)
        audio = workdir / f"rephrase_{new_id('vo')}.wav"
        await synthesize_narration(script, audio, voice=self.voice,
                                   tone="confident, warm, broadcast-clean",
                                   tts_fn=self.tts_fn)
        return RephraseResult(script=script, audio_path=audio, voice=self.voice,
                              source_confidence=round(transcript.mean_confidence, 3),
                              source_words=transcript.word_count)

    async def _rewrite(self, transcript: SourceTranscript, plan: RecomposePlan,
                       duration_s: float) -> str:
        """One bounded model call: source transcript → arc-fitted script."""

        import json as _json

        word_budget = int(max(duration_s - 1.2, 3.0) * 2.2)
        payload = {
            "duration_s": round(duration_s, 1),
            "word_budget": word_budget,
            "direction": plan.hypothesis,
            "passages": [{"focus": p.semantic_focus, "intent": p.arc_note}
                         for p in plan.passages if p.slot_indices],
            "source_transcript": transcript.full_text[:4000],
        }
        decision, _usage = await self.llm_client.complete_messages(
            [{"role": "system", "content": REPHRASE_SYSTEM},
             {"role": "user", "content": _json.dumps(payload, ensure_ascii=False)}],
            json_schema={"name": "rephrase_script", "strict": True,
                         "schema": REPHRASE_SCHEMA},
            max_tokens=700,
        )
        script = str(decision.get("script") or "").strip()
        if not script:
            raise ValueError("rephrase model returned an empty script")
        words = script.split()
        if len(words) > int(word_budget * 1.3):  # keep TTS inside the window
            script = " ".join(words[: int(word_budget * 1.2)]).rstrip(",;:") + "."
        return script
