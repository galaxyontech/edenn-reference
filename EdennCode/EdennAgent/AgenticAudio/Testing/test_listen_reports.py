"""Listening back to the layers that nobody was listening to.

Narration has had a listen-back report since the day it shipped: on every
hydrate it says what the listener actually got. Music — the expensive layer, the
spine of the product — had nothing. A take was hydrated blind: a status, a URL,
and a stall flag. Whether the music stopped six seconds before the video ended,
opened on three seconds of silence, or had a hole in the middle was invisible
until somebody played the file. SFX was worse: its render did not even report
what it had placed.

The split from narration_alignment is kept deliberately. FAULTS go in notes and
are worth acting on. The SHAPE of a take — how its energy moves — goes in
observations and is the music being music; an agent handed that as a defect
regenerates takes that were never wrong.
"""

from __future__ import annotations

from EdennCode.EdennAgent.AgenticAudio.tools.media import (
    music_alignment,
    sfx_render_diff,
)


VIDEO_20S = {"duration_s": 20.0}


# ---------------------------------------------------------------------------#
# music: faults                                                               #
# ---------------------------------------------------------------------------#


def test_music_that_stops_before_the_video_ends_is_a_fault() -> None:
    report = music_alignment(
        {"cut_duration_s": 14.0, "leading_silence_s": 0.0, "trailing_silence_s": 0.0},
        observation=VIDEO_20S,
    )
    assert report["clean"] is False
    assert any("6.0s before the video ends" in note for note in report["notes"])


def test_a_silent_opening_is_a_fault() -> None:
    report = music_alignment(
        {"cut_duration_s": 20.0, "leading_silence_s": 2.4, "trailing_silence_s": 0.1},
        observation=VIDEO_20S,
    )
    assert any("opens with 2.4s of silence" in note for note in report["notes"])


def test_a_dead_tail_is_a_fault() -> None:
    report = music_alignment(
        {"cut_duration_s": 20.0, "leading_silence_s": 0.0, "trailing_silence_s": 3.2},
        observation=VIDEO_20S,
    )
    assert any("ends with 3.2s of silence" in note for note in report["notes"])


def test_a_hole_in_the_middle_is_a_fault() -> None:
    report = music_alignment(
        {"cut_duration_s": 20.0, "leading_silence_s": 0.0, "trailing_silence_s": 0.0,
         "internal_gaps_s": [2.6]},
        observation=VIDEO_20S,
    )
    assert any("gap of near-silence" in note for note in report["notes"])


def test_a_take_that_fits_its_video_is_clean() -> None:
    report = music_alignment(
        {"cut_duration_s": 20.0, "leading_silence_s": 0.1, "trailing_silence_s": 0.4},
        observation=VIDEO_20S,
    )
    assert report["clean"] is True
    assert report["notes"] == []


# ---------------------------------------------------------------------------#
# music: shape is an observation, never a fault                               #
# ---------------------------------------------------------------------------#


def test_the_energy_arc_is_reported_as_shape_not_as_a_defect() -> None:
    building = music_alignment(
        {
            "cut_duration_s": 20.0, "leading_silence_s": 0.0, "trailing_silence_s": 0.0,
            "energy_windows_db": [-30, -28, -26, -22, -18, -14, -12, -10, -9],
        },
        observation=VIDEO_20S,
    )
    assert building["clean"] is True, "a take that builds is not broken"
    assert any("builds" in text for text in building["observations"])

    settling = music_alignment(
        {
            "cut_duration_s": 20.0, "leading_silence_s": 0.0, "trailing_silence_s": 0.0,
            "energy_windows_db": [-9, -10, -12, -16, -20, -24, -26, -28, -30],
        },
        observation=VIDEO_20S,
    )
    assert settling["clean"] is True
    assert any("settles" in text for text in settling["observations"])


# ---------------------------------------------------------------------------#
# music: honesty about not knowing                                            #
# ---------------------------------------------------------------------------#


def test_no_measurements_means_no_report_rather_than_an_invented_one() -> None:
    assert music_alignment({}) == {}
    assert music_alignment(None) == {}


