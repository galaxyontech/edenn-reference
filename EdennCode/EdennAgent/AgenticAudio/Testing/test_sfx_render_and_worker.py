"""Sound effects, from the approved plan to the thing that renders it.

Two defects live behind this file, and they are different kinds.

The first is a **money** defect on the deployment that is in production. The
redo path names the effects to change and carries every other effect forward by
path, so fixing one hit in a bed of twelve should cost one generation. The map
reached the job payload and the only code that ran it never read the field — so
every redo re-synthesised the whole bed, charged for it, and handed back eleven
subtly different sounds the user had already accepted.

The second is a **coverage** defect on the fleet. The agentic tool enqueued
``video_sfx`` tasks and there was no worker class and no role — nothing ran
them. The standalone completes jobs in-process, so it never showed.

Both are the same root cause in different clothes: a capability that is opt-in
and fails open, where nothing about the code looks wrong.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from EdennCode.Deployment.async_pipeline_v2.models import JobStatus, TaskStatus
from EdennCode.EdennAgent.AgenticAudio.tools import sfx_render
from EdennCode.EdennAgent.AgenticAudio.tools.sfx_render import (
    SPOTTING_ENGINE,
    SPOTTING_PLAN,
    SfxRenderFailed,
    SfxRenderResult,
    engine_prompt,
    render_sfx,
    resolve_spotting,
    usable_reuse_map,
)

from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_api import (
    _RecordingStorage,
    _create_session,
    _test_client,
)


# ---- fakes for the workflow underneath ---------------------------------


def _event(event_id: str, *, rendered: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        event_id=event_id,
        event_description=f"{event_id} sound",
        effective_start=1.25,
        start_time=1.0,
        sound_prompt=f"{event_id} prompt",
        active_audio_path=f"/tmp/{event_id}.wav" if rendered else "",
        origin="user",
        timing_authority="user",
    )


class _FakeRun:
    """Stands in for both workflow entrypoints; records what it was given."""

    last_input: Any = None
    output: Any = None

    def __init__(self) -> None:
        pass

    async def execute(self, stage_input: Any) -> Any:
        type(self).last_input = stage_input
        return type(self).output


class _FakePlannedRun(_FakeRun):
    pass


class _FakeEngineRun(_FakeRun):
    pass


@pytest.fixture()
def workflow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Patch both workflow entrypoints where ``render_sfx`` imports them."""
    from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow import (
        planned_run,
        video_sound_effect_workflow,
    )

    video = tmp_path / "out.mp4"
    audio = tmp_path / "out.wav"
    video.write_bytes(b"v" * 16)
    audio.write_bytes(b"a" * 16)
    output = SimpleNamespace(
        final_video_path=str(video),
        mixed_audio_path=str(audio),
        generated_sound_events=[_event("e1"), _event("e2")],
    )
    _FakePlannedRun.last_input = None
    _FakeEngineRun.last_input = None
    _FakePlannedRun.output = output
    _FakeEngineRun.output = output
    monkeypatch.setattr(planned_run, "PlannedSfxRun", _FakePlannedRun)
    monkeypatch.setattr(
        video_sound_effect_workflow, "VideoSoundEffectWorkflowE2E", _FakeEngineRun
    )
    return SimpleNamespace(
        planned=_FakePlannedRun, engine=_FakeEngineRun, output=output,
        video=video, audio=audio,
    )


# ---- who spotted the video ---------------------------------------------


def test_an_approved_plan_is_honoured() -> None:
    assert resolve_spotting(
        "plan", events=[{"start_s": 1.0}], ambience=""
    ) == SPOTTING_PLAN


def test_a_bed_with_no_moments_is_still_a_plan() -> None:
    """Ambience alone is a thing a user can approve."""
    assert resolve_spotting("plan", events=[], ambience="room tone") == SPOTTING_PLAN


def test_a_plan_with_nothing_in_it_falls_back_to_the_engine() -> None:
    """Otherwise the render honours an empty plan and delivers silence."""
    assert resolve_spotting("plan", events=[], ambience="") == SPOTTING_ENGINE


def test_a_job_from_before_plan_authority_keeps_the_old_behaviour() -> None:
    """Its event list may be empty and re-spotting is what it expected."""
    assert resolve_spotting(None, events=[], ambience="") == SPOTTING_ENGINE
    assert resolve_spotting("", events=[{"start_s": 1.0}], ambience="") == SPOTTING_ENGINE


