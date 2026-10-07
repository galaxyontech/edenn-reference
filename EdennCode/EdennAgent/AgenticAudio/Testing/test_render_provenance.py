"""Which measurements are allowed to describe a take, and when they stop being.

A take is re-presented for free — a re-cut window today, an envelope or a splice
tomorrow — and every re-presentation leaves the previous render's measurements
describing audio nobody can hear any more. Two writers touch those fields: the
tool that re-presents, and the hydrate projection running on the 2.5s poll.

Deleting the stale report in the tool was not enough, and could never have been:
hydration's whole contract is "re-derive everything and converge", so on the very
next tick it rebuilt the report from the ORIGINAL render's measurements and the
fault the user had just sculpted away came back — for the rest of the session,
in the state summary, in the comparison table, and in the prompt's fault-to-fix
mapping that tells the agent to go and fix it again.

So measurements are signed with the generation of audio they were taken from,
and judgement reads only measurements stamped with the generation the candidate
presents now. Everything else is honestly unmeasured.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from EdennCode.EdennAgent.AgenticAudio.tools.media import (
    RENDER_EPOCH_KEY,
    TAKE_SIGNALS_KEY,
    candidate_render_epoch,
    comparison_still_describes,
    stamped_take_signals,
    take_signals_for_candidate,
)


VIDEO_20S: dict[str, Any] = {"duration_s": 20.0}

#: A take that ends on two seconds of silence — the fault a user sculpts to escape.
DEAD_TAIL_SIGNALS: dict[str, Any] = {
    "cut_duration_s": 20.0,
    "leading_silence_s": 0.0,
    "trailing_silence_s": 2.0,
    "full_duration_s": 180.0,
}

#: The same take re-cut to a window that fills the video cleanly.
CLEAN_SIGNALS: dict[str, Any] = {
    "cut_duration_s": 20.0,
    "leading_silence_s": 0.0,
    "trailing_silence_s": 0.1,
    "full_duration_s": 180.0,
}


def _build_tools(async_repo: Any, window_fn: Any = None) -> Any:
    """AgenticAudioTools with just enough wired for hydrate and the window path."""

    from EdennCode.EdennAgent.AgenticAudio.tools import AgenticAudioTools

    return AgenticAudioTools(
        async_repository=async_repo,
        settings=SimpleNamespace(workdir="/tmp"),
        window_fn=window_fn,
    )


def _completed_job(async_repo: Any, job_id: str, result: dict[str, Any]) -> None:
    """Record a finished music job carrying the render's own measurements."""

    async_repo.create_job(job_id=job_id, job_type="video_music",
                          request_json={"modelspec": "edenn_studio"})
    async_repo.update_job_status(job_id, status="completed", result_json=result)


# ---------------------------------------------------------------------------#
# resolving which measurements apply                                          #
# ---------------------------------------------------------------------------#


def test_a_fresh_take_is_described_by_the_render_that_made_it() -> None:
    candidate = {"candidate_id": "c1"}
    result = {TAKE_SIGNALS_KEY: DEAD_TAIL_SIGNALS}

    assert take_signals_for_candidate(candidate, result) == DEAD_TAIL_SIGNALS
    assert candidate_render_epoch(candidate) == 0


def test_measurements_the_candidate_took_itself_win_when_they_match() -> None:
    candidate = {
        "candidate_id": "c1",
        RENDER_EPOCH_KEY: 1,
        TAKE_SIGNALS_KEY: stamped_take_signals(CLEAN_SIGNALS, render_epoch=1),
    }
    result = {TAKE_SIGNALS_KEY: DEAD_TAIL_SIGNALS}

    resolved = take_signals_for_candidate(candidate, result)
    assert resolved is not None
    assert resolved["trailing_silence_s"] == 0.1, "the re-cut's own numbers, not the render's"


