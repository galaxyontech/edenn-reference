import unittest

from EdennCode.MusicGenerationCore.models import (
    MusicSection,
    NarrativeCue,
    SectionPlan,
    normalize_modelspec,
)


class MusicGenerationCoreModelTests(unittest.TestCase):
    def test_normalize_modelspec_maps_aliases(self) -> None:
        self.assertEqual(normalize_modelspec("provider_a").value, "edenn_basic")
        self.assertEqual(normalize_modelspec("provider_b").value, "edenn_enhanced")
        self.assertEqual(normalize_modelspec("provider_c").value, "edenn_studio")
        self.assertEqual(normalize_modelspec("unknown").value, "edenn_basic")

    def test_section_plan_emits_legacy_metadata(self) -> None:
        plan = SectionPlan(
            summary="A three-shot lifestyle story",
            total_duration_s=9.0,
            overall_mood="uplifting",
            target_bpm=112.0,
            primary_instruments=["piano", "drums"],
            cues=[
                NarrativeCue(
                    cue_id="image_1",
                    label="Frame 1",
                    role="setup",
                    target_duration_s=3.0,
                    emotion="warm",
                    description="Open on the hero product",
                    transition_hint="Fade in",
                    source_refs=["image:1"],
                )
            ],
            sections=[
                MusicSection(
                    section_id="intro",
                    label="Intro",
                    target_duration_s=3.0,
                    objective="Set the scene",
                    energy_start=0.2,
                    energy_end=0.4,
                    image_indices=[1],
                    cue_ids=["image_1"],
                    instrumentation_focus=["piano"],
                    lyric_lines=["Open up the moment"],
                )
            ],
            music_prompt_summary="Warm uplifting piano pop",
        )

        legacy = plan.legacy_metadata()
        self.assertEqual(legacy["recommended_mood"], "uplifting")
        self.assertEqual(legacy["tempo_bpm"], 112.0)
        self.assertEqual(legacy["music_prompt"], "Warm uplifting piano pop")
        self.assertIn("Intro: Set the scene", legacy["music_structure_notes"])
        self.assertEqual(len(legacy["sequence_plan"]), 1)


class StripSectionHeaderWordsTests(unittest.TestCase):
    """Section headers leak into provider word timelines split across tokens with
    brackets lost — '[Peak & Resolve]' arrives as '[Peak', '&', 'Resolve'.
    Regression for the live job where '[Warm' / 'Build' surfaced as sung words.
    """

    @staticmethod
    def _w(text, start, end, i=0):
        from EdennCode.MusicGenerationCore.models import TimestampedWord
        return TimestampedWord(text=text, startS=start, endS=end, i=i)

    def test_strips_split_multiword_headers_using_full_lyrics(self) -> None:
        from EdennCode.MusicGenerationCore.audio import strip_section_header_words
        full = "[Intro]\nLeaves fall\n\n[Warm Build]\nRaise a glass\n\n[Peak & Resolve]\nAbove the clouds"
        words = [
            self._w("Leaves", 8.4, 9.0), self._w("fall", 9.0, 9.5),
            self._w("[Warm", 15.5, 15.6), self._w("Build", 15.7, 15.8),
            self._w("Raise", 17.0, 17.4), self._w("a", 17.4, 17.5), self._w("glass", 17.5, 18.0),
            self._w("[Peak", 23.8, 25.4), self._w("&", 25.4, 25.5), self._w("Resolve", 25.5, 25.6),
            self._w("Above", 33.8, 34.2),
        ]
        out = strip_section_header_words(words, full_lyrics=full)
        self.assertEqual(
            [w.text for w in out],
            ["Leaves", "fall", "Raise", "a", "glass", "Above"],
        )

    def test_lyric_words_matching_header_text_survive_without_bracket(self) -> None:
        from EdennCode.MusicGenerationCore.audio import strip_section_header_words
        # A sung word that HAPPENS to equal a header word must not be removed —
        # a run only starts on bracket evidence.
        full = "[Build]\nBuild me up"
        words = [
            self._w("[Build]", 0.0, 0.1),
            self._w("Build", 1.0, 1.4), self._w("me", 1.4, 1.6), self._w("up", 1.6, 1.9),
        ]
        out = strip_section_header_words(words, full_lyrics=full)
        self.assertEqual([w.text for w in out], ["Build", "me", "up"])

    def test_without_lyrics_text_only_bracketed_tokens_drop(self) -> None:
        from EdennCode.MusicGenerationCore.audio import strip_section_header_words
        words = [self._w("[Verse]", 0.0, 0.1), self._w("hello", 1.0, 1.5)]
        out = strip_section_header_words(words, full_lyrics=None)
        self.assertEqual([w.text for w in out], ["hello"])

    def test_empty_and_clean_lists_pass_through(self) -> None:
        from EdennCode.MusicGenerationCore.audio import strip_section_header_words
        self.assertEqual(strip_section_header_words([], full_lyrics="[Intro]\nx"), [])
        clean = [self._w("la", 0.0, 0.5)]
        self.assertEqual(strip_section_header_words(clean, full_lyrics=None), clean)

    def test_glued_complete_tag_keeps_the_lyric_remainder(self) -> None:
        # Video/studio shape: the tag and the first lyric word arrive fused in ONE
        # token — "[Verse]\nSnowflakes ". The tag goes; the word (and time) stays.
        from EdennCode.MusicGenerationCore.audio import strip_section_header_words
        words = [
            self._w("[Verse]\nSnowflakes ", 14.1, 15.9),
            self._w("fall", 15.9, 16.3),
            self._w("[Chorus]\nOh ", 33.4, 33.8),
            self._w("[Bridge]", 40.0, 40.1),  # tag only -> dropped
        ]
        out = strip_section_header_words(words, full_lyrics=None)
        self.assertEqual([w.text for w in out], ["Snowflakes", "fall", "Oh"])
        self.assertEqual(out[0].startS, 14.1)