def test_the_plan_still_steers_the_mode_that_does_its_own_spotting() -> None:
    prompt = engine_prompt(
        "handheld, urgent",
        [{"label": "Whoosh", "start_s": 1.04}, {"prompt": "deep impact", "start_s": 5}],
    )
    assert "handheld, urgent" in prompt
    assert "Whoosh at 1.0s" in prompt
    assert "deep impact at 5.0s" in prompt


# ---- the effects the user kept ------------------------------------------


def test_effects_the_caller_already_has_are_carried_forward(
    workflow, tmp_path: Path
) -> None:
    """The defect, stated once: the reuse map reached the job payload and the
    render never read it, so a one-hit fix re-synthesised — and re-charged
    for — the whole bed."""
    kept = tmp_path / "kept.wav"
    kept.write_bytes(b"k" * 8)

    asyncio.run(
        render_sfx(
            video_path=tmp_path / "src.mp4",
            events=[{"id": "e1", "start_s": 1.0}, {"id": "e2", "start_s": 5.0}],
            spotting="plan",
            reuse_event_audio={"e2": str(kept)},
            run_dir=tmp_path / "run",
        )
    )

    assert workflow.planned.last_input is not None
    assert workflow.planned.last_input.reuse_event_audio == {"e2": str(kept)}


def test_a_kept_effect_whose_file_is_gone_is_not_promised(tmp_path: Path) -> None:
    """Reuse is a local path, which is true on the container that wrote it and
    false everywhere else. Claiming it would place silence."""
    present = tmp_path / "here.wav"
    present.write_bytes(b"x")
    said: list[str] = []

    usable = usable_reuse_map(
        {"a": str(present), "b": str(tmp_path / "gone.wav"), "c": ""},
        log=said.append,
    )

    assert usable == {"a": str(present)}
    assert said and "generated again" in said[0], (
        "a re-render that costs more than the user expects must not be silent"
    )


# ---- which effects may be carried forward -------------------------------


def _previous(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"rendered_events": list(rows)}


def _rendered(
    event_id: str, label: str, prompt: str, *, duration: Any = None
) -> dict[str, Any]:
    return {
        "id": event_id, "label": label, "prompt": prompt,
        "requested_duration_s": duration,
        "audio_url": f"/dev/media/{event_id}.wav", "rendered": True,
    }


def _row(
    event_id: str, label: str, prompt: str, start_s: float, *, duration: Any = None
) -> dict[str, Any]:
    row = {"id": event_id, "label": label, "prompt": prompt, "start_s": start_s}
    if duration is not None:
        row["duration_s"] = duration
    return row


def test_only_the_named_effects_are_paid_for_again() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import reusable_event_audio

    carried = reusable_event_audio(
        previous_variant=_previous(
            _rendered("sfx_event_000", "Whoosh", "airy whoosh"),
            _rendered("sfx_event_001", "Impact", "deep impact"),
        ),
        plan_rows=[
            _row("sfx_event_000", "Whoosh", "airy whoosh", 1.0),
            _row("sfx_event_001", "Impact", "deep impact", 5.0),
        ],
        redo={"sfx_event_001"},
    )

    assert carried == {"sfx_event_000": "/dev/media/sfx_event_000.wav"}


def test_an_edited_plan_cannot_hand_one_moment_the_other_ones_sound() -> None:
    """Event ids are positional, so deleting a row moves every id after it onto
    a different moment. Reuse is keyed by id — without this check an edited plan
    staples the wrong sound to the wrong hit, at full confidence, silently."""
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import reusable_event_audio

    carried = reusable_event_audio(
        previous_variant=_previous(
            _rendered("sfx_event_000", "Whoosh", "airy whoosh"),
            _rendered("sfx_event_001", "Impact", "deep impact"),
        ),
        # The whoosh was deleted; the impact is now row zero.
        plan_rows=[_row("sfx_event_000", "Impact", "deep impact", 5.0)],
        redo=set(),
    )

    assert carried == {}, "the impact would have been given the whoosh's audio"


def test_moving_a_hit_in_time_does_not_cost_a_new_generation() -> None:
    """Identity is the text, not the timing. The same sound placed later is
    exactly what the user asked for."""
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import reusable_event_audio

    carried = reusable_event_audio(
        previous_variant=_previous(_rendered("sfx_event_000", "Whoosh", "airy whoosh")),
        plan_rows=[_row("sfx_event_000", "Whoosh", "airy whoosh", 9.5)],
        redo=set(),
    )

    assert carried == {"sfx_event_000": "/dev/media/sfx_event_000.wav"}


