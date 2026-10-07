from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.EdennAgent.AgenticAudio.persistence.collab import (
    InMemoryCollabRepository,
)
from EdennCode.EdennAgent.AgenticAudio.api import (
    create_agentic_audio_router,
    mount_agentic_audio_router,
)
from EdennCode.EdennAgent.AgenticAudio.models import (
    AgenticAudioChoice,
    AgenticAudioMessage,
    AgenticAudioSession,
    AgenticAudioSessionPhase,
    AgenticAudioSessionSnapshot,
    AgenticAudioToolCall,
    AgenticSessionStatus,
)
from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Artifact,
    AsyncV2Job,
    AsyncV2JobEvent,
    AsyncV2StageRun,
    AsyncV2Task,
    JobStatus,
    StageStatus,
    TaskEnvelope,
    TaskStatus,
    new_id,
)
from EdennCode.Deployment.async_pipeline_v2.api import create_async_pipeline_v2_router
from EdennCode.Deployment.async_pipeline_v2.artifact_service import ArtifactStagingService
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository
from EdennCode.Deployment.async_pipeline_v2.workers.monolith_worker import VideoMusicMonolithWorker
from EdennCode.Deployment.async_pipeline_v2.workers import audio_creative_edit_worker as cew
from EdennCode.Deployment.async_pipeline_v2.workers.audio_creative_edit_worker import (
    AudioCreativeEditWorker,
)
from EdennCode.EdennAgent.AgenticAudio.persistence.repositories import AgenticAudioRepository
from EdennCode.EdennAgent.AgenticAudio.tools import AgenticAudioTools
from EdennCode.EdennAgent.AgenticAudio.tools.media import mix_stems
from EdennCode.Deployment.postgres_wrapper import PostgresClient
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.Deployment.workflows import VideoGenerationOrchestrator, VideoGenerationResult
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata import VideoMetadata
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import WordTS
from EdennCode.env import load_env

from EdennCode.EdennAgent.AgenticAudio.design.memory_fakes import (  # noqa: F401 — shared fakes
    _MemoryAgenticRepository,
    _MemoryAsyncRepository,
    _MemoryQueue,
    _fake_analyze,
    _fake_remix,
    _now,
    _seed_source_video,
)


# 20.6s / 320x568 (portrait) / 2.3MB — must satisfy the source-video input
# guardrails (>15s, <=150s, <=300MB): these tests submit real video-music jobs.
SMOKE_VIDEO = Path(
    "EdennCode/TestSuites/assets/smoke/videos/Videos2026-04-08_104426_347.mp4"
)


def _postgres_env_available() -> bool:
    load_env()
    return bool(
        os.getenv("DATABASE_URL")
        or (os.getenv("PGHOST") and os.getenv("PGDATABASE") and os.getenv("PGUSER"))
    )


def _storage_env_available() -> bool:
    load_env()
    provider = (os.getenv("MEDIA_STORAGE_PROVIDER") or "azure").strip().lower()
    if provider in {"china_cos", "cos", "tencent_cos", "tencent-cos"}:
        return bool(
            os.getenv("COS_SECRET_ID")
            and os.getenv("COS_SECRET_KEY")
            and os.getenv("COS_REGION")
            and os.getenv("COS_VIDEO_BUCKET")
            and os.getenv("COS_AUDIO_BUCKET")
            and os.getenv("COS_IMAGE_BUCKET")
        )
    return bool(
        os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        or os.getenv("AZURE_STORAGE_ACCOUNT_URL")
    )


def _deployment_settings_env_available() -> bool:
    load_env()
    return bool(
        os.getenv("AZURE_ENDPOINT")
        and os.getenv("AZURE_MODEL")
        and os.getenv("AZURE_API_KEY")
    )


def _real_provider_env_available(modelspec: str) -> bool:
    load_env()
    if modelspec == "edenn_basic":
        return bool(os.getenv("PROVIDER_A_API_KEY"))
    if modelspec == "edenn_enhanced":
        return bool(
            os.getenv("EDENN_ENHANCED_PROVIDER_B_API_KEY")
            or os.getenv("PROVIDER_B_API_KEY")
            or os.getenv("PROVIDER_B_API_KEY_1")
            or os.getenv("PROVIDER_B_API_KEY_2")
        )
    if modelspec == "edenn_studio":
        return bool(os.getenv("PROVIDER_C_API_KEY"))
    return False


class _ScriptedAgentClient:
    """Fake agent LLM client returning pre-scripted decisions.

    Each ``complete_messages`` call pops the next scripted decision; once the
    script is exhausted it returns a benign ``noop`` so extra conversational
    turns do not error.
    """

    _USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def __init__(
        self,
        decisions: list[dict[str, Any]] | None = None,
        *,
        fallback: dict[str, Any] | None = None,
    ) -> None:
        self.decisions = list(decisions or [])
        self.fallback = fallback or {
            "thought": "",
            "assistant_message": "Okay.",
            "action": {"type": "noop"},
        }
        self.calls: list[list[dict[str, Any]]] = []

    async def complete_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema: Any = None,
        max_tokens: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        del json_schema, max_tokens
        self.calls.append(messages)
        if self.decisions:
            return self.decisions.pop(0), dict(self._USAGE)
        return dict(self.fallback), dict(self._USAGE)


def _bootstrap_decisions() -> list[dict[str, Any]]:
    """Analyze the video, then propose three directions (cinematic = edenn_basic)."""

    return [
        {
            "thought": "Analyze before proposing.",
            "assistant_message": "Analyzing your video.",
            "action": {"type": "call_tool", "tool_name": "analyze_video", "tool_args": {}},
        },
        {
            "thought": "Offer directions.",
            "assistant_message": "I prepared three music directions.",
            "action": {
                "type": "propose",
                "proposals": [
                    {
                        "proposal_id": "proposal_cinematic",
                        "title": "Cinematic Momentum",
                        "prompt": "Cinematic, polished instrumental with building momentum.",
                        "modelspec": "edenn_basic",
                        "include_vocals": False,
                    },
                    {
                        "proposal_id": "proposal_social_pop",
                        "title": "Social Pop Lift",
                        "prompt": "Bright, upbeat pop for social video.",
                        "modelspec": "edenn_basic",
                        "include_vocals": False,
                    },
                    {
                        "proposal_id": "proposal_vocal_hook",
                        "title": "Vocal Hook",
                        "prompt": "Concise vocal hook aligned to the pacing.",
                        "modelspec": "edenn_basic",
                        "include_vocals": True,
                    },
                ],
            },
        },
    ]


def _approve_decision(proposal_id: str) -> dict[str, Any]:
    """A free-text approval step that unlocks the music-generation cost gate."""

    return {
        "thought": "User explicitly approved.",
        "intent": "approve_direction",
        "assistant_message": "Great — locking that in.",
        "action": {
            "type": "call_tool",
            "tool_name": "approve_direction",
            "tool_args": {"proposal_id": proposal_id},
        },
    }


class _RecordingStorage:
    enabled = True

    def __init__(self) -> None:
        self.upload_calls: list[dict[str, Any]] = []

    def upload_path(
        self,
        *,
        container: str,
        path: Path,
        blob_name: str | None = None,
        content_type: str | None = None,
    ) -> str:
        self.upload_calls.append(
            {
                "container": container,
                "path": path,
                "blob_name": blob_name,
                "content_type": content_type,
            }
        )
        return blob_name or path.name

    def generate_sas_url(
        self,
        *,
        container: str,
        blob_name: str,
        ttl_minutes: int | None = None,
        require_signed: bool = False,
    ) -> str:
        del ttl_minutes, require_signed
        return f"https://storage.test/{container}/{blob_name}?sig=fake"


class _FakeOrchestrator:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> VideoGenerationResult:
        self.calls.append(kwargs)
        video_path = Path(kwargs["video_path"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        matched_audio = self.output_dir / f"{kwargs['job_id']}_matched_audio.wav"
        complete_audio = self.output_dir / f"{kwargs['job_id']}_complete_audio.wav"
        remixed_video = self.output_dir / f"{kwargs['job_id']}_remixed_video.mp4"
        thumbnail = self.output_dir / f"{kwargs['job_id']}_thumbnail.webp"
        matched_audio.write_bytes(b"RIFF....WAVEfmt ")
        complete_audio.write_bytes(b"RIFF....WAVEfmt complete")
        shutil.copyfile(video_path, remixed_video)
        thumbnail.write_bytes(b"RIFFWEBP")

        return VideoGenerationResult(
            video_metadata=VideoMetadata.from_file(video_path),
            scenes=[
                SimpleNamespace(
                    scene_index=0,
                    start_timestamp=0.0,
                    end_timestamp=2.6,
                    visual_summary="A quick opening shot from a square social video.",
                    key_actions="camera movement",
                    mood="energetic",
                ),
                SimpleNamespace(
                    scene_index=1,
                    start_timestamp=2.6,
                    end_timestamp=5.2,
                    visual_summary="The clip ends with a fast visual beat.",
                    key_actions="cut and motion",
                    mood="upbeat",
                ),
            ],
            video_summary={
                "overall_mood": "upbeat",
                "pacing": "fast",
                "content_type": "social_video",
            },
            video_title="Agentic E2E Smoke Video",
            video_description="A short smoke-test video for agentic audio.",
            music_prompt={
                "style_prompt": "upbeat cinematic pop",
                "lyrics_prompt": "",
                "global_mood": "optimistic",
                "instruments": ["drums", "synth"],
            },
            music_prompt_in_chinese=None,
            generated_music_path=matched_audio,
            complete_generated_music_path=complete_audio,
            secondary_complete_generated_music_path=None,
            remixed_video_path=remixed_video,
            include_vocals=bool(kwargs.get("include_vocals", False)),
            vocal_gender=str(kwargs.get("vocal_gender") or "female"),
            lyrics_timestamps=[WordTS(text="beat", startS=0.0, endS=1.0, i=0)],
            word_level_lyrics_timestamps=[WordTS(text="beat", startS=0.0, endS=1.0, i=0)],
            matching_used_track="primary",
            thumbnail_path=thumbnail,
            token_usage={"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17},
            token_usage_breakdown={
                "user_prompt_preprocessor": {
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                }
            },
            used_music_model_spec=str(kwargs.get("modelspec") or "edenn_basic"),
            user_requested_language="en",
            job_id=str(kwargs["job_id"]),
            video_id=str(kwargs["video_id"]),
            creative_id=str(kwargs["creative_id"]),
            primary_music_id=str(kwargs["primary_music_id"]),
            selected_music_id=str(kwargs["selected_music_id"]),
            alignment_id=str(kwargs["alignment_id"]),
            music_start_s=0.0,
            alignment_score=0.91,
            alignment_details={"policy": "fake-orchestrator"},
            generation_api_call_count=2,
        )


def _context(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            workdir=tmp_path,
            async_v2_queue_namespace="agentic-test",
            upload_container="user-uploads",
            output_container="generated-media",
            audio_container_name="generated-audio",
        ),
        storage=None,
    )


def _test_client(
    tmp_path: Path,
) -> tuple[TestClient, _MemoryAgenticRepository, _MemoryAsyncRepository, _MemoryQueue, AsyncV2Artifact]:
    agent_repo = _MemoryAgenticRepository()
    async_repo = _MemoryAsyncRepository()
    queue = _MemoryQueue()
    source = _seed_source_video(async_repo)
    context = _context(tmp_path)
    tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=context.settings,
        analyze_fn=_fake_analyze,
    )
    app = FastAPI()
    app.include_router(
        create_agentic_audio_router(
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=tools,
            llm_client=_ScriptedAgentClient(_bootstrap_decisions()),
            collab_repository=InMemoryCollabRepository(),
        )
    )
    return TestClient(app), agent_repo, async_repo, queue, source


def _client_with_decisions(
    tmp_path: Path, decisions: list[dict[str, Any]], analyze_fn: Any = _fake_analyze
) -> tuple[TestClient, _MemoryAgenticRepository, _MemoryAsyncRepository, _MemoryQueue, AsyncV2Artifact]:
    agent_repo = _MemoryAgenticRepository()
    async_repo = _MemoryAsyncRepository()
    queue = _MemoryQueue()
    source = _seed_source_video(async_repo)
    context = _context(tmp_path)
    tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=context.settings,
        analyze_fn=analyze_fn,
    )
    app = FastAPI()
    app.include_router(
        create_agentic_audio_router(
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=tools,
            llm_client=_ScriptedAgentClient(decisions),
            collab_repository=InMemoryCollabRepository(),
        )
    )
    return TestClient(app), agent_repo, async_repo, queue, source


def _client_with_decisions_and_remix(
    tmp_path: Path, decisions: list[dict[str, Any]]
) -> tuple[TestClient, _MemoryAgenticRepository, _MemoryAsyncRepository, _MemoryQueue, AsyncV2Artifact]:
    agent_repo = _MemoryAgenticRepository()
    async_repo = _MemoryAsyncRepository()
    queue = _MemoryQueue()
    source = _seed_source_video(async_repo)
    context = _context(tmp_path)
    tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=context.settings,
        analyze_fn=_fake_analyze,
        remix_fn=_fake_remix,
    )
    app = FastAPI()
    app.include_router(
        create_agentic_audio_router(
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=tools,
            llm_client=_ScriptedAgentClient(decisions),
            collab_repository=InMemoryCollabRepository(),
        )
    )
    return TestClient(app), agent_repo, async_repo, queue, source


def _e2e_client(
    tmp_path: Path,
) -> tuple[
    TestClient,
    _MemoryAgenticRepository,
    _MemoryAsyncRepository,
    _MemoryQueue,
    _RecordingStorage,
    SimpleNamespace,
]:
    agent_repo = _MemoryAgenticRepository()
    async_repo = _MemoryAsyncRepository()
    queue = _MemoryQueue()
    storage = _RecordingStorage()
    context = SimpleNamespace(
        settings=SimpleNamespace(
            workdir=tmp_path,
            async_v2_queue_namespace="agentic-e2e",
            upload_container="user-uploads",
            output_container="generated-media",
            audio_container_name="generated-audio",
        ),
        storage=storage,
    )
    staging = ArtifactStagingService(
        repository=async_repo,  # type: ignore[arg-type]
        storage=storage,
        upload_container=context.settings.upload_container,
    )
    tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=context.settings,
        storage=storage,
        analyze_fn=_fake_analyze,
    )
    app = FastAPI()
    app.include_router(
        create_async_pipeline_v2_router(
            context,  # type: ignore[arg-type]
            repository=async_repo,  # type: ignore[arg-type]
            queue=queue,  # type: ignore[arg-type]
            artifact_staging=staging,
        )
    )
    app.include_router(
        create_agentic_audio_router(
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=tools,
            llm_client=_ScriptedAgentClient(_bootstrap_decisions()),
        )
    )
    return TestClient(app), agent_repo, async_repo, queue, storage, context


def _create_session(
    client: TestClient,
    source: AsyncV2Artifact,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/api/v2/agentic/audio/sessions",
        json={
            "source_video_artifact_id": source.artifact_id,
            "creator_user_id": "creator_agentic_test",
            "initial_message": "Make it cinematic and polished.",
        },
        headers=headers or {},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_agentic_audio_e2e_video_upload_to_worker_completion_to_final_artifact(
    tmp_path: Path,
) -> None:
    assert SMOKE_VIDEO.exists()
    client, _, async_repo, queue, storage, context = _e2e_client(tmp_path)

    with SMOKE_VIDEO.open("rb") as handle:
        asset_response = client.post(
            "/api/v2/assets/video",
            files={"video": ("agentic-source.mp4", handle, "video/mp4")},
            data={
                "creator_user_id": "creator_agentic_e2e",
                "session_id": "upload_session_agentic_e2e",
            },
        )
    assert asset_response.status_code == 200, asset_response.text
    asset = asset_response.json()
    assert asset["artifact_type"] == "source_video"
    assert asset["metadata"]["duration"] > 0
    assert asset["url"].startswith("https://storage.test/")
    assert storage.upload_calls[0]["content_type"] == "video/mp4"

    session_response = client.post(
        "/api/v2/agentic/audio/sessions",
        json={
            "source_video_artifact_id": asset["artifact_id"],
            "creator_user_id": "creator_agentic_e2e",
            "initial_message": "Give it a cinematic social ad sound.",
        },
    )
    assert session_response.status_code == 200, session_response.text
    session = session_response.json()
    assert session["phase"] == AgenticAudioSessionPhase.AWAITING_PLAN_CHOICE

    choose_proposal_response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_cinematic"},
    )
    assert choose_proposal_response.status_code == 200, choose_proposal_response.text
    candidate_snapshot = choose_proposal_response.json()["snapshot"]
    assert candidate_snapshot["phase"] == AgenticAudioSessionPhase.AWAITING_CANDIDATE_CHOICE
    candidates = candidate_snapshot["state"]["candidates"]
    # proposal_cinematic is edenn_basic -> the basic tier yields a single take
    # (default_candidate_count_for_modelspec("edenn_basic") == 1).
    assert len(candidates) == 1
    selected_candidate = candidates[0]
    linked_job_id = selected_candidate["linked_job_id"]
    assert linked_job_id

    queued_task = queue.tasks[queue.envelopes[0].task_id]
    assert queued_task.job_id == linked_job_id
    assert queued_task.queue_name == "agentic-e2e:video-music-pipeline"
    assert queued_task.task_type == "video_music_monolith"
    linked_job = async_repo.get_job(linked_job_id)
    assert linked_job is not None
    assert linked_job.request_json["agentic_session_id"] == session["session_id"]
    assert linked_job.request_json["agentic_candidate_id"] == selected_candidate["candidate_id"]
    linked_source_id = linked_job.request_json["source_video_artifact_id"]
    linked_source = async_repo.get_artifact(linked_source_id)
    assert linked_source is not None
    assert linked_source.metadata_json["source_artifact_id"] == asset["artifact_id"]

    worker = VideoMusicMonolithWorker(
        repository=async_repo,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        orchestrator=_FakeOrchestrator(tmp_path / "worker_outputs"),
        settings=context.settings,
        storage=storage,
        worker_id="agentic-e2e-worker",
        queue_name="agentic-e2e:video-music-pipeline",
        lease_seconds=30,
    )
    processed = asyncio.run(worker.process_one())
    assert processed is not None
    assert processed.status == TaskStatus.COMPLETED
    assert processed.job_id == linked_job_id

    status_response = client.get(f"/api/v2/jobs/{linked_job_id}")
    assert status_response.status_code == 200, status_response.text
    status = status_response.json()
    assert status["status"] == JobStatus.COMPLETED
    assert status["result"]["audio_metadata"]["audio_url"].startswith("https://storage.test/")
    assert status["result"]["video_metadata"]["video_url"].startswith("https://storage.test/")
    # The public status response intentionally omits `artifacts` (see
    # _public_status_view in async_pipeline_v2/api.py), so verify the produced
    # artifact types through the repository instead of the public payload.
    assert {
        "source_video",
        "matched_audio",
        "complete_audio",
        "remixed_video",
        "thumbnail",
    }.issubset({artifact.artifact_type for artifact in async_repo.list_artifacts(linked_job_id)})
    assert "job.completed" in [
        event["event_type"]
        for event in client.get(f"/api/v2/jobs/{linked_job_id}/events").json()["events"]
    ]

    refreshed_response = client.get(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}"
    )
    assert refreshed_response.status_code == 200, refreshed_response.text
    refreshed_candidate = refreshed_response.json()["state"]["candidates"][0]
    assert refreshed_candidate["status"] == JobStatus.COMPLETED
    assert refreshed_candidate["audio_url"].startswith("https://storage.test/")
    assert refreshed_candidate["video_url"].startswith("https://storage.test/")

    final_response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={
            "choice_type": "candidate",
            "target_id": selected_candidate["candidate_id"],
        },
    )
    assert final_response.status_code == 200, final_response.text
    final_snapshot = final_response.json()["snapshot"]
    assert final_snapshot["selected_candidate_id"] == selected_candidate["candidate_id"]
    assert final_snapshot["status"] == AgenticSessionStatus.COMPLETED
    assert final_snapshot["phase"] == AgenticAudioSessionPhase.COMPLETED
    assert final_snapshot["state"]["final_artifact"]["linked_job_id"] == linked_job_id
    assert final_snapshot["state"]["final_artifact"]["audio_url"].startswith(
        "https://storage.test/"
    )
    assert final_snapshot["state"]["final_artifact"]["video_url"].startswith(
        "https://storage.test/"
    )
    assert [event["event_type"] for event in final_response.json()["events"][:5]] == [
        "choice.recorded",
        "candidate.selected",
        "tool.started",
        "tool.completed",
        "final.artifact",
    ]


