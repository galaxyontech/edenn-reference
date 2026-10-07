"""Pure-unit tests (no DB) for MultiImageMonolithWorker's process_one lifecycle:
happy path (lease -> run -> record artifacts -> build unified response -> complete)
and the no-retry dead-letter path (max_attempts=1)."""
import asyncio
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Optional

from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Artifact,
    AsyncV2Task,
    JobStatus,
    TaskStatus,
)
from EdennCode.Deployment.async_pipeline_v2.workers.multi_image_worker import (
    MultiImageMonolithWorker,
)
from EdennCode.Deployment.multi_image_workflows import MultiImageGenerationResult
from EdennCode.MusicGenerationCore.models import SectionTiming


class _FakeRepo:
    def __init__(self) -> None:
        self.jobs: dict[str, Any] = {}
        self.artifacts: dict[str, AsyncV2Artifact] = {}
        self.stage_runs: dict[str, Any] = {}
        self.events: list[dict] = []
        self.job_status: dict[str, dict] = {}

    def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    def get_artifact(self, artifact_id: str):
        return self.artifacts.get(artifact_id)

    def add_artifact(self, *, artifact_id, job_id, artifact_type, role, container,
                     blob_name, url, content_type, local_path, metadata_json):
        art = AsyncV2Artifact(
            artifact_id=artifact_id, job_id=job_id, artifact_type=artifact_type,
            role=role, container=container, blob_name=blob_name, url=url,
            content_type=content_type, local_path=local_path,
            metadata_json=metadata_json or {},
        )
        self.artifacts[artifact_id] = art
        return art

    def start_stage_run(self, *, job_id, task_id, stage_name, attempt, input_json):
        run = SimpleNamespace(stage_run_id=f"{job_id}:{stage_name}:{attempt}")
        self.stage_runs[run.stage_run_id] = run
        return run

    def update_stage_run(self, stage_run_id, **kwargs):
        self.stage_runs[stage_run_id] = SimpleNamespace(stage_run_id=stage_run_id, **kwargs)

    _TERMINAL = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}

    def update_job_status(self, job_id, *, status, **kwargs):
        # Faithful to production: terminal states are never overwritten.
        job, _ = self.update_job_status_checked(job_id, status=status, **kwargs)
        return job

    def update_job_status_checked(self, job_id, *, status, **kwargs):
        current = self.job_status.get(job_id, {}).get("status")
        if current in self._TERMINAL:
            return SimpleNamespace(job_id=job_id, status=current), False
        self.job_status[job_id] = {"status": status, **kwargs}
        return SimpleNamespace(job_id=job_id, status=status), True

    def add_event(self, **kwargs):
        self.events.append(kwargs)

    @contextmanager
    def transaction(self):
        yield None


class _FakeQueue:
    def __init__(self, task: AsyncV2Task, *, fail_status: str = "dead_lettered") -> None:
        self._task = task
        self._leased = False
        self.completed = False
        self.failed = False
        self._fail_status = fail_status

    def lease(self, *, queue_name, worker_id, lease_seconds):
        if self._leased:
            return None
        self._leased = True
        return self._task

    def heartbeat(self, **kwargs):
        pass

    def complete(self, *, task_id, worker_id, client=None):
        self.completed = True
        return SimpleNamespace(task_id=task_id, status=TaskStatus.COMPLETED)

    def fail(self, *, task_id, worker_id, error, retry, backoff_seconds=0, client=None):
        self.failed = True
        self.fail_retry = retry
        self.fail_error = error
        return SimpleNamespace(task_id=task_id, status=self._fail_status)


class _DisabledStorage:
    enabled = False


