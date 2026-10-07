"""The spotting sheet: one moment list, one owner per moment.

In film post, the spotting session is where the director, composer and sound
designer watch the cut together and agree what each moment needs. This module is
that session, made explicit, because without it the layers plan blind and collide
in the mix: on a 16s reference reel, four of seven sound-design beats fired
underneath narration, unplanned by anyone, while both layers independently
targeted the same 14.9s climax.

The sheet is built for every session — it is not a mode and not a card the user
has to answer. Ownership is proposed here from the picture alone, and the agent
or the user may reassign it; what must never happen is two layers arriving at the
same moment without either of them knowing.
"""

from __future__ import annotations

from typing import Any

# Owners a moment can have. ``silence`` is a real assignment, not the absence of
# one: "let the footage play here" is a decision, and it needs somewhere to live.
OWNER_NARRATE = "narrate"
OWNER_SFX = "sfx"
OWNER_MUSIC = "music"
OWNER_SILENCE = "silence"
OWNERS = (OWNER_NARRATE, OWNER_SFX, OWNER_MUSIC, OWNER_SILENCE)

# A final shot shorter than this cannot hold a spoken line, so the button is not
# offered and the moment stays with sound design.
MIN_BUTTON_WINDOW_S = 1.0


def build_spotting_sheet(observation: dict[str, Any]) -> dict[str, Any]:
    """Derive the moment list and a proposed owner for each, from the picture.

    Only signals we actually trust are used. Cuts come from the detector and
    carry their provenance: when ``cut_source`` is not the real detector the
    sheet still exists, but it says so, and downstream must treat its timings as
    advisory rather than as hard constraints.
    """

    duration = float(observation.get("duration_s") or 0.0)
    cuts = [float(c) for c in (observation.get("cuts") or []) if 0.05 < float(c) < duration]
    cut_source = str(observation.get("cut_source") or "unavailable")
    scenes = observation.get("scenes") or []

    def scene_at(t: float) -> dict[str, Any]:
        for sc in scenes:
            try:
                start = float(sc.get("start_timestamp"))
                end = float(sc.get("end_timestamp"))
            except (TypeError, ValueError):
                continue
            if start <= t < end:
                return sc
        return {}

    moments: list[dict[str, Any]] = []

    # Every cut is a moment: it is the one instant the audience's attention is
    # guaranteed to move, which is why sound design lives on them.
    for i, t in enumerate(cuts):
        nxt = cuts[i + 1] if i + 1 < len(cuts) else duration
        sc = scene_at(t)
        moments.append({
            "id": f"moment_{len(moments) + 1:02d}",
            "t": round(t, 2),
            "window": [round(t, 2), round(min(nxt, duration), 2)],
            "what": (str(sc.get("visual_summary") or "").strip()[:120]
                     or "the picture changes"),
            "mood": str(sc.get("mood") or "").strip(),
            "source": "cut",
            "owner": OWNER_SFX,
            "owner_source": "proposed",
        })

    # The last shot is the piece's ending, and endings are decided, never drifted
    # into. It gets its own moment so the choice — land a line on it, or hold
    # silence and let the picture close — is recorded rather than implied.
    if duration > 0:
        finale_start = cuts[-1] if cuts else 0.0
        window = duration - finale_start
        sc = scene_at(finale_start)
        if moments and cuts and abs(moments[-1]["t"] - finale_start) < 0.01:
            # The final cut IS the finale; promote it rather than duplicating it.
            moments[-1].update({
                "source": "finale",
                "what": (str(sc.get("visual_summary") or "").strip()[:120]
                         or "the closing image"),
                "owner": OWNER_NARRATE if window >= MIN_BUTTON_WINDOW_S else OWNER_SFX,
            })
        else:
            moments.append({
                "id": f"moment_{len(moments) + 1:02d}",
                "t": round(finale_start, 2),
                "window": [round(finale_start, 2), round(duration, 2)],
                "what": (str(sc.get("visual_summary") or "").strip()[:120]
                         or "the closing image"),
                "mood": str(sc.get("mood") or "").strip(),
                "source": "finale",
                "owner": OWNER_NARRATE if window >= MIN_BUTTON_WINDOW_S else OWNER_SFX,
                "owner_source": "proposed",
            })

    # The footage's own voice. These are not proposals: everything else on this
    # sheet is a suggestion about where our layers COULD go, whereas a stretch
    # where the clip is already speaking is somewhere they must not. Ranked
    # first, because talking over the subject is the most obvious mistake the
    # system can make and the one it was previously incapable of noticing.
    for window in observation.get("speech_windows") or []:
        try:
            lo, hi = float(window[0]), float(window[1])
        except (TypeError, ValueError, IndexError):
            continue
        if hi <= lo:
            continue
        moments.append({
            "id": f"moment_{len(moments) + 1:02d}",
            "t": round(lo, 2),
            "window": [round(lo, 2), round(min(hi, duration or hi), 2)],
            "what": "the footage is already making sound here",
            "mood": "",
            "source": "source_audio",
            "owner": OWNER_SILENCE,
            "owner_source": "footage",
            "reason": "the clip speaks for itself here",
        })

    # Rank: the ending first, then chronologically. Rank drives what a planner
    # protects when it cannot have everything.
    def _priority(moment: dict[str, Any]) -> int:
        if moment["source"] == "source_audio":
            return 0        # a place to keep out of outranks any place to fill
        return 1 if moment["source"] == "finale" else 2

    order = sorted(
        range(len(moments)),
        key=lambda i: (_priority(moments[i]), moments[i]["t"]),
    )
    for rank, idx in enumerate(order, start=1):
        moments[idx]["rank"] = rank

    return {
        "moments": moments,
        "cut_source": cut_source,
        "reliable": cut_source == "pyscenedetect" and bool(cuts),
        "duration_s": round(duration, 2),
    }


