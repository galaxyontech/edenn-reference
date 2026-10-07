"""Collab-mode persistence: comment threads, participants, read receipts.

Two interchangeable repositories (duck-typed, mirroring how the router accepts
``repository=``):

- :class:`CollabRepository` — Postgres, sharing the ``PostgresClient`` wrapper
  and running migrations 001+002 on first use (002 references the sessions
  table).
- :class:`InMemoryCollabRepository` — hermetic, used by the devserver and the
  test-suite.

Threads anchor to a lineage node (candidate / proposal / source) plus an
optional time range, so feedback survives re-renders of the underlying take.
Comments are soft-deleted (the thread history stays reviewable); resolving a
thread never deletes anything.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from EdennCode.Deployment.async_pipeline_v2.models import new_id
from .pool import pooled_client
from EdennCode.Deployment.postgres_wrapper import PostgresClient

from .repositories import MIGRATION_PATH as SESSIONS_MIGRATION_PATH

USERS_MIGRATION_PATH = (
    Path(__file__).resolve().parent / "migrations" / "003_users.sql"
)
COLLAB_MIGRATION_PATH = Path(__file__).resolve().parent / "migrations" / "002_collab.sql"

# Share roles, weakest → strongest. ``iterate`` may branch/generate; ``comment``
# may join threads; ``view`` may only read. The session creator is implicitly
# ``owner`` without a participants row.
ROLE_ORDER = ("view", "comment", "iterate", "owner")


def role_at_least(role: Optional[str], minimum: str) -> bool:
    try:
        return ROLE_ORDER.index(role or "") >= ROLE_ORDER.index(minimum)
    except ValueError:
        return False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


@dataclass(frozen=True)
class CollabThread:
    thread_id: str
    session_id: str
    anchor_node_id: str
    anchor_label: Optional[str] = None
    anchor_start_s: Optional[float] = None
    anchor_end_s: Optional[float] = None
    status: str = "open"
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class CollabComment:
    comment_id: str
    thread_id: str
    session_id: str
    author_id: str
    author_name: Optional[str]
    author_kind: str
    body: str
    mentions: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    reactions: dict[str, list[str]] = field(default_factory=dict)
    edited_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class CollabParticipant:
    session_id: str
    user_id: str
    display_name: Optional[str]
    role: str
    added_by: Optional[str] = None
    added_at: Optional[datetime] = None


def thread_to_dict(thread: CollabThread) -> dict[str, Any]:
    return {
        "thread_id": thread.thread_id,
        "session_id": thread.session_id,
        "anchor_node_id": thread.anchor_node_id,
        "anchor_label": thread.anchor_label,
        "anchor_start_s": thread.anchor_start_s,
        "anchor_end_s": thread.anchor_end_s,
        "status": thread.status,
        "resolved_by": thread.resolved_by,
        "resolved_at": _iso(thread.resolved_at),
        "created_by": thread.created_by,
        "created_at": _iso(thread.created_at),
        "updated_at": _iso(thread.updated_at),
    }


def comment_to_dict(comment: CollabComment) -> dict[str, Any]:
    deleted = comment.deleted_at is not None
    return {
        "comment_id": comment.comment_id,
        "thread_id": comment.thread_id,
        "author_id": comment.author_id,
        "author_name": comment.author_name,
        "author_kind": comment.author_kind,
        # A deleted comment keeps its slot (thread history stays coherent) but
        # never leaks its text.
        "body": "" if deleted else comment.body,
        "deleted": deleted,
        "mentions": list(comment.mentions),
        "attachments": list(comment.attachments),
        "reactions": {k: list(v) for k, v in (comment.reactions or {}).items()},
        "edited_at": _iso(comment.edited_at),
        "created_at": _iso(comment.created_at),
    }


def participant_to_dict(participant: CollabParticipant) -> dict[str, Any]:
    return {
        "user_id": participant.user_id,
        "display_name": participant.display_name,
        "role": participant.role,
        "added_at": _iso(participant.added_at),
    }


def build_collab_payload(
    repo: Any, session_id: str, viewer_id: Optional[str]
) -> dict[str, Any]:
    """The one-fetch panel payload: threads (with comments + unread), people."""

    threads = repo.list_threads(session_id)
    comments = repo.list_comments(session_id)
    reads = repo.reads_for(session_id, viewer_id) if viewer_id else {}
    by_thread: dict[str, list[CollabComment]] = {}
    for comment in comments:
        by_thread.setdefault(comment.thread_id, []).append(comment)
    out_threads = []
    for thread in threads:
        thread_comments = by_thread.get(thread.thread_id, [])
        last_read = reads.get(thread.thread_id)
        unread = 0
        if viewer_id:
            unread = sum(
                1
                for c in thread_comments
                if c.author_id != viewer_id
                and c.deleted_at is None
                and (last_read is None or (c.created_at and c.created_at > last_read))
            )
        item = thread_to_dict(thread)
        item["comments"] = [comment_to_dict(c) for c in thread_comments]
        item["unread"] = unread
        out_threads.append(item)
    return {
        "threads": out_threads,
        "participants": [participant_to_dict(p) for p in repo.list_participants(session_id)],
    }


class CollabRepository:
    """Postgres-backed collab store (prod). Mirrors AgenticAudioRepository style."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], PostgresClient] = pooled_client,
    ) -> None:
        self._client_factory = client_factory
        self._ensure_lock = threading.Lock()
        self._schema_ready = False

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._ensure_lock:
            if self._schema_ready:
                return
            # The whole ordered set, through the runner: 002 has an FK on the
            # table 001 creates and 003 backfills from both, so the order is
            # load-bearing and now lives in the filenames rather than here.
            from .migrator import apply_all, discover

            apply_all(self._client_factory, discover(SESSIONS_MIGRATION_PATH.parent))
            self._schema_ready = True

    # ---- row mappers ------------------------------------------------------
    @staticmethod
    def _thread_from_row(row: dict[str, Any] | None) -> Optional[CollabThread]:
        if row is None:
            return None
        return CollabThread(
            thread_id=str(row["thread_id"]),
            session_id=str(row["session_id"]),
            anchor_node_id=str(row["anchor_node_id"]),
            anchor_label=row.get("anchor_label"),
            anchor_start_s=row.get("anchor_start_s"),
            anchor_end_s=row.get("anchor_end_s"),
            status=str(row["status"]),
            resolved_by=row.get("resolved_by"),
            resolved_at=row.get("resolved_at"),
            created_by=row.get("created_by"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    @staticmethod
    def _comment_from_row(row: dict[str, Any] | None) -> Optional[CollabComment]:
        if row is None:
            return None
        return CollabComment(
            comment_id=str(row["comment_id"]),
            thread_id=str(row["thread_id"]),
            session_id=str(row["session_id"]),
            author_id=str(row["author_id"]),
            author_name=row.get("author_name"),
            author_kind=str(row.get("author_kind") or "user"),
            body=str(row["body"]),
            mentions=list(row.get("mentions") or []),
            attachments=list(row.get("attachments") or []),
            reactions=dict(row.get("reactions") or {}),
            edited_at=row.get("edited_at"),
            deleted_at=row.get("deleted_at"),
            created_at=row.get("created_at"),
        )

    @staticmethod
    def _participant_from_row(row: dict[str, Any] | None) -> Optional[CollabParticipant]:
        if row is None:
            return None
        return CollabParticipant(
            session_id=str(row["session_id"]),
            user_id=str(row["user_id"]),
            display_name=row.get("display_name"),
            role=str(row["role"]),
            added_by=row.get("added_by"),
            added_at=row.get("added_at"),
        )

    # ---- threads ----------------------------------------------------------
    def create_thread(
        self,
        *,
        session_id: str,
        anchor_node_id: str,
        anchor_label: Optional[str] = None,
        anchor_start_s: Optional[float] = None,
        anchor_end_s: Optional[float] = None,
        created_by: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> CollabThread:
        self.ensure_schema()
        actual_id = thread_id or new_id("thread")
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO agentic_audio_comment_threads (
                    thread_id, session_id, anchor_node_id, anchor_label,
                    anchor_start_s, anchor_end_s, created_by
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                params=[
                    actual_id,
                    session_id,
                    anchor_node_id,
                    anchor_label,
                    anchor_start_s,
                    anchor_end_s,
                    created_by,
                ],
            )
        thread = self._thread_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if thread is None:
            raise KeyError(actual_id)
        return thread

    def get_thread(self, thread_id: str) -> Optional[CollabThread]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                "SELECT * FROM agentic_audio_comment_threads WHERE thread_id = %s LIMIT 1",
                params=[thread_id],
            )
        return self._thread_from_row(rows[0] if isinstance(rows, list) and rows else None)

    def list_threads(self, session_id: str) -> list[CollabThread]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT * FROM agentic_audio_comment_threads
                WHERE session_id = %s
                ORDER BY created_at, thread_id
                """,
                params=[session_id],
            )
        return [
            thread
            for row in (rows if isinstance(rows, list) else [])
            if (thread := self._thread_from_row(row)) is not None
        ]

    def update_thread_status(
        self, thread_id: str, *, status: str, resolved_by: Optional[str] = None
    ) -> CollabThread:
        self.ensure_schema()
        resolved = status == "resolved"
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                UPDATE agentic_audio_comment_threads
                SET status = %s,
                    resolved_by = CASE WHEN %s THEN %s ELSE NULL END,
                    resolved_at = CASE WHEN %s THEN now() ELSE NULL END,
                    updated_at = now()
                WHERE thread_id = %s
                RETURNING *
                """,
                params=[status, resolved, resolved_by, resolved, thread_id],
            )
        thread = self._thread_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if thread is None:
            raise KeyError(thread_id)
        return thread

    # ---- comments ---------------------------------------------------------
    def add_comment(
        self,
        *,
        thread_id: str,
        session_id: str,
        author_id: str,
        body: str,
        author_name: Optional[str] = None,
        author_kind: str = "user",
        mentions: Optional[list[dict[str, Any]]] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
        comment_id: Optional[str] = None,
    ) -> CollabComment:
        self.ensure_schema()
        actual_id = comment_id or new_id("comment")
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO agentic_audio_comments (
                    comment_id, thread_id, session_id, author_id, author_name,
                    author_kind, body, mentions, attachments
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                params=[
                    actual_id,
                    thread_id,
                    session_id,
                    author_id,
                    author_name,
                    author_kind,
                    body,
                    mentions or [],
                    attachments or [],
                ],
            )
            client.run_sql(
                "UPDATE agentic_audio_comment_threads SET updated_at = now() WHERE thread_id = %s",
                params=[thread_id],
            )
        comment = self._comment_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if comment is None:
            raise KeyError(actual_id)
        return comment

    def get_comment(self, comment_id: str) -> Optional[CollabComment]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                "SELECT * FROM agentic_audio_comments WHERE comment_id = %s LIMIT 1",
                params=[comment_id],
            )
        return self._comment_from_row(rows[0] if isinstance(rows, list) and rows else None)

    def list_comments(self, session_id: str) -> list[CollabComment]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT * FROM agentic_audio_comments
                WHERE session_id = %s
                ORDER BY created_at, comment_id
                """,
                params=[session_id],
            )
        return [
            comment
            for row in (rows if isinstance(rows, list) else [])
            if (comment := self._comment_from_row(row)) is not None
        ]

    def edit_comment(self, comment_id: str, *, body: str) -> CollabComment:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                UPDATE agentic_audio_comments
                SET body = %s, edited_at = now()
                WHERE comment_id = %s AND deleted_at IS NULL
                RETURNING *
                """,
                params=[body, comment_id],
            )
        comment = self._comment_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if comment is None:
            raise KeyError(comment_id)
        return comment

    def delete_comment(self, comment_id: str) -> CollabComment:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                UPDATE agentic_audio_comments
                SET deleted_at = now()
                WHERE comment_id = %s
                RETURNING *
                """,
                params=[comment_id],
            )
        comment = self._comment_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if comment is None:
            raise KeyError(comment_id)
        return comment

    def set_reaction(
        self, comment_id: str, *, emoji: str, user_id: str, on: bool
    ) -> CollabComment:
        self.ensure_schema()
        comment = self.get_comment(comment_id)
        if comment is None:
            raise KeyError(comment_id)
        reactions = {k: list(v) for k, v in (comment.reactions or {}).items()}
        users = reactions.setdefault(emoji, [])
        if on and user_id not in users:
            users.append(user_id)
        if not on and user_id in users:
            users.remove(user_id)
        if not users:
            reactions.pop(emoji, None)
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                UPDATE agentic_audio_comments
                SET reactions = %s
                WHERE comment_id = %s
                RETURNING *
                """,
                params=[reactions, comment_id],
            )
        updated = self._comment_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if updated is None:
            raise KeyError(comment_id)
        return updated

    # ---- participants -----------------------------------------------------
    # ---- users ------------------------------------------------------------

    def delete_session(self, session_id: str) -> bool:
        for key in [k for k in self.participants if k[0] == session_id]:
            self.participants.pop(key, None)
        for tid in [t for t, th in getattr(self, "threads", {}).items()
                    if getattr(th, "session_id", None) == session_id]:
            self.threads.pop(tid, None)
        return True

    def touch_user(
        self,
        user_id: str,
        *,
        auth_source: str = "identity",
        display_name: str = "",
        account_id: Optional[str] = None,
    ) -> None:
        """Record that this principal exists and was just seen.

        Called on authentication, so the table fills itself as people arrive
        rather than needing a separate registration step — there is no sign-up
        of our own to hang one on.

        A display name supplied here NEVER overwrites one the person set: the
        identity provider's name is a default, not an override.

        ``account_id`` follows the same rule for a different reason: ``None``
        means the lookup had no answer — nobody has signed up yet, or the index
        could not be reached — and that must not erase an account this row has
        already been told about. An absent answer is not a correction.
        """
        self.ensure_schema()
        with self._client_factory() as client:
            client.run_sql(
                """
                INSERT INTO agentic_audio_users (
                    user_id, auth_source, display_name, account_id
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET last_seen_at = now(),
                    auth_source = EXCLUDED.auth_source,
                    display_name = CASE
                        WHEN agentic_audio_users.display_name = ''
                        THEN EXCLUDED.display_name
                        ELSE agentic_audio_users.display_name
                    END,
                    account_id = COALESCE(
                        EXCLUDED.account_id, agentic_audio_users.account_id
                    )
                """,
                params=[user_id, auth_source, display_name or "", account_id or None],
            )

    def get_user(self, user_id: str) -> Optional[dict[str, Any]]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                "SELECT * FROM agentic_audio_users WHERE user_id = %s LIMIT 1",
                params=[user_id],
            )
        if not (isinstance(rows, list) and rows):
            return None
        row = dict(rows[0])
        return {
            "user_id": row.get("user_id"),
            "display_name": row.get("display_name") or "",
            "auth_source": row.get("auth_source") or "identity",
            # None until the uid is linked to a platform account. Never "",
            # because an empty string reads as an account whose id is blank.
            "account_id": row.get("account_id") or None,
        }

    def set_display_name(self, user_id: str, display_name: str) -> None:
        self.ensure_schema()
        with self._client_factory() as client:
            client.run_sql(
                """
                INSERT INTO agentic_audio_users (user_id, display_name)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET display_name = EXCLUDED.display_name
                """,
                params=[user_id, display_name],
            )

    def delete_user(self, user_id: str) -> bool:
        """Erase the account record and every collaboration it took part in.

        Sessions the person OWNS are not touched here — that is a product
        decision the caller makes explicitly, because deleting an account and
        destroying work a team still depends on are not the same act.
        """
        self.ensure_schema()
        with self._client_factory() as client:
            client.run_sql(
                "DELETE FROM agentic_audio_participants WHERE user_id = %s",
                params=[user_id],
            )
            rows = client.run_sql(
                "DELETE FROM agentic_audio_users WHERE user_id = %s RETURNING user_id",
                params=[user_id],
            )
        return bool(isinstance(rows, list) and rows)

    def upsert_participant(
        self,
        *,
        session_id: str,
        user_id: str,
        role: str,
        display_name: Optional[str] = None,
        added_by: Optional[str] = None,
    ) -> CollabParticipant:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO agentic_audio_participants (
                    session_id, user_id, display_name, role, added_by
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (session_id, user_id)
                DO UPDATE SET role = EXCLUDED.role,
                              display_name = COALESCE(EXCLUDED.display_name,
                                                      agentic_audio_participants.display_name)
                RETURNING *
                """,
                params=[session_id, user_id, display_name, role, added_by],
            )
        participant = self._participant_from_row(
            rows[0] if isinstance(rows, list) and rows else None
        )
        if participant is None:
            raise KeyError(user_id)
        return participant

    def get_participant(self, session_id: str, user_id: str) -> Optional[CollabParticipant]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT * FROM agentic_audio_participants
                WHERE session_id = %s AND user_id = %s LIMIT 1
                """,
                params=[session_id, user_id],
            )
        return self._participant_from_row(rows[0] if isinstance(rows, list) and rows else None)

    def remove_participant(self, session_id: str, user_id: str) -> bool:
        """Take a collaborator's access away. True when a row was removed.

        Their comments stay: a thread with the replies deleted out of it is a
        conversation nobody can follow, and removal is about future access, not
        rewriting what was said.
        """
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                DELETE FROM agentic_audio_participants
                WHERE session_id = %s AND user_id = %s
                RETURNING user_id
                """,
                params=[session_id, user_id],
            )
        return bool(isinstance(rows, list) and rows)

    def sessions_for_user(self, user_id: str) -> list[str]:
        """Session ids shared WITH this user (any role) — backs the shared rows
        of the History/Sessions views."""
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT session_id FROM agentic_audio_participants
                WHERE user_id = %s
                ORDER BY added_at
                """,
                params=[user_id],
            )
        return [str(r["session_id"]) for r in rows] if isinstance(rows, list) else []

    def list_participants(self, session_id: str) -> list[CollabParticipant]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT * FROM agentic_audio_participants
                WHERE session_id = %s
                ORDER BY added_at, user_id
                """,
                params=[session_id],
            )
        return [
            participant
            for row in (rows if isinstance(rows, list) else [])
            if (participant := self._participant_from_row(row)) is not None
        ]

    # ---- read receipts ----------------------------------------------------
    def mark_read(self, thread_id: str, user_id: str) -> None:
        self.ensure_schema()
        with self._client_factory() as client:
            client.run_sql(
                """
                INSERT INTO agentic_audio_thread_reads (thread_id, user_id, last_read_at)
                VALUES (%s, %s, now())
                ON CONFLICT (thread_id, user_id) DO UPDATE SET last_read_at = now()
                """,
                params=[thread_id, user_id],
            )

    def reads_for(self, session_id: str, user_id: str) -> dict[str, datetime]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT r.thread_id, r.last_read_at
                FROM agentic_audio_thread_reads r
                JOIN agentic_audio_comment_threads t ON t.thread_id = r.thread_id
                WHERE t.session_id = %s AND r.user_id = %s
                """,
                params=[session_id, user_id],
            )
        return {
            str(row["thread_id"]): row["last_read_at"]
            for row in (rows if isinstance(rows, list) else [])
            if row.get("last_read_at") is not None
        }


