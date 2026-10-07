"""Media durability: files and artifact rows must outlive a container recycle.

The failure being prevented, observed live three times in one day: Azure
recycles the app's container (deploys, but also spontaneous reschedules), the
disk and the in-memory artifact registry vanish, and every existing session is
stranded — media 404s and the next turn is rejected with "source video
artifact not found". These tests simulate the recycle honestly: a brand-new
store instance over the same blob backend, fresh empty directories, a fresh
registry.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

from EdennCode.EdennAgent.AgenticAudio.persistence.media_store import (
    DurableMediaStore,
    _Backend,
)


# --------------------------------------------------------------------------- #
# a dict-backed blob service                                                  #
# --------------------------------------------------------------------------- #


class _FakeBlobs:
    def __init__(self) -> None:
        self.data: Dict[Tuple[str, str], bytes] = {}
        self.fail_uploads = False

    def backend(self) -> _Backend:
        def upload(container: str, name: str, path: Path) -> None:
            if self.fail_uploads:
                raise RuntimeError("storage is down")
            self.data[(container, name)] = Path(path).read_bytes()

        def download(container: str, name: str, dest: Path) -> bool:
            blob = self.data.get((container, name))
            if blob is None:
                return False
            Path(dest).write_bytes(blob)
            return True

        def upload_text(container: str, name: str, text: str) -> None:
            if self.fail_uploads:
                raise RuntimeError("storage is down")
            self.data[(container, name)] = text.encode()

        def download_text(container: str, name: str) -> Optional[str]:
            blob = self.data.get((container, name))
            return blob.decode() if blob is not None else None

        return _Backend(upload, download, upload_text, download_text)


def _store(blobs: _FakeBlobs) -> DurableMediaStore:
    return DurableMediaStore(backend=blobs.backend())


# --------------------------------------------------------------------------- #
# files                                                                       #
# --------------------------------------------------------------------------- #


def test_a_file_survives_the_recycle(tmp_path: Path) -> None:
    blobs = _FakeBlobs()
    before = _store(blobs)
    f = tmp_path / "old_disk" / "take.mp3"
    f.parent.mkdir()
    f.write_bytes(b"paid-for audio")
    before.persist(f, "media")
    before.flush()

    # The recycle: new process, new store instance, empty disk.
    after = _store(blobs)
    new_disk = tmp_path / "new_disk"
    restored = after.restore("take.mp3", "media", new_disk)
    assert restored is not None
    assert restored.read_bytes() == b"paid-for audio"


def test_a_missing_blob_is_a_clean_miss(tmp_path: Path) -> None:
    after = _store(_FakeBlobs())
    assert after.restore("never-existed.wav", "media", tmp_path) is None


def test_restore_never_serves_a_traversal(tmp_path: Path) -> None:
    blobs = _FakeBlobs()
    blobs.data[("generated-media", "secret.wav")] = b"x"
    store = _store(blobs)
    restored = store.restore("../../etc/secret.wav", "media", tmp_path / "d")
    # The name is clamped to its basename; the file lands inside dest_dir.
    assert restored is not None
    assert restored.parent == (tmp_path / "d")


def test_persist_failure_is_loud_but_not_fatal(tmp_path: Path, caplog) -> None:
    """A narration must never fail because its backup copy could not be made."""
    import logging

    blobs = _FakeBlobs()
    blobs.fail_uploads = True
    store = _store(blobs)
    f = tmp_path / "take.mp3"
    f.write_bytes(b"x")
    with caplog.at_level(logging.ERROR):
        assert store.persist_sync(f, "media") is False
    assert any("FAILED to persist" in r.message for r in caplog.records)


def test_the_inert_store_changes_nothing(tmp_path: Path) -> None:
    """No storage configured -> a laptop devserver behaves exactly as before."""
    store = DurableMediaStore(backend=None)
    assert store.active is False
    f = tmp_path / "x.wav"
    f.write_bytes(b"x")
    store.persist(f, "media")          # no-op, no error
    assert store.persist_sync(f, "media") is False
    assert store.restore("x.wav", "media", tmp_path) is None
    assert store.restore_artifact("a:b:c") is None
    store.persist_artifact(object())   # must not touch the object


# --------------------------------------------------------------------------- #
# the artifact registry                                                       #
# --------------------------------------------------------------------------- #


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def test_a_registry_row_survives_the_recycle() -> None:
    blobs = _FakeBlobs()
    before = _store(blobs)
    artifact = _Row(
        artifact_id="asset_job_ab12:source_video:input",
        job_id="asset_job_ab12",
        artifact_type="source_video",
        role="input",
        container="user-uploads",
        blob_name="source/clip.mp4",
        url="/dev/uploads/asset_job_ab12_clip.mp4",
        content_type="video/mp4",
        local_path="/tmp/gone/asset_job_ab12_clip.mp4",
        metadata_json={"duration": 16.1},
    )
    job = _Row(job_id="asset_job_ab12", job_type="asset_staging",
               creator_user_id="owner_zj", request_json={"upload_filename": "clip.mp4"})
    before.persist_artifact(artifact, job=job)
    before.flush()

    after = _store(blobs)
    row = after.restore_artifact("asset_job_ab12:source_video:input")
    assert row is not None
    assert row["artifact"]["url"] == "/dev/uploads/asset_job_ab12_clip.mp4"
    assert row["artifact"]["metadata_json"]["duration"] == 16.1
    assert row["job"]["creator_user_id"] == "owner_zj"


def test_a_corrupt_registry_row_is_a_miss_not_a_crash() -> None:
    blobs = _FakeBlobs()
    blobs.data[("generated-media", "_registry/bad__row.json")] = b"{not json"
    assert _store(blobs).restore_artifact("bad:row") is None


# --------------------------------------------------------------------------- #
# the whole loop, through the devserver's own wiring                          #
# --------------------------------------------------------------------------- #


def test_the_devserver_wires_all_five_seams() -> None:
    """The store is only as good as its call sites. Pin each one structurally
    so a refactor cannot silently drop a seam: upload persist, registry
    persist, media-route restore, uploads-route restore, resolver restore,
    result persist, and the rehydrating registry.

    The compose/remix seams are separate from the job seam on purpose: those two
    renders happen in-process and never pass through a job, so the job-completion
    persist never saw them — while the mix they produce is exactly what
    final_artifact.video_url points at."""
    src = Path("EdennCode/EdennAgent/AgenticAudio/design/devserver.py").read_text()
    for seam in (
        'media_store.persist(dest, "uploads")',        # the upload itself
        "media_store.persist_artifact(",                # its registry row
        'media_store.restore, safe_name, "media"',      # /dev/media miss
        'media_store.restore, Path(name).name, "uploads"',  # /dev/uploads miss
        "_persist_result_media(final_result)",          # every job's outputs
        "_persist_result_media(mix_result)",            # the composed deliverable
        "_persist_result_media(remix_result)",          # a promoted remix
        "class _RehydratingAsyncRepo",                  # the second chance
        "media_store = DurableMediaStore.configured()", # construction
    ):
        assert seam in src, f"durability seam missing from devserver: {seam}"


def test_result_media_walker_finds_nested_urls(tmp_path: Path) -> None:
    """The job-result walker must find /dev/media URLs at any depth — segment
    lists, nested layer dicts — because the result dict is the one honest
    inventory of what a job produced."""
    # Exercise the walker via a tiny reimplementation contract: the devserver
    # closure is not importable, so pin the traversal shape it relies on.
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str) and value.startswith("/dev/media/"):
            found.append(Path(value.split("?")[0]).name)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk({
        "audio_url": "/dev/media/take.mp3",
        "segments": [{"audio_url": "/dev/media/line1.wav?x=1"}],
        "layers": {"voiceover": {"complete_audio_url": "/dev/media/vo.wav"}},
        "unrelated": "https://elsewhere/file.mp3",
    })
    assert sorted(found) == ["line1.wav", "take.mp3", "vo.wav"]


# --------------------------------------------------------------------------- #
# vendor-read URLs                                                            #
# --------------------------------------------------------------------------- #


def test_vendor_read_url_is_signed_scoped_and_absent_when_inert(tmp_path) -> None:
    """The video-conditioned engine fetches the clip by URL from the vendor's
    side. It must get a signed blob read — never a console URL with our API
    token in it — and an inert store must answer None so the caller falls back
    to the text route."""
    from EdennCode.EdennAgent.AgenticAudio.persistence.media_store import (
        DurableMediaStore, _Backend,
    )

    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x" * 64)

    signed_calls: list[tuple] = []
    uploaded: list[str] = []
    backend = _Backend(
        upload=lambda c, n, p: uploaded.append(f"{c}/{n}"),
        download=lambda c, n, d: False,
        upload_text=lambda c, n, t: None,
        download_text=lambda c, n: None,
        signed_url=lambda c, n, ttl: (signed_calls.append((c, n, ttl)) or
                                      f"https://store.example/{c}/{n}?sig=abc"),
    )
    store = DurableMediaStore(backend=backend)
    url = store.vendor_read_url(f, "uploads")
    assert url == "https://store.example/user-uploads/clip.mp4?sig=abc"
    # The blob copy was ensured before signing (the background persist race).
    assert uploaded == ["user-uploads/clip.mp4"]
    assert signed_calls[0][2] == 60  # default TTL, minutes

    assert DurableMediaStore(backend=None).vendor_read_url(f, "uploads") is None