@pytest.mark.remote_integration
def test_agentic_audio_real_provider_postgres_storage_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real Postgres, real storage, a real provider render. Costs money and needs
    CI secrets, so it is opt-in: ``pytest -m remote_integration``."""
    assert SMOKE_VIDEO.exists()
    assert _postgres_env_available(), "Postgres env is required for the real E2E test."
    assert _storage_env_available(), "Configured storage provider env is required for the real E2E test."
    assert _deployment_settings_env_available(), "DeploymentSettings env is required for the real E2E test."
    assert _real_provider_env_available("edenn_basic"), "PROVIDER_A_API_KEY is required for the real E2E test."
    suffix = uuid.uuid4().hex
    queue_namespace = f"agentic-real-e2e-{suffix}"
    monkeypatch.setenv("ASYNC_V2_QUEUE_NAMESPACE", queue_namespace)

    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    async_repo = AsyncPipelineV2Repository()
    agent_repo = AgenticAudioRepository()
    queue = PostgresTaskQueue()
    context = SimpleNamespace(settings=settings, storage=storage)
    app = FastAPI()
    app.include_router(
        create_async_pipeline_v2_router(
            context,  # type: ignore[arg-type]
            repository=async_repo,
            queue=queue,
        )
    )
    real_tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=settings,
        storage=storage,
        analyze_fn=_fake_analyze,
    )
    app.include_router(
        create_agentic_audio_router(
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=real_tools,
            llm_client=_ScriptedAgentClient(_bootstrap_decisions()),
        )
    )
    client = TestClient(app)
    uploaded: list[tuple[str, str]] = []
    job_ids: list[str] = []
    session_id: str | None = None

    try:
        with SMOKE_VIDEO.open("rb") as handle:
            asset_response = client.post(
                "/api/v2/assets/video",
                files={"video": (f"agentic-real-{suffix}.mp4", handle, "video/mp4")},
                data={
                    "creator_user_id": "creator_agentic_real_e2e",
                    "session_id": f"agentic-real-upload-{suffix}",
                },
            )
        assert asset_response.status_code == 200, asset_response.text
        asset = asset_response.json()
        job_ids.append(asset["job_id"])
        if asset.get("container") and asset.get("blob_name"):
            uploaded.append((asset["container"], asset["blob_name"]))
        assert asset["url"], asset
        assert asset["metadata"]["duration"] > 0
        assert asset["metadata"]["uploaded"] is True

        session_response = client.post(
            "/api/v2/agentic/audio/sessions",
            json={
                "source_video_artifact_id": asset["artifact_id"],
                "creator_user_id": "creator_agentic_real_e2e",
                "initial_message": "Create cinematic ad music for this video.",
            },
        )
        assert session_response.status_code == 200, session_response.text
        session = session_response.json()
        session_id = session["session_id"]
        assert session["phase"] == AgenticAudioSessionPhase.AWAITING_PLAN_CHOICE

        choose_proposal_response = client.post(
            f"/api/v2/agentic/audio/sessions/{session_id}/choices",
            json={"choice_type": "proposal", "target_id": "proposal_cinematic"},
        )
        assert choose_proposal_response.status_code == 200, choose_proposal_response.text
        proposal_snapshot = choose_proposal_response.json()["snapshot"]
        linked_job_ids = list(proposal_snapshot["linked_job_ids"])
        job_ids.extend(linked_job_ids)
        # proposal_cinematic is edenn_basic -> a single take (1 linked job).
        assert len(linked_job_ids) == 1
        selected_candidate = proposal_snapshot["state"]["candidates"][0]
        linked_job_id = selected_candidate["linked_job_id"]
        assert linked_job_id in linked_job_ids

        job_created_events = async_repo.list_events(linked_job_id)
        created_event = next(
            event for event in job_created_events if event.event_type == "job.created"
        )
        task_id = created_event.payload_json["task_id"]
        task = queue.get_task(task_id)
        assert task is not None
        assert task.queue_name == f"{queue_namespace}:video-music-pipeline"
        assert task.task_type == "video_music_monolith"
        assert task.payload_json["agentic_session_id"] == session_id
        assert task.payload_json["agentic_candidate_id"] == selected_candidate["candidate_id"]

        orchestrator = VideoGenerationOrchestrator(
            storage=storage,
            llm_image_container=settings.llm_image_container,
            llm_image_sas_ttl_minutes=settings.llm_image_sas_ttl_minutes,
            llm_image_cleanup_delay_seconds=settings.llm_image_cleanup_delay_seconds,
        )
        worker = VideoMusicMonolithWorker(
            repository=async_repo,
            queue=queue,
            orchestrator=orchestrator,
            settings=settings,
            storage=storage,
            worker_id=f"agentic-real-worker-{suffix}",
            queue_name=f"{queue_namespace}:video-music-pipeline",
            lease_seconds=2400,
        )
        processed = asyncio.run(worker.process_one())
        assert processed is not None
        assert processed.status == TaskStatus.COMPLETED
        assert processed.job_id == linked_job_id

        status = async_repo.build_status_view(linked_job_id)
        assert status["status"] == JobStatus.COMPLETED, status
        assert status["result"]["audio_metadata"]["audio_url"], status["result"]
        assert status["result"]["video_metadata"]["video_url"], status["result"]
        assert status["result"]["video_metadata"]["thumbnail_url"], status["result"]
        for artifact in async_repo.list_artifacts(linked_job_id):
            if artifact.container and artifact.blob_name:
                uploaded.append((artifact.container, artifact.blob_name))
        artifact_types = {artifact["artifact_type"] for artifact in status["artifacts"]}
        assert {
            "source_video",
            "matched_audio",
            "remixed_video",
            "thumbnail",
        }.issubset(artifact_types)
        if status["result"]["audio_metadata"].get("complete_audio_url"):
            assert "complete_audio" in artifact_types

        refreshed_response = client.get(f"/api/v2/agentic/audio/sessions/{session_id}")
        assert refreshed_response.status_code == 200, refreshed_response.text
        refreshed_candidate = refreshed_response.json()["state"]["candidates"][0]
        assert refreshed_candidate["status"] == JobStatus.COMPLETED
        assert refreshed_candidate["audio_url"] == status["result"]["audio_metadata"]["audio_url"]
        assert refreshed_candidate["video_url"] == status["result"]["video_metadata"]["video_url"]

        final_response = client.post(
            f"/api/v2/agentic/audio/sessions/{session_id}/choices",
            json={
                "choice_type": "candidate",
                "target_id": selected_candidate["candidate_id"],
            },
        )
        assert final_response.status_code == 200, final_response.text
        final_snapshot = final_response.json()["snapshot"]
        assert final_snapshot["status"] == AgenticSessionStatus.COMPLETED
        assert final_snapshot["phase"] == AgenticAudioSessionPhase.COMPLETED
        assert final_snapshot["state"]["final_artifact"]["linked_job_id"] == linked_job_id
        assert (
            final_snapshot["state"]["final_artifact"]["audio_url"]
            == status["result"]["audio_metadata"]["audio_url"]
        )
        assert (
            final_snapshot["state"]["final_artifact"]["video_url"]
            == status["result"]["video_metadata"]["video_url"]
        )
    finally:
        seen_uploads = set()
        for container, blob_name in uploaded:
            if not container or not blob_name or (container, blob_name) in seen_uploads:
                continue
            seen_uploads.add((container, blob_name))
            if hasattr(storage, "delete_blob"):
                storage.delete_blob(container=container, blob_name=blob_name)
        with PostgresClient.from_env() as db:
            if session_id:
                db.run_sql(
                    "DELETE FROM agentic_audio_sessions WHERE session_id = %s",
                    params=[session_id],
                )
            for job_id in job_ids:
                db.run_sql("DELETE FROM async_v2_jobs WHERE job_id = %s", params=[job_id])


def test_mount_helper_respects_feature_flag(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AGENTIC_AUDIO_ENABLED", raising=False)
    disabled_app = FastAPI()
    assert (
        mount_agentic_audio_router(
            disabled_app,
            _context(tmp_path),  # type: ignore[arg-type]
            repository=_MemoryAgenticRepository(),
            async_repository=_MemoryAsyncRepository(),
            queue=_MemoryQueue(),
        )
        is False
    )
    assert not any(route.path.startswith("/api/v2/agentic/audio") for route in disabled_app.routes)
    assert TestClient(disabled_app).get("/api/v2/agentic/audio/sessions/missing").status_code == 404

    monkeypatch.setenv("AGENTIC_AUDIO_ENABLED", "true")
    enabled_app = FastAPI()
    agent_repo = _MemoryAgenticRepository()
    async_repo = _MemoryAsyncRepository()
    queue = _MemoryQueue()
    source = _seed_source_video(async_repo)
    context = _context(tmp_path)
    enabled_tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=context.settings,
        analyze_fn=_fake_analyze,
    )
    assert (
        mount_agentic_audio_router(
            enabled_app,
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=enabled_tools,
            llm_client=_ScriptedAgentClient(_bootstrap_decisions()),
        )
        is True
    )
    assert any(route.path.startswith("/api/v2/agentic/audio") for route in enabled_app.routes)

    response = TestClient(enabled_app).post(
        "/api/v2/agentic/audio/sessions",
        json={"source_video_artifact_id": source.artifact_id},
    )
    assert response.status_code == 200, response.text


def test_session_creation_observes_video_and_persists_snapshot(tmp_path: Path) -> None:
    client, _, _, queue, source = _test_client(tmp_path)
    payload = _create_session(client, source)

    assert payload["status"] == AgenticSessionStatus.ACTIVE
    assert payload["phase"] == AgenticAudioSessionPhase.AWAITING_PLAN_CHOICE
    assert payload["ws_url"].endswith(f"/sessions/{payload['session_id']}/ws")
    assert queue.envelopes == []

    snapshot_response = client.get(payload["status_url"])
    assert snapshot_response.status_code == 200, snapshot_response.text
    snapshot = snapshot_response.json()
    assert snapshot["source_video_artifact_id"] == source.artifact_id
    assert snapshot["state"]["observation"]["duration_s"] == 12.5
    # At most two directions are ever shown, even if the model proposes more.
    assert [proposal["proposal_id"] for proposal in snapshot["state"]["proposals"]] == [
        "proposal_cinematic",
        "proposal_social_pop",
    ]
    # initial user message + one assistant message per agent reasoning step.
    assert [message["role"] for message in snapshot["messages"]] == [
        "user",
        "assistant",
        "assistant",
    ]
    assert [tool_call["tool_name"] for tool_call in snapshot["tool_calls"]] == [
        "analyze_video",
        "propose_music_plan",
    ]


def test_message_append_rebuilds_snapshot(tmp_path: Path) -> None:
    client, _, _, _, source = _test_client(tmp_path)
    session = _create_session(client, source)

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Push it brighter but still premium."},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    # user message.created, then the instant turn-start beat (liveness while the
    # first model step runs), the model's own reasoning beat, and the assistant
    # message.created.
    assert [event["event_type"] for event in payload["events"]] == [
        "message.created",
        "agent.reasoning",
        "agent.reasoning",
        "message.created",
    ]
    assert payload["snapshot"]["messages"][-2]["content"] == "Push it brighter but still premium."
    assert payload["snapshot"]["messages"][-1]["role"] == "assistant"


def test_proposal_choice_generates_candidates_with_existing_async_contract(
    tmp_path: Path,
) -> None:
    client, _, async_repo, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_cinematic"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    snapshot = payload["snapshot"]
    candidates = snapshot["state"]["candidates"]
    assert snapshot["phase"] == AgenticAudioSessionPhase.AWAITING_CANDIDATE_CHOICE
    # edenn_basic generates a single take (model-dependent default).
    assert len(candidates) == 1
    assert len(snapshot["linked_job_ids"]) == 1
    assert [choice["target_id"] for choice in snapshot["choices"]] == ["proposal_cinematic"]
    assert [event["event_type"] for event in payload["events"][:4]] == [
        "choice.recorded",
        "tool.started",
        "tool.completed",
        "candidate.cards",
    ]

    assert len(queue.envelopes) == 1
    forbidden_worker_queues = {
        "music-basic",
        "music-enhanced",
        "music-studio",
        "selection-ranking-remix-finalize",
    }
    for envelope in queue.envelopes:
        assert envelope.task_type == "video_music_monolith"
        assert envelope.queue_name == "agentic-test:video-music-pipeline"
        assert envelope.queue_name not in forbidden_worker_queues
        assert envelope.payload_json["source_video_artifact_id"].endswith(":source_video:input")
        linked_source = async_repo.get_artifact(envelope.payload_json["source_video_artifact_id"])
        assert linked_source is not None
        assert linked_source.artifact_type == "source_video"
        assert linked_source.metadata_json["source_artifact_id"] == source.artifact_id

        job = async_repo.get_job(envelope.job_id)
        assert job is not None
        assert job.job_type == "video_music"
        assert job.session_id == session["session_id"]
        assert job.request_json["requested_source_video_artifact_id"] == source.artifact_id
        assert job.request_json["source_video_artifact_id"] == linked_source.artifact_id
        assert job.request_json["mode"] == "monolith"
        assert job.request_json["agentic_session_id"] == session["session_id"]
        assert job.request_json["agentic_candidate_id"].startswith("candidate_proposal_cinematic_")


def test_agent_message_drives_generation_via_llm_loop(tmp_path: Path) -> None:
    """A free-text message routes through the LLM loop, which calls the tool."""

    generate_decision = {
        "thought": "User approved the cinematic direction.",
        "assistant_message": "Generating cinematic candidates now.",
        "action": {
            "type": "call_tool",
            "tool_name": "generate_candidates",
            "tool_args": {"proposal_id": "proposal_cinematic", "count": 2},
        },
    }
    client, _, async_repo, queue, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [_approve_decision("proposal_cinematic"), generate_decision]
    )
    session = _create_session(client, source)
    assert session["phase"] == AgenticAudioSessionPhase.AWAITING_PLAN_CHOICE

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Let's go with the cinematic one."},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    snapshot = payload["snapshot"]
    assert snapshot["phase"] == AgenticAudioSessionPhase.AWAITING_CANDIDATE_CHOICE
    assert len(snapshot["state"]["candidates"]) == 2
    assert len(queue.envelopes) == 2
    event_types = [event["event_type"] for event in payload["events"]]
    assert "candidate.cards" in event_types
    for envelope in queue.envelopes:
        assert envelope.task_type == "video_music_monolith"
        job = async_repo.get_job(envelope.job_id)
        assert job is not None
        assert job.request_json["agentic_session_id"] == session["session_id"]


def test_compose_requires_selected_candidate(tmp_path: Path) -> None:
    client, agent_repo, _, _, source = _test_client(tmp_path)
    session = _create_session(client, source)

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "compose", "target_id": "final"},
    )

    assert response.status_code == 400
    assert "Select a candidate" in response.json()["detail"]
    assert agent_repo.list_choices(session["session_id"]) == []


def test_candidate_selection_runs_agentic_final_compose(tmp_path: Path) -> None:
    client, _, async_repo, _, source = _test_client(tmp_path)
    session = _create_session(client, source)
    choose_proposal = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_cinematic"},
    )
    assert choose_proposal.status_code == 200, choose_proposal.text
    candidate = choose_proposal.json()["snapshot"]["state"]["candidates"][0]
    async_repo.update_job_status(
        candidate["linked_job_id"],
        status=JobStatus.COMPLETED,
        result_json={
            "audio_metadata": {"audio_url": "https://cdn.test/final-audio.mp3"},
            "video_metadata": {"video_url": "https://cdn.test/final-video.mp4"},
        },
        finished=True,
    )

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "candidate", "target_id": candidate["candidate_id"]},
    )

    assert response.status_code == 200, response.text
    snapshot = response.json()["snapshot"]
    assert snapshot["selected_candidate_id"] == candidate["candidate_id"]
    assert snapshot["phase"] == AgenticAudioSessionPhase.COMPLETED
    assert snapshot["status"] == AgenticSessionStatus.COMPLETED
    assert snapshot["state"]["final_artifact"]["linked_job_id"] == candidate["linked_job_id"]
    assert snapshot["state"]["final_artifact"]["video_url"] == "https://cdn.test/final-video.mp4"
    assert [event["event_type"] for event in response.json()["events"][:4]] == [
        "choice.recorded",
        "candidate.selected",
        "tool.started",
        "tool.completed",
    ]


def test_message_content_length_is_capped(tmp_path: Path) -> None:
    """Unbounded free text fed the LLM/TTS directly (unhappy-path review P3).
    An over-long message is rejected at the request boundary (422)."""
    client, _, _, _, source = _test_client(tmp_path)
    session = _create_session(client, source)
    resp = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "A" * 99999},
    )
    assert resp.status_code == 422, resp.text


def test_create_session_initial_message_length_is_capped(tmp_path: Path) -> None:
    client, _, _, _, source = _test_client(tmp_path)
    resp = client.post(
        "/api/v2/agentic/audio/sessions",
        json={
            "source_video_artifact_id": source.artifact_id,
            "creator_user_id": "creator_agentic_test",
            "initial_message": "A" * 99999,
        },
    )
    assert resp.status_code == 422, resp.text


def test_ws_binary_frame_is_handled_gracefully(tmp_path: Path) -> None:
    """A binary frame must not crash the socket with a KeyError('text') (P3):
    it gets a clean error frame and the socket stays open for a real turn."""
    client, _, _, _, source = _test_client(tmp_path)
    session = _create_session(client, source)
    with client.websocket_connect(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/ws"
    ) as websocket:
        assert websocket.receive_json()["event_type"] == "session.opened"
        websocket.send_bytes(b"\x00\x01\x02\x03binary-garbage")
        err = websocket.receive_json()
        assert err["event_type"] == "error"
        assert "text frames" in err["payload"]["message"].lower()
        # Socket is still usable — a real choice frame still turns.
        websocket.send_json({"choice_type": "proposal", "target_id": "proposal_cinematic"})
        assert websocket.receive_json()["event_type"] == "choice.recorded"


def test_ws_non_object_json_frame_is_rejected_without_crash(tmp_path: Path) -> None:
    client, _, _, _, source = _test_client(tmp_path)
    session = _create_session(client, source)
    with client.websocket_connect(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/ws"
    ) as websocket:
        assert websocket.receive_json()["event_type"] == "session.opened"
        websocket.send_text("[1, 2, 3]")  # valid JSON, not a frame object
        err = websocket.receive_json()
        assert err["event_type"] == "error"


def test_ws_rejects_an_overlong_frame_before_it_costs_a_generation_slot(
    tmp_path: Path,
) -> None:
    """REST caps message length through pydantic; the socket had no cap at all,
    so an arbitrarily large frame landed in the transcript and in every prompt
    built from it afterwards. And because every non-choice frame counts as
    spending, the limiter charged for it before anyone looked at the content."""
    from EdennCode.EdennAgent.AgenticAudio.api.limits import Limiter, set_limiter
    from EdennCode.EdennAgent.AgenticAudio.models import MAX_MESSAGE_CHARS

    set_limiter(Limiter(daily_generation_limit=1))
    try:
        client, agent_repo, _, _, source = _test_client(tmp_path)
        session = _create_session(client, source)
        session_id = session["session_id"]
        with client.websocket_connect(
            f"/api/v2/agentic/audio/sessions/{session_id}/ws"
        ) as websocket:
            assert websocket.receive_json()["event_type"] == "session.opened"
            before = len(agent_repo.list_messages(session_id))

            websocket.send_json({"content": "x" * (MAX_MESSAGE_CHARS + 1)})
            err = websocket.receive_json()
            assert err["event_type"] == "error"
            assert "too long" in err["payload"]["message"]
            assert len(agent_repo.list_messages(session_id)) == before, (
                "the oversized frame was persisted into the transcript"
            )

            # The single generation slot must still be unspent: a frame that was
            # never going to run must not have been charged for.
            websocket.send_json({"content": "make it warmer"})
            first = websocket.receive_json()
            assert first["event_type"] != "error", (
                f"the refused frame burned the daily budget: {first}"
            )
    finally:
        set_limiter(None)


def test_websocket_streams_reconnect_snapshot_then_ordered_choice_events(
    tmp_path: Path,
) -> None:
    client, _, _, _, source = _test_client(tmp_path)
    session = _create_session(client, source)

    with client.websocket_connect(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/ws"
    ) as websocket:
        opened = websocket.receive_json()
        assert opened["event_type"] == "session.opened"
        assert opened["payload"]["snapshot"]["phase"] == AgenticAudioSessionPhase.AWAITING_PLAN_CHOICE

        websocket.send_json({"choice_type": "proposal", "target_id": "proposal_cinematic", "request_id": "attempt-first"})
        received = [websocket.receive_json() for _ in range(7)]
        assert all(event.get("request_id") == "attempt-first" for event in received)
        websocket.send_json({"content": "", "request_id": "attempt-second"})
        rejected = websocket.receive_json()
        assert rejected["event_type"] == "error"
        assert rejected["request_id"] == "attempt-second"

    assert [event["event_type"] for event in received] == [
        "choice.recorded",
        "tool.started",
        "tool.completed",
        "candidate.cards",
        "phase.changed",
        # Approval acknowledges deterministically (spend + ETA expectations) so
        # the button-dim isn't a wordless UI beat.
        "message.created",
        "session.opened",
    ]
    assert received[-1]["payload"]["snapshot"]["phase"] == AgenticAudioSessionPhase.AWAITING_CANDIDATE_CHOICE


def test_adjust_remix_no_new_generation(tmp_path: Path) -> None:
    """'Lower the music' re-muxes in-process: no new queue jobs, new remixed video."""

    adjust_decision = {
        "thought": "Volume-only tweak, use the cheap remix path.",
        "assistant_message": "Lowering the music for you.",
        "action": {
            "type": "call_tool",
            "tool_name": "adjust_remix",
            "tool_args": {
                "candidate_id": "candidate_proposal_cinematic_1",
                "music_volume": 0.3,
                "preserve_original_audio": True,
            },
        },
    }
    client, agent_repo, _, queue, source = _client_with_decisions_and_remix(
        tmp_path, _bootstrap_decisions() + [adjust_decision]
    )
    session = _create_session(client, source)
    choose = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_cinematic"},
    )
    assert choose.status_code == 200, choose.text
    envelopes_after_generate = len(queue.envelopes)
    assert envelopes_after_generate == 1  # edenn_basic → 1 take

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Lower the music to 30% and keep the original talking."},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    # The cheap re-mux must NOT enqueue any new generation job.
    assert len(queue.envelopes) == envelopes_after_generate

    candidates = payload["snapshot"]["state"]["candidates"]
    remixed = next(c for c in candidates if c["candidate_id"] == "candidate_proposal_cinematic_1")
    assert remixed["music_volume"] == 0.3
    assert remixed["preserve_original_audio"] is True
    assert remixed["remixed_video_url"].endswith("candidate_proposal_cinematic_1.mp4?sig=fake")
    assert len(remixed["remixes"]) == 1

    tool_names = [tc.tool_name for tc in agent_repo.list_tool_calls(session["session_id"])]
    assert "adjust_remix" in tool_names
    assert "candidate.cards" in [event["event_type"] for event in payload["events"]]


def test_finalize_promotes_remuxed_video_as_deliverable(tmp_path: Path) -> None:
    """Scenario 7.5 (blocker): after a slider remux, locking the take must deliver
    the REMUXED video — not fall back to the bare music stem."""

    adjust_decision = {
        "thought": "Volume tweak — cheap remix.",
        "assistant_message": "Lowering the music.",
        "action": {"type": "call_tool", "tool_name": "adjust_remix",
                   "tool_args": {"candidate_id": "candidate_proposal_cinematic_1", "music_volume": 0.6}},
    }
    client, agent_repo, _, queue, source = _client_with_decisions_and_remix(
        tmp_path, _bootstrap_decisions() + [adjust_decision]
    )
    session = _create_session(client, source)
    sid = session["session_id"]
    client.post(f"/api/v2/agentic/audio/sessions/{sid}/choices",
                json={"choice_type": "proposal", "target_id": "proposal_cinematic"})
    client.post(f"/api/v2/agentic/audio/sessions/{sid}/messages",
                json={"content": "Lower the music to 60%."})
    # Lock the remuxed take → finalize.
    resp = client.post(f"/api/v2/agentic/audio/sessions/{sid}/choices",
                       json={"choice_type": "candidate", "target_id": "candidate_proposal_cinematic_1"})
    assert resp.status_code == 200, resp.text
    fin = resp.json()["snapshot"]["state"]["final_artifact"]
    assert fin is not None
    # The deliverable is the muxed VIDEO, not just the audio stem.
    assert fin.get("video_url"), f"final_artifact has no video_url: {fin}"
    assert fin["video_url"].endswith("candidate_proposal_cinematic_1.mp4?sig=fake")


def test_voiceover_empty_script_choice_is_rejected_not_respent(tmp_path: Path) -> None:
    """Scenario 5.3 (blocker): a cleared script must NOT silently re-fire a paid
    TTS job off the stored script — it's a clean 400, no new job."""

    client, agent_repo, async_repo, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    # Seed a completed voiceover layer with a prior script.
    sess = agent_repo.get_session(sid)
    state = dict(sess.state_json)
    state["layers"] = {"voiceover": {"script": "Prior narration.", "voice_id": "warm_female",
                                     "status": "completed", "audio_url": "https://cdn.test/vo.mp3"}}
    agent_repo.update_session(sid, state_json=state)
    envelopes_before = len(queue.envelopes)
    resp = client.post(f"/api/v2/agentic/audio/sessions/{sid}/choices",
                       json={"choice_type": "voiceover", "payload": {"script": ""}})
    assert resp.status_code == 400, resp.text
    assert len(queue.envelopes) == envelopes_before  # no paid TTS job fired


def test_create_session_missing_artifact_is_documented_400(tmp_path: Path) -> None:
    """Scenario 1.4: absent/null source_video_artifact_id → the documented 400,
    not a raw Pydantic 422."""

    client, _, _, _, _ = _test_client(tmp_path)
    for body in ({"creator_user_id": "t"}, {"source_video_artifact_id": None}, {"source_video_artifact_id": ""}):
        r = client.post("/api/v2/agentic/audio/sessions", json=body)
        assert r.status_code == 400, (body, r.status_code, r.text)
        assert "source_video_artifact_id is required" in r.text


def test_edit_audio_branches_candidate(tmp_path: Path) -> None:
    """'Make it longer' branches a versioned child candidate via one new job."""

    edit_decision = {
        "thought": "The cut is longer; extend the track.",
        "assistant_message": "Extending the track to match the new length.",
        "action": {
            "type": "call_tool",
            "tool_name": "edit_audio",
            "tool_args": {
                "candidate_id": "candidate_proposal_cinematic_1",
                "edit_kind": "extend",
                "extend_seconds": 40,
            },
        },
    }
    client, agent_repo, async_repo, queue, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [edit_decision]
    )
    session = _create_session(client, source)
    choose = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_cinematic"},
    )
    assert choose.status_code == 200, choose.text
    assert len(queue.envelopes) == 1  # edenn_basic → 1 take

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "The cut is now 40 seconds, please make the music longer."},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    # Exactly one new generation job for the branched candidate (1 base + 1 edit).
    assert len(queue.envelopes) == 2

    candidates = payload["snapshot"]["state"]["candidates"]
    assert len(candidates) == 2
    child = next(c for c in candidates if c.get("parent_candidate_id"))
    assert child["parent_candidate_id"] == "candidate_proposal_cinematic_1"
    assert child["version"] == 2
    assert child["edit_kind"] == "extend"
    assert child["candidate_id"] == "candidate_proposal_cinematic_1_v2"

    new_envelope = queue.envelopes[-1]
    job = async_repo.get_job(new_envelope.job_id)
    assert job is not None
    assert job.request_json["agentic_edit_kind"] == "extend"
    assert job.request_json["agentic_parent_candidate_id"] == "candidate_proposal_cinematic_1"
    assert job.request_json["extend_seconds"] == 40.0

    tool_names = [tc.tool_name for tc in agent_repo.list_tool_calls(session["session_id"])]
    assert "edit_audio" in tool_names


def _studio_bootstrap_decisions() -> list[dict[str, Any]]:
    """Analyze, then propose a single studio-tier (edenn_studio) direction."""

    return [
        {
            "thought": "Analyze first.",
            "assistant_message": "Analyzing.",
            "action": {"type": "call_tool", "tool_name": "analyze_video", "tool_args": {}},
        },
        {
            "thought": "Offer a studio direction.",
            "assistant_message": "Here is a studio direction.",
            "action": {
                "type": "propose",
                "proposals": [
                    {
                        "proposal_id": "proposal_studio",
                        "title": "Studio Cut",
                        "prompt": "Cinematic studio-grade arrangement.",
                        "modelspec": "edenn_studio",
                        "include_vocals": False,
                    }
                ],
            },
        },
    ]


def test_provider_identity_hydrates_onto_candidate(tmp_path: Path) -> None:
    """provider/provider_audio_id/task_id hydrate onto the SERVER-SIDE candidate
    (for native edits) but are scrubbed from every client-facing response."""

    client, agent_repo, async_repo, _, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    choose = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_cinematic"},
    )
    assert choose.status_code == 200, choose.text
    candidate = choose.json()["snapshot"]["state"]["candidates"][0]
    # The client response must NOT leak the upstream vendor identity.
    assert "provider" not in candidate
    assert "provider_audio_id" not in candidate
    assert "provider_task_id" not in candidate
    # Server-side, the provider is still derived from the modelspec (edenn_basic
    # -> the basic tier's provider) so native-edit routing keeps working.
    stored = agent_repo.get_session(sid).state_json["candidates"][0]
    assert stored["provider"] == "provider_a"

    async_repo.update_job_status(
        candidate["linked_job_id"],
        status=JobStatus.COMPLETED,
        result_json={
            "audio_metadata": {"audio_url": "https://cdn.test/a.mp3"},
            "video_metadata": {"video_url": "https://cdn.test/v.mp4"},
            # The provider handles are NOT part of the job-response blocks; the
            # hydrator reads them as flat top-level keys (see MediaToolset.
            # hydrate_candidate_results), so keep them where it looks.
            "provider_audio_id": "prov_aud_123",
            "provider_task_id": "prov_task_456",
        },
        finished=True,
    )

    # The GET re-hydrates and persists provider handles onto the stored candidate.
    snapshot = client.get(f"/api/v2/agentic/audio/sessions/{sid}").json()
    hydrated = next(
        c for c in snapshot["state"]["candidates"]
        if c["candidate_id"] == candidate["candidate_id"]
    )
    # Client view stays clean...
    assert "provider_audio_id" not in hydrated
    assert "provider_task_id" not in hydrated
    assert "provider" not in hydrated
    # ...while the server-side state holds the handles native edits target.
    stored = next(
        c for c in agent_repo.get_session(sid).state_json["candidates"]
        if c["candidate_id"] == candidate["candidate_id"]
    )
    assert stored["provider_audio_id"] == "prov_aud_123"
    assert stored["provider_task_id"] == "prov_task_456"


def test_extend_routes_native_when_provider_supports(tmp_path: Path) -> None:
    """A studio-tier candidate with a known provider_audio_id extends natively."""

    extend_decision = {
        "thought": "Cut is longer; extend natively.",
        "assistant_message": "Extending the track.",
        "action": {
            "type": "call_tool",
            "tool_name": "edit_audio",
            "tool_args": {
                "candidate_id": "candidate_proposal_studio_1",
                "edit_kind": "extend",
                "extend_seconds": 40,
            },
        },
    }
    client, _, async_repo, queue, source = _client_with_decisions(
        tmp_path, _studio_bootstrap_decisions() + [extend_decision]
    )
    session = _create_session(client, source)
    choose = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_studio"},
    )
    assert choose.status_code == 200, choose.text
    parent = choose.json()["snapshot"]["state"]["candidates"][0]
    # Client view never carries the upstream vendor name; native routing is
    # verified below via the enqueued job's internal request_json.
    assert "provider" not in parent

    # Complete the parent job so its provider track id hydrates into state.
    async_repo.update_job_status(
        parent["linked_job_id"],
        status=JobStatus.COMPLETED,
        result_json={
            "audio_metadata": {"audio_url": "https://cdn.test/a.mp3"},
            "video_metadata": {"video_url": "https://cdn.test/v.mp4"},
            # Flat, top-level: the provider handles live outside the response
            # blocks and the hydrator reads them from the result root.
            "provider_audio_id": "provider_c_aud_1",
            "provider_task_id": "provider_c_task_1",
        },
        finished=True,
    )
    # GET persists the hydrated provider_audio_id back onto the candidate.
    client.get(f"/api/v2/agentic/audio/sessions/{session['session_id']}")
    envelopes_before = len(queue.envelopes)

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "The cut is 40s now, extend the music."},
    )
    assert response.status_code == 200, response.text
    assert len(queue.envelopes) == envelopes_before + 1

    child = next(
        c for c in response.json()["snapshot"]["state"]["candidates"]
        if c.get("edit_kind") == "extend"
    )
    # This assertion used to read `== "native"`, and that is the defect it was
    # pinning rather than preventing: a provider that CAN extend and a handle to
    # extend FROM are two of the three things needed, and the third — something
    # that performs the extension — has never existed. Labelling the take native
    # while the ordinary generation path produced a different piece of music
    # made the claim worse than the silence, because a label reads as a promise.
    #
    # The routing itself is still what is under test: the parent handle resolves
    # and travels with the job, so the consumer has something to use on the day
    # it is written. Flip NATIVE_EXTEND_CONSUMER_AVAILABLE in that same change
    # and this expectation becomes "native" again.
    from EdennCode.EdennAgent.AgenticAudio.models import NATIVE_EXTEND_CONSUMER_AVAILABLE

    expected_mode = "native" if NATIVE_EXTEND_CONSUMER_AVAILABLE else "regenerate_fallback"
    assert child["extend_mode"] == expected_mode

    job = async_repo.get_job(queue.envelopes[-1].job_id)
    assert job.request_json["agentic_extend_mode"] == expected_mode
    if NATIVE_EXTEND_CONSUMER_AVAILABLE:
        assert job.request_json["agentic_parent_provider_audio_id"] == "provider_c_aud_1"
        assert job.request_json["agentic_parent_provider_task_id"] == "provider_c_task_1"


def test_extend_falls_back_to_regenerate_on_the_basic_tier(tmp_path: Path) -> None:
    """An edenn_basic candidate has no addressable track id, so extend regenerates."""

    extend_decision = {
        "thought": "Extend, but provider can't do it in place.",
        "assistant_message": "Regenerating a longer take.",
        "action": {
            "type": "call_tool",
            "tool_name": "edit_audio",
            "tool_args": {
                "candidate_id": "candidate_proposal_cinematic_1",
                "edit_kind": "extend",
                "extend_seconds": 40,
            },
        },
    }
    client, _, async_repo, queue, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [extend_decision]
    )
    session = _create_session(client, source)
    client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_cinematic"},
    )

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Make the music longer."},
    )
    assert response.status_code == 200, response.text

    child = next(
        c for c in response.json()["snapshot"]["state"]["candidates"]
        if c.get("edit_kind") == "extend"
    )
    assert child["extend_mode"] == "regenerate_fallback"

    job = async_repo.get_job(queue.envelopes[-1].job_id)
    assert job.request_json["agentic_extend_mode"] == "regenerate_fallback"
    assert "agentic_parent_provider_audio_id" not in job.request_json


# --------------------------------------------------------------------------- #
# creative_edit (audio-to-audio restyle)                                       #
# --------------------------------------------------------------------------- #


def _complete_parent_job(
    async_repo: _MemoryAsyncRepository, candidate: dict[str, Any]
) -> None:
    """Mark a candidate's generation job completed so its audio/provider hydrate."""

    async_repo.update_job_status(
        candidate["linked_job_id"],
        status=JobStatus.COMPLETED,
        result_json={
            "audio_metadata": {"audio_url": "https://cdn.test/parent-audio.mp3"},
            "video_metadata": {"video_url": "https://cdn.test/parent-video.mp4"},
            # Flat, top-level: the provider handles live outside the response
            # blocks and the hydrator reads them from the result root.
            "provider_audio_id": "prov_aud_parent",
            "provider_task_id": "prov_task_parent",
        },
        finished=True,
    )