class _FakeOrchestrator:
    def __init__(self, out_dir: Path, *, raise_exc: Optional[Exception] = None) -> None:
        self.out_dir = out_dir
        self.raise_exc = raise_exc

    async def run(self, *, folder_path, output_path, **kwargs):
        if self.raise_exc is not None:
            raise self.raise_exc
        # Assert the worker downloaded the source images into folder_path.
        images = list(Path(folder_path).glob("*"))
        assert images, "orchestrator received no staged images"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        music = self.out_dir / "music.wav"
        music.write_bytes(b"RIFF0000WAVEfmt ")
        video = Path(output_path)
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return MultiImageGenerationResult(
            final_video_path=video,
            silent_video_path=video,
            generated_music_path=music,
            full_track_paths=[music],
            processed_image_paths=[Path(p) for p in images],
            planning_metadata={"storyline_summary": "s", "overall_mood": "bright"},
            music_prompt="bright pop",
            lyrics_timestamps=[],
            section_timeline=[SectionTiming(section_id="intro", expected_start_s=0.0, expected_end_s=3.0)],
            video_title="Trip", music_title="Golden Hour", video_description="A trip",
            include_vocals=False, vocal_gender="female", lyrics_language="",
            user_requested_language="ENGLISH_US", used_music_model_spec="edenn_studio",
            compression_applied=False, vocal_id_used=None,
            job_received_timestamp=1, job_finished_timestamp=2,
        )


def _make_task(job_id: str, image_ids: list[str], *, max_attempts: int = 1, attempt: int = 1) -> AsyncV2Task:
    return AsyncV2Task(
        task_id=f"{job_id}:task", job_id=job_id, queue_name="multi-image-pipeline",
        task_type="multi_image_monolith", status=TaskStatus.LEASED,
        payload_json={"image_artifact_ids": image_ids}, attempt=attempt,
        max_attempts=max_attempts,
    )


