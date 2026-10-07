"""Where the beats are, and where the piece changes.

Every editing verb here speaks in seconds, which means the user supplies the
seconds and the agent guesses. Music does not have seconds in it — it has beats
and sections — so "make the chorus hit at 0:12" and "cut on the beat" were not
requests this could act on, only paraphrase.

The measurements come from the analyzer the music-matching stage already uses,
rather than a second beat detector living beside the first.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from EdennCode.EdennAgent.AgenticAudio.tools.media import (
    MAX_BEATS_KEPT,
    music_structure,
    snap_to_beat,
)


def _ffmpeg() -> str:
    from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary

    return resolve_ffmpeg_binary()


def _has_ffmpeg() -> bool:
    import shutil

    return bool(shutil.which(_ffmpeg()))


# ---------------------------------------------------------------------------#
# snapping                                                                    #
# ---------------------------------------------------------------------------#


def test_a_moment_close_to_a_beat_lands_on_it() -> None:
    assert snap_to_beat(12.1, {"beat_times_s": [11.0, 12.0, 13.0]}) == 12.0


def test_a_moment_far_from_any_beat_is_left_where_the_user_put_it() -> None:
    """Timid on purpose. A cut dragged half a bar to reach a beat is not the
    moment they asked for any more."""
    assert snap_to_beat(12.5, {"beat_times_s": [11.0, 14.0]}) == 12.5


def test_music_with_no_grid_snaps_to_nothing() -> None:
    assert snap_to_beat(12.34, {}) == 12.34
    assert snap_to_beat(12.34, None) == 12.34


# ---------------------------------------------------------------------------#
# honesty about not knowing                                                   #
# ---------------------------------------------------------------------------#


def test_a_track_that_cannot_be_read_simply_has_no_structure() -> None:
    """Saying nothing beats a confident grid that is wrong."""
    assert music_structure(None) == {}
    assert music_structure(Path("/nonexistent/track.mp3")) == {}


# ---------------------------------------------------------------------------#
# against audio with a known answer                                           #
# ---------------------------------------------------------------------------#


@pytest.mark.skipif(not _has_ffmpeg(), reason="needs a local ffmpeg")
def test_the_beat_grid_matches_a_track_whose_tempo_we_chose(tmp_path: Path) -> None:
    """A 120 BPM click: a beat every half second, and that is checkable."""
    import statistics

    track = tmp_path / "click.wav"
    subprocess.run(
        [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=1000:duration=20:sample_rate=44100",
         "-af", r"volume='0.6*lt(mod(t\,0.5)\,0.05)':eval=frame", str(track)],
        check=True, capture_output=True,
    )

    structure = music_structure(track)
    beats = structure.get("beat_times_s") or []
    assert len(beats) > 10, "a steady click should be findable"

    # Evaluated the way beat tracking is actually evaluated: up to an OCTAVE.
    # Trackers routinely read 120 as 60, and this one does on sparse material.
    # What matters for cutting is that the grid it reports consists of REAL
    # beats — every other one is still every other real one — so the period has
    # to be the true period times a simple whole number, and each reported beat
    # has to sit on the true grid.
    gaps = [round(b - a, 3) for a, b in zip(beats, beats[1:])]
    period = statistics.median(gaps)
    ratio = period / 0.5
    assert abs(ratio - round(ratio)) < 0.12, f"{period}s is not a multiple of the beat"

    for beat in beats[1:-1]:
        offset = beat % 0.5
        assert min(offset, 0.5 - offset) < 0.09, f"beat at {beat}s is off the grid"

    assert structure["downbeats_s"] == beats[::4], "a downbeat every fourth beat"
    assert structure["tempo_bpm"] > 0


@pytest.mark.skipif(not _has_ffmpeg(), reason="needs a local ffmpeg")
def test_the_section_map_finds_a_shape_we_built_on_purpose(tmp_path: Path) -> None:
    """Quiet for ten seconds, louder for ten, loudest for ten. The map should
    say roughly that and should not invent a fourth thing."""
    track = tmp_path / "arc.wav"
    subprocess.run(
        [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=30:sample_rate=44100",
         "-af", r"volume='if(lt(t\,10)\,0.08\,if(lt(t\,20)\,0.35\,1.0))':eval=frame",
         str(track)],
        check=True, capture_output=True,
    )

    sections = music_structure(track).get("sections") or []
    assert 2 <= len(sections) <= 4, f"a three-stage arc, not noise: {sections}"

    # Monotonically louder, and contiguous with no gaps or overlaps.
    levels = [s["level_db"] for s in sections]
    assert levels == sorted(levels), f"the arc rises: {levels}"
    for earlier, later in zip(sections, sections[1:]):
        assert earlier["end_s"] == later["start_s"], "the map has no holes in it"
    assert sections[0]["start_s"] == 0.0
    assert sections[0]["label"] == "quiet"
    assert sections[-1]["label"] == "full"


@pytest.mark.skipif(not _has_ffmpeg(), reason="needs a local ffmpeg")
def test_every_number_is_rounded_because_this_lands_in_session_state(
    tmp_path: Path,
) -> None:
    """It is compared by equality on a 2.5s poll; an unrounded float would
    rewrite the session forever."""
    track = tmp_path / "steady.wav"
    subprocess.run(
        [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=330:duration=12:sample_rate=44100",
         str(track)],
        check=True, capture_output=True,
    )

    first = music_structure(track)
    assert first == music_structure(track), "two reads of one file must agree"

    for beat in (first.get("beat_times_s") or []):
        assert round(beat, 2) == beat
    for section in (first.get("sections") or []):
        assert round(section["level_db"], 1) == section["level_db"]
    assert len(first.get("beat_times_s") or []) <= MAX_BEATS_KEPT