def test_changing_what_an_effect_should_sound_like_regenerates_it() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import reusable_event_audio

    carried = reusable_event_audio(
        previous_variant=_previous(_rendered("sfx_event_000", "Whoosh", "airy whoosh")),
        plan_rows=[_row("sfx_event_000", "Whoosh", "metallic scrape", 1.0)],
        redo=set(),
    )

    assert carried == {}


def test_lengthening_an_effect_regenerates_it() -> None:
    """Length is a generation parameter: reusing the old audio for a longer
    moment delivers the old short clip followed by silence, flagged nowhere."""
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import reusable_event_audio

    carried = reusable_event_audio(
        previous_variant=_previous(
            _rendered("sfx_event_000", "Whoosh", "airy whoosh", duration=1.0)
        ),
        plan_rows=[_row("sfx_event_000", "Whoosh", "airy whoosh", 1.0, duration=3.0)],
        redo=set(),
    )

    assert carried == {}


def test_an_unchanged_duration_still_reuses(tmp_path: Path) -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import reusable_event_audio

    carried = reusable_event_audio(
        previous_variant=_previous(
            _rendered("sfx_event_000", "Whoosh", "airy whoosh", duration=2.0)
        ),
        plan_rows=[_row("sfx_event_000", "Whoosh", "airy whoosh", 1.0, duration=2.0)],
        redo=set(),
    )

    assert carried == {"sfx_event_000": "/dev/media/sfx_event_000.wav"}


def test_a_manifest_without_the_duration_stamp_is_not_trusted() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import reusable_event_audio

    rendered = _rendered("sfx_event_000", "Whoosh", "airy whoosh")
    rendered.pop("requested_duration_s")

    carried = reusable_event_audio(
        previous_variant=_previous(rendered),
        plan_rows=[_row("sfx_event_000", "Whoosh", "airy whoosh", 1.0)],
        redo=set(),
    )

    assert carried == {}


def test_the_render_stamps_the_duration_the_plan_asked_for(
    workflow, tmp_path: Path
) -> None:
    """The stamp is the PLAN's number verbatim — None included — never the
    clamped end time, or every tail-of-video effect would regenerate forever."""
    out = asyncio.run(
        render_sfx(
            video_path=tmp_path / "src.mp4",
            events=[
                _row("e1", "Whoosh", "airy whoosh", 1.0, duration=2.5),
                _row("e2", "Impact", "deep impact", 5.0),
            ],
            spotting="plan",
            run_dir=tmp_path / "run",
        )
    )

    by_id = {e["id"]: e for e in out.rendered_events}
    assert by_id["e1"]["requested_duration_s"] == 2.5
    assert by_id["e2"]["requested_duration_s"] is None


# ---- what a result may carry --------------------------------------------


def test_a_result_carries_urls_never_server_paths(tmp_path: Path) -> None:
    """A filesystem path in result_json is served to every session viewer and
    is meaningless to every other container — the same server-path leak this
    repo has already had once."""
    from EdennCode.EdennAgent.AgenticAudio.tools.sfx_render import published_manifest

    audio = tmp_path / "e1.wav"
    audio.write_bytes(b"a")
    published = published_manifest(
        [
            {"id": "e1", "rendered": True, "audio_path": str(audio)},
            {"id": "e2", "rendered": False, "audio_path": ""},
        ],
        publish=lambda _eid, path: f"/dev/media/{path.name}",
    )

    assert all("audio_path" not in entry for entry in published)
    assert published[0]["audio_url"] == "/dev/media/e1.wav"
    assert published[1]["audio_url"] == ""


def test_a_kept_effect_keeps_the_url_it_was_first_published_under(
    tmp_path: Path,
) -> None:
    """Re-uploading a reused sound mints a fresh URL that makes it look
    regenerated — URL identity is how the outside verifies the redo promise."""
    from EdennCode.EdennAgent.AgenticAudio.tools.sfx_render import published_manifest

    audio = tmp_path / "kept.wav"
    audio.write_bytes(b"a")
    calls: list[str] = []

    def publish(event_id: str, path: Path) -> str:
        calls.append(event_id)
        return f"/dev/media/fresh_{path.name}"

    published = published_manifest(
        [{"id": "e1", "rendered": True, "audio_path": str(audio)}],
        publish=publish,
        known_urls={"e1": "/dev/media/original.wav"},
    )

    assert published[0]["audio_url"] == "/dev/media/original.wav"
    assert calls == [], "a kept effect must not be published again"


