"""
Annotation event emitted after Stage 3.1 (MusicPromptOrchestration).

Records the exact prompt(s) forwarded to the music generation provider.
These are the highest-fidelity proxy for the intended musical output and are the
primary input to the offline LLM enrichment step described in the recommendation
design document (§3.5 Offline Enrichment Pipeline).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from EdennCode.Annotation.core.annotation_event import AnnotationEvent


@dataclass(kw_only=True)
class MusicPromptEvent(AnnotationEvent):
    """
    Music generation prompts produced by the orchestration stage.

    The prompt structure varies by model:

    * **EDENN_BASIC** — a single ``prompt`` string in ProviderA format.
    * **EDENN_ENHANCED** — dual prompts: ``style_prompt`` (musical style
      description) and ``lyrics_prompt`` (lyric content guidance).
    * **EDENN_STUDIO** — same dual-prompt layout as EDENN_ENHANCED; ProviderC
      generates the actual lyrics from ``lyrics_prompt``.

    The raw ``prompt_dict`` field preserves the full dict so that schema
    changes in the orchestration stage do not require this event to be updated.

    Attributes
    ----------
    event_type:
        Always ``"music_prompt"``.  Do not change.
    model_spec:
        The normalised model specifier forwarded to the generation stage
        (``"edenn_basic"``, ``"edenn_enhanced"``, or ``"edenn_studio"``).
    style_prompt:
        Style/genre/vibe description forwarded to the provider.  ``None`` for
        EDENN_BASIC which uses a single combined prompt.
    lyrics_prompt:
        Lyric content guidance forwarded to the provider.  ``None`` for
        EDENN_BASIC and for instrumental requests.
    combined_prompt:
        Single-prompt string for EDENN_BASIC.  ``None`` for dual-prompt models.
    include_vocals:
        Whether this prompt targets a vocal generation path.
    vocal_gender:
        Gender hint included in the prompt (``"male"``, ``"female"``, or ``""``).
    generation_language:
        Effective language for lyrics generation (e.g. ``"EN"``,
        ``"CHINESE_MAINLAND"``).
    stage_latency_s:
        Wall-clock seconds the orchestration stage took.
    token_usage:
        LLM token counts for this stage.
    prompt_dict:
        The raw prompt dictionary as returned by the orchestration stage.  Stored
        for full-fidelity replay and audit.
    """

    event_type: str = "music_prompt"
    model_spec: str = ""
    style_prompt: Optional[str] = None
    lyrics_prompt: Optional[str] = None
    combined_prompt: Optional[str] = None
    include_vocals: bool = False
    vocal_gender: str = ""
    generation_language: str = ""
    stage_latency_s: float = 0.0
    token_usage: Dict[str, int] = field(default_factory=dict)
    prompt_dict: Dict[str, Any] = field(default_factory=dict)
