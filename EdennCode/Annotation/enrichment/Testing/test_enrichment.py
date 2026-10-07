"""
Integration-level tests for the taxonomy enrichment subsystem.

All async tests use :class:`unittest.IsolatedAsyncioTestCase` (matching the
project convention in ``EdennCode/MusicGenerationCore/Testing/``).
The LLM client is replaced with a lightweight ``FakeLLMClient`` so tests run
without Azure credentials.

Coverage
--------
1.  ``TextSlot`` rejects empty text.
2.  ``TaxonomyExtractionInput.from_job_events`` slot mapping for each event type.
3.  ``TaxonomyExtractionInput.from_job_events`` deduplicates identical text.
4.  ``TaxonomyExtractionInput.from_job_events`` caps scene count and char limit.
5.  ``TaxonomyExtractionInput.from_job_events`` populates context dict.
6.  ``TaxonomyExtractionInput.from_job_events`` on empty event list.
7.  ``ExtractedTaxonomy.from_llm_dict`` applies safe defaults on missing keys.
8.  ``TaxonomyExtractor.extract`` raises on no-text input.
9.  ``TaxonomyExtractor.extract`` builds correct messages and returns taxonomy.
10. ``EnrichmentProcessor.process_job`` → ENRICHED path (happy path).
11. ``EnrichmentProcessor.process_job`` → SKIPPED_ALREADY_ENRICHED (same version).
12. ``EnrichmentProcessor.process_job`` → version mismatch triggers re-enrichment.
13. ``EnrichmentProcessor.process_job`` → SKIPPED_NO_TEXT (no text events).
14. ``EnrichmentProcessor.process_job`` → FAILED (LLM raises exception).
15. ``EnrichmentProcessor.process_all_pending`` enriches multiple jobs, skips done ones.
16. ``TaxonomyEnrichmentEvent.to_dict`` serialises nested taxonomy correctly.
"""
from __future__ import annotations

import unittest
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock

from EdennCode.Annotation.enrichment.enrichment_processor import EnrichmentProcessor
from EdennCode.Annotation.enrichment.enrichment_processor_result import EnrichmentResult
from EdennCode.Annotation.enrichment.taxonomy_enrichment_event import TaxonomyEnrichmentEvent
from EdennCode.Annotation.enrichment.taxonomy_extraction_input import (
    TaxonomyExtractionInput,
    _MAX_SCENE_COUNT,
    _MAX_SCENE_DESCRIPTION_CHARS,
)
from EdennCode.Annotation.enrichment.taxonomy_extractor import TaxonomyExtractor
from EdennCode.Annotation.enrichment.taxonomy_schema import ExtractedTaxonomy
from EdennCode.Annotation.enrichment.text_slot import TextSlot, TextSlotType
from EdennCode.Annotation.store.in_memory_store import InMemoryAnnotationStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_TAXONOMY_DICT: Dict[str, Any] = {
    "mood_tags": ["reflective", "hopeful"],
    "sentiment": "positive",
    "energy_level": 0.6,
    "genre_tags": ["pop", "ambient"],
    "instrument_tags": ["piano", "synth"],
    "tempo_class": "medium",
    "vocal_style": "melodic",
    "theme_tags": ["journey", "connection"],
    "activity_tags": ["commute"],
    "location_types": ["urban_exterior"],
    "subject_types": ["person"],
    "motion_class": "dynamic",
    "lyric_themes": ["sunrise", "new_beginning"],
    "lyric_sentiment": "positive",
}

_FAKE_TOKEN_USAGE: Dict[str, int] = {
    "prompt_tokens": 120,
    "completion_tokens": 80,
    "total_tokens": 200,
}


def _make_fake_llm_client(
    response: Dict[str, Any] = _FAKE_TAXONOMY_DICT,
    usage: Dict[str, int] = _FAKE_TOKEN_USAGE,
) -> MagicMock:
    """Return a mock AzureMultimodalClient whose complete_messages is an AsyncMock."""
    client = MagicMock()
    client.azure_model = "chat-test"
    client.complete_messages = AsyncMock(return_value=(response, usage))
    return client


def _make_music_gen_event(
    job_id: str = "job-1",
    full_lyrics_text: str = "Under the city lights",
    style_prompt: str = "upbeat pop piano",
    combined_prompt: str = "",
    lyrics_prompt: str = "Write about hope",
) -> MagicMock:
    ev = MagicMock()
    ev.event_type = "music_generation"
    ev.job_id = job_id
    ev.event_id = f"{job_id}-music-gen"
    ev.full_lyrics_text = full_lyrics_text
    ev.style_prompt = style_prompt
    ev.combined_prompt = combined_prompt
    ev.lyrics_prompt = lyrics_prompt
    ev.lyrics_language = "en"
    ev.overall_mood = "hopeful"
    ev.video_category = "urban"
    ev.video_duration_s = 30.0
    ev.include_vocals = True
    return ev