def test_a_manifest_that_cannot_be_checked_is_not_trusted() -> None:
    """Takes rendered before the prompt was recorded carry no way to prove the
    moment is still the same one. Paying again beats placing the wrong sound."""
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import reusable_event_audio

    carried = reusable_event_audio(
        previous_variant=_previous(
            {"id": "sfx_event_000", "label": "Whoosh", "audio_path": "/old.wav"}
        ),
        plan_rows=[_row("sfx_event_000", "Whoosh", "airy whoosh", 1.0)],
        redo=set(),
    )

    assert carried == {}


def test_the_render_manifest_records_what_each_effect_should_sound_like() -> None:
    """Without it the check above has nothing to compare."""
    from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.planned_run import (
        rendered_event_manifest,
    )

    event = _event("e1")
    event.sound_prompt = "airy whoosh"
    manifest = rendered_event_manifest([event])

    assert manifest[0]["prompt"] == "airy whoosh"


def test_re_spotting_never_reuses_a_previous_bed(workflow, tmp_path: Path) -> None:
    """The events the engine will choose are not the events those files belong
    to, so honouring the map would staple old audio onto new moments."""
    kept = tmp_path / "kept.wav"
    kept.write_bytes(b"k")

    out = asyncio.run(
        render_sfx(
            video_path=tmp_path / "src.mp4",
            events=[{"id": "e1", "start_s": 1.0}],
            spotting="engine",
            reuse_event_audio={"e1": str(kept)},
            run_dir=tmp_path / "run",
        )
    )

    assert out.spotting == SPOTTING_ENGINE
    assert out.reused_event_ids == ()
    assert workflow.planned.last_input is None


# ---- an empty bed is a failure -----------------------------------------


def test_a_bed_where_every_effect_came_back_empty_is_a_failure(
    workflow, tmp_path: Path
) -> None:
    """Per-effect errors are swallowed so one bad sound cannot sink a batch,
    which means an outage returns a full set of silent events and a mux that
    still produces a video."""
    workflow.planned.output = SimpleNamespace(
        final_video_path=str(workflow.video),
        mixed_audio_path=str(workflow.audio),
        generated_sound_events=[_event("e1", rendered=False)],
    )

    with pytest.raises(SfxRenderFailed):
        asyncio.run(
            render_sfx(
                video_path=tmp_path / "src.mp4",
                events=[{"id": "e1", "start_s": 1.0}],
                spotting="plan",
                run_dir=tmp_path / "run",
            )
        )


def test_a_run_with_no_output_file_is_a_failure(workflow, tmp_path: Path) -> None:
    workflow.planned.output = SimpleNamespace(
        final_video_path="", mixed_audio_path="", generated_sound_events=[_event("e1")]
    )

    with pytest.raises(SfxRenderFailed):
        asyncio.run(
            render_sfx(
                video_path=tmp_path / "src.mp4",
                events=[{"id": "e1", "start_s": 1.0}],
                spotting="plan",
                run_dir=tmp_path / "run",
            )
        )


def test_what_rendered_is_reported_back(workflow, tmp_path: Path) -> None:
    """The manifest is the only way anything downstream can check the take
    against the plan card, which is the whole reason the card exists."""
    out = asyncio.run(
        render_sfx(
            video_path=tmp_path / "src.mp4",
            events=[{"id": "e1", "start_s": 1.0}],
            spotting="plan",
            run_dir=tmp_path / "run",
        )
    )

    assert out.placed_count == 2
    assert [e["id"] for e in out.rendered_events] == ["e1", "e2"]
    assert all(e["rendered"] for e in out.rendered_events)
    assert out.final_video_path == workflow.video
    assert out.mixed_audio_path == workflow.audio


# ---- the fleet now runs it ----------------------------------------------


def _settings(tmp_path: Path, namespace: str) -> SimpleNamespace:
    return SimpleNamespace(
        workdir=tmp_path,
        async_v2_queue_namespace=namespace,
        upload_container="user-uploads",
        output_container="generated-media",
        audio_container_name="generated-audio",
    )


def _give_the_source_a_real_file(async_repo, tmp_path: Path) -> None:
    """The seeded source is a blob-only stub. A worker that has to open the
    clip needs a file, and re-adding under the same id is how the dev server
    attaches one too."""
    local = tmp_path / "source.mp4"
    local.write_bytes(b"FAKE-MP4")
    async_repo.add_artifact(
        artifact_id="artifact_source_video",
        job_id="asset_job_source",
        artifact_type="source_video",
        role="input",
        container="user-uploads",
        blob_name="source/source.mp4",
        url="https://cdn.test/source.mp4",
        content_type="video/mp4",
        local_path=str(local),
        metadata_json={"duration": 12.5},
    )