class MultiImageWorkerTests(unittest.TestCase):
    def _seed_images(self, repo: _FakeRepo, job_id: str, tmp: Path, n: int = 2) -> list[str]:
        ids = []
        for i in range(n):
            p = tmp / f"src_{i}.png"
            p.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([i]))
            aid = f"{job_id}:image:{i}"
            repo.artifacts[aid] = AsyncV2Artifact(
                artifact_id=aid, job_id=job_id, artifact_type="source_image",
                role=f"image_{i}", container=None, blob_name=None, url=None,
                content_type="image/png", local_path=str(p), metadata_json={},
            )
            ids.append(aid)
        return ids

    def test_process_one_happy_path_records_artifacts_and_builds_unified_response(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_id = "job1"
            repo = _FakeRepo()
            image_ids = self._seed_images(repo, job_id, tmp_path, n=2)
            repo.jobs[job_id] = SimpleNamespace(
                job_id=job_id,
                request_json={"modelspec": "edenn_studio", "image_artifact_ids": image_ids},
            )
            task = _make_task(job_id, image_ids)
            queue = _FakeQueue(task)
            worker = MultiImageMonolithWorker(
                repository=repo, queue=queue,
                orchestrator=_FakeOrchestrator(tmp_path / "out"),
                settings=SimpleNamespace(workdir=tmp_path / "work",
                                         audio_container_name="audio", output_container="video"),
                storage=_DisabledStorage(),
            )

            processed = asyncio.run(worker.process_one())

            self.assertIsNotNone(processed)
            self.assertEqual(processed.status, TaskStatus.COMPLETED)
            self.assertTrue(queue.completed)
            self.assertEqual(repo.job_status[job_id]["status"], JobStatus.COMPLETED)
            result_json = repo.job_status[job_id]["result_json"]
            # Unified response fields present, under the nested metadata blocks.
            audio_metadata = result_json["audio_metadata"]
            video_metadata = result_json["video_metadata"]
            self.assertEqual(audio_metadata["music_title"], "Golden Hour")
            # This fake yields no distinct window (matched_music_path unset), so
            # audio_* falls back to the full primary track and equals complete_*.
            self.assertEqual(
                audio_metadata["complete_audio_url"], audio_metadata["audio_url"]
            )
            # music_title is surfaced once (audio_metadata), not duplicated in the summary.
            self.assertNotIn("music_title", video_metadata["video_summary"])
            self.assertNotIn("scenes", video_metadata)  # removed from the job response
            # Output artifacts recorded for URL refresh.
            self.assertIn("job1:complete_audio:primary", repo.artifacts)
            self.assertIn("job1:output_video:final", repo.artifacts)

    def test_process_one_dead_letters_without_retry_on_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_id = "job2"
            repo = _FakeRepo()
            image_ids = self._seed_images(repo, job_id, tmp_path, n=1)
            repo.jobs[job_id] = SimpleNamespace(
                job_id=job_id, request_json={"image_artifact_ids": image_ids},
            )
            task = _make_task(job_id, image_ids, max_attempts=1)
            queue = _FakeQueue(task, fail_status="dead_lettered")
            worker = MultiImageMonolithWorker(
                repository=repo, queue=queue,
                orchestrator=_FakeOrchestrator(tmp_path / "out", raise_exc=RuntimeError("provider failure marker-x9f2")),
                settings=SimpleNamespace(workdir=tmp_path / "work",
                                         audio_container_name="audio", output_container="video"),
                storage=_DisabledStorage(),
            )

            processed = asyncio.run(worker.process_one())

            self.assertTrue(queue.failed)
            self.assertEqual(repo.job_status[job_id]["status"], JobStatus.FAILED)
            # Sanitized error — never leaks the raw exception text (which could name a provider).
            err = repo.job_status[job_id]["error_json"]
            self.assertNotIn("marker-x9f2", str(err))
            # An unexpected internal error is transient by classification: the
            # client may retry, and the queue may retry if attempts remain.
            self.assertTrue(err["retryable"])
            self.assertTrue(queue.fail_retry)

    def test_permanent_failure_is_not_retryable_and_not_retried(self) -> None:
        """Content-policy rejection: retryable=false to the client AND no
        internal queue retry — every retry would re-bill the music provider."""
        from EdennCode.exceptions import EdennContentPolicyViolationError

        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_id = "job3"
            repo = _FakeRepo()
            image_ids = self._seed_images(repo, job_id, tmp_path, n=1)
            repo.jobs[job_id] = SimpleNamespace(
                job_id=job_id, request_json={"image_artifact_ids": image_ids},
            )
            task = _make_task(job_id, image_ids, max_attempts=3)
            queue = _FakeQueue(task, fail_status="failed")
            worker = MultiImageMonolithWorker(
                repository=repo, queue=queue,
                orchestrator=_FakeOrchestrator(
                    tmp_path / "out",
                    raise_exc=EdennContentPolicyViolationError("flagged marker-y7q4"),
                ),
                settings=SimpleNamespace(workdir=tmp_path / "work",
                                         audio_container_name="audio", output_container="video"),
                storage=_DisabledStorage(),
            )

            asyncio.run(worker.process_one())

            self.assertTrue(queue.failed)
            self.assertFalse(queue.fail_retry)  # no internal retry despite max_attempts=3
            self.assertEqual(repo.job_status[job_id]["status"], JobStatus.FAILED)
            err = repo.job_status[job_id]["error_json"]
            self.assertFalse(err["retryable"])
            self.assertEqual(err["error_code"], 11001)
            self.assertNotIn("marker-y7q4", str(err))

    def test_canceled_job_is_not_resurrected_by_late_completion(self) -> None:
        """A job canceled before the worker's terminal write stays canceled:
        the completed write no-ops and no job.completed event is emitted."""
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_id = "job4"
            repo = _FakeRepo()
            image_ids = self._seed_images(repo, job_id, tmp_path, n=1)
            repo.jobs[job_id] = SimpleNamespace(
                job_id=job_id, request_json={"image_artifact_ids": image_ids},
            )
            # Client cancel landed while the worker was mid-run.
            repo.job_status[job_id] = {"status": JobStatus.CANCELED}
            task = _make_task(job_id, image_ids, max_attempts=1)
            queue = _FakeQueue(task)
            worker = MultiImageMonolithWorker(
                repository=repo, queue=queue,
                orchestrator=_FakeOrchestrator(tmp_path / "out"),
                settings=SimpleNamespace(workdir=tmp_path / "work",
                                         audio_container_name="audio", output_container="video"),
                storage=_DisabledStorage(),
            )

            asyncio.run(worker.process_one())

            self.assertEqual(repo.job_status[job_id]["status"], JobStatus.CANCELED)
            self.assertNotIn(
                "job.completed", [e.get("event_type") for e in repo.events]
            )
            # The leased task itself still completes (the work ran; billing stands).
            self.assertTrue(queue.completed)


if __name__ == "__main__":
    unittest.main()