def _make_scene_understanding_event(
    job_id: str = "job-1",
    n_scenes: int = 3,
) -> MagicMock:
    ev = MagicMock()
    ev.event_type = "scene_understanding"
    ev.job_id = job_id
    ev.event_id = f"{job_id}-scene"
    scenes = []
    for i in range(n_scenes):
        sc = MagicMock()
        sc.scene_index = i
        sc.visual_summary = f"A busy street scene {i}"
        sc.key_actions = "people walking"
        sc.mood = "energetic"
        scenes.append(sc)
    ev.scenes = scenes
    return ev


def _make_video_understanding_event(job_id: str = "job-1") -> MagicMock:
    ev = MagicMock()
    ev.event_type = "video_understanding"
    ev.job_id = job_id
    ev.event_id = f"{job_id}-video-und"
    ev.video_description = "A montage of urban commuters at dawn"
    ev.overall_mood = "optimistic"
    ev.core_message = "city energy"
    return ev


def _make_music_prompt_event(job_id: str = "job-1") -> MagicMock:
    ev = MagicMock()
    ev.event_type = "music_prompt"
    ev.job_id = job_id
    ev.event_id = f"{job_id}-music-prompt"
    ev.style_prompt = None
    ev.combined_prompt = None
    ev.lyrics_prompt = None
    ev.prompt_dict = {
        "global_music_prompt": "Uplifting pop track with piano and synth",
        "tempo_bpm": 120,
        "instruments": ["piano", "synth"],
    }
    ev.generation_language = "en"
    return ev


def _make_request_context_event(job_id: str = "job-1") -> MagicMock:
    ev = MagicMock()
    ev.event_type = "request_context"
    ev.job_id = job_id
    ev.event_id = f"{job_id}-req-ctx"
    ev.detected_category = "lifestyle"
    ev.include_vocals = True
    ev.vocal_gender = "female"
    return ev


def _make_video_feature_event(job_id: str = "job-1") -> MagicMock:
    ev = MagicMock()
    ev.event_type = "video_feature"
    ev.job_id = job_id
    ev.event_id = f"{job_id}-vid-feat"
    ev.duration_s = 45.0
    return ev


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestTextSlot(unittest.TestCase):

    def test_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            TextSlot(slot_type=TextSlotType.LYRICS, text="", source_field="x")

    def test_rejects_whitespace_only(self):
        with self.assertRaises(ValueError):
            TextSlot(slot_type=TextSlotType.LYRICS, text="   ", source_field="x")

    def test_accepts_valid_text(self):
        slot = TextSlot(slot_type=TextSlotType.STYLE_PROMPT, text="pop piano", source_field="x")
        self.assertEqual(slot.text, "pop piano")
        self.assertIsNone(slot.locale)

    def test_locale_preserved(self):
        slot = TextSlot(slot_type=TextSlotType.LYRICS, text="hello", source_field="x", locale="en")
        self.assertEqual(slot.locale, "en")


