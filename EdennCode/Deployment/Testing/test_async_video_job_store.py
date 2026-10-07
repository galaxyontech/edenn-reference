from __future__ import annotations

import unittest
from unittest.mock import patch

from EdennCode.Deployment.api_common import JobStatus
from EdennCode.Deployment.async_video_job_store import (
    AsyncVideoJobState,
    InMemoryAsyncVideoJobStore,
    PostgresAsyncVideoJobStore,
    build_async_video_job_store_from_env,
)


class AsyncVideoJobStoreTests(unittest.TestCase):
    def test_memory_store_lifecycle_and_ttl_cleanup(self) -> None:
        store = InMemoryAsyncVideoJobStore()

        pending = store.register("job_1")
        self.assertEqual(pending.status, JobStatus.PENDING)

        processing = store.set_processing("job_1")
        self.assertEqual(processing.status, JobStatus.PROCESSING)

        completed = store.complete("job_1", {"job_id": "job_1"})
        self.assertEqual(completed.status, JobStatus.COMPLETED)
        self.assertEqual(completed.result, {"job_id": "job_1"})
        self.assertIsNotNone(completed.finished_at)

        assert completed.finished_at is not None
        store.cleanup_expired(ttl_seconds=1, now=completed.finished_at + 1)
        self.assertIsNone(store.get("job_1"))

    def test_postgres_state_from_row_preserves_json_payloads(self) -> None:
        state = PostgresAsyncVideoJobStore._state_from_row(
            {
                "status": "completed",
                "created_at": 100,
                "updated_at": 200,
                "finished_at": 300,
                "result_json": {"job_id": "job_2"},
                "error_json": None,
            }
        )

        self.assertIsInstance(state, AsyncVideoJobState)
        assert state is not None
        self.assertEqual(state.status, JobStatus.COMPLETED)
        self.assertEqual(state.result, {"job_id": "job_2"})
        self.assertIsNone(state.error)

    def test_build_store_auto_uses_postgres_when_database_env_exists(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": "postgresql://example/test"},
            clear=True,
        ):
            store = build_async_video_job_store_from_env()

        self.assertIsInstance(store, PostgresAsyncVideoJobStore)

    def test_build_store_auto_uses_memory_without_database_env(self) -> None:
        memory_store = InMemoryAsyncVideoJobStore()
        with patch.dict("os.environ", {}, clear=True):
            store = build_async_video_job_store_from_env(memory_store=memory_store)

        self.assertIs(store, memory_store)


if __name__ == "__main__":
    unittest.main()