def _plan_sfx(client, session_id: str):
    return client.post(
        f"/api/v2/agentic/audio/sessions/{session_id}/choices",
        json={
            "choice_type": "sfx",
            "payload": {
                "sfx_summary": "whooshes on the cuts",
                "sfx_events": [
                    {"label": "Whoosh", "prompt": "airy whoosh", "start_s": 1.0},
                    {"label": "Impact", "prompt": "deep impact", "start_s": 5.0},
                ],
            },
        },
    )


def test_the_fleet_renders_an_sfx_job_and_the_session_hydrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain that was broken: tool enqueues, worker leases, session shows it.

    Nothing consumed ``video_sfx`` on the fleet, so this whole path ended in a
    queue nobody read while the API reported the job queued.
    """
    from EdennCode.Deployment.async_pipeline_v2.workers.video_sfx_worker import (
        VideoSfxWorker,
    )

    video = tmp_path / "sfx.mp4"
    audio = tmp_path / "sfx.wav"
    video.write_bytes(b"v" * 32)
    audio.write_bytes(b"a" * 32)

    seen: dict[str, Any] = {}

    async def fake_render(**kwargs: Any) -> SfxRenderResult:
        seen.update(kwargs)
        return SfxRenderResult(
            final_video_path=video,
            mixed_audio_path=audio,
            spotting=SPOTTING_PLAN,
            rendered_events=[{"id": "e1", "rendered": True, "audio_path": "/tmp/e1.wav"}],
            reused_event_ids=(),
        )

    monkeypatch.setattr(sfx_render, "render_sfx", fake_render)

    client, _agent_repo, async_repo, queue, source = _test_client(tmp_path)
    _give_the_source_a_real_file(async_repo, tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    assert _plan_sfx(client, sid).status_code == 200

    envelope = next(e for e in queue.envelopes if e.task_type == "video_sfx")
    storage = _RecordingStorage()
    worker = VideoSfxWorker(
        repository=async_repo,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        settings=_settings(tmp_path, "sfx-e2e"),
        storage=storage,
        worker_id="sfx-e2e-worker",
        queue_name=envelope.queue_name,
        lease_seconds=30,
    )

    processed = asyncio.run(worker.process_one())

    assert processed is not None
    assert processed.status == TaskStatus.COMPLETED
    job = async_repo.get_job(envelope.job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.result_json["video_url"].startswith("https://storage.test/")
    assert job.result_json["spotting"] == SPOTTING_PLAN
    # The approved plan reached the render, rather than being re-decided.
    assert [e["start_s"] for e in seen["events"]] == [1.0, 5.0]

    state = client.get(f"/api/v2/agentic/audio/sessions/{sid}").json()["state"]
    variant = state["layers"]["sfx"]["variants"][0]
    assert variant["status"] == JobStatus.COMPLETED
    assert variant["video_url"].startswith("https://storage.test/")
    assert variant["rendered_events"]
    # The session can tell a one-hit redo from a full re-render of the bed.
    assert variant["reused_event_ids"] == []
    assert {"sfx_audio", "sfx_video"} <= {
        a.artifact_type for a in async_repo.artifacts.values()
    }


def test_a_failed_render_fails_the_job_rather_than_re_queueing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every effect is its own paid generation, so a queue-level retry of a run
    that failed halfway pays again for the ones that already succeeded. The task
    is enqueued with one attempt; this is the behaviour that depends on it."""
    from EdennCode.Deployment.async_pipeline_v2.workers.video_sfx_worker import (
        VideoSfxWorker,
    )

    async def fake_render(**_kwargs: Any) -> SfxRenderResult:
        raise SfxRenderFailed("every planned sound effect came back empty")

    monkeypatch.setattr(sfx_render, "render_sfx", fake_render)

    client, _agent_repo, async_repo, queue, source = _test_client(tmp_path)
    _give_the_source_a_real_file(async_repo, tmp_path)
    session = _create_session(client, source)
    assert _plan_sfx(client, session["session_id"]).status_code == 200
    envelope = next(e for e in queue.envelopes if e.task_type == "video_sfx")

    worker = VideoSfxWorker(
        repository=async_repo,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        settings=_settings(tmp_path, "sfx-e2e"),
        storage=None,
        worker_id="sfx-fail-worker",
        queue_name=envelope.queue_name,
        lease_seconds=30,
    )

    asyncio.run(worker.process_one())

    job = async_repo.get_job(envelope.job_id)
    assert job.status == JobStatus.FAILED
    assert job.error_json, "a failed render must say something"


