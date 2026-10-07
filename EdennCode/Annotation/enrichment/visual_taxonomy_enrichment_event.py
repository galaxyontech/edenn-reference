"""
VisualTaxonomyEnrichmentEvent — persisted result of one visual taxonomy extraction.

Written by :class:`~EdennCode.Annotation.enrichment.enrichment_processor.EnrichmentProcessor`
after ``process_visual_taxonomy()`` completes.  Acts as the idempotency marker
for the visual enrichment pass (separate from the music taxonomy pass).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from EdennCode.Annotation.core.annotation_event import AnnotationEvent
from EdennCode.Annotation.enrichment.visual_taxonomy_schema import ExtractedVisualTaxonomy


@dataclass(kw_only=True)
class VisualTaxonomyEnrichmentEvent(AnnotationEvent):
    """
    Structured visual taxonomy result for one pipeline job.

    Attributes
    ----------
    event_type:
        Always ``"visual_taxonomy_enrichment"``.
    taxonomy:
        The extracted :class:`~EdennCode.Annotation.enrichment.visual_taxonomy_schema.ExtractedVisualTaxonomy`.
    extraction_prompt_version:
        Prompt version used — idempotency key alongside ``job_id``.
    extraction_model:
        Azure deployment name used for the semantic LLM call.
        Empty string when only deterministic features were computed.
    extraction_latency_s:
        Wall-clock seconds for the LLM call.  0.0 if LLM was not called.
    token_usage:
        Token usage dict from the LLM client.  Empty when LLM was not called.
    deterministic_only:
        ``True`` when no scene/video descriptions were available and only
        computed features (pacing, platform, resolution) were populated.
    failed:
        ``True`` when the LLM call raised an unhandled exception.
    error_message:
        Description of the failure when ``failed`` is ``True``.
    """

    event_type: str = "visual_taxonomy_enrichment"
    taxonomy: ExtractedVisualTaxonomy = field(default_factory=ExtractedVisualTaxonomy)
    extraction_prompt_version: str = "v1"
    extraction_model: str = ""
    extraction_latency_s: float = 0.0
    token_usage: Dict[str, Any] = field(default_factory=dict)
    deterministic_only: bool = False
    failed: bool = False
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)