def test_a_stamp_from_an_older_generation_describes_nothing() -> None:
    """The audio moved on after the measuring. A stale report is worse than none."""
    candidate = {
        "candidate_id": "c1",
        RENDER_EPOCH_KEY: 2,
        TAKE_SIGNALS_KEY: stamped_take_signals(CLEAN_SIGNALS, render_epoch=1),
    }
    assert take_signals_for_candidate(candidate, {TAKE_SIGNALS_KEY: DEAD_TAIL_SIGNALS}) is None


def test_a_re_presented_take_never_falls_back_to_the_renders_numbers() -> None:
    """The re-cut could not be measured. Falling back to the render's
    measurements is precisely the resurrection this mechanism exists to stop."""
    candidate = {"candidate_id": "c1", RENDER_EPOCH_KEY: 1}
    assert take_signals_for_candidate(candidate, {TAKE_SIGNALS_KEY: DEAD_TAIL_SIGNALS}) is None


def test_a_take_re_cut_before_provenance_existed_is_treated_as_unmeasured() -> None:
    """Sessions already on disk have candidates that were sculpted back when a
    re-cut only popped the report: no epoch, and the render's measurements still
    on the job. Without this guard every one of them resurrects its old fault
    once, on the first poll after this ships."""
    candidate = {
        "candidate_id": "c1",
        "window": {"start_s": 96.0, "source": "user"},
    }
    assert take_signals_for_candidate(candidate, {TAKE_SIGNALS_KEY: DEAD_TAIL_SIGNALS}) is None

    # The matcher's own window is not a re-cut and must keep its report.
    matched = {"candidate_id": "c2", "window": {"start_s": 96.0, "source": "matcher"}}
    assert take_signals_for_candidate(matched, {TAKE_SIGNALS_KEY: DEAD_TAIL_SIGNALS})


def test_stamping_is_additive_so_the_measurements_stay_where_readers_look() -> None:
    """The card and the report layer read measurement keys directly; a wrapper
    would move every one of them."""
    stamped = stamped_take_signals(CLEAN_SIGNALS, render_epoch=3)

    assert stamped["cut_duration_s"] == 20.0
    assert stamped[RENDER_EPOCH_KEY] == 3
    assert CLEAN_SIGNALS.get(RENDER_EPOCH_KEY) is None, "the caller's dict is not mutated"
    assert stamped_take_signals({}, render_epoch=3) == {}


def test_a_garbled_epoch_is_read_as_the_original_render() -> None:
    assert candidate_render_epoch({RENDER_EPOCH_KEY: "not a number"}) == 0
    assert candidate_render_epoch({RENDER_EPOCH_KEY: -4}) == 0
    assert candidate_render_epoch(None) == 0


# ---------------------------------------------------------------------------#
# the regression this whole mechanism exists for                              #
# ---------------------------------------------------------------------------#


def test_a_sculpted_away_fault_stays_away_across_repeated_hydration() -> None:
    """THE test. Hydrate runs every 2.5 seconds for the life of the session, so
    "fixed" has to mean fixed on the hundredth pass, not the first."""
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
        _MemoryAsyncRepository,
    )

    async_repo = _MemoryAsyncRepository()
    _completed_job(async_repo, "job_take", {
        "audio_url": "/dev/media/take.mp3",
        "complete_audio_url": "/dev/media/full.mp3",
        TAKE_SIGNALS_KEY: DEAD_TAIL_SIGNALS,
    })
    tools = _build_tools(async_repo)

    # Before the re-cut the fault is real and reported.
    [before] = tools.hydrate_candidate_results(
        candidates=[{"candidate_id": "c1", "linked_job_id": "job_take"}],
        observation=VIDEO_20S,
    )
    assert before["listen_report"]["notes"] == ["it ends with 2.0s of silence"]

    # The user re-cuts to a better window; the render measured what it produced.
    sculpted = {
        "candidate_id": "c1",
        "linked_job_id": "job_take",
        "window": {"start_s": 40.0, "source": "user"},
        RENDER_EPOCH_KEY: 1,
        TAKE_SIGNALS_KEY: stamped_take_signals(CLEAN_SIGNALS, render_epoch=1),
    }

    for pass_number in range(1, 4):
        [hydrated] = tools.hydrate_candidate_results(
            candidates=[sculpted], observation=VIDEO_20S
        )
        assert hydrated["listen_report"]["clean"] is True, (
            f"the dead tail came back on hydrate pass {pass_number}"
        )
        assert hydrated["listen_report"]["notes"] == []
        # Feed the projection back in, the way the session does.
        sculpted = hydrated