def test_the_report_is_deterministic() -> None:
    # It is recomputed on every 2.5s poll and compared by equality; an unstable
    # value would rewrite the session on every tick, forever.
    signals = {
        "cut_duration_s": 19.333333, "leading_silence_s": 0.0,
        "trailing_silence_s": 1.66666, "energy_windows_db": [-20, -18, -15, -12, -19],
    }
    assert music_alignment(signals, observation=VIDEO_20S) == music_alignment(
        signals, observation=VIDEO_20S
    )


def test_a_missing_video_duration_does_not_invent_a_truncation_fault() -> None:
    report = music_alignment(
        {"cut_duration_s": 8.0, "leading_silence_s": 0.0, "trailing_silence_s": 0.0},
        observation={},
    )
    assert report["clean"] is True, "with no video length there is nothing to compare against"


# ---------------------------------------------------------------------------#
# sfx: plan vs render                                                         #
# ---------------------------------------------------------------------------#


def test_a_faithful_render_matches_every_planned_moment() -> None:
    planned = [{"id": "e1", "label": "door", "start_s": 3.0},
               {"id": "e2", "label": "step", "start_s": 8.0}]
    rendered = [{"id": "e1", "label": "door", "start_s": 3.0},
                {"id": "e2", "label": "step", "start_s": 8.0}]

    diff = sfx_render_diff(planned, rendered)

    assert diff["matched"] == 2
    assert diff["moved"] == [] and diff["not_rendered"] == [] and diff["unplanned"] == 0


def test_a_moved_hit_is_named_with_how_far_it_moved() -> None:
    diff = sfx_render_diff(
        [{"id": "e1", "label": "door", "start_s": 3.0}],
        [{"id": "e1", "label": "door", "start_s": 3.2}],
    )
    assert diff["matched"] == 1
    assert any("moved 0.2s" in entry for entry in diff["moved"])


def test_an_engine_render_that_ignored_the_plan_is_a_diff_not_a_verdict() -> None:
    # Under engine spotting this is the DESIGNED behaviour. The shape of the
    # result has to let the agent describe it without calling it a failure.
    diff = sfx_render_diff(
        [{"id": "e1", "label": "door", "start_s": 3.0}],
        [{"id": "x1", "label": "wind", "start_s": 11.0},
         {"id": "x2", "label": "hum", "start_s": 15.0}],
    )
    assert diff["not_rendered"] == ["door"]
    assert diff["unplanned"] == 2
    assert "clean" not in diff, "a diff must not present itself as a pass/fail verdict"


def test_a_render_that_placed_nothing_is_the_loudest_diff_of_all() -> None:
    """No manifest at all (a legacy job, a placeholder) means there is nothing
    to compare. An EMPTY manifest is a different thing entirely — a real render
    that placed none of the approved moments — and collapsing the two hid
    exactly the failure the manifest exists to expose."""
    assert sfx_render_diff([], None) == {}, "no manifest, nothing to say"

    diff = sfx_render_diff([{"id": "e1", "label": "door", "start_s": 1.0}], [])
    assert diff["matched"] == 0
    assert diff["not_rendered"] == ["door"]


def test_an_effect_that_produced_no_audio_is_not_a_match() -> None:
    # Per-event failures are swallowed so one bad effect cannot sink a batch,
    # which means a provider outage returns a full manifest of silent events.
    # Matching on time alone called that a faithful render.
    diff = sfx_render_diff(
        [{"id": "e1", "label": "door", "start_s": 3.0}],
        [{"id": "e1", "label": "door", "start_s": 3.0, "rendered": False}],
    )
    assert diff["matched"] == 0
    assert diff["silent"] == ["door"]


# ---------------------------------------------------------------------------#
# the mix: the artifact the user actually keeps                               #
# ---------------------------------------------------------------------------#