class TestTaxonomyExtractionInputBuilder(unittest.TestCase):

    def test_empty_events_returns_no_slots(self):
        inp = TaxonomyExtractionInput.from_job_events([])
        self.assertEqual(inp.job_id, "")
        self.assertFalse(inp.has_usable_text)
        self.assertEqual(inp.slots, [])

    def test_music_gen_event_creates_correct_slots(self):
        ev = _make_music_gen_event()
        inp = TaxonomyExtractionInput.from_job_events([ev])
        slot_types = inp.slot_types
        self.assertIn("lyrics", slot_types)
        self.assertIn("style_prompt", slot_types)
        self.assertIn("lyrics_prompt", slot_types)

    def test_music_prompt_event_global_prompt_slot(self):
        ev = _make_music_prompt_event()
        inp = TaxonomyExtractionInput.from_job_events([ev])
        self.assertIn("global_music_prompt", inp.slot_types)

    def test_music_prompt_event_populates_context(self):
        ev = _make_music_prompt_event()
        inp = TaxonomyExtractionInput.from_job_events([ev])
        self.assertEqual(inp.context["tempo_bpm"], 120)
        self.assertEqual(inp.context["instruments"], ["piano", "synth"])

    def test_scene_understanding_creates_scene_description_slot(self):
        ev = _make_scene_understanding_event(n_scenes=2)
        inp = TaxonomyExtractionInput.from_job_events([ev])
        self.assertIn("scene_description", inp.slot_types)

    def test_scene_understanding_caps_scene_count(self):
        ev = _make_scene_understanding_event(n_scenes=_MAX_SCENE_COUNT + 5)
        inp = TaxonomyExtractionInput.from_job_events([ev])
        scene_slot = next(s for s in inp.slots if s.slot_type == TextSlotType.SCENE_DESCRIPTION)
        # Count "[Scene N]" occurrences — should be at most _MAX_SCENE_COUNT
        count = scene_slot.text.count("[Scene ")
        self.assertLessEqual(count, _MAX_SCENE_COUNT)

    def test_scene_description_truncated_to_char_limit(self):
        ev = _make_scene_understanding_event(n_scenes=3)
        # Make visual summaries very long
        for sc in ev.scenes:
            sc.visual_summary = "X" * 1000
        inp = TaxonomyExtractionInput.from_job_events([ev])
        scene_slot = next(s for s in inp.slots if s.slot_type == TextSlotType.SCENE_DESCRIPTION)
        self.assertLessEqual(len(scene_slot.text), _MAX_SCENE_DESCRIPTION_CHARS)

    def test_video_understanding_creates_video_description_slot(self):
        ev = _make_video_understanding_event()
        inp = TaxonomyExtractionInput.from_job_events([ev])
        self.assertIn("video_description", inp.slot_types)

    def test_video_understanding_populates_context(self):
        ev = _make_video_understanding_event()
        inp = TaxonomyExtractionInput.from_job_events([ev])
        self.assertEqual(inp.context["overall_mood"], "optimistic")
        self.assertEqual(inp.context["core_message"], "city energy")

    def test_request_context_event_populates_context(self):
        ev = _make_request_context_event()
        inp = TaxonomyExtractionInput.from_job_events([ev])
        self.assertEqual(inp.context["video_category"], "lifestyle")
        self.assertTrue(inp.context["include_vocals"])
        self.assertEqual(inp.context["vocal_gender"], "female")

    def test_video_feature_event_populates_duration(self):
        ev = _make_video_feature_event()
        inp = TaxonomyExtractionInput.from_job_events([ev])
        self.assertEqual(inp.context["video_duration_s"], 45.0)

    def test_deduplication_same_text_different_events(self):
        ev1 = _make_music_gen_event(job_id="job-dedup", style_prompt="lo-fi beats")
        ev2 = _make_music_gen_event(job_id="job-dedup", style_prompt="lo-fi beats")
        ev2.event_id = "job-dedup-music-gen-2"
        inp = TaxonomyExtractionInput.from_job_events([ev1, ev2])
        style_slots = [s for s in inp.slots if s.slot_type == TextSlotType.STYLE_PROMPT]
        texts = [s.text for s in style_slots]
        self.assertEqual(len(texts), len(set(texts)), "Duplicate text should be deduplicated")

    def test_unknown_event_type_silently_skipped(self):
        ev = MagicMock()
        ev.event_type = "totally_unknown_type"
        ev.job_id = "job-unknown"
        ev.event_id = "ev-unknown"
        inp = TaxonomyExtractionInput.from_job_events([ev])
        self.assertFalse(inp.has_usable_text)

    def test_source_event_ids_recorded(self):
        ev = _make_music_gen_event(job_id="job-src")
        ev.event_id = "explicit-ev-id"
        inp = TaxonomyExtractionInput.from_job_events([ev])
        self.assertIn("explicit-ev-id", inp.source_event_ids)


class TestExtractedTaxonomySafeDefaults(unittest.TestCase):

    def test_from_llm_dict_full(self):
        taxonomy = ExtractedTaxonomy.from_llm_dict(_FAKE_TAXONOMY_DICT)
        self.assertEqual(taxonomy.mood_tags, ["reflective", "hopeful"])
        self.assertEqual(taxonomy.sentiment, "positive")
        self.assertAlmostEqual(taxonomy.energy_level, 0.6)
        self.assertEqual(taxonomy.genre_tags, ["pop", "ambient"])
        self.assertEqual(taxonomy.tempo_class, "medium")
        self.assertEqual(taxonomy.vocal_style, "melodic")
        self.assertEqual(taxonomy.lyric_sentiment, "positive")

    def test_from_llm_dict_empty_applies_defaults(self):
        taxonomy = ExtractedTaxonomy.from_llm_dict({})
        self.assertEqual(taxonomy.mood_tags, [])
        self.assertEqual(taxonomy.sentiment, "neutral")
        self.assertAlmostEqual(taxonomy.energy_level, 0.5)
        self.assertEqual(taxonomy.tempo_class, "unknown")
        self.assertEqual(taxonomy.lyric_sentiment, "none")

    def test_from_llm_dict_bad_energy_level_falls_back(self):
        taxonomy = ExtractedTaxonomy.from_llm_dict({"energy_level": "not-a-float"})
        self.assertAlmostEqual(taxonomy.energy_level, 0.5)

    def test_from_llm_dict_non_list_tags_returns_empty(self):
        taxonomy = ExtractedTaxonomy.from_llm_dict({"mood_tags": "single-string"})
        self.assertEqual(taxonomy.mood_tags, [])


