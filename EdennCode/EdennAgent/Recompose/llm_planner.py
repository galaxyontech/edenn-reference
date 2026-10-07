"""LLM planning pass — the model makes the creative picks (owner directive).

Division of labor:
- the DETERMINISTIC machinery defines legality: beat-aligned slots, passage
  candidate pools, the HARD no-reuse rule, locks — and streams each slot's
  legal top-k as offers;
- the LLM chooses among offers for every slot in ONE schema-enforced call,
  and writes the passage focus/arc notes (user-facing prose);
- a sequential commit pass re-validates every choice against no-reuse (an
  earlier pick can invalidate a later offer) and auto-repairs illegal picks
  to the best legal deterministic candidate, marked ``[llm-repaired]``.

Client contract: ``await client.complete_messages(messages, json_schema=...)
-> (dict, usage)`` — the same AzureMultimodalClient the agentic vertical
uses (build_agentic_audio_agent_client), always injected, never constructed
here.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .domain import (
    AssetRecord,
    CutSpec,
    MusicSheet,
    RecomposePlan,
    SegmentNode,
    SegmentTree,
)
from .planner import _scene_ancestor, plan_recompose
from .understanding import arousal_score

logger = logging.getLogger(__name__)

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "node_id": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["index", "node_id", "why"],
                "additionalProperties": False,
            },
        },
        "passages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "semantic_focus": {"type": "string"},
                    "arc_note": {"type": "string"},
                },
                "required": ["index", "semantic_focus", "arc_note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["slots", "passages"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a music-video editor choosing shots for a beat-cut edit.
You receive timeline slots (beat-aligned, with musical energy and narrative role)
and for each slot a shortlist of legal candidate segments described in text.
Rules:
- choose exactly one candidate node_id per slot, FROM THAT SLOT'S SHORTLIST only;
- never choose the same node_id twice;
- REPETITION IS THE CARDINAL SIN: never place two shots from the same parent
  scene next to each other, spread any scene's material far apart, and prefer
  showing a NEW scene over returning to one already shown;
- serve the narrative: opening establishes the subject, energy builds with the
  music, the hero slot carries the strongest moment, the closing resolves calmly;
- keep each passage on its semantic focus; accents (from the secondary asset)
  are punctuation, not the story;
- when knobs.source_audio is "duck" the footage's ORIGINAL narration/dialogue
  stays audible under the music: place speech-bearing candidates (high
  "speech") in longer, lower-energy slots so sentences play out, and never
  chop speech into sub-second slots;
- 'why' is one short editorial phrase per slot (not a restatement of the rules).
Also write, per passage, a semantic_focus (a few words) and an arc_note (one
line of editorial intent). Respond with JSON only."""


def _slot_offer_payload(
    index: int,
    spec_slot,
    candidates: list[SegmentNode],
    trees: dict[str, SegmentTree],
) -> dict[str, Any]:
    return {
        "index": index,
        "role": spec_slot.role,
        "energy": round(spec_slot.energy, 2),
        "dur_s": round(spec_slot.dur_s, 2),
        "passage": spec_slot.passage_index,
        "candidates": [
            {
                "node_id": n.node_id,
                "text": (n.summary or "")[:110],
                "mood": (n.mood or "")[:40],
                "arousal": arousal_score(n.text()),
                "motion": n.motion,
                "speech": n.speech,  # original-audio narration likelihood
                "dur_s": round(n.dur_s, 2),
                # the visual family — the model must spread these apart
                "scene": n.node_id if n.is_still else _scene_ancestor(trees[n.asset_id], n),
                "still": n.is_still,
            }
            for n in candidates
        ],
    }


