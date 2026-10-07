"""M3 tests: MusicSheet extraction + CutSpec properties.

The audio fixture is a real rendered click-adjacent track: a 120 BPM pulse
(sine bursts every 0.5 s) with a loud middle section, so beat tracking and
energy terciles have real structure to find.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from EdennCode.EdennAgent.Recompose.cutspec import generate_cut_spec
from EdennCode.EdennAgent.Recompose.domain import Knobs
from EdennCode.EdennAgent.Recompose.musicsheet import build_music_sheet


@pytest.fixture(scope="module")
def pulse_track(tmp_path_factory) -> str:
    """~24 s: quiet pulse, loud pulse, quiet pulse — 120 BPM feel."""

    dest = tmp_path_factory.mktemp("m3") / "pulse.wav"
    # 80ms 880Hz burst every 0.5s, audible throughout (0.55) with a louder
    # middle section (1.0), over a faint hum so RMS never collapses to zero.
    expr = (
        "(0.55 + 0.45*between(t,8,16))*sin(2*PI*880*t)*lt(mod(t,0.5),0.08)"
        " + 0.02*sin(2*PI*110*t)"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"aevalsrc='{expr}':d=24:s=22050",
         str(dest)],
        check=True, capture_output=True, timeout=60,
    )
    return str(dest)


@pytest.fixture(scope="module")
def sheet(pulse_track: str):
    return build_music_sheet(pulse_track, window_s=22.0)


# ------------------------------------------------------------------ musicsheet
def test_sheet_finds_a_credible_beat_grid(sheet) -> None:
    assert len(sheet.beats) >= 20
    intervals = np.diff(sheet.beats)
    # 120 BPM pulse -> ~0.5 s intervals (librosa may halve/double: accept 0.25/0.5/1.0)
    med = float(np.median(intervals))
    assert any(abs(med - x) < 0.08 for x in (0.25, 0.5, 1.0)), med
    assert sheet.window_s <= 22.0 + 1e-6
    assert len(sheet.beat_energy) == len(sheet.beats)
    assert 0.0 <= min(sheet.beat_energy) and max(sheet.beat_energy) <= 1.0


def test_sheet_energy_sees_the_loud_middle(sheet) -> None:
    beats = np.array(sheet.beats)
    energy = np.array(sheet.beat_energy)
    mid = energy[(beats >= 9) & (beats <= 15)]
    outer = energy[(beats < 7) | (beats > 17)]
    assert mid.size and outer.size
    assert float(np.mean(mid)) > float(np.mean(outer)) + 0.2


def test_sheet_phrases_cover_window_contiguously(sheet) -> None:
    ph = sheet.phrases
    assert ph, "at least one phrase"
    assert abs(ph[0].start_s - sheet.window_start_s) < 1e-6
    assert abs(ph[-1].end_s - (sheet.window_start_s + sheet.window_s)) < 1e-3
    for a, b in zip(ph, ph[1:]):
        assert abs(a.end_s - b.start_s) < 1e-6


# --------------------------------------------------------------------- cutspec
def _cuts_on_beats(spec, sheet) -> float:
    """Max distance (ms) from any slot boundary to the nearest beat."""

    beats = np.array(sheet.beats) - sheet.beats[0]
    t, worst = 0.0, 0.0
    for slot in spec.slots:
        t += slot.dur_s
        worst = max(worst, float(np.min(np.abs(beats - t))) * 1000)
    return worst


def test_every_cut_lands_on_a_beat(sheet) -> None:
    spec = generate_cut_spec(sheet, Knobs())
    assert _cuts_on_beats(spec, sheet) < 25.0  # rounding only


def test_slots_cover_the_window(sheet) -> None:
    spec = generate_cut_spec(sheet, Knobs())
    span = sheet.beats[-1] - sheet.beats[0]
    assert abs(spec.total_s - span) < 1.0


def test_density_orders_slot_counts(sheet) -> None:
    counts = {d: len(generate_cut_spec(sheet, Knobs(cut_density=d)).slots)
              for d in ("sparse", "medium", "dense")}
    assert counts["sparse"] < counts["medium"] < counts["dense"], counts


def test_shot_clamps_hold(sheet) -> None:
    knobs = Knobs(cut_density="dense", min_shot_s=0.8, max_shot_s=3.0)
    spec = generate_cut_spec(sheet, knobs)
    for slot in spec.slots[:-1]:  # final slot may absorb a short tail
        assert slot.dur_s >= 0.8 - 1e-6
        assert slot.dur_s <= 3.0 + 0.6  # + one beat of clamp slack


def test_energy_literalness_low_flattens_density(sheet) -> None:
    hi = generate_cut_spec(sheet, Knobs(cut_density="dense", energy_literalness="high"))
    lo = generate_cut_spec(sheet, Knobs(cut_density="dense", energy_literalness="low"))
    hi_durs = {round(s.dur_s, 2) for s in hi.slots}
    lo_durs = {round(s.dur_s, 2) for s in lo.slots}
    assert len(lo_durs) <= len(hi_durs), "flat mapping must not increase variety"


def test_roles_and_passages_assigned(sheet) -> None:
    spec = generate_cut_spec(sheet, Knobs())
    roles = [s.role for s in spec.slots]
    assert roles[0] == "opening" and roles[-1] == "closing"
    assert roles.count("hero") == 1
    passages = [s.passage_index for s in spec.slots]
    assert passages == sorted(passages), "passage indices must be monotonic"
    assert all(0 <= p < max(len(sheet.phrases), 1) or p == sheet.phrases[-1].index
               for p in passages)


def test_deterministic(sheet) -> None:
    a = generate_cut_spec(sheet, Knobs()).model_dump_json()
    b = generate_cut_spec(sheet, Knobs()).model_dump_json()
    assert a == b
