import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from EdennCode.MusicGenerationCore.models import (
    MusicGenerationOptions,
    MusicGenerationRequest,
    MusicGenerationResult,
    MusicModelSpec,
    MusicSection,
    MusicVariant,
    NarrativeCue,
    ProviderJobRef,
    SectionPlan,
)
from EdennCode.MusicGenerationCore.providers.base import MusicGenerationStrategy
from EdennCode.MusicGenerationCore.audio import measure_audio_duration
from EdennCode.MusicGenerationCore.service import MusicGenerationService
from EdennCode.Util.MediaUtils import resolve_ffmpeg_binary


class _FakeStrategy:
    def __init__(self, label: str) -> None:
        self.label = label
        self.last_request = None

    async def generate(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        self.last_request = request
        request.output_dir.mkdir(parents=True, exist_ok=True)
        audio_path = request.output_dir / f"{self.label}.wav"
        audio_path.write_bytes(b"RIFF")
        return MusicGenerationResult(
            used_modelspec=request.modelspec,
            primary=MusicVariant(
                variant_id="primary",
                audio_path=audio_path,
                duration_s=request.section_plan.total_duration_s,
            ),
            alternates=[],
            job_ref=ProviderJobRef(provider=self.label),
            prompt_manifest={"label": self.label},
            prompt_summary=f"summary:{self.label}",
        )


class MusicGenerationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_routes_to_normalized_modelspec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            basic = _FakeStrategy("basic")
            studio = _FakeStrategy("studio")
            service = MusicGenerationService(
                strategy_builders={
                    MusicModelSpec.EDENN_BASIC: basic,
                    MusicModelSpec.EDENN_STUDIO: studio,
                }
            )
            plan = SectionPlan(
                summary="story",
                total_duration_s=6.0,
                overall_mood="bright",
                target_bpm=120.0,
                primary_instruments=["synth"],
                cues=[
                    NarrativeCue(
                        cue_id="image_1",
                        label="Frame 1",
                        role="setup",
                        target_duration_s=3.0,
                    )
                ],
                sections=[
                    MusicSection(
                        section_id="intro",
                        label="Intro",
                        target_duration_s=6.0,
                        objective="set up",
                        energy_start=0.2,
                        energy_end=0.6,
                        image_indices=[1],
                        cue_ids=["image_1"],
                    )
                ],
                music_prompt_summary="bright synth",
            )
            request = MusicGenerationRequest(
                request_id="req_1",
                modelspec=MusicModelSpec.EDENN_STUDIO,
                section_plan=plan,
                options=MusicGenerationOptions(),
                output_dir=Path(tmp),
            )

            result = await service.generate(request)

            self.assertEqual(result.job_ref.provider, "studio")
            self.assertIsNone(basic.last_request)
            self.assertIsNotNone(studio.last_request)

    async def test_service_appends_watermark_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            studio = _FakeStrategy("studio")
            service = MusicGenerationService(
                strategy_builders={MusicModelSpec.EDENN_STUDIO: studio}
            )
            plan = SectionPlan(
                summary="story",
                total_duration_s=6.0,
                overall_mood="bright",
                target_bpm=120.0,
                primary_instruments=["synth"],
                cues=[],
                sections=[],
                music_prompt_summary="bright synth",
            )
            request = MusicGenerationRequest(
                request_id="req_1",
                modelspec=MusicModelSpec.EDENN_STUDIO,
                section_plan=plan,
                options=MusicGenerationOptions(water_mark=True),
                output_dir=Path(tmp),
            )
            watermarked_path = Path(tmp) / "studio_watermarked.wav"
            watermarked_path.write_bytes(b"RIFF")

            with patch(
                "EdennCode.MusicGenerationCore.service.append_voice_watermark_to_audio",
                return_value=watermarked_path,
            ) as watermark, patch(
                "EdennCode.MusicGenerationCore.service.measure_audio_duration",
                return_value=6.7,
            ):
                result = await service.generate(request)

            self.assertEqual(result.primary.audio_path, watermarked_path)
            self.assertEqual(result.primary.duration_s, 6.7)
            watermark.assert_called_once_with(Path(tmp) / "studio.wav")


class _TaggingStrategy(MusicGenerationStrategy):
    """A provider that ships a real audio file with a trailing vendor tag."""

    appends_provider_tail = True

    def __init__(self, *, track_duration_s: float) -> None:
        self.track_duration_s = track_duration_s

    async def generate(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        audio_path = request.output_dir / "primary.mp3"
        subprocess.run(
            [
                resolve_ffmpeg_binary(), "-y", "-f", "lavfi",
                "-i", f"sine=frequency=440:duration={self.track_duration_s}",
                "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "192k",
                str(audio_path),
            ],
            check=True,
            capture_output=True,
        )
        return MusicGenerationResult(
            used_modelspec=request.modelspec,
            primary=MusicVariant(
                variant_id="primary",
                audio_path=audio_path,
                duration_s=self.track_duration_s,
            ),
            alternates=[],
            job_ref=ProviderJobRef(provider="tagging"),
            prompt_manifest={},
            prompt_summary="tagging",
        )


class _UntaggedStrategy(_TaggingStrategy):
    """A provider whose output carries no vendor tag — must not be chopped."""

    appends_provider_tail = False


def _plan(total_duration_s: float) -> SectionPlan:
    return SectionPlan(
        summary="story",
        total_duration_s=total_duration_s,
        overall_mood="bright",
        target_bpm=120.0,
        primary_instruments=["synth"],
        cues=[
            NarrativeCue(
                cue_id="image_1",
                label="Frame 1",
                role="setup",
                target_duration_s=total_duration_s,
            )
        ],
        sections=[
            MusicSection(
                section_id="intro",
                label="Intro",
                target_duration_s=total_duration_s,
                objective="set up",
                energy_start=0.2,
                energy_end=0.6,
                image_indices=[1],
                cue_ids=["image_1"],
            )
        ],
        music_prompt_summary="bright synth",
    )


class ProviderTailStrippingTests(unittest.IsolatedAsyncioTestCase):
    async def _generate(self, strategy, *, plan_duration_s: float, water_mark: bool):
        with tempfile.TemporaryDirectory() as tmp:
            service = MusicGenerationService(
                strategy_builders={MusicModelSpec.EDENN_STUDIO: strategy}
            )
            request = MusicGenerationRequest(
                request_id="tail",
                modelspec=MusicModelSpec.EDENN_STUDIO,
                section_plan=_plan(plan_duration_s),
                options=MusicGenerationOptions(water_mark=water_mark),
                output_dir=Path(tmp),
            )
            result = await service.generate(request)
            yield_path = result.primary.audio_path
            return result, yield_path, measure_audio_duration(yield_path, fallback_s=0.0)

    async def test_tail_is_stripped_even_when_the_track_barely_covers_the_plan(self) -> None:
        """The strategies' own chop stands down here; the service's must not.

        A track that only just covers what was asked for is exactly the case
        where a caller's window runs to the end of the file and plays the tag.
        """

        result, path, duration_s = await self._generate(
            _TaggingStrategy(track_duration_s=20.0), plan_duration_s=18.0, water_mark=False
        )
        self.assertTrue(path.stem.endswith("_trimmed_tail6s"), path.name)
        self.assertAlmostEqual(duration_s, 14.0, delta=0.3)
        self.assertAlmostEqual(result.primary.duration_s, 14.0, delta=0.3)

    async def test_untagged_provider_keeps_every_second(self) -> None:
        _, path, duration_s = await self._generate(
            _UntaggedStrategy(track_duration_s=20.0), plan_duration_s=18.0, water_mark=False
        )
        self.assertNotIn("_trimmed_tail", path.stem)
        self.assertAlmostEqual(duration_s, 20.0, delta=0.3)

    async def test_watermark_lands_on_the_stripped_track_not_the_tag(self) -> None:
        with patch(
            "EdennCode.MusicGenerationCore.service.append_voice_watermark_to_audio",
            side_effect=lambda path: path,
        ) as watermark:
            _, path, _ = await self._generate(
                _TaggingStrategy(track_duration_s=20.0), plan_duration_s=18.0, water_mark=True
            )
        watermark.assert_called_once()
        self.assertTrue(watermark.call_args.args[0].stem.endswith("_trimmed_tail6s"))
        self.assertEqual(path, watermark.call_args.args[0])
