"""Media-correctness tests: actually compose audio with ffmpeg and MEASURE the
output to confirm the deliverable is right — the voice-over lands at the requested
moment, the music ducks under it by the requested gain, and a voice-over-only
deliverable is non-empty with the narration at the right offset.

Uses synthetic tones (music=220Hz, VO=440Hz, original=100Hz) so each layer is
separable by an FFT band. Skipped where ffmpeg/numpy aren't available.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
if shutil.which("ffmpeg") is None:
    pytest.skip("ffmpeg not available", allow_module_level=True)

from EdennCode.Util.MediaUtils.ffmpeg_utils import (
    compose_voiceover_mix_on_video,
    overlay_voiceover_on_video,
)


def _sh(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True)


def _inputs(tmp: Path) -> tuple[Path, Path, Path]:
    video, music, vo = tmp / "v.mp4", tmp / "m.wav", tmp / "vo.wav"
    _sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=100:duration=10",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video))
    _sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=10", str(music))
    _sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(vo))
    return video, music, vo


def _band(tmp: Path, path: Path, t0: float, t1: float, freq: float, bw: float = 18.0) -> float:
    """Per-sample mean FFT magnitude in [freq±bw] over [t0,t1] (comparable across
    window lengths)."""
    w = tmp / "x.wav"
    _sh("ffmpeg", "-y", "-i", str(path), "-ac", "1", "-ar", "16000", str(w))
    wf = wave.open(str(w), "rb")
    sr = wf.getframerate()
    sig = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(float)
    wf.close()
    seg = sig[int(t0 * sr):int(t1 * sr)]
    if len(seg) < 128:
        return 0.0
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    fr = np.fft.rfftfreq(len(seg), 1 / sr)
    return float(spec[(fr >= freq - bw) & (fr <= freq + bw)].sum()) / len(seg)


@pytest.mark.parametrize("duck_db, expected", [(-9.0, 0.355), (-15.0, 0.178)])
def test_compose_ducks_music_under_voiceover_and_places_it(tmp_path: Path, duck_db: float, expected: float) -> None:
    video, music, vo = _inputs(tmp_path)
    out = tmp_path / f"mix{int(duck_db)}.mp4"
    compose_voiceover_mix_on_video(
        video, music, vo, out,
        music_volume=0.8, voiceover_volume=1.0, voiceover_start_s=3.0,
        duck_gain_db=duck_db, preserve_original_audio=False,
    )
    # Voice-over (440Hz) is present ONLY during [3,5].
    vo_before = _band(tmp_path, out, 0.5, 2.5, 440)
    vo_during = _band(tmp_path, out, 3.3, 4.7, 440)
    vo_after = _band(tmp_path, out, 5.6, 9.5, 440)
    assert vo_during > 20 * max(vo_before, vo_after, 0.01), "VO not placed at voiceover_start_s"
    # Music (220Hz) ducks under the narration by ~duck_gain_db, and stays audible.
    mu_before = _band(tmp_path, out, 0.6, 2.4, 220)
    mu_during = _band(tmp_path, out, 3.4, 4.6, 220)
    ratio = mu_during / max(mu_before, 1e-9)
    assert abs(ratio - expected) < 0.06, f"duck ratio {ratio:.3f} != expected {expected:.3f} (ducking not applied?)"
    assert mu_during > 0.03 * mu_before, "music fully silenced under VO (should only duck)"


def test_voiceover_only_overlay_is_nonempty_and_placed(tmp_path: Path) -> None:
    video, _music, vo = _inputs(tmp_path)
    out = tmp_path / "voonly.mp4"
    overlay_voiceover_on_video(
        video, vo, out, voiceover_volume=1.0, voiceover_start_s=2.0, preserve_original_audio=False
    )
    assert out.stat().st_size > 2000, "voice-over-only deliverable is empty"
    before = _band(tmp_path, out, 0.3, 1.7, 440)
    during = _band(tmp_path, out, 2.3, 3.7, 440)
    assert during > 20 * max(before, 0.01), "narration not placed at voiceover_start_s"


def test_trim_edge_silence_measures_speech_not_padding(tmp_path: Path) -> None:
    """A take arrives padded with dead air at both ends. That padding is measured
    as part of the line's duration, so the timeline places the audible speech off
    its cue and stacks the air on top of the director's gap. Trimming must remove
    the edges, leave the interior untouched, and never clip the first phoneme."""
    from EdennCode.Util.MediaUtils.ffmpeg_utils import trim_edge_silence, get_video_duration

    take = tmp_path / "take.wav"
    # 0.9s silence | 1.0s tone | 0.5s silence (an interior beat) | 1.0s tone | 1.4s silence
    _sh("ffmpeg", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=0.9",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1.0:sample_rate=44100",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=0.5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1.0:sample_rate=44100",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1.4",
        "-filter_complex", "[0:a][1:a][2:a][3:a][4:a]concat=n=5:v=0:a=1[a]",
        "-map", "[a]", str(take))
    assert abs(get_video_duration(take) - 4.8) < 0.1

    removed = trim_edge_silence(take)
    after = get_video_duration(take)

    # ~2.3s of edge silence goes; the 0.5s interior beat stays.
    assert removed > 1.9, f"expected the edges gone, removed {removed}"
    assert 2.4 < after < 2.8, f"interior beat lost or edges kept: {after}"

    # The head keeps a sliver so the attack is never clipped.
    head = subprocess.run(
        ["ffmpeg", "-i", str(take), "-t", "0.15", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    assert "max_volume" in head


def test_trim_edge_silence_keeps_a_silent_take_intact(tmp_path: Path) -> None:
    """Trimming a take that is silent throughout would leave nothing to mix, and
    a line that vanishes silently is worse than one that is merely quiet."""
    from EdennCode.Util.MediaUtils.ffmpeg_utils import trim_edge_silence, get_video_duration

    silent = tmp_path / "silent.wav"
    _sh("ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2.0", str(silent))
    before = get_video_duration(silent)
    assert trim_edge_silence(silent) == 0.0
    assert abs(get_video_duration(silent) - before) < 0.05


def test_trim_edge_silence_is_safe_on_a_missing_file(tmp_path: Path) -> None:
    from EdennCode.Util.MediaUtils.ffmpeg_utils import trim_edge_silence

    assert trim_edge_silence(tmp_path / "nope.wav") == 0.0


def test_attach_cut_list_reports_cuts_and_their_source(tmp_path: Path) -> None:
    """The writer times lines against cuts, so the cut list must be complete and
    must say where it came from — an invented cut is worse than a missing one."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import attach_cut_list

    # Three hard cuts: red | green | blue | red, 2s each.
    clip = tmp_path / "cuts.mp4"
    _sh("ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=320x240:d=2:r=25",
        "-f", "lavfi", "-i", "color=c=green:s=320x240:d=2:r=25",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=2:r=25",
        "-f", "lavfi", "-i", "color=c=red:s=320x240:d=2:r=25",
        "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip))

    obs: dict = {}
    attach_cut_list(obs, clip)
    assert obs["cut_source"] in {"pyscenedetect", "ffprobe_fallback", "unavailable"}
    if obs["cut_source"] == "pyscenedetect":
        # Found near 2/4/6s, and never t=0 (the clip start is not a cut).
        assert len(obs["cuts"]) >= 2, obs["cuts"]
        assert all(c > 0.05 for c in obs["cuts"])
        assert any(abs(c - 4.0) < 0.3 for c in obs["cuts"]), obs["cuts"]
    assert obs["cuts"] == sorted(obs["cuts"])


def test_attach_cut_list_degrades_instead_of_raising(tmp_path: Path) -> None:
    """Analysis must not fail because a clip was awkward to scan."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import attach_cut_list

    obs: dict = {}
    attach_cut_list(obs, tmp_path / "missing.mp4")
    assert obs == {"cuts": [], "cut_source": "unavailable"}


def _reference_sheet():
    """The 2026-08-16 reference reel: 16.17s, four real cuts, five shots."""
    from EdennCode.EdennAgent.AgenticAudio.tools.spotting import build_spotting_sheet
    return build_spotting_sheet({
        "duration_s": 16.167,
        "cuts": [5.867, 8.9, 12.2, 14.9],
        "cut_source": "pyscenedetect",
        "scenes": [
            {"start_timestamp": 0.0, "end_timestamp": 5.77, "visual_summary": "Close-up at a formal event", "mood": "Elegant"},
            {"start_timestamp": 5.77, "end_timestamp": 8.4, "visual_summary": "Emotional then surprised", "mood": "emotional"},
            {"start_timestamp": 8.4, "end_timestamp": 12.0, "visual_summary": "Animated reaction", "mood": "excited"},
            {"start_timestamp": 12.0, "end_timestamp": 14.9, "visual_summary": "Dim-lit audience", "mood": "glamorous"},
            {"start_timestamp": 14.9, "end_timestamp": 16.0, "visual_summary": "Sparkling earrings, reflective", "mood": "reflective"},
        ],
    })


def test_spotting_sheet_gives_every_moment_one_owner_and_ranks_the_ending_first() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.spotting import OWNERS, OWNER_NARRATE

    sheet = _reference_sheet()
    moments = sheet["moments"]
    assert sheet["reliable"] is True
    assert len(moments) == 4, [m["t"] for m in moments]
    assert all(m["owner"] in OWNERS for m in moments)
    assert all("rank" in m for m in moments)

    # The ending is the piece's most important moment, so it ranks first, and a
    # final shot long enough to speak over belongs to narration by default.
    finale = next(m for m in moments if m["source"] == "finale")
    assert finale["rank"] == 1
    assert finale["t"] == 14.9
    assert finale["owner"] == OWNER_NARRATE
    assert finale["window"] == [14.9, 16.17]


def test_spotting_sheet_reproduces_the_real_collision_that_shipped() -> None:
    """The 16 Aug master mix put four of seven effects under the narration, and
    nobody noticed until the mix. The sheet has to catch exactly that."""
    from EdennCode.EdennAgent.AgenticAudio.tools.spotting import sfx_conflicts, narration_conflicts

    sheet = _reference_sheet()
    vo = [
        {"id": "seg_01", "text": "In a room full of lights... the real moment is written on their faces.", "start_s": 0.8, "duration_s": 5.25},
        {"id": "seg_02", "text": "Every glance holds anticipation.", "start_s": 6.45, "duration_s": 3.35},
        {"id": "seg_03", "text": "And then... emotion takes over.", "start_s": 11.8, "duration_s": 2.85},
    ]
    sfx = [
        {"label": "Opening flash", "start_s": 0.3},
        {"label": "Cut whoosh", "start_s": 2.6},
        {"label": "Tear sparkle", "start_s": 4.4},
        {"label": "Reaction rise", "start_s": 6.1},
        {"label": "Excited flash", "start_s": 8.7},
        {"label": "Glamour transition", "start_s": 12.2},
        {"label": "Final reflective look", "start_s": 15.0},
    ]
    collisions = sfx_conflicts(sheet, sfx, vo)
    under = [c for c in collisions if c["reason"] == "fires under narration"]
    assert {c["start_s"] for c in under} == {2.6, 4.4, 8.7, 12.2}

    # The closing hit at 15.0s is NOT a clash: that narration ended at 14.65 and
    # never reached the closing image, so the moment it nominally owned is
    # released. The real failure in that mix was narration abandoning the
    # climax, not sound design taking it.
    owned = [c for c in collisions if c["reason"] == "lands on a moment narration owns"]
    assert owned == []

    # Move the last line onto the closing image and the hit does clash.
    lands = vo[:2] + [{"id": "seg_03", "start_s": 14.7, "duration_s": 1.3}]
    assert any(c["start_s"] == 15.0 for c in sfx_conflicts(sheet, sfx, lands))

    # All three lines were spoken across cuts sound design owns.
    assert len(narration_conflicts(sheet, vo)) == 3


def test_spotting_sheet_survives_a_clip_with_no_usable_cuts() -> None:
    """No cut list still gets a sheet — the ending always needs an owner — but it
    must declare itself unreliable so nothing treats its timings as hard."""
    from EdennCode.EdennAgent.AgenticAudio.tools.spotting import build_spotting_sheet

    sheet = build_spotting_sheet(
        {"duration_s": 20.0, "cuts": [], "cut_source": "ffprobe_fallback", "scenes": []})
    assert sheet["reliable"] is False
    assert [m["source"] for m in sheet["moments"]] == ["finale"]
    assert sheet["moments"][0]["window"] == [0.0, 20.0]

    empty = build_spotting_sheet({})
    assert empty["moments"] == [] and empty["reliable"] is False


def test_free_windows_are_the_stretches_narration_leaves_alone() -> None:
    """A planner told only "that's wrong" moves the effect somewhere equally
    wrong, so a collision has to come with somewhere to put it."""
    from EdennCode.EdennAgent.AgenticAudio.tools.spotting import free_windows

    sheet = _reference_sheet()  # finale 14.9-16.17 is narration's
    vo = [
        {"id": "seg_01", "start_s": 0.6, "duration_s": 1.94},
        {"id": "seg_02", "start_s": 6.4, "duration_s": 2.76},
        # This one actually lands on the closing image, so narration keeps it.
        {"id": "seg_03", "start_s": 15.0, "duration_s": 1.0},
    ]
    free = free_windows(sheet, vo, duration_s=16.167)
    assert free, "there must be somewhere to put an effect"

    def covered(t: float) -> bool:
        return any(lo <= t <= hi for lo, hi in free)

    assert not covered(1.5), "mid-line is not free"
    assert not covered(7.5), "mid-line is not free"
    assert not covered(15.5), "a window narration owns AND uses is not free"
    assert covered(4.0), "the gap between lines is free"
    assert covered(11.0), "the long stretch before the finale is free"


def test_sfx_does_not_collide_with_narration_that_does_not_exist() -> None:
    """The finale's default owner is a proposal about where a line WOULD go, not
    a reservation that keeps sound design off the ending of a piece that will
    never have a voice."""
    from EdennCode.EdennAgent.AgenticAudio.tools.spotting import sfx_conflicts

    sheet = _reference_sheet()
    ending_hit = [{"label": "final impact", "start_s": 15.0}]
    assert sfx_conflicts(sheet, ending_hit, []) == []
    # But once a line is actually written across it, the clash is real.
    spoken = [{"id": "seg_01", "start_s": 14.6, "duration_s": 1.2}]
    assert len(sfx_conflicts(sheet, ending_hit, spoken)) == 1


def test_a_moment_narration_declines_is_released_not_reserved() -> None:
    """A live run ended on silence nobody asked for: narration held the finale,
    chose not to speak over it, and the reservation still kept the sound-design
    hit away. An owner that declines its moment releases it."""
    from EdennCode.EdennAgent.AgenticAudio.tools.spotting import sfx_conflicts, free_windows

    sheet = _reference_sheet()  # finale window is 14.9-16.17, owned by narrate
    ending_hit = [{"label": "final impact", "start_s": 15.0}]

    # Narration stops at 14.68 — it never reaches the closing image.
    declined = [
        {"id": "seg_01", "start_s": 0.5, "duration_s": 1.58},
        {"id": "seg_03", "start_s": 12.4, "duration_s": 2.28},
    ]
    assert sfx_conflicts(sheet, ending_hit, declined) == [], \
        "the ending must not be left empty by a reservation nobody used"
    assert any(lo <= 15.0 <= hi for lo, hi in free_windows(
        sheet, declined, duration_s=16.167))

    # But a line that actually lands on the closing image does reserve it.
    used = declined[:1] + [{"id": "seg_03", "start_s": 14.7, "duration_s": 1.3}]
    assert len(sfx_conflicts(sheet, ending_hit, used)) == 1


def test_music_recovers_between_narration_lines(tmp_path: Path) -> None:
    """One duck window over the whole read holds the score down through every
    pause in it. The wordless beats between lines are where the music is meant to
    carry the piece, so they are the worst thing to suppress.

    Measured in the music BAND (220Hz), not on total level: under a line the
    voice dominates the mix, so a broadband reading says nothing about the music.
    """
    from EdennCode.Util.MediaUtils.ffmpeg_utils import compose_master_mix_on_video

    video = tmp_path / "v.mp4"
    music = tmp_path / "m.wav"
    vo = tmp_path / "vo.wav"
    _sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=14",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(video))
    _sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=14", str(music))
    # Speech at 1-3s and 9-11s; a long musical beat at 4.5-8.5s between them.
    _sh("ffmpeg", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=3",
        "-filter_complex", "[0:a][1:a][2:a][3:a][4:a]concat=n=5:v=0:a=1[a]",
        "-map", "[a]", str(vo))

    lines = [(1.0, 3.0), (9.0, 11.0)]
    per_line = tmp_path / "per_line.mp4"
    compose_master_mix_on_video(
        video, per_line, music_path=music, voiceover_path=vo,
        music_volume=0.9, voiceover_volume=1.0, duck_gain_db=-12.0,
        voiceover_segments=lines,
    )
    whole_span = tmp_path / "whole.mp4"
    compose_master_mix_on_video(
        video, whole_span, music_path=music, voiceover_path=vo,
        music_volume=0.9, voiceover_volume=1.0, duck_gain_db=-12.0,
    )

    # Between the lines (5.5-7.5s) the per-line mix keeps the music up; the
    # single-window mix is still holding it down for a pause nobody is speaking in.
    gap_new = _band(tmp_path, per_line, 5.5, 7.5, 220)
    gap_old = _band(tmp_path, whole_span, 5.5, 7.5, 220)
    assert gap_new > 2.5 * gap_old, (
        f"music should recover between lines: per-line {gap_new:.1f} vs "
        f"whole-span {gap_old:.1f}")

    # Recovery is not the same as never ducking: it must still dip UNDER a line.
    under_line = _band(tmp_path, per_line, 1.5, 2.5, 220)
    assert under_line < 0.55 * gap_new, (
        f"music must still dip under speech: under-line {under_line:.1f} vs "
        f"gap {gap_new:.1f}")
    # And it must not be silenced there — ducking, not muting.
    assert under_line > 0.05 * gap_new, "music fully silenced under the line"


def test_master_mix_without_segments_keeps_the_single_window(tmp_path: Path) -> None:
    """A caller that cannot say where the lines are still gets honest ducking."""
    from EdennCode.Util.MediaUtils.ffmpeg_utils import narration_duck_expr

    assert narration_duck_expr(0.8, []) == "0.8"
    expr = narration_duck_expr(0.8, [(1.0, 2.0)], gain_db=-12.0)
    assert "max(" in expr and "min(" in expr and expr.startswith("0.8*")


def test_holding_a_beat_silent_protects_it_from_every_layer() -> None:
    """Restraint has to be recordable. A beat left alone by accident is released
    to whoever wants it; a beat HELD on purpose is protected from all of them."""
    from EdennCode.EdennAgent.AgenticAudio.tools.spotting import (
        set_moment_owner, sfx_conflicts, free_windows, OWNER_SILENCE,
    )

    sheet = _reference_sheet()
    target = next(m for m in sheet["moments"] if m["t"] == 12.2)
    hit = [{"label": "transition whoosh", "start_s": 12.2}]

    # Before: sound design owns 12.2, so its own effect there is fine.
    assert sfx_conflicts(sheet, hit, [{"id": "s", "start_s": 0.5, "duration_s": 1.0}]) == []

    updated = set_moment_owner(sheet, target["id"], OWNER_SILENCE,
                               source="narration", reason="let the room land")
    assert updated is not None
    assert updated["owner"] == OWNER_SILENCE
    assert updated["owner_source"] == "narration"
    assert updated["revised_from"]["owner"] == "sfx"  # provenance kept

    # After: protected.
    clash = sfx_conflicts(sheet, hit, [{"id": "s", "start_s": 0.5, "duration_s": 1.0}])
    assert [c["reason"] for c in clash] == ["lands on a moment held silent on purpose"]

    # And unlike narration's claim — which is released when no line uses it — a
    # held beat holds even in a session with no narration at all, because it is
    # a decision about the picture rather than about the voice.
    sheet2 = _reference_sheet()
    set_moment_owner(sheet2, target["id"], OWNER_SILENCE, source="user")
    assert len(sfx_conflicts(sheet2, hit, [])) == 1

    # And it is not offered as free space.
    assert not any(lo <= 12.2 <= hi for lo, hi in
                   free_windows(sheet2, [], duration_s=16.167))


def test_set_moment_owner_rejects_nonsense_and_records_who_decided() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.spotting import set_moment_owner

    sheet = _reference_sheet()
    mid = sheet["moments"][0]["id"]
    assert set_moment_owner(sheet, mid, "trumpet", source="user") is None
    assert set_moment_owner(sheet, "moment_99", "narrate", source="user") is None
    got = set_moment_owner(sheet, mid, "narrate", source="user", reason="I want a line here")
    assert got["owner"] == "narrate" and got["owner_source"] == "user"
    assert got["reason"] == "I want a line here"


def test_a_users_ownership_choice_is_not_restamped_by_the_agent() -> None:
    """A live run showed the writer AGREEING with a user's choice and still
    restamping it as its own, which quietly loses the fact that a person chose
    it. Telling the model the user outranks it is not enough."""
    from EdennCode.EdennAgent.AgenticAudio.tools.spotting import set_moment_owner

    sheet = _reference_sheet()
    mid = sheet["moments"][0]["id"]
    assert set_moment_owner(sheet, mid, "silence", source="user",
                            reason="let it breathe") is not None

    # The agent may not overwrite it — not to something else...
    assert set_moment_owner(sheet, mid, "sfx", source="narration") is None
    # ...and not even to the SAME owner, which would downgrade the provenance.
    assert set_moment_owner(sheet, mid, "silence", source="narration") is None

    m = sheet["moments"][0]
    assert m["owner"] == "silence"
    assert m["owner_source"] == "user"
    assert m["reason"] == "let it breathe"

    # The user can still change their own mind.
    assert set_moment_owner(sheet, mid, "narrate", source="user") is not None
    assert sheet["moments"][0]["owner"] == "narrate"


def test_holding_a_beat_and_speaking_over_it_is_caught() -> None:
    """A live run declared the final shot silent and then wrote a line running
    straight into it. The check missed it because it ran BEFORE the hold was
    applied, when that moment was still owned by narration."""
    from EdennCode.EdennAgent.AgenticAudio.tools.spotting import (
        set_moment_owner, narration_conflicts, OWNER_SILENCE,
    )

    sheet = _reference_sheet()
    finale = next(m for m in sheet["moments"] if m["source"] == "finale")
    # A line that runs from 12.5 to 15.86 — straight through the 14.9 finale.
    segments = [{"id": "seg_03", "text": "x", "start_s": 12.5, "duration_s": 3.36}]

    # While narration still owns the finale, speaking there is its own business.
    assert narration_conflicts(sheet, segments) == []

    # Once it has been declared silent, the same line is a contradiction.
    set_moment_owner(sheet, finale["id"], OWNER_SILENCE,
                     source="narration", reason="let the close-up land")
    clashes = narration_conflicts(sheet, segments)
    assert [c["owner"] for c in clashes] == [OWNER_SILENCE]
    assert clashes[0]["segment_id"] == "seg_03"


def _short_inputs(tmp: Path):
    """A 10s video with audio layers that all END EARLY — the shape restraint
    produces, and the shape that used to truncate the customer's footage."""
    video, music, vo, sfx = tmp/"v.mp4", tmp/"m.wav", tmp/"vo.wav", tmp/"fx.wav"
    _sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=100:duration=10",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video))
    _sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=4", str(music))
    _sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", str(vo))
    _sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=2", str(sfx))
    return video, music, vo, sfx


@pytest.mark.parametrize("preserve", [False, True])
def test_no_compose_path_ever_shortens_the_customers_video(tmp_path: Path, preserve: bool) -> None:
    """The deliverable is the user's footage. Audio that stops early must never
    take the picture with it — and this work deliberately ends narration early
    and holds finales silent, so the truncating case is now the NORMAL case.

    Measured before the fix: a 16.17s clip narrated to 12.0s shipped as 12.0s,
    with four seconds of footage gone.
    """
    from EdennCode.Util.MediaUtils.ffmpeg_utils import (
        compose_master_mix_on_video, compose_voiceover_mix_on_video,
        overlay_voiceover_on_video, overlay_music_on_video, get_video_duration,
    )
    video, music, vo, sfx = _short_inputs(tmp_path)
    source_len = get_video_duration(video)

    outs = {}
    outs["music_only"] = tmp_path / f"a{int(preserve)}.mp4"
    overlay_music_on_video(video, music, outs["music_only"],
                           preserve_original_audio=preserve)
    outs["voiceover_only"] = tmp_path / f"b{int(preserve)}.mp4"
    overlay_voiceover_on_video(video, vo, outs["voiceover_only"],
                               voiceover_start_s=1.0, preserve_original_audio=preserve)
    outs["music_plus_vo"] = tmp_path / f"c{int(preserve)}.mp4"
    compose_voiceover_mix_on_video(video, music, vo, outs["music_plus_vo"],
                                   voiceover_start_s=1.0, preserve_original_audio=preserve)
    outs["master_all"] = tmp_path / f"d{int(preserve)}.mp4"
    compose_master_mix_on_video(video, outs["master_all"], music_path=music,
                                voiceover_path=vo, sfx_path=sfx,
                                voiceover_start_s=1.0, preserve_original_audio=preserve)
    outs["master_vo_only"] = tmp_path / f"e{int(preserve)}.mp4"
    compose_master_mix_on_video(video, outs["master_vo_only"], voiceover_path=vo,
                                voiceover_start_s=1.0, preserve_original_audio=preserve)

    for name, path in outs.items():
        got = get_video_duration(path)
        assert abs(got - source_len) < 0.35, (
            f"{name} delivered {got:.2f}s of a {source_len:.2f}s video — "
            f"{source_len - got:.2f}s of the user's footage was cut off")


def test_a_placeholder_narration_is_badged_as_one(tmp_path: Path) -> None:
    """A stand-in tone arriving as a finished read, with nothing to distinguish
    it, is how a paid deliverable quietly becomes a sine wave. Candidates and
    SFX variants already carry this flag; narration did not."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import AgenticAudioTools

    class _Job:
        status = "completed"
        result_json = {"audio_url": "/dev/media/tone.wav", "placeholder": True}

    class _Repo:
        def get_job(self, _job_id):
            return _Job()

    tools = AgenticAudioTools(async_repository=_Repo())
    hydrated = tools.hydrate_voiceover_layer(
        voiceover={"linked_job_id": "job_1", "status": "queued"})
    assert hydrated["placeholder"] is True
    assert hydrated["audio_url"] == "/dev/media/tone.wav"

    class _RealJob(_Job):
        result_json = {"audio_url": "/dev/media/real.wav"}

    class _RealRepo:
        def get_job(self, _job_id):
            return _RealJob()

    real = AgenticAudioTools(async_repository=_RealRepo()).hydrate_voiceover_layer(
        voiceover={"linked_job_id": "job_2", "status": "queued"})
    assert "placeholder" not in real


def test_source_audio_scan_finds_the_gaps_in_a_talking_clip(tmp_path: Path) -> None:
    """The system was deaf to its own input: someone speaks on camera, the agent
    writes a line over them, and the alignment report calls it clean."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import attach_source_audio

    # 12s clip: quiet 0-2, sound 2-5, quiet 5-8, sound 8-10, quiet 10-12.
    clip = tmp_path / "talking.mp4"
    _sh("ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=12",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=3:sample_rate=44100",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=3",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=2:sample_rate=44100",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2",
        "-filter_complex", "[1:a][2:a][3:a][4:a][5:a]concat=n=5:v=0:a=1[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-shortest", str(clip))

    obs: dict = {"duration_s": 12.0}
    attach_source_audio(obs, clip)
    assert obs["source_audio"] == "present", obs
    windows = obs["speech_windows"]
    assert windows, "the loud stretches must be found"

    def inside(t):
        return any(lo <= t <= hi for lo, hi in windows)

    assert inside(3.5), windows      # mid first sound
    assert inside(9.0), windows      # mid second sound
    assert not inside(6.5), windows  # the gap between them stays free


def test_a_continuous_bed_is_not_treated_as_a_no_go_zone(tmp_path: Path) -> None:
    """A clip whose audio runs end to end is music or room tone. Treating that
    as somewhere to keep out of would ban narration from the entire video."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import attach_source_audio

    clip = tmp_path / "bed.mp4"
    _sh("ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=8",
        "-f", "lavfi", "-i", "sine=frequency=200:duration=8:sample_rate=44100",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(clip))
    obs: dict = {"duration_s": 8.0}
    attach_source_audio(obs, clip)
    assert obs["source_audio"] == "continuous"
    assert obs["speech_windows"] == []


def test_unreadable_source_audio_is_not_reported_as_silent(tmp_path: Path) -> None:
    """Silent would licence narration to speak anywhere; unknown must not."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import attach_source_audio

    obs: dict = {"duration_s": 10.0}
    attach_source_audio(obs, tmp_path / "missing.mp4")
    assert obs["source_audio"] == "unavailable"
    assert obs["speech_windows"] == []


def test_the_footages_own_voice_outranks_everything_on_the_sheet() -> None:
    """A place to keep OUT of outranks any place to fill."""
    from EdennCode.EdennAgent.AgenticAudio.tools.spotting import (
        build_spotting_sheet, sfx_conflicts, OWNER_SILENCE,
    )

    sheet = build_spotting_sheet({
        "duration_s": 16.167, "cuts": [5.867, 12.2], "cut_source": "pyscenedetect",
        "speech_windows": [[2.0, 4.5]], "source_audio": "present", "scenes": [],
    })
    top = min(sheet["moments"], key=lambda m: m["rank"])
    assert top["source"] == "source_audio"
    assert top["owner"] == OWNER_SILENCE
    assert top["owner_source"] == "footage"

    # And it is protected from sound design, with or without narration present.
    hit = [{"label": "whoosh", "start_s": 3.0}]
    assert len(sfx_conflicts(sheet, hit, [])) == 1


def test_one_window_swallowing_the_clip_is_a_bed_not_a_constraint(tmp_path: Path) -> None:
    """Real footage forced this. Two reference clips came back as a SINGLE window
    over ~86% of their runtime — obviously a soundtrack, not a moment to keep out
    of — and a plain coverage threshold let them through as no-go zones, which
    would have banned narration from almost the whole video.

    Presence detection cannot tell dialogue from music. The honest handling is to
    ask what would be LEFT: a constraint with no usable gaps is not a constraint.
    """
    from EdennCode.EdennAgent.AgenticAudio.tools.media import attach_source_audio

    clip = tmp_path / "mostly_scored.mp4"
    # 20s clip, sound for the first 17 — the shape of a scored reel.
    _sh("ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=20",
        "-f", "lavfi", "-i", "sine=frequency=200:duration=17:sample_rate=44100",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=3",
        "-filter_complex", "[1:a][2:a]concat=n=2:v=0:a=1[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-shortest", str(clip))

    obs: dict = {"duration_s": 20.0}
    attach_source_audio(obs, clip)
    assert obs["source_audio"] == "continuous", obs
    assert obs["speech_windows"] == [], "a bed must not become a no-go zone"

    # And with that, nothing is refused — narration is free across the clip.
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import _narration_over_source_audio
    assert _narration_over_source_audio(
        [{"id": "a", "text": "A line anywhere.", "start_s": 5.0}],
        observation=obs) is None