def test_creative_edit_enqueues_audio_creative_edit_job(tmp_path: Path) -> None:
    """A studio-tier candidate with rendered audio restyles via a dedicated v2 job."""

    creative_decision = {
        "thought": "User wants a restyle.",
        "assistant_message": "Restyling the track to lo-fi.",
        "action": {
            "type": "call_tool",
            "tool_name": "edit_audio",
            "tool_args": {
                "candidate_id": "candidate_proposal_studio_1",
                "edit_kind": "creative_edit",
                "prompt": "Make it a warm lo-fi version.",
            },
        },
    }
    client, _, async_repo, queue, source = _client_with_decisions(
        tmp_path, _studio_bootstrap_decisions() + [creative_decision]
    )
    session = _create_session(client, source)
    choose = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_studio"},
    )
    parent = choose.json()["snapshot"]["state"]["candidates"][0]
    _complete_parent_job(async_repo, parent)
    client.get(f"/api/v2/agentic/audio/sessions/{session['session_id']}")
    video_music_jobs_before = len(queue.envelopes)

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Make this a lo-fi version."},
    )
    assert response.status_code == 200, response.text
    assert len(queue.envelopes) == video_music_jobs_before + 1

    envelope = queue.envelopes[-1]
    assert envelope.task_type == "audio_creative_edit"
    assert envelope.queue_name.endswith("audio-creative-edit-pipeline")
    job = async_repo.get_job(envelope.job_id)
    assert job.job_type == "audio_creative_edit"
    assert job.request_json["source_audio_url"] == "https://cdn.test/parent-audio.mp3"
    assert job.request_json["user_prompt"] == "Make it a warm lo-fi version."
    assert job.request_json["agentic_edit_kind"] == "creative_edit"
    assert job.request_json["modelspec"] == "edenn_studio"

    child = next(
        c for c in response.json()["snapshot"]["state"]["candidates"]
        if c.get("edit_kind") == "creative_edit"
    )
    assert child["candidate_id"] == "candidate_proposal_studio_1_v2"
    assert "provider" not in child


def test_creative_edit_falls_back_to_regenerate_on_the_basic_tier(tmp_path: Path) -> None:
    """edenn_basic has no audio-to-audio path, so creative_edit regenerates."""

    creative_decision = {
        "thought": "Restyle requested but provider can't.",
        "assistant_message": "Regenerating in the new style.",
        "action": {
            "type": "call_tool",
            "tool_name": "edit_audio",
            "tool_args": {
                "candidate_id": "candidate_proposal_cinematic_1",
                "edit_kind": "creative_edit",
                "prompt": "Make it lo-fi.",
            },
        },
    }
    client, _, async_repo, queue, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [creative_decision]
    )
    session = _create_session(client, source)
    choose = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_cinematic"},
    )
    _complete_parent_job(async_repo, choose.json()["snapshot"]["state"]["candidates"][0])
    client.get(f"/api/v2/agentic/audio/sessions/{session['session_id']}")

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Make this lo-fi."},
    )
    assert response.status_code == 200, response.text

    child = next(
        c for c in response.json()["snapshot"]["state"]["candidates"]
        if c.get("parent_candidate_id")
    )
    assert child["edit_kind"] == "regenerate"
    assert child["requested_edit_kind"] == "creative_edit"

    job = async_repo.get_job(queue.envelopes[-1].job_id)
    # Fell back to the video_music monolith path, not the creative-edit job.
    assert job.job_type == "video_music"
    assert envelope_task_types(queue)[-1] == "video_music_monolith"


def envelope_task_types(queue: _MemoryQueue) -> list[str]:
    return [env.task_type for env in queue.envelopes]


class _FakeCreativeEditOrchestrator:
    """Fake AudioCreativeEditOrchestrator: writes a real edited-audio file."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        edited = self.output_dir / "edited_audio.wav"
        edited.write_bytes(b"RIFF....WAVEfmt edited")
        return SimpleNamespace(
            edited_audio_path=edited,
            secondary_edited_audio_path=None,
            used_music_model_spec=str(kwargs.get("modelspec") or "edenn_studio"),
            include_vocals=False,
            vocal_gender="female",
            creative_edit_prompt={"edit_prompt": str(kwargs.get("user_prompt") or "")},
        )


def test_creative_edit_worker_completes_and_hydrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: creative_edit job -> worker -> restyled audio + remixed video."""

    # Patch the worker's media I/O so the test stays hermetic (no ffmpeg/network).
    async def _fake_download(*, url: str, destination: Path, asset_label: str) -> Path:
        del url, asset_label
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"RIFF....WAVEfmt source")
        return destination

    def _fake_overlay(video_path: Path, music_path: Path, output_path: Path, **kwargs: Any):
        del video_path, music_path, kwargs
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"FAKE-MP4")
        return output_path

    monkeypatch.setattr(cew, "download_public_file_to_disk", _fake_download)
    monkeypatch.setattr(cew, "overlay_music_on_video", _fake_overlay)

    agent_repo = _MemoryAgenticRepository()
    async_repo = _MemoryAsyncRepository()
    queue = _MemoryQueue()
    storage = _RecordingStorage()
    context = SimpleNamespace(
        settings=SimpleNamespace(
            workdir=tmp_path,
            async_v2_queue_namespace="creative-e2e",
            upload_container="user-uploads",
            output_container="generated-media",
            audio_container_name="generated-audio",
        ),
        storage=storage,
    )
    # Source video artifact resolvable locally (worker skips download when present).
    source_local = tmp_path / "source.mp4"
    source_local.write_bytes(b"FAKE-MP4")
    async_repo.create_job(
        job_id="asset_job_source",
        job_type="asset_staging",
        request_json={"source": "upload"},
        status=JobStatus.COMPLETED,
    )
    source = async_repo.add_artifact(
        artifact_id="artifact_source_video",
        job_id="asset_job_source",
        artifact_type="source_video",
        role="input",
        container="user-uploads",
        blob_name="source/source.mp4",
        url="https://cdn.test/source.mp4",
        content_type="video/mp4",
        local_path=str(source_local),
        metadata_json={"duration": 12.5, "width": 360, "height": 360},
    )
    tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=context.settings,
        storage=storage,
        analyze_fn=_fake_analyze,
    )
    app = FastAPI()
    app.include_router(
        create_agentic_audio_router(
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=tools,
            llm_client=_ScriptedAgentClient(
                _studio_bootstrap_decisions()
                + [
                    {
                        "thought": "Restyle.",
                        "assistant_message": "Restyling.",
                        "action": {
                            "type": "call_tool",
                            "tool_name": "edit_audio",
                            "tool_args": {
                                "candidate_id": "candidate_proposal_studio_1",
                                "edit_kind": "creative_edit",
                                "prompt": "Lo-fi version.",
                            },
                        },
                    }
                ]
            ),
        )
    )
    client = TestClient(app)
    session = _create_session(client, source)
    choose = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_studio"},
    )
    _complete_parent_job(async_repo, choose.json()["snapshot"]["state"]["candidates"][0])
    client.get(f"/api/v2/agentic/audio/sessions/{session['session_id']}")
    client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Lo-fi please."},
    )

    creative_envelope = queue.envelopes[-1]
    assert creative_envelope.task_type == "audio_creative_edit"

    worker = AudioCreativeEditWorker(
        repository=async_repo,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        orchestrator=_FakeCreativeEditOrchestrator(tmp_path / "creative_out"),
        settings=context.settings,
        storage=storage,
        worker_id="creative-e2e-worker",
        queue_name="creative-e2e:audio-creative-edit-pipeline",
        lease_seconds=30,
    )
    processed = asyncio.run(worker.process_one())
    assert processed is not None
    assert processed.status == TaskStatus.COMPLETED

    job = async_repo.get_job(creative_envelope.job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.result_json["audio_url"].startswith("https://storage.test/")
    assert job.result_json["video_url"].startswith("https://storage.test/")

    snapshot = client.get(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}"
    ).json()["state"]
    child = next(
        c for c in snapshot["candidates"] if c.get("edit_kind") == "creative_edit"
    )
    assert child["audio_url"].startswith("https://storage.test/")
    assert child["video_url"].startswith("https://storage.test/")
    assert {"edited_audio", "remixed_video"}.issubset(
        {a.artifact_type for a in async_repo.artifacts.values()}
    )


# --------------------------------------------------------------------------- #
# multi-turn conversations                                                     #
# --------------------------------------------------------------------------- #


def test_multi_turn_generate_then_adjust_then_restyle(tmp_path: Path) -> None:
    """A realistic 3-turn session: approve -> lower music -> restyle."""

    decisions = _studio_bootstrap_decisions() + [
        _approve_decision("proposal_studio"),  # turn 1a: explicit approval (free)
        {  # turn 1b: approved -> generate
            "thought": "Approved.",
            "intent": "approve_direction",
            "assistant_message": "Generating two candidates.",
            "action": {
                "type": "call_tool",
                "tool_name": "generate_candidates",
                "tool_args": {"proposal_id": "proposal_studio", "count": 2},
            },
        },
        {  # turn 2: lower the music (cheap remux, no new job)
            "thought": "Volume tweak.",
            "assistant_message": "Lowering the music.",
            "action": {
                "type": "call_tool",
                "tool_name": "adjust_remix",
                "tool_args": {
                    "candidate_id": "candidate_proposal_studio_1",
                    "music_volume": 0.4,
                },
            },
        },
        {  # adjust_remix is light, so the loop continues; stop the turn here.
            "thought": "Done for now.",
            "assistant_message": "Music lowered.",
            "action": {"type": "noop"},
        },
        {  # turn 3: restyle (creative_edit)
            "thought": "Restyle.",
            "assistant_message": "Restyling to cinematic.",
            "action": {
                "type": "call_tool",
                "tool_name": "edit_audio",
                "tool_args": {
                    "candidate_id": "candidate_proposal_studio_1",
                    "edit_kind": "creative_edit",
                    "prompt": "Cinematic strings version.",
                },
            },
        },
    ]
    client, agent_repo, async_repo, queue, source = _client_with_decisions_and_remix(
        tmp_path, decisions
    )
    session = _create_session(client, source)
    assert session["phase"] == AgenticAudioSessionPhase.AWAITING_PLAN_CHOICE

    # Turn 1: approval -> generate
    gen = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Go with the studio cut."},
    )
    gen_state = gen.json()["snapshot"]["state"]
    assert len(gen_state["candidates"]) == 2
    assert len(queue.envelopes) == 2
    parent = gen_state["candidates"][0]
    _complete_parent_job(async_repo, parent)

    # Turn 2: adjust_remix -> no new job, remixed_video set
    adj = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Lower the music a lot."},
    )
    assert len(queue.envelopes) == 2  # adjust_remix never enqueues
    remixed = next(
        c for c in adj.json()["snapshot"]["state"]["candidates"]
        if c["candidate_id"] == "candidate_proposal_studio_1"
    )
    assert remixed["music_volume"] == 0.4
    assert remixed["remixed_video_url"]

    # Turn 3: creative_edit -> one new audio_creative_edit job
    res = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Now make it cinematic strings."},
    )
    assert len(queue.envelopes) == 3
    assert queue.envelopes[-1].task_type == "audio_creative_edit"
    child = next(
        c for c in res.json()["snapshot"]["state"]["candidates"]
        if c.get("edit_kind") == "creative_edit"
    )
    assert child["parent_candidate_id"] == "candidate_proposal_studio_1"

    # The conversation accumulated assistant turns across all messages.
    roles = [m["role"] for m in res.json()["snapshot"]["messages"]]
    assert roles.count("user") >= 4  # initial + 3 turns
    tool_names = {tc.tool_name for tc in agent_repo.list_tool_calls(session["session_id"])}
    assert {"analyze_video", "generate_candidates", "adjust_remix", "edit_audio"} <= tool_names


def test_multi_turn_iteration_after_finalize(tmp_path: Path) -> None:
    """After finalize the session stays open and still accepts edit turns."""

    client, _, async_repo, queue, source = _client_with_decisions_and_remix(
        tmp_path,
        _bootstrap_decisions()
        + [
            {  # post-finalize turn: a cheap remix is still accepted
                "thought": "Tweak after finalize.",
                "assistant_message": "Nudging the mix.",
                "action": {
                    "type": "call_tool",
                    "tool_name": "adjust_remix",
                    "tool_args": {
                        "candidate_id": "candidate_proposal_cinematic_1",
                        "music_volume": 0.6,
                    },
                },
            },
        ],
    )
    session = _create_session(client, source)
    choose = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_cinematic"},
    )
    candidate = choose.json()["snapshot"]["state"]["candidates"][0]
    async_repo.update_job_status(
        candidate["linked_job_id"],
        status=JobStatus.COMPLETED,
        result_json={
            "audio_metadata": {"audio_url": "https://cdn.test/a.mp3"},
            "video_metadata": {"video_url": "https://cdn.test/v.mp4"},
        },
        finished=True,
    )
    finalize = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "candidate", "target_id": candidate["candidate_id"]},
    )
    assert finalize.json()["snapshot"]["phase"] == AgenticAudioSessionPhase.COMPLETED

    # A further edit turn after finalize is still accepted (iteration model).
    after = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Actually nudge the music down a touch."},
    )
    assert after.status_code == 200, after.text
    remixed = next(
        c for c in after.json()["snapshot"]["state"]["candidates"]
        if c["candidate_id"] == candidate["candidate_id"]
    )
    assert remixed["music_volume"] == 0.6


# --------------------------------------------------------------------------- #
# intent detection + session memory / turn history                            #
# --------------------------------------------------------------------------- #


def test_intent_and_memory_recorded_per_turn(tmp_path: Path) -> None:
    decisions = [
        {
            "thought": "",
            "intent": "analyze",
            "assistant_message": "Analyzing your video.",
            "memory_update": {
                "creative_direction": "cinematic social ad",
                "style_keywords": ["cinematic", "warm"],
                "avoid": ["harsh"],
            },
            "action": {"type": "call_tool", "tool_name": "analyze_video", "tool_args": {}},
        },
        {
            "thought": "",
            "intent": "request_proposals",
            "assistant_message": "Here are some directions.",
            "action": {
                "type": "propose",
                "proposals": [
                    {
                        "proposal_id": "proposal_cinematic",
                        "title": "Cinematic",
                        "prompt": "Cinematic instrumental.",
                        "modelspec": "edenn_studio",
                        "include_vocals": False,
                    }
                ],
            },
        },
    ]
    client, _, _, _, source = _client_with_decisions(tmp_path, decisions)
    session = _create_session(client, source)

    state = client.get(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}"
    ).json()["state"]
    turns = state["turns"]
    assert len(turns) == 1  # one user-driven turn (the initial message)
    assert turns[0]["intent"] == "analyze"  # first classified intent labels the turn
    assert "analyze_video" in turns[0]["tools"]
    assert turns[0]["user_message"] == "Make it cinematic and polished."

    memory = state["memory"]
    assert memory["creative_direction"] == "cinematic social ad"
    assert "cinematic" in memory["style_keywords"]
    assert "harsh" in memory["avoid"]
    assert memory["recent_intents"] == ["analyze"]


def test_intent_defaults_to_other_when_unclassified(tmp_path: Path) -> None:
    # _bootstrap_decisions carry no `intent`, so the turn defaults to "other".
    client, _, _, _, source = _test_client(tmp_path)
    session = _create_session(client, source)
    state = client.get(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}"
    ).json()["state"]
    assert state["turns"][0]["intent"] == "other"
    assert state["memory"]["recent_intents"] == ["other"]


def test_memory_accumulates_preferences_across_turns(tmp_path: Path) -> None:
    decisions = _studio_bootstrap_decisions() + [
        _approve_decision("proposal_studio"),
        {
            "thought": "",
            "intent": "approve_direction",
            "assistant_message": "Generating.",
            "action": {
                "type": "call_tool",
                "tool_name": "generate_candidates",
                "tool_args": {"proposal_id": "proposal_studio", "count": 2},
            },
        },
        {
            "thought": "",
            "intent": "adjust_mix",
            "assistant_message": "Lowering the music.",
            "action": {
                "type": "call_tool",
                "tool_name": "adjust_remix",
                "tool_args": {"candidate_id": "candidate_proposal_studio_1", "music_volume": 0.4},
            },
        },
        {"thought": "", "intent": "adjust_mix", "assistant_message": "Done.", "action": {"type": "noop"}},
    ]
    client, _, _, _, source = _client_with_decisions_and_remix(tmp_path, decisions)
    session = _create_session(client, source)
    client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Go with the studio cut."},
    )
    client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Lower the music."},
    )

    state = client.get(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}"
    ).json()["state"]
    prefs = state["memory"]["preferences"]
    assert prefs["modelspec"] == "edenn_studio"  # derived from the generated candidate
    assert prefs["music_volume"] == 0.4  # derived from the adjust_remix tweak
    # Three user turns recorded (initial bootstrap + two messages). The bootstrap
    # decisions carry no intent, so it defaults to "other"; the two messages do.
    intents = [t["intent"] for t in state["turns"]]
    assert len(intents) == 3
    assert intents[1:] == ["approve_direction", "adjust_mix"]
    assert state["memory"]["recent_intents"][-2:] == ["approve_direction", "adjust_mix"]


def test_memory_is_fed_into_agent_context(tmp_path: Path) -> None:
    agent_repo = _MemoryAgenticRepository()
    async_repo = _MemoryAsyncRepository()
    queue = _MemoryQueue()
    source = _seed_source_video(async_repo)
    context = _context(tmp_path)
    tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=context.settings,
        analyze_fn=_fake_analyze,
    )
    scripted = _ScriptedAgentClient(
        [
            {
                "thought": "",
                "intent": "analyze",
                "assistant_message": "Analyzing.",
                "memory_update": {"creative_direction": "moody lo-fi"},
                "action": {"type": "call_tool", "tool_name": "analyze_video", "tool_args": {}},
            },
            {"thought": "", "intent": "ask_question", "assistant_message": "What vibe?", "action": {"type": "ask"}},
            {"thought": "", "intent": "other", "assistant_message": "Okay.", "action": {"type": "noop"}},
        ]
    )
    app = FastAPI()
    app.include_router(
        create_agentic_audio_router(
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=tools,
            llm_client=scripted,
        )
    )
    client = TestClient(app)
    session = _create_session(client, source)  # bootstrap turn writes memory
    calls_before = len(scripted.calls)

    client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Keep it consistent please."},
    )
    # The first decision call of the SECOND turn must carry the prior memory.
    second_turn_messages = scripted.calls[calls_before]
    state_summary_blob = next(
        m["content"] for m in second_turn_messages
        if m["role"] == "system" and "Current session state" in m["content"]
    )
    assert "moody lo-fi" in state_summary_blob
    assert "recent_intents" in state_summary_blob
    assert "analyze" in state_summary_blob


# --------------------------------------------------------------------------- #
# intermediate reasoning events + clarification cards                         #
# --------------------------------------------------------------------------- #


def test_agent_emits_reasoning_status_events(tmp_path: Path) -> None:
    decisions = _bootstrap_decisions() + [
        _approve_decision("proposal_cinematic"),
        {
            "thought": "User approved.",
            "intent": "approve_direction",
            "assistant_message": "Generating.",
            "action": {
                "type": "call_tool",
                "tool_name": "generate_candidates",
                "tool_args": {"proposal_id": "proposal_cinematic", "count": 2},
            },
        },
    ]
    client, _, _, _, source = _client_with_decisions(tmp_path, decisions)
    session = _create_session(client, source)

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Go with the cinematic one."},
    )
    events = response.json()["events"]
    reasoning = [e for e in events if e["event_type"] == "agent.reasoning"]
    assert reasoning, "expected at least one agent.reasoning status event"
    statuses = [e["payload"]["status"] for e in reasoning]
    # Two reasoning beats this turn: the approval, then composing.
    assert "Locking in your approval…" in statuses
    assert "Composing your tracks…" in statuses
    # Reasoning is emitted before the tool's effect is streamed.
    types = [e["event_type"] for e in events]
    assert types.index("agent.reasoning") < types.index("candidate.cards")


def test_clarify_emits_choice_cards_and_tracks_pending(tmp_path: Path) -> None:
    clarify_decision = {
        "thought": "Underspecified.",
        "intent": "ask_question",
        "assistant_message": "One quick question.",
        "action": {
            "type": "clarify",
            "clarification": {
                "question": "Vocals or instrumental?",
                "options": [
                    {"id": "vox", "label": "With vocals", "hint": "sung hook"},
                    {"label": "Instrumental"},
                ],
            },
        },
    }
    client, _, _, _, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [clarify_decision]
    )
    session = _create_session(client, source)

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Make it good."},
    )
    payload = response.json()
    cards = [e for e in payload["events"] if e["event_type"] == "clarify.cards"]
    assert len(cards) == 1
    card = cards[0]["payload"]
    assert card["question"] == "Vocals or instrumental?"
    assert [o["label"] for o in card["options"]] == ["With vocals", "Instrumental"]
    assert card["options"][0]["id"] == "vox"
    assert card["options"][1]["id"] == "option_2"  # auto-assigned id

    pending = payload["snapshot"]["state"]["pending_clarification"]
    assert pending["question"] == "Vocals or instrumental?"

    # A follow-up turn that doesn't clarify clears the outstanding question.
    follow = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "With vocals."},
    )
    assert follow.json()["snapshot"]["state"]["pending_clarification"] is None


def test_clarify_chip_click_closes_the_loop(tmp_path: Path) -> None:
    """Tapping a clarify option records the answer, clears pending, and the agent acts."""

    clarify_decision = {
        "thought": "Underspecified.",
        "intent": "ask_question",
        "assistant_message": "One quick question.",
        "action": {
            "type": "clarify",
            "clarification": {
                "question": "Vocals or instrumental?",
                "options": [
                    {"id": "vox", "label": "With vocals"},
                    {"id": "instr", "label": "Instrumental"},
                ],
            },
        },
    }
    after_answer = {
        "thought": "They chose vocals.",
        "intent": "request_proposals",
        "assistant_message": "Great — I'll plan a vocal direction.",
        "action": {"type": "noop"},
    }
    client, agent_repo, _, _, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [clarify_decision, after_answer]
    )
    session = _create_session(client, source)
    sid = session["session_id"]
    client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/messages",
        json={"content": "Make it good."},
    )

    # The user taps the "With vocals" chip -> structured choice.
    response = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "clarification", "target_id": "vox"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    event_types = [e["event_type"] for e in payload["events"]]
    assert "choice.recorded" in event_types

    snapshot = payload["snapshot"]
    # The outstanding question is cleared and the chosen option became the answer.
    assert snapshot["state"]["pending_clarification"] is None
    assert snapshot["messages"][-2]["content"] == "With vocals"
    assert snapshot["messages"][-1]["role"] == "assistant"  # the agent reacted
    # The choice was recorded against the tapped option.
    assert [c["target_id"] for c in snapshot["choices"]] == ["vox"]
    # And the answer turn was logged in history.
    assert snapshot["state"]["turns"][-1]["user_message"] == "With vocals"


# --------------------------------------------------------------------------- #
# audio-director production plan (Phase A1)                                    #
# --------------------------------------------------------------------------- #


def test_set_production_plan_records_mode_and_scaffolds_layers(tmp_path: Path) -> None:
    plan_decision = {
        "thought": "User chose the full e2e plan.",
        "intent": "plan_audio",
        "assistant_message": "Planning music + voice-over.",
        "action": {
            "type": "call_tool",
            "tool_name": "set_production_plan",
            "tool_args": {"mode": "full_e2e", "layers": ["voiceover"]},
        },
    }
    client, agent_repo, _, _, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [plan_decision, {"thought": "", "intent": "other", "assistant_message": "Ok.", "action": {"type": "noop"}}]
    )
    session = _create_session(client, source)

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Let's do the full plan with narration."},
    )
    payload = response.json()
    assert any(e["event_type"] == "production.plan" for e in payload["events"])

    state = payload["snapshot"]["state"]
    plan = state["production_plan"]
    assert plan["mode"] == "full_e2e"
    # Music is always included and leads, voiceover appended, de-duplicated.
    assert plan["layers"] == ["music", "voiceover"]
    assert "voiceover" in state["layers"]

    tool_names = [tc.tool_name for tc in agent_repo.list_tool_calls(session["session_id"])]
    assert "set_production_plan" in tool_names


def test_set_production_plan_defaults_invalid_mode_to_music_first(tmp_path: Path) -> None:
    plan_decision = {
        "thought": "",
        "intent": "plan_audio",
        "assistant_message": "Music first.",
        "action": {
            "type": "call_tool",
            "tool_name": "set_production_plan",
            "tool_args": {"mode": "nonsense"},
        },
    }
    client, _, _, _, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [plan_decision, {"thought": "", "intent": "other", "assistant_message": "Ok.", "action": {"type": "noop"}}]
    )
    session = _create_session(client, source)
    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Just music for now."},
    )
    plan = response.json()["snapshot"]["state"]["production_plan"]
    assert plan["mode"] == "music_first"
    assert plan["layers"] == ["music"]


def test_set_production_plan_voiceover_only_omits_music_on_llm_path(tmp_path: Path) -> None:
    """F1: a music_first plan that lists only voiceover is a narration-only plan;
    the LLM path must NOT re-inject a music layer (was forced for any LLM plan)."""

    plan_decision = {
        "thought": "",
        "intent": "plan_audio",
        "assistant_message": "Narration only.",
        "action": {
            "type": "call_tool",
            "tool_name": "set_production_plan",
            "tool_args": {"mode": "music_first", "layers": ["voiceover"]},
        },
    }
    client, _, _, _, source = _client_with_decisions(
        tmp_path,
        _bootstrap_decisions()
        + [plan_decision, {"thought": "", "intent": "other", "assistant_message": "Ok.", "action": {"type": "noop"}}],
    )
    session = _create_session(client, source)
    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Just narration over the video, no music at all."},
    )
    plan = response.json()["snapshot"]["state"]["production_plan"]
    assert plan["mode"] == "music_first"
    assert plan["layers"] == ["voiceover"]  # music NOT force-injected


# --------------------------------------------------------------------------- #
# voice-over layer (Phase A2)                                                  #
# --------------------------------------------------------------------------- #

_NOOP = {"thought": "", "intent": "other", "assistant_message": "Ok.", "action": {"type": "noop"}}



def _timed_segments(text: str) -> list[dict[str, Any]]:
    """Minimal valid timed plan for the 12.5s fixture — the draft tool refuses
    a flat agent script on footage this long (it would render one read parked
    at 0:00), so tests draft the way the contract now demands."""
    return [{"text": text, "start_s": 0.8, "delivery": "even, clear"}]


def _user_generates_voiceover(client: TestClient, session_id: str, **payload: Any):
    """The one legitimate spend path: the card's Generate button (choice)."""
    return client.post(
        f"/api/v2/agentic/audio/sessions/{session_id}/choices",
        json={"choice_type": "voiceover", "payload": payload},
    )

def test_propose_script_records_draft_and_voice_options(tmp_path: Path) -> None:
    script_decision = {
        "thought": "Draft narration.",
        "intent": "add_voiceover",
        "assistant_message": "Here's a script.",
        "action": {
            "type": "call_tool",
            "tool_name": "propose_script",
            "tool_args": {
                "narration_segments": _timed_segments(
                    "Discover the new collection. Link in bio."
                ),
                "voice_id": "bright_female",
            },
        },
    }
    client, _, _, queue, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [script_decision, _NOOP]
    )
    session = _create_session(client, source)
    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Add a voiceover."},
    )
    payload = response.json()
    cards = [e for e in payload["events"] if e["event_type"] == "voiceover.script"]
    assert len(cards) == 1
    assert cards[0]["payload"]["script"].startswith("Discover the new collection")
    assert cards[0]["payload"]["voice_id"] == "bright_female"
    assert any(o["id"] == "warm_female" for o in cards[0]["payload"]["voice_options"])

    vo = payload["snapshot"]["state"]["layers"]["voiceover"]
    assert vo["status"] == "draft"
    assert vo["voice_id"] == "bright_female"
    # The roster travels with the LAYER from the draft on. Attached only by
    # post-render hydration, the console fell back to a stale hardcoded list at
    # exactly the moment a user picks a voice — and spent a paid render on a
    # voice the select had silently substituted.
    assert len(vo["voice_options"]) == len(cards[0]["payload"]["voice_options"]) > 5
    # Drafting a script is free: no generation job was enqueued.
    assert all(env.task_type != "voiceover" for env in queue.envelopes)


