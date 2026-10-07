"""
EnrichmentResult enum — outcome codes from :class:`EnrichmentProcessor.process_job`.
"""
from __future__ import annotations

from enum import Enum


class EnrichmentResult(str, Enum):
    """
    Outcome of a single :meth:`~EnrichmentProcessor.process_job` call.

    Values
    ------
    ENRICHED:
        LLM extraction succeeded and a new enrichment event was written.
    SKIPPED_ALREADY_ENRICHED:
        A valid enrichment event already existed in the store for this job and
        prompt version; no LLM call was made.
    SKIPPED_NO_TEXT:
        The job had no usable text slots (e.g. purely instrumental with no
        prompt); no LLM call was made.
    FAILED:
        The LLM extraction raised an exception; a failed enrichment event was
        still written to prevent infinite retries.
    """

    ENRICHED = "enriched"
    SKIPPED_ALREADY_ENRICHED = "skipped_already_enriched"
    SKIPPED_NO_TEXT = "skipped_no_text"
    FAILED = "failed"
