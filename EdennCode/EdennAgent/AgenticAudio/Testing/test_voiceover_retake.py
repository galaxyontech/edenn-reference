"""Re-reading one line without re-recording the script.

The renderer has always synthesized line by line — that is how per-line
placement and ducking work at all — and then threw the pieces away, returning
only where each line landed. So "line three sounds rushed" meant re-recording
every line: the user pays again for nine readings they were happy with, waits
again, and gets a subtly different performance of all of them because synthesis
is not deterministic.

The parts existed. They just were not kept.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from EdennCode.EdennAgent.AgenticAudio.tools.narration_render import (
    render_segmented_narration,
)


def _ffmpeg() -> str:
    from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary

    return resolve_ffmpeg_binary()


def _has_ffmpeg() -> bool:
    import shutil

    return bool(shutil.which(_ffmpeg()))


def _tone(path: Path, *, seconds: float = 1.0, hz: int = 300) -> None:
    subprocess.run(
        [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={seconds}",
         "-ar", "44100", str(path)],
        check=True, capture_output=True,
    )


class _CountingSynth:
    """Stands in for the speech provider, and counts what it was asked to say."""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def __call__(self, *, script: str, voice: str, instructions: str,
                       speed: float, out_path: Path) -> None:
        del voice, instructions, speed
        self.spoken.append(script)
        _tone(Path(out_path))


SEGMENTS: list[dict[str, Any]] = [
    {"id": "l1", "text": "First line.", "start_s": 0.0},
    {"id": "l2", "text": "Second line.", "start_s": 3.0},
    {"id": "l3", "text": "Third line.", "start_s": 6.0},
]


@pytest.mark.skipif(not _has_ffmpeg(), reason="needs a local ffmpeg")
def test_every_line_carries_its_own_audio_back(tmp_path: Path) -> None:
    """Without this a retake is impossible: the pieces are made and discarded
    in the same call."""
    synth = _CountingSynth()
    placements = asyncio.run(render_segmented_narration(
        segments=SEGMENTS, synthesize=synth, voice="calm_male",
        workdir=tmp_path / "lines", out_path=tmp_path / "vo.wav",
        video_duration_s=12.0,
    ))

    assert placements is not None and len(placements) == 3
    for placement in placements:
        assert Path(placement["audio_path"]).exists()


@pytest.mark.skipif(not _has_ffmpeg(), reason="needs a local ffmpeg")
def test_a_retake_records_only_the_line_that_was_wrong(tmp_path: Path) -> None:
    """The measurable claim: the provider is asked to say ONE line, not three."""
    first = _CountingSynth()
    placements = asyncio.run(render_segmented_narration(
        segments=SEGMENTS, synthesize=first, voice="calm_male",
        workdir=tmp_path / "take1", out_path=tmp_path / "vo1.wav",
        video_duration_s=12.0,
    ))
    assert len(first.spoken) == 3, "the first pass records everything"

    keep = {
        str(p["id"]): Path(p["audio_path"])
        for p in (placements or []) if p.get("id") != "l3"
    }
    second = _CountingSynth()
    asyncio.run(render_segmented_narration(
        segments=SEGMENTS, synthesize=second, voice="calm_male",
        workdir=tmp_path / "take2", out_path=tmp_path / "vo2.wav",
        video_duration_s=12.0, reuse=keep,
    ))

    assert second.spoken == ["Third line."], (
        f"the whole script was re-recorded to change one line: {second.spoken}"
    )


@pytest.mark.skipif(not _has_ffmpeg(), reason="needs a local ffmpeg")
def test_the_kept_lines_are_the_same_recording_not_a_new_one(tmp_path: Path) -> None:
    """"Kept" has to mean the identical audio. A re-synthesis that happens to
    be skipped is not the same as the take the user approved."""
    synth = _CountingSynth()
    placements = asyncio.run(render_segmented_narration(
        segments=SEGMENTS, synthesize=synth, voice="calm_male",
        workdir=tmp_path / "one", out_path=tmp_path / "a.wav", video_duration_s=12.0,
    ))
    original = {str(p["id"]): Path(p["audio_path"]).read_bytes() for p in placements or []}

    kept = {
        str(p["id"]): Path(p["audio_path"])
        for p in (placements or []) if p.get("id") != "l3"
    }
    again = asyncio.run(render_segmented_narration(
        segments=SEGMENTS, synthesize=_CountingSynth(), voice="calm_male",
        workdir=tmp_path / "two", out_path=tmp_path / "b.wav",
        video_duration_s=12.0, reuse=kept,
    ))

    for placement in again or []:
        if placement["id"] in ("l1", "l2"):
            assert Path(placement["audio_path"]).read_bytes() == original[placement["id"]]


@pytest.mark.skipif(not _has_ffmpeg(), reason="needs a local ffmpeg")
def test_a_line_whose_audio_has_vanished_is_simply_recorded_again(tmp_path: Path) -> None:
    """A stale path is not a reason to fail a render."""
    synth = _CountingSynth()
    asyncio.run(render_segmented_narration(
        segments=SEGMENTS, synthesize=synth, voice="calm_male",
        workdir=tmp_path / "w", out_path=tmp_path / "vo.wav", video_duration_s=12.0,
        reuse={"l1": tmp_path / "gone.wav"},
    ))
    assert "First line." in synth.spoken


def test_the_tool_never_keeps_a_line_whose_words_changed() -> None:
    """Editing the text is exactly when a new recording is owed."""
    source = Path("EdennCode/EdennAgent/AgenticAudio/tools/impls.py").read_text()
    retake = source.split("retake_id = str(args.get")[1].split("events = [")[0]

    assert "rendered_text" in retake, "the kept line is compared against what was read"
    assert 'seg_id == retake_id' in retake, "the named line is never kept"


def test_the_agent_is_told_the_retake_exists() -> None:
    from EdennCode.EdennAgent.AgenticAudio.agent.prompts import SYSTEM_PROMPT

    assert "ONE LINE CAN BE RE-READ ON ITS OWN" in SYSTEM_PROMPT
    assert "segment_id" in SYSTEM_PROMPT