def test_generate_voiceover_is_gated_then_enqueues(tmp_path: Path) -> None:
    """The spend is the USER'S CLICK, not the agent's judgement. A chat "yes"
    used to let the agent draft and record in one move — a live session watched
    a paid render it never approved (2026-08-27). Now the agent's own
    generate_voiceover is refused even WITH a drafted script and an approving
    message; only the card's Generate button (the choice endpoint) records."""

    decisions = _bootstrap_decisions() + [
        {  # turn 1: draft the script (free)
            "thought": "",
            "intent": "add_voiceover",
            "assistant_message": "Draft.",
            "action": {
                "type": "call_tool",
                "tool_name": "propose_script",
                "tool_args": {
                    "narration_segments": _timed_segments("Shop the collection now."),
                    "voice_id": "calm_male",
                },
            },
        },
        _NOOP,
        {  # turn 2: the agent tries to record off a chat "yes" -> BLOCKED
            "thought": "Approved.",
            "intent": "approve_direction",
            "assistant_message": "Recording it.",
            "action": {
                "type": "call_tool",
                "tool_name": "generate_voiceover",
                "tool_args": {"voice_id": "calm_male", "speed": 1.0},
            },
        },
    ]
    client, _, async_repo, queue, source = _client_with_decisions(tmp_path, decisions)
    session = _create_session(client, source)
    client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Add narration."},
    )
    envs_before = len(queue.envelopes)

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Yes, generate that voiceover."},
    )
    assert response.status_code == 200, response.text
    # Nothing spent: the chat approval is not the click.
    assert len(queue.envelopes) == envs_before
    assistant = [
        m for m in response.json()["snapshot"]["messages"] if m["role"] == "assistant"
    ][-1]
    assert "generate" in assistant["content"].lower()
    assert "card" in assistant["content"].lower()

    # The user's own click on the card IS the approval.
    choice = _user_generates_voiceover(
        client, session["session_id"], voice_id="calm_male", speed=1.0
    )
    assert choice.status_code == 200, choice.text
    assert len(queue.envelopes) == envs_before + 1
    envelope = queue.envelopes[-1]
    assert envelope.task_type == "voiceover"
    assert envelope.queue_name.endswith("voiceover-pipeline")
    job = async_repo.get_job(envelope.job_id)
    assert job.job_type == "voiceover"
    assert job.request_json["script"] == "Shop the collection now."
    assert job.request_json["tts_voice"] == "onyx"  # calm_male -> onyx

    vo = choice.json()["snapshot"]["state"]["layers"]["voiceover"]
    assert vo["status"] == "queued"
    assert vo["linked_job_id"] == envelope.job_id


def test_generate_click_with_unchanged_script_keeps_the_timed_draft(
    tmp_path: Path,
) -> None:
    """The card sends its textarea back on every Generate click. An unchanged
    script must NOT re-draft — that wiped the agent's timed segments and the
    held-silent beat, and the render came out as one continuous read at 0:00
    (live, twice, 2026-08-27)."""

    timed_draft = {
        "thought": "",
        "intent": "add_voiceover",
        "assistant_message": "Draft.",
        "action": {
            "type": "call_tool",
            "tool_name": "propose_script",
            "tool_args": {
                "narration_segments": [
                    {"text": "First line.", "start_s": 1.0, "delivery": "soft"},
                    {"text": "Last line.", "start_s": 9.5, "delivery": "final"},
                ],
                "voice_id": "narrator_male",
            },
        },
    }
    client, _, async_repo, queue, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [timed_draft, _NOOP]
    )
    session = _create_session(client, source)
    drafted = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Add narration."},
    ).json()["snapshot"]["state"]["layers"]["voiceover"]
    assert len(drafted["segments"]) == 2

    # The card's Generate click, textarea untouched — sends the same script.
    choice = _user_generates_voiceover(
        client, session["session_id"],
        script=drafted["script"], voice_id="narrator_male",
    )
    assert choice.status_code == 200, choice.text
    job = async_repo.get_job(queue.envelopes[-1].job_id)
    starts = [s2["start_s"] for s2 in job.request_json.get("segments") or []]
    assert starts == [1.0, 9.5], (
        f"the timed plan must reach the render, got segments={job.request_json.get('segments')}"
    )

    # An actual edit still re-drafts (flat, user's explicit intent).
    edited = _user_generates_voiceover(
        client, session["session_id"],
        script="Something the user actually rewrote.", voice_id="narrator_male",
    )
    assert edited.status_code == 200, edited.text
    job2 = async_repo.get_job(queue.envelopes[-1].job_id)
    assert job2.request_json["script"] == "Something the user actually rewrote."


def test_proposal_tiers_are_clamped_to_available_providers(monkeypatch) -> None:
    """The director proposed premium tiers whose provider had no key on the
    box; render-time fallback then substituted a different (slower) provider
    than every label on screen. Tiers are now decided at PROPOSE time, from
    what can actually run here."""

    from EdennCode.EdennAgent.AgenticAudio.models import usable_music_modelspec

    for var in ("PROVIDER_B_API_KEY", "EDENN_ENHANCED_PROVIDER_B_API_KEY",
                "PROVIDER_C_API_KEY", "PROVIDER_A_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    for k in [k for k in list(__import__("os").environ) if k.startswith(("PROVIDER_B_API_KEY_", "PROVIDER_C_API_KEY_"))]:
        monkeypatch.delenv(k, raising=False)

    # Only the basic tier's provider is configured -> everything lands there.
    monkeypatch.setenv("PROVIDER_A_API_KEY", "k")
    assert usable_music_modelspec("edenn_enhanced") == "edenn_basic"
    assert usable_music_modelspec("edenn_basic") == "edenn_basic"

    # The premium tier's own key makes it usable again.
    monkeypatch.setenv("PROVIDER_B_API_KEY", "k")
    assert usable_music_modelspec("edenn_enhanced") == "edenn_enhanced"


def test_the_rendered_tier_outranks_the_requested_one_on_the_candidate(
    tmp_path: Path,
) -> None:
    """A render-time fallback must be visible on the take: the tier that
    actually played, with the request preserved alongside."""

    client, _, async_repo, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    # Simulate a completed job whose render fell back to a different tier.
    snap = client.get(f"/api/v2/agentic/audio/sessions/{sid}").json()
    agent_state = snap["state"]
    job = async_repo.create_job(job_id="job_tier", job_type="video_music",
                                request_json={"modelspec": "edenn_enhanced"})
    async_repo.update_job_status("job_tier", status="completed", result_json={
        "audio_url": "/dev/media/take.mp3", "placeholder": False,
        "modelspec": "edenn_studio",
    })
    from EdennCode.EdennAgent.AgenticAudio.tools.media import AgenticAudioTools

    tools = AgenticAudioTools(async_repository=async_repo, queue=queue,
                              settings=SimpleNamespace(workdir=tmp_path),
                              analyze_fn=_fake_analyze)
    [item] = tools.hydrate_candidate_results(candidates=[{
        "candidate_id": "c1", "modelspec": "edenn_enhanced",
        "linked_job_id": "job_tier", "status": "queued",
    }])
    assert item["modelspec"] == "edenn_studio"
    assert item["requested_modelspec"] == "edenn_enhanced"
    assert item["provider"] == "provider_c"


def test_rerecord_starts_from_intent_not_the_last_renders_speed(tmp_path: Path) -> None:
    """The renderer writes realized speeds back onto the layer's segments; a
    re-record must NOT inherit them — that made every re-record start at the
    previous render's sped-up values, so the rush compounded forever."""

    timed_draft = {
        "thought": "", "intent": "add_voiceover", "assistant_message": "Draft.",
        "action": {"type": "call_tool", "tool_name": "propose_script",
                   "tool_args": {"narration_segments": _timed_segments("A line."),
                                 "voice_id": "calm_male"}},
    }
    client, agent_repo, async_repo, queue, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [timed_draft, _NOOP]
    )
    session = _create_session(client, source)
    sid = session["session_id"]
    client.post(f"/api/v2/agentic/audio/sessions/{sid}/messages",
                json={"content": "Add narration."})

    # Simulate the previous render having realized a sped-up read.
    stored = agent_repo.get_session(sid)
    state = dict(stored.state_json)
    vo = dict((state.get("layers") or {}).get("voiceover") or {})
    vo["segments"] = [{"id": "seg_01", "text": "A line.", "start_s": 0.8,
                      "delivery": "even", "speed": 1.15, "duration_s": 2.4,
                      "crosses_cut": 2.0}]
    state["layers"] = {**(state.get("layers") or {}), "voiceover": vo}
    agent_repo.update_session(sid, state_json=state)

    choice = _user_generates_voiceover(client, sid, script=vo.get("script") or "A line.",
                                       voice_id="calm_male")
    assert choice.status_code == 200, choice.text
    job = async_repo.get_job(queue.envelopes[-1].job_id)
    [seg] = job.request_json["segments"]
    assert "speed" not in seg and "duration_s" not in seg and "crosses_cut" not in seg, seg
    assert seg["start_s"] == 0.8 and seg["delivery"] == "even"


def test_each_tier_calls_its_own_provider_no_cross_wiring() -> None:
    """Basic is basic, enhanced is enhanced: the registry must bind each tier
    to ITS provider's strategy, and the tier→provider map must agree. A swap
    here mislabels every take that renders."""

    from EdennCode.EdennAgent.AgenticAudio.models import (
        PROVIDER_BY_MODELSPEC,
        PROVIDER_A,
        PROVIDER_B,
        PROVIDER_C,
    )
    from EdennCode.MusicGenerationCore.provider_registry import (
        build_default_music_generation_service,
    )
    from EdennCode.MusicGenerationCore.models import MusicModelSpec

    assert PROVIDER_BY_MODELSPEC == {
        "edenn_basic": PROVIDER_A,
        "edenn_enhanced": PROVIDER_B,
        "edenn_studio": PROVIDER_C,
    }
    service = build_default_music_generation_service()
    builders = service._strategy_builders
    # The strategy CLASS names carry the provider; assert the binding without
    # constructing (construction requires live keys).
    by_spec = {
        MusicModelSpec.EDENN_BASIC: "ProviderA",
        MusicModelSpec.EDENN_ENHANCED: "ProviderB",
        MusicModelSpec.EDENN_STUDIO: "ProviderC",
    }
    import inspect

    for spec, provider_marker in by_spec.items():
        src = inspect.getsource(builders[spec])
        assert provider_marker in src, f"{spec} is not bound to {provider_marker}"


def test_tier_availability_reads_the_same_keys_the_builders_read(monkeypatch) -> None:
    """Availability may be conservative, never wrong: every key family a
    builder accepts must make the tier available (a numbered-keys-only box
    used to withhold basic while fully able to render it)."""

    from EdennCode.EdennAgent.AgenticAudio.models import music_modelspec_available

    for var in list(__import__("os").environ):
        if "API_KEY" in var:
            monkeypatch.delenv(var, raising=False)
    assert not music_modelspec_available("edenn_basic")
    monkeypatch.setenv("PROVIDER_A_API_KEY_2", "k")
    assert music_modelspec_available("edenn_basic")
    monkeypatch.setenv("PROVIDER_B_API_KEY_1", "k")
    assert music_modelspec_available("edenn_enhanced")
    monkeypatch.setenv("PROVIDER_C_API_KEY_3", "k")
    assert music_modelspec_available("edenn_studio")


def test_render_fallback_chain_matches_the_propose_chain() -> None:
    """The two deciding differently is how a label goes stale between
    screens — the devserver's render-time chain must offer every tier the
    propose-time gate can offer."""

    src = Path("EdennCode/EdennAgent/AgenticAudio/design/devserver.py").read_text()
    assert '("edenn_studio", "edenn_basic", "edenn_enhanced")' in src


def test_retake_ladder_keeps_the_calm_read_when_no_speed_can_clear(tmp_path: Path) -> None:
    """On fast-cut footage every line straddles a cut; the ladder used to speed
    up the entire read for a shrink that could never clear the shot — a live
    listener called the result "way too fast". A doomed retake is skipped, and
    a restrained delivery never takes the top rung."""

    import asyncio as _asyncio

    from EdennCode.EdennAgent.AgenticAudio.tools.narration_render import (
        render_segmented_narration,
    )

    calls: list[float] = []

    async def synth(*, script, voice, instructions, speed, out_path) -> None:
        calls.append(speed)
        import math, struct, wave

        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
            # ~2.6s of tone: longer than any 2s shot even at 1.15x.
            frames = int(8000 * 2.6 / max(speed, 0.5))
            w.writeframes(b"".join(
                struct.pack("<h", int(3000 * math.sin(i / 8))) for i in range(frames)
            ))

    out = tmp_path / "vo.wav"
    result = _asyncio.run(render_segmented_narration(
        segments=[{"id": "seg_01", "text": "A line that will straddle.",
                   "start_s": 0.2, "delivery": "low, ominous, controlled"}],
        synthesize=synth,
        voice="onyx",
        workdir=tmp_path / "wd",
        out_path=out,
        video_duration_s=20.0,
        # Cuts every ~2s: a 2.6s line cannot fit any shot at any legal speed.
        cuts=[2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0],
    ))
    assert result is not None and out.exists()
    # One synthesis only: the doomed retake rungs were skipped entirely.
    assert calls == [1.0], f"retakes fired for an unwinnable shot: {calls}"
    assert result[0]["speed"] == 1.0


def test_model_cannot_launder_its_plan_as_the_users(tmp_path: Path) -> None:
    """source:"user" counts only from a real user control. A model writing the
    magic string into its own args must not be able to delete the layers the
    user ticked at the gate — and the refusal must be LOUD in the result, or
    the model retries the same call every turn (live stall, 2026-08-27)."""

    laundering = {
        "thought": "",
        "intent": "plan_audio",
        "assistant_message": "Narration only now.",
        "action": {
            "type": "call_tool",
            "tool_name": "set_production_plan",
            "tool_args": {"mode": "music_first", "layers": ["voiceover"],
                          "source": "user", "force_music": False},
        },
    }
    client, source = _gate_client_with_decisions(tmp_path, [laundering])
    session = _create_session(client, source)
    sid = session["session_id"]

    # The USER ticks music+sfx at the gate; the model then runs its laundering
    # turn claiming its narration-only plan is the user's own.
    _answer_gate(client, sid, "music_only", ["music", "sfx"])
    snap = client.get(f"/api/v2/agentic/audio/sessions/{sid}").json()
    plan = snap["state"]["production_plan"]
    assert "music" in plan["layers"] and "sfx" in plan["layers"], plan
    assert plan.get("source") == "user"


def test_agent_flat_script_on_long_footage_is_refused_with_timing_instructions(
    tmp_path: Path,
) -> None:
    """A flat agent script renders as one continuous read parked at 0:00 — a
    live 15s session got exactly that: narration from second zero straight
    through every cut, dead air after (2026-08-27). On footage >12s the draft
    tool refuses and instructs timed segments. GATED, not advised."""

    flat_draft = {
        "thought": "",
        "intent": "add_voiceover",
        "assistant_message": "Draft.",
        "action": {
            "type": "call_tool",
            "tool_name": "propose_script",
            "tool_args": {"script": "One long read.", "voice_id": "narrator_male"},
        },
    }
    client, _, _, queue, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [flat_draft, _NOOP]
    )
    session = _create_session(client, source)
    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Add a voiceover."},
    )
    assert response.status_code == 200, response.text
    vo = (response.json()["snapshot"]["state"].get("layers") or {}).get("voiceover") or {}
    # No draft was stored off the flat script, and nothing was enqueued.
    assert not (vo.get("script") or "").strip()
    assert all(env.task_type != "voiceover" for env in queue.envelopes)


def test_generate_voiceover_refused_without_script(tmp_path: Path) -> None:
    """Cost gate: generate_voiceover with no drafted script degrades safely (no spend)."""

    decisions = _bootstrap_decisions() + [
        {
            "thought": "Trying to skip approval.",
            "intent": "add_voiceover",
            "assistant_message": "Generating.",
            "action": {
                "type": "call_tool",
                "tool_name": "generate_voiceover",
                "tool_args": {"voice_id": "warm_female"},
            },
        }
    ]
    client, _, _, queue, source = _client_with_decisions(tmp_path, decisions)
    session = _create_session(client, source)
    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Just make a voiceover right now."},
    )
    # The gate degrades gracefully (re-ask, not 400) and nothing was enqueued.
    assert response.status_code == 200, response.text
    assert all(env.task_type != "voiceover" for env in queue.envelopes)


class _FakeSynthesizer:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.calls: list[dict[str, Any]] = []

    async def synthesize(self, *, script, voice, instructions, speed, out_path) -> Path:
        self.calls.append(
            {"script": script, "voice": voice, "instructions": instructions, "speed": speed}
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"RIFF....WAVEfmt voiceover")
        return out_path


def test_voiceover_worker_synthesizes_and_hydrates(tmp_path: Path) -> None:
    from EdennCode.Deployment.async_pipeline_v2.workers.voiceover_worker import (
        VoiceoverWorker,
    )

    decisions = _bootstrap_decisions() + [
        {
            "thought": "",
            "intent": "add_voiceover",
            "assistant_message": "Draft.",
            "action": {
                "type": "call_tool",
                "tool_name": "propose_script",
                "tool_args": {
                    "narration_segments": _timed_segments("Welcome to the show."),
                    "voice_id": "narrator_male",
                },
            },
        },
        _NOOP,
    ]
    agent_repo = _MemoryAgenticRepository()
    async_repo = _MemoryAsyncRepository()
    queue = _MemoryQueue()
    storage = _RecordingStorage()
    source = _seed_source_video(async_repo)
    context = SimpleNamespace(
        settings=SimpleNamespace(
            workdir=tmp_path,
            async_v2_queue_namespace="vo-e2e",
            upload_container="user-uploads",
            output_container="generated-media",
            audio_container_name="generated-audio",
        ),
        storage=storage,
    )
    tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=context.settings,
        storage=storage,
        analyze_fn=_fake_analyze,
    )
    app = FastAPI()
    app.include_router(
        create_agentic_audio_router(
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=tools,
            llm_client=_ScriptedAgentClient(decisions),
            collab_repository=InMemoryCollabRepository(),
        )
    )
    client = TestClient(app)
    session = _create_session(client, source)
    client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Add a voiceover."},
    )
    # The user's own click on the card records; a chat approval no longer can.
    choice = _user_generates_voiceover(
        client, session["session_id"], voice_id="narrator_male"
    )
    assert choice.status_code == 200, choice.text
    vo_envelope = queue.envelopes[-1]
    assert vo_envelope.task_type == "voiceover"

    worker = VoiceoverWorker(
        repository=async_repo,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        settings=context.settings,
        storage=storage,
        synthesizer=_FakeSynthesizer(tmp_path / "vo_out"),
        worker_id="vo-e2e-worker",
        queue_name="vo-e2e:voiceover-pipeline",
        lease_seconds=30,
    )
    processed = asyncio.run(worker.process_one())
    assert processed is not None
    assert processed.status == TaskStatus.COMPLETED

    job = async_repo.get_job(vo_envelope.job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.result_json["audio_url"].startswith("https://storage.test/")

    state = client.get(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}"
    ).json()["state"]
    vo = state["layers"]["voiceover"]
    assert vo["status"] == JobStatus.COMPLETED
    assert vo["audio_url"].startswith("https://storage.test/")
    assert "voiceover_audio" in {a.artifact_type for a in async_repo.artifacts.values()}


def test_voice_tone_flows_into_tts_instructions(tmp_path: Path) -> None:
    decisions = _bootstrap_decisions() + [
        {
            "thought": "",
            "intent": "add_voiceover",
            "assistant_message": "Draft.",
            "action": {
                "type": "call_tool",
                "tool_name": "propose_script",
                "tool_args": {
                    "narration_segments": _timed_segments("Big news today."),
                    "voice_id": "warm_female",
                    "tone": "excited and breathy",
                },
            },
        },
        _NOOP,
    ]
    client, _, async_repo, queue, source = _client_with_decisions(tmp_path, decisions)
    session = _create_session(client, source)
    draft = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Add a voiceover, sound excited."},
    )
    assert draft.json()["snapshot"]["state"]["layers"]["voiceover"]["tone"] == "excited and breathy"

    choice = _user_generates_voiceover(
        client, session["session_id"], voice_id="warm_female"
    )
    assert choice.status_code == 200, choice.text
    job = async_repo.get_job(queue.envelopes[-1].job_id)
    assert job.request_json["tone"] == "excited and breathy"
    assert "excited and breathy" in job.request_json["tts_instructions"]


class _RecordingCompose:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "status": "completed",
            "video_url": "https://storage.test/mix.mp4?sig=fake",
            "music_volume": kwargs["music_volume"],
            "voiceover_volume": kwargs["voiceover_volume"],
            "voiceover_start_s": kwargs["voiceover_start_s"],
            "duck_gain_db": kwargs["duck_gain_db"],
        }


def _client_with_compose(
    tmp_path: Path, decisions: list[dict[str, Any]], compose: _RecordingCompose
) -> tuple[TestClient, _MemoryAgenticRepository, _MemoryAsyncRepository, _MemoryQueue, AsyncV2Artifact]:
    agent_repo = _MemoryAgenticRepository()
    async_repo = _MemoryAsyncRepository()
    queue = _MemoryQueue()
    source = _seed_source_video(async_repo)
    context = _context(tmp_path)
    tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=context.settings,
        analyze_fn=_fake_analyze,
        compose_fn=compose,
    )
    app = FastAPI()
    app.include_router(
        create_agentic_audio_router(
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=tools,
            llm_client=_ScriptedAgentClient(decisions),
            collab_repository=InMemoryCollabRepository(),
        )
    )
    return TestClient(app), agent_repo, async_repo, queue, source


def _seed_music_and_voiceover(agent_repo: _MemoryAgenticRepository, session_id: str) -> None:
    """Inject a ready music candidate + voice-over layer for compose tests."""

    session = agent_repo.get_session(session_id)
    state = dict(session.state_json)
    state["candidates"] = [
        {
            "candidate_id": "c1",
            "proposal_id": "proposal_studio",
            "title": "Studio",
            "modelspec": "edenn_studio",
            "audio_url": "https://cdn.test/music.mp3",
            "music_volume": 0.85,
            "status": "completed",
        }
    ]
    state["layers"] = {
        "voiceover": {"audio_url": "https://cdn.test/vo.wav", "status": "completed"}
    }
    agent_repo.update_session(session_id, state_json=state)


def test_compose_mix_layers_music_and_voiceover(tmp_path: Path) -> None:
    compose = _RecordingCompose()
    decisions = _bootstrap_decisions() + [
        {
            "thought": "",
            "intent": "adjust_mix",
            "assistant_message": "Mixing.",
            "action": {
                "type": "call_tool",
                "tool_name": "compose_mix",
                "tool_args": {
                    "music_volume": 0.5,
                    "voiceover_volume": 1.2,
                    "voiceover_start_s": 2.0,
                    "duck_gain_db": -12.0,
                },
            },
        },
        _NOOP,
    ]
    client, agent_repo, _, queue, source = _client_with_compose(tmp_path, decisions, compose)
    session = _create_session(client, source)
    _seed_music_and_voiceover(agent_repo, session["session_id"])

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Lower the music under the voice and start narration at 2s."},
    )
    payload = response.json()
    assert any(e["event_type"] == "mix.updated" for e in payload["events"])
    # compose_mix is free: no generation job enqueued.
    assert len(queue.envelopes) == 0

    mix = payload["snapshot"]["state"]["mix"]
    assert mix["video_url"].endswith("mix.mp4?sig=fake")
    assert mix["music_volume"] == 0.5
    assert mix["voiceover_volume"] == 1.2
    assert mix["voiceover_start_s"] == 2.0
    assert mix["duck_gain_db"] == -12.0
    assert mix["music_candidate_id"] == "c1"

    assert compose.calls[-1]["music_audio_url"] == "https://cdn.test/music.mp3"
    assert compose.calls[-1]["voiceover_audio_url"] == "https://cdn.test/vo.wav"


def _compose_turn(client: TestClient, session_id: str) -> dict[str, Any]:
    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session_id}/messages",
        json={"content": "Mix it."},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _mix_decisions() -> list[dict[str, Any]]:
    return _bootstrap_decisions() + [
        {
            "thought": "",
            "intent": "adjust_mix",
            "assistant_message": "Mixing.",
            "action": {"type": "call_tool", "tool_name": "compose_mix", "tool_args": {}},
        },
        _NOOP,
    ]


def test_bad_generation_args_are_refused_without_leaving_a_spend_fingerprint(
    tmp_path: Path,
) -> None:
    """The guard records a fingerprint BEFORE a paid tool runs, so a call that
    dies inside the tool leaves one behind and the corrected retry reads as a
    duplicate. Validating first means a refused call never spends and never
    blocks the repair."""
    bad_then_good = _bootstrap_decisions() + [
        {
            "thought": "",
            "intent": "request_proposals",
            "assistant_message": "",
            "action": {
                "type": "call_tool",
                "tool_name": "generate_candidates",
                # int({}) raises TypeError inside the tool — nothing caught it.
                "tool_args": {"proposal_id": "proposal_cinematic", "count": {"n": 2}},
            },
        },
        _NOOP,
    ]
    client, agent_repo, _, queue, source = _client_with_decisions(tmp_path, bad_then_good)
    session = _create_session(client, source)
    session_id = session["session_id"]
    enqueued_before = len(queue.envelopes)

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session_id}/messages",
        json={"content": "make me two takes"},
    )

    assert response.status_code == 200, "a bad argument tore down the whole turn"
    assert len(queue.envelopes) == enqueued_before, "a refused call still spent"

    state = agent_repo.get_session(session_id).state_json
    assert not (state.get("recent_generations") or []), (
        "the refused call left a fingerprint that would block the corrected retry"
    )
    turn = (state.get("turns") or [])[-1]
    assert any(a.get("action") == "tool_error" for a in turn["actions"]), (
        "the turn record should show the call was rejected, not that it ran"
    )


def test_compose_forwards_realized_narration_windows_for_per_line_ducking(
    tmp_path: Path,
) -> None:
    """The mix ducks the music under each spoken LINE and recovers between them
    — but only if the realized windows reach ffmpeg. They are computed by the
    tool and accepted by the in-process path, and were dropped on the injected
    path, which is the one the standalone deploy actually runs: the shipped mix
    ducked one flat window across the whole narration."""
    compose = _RecordingCompose()
    client, agent_repo, _, _, source = _client_with_compose(
        tmp_path, _mix_decisions(), compose
    )
    session = _create_session(client, source)
    session_id = session["session_id"]
    _seed_music_and_voiceover(agent_repo, session_id)

    state = dict(agent_repo.get_session(session_id).state_json)
    state["layers"]["voiceover"]["segments"] = [
        {"id": "s1", "text": "first line", "start_s": 1.0, "duration_s": 2.5},
        {"id": "s2", "text": "second line", "start_s": 8.0, "duration_s": 1.5},
    ]
    agent_repo.update_session(session_id, state_json=state)

    _compose_turn(client, session_id)

    assert compose.calls[-1]["voiceover_segments"] == [(1.0, 3.5), (8.0, 9.5)]


def test_compose_sends_no_windows_when_the_narration_is_not_measured_yet(
    tmp_path: Path,
) -> None:
    """A still-planned segment has no realized duration. Guessing one would duck
    the music over silence; the honest fallback is one window across the whole
    narration, which is what ffmpeg does when it gets nothing."""
    compose = _RecordingCompose()
    client, agent_repo, _, _, source = _client_with_compose(
        tmp_path, _mix_decisions(), compose
    )
    session = _create_session(client, source)
    session_id = session["session_id"]
    _seed_music_and_voiceover(agent_repo, session_id)

    state = dict(agent_repo.get_session(session_id).state_json)
    state["layers"]["voiceover"]["segments"] = [
        {"id": "s1", "text": "not rendered yet", "start_s": 1.0}
    ]
    agent_repo.update_session(session_id, state_json=state)

    _compose_turn(client, session_id)

    assert compose.calls[-1]["voiceover_segments"] is None


def _seed_two_rendered_takes(
    agent_repo: _MemoryAgenticRepository, session_id: str
) -> None:
    """Two finished takes, NOTHING locked — the state the live P0 was found in."""

    session = agent_repo.get_session(session_id)
    state = dict(session.state_json)
    state["candidates"] = [
        {
            "candidate_id": "c1",
            "proposal_id": "proposal_a",
            "title": "Take one",
            "modelspec": "edenn_enhanced",
            "audio_url": "https://cdn.test/music-1.mp3",
            "status": "completed",
        },
        {
            "candidate_id": "c2",
            "proposal_id": "proposal_a",
            "title": "Take two",
            "modelspec": "edenn_enhanced",
            "audio_url": "https://cdn.test/music-2.mp3",
            "status": "completed",
        },
    ]
    state.pop("selected_candidate_id", None)
    state["layers"] = {
        "voiceover": {"audio_url": "https://cdn.test/vo.wav", "status": "completed"}
    }
    agent_repo.update_session(session_id, state_json=state)


