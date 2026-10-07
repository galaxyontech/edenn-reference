"""
EnrichmentProcessor — orchestrates fire-and-forget taxonomy enrichment.

Two enrichment passes are available per job:

* :meth:`process_job` — music taxonomy (mood, genre, instruments, lyrics themes)
  via :class:`~EdennCode.Annotation.enrichment.taxonomy_extractor.TaxonomyExtractor`.
* :meth:`process_visual_taxonomy` — visual taxonomy (pacing, platform, setting,
  lighting, color mood, content type, etc.) via
  :class:`~EdennCode.Annotation.enrichment.visual_taxonomy_extractor.VisualTaxonomyExtractor`.

Both passes are idempotent: they skip the job if an up-to-date enrichment event
already exists in the store.

Usage pattern (fire-and-forget from API layer)::

    loop.create_task(processor.process_job(job_id))
    loop.create_task(processor.process_visual_taxonomy(job_id))

Catch-up for all pending jobs::

    await processor.process_all_pending(limit=500)
    await processor.process_all_visual_pending(limit=500)
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from EdennCode.Annotation.core.annotation_store import AnnotationStore
from EdennCode.Annotation.enrichment.enrichment_processor_result import EnrichmentResult
from EdennCode.Annotation.enrichment.music_audio_feature_extractor import MusicAudioFeatureExtractor
from EdennCode.Annotation.enrichment.music_audio_features_event import MusicAudioFeaturesEvent
from EdennCode.Annotation.enrichment.taxonomy_enrichment_event import TaxonomyEnrichmentEvent
from EdennCode.Annotation.enrichment.taxonomy_extraction_input import TaxonomyExtractionInput
from EdennCode.Annotation.enrichment.taxonomy_extractor import TaxonomyExtractor
from EdennCode.Annotation.enrichment.visual_taxonomy_enrichment_event import VisualTaxonomyEnrichmentEvent
from EdennCode.Annotation.enrichment.visual_taxonomy_extractor import VisualTaxonomyExtractor
from EdennCode.Annotation.enrichment.visual_taxonomy_schema import ExtractedVisualTaxonomy
from EdennCode.Annotation.enrichment.taxonomy_schema import ExtractedTaxonomy

logger = logging.getLogger(__name__)


class EnrichmentProcessor:
    """
    Idempotent orchestrator for both music and visual taxonomy enrichment.

    Parameters
    ----------
    store:
        The :class:`~EdennCode.Annotation.core.annotation_store.AnnotationStore`
        from which events are read and to which enrichment events are written.
    extractor:
        Configured :class:`~EdennCode.Annotation.enrichment.taxonomy_extractor.TaxonomyExtractor`
        for music taxonomy.
    visual_extractor:
        Optional :class:`~EdennCode.Annotation.enrichment.visual_taxonomy_extractor.VisualTaxonomyExtractor`
        for visual taxonomy.  If ``None``, :meth:`process_visual_taxonomy` will
        only compute deterministic features.
    extraction_prompt_version:
        Version tag for the current music taxonomy prompt.
    visual_prompt_version:
        Version tag for the current visual taxonomy prompt.
    """

    def __init__(
        self,
        store: AnnotationStore,
        extractor: TaxonomyExtractor,
        *,
        visual_extractor: Optional[VisualTaxonomyExtractor] = None,
        audio_extractor: Optional[MusicAudioFeatureExtractor] = None,
        extraction_prompt_version: str = "v1",
        visual_prompt_version: str = "v1",
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._visual_extractor = visual_extractor
        self._audio_extractor = audio_extractor or MusicAudioFeatureExtractor()
        self._prompt_version = extraction_prompt_version
        self._visual_prompt_version = visual_prompt_version

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process_job(self, job_id: str) -> EnrichmentResult:
        """
        Enrich a single pipeline job identified by *job_id*.

        The method is idempotent: if a :class:`~EdennCode.Annotation.enrichment.taxonomy_enrichment_event.TaxonomyEnrichmentEvent`
        already exists in the store for *job_id* with a matching
        ``extraction_prompt_version``, the call returns immediately with
        ``EnrichmentResult.SKIPPED_ALREADY_ENRICHED``.

        Parameters
        ----------
        job_id:
            The pipeline-run identifier.

        Returns
        -------
        EnrichmentResult
            Enum indicating what happened:
            ``ENRICHED``, ``SKIPPED_ALREADY_ENRICHED``, ``SKIPPED_NO_TEXT``,
            or ``FAILED``.
        """
        # Idempotency check
        existing = await self._store.get_by_job_and_type(job_id, "taxonomy_enrichment")
        for ev in existing:
            stored_version = getattr(ev, "extraction_prompt_version", None)
            if stored_version == self._prompt_version:
                logger.debug(
                    "EnrichmentProcessor: skipping job_id=%s — already enriched "
                    "with prompt version %s",
                    job_id,
                    self._prompt_version,
                )
                return EnrichmentResult.SKIPPED_ALREADY_ENRICHED

        # Build extraction input from all events for this job
        events = await self._store.get_by_job(job_id)
        extraction_input = TaxonomyExtractionInput.from_job_events(
            events,
            extraction_prompt_version=self._prompt_version,
        )

        if not extraction_input.has_usable_text:
            logger.debug(
                "EnrichmentProcessor: skipping job_id=%s — no usable text slots",
                job_id,
            )
            return EnrichmentResult.SKIPPED_NO_TEXT

        # Call the LLM extractor
        taxonomy: Optional[ExtractedTaxonomy] = None
        token_usage = {}
        failed = False
        error_message: Optional[str] = None
        t0 = time.perf_counter()

        try:
            taxonomy, token_usage = await self._extractor.extract(extraction_input)
        except Exception as exc:  # noqa: BLE001
            failed = True
            error_message = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "EnrichmentProcessor: extraction failed for job_id=%s error=%s",
                job_id,
                error_message,
            )

        elapsed = time.perf_counter() - t0

        ctx = extraction_input.context
        enrichment_event = TaxonomyEnrichmentEvent(
            job_id=job_id,
            source_event_ids=extraction_input.source_event_ids,
            slot_types_processed=extraction_input.slot_types,
            taxonomy=taxonomy if taxonomy is not None else ExtractedTaxonomy(),
            extraction_prompt_version=self._prompt_version,
            extraction_model=self._extractor.model_name,
            extraction_latency_s=elapsed,
            token_usage=token_usage,
            provider_name=ctx.get("provider_name", ""),
            model_spec=ctx.get("model_spec", ""),
            music_filename=ctx.get("music_filename", ""),
            failed=failed,
            error_message=error_message,
        )

        await self._store.write(enrichment_event)
        logger.info(
            "EnrichmentProcessor: wrote taxonomy_enrichment for job_id=%s "
            "failed=%s latency=%.2fs",
            job_id,
            failed,
            elapsed,
        )

        return EnrichmentResult.FAILED if failed else EnrichmentResult.ENRICHED

    async def process_all_pending(self, limit: int = 100) -> List[EnrichmentResult]:
        """
        Enrich all jobs in the store that do not yet have a current-version enrichment.

        This is the catch-up entry point for:

        * Jobs that were emitted before the processor was wired up.
        * Jobs that need re-enrichment after a prompt version bump.

        Parameters
        ----------
        limit:
            Maximum number of distinct ``job_id`` values to process in a
            single call.  Prevents runaway processing on a large store.

        Returns
        -------
        List[EnrichmentResult]
            One result per processed ``job_id``, in the order they were
            encountered.
        """
        all_events = await self._store.all_events()

        # Collect unique job IDs preserving insertion order
        seen: set[str] = set()
        job_ids: List[str] = []
        for ev in all_events:
            if ev.event_type != "taxonomy_enrichment" and ev.job_id not in seen:
                seen.add(ev.job_id)
                job_ids.append(ev.job_id)
                if len(job_ids) >= limit:
                    break

        logger.info(
            "EnrichmentProcessor.process_all_pending: found %d candidate jobs "
            "(limit=%d)",
            len(job_ids),
            limit,
        )

        results: List[EnrichmentResult] = []
        for job_id in job_ids:
            result = await self.process_job(job_id)
            results.append(result)

        return results

    async def process_audio_features(
        self,
        job_id: str,
        file_path: str,
        *,
        provider_name: str = "",
        model_spec: str = "",
    ) -> EnrichmentResult:
        """
        Extract librosa audio features for *file_path* and persist a
        :class:`~EdennCode.Annotation.enrichment.music_audio_features_event.MusicAudioFeaturesEvent`.

        Idempotent: if a ``music_audio_features`` event already exists for
        *job_id*, the call returns ``EnrichmentResult.SKIPPED_ALREADY_ENRICHED``
        without re-running librosa.

        Parameters
        ----------
        job_id:
            The pipeline-run identifier.
        file_path:
            Absolute path to the music file to analyse.
        provider_name:
            Music generation provider (e.g. ``"provider_b"``, ``"provider_a"``).
        model_spec:
            Edenn model tier (e.g. ``"edenn_basic"``).

        Returns
        -------
        EnrichmentResult
            ``ENRICHED``, ``SKIPPED_ALREADY_ENRICHED``, or ``FAILED``.
        """
        existing = await self._store.get_by_job_and_type(job_id, "music_audio_features")
        if existing:
            logger.debug(
                "EnrichmentProcessor: skipping audio features for job_id=%s — already extracted",
                job_id,
            )
            return EnrichmentResult.SKIPPED_ALREADY_ENRICHED

        from pathlib import Path
        music_filename = Path(file_path).name
        failed = False
        error_message: Optional[str] = None
        features = None

        try:
            features = await self._audio_extractor.extract(file_path)
        except Exception as exc:  # noqa: BLE001
            failed = True
            error_message = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "EnrichmentProcessor: audio extraction failed for job_id=%s error=%s",
                job_id,
                error_message,
            )

        if features is not None:
            event = MusicAudioFeaturesEvent(
                job_id=job_id,
                music_filename=music_filename,
                complete_music_filename=file_path,
                provider_name=provider_name,
                model_spec=model_spec,
                bpm_actual=features.bpm_actual,
                rms_energy_db=features.rms_energy_db,
                spectral_brightness=features.spectral_brightness,
                acousticness=features.acousticness,
                danceability=features.danceability,
                vocal_energy_ratio=features.vocal_energy_ratio,
                duration_s=features.duration_s,
                extraction_latency_s=features.extraction_latency_s,
                failed=False,
            )
        else:
            event = MusicAudioFeaturesEvent(
                job_id=job_id,
                music_filename=music_filename,
                complete_music_filename=file_path,
                provider_name=provider_name,
                model_spec=model_spec,
                failed=True,
                error_message=error_message,
            )

        await self._store.write(event)
        logger.info(
            "EnrichmentProcessor: wrote music_audio_features for job_id=%s failed=%s",
            job_id,
            failed,
        )
        return EnrichmentResult.FAILED if failed else EnrichmentResult.ENRICHED

    async def process_visual_taxonomy(self, job_id: str) -> EnrichmentResult:
        """
        Extract visual taxonomy for *job_id* and persist a
        :class:`~EdennCode.Annotation.enrichment.visual_taxonomy_enrichment_event.VisualTaxonomyEnrichmentEvent`.

        Idempotent: skips if a ``visual_taxonomy_enrichment`` event already
        exists with a matching ``extraction_prompt_version``.

        Deterministic features (pacing, platform hint, resolution) are always
        computed.  The LLM semantic pass runs only when
        ``SceneUnderstandingEvent`` or ``VideoUnderstandingEvent`` data is
        available AND a :class:`~EdennCode.Annotation.enrichment.visual_taxonomy_extractor.VisualTaxonomyExtractor`
        was supplied at construction time.

        Parameters
        ----------
        job_id:
            The pipeline-run identifier.

        Returns
        -------
        EnrichmentResult
            ``ENRICHED``, ``SKIPPED_ALREADY_ENRICHED``, or ``FAILED``.
        """
        existing = await self._store.get_by_job_and_type(job_id, "visual_taxonomy_enrichment")
        for ev in existing:
            if getattr(ev, "extraction_prompt_version", None) == self._visual_prompt_version:
                logger.debug(
                    "EnrichmentProcessor: skipping visual taxonomy for job_id=%s "
                    "— already enriched with version %s",
                    job_id,
                    self._visual_prompt_version,
                )
                return EnrichmentResult.SKIPPED_ALREADY_ENRICHED

        events = await self._store.get_by_job(job_id)

        taxonomy: Optional[ExtractedVisualTaxonomy] = None
        token_usage: dict = {}
        deterministic_only = False
        failed = False
        error_message: Optional[str] = None
        model_name = ""
        t0 = time.perf_counter()

        try:
            if self._visual_extractor is not None:
                taxonomy, token_usage, deterministic_only = await self._visual_extractor.extract(events)
                model_name = self._visual_extractor.model_name
            else:
                # No LLM extractor — deterministic features only via a lightweight path
                from EdennCode.Annotation.enrichment.visual_taxonomy_extractor import _compute_deterministic
                computed = _compute_deterministic(events)
                taxonomy = ExtractedVisualTaxonomy(**{k: v for k, v in computed.items() if hasattr(ExtractedVisualTaxonomy, k) or True})
                for k, v in computed.items():
                    setattr(taxonomy, k, v)
                deterministic_only = True
        except Exception as exc:  # noqa: BLE001
            failed = True
            error_message = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "EnrichmentProcessor: visual taxonomy extraction failed "
                "for job_id=%s error=%s",
                job_id,
                error_message,
            )

        elapsed = time.perf_counter() - t0

        enrichment_event = VisualTaxonomyEnrichmentEvent(
            job_id=job_id,
            taxonomy=taxonomy if taxonomy is not None else ExtractedVisualTaxonomy(),
            extraction_prompt_version=self._visual_prompt_version,
            extraction_model=model_name,
            extraction_latency_s=elapsed,
            token_usage=token_usage,
            deterministic_only=deterministic_only,
            failed=failed,
            error_message=error_message,
        )

        await self._store.write(enrichment_event)
        logger.info(
            "EnrichmentProcessor: wrote visual_taxonomy_enrichment for job_id=%s "
            "deterministic_only=%s failed=%s latency=%.2fs",
            job_id,
            deterministic_only,
            failed,
            elapsed,
        )

        return EnrichmentResult.FAILED if failed else EnrichmentResult.ENRICHED

    async def process_all_visual_pending(self, limit: int = 100) -> List[EnrichmentResult]:
        """
        Run :meth:`process_visual_taxonomy` for every job that does not yet
        have a current-version ``visual_taxonomy_enrichment`` event.

        Parameters
        ----------
        limit:
            Maximum number of distinct ``job_id`` values to process.

        Returns
        -------
        List[EnrichmentResult]
            One result per processed job.
        """
        all_events = await self._store.all_events()

        seen: set[str] = set()
        job_ids: List[str] = []
        for ev in all_events:
            if ev.event_type != "visual_taxonomy_enrichment" and ev.job_id not in seen:
                seen.add(ev.job_id)
                job_ids.append(ev.job_id)
                if len(job_ids) >= limit:
                    break

        logger.info(
            "EnrichmentProcessor.process_all_visual_pending: found %d candidate jobs",
            len(job_ids),
        )

        results: List[EnrichmentResult] = []
        for job_id in job_ids:
            results.append(await self.process_visual_taxonomy(job_id))

        return results