def test_a_master_cut_short_of_the_picture_is_the_loudest_fault() -> None:
    """This one has shipped more than once, and the only way anyone found out
    was by playing the file."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import mix_alignment

    report = mix_alignment(
        {"cut_duration_s": 14.0, "leading_silence_s": 0.0, "trailing_silence_s": 0.0},
        observation=VIDEO_20S,
    )
    assert report["clean"] is False
    assert any("6.0s short of the video" in note for note in report["notes"])


def test_a_mix_that_is_silent_throughout_says_only_that() -> None:
    """A deliverable that looks finished in every field the UI reads. It should
    not also come with a list of secondary complaints."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import mix_alignment

    report = mix_alignment(
        {"cut_duration_s": 20.0, "leading_silence_s": 20.0, "trailing_silence_s": 20.0},
        observation=VIDEO_20S,
    )
    assert report["notes"] == ["the mix is silent for its whole 20.0s"]


def test_a_master_that_fits_and_plays_is_clean() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.media import mix_alignment

    report = mix_alignment(
        {"cut_duration_s": 20.1, "leading_silence_s": 0.2, "trailing_silence_s": 0.3,
         "energy_windows_db": [-18, -16, -14, -15, -17]},
        observation=VIDEO_20S,
    )
    assert report["clean"] is True
    assert report["notes"] == []


def test_the_mix_report_never_claims_to_hear_the_balance() -> None:
    """Per-line duck depth is folded into one graph with everything else and is
    not measurable from the finished file. A report that implied otherwise
    would be the same false confidence this work exists to remove."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import mix_alignment

    report = mix_alignment(
        {"cut_duration_s": 20.0, "leading_silence_s": 0.0, "trailing_silence_s": 0.0,
         "energy_windows_db": [-20, -18, -16, -18, -20]},
        observation=VIDEO_20S,
        preserve_original_audio=True,
    )
    text = " ".join(report["notes"] + report["observations"]).lower()
    for word in ("duck", "ducking", "balance", "under the narration"):
        assert word not in text, f"the report claimed to hear {word!r}"
    assert any("own audio is kept" in o for o in report["observations"])


def test_clipping_is_a_fault_and_a_quiet_mix_is_only_an_observation() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.media import mix_alignment

    hot = mix_alignment(
        {"cut_duration_s": 20.0, "leading_silence_s": 0.0, "trailing_silence_s": 0.0,
         "energy_windows_db": [-3, -2, -0.5, -2, -3]},
        observation=VIDEO_20S,
    )
    assert any("clipping" in note for note in hot["notes"])

    quiet = mix_alignment(
        {"cut_duration_s": 20.0, "leading_silence_s": 0.0, "trailing_silence_s": 0.0,
         "energy_windows_db": [-45, -44, -46, -44, -45]},
        observation=VIDEO_20S,
    )
    assert quiet["clean"] is True, "quiet is a choice, not a defect"
    assert any("very quiet" in o for o in quiet["observations"])


def test_the_mix_report_is_deterministic() -> None:
    """It rides the same 2.5s poll every other report does."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import mix_alignment

    signals = {"cut_duration_s": 19.3333, "leading_silence_s": 0.0,
               "trailing_silence_s": 1.66666, "energy_windows_db": [-20, -18, -15]}
    assert mix_alignment(signals, observation=VIDEO_20S) == mix_alignment(
        signals, observation=VIDEO_20S
    )


def test_no_measurements_means_no_verdict_on_the_deliverable() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.media import mix_alignment

    assert mix_alignment({}) == {}
    assert mix_alignment(None) == {}


# ---------------------------------------------------------------------------#
# the effects bed: the one rendered artifact nobody listened back to           #
# ---------------------------------------------------------------------------#