def _no_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier availability must not depend on whatever keys the dev box holds."""

    for key in list(os.environ):
        if key.startswith(
            ("PROVIDER_C_API_KEY", "PROVIDER_B_API_KEY", "PROVIDER_A_API_KEY", "EDENN_ENHANCED_PROVIDER_B")
        ):
            monkeypatch.delenv(key, raising=False)


def test_proposal_choice_honours_the_users_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tier rides the approval click, and outranks the director's pick.

    There was no tier control in the console at all: across every organic
    session on the deployment the director chose a premium tier 26 times out of
    26 while the analysis suggested the basic one every time.
    """

    _no_provider_keys(monkeypatch)
    client, agent_repo, _, _, source = _client_with_compose(
        tmp_path, _bootstrap_decisions() + [_NOOP], _RecordingCompose()
    )
    sid = _create_session(client, source)["session_id"]
    sess = agent_repo.get_session(sid)
    state = dict(sess.state_json)
    # As the director actually proposes: a premium tier, unasked.
    for entry in state["proposals"]:
        entry["modelspec"] = "edenn_studio"
    agent_repo.update_session(sid, state_json=state)

    body = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={
            "choice_type": "proposal",
            "target_id": "proposal_cinematic",
            "payload": {"modelspec": "edenn_basic"},
        },
    ).json()
    st = body["snapshot"]["state"]

    approved = next(p for p in st["proposals"] if p["proposal_id"] == "proposal_cinematic")
    assert approved["modelspec"] == "edenn_basic"
    assert approved["modelspec_source"] == "user"
    # The take count follows the tier, so the choice reached generation.
    assert len(st["candidates"]) == 1
    assert st["candidates"][0]["modelspec"] == "edenn_basic"


def test_proposal_choice_keeps_the_ask_when_the_tier_is_substituted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tier this box cannot render is re-pinned — and says so."""

    _no_provider_keys(monkeypatch)
    monkeypatch.setenv("PROVIDER_C_API_KEY", "studio-key")  # studio only
    client, agent_repo, _, _, source = _client_with_compose(
        tmp_path, _bootstrap_decisions() + [_NOOP], _RecordingCompose()
    )
    sid = _create_session(client, source)["session_id"]

    body = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={
            "choice_type": "proposal",
            "target_id": "proposal_cinematic",
            "payload": {"modelspec": "edenn_enhanced"},
        },
    ).json()
    approved = next(
        p for p in body["snapshot"]["state"]["proposals"]
        if p["proposal_id"] == "proposal_cinematic"
    )
    assert approved["modelspec"] == "edenn_studio"        # what can render here
    assert approved["requested_modelspec"] == "edenn_enhanced"  # what was asked


def test_structured_choice_refolds_the_session_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approving a direction updates the memory the notes panel reads.

    The fold used to run only on free-text chat turns, so in a session whose
    only chat turn came BEFORE any proposal existed it froze on the analysis
    default — Director's notes read "edenn_basic" beside canvas takes that had
    rendered and billed on another tier (live audit, 2026-08-30).
    """

    _no_provider_keys(monkeypatch)
    client, agent_repo, _, _, source = _client_with_compose(
        tmp_path, _bootstrap_decisions() + [_NOOP], _RecordingCompose()
    )
    sid = _create_session(client, source)["session_id"]
    before = agent_repo.get_session(sid).state_json["memory"]["preferences"]
    assert before.get("modelspec") == "edenn_basic"  # the analysis default

    body = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={
            "choice_type": "proposal",
            "target_id": "proposal_cinematic",
            "payload": {"modelspec": "edenn_studio"},
        },
    ).json()
    memory = body["snapshot"]["state"]["memory"]
    assert memory["preferences"]["modelspec"] == "edenn_studio"
    # A fold is not a turn: it must not pad the intent history.
    assert memory["recent_intents"].count("other") <= 1


def test_compose_mix_refuses_when_the_music_take_is_ambiguous(tmp_path: Path) -> None:
    """Two rendered takes and none locked: the mix would carry no music at all.

    The deployed console shipped exactly that file — 45-53 dB under the music
    stem — because the candidate-resolution failure was swallowed and the mix
    composed anyway. Nothing may be composed here, and the refusal must name
    the takes so the next call can pick one.
    """

    compose = _RecordingCompose()
    decisions = _bootstrap_decisions() + [
        {
            "thought": "",
            "intent": "adjust_mix",
            "assistant_message": "Mixing.",
            "action": {"type": "call_tool", "tool_name": "compose_mix", "tool_args": {}},
        },
        _NOOP,
    ]
    client, agent_repo, _, queue, source = _client_with_compose(tmp_path, decisions, compose)
    session = _create_session(client, source)
    _seed_two_rendered_takes(agent_repo, session["session_id"])

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Lock take 2 and compose the final mix."},
    )
    body = response.json()

    # Nothing was composed: no ffmpeg call, no mix on the session, no event
    # telling the user a mix is ready.
    assert compose.calls == []
    assert not (body["snapshot"]["state"].get("mix") or {}).get("video_url")
    assert "mix.updated" not in [e["event_type"] for e in body["events"]]

    # The refusal is recorded, and it names both takes.
    blocked = [
        tc
        for tc in agent_repo.list_tool_calls(session["session_id"])
        if tc.tool_name == "compose_mix"
    ]
    assert blocked and blocked[-1].status == "failed"
    assert blocked[-1].output_json["candidate_ids"] == ["c1", "c2"]


def test_compose_mix_carries_the_named_take(tmp_path: Path) -> None:
    """The same ambiguous session composes fine once the take is named."""

    compose = _RecordingCompose()
    decisions = _bootstrap_decisions() + [
        {
            "thought": "",
            "intent": "adjust_mix",
            "assistant_message": "Mixing take two.",
            "action": {
                "type": "call_tool",
                "tool_name": "compose_mix",
                "tool_args": {"candidate_id": "c2"},
            },
        },
        _NOOP,
    ]
    client, agent_repo, _, queue, source = _client_with_compose(tmp_path, decisions, compose)
    session = _create_session(client, source)
    _seed_two_rendered_takes(agent_repo, session["session_id"])

    body = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Compose the mix with take two."},
    ).json()

    mix = body["snapshot"]["state"]["mix"]
    assert mix["music_candidate_id"] == "c2"
    assert compose.calls[-1]["music_audio_url"] == "https://cdn.test/music-2.mp3"


def test_compose_mix_adjusts_one_knob_at_a_time(tmp_path: Path) -> None:
    compose = _RecordingCompose()
    decisions = _bootstrap_decisions() + [
        {
            "thought": "",
            "intent": "adjust_mix",
            "assistant_message": "Mixing.",
            "action": {
                "type": "call_tool",
                "tool_name": "compose_mix",
                "tool_args": {"music_volume": 0.5, "duck_gain_db": -12.0},
            },
        },
        _NOOP,
        {
            "thought": "",
            "intent": "adjust_mix",
            "assistant_message": "Less ducking.",
            "action": {
                "type": "call_tool",
                "tool_name": "compose_mix",
                "tool_args": {"duck_gain_db": -6.0},
            },
        },
        _NOOP,
    ]
    client, agent_repo, _, _, source = _client_with_compose(tmp_path, decisions, compose)
    session = _create_session(client, source)
    _seed_music_and_voiceover(agent_repo, session["session_id"])

    client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Music to 50%, duck hard."},
    )
    second = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Actually duck less."},
    )
    mix = second.json()["snapshot"]["state"]["mix"]
    # The unspecified music_volume is retained from the prior mix; duck updated.
    assert mix["music_volume"] == 0.5
    assert mix["duck_gain_db"] == -6.0


class _NumberedCompose(_RecordingCompose):
    """Each compose returns a DIFFERENT file, so "which one shipped" is answerable."""

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        result = await super().__call__(**kwargs)
        result["video_url"] = f"https://storage.test/mix-{len(self.calls)}.mp4?sig=fake"
        return result


def test_composing_after_locking_updates_the_deliverable(tmp_path: Path) -> None:
    """Lock first, compose second — the order the live director actually used.

    That finalize produces a music-only artifact with no video, so a refresh
    that required an existing video skipped, and the session list went on
    serving the bare music stem while a real composed mix sat beside it in
    state. Found by driving the running server, not by reading the code.
    """

    compose = _NumberedCompose()
    turn = lambda name, args: {
        "thought": "",
        "intent": "adjust_mix",
        "assistant_message": "Working.",
        "action": {"type": "call_tool", "tool_name": name, "tool_args": args},
    }
    decisions = _bootstrap_decisions() + [
        turn("finalize", {"candidate_id": "c1"}), _NOOP,
        turn("compose_mix", {}), _NOOP,
    ]
    client, agent_repo, _, _, source = _client_with_compose(tmp_path, decisions, compose)
    sid = _create_session(client, source)["session_id"]
    _seed_music_and_voiceover(agent_repo, sid)
    post = lambda text: client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/messages", json={"content": text}
    ).json()

    post("Lock take one.")
    state = post("Now compose the mix.")["snapshot"]["state"]

    assert state["mix"]["video_url"] == "https://storage.test/mix-1.mp4?sig=fake"
    assert state["final_artifact"]["video_url"] == "https://storage.test/mix-1.mp4?sig=fake"
    assert state["final_artifact"]["deliverable"] == "compose_mix"

    listed = client.get("/api/v2/agentic/audio/sessions").json()
    row = next(s for s in listed["sessions"] if s["session_id"] == sid)
    assert row["final_media_url"] == "https://storage.test/mix-1.mp4?sig=fake"


def test_recompose_replaces_the_finalized_deliverable(tmp_path: Path) -> None:
    """A fixed mix must reach Export, the gallery and the share sheet.

    On the live deployment the user noticed the fault, recomposed, and state.mix
    held the corrected file — while final_artifact (and therefore every download
    surface, all of which derive from it) still served the broken one.
    """

    compose = _NumberedCompose()
    turn = lambda name, args: {
        "thought": "",
        "intent": "adjust_mix",
        "assistant_message": "Working.",
        "action": {"type": "call_tool", "tool_name": name, "tool_args": args},
    }
    decisions = _bootstrap_decisions() + [
        turn("compose_mix", {}), _NOOP,
        turn("finalize", {"candidate_id": "c1"}), _NOOP,
        turn("compose_mix", {"music_volume": 0.7}), _NOOP,
    ]
    client, agent_repo, _, _, source = _client_with_compose(tmp_path, decisions, compose)
    sid = _create_session(client, source)["session_id"]
    _seed_music_and_voiceover(agent_repo, sid)
    post = lambda text: client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/messages", json={"content": text}
    ).json()

    post("Compose the mix.")
    final = post("Lock it in.")["snapshot"]["state"]["final_artifact"]
    assert final["video_url"] == "https://storage.test/mix-1.mp4?sig=fake"

    state = post("Bring the music up a little and recompose.")["snapshot"]["state"]
    # The mix and the deliverable cannot disagree: one write, one source.
    assert state["mix"]["video_url"] == "https://storage.test/mix-2.mp4?sig=fake"
    assert state["final_artifact"]["video_url"] == "https://storage.test/mix-2.mp4?sig=fake"

    # The session list (gallery + resume) derives from final_artifact, so it
    # follows without a second code path.
    listed = client.get("/api/v2/agentic/audio/sessions").json()
    row = next(s for s in listed["sessions"] if s["session_id"] == sid)
    assert row["final_media_url"] == "https://storage.test/mix-2.mp4?sig=fake"


# --------------------------------------------------------------------------- #
# eval-plan correctness fixes: approval gate (S1/S3) + finalize-uses-mix (S7)  #
# --------------------------------------------------------------------------- #


def test_generate_without_approval_reasks_gracefully(tmp_path: Path) -> None:
    """A: a non-approval turn that tries to generate is re-asked (no 400, 0 jobs)."""

    cold_generate = {
        "thought": "Trying to spend without approval.",
        "intent": "new_variation",  # NOT an approval turn -> B does not fire
        "assistant_message": "Generating everything now.",
        "action": {
            "type": "call_tool",
            "tool_name": "generate_candidates",
            "tool_args": {"proposal_id": "proposal_cinematic", "count": 3},
        },
    }
    client, _, _, queue, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [cold_generate]
    )
    session = _create_session(client, source)
    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Generate three tracks."},
    )
    # Degrades safely: a friendly re-ask, NOT a 400, and nothing was spent.
    assert response.status_code == 200, response.text
    assert len(queue.envelopes) == 0
    assistant = [
        m for m in response.json()["snapshot"]["messages"] if m["role"] == "assistant"
    ][-1]
    assert "go ahead and generate" in assistant["content"].lower()


def test_approval_intent_alone_does_not_autospend(tmp_path: Path) -> None:
    """Safety: a bare approval-INTENT turn must NOT auto-unlock generation.

    The real-LLM eval showed the model labels a pushy "just spend money on
    everything" as approve_direction. So a free-text approval intent without the
    explicit approve_direction tool (or a /choices pick) re-asks — it never spends.
    """

    approve_intent_then_generate = {
        "thought": "Labeled as approval, but no explicit approve step.",
        "intent": "approve_direction",
        "assistant_message": "Generating now.",
        "action": {
            "type": "call_tool",
            "tool_name": "generate_candidates",
            "tool_args": {"proposal_id": "proposal_cinematic", "count": 2},
        },
    }
    client, agent_repo, _, queue, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [approve_intent_then_generate]
    )
    session = _create_session(client, source)
    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Just spend money and generate everything now."},
    )
    assert response.status_code == 200, response.text
    assert len(queue.envelopes) == 0  # NOTHING spent without an explicit approval
    assert response.json()["snapshot"]["state"].get("approved_direction") is not True


def test_approve_direction_then_generate_unlocks_gate(tmp_path: Path) -> None:
    """S1: explicit approval (free) unlocks generation in the same turn."""

    decisions = _bootstrap_decisions() + [
        _approve_decision("proposal_cinematic"),
        {
            "thought": "Now generate.",
            "intent": "approve_direction",
            "assistant_message": "Generating.",
            "action": {
                "type": "call_tool",
                "tool_name": "generate_candidates",
                "tool_args": {"proposal_id": "proposal_cinematic", "count": 2},
            },
        },
    ]
    client, agent_repo, _, queue, source = _client_with_decisions(tmp_path, decisions)
    session = _create_session(client, source)
    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Yes, go with the cinematic one."},
    )
    assert response.status_code == 200, response.text
    assert len(queue.envelopes) == 2
    assert response.json()["snapshot"]["state"]["approved_direction"] is True
    tools = [tc.tool_name for tc in agent_repo.list_tool_calls(session["session_id"])]
    assert "approve_direction" in tools and "generate_candidates" in tools


def test_proposal_choice_sets_approval_flag(tmp_path: Path) -> None:
    """The deterministic /choices proposal path is itself the approval."""

    client, _, _, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_cinematic"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"]["state"]["approved_direction"] is True
    assert len(queue.envelopes) == 1  # edenn_basic → 1 take


def test_finalize_prefers_composed_mix_over_music_candidate(tmp_path: Path) -> None:
    """S7: when a music+VO mix exists, finalize delivers the composed mix video."""

    client, agent_repo, async_repo, _, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    choose = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_cinematic"},
    )
    candidate = choose.json()["snapshot"]["state"]["candidates"][0]
    async_repo.update_job_status(
        candidate["linked_job_id"],
        status=JobStatus.COMPLETED,
        result_json={
            "audio_metadata": {"audio_url": "https://cdn.test/music.mp3"},
            "video_metadata": {"video_url": "https://cdn.test/music-only.mp4"},
        },
        finished=True,
    )
    # Inject a composed mix (music + voice-over) as if compose_mix had run.
    sess = agent_repo.get_session(sid)
    state = dict(sess.state_json)
    state["mix"] = {
        "video_url": "https://storage.test/mix.mp4?sig=fake",
        # A real compose_mix always records which take it carried; finalize
        # now refuses to promote a mix that carries a different one. It also
        # records the stems it was built from, so finalize can tell a current
        # master from one composed before a re-cut or a retaken line.
        "music_candidate_id": candidate["candidate_id"],
        "built_from": mix_stems(state, candidate_id=candidate["candidate_id"]),
        "music_volume": 0.6,
        "voiceover_volume": 1.0,
    }
    agent_repo.update_session(sid, state_json=state)

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "candidate", "target_id": candidate["candidate_id"]},
    )
    assert response.status_code == 200, response.text
    final = response.json()["snapshot"]["state"]["final_artifact"]
    # The deliverable is the composed mix, NOT the music-only candidate video.
    assert final["video_url"] == "https://storage.test/mix.mp4?sig=fake"
    assert final["deliverable"] == "compose_mix"


# --------------------------------------------------------------------------- #
# Structured /choices contract (Phase 4 co-design): mix / variation / voiceover #
# --------------------------------------------------------------------------- #


def _seed_music_only(agent_repo: _MemoryAgenticRepository, session_id: str) -> None:
    """Inject a single ready music candidate (no voice-over) for remix tests."""

    session = agent_repo.get_session(session_id)
    state = dict(session.state_json)
    state["candidates"] = [
        {
            "candidate_id": "c1",
            "proposal_id": "proposal_cinematic",
            "title": "Cinematic",
            "prompt": "Cinematic build.",
            "modelspec": "edenn_basic",
            "audio_url": "https://cdn.test/music.mp3",
            "music_volume": 0.85,
            "status": "completed",
        }
    ]
    state["selected_candidate_id"] = "c1"
    agent_repo.update_session(session_id, selected_candidate_id="c1", state_json=state)


def test_mix_choice_composes_when_voiceover_present(tmp_path: Path) -> None:
    """A structured 'mix' choice composes music+VO deterministically (no LLM, no spend)."""

    compose = _RecordingCompose()
    client, agent_repo, _, queue, source = _client_with_compose(
        tmp_path, _bootstrap_decisions(), compose
    )
    session = _create_session(client, source)
    _seed_music_and_voiceover(agent_repo, session["session_id"])

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={
            "choice_type": "mix",
            "payload": {
                "music_volume": 0.5,
                "voiceover_volume": 1.2,
                "voiceover_start_s": 2.0,
                "duck_gain_db": -12.0,
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    types = [e["event_type"] for e in body["events"]]
    assert "choice.recorded" in types and "mix.updated" in types
    assert len(queue.envelopes) == 0  # compose_mix is free
    mix = body["snapshot"]["state"]["mix"]
    assert mix["music_volume"] == 0.5 and mix["voiceover_volume"] == 1.2
    assert compose.calls and compose.calls[-1]["voiceover_start_s"] == 2.0


def test_mix_choice_remuxes_when_music_only(tmp_path: Path) -> None:
    """With no voice-over, a 'mix' choice falls to the cheap re-mux (adjust_remix)."""

    client, agent_repo, _, queue, source = _client_with_decisions_and_remix(
        tmp_path, _bootstrap_decisions()
    )
    session = _create_session(client, source)
    _seed_music_only(agent_repo, session["session_id"])

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "mix", "payload": {"candidate_id": "c1", "music_volume": 0.6}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    types = [e["event_type"] for e in body["events"]]
    assert "choice.recorded" in types and "candidate.cards" in types
    assert len(queue.envelopes) == 0  # adjust_remix spends nothing
    cand = body["snapshot"]["state"]["candidates"][0]
    assert cand["music_volume"] == 0.6 and cand["remixed_video_url"].endswith("c1.mp4?sig=fake")


def test_resolve_narration_timeline_pushes_colliding_lines_apart() -> None:
    """Planned starts that ignore spoken durations must be repaired: no overlaps,
    a minimum air gap, and the tail pulled inside the clip when possible."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import resolve_narration_timeline

    # The EXACT failure heard in review: 4 lines planned at 0.6/4.2/9.0/13.4 with
    # real durations 4.45/5.70/5.85/4.55 on a 16.17s clip — every joint collided
    # and the last line spilled past the end.
    planned = [
        {"id": "seg_01", "start_s": 0.6, "duration_s": 4.45},
        {"id": "seg_02", "start_s": 4.2, "duration_s": 5.70},
        {"id": "seg_03", "start_s": 9.0, "duration_s": 5.85},
        {"id": "seg_04", "start_s": 13.4, "duration_s": 4.55},
    ]
    resolved, fits = resolve_narration_timeline(planned, video_duration_s=16.17, min_gap_s=0.4)
    prev_end = 0.0
    for i, s in enumerate(resolved):
        if i > 0:
            assert s["start_s"] >= prev_end + 0.4 - 0.01, (i, s, prev_end)  # air between lines
        prev_end = s["start_s"] + s["duration_s"]
    # This much speech (20.55s + gaps) cannot fit a 16.17s clip — reported honestly.
    assert fits is False

    # A plan that already fits is left untouched.
    ok_plan = [
        {"id": "a", "start_s": 0.5, "duration_s": 3.0},
        {"id": "b", "start_s": 5.0, "duration_s": 3.0},
        {"id": "c", "start_s": 11.0, "duration_s": 3.5},
    ]
    same, fits2 = resolve_narration_timeline(ok_plan, video_duration_s=16.0)
    assert fits2 is True
    assert [s["start_s"] for s in same] == [0.5, 5.0, 11.0]

    # A mild spill pulls back ONLY the overflowing tail, by the least amount
    # that lands it — the line before it keeps its cue exactly.
    spill = [
        {"id": "a", "start_s": 1.0, "duration_s": 4.0},
        {"id": "b", "start_s": 12.0, "duration_s": 5.0},  # would end at 17 > 16
    ]
    packed, fits3 = resolve_narration_timeline(spill, video_duration_s=16.0)
    assert fits3 is True
    assert packed[1]["start_s"] + packed[1]["duration_s"] <= 16.0 + 0.01
    assert packed[0]["start_s"] == 1.0  # untouched: it already landed
    assert packed[1]["start_s"] == 11.0  # pulled by exactly the 1.0s overflow


def test_resolve_narration_timeline_never_drags_the_whole_read_off_its_cues() -> None:
    """A planned start is a cue against the picture. One overlong tail line must
    not pull the lines before it earlier — and nothing may restack from t=0,
    which would divorce the narration from the footage entirely."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import resolve_narration_timeline

    # Lines 1-2 sit on real moments and fit; the last line is far too long.
    plan = [
        {"id": "seg_01", "start_s": 1.0, "duration_s": 2.0},
        {"id": "seg_02", "start_s": 5.0, "duration_s": 2.0},
        {"id": "seg_03", "start_s": 12.0, "duration_s": 9.0},  # ends at 21 on a 16s clip
    ]
    resolved, fits = resolve_narration_timeline(plan, video_duration_s=16.0)
    assert fits is False  # surfaced, not silently "repaired"
    assert resolved[0]["start_s"] == 1.0  # cue held
    assert resolved[1]["start_s"] == 5.0  # cue held
    # The tail may drift back, but only within the bound — never to zero.
    assert resolved[2]["start_s"] >= 12.0 - 1.5 - 0.01

    # And the tail is never pulled back past its own bound even when that would
    # have made it fit: honesty beats a line placed nowhere near its moment.
    far = [{"id": "only", "start_s": 14.0, "duration_s": 6.0}]
    out, fits_far = resolve_narration_timeline(far, video_duration_s=16.0)
    assert fits_far is False
    assert out[0]["start_s"] >= 14.0 - 1.5 - 0.01


def test_voiceover_narration_segments_are_stored_and_enqueued(tmp_path: Path) -> None:
    """Video-informed narration: a voiceover choice with timed segments stores
    the plan (sorted, ids, joined script) and the TTS job payload carries the
    segments so the worker can render when/how each line lands."""

    client, agent_repo, async_repo, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "voiceover", "payload": {
            "voice_id": "narrator_male",
            "voice_rationale": "slow elegant footage — low intimate narrator",
            "narration_segments": [
                # 9.5s, not 11.0: on a 12.5s clip a 4-word line starting at 11.0
                # is still being spoken past the final frame, and the fit gate
                # (rightly) refuses it.
                {"text": "Everything, all at once.", "start_s": 9.5, "delivery": "final, resolute"},
                {"text": "Sometimes the loudest moments are silent.", "start_s": 1.0, "delivery": "hushed"},
            ],
        }},
    )
    assert r.status_code == 200, r.text
    vo = r.json()["snapshot"]["state"]["layers"]["voiceover"]
    segs = vo["segments"]
    assert [s["start_s"] for s in segs] == [1.0, 9.5]  # sorted by time
    assert segs[0]["id"] == "seg_01" and segs[0]["delivery"] == "hushed"
    assert vo["script"].startswith("Sometimes the loudest")  # joined in time order
    assert vo["voice_rationale"].startswith("slow elegant")
    # The queued TTS job carries the segments.
    job = async_repo.jobs[vo["linked_job_id"]]
    payload_segments = job.request_json["segments"]
    assert len(payload_segments) == 2 and payload_segments[0]["delivery"] == "hushed"


def test_voiceover_flat_script_clears_stale_segments(tmp_path: Path) -> None:
    """Editing back to a flat script must drop the old timed plan — the render
    always matches what the card shows."""

    client, agent_repo, _, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    client.post(f"/api/v2/agentic/audio/sessions/{sid}/choices",
                json={"choice_type": "voiceover", "payload": {
                    "narration_segments": [{"text": "Line one.", "start_s": 2.0}]}})
    r = client.post(f"/api/v2/agentic/audio/sessions/{sid}/choices",
                    json={"choice_type": "voiceover", "payload": {"script": "Just this, flat."}})
    assert r.status_code == 200, r.text
    vo = r.json()["snapshot"]["state"]["layers"]["voiceover"]
    assert vo["script"] == "Just this, flat."
    assert not vo.get("segments")


def test_mix_includes_sfx_layer_in_master_compose(tmp_path: Path) -> None:
    """Master mix: with a completed SFX variant, a mix choice routes to compose
    and passes the SFX bed + sfx_volume; the mix state records inclusion."""

    compose = _RecordingCompose()
    client, agent_repo, _, queue, source = _client_with_compose(
        tmp_path, _bootstrap_decisions(), compose
    )
    session = _create_session(client, source)
    sid = session["session_id"]
    # Seed a completed SFX variant + a completed voiceover (so compose runs).
    sess = agent_repo.get_session(sid)
    state = dict(sess.state_json)
    state["layers"] = {
        "voiceover": {"script": "n", "status": "completed", "audio_url": "https://cdn.test/vo.mp3"},
        "sfx": {"status": "completed", "events": [{"id": "e1", "label": "hit", "prompt": "hit", "start_s": 1.0}],
                "variants": [{"variant_id": "sfx_variant_1", "status": "completed",
                              "audio_url": "https://cdn.test/sfx.mp3"}],
                "selected_variant_id": "sfx_variant_1"},
    }
    agent_repo.update_session(sid, state_json=state)
    r = client.post(f"/api/v2/agentic/audio/sessions/{sid}/choices",
                    json={"choice_type": "mix", "payload": {"sfx_volume": 0.8}})
    assert r.status_code == 200, r.text
    mix = r.json()["snapshot"]["state"]["mix"]
    assert mix["sfx_included"] is True
    assert mix["sfx_volume"] == 0.8
    # The compose stub actually received the SFX bed.
    assert compose.calls and compose.calls[-1].get("sfx_audio_url") == "https://cdn.test/sfx.mp3"
    # And clamped like every other mix param.
    r2 = client.post(f"/api/v2/agentic/audio/sessions/{sid}/choices",
                     json={"choice_type": "mix", "payload": {"sfx_volume": 999}})
    assert r2.status_code == 200
    assert r2.json()["snapshot"]["state"]["mix"]["sfx_volume"] <= 2.0


def test_sfx_plan_generate_variants_and_select(tmp_path: Path) -> None:
    """SFX is a first-class layer: an 'sfx' choice spots the plan and renders a
    variant; a second call adds another variant; a select points at one; and a
    completed job hydrates into the variant."""

    client, agent_repo, async_repo, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]

    # Plan + generate the first variant in one choice.
    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={
            "choice_type": "sfx",
            "payload": {
                "sfx_summary": "whooshes on the cuts, impact on the close",
                "sfx_events": [
                    {"label": "Whoosh", "prompt": "airy transition whoosh", "start_s": 1.0},
                    {"label": "Impact", "prompt": "deep cinematic impact", "start_s": 5.0},
                ],
            },
        },
    )
    assert r.status_code == 200, r.text
    sfx = r.json()["snapshot"]["state"]["layers"]["sfx"]
    assert len(sfx["events"]) == 2
    assert len(sfx["variants"]) == 1
    assert sfx["variants"][0]["status"] == "queued"
    # Nothing is "chosen" while it is still rendering: nobody has heard it, and
    # a compose that ran now would mix a take with no audio in it.
    assert not sfx.get("selected_variant_id")
    assert any(e.task_type == "video_sfx" for e in queue.envelopes)
    v1_job = sfx["variants"][0]["linked_job_id"]
    assert v1_job

    # A second generate appends another variant (A/B), no re-plan.
    r2 = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "sfx", "payload": {}},
    )
    assert r2.status_code == 200, r2.text
    sfx2 = r2.json()["snapshot"]["state"]["layers"]["sfx"]
    assert len(sfx2["variants"]) == 2
    assert not sfx2.get("selected_variant_id")

    # Complete variant 1's job → a GET/refresh hydrates it, and the first take
    # that actually finished becomes the working choice.
    async_repo.update_job_status(
        v1_job,
        status="completed",
        result_json={"audio_url": "https://cdn.test/sfx1.mp3", "video_url": "https://cdn.test/sfx1.mp4"},
    )
    hydrated = client.get(f"/api/v2/agentic/audio/sessions/{sid}").json()
    assert hydrated["state"]["layers"]["sfx"]["selected_variant_id"] == "sfx_variant_1"
    # Select variant 1 (free, no new job).
    envelopes_before = len(queue.envelopes)
    r3 = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "sfx", "payload": {"select_variant_id": "sfx_variant_1"}},
    )
    assert r3.status_code == 200, r3.text
    sfx3 = r3.json()["snapshot"]["state"]["layers"]["sfx"]
    assert sfx3["selected_variant_id"] == "sfx_variant_1"
    assert len(queue.envelopes) == envelopes_before  # select spends nothing
    v1 = next(v for v in sfx3["variants"] if v["variant_id"] == "sfx_variant_1")
    assert v1["status"] == "completed"
    assert v1["audio_url"].endswith("sfx1.mp3")
    assert v1["video_url"].endswith("sfx1.mp4")


