"""Rendering the plan a person approved, instead of re-deciding it.

The end-to-end workflow spots its own events, which is right when nobody has
said what they want. But the console asks a user to approve specific effects at
specific moments and then handed that plan over as a sentence of prose, letting
the workflow spot again from scratch — so the timestamps on the card and the
hits in the take were related only by coincidence.

These tests pin the other mode: the plan IS the spotting, no analysis stage is
ever constructed, and a moment a person placed is not quietly moved.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import List

from EdennCode.ModelFactory.VideoSFXModelFactory.CloudSoundEffectGen.base import (
    SoundEffectProvider,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.SoundEffectGenerationStage.sound_effect_generation_stage import (
    SoundEffectGenerationStage,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.planned_run import (
    DEFAULT_EVENT_DURATION_S,
    MIN_AUDIBLE_EVENT_S,
    PlannedSfxInput,
    PlannedSfxRun,
    plan_rows_to_events,
    rendered_event_manifest,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.video_sound_effect_workflow import (
    VideoSfxWorkflowOptions,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.tests._synthetic_media import (
    make_click_wav,
    make_flash_video,
)


class _RecordingProvider(SoundEffectProvider):
    """Writes a short tone and records every prompt it was asked to render."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: List[dict] = []

    async def generate(
        self,
        *,
        prompt: str,
        duration_seconds: float,
        output_stem: str,
        output_dir: Path,
        sample_rate: int = 44100,
        loop: bool = False,
    ) -> Path:
        self.calls.append({"prompt": prompt, "duration": duration_seconds, "loop": loop})
        return make_click_wav(
            output_dir / f"{output_stem}.wav",
            duration_s=max(0.2, min(duration_seconds, 2.0)),
            click_duration_s=0.1,
            sample_rate=sample_rate,
        )


class _ExplodingTimingStage:
    """Timing refinement must never see a row a person placed."""

    def __init__(self) -> None:
        self.seen: List[str] = []

    async def run(self, stage_input):  # noqa: ANN001 - stage protocol
        self.seen.extend(e.event_id for e in stage_input.events)
        raise AssertionError(
            f"timing refinement ran on user-placed rows: {self.seen}"
        )


# ---------------------------------------------------------------------------#
# plan rows -> events                                                         #
# ---------------------------------------------------------------------------#


class PlanRowMappingTests(unittest.TestCase):
    def test_the_prompt_drives_generation_not_the_label(self) -> None:
        # The generator prefers sound_prompt and falls back to the description,
        # so mapping both to the description synthesises from "Effect 2".
        events = plan_rows_to_events(
            [{"label": "Effect 2", "prompt": "a heavy wooden door"}], video_duration=10.0
        )
        self.assertEqual(events[0].generation_prompt, "a heavy wooden door")
        self.assertEqual(events[0].event_description, "Effect 2")

    def test_a_planned_row_becomes_a_user_owned_event(self) -> None:
        events = plan_rows_to_events([{"label": "door", "start_s": 3.0}], video_duration=10.0)
        self.assertEqual(events[0].origin, "user")
        self.assertEqual(events[0].source, "user")
        self.assertEqual(events[0].timing_authority, "user")

    def test_a_row_without_a_duration_gets_a_usable_one(self) -> None:
        # The generator's own floor is 0.2s, which reads as a click rather than
        # a sound; a planned row carries a moment, not a length.
        events = plan_rows_to_events([{"label": "door", "start_s": 1.0}], video_duration=10.0)
        self.assertAlmostEqual(events[0].duration, DEFAULT_EVENT_DURATION_S, places=3)

    def test_a_stated_duration_is_honoured_and_clamped_to_the_video(self) -> None:
        events = plan_rows_to_events(
            [{"start_s": 9.5, "duration_s": 5.0}], video_duration=10.0
        )
        self.assertLessEqual(events[0].end_time, 10.0)

    def test_an_accepted_transition_keeps_its_snap(self) -> None:
        # A stylistic suggestion was cut-snapped when it was proposed; accepting
        # it should not turn it into a hand-placed hit.
        events = plan_rows_to_events(
            [{"label": "whoosh", "start_s": 4.0, "timing_authority": "cut_snap"}],
            video_duration=10.0,
        )
        self.assertEqual(events[0].timing_authority, "cut_snap")

    def test_junk_rows_are_skipped_not_fatal(self) -> None:
        events = plan_rows_to_events(
            ["nonsense", None, {"label": "real", "start_s": 1.0}], video_duration=10.0
        )
        self.assertEqual(len(events), 1)

    def test_an_unreadable_start_falls_back_rather_than_raising(self) -> None:
        events = plan_rows_to_events([{"start_s": "soon"}], video_duration=10.0)
        self.assertEqual(events[0].start_time, 0.0)


# ---------------------------------------------------------------------------#
# the run itself                                                              #
# ---------------------------------------------------------------------------#


class PlannedRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, rows, *, ambience: str = "", timing_stage=None):
        video = make_flash_video(self.tmp / "video.mp4", duration_s=3.0, flash_times_s=[1.5])
        provider = _RecordingProvider()
        run = PlannedSfxRun(
            timing_refinement_stage=timing_stage,
            sound_effect_generation_stage=SoundEffectGenerationStage(
                sound_effect_generation_model=provider,
                video_conditioned_model=None,  # offline: the text route only
            ),
        )
        out = asyncio.run(
            run.execute(
                PlannedSfxInput(
                    video_path=str(video),
                    uploaded_public_facing_url="",
                    events=rows,
                    ambience_prompt=ambience,
                    run_dir=str(self.tmp / "run"),
                    options=VideoSfxWorkflowOptions(
                        enable_ambience=bool(ambience), num_variants=1,
                    ),
                )
            )
        )
        return out, provider

    def test_every_approved_event_is_rendered_at_the_moment_it_was_placed(self) -> None:
        rows = [
            {"id": "sfx_event_001", "label": "door", "prompt": "a wooden door", "start_s": 0.5},
            {"id": "sfx_event_002", "label": "step", "prompt": "a footstep", "start_s": 2.0},
        ]
        out, provider = self._run(rows)

        self.assertEqual(len(out.generated_sound_events), 2)
        starts = sorted(e.effective_start for e in out.generated_sound_events)
        self.assertEqual([round(s, 2) for s in starts], [0.5, 2.0])
        prompts = sorted(call["prompt"] for call in provider.calls)
        self.assertEqual(prompts, ["a footstep", "a wooden door"])
        self.assertTrue(Path(out.final_video_path).exists())

    def test_the_analysis_stage_is_never_constructed(self) -> None:
        # A planned render costs nothing in video-understanding calls; that is
        # half the point of having the mode at all.
        run = PlannedSfxRun()
        self.assertFalse(hasattr(run, "video_event_analysis_stage"))

    def test_a_user_placed_moment_is_not_re_snapped(self) -> None:
        # The timing stage decides by event type and duration and never reads
        # timing_authority, so the filtering has to happen at the call site.
        out, _ = self._run(
            [{"label": "door", "start_s": 0.5}], timing_stage=_ExplodingTimingStage(),
        )
        self.assertEqual(round(out.generated_sound_events[0].effective_start, 2), 0.5)

    def test_an_ambience_only_plan_renders_a_bed_with_the_approved_words(self) -> None:
        out, provider = self._run([], ambience="quiet room tone")

        self.assertEqual(out.generated_sound_events, [])
        self.assertTrue(any("room tone" in c["prompt"] for c in provider.calls))
        self.assertTrue(Path(out.final_video_path).exists())

    def test_the_manifest_reports_what_rendered(self) -> None:
        out, _ = self._run([{"id": "e1", "label": "door", "start_s": 1.0}])
        manifest = rendered_event_manifest(out.generated_sound_events)

        self.assertEqual(manifest[0]["id"], "e1")
        self.assertTrue(manifest[0]["rendered"])
        self.assertEqual(manifest[0]["origin"], "user")

    def test_the_manifest_is_stable_across_repeated_reads(self) -> None:
        # It is folded onto the variant on every 2.5s poll; unstable ordering or
        # unrounded floats would rewrite the session on every tick.
        out, _ = self._run([{"id": "e1", "start_s": 1.0}, {"id": "e2", "start_s": 2.0}])
        self.assertEqual(
            rendered_event_manifest(out.generated_sound_events),
            rendered_event_manifest(out.generated_sound_events),
        )


if __name__ == "__main__":
    unittest.main()


class PlanRowEdgeTests(unittest.TestCase):
    """The edges the first pass got wrong."""

    def test_a_hit_on_the_final_frame_stays_audible(self) -> None:
        # The plan clamps its own starts to the duration INCLUSIVELY, so "an
        # impact on the last frame" of a 10s clip arrives as start_s 10.0. It
        # used to become a 0.05s click — the exact thing a default duration is
        # there to prevent.
        events = plan_rows_to_events([{"label": "impact", "start_s": 10.0}], video_duration=10.0)
        self.assertAlmostEqual(events[0].duration, MIN_AUDIBLE_EVENT_S, places=6)

    def test_an_event_that_already_fits_is_not_moved(self) -> None:
        events = plan_rows_to_events([{"start_s": 2.0}], video_duration=3.0)
        self.assertEqual(events[0].start_time, 2.0, "a hit that fits was relocated")

    def test_duplicate_ids_are_made_distinct(self) -> None:
        # Ids address events for refinement and for the render manifest, so a
        # duplicate makes two rows the same row — one moment silently replaced,
        # generated twice, and mutated from two coroutines at once.
        events = plan_rows_to_events(
            [{"id": "dup", "start_s": 1.0}, {"id": "dup", "start_s": 5.0}],
            video_duration=20.0,
        )
        self.assertNotEqual(events[0].event_id, events[1].event_id)

    def test_an_unrecognised_timing_authority_keeps_the_user_placement(self) -> None:
        # Fail closed: a typo must not hand a user-placed moment to the motion
        # detector, which may move it by up to a second.
        events = plan_rows_to_events(
            [{"start_s": 4.0, "timing_authority": "Manual"}], video_duration=20.0,
        )
        self.assertEqual(events[0].timing_authority, "user")

    def test_a_snappable_authority_is_preserved(self) -> None:
        events = plan_rows_to_events(
            [{"start_s": 4.0, "timing_authority": "cut_snap"}], video_duration=20.0,
        )
        self.assertEqual(events[0].timing_authority, "cut_snap")

    def test_an_infinite_duration_falls_back(self) -> None:
        events = plan_rows_to_events(
            [{"start_s": 1.0, "duration_s": float("inf")}], video_duration=20.0,
        )
        self.assertAlmostEqual(events[0].duration, DEFAULT_EVENT_DURATION_S, places=2)


class RefinementBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_duplicate_id_cannot_let_refinement_cross_the_exclusion_line(self) -> None:
        """Refinements are written back POSITIONALLY. Matching by id let a
        duplicate reach across and replace a user-placed moment with an
        engine-snapped one — losing the user's hit and rendering the other
        twice."""
        video = make_flash_video(self.tmp / "v.mp4", duration_s=6.0, flash_times_s=[3.0])
        provider = _RecordingProvider()

        class _MovingStage:
            async def run(self, stage_input):  # noqa: ANN001
                for event in stage_input.events:
                    event.refined_start_time = 5.4
                return type("Out", (), {"events": stage_input.events, "refinements": []})()

        out = asyncio.run(
            PlannedSfxRun(
                timing_refinement_stage=_MovingStage(),
                sound_effect_generation_stage=SoundEffectGenerationStage(
                    sound_effect_generation_model=provider, video_conditioned_model=None,
                ),
            ).execute(
                PlannedSfxInput(
                    video_path=str(video), uploaded_public_facing_url="",
                    events=[
                        {"id": "dup", "label": "user hit", "start_s": 1.0},
                        {"id": "dup", "label": "engine hit", "start_s": 4.0,
                         "timing_authority": "cut_snap"},
                    ],
                    run_dir=str(self.tmp / "run"),
                    options=VideoSfxWorkflowOptions(enable_ambience=False),
                )
            )
        )
        starts = sorted(round(e.effective_start, 2) for e in out.generated_sound_events)
        self.assertEqual(len(out.generated_sound_events), 2)
        self.assertIn(1.0, starts, "the user-placed moment was replaced")


class RenderedEffectReuseTests(unittest.TestCase):
    """Fixing one hit should cost one generation, not a whole bed's worth.

    Every effect is its own paid synthesis. Re-rendering a bed of twelve to
    correct one means the user pays again for eleven sounds they were happy
    with — and gets subtly different ones, because synthesis is not
    deterministic. The mix itself is cheap local ffmpeg; the synthesis is the
    cost, which is why reuse belongs at the event level and not the bed level.
    """

    def test_the_manifest_carries_each_effect_s_audio(self) -> None:
        """Without this a later render has nothing to reuse: the per-event
        audio exists during the render and was then unreachable."""
        from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (  # noqa: E501
            SfxVariant,
            SoundFXEvent,
        )

        event = SoundFXEvent(
            event_id="e1", start_time=2.0, end_time=2.4,
            event_description="door", sound_event_local_path="",
            variants=[SfxVariant(path="/tmp/door.wav")], selected_variant=0,
        )
        [row] = rendered_event_manifest([event])

        self.assertEqual(row["audio_path"], "/tmp/door.wav")
        self.assertTrue(row["rendered"])

    def test_an_effect_that_never_rendered_offers_no_audio_to_reuse(self) -> None:
        from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (  # noqa: E501
            SoundFXEvent,
        )

        event = SoundFXEvent(
            event_id="e2", start_time=1.0, end_time=1.3,
            event_description="silent", sound_event_local_path="",
        )
        [row] = rendered_event_manifest([event])

        self.assertEqual(row["audio_path"], "")
        self.assertFalse(row["rendered"])

    def test_the_planned_run_accepts_a_reuse_map(self) -> None:
        """The contract the agent fills in: event id -> audio already made."""
        import dataclasses

        fields = {f.name for f in dataclasses.fields(PlannedSfxInput)}
        self.assertIn("reuse_event_audio", fields)

    def test_kept_effects_are_never_sent_for_generation(self) -> None:
        """The measurable claim, read off the code that splits the work: only
        the events with no kept audio reach the generation stage, and results
        are written back POSITIONALLY so a kept effect cannot be replaced by a
        generated one."""
        source = Path(
            "EdennCode/WorkflowFactory/VideoSoundEffectWorkflow/planned_run.py"
        ).read_text()
        body = source.split("reuse = {")[1].split("ambience: Optional[AmbienceBed]")[0]

        self.assertIn("list_of_generation_packages=pending", body)
        self.assertIn("zip(pending_slots,", body)
        self.assertIn("event.selected_variant = 0", body)
