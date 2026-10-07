import tempfile
import unittest
import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from EdennCode.MusicGenerationCore.models import (
    MusicGenerationOptions,
    MusicGenerationRequest,
    MusicModelSpec,
    MusicSection,
    NarrativeCue,
    SectionPlan,
)
from EdennCode.MusicGenerationCore.providers.provider_a import ProviderAMusicGenerationStrategy
from EdennCode.MusicGenerationCore.providers.provider_b import ProviderBMusicGenerationStrategy
from EdennCode.MusicGenerationCore.providers.provider_c import ProviderCMusicGenerationStrategy
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import GenerationResult, ProviderCTrack


def _build_plan() -> SectionPlan:
    return SectionPlan(
        summary="Three-scene lifestyle arc",
        total_duration_s=9.0,
        overall_mood="uplifting",
        target_bpm=118.0,
        primary_instruments=["piano", "claps"],
        cues=[
            NarrativeCue(
                cue_id="image_1",
                label="Hook",
                role="setup",
                target_duration_s=3.0,
                emotion="curious",
                description="Open on the hero image.",
            ),
            NarrativeCue(
                cue_id="image_2",
                label="Lift",
                role="build",
                target_duration_s=3.0,
                emotion="excited",
                description="Energy builds around the product.",
            ),
            NarrativeCue(
                cue_id="image_3",
                label="Resolve",
                role="payoff",
                target_duration_s=3.0,
                emotion="joyful",
                description="The payoff lands.",
            ),
        ],
        sections=[
            MusicSection(
                section_id="intro",
                label="Intro",
                target_duration_s=3.0,
                objective="Set up the story",
                energy_start=0.2,
                energy_end=0.5,
                image_indices=[1],
                cue_ids=["image_1"],
                instrumentation_focus=["piano"],
                lyric_lines=["Open up the daylight"],
            ),
            MusicSection(
                section_id="lift",
                label="Lift",
                target_duration_s=3.0,
                objective="Raise momentum",
                energy_start=0.5,
                energy_end=0.8,
                image_indices=[2],
                cue_ids=["image_2"],
                instrumentation_focus=["claps"],
                lyric_lines=["Feel the city rising"],
            ),
            MusicSection(
                section_id="resolve",
                label="Resolve",
                target_duration_s=3.0,
                objective="Land the payoff",
                energy_start=0.7,
                energy_end=0.6,
                image_indices=[3],
                cue_ids=["image_3"],
                instrumentation_focus=["pads"],
                lyric_lines=["Hold the moment close"],
            ),
        ],
        music_prompt_summary="Uplifting piano pop with a bright payoff.",
    )