class InMemoryCollabRepository:
    """Hermetic collab store for the devserver and tests. Same duck type."""

    def __init__(self) -> None:
        self.threads: dict[str, CollabThread] = {}
        self.comments: dict[str, CollabComment] = {}
        self.participants: dict[tuple[str, str], CollabParticipant] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.reads: dict[tuple[str, str], datetime] = {}

    def ensure_schema(self) -> None:  # symmetry with the pg repo
        return

    def create_thread(self, **kwargs: Any) -> CollabThread:
        thread = CollabThread(
            thread_id=kwargs.get("thread_id") or new_id("thread"),
            session_id=kwargs["session_id"],
            anchor_node_id=kwargs["anchor_node_id"],
            anchor_label=kwargs.get("anchor_label"),
            anchor_start_s=kwargs.get("anchor_start_s"),
            anchor_end_s=kwargs.get("anchor_end_s"),
            created_by=kwargs.get("created_by"),
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        self.threads[thread.thread_id] = thread
        return thread

    def get_thread(self, thread_id: str) -> Optional[CollabThread]:
        return self.threads.get(thread_id)

    def list_threads(self, session_id: str) -> list[CollabThread]:
        rows = [t for t in self.threads.values() if t.session_id == session_id]
        rows.sort(key=lambda t: (t.created_at or _utcnow(), t.thread_id))
        return rows

    def update_thread_status(
        self, thread_id: str, *, status: str, resolved_by: Optional[str] = None
    ) -> CollabThread:
        thread = self.threads.get(thread_id)
        if thread is None:
            raise KeyError(thread_id)
        resolved = status == "resolved"
        updated = replace(
            thread,
            status=status,
            resolved_by=resolved_by if resolved else None,
            resolved_at=_utcnow() if resolved else None,
            updated_at=_utcnow(),
        )
        self.threads[thread_id] = updated
        return updated

    def add_comment(self, **kwargs: Any) -> CollabComment:
        thread_id = kwargs["thread_id"]
        if thread_id not in self.threads:
            raise KeyError(thread_id)
        comment = CollabComment(
            comment_id=kwargs.get("comment_id") or new_id("comment"),
            thread_id=thread_id,
            session_id=kwargs["session_id"],
            author_id=kwargs["author_id"],
            author_name=kwargs.get("author_name"),
            author_kind=kwargs.get("author_kind") or "user",
            body=kwargs["body"],
            mentions=list(kwargs.get("mentions") or []),
            attachments=list(kwargs.get("attachments") or []),
            created_at=_utcnow(),
        )
        self.comments[comment.comment_id] = comment
        self.threads[thread_id] = replace(self.threads[thread_id], updated_at=_utcnow())
        return comment

    def get_comment(self, comment_id: str) -> Optional[CollabComment]:
        return self.comments.get(comment_id)

    def list_comments(self, session_id: str) -> list[CollabComment]:
        rows = [c for c in self.comments.values() if c.session_id == session_id]
        rows.sort(key=lambda c: (c.created_at or _utcnow(), c.comment_id))
        return rows

    def edit_comment(self, comment_id: str, *, body: str) -> CollabComment:
        comment = self.comments.get(comment_id)
        if comment is None or comment.deleted_at is not None:
            raise KeyError(comment_id)
        updated = replace(comment, body=body, edited_at=_utcnow())
        self.comments[comment_id] = updated
        return updated

    def delete_comment(self, comment_id: str) -> CollabComment:
        comment = self.comments.get(comment_id)
        if comment is None:
            raise KeyError(comment_id)
        updated = replace(comment, deleted_at=_utcnow())
        self.comments[comment_id] = updated
        return updated

    def set_reaction(
        self, comment_id: str, *, emoji: str, user_id: str, on: bool
    ) -> CollabComment:
        comment = self.comments.get(comment_id)
        if comment is None:
            raise KeyError(comment_id)
        reactions = {k: list(v) for k, v in (comment.reactions or {}).items()}
        users = reactions.setdefault(emoji, [])
        if on and user_id not in users:
            users.append(user_id)
        if not on and user_id in users:
            users.remove(user_id)
        if not users:
            reactions.pop(emoji, None)
        updated = replace(comment, reactions=reactions)
        self.comments[comment_id] = updated
        return updated

    def upsert_participant(self, **kwargs: Any) -> CollabParticipant:
        key = (kwargs["session_id"], kwargs["user_id"])
        existing = self.participants.get(key)
        participant = CollabParticipant(
            session_id=kwargs["session_id"],
            user_id=kwargs["user_id"],
            display_name=kwargs.get("display_name")
            or (existing.display_name if existing else None),
            role=kwargs["role"],
            added_by=kwargs.get("added_by"),
            added_at=existing.added_at if existing else _utcnow(),
        )
        self.participants[key] = participant
        return participant

    def touch_user(
        self,
        user_id: str,
        *,
        auth_source: str = "identity",
        display_name: str = "",
        account_id: Optional[str] = None,
    ) -> None:
        existing = self.users.get(user_id) or {}
        self.users[user_id] = {
            "user_id": user_id,
            "auth_source": auth_source,
            # An identity's name is a default; a name the person chose wins.
            "display_name": existing.get("display_name") or (display_name or ""),
            # An absent answer never erases a known account.
            "account_id": account_id or existing.get("account_id") or None,
        }

    def get_user(self, user_id: str) -> Optional[dict[str, Any]]:
        return self.users.get(user_id)

    def set_display_name(self, user_id: str, display_name: str) -> None:
        row = self.users.setdefault(
            user_id, {"user_id": user_id, "auth_source": "identity", "display_name": ""}
        )
        row["display_name"] = display_name

    def delete_user(self, user_id: str) -> bool:
        for key in [k for k in self.participants if k[1] == user_id]:
            self.participants.pop(key, None)
        return self.users.pop(user_id, None) is not None

    def get_participant(self, session_id: str, user_id: str) -> Optional[CollabParticipant]:
        return self.participants.get((session_id, user_id))

    def remove_participant(self, session_id: str, user_id: str) -> bool:
        return self.participants.pop((session_id, user_id), None) is not None

    def sessions_for_user(self, user_id: str) -> list[str]:
        return [sid for (sid, uid) in self.participants.keys() if uid == user_id]

    def list_participants(self, session_id: str) -> list[CollabParticipant]:
        rows = [p for p in self.participants.values() if p.session_id == session_id]
        rows.sort(key=lambda p: (p.added_at or _utcnow(), p.user_id))
        return rows

    def mark_read(self, thread_id: str, user_id: str) -> None:
        self.reads[(thread_id, user_id)] = _utcnow()

    def reads_for(self, session_id: str, user_id: str) -> dict[str, datetime]:
        return {
            thread_id: at
            for (thread_id, uid), at in self.reads.items()
            if uid == user_id
            and (t := self.threads.get(thread_id)) is not None
            and t.session_id == session_id
        }


__all__ = [
    "COLLAB_MIGRATION_PATH",
    "CollabComment",
    "CollabParticipant",
    "CollabRepository",
    "CollabThread",
    "InMemoryCollabRepository",
    "ROLE_ORDER",
    "build_collab_payload",
    "comment_to_dict",
    "participant_to_dict",
    "role_at_least",
    "thread_to_dict",
]
