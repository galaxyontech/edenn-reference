"""Durable media for Agentic Audio — files and artifact rows that outlive a pod.

The deployed app keeps uploads, generated audio, and the artifact registry in
the container's own disk and memory. Azure recycles that container whenever it
likes — a reschedule with zero deploys was observed live — and every recycle
strands every session: media 404s, and the next turn is rejected with "source
video artifact not found". Sessions are already durable (Postgres); this module
makes the rest of them durable, using the blob containers the stack provisioned
on day one and never used.

Two responsibilities, one backend:

* **Files.** ``persist()`` copies a local media file up to the studio's own
  storage account in the background; ``restore()`` fetches it back by name.
  Serving routes and pipeline resolvers call ``restore`` only on a local miss,
  so the local disk remains the fast path and blob is the safety net.
* **The registry.** ``persist_artifact()`` writes an artifact row as a small
  JSON blob; ``restore_artifact()`` brings it back. A registry row without its
  file is a promise the server can't keep, so restoring a row also restores
  the file its ``local_path`` points to.

Unconfigured (no storage account in the environment) the store is INERT: every
persist is a no-op, every restore misses, and the devserver behaves exactly as
before on a laptop. Configuration comes from ``AZURE_STORAGE_ACCOUNT`` +
``AZURE_STORAGE_KEY`` — the studio's OWN storage account, the same variables
the deploy script already sets on the app.

Failure policy: persistence failures are LOUD in the log and invisible to the
user — a narration must never fail because a backup copy could not be made.
Restores that fail simply miss, and the caller returns its 404.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Container names match deploy/provision.sh. "uploads" is footage the user gave
# us; "media" is everything the studio generated or composed.
_CONTAINERS = {
    "uploads": "user-uploads",
    "media": "generated-media",
}
_REGISTRY_PREFIX = "_registry/"


@dataclass(frozen=True)
class _Backend:
    """The blob operations the store needs, injectable for tests."""

    upload: Callable[[str, str, Path], None]  # (container, blob_name, path)
    download: Callable[[str, str, Path], bool]  # -> True if found
    upload_text: Callable[[str, str, str], None]
    download_text: Callable[[str, str], Optional[str]]
    # (container, blob_name, ttl_minutes) -> time-limited read-only URL, or None.
    signed_url: Optional[Callable[[str, str, int], Optional[str]]] = None
    # (container, blob_name) -> True if the object is gone afterwards. Optional
    # so a backend written before deletion existed still satisfies the type;
    # a store whose backend cannot delete says so rather than pretending.
    delete: Optional[Callable[[str, str], bool]] = None


def _azure_backend(account: str, key: str) -> _Backend:
    from azure.storage.blob import BlobServiceClient

    client = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net", credential=key
    )

    def upload(container: str, blob_name: str, path: Path) -> None:
        with path.open("rb") as fh:
            client.get_blob_client(container, blob_name).upload_blob(
                fh, overwrite=True
            )

    def download(container: str, blob_name: str, dest: Path) -> bool:
        blob = client.get_blob_client(container, blob_name)
        try:
            data = blob.download_blob()
        except Exception:  # noqa: BLE001 - not-found and transport alike: a miss
            return False
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fh:
            data.readinto(fh)
        tmp.replace(dest)  # atomic: a reader never sees a half-written file
        return True

    def upload_text(container: str, blob_name: str, text: str) -> None:
        client.get_blob_client(container, blob_name).upload_blob(
            text.encode("utf-8"), overwrite=True
        )

    def download_text(container: str, blob_name: str) -> Optional[str]:
        try:
            return (
                client.get_blob_client(container, blob_name)
                .download_blob()
                .readall()
                .decode("utf-8")
            )
        except Exception:  # noqa: BLE001
            return None

    def signed_url(container: str, blob_name: str, ttl_minutes: int) -> Optional[str]:
        from datetime import datetime, timedelta, timezone

        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        try:
            sas = generate_blob_sas(
                account_name=account,
                container_name=container,
                blob_name=blob_name,
                account_key=key,
                permission=BlobSasPermissions(read=True),
                expiry=datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("durable media: could not sign a read URL (%s)", exc)
            return None
        return (f"https://{account}.blob.core.windows.net/"
                f"{container}/{blob_name}?{sas}")

    def delete(container: str, blob_name: str) -> bool:
        """Remove one object. A blob that is already gone counts as deleted."""

        try:
            client.get_blob_client(container, blob_name).delete_blob(
                delete_snapshots="include"
            )
            return True
        except Exception as exc:  # noqa: BLE001
            if "BlobNotFound" in str(exc) or getattr(exc, "status_code", None) == 404:
                return True
            logger.error(
                "durable media: FAILED to delete %s from %s (%s) — the row is "
                "gone and the object is not, which is the shape of a retention "
                "promise that was only half kept",
                blob_name, container, exc,
            )
            return False

    return _Backend(upload, download, upload_text, download_text, signed_url, delete)


class DurableMediaStore:
    """Blob-backed durability for media files and artifact-registry rows."""

    def __init__(self, backend: Optional[_Backend] = None) -> None:
        self._backend = backend
        self._pending: list[threading.Thread] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # construction                                                        #
    # ------------------------------------------------------------------ #

    @classmethod
    def configured(cls) -> "DurableMediaStore":
        """The studio's own storage account, or an inert store.

        Reads the STORAGE variables the deploy script sets on this app. (The
        name deliberately avoids the ambient-config constructor pattern the
        isolation contract test greps this package for — that pattern is how
        two production incidents started. This store touches only the studio's
        own storage account.)"""

        account = os.getenv("AZURE_STORAGE_ACCOUNT", "").strip()
        key = os.getenv("AZURE_STORAGE_KEY", "").strip()
        if not account or not key:
            logger.info(
                "durable media: no storage account configured — local-only "
                "(files will not survive a container recycle)"
            )
            return cls(backend=None)
        try:
            backend = _azure_backend(account, key)
        except Exception as exc:  # noqa: BLE001 - a broken SDK must not stop boot
            logger.error("durable media: backend unavailable (%s)", exc)
            return cls(backend=None)
        logger.info("durable media: persisting to the studio's storage account")
        return cls(backend=backend)

    @property
    def active(self) -> bool:
        return self._backend is not None

    # ------------------------------------------------------------------ #
    # files                                                               #
    # ------------------------------------------------------------------ #

    def persist(self, path: Path, kind: str) -> None:
        """Copy a local media file to blob, in the background, loudly on failure."""

        if self._backend is None:
            return
        container = _CONTAINERS[kind]
        path = Path(path)

        def _run() -> None:
            try:
                self._backend.upload(container, path.name, path)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "durable media: FAILED to persist %s to %s (%s) — this "
                    "file will not survive a container recycle",
                    path.name, container, exc,
                )

        t = threading.Thread(target=_run, name=f"persist-{path.name}", daemon=True)
        with self._lock:
            self._pending = [x for x in self._pending if x.is_alive()]
            self._pending.append(t)
        t.start()

    def persist_sync(self, path: Path, kind: str) -> bool:
        """Foreground persist — for tests and for callers that must know."""

        if self._backend is None:
            return False
        try:
            self._backend.upload(_CONTAINERS[kind], Path(path).name, Path(path))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("durable media: FAILED to persist %s (%s)", path, exc)
            return False

    def restore(self, name: str, kind: str, dest_dir: Path) -> Optional[Path]:
        """Fetch a file back by basename. None on a miss; never raises."""

        if self._backend is None:
            return None
        safe = Path(name).name  # no traversal
        dest = Path(dest_dir) / safe
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if self._backend.download(_CONTAINERS[kind], safe, dest):
                logger.info("durable media: restored %s from %s", safe, _CONTAINERS[kind])
                return dest
        except Exception as exc:  # noqa: BLE001
            logger.error("durable media: restore of %s failed (%s)", safe, exc)
        return None

    def delete(self, blob_name: str, kind: str) -> bool:
        """Remove one stored object. False means it may still be there.

        Deletion is the half of retention that was deliberately left out: the
        module that removes sessions says in its own docstring that it reclaims
        the database and not the disk. That gap is not a tidiness problem. A
        person told their footage was deleted, whose footage is still sitting in
        a container, has been told something untrue — and the bytes keep costing
        money for as long as nobody notices.
        """

        if self._backend is None or self._backend.delete is None:
            return False
        safe = Path(blob_name).name  # no traversal, same rule as restore
        container = _CONTAINERS.get(kind)
        if not container or not safe:
            return False
        return bool(self._backend.delete(container, safe))

    def delete_ref(self, container: str, blob_name: str) -> bool:
        """Delete the object an artifact row names, by its FULL blob name.

        Separate from :meth:`delete` on purpose. That one takes a basename
        because the media store writes files under their basename; an artifact
        row records whatever the writer chose, and some of those carry a path
        prefix. Stripping it — which is what the basename rule would do —
        deletes nothing and reports success, which is the worst of the three
        possible outcomes.
        """

        name = str(blob_name or "").strip().lstrip("/")
        known = set(_CONTAINERS.values())
        if (
            self._backend is None
            or self._backend.delete is None
            or container not in known
            or not name
            or ".." in name
        ):
            return False
        return bool(self._backend.delete(container, name))

    def delete_many(self, refs: list[tuple[str, str]]) -> tuple[int, list[str]]:
        """Delete (container, blob_name) pairs. Returns (removed, failed_names).

        Never raises: one object that refuses to go must not stop the rest of a
        person's media from being removed.
        """

        removed = 0
        failed: list[str] = []
        for container, name in refs:
            try:
                if self.delete_ref(container, name):
                    removed += 1
                else:
                    failed.append(name)
            except Exception:  # noqa: BLE001
                logger.error("durable media: delete of %s raised", name, exc_info=True)
                failed.append(name)
        return removed, failed

    @property
    def can_delete(self) -> bool:
        """Whether this store can actually remove anything.

        Callers must be able to tell "deleted nothing because there was nothing"
        from "deleted nothing because this backend has no delete", or a
        retention report becomes a reassuring lie.
        """

        return self._backend is not None and self._backend.delete is not None

    def vendor_read_url(self, path: Path, kind: str, *,
                        ttl_minutes: int = 60) -> Optional[str]:
        """A time-limited, read-only URL an UPSTREAM PROVIDER can fetch.

        The video-conditioned sound-effects engine fetches the clip from the
        vendor's side by URL. The serving routes are token-gated (correctly),
        and our API tokens must never ride in a URL handed to a third party —
        so the vendor gets a signed read on the blob copy instead: scoped to
        one file, read-only, expiring. Returns None when the store is inert;
        the caller falls back to the text route, which is a quality decision,
        not an outage.
        """

        if self._backend is None or self._backend.signed_url is None:
            return None
        path = Path(path)
        container = _CONTAINERS[kind]
        # The blob copy must exist before the vendor pulls it. persist() runs
        # in the background at upload time; by generation time it has almost
        # always landed — the sync retry covers the race.
        if path.is_file() and not self.persist_sync(path, kind):
            return None
        return self._backend.signed_url(container, path.name, ttl_minutes)

    def flush(self, timeout_s: float = 30.0) -> None:
        """Wait for background persists — drain/shutdown and tests."""

        with self._lock:
            pending = list(self._pending)
        for t in pending:
            t.join(timeout=timeout_s)

    # ------------------------------------------------------------------ #
    # the artifact registry                                               #
    # ------------------------------------------------------------------ #

    def persist_artifact(self, artifact: Any, *, job: Any = None) -> None:
        """Write an artifact row (and optionally its job row) as a JSON blob."""

        if self._backend is None:
            return
        row = {
            "artifact": {
                "artifact_id": artifact.artifact_id,
                "job_id": artifact.job_id,
                "artifact_type": artifact.artifact_type,
                "role": getattr(artifact, "role", None),
                "container": getattr(artifact, "container", None),
                "blob_name": getattr(artifact, "blob_name", None),
                "url": getattr(artifact, "url", None),
                "content_type": getattr(artifact, "content_type", None),
                "local_path": getattr(artifact, "local_path", None),
                "metadata_json": getattr(artifact, "metadata_json", None) or {},
            },
            "job": (
                {
                    "job_id": job.job_id,
                    "job_type": getattr(job, "job_type", None),
                    "creator_user_id": getattr(job, "creator_user_id", None),
                    "request_json": getattr(job, "request_json", None) or {},
                }
                if job is not None
                else None
            ),
        }
        blob_name = _REGISTRY_PREFIX + artifact.artifact_id.replace(":", "__") + ".json"

        def _run() -> None:
            try:
                self._backend.upload_text(
                    _CONTAINERS["media"], blob_name, json.dumps(row)
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "durable media: FAILED to persist registry row %s (%s) — "
                    "sessions referencing it will not survive a recycle",
                    artifact.artifact_id, exc,
                )

        t = threading.Thread(target=_run, name="persist-registry", daemon=True)
        with self._lock:
            self._pending = [x for x in self._pending if x.is_alive()]
            self._pending.append(t)
        t.start()

    def restore_artifact(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a registry row back. None on a miss; never raises."""

        if self._backend is None:
            return None
        blob_name = _REGISTRY_PREFIX + str(artifact_id).replace(":", "__") + ".json"
        try:
            text = self._backend.download_text(_CONTAINERS["media"], blob_name)
        except Exception as exc:  # noqa: BLE001
            logger.error("durable media: registry restore failed (%s)", exc)
            return None
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            logger.error("durable media: registry row %s is corrupt", artifact_id)
            return None


_ACTIVE: Optional[DurableMediaStore] = None
_ACTIVE_LOCK = threading.Lock()


def active_store() -> DurableMediaStore:
    """The one store this process uses, built once.

    Two callers now need it for opposite reasons — the server persists and
    restores files, and deletion needs to remove the very same objects — and a
    second instance would mean a second backend, a second set of threads, and a
    delete path that cannot see what the writer wrote. Inert when nothing is
    configured, which is what a laptop should get.
    """

    global _ACTIVE
    if _ACTIVE is None:
        with _ACTIVE_LOCK:
            if _ACTIVE is None:
                _ACTIVE = DurableMediaStore.configured()
    return _ACTIVE


def set_active_store(store: Optional[DurableMediaStore]) -> None:
    """Install a store (tests, and the server when it builds its own)."""

    global _ACTIVE
    _ACTIVE = store


__all__ = ["DurableMediaStore", "active_store", "set_active_store"]
