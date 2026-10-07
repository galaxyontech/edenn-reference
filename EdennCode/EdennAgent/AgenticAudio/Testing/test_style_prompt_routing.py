"""The fused prompt has to land in the slot the tier actually reads.

Every music enqueue fuses the approved direction with what the footage is doing
— mood, tempo, a timed scene arc — because the direction prose alone produces
music unrelated to the video. That fused text was then dropped on the floor by
both deployments: the pipeline consumes ``music_style_prompt`` only under
``verbose_instruction`` (music_prompt_orchestration_stage reads it behind that
flag and nowhere else), nothing ever set the flag, and the standalone renderer
did not forward the field at all.

The flag is not free to set: the preprocessor REJECTS a verbose request whose
user_prompt is non-empty, and rejects it outright below the enhanced tier. So
routing is a decision, made once here and re-made by the standalone renderer
when a missing provider key forces a different tier than the one enqueued.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from EdennCode.EdennAgent.AgenticAudio.tools.media import AgenticAudioTools

route = AgenticAudioTools.route_style_prompt

FUSED = "Wistful piano at 96 BPM. 0–6s sunrise over the harbor; 6–12s the boats leave."


# ---------------------------------------------------------------------------#
# where the text goes                                                         #
# ---------------------------------------------------------------------------#


def test_a_verbose_capable_tier_gets_the_explicit_style_slot() -> None:
    routed = route(FUSED, modelspec="edenn_studio", user_prompt="cinematic and warm")

    assert routed["verbose_instruction"] is True
    assert routed["music_style_prompt"] == FUSED
    assert routed["user_prompt"] == "", (
        "verbose requests are rejected outright when user_prompt is non-empty"
    )


def test_the_basic_tier_gets_the_text_through_the_ordinary_prompt() -> None:
    # edenn_basic cannot take an explicit direction: setting the flag would fail
    # the job. The grounding still has somewhere to go.
    routed = route(FUSED, modelspec="edenn_basic", user_prompt="cinematic and warm")

    assert routed["verbose_instruction"] is False
    assert routed["user_prompt"] == FUSED, "the video grounding was dropped"
    assert routed["music_style_prompt"] == FUSED, (
        "the fused text is still the payload's record of what was fused"
    )


def test_no_fusion_leaves_the_direction_prompt_alone() -> None:
    # fuse_music_style_prompt returns nothing when there is no observation to
    # ground against; the session's own words are then all there is.
    routed = route(None, modelspec="edenn_studio", user_prompt="cinematic and warm")

    assert routed["user_prompt"] == "cinematic and warm"
    assert routed["verbose_instruction"] is False
    assert routed["music_style_prompt"] is None


def test_routing_is_idempotent_across_a_tier_substitution() -> None:
    """The standalone renderer re-routes against the tier it will really use.

    Carrying an enqueued verbose flag across a substitution to a tier that
    rejects explicit direction would fail the job instead of degrading it — the
    exact trap that makes this a re-decision rather than a pass-through.
    """
    enqueued = route(FUSED, modelspec="edenn_studio", user_prompt="cinematic")
    assert enqueued["verbose_instruction"] is True and enqueued["user_prompt"] == ""

    # Studio key missing at render time -> the substitute tier decides again.
    rerouted = route(
        enqueued["music_style_prompt"],
        modelspec="edenn_basic",
        user_prompt=enqueued["user_prompt"],
    )
    assert rerouted["verbose_instruction"] is False
    assert rerouted["user_prompt"] == FUSED, (
        "the substitution lost the grounding instead of degrading to the prompt"
    )

    # And re-routing at the SAME tier changes nothing.
    assert route(
        enqueued["music_style_prompt"],
        modelspec="edenn_studio",
        user_prompt=enqueued["user_prompt"],
    ) == enqueued


def test_a_legacy_tier_alias_still_routes_by_capability() -> None:
    routed = route(FUSED, modelspec="EDENN_STUDIO", user_prompt="x")
    assert routed["verbose_instruction"] is True


def test_an_unknown_tier_falls_back_to_the_safe_route() -> None:
    # normalize_music_modelspec resolves anything unrecognised to the basic tier,
    # which cannot take an explicit direction.
    routed = route(FUSED, modelspec="not_a_real_tier", user_prompt="x")
    assert routed["verbose_instruction"] is False
    assert routed["user_prompt"] == FUSED


# ---------------------------------------------------------------------------#
# the constant this all rests on                                              #
# ---------------------------------------------------------------------------#


def test_the_capability_set_matches_the_stage_that_enforces_it() -> None:
    """The routing mirrors a constant in another package, and the mirror is the
    single load-bearing assumption of the whole fix: add a tier there and the
    agent silently stops sending grounding for it; remove one and every enqueue
    on that tier fails validation outright."""
    from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.UserIntentUnderstandingStage.user_prompt_preprocessor import (  # noqa: E501
        _VALID_VERBOSE_MODELSPECS,
    )
    from EdennCode.EdennAgent.AgenticAudio.tools.media import VERBOSE_CAPABLE_MODELSPECS

    assert set(VERBOSE_CAPABLE_MODELSPECS) == set(_VALID_VERBOSE_MODELSPECS)


# ---------------------------------------------------------------------------#
# the call site (the helper being right proves nothing on its own)            #
# ---------------------------------------------------------------------------#


def _tools_with_queue(tmp_path: Path):
    from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (
        _MemoryAsyncRepository,
        _MemoryQueue,
        _seed_source_video,
    )
    from EdennCode.EdennAgent.AgenticAudio.tools import AgenticAudioTools

    async_repo = _MemoryAsyncRepository()
    queue = _MemoryQueue()
    source = _seed_source_video(async_repo)
    tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=SimpleNamespace(workdir=str(tmp_path), storage=None),
    )
    return tools, async_repo, source


def _enqueued_payload(tmp_path: Path, modelspec: str) -> dict:
    tools, async_repo, source = _tools_with_queue(tmp_path)
    cards = tools.generate_music_candidates(
        session_id="sess_routing",
        source_video_artifact_id=source.artifact_id,
        proposal={
            "proposal_id": "p1",
            "title": "Cinematic",
            "prompt": "Warm cinematic strings that swell at the turn.",
            "modelspec": modelspec,
        },
        count=1,
        observation={
            "duration_s": 20.0,
            "music_prompt": {"global_mood": "wistful, hopeful", "tempo_bpm": 96},
            "scenes": [{"start_s": 0, "end_s": 6, "label": "sunrise over the harbor"}],
        },
    )
    job = async_repo.get_job(cards[0].linked_job_id)
    return dict(job.request_json)


def test_a_studio_enqueue_actually_ships_the_verbose_slots(tmp_path: Path) -> None:
    """The helper being correct proves nothing unless the enqueue calls it — the
    defect was that the payload carried a style prompt nothing consumed."""
    payload = _enqueued_payload(tmp_path, "edenn_studio")

    assert payload["verbose_instruction"] is True
    assert payload["user_prompt"] == ""
    assert "96 BPM" in payload["music_style_prompt"], "the footage grounding is missing"
    assert "sunrise over the harbor" in payload["music_style_prompt"]


def test_a_basic_enqueue_ships_the_grounding_through_the_ordinary_prompt(
    tmp_path: Path,
) -> None:
    payload = _enqueued_payload(tmp_path, "edenn_basic")

    assert payload["verbose_instruction"] is False
    assert "96 BPM" in payload["user_prompt"], (
        "the basic tier lost the grounding entirely — the bug this fixes"
    )


def test_the_standalone_renderer_routes_before_it_generates() -> None:
    """The devserver's renderer is a closure inside create_app and cannot be
    imported, so its call site is pinned structurally — without it, a studio job
    would be generated from an empty prompt."""
    src = Path(
        "EdennCode/EdennAgent/AgenticAudio/design/devserver.py"
    ).read_text()
    assert "AgenticAudioTools.route_style_prompt(" in src
    assert "verbose_instruction=routed[" in src
    assert "music_style_prompt=routed[" in src
