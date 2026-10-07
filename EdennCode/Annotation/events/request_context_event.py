"""
Annotation event emitted after Stage 0 (UserPromptPreprocessor).

Captures user intent and TOS-compliance metadata that contextualise all
downstream generation decisions for a pipeline run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from EdennCode.Annotation.core.annotation_event import AnnotationEvent


@dataclass(kw_only=True)
class RequestContextEvent(AnnotationEvent):
    """
    Snapshot of the user's inferred intent captured at the start of each run.

    This event is emitted immediately after the ``UserPromptPreprocessorAgent``
    returns, before any video or audio processing begins.  It anchors the
    user-visible creative direction to the concrete generation decisions made
    later in the pipeline (model selection, language routing, vocal path).

    Attributes
    ----------
    event_type:
        Always ``"request_context"``.  Do not change.
    original_prompt:
        The raw prompt string supplied by the caller, before sanitisation.
    sanitized_prompt:
        The English-normalised, TOS-compliant prompt forwarded to downstream
        stages.  Equal to *original_prompt* when no transformation was needed.
    was_prompt_transformed:
        ``True`` if the preprocessor detected and sanitised a real-person voice
        reference (TOS compliance rule).
    detected_references:
        List of person names that were sanitised out of the prompt.  Empty when
        *was_prompt_transformed* is ``False``.
    detected_language:
        Language constant inferred for general titles/descriptions
        (e.g. ``"CHINESE_MAINLAND"``, ``"EN"``).
    detected_vocal_language:
        Language inferred for vocals/lyrics generation.  Empty string when the
        request is for an instrumental track.
    detected_category:
        Video category inferred by the LLM (``"ADVERTISEMENT"``, ``"VLOG"``,
        ``"CREATOR_CONTENT"``, ``"VIDEO"``).
    include_vocals:
        Whether the preprocessor determined the user wants vocals/lyrics.
    vocal_gender:
        Gender inferred for vocal generation: ``"male"``, ``"female"``, or
        ``"unknown"``.
    verbose_instruction:
        ``True`` when the caller supplied structured ``music_style_prompt`` /
        ``lyrics_prompt`` fields instead of a single ``user_prompt``.
    stage_latency_s:
        Wall-clock seconds the preprocessor stage took, measured by the
        orchestrator.  ``None`` if not instrumented.
    """

    event_type: str = "request_context"
    original_prompt: str = ""
    sanitized_prompt: str = ""
    was_prompt_transformed: bool = False
    detected_references: List[str] = field(default_factory=list)
    detected_language: str = ""
    detected_vocal_language: str = ""
    detected_category: str = ""
    include_vocals: bool = False
    vocal_gender: str = ""
    verbose_instruction: bool = False
    stage_latency_s: float = 0.0