def moments_owned_by(sheet: dict[str, Any], owner: str) -> list[dict[str, Any]]:
    return [m for m in (sheet.get("moments") or []) if m.get("owner") == owner]


def set_moment_owner(
    sheet: dict[str, Any],
    moment_id: str,
    owner: str,
    *,
    source: str,
    reason: str = "",
) -> dict[str, Any] | None:
    """Reassign one moment, keeping what it was and who changed it.

    Ownership is a decision, and decisions are worth being able to read back —
    especially the quiet ones. ``silence`` in particular is not the absence of a
    choice: it means somebody decided the picture carries this beat alone, and
    that has to be distinguishable from a moment nobody got round to.

    ``source`` says who: ``user`` outranks anything the system proposed, and an
    owner set by the user is never silently re-derived. Mirrors the treatment
    card's ``revised_from`` provenance rule.

    Returns the updated moment, or None when the id is unknown or the owner is
    not a real one.
    """

    if owner not in OWNERS:
        return None
    for m in sheet.get("moments") or []:
        if str(m.get("id")) != str(moment_id):
            continue
        if m.get("owner_source") == "user" and source != "user":
            # The user decided this one. Telling the model it outranks them is
            # not enough — a live run showed the writer agreeing with a user's
            # choice and still restamping it as its own, which quietly loses the
            # fact that a person chose it. Refuse the write instead.
            return None
        if m.get("owner") != owner or reason:
            m["revised_from"] = {
                "owner": m.get("owner"),
                "owner_source": m.get("owner_source"),
            }
        m["owner"] = owner
        m["owner_source"] = source
        if reason:
            m["reason"] = reason
        return m
    return None


def narration_conflicts(
    sheet: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    words_per_second: float = 2.1,
) -> list[dict[str, Any]]:
    """Narration lines that cover a moment another layer owns.

    Speaking across someone else's moment is not automatically wrong — a line can
    sit deliberately underneath an effect — but it has to be a decision. This
    reports the overlaps so it can be one.
    """

    out = []
    for seg in segments or []:
        try:
            start = float(seg.get("start_s") or 0.0)
        except (TypeError, ValueError):
            continue
        dur = seg.get("duration_s")
        try:
            dur = float(dur) if dur else len(str(seg.get("text") or "").split()) / words_per_second
        except (TypeError, ValueError):
            continue
        end = start + dur
        for m in sheet.get("moments") or []:
            if m.get("owner") in (OWNER_NARRATE, OWNER_MUSIC):
                continue
            t = float(m.get("t") or 0.0)
            if start <= t <= end:
                out.append({
                    "segment_id": seg.get("id"),
                    "moment_id": m.get("id"),
                    "moment_t": t,
                    "owner": m.get("owner"),
                    "what": m.get("what"),
                })
    return out


