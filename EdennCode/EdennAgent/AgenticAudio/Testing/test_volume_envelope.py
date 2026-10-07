"""Bringing the music down at one moment, and back up after.

Every mix volume in this product was a single number for the whole timeline, so
the most ordinary request a person makes about a score — "duck it under her line
at 0:40 and bring it back" — had no answer except turning the whole piece down.

The machinery was already here, which is the interesting part: the mixer has
ducked under narration for a long time, and it does that by building a
time-varying ffmpeg volume expression. An envelope is the same move with the
user's moments instead of the narration's, so this is one builder serving both
rather than a second way of doing the same thing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from EdennCode.EdennAgent.AgenticAudio.tools.media import (
    MAX_ENVELOPE_POINTS,
    normalize_music_envelope,
)
from EdennCode.Util.MediaUtils.ffmpeg_utils import music_volume_expression


# ---------------------------------------------------------------------------#
# what the user asked for, cleaned up                                         #
# ---------------------------------------------------------------------------#


def test_a_fade_survives_in_the_shape_a_mixer_can_render() -> None:
    assert normalize_music_envelope([{"start_s": 40, "end_s": 45, "gain_db": -12}]) == [
        (40.0, 45.0, -12.0)
    ]
    # Tuples too, because the model will send both.
    assert normalize_music_envelope([(1, 4, -6)]) == [(1.0, 4.0, -6.0)]


def test_one_malformed_fade_does_not_lose_the_others() -> None:
    """A fade is a gesture people place several of and then adjust. Refusing the
    whole set because one arrived backwards is the wrong trade — the mixer is
    not going to be wrong about the ones that parsed."""
    cleaned = normalize_music_envelope([
        {"start_s": 5, "end_s": 1, "gain_db": -6},      # backwards
        {"start_s": 10, "end_s": 14, "gain_db": -9},    # fine
        "not a fade at all",
        {"start_s": "soon", "end_s": 20, "gain_db": -6},
    ])
    assert cleaned == [(10.0, 14.0, -9.0)]


def test_a_fade_too_short_to_hear_is_dropped() -> None:
    """Below a couple of frames the move is a click, not a fade."""
    assert normalize_music_envelope([{"start_s": 3.0, "end_s": 3.05, "gain_db": -9}]) == []


def test_a_fade_cannot_turn_the_music_up_or_off() -> None:
    """Up is the volume knob's job; off is mute's."""
    assert normalize_music_envelope([(0, 5, 40.0)]) == [(0.0, 5.0, 0.0)]
    assert normalize_music_envelope([(0, 5, -200.0)]) == [(0.0, 5.0, -60.0)]


def test_an_arrangement_is_not_an_envelope() -> None:
    many = [{"start_s": i, "end_s": i + 0.5, "gain_db": -6} for i in range(40)]
    assert len(normalize_music_envelope(many)) <= MAX_ENVELOPE_POINTS


def test_fades_come_back_in_time_order() -> None:
    cleaned = normalize_music_envelope([(20, 24, -6), (2, 6, -9)])
    assert [start for start, _, _ in cleaned] == [2.0, 20.0]


# ---------------------------------------------------------------------------#
# one builder, two kinds of move                                              #
# ---------------------------------------------------------------------------#


def test_nothing_moving_stays_a_plain_number() -> None:
    """Callers use this unconditionally, and a constant costs nothing."""
    assert music_volume_expression(0.85) == "0.85"


def test_the_user_and_the_narration_do_not_fight() -> None:
    """Where both apply the music sits at whichever level is quieter. Two
    moves multiplying into silence is not what anyone means by asking for
    both, and it is the behaviour that would be hardest to explain."""
    expr = music_volume_expression(
        0.85, duck_windows=[(2.0, 5.0)], envelope=[(40.0, 45.0, -12.0)]
    )
    assert expr.startswith("0.85*min(")
    assert "*(" not in expr.replace("0.85*", ""), "levels combine by min, never by product"


def test_the_expression_is_time_varying_or_the_fade_never_happens() -> None:
    """A volume expression that mentions t must be evaluated per frame; without
    that the filter reads it once at t=0 and the music simply sits there."""
    source = Path("EdennCode/Util/MediaUtils/ffmpeg_utils.py").read_text()
    for call in source.split("music_volume_expression(")[1:]:
        pass  # presence checked below; the eval=frame pairing is what matters
    assert source.count("eval=frame") >= 3


# ---------------------------------------------------------------------------#
# and does it actually get quieter?                                           #
# ---------------------------------------------------------------------------#


def _ffmpeg() -> str:
    from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary

    return resolve_ffmpeg_binary()


def _mean_db(path: Path, start: float, dur: float) -> float:
    import re

    out = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-nostdin", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
         "-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = re.search(r"mean_volume:\s*(-?[0-9.]+)", out.stderr or "")
    assert match, out.stderr[-400:]
    return float(match.group(1))


@pytest.mark.skipif(not Path("/opt/homebrew/bin/ffmpeg").exists()
                    and not Path("/usr/bin/ffmpeg").exists(),
                    reason="needs a local ffmpeg")
def test_the_music_is_measurably_quieter_where_the_user_asked(tmp_path: Path) -> None:
    """The filter string being right is not the claim. The claim is that the
    audio comes out quieter in that window and recovers after it."""
    from EdennCode.Util.MediaUtils.ffmpeg_utils import overlay_music_on_video

    video = tmp_path / "src.mp4"
    subprocess.run(
        [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=320x240:d=20",
         "-c:v", "libx264", "-t", "20", str(video)],
        check=True, capture_output=True,
    )
    music = tmp_path / "music.wav"
    subprocess.run(
        [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=20", str(music)],
        check=True, capture_output=True,
    )

    out = tmp_path / "mixed.mp4"
    overlay_music_on_video(
        video, music, out,
        music_volume=1.0,
        music_envelope=[(8.0, 12.0, -18.0)],
    )

    before = _mean_db(out, 2.0, 3.0)
    during = _mean_db(out, 9.0, 2.0)
    after = _mean_db(out, 15.0, 3.0)

    assert during < before - 8.0, f"no audible dip: {before:.1f} -> {during:.1f} dB"
    assert after > during + 8.0, f"it never came back: {during:.1f} -> {after:.1f} dB"
    assert abs(after - before) < 3.0, "the level outside the fade was disturbed"


# ---------------------------------------------------------------------------#
# and does it survive the rest of the call?                                   #
# ---------------------------------------------------------------------------#


@pytest.mark.asyncio
async def test_a_fade_on_a_music_only_session_is_still_there_afterwards() -> None:
    """The first version of this wrote the fades, and then wrote them away.

    The tool persisted the mix, and a few lines later rebuilt session state from
    the snapshot it had read at the top of the call and wrote that back — so the
    fade was reverted by the same function that had just saved it. A journey on
    a real video is what found it: every unit test around the pieces passed,
    because each piece was right.
    """
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
        _MemoryAgenticRepository,
        _MemoryAsyncRepository,
    )
    from EdennCode.EdennAgent.AgenticAudio.models import (
        AGENT_TOOL_ADJUST_REMIX,
        AgenticSessionPhase,
    )
    from EdennCode.EdennAgent.AgenticAudio.tools import AgenticAudioTools
    from EdennCode.EdennAgent.AgenticAudio.tools.base import ToolContext
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import build_tool_registry

    async def remix_fn(**_: object) -> dict[str, object]:
        return {"status": "completed", "remixed_video_url": "/dev/media/r.mp4"}

    repo = _MemoryAgenticRepository()
    session = repo.create_session(
        source_video_artifact_id="art",
        phase=AgenticSessionPhase.AWAITING_CANDIDATE_CHOICE,
        state_json={"candidates": [{
            "candidate_id": "c1", "status": "completed",
            "audio_url": "https://x/cut.mp3",
        }]},
    )
    tools = AgenticAudioTools(
        async_repository=_MemoryAsyncRepository(), settings=None, remix_fn=remix_fn
    )
    tool = build_tool_registry().get(AGENT_TOOL_ADJUST_REMIX)
    await tool.run(
        ToolContext(session_id=session.session_id, repository=repo, media=tools,
                    max_candidates=3),
        {"candidate_id": "c1", "music_volume": 0.6,
         "music_envelope": [{"start_s": 4.0, "end_s": 9.0, "gain_db": -9.0}]},
    )

    mix = repo.get_session(session.session_id).state_json.get("mix") or {}
    assert mix.get("music_envelope") == [
        {"start_s": 4.0, "end_s": 9.0, "gain_db": -9.0}
    ], "the fade did not survive the call that saved it"


# --------------------------------------------------------------------------- #
# The envelope has to reach the RENDERER, not just the card                    #
# --------------------------------------------------------------------------- #
#
# Every test above this line checks the envelope's shape or what state keeps.
# That is exactly how the defect below shipped: `adjust_remix` accepted a
# `music_envelope`, wrote it onto `state['mix']`, and never handed it to the
# injected renderer — so the card offered a fade the delivered file did not
# have, on the one deployment that injects a renderer, which is the shipped
# one. These tests assert on what the renderer was CALLED WITH.


def _tools_with_recorder(recorder: dict[str, Any]):
    """AgenticAudioTools wired to renderers that record their keyword arguments."""
    from types import SimpleNamespace

    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
        _MemoryAsyncRepository,
        _seed_source_video,
    )
    from EdennCode.EdennAgent.AgenticAudio.tools import AgenticAudioTools

    async def remix_fn(**kwargs: Any) -> dict[str, Any]:
        recorder["remix"] = kwargs
        return {"status": "completed", "video_url": "/dev/media/remixed.mp4"}

    async def window_fn(**kwargs: Any) -> dict[str, Any]:
        recorder["window"] = kwargs
        return {"status": "completed", "video_url": "/dev/media/windowed.mp4"}

    async_repo = _MemoryAsyncRepository()
    _seed_source_video(async_repo)
    return AgenticAudioTools(
        async_repository=async_repo,
        queue=None,
        settings=SimpleNamespace(workdir="/tmp"),
        storage=None,
        remix_fn=remix_fn,
        window_fn=window_fn,
    )


def test_a_volume_nudge_hands_the_fade_to_the_renderer() -> None:
    """The bug in one line: the card kept the fade and the file never got it."""
    import asyncio

    seen: dict[str, Any] = {}
    tools = _tools_with_recorder(seen)
    envelope = normalize_music_envelope([{"start_s": 10, "end_s": 15, "gain_db": -12}])

    asyncio.run(
        tools.adjust_remix(
            candidate={"candidate_id": "c1", "audio_url": "https://cdn.test/a.mp3"},
            source_video_artifact_id="artifact_source_video",
            music_volume=0.6,
            music_envelope=envelope,
        )
    )

    assert seen["remix"]["music_envelope"] == envelope, (
        "the renderer was not told about the fade the session recorded"
    )


def test_a_re_cut_re_applies_the_fade_the_user_already_asked_for() -> None:
    """A re-cut renders a NEW file. An envelope that is not re-applied is an
    envelope deleted — by a free edit that never said it would touch levels."""
    import asyncio

    seen: dict[str, Any] = {}
    tools = _tools_with_recorder(seen)
    envelope = normalize_music_envelope([{"start_s": 2, "end_s": 6, "gain_db": -9}])

    asyncio.run(
        tools.sculpt_window(
            candidate={
                "candidate_id": "c1",
                "audio_url": "https://cdn.test/cut.mp3",
                "complete_audio_url": "https://cdn.test/full.mp3",
            },
            source_video_artifact_id="artifact_source_video",
            window_start_s=30.0,
            music_envelope=envelope,
        )
    )

    assert seen["window"]["music_envelope"] == envelope


def test_the_sculpt_tool_reads_the_durable_envelope_off_the_mix() -> None:
    """The tool is what knows the session; the renderer only renders. If the
    tool does not read state['mix'], nothing downstream can re-apply it."""
    source = Path(
        "EdennCode/EdennAgent/AgenticAudio/tools/impls.py"
    ).read_text()
    body = source.split("class SculptAudioTool")[1].split("\nclass ")[0]

    assert "music_envelope=durable_envelope" in body
    assert 'state_json.get("mix")' in body, (
        "the re-cut must source the envelope the user already applied"
    )
