"""Building a take out of more than one piece of its own track.

A take has always been ONE window of a longer piece: the matcher picks where to
cut, and the user can move that cut. "Open on the quiet part and let the drop
land on the product shot" needs two places at once, and no amount of moving a
single window gets there.

Free, like every other re-cut — it re-presents music the session already paid
for — and it goes through the same measurement and provenance as a shifted
window, because from everything downstream's point of view it IS just another
window of audio.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from EdennCode.EdennAgent.AgenticAudio.models import (
    MAX_SPLICE_SEGMENTS,
    SCULPT_KIND_SPLICE,
    SCULPT_KINDS,
)
from EdennCode.EdennAgent.AgenticAudio.tools.media import normalize_splice_segments


def _ffmpeg() -> str:
    from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary

    return resolve_ffmpeg_binary()


def _has_ffmpeg() -> bool:
    import shutil

    return bool(shutil.which(_ffmpeg()))


# ---------------------------------------------------------------------------#
# what the user asked to assemble                                             #
# ---------------------------------------------------------------------------#


def test_splice_is_a_sculpt_kind_and_therefore_free() -> None:
    """It re-presents audio already paid for. Putting it under edit_audio would
    have given it a spend class it does not deserve."""
    from EdennCode.EdennAgent.AgenticAudio.models import (
        AGENT_GENERATION_TOOLS,
        AGENT_TOOL_SCULPT_AUDIO,
    )

    assert SCULPT_KIND_SPLICE in SCULPT_KINDS
    assert AGENT_TOOL_SCULPT_AUDIO not in AGENT_GENERATION_TOOLS


def test_the_pieces_keep_the_order_they_were_given() -> None:
    """Going backwards through the track is an arrangement, not a mistake."""
    assert normalize_splice_segments([
        {"start_s": 60, "duration_s": 8},
        {"start_s": 0, "duration_s": 6},
    ], full_duration_s=180) == [(60.0, 8.0), (0.0, 6.0)]


def test_a_piece_may_be_used_twice() -> None:
    """Looping a section under the titles is a normal thing to ask for."""
    assert normalize_splice_segments([
        {"start_s": 12, "duration_s": 4},
        {"start_s": 12, "duration_s": 4},
    ], full_duration_s=180) == [(12.0, 4.0), (12.0, 4.0)]


def test_piece_starts_snap_to_the_beat_but_lengths_do_not() -> None:
    """A join landing mid-beat is heard as an error even when the choice was
    right. A length is dictated by the picture, and quantising it would move
    the very edit being placed."""
    structure = {"beat_times_s": [0.0, 0.5, 12.0, 12.5]}
    assert normalize_splice_segments(
        [{"start_s": 12.12, "duration_s": 6.37}],
        structure=structure, full_duration_s=180,
    ) == [(12.0, 6.37)]


def test_a_piece_past_the_end_of_the_track_is_dropped() -> None:
    assert normalize_splice_segments(
        [{"start_s": 179.9, "duration_s": 5}], full_duration_s=180
    ) == []


def test_a_piece_running_off_the_end_is_shortened_to_what_exists() -> None:
    """The tail would be silence presented as music."""
    assert normalize_splice_segments(
        [{"start_s": 176, "duration_s": 10}], full_duration_s=180
    ) == [(176.0, 4.0)]


def test_a_stutter_is_not_a_section_of_music() -> None:
    assert normalize_splice_segments(
        [{"start_s": 10, "duration_s": 0.2}], full_duration_s=180
    ) == []


def test_an_arrangement_has_a_ceiling() -> None:
    many = [{"start_s": i, "duration_s": 2} for i in range(30)]
    assert len(normalize_splice_segments(many, full_duration_s=180)) <= MAX_SPLICE_SEGMENTS


def test_one_malformed_piece_does_not_lose_the_arrangement() -> None:
    assert normalize_splice_segments([
        {"start_s": "chorus", "duration_s": 4},
        {"start_s": 20, "duration_s": 4},
    ], full_duration_s=180) == [(20.0, 4.0)]


# ---------------------------------------------------------------------------#
# and does the audio actually come out in that order?                         #
# ---------------------------------------------------------------------------#


@pytest.mark.skipif(not _has_ffmpeg(), reason="needs a local ffmpeg")
def test_the_assembled_audio_is_the_material_that_was_asked_for(tmp_path: Path) -> None:
    """Three distinct tones in the source, so the order of the result is
    measurable rather than merely plausible."""
    import numpy as np
    import soundfile as sf

    from EdennCode.Util.MediaUtils.ffmpeg_utils import (
        get_video_duration,
        splice_audio_windows,
    )

    source = tmp_path / "track.wav"
    subprocess.run(
        [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
         "-f", "lavfi", "-i", "sine=frequency=880:duration=10",
         "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1[a]", "-map", "[a]",
         str(source)],
        check=True, capture_output=True,
    )

    out = tmp_path / "arranged.wav"
    # Last third first, then the first third: an order that exists nowhere in
    # the source, so a pass cannot be a coincidence of copying.
    splice_audio_windows(source, out, windows=[(20.0, 4.0), (0.0, 4.0)])

    assert get_video_duration(out) == pytest.approx(8.0, abs=0.2)

    audio, sr = sf.read(out)

    def dominant_hz(start_s: float, end_s: float) -> float:
        window = audio[int(start_s * sr):int(end_s * sr)]
        spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
        return float(np.fft.rfftfreq(len(window), 1 / sr)[int(np.argmax(spectrum))])

    assert dominant_hz(0.5, 3.0) == pytest.approx(880, abs=15), "the piece asked for first"
    assert dominant_hz(5.0, 7.5) == pytest.approx(220, abs=15), "the piece asked for second"


@pytest.mark.skipif(not _has_ffmpeg(), reason="needs a local ffmpeg")
def test_the_seam_is_crossfaded_rather_than_butt_joined(tmp_path: Path) -> None:
    """Two pieces of music meeting at a sample boundary click almost every
    time, and a click is the one artefact that makes an edit sound like a
    mistake instead of a choice."""
    import numpy as np
    import soundfile as sf

    from EdennCode.Util.MediaUtils.ffmpeg_utils import splice_audio_windows

    source = tmp_path / "track.wav"
    subprocess.run(
        [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=200:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=1400:duration=6",
         "-filter_complex", "[0][1]concat=n=2:v=0:a=1[a]", "-map", "[a]",
         str(source)],
        check=True, capture_output=True,
    )
    out = tmp_path / "joined.wav"
    splice_audio_windows(source, out, windows=[(1.0, 3.0), (7.0, 3.0)])

    audio, sr = sf.read(out)
    # The largest sample-to-sample jump anywhere near the join. A hard splice
    # between two unrelated phases produces a step; a crossfade does not.
    seam = audio[int(2.6 * sr):int(3.4 * sr)]
    assert float(np.max(np.abs(np.diff(seam)))) < 0.25, "the join steps audibly"


# --------------------------------------------------------------------------- #
# An arrangement has to survive the next thing the user does                   #
# --------------------------------------------------------------------------- #
#
# A splice assembles pieces of the take's own track into a new order and
# renders that. Before this, only the muxed video kept the result: the window
# was recorded as `source: "user"` with `start_s: 0.0`, which sent every later
# renderer back to the FULL track from the top. So the next volume nudge,
# compose or finalize quietly played something the user had never arranged —
# the same "a free verb betrays the user" failure the window move had, reached
# through the newer verb.


def test_an_arrangement_is_what_later_renders_play() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.media import candidate_music_source

    arranged = {
        "candidate_id": "c1",
        "audio_url": "https://x/cut.mp3",
        "complete_audio_url": "https://x/full.mp3",
        "arranged_audio_url": "https://x/arranged.m4a",
        "window": {"start_s": 0.0, "source": "user",
                   "segments": [{"start_s": 40, "duration_s": 6}]},
    }

    url, start = candidate_music_source(arranged)

    assert url == "https://x/arranged.m4a", (
        "a later render would have played the untouched full track"
    )
    assert start == 0.0


def test_a_moved_window_still_seeks_into_the_full_track() -> None:
    """The arrangement rule must not swallow the plain re-cut it sits beside."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import candidate_music_source

    moved = {
        "candidate_id": "c1",
        "audio_url": "https://x/cut.mp3",
        "complete_audio_url": "https://x/full.mp3",
        "window": {"start_s": 29.8, "source": "user"},
    }

    assert candidate_music_source(moved) == ("https://x/full.mp3", 29.8)