def test_sfx_generate_blocked_without_plan(tmp_path: Path) -> None:
    """generate_sfx is a spend gate: an sfx choice with no events plans nothing
    to render, so a bare generate has no plan and is rejected/does nothing."""

    client, agent_repo, async_repo, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "sfx", "payload": {}},
    )
    # ApprovalRequiredError (a ValueError) surfaces as a clean 400, no job queued.
    assert r.status_code == 400, r.text
    assert not any(e.task_type == "video_sfx" for e in queue.envelopes)


def test_sfx_treatment_card_answer_is_durable_and_frames_the_plan(tmp_path: Path) -> None:
    """The ONE treatment card: a clarify tagged topic=sfx_treatment surfaces with
    the agent's pick marked; tapping an option records state.sfx_treatment
    durably (source=card), and a later plan carries that treatment — not the
    'Direct plan edit' fallback."""

    treatment_clarify = {
        "thought": "Register + density are unknown until the user answers.",
        "intent": "ask_question",
        "assistant_message": "Before I spot anything — what should the effects feel like?",
        "action": {
            "type": "clarify",
            "clarification": {
                "question": "What should the sound effects feel like?",
                "topic": "sfx_treatment",
                "options": [
                    {"id": "editorial", "label": "Editorial accents",
                     "hint": "3 stylized hits on the cuts", "recommended": True},
                    {"id": "diegetic", "label": "Diegetic / foley"},
                    {"id": "ambience", "label": "Ambience only"},
                ],
            },
        },
    }
    after_answer = {
        "thought": "Treatment chosen; plan next.",
        "intent": "add_sfx",
        "assistant_message": "Editorial accents it is.",
        "action": {"type": "noop"},
    }
    client, agent_repo, _, _, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [treatment_clarify, after_answer]
    )
    session = _create_session(client, source)
    sid = session["session_id"]

    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/messages",
        json={"content": "add sound effects"},
    )
    assert r.status_code == 200, r.text
    pending = r.json()["snapshot"]["state"]["pending_clarification"]
    assert pending["topic"] == "sfx_treatment"
    assert pending["options"][0]["recommended"] is True
    assert pending["options"][1].get("recommended") is None

    r2 = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "clarification", "target_id": "editorial"},
    )
    assert r2.status_code == 200, r2.text
    st = r2.json()["snapshot"]["state"]
    assert st["pending_clarification"] is None
    assert st["sfx_treatment"]["label"] == "Editorial accents"
    assert st["sfx_treatment"]["source"] == "card"

    # A direct plan now runs WITHIN the stored treatment (no fallback override).
    r3 = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={
            "choice_type": "sfx",
            "payload": {
                "sfx_events": [
                    {"label": "Whoosh", "prompt": "airy whoosh", "start_s": 1.0},
                ]
            },
        },
    )
    assert r3.status_code == 200, r3.text
    sfx = r3.json()["snapshot"]["state"]["layers"]["sfx"]
    assert sfx["treatment"]["label"] == "Editorial accents"


def test_sfx_direct_plan_edit_opens_gate_and_annotates_density_cap(tmp_path: Path) -> None:
    """Events submitted through the plan editor ARE the user's treatment: the
    gate opens inline (recorded durably), and the plan carries the legible
    density budget — ~1 effect per 5s, over-budget flagged, never trimmed."""

    client, agent_repo, _, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    sess = agent_repo.get_session(sid)
    state = dict(sess.state_json)
    obs = dict(state.get("observation") or {})
    obs["duration_s"] = 16.0
    state["observation"] = obs
    agent_repo.update_session(sid, state_json=state)

    five = [
        {"label": f"Hit {i}", "prompt": "hit", "start_s": float(i * 3), "reason": "hard cut"}
        for i in range(5)
    ]
    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "sfx", "payload": {"sfx_events": five}},
    )
    assert r.status_code == 200, r.text
    sfx = r.json()["snapshot"]["state"]["layers"]["sfx"]
    assert sfx["treatment"]["label"] == "Direct plan edit"
    assert sfx["density_cap"] == 3  # 16s / 5
    assert sfx["over_budget"] is True
    assert "3" in sfx["cap_note"] and "denser" in sfx["cap_note"]
    assert len(sfx["events"]) == 5  # visible flag, never a silent trim
    assert sfx["events"][0]["reason"] == "hard cut"
    # The inline treatment is durable state, so the card is never asked later.
    assert (
        agent_repo.get_session(sid).state_json["sfx_treatment"]["label"]
        == "Direct plan edit"
    )


def test_sfx_plan_without_treatment_soft_blocks_not_500(tmp_path: Path) -> None:
    """If the model calls plan_sfx before any treatment exists, the tool returns
    instructive data (no plan recorded) instead of failing the user's turn."""

    plan_attempt = {
        "thought": "Jumping straight to spotting (wrongly).",
        "intent": "add_sfx",
        "assistant_message": "Spotting the moments.",
        "action": {
            "type": "call_tool",
            "tool_name": "plan_sfx",
            "tool_args": {
                "sfx_events": [{"label": "Whoosh", "prompt": "whoosh", "start_s": 1.0}]
            },
        },
    }
    recover = {
        "thought": "Blocked — ask the treatment card first.",
        "intent": "ask_question",
        "assistant_message": "One question before I spot anything.",
        "action": {"type": "noop"},
    }
    client, agent_repo, _, queue, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [plan_attempt, recover]
    )
    session = _create_session(client, source)
    sid = session["session_id"]
    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/messages",
        json={"content": "add sound effects"},
    )
    assert r.status_code == 200, r.text
    state = r.json()["snapshot"]["state"]
    sfx = (state.get("layers") or {}).get("sfx")
    assert not (isinstance(sfx, dict) and (sfx.get("events") or []))  # nothing planned
    assert not state.get("sfx_treatment")


def test_sfx_ambience_only_plan_is_plannable_and_generatable(tmp_path: Path) -> None:
    """The card's 'Ambience only' option must be executable: zero discrete
    events + a bed is a legitimate plan, and generate renders it."""

    client, agent_repo, _, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={
            "choice_type": "sfx",
            "payload": {"sfx_events": [], "sfx_ambience": "soft room-energy bed"},
        },
    )
    assert r.status_code == 200, r.text
    sfx = r.json()["snapshot"]["state"]["layers"]["sfx"]
    assert sfx["events"] == []
    assert sfx["ambience"] == "soft room-energy bed"
    assert sfx["cap_note"] == ""  # no discrete events — no cap chatter
    assert len(sfx["variants"]) == 1  # the bed rendered as a variant
    assert any(e.task_type == "video_sfx" for e in queue.envelopes)


def test_sfx_deleting_every_event_blocks_generate_not_spends_stale_plan(tmp_path: Path) -> None:
    """An explicitly EMPTY sfx_events list is a plan edit. With no bed either it
    is invalid — and it must never fall through to generate against the stale
    previous plan the user just deleted."""

    client, agent_repo, _, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={
            "choice_type": "sfx",
            "payload": {
                "sfx_events": [
                    {"label": "Whoosh", "prompt": "whoosh", "start_s": 1.0},
                    {"label": "Impact", "prompt": "impact", "start_s": 5.0},
                ]
            },
        },
    )
    assert r.status_code == 200, r.text
    spent_before = sum(1 for e in queue.envelopes if e.task_type == "video_sfx")

    r2 = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "sfx", "payload": {"sfx_events": []}},
    )
    assert r2.status_code == 400, r2.text  # invalid edit, clearly rejected
    spent_after = sum(1 for e in queue.envelopes if e.task_type == "video_sfx")
    assert spent_after == spent_before  # and nothing was billed for it


def test_sfx_card_answer_is_not_silently_overwritten_by_model_arg(tmp_path: Path) -> None:
    """A model-authored inline treatment can never masquerade as the user's
    card tap: replacing a stored answer records source='revised' and preserves
    what it replaced."""

    plan_with_inline = {
        "thought": "Replanning with my own treatment (a pivot).",
        "intent": "add_sfx",
        "assistant_message": "Replanning.",
        "action": {
            "type": "call_tool",
            "tool_name": "plan_sfx",
            "tool_args": {
                "sfx_events": [{"label": "Boing", "prompt": "comedic boing", "start_s": 2.0}],
                "treatment": {"label": "Comedic pops", "register": "comedic"},
            },
        },
    }
    done = {
        "thought": "Planned.",
        "intent": "add_sfx",
        "assistant_message": "Done.",
        "action": {"type": "noop"},
    }
    client, agent_repo, _, _, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [plan_with_inline, done]
    )
    session = _create_session(client, source)
    sid = session["session_id"]
    sess = agent_repo.get_session(sid)
    state = dict(sess.state_json)
    state["sfx_treatment"] = {"id": "editorial", "label": "Editorial accents", "source": "card"}
    agent_repo.update_session(sid, state_json=state)

    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/messages",
        json={"content": "make them comedic boings"},
    )
    assert r.status_code == 200, r.text
    treatment = r.json()["snapshot"]["state"]["sfx_treatment"]
    assert treatment["label"] == "Comedic pops"
    assert treatment["source"] == "revised"  # honest provenance, never 'card'/'user_message'
    assert treatment["revised_from"] == {"label": "Editorial accents", "source": "card"}


def test_sfx_treatment_garbage_answer_keeps_card_outstanding(tmp_path: Path) -> None:
    """An empty clarification answer (no option, no text) must not settle the
    treatment as garbage — nothing is stored and the card stays pending."""

    client, agent_repo, _, _, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    sess = agent_repo.get_session(sid)
    state = dict(sess.state_json)
    state["pending_clarification"] = {
        "question": "What should the sound effects feel like?",
        "topic": "sfx_treatment",
        "options": [{"id": "editorial", "label": "Editorial accents"}],
    }
    agent_repo.update_session(sid, state_json=state)

    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "clarification", "target_id": "nonexistent_option"},
    )
    assert r.status_code == 200, r.text
    st = agent_repo.get_session(sid).state_json
    assert not st.get("sfx_treatment")
    assert (st.get("pending_clarification") or {}).get("topic") == "sfx_treatment"


def test_sfx_direct_plan_clears_pending_treatment_card(tmp_path: Path) -> None:
    """Planning settles the outstanding treatment card however the gate was
    opened — a live card over a recorded plan invites a contradictory late tap."""

    client, agent_repo, _, _, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    sess = agent_repo.get_session(sid)
    state = dict(sess.state_json)
    state["pending_clarification"] = {
        "question": "What should the sound effects feel like?",
        "topic": "sfx_treatment",
        "options": [{"id": "editorial", "label": "Editorial accents"}],
    }
    agent_repo.update_session(sid, state_json=state)

    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={
            "choice_type": "sfx",
            "payload": {"sfx_events": [{"label": "Whoosh", "prompt": "whoosh", "start_s": 1.0}]},
        },
    )
    assert r.status_code == 200, r.text
    st = r.json()["snapshot"]["state"]
    assert st["sfx_treatment"]["label"] == "Direct plan edit"
    assert st["pending_clarification"] is None


def test_sfx_treatment_reask_is_skipped_once_settled(tmp_path: Path) -> None:
    """Once state.sfx_treatment is set the question is settled: a model attempt
    to re-ask the card is skipped and the turn moves on."""

    reask = {
        "thought": "Asking the treatment again (wrongly).",
        "intent": "ask_question",
        "assistant_message": "What should the effects feel like?",
        "action": {
            "type": "clarify",
            "clarification": {
                "question": "What should the sound effects feel like?",
                "topic": "sfx_treatment",
                "options": [{"id": "editorial", "label": "Editorial accents"}],
            },
        },
    }
    then_move_on = {
        "thought": "Right — it's settled; keep going.",
        "intent": "add_sfx",
        "assistant_message": "Planning within the chosen treatment.",
        "action": {"type": "noop"},
    }
    client, agent_repo, _, _, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [reask, then_move_on]
    )
    session = _create_session(client, source)
    sid = session["session_id"]
    sess = agent_repo.get_session(sid)
    state = dict(sess.state_json)
    state["sfx_treatment"] = {"id": "editorial", "label": "Editorial accents", "source": "card"}
    agent_repo.update_session(sid, state_json=state)

    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/messages",
        json={"content": "add sound effects"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    # No clarify card surfaced; the settled treatment stands; the turn moved on.
    assert not [e for e in payload["events"] if e["event_type"] == "clarify.cards"]
    assert payload["snapshot"]["state"]["pending_clarification"] is None
    assert payload["snapshot"]["state"]["sfx_treatment"]["label"] == "Editorial accents"


def test_mix_music_volume_out_of_range_is_clamped(tmp_path: Path) -> None:
    """An absurd music_volume must never reach ffmpeg unbounded (unhappy-path
    review P1: 3.4e10 hung the mux and deadlocked the session). It clamps into
    range and persists the clamped value, not the raw one."""

    client, agent_repo, _, queue, source = _client_with_decisions_and_remix(
        tmp_path, _bootstrap_decisions()
    )
    session = _create_session(client, source)
    _seed_music_only(agent_repo, session["session_id"])

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "mix", "payload": {"candidate_id": "c1", "music_volume": 3.4e10}},
    )
    assert response.status_code == 200, response.text
    cand = response.json()["snapshot"]["state"]["candidates"][0]
    assert 0.0 <= cand["music_volume"] <= 2.0  # clamped, never the raw 3.4e10
    # And a negative gain clamps to zero, not a phase-inverted 50x boost.
    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "mix", "payload": {"candidate_id": "c1", "music_volume": -50}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"]["state"]["candidates"][0]["music_volume"] == 0.0


def test_mix_music_volume_wrong_type_returns_400(tmp_path: Path) -> None:
    """A non-numeric music_volume is a clean 400, not a 500 with a raw coercion
    string (unhappy-path review P3)."""

    client, agent_repo, _, queue, source = _client_with_decisions_and_remix(
        tmp_path, _bootstrap_decisions()
    )
    session = _create_session(client, source)
    _seed_music_only(agent_repo, session["session_id"])

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "mix", "payload": {"candidate_id": "c1", "music_volume": [1, 2, 3]}},
    )
    assert response.status_code == 400, response.text


def test_variation_choice_branches_a_new_take(tmp_path: Path) -> None:
    """A 'variation' choice regenerates a fresh take branched off the candidate."""

    client, agent_repo, _, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    _seed_music_only(agent_repo, session["session_id"])

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "variation", "target_id": "c1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "candidate.cards" in [e["event_type"] for e in body["events"]]
    assert len(queue.envelopes) == 1  # one new generation job
    cands = body["snapshot"]["state"]["candidates"]
    branched = [c for c in cands if c.get("parent_candidate_id") == "c1"]
    assert branched and branched[-1]["edit_kind"] == "regenerate" and branched[-1]["version"] == 2


def test_voiceover_choice_drafts_then_generates(tmp_path: Path) -> None:
    """A 'voiceover' choice drafts the supplied script (free) then TTSes it."""

    client, agent_repo, _, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={
            "choice_type": "voiceover",
            "payload": {"script": "Welcome to the future.", "voice_id": "warm_female", "tone": "warm"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    types = [e["event_type"] for e in body["events"]]
    assert "voiceover.script" in types and "voiceover.generating" in types
    assert len(queue.envelopes) == 1  # the TTS job
    vo = body["snapshot"]["state"]["layers"]["voiceover"]
    assert vo["script"] == "Welcome to the future." and vo["status"] == "queued"


# --------------------------------------------------------------------------- #
# Authentication / authorization (gated by AGENTIC_AUDIO_REQUIRE_AUTH)         #
# --------------------------------------------------------------------------- #

_AUDIO = "/api/v2/agentic/audio"


def test_auth_disabled_by_default_needs_no_credentials(tmp_path: Path) -> None:
    """With the flag unset, the API behaves exactly as before — no auth."""

    client, _, _, _, source = _test_client(tmp_path)
    created = client.post(f"{_AUDIO}/sessions", json={"source_video_artifact_id": source.artifact_id})
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]
    assert client.get(f"{_AUDIO}/sessions/{sid}").status_code == 200


def test_auth_required_rejects_missing_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    client, _, _, _, source = _test_client(tmp_path)
    r = client.post(f"{_AUDIO}/sessions", json={"source_video_artifact_id": source.artifact_id})
    assert r.status_code == 401, r.text


def test_auth_binds_owner_and_enforces_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    # Token-is-user is a local convenience and now has to be asked for by name.
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    client, agent_repo, _, _, source = _test_client(tmp_path)
    alice = {"Authorization": "Bearer alice"}
    bob = {"Authorization": "Bearer bob"}

    created = client.post(f"{_AUDIO}/sessions", json={"source_video_artifact_id": source.artifact_id}, headers=alice)
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]
    # The session is owned by the authenticated caller, not any request body value.
    assert agent_repo.get_session(sid).creator_user_id == "alice"

    # Owner can read + act; a different principal is forbidden; no creds = 401.
    assert client.get(f"{_AUDIO}/sessions/{sid}", headers=alice).status_code == 200
    assert client.get(f"{_AUDIO}/sessions/{sid}", headers=bob).status_code == 403
    assert client.get(f"{_AUDIO}/sessions/{sid}").status_code == 401
    assert client.post(f"{_AUDIO}/sessions/{sid}/messages", json={"content": "hi"}, headers=alice).status_code == 200
    assert client.post(f"{_AUDIO}/sessions/{sid}/messages", json={"content": "hi"}, headers=bob).status_code == 403
    assert client.post(
        f"{_AUDIO}/sessions/{sid}/choices", json={"choice_type": "candidate", "target_id": "x"}, headers=bob
    ).status_code == 403


def test_auth_refuses_every_request_when_the_key_map_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth on + no key map must be CLOSED, not "anyone may be anyone".

    The old behaviour returned the presented token as the user id, so one unset
    environment variable silently turned "authentication required" into an open
    door where a caller could name whichever user they wanted to be.
    """
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.delenv("AGENTIC_AUDIO_TOKEN_IS_USER", raising=False)
    client, _, _, _, source = _test_client(tmp_path)

    for creds in ("Bearer anything", "Bearer admin", "Bearer alice"):
        r = client.post(
            f"{_AUDIO}/sessions",
            json={"source_video_artifact_id": source.artifact_id},
            headers={"Authorization": creds},
        )
        assert r.status_code == 401, f"{creds!r} authenticated: {r.text}"


def test_auth_token_is_user_mode_must_be_asked_for_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The convenience still exists — it just cannot happen by accident."""
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    client, agent_repo, _, _, source = _test_client(tmp_path)

    created = client.post(
        f"{_AUDIO}/sessions",
        json={"source_video_artifact_id": source.artifact_id},
        headers={"Authorization": "Bearer carol"},
    )
    assert created.status_code == 200, created.text
    assert agent_repo.get_session(created.json()["session_id"]).creator_user_id == "carol"


def test_auth_key_map_wins_over_token_is_user_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured key map is the authority even if the dev flag is left on."""
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "sek_alice:alice")
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    client, _, _, _, source = _test_client(tmp_path)

    assert client.post(
        f"{_AUDIO}/sessions",
        json={"source_video_artifact_id": source.artifact_id},
        headers={"Authorization": "Bearer mallory"},
    ).status_code == 401


def test_auth_api_key_map_resolves_token_to_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "sek_alice:alice,sek_bob:bob")
    client, agent_repo, _, _, source = _test_client(tmp_path)

    # Unknown token rejected; a configured token maps to its user.
    assert client.post(
        f"{_AUDIO}/sessions", json={"source_video_artifact_id": source.artifact_id},
        headers={"Authorization": "Bearer nope"},
    ).status_code == 401
    created = client.post(
        f"{_AUDIO}/sessions", json={"source_video_artifact_id": source.artifact_id},
        headers={"Authorization": "Bearer sek_alice"},
    )
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]
    assert agent_repo.get_session(sid).creator_user_id == "alice"
    # Bob's valid key still can't touch Alice's session.
    assert client.get(f"{_AUDIO}/sessions/{sid}", headers={"Authorization": "Bearer sek_bob"}).status_code == 403


# --------------------------------------------------------------------------- #
# Voice-over-only deliverable (gap #1): a narration-only session must reach an  #
# output — compose the VO over the video (no music), then finalize.            #
# --------------------------------------------------------------------------- #


def _seed_voiceover_only(agent_repo: _MemoryAgenticRepository, session_id: str) -> None:
    """A narration-only session: a completed VO layer, no music candidates."""

    session = agent_repo.get_session(session_id)
    state = dict(session.state_json)
    state["production_plan"] = {"mode": "music_first", "layers": ["voiceover"]}
    state["layers"] = {
        "voiceover": {"audio_url": "https://cdn.test/vo.wav", "status": "completed", "script": "Hello there."}
    }
    state["candidates"] = []
    agent_repo.update_session(session_id, state_json=state)


def test_compose_mix_voiceover_only_lays_narration_over_video(tmp_path: Path) -> None:
    compose = _RecordingCompose()
    decisions = _bootstrap_decisions() + [
        {
            "thought": "",
            "intent": "add_voiceover",
            "assistant_message": "Laying your narration over the video.",
            "action": {"type": "call_tool", "tool_name": "compose_mix", "tool_args": {}},
        },
        _NOOP,
    ]
    client, agent_repo, _, queue, source = _client_with_compose(tmp_path, decisions, compose)
    session = _create_session(client, source)
    _seed_voiceover_only(agent_repo, session["session_id"])

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Put my narration on the video."},
    )
    body = response.json()
    assert "mix.updated" in [e["event_type"] for e in body["events"]]
    assert len(queue.envelopes) == 0  # free, no generation
    mix = body["snapshot"]["state"]["mix"]
    assert mix["video_url"]  # a deliverable was composed from VO alone
    # compose_mix was invoked with NO music track (voice-over only).
    assert compose.calls and compose.calls[-1]["music_audio_url"] is None


def test_finalize_voiceover_only_uses_the_composed_mix(tmp_path: Path) -> None:
    decisions = _bootstrap_decisions() + [
        {
            "thought": "",
            "intent": "select_final",
            "assistant_message": "Locking in your narration video.",
            "action": {"type": "call_tool", "tool_name": "finalize", "tool_args": {}},
        },
        _NOOP,
    ]
    client, agent_repo, _, queue, source = _client_with_decisions(tmp_path, decisions)
    session = _create_session(client, source)
    sid = session["session_id"]
    # A composed VO-only mix already exists; no music candidate.
    s = agent_repo.get_session(sid)
    st = dict(s.state_json)
    st["mix"] = {"video_url": "https://storage.test/vo_mix.mp4", "status": "completed"}
    st["layers"] = {"voiceover": {"audio_url": "https://cdn.test/vo.wav", "status": "completed"}}
    st["candidates"] = []
    agent_repo.update_session(sid, state_json=st)

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/messages",
        json={"content": "Finalize it."},
    )
    body = response.json()
    assert "final.artifact" in [e["event_type"] for e in body["events"]]
    final = body["snapshot"]["state"]["final_artifact"]
    assert final["deliverable"] == "voiceover_only"
    assert final["video_url"].endswith("vo_mix.mp4")
    assert body["snapshot"]["status"] == "completed"


# --------------------------------------------------------------------------- #
# Sessions list (gap #7 — History): GET /sessions, scoped to the caller        #
# --------------------------------------------------------------------------- #


def test_list_sessions_scoped_to_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    client, _, _, _, source = _test_client(tmp_path)
    alice = {"Authorization": "Bearer alice"}
    bob = {"Authorization": "Bearer bob"}

    s_alice = client.post(
        f"{_AUDIO}/sessions", json={"source_video_artifact_id": source.artifact_id}, headers=alice
    ).json()["session_id"]
    s_bob = client.post(
        f"{_AUDIO}/sessions", json={"source_video_artifact_id": source.artifact_id}, headers=bob
    ).json()["session_id"]

    listing = client.get(f"{_AUDIO}/sessions", headers=alice)
    assert listing.status_code == 200, listing.text
    ids = {row["session_id"] for row in listing.json()["sessions"]}
    assert s_alice in ids and s_bob not in ids  # each caller sees only their own
    # No credentials -> 401 (consistent with the rest of the surface).
    assert client.get(f"{_AUDIO}/sessions").status_code == 401


def test_list_sessions_open_when_auth_disabled(tmp_path: Path) -> None:
    client, _, _, _, source = _test_client(tmp_path)
    sid = client.post(
        f"{_AUDIO}/sessions",
        json={"source_video_artifact_id": source.artifact_id, "creator_user_id": "maya"},
    ).json()["session_id"]
    rows = client.get(f"{_AUDIO}/sessions").json()["sessions"]
    assert any(r["session_id"] == sid for r in rows)
    # creator filter narrows it
    assert all(r["creator_user_id"] == "maya" for r in client.get(f"{_AUDIO}/sessions?creator=maya").json()["sessions"])


def _client_with_memory_collab(tmp_path: Path) -> tuple[TestClient, AsyncV2Artifact]:
    """A test client whose COLLAB store is hermetic too — the default
    CollabRepository is the live pg one, so collab-touching tests must not use
    plain _test_client (FK violations against the shared dev database)."""
    from EdennCode.EdennAgent.AgenticAudio.persistence.collab import InMemoryCollabRepository

    agent_repo = _MemoryAgenticRepository()
    async_repo = _MemoryAsyncRepository()
    queue = _MemoryQueue()
    source = _seed_source_video(async_repo)
    context = _context(tmp_path)
    tools = AgenticAudioTools(
        async_repository=async_repo, queue=queue,
        settings=context.settings, analyze_fn=_fake_analyze,
    )
    app = FastAPI()
    app.include_router(
        create_agentic_audio_router(
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=tools,
            llm_client=_ScriptedAgentClient(_bootstrap_decisions()),
            collab_repository=InMemoryCollabRepository(),
        )
    )
    return TestClient(app), source


def test_list_sessions_includes_shared_sessions(tmp_path: Path) -> None:
    """A participant sees sessions shared WITH them, flagged `shared` with their
    role — the collaborator's way back in after the link's tab is gone."""
    client, source = _client_with_memory_collab(tmp_path)
    sid = client.post(
        f"{_AUDIO}/sessions",
        json={"source_video_artifact_id": source.artifact_id, "creator_user_id": "maya"},
    ).json()["session_id"]
    client.post(
        f"{_AUDIO}/sessions/{sid}/collab/participants",
        json={"user_id": "ken", "role": "comment", "display_name": "Ken"},
    )
    rows = client.get(f"{_AUDIO}/sessions?creator=ken").json()["sessions"]
    shared = [r for r in rows if r["session_id"] == sid]
    assert shared and shared[0]["shared"] is True and shared[0]["shared_role"] == "comment"
    # ...and the owner's own listing does NOT double-flag it.
    own = [r for r in client.get(f"{_AUDIO}/sessions?creator=maya").json()["sessions"] if r["session_id"] == sid]
    assert own and not own[0].get("shared")


def test_share_grant_flow_under_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The signed invite loop with auth ON: only the owner mints; a stranger
    can't read the session until they redeem the grant AS THEMSELVES; the
    granted role is enforced; tampered/foreign grants are refused."""
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    client, source = _client_with_memory_collab(tmp_path)
    alice = {"Authorization": "Bearer alice"}
    bob = {"Authorization": "Bearer bob"}

    sid = client.post(
        f"{_AUDIO}/sessions", json={"source_video_artifact_id": source.artifact_id}, headers=alice
    ).json()["session_id"]

    # A stranger can't read, and can't mint.
    assert client.get(f"{_AUDIO}/sessions/{sid}", headers=bob).status_code == 403
    assert client.post(f"{_AUDIO}/sessions/{sid}/collab/links", json={"role": "comment"}, headers=bob).status_code == 403

    grant = client.post(
        f"{_AUDIO}/sessions/{sid}/collab/links", json={"role": "comment"}, headers=alice
    ).json()["grant"]

    # No credentials → 401 (identity comes from the bearer, never the link).
    assert client.post(f"{_AUDIO}/sessions/{sid}/collab/join", json={"grant": grant}).status_code == 401
    # Tampered grant → 403.
    assert client.post(
        f"{_AUDIO}/sessions/{sid}/collab/join",
        json={"grant": grant[:-4] + "beef"}, headers=bob,
    ).status_code == 403

    joined = client.post(
        f"{_AUDIO}/sessions/{sid}/collab/join",
        json={"grant": grant, "display_name": "Bob"}, headers=bob,
    )
    assert joined.status_code == 200, joined.text
    assert joined.json()["participant"]["user_id"] == "bob"
    assert joined.json()["role"] == "comment"

    # Member now: reads open up, comments carry the SERVER-resolved identity...
    assert client.get(f"{_AUDIO}/sessions/{sid}", headers=bob).status_code == 200
    thread = client.post(
        f"{_AUDIO}/sessions/{sid}/collab/threads",
        json={"author_id": "SPOOF", "author_name": "Mallory", "body": "hi", "anchor_node_id": "__source__"},
        headers=bob,
    ).json()["thread"]
    assert thread["comments"][0]["author_id"] == "bob"
    assert thread["comments"][0]["author_name"] == "Bob"
    # ...but comment-role still can't direct the session.
    assert client.post(
        f"{_AUDIO}/sessions/{sid}/messages", json={"content": "go"}, headers=bob
    ).status_code == 403

    # The grant is bound to ITS session.
    sid2 = client.post(
        f"{_AUDIO}/sessions", json={"source_video_artifact_id": source.artifact_id}, headers=alice
    ).json()["session_id"]
    assert client.post(
        f"{_AUDIO}/sessions/{sid2}/collab/join", json={"grant": grant}, headers=bob
    ).status_code == 403

    # The owner redeeming their own link keeps ownership (no self-demotion).
    own = client.post(
        f"{_AUDIO}/sessions/{sid}/collab/join", json={"grant": grant}, headers=alice
    ).json()
    assert own["role"] == "owner" and own["participant"] is None


