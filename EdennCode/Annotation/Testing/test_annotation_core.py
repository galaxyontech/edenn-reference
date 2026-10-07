"""
Integration tests for the annotation system core layer.

Covers:
* AnnotationEvent construction and identity guarantees
* InMemoryAnnotationStore write / query / get_by_job / get_by_job_and_type / clear
* AnnotationDispatcher fan-out, non-blocking semantics, and error isolation
* Every typed event dataclass (field defaults, factory methods, helpers)
* End-to-end: dispatcher → store → retrieval in a running event loop

All tests are self-contained and require no external dependencies.
Async tests use unittest.IsolatedAsyncioTestCase, matching the project convention.
"""
from __future__ import annotations

import asyncio
import time
import unittest
import uuid
from unittest.mock import AsyncMock

from EdennCode.Annotation.core.annotation_dispatcher import (
    AnnotationDispatcher,
    safe_emit_annotation,
)
from EdennCode.Annotation.core.annotation_event import AnnotationEvent
from EdennCode.Annotation.core.annotation_store import AnnotationStore
from EdennCode.Annotation.events.music_generation_event import MusicGenerationEvent
from EdennCode.Annotation.events.music_prompt_event import MusicPromptEvent
from EdennCode.Annotation.events.remix_completion_event import RemixCompletionEvent
from EdennCode.Annotation.events.request_context_event import RequestContextEvent
from EdennCode.Annotation.events.scene_understanding_event import (
    SceneAnnotation,
    SceneUnderstandingEvent,
)
from EdennCode.Annotation.events.video_feature_event import VideoFeatureEvent
from EdennCode.Annotation.events.video_understanding_event import VideoUnderstandingEvent
from EdennCode.Annotation.store.in_memory_store import InMemoryAnnotationStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job_id() -> str:
    return str(uuid.uuid4())


def _make_event(event_type: str = "test_event", job_id: str | None = None) -> AnnotationEvent:
    return AnnotationEvent(
        event_type=event_type,
        job_id=job_id or _make_job_id(),
    )