def test_an_effect_that_came_back_silent_is_reported() -> None:
    """"Rendered" has only ever meant the provider returned a path. An effect
    that came back as silence looked exactly like one that worked — on the
    card, in the manifest, in the session — and the only way to find out was to
    play the video."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import sfx_listen_report

    report = sfx_listen_report({"silent_event_ids": ["sfx_2"], "bed_peak_dbfs": -8.0})

    assert report["clean"] is False
    assert any("silent" in note for note in report["notes"])
    assert "sfx_2" in " ".join(report["notes"])


def test_a_bed_at_full_scale_is_reported() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.media import sfx_listen_report

    assert sfx_listen_report({"bed_peak_dbfs": -0.1})["clean"] is False
    assert sfx_listen_report({"bed_peak_dbfs": -12.0})["clean"] is True


def test_what_was_not_measured_is_named_rather_than_guessed() -> None:
    """A critic that invents a judgement from a failed measurement is worse
    than one that says it did not check."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import sfx_listen_report

    report = sfx_listen_report({"unmeasured_event_ids": ["sfx_1", "sfx_2"]})

    assert report["clean"] is True, "unmeasured is not a fault"
    assert any("could not be measured" in item for item in report["unchecked"])
    assert any("suits its moment" in item for item in report["unchecked"]), (
        "craft is never judged by arithmetic"
    )


def test_wildly_uneven_effect_levels_are_an_observation_not_a_fault() -> None:
    """The FAULTS-versus-observations split exists so a critic cannot flatten
    the work it was meant to protect: a quiet effect under a loud one may be
    exactly what the moment wants."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import sfx_listen_report

    report = sfx_listen_report(
        {"event_level_dbfs": {"a": -6.0, "b": -40.0}, "bed_peak_dbfs": -9.0}
    )

    assert report["clean"] is True
    assert any("different levels" in item for item in report["observations"])


def test_the_bed_is_measured_at_render_and_judged_on_hydrate(tmp_path) -> None:
    """The split that makes this safe: hydration runs on a 2.5s poll inside a
    locked read-modify-write, so it may never probe a file."""
    from EdennCode.EdennAgent.AgenticAudio.tools import media as media_module

    from pathlib import Path as _Path

    source = _Path(media_module.__file__).read_text()
    hydrate = source.split("def hydrate_sfx_layer(")[1].split("\n    def ")[0]

    assert "sfx_listen_report(" in hydrate
    for probe in ("_peak_and_mean_db", "sfx_take_signals", "subprocess"):
        assert probe not in hydrate, f"hydration probes files via {probe}"


def test_a_sound_effects_take_is_not_chosen_before_it_exists() -> None:
    """The layer marked a variant chosen the moment it was enqueued — before it
    rendered, before anyone could hear it. The card showed a pick the user had
    not made, and a compose running before the render landed would have mixed a
    take with no audio in it."""
    from pathlib import Path as _Path

    impls = _Path("EdennCode/EdennAgent/AgenticAudio/tools/impls.py").read_text()
    enqueue = impls.split("class GenerateSfxTool")[1].split("\nclass ")[0]
    update = enqueue.split("sfx.update(")[1].split(")")[0]

    assert "selected_variant_id" not in update, (
        "the enqueue still claims a choice nobody has made"
    )


def test_the_first_take_that_finishes_becomes_the_working_choice() -> None:
    """Deterministic — first in order, not newest — because two hydrate passes
    over the same rows have to agree, or the 2.5s poll rewrites the session on
    every tick."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import AgenticAudioTools

    layer = {
        "variants": [
            {"variant_id": "sfx_variant_1", "status": "queued"},
            {"variant_id": "sfx_variant_2", "status": "completed",
             "audio_url": "https://cdn.test/two.mp3"},
        ],
    }
    tools = AgenticAudioTools.__new__(AgenticAudioTools)
    tools.async_repository = None

    first = AgenticAudioTools.hydrate_sfx_layer(tools, sfx=dict(layer))
    second = AgenticAudioTools.hydrate_sfx_layer(tools, sfx=dict(first))

    assert first["selected_variant_id"] == "sfx_variant_2"
    assert second["selected_variant_id"] == "sfx_variant_2", "two passes disagree"