def test_sfx_suggestions_propose_accept_reject(tmp_path: Path) -> None:
    """Ghost suggestions (hybrid proposers surfaced in the console): stylistic
    ghosts come from the analysis scene cuts (free), accept folds one into the
    committed plan, reject sticks and is never re-proposed, and none of it
    ever dispatches generate_sfx."""

    async def _analyze_with_scenes(**kwargs: Any) -> dict[str, Any]:
        obs = await _fake_analyze(**kwargs)
        obs["duration_s"] = 35.0
        obs["scenes"] = [
            {"index": 0, "start_s": 0.0, "end_s": 3.4, "label": "open"},
            {"index": 1, "start_s": 3.4, "end_s": 7.1, "label": "action"},
            {"index": 2, "start_s": 7.1, "end_s": 35.0, "label": "close"},
        ]
        return obs

    client, _, _, _, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions(), analyze_fn=_analyze_with_scenes
    )
    sid = client.post(
        f"{_AUDIO}/sessions", json={"source_video_artifact_id": source.artifact_id}
    ).json()["session_id"]
    client.post(
        f"{_AUDIO}/sessions/{sid}/choices",
        json={"choice_type": "sfx", "payload": {
            "sfx_events": [{"label": "Impact", "start_s": 12}], "plan_only": True}},
    )

    r = client.post(
        f"{_AUDIO}/sessions/{sid}/choices",
        json={"choice_type": "sfx", "payload": {"sfx_suggest": "stylistic", "style": "cinematic"}},
    )
    assert r.status_code == 200, r.text
    sfx = r.json()["snapshot"]["state"]["layers"]["sfx"]
    pending = [s for s in sfx.get("suggestions", []) if s["status"] == "pending"]
    assert len(pending) == 2, sfx.get("suggestions")  # both scene cuts, density-capped

    def act(sug_id: str, action: str) -> dict[str, Any]:
        res = client.post(
            f"{_AUDIO}/sessions/{sid}/choices",
            json={"choice_type": "sfx", "payload": {
                "suggestion_id": sug_id, "suggestion_action": action}},
        )
        assert res.status_code == 200, res.text
        return res.json()["snapshot"]["state"]["layers"]["sfx"]

    sfx = act(pending[0]["suggestion_id"], "reject")
    assert [s["status"] for s in sfx["suggestions"]].count("rejected") == 1
    assert len(sfx["events"]) == 1  # reject never touches the plan

    sfx = act(pending[1]["suggestion_id"], "accept")
    assert len(sfx["events"]) == 2  # accepted ghost is now a committed row
    assert any(e.get("reason") for e in sfx["events"])  # rationale rides along

    # Re-proposing never duplicates or resurrects: same cuts → nothing fresh.
    r2 = client.post(
        f"{_AUDIO}/sessions/{sid}/choices",
        json={"choice_type": "sfx", "payload": {"sfx_suggest": "stylistic", "style": "cinematic"}},
    )
    sfx2 = r2.json()["snapshot"]["state"]["layers"]["sfx"]
    assert len(sfx2["suggestions"]) == 2

    snap = client.get(f"{_AUDIO}/sessions/{sid}").json()
    assert not any(t["tool_name"] == "generate_sfx" for t in snap["tool_calls"])


def test_sfx_plan_only_choice_records_plan_without_generating(tmp_path: Path) -> None:
    """The plan editor's save (`plan_only`) must record the edited events and
    NEVER dispatch generate_sfx — fixing a typo in a row must not spend."""
    client, _, _, _, source = _test_client(tmp_path)
    sid = client.post(
        f"{_AUDIO}/sessions", json={"source_video_artifact_id": source.artifact_id}
    ).json()["session_id"]
    r = client.post(
        f"{_AUDIO}/sessions/{sid}/choices",
        json={
            "choice_type": "sfx",
            "payload": {
                "sfx_events": [{"label": "Impact on the logo", "start_s": 3}],
                "plan_only": True,
            },
        },
    )
    assert r.status_code == 200, r.text
    snap = r.json()["snapshot"]
    sfx = (snap["state"].get("layers") or {}).get("sfx") or {}
    assert [e["label"] for e in sfx.get("events", [])] == ["Impact on the logo"]
    assert sfx.get("variants", []) == []
    assert not any(t["tool_name"] == "generate_sfx" for t in snap["tool_calls"])


def test_console_asset_allowlist_covers_every_index_asset(tmp_path: Path) -> None:
    """Every relative <script src>/<link href> in index.html must be allow-listed
    AND fetchable from the production mount — otherwise a new frontend module
    silently 404s in prod while the devserver (which serves the whole dir) hides it.
    (Regression: js/canvas-mode.js shipped without an allow-list entry.)"""
    import re as _re

    from EdennCode.EdennAgent.AgenticAudio.api.router import FRONTEND_ASSETS, FRONTEND_DIR

    html = (FRONTEND_DIR / "index.html").read_text()
    assets = _re.findall(r'(?:src|href)="\./([^"]+)"', html)
    assert assets, "index.html should reference at least one relative asset"
    # Cache-busting queries (styles.css?v=…) are not part of the served path —
    # the allow-list holds bare paths; the fetch below still uses the full ref.
    paths = [a.split("?", 1)[0] for a in assets]
    missing = [p for p in paths if p not in FRONTEND_ASSETS]
    assert not missing, f"index.html assets missing from FRONTEND_ASSETS: {missing}"

    client, _, _, _, _ = _test_client(tmp_path)
    for asset in assets:
        r = client.get(f"{_AUDIO}/app/{asset}")
        assert r.status_code == 200, f"{asset} not served by the production mount: {r.status_code}"


def test_fused_style_prompt_grounds_generation_in_the_video() -> None:
    """Generation jobs must carry the video-derived structure, not just the
    proposal prose (regression: takes sounded unrelated to the footage because
    the model saw no tempo/mood/scene arc)."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import AgenticAudioTools as MediaToolset

    observation = {
        "duration_s": 60.0,
        "scenes": [
            {"index": 0, "start_s": 0.0, "end_s": 12.0, "label": "children amid rubble"},
            {"index": 1, "start_s": 12.0, "end_s": 30.0, "label": "explosions and panic"},
            {"index": 2, "start_s": 30.0, "end_s": 60.0, "label": "rescue and mourning"},
        ],
        "music_prompt": {
            "tempo_bpm": 118,
            "global_mood": "Somber, chaotic, urgent",
            "instruments": ["solo piano", "cello", "taiko-style percussion"],
        },
    }
    fused = MediaToolset.fuse_music_style_prompt("Fragile piano building to taiko.", observation)
    assert fused is not None
    assert "Fragile piano" in fused                      # the chosen direction stays dominant
    assert "118 BPM" in fused                            # video-derived tempo
    assert "Somber, chaotic, urgent" in fused            # footage mood
    assert "0–12s children amid rubble" in fused         # timed scene arc
    assert "Total length ≈ 60s" in fused

    # Nothing video-derived -> None (pipeline orchestrates on its own).
    assert MediaToolset.fuse_music_style_prompt("Prose only.", {"scenes": [], "music_prompt": {}}) is None
    assert MediaToolset.fuse_music_style_prompt("Prose only.", None) is None


def test_fused_style_prompt_reads_the_real_analysis_scene_shape() -> None:
    """The real pipeline emits start_timestamp/end_timestamp/visual_summary —
    the fusion must read that shape and sample the arc across the WHOLE video
    (regression: it read only start_s/end_s/label, so real jobs carried
    '0–0s scene; 0–0s scene…' and the arc stopped at the first 8 scenes)."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import AgenticAudioTools as MediaToolset

    scenes = [
        {
            "scene_index": i,
            "start_timestamp": i * 2.4,
            "end_timestamp": (i + 1) * 2.4,
            "visual_summary": f"Scene {i}: a child walks through rubble, take {i}.",
            "mood": "somber",
        }
        for i in range(25)
    ]
    observation = {
        "duration_s": 60.066667,
        "scenes": scenes,
        "music_prompt": {"tempo_bpm": 118, "global_mood": "Somber, urgent", "instruments": []},
    }
    fused = MediaToolset.fuse_music_style_prompt("Fragile piano.", observation)
    assert fused is not None
    assert "0–0s" not in fused                       # timestamps actually read
    assert "0–2s Scene 0" in fused                   # arc starts at the beginning...
    assert "58–60s Scene 24" in fused                # ...and reaches the END of the video
    assert "Total length ≈ 60s" in fused

    # Degenerate scenes (no labels, no timestamps) -> no garbage arc, but the
    # mood/tempo grounding still ships.
    fused2 = MediaToolset.fuse_music_style_prompt(
        "Fragile piano.",
        {"duration_s": 30.0, "scenes": [{"foo": 1}, {"bar": 2}], "music_prompt": {"tempo_bpm": 90}},
    )
    assert fused2 is not None
    assert "Follow the video's arc" not in fused2
    assert "90 BPM" in fused2


def test_refresh_signed_url_resigns_expired_blob_sas(tmp_path: Path) -> None:
    """Mix/remix composes download candidate audio hours after generation, when
    the stored SAS has expired (regression: compose_mix 403'd on a 5h-old
    candidate URL and the turn crashed). URLs on our account get re-signed
    from the blob path; foreign URLs pass through untouched."""
    from types import SimpleNamespace

    from EdennCode.EdennAgent.AgenticAudio.tools.media import AgenticAudioTools

    class _Storage:
        enabled = True

        def generate_sas_url(self, *, container: str, blob_name: str, **_: Any) -> str:
            return f"https://acct.blob.core.windows.net/{container}/{blob_name}?sig=FRESH"

    tools = AgenticAudioTools(
        async_repository=_MemoryAsyncRepository(),
        queue=_MemoryQueue(),
        settings=SimpleNamespace(storage_account_name="acct", workdir=str(tmp_path)),
        analyze_fn=_fake_analyze,
        storage=_Storage(),
    )
    stale = (
        "https://acct.blob.core.windows.net/voicestorage/jobs/job_x/audio/matched%20audio.wav"
        "?se=2026-07-06T22%3A51%3A20Z&sig=STALE"
    )
    fresh = tools._refresh_signed_url(stale)
    assert fresh == (
        "https://acct.blob.core.windows.net/voicestorage/jobs/job_x/audio/matched audio.wav?sig=FRESH"
    )

    # Foreign hosts (provider CDNs etc.) are not ours to sign.
    other = "https://cdn.provider.example/track.mp3?token=abc"
    assert tools._refresh_signed_url(other) == other
    # No storage -> passthrough.
    tools_nostorage = AgenticAudioTools(
        async_repository=_MemoryAsyncRepository(),
        queue=_MemoryQueue(),
        settings=SimpleNamespace(storage_account_name="acct", workdir=str(tmp_path)),
        analyze_fn=_fake_analyze,
    )
    assert tools_nostorage._refresh_signed_url(stale) == stale
    assert tools._refresh_signed_url(None) is None


async def _grounded_analyze(
    *,
    artifact: Any,
    user_prompt: str = "",
    modelspec: str = "edenn_basic",
) -> dict[str, Any]:
    """_fake_analyze plus a video-derived music_prompt + scene arc, so tests can
    assert the fusion travels into job payloads."""

    observation = await _fake_analyze(
        artifact=artifact, user_prompt=user_prompt, modelspec=modelspec
    )
    observation["music_prompt"] = {
        "tempo_bpm": 96,
        "global_mood": "wistful, hopeful",
        "instruments": ["felt piano", "strings"],
    }
    observation["scenes"] = [
        {"index": 0, "start_s": 0.0, "end_s": 6.0, "label": "sunrise over the harbor"},
        {"index": 1, "start_s": 6.0, "end_s": 12.5, "label": "boats leaving in the mist"},
    ]
    return observation


def test_edit_jobs_carry_video_grounded_style_prompt(tmp_path: Path) -> None:
    """Regenerate/extend edits must ship the same video-grounded fusion as base
    generation — branches sounded as unrelated to the footage as the pre-fix
    takes because they inherited only the parent prose."""

    edit_decision = {
        "thought": "The cut is longer; extend the track.",
        "assistant_message": "Extending the track to match the new length.",
        "action": {
            "type": "call_tool",
            "tool_name": "edit_audio",
            "tool_args": {
                "candidate_id": "candidate_proposal_cinematic_1",
                "edit_kind": "extend",
                "extend_seconds": 40,
            },
        },
    }
    client, _, async_repo, queue, source = _client_with_decisions(
        tmp_path, _bootstrap_decisions() + [edit_decision], analyze_fn=_grounded_analyze
    )
    session = _create_session(client, source)
    choose = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_cinematic"},
    )
    assert choose.status_code == 200, choose.text

    # The base generation job is grounded (fusion travels through the API path).
    base_job = async_repo.get_job(queue.envelopes[0].job_id)
    base_fused = base_job.request_json.get("music_style_prompt")
    assert base_fused, "base generation job lost the fused style prompt"
    assert "96 BPM" in base_fused and "wistful, hopeful" in base_fused

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "The cut is now 40 seconds, please make the music longer."},
    )
    assert response.status_code == 200, response.text
    assert len(queue.envelopes) == 2

    edit_job = async_repo.get_job(queue.envelopes[-1].job_id)
    assert edit_job.request_json["agentic_edit_kind"] == "extend"
    fused = edit_job.request_json.get("music_style_prompt")
    assert fused, "edit job carries no fused style prompt"
    assert "Extend the track" in fused                     # the edit ask stays dominant
    assert "96 BPM" in fused                               # video-derived tempo
    assert "wistful, hopeful" in fused                     # footage mood
    assert "0–6s sunrise over the harbor" in fused         # timed scene arc


def test_creative_edit_prompt_gets_compact_video_grounding(tmp_path: Path) -> None:
    """Melody-guided restyles keep a lightweight control prompt: mood + tempo are
    appended, but NOT the full scene arc (the parent audio is the main guide)."""

    creative_decision = {
        "thought": "User wants a restyle.",
        "assistant_message": "Restyling the track to lo-fi.",
        "action": {
            "type": "call_tool",
            "tool_name": "edit_audio",
            "tool_args": {
                "candidate_id": "candidate_proposal_studio_1",
                "edit_kind": "creative_edit",
                "prompt": "Make it a warm lo-fi version.",
            },
        },
    }
    client, _, async_repo, queue, source = _client_with_decisions(
        tmp_path,
        _studio_bootstrap_decisions() + [creative_decision],
        analyze_fn=_grounded_analyze,
    )
    session = _create_session(client, source)
    choose = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/choices",
        json={"choice_type": "proposal", "target_id": "proposal_studio"},
    )
    _complete_parent_job(async_repo, choose.json()["snapshot"]["state"]["candidates"][0])
    client.get(f"/api/v2/agentic/audio/sessions/{session['session_id']}")

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{session['session_id']}/messages",
        json={"content": "Make this a lo-fi version."},
    )
    assert response.status_code == 200, response.text

    envelope = queue.envelopes[-1]
    assert envelope.task_type == "audio_creative_edit"
    job = async_repo.get_job(envelope.job_id)
    edit_prompt = job.request_json["user_prompt"]
    assert edit_prompt.startswith("Make it a warm lo-fi version.")
    assert "footage mood: wistful, hopeful" in edit_prompt
    assert "96 BPM" in edit_prompt
    assert "sunrise" not in edit_prompt  # no scene arc — control prompt stays light


def test_a_stuck_narration_gate_stops_being_the_models_secret(tmp_path: Path) -> None:
    """Two refusals are guidance. The third is the user's business.

    Live, a draft was refused three times while the agent narrated success in
    detail — "I tightened the final line so it lands cleanly" — and nothing was
    ever saved. The user read a confident story and got an empty card. Assert on
    the thread and the queue, never on the director's wording.
    """

    overruns = {
        "thought": "",
        "intent": "add_voiceover",
        "assistant_message": "I tightened the final line so it lands cleanly.",
        "action": {
            "type": "call_tool",
            "tool_name": "propose_script",
            "tool_args": {
                "narration_segments": [
                    {"id": "seg_01", "text": "Sometimes... a moment is so beautiful, it leaves you speechless.", "start_s": 0.2},
                    {"id": "seg_02", "text": "You can see it before a single word is spoken.", "start_s": 4.2},
                    {"id": "seg_03", "text": "In the eyes. In the silence. In the disbelief.", "start_s": 8.6},
                    {"id": "seg_04", "text": "And for just a second... everyone feels it together.", "start_s": 12.2},
                ]
            },
        },
    }
    client, agent_repo, _, queue, source = _client_with_decisions(
        tmp_path,
        _bootstrap_decisions() + [overruns, _NOOP, overruns, _NOOP, overruns, _NOOP],
    )
    sid = _create_session(client, source)["session_id"]

    bodies = [
        client.post(
            f"/api/v2/agentic/audio/sessions/{sid}/messages",
            json={"content": "Add a short voice-over."},
        ).json()
        for _ in range(3)
    ]

    # Nothing was drafted and nothing was spent, through all three attempts.
    assert "voiceover" not in (bodies[-1]["snapshot"]["state"].get("layers") or {})
    assert all(env.task_type != "voiceover" for env in queue.envelopes)

    def user_told(body: dict[str, Any]) -> bool:
        return any(
            "couldn't fit a voice-over" in str(m.get("content") or "")
            for m in body["snapshot"]["messages"]
        )

    # Silent while the model still has room to fix it; audible once it doesn't.
    assert not user_told(bodies[0])
    assert not user_told(bodies[1])
    assert user_told(bodies[2])


def test_narration_gate_names_the_constraint_that_actually_binds() -> None:
    """A refusal has to be resolvable by following it.

    The deployed console refused the same Japanese draft three times running,
    each time advising "cut at least 1 character" — against its own stated
    budget of 73 characters, for a script of 22. The script was never the
    problem: the last line was cued at 14.95s on a clip whose remaining 1.2s
    could hold about five characters. Advice that cannot resolve the refusal is
    worse than no advice, because it reads as progress.
    """

    from EdennCode.EdennAgent.AgenticAudio.tools.impls import _narration_overrun

    placed_too_late = [
        {"id": "a", "text": "言葉は、もう要らなかった。", "start_s": 0.4},
        {"id": "b", "text": "涙になるほど、美しい。", "start_s": 14.95},
    ]
    overrun = _narration_overrun(placed_too_late, duration_s=16.167)
    assert overrun is not None
    assert overrun["binding"] == "placement"
    # The script fits the clip with room to spare — cutting it is not the fix.
    assert overrun["total_words"] <= overrun["word_budget"]
    assert "cut at least" not in overrun["instruction"].lower()
    # And the way out is stated as a number the writer can act on.
    assert 0 <= overrun["latest_start_s"] < 14.95
    assert f"{overrun['latest_start_s']:.1f}" in overrun["instruction"]
    assert overrun["fits_at_cue"] >= 0

    # A script that genuinely IS too long still gets the length advice.
    too_long_lines = [
        {"id": "seg_01", "text": "Sometimes... a moment is so beautiful, it leaves you speechless.", "start_s": 0.2},
        {"id": "seg_02", "text": "You can see it before a single word is spoken.", "start_s": 4.2},
        {"id": "seg_03", "text": "In the eyes. In the silence. In the disbelief.", "start_s": 8.6},
        {"id": "seg_04", "text": "And for just a second... everyone feels it together.", "start_s": 12.2},
    ]
    length = _narration_overrun(too_long_lines, duration_s=16.167)
    assert length["binding"] == "length"
    assert "cut at least" in length["instruction"].lower()


def test_propose_script_refuses_a_narration_that_cannot_fit(tmp_path: Path) -> None:
    """A script longer than its clip gets amputated mid-phrase at the mux, and
    the line it eats is the last one — the one that most needs to land. Catch it
    at plan time, before any synthesis is paid for, and say how to fix it."""
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import _narration_overrun

    # The exact plan a live session produced on a 16.17s clip: 38 words over four
    # lines, which actually rendered to 17.41s and lost its closing phrase.
    too_long = [
        {"id": "seg_01", "text": "Sometimes... a moment is so beautiful, it leaves you speechless.", "start_s": 0.2},
        {"id": "seg_02", "text": "You can see it before a single word is spoken.", "start_s": 4.2},
        {"id": "seg_03", "text": "In the eyes. In the silence. In the disbelief.", "start_s": 8.6},
        {"id": "seg_04", "text": "And for just a second... everyone feels it together.", "start_s": 12.2},
    ]
    overrun = _narration_overrun(too_long, duration_s=16.167)
    assert overrun is not None
    assert overrun["total_words"] == 38
    assert overrun["cut_at_least_words"] >= 1
    # The budget must come from the same rate as the check, or the advice reads
    # as a contradiction ("38 words against a 40-word budget", then refused).
    assert overrun["word_budget"] < overrun["total_words"]
    assert "propose_script again" in overrun["instruction"]

    # A plan that fits is left alone — including a short button line at the end.
    fits = [
        {"id": "a", "text": "In a room full of lights... the real moment is written on their faces.", "start_s": 0.8},
        {"id": "b", "text": "Every glance holds anticipation.", "start_s": 6.45},
        {"id": "c", "text": "And then... emotion takes over.", "start_s": 11.8},
    ]
    assert _narration_overrun(fits, duration_s=16.167) is None
    assert _narration_overrun([{"id": "z", "text": "Everything, all at once.", "start_s": 13.0}],
                              duration_s=16.167) is None
    # Degenerate inputs never block.
    assert _narration_overrun([], duration_s=16.0) is None
    assert _narration_overrun(fits, duration_s=0.0) is None


def test_voiceover_choice_rejects_an_unfittable_script_and_spends_nothing(tmp_path: Path) -> None:
    """Submitting an over-long plan from the editor must say what is wrong with
    THAT plan — not fall through to a paid render of the previous script and
    report the stale "draft a script first"."""
    client, agent_repo, _, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "voiceover", "payload": {
            "voice_id": "narrator_male",
            "narration_segments": [
                # Far too much speech for the 12.5s fixture clip.
                {"text": "This is a deliberately overlong opening line that simply cannot fit.", "start_s": 0.5},
                {"text": "And here is a second line that makes it very much worse indeed.", "start_s": 6.0},
                {"text": "And a third to be certain of it.", "start_s": 11.0},
            ],
        }},
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "does not fit" in detail and "Cut at least" in detail, detail
    assert "propose_script" not in detail.split("Cut at least")[0][:60]

    vo = (agent_repo.get_session(sid).state_json.get("layers") or {}).get("voiceover") or {}
    assert not vo.get("segments"), "an unfittable plan must not be stored"
    assert queue.envelopes == [], "and must not reach synthesis"


def test_user_can_take_a_moment_back_and_the_choice_sticks(tmp_path: Path) -> None:
    """Ownership governs every layer, so it has to be reachable directly — not
    only through whatever the agent infers from a sentence."""
    client, agent_repo, _, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]

    state = agent_repo.get_session(sid).state_json
    sheet = state.get("spotting_sheet") or {}
    assert sheet.get("moments"), "every session gets a sheet, with no card to answer"
    target = sheet["moments"][0]
    was = target["owner"]

    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "spotting", "target_id": target["id"],
              "payload": {"owner": "silence", "reason": "let the room land"}},
    )
    assert r.status_code == 200, r.text

    moment = (agent_repo.get_session(sid).state_json["spotting_sheet"]["moments"])[0]
    assert moment["owner"] == "silence"
    assert moment["owner_source"] == "user"       # outranks anything proposed
    assert moment["reason"] == "let the room land"
    assert moment["revised_from"]["owner"] == was  # what it was is recoverable
    assert queue.envelopes == [], "reassigning an owner must never spend"


def test_a_bad_owner_is_refused_rather_than_silently_ignored(tmp_path: Path) -> None:
    client, agent_repo, _, _, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    state = agent_repo.get_session(sid).state_json
    state["spotting_sheet"] = {"moments": [{
        "id": "moment_01", "t": 8.0, "window": [8.0, 12.5], "what": "x",
        "source": "cut", "owner": "sfx", "owner_source": "proposed", "rank": 1,
    }], "reliable": True}
    agent_repo.update_session(sid, state_json=state)

    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "spotting", "target_id": "moment_01",
              "payload": {"owner": "trumpet"}},
    )
    assert r.status_code == 400, r.text
    assert "trumpet" in r.json()["detail"]
    still = agent_repo.get_session(sid).state_json["spotting_sheet"]["moments"][0]
    assert still["owner"] == "sfx", "a refused change must not half-apply"


def test_resolver_snaps_lines_clear_of_the_cuts() -> None:
    """The writer plans before the takes exist, so it is guessing at durations
    and cannot reliably keep a line inside one shot. Once the real durations are
    known, the smallest legal nudge should clear the cut."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import resolve_narration_timeline

    CUTS = [5.867, 8.9, 12.2, 14.9]
    # The plan a live session actually produced: seg_02 ran across 8.9 and
    # seg_03 across 14.9.
    plan = [
        {"id": "seg_01", "start_s": 0.6, "duration_s": 1.94},
        {"id": "seg_02", "start_s": 6.4, "duration_s": 2.76},
        {"id": "seg_03", "start_s": 13.1, "duration_s": 2.15},
    ]
    out, fits = resolve_narration_timeline(
        [dict(s) for s in plan], video_duration_s=16.167, cuts=CUTS)
    assert fits is True
    assert not [s for s in out if s.get("crosses_cut")], out
    for s in out:
        end = s["start_s"] + s["duration_s"]
        assert not [c for c in CUTS if s["start_s"] < c < end - 0.01], s

    # Order and air between lines survive the snapping.
    prev_end = 0.0
    for i, s in enumerate(out):
        if i:
            assert s["start_s"] >= prev_end + 0.4 - 0.01, out
        prev_end = s["start_s"] + s["duration_s"]


def test_resolver_without_cuts_behaves_exactly_as_before() -> None:
    """Cut-snapping is additive: a caller with no trustworthy cut list (the
    fallback detector invents them) must get the old placement untouched."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import resolve_narration_timeline

    plan = [
        {"id": "a", "start_s": 0.5, "duration_s": 3.0},
        {"id": "b", "start_s": 5.0, "duration_s": 3.0},
    ]
    out, fits = resolve_narration_timeline([dict(s) for s in plan], video_duration_s=16.0)
    assert fits is True
    assert [s["start_s"] for s in out] == [0.5, 5.0]
    assert not any("crosses_cut" in s for s in out)