async def _drain() -> None:
    """Yield control to the event loop so background tasks complete."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)  # Two yields handle nested task scheduling


# ---------------------------------------------------------------------------
# AnnotationEvent (synchronous — plain unittest.TestCase)
# ---------------------------------------------------------------------------

class TestAnnotationEvent(unittest.TestCase):

    def test_unique_event_ids(self) -> None:
        e1 = _make_event()
        e2 = _make_event()
        self.assertNotEqual(e1.event_id, e2.event_id)

    def test_timestamp_is_recent(self) -> None:
        before = time.time()
        event = _make_event()
        after = time.time()
        self.assertGreaterEqual(event.timestamp_utc, before)
        self.assertLessEqual(event.timestamp_utc, after)

    def test_schema_version_default(self) -> None:
        self.assertEqual(_make_event().schema_version, "v1")

    def test_metadata_defaults_to_empty_dict(self) -> None:
        event = _make_event()
        self.assertEqual(event.metadata, {})
        # Mutable default must not be shared between instances
        event.metadata["key"] = "value"
        self.assertEqual(_make_event().metadata, {})

    def test_to_dict_returns_plain_dict(self) -> None:
        event = _make_event(event_type="smoke", job_id="job-1")
        d = event.to_dict()
        self.assertEqual(d["event_type"], "smoke")
        self.assertEqual(d["job_id"], "job-1")
        self.assertIn("event_id", d)
        self.assertIn("timestamp_utc", d)


# ---------------------------------------------------------------------------
# InMemoryAnnotationStore
# ---------------------------------------------------------------------------

class TestInMemoryAnnotationStore(unittest.IsolatedAsyncioTestCase):

    async def test_write_and_query_by_type(self) -> None:
        store = InMemoryAnnotationStore()
        job_id = _make_job_id()
        e1 = _make_event("music_generation", job_id)
        e2 = _make_event("music_generation", job_id)
        e3 = _make_event("request_context", job_id)

        await store.write(e1)
        await store.write(e2)
        await store.write(e3)

        results = await store.query("music_generation")
        self.assertEqual(len(results), 2)
        self.assertIs(results[0], e1)
        self.assertIs(results[1], e2)

    async def test_query_limit_is_respected(self) -> None:
        store = InMemoryAnnotationStore()
        job_id = _make_job_id()
        for _ in range(10):
            await store.write(_make_event("music_generation", job_id))

        results = await store.query("music_generation", limit=3)
        self.assertEqual(len(results), 3)

    async def test_query_returns_newest_slice(self) -> None:
        """limit slices from the end (most recent)."""
        store = InMemoryAnnotationStore()
        job_id = _make_job_id()
        events = [_make_event("t", job_id) for _ in range(5)]
        for e in events:
            await store.write(e)

        results = await store.query("t", limit=2)
        self.assertEqual(results, events[-2:])

    async def test_get_by_job_isolates_by_id(self) -> None:
        store = InMemoryAnnotationStore()
        job_a = _make_job_id()
        job_b = _make_job_id()
        ea1 = _make_event("t", job_a)
        ea2 = _make_event("t", job_a)
        eb1 = _make_event("t", job_b)
        for e in (ea1, ea2, eb1):
            await store.write(e)

        self.assertEqual(await store.get_by_job(job_a), [ea1, ea2])
        self.assertEqual(await store.get_by_job(job_b), [eb1])

    async def test_get_by_job_unknown_returns_empty(self) -> None:
        store = InMemoryAnnotationStore()
        self.assertEqual(await store.get_by_job("nonexistent-job"), [])

    async def test_get_by_job_and_type(self) -> None:
        store = InMemoryAnnotationStore()
        job_id = _make_job_id()
        e_music = _make_event("music_generation", job_id)
        e_ctx = _make_event("request_context", job_id)
        await store.write(e_music)
        await store.write(e_ctx)

        self.assertEqual(
            await store.get_by_job_and_type(job_id, "music_generation"),
            [e_music],
        )
        self.assertEqual(
            await store.get_by_job_and_type(job_id, "request_context"),
            [e_ctx],
        )

    async def test_all_events(self) -> None:
        store = InMemoryAnnotationStore()
        events = [_make_event() for _ in range(5)]
        for e in events:
            await store.write(e)
        self.assertEqual(await store.all_events(), events)

    async def test_all_events_returns_copy(self) -> None:
        """Mutations to the returned list must not affect the store."""
        store = InMemoryAnnotationStore()
        await store.write(_make_event())
        snapshot = await store.all_events()
        snapshot.clear()
        self.assertEqual(len(await store.all_events()), 1)

    async def test_clear_empties_all_indices(self) -> None:
        store = InMemoryAnnotationStore()
        job_id = _make_job_id()
        await store.write(_make_event("music_generation", job_id))
        await store.write(_make_event("request_context", job_id))
        await store.clear()

        self.assertEqual(await store.all_events(), [])
        self.assertEqual(await store.query("music_generation"), [])
        self.assertEqual(await store.get_by_job(job_id), [])

    def test_stats_returns_counts(self) -> None:
        store = InMemoryAnnotationStore()

        async def _fill():
            await store.write(_make_event("a"))
            await store.write(_make_event("a"))
            await store.write(_make_event("b"))

        asyncio.run(_fill())
        stats = store.stats()
        self.assertEqual(stats["total_events"], 3)
        self.assertEqual(stats["by_type"]["a"], 2)
        self.assertEqual(stats["by_type"]["b"], 1)

    def test_repr_contains_useful_info(self) -> None:
        store = InMemoryAnnotationStore()
        r = repr(store)
        self.assertIn("InMemoryAnnotationStore", r)
        self.assertIn("total_events=0", r)


# ---------------------------------------------------------------------------
# AnnotationDispatcher
# ---------------------------------------------------------------------------

class TestAnnotationDispatcher(unittest.IsolatedAsyncioTestCase):

    async def test_emit_writes_to_store(self) -> None:
        store = InMemoryAnnotationStore()
        dispatcher = AnnotationDispatcher(stores=[store])
        event = _make_event("music_generation")

        dispatcher.emit(event)
        await _drain()

        results = await store.query("music_generation")
        self.assertEqual(len(results), 1)
        self.assertIs(results[0], event)

    async def test_emit_fans_out_to_multiple_stores(self) -> None:
        store_a = InMemoryAnnotationStore()
        store_b = InMemoryAnnotationStore()
        dispatcher = AnnotationDispatcher(stores=[store_a, store_b])

        dispatcher.emit(_make_event("test"))
        await _drain()

        self.assertEqual(len(await store_a.all_events()), 1)
        self.assertEqual(len(await store_b.all_events()), 1)

    async def test_failing_store_does_not_prevent_other_stores(self) -> None:
        """A broken store must not stop the healthy store from receiving the event."""
        broken_store = AsyncMock(spec=AnnotationStore)
        broken_store.write.side_effect = RuntimeError("disk full")

        healthy_store = InMemoryAnnotationStore()
        dispatcher = AnnotationDispatcher(stores=[broken_store, healthy_store])

        dispatcher.emit(_make_event("music_generation"))
        await _drain()

        self.assertEqual(len(await healthy_store.all_events()), 1)

    async def test_multiple_emits_accumulate(self) -> None:
        store = InMemoryAnnotationStore()
        dispatcher = AnnotationDispatcher(stores=[store])
        job_id = _make_job_id()

        for event_type in ("request_context", "video_feature", "music_generation"):
            dispatcher.emit(_make_event(event_type, job_id))

        await _drain()

        all_events = await store.get_by_job(job_id)
        event_types = {e.event_type for e in all_events}
        self.assertEqual(event_types, {"request_context", "video_feature", "music_generation"})

    def test_emit_without_running_loop_does_not_raise(self) -> None:
        """emit() called outside an event loop must silently discard the event."""
        store = InMemoryAnnotationStore()
        dispatcher = AnnotationDispatcher(stores=[store])
        dispatcher.emit(_make_event())  # Must not raise

    async def test_empty_stores_list_does_not_raise(self) -> None:
        dispatcher = AnnotationDispatcher(stores=[])
        dispatcher.emit(_make_event())
        await _drain()  # Must complete without error

    async def test_safe_emit_annotation_catches_event_construction_failure(self) -> None:
        store = InMemoryAnnotationStore()
        dispatcher = AnnotationDispatcher(stores=[store])

        def _broken_factory() -> AnnotationEvent:
            raise RuntimeError("bad annotation payload")

        safe_emit_annotation(dispatcher, _broken_factory)
        await _drain()

        self.assertEqual(await store.all_events(), [])

    async def test_safe_emit_annotation_catches_synchronous_emit_failure(self) -> None:
        class _BrokenDispatcher:
            def emit(self, _event: AnnotationEvent) -> None:
                raise RuntimeError("dispatcher unavailable")

        safe_emit_annotation(
            _BrokenDispatcher(),  # type: ignore[arg-type]
            lambda: _make_event("request_context"),
        )
        await _drain()


# ---------------------------------------------------------------------------
# Typed event dataclasses (synchronous)
# ---------------------------------------------------------------------------

class TestRequestContextEvent(unittest.TestCase):

    def test_event_type_is_correct(self) -> None:
        self.assertEqual(RequestContextEvent(job_id="j1").event_type, "request_context")

    def test_defaults(self) -> None:
        e = RequestContextEvent(job_id="j1")
        self.assertFalse(e.include_vocals)
        self.assertFalse(e.was_prompt_transformed)
        self.assertEqual(e.detected_references, [])
        self.assertEqual(e.stage_latency_s, 0.0)

    def test_mutable_list_not_shared(self) -> None:
        e1 = RequestContextEvent(job_id="j1")
        e2 = RequestContextEvent(job_id="j2")
        e1.detected_references.append("Jay-Z")
        self.assertEqual(e2.detected_references, [])

    def test_full_construction(self) -> None:
        e = RequestContextEvent(
            job_id="j1",
            original_prompt="raw",
            sanitized_prompt="clean",
            was_prompt_transformed=True,
            detected_references=["PersonA"],
            detected_language="EN",
            detected_vocal_language="EN",
            detected_category="VLOG",
            include_vocals=True,
            vocal_gender="female",
            stage_latency_s=0.75,
        )
        self.assertEqual(e.vocal_gender, "female")
        self.assertEqual(e.detected_references, ["PersonA"])


class TestVideoFeatureEvent(unittest.TestCase):

    def test_event_type_is_correct(self) -> None:
        self.assertEqual(VideoFeatureEvent(job_id="j1").event_type, "video_feature")

    def test_defaults(self) -> None:
        e = VideoFeatureEvent(job_id="j1")
        self.assertFalse(e.has_audio)
        self.assertEqual(e.audio_activity_segments, [])
        self.assertIsNone(e.width)

    def test_audio_activity_not_shared(self) -> None:
        e1 = VideoFeatureEvent(job_id="j1")
        e2 = VideoFeatureEvent(job_id="j2")
        e1.audio_activity_segments.append((0.0, 1.0))
        self.assertEqual(e2.audio_activity_segments, [])


class TestSceneAnnotation(unittest.TestCase):

    def test_to_dict(self) -> None:
        sa = SceneAnnotation(
            scene_index=0,
            start_s=0.0,
            end_s=5.0,
            visual_summary="forest path",
            key_actions="walking",
            mood="peaceful",
        )
        d = sa.to_dict()
        self.assertEqual(d["mood"], "peaceful")
        self.assertEqual(d["scene_index"], 0)


class TestSceneUnderstandingEvent(unittest.TestCase):

    def test_event_type_is_correct(self) -> None:
        self.assertEqual(
            SceneUnderstandingEvent(job_id="j1").event_type, "scene_understanding"
        )

    def test_from_scene_understandings_factory(self) -> None:
        """Verify the factory correctly converts pipeline dataclass instances."""

        class _FakeScene:
            def __init__(self, idx: int, start: float, end: float, mood: str) -> None:
                self.scene_index = idx
                self.start_timestamp = start
                self.end_timestamp = end
                self.visual_summary = "summary"
                self.key_actions = "actions"
                self.mood = mood

        fakes = [
            _FakeScene(0, 0.0, 5.0, "peaceful"),
            _FakeScene(1, 5.0, 10.0, "tense"),
            _FakeScene(2, 10.0, 15.0, "peaceful"),
        ]
        event = SceneUnderstandingEvent.from_scene_understandings(
            job_id="j1",
            scene_understandings=fakes,
            stage_latency_s=2.3,
            token_usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )

        self.assertEqual(event.scene_count, 3)
        self.assertEqual(len(event.scenes), 3)
        # "peaceful" appears twice but dominant_moods deduplicates, preserving insertion order
        self.assertEqual(event.dominant_moods, ["peaceful", "tense"])
        self.assertEqual(event.stage_latency_s, 2.3)
        self.assertEqual(event.token_usage["total_tokens"], 150)

    def test_from_scene_understandings_empty(self) -> None:
        event = SceneUnderstandingEvent.from_scene_understandings(
            job_id="j1",
            scene_understandings=[],
        )
        self.assertEqual(event.scene_count, 0)
        self.assertEqual(event.dominant_moods, [])


class TestVideoUnderstandingEvent(unittest.TestCase):

    def test_event_type_is_correct(self) -> None:
        self.assertEqual(
            VideoUnderstandingEvent(job_id="j1").event_type, "video_understanding"
        )

    def test_defaults(self) -> None:
        e = VideoUnderstandingEvent(job_id="j1")
        self.assertFalse(e.has_explicit_call_to_action)
        self.assertIsNone(e.raw_descriptions)

    def test_raw_descriptions_roundtrip(self) -> None:
        payload = {"overall_mood": "joyful", "core_message": "celebrate"}
        e = VideoUnderstandingEvent(job_id="j1", raw_descriptions=payload)
        self.assertEqual(e.raw_descriptions["overall_mood"], "joyful")


class TestMusicPromptEvent(unittest.TestCase):

    def test_event_type_is_correct(self) -> None:
        self.assertEqual(MusicPromptEvent(job_id="j1").event_type, "music_prompt")

    def test_dual_prompt_model(self) -> None:
        e = MusicPromptEvent(
            job_id="j1",
            model_spec="edenn_enhanced",
            style_prompt="chill lo-fi hip-hop",
            lyrics_prompt="write about late nights coding",
            include_vocals=True,
            vocal_gender="female",
        )
        self.assertIsNone(e.combined_prompt)
        self.assertEqual(e.style_prompt, "chill lo-fi hip-hop")

    def test_single_prompt_model(self) -> None:
        e = MusicPromptEvent(
            job_id="j1",
            model_spec="edenn_basic",
            combined_prompt="upbeat pop track",
        )
        self.assertIsNone(e.style_prompt)
        self.assertIsNone(e.lyrics_prompt)


class TestMusicGenerationEvent(unittest.TestCase):

    def test_event_type_is_correct(self) -> None:
        self.assertEqual(MusicGenerationEvent(job_id="j1").event_type, "music_generation")

    def test_defaults(self) -> None:
        e = MusicGenerationEvent(job_id="j1")
        self.assertFalse(e.has_lyrics)
        self.assertEqual(e.line_timestamp_count, 0)
        self.assertEqual(e.extension_rounds, 0)
        self.assertIsNone(e.generation_latency_s)

    def test_provider_name_for_spec(self) -> None:
        self.assertEqual(MusicGenerationEvent.provider_name_for_spec("edenn_basic"), "provider_a")
        self.assertEqual(MusicGenerationEvent.provider_name_for_spec("edenn_enhanced"), "provider_b")
        self.assertEqual(MusicGenerationEvent.provider_name_for_spec("edenn_studio"), "provider_c")
        self.assertEqual(MusicGenerationEvent.provider_name_for_spec("unknown_model"), "unknown")

    def test_provider_name_for_spec_case_insensitive(self) -> None:
        self.assertEqual(MusicGenerationEvent.provider_name_for_spec("EDENN_BASIC"), "provider_a")

    def test_full_construction(self) -> None:
        e = MusicGenerationEvent(
            job_id="j1",
            model_spec="edenn_enhanced",
            provider_name="provider_b",
            include_vocals=True,
            vocal_gender="female",
            has_lyrics=True,
            full_lyrics_text="verse 1\nverse 2",
            line_timestamp_count=8,
            word_timestamp_count=64,
            video_duration_s=45.0,
            overall_mood="energetic",
            generation_latency_s=12.5,
        )
        self.assertTrue(e.has_lyrics)
        self.assertEqual(e.generation_latency_s, 12.5)
        self.assertEqual(e.overall_mood, "energetic")


class TestRemixCompletionEvent(unittest.TestCase):

    def test_event_type_is_correct(self) -> None:
        self.assertEqual(RemixCompletionEvent(job_id="j1").event_type, "remix_completion")

    def test_defaults(self) -> None:
        e = RemixCompletionEvent(job_id="j1")
        self.assertEqual(e.music_volume, 1.0)
        self.assertEqual(e.duck_gain_db, -9.0)
        self.assertIsNone(e.total_pipeline_latency_s)


# ---------------------------------------------------------------------------
# Integration: full annotation chain through dispatcher → store
# ---------------------------------------------------------------------------

class TestAnnotationChainIntegration(unittest.IsolatedAsyncioTestCase):
    """
    Simulate a pipeline run emitting all 7 event types in sequence and verify
    the store state reflects the correct per-job and per-type indices.
    """

    async def test_full_pipeline_annotation_chain(self) -> None:
        store = InMemoryAnnotationStore()
        dispatcher = AnnotationDispatcher(stores=[store])
        job_id = _make_job_id()

        # Stage 0
        dispatcher.emit(RequestContextEvent(
            job_id=job_id,
            original_prompt="chill vibes",
            sanitized_prompt="chill vibes",
            detected_language="EN",
            include_vocals=False,
            stage_latency_s=0.5,
        ))
        # Stage 1
        dispatcher.emit(VideoFeatureEvent(
            job_id=job_id,
            video_filename="clip.mp4",
            duration_s=30.0,
            fps=30.0,
            has_audio=True,
            stage_latency_s=0.1,
        ))
        # Stage 2
        dispatcher.emit(SceneUnderstandingEvent.from_scene_understandings(
            job_id=job_id,
            scene_understandings=[],
        ))
        # Stage 3
        dispatcher.emit(VideoUnderstandingEvent(
            job_id=job_id,
            video_title="My Video",
            overall_mood="calm",
        ))
        # Stage 3.1
        dispatcher.emit(MusicPromptEvent(
            job_id=job_id,
            model_spec="edenn_basic",
            combined_prompt="calm ambient music",
        ))
        # Stage 4
        dispatcher.emit(MusicGenerationEvent(
            job_id=job_id,
            model_spec="edenn_basic",
            provider_name="provider_a",
            video_duration_s=30.0,
            overall_mood="calm",
            generation_latency_s=8.2,
        ))
        # Stage 5
        dispatcher.emit(RemixCompletionEvent(
            job_id=job_id,
            remixed_video_filename="output.mp4",
            total_pipeline_latency_s=25.0,
        ))

        await _drain()

        all_for_job = await store.get_by_job(job_id)
        self.assertEqual(len(all_for_job), 7)

        event_types_in_order = [e.event_type for e in all_for_job]
        self.assertEqual(event_types_in_order, [
            "request_context",
            "video_feature",
            "scene_understanding",
            "video_understanding",
            "music_prompt",
            "music_generation",
            "remix_completion",
        ])

    async def test_music_generation_event_queryable_by_type(self) -> None:
        store = InMemoryAnnotationStore()
        dispatcher = AnnotationDispatcher(stores=[store])

        job_a = _make_job_id()
        job_b = _make_job_id()

        dispatcher.emit(MusicGenerationEvent(job_id=job_a, model_spec="edenn_basic"))
        dispatcher.emit(MusicGenerationEvent(job_id=job_b, model_spec="edenn_enhanced"))
        await _drain()

        results = await store.query("music_generation", limit=10)
        self.assertEqual(len(results), 2)
        specs = {e.model_spec for e in results}
        self.assertEqual(specs, {"edenn_basic", "edenn_enhanced"})

    async def test_no_cross_job_contamination(self) -> None:
        """Events for job A must not appear when querying job B."""
        store = InMemoryAnnotationStore()
        dispatcher = AnnotationDispatcher(stores=[store])

        job_a = _make_job_id()
        job_b = _make_job_id()

        dispatcher.emit(RequestContextEvent(job_id=job_a, original_prompt="disco"))
        dispatcher.emit(RequestContextEvent(job_id=job_b, original_prompt="jazz"))
        await _drain()

        events_a = await store.get_by_job(job_a)
        events_b = await store.get_by_job(job_b)

        self.assertEqual(len(events_a), 1)
        self.assertEqual(len(events_b), 1)
        self.assertEqual(events_a[0].job_id, job_a)
        self.assertEqual(events_b[0].job_id, job_b)

    async def test_store_clear_resets_state(self) -> None:
        store = InMemoryAnnotationStore()
        await store.write(_make_event("music_generation"))
        await store.clear()
        self.assertEqual(store.stats()["total_events"], 0)

    async def test_dispatcher_emit_is_non_blocking(self) -> None:
        """
        Verify that emit() itself completes synchronously (no await needed)
        while the background write completes after yielding.
        """
        store = InMemoryAnnotationStore()
        dispatcher = AnnotationDispatcher(stores=[store])
        event = MusicGenerationEvent(job_id=_make_job_id(), model_spec="edenn_studio")

        result = dispatcher.emit(event)
        self.assertIsNone(result)  # Synchronous return

        await _drain()
        self.assertEqual(len(await store.all_events()), 1)

    async def test_get_by_job_and_type_pinpoints_single_event(self) -> None:
        store = InMemoryAnnotationStore()
        dispatcher = AnnotationDispatcher(stores=[store])
        job_id = _make_job_id()

        dispatcher.emit(MusicGenerationEvent(job_id=job_id, model_spec="edenn_studio"))
        dispatcher.emit(RequestContextEvent(job_id=job_id, original_prompt="test"))
        await _drain()

        music_events = await store.get_by_job_and_type(job_id, "music_generation")
        self.assertEqual(len(music_events), 1)
        self.assertIsInstance(music_events[0], MusicGenerationEvent)


# ---------------------------------------------------------------------------
# Pipeline wiring: VideoMusicWorkflowE2EInput annotation_dispatcher field
# ---------------------------------------------------------------------------

class TestWorkflowInputAnnotationField(unittest.TestCase):
    """
    Verify that VideoMusicWorkflowE2EInput accepts an annotation_dispatcher
    and that the field defaults to None without breaking existing callers.
    """

    def test_annotation_dispatcher_field_defaults_to_none(self) -> None:
        from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow import (
            VideoMusicWorkflowE2EInput,
        )
        inp = VideoMusicWorkflowE2EInput(
            video_path="/tmp/video.mp4",
            user_prompt="test",
            music_model_spec="edenn_basic",
        )
        self.assertIsNone(inp.annotation_dispatcher)

    def test_annotation_dispatcher_field_accepts_dispatcher(self) -> None:
        from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow import (
            VideoMusicWorkflowE2EInput,
        )
        store = InMemoryAnnotationStore()
        dispatcher = AnnotationDispatcher(stores=[store])
        inp = VideoMusicWorkflowE2EInput(
            video_path="/tmp/video.mp4",
            user_prompt="test",
            music_model_spec="edenn_basic",
            annotation_dispatcher=dispatcher,
        )
        self.assertIs(inp.annotation_dispatcher, dispatcher)


if __name__ == "__main__":
    unittest.main()
