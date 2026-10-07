"""
TaxonomyEnrichmentEvent — persisted result of one offline LLM taxonomy extraction.

Written to the annotation store by :class:`~EdennCode.Annotation.enrichment.enrichment_processor.EnrichmentProcessor`
after a successful (or failed) extraction call.  The event acts as both the
enrichment payload and the idempotency marker: before issuing an LLM call,
the processor checks whether a ``taxonomy_enrichment`` event already exists
for the job with a matching ``extraction_prompt_version``.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from EdennCode.Annotation.core.annotation_event import AnnotationEvent
from EdennCode.Annotation.enrichment.taxonomy_schema import ExtractedTaxonomy


@dataclass(kw_only=True)
class TaxonomyEnrichmentEvent(AnnotationEvent):
    """
    Structured result of one LLM-based taxonomy extraction for a pipeline job.

    This event is written exactly once per ``(job_id, extraction_prompt_version)``
    pair.  The processor skips re-extraction if an event already exists for the
    combination.

    Attributes
    ----------
    event_type:
        Always ``"taxonomy_enrichment"``.
    source_event_ids:
        Ordered ``event_id`` values of the annotation events whose text was
        consumed by the extractor (same list as
        :attr:`~EdennCode.Annotation.enrichment.taxonomy_extraction_input.TaxonomyExtractionInput.source_event_ids`).
    slot_types_processed:
        Ordered list of :class:`~EdennCode.Annotation.enrichment.text_slot.TextSlotType`
        value strings that were present in the extraction input.  Useful for
        diagnosing which signal sources contributed to the taxonomy.
    taxonomy:
        The :class:`~EdennCode.Annotation.enrichment.taxonomy_schema.ExtractedTaxonomy`
        produced by the LLM, or a zeroed-out default if ``failed`` is ``True``.
    extraction_prompt_version:
        Version tag of the prompt that was used.  Copied from the extraction
        input so the processor can detect stale enrichments when the prompt
        changes.
    extraction_model:
        Azure deployment name of the model used for extraction (e.g.
        ``"chat-advanced"``).  ``""`` if extraction failed before the LLM call.
    extraction_latency_s:
        Wall-clock seconds spent on the LLM call.  ``0.0`` if the extraction
        was skipped or failed before the call.
    token_usage:
        Token usage dict returned by the LLM client:
        ``{"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}``.
        Empty dict if extraction failed or no usage was reported.
    failed:
        ``True`` when the extraction raised an unhandled exception.  The
        processor writes the event anyway so the job is not retried infinitely;
        callers can filter on this field to find broken jobs.
    error_message:
        Human-readable description of the failure.  ``None`` when ``failed``
        is ``False``.
    """

    event_type: str = "taxonomy_enrichment"
    source_event_ids: List[str] = field(default_factory=list)
    slot_types_processed: List[str] = field(default_factory=list)
    taxonomy: ExtractedTaxonomy = field(default_factory=ExtractedTaxonomy)
    extraction_prompt_version: str = "v1"
    extraction_model: str = ""
    extraction_latency_s: float = 0.0
    token_usage: Dict[str, Any] = field(default_factory=dict)
    # Track identity — populated from MusicGenerationEvent when available
    provider_name: str = ""
    model_spec: str = ""
    music_filename: str = ""
    failed: bool = False
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """
        Return a serialisable dict, expanding the nested :class:`ExtractedTaxonomy`
        into a plain sub-dict.
        """
        d = dataclasses.asdict(self)
        # dataclasses.asdict recurses into nested dataclasses automatically,
        # so `taxonomy` becomes a plain dict already.
        return d