def _write_sine_wav(path: Path, duration: float = 0.5, sample_rate: int = 16000) -> None:
    frames = int(duration * sample_rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for i in range(frames):
            val = int(32767 * 0.1 * math.sin(2 * math.pi * 440 * (i / sample_rate)))
            wav.writeframes(struct.pack("<h", val))


@dataclass
class _Word:
    text: str
    startS: float
    endS: float
    i: int = 0


class _FakeElevenProvider:
    def __init__(self) -> None:
        self.calls = []

    async def generate(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        output_path = Path(tempfile.gettempdir()) / "fake_eleven.wav"
        output_path.write_bytes(b"RIFF")
        return output_path, [_Word("hello", 0.0, 1.0, 0)]


class _FakeProviderBProvider:
    def __init__(self) -> None:
        self.calls = []

    async def generate_song_task(self, **kwargs):
        self.calls.append(("generate_song_task", kwargs))
        return SimpleNamespace(task_id="song_task")

    async def clone_vocal(self, audio_path):
        self.calls.append(("clone_vocal", {"audio_path": str(audio_path)}))
        return "vocal_321"

    async def wait_song_task(self, task_id, timeout_s=600.0, poll_s=5.0):
        self.calls.append(("wait_song_task", {"task_id": task_id}))
        return SimpleNamespace(
            task_id=task_id,
            trace_id="trace_1",
            raw={
                "audio_urls": [
                    "https://example.com/provider_b_primary.mp3",
                    "https://example.com/provider_b_alt.mp3",
                ],
                "lyrics_sections": [
                    {"lines": [{"text": "hello", "start": 0, "end": 1000}]}
                ],
            },
        )

    async def generate_instrumental_task(self, **kwargs):
        self.calls.append(("generate_instrumental_task", kwargs))
        return SimpleNamespace(task_id="inst_task")

    async def wait_instrumental_task(self, task_id, timeout_s=600.0, poll_s=4.0):
        self.calls.append(("wait_instrumental_task", {"task_id": task_id}))
        return SimpleNamespace(
            task_id=task_id,
            trace_id="trace_2",
            raw={"audio_urls": ["https://example.com/provider_b_inst.mp3"]},
        )

    async def download_audio(self, url, dest_path):
        self.calls.append(("download_audio", {"url": url, "dest_path": str(dest_path)}))
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"ID3")
        return dest_path

    async def extend_song_from_audio(self, **kwargs):
        self.calls.append(("extend_song_from_audio", kwargs))
        output_path = Path(kwargs["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"ID3")
        return output_path, [_Word("hello", 0.0, 1.0, 0)], kwargs["lyrics"]


class _FakeProviderCProvider:
    def __init__(self) -> None:
        self.calls = []

    async def generate_and_poll_tracks(self, params, *, timeout_s=300.0, poll_s=10.0):
        self.calls.append(("generate_and_poll_tracks", params))
        result = GenerationResult(
            task_id="provider_c_task",
            status="SUCCESS",
            tracks=[
                ProviderCTrack(audio_id="audio_1", audio_url="https://example.com/audio1.wav"),
                ProviderCTrack(audio_id="audio_2", audio_url="https://example.com/audio2.wav"),
            ],
        )
        return "provider_c_task", result, "https://api.provider-c.example.invalid/api/v1"

    async def download(self, track, dest_path):
        self.calls.append(("download", {"audio_id": track.audio_id, "dest_path": str(dest_path)}))
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"RIFF")
        return dest_path

    async def extend_and_poll_track(self, audio_id, *, model="V5", timeout_s=180.0, poll_s=10.0,
                                    instrumental=None):
        self.calls.append(("extend_and_poll_track",
                           {"audio_id": audio_id, "model": model, "instrumental": instrumental}))
        return "extended_task", ProviderCTrack(audio_id=audio_id, audio_url=f"https://example.com/{audio_id}.wav"), "base"

    async def wait_for_timestamped_lyrics(self, task_id, audio_id, *, timeout_s=100.0, poll_s=None, max_poll_s=None, backoff=None):
        self.calls.append(("wait_for_timestamped_lyrics", {"task_id": task_id, "audio_id": audio_id}))
        return [_Word("hello", 0.0, 1.0, 0)]


class ProviderBranchMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_a_supports_instrumental_and_vocal_modes(self) -> None:
        provider = _FakeElevenProvider()
        strategy = ProviderAMusicGenerationStrategy(music_provider=provider)
        plan = _build_plan()
        with tempfile.TemporaryDirectory() as tmp:
            for include_vocals in (False, True):
                request = MusicGenerationRequest(
                    request_id=f"eleven_{include_vocals}",
                    modelspec=MusicModelSpec.EDENN_BASIC,
                    section_plan=plan,
                    options=MusicGenerationOptions(include_vocals=include_vocals, lyrics_language="EN"),
                    output_dir=Path(tmp),
                )
                result = await strategy.generate(request)
                call = provider.calls[-1]
                sections = call["extra"]["composition_plan"]["sections"]
                for section in sections:
                    with self.subTest(include_vocals=include_vocals, section=section["section_name"]):
                        self.assertIn("lines", section)
                        if include_vocals:
                            self.assertTrue(section["lines"])
                        else:
                            self.assertEqual(section["lines"], [])
                self.assertEqual(result.used_modelspec.value, "edenn_basic")

    async def test_provider_b_supports_instrumental_and_vocal_modes(self) -> None:
        provider = _FakeProviderBProvider()
        strategy = ProviderBMusicGenerationStrategy(music_provider=provider)
        plan = _build_plan()
        with tempfile.TemporaryDirectory() as tmp:
            for include_vocals, expected_call in (
                (False, "generate_instrumental_task"),
                (True, "generate_song_task"),
            ):
                request = MusicGenerationRequest(
                    request_id=f"provider_b_{include_vocals}",
                    modelspec=MusicModelSpec.EDENN_ENHANCED,
                    section_plan=plan,
                    options=MusicGenerationOptions(include_vocals=include_vocals),
                    output_dir=Path(tmp),
                )
                result = await strategy.generate(request)
                call_names = [name for name, _ in provider.calls]
                with self.subTest(include_vocals=include_vocals):
                    self.assertIn(expected_call, call_names)
                    self.assertEqual(result.used_modelspec.value, "edenn_enhanced")
                    if include_vocals:
                        self.assertTrue(result.primary.lyrics_timestamps)
                    else:
                        self.assertEqual(result.primary.lyrics_timestamps, [])
                provider.calls.clear()

    async def test_provider_b_trims_enhanced_full_track_tail_and_clips_timestamps(self) -> None:
        class _LongTrackProviderBProvider:
            async def generate_song_task(self, **_kwargs):
                return SimpleNamespace(task_id="song_task")

            async def wait_song_task(self, task_id, timeout_s=600.0, poll_s=5.0):
                return SimpleNamespace(
                    task_id=task_id,
                    trace_id="trace_1",
                    raw={
                        "audio_urls": ["https://example.com/provider_b_primary.mp3"],
                        "lyrics_sections": [
                            {
                                "lines": [
                                    {
                                        "text": "keep clip drop",
                                        "start": 12000,
                                        "end": 16500,
                                        "words": [
                                            {"text": "keep", "start": 12000, "end": 13000},
                                            {"text": "clip", "start": 13500, "end": 16000},
                                            {"text": "drop", "start": 15000, "end": 16500},
                                        ],
                                    }
                                ]
                            }
                        ],
                    },
                )

            async def download_audio(self, _url, dest_path):
                _write_sine_wav(Path(dest_path), duration=20.0)
                return Path(dest_path)

            async def extend_song_from_audio(self, **_kwargs):
                raise AssertionError("20s source should remain longer than the 9s plan after trimming")

        strategy = ProviderBMusicGenerationStrategy(
            music_provider=_LongTrackProviderBProvider()
        )
        with tempfile.TemporaryDirectory() as tmp:
            request = MusicGenerationRequest(
                request_id="provider_b_trim",
                modelspec=MusicModelSpec.EDENN_ENHANCED,
                section_plan=_build_plan(),
                options=MusicGenerationOptions(include_vocals=True, max_variants=1),
                output_dir=Path(tmp),
            )
            result = await strategy.generate(request)

            self.assertIn("_trimmed_tail6s", result.primary.audio_path.stem)
            self.assertAlmostEqual(result.primary.duration_s, 14.0, delta=0.35)
            self.assertEqual(
                [(word.text, word.startS, word.endS) for word in result.primary.lyrics_timestamps],
                [
                    ("keep", 12.0, 13.0),
                    ("clip", 13.5, 14.0),
                ],
            )

    async def test_provider_c_keeps_the_whole_studio_full_track(self) -> None:
        """The studio provider does not tag its tracks, so nothing is chopped.

        Verified against production audio 2026-08-23: studio tracks fade out
        musically with no isolated trailing segment, while enhanced tracks close
        with a 1.95s tag after a silence gap. A defensive chop here deleted six
        seconds of the customer's music — twice observed cutting mid-phrase — so
        the strategy declares appends_provider_tail = False and the shared chop
        stands down. Timestamps stay whole because the audio does.
        """
        class _LongTrackProviderCProvider(_FakeProviderCProvider):
            async def download(self, track, dest_path):
                self.calls.append(("download", {"audio_id": track.audio_id}))
                _write_sine_wav(Path(dest_path), duration=20.0)
                return Path(dest_path)

            async def wait_for_timestamped_lyrics(self, task_id, audio_id, **_kw):
                # 'keep' is inside the 14s trimmed window; 'drop' starts past it.
                return [_Word("keep", 12.0, 13.0, 0), _Word("drop", 15.0, 16.5, 1)]

        strategy = ProviderCMusicGenerationStrategy(music_provider=_LongTrackProviderCProvider())
        with tempfile.TemporaryDirectory() as tmp:
            request = MusicGenerationRequest(
                request_id="provider_c_trim",
                modelspec=MusicModelSpec.EDENN_STUDIO,
                section_plan=_build_plan(),  # 9s plan; 20s - 6s trim = 14s > plan
                options=MusicGenerationOptions(include_vocals=True, max_variants=1),
                output_dir=Path(tmp),
            )
            result = await strategy.generate(request)

            self.assertNotIn("_trimmed_tail", result.primary.audio_path.stem)
            self.assertAlmostEqual(result.primary.duration_s, 20.0, delta=0.35)
            # Both words are inside the untouched 20s track.
            texts = [word.text for word in result.primary.lyrics_timestamps]
            self.assertIn("keep", texts)
            self.assertIn("drop", texts)

    async def test_provider_c_extension_keeps_every_second_it_produced(self) -> None:
        """An extended studio track is delivered whole.

        There is no provider tag to remove on this tier, so neither the initial
        download nor any extension round loses its tail — the customer keeps the
        music they paid the extension for.
        """
        class _ShortThenLongProviderCProvider(_FakeProviderCProvider):
            def __init__(self) -> None:
                super().__init__()
                self.extend_calls = 0
                self.extend_instrumental = []

            async def download(self, track, dest_path):
                # Initial track is short (needs extension); an extension round
                # downloads a long track that must then be tail-trimmed.
                is_extend = "extend" in Path(dest_path).name
                _write_sine_wav(Path(dest_path), duration=22.0 if is_extend else 8.0)
                return Path(dest_path)

            async def extend_and_poll_track(self, audio_id, *, model="V5", timeout_s=180.0, poll_s=10.0,
                                            instrumental=None):
                self.extend_calls += 1
                self.extend_instrumental.append(instrumental)
                return "ext_task", ProviderCTrack(audio_id=audio_id, audio_url=f"https://example.com/{audio_id}.wav"), "base"

            async def wait_for_timestamped_lyrics(self, *a, **k):
                return [_Word("keep", 2.0, 3.0, 0)]

        provider = _ShortThenLongProviderCProvider()
        strategy = ProviderCMusicGenerationStrategy(music_provider=provider)
        with tempfile.TemporaryDirectory() as tmp:
            request = MusicGenerationRequest(
                request_id="provider_c_ext_trim",
                modelspec=MusicModelSpec.EDENN_STUDIO,
                section_plan=_build_plan(),  # 9s target
                options=MusicGenerationOptions(include_vocals=True, max_variants=1),
                output_dir=Path(tmp),
            )
            result = await strategy.generate(request)

            self.assertGreater(provider.extend_calls, 0)  # extension actually ran
            # Instrumental-ness is restated on every round rather than left to the
            # provider, which does not inherit it from the source track. This
            # request is a vocal one, so every round must say so.
            self.assertEqual(provider.extend_instrumental, [False] * provider.extend_calls)
            self.assertNotIn("_trimmed_tail", result.primary.audio_path.stem)
            self.assertAlmostEqual(result.primary.duration_s, 22.0, delta=0.35)

    async def test_provider_c_extension_of_an_instrumental_stays_instrumental(self) -> None:
        """An extended instrumental must not come back singing.

        `defaultParamFlag=False` inherits the source track's settings but NOT
        instrumental-ness: a live extension of an instrumental was recorded by the
        provider as `instrumental: false`, so the round must restate it.
        """

        class _ShortInstrumentalProvider(_FakeProviderCProvider):
            def __init__(self) -> None:
                super().__init__()
                self.extend_instrumental = []

            async def download(self, track, dest_path):
                is_extend = "extend" in Path(dest_path).name
                _write_sine_wav(Path(dest_path), duration=22.0 if is_extend else 8.0)
                return Path(dest_path)

            async def extend_and_poll_track(self, audio_id, *, model="V5", timeout_s=180.0,
                                            poll_s=10.0, instrumental=None):
                self.extend_instrumental.append(instrumental)
                return "ext_task", ProviderCTrack(audio_id=audio_id,
                                             audio_url=f"https://example.com/{audio_id}.wav"), "base"

        provider = _ShortInstrumentalProvider()
        strategy = ProviderCMusicGenerationStrategy(music_provider=provider)
        with tempfile.TemporaryDirectory() as tmp:
            request = MusicGenerationRequest(
                request_id="provider_c_instr_ext",
                modelspec=MusicModelSpec.EDENN_STUDIO,
                section_plan=_build_plan(),
                options=MusicGenerationOptions(include_vocals=False, max_variants=1),
                output_dir=Path(tmp),
            )
            await strategy.generate(request)

            self.assertTrue(provider.extend_instrumental, "extension never ran")
            self.assertEqual(
                provider.extend_instrumental,
                [True] * len(provider.extend_instrumental),
            )

    async def test_provider_b_refreshes_line_level_after_extension(self) -> None:
        """After a ProviderB extension, the delivered LINE-level timestamps come from
        the extended track (via the detailed extend), not the stale pre-extension
        line sheet. Regression guard for line-level lagging word-level + audio
        length after extension (Bug 2)."""
        class _ShortThenExtendedProviderB:
            def __init__(self) -> None:
                self.extend_calls = 0

            async def generate_song_task(self, **_kwargs):
                return SimpleNamespace(task_id="song_task")

            async def wait_song_task(self, task_id, timeout_s=600.0, poll_s=5.0):
                # 8s track (needs extension vs the 9s plan). Pre-extension line
                # sheet says "old line" — this MUST NOT survive into the response.
                return SimpleNamespace(
                    task_id=task_id, trace_id="t",
                    raw={
                        "audio_urls": ["https://example.com/provider_b_primary.mp3"],
                        "lyrics_sections": [{"lines": [{
                            "text": "old line", "start": 1000, "end": 7000,
                            "words": [
                                {"text": "old", "start": 1000, "end": 2000},
                                {"text": "line", "start": 6000, "end": 7000},
                            ],
                        }]}],
                    },
                )

            async def download_audio(self, _url, dest_path):
                _write_sine_wav(Path(dest_path), duration=8.0)  # short -> extend
                return Path(dest_path)

            async def extend_song_from_audio_detailed(self, *, audio_path, prompt, lyrics, output_path, **_kwargs):
                self.extend_calls += 1
                _write_sine_wav(Path(output_path), duration=20.0)  # extended, long
                timestamps = SimpleNamespace(
                    word_level=[_Word("new", 1.0, 2.0, 0), _Word("song", 12.0, 13.0, 1)],
                    line_level=[_Word("new song line", 1.0, 13.0, 0)],
                )
                return Path(output_path), timestamps, lyrics

            async def extend_song_from_audio(self, **_kwargs):
                raise AssertionError("must use extend_song_from_audio_detailed for line-level")

        provider = _ShortThenExtendedProviderB()
        strategy = ProviderBMusicGenerationStrategy(music_provider=provider)
        with tempfile.TemporaryDirectory() as tmp:
            request = MusicGenerationRequest(
                request_id="provider_b_ext_line",
                modelspec=MusicModelSpec.EDENN_ENHANCED,
                section_plan=_build_plan(),  # 9s target
                options=MusicGenerationOptions(include_vocals=True, max_variants=1),
                output_dir=Path(tmp),
            )
            result = await strategy.generate(request)

            self.assertGreater(provider.extend_calls, 0)  # extension actually ran
            line_texts = [w.text for w in result.primary.line_level_lyrics_timestamps]
            word_texts = [w.text for w in result.primary.lyrics_timestamps]
            # Line-level is the EXTENDED sheet, not the stale pre-extension "old line".
            self.assertEqual(line_texts, ["new song line"])
            self.assertNotIn("old line", line_texts)
            self.assertEqual(word_texts, ["new", "song"])
            # Line-level never extends past the delivered (trimmed) audio length.
            max_line_end = max(w.endS for w in result.primary.line_level_lyrics_timestamps)
            self.assertLessEqual(max_line_end, result.primary.duration_s + 0.01)

    async def test_provider_b_pairs_take0_audio_with_take0_alignment(self) -> None:
        """Delivered audio and lyric timestamps must come from the SAME take:
        take 0's URL is downloaded and take 0's alignment is attached even
        when the payload lists another take whose alignment differs."""

        def _take(url: str, start_ms: int) -> dict:
            return {
                "mp3_url": url,
                "lyrics_sections": [
                    {
                        "lines": [
                            {
                                "text": "hello world",
                                "start": start_ms,
                                "end": start_ms + 2000,
                                "words": [
                                    {"text": "hello", "start": start_ms, "end": start_ms + 900},
                                    {"text": "world", "start": start_ms + 1000, "end": start_ms + 2000},
                                ],
                            }
                        ]
                    }
                ],
            }

        class _TwoTakeProvider:
            def __init__(self) -> None:
                self.downloaded: list[str] = []

            async def generate_song_task(self, **_kwargs):
                return SimpleNamespace(task_id="song_task")

            async def wait_song_task(self, task_id, timeout_s=600.0, poll_s=5.0):
                return SimpleNamespace(
                    task_id=task_id,
                    trace_id="trace_2",
                    raw={"choices": [
                        _take("https://example.com/take0.mp3", 3000),
                        _take("https://example.com/take1.mp3", 9000),
                    ]},
                )

            async def download_audio(self, url, dest_path):
                self.downloaded.append(url)
                _write_sine_wav(Path(dest_path), duration=20.0)
                return Path(dest_path)

            async def extend_song_from_audio(self, **_kwargs):
                raise AssertionError("no extension expected")

        provider = _TwoTakeProvider()
        strategy = ProviderBMusicGenerationStrategy(music_provider=provider)
        with tempfile.TemporaryDirectory() as tmp:
            request = MusicGenerationRequest(
                request_id="provider_b_take_pairing",
                modelspec=MusicModelSpec.EDENN_ENHANCED,
                section_plan=_build_plan(),
                options=MusicGenerationOptions(include_vocals=True, max_variants=2),
                output_dir=Path(tmp),
            )
            result = await strategy.generate(request)

        self.assertEqual(provider.downloaded[0], "https://example.com/take0.mp3")
        starts = [word.startS for word in result.primary.lyrics_timestamps]
        self.assertEqual(starts, [3.0, 4.0])  # take 0's alignment, never take 1's

    async def test_provider_b_delivers_without_timestamps_when_take0_has_no_alignment(self) -> None:
        """Another take's timing is worse than no timing: with explicit takes
        present and take 0 unaligned, timestamps are empty (no cross-take
        fallback)."""

        class _UnalignedTake0Provider:
            async def generate_song_task(self, **_kwargs):
                return SimpleNamespace(task_id="song_task")

            async def wait_song_task(self, task_id, timeout_s=600.0, poll_s=5.0):
                return SimpleNamespace(
                    task_id=task_id,
                    trace_id="trace_3",
                    raw={"choices": [
                        {"mp3_url": "https://example.com/take0.mp3"},
                        {
                            "mp3_url": "https://example.com/take1.mp3",
                            "lyrics_sections": [
                                {"lines": [{"text": "wrong take", "start": 9000, "end": 10000,
                                            "words": [{"text": "wrong", "start": 9000, "end": 9500}]}]}
                            ],
                        },
                    ]},
                )

            async def download_audio(self, url, dest_path):
                _write_sine_wav(Path(dest_path), duration=20.0)
                return Path(dest_path)

            async def extend_song_from_audio(self, **_kwargs):
                raise AssertionError("no extension expected")

        strategy = ProviderBMusicGenerationStrategy(music_provider=_UnalignedTake0Provider())
        with tempfile.TemporaryDirectory() as tmp:
            request = MusicGenerationRequest(
                request_id="provider_b_take0_unaligned",
                modelspec=MusicModelSpec.EDENN_ENHANCED,
                section_plan=_build_plan(),
                options=MusicGenerationOptions(include_vocals=True, max_variants=2),
                output_dir=Path(tmp),
            )
            result = await strategy.generate(request)

        self.assertEqual(result.primary.lyrics_timestamps, [])
        self.assertEqual(result.primary.line_level_lyrics_timestamps, [])

    async def test_provider_b_forwards_vocal_clone_inputs_in_vocal_mode(self) -> None:
        provider = _FakeProviderBProvider()
        strategy = ProviderBMusicGenerationStrategy(music_provider=provider)
        plan = _build_plan()
        with tempfile.TemporaryDirectory() as tmp:
            vocal_sample = Path(tmp) / "voice.m4a"
            vocal_sample.write_bytes(b"fake")
            request = MusicGenerationRequest(
                request_id="provider_b_vocal_clone",
                modelspec=MusicModelSpec.EDENN_ENHANCED,
                section_plan=plan,
                options=MusicGenerationOptions(
                    include_vocals=True,
                    vocal_sample_path=vocal_sample,
                ),
                output_dir=Path(tmp),
            )
            result = await strategy.generate(request)

        call_map = {name: payload for name, payload in provider.calls}
        self.assertIn("clone_vocal", call_map)
        self.assertEqual(call_map["generate_song_task"]["vocal_id"], "vocal_321")
        self.assertEqual(result.vocal_id_used, "vocal_321")

    async def test_provider_c_supports_instrumental_and_vocal_modes(self) -> None:
        provider = _FakeProviderCProvider()
        strategy = ProviderCMusicGenerationStrategy(music_provider=provider)
        plan = _build_plan()
        with tempfile.TemporaryDirectory() as tmp:
            for include_vocals in (False, True):
                request = MusicGenerationRequest(
                    request_id=f"provider_c_{include_vocals}",
                    modelspec=MusicModelSpec.EDENN_STUDIO,
                    section_plan=plan,
                    options=MusicGenerationOptions(
                        include_vocals=include_vocals,
                        vocal_gender="female",
                    ),
                    output_dir=Path(tmp),
                )
                result = await strategy.generate(request)
                params = provider.calls[0][1]
                with self.subTest(include_vocals=include_vocals):
                    self.assertEqual(params.custom_mode, include_vocals)
                    self.assertEqual(params.instrumental, not include_vocals)
                    if include_vocals:
                        self.assertTrue(result.primary.lyrics_timestamps)
                        # The sheet the track was asked to sing — an unset value
                        # here surfaced as an empty full_lyrics on studio jobs.
                        self.assertTrue(result.primary.full_lyrics)
                    else:
                        self.assertEqual(result.primary.lyrics_timestamps, [])
                        self.assertIsNone(result.primary.full_lyrics)
                provider.calls.clear()