def test_re_cutting_after_an_arrangement_drops_the_arrangement() -> None:
    """Moving the window of a take that was spliced is a NEW decision about
    unarranged audio; keeping the old arrangement would play pieces the user
    just stopped asking for."""
    source = Path(
        "EdennCode/EdennAgent/AgenticAudio/tools/impls.py"
    ).read_text()
    body = source.split("class SculptAudioTool")[1].split("\nclass ")[0]

    assert 'candidate.pop("arranged_audio_url", None)' in body
    assert 'candidate["arranged_audio_url"] = str(arranged)' in body


def test_both_renderers_keep_the_arrangement() -> None:
    """The mix surface switches renderers the moment narration or effects join.
    An arrangement kept by only one of them survives for some sessions and
    vanishes in the rest."""
    media = Path("EdennCode/EdennAgent/AgenticAudio/tools/media.py").read_text()
    dev = Path("EdennCode/EdennAgent/AgenticAudio/design/devserver.py").read_text()

    sculpt = media.split("async def sculpt_window")[1].split("\n    async def ")[0]
    assert "arranged_audio_url" in sculpt or "arranged_audio_local_path" in sculpt

    window = dev.split("async def _dev_window")[1].split("\n    async def ")[0]
    assert "arranged_audio_url" in window


def test_the_mock_carries_the_same_two_fields() -> None:
    """Mock mirrors real: a client-visible field the console cannot see is a
    field the browser suite cannot cover."""
    mock = Path(
        "EdennCode/EdennAgent/AgenticAudio/frontend/mock-backend.js"
    ).read_text()
    sculpt = mock.split("async _sculpt(")[1].split("\n    async ")[0]

    assert "arranged_audio_url" in sculpt
    assert "segments" in sculpt