class TestTaxonomyExtractor(unittest.IsolatedAsyncioTestCase):

    async def test_raises_on_no_usable_text(self):
        client = _make_fake_llm_client()
        extractor = TaxonomyExtractor(client)
        empty_input = TaxonomyExtractionInput(job_id="job-empty", slots=[], source_event_ids=[])
        with self.assertRaises(ValueError):
            await extractor.extract(empty_input)

    async def test_happy_path_returns_taxonomy_and_usage(self):
        client = _make_fake_llm_client()
        extractor = TaxonomyExtractor(client)
        ev = _make_music_gen_event(job_id="job-ext")
        extraction_input = TaxonomyExtractionInput.from_job_events([ev])
        taxonomy, usage = await extractor.extract(extraction_input)
        self.assertIsInstance(taxonomy, ExtractedTaxonomy)
        self.assertEqual(taxonomy.mood_tags, ["reflective", "hopeful"])
        self.assertEqual(usage["total_tokens"], 200)

    async def test_messages_passed_to_llm_client(self):
        client = _make_fake_llm_client()
        extractor = TaxonomyExtractor(client)
        ev = _make_music_gen_event(job_id="job-msg")
        extraction_input = TaxonomyExtractionInput.from_job_events([ev])
        await extractor.extract(extraction_input)
        call_args = client.complete_messages.call_args
        messages = call_args[0][0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        # User message should contain the lyrics text
        self.assertIn("Under the city lights", messages[1]["content"])

    async def test_model_name_property(self):
        client = _make_fake_llm_client()
        extractor = TaxonomyExtractor(client)
        self.assertEqual(extractor.model_name, "chat-test")


class TestEnrichmentProcessor(unittest.IsolatedAsyncioTestCase):

    def _make_processor(self, version: str = "v1") -> tuple:
        store = InMemoryAnnotationStore()
        client = _make_fake_llm_client()
        extractor = TaxonomyExtractor(client)
        processor = EnrichmentProcessor(store, extractor, extraction_prompt_version=version)
        return store, client, processor

    async def _seed_job(self, store: InMemoryAnnotationStore, job_id: str) -> None:
        """Write a music_gen event so the job has usable text."""
        from EdennCode.Annotation.events.music_generation_event import MusicGenerationEvent
        ev = MusicGenerationEvent(
            job_id=job_id,
            model_spec="edenn_basic",
            provider_name="provider_a",
            full_lyrics_text="Walking in the city rain",
            style_prompt="acoustic indie guitar",
        )
        await store.write(ev)

    async def test_happy_path_enriched(self):
        store, client, processor = self._make_processor()
        await self._seed_job(store, "job-happy")
        result = await processor.process_job("job-happy")
        self.assertEqual(result, EnrichmentResult.ENRICHED)
        enrichment_events = await store.get_by_job_and_type("job-happy", "taxonomy_enrichment")
        self.assertEqual(len(enrichment_events), 1)
        ev = enrichment_events[0]
        self.assertFalse(ev.failed)
        self.assertEqual(ev.extraction_model, "chat-test")
        self.assertEqual(ev.token_usage["total_tokens"], 200)

    async def test_skipped_already_enriched_same_version(self):
        store, client, processor = self._make_processor(version="v1")
        await self._seed_job(store, "job-idem")
        # First run enriches
        r1 = await processor.process_job("job-idem")
        self.assertEqual(r1, EnrichmentResult.ENRICHED)
        # Second run should skip
        r2 = await processor.process_job("job-idem")
        self.assertEqual(r2, EnrichmentResult.SKIPPED_ALREADY_ENRICHED)
        # LLM should have been called exactly once
        self.assertEqual(client.complete_messages.call_count, 1)

    async def test_version_mismatch_triggers_reenrichment(self):
        store, client, proc_v1 = self._make_processor(version="v1")
        await self._seed_job(store, "job-version")
        await proc_v1.process_job("job-version")

        # Upgrade prompt version
        proc_v2 = EnrichmentProcessor(store, proc_v1._extractor, extraction_prompt_version="v2")
        result = await proc_v2.process_job("job-version")
        self.assertEqual(result, EnrichmentResult.ENRICHED)
        self.assertEqual(client.complete_messages.call_count, 2)

    async def test_skipped_no_text(self):
        store, client, processor = self._make_processor()
        # Write an event with no text fields (e.g. video_feature only)
        ev = _make_video_feature_event("job-notext")
        from EdennCode.Annotation.core.annotation_event import AnnotationEvent
        # Simulate a bare annotation event with no text
        plain_ev = MagicMock(spec=AnnotationEvent)
        plain_ev.event_type = "video_feature"
        plain_ev.job_id = "job-notext"
        plain_ev.event_id = "vid-feat-notext"
        plain_ev.duration_s = 30.0
        await store.write(plain_ev)

        result = await processor.process_job("job-notext")
        self.assertEqual(result, EnrichmentResult.SKIPPED_NO_TEXT)
        client.complete_messages.assert_not_called()

    async def test_failed_result_on_llm_exception(self):
        store, client, processor = self._make_processor()
        await self._seed_job(store, "job-fail")
        client.complete_messages.side_effect = RuntimeError("Azure 500")
        result = await processor.process_job("job-fail")
        self.assertEqual(result, EnrichmentResult.FAILED)
        # A failed enrichment event should still be written
        enrichment_events = await store.get_by_job_and_type("job-fail", "taxonomy_enrichment")
        self.assertEqual(len(enrichment_events), 1)
        self.assertTrue(enrichment_events[0].failed)
        self.assertIn("Azure 500", enrichment_events[0].error_message)

    async def test_process_all_pending_enriches_multiple_jobs(self):
        store, client, processor = self._make_processor()
        for i in range(3):
            await self._seed_job(store, f"job-batch-{i}")
        results = await processor.process_all_pending(limit=10)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r == EnrichmentResult.ENRICHED for r in results))
        self.assertEqual(client.complete_messages.call_count, 3)

    async def test_process_all_pending_skips_already_enriched(self):
        store, client, processor = self._make_processor()
        await self._seed_job(store, "job-pre")
        await self._seed_job(store, "job-new")
        # Pre-enrich job-pre
        await processor.process_job("job-pre")
        # Now process all — job-pre should be skipped
        results = await processor.process_all_pending(limit=10)
        result_counts = {r: results.count(r) for r in set(results)}
        self.assertEqual(result_counts.get(EnrichmentResult.SKIPPED_ALREADY_ENRICHED, 0), 1)
        self.assertEqual(result_counts.get(EnrichmentResult.ENRICHED, 0), 1)

    async def test_process_all_pending_respects_limit(self):
        store, client, processor = self._make_processor()
        for i in range(10):
            await self._seed_job(store, f"job-limit-{i}")
        results = await processor.process_all_pending(limit=4)
        self.assertEqual(len(results), 4)