def test_a_re_cut_that_could_not_be_measured_reports_nothing_rather_than_the_old_window() -> None:
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
        _MemoryAsyncRepository,
    )

    async_repo = _MemoryAsyncRepository()
    _completed_job(async_repo, "job_take", {
        "audio_url": "/dev/media/take.mp3", TAKE_SIGNALS_KEY: DEAD_TAIL_SIGNALS,
    })
    tools = _build_tools(async_repo)

    [hydrated] = tools.hydrate_candidate_results(
        candidates=[{
            "candidate_id": "c1", "linked_job_id": "job_take",
            RENDER_EPOCH_KEY: 1,          # re-cut, but the measurement failed
        }],
        observation=VIDEO_20S,
    )
    assert "listen_report" not in hydrated


def test_hydration_stays_byte_stable_so_the_poll_does_not_rewrite_the_session() -> None:
    """The churn guard compares dicts; an unstable value here rewrites the
    session on every tick, forever."""
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
        _MemoryAsyncRepository,
    )

    async_repo = _MemoryAsyncRepository()
    _completed_job(async_repo, "job_take", {
        "audio_url": "/dev/media/take.mp3",
        TAKE_SIGNALS_KEY: DEAD_TAIL_SIGNALS,
        "music_start_s": 96.0,
    })
    tools = _build_tools(async_repo)
    candidate = {"candidate_id": "c1", "linked_job_id": "job_take"}

    [first] = tools.hydrate_candidate_results(candidates=[candidate], observation=VIDEO_20S)
    [second] = tools.hydrate_candidate_results(candidates=[first], observation=VIDEO_20S)
    [third] = tools.hydrate_candidate_results(candidates=[second], observation=VIDEO_20S)

    assert first == second == third


# ---------------------------------------------------------------------------#
# the re-cut closes its own loop                                              #
# ---------------------------------------------------------------------------#


@pytest.mark.asyncio
async def test_sculpting_advances_the_epoch_and_stamps_what_it_measured() -> None:
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
        _MemoryAgenticRepository,
        _MemoryAsyncRepository,
    )
    from EdennCode.EdennAgent.AgenticAudio.models import (
        AGENT_TOOL_SCULPT_AUDIO,
        AgenticSessionPhase,
    )
    from EdennCode.EdennAgent.AgenticAudio.tools.base import ToolContext
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import build_tool_registry

    async def window_fn(**_: Any) -> dict[str, Any]:
        return {
            "status": "completed",
            "remixed_video_url": "/dev/media/w.mp4",
            "window_start_s": 40.0,
            "full_duration_s": 180.0,
            TAKE_SIGNALS_KEY: CLEAN_SIGNALS,
        }

    repo = _MemoryAgenticRepository()
    session = repo.create_session(
        source_video_artifact_id="art",
        phase=AgenticSessionPhase.AWAITING_CANDIDATE_CHOICE,
        state_json={
            "observation": VIDEO_20S,
            "candidates": [{
                "candidate_id": "c1", "status": "completed",
                "audio_url": "https://x/cut.mp3",
                "complete_audio_url": "https://x/full.mp3",
                # What the paid render left behind: the fault being escaped.
                "listen_report": {"clean": False,
                                  "notes": ["it ends with 2.0s of silence"],
                                  "observations": [], "measured": {}},
            }],
        },
    )
    tool = build_tool_registry().get(AGENT_TOOL_SCULPT_AUDIO)
    await tool.run(
        ToolContext(session_id=session.session_id, repository=repo,
                    media=_build_tools(_MemoryAsyncRepository(), window_fn),
                    max_candidates=3),
        {"candidate_id": "c1", "window_start_s": 40.0},
    )

    [stored] = repo.get_session(session.session_id).state_json["candidates"]
    assert stored[RENDER_EPOCH_KEY] == 1, "a re-cut is a new generation of audio"
    assert stored[TAKE_SIGNALS_KEY][RENDER_EPOCH_KEY] == 1
    assert stored[TAKE_SIGNALS_KEY]["trailing_silence_s"] == 0.1
    # Judged immediately, so the card the user is looking at is about the audio
    # they just asked for — not blank until the next poll.
    assert stored["listen_report"]["clean"] is True
    assert stored["window"]["start_s"] == 40.0
    assert stored["window"]["source"] == "user"


