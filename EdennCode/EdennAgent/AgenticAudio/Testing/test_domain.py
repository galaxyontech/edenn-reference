"""Unit tests for the typed domain layer (User / Artifacts over state_json)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from EdennCode.EdennAgent.AgenticAudio.domain import (
    CandidateGraph,
    FinalArtifact,
    Mix,
    ProductionPlan,
    SessionState,
    User,
    VoiceoverLayer,
)


# --------------------------------------------------------------------------- #
# User                                                                        #
# --------------------------------------------------------------------------- #


def test_user_from_session_and_anonymity() -> None:
    assert User.from_session(SimpleNamespace(creator_user_id="u1")) == User(id="u1")
    assert User(id="u1").is_anonymous is False
    assert User().is_anonymous is True
    assert User.from_session(SimpleNamespace(creator_user_id=None)).is_anonymous is True


# --------------------------------------------------------------------------- #
# CandidateGraph (the branching graph)                                        #
# --------------------------------------------------------------------------- #


def _candidates() -> list[dict]:
    return [
        {"candidate_id": "c1", "version": 1},
        {"candidate_id": "c1_v2", "parent_candidate_id": "c1", "version": 2, "edit_kind": "regenerate"},
        {"candidate_id": "c1_v3", "parent_candidate_id": "c1", "version": 3, "edit_kind": "creative_edit"},
        {"candidate_id": "c2", "version": 1},
    ]


def test_candidate_graph_find_and_children() -> None:
    graph = CandidateGraph(_candidates())
    assert graph.find("c1")["candidate_id"] == "c1"
    assert graph.find("missing") is None
    children = [c["candidate_id"] for c in graph.children_of("c1")]
    assert children == ["c1_v2", "c1_v3"]
    assert graph.children_of("c2") == []


def test_candidate_graph_next_version() -> None:
    graph = CandidateGraph(_candidates())
    # Two children at v2 and v3 -> next branch off c1 is v4.
    assert graph.next_version("c1", graph.find("c1")) == 4
    # A leaf with no children -> parent version + 1.
    assert graph.next_version("c2", graph.find("c2")) == 2


def test_candidate_graph_resolve() -> None:
    graph = CandidateGraph(_candidates())
    candidate, cid = graph.resolve("c1_v2")
    assert cid == "c1_v2" and candidate["edit_kind"] == "regenerate"
    # Ambiguous + no id -> KeyError (multiple candidates).
    with pytest.raises(KeyError):
        graph.resolve("")
    # Unknown id but exactly one candidate -> returns the only one.
    single = CandidateGraph([{"candidate_id": "solo"}])
    only, only_id = single.resolve("")
    assert only_id == "solo"


def test_candidate_graph_typed_view() -> None:
    # typed() lifts the raw dicts to full Candidate models, which require the
    # card fields real candidates always carry (set at creation).
    full = [
        {"candidate_id": "c1", "proposal_id": "p1", "title": "A", "prompt": "x", "modelspec": "edenn_basic", "version": 1},
        {"candidate_id": "c1_v2", "proposal_id": "p1", "title": "A", "prompt": "x", "modelspec": "edenn_basic",
         "parent_candidate_id": "c1", "version": 2, "edit_kind": "creative_edit"},
    ]
    typed = CandidateGraph(full).typed()
    assert typed[0].candidate_id == "c1"
    assert typed[1].edit_kind == "creative_edit"


# --------------------------------------------------------------------------- #
# SessionState                                                                #
# --------------------------------------------------------------------------- #


def test_session_state_resolve_proposal_stored_inline_and_missing() -> None:
    state = SessionState({"proposals": [{"proposal_id": "p1", "title": "Cinematic", "prompt": "...", "modelspec": "edenn_basic"}]})
    assert state.resolve_proposal({"proposal_id": "p1"})["title"] == "Cinematic"
    # Inline plan when the id is unknown but a prompt is supplied.
    inline = state.resolve_proposal({"prompt": "warm lofi", "modelspec": "edenn_enhanced"})
    assert inline["proposal_id"] == "proposal_inline" and inline["prompt"] == "warm lofi"
    with pytest.raises(KeyError):
        state.resolve_proposal({"proposal_id": "nope"})


def test_session_state_typed_singletons_roundtrip_is_lossless() -> None:
    raw = {
        "mix": {"music_volume": 0.5, "video_url": "http://x", "future_key": "keep-me"},
        "layers": {"voiceover": {"script": "hi", "voice_id": "warm_female", "extra": 1}},
        "final_artifact": {"video_url": "http://final", "deliverable": "compose_mix"},
        "production_plan": {"mode": "music_first", "layers": ["music"], "vendor_note": "x"},
        "approved_direction": True,
    }
    state = SessionState(raw)
    assert state.approved_direction is True
    # Typed views parse...
    assert state.mix.music_volume == 0.5
    assert state.voiceover.script == "hi"
    assert state.final_artifact.deliverable == "compose_mix"
    assert state.production_plan.layers == ["music"]
    # ...and round-trip without dropping unknown keys (byte-safe over state_json).
    assert state.mix.model_dump()["future_key"] == "keep-me"
    assert state.voiceover.model_dump()["extra"] == 1
    assert state.production_plan.model_dump()["vendor_note"] == "x"


def test_session_state_empty_singletons_are_none() -> None:
    state = SessionState({})
    assert state.mix is None
    assert state.voiceover is None
    assert state.final_artifact is None
    assert state.production_plan is None
    assert state.candidates == []
    assert len(state.graph) == 0