class TestTaxonomyEnrichmentEventSerialization(unittest.TestCase):

    def test_to_dict_contains_taxonomy_subdict(self):
        taxonomy = ExtractedTaxonomy.from_llm_dict(_FAKE_TAXONOMY_DICT)
        ev = TaxonomyEnrichmentEvent(
            job_id="job-serial",
            source_event_ids=["ev-1"],
            slot_types_processed=["lyrics", "style_prompt"],
            taxonomy=taxonomy,
            extraction_model="chat-advanced",
            extraction_latency_s=1.23,
            token_usage=_FAKE_TOKEN_USAGE,
        )
        d = ev.to_dict()
        self.assertIn("taxonomy", d)
        self.assertIsInstance(d["taxonomy"], dict)
        self.assertEqual(d["taxonomy"]["mood_tags"], ["reflective", "hopeful"])
        self.assertEqual(d["extraction_model"], "chat-advanced")
        self.assertFalse(d["failed"])

    def test_failed_event_serialises_correctly(self):
        ev = TaxonomyEnrichmentEvent(
            job_id="job-fail-serial",
            failed=True,
            error_message="timeout",
        )
        d = ev.to_dict()
        self.assertTrue(d["failed"])
        self.assertEqual(d["error_message"], "timeout")


if __name__ == "__main__":
    unittest.main()