@pytest.mark.asyncio
async def test_an_unmeasurable_re_cut_drops_the_previous_windows_numbers() -> None:
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
        _MemoryAgenticRepository,
        _MemoryAsyncRepository,
    )
    from EdennCode.EdennAgent.AgenticAudio.models import (
        AGENT_TOOL_SCULPT_AUDIO,
        AgenticSessionPhase,
    )
    from EdennCode.EdennAgent.AgenticAudio.tools.base import ToolContext
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import build_tool_registry

    async def window_fn(**_: Any) -> dict[str, Any]:
        return {"status": "completed", "remixed_video_url": "/dev/media/w.mp4",
                "window_start_s": 40.0}          # no measurements came back

    repo = _MemoryAgenticRepository()
    session = repo.create_session(
        source_video_artifact_id="art",
        phase=AgenticSessionPhase.AWAITING_CANDIDATE_CHOICE,
        state_json={"observation": VIDEO_20S, "candidates": [{
            "candidate_id": "c1", "status": "completed",
            "audio_url": "https://x/cut.mp3",
            "complete_audio_url": "https://x/full.mp3",
            RENDER_EPOCH_KEY: 1,
            TAKE_SIGNALS_KEY: stamped_take_signals(DEAD_TAIL_SIGNALS, render_epoch=1),
            "listen_report": {"clean": False, "notes": ["it ends with 2.0s of silence"],
                              "observations": [], "measured": {}},
        }]},
    )
    tool = build_tool_registry().get(AGENT_TOOL_SCULPT_AUDIO)
    await tool.run(
        ToolContext(session_id=session.session_id, repository=repo,
                    media=_build_tools(_MemoryAsyncRepository(), window_fn),
                    max_candidates=3),
        {"candidate_id": "c1", "window_start_s": 40.0},
    )

    [stored] = repo.get_session(session.session_id).state_json["candidates"]
    assert stored[RENDER_EPOCH_KEY] == 2
    assert TAKE_SIGNALS_KEY not in stored, "epoch-1 numbers do not describe epoch 2"
    assert "listen_report" not in stored


def test_both_renderers_measure_the_window_they_produced() -> None:
    """The mix switches renderer the moment narration or SFX joins. A re-cut
    measured on only one of them is a loop that closes for some sessions and
    lies in the rest."""
    library = Path("EdennCode/EdennAgent/AgenticAudio/tools/media.py").read_text()
    devserver = Path("EdennCode/EdennAgent/AgenticAudio/design/devserver.py").read_text()

    # The library renderer both defines the helper and calls it.
    assert library.count("measure_window_signals(") >= 2

    # The devserver twin imports it and calls it inside its own window renderer.
    # Sliced to the NEXT function rather than a character count, so the check
    # cannot quietly pass by cutting the call in half.
    assert "measure_window_signals," in devserver, "the devserver twin imports it"
    dev_window_body = devserver.split("async def _dev_window")[1].split("async def ")[0]
    assert "measure_window_signals," in dev_window_body
    assert "take_signals" in dev_window_body