def sfx_conflicts(
    sheet: dict[str, Any],
    sfx_events: list[dict[str, Any]],
    narration_segments: list[dict[str, Any]],
    *,
    words_per_second: float = 2.1,
) -> list[dict[str, Any]]:
    """Sound-design beats that fire underneath narration, or on a narrated moment.

    This is the collision that the mix cannot fix and the user always hears: an
    accent landing under a spoken line either fights the voice or disappears.

    Nothing collides with narration that does not exist. A session with no
    narration written has no claim to defend — the finale's default owner is a
    proposal about where a line *would* go, not a reservation that should keep
    sound design off the ending of a piece that will never have a voice.
    Protected silence is the exception: it holds whether or not anyone speaks,
    because it is a decision about the picture rather than about the voice.
    """

    spoken: list[tuple[float, float, Any]] = []
    for seg in narration_segments or []:
        try:
            start = float(seg.get("start_s") or 0.0)
            dur = seg.get("duration_s")
            dur = float(dur) if dur else len(str(seg.get("text") or "").split()) / words_per_second
        except (TypeError, ValueError):
            continue
        spoken.append((start, start + dur, seg.get("id")))

    # A moment owns a WINDOW, not an instant: an effect 0.1s after the final cut
    # is landing on the closing image just as surely as one exactly on it.
    #
    # But ownership only reserves a moment that narration ACTUALLY uses. A live
    # run ended with the closing image carrying nothing at all: narration held
    # the finale, chose not to speak over it, and the reservation still kept the
    # sound-design hit away — so the piece ended on silence nobody asked for.
    # An owner that declines its moment releases it.
    narrated: list[tuple[float, float]] = []
    if spoken:
        for m in moments_owned_by(sheet, OWNER_NARRATE):
            win = m.get("window") or []
            try:
                lo, hi = float(win[0]), float(win[1])
            except (TypeError, ValueError, IndexError):
                lo = hi = float(m.get("t") or 0.0)
            if any(s_start < hi and s_end > lo for s_start, s_end, _ in spoken):
                narrated.append((lo, hi))

    # Protected silence, unlike narration's claim, is never released: the whole
    # point of the decision is that this beat plays with nothing on top of it.
    held: list[tuple[float, float]] = []
    for m in moments_owned_by(sheet, OWNER_SILENCE):
        win = m.get("window") or []
        try:
            held.append((float(win[0]), float(win[1])))
        except (TypeError, ValueError, IndexError):
            t = float(m.get("t") or 0.0)
            held.append((t, t))

    out = []
    for ev in sfx_events or []:
        try:
            t = float(ev.get("start_s"))
        except (TypeError, ValueError):
            continue
        if any(lo <= t <= hi for lo, hi in held):
            out.append({
                "event": str(ev.get("label") or ev.get("prompt") or "effect")[:40],
                "start_s": round(t, 2),
                "reason": "lands on a moment held silent on purpose",
                "segment_id": None,
            })
            continue
        for start, end, seg_id in spoken:
            if start <= t <= end:
                out.append({
                    "event": str(ev.get("label") or ev.get("prompt") or "effect")[:40],
                    "start_s": round(t, 2),
                    "reason": "fires under narration",
                    "segment_id": seg_id,
                })
                break
        else:
            if any(lo <= t <= hi for lo, hi in narrated):
                out.append({
                    "event": str(ev.get("label") or ev.get("prompt") or "effect")[:40],
                    "start_s": round(t, 2),
                    "reason": "lands on a moment narration owns",
                    "segment_id": None,
                })
    return out


def free_windows(
    sheet: dict[str, Any],
    narration_segments: list[dict[str, Any]],
    *,
    duration_s: float,
    pad_s: float = 0.25,
    words_per_second: float = 2.1,
) -> list[list[float]]:
    """Stretches where an effect can land without fighting anything.

    Everything narration is speaking over, plus the windows it owns, minus a
    little padding either side so an accent does not clip the edge of a word.
    Reported alongside a collision so the fix is concrete: a planner told only
    "this is wrong" tends to move the effect somewhere equally wrong.
    """

    busy: list[tuple[float, float]] = []
    spoken_spans: list[tuple[float, float]] = []
    for seg in narration_segments or []:
        try:
            start = float(seg.get("start_s") or 0.0)
            dur = seg.get("duration_s")
            dur = float(dur) if dur else len(str(seg.get("text") or "").split()) / words_per_second
        except (TypeError, ValueError):
            continue
        busy.append((max(0.0, start - pad_s), start + dur + pad_s))
        # Unpadded: the release test below asks whether narration REALLY reaches
        # a moment, and the padding would make a line that stops 0.2s short look
        # as though it had claimed it.
        spoken_spans.append((start, start + dur))
    # A beat somebody chose to hold silent is not free space.
    for m in moments_owned_by(sheet, OWNER_SILENCE):
        win = m.get("window") or []
        try:
            busy.append((float(win[0]) - pad_s, float(win[1])))
        except (TypeError, ValueError, IndexError):
            continue
    # Same rule as the collision check: a moment narration owns but never speaks
    # over is free, or the ending is left carrying nothing at all.
    for m in moments_owned_by(sheet, OWNER_NARRATE):
        win = m.get("window") or []
        try:
            lo, hi = float(win[0]), float(win[1])
        except (TypeError, ValueError, IndexError):
            continue
        if any(s_lo < hi and s_hi > lo for s_lo, s_hi in spoken_spans):
            busy.append((lo - pad_s, hi))

    busy.sort()
    merged: list[list[float]] = []
    for lo, hi in busy:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])

    out, cursor = [], 0.0
    for lo, hi in merged:
        if lo - cursor > 0.4:
            out.append([round(cursor, 2), round(lo, 2)])
        cursor = max(cursor, hi)
    if duration_s - cursor > 0.4:
        out.append([round(cursor, 2), round(duration_s, 2)])
    return out


__all__ = [
    "build_spotting_sheet",
    "free_windows",
    "moments_owned_by",
    "narration_conflicts",
    "sfx_conflicts",
    "OWNER_NARRATE",
    "OWNER_SFX",
    "OWNER_MUSIC",
    "OWNER_SILENCE",
    "OWNERS",
]
