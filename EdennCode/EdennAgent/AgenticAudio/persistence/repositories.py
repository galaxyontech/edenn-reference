from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from EdennCode.Deployment.async_pipeline_v2.models import new_id
from .pool import pooled_client
from EdennCode.Deployment.postgres_wrapper import PostgresClient

from ..models import (
    AgenticAudioChoice,
    AgenticAudioMessage,
    AgenticAudioSession,
    AgenticAudioSessionSnapshot,
    AgenticAudioToolCall,
    AgenticSessionStatus,
)


VERSION_MIGRATION_PATH = (
    Path(__file__).resolve().parent / "migrations" / "004_session_version.sql"
)
MIGRATION_PATH = Path(__file__).resolve().parent / "migrations" / "001_agentic_audio.sql"


class StaleSessionState(RuntimeError):
    """A write was refused because the row moved since the caller read it.

    Raised only for callers that opt into compare-and-set by passing the version
    they read. It is the loud version of the silent lost update: the write did
    not land, and the caller can re-read and decide what to do.
    """


class AgenticAudioRepository:
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
            # Applied once each, in filename order, with one replica holding an
            # advisory lock — see persistence/migrator.py. This used to replay
            # every file on every cold start, which is why the schema could only
            # ever contain statements that survive being run twice.
            from .migrator import apply_all, discover

            apply_all(self._client_factory, discover(MIGRATION_PATH.parent))
            self._schema_ready = True

    @contextmanager
    def _client_context(
        self,
        client: Optional[PostgresClient] = None,
    ) -> Iterator[PostgresClient]:
        if client is not None:
            yield client
            return
        with self._client_factory() as created:
            yield created

    @staticmethod
    def _session_from_row(row: dict[str, Any] | None) -> Optional[AgenticAudioSession]:
        if row is None:
            return None
        return AgenticAudioSession(
            session_id=str(row["session_id"]),
            source_video_artifact_id=str(row["source_video_artifact_id"]),
            creator_user_id=row.get("creator_user_id"),
            status=str(row["status"]),
            phase=str(row["phase"]),
            selected_candidate_id=row.get("selected_candidate_id"),
            linked_job_ids=list(row.get("linked_job_ids") or []),
            state_json=dict(row.get("state_json") or {}),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            finished_at=row.get("finished_at"),
            version=int(row.get("version") or 0),
        )

    @staticmethod
    def _message_from_row(row: dict[str, Any] | None) -> Optional[AgenticAudioMessage]:
        if row is None:
            return None
        return AgenticAudioMessage(
            message_id=str(row["message_id"]),
            session_id=str(row["session_id"]),
            role=str(row["role"]),
            content=str(row["content"]),
            payload_json=dict(row.get("payload_json") or {}),
            created_at=row.get("created_at"),
        )

    @staticmethod
    def _tool_call_from_row(row: dict[str, Any] | None) -> Optional[AgenticAudioToolCall]:
        if row is None:
            return None
        return AgenticAudioToolCall(
            tool_call_id=str(row["tool_call_id"]),
            session_id=str(row["session_id"]),
            tool_name=str(row["tool_name"]),
            status=str(row["status"]),
            input_json=dict(row.get("input_json") or {}),
            output_json=row.get("output_json"),
            error_json=row.get("error_json"),
            linked_job_id=row.get("linked_job_id"),
            linked_artifact_ids=list(row.get("linked_artifact_ids") or []),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            finished_at=row.get("finished_at"),
        )

    @staticmethod
    def _choice_from_row(row: dict[str, Any] | None) -> Optional[AgenticAudioChoice]:
        if row is None:
            return None
        return AgenticAudioChoice(
            choice_id=str(row["choice_id"]),
            session_id=str(row["session_id"]),
            choice_type=str(row["choice_type"]),
            target_id=str(row["target_id"]),
            payload_json=dict(row.get("payload_json") or {}),
            created_at=row.get("created_at"),
        )

    def create_session(
        self,
        *,
        source_video_artifact_id: str,
        creator_user_id: Optional[str] = None,
        phase: str,
        state_json: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> AgenticAudioSession:
        self.ensure_schema()
        actual_session_id = session_id or new_id("agent_session")
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO agentic_audio_sessions (
                    session_id, source_video_artifact_id, creator_user_id,
                    status, phase, state_json
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                params=[
                    actual_session_id,
                    source_video_artifact_id,
                    creator_user_id,
                    AgenticSessionStatus.ACTIVE,
                    phase,
                    state_json or {},
                ],
            )
        session = self._session_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if session is None:
            raise KeyError(actual_session_id)
        return session

    def get_session(self, session_id: str) -> Optional[AgenticAudioSession]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                "SELECT * FROM agentic_audio_sessions WHERE session_id = %s LIMIT 1",
                params=[session_id],
            )
        return self._session_from_row(rows[0] if isinstance(rows, list) and rows else None)

    def ping(self) -> None:
        """One cheap question: is the store actually reachable?

        Readiness read in-process flags only, so a replica whose database had
        gone away went on reporting itself ready and kept being sent traffic it
        could not serve. Raises on failure — the caller decides what that means.
        """

        with self._client_factory() as client:
            client.run_sql("SELECT 1")

    def sessions_sharing_source(self, artifact_id: str, *, excluding: str = "") -> int:
        """How many OTHER sessions were made from the same uploaded video.

        Deleting a session is supposed to take the customer's footage with it,
        and that footage is a single stored object. Two sessions can be started
        from one upload, so deleting the object because one of them went would
        break the other — a delete that reaches past what the user asked to
        delete. Zero here is the only safe answer.
        """

        clean = (artifact_id or "").strip()
        if not clean:
            return 0
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT COUNT(*) AS n FROM agentic_audio_sessions
                WHERE source_video_artifact_id = %s AND session_id <> %s
                """,
                params=[clean, excluding or ""],
            )
        if isinstance(rows, list) and rows:
            return int(rows[0].get("n") or 0)
        return 0

    def list_sessions(
        self, *, creator_user_id: Optional[str] = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Lightweight list of sessions (most-recent first), optionally filtered by
        creator — backs the History view. Returns dicts, not full snapshots."""

        self.ensure_schema()
        where = "WHERE creator_user_id = %s" if creator_user_id else ""
        params: list[Any] = ([creator_user_id] if creator_user_id else []) + [int(limit)]
        with self._client_factory() as client:
            rows = client.run_sql(
                f"""
                SELECT session_id, status, phase, creator_user_id,
                       source_video_artifact_id, created_at, updated_at,
                       state_json->'observation'->>'video_title' AS title,
                       COALESCE(state_json->'final_artifact'->>'video_url',
                                state_json->'final_artifact'->>'audio_url') AS final_media_url
                FROM agentic_audio_sessions
                {where}
                ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                LIMIT %s
                """,
                params=params,
            )
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            updated = row.get("updated_at")
            out.append(
                {
                    "session_id": str(row["session_id"]),
                    "status": str(row["status"]),
                    "phase": str(row["phase"]),
                    "creator_user_id": row.get("creator_user_id"),
                    "source_video_artifact_id": str(row["source_video_artifact_id"]),
                    "updated_at": updated.isoformat() if updated else None,
                    # Display extras for the entrance Sessions/Gallery views: the
                    # analysis title and the finished deliverable (when one exists).
                    "title": row.get("title"),
                    "final_media_url": row.get("final_media_url"),
                }
            )
        return out

    def update_session(
        self,
        session_id: str,
        *,
        phase: Optional[str] = None,
        status: Optional[str] = None,
        selected_candidate_id: Optional[str] = None,
        linked_job_ids: Optional[list[str]] = None,
        state_json: Optional[dict[str, Any]] = None,
        finished: bool = False,
        expected_version: Optional[int] = None,
        client: Optional[PostgresClient] = None,
    ) -> AgenticAudioSession:
        """Write named fields.

        ``state_json`` here is a BLIND overwrite of the whole document, which is
        only safe when the caller holds the turn lock or genuinely owns the
        whole document. For read-modify-write — which is what almost every
        caller is really doing — use :meth:`mutate_session_state`, which holds
        the row for the read as well as the write.

        ``expected_version`` turns this into compare-and-set: the write lands
        only if the row is still the one the caller read, and raises
        :class:`StaleSessionState` otherwise.
        """
        self.ensure_schema()
        with self._client_context(client) as active_client:
            rows = active_client.run_sql(
                """
                UPDATE agentic_audio_sessions
                SET phase = COALESCE(%s, phase),
                    status = COALESCE(%s, status),
                    selected_candidate_id = COALESCE(%s, selected_candidate_id),
                    linked_job_ids = COALESCE(%s, linked_job_ids),
                    state_json = COALESCE(%s, state_json),
                    version = version + 1,
                    updated_at = now(),
                    finished_at = CASE WHEN %s THEN now() ELSE finished_at END
                WHERE session_id = %s
                  AND (%s::bigint IS NULL OR version = %s::bigint)
                RETURNING *
                """,
                params=[
                    phase,
                    status,
                    selected_candidate_id,
                    linked_job_ids,
                    state_json,
                    finished,
                    session_id,
                    expected_version,
                    expected_version,
                ],
            )
        session = self._session_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if session is None:
            if expected_version is not None and self.get_session(session_id) is not None:
                raise StaleSessionState(
                    f"session {session_id} moved past version {expected_version}"
                )
            raise KeyError(session_id)
        return session

    def delete_session(self, session_id: str) -> bool:
        """Remove a session and everything that hangs off it.

        Messages, tool calls, candidates, comment threads and participants all
        carry ON DELETE CASCADE, so one delete takes the lot. That is the point:
        a partial delete leaves a person believing their footage is gone while
        the transcript that describes it is still there.
        """
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                "DELETE FROM agentic_audio_sessions WHERE session_id = %s "
                "RETURNING session_id",
                params=[session_id],
            )
        return bool(isinstance(rows, list) and rows)

    def sessions_older_than(self, days: int, *, limit: int = 500) -> list[str]:
        """Ids of sessions last touched more than ``days`` ago.

        The selection half of a retention policy, kept separate from the
        deleting half on purpose: a policy nobody can inspect before it runs is
        a policy nobody will trust enough to turn on.
        """
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT session_id FROM agentic_audio_sessions
                WHERE updated_at < now() - make_interval(days => %s)
                ORDER BY updated_at
                LIMIT %s
                """,
                params=[int(days), int(limit)],
            )
        return [str(r["session_id"]) for r in (rows if isinstance(rows, list) else [])]

    def mutate_session_state(
        self,
        session_id: str,
        mutate: Callable[[dict[str, Any]], Optional[dict[str, Any]]],
    ) -> AgenticAudioSession:
        """Read-modify-write ``state_json`` with the row held.

        A read-modify-write cannot be made safe from the outside — the read and
        the write have to happen in the same critical section — so this is a
        repository primitive rather than a rule callers are asked to follow.

        ``mutate`` receives the CURRENT document and returns the new one, or
        ``None`` to mean "nothing to do", which costs no write and no version
        bump: on the poll path, "I looked and everything was already hydrated"
        is the common case.

        If ``mutate`` raises, the transaction rolls back and the row is exactly
        as it was.
        """
        self.ensure_schema()
        with self._client_factory() as client:
            with client.transaction() as tx:
                rows = tx.run_sql(
                    """
                    SELECT * FROM agentic_audio_sessions
                    WHERE session_id = %s
                    FOR UPDATE
                    """,
                    params=[session_id],
                )
                current = self._session_from_row(
                    rows[0] if isinstance(rows, list) and rows else None
                )
                if current is None:
                    raise KeyError(session_id)

                updated = mutate(dict(current.state_json or {}))
                if updated is None:
                    return current

                written = tx.run_sql(
                    """
                    UPDATE agentic_audio_sessions
                    SET state_json = %s,
                        version = version + 1,
                        updated_at = now()
                    WHERE session_id = %s
                    RETURNING *
                    """,
                    params=[updated, session_id],
                )
        session = self._session_from_row(
            written[0] if isinstance(written, list) and written else None
        )
        if session is None:
            raise KeyError(session_id)
        return session

    def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        payload_json: Optional[dict[str, Any]] = None,
        message_id: Optional[str] = None,
    ) -> AgenticAudioMessage:
        self.ensure_schema()
        actual_message_id = message_id or new_id("agent_msg")
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO agentic_audio_messages (
                    message_id, session_id, role, content, payload_json
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                params=[
                    actual_message_id,
                    session_id,
                    role,
                    content,
                    payload_json or {},
                ],
            )
        message = self._message_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if message is None:
            raise KeyError(actual_message_id)
        return message

    def list_messages(self, session_id: str) -> list[AgenticAudioMessage]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT *
                FROM agentic_audio_messages
                WHERE session_id = %s
                ORDER BY created_at, message_id
                """,
                params=[session_id],
            )
        return [
            message
            for row in (rows if isinstance(rows, list) else [])
            if (message := self._message_from_row(row)) is not None
        ]

    def record_tool_call(
        self,
        *,
        session_id: str,
        tool_name: str,
        status: str,
        input_json: Optional[dict[str, Any]] = None,
        output_json: Optional[dict[str, Any]] = None,
        error_json: Optional[dict[str, Any]] = None,
        linked_job_id: Optional[str] = None,
        linked_artifact_ids: Optional[list[str]] = None,
        finished: bool = False,
        tool_call_id: Optional[str] = None,
    ) -> AgenticAudioToolCall:
        self.ensure_schema()
        actual_tool_call_id = tool_call_id or new_id("agent_tool")
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO agentic_audio_tool_calls (
                    tool_call_id, session_id, tool_name, status, input_json,
                    output_json, error_json, linked_job_id, linked_artifact_ids,
                    finished_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s THEN now() ELSE NULL END)
                RETURNING *
                """,
                params=[
                    actual_tool_call_id,
                    session_id,
                    tool_name,
                    status,
                    input_json or {},
                    output_json,
                    error_json,
                    linked_job_id,
                    linked_artifact_ids or [],
                    finished,
                ],
            )
        tool_call = self._tool_call_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if tool_call is None:
            raise KeyError(actual_tool_call_id)
        return tool_call

    def list_tool_calls(self, session_id: str) -> list[AgenticAudioToolCall]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT *
                FROM agentic_audio_tool_calls
                WHERE session_id = %s
                ORDER BY created_at, tool_call_id
                """,
                params=[session_id],
            )
        return [
            tool_call
            for row in (rows if isinstance(rows, list) else [])
            if (tool_call := self._tool_call_from_row(row)) is not None
        ]

    def record_choice(
        self,
        *,
        session_id: str,
        choice_type: str,
        target_id: str,
        payload_json: Optional[dict[str, Any]] = None,
        choice_id: Optional[str] = None,
    ) -> AgenticAudioChoice:
        self.ensure_schema()
        actual_choice_id = choice_id or new_id("agent_choice")
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                INSERT INTO agentic_audio_choices (
                    choice_id, session_id, choice_type, target_id, payload_json
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                params=[
                    actual_choice_id,
                    session_id,
                    choice_type,
                    target_id,
                    payload_json or {},
                ],
            )
        choice = self._choice_from_row(rows[0] if isinstance(rows, list) and rows else None)
        if choice is None:
            raise KeyError(actual_choice_id)
        return choice

    def list_choices(self, session_id: str) -> list[AgenticAudioChoice]:
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                """
                SELECT *
                FROM agentic_audio_choices
                WHERE session_id = %s
                ORDER BY created_at, choice_id
                """,
                params=[session_id],
            )
        return [
            choice
            for row in (rows if isinstance(rows, list) else [])
            if (choice := self._choice_from_row(row)) is not None
        ]

    def build_snapshot(self, session_id: str) -> AgenticAudioSessionSnapshot:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        messages = self.list_messages(session_id)
        tool_calls = self.list_tool_calls(session_id)
        choices = self.list_choices(session_id)
        return AgenticAudioSessionSnapshot(
            session_id=session.session_id,
            source_video_artifact_id=session.source_video_artifact_id,
            status=session.status,
            phase=session.phase,
            creator_user_id=session.creator_user_id,
            selected_candidate_id=session.selected_candidate_id,
            linked_job_ids=session.linked_job_ids,
            # Vendor identity is scrubbed centrally by the snapshot model's
            # serializer (see AgenticAudioSessionSnapshot in models.py); the full
            # state_json is passed through here so server-side native edits keep
            # the provider handles.
            state=session.state_json,
            messages=[
                {
                    "message_id": message.message_id,
                    "role": message.role,
                    "content": message.content,
                    "payload": message.payload_json,
                    "created_at": (
                        message.created_at.isoformat()
                        if message.created_at
                        else None
                    ),
                }
                for message in messages
            ],
            tool_calls=[
                {
                    "tool_call_id": tool_call.tool_call_id,
                    "tool_name": tool_call.tool_name,
                    "status": tool_call.status,
                    "input": tool_call.input_json,
                    "output": tool_call.output_json,
                    "error": tool_call.error_json,
                    "linked_job_id": tool_call.linked_job_id,
                    "linked_artifact_ids": tool_call.linked_artifact_ids,
                    "created_at": (
                        tool_call.created_at.isoformat()
                        if tool_call.created_at
                        else None
                    ),
                    "finished_at": (
                        tool_call.finished_at.isoformat()
                        if tool_call.finished_at
                        else None
                    ),
                }
                for tool_call in tool_calls
            ],
            choices=[
                {
                    "choice_id": choice.choice_id,
                    "choice_type": choice.choice_type,
                    "target_id": choice.target_id,
                    "payload": choice.payload_json,
                    "created_at": (
                        choice.created_at.isoformat()
                        if choice.created_at
                        else None
                    ),
                }
                for choice in choices
            ],
        )


__all__ = ["AgenticAudioRepository", "MIGRATION_PATH"]