def test_the_measurement_helper_refuses_to_run_on_the_read_path() -> None:
    """Signature check: it takes local paths, which hydrate never has — the
    2.5s poll holds a row open and must never touch a file."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import measure_window_signals

    params = inspect.signature(measure_window_signals).parameters
    assert set(params) == {"full_track_path", "window_start_s", "window_duration_s"}
    assert measure_window_signals(
        full_track_path=Path("/nonexistent/track.mp3"),
        window_start_s=0.0, window_duration_s=0.0,
    ) == {}, "a zero-length window is not measurable, and says so quietly"


# ---------------------------------------------------------------------------#
# a comparison stops describing takes that moved on                           #
# ---------------------------------------------------------------------------#


def test_a_comparison_survives_while_its_takes_stand_still() -> None:
    comparison = {"compared": 2, "candidate_epochs": {"c1": 0, "c2": 1}}
    candidates = [
        {"candidate_id": "c1"},
        {"candidate_id": "c2", RENDER_EPOCH_KEY: 1},
    ]
    assert comparison_still_describes(comparison, candidates) is True


def test_a_comparison_dies_the_moment_one_of_its_takes_is_re_cut() -> None:
    """"Go with the second one" must not resolve against a duration that stopped
    being true two turns ago."""
    comparison = {"compared": 2, "candidate_epochs": {"c1": 0, "c2": 0}}
    candidates = [
        {"candidate_id": "c1"},
        {"candidate_id": "c2", RENDER_EPOCH_KEY: 1},   # re-cut since
    ]
    assert comparison_still_describes(comparison, candidates) is False


def test_a_comparison_naming_a_vanished_take_describes_nothing() -> None:
    comparison = {"compared": 1, "candidate_epochs": {"c9": 0}}
    assert comparison_still_describes(comparison, [{"candidate_id": "c1"}]) is False


def test_an_unstamped_comparison_fails_closed() -> None:
    """Written before provenance existed. Re-running a free tool costs nothing;
    serving a table that cannot be checked costs the user their trust."""
    assert comparison_still_describes({"compared": 1}, [{"candidate_id": "c1"}]) is False
    assert comparison_still_describes(None, []) is False


@pytest.mark.asyncio
async def test_comparing_stamps_the_generation_each_row_measured() -> None:
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import _MemoryAgenticRepository
    from EdennCode.EdennAgent.AgenticAudio.models import (
        AGENT_TOOL_COMPARE_TAKES, AgenticSessionPhase,
    )
    from EdennCode.EdennAgent.AgenticAudio.tools.base import ToolContext
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import build_tool_registry

    repo = _MemoryAgenticRepository()
    session = repo.create_session(
        source_video_artifact_id="art", phase=AgenticSessionPhase.AWAITING_CANDIDATE_CHOICE,
        state_json={"candidates": [
            {"candidate_id": "c1", "status": "completed"},
            {"candidate_id": "c2", "status": "completed", RENDER_EPOCH_KEY: 2},
        ]},
    )
    tool = build_tool_registry().get(AGENT_TOOL_COMPARE_TAKES)
    result = await tool.run(
        ToolContext(session_id=session.session_id, repository=repo, media=None,
                    max_candidates=3),
        {},
    )

    assert result.data["candidate_epochs"] == {"c1": 0, "c2": 2}


# ---------------------------------------------------------------------------#
# a re-cut window survives the next thing the user does                       #
# ---------------------------------------------------------------------------#


SCULPTED_TAKE: dict[str, Any] = {
    "candidate_id": "c1",
    "status": "completed",
    "audio_url": "https://x/cut.mp3",
    "complete_audio_url": "https://x/full.mp3",
    "window": {"start_s": 42.5, "source": "user"},
}


def test_a_moved_window_selects_the_whole_track_seeked_into() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.media import candidate_music_source

    url, start = candidate_music_source(SCULPTED_TAKE)
    assert url == "https://x/full.mp3", "the cut is not the music the user chose"
    assert start == 42.5


def test_an_untouched_take_plays_its_own_cut_from_the_top() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.media import candidate_music_source

    assert candidate_music_source({
        "audio_url": "https://x/cut.mp3",
        "complete_audio_url": "https://x/full.mp3",
        "window": {"start_s": 96.0, "source": "matcher"},
    }) == ("https://x/cut.mp3", 0.0)


def test_a_garbled_window_plays_the_cut_rather_than_the_wrong_music() -> None:
    """Falling back to the full track from 0.0 would hand the user a different
    piece of music than the one on the card."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import candidate_music_source

    assert candidate_music_source({
        "audio_url": "https://x/cut.mp3",
        "complete_audio_url": "https://x/full.mp3",
        "window": {"start_s": "somewhere", "source": "user"},
    }) == ("https://x/cut.mp3", 0.0)


