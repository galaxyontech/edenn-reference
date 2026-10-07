"""CutSpec = f(MusicSheet, Knobs) — where user taste enters the pipeline.

Every cut lands on a beat; how MANY cuts is the user's call (cut_density),
how literally density tracks the energy curve is energy_literalness, and
min/max shot clamps keep any mapping watchable. W0 shipped the equivalent
of 'dense' (owner: too dense) — 'medium' is the default.
"""

from __future__ import annotations

import numpy as np

from .domain import CutSpec, Knobs, MusicSheet, SlotSpec

# beats-per-slot at (low, mid, high) local energy
DENSITY_BEATS: dict[str, tuple[int, int, int]] = {
    "sparse": (8, 6, 4),
    "medium": (6, 4, 2),
    "dense": (4, 2, 1),
}


def generate_cut_spec(sheet: MusicSheet, knobs: Knobs) -> CutSpec:
    beats = np.array(sheet.beats)
    energy = np.array(sheet.beat_energy)
    if len(beats) < 4:
        raise ValueError("MusicSheet has too few beats for a cut spec")

    lo_map, mid_map, hi_map = DENSITY_BEATS[knobs.cut_density]
    if knobs.energy_literalness == "low":
        # Flatten the mapping toward the middle: density barely follows energy.
        lo_map = hi_map = mid_map
    q1, q2 = np.quantile(energy, 0.33), np.quantile(energy, 0.66)

    slots: list[SlotSpec] = []
    i = 0
    while i < len(beats) - 1:
        e = energy[i]
        step = lo_map if e < q1 else (mid_map if e < q2 else hi_map)
        j = min(i + step, len(beats) - 1)
        # Clamp UP: extend to more beats until >= min_shot_s.
        while beats[j] - beats[i] < knobs.min_shot_s and j < len(beats) - 1:
            j += 1
        # Clamp DOWN: if we overshot max_shot_s, cut at the last beat inside it.
        while j > i + 1 and beats[j] - beats[i] > knobs.max_shot_s:
            j -= 1
        dur = float(beats[j] - beats[i])
        if dur <= 0:
            break
        if slots and dur < knobs.min_shot_s:  # tail too short: merge into previous
            slots[-1].dur_s = round(slots[-1].dur_s + dur, 4)
            break
        slots.append(SlotSpec(
            index=len(slots),
            t_start=round(float(beats[i] - beats[0]), 4),
            dur_s=round(dur, 4),
            energy=round(float(np.mean(energy[i:j])), 4),
        ))
        i = j

    _assign_passages(slots, sheet)
    _assign_roles(slots)
    return CutSpec(knobs=knobs, slots=slots)


def _assign_passages(slots: list[SlotSpec], sheet: MusicSheet) -> None:
    """A slot belongs to the phrase containing its midpoint (track time)."""

    for slot in slots:
        mid = sheet.window_start_s + slot.t_start + slot.dur_s / 2
        slot.passage_index = 0
        for ph in sheet.phrases:
            if ph.start_s <= mid < ph.end_s:
                slot.passage_index = ph.index
                break
        else:
            if sheet.phrases and mid >= sheet.phrases[-1].end_s:
                slot.passage_index = sheet.phrases[-1].index


def _assign_roles(slots: list[SlotSpec]) -> None:
    if not slots:
        return
    slots[0].role = "opening"
    slots[-1].role = "closing"
    body = slots[1:-1]
    if body:
        hero = max(body, key=lambda s: s.energy)
        hero.role = "hero"
