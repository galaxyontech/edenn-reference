"""Lyric timestamps must describe the SAME take as the downloaded audio.

The song task returns n takes of one lyric sheet, each with its own alignment.
The task-level extractors return the first take that carries lyrics_sections —
not necessarily take 0, whose audio we download. That mismatch shipped word
timestamps drifting seconds off the delivered track (verified acoustically on
2026-07-17). These tests pin the per-choice pairing contract.
"""
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_payload import (
    extract_choice_audio_url,
    extract_timestamped_lyrics_for_choice,
    extract_timestamped_words,
)


def _choice(url: str, start_ms: int) -> dict:
    return {
        "mp3_url": url,
        "lyrics_sections": [
            {
                "lines": [
                    {
                        "text": "Golden leaves in the morning light",
                        "start": start_ms,
                        "end": start_ms + 4000,
                        "words": [
                            {"text": "Golden", "start": start_ms, "end": start_ms + 800},
                            {"text": "leaves", "start": start_ms + 800, "end": start_ms + 1600},
                        ],
                    }
                ]
            }
        ],
    }


def test_choice_extraction_pairs_timestamps_with_the_same_take():
    payload = {"choices": [_choice("https://cdn/take0.mp3", 10_000),
                           _choice("https://cdn/take1.mp3", 19_000)]}
    take0 = extract_timestamped_lyrics_for_choice(payload, 0)
    take1 = extract_timestamped_lyrics_for_choice(payload, 1)
    assert take0.word_level[0].startS == 10.0
    assert take1.word_level[0].startS == 19.0


def test_task_level_extractor_can_return_the_wrong_take():
    """Documents WHY the client must use per-choice extraction: when take 0 has
    no alignment, the task-level extractor silently returns take 1's timing."""
    no_alignment = {"mp3_url": "https://cdn/take0.mp3"}
    payload = {"choices": [no_alignment, _choice("https://cdn/take1.mp3", 19_000)]}
    words = extract_timestamped_words(payload)
    assert words and words[0].startS == 19.0  # take 1's timing, take 0's audio


def test_choice_extraction_empty_when_take_has_no_alignment():
    no_alignment = {"mp3_url": "https://cdn/take0.mp3"}
    payload = {"choices": [no_alignment, _choice("https://cdn/take1.mp3", 19_000)]}
    take0 = extract_timestamped_lyrics_for_choice(payload, 0)
    assert not take0.word_level and not take0.line_level


def test_choice_audio_url_pins_the_same_take():
    """The generic URL walk does not guarantee take order (LIFO traversal can
    surface the LAST choice first); per-choice extraction is deterministic."""
    payload = {"choices": [_choice("https://cdn/take0.mp3", 10_000),
                           _choice("https://cdn/take1.mp3", 19_000)]}
    assert extract_choice_audio_url(payload, 0) == "https://cdn/take0.mp3"
    assert extract_choice_audio_url(payload, 1) == "https://cdn/take1.mp3"
    assert extract_choice_audio_url(payload, 2) is None
    assert extract_choice_audio_url({}, 0) is None