def test_resolver_reports_a_straddle_it_could_not_legally_fix() -> None:
    """Collisions and overflow outrank shot boundaries — a line pushed off its
    neighbour can land back across a cut, and that is the right trade. What must
    not happen is the caller believing it was fixed."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import resolve_narration_timeline

    # One long line on a clip whose cuts leave no shot big enough to hold it.
    out, _ = resolve_narration_timeline(
        [{"id": "only", "start_s": 1.0, "duration_s": 6.0}],
        video_duration_s=10.0, cuts=[2.0, 4.0, 6.0, 8.0],
    )
    assert out[0].get("crosses_cut"), "an unfixable straddle must be surfaced"


def test_alignment_report_says_what_the_listener_actually_got() -> None:
    """Nothing in this pipeline ever listened back: a line that drifted off its
    cue, ran across a shot change, or came back at half its neighbour's pace was
    invisible until somebody played the file."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import narration_alignment

    obs = {"duration_s": 16.167, "cuts": [5.867, 8.9, 12.2, 14.9],
           "cut_source": "pyscenedetect"}
    planned = [
        {"id": "seg_01", "start_s": 0.5},
        {"id": "seg_02", "start_s": 6.3},
        {"id": "seg_03", "start_s": 13.1},
    ]
    rendered = [
        {"id": "seg_01", "text": "Some moments change a room.", "start_s": 0.5, "duration_s": 1.58},
        {"id": "seg_02", "text": "You can see it before anyone speaks.", "start_s": 6.3, "duration_s": 2.59},
        # Pushed 1.6s off its cue and now running past the last frame.
        {"id": "seg_03", "text": "And feel it long after.", "start_s": 14.7, "duration_s": 2.2},
    ]
    rep = narration_alignment(rendered, planned=planned, observation=obs)

    assert rep["clean"] is False
    assert rep["overruns"] == 1
    assert rep["straddles"] == 1                      # seg_03 crosses 14.9
    by_id = {line["id"]: line for line in rep["lines"]}
    assert by_id["seg_01"]["drift_from_cue_s"] == 0.0
    assert by_id["seg_03"]["drift_from_cue_s"] == 1.6
    assert by_id["seg_03"]["crosses_cut"] == 14.9
    assert by_id["seg_03"]["overruns_video"] is True
    assert any("past the end" in n for n in rep["notes"])
    assert any("moved more than" in n for n in rep["notes"])
    # Faults only: pace and coverage are craft, and belong in observations —
    # reporting them as defects would push the agent to flatten a deliberate
    # arc, or to add lines nobody wants.
    assert not any("pace" in n or "%" in n for n in rep["notes"])
    assert any("pace ranges" in o for o in rep["observations"])
    assert 0.0 < rep["coverage"] < 1.0


def test_alignment_report_is_clean_when_the_read_landed() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.media import narration_alignment

    obs = {"duration_s": 16.167, "cuts": [5.867, 8.9, 12.2, 14.9],
           "cut_source": "pyscenedetect"}
    # The real 0/3 result from a live session.
    rendered = [
        {"id": "seg_01", "text": "Some moments are watched.", "start_s": 1.0, "duration_s": 1.83},
        {"id": "seg_02", "text": "The rare ones are felt.", "start_s": 6.4, "duration_s": 2.06},
        {"id": "seg_03", "text": "And everyone in the room changes with them.", "start_s": 12.25, "duration_s": 2.65},
    ]
    rep = narration_alignment(rendered, planned=rendered, observation=obs)
    assert rep["clean"] is True and rep["notes"] == []
    assert rep["straddles"] == 0 and rep["overruns"] == 0
    # A varied pace across differently-directed lines is an ARC. It must not
    # stop the report reading clean — the earlier failure was flat, undirected
    # delivery, so the fix was more variation, not less.
    assert rep["observations"], "craft context should still be reported"


def test_alignment_ignores_cuts_it_should_not_trust() -> None:
    """The fallback detector invents cuts on fast motion; reporting straddles
    against invented cuts would send the agent chasing ghosts."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import narration_alignment

    rendered = [{"id": "a", "text": "one two three", "start_s": 1.0, "duration_s": 4.0}]
    obs = {"duration_s": 16.0, "cuts": [2.0, 3.0], "cut_source": "ffprobe_fallback"}
    assert narration_alignment(rendered, observation=obs)["straddles"] == 0
    assert narration_alignment(rendered, observation={})["clean"] is True


def test_requested_language_actually_reaches_synthesis(tmp_path: Path) -> None:
    """Language was captured on the job and echoed back in the result but never
    reached the synthesiser, so a Japanese request produced an English read with
    the request visible in the UI the whole time."""
    client, agent_repo, async_repo, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]

    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "voiceover", "payload": {
            "voice_id": "calm_male",
            "language": "Japanese",
            "tone": "hushed and reverent",
            "narration_segments": [
                {"text": "A quiet moment.", "start_s": 1.0, "delivery": "hushed"},
            ],
        }},
    )
    assert r.status_code == 200, r.text

    vo = (agent_repo.get_session(sid).state_json["layers"])["voiceover"]
    payload = async_repo.jobs[vo["linked_job_id"]].request_json
    # The synthesiser only ever sees voice / instructions / speed, so the
    # language has to be carried in the instructions to have any effect at all.
    assert "Japanese" in payload["tts_instructions"]
    assert "hushed and reverent" in payload["tts_instructions"]
    assert payload["language"] == "Japanese"
    assert payload["tts_voice"] == "onyx"   # the chosen preset, not the default


def test_no_language_leaves_the_instructions_alone(tmp_path: Path) -> None:
    client, agent_repo, async_repo, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "voiceover", "payload": {
            "voice_id": "warm_female",
            "narration_segments": [{"text": "A quiet moment.", "start_s": 1.0}],
        }},
    )
    vo = (agent_repo.get_session(sid).state_json["layers"])["voiceover"]
    payload = async_repo.jobs[vo["linked_job_id"]].request_json
    assert "Speak entirely in" not in payload["tts_instructions"]


def test_fit_checks_count_the_script_that_is_actually_spoken() -> None:
    """Whitespace word counts are not merely inaccurate in Japanese, Chinese,
    Korean and Thai — they are meaningless, because a whole sentence is one
    "word". A live Japanese read reported 0.3 words/sec and sailed through a
    budget check that thought the entire script took 1.4 seconds, then overran
    the clip and crossed two shot changes."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import (
        speech_units, estimated_speech_seconds,
    )
    from EdennCode.EdennAgent.AgenticAudio.tools.impls import _narration_overrun

    line = "言葉は、もう要らなかった。"
    count, unit = speech_units(line)
    assert unit == "characters" and count == 13
    assert len(line.split()) == 1, "the naive count that caused the bug"
    # It really took 2.43s; the estimate must be in that neighbourhood, not 0.5s.
    assert 2.0 < estimated_speech_seconds(line) < 3.5

    # Latin script is untouched.
    assert speech_units("Some moments change a room.") == (5, "words")
    assert 2.0 < estimated_speech_seconds("Some moments change a room.") < 2.8

    # The exact script that overran is now refused, in its own units.
    ja = [
        {"id": "seg_01", "text": "言葉は、もう要らなかった。", "start_s": 0.4},
        {"id": "seg_02", "text": "ただ、誰かの想いが…静かに届いていた。", "start_s": 3.0},
        {"id": "seg_03", "text": "涙になるほど、美しい瞬間がある。", "start_s": 14.95},
    ]
    overrun = _narration_overrun(ja, duration_s=16.167)
    assert overrun is not None
    assert "characters" in overrun["instruction"]
    assert overrun["lines"][0]["unit"] == "characters"

    # A short Japanese read that genuinely fits is still allowed.
    assert _narration_overrun(
        [{"id": "a", "text": "言葉は要らない。", "start_s": 2.0}], duration_s=16.167) is None


def test_alignment_reports_pace_in_the_right_unit() -> None:
    from EdennCode.EdennAgent.AgenticAudio.tools.media import narration_alignment

    ja = [
        {"id": "seg_01", "text": "言葉は、もう要らなかった。", "start_s": 0.4, "duration_s": 2.43},
        {"id": "seg_02", "text": "涙になるほど、美しい瞬間がある。", "start_s": 4.0, "duration_s": 3.10},
    ]
    rep = narration_alignment(ja, planned=ja, observation={"duration_s": 16.167})
    assert all(line["unit"] == "characters" for line in rep["lines"])
    assert any("characters/sec" in o for o in rep["observations"])
    # ~5 characters a second, not the nonsensical 0.3 "words" a second.
    assert 3.0 < rep["lines"][0]["per_second"] < 8.0


def test_production_worker_renders_the_timed_plan_not_a_flat_read(tmp_path: Path) -> None:
    """The production path used to flat-synthesize the concatenated script and
    return no placements, so every start time, delivery direction and deliberate
    silence the director chose was discarded exactly where it mattered."""
    import asyncio
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")
    from EdennCode.EdennAgent.AgenticAudio.tools.narration_render import (
        render_segmented_narration,
    )

    calls: list[dict] = []

    async def fake_synthesize(*, script, voice, instructions, speed, out_path):
        calls.append({"script": script, "voice": voice,
                      "instructions": instructions, "speed": speed})
        # 1s of tone per call, so durations are real and measurable.
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=1:sample_rate=44100", str(out_path)],
            check=True, capture_output=True)

    segments = [
        {"id": "seg_01", "text": "First line.", "start_s": 1.0, "delivery": "hushed"},
        {"id": "seg_02", "text": "Second line.", "start_s": 6.0, "delivery": "lifting"},
    ]
    realized = asyncio.run(render_segmented_narration(
        segments=segments, synthesize=fake_synthesize, voice="onyx",
        base_instructions="Speak calmly.", default_speed=1.0,
        workdir=tmp_path / "lines", out_path=tmp_path / "vo.wav",
        video_duration_s=16.0, cuts=[5.867, 8.9], log=lambda m: None,
    ))

    # Every line was synthesized on its own, carrying its OWN direction.
    assert len(calls) == 2
    assert "hushed" in calls[0]["instructions"]
    assert "lifting" in calls[1]["instructions"]
    assert all(c["voice"] == "onyx" for c in calls)

    # And the realized placements come back, which is what lets the mix duck per
    # line and the alignment report have anything to measure.
    assert realized is not None and len(realized) == 2
    assert all("duration_s" in s and s["duration_s"] > 0 for s in realized)
    assert realized[0]["start_s"] == 1.0
    assert not any("_path" in s for s in realized), "internal paths must not leak"
    assert (tmp_path / "vo.wav").exists()


def test_the_enqueued_job_carries_the_picture_the_plan_is_timed_against(tmp_path: Path) -> None:
    """A worker given segments but no duration or cuts can place lines relative
    to each other, but not relative to the video."""
    client, agent_repo, async_repo, _, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]

    state = agent_repo.get_session(sid).state_json
    state["observation"] = {**(state.get("observation") or {}), "duration_s": 12.5,
                            "cuts": [3.0, 7.5], "cut_source": "pyscenedetect"}
    agent_repo.update_session(sid, state_json=state)

    client.post(f"/api/v2/agentic/audio/sessions/{sid}/choices",
                json={"choice_type": "voiceover", "payload": {
                    "voice_id": "calm_male",
                    "narration_segments": [{"text": "A quiet moment.", "start_s": 1.0}]}})

    vo = (agent_repo.get_session(sid).state_json["layers"])["voiceover"]
    payload = async_repo.jobs[vo["linked_job_id"]].request_json
    assert payload["video_duration_s"] == 12.5
    assert payload["cuts"] == [3.0, 7.5]
    assert payload["segments"], "the timed plan must reach the worker"


def test_untrustworthy_cuts_are_not_sent_to_the_worker(tmp_path: Path) -> None:
    """The fallback detector invents cuts on fast motion; an invented cut would
    become a hard placement constraint in the render."""
    client, agent_repo, async_repo, _, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    state = agent_repo.get_session(sid).state_json
    state["observation"] = {**(state.get("observation") or {}), "duration_s": 12.5,
                            "cuts": [3.0, 7.5], "cut_source": "ffprobe_fallback"}
    agent_repo.update_session(sid, state_json=state)

    client.post(f"/api/v2/agentic/audio/sessions/{sid}/choices",
                json={"choice_type": "voiceover", "payload": {
                    "voice_id": "calm_male",
                    "narration_segments": [{"text": "A quiet moment.", "start_s": 1.0}]}})
    vo = (agent_repo.get_session(sid).state_json["layers"])["voiceover"]
    assert async_repo.jobs[vo["linked_job_id"]].request_json["cuts"] == []


def test_a_line_written_over_the_footages_voice_is_refused(tmp_path: Path) -> None:
    """The plainest mistake the system can make, and the one it was structurally
    incapable of noticing until the source track was scanned at all."""
    client, agent_repo, async_repo, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]

    state = agent_repo.get_session(sid).state_json
    state["observation"] = {**(state.get("observation") or {}), "duration_s": 12.5,
                            "source_audio": "present",
                            "speech_windows": [[2.0, 6.0]]}
    agent_repo.update_session(sid, state_json=state)

    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "voiceover", "payload": {
            "voice_id": "calm_male",
            "narration_segments": [
                {"text": "Talking straight over them.", "start_s": 3.0}]}},
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "talk over sound the footage is already making" in detail
    assert "quiet during" in detail, "a refusal must say where it CAN go"

    vo = (agent_repo.get_session(sid).state_json.get("layers") or {}).get("voiceover") or {}
    assert not vo.get("segments")
    assert queue.envelopes == [], "and it must never reach synthesis"


def test_a_line_in_the_quiet_stretch_is_allowed(tmp_path: Path) -> None:
    client, agent_repo, async_repo, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    state = agent_repo.get_session(sid).state_json
    state["observation"] = {**(state.get("observation") or {}), "duration_s": 12.5,
                            "source_audio": "present",
                            "speech_windows": [[2.0, 6.0]]}
    agent_repo.update_session(sid, state_json=state)

    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "voiceover", "payload": {
            "voice_id": "calm_male",
            "narration_segments": [{"text": "In the quiet part.", "start_s": 7.0}]}},
    )
    assert r.status_code == 200, r.text
    vo = (agent_repo.get_session(sid).state_json["layers"])["voiceover"]
    assert vo["segments"], "a line clear of the footage's voice must pass"


def test_stacked_gates_report_every_problem_at_once(tmp_path: Path) -> None:
    """Rejecting one fault at a time costs the writer a step per fault. A live
    session burned its entire per-turn budget on four sequential refusals and
    ended with no script at all, told about each problem only after fixing the
    previous one. Gates that stack have to report together."""
    client, agent_repo, async_repo, queue, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    state = agent_repo.get_session(sid).state_json
    state["observation"] = {**(state.get("observation") or {}), "duration_s": 12.5,
                            "source_audio": "present", "speech_windows": [[2.0, 6.0]]}
    agent_repo.update_session(sid, state_json=state)

    r = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "voiceover", "payload": {
            "voice_id": "calm_male",
            "narration_segments": [
                # Over the footage's voice AND far too long for the clip.
                {"text": "A line written straight over the person talking on camera.", "start_s": 3.0},
                {"text": "And another long one that cannot possibly fit inside this clip.", "start_s": 8.0},
                {"text": "And a third to be quite sure it overruns the end.", "start_s": 11.0},
            ]}},
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "talk over sound the footage is already making" in detail
    assert "does not fit the clip" in detail, "both faults must arrive together"
    assert queue.envelopes == []


def test_snapping_to_a_cut_never_moves_a_line_onto_the_footages_voice() -> None:
    """A constraint enforced at one stage and ignored at the next is not
    enforced. A live run passed the plan-time talk-over gate, and then the
    cut-snapper sild the line BACKWARDS onto someone speaking on camera to clear
    a cut — 2.31-5.67s over a voice occupying 0.0-3.5s."""
    from EdennCode.EdennAgent.AgenticAudio.tools.media import resolve_narration_timeline

    voice = [[0.0, 3.5], [7.5, 10.5]]
    plan = [{"id": "seg_01", "text": "x", "start_s": 3.7, "duration_s": 3.36}]

    # Without the windows, snapping trades a cut fault for a talk-over fault.
    naive, _ = resolve_narration_timeline(
        [dict(s) for s in plan], video_duration_s=16.167, cuts=[5.867, 8.9, 12.2])
    n_start = naive[0]["start_s"]
    assert n_start < 3.5, "this is the regression being pinned"

    guarded, _ = resolve_narration_timeline(
        [dict(s) for s in plan], video_duration_s=16.167, cuts=[5.867, 8.9, 12.2],
        avoid_windows=voice)
    start = guarded[0]["start_s"]
    end = start + 3.36
    for lo, hi in voice:
        assert min(end, hi) - max(start, lo) <= 0.25, (
            f"placed {start}-{end}, over the footage's voice at {lo}-{hi}")


def test_the_enqueued_job_carries_the_windows_placement_must_avoid(tmp_path: Path) -> None:
    client, agent_repo, async_repo, _, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]
    state = agent_repo.get_session(sid).state_json
    state["observation"] = {**(state.get("observation") or {}), "duration_s": 12.5,
                            "source_audio": "present", "speech_windows": [[0.0, 2.0]]}
    agent_repo.update_session(sid, state_json=state)

    client.post(f"/api/v2/agentic/audio/sessions/{sid}/choices",
                json={"choice_type": "voiceover", "payload": {
                    "voice_id": "calm_male",
                    "narration_segments": [{"text": "In the clear part.", "start_s": 4.0}]}})
    vo = (agent_repo.get_session(sid).state_json["layers"])["voiceover"]
    assert async_repo.jobs[vo["linked_job_id"]].request_json["speech_windows"] == [[0.0, 2.0]]


# ---------------------------------------------------------------------------#
# intent gate: the layer picker's selection is the plan                       #
# ---------------------------------------------------------------------------#


def _gate_client(tmp_path: Path) -> tuple[TestClient, AsyncV2Artifact]:
    """A client with the deterministic intent gate on (the production mount)."""
    agent_repo = _MemoryAgenticRepository()
    async_repo = _MemoryAsyncRepository()
    queue = _MemoryQueue()
    source = _seed_source_video(async_repo)
    context = _context(tmp_path)
    tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=context.settings,
        analyze_fn=_fake_analyze,
    )
    app = FastAPI()
    app.include_router(
        create_agentic_audio_router(
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=tools,
            llm_client=_ScriptedAgentClient([]),
            require_intent_gate=True,
        )
    )
    return TestClient(app), source


def _answer_gate(client: TestClient, session_id: str, target_id: str, layers=None) -> dict:
    body: dict[str, Any] = {"choice_type": "clarification", "target_id": target_id}
    if layers is not None:
        body["payload"] = {"layers": list(layers)}
    r = client.post(f"/api/v2/agentic/audio/sessions/{session_id}/choices", json=body)
    assert r.status_code == 200, r.text
    return r.json()["snapshot"]["state"]["production_plan"]


def _open_with(client: TestClient, source: AsyncV2Artifact, message: str) -> dict[str, Any]:
    sid = client.post(
        "/api/v2/agentic/audio/sessions",
        json={
            "source_video_artifact_id": source.artifact_id,
            "creator_user_id": "creator_agentic_test",
            "initial_message": message,
        },
    ).json()["session_id"]
    return client.get(f"/api/v2/agentic/audio/sessions/{sid}").json()["state"]


def test_the_gate_does_not_ask_what_the_user_already_said(tmp_path: Path) -> None:
    """"What are you adding today?" is the right question — of someone who has
    not said. Asked of someone who opened with "add background music to this
    clip", it is the product not listening."""

    client, source = _gate_client(tmp_path)
    state = _open_with(client, source, "Add background music to this clip.")

    assert not state.get("pending_clarification")
    plan = state["production_plan"]
    assert plan["layers"] == ["music"]
    # Their words ARE the source, so a later model plan cannot drop the layer.
    assert plan["source"] == "user"


def test_the_gate_still_asks_when_the_message_is_not_an_answer(tmp_path: Path) -> None:
    """Conservative on purpose: two layers named, or none, and it still asks.

    The failure mode of guessing wrong here is a modality nobody chose, which is
    the same class of defect as a layer picker whose selection was discarded.
    """

    client, source = _gate_client(tmp_path)
    for message in (
        "Make it cinematic and polished.",          # names no layer
        "Add music and a voice-over please.",       # names two
    ):
        state = _open_with(client, source, message)
        assert state["pending_clarification"]["gate"] == "intent"
        assert not state.get("production_plan")


def test_intent_gate_plans_exactly_the_layers_the_user_picked(tmp_path: Path) -> None:
    """The picker offers any subset; the plan must be that subset.

    There are only four intent ids, so several subsets collapse onto an id whose
    canned layer list is not what was ticked. The client sends the real selection
    alongside the id, and the plan has to follow the selection, not the id.
    """
    # music + voiceover maps to "full_audio", whose canned list ALSO has sfx.
    client, source = _gate_client(tmp_path)
    session = _create_session(client, source)
    plan = _answer_gate(client, session["session_id"], "full_audio", ["music", "voiceover"])
    assert plan["layers"] == ["music", "voiceover"], "sfx was silently re-added"
    assert plan["mode"] == "full_e2e"


def test_intent_gate_keeps_sound_effects_the_user_asked_for(tmp_path: Path) -> None:
    """music + sfx collapses onto "music_only", which would silently drop sfx."""
    client, source = _gate_client(tmp_path)
    session = _create_session(client, source)
    plan = _answer_gate(client, session["session_id"], "music_only", ["music", "sfx"])
    assert plan["layers"] == ["music", "sfx"], "the sound-effects layer was dropped"


def test_intent_gate_without_layers_falls_back_to_the_id(tmp_path: Path) -> None:
    """A caller that sends no layers keeps the historical id -> plan mapping."""
    client, source = _gate_client(tmp_path)
    session = _create_session(client, source)
    plan = _answer_gate(client, session["session_id"], "full_audio")
    assert plan["layers"] == ["music", "voiceover", "sfx"]
    assert plan["mode"] == "full_e2e"


def test_intent_gate_ignores_layers_it_does_not_recognise(tmp_path: Path) -> None:
    """Junk in the payload must not become a planned layer."""
    client, source = _gate_client(tmp_path)
    session = _create_session(client, source)
    plan = _answer_gate(
        client, session["session_id"], "music_only", ["music", "foley", "colour_grade"]
    )
    assert plan["layers"] == ["music"]


def test_intent_gate_does_not_force_music_when_music_is_unticked(tmp_path: Path) -> None:
    """A voiceover+sfx session must not have a music layer forced onto it."""
    client, source = _gate_client(tmp_path)
    session = _create_session(client, source)
    plan = _answer_gate(
        client, session["session_id"], "voiceover_only", ["voiceover", "sfx"]
    )
    assert plan["layers"] == ["voiceover", "sfx"]
    assert "music" not in plan["layers"]


def _gate_client_with_decisions(
    tmp_path: Path, decisions: list[dict[str, Any]]
) -> tuple[TestClient, AsyncV2Artifact]:
    """Gate on, and a scripted model that acts after the gate is answered."""
    agent_repo = _MemoryAgenticRepository()
    async_repo = _MemoryAsyncRepository()
    queue = _MemoryQueue()
    source = _seed_source_video(async_repo)
    context = _context(tmp_path)
    tools = AgenticAudioTools(
        async_repository=async_repo,
        queue=queue,
        settings=context.settings,
        analyze_fn=_fake_analyze,
    )
    app = FastAPI()
    app.include_router(
        create_agentic_audio_router(
            context,  # type: ignore[arg-type]
            repository=agent_repo,
            async_repository=async_repo,
            queue=queue,
            tools=tools,
            llm_client=_ScriptedAgentClient(decisions),
            collab_repository=InMemoryCollabRepository(),
            require_intent_gate=True,
        )
    )
    return TestClient(app), source


def test_model_cannot_drop_a_layer_the_user_chose(tmp_path: Path) -> None:
    """The gate records the user's pick; a later model plan must not delete it.

    The reasoning loop routinely re-asserts a production plan mid-turn. Without a
    guard, that call overwrites the gate answer — so ticking "music + sound
    effects" produced a music-only session seconds later.
    """
    model_narrows_the_plan = {
        "thought": "Music first.",
        "intent": "plan_audio",
        "assistant_message": "Starting with the music.",
        "action": {
            "type": "call_tool",
            "tool_name": "set_production_plan",
            "tool_args": {"mode": "music_first", "layers": ["music"]},
        },
    }
    client, source = _gate_client_with_decisions(tmp_path, [model_narrows_the_plan])
    session = _create_session(client, source)
    sid = session["session_id"]

    plan = _answer_gate(client, sid, "music_only", ["music", "sfx"])
    assert plan["layers"] == ["music", "sfx"]
    assert plan.get("source") == "user"

    snap = client.get(f"/api/v2/agentic/audio/sessions/{sid}").json()
    after = snap["state"]["production_plan"]
    assert "sfx" in after["layers"], f"the model dropped the user's layer: {after}"
    assert after.get("source") == "user", "the plan stopped being the user's"


def test_model_may_still_add_a_layer_to_the_users_plan(tmp_path: Path) -> None:
    """Protecting the pick must not freeze the session: growth is still allowed."""
    model_adds_voiceover = {
        "thought": "They asked for narration too.",
        "intent": "plan_audio",
        "assistant_message": "Adding a voice-over.",
        "action": {
            "type": "call_tool",
            "tool_name": "set_production_plan",
            "tool_args": {"mode": "full_e2e", "layers": ["music", "voiceover"]},
        },
    }
    client, source = _gate_client_with_decisions(tmp_path, [model_adds_voiceover])
    session = _create_session(client, source)
    sid = session["session_id"]

    _answer_gate(client, sid, "music_only", ["music"])
    snap = client.get(f"/api/v2/agentic/audio/sessions/{sid}").json()
    layers = snap["state"]["production_plan"]["layers"]
    assert "music" in layers and "voiceover" in layers, layers


def test_re_reading_one_line_keeps_the_lines_that_were_not_touched(
    tmp_path: Path,
) -> None:
    """The card offers a re-record per line and sends `segment_id` with the
    click. The choice branch forwarded a hand-written list of four argument
    names, and that name was not among them — so every per-line re-record
    silently re-recorded the WHOLE script: the user paid for nine lines to fix
    one, waited for all of them, and got a subtly different performance of the
    eight they were happy with."""
    client, agent_repo, async_repo, _, source = _test_client(tmp_path)
    session = _create_session(client, source)
    sid = session["session_id"]

    client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "voiceover", "payload": {
            "voice_id": "calm_male",
            "narration_segments": [
                {"text": "A quiet moment.", "start_s": 1.0},
                {"text": "Then it opens up.", "start_s": 5.0},
            ],
        }},
    )

    # The render comes back: each line now has audio of its own on disk.
    state = dict(agent_repo.get_session(sid).state_json)
    layers = dict(state.get("layers") or {})
    voiceover = dict(layers.get("voiceover") or {})
    voiceover["segments"] = [
        {**seg, "audio_path": f"/tmp/{seg['id']}.mp3", "rendered_text": seg["text"]}
        for seg in voiceover["segments"]
    ]
    voiceover["status"] = "completed"
    layers["voiceover"] = voiceover
    state["layers"] = layers
    agent_repo.update_session(sid, state_json=state)
    kept, retaken = (seg["id"] for seg in voiceover["segments"])

    response = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/choices",
        json={"choice_type": "voiceover", "payload": {"segment_id": retaken}},
    )
    assert response.status_code == 200, response.text

    job = async_repo.jobs[
        agent_repo.get_session(sid).state_json["layers"]["voiceover"]["linked_job_id"]
    ]
    assert job.request_json.get("reuse_segment_audio") == {
        kept: f"/tmp/{kept}.mp3"
    }, "the line nobody complained about was re-recorded too"


def test_the_console_is_told_where_sign_in_actually_lives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Sign in button led to `/console/signin`, a path that exists on the
    platform host and nowhere else — so on the standalone deployment, which is
    the studio's own production, the one button offering a way in returned a
    404. Where sign-in lives is a property of the deployment, so the deployment
    says."""
    client, _, _, _, _ = _test_client(tmp_path)

    monkeypatch.delenv("AGENTIC_AUDIO_SIGNIN_URL", raising=False)
    default = client.get("/api/v2/agentic/audio/auth/config").json()
    assert default["signin_url"] == "/console/signin"

    monkeypatch.setenv("AGENTIC_AUDIO_SIGNIN_URL", "https://console.example/signin")
    moved = client.get("/api/v2/agentic/audio/auth/config").json()
    assert moved["signin_url"] == "https://console.example/signin"