def test_effects_written_from_a_prompt_say_so() -> None:
    """Two different products at two different prices — sound designed to the
    picture, and sound designed to a DESCRIPTION of the picture. A deployment
    with no reachable URL for the clip degrades to the second one silently, and
    the difference appeared in one log line and nowhere a user could see it."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import sfx_listen_report

    report = sfx_listen_report(
        {"bed_peak_dbfs": -8.0},
        watched_the_video=False,
        not_watched_reason="the clip has no URL the engine can fetch",
    )

    assert report["clean"] is True, "prompt-written effects are not a FAULT"
    assert any("not from watching" in o for o in report["observations"])
    assert any("no URL" in o for o in report["observations"]), "the reason is named"
    assert any("tracks the movement" in u for u in report["unchecked"])


def test_effects_the_engine_watched_make_no_such_claim() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.media import sfx_listen_report

    report = sfx_listen_report({"bed_peak_dbfs": -8.0}, watched_the_video=True)

    assert not any("watching" in o for o in report["observations"])
    assert not any("tracks the movement" in u for u in report["unchecked"])


def test_a_bed_that_was_paid_for_and_failed_is_not_reported_as_no_bed() -> None:
    """`enabled` is set to False when the render throws, so reading it
    afterwards reports a bed that was bought as a bed nobody asked for."""
    from pathlib import Path as _Path

    run = _Path(
        "EdennCode/WorkflowFactory/VideoSoundEffectWorkflow/planned_run.py"
    ).read_text()
    body = run.split("ambience: Optional[AmbienceBed] = None")[1].split("project = SfxProject")[0]

    attempted_at = body.index("ambience_attempted = True")
    try_at = body.index("try:")
    assert attempted_at < try_at, "the attempt is recorded after the render, not before"


# ---------------------------------------------------------------------------#
# the two ways to make a sound-effects take, as peers                         #
# ---------------------------------------------------------------------------#


def test_the_agent_can_choose_how_the_effects_are_made() -> None:
    """The route was decided deep inside the workflow from what happened to be
    reachable. The agent — which had just watched the video and written the
    plan — could not ask for either one, could not explain which it got, and
    could not compare them."""
    from EdennCode.EdennAgent.AgenticAudio.models import (
        AGENT_DECISION_SCHEMA,
        SFX_ROUTES,
        TOOL_SPECS_BY_NAME,
    )

    for tool in ("plan_sfx", "generate_sfx"):
        names = {f.name for f in TOOL_SPECS_BY_NAME[tool].args}
        assert "sfx_route" in names, f"{tool} cannot be told how to render"

    shown = AGENT_DECISION_SCHEMA["schema"]["properties"]["action"]["properties"][
        "tool_args"
    ]["properties"]["sfx_route"]["enum"]
    assert set(shown) - {""} == set(SFX_ROUTES)


def test_the_prompt_teaches_them_as_peers_and_not_as_a_fallback() -> None:
    """A route reached only when another one fails is not a capability the
    agent can direct with — and the whole point of this product is the one that
    watches the footage."""
    from EdennCode.EdennAgent.AgenticAudio.agent.prompts import SYSTEM_PROMPT

    block = SYSTEM_PROMPT.split("HOW THE TAKE IS MADE")[1].split("- generate_sfx")[0]

    assert "PEERS" in block
    assert "video_native" in block and "text" in block
    # Each has to have a reason to be chosen, or "peer" is just a word.
    assert "WATCHES the footage" in block
    assert "Exact control over what each sound IS" in block
    # And the agent must pass on a degrade rather than papering over it.
    assert "must pass that reason on" in block


def test_the_route_a_take_ran_on_travels_with_the_take() -> None:
    from pathlib import Path as _Path

    render = _Path("EdennCode/EdennAgent/AgenticAudio/tools/sfx_render.py").read_text()
    assert "bed_route=wanted_route" in render, "the render ignores what was asked"

    media = _Path("EdennCode/EdennAgent/AgenticAudio/tools/media.py").read_text()
    assert '"agentic_sfx_route"' in media, "the job payload does not carry the route"

    dev = _Path("EdennCode/EdennAgent/AgenticAudio/design/devserver.py").read_text()
    worker = _Path(
        "EdennCode/Deployment/async_pipeline_v2/workers/video_sfx_worker.py"
    ).read_text()
    for name, src in (("standalone", dev), ("fleet", worker)):
        assert "agentic_sfx_route" in src, f"the {name} renderer ignores the route"