async def plan_recompose_llm(
    *,
    llm_client: Any,
    assets: list[AssetRecord],
    trees: dict[str, SegmentTree],
    sheet: MusicSheet,
    spec: CutSpec,
    hypothesis: str = "",
    spine_asset_id: Optional[str] = None,
    parent_plan_id: Optional[str] = None,
    max_tokens: int = 6000,
    exclude_intervals: Optional[dict[str, list[tuple[float, float]]]] = None,
) -> RecomposePlan:
    """LLM-planned recomposition: offers -> one model call -> validated commit."""

    # Pass 1 — deterministic dry run collects each slot's legal top-k offers.
    offers: dict[int, list[SegmentNode]] = {}
    spec_by_index = {s.index: s for s in spec.slots}

    def collect(index: int, candidates: list[SegmentNode]) -> None:
        offers[index] = candidates

    dry = plan_recompose(
        assets=assets, trees=trees, sheet=sheet, spec=spec,
        hypothesis=hypothesis, spine_asset_id=spine_asset_id,
        offer_collector=collect, exclude_intervals=exclude_intervals,
    )

    # Pass 2 — one schema-enforced model call over all slots + passages.
    user_payload = {
        "hypothesis": hypothesis or "coherent, musical edit of the material",
        "knobs": spec.knobs.model_dump(exclude={"locks"}),
        "music": {
            "tempo_bpm": sheet.tempo_bpm,
            "phrases": [p.model_dump() for p in sheet.phrases],
        },
        "slots": [
            _slot_offer_payload(i, spec_by_index[i], cands, trees)
            for i, cands in sorted(offers.items())
        ],
    }
    import json as _json

    async def _call(payload: dict[str, Any]) -> dict[str, Any]:
        decision, _usage = await llm_client.complete_messages(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _json.dumps(payload, ensure_ascii=False)},
            ],
            json_schema={"name": "recompose_plan", "strict": True, "schema": PLAN_SCHEMA},
            max_tokens=max_tokens,
        )
        return decision

    try:
        decision = await _call(user_payload)
    except Exception as exc:  # noqa: BLE001 - provider errors must not kill a render
        filtered = "content_filter" in str(exc) or "content management policy" in str(exc)
        if filtered:
            # Upstream safety filter false-positived on the scene text; retry
            # once with heavily compacted descriptions before giving up.
            logger.warning("llm plan: prompt filtered upstream; retrying compacted")
            compact = _json.loads(_json.dumps(user_payload))
            for slot in compact["slots"]:
                for cand in slot["candidates"]:
                    cand["text"] = cand["text"][:45]
                    cand.pop("mood", None)
            try:
                decision = await _call(compact)
            except Exception as exc2:  # noqa: BLE001
                logger.warning(
                    "llm plan unavailable (%s) — shipping the deterministic plan",
                    str(exc2)[:120],
                )
                return dry
        else:
            logger.warning(
                "llm plan unavailable (%s) — shipping the deterministic plan",
                str(exc)[:120],
            )
            return dry

    picks: dict[int, str] = {}
    whys: dict[int, str] = {}
    for item in decision.get("slots") or []:
        try:
            picks[int(item["index"])] = str(item["node_id"])
            whys[int(item["index"])] = str(item.get("why") or "")
        except (KeyError, TypeError, ValueError):
            continue

    # Pass 3 — sequential commit with legality repair (earlier picks can
    # invalidate later offers; plan_recompose re-checks free windows).
    plan = plan_recompose(
        assets=assets, trees=trees, sheet=sheet, spec=spec,
        hypothesis=hypothesis, spine_asset_id=spine_asset_id,
        parent_plan_id=parent_plan_id, pick_overrides=picks,
        exclude_intervals=exclude_intervals,
    )
    repaired = 0
    for slot in plan.slots:
        editorial = whys.get(slot.spec.index)
        if editorial and slot.node_id == picks.get(slot.spec.index):
            slot.why = f"[llm] {editorial}"
        elif "[llm-repaired]" in slot.why:
            repaired += 1
    if repaired:
        logger.info("llm plan: %d/%d picks auto-repaired for legality", repaired, len(plan.slots))

    for item in decision.get("passages") or []:
        try:
            idx = int(item["index"])
        except (KeyError, TypeError, ValueError):
            continue
        for passage in plan.passages:
            if passage.index == idx:
                passage.semantic_focus = str(item.get("semantic_focus") or passage.semantic_focus)
                passage.arc_note = str(item.get("arc_note") or passage.arc_note)

    del dry
    return plan