def test_a_take_with_nothing_rendered_yet_has_nothing_to_play() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.media import candidate_music_source

    assert candidate_music_source({"candidate_id": "c1"}) == (None, 0.0)
    assert candidate_music_source(None) == (None, 0.0)


@pytest.mark.asyncio
async def test_adjusting_the_volume_does_not_throw_away_the_re_cut() -> None:
    """The bug in one line: sculpt to the drop, then say "a bit quieter", and the
    music jumped back to the window the matcher originally chose."""
    seen: dict[str, Any] = {}

    def _overlay(video: Any, music: Any, out: Any, **kwargs: Any) -> None:
        seen.update(kwargs)
        Path(out).write_bytes(b"")

    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
        _MemoryAsyncRepository,
    )
    import EdennCode.EdennAgent.AgenticAudio.tools.media as media_module

    tools = _build_tools(_MemoryAsyncRepository())
    tools.get_source_video_artifact = lambda _id: SimpleNamespace(artifact_id=_id)

    class _Prep:
        async def resolve_source_video(self, _artifact: Any) -> str:
            return "/tmp/source.mp4"

    tools._get_source_preparation = lambda: _Prep()
    tools._refresh_signed_url = lambda url: url

    async def _download(**kwargs: Any) -> None:
        Path(kwargs["destination"]).write_bytes(b"")

    original_overlay = media_module.overlay_music_on_video
    original_download = media_module.download_public_file_to_disk
    media_module.overlay_music_on_video = _overlay
    media_module.download_public_file_to_disk = _download
    try:
        result = await tools.adjust_remix(
            candidate=SCULPTED_TAKE,
            source_video_artifact_id="art",
            music_volume=0.5,
        )
    finally:
        media_module.overlay_music_on_video = original_overlay
        media_module.download_public_file_to_disk = original_download

    assert result["status"] == "completed"
    assert seen["music_start_s"] == 42.5, "the volume change reverted the window"
    assert seen["music_volume"] == 0.5


def test_every_renderer_asks_the_same_question_about_what_a_take_plays() -> None:
    """Three renderers re-present a take — the library re-mux, the master mix,
    and the devserver twin that serves the shipped deployment. Each keeping its
    own copy of the rule is exactly how one of them drifted."""
    media_source = Path("EdennCode/EdennAgent/AgenticAudio/tools/media.py").read_text()
    impls_source = Path("EdennCode/EdennAgent/AgenticAudio/tools/impls.py").read_text()
    devserver_source = Path(
        "EdennCode/EdennAgent/AgenticAudio/design/devserver.py"
    ).read_text()

    assert "def candidate_music_source(" in media_source
    assert "candidate_music_source(candidate)" in media_source, "the library re-mux"
    assert "candidate_music_source(candidate)" in impls_source, "the master mix"
    assert "candidate_music_source(candidate)" in devserver_source, "the devserver twin"

    # ...and nobody hand-rolls the window rule beside it any more.
    assert '"user" and candidate.get("complete_audio_url")' not in impls_source


# ---------------------------------------------------------------------------#
# the measurement, against audio that actually exists                         #
# ---------------------------------------------------------------------------#


