"""Deleting what we said we would delete.

The selection half of retention has existed for a while and the deleting half
was deliberately absent, because an interval is a product decision and inventing
one in code is how a product ends up quietly keeping people's footage forever.
The interval is now set: **7 days** since a session was last touched.

Three properties, and each of them is there because of how retention jobs
usually go wrong:

* **Dry run by default.** ``sweep()`` reports what it would delete and deletes
  nothing unless asked. The first thing anyone should do with a deletion job is
  watch it not delete anything.
* **Bounded.** One pass removes at most ``limit`` sessions. A retention job that
  discovers a year of backlog and tries to remove it in one transaction is an
  outage, not a cleanup.
* **Audited.** Every deletion is recorded as a destructive action by the actor
  ``retention``, so a user asking "where did my session go" gets an answer
  rather than a shrug.

What it deletes is the session row and everything the schema cascades from it —
messages, tool calls, participants, comments — **and the stored media those
renders produced**. The media half used to be missing, and that gap was the
whole promise: a person told their footage was deleted, whose footage is still
sitting in a container, has been told something untrue, and the bytes go on
costing money for as long as nobody looks.

Order matters and is deliberate: **objects first, the session row last**. The
row is the only thing that remembers the object names — artifacts cascade from
jobs and jobs cascade from the session — so deleting it first strands bytes
nobody can ever name again. Dying part-way through the other order leaves the
row intact with some media already gone, which the next pass simply finishes:
the session is a week stale and on its way out regardless.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from ..api import audit

logger = logging.getLogger(__name__)

# The interval, set by the product rather than guessed at by this module.
DEFAULT_RETENTION_DAYS = 7

# One pass is a bounded amount of work. A job that finds a year of backlog and
# tries to clear it in one go takes the database down with it.
DEFAULT_LIMIT = 200


def retention_days() -> int:
    try:
        value = int(os.getenv("AGENTIC_AUDIO_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    # Zero or negative would mean "delete everything, now". If somebody wants
    # that they can pass an explicit day count; they cannot get it from a typo.
    return value if value >= 1 else DEFAULT_RETENTION_DAYS


@dataclass
class SweepResult:
    """What a pass did, or would have done."""

    considered: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    #: Stored objects removed, and the ones that would not go. The second list
    #: is the important one: it is the exact set for which the promise is not
    #: yet kept, by name, so somebody can finish the job by hand.
    media_deleted: int = 0
    media_failed: list[str] = field(default_factory=list)
    #: True when there was media to remove and no configured way to remove it.
    #: Reported rather than silently counted as success.
    media_unsupported: bool = False
    dry_run: bool = True
    days: int = DEFAULT_RETENTION_DAYS

    @property
    def summary(self) -> str:
        verb = "would delete" if self.dry_run else "deleted"
        note = f", {len(self.failed)} failed" if self.failed else ""
        if not self.dry_run:
            note += f", {self.media_deleted} media object(s) removed"
            if self.media_failed:
                note += f", {len(self.media_failed)} NOT removed"
            if self.media_unsupported:
                note += ", media store cannot delete (objects left behind)"
        return (
            f"retention({self.days}d): {len(self.considered)} eligible, "
            f"{verb} {len(self.deleted) if not self.dry_run else len(self.considered)}{note}"
        )


def source_upload_refs(
    session_id: str,
    *,
    repository: Any = None,
    job_repository: Any = None,
) -> list[tuple[str, str]]:
    """The uploaded video this session was made from — if only this session has it.

    The footage is the object the promise is most about, and it is NOT reachable
    from the session's renders: an upload is staged before any session exists,
    so its job row carries no session id. It has to be found through the
    session's own ``source_video_artifact_id``.

    Returns nothing when another session was started from the same upload.
    Removing it then would reach past what the user asked to delete and break
    somebody else's work.
    """

    if repository is None or job_repository is None:
        return []
    try:
        session = repository.get_session(session_id)
        artifact_id = str(getattr(session, "source_video_artifact_id", "") or "")
        if not artifact_id:
            return []
        if repository.sessions_sharing_source(artifact_id, excluding=session_id):
            logger.info(
                "retention: leaving the upload for %s in place — another "
                "session was made from it",
                session_id,
            )
            return []
        artifact = job_repository.get_artifact(artifact_id)
        container = str(getattr(artifact, "container", "") or "")
        blob_name = str(getattr(artifact, "blob_name", "") or "")
        return [(container, blob_name)] if container and blob_name else []
    except Exception:  # noqa: BLE001 - never let bookkeeping stop a deletion
        logger.warning(
            "retention: could not resolve the upload for %s", session_id, exc_info=True
        )
        return []


def delete_session_media(
    session_id: str,
    *,
    job_repository: Any = None,
    media_store: Any = None,
    extra_refs: tuple[tuple[str, str], ...] = (),
) -> tuple[int, list[str], bool]:
    """Remove the stored objects a session's renders produced.

    Returns ``(removed, failed_names, unsupported)``. Call it BEFORE the session
    row is deleted — the names live on artifact rows that cascade away with it.

    Never raises. A retention pass that dies on one unreachable object leaves
    every session after it in the backlog undeleted, which turns a storage
    problem into a promise problem.
    """

    if job_repository is None or media_store is None:
        return 0, [], False
    try:
        refs = list(job_repository.session_media_refs(session_id))
    except Exception:  # noqa: BLE001
        logger.warning(
            "retention: could not list media for %s", session_id, exc_info=True
        )
        refs = []
    for ref in extra_refs:
        if ref not in refs:
            refs.append(ref)
    if not refs:
        return 0, [], False
    if not getattr(media_store, "can_delete", False):
        # Say it out loud. "0 removed" and "there is no delete" look identical
        # in a report and mean opposite things.
        logger.warning(
            "retention: %d stored object(s) for %s are being left behind — the "
            "media store has no delete",
            len(refs), session_id,
        )
        return 0, [str(name) for _, name in refs], True
    removed, failed = media_store.delete_many(refs)
    if failed:
        logger.error(
            "retention: %d object(s) for %s were NOT removed: %s",
            len(failed), session_id, ", ".join(failed[:10]),
        )
    return removed, failed, False


def sweep(
    repository: Any,
    *,
    days: Optional[int] = None,
    limit: int = DEFAULT_LIMIT,
    apply: bool = False,
    job_repository: Any = None,
    media_store: Any = None,
) -> SweepResult:
    """One retention pass.

    ``apply=False`` (the default) reports and changes nothing.

    ``job_repository`` and ``media_store`` are what make the promise real; pass
    both and the pass removes the stored media too. Omitted, it behaves exactly
    as it always did and says so in the report rather than implying the objects
    are gone.
    """

    window = int(days if days is not None else retention_days())
    result = SweepResult(dry_run=not apply, days=window)
    result.considered = list(repository.sessions_older_than(window, limit=limit))
    if not apply or not result.considered:
        logger.info("%s", result.summary)
        return result

    for session_id in result.considered:
        # Names first: they are gone the moment the row is.
        media_removed, media_failed, unsupported = delete_session_media(
            session_id,
            job_repository=job_repository,
            media_store=media_store,
            extra_refs=tuple(
                source_upload_refs(
                    session_id, repository=repository, job_repository=job_repository
                )
            ),
        )
        result.media_deleted += media_removed
        result.media_failed.extend(media_failed)
        result.media_unsupported = result.media_unsupported or unsupported
        try:
            removed = repository.delete_session(session_id)
        except Exception:  # noqa: BLE001 - one bad row must not stop the pass
            logger.warning("retention: could not delete %s", session_id, exc_info=True)
            result.failed.append(session_id)
            continue
        if removed:
            result.deleted.append(session_id)
            audit.record(
                "session.deleted",
                kind=audit.DESTRUCTIVE,
                actor="retention",
                session_id=session_id,
                detail={"reason": "retention", "days": window},
            )
    logger.info("%s", result.summary)
    return result


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_RETENTION_DAYS",
    "SweepResult",
    "delete_session_media",
    "retention_days",
    "source_upload_refs",
    "sweep",
]
