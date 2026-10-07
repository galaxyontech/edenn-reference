"""Unit tests for the multi_image worker's input-download retry.

Input materialization runs before any paid provider call, so a transient blob/
network blip is retried (up to INPUT_DOWNLOAD_MAX_ATTEMPTS) with a fresh SAS each
attempt, instead of dead-lettering the whole job under max_attempts=1.
"""
import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from EdennCode.Deployment.async_pipeline_v2.workers import multi_image_worker as mod
from EdennCode.Deployment.async_pipeline_v2.workers.multi_image_worker import (
    MultiImageMonolithWorker,
)
from EdennCode.Deployment.async_pipeline_v2.models import AsyncV2Artifact


class _FakeStorage:
    def __init__(self):
        self.sign_calls = 0

    def generate_sas_url(self, *, container, blob_name, require_signed):
        self.sign_calls += 1
        return f"https://acct.blob.core.windows.net/{container}/{blob_name}?sig=FRESH{self.sign_calls}"


def _worker(storage):
    return MultiImageMonolithWorker(
        repository=MagicMock(), queue=MagicMock(), orchestrator=MagicMock(),
        settings=SimpleNamespace(workdir="/tmp"), storage=storage,
    )


def _artifact():
    return AsyncV2Artifact(
        artifact_id="art1", job_id="job1", artifact_type="source_image", role="image_0",
        container="voicestorage", blob_name="jobs/job1/input/images/image_1.webp",
        url=None, content_type="image/webp", local_path=None, metadata_json={},
    )


class MaterializeRetryTests(unittest.TestCase):
    def test_recovers_after_transient_failures_with_fresh_sas(self):
        storage = _FakeStorage()
        worker = _worker(storage)
        seen_urls = []
        calls = {"n": 0}

        async def fake_dl(*, url, destination, asset_label):
            calls["n"] += 1
            seen_urls.append(url)
            if calls["n"] < 3:  # fail twice, succeed on the 3rd
                raise RuntimeError("transient blob blip")
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"imgbytes")
            return Path(destination)

        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "image_1.webp"
            with patch.object(mod, "download_public_file_to_disk", side_effect=fake_dl), \
                 patch.object(mod, "INPUT_DOWNLOAD_BACKOFF_S", 0):
                result = asyncio.run(worker._materialize_artifact(_artifact(), dest))
            self.assertTrue(result.exists())
            self.assertEqual(calls["n"], 3)          # retried until success
            self.assertEqual(storage.sign_calls, 3)  # a fresh SAS minted per attempt
            self.assertEqual(len(set(seen_urls)), 3)  # each attempt used a distinct URL

    def test_gives_up_after_max_attempts_and_reraises(self):
        storage = _FakeStorage()
        worker = _worker(storage)
        calls = {"n": 0}

        async def always_fail(*, url, destination, asset_label):
            calls["n"] += 1
            raise RuntimeError("provider-neutral download error")

        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "image_1.webp"
            with patch.object(mod, "download_public_file_to_disk", side_effect=always_fail), \
                 patch.object(mod, "INPUT_DOWNLOAD_BACKOFF_S", 0):
                with self.assertRaises(RuntimeError):
                    asyncio.run(worker._materialize_artifact(_artifact(), dest))
            self.assertEqual(calls["n"], mod.INPUT_DOWNLOAD_MAX_ATTEMPTS)  # exactly 3, no more
            self.assertEqual(storage.sign_calls, mod.INPUT_DOWNLOAD_MAX_ATTEMPTS)

    def test_max_attempts_is_three(self):
        self.assertEqual(mod.INPUT_DOWNLOAD_MAX_ATTEMPTS, 3)


if __name__ == "__main__":
    unittest.main()