def test_an_output_the_run_did_not_make_gets_no_artifact_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planned render with no ambience bed produces a video and no standalone
    audio track. Inventing a row for a file that is not there is how a URL that
    404s reaches a player."""
    from EdennCode.Deployment.async_pipeline_v2.workers.video_sfx_worker import (
        VideoSfxWorker,
    )

    video = tmp_path / "only.mp4"
    video.write_bytes(b"v" * 8)

    async def fake_render(**_kwargs: Any) -> SfxRenderResult:
        return SfxRenderResult(
            final_video_path=video,
            mixed_audio_path=None,
            spotting=SPOTTING_PLAN,
            rendered_events=[],
        )

    monkeypatch.setattr(sfx_render, "render_sfx", fake_render)

    client, _agent_repo, async_repo, queue, source = _test_client(tmp_path)
    _give_the_source_a_real_file(async_repo, tmp_path)
    session = _create_session(client, source)
    assert _plan_sfx(client, session["session_id"]).status_code == 200
    envelope = next(e for e in queue.envelopes if e.task_type == "video_sfx")

    worker = VideoSfxWorker(
        repository=async_repo,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        settings=_settings(tmp_path, "sfx-e2e"),
        storage=_RecordingStorage(),
        worker_id="sfx-partial-worker",
        queue_name=envelope.queue_name,
        lease_seconds=30,
    )
    asyncio.run(worker.process_one())

    kinds = {a.artifact_type for a in async_repo.artifacts.values()}
    assert "sfx_video" in kinds
    assert "sfx_audio" not in kinds
    job = async_repo.get_job(envelope.job_id)
    assert job.result_json["audio_url"] == job.result_json["video_url"]


# ---- the two deployments answer the same ---------------------------------

def _slice(text: str, start: str, end: str) -> str:
    """The text between two markers, refusing when either is missing.

    ``str.split`` on an absent marker silently returns the whole remainder,
    which turns a scoped assertion into one any code in the file can satisfy
    — the test keeps passing while looking at nothing in particular.
    """
    head, sep, tail = text.partition(start)
    assert sep, f"start marker not found: {start!r}"
    body, sep, _rest = tail.partition(end)
    assert sep, f"end marker not found: {end!r}"
    return body


#: The keys a session's SFX hydration reads, plus the ones the redo path needs.
_RESULT_CONTRACT = {
    "status", "audio_url", "complete_audio_url", "video_url",
    "agentic_sfx_id", "spotting", "rendered_events", "reused_event_ids",
    "placeholder",
}


def test_both_deployments_return_the_same_shape() -> None:
    """The session hydrates a variant the same way whichever rendered it, so a
    key that exists on one side and not the other is a feature that works in
    one deployment and silently does nothing in the other — which is the exact
    shape of every defect this file was written for."""
    devserver = Path(
        "EdennCode/EdennAgent/AgenticAudio/design/devserver.py"
    ).read_text()
    worker = Path(
        "EdennCode/Deployment/async_pipeline_v2/workers/video_sfx_worker.py"
    ).read_text()

    standalone_block = _slice(
        devserver, "async def _real_sfx_result", "async def _real_creative_edit_result"
    )
    fleet_block = _slice(worker, "return {", "def _record_output_artifact")

    for key in _RESULT_CONTRACT:
        assert f'"{key}"' in standalone_block, f"the standalone drops {key}"
        assert f'"{key}"' in fleet_block, f"the fleet worker drops {key}"


def test_neither_deployment_decides_the_spotting_for_itself() -> None:
    """The plan-versus-engine rule lives in one module because two copies of it
    is how the plan card becomes a suggestion box on one deployment only."""
    devserver = Path(
        "EdennCode/EdennAgent/AgenticAudio/design/devserver.py"
    ).read_text()
    worker = Path(
        "EdennCode/Deployment/async_pipeline_v2/workers/video_sfx_worker.py"
    ).read_text()

    for source, who in ((devserver, "standalone"), (worker, "fleet worker")):
        assert "render_sfx" in source, f"{who} does not use the shared render"
        assert "PlannedSfxRun" not in source, f"{who} re-implements the plan route"
        assert "VideoSoundEffectWorkflowE2E" not in source, (
            f"{who} re-implements the engine route"
        )