def _ffmpeg_available() -> bool:
    import shutil

    from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary

    try:
        return bool(shutil.which(resolve_ffmpeg_binary()))
    except Exception:  # noqa: BLE001 — a missing binary is a skip, not a failure
        return False


@pytest.mark.skipif(not _ffmpeg_available(), reason="needs a local ffmpeg")
def test_re_measuring_hears_what_is_actually_in_the_new_window(tmp_path: Path) -> None:
    """End to end on real audio, with no provider involved: a synthetic track
    with a known hole in it, cut two different ways.

    This is the half of the loop that static assertions cannot reach — that the
    slice measured is genuinely the slice the mux plays. The extraction seeks
    the same way the mux does, so a hole 30s into the track has to surface 2s
    into a window that starts at 28s.
    """
    import subprocess

    from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary
    from EdennCode.EdennAgent.AgenticAudio.tools.media import measure_window_signals

    track = tmp_path / "full.m4a"
    subprocess.run(
        [
            resolve_ffmpeg_binary(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=5",
            "-f", "lavfi", "-i", "sine=frequency=330:duration=25",
            "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1[a]", "-map", "[a]",
            str(track),
        ],
        check=True, capture_output=True,
    )

    before = set(tmp_path.iterdir())
    clean = measure_window_signals(
        full_track_path=track, window_start_s=0.0, window_duration_s=20.0
    )
    holed = measure_window_signals(
        full_track_path=track, window_start_s=28.0, window_duration_s=20.0
    )

    assert clean["cut_duration_s"] == pytest.approx(20.0, abs=0.1)
    assert clean["full_duration_s"] == pytest.approx(60.0, abs=0.1)
    assert not clean.get("internal_gaps_s"), "solid tone is not a hole"

    assert holed.get("internal_gaps_s") == [5.0], "the window landed on the silence"

    # ...and the judgement layer turns that into something the agent can act on.
    from EdennCode.EdennAgent.AgenticAudio.tools.media import music_alignment

    assert music_alignment(clean, observation=VIDEO_20S)["notes"] == []
    assert any(
        "5.0s gap of near-silence" in note
        for note in music_alignment(holed, observation=VIDEO_20S)["notes"]
    )

    assert set(tmp_path.iterdir()) == before, "the temporary cut was left behind"


# ---------------------------------------------------------------------------#
# both renderers listen to the master they produced                           #
# ---------------------------------------------------------------------------#


def test_both_compose_renderers_measure_the_deliverable() -> None:
    """The mix surface switches renderer the moment narration or SFX joins. A
    master checked on only one of them is a check that holds for some sessions
    and quietly does not for the rest — the same trap the re-cut hit."""
    library = Path("EdennCode/EdennAgent/AgenticAudio/tools/media.py").read_text()
    devserver = Path("EdennCode/EdennAgent/AgenticAudio/design/devserver.py").read_text()

    compose_body = library.split("async def compose_mix")[1].split("    def edit_audio")[0]
    assert "music_take_signals(output_path)" in compose_body
    assert '"mix_signals"' in compose_body

    dev_body = devserver.split("async def _dev_compose")[1].split("async def ")[0]
    assert "music_take_signals" in dev_body
    assert '"mix_signals"' in dev_body


def test_the_mix_report_reaches_the_session_not_just_the_render_result() -> None:
    impls = Path("EdennCode/EdennAgent/AgenticAudio/tools/impls.py").read_text()

    assert "mix_alignment(" in impls
    assert 'mix["listen_report"] = report' in impls


def test_the_prompt_tells_the_agent_to_check_the_deliverable() -> None:
    from EdennCode.EdennAgent.AgenticAudio.agent.prompts import SYSTEM_PROMPT

    assert "mix.listen_report" in SYSTEM_PROMPT
    # ...and warns it off the one thing the report cannot hear.
    assert "not measurable from the finished file" in SYSTEM_PROMPT
