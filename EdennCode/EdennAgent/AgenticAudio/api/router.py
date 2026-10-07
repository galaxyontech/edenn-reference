from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.error_codes import public_error_payload, scrub_provider_names
from EdennCode.Deployment.async_pipeline_v2.models import new_id
from EdennCode.Deployment.async_pipeline_v2.queue.postgres_queue import PostgresTaskQueue
from EdennCode.Deployment.async_pipeline_v2.repositories import AsyncPipelineV2Repository

from ..agent.agent import build_agentic_audio_agent_client
from ..agent.turn_lock import SessionBusy
from . import audit
from .fanout import CrossReplicaFanout
from .tickets import PURPOSE_MEDIA, PURPOSE_WS, mint_ticket, redeem_ticket
from .observability import new_request_id as _new_request_id
from .accounts import account_for
from .accounts import directory_configured as account_directory_configured
from .auth import (
    AuthError,
    auth_enabled,
    authorize_owner,
    mint_share_grant,
    resolve_caller,
    resolve_principal,
    verify_share_grant,
)
from .serialization import event_payload, events_payload, snapshot_opened_event
from ..events import EventType
from ..models import (
    AgenticMessageRole,
    AgenticAudioChoiceRequest,
    AgenticAudioMessageRequest,
    AgenticAudioSessionPhase,
    AgenticAudioSessionSnapshot,
    CreateAgenticAudioSessionRequest,
    CreateAgenticAudioSessionResponse,
    MAX_MESSAGE_CHARS,
)
from ..planner import AgenticAudioPlanner
from ..persistence.collab import (
    CollabRepository,
    build_collab_payload,
    comment_to_dict,
    participant_to_dict,
    role_at_least,
    thread_to_dict,
)
from ..persistence import media_store as media_store_module
from ..persistence import retention
from ..persistence.repositories import AgenticAudioRepository
from ..tools.media import AgenticAudioTools


logger = logging.getLogger(__name__)

# Static single-page console (chat-first audio-director UI). Served same-origin so
# the browser hits the WS/REST endpoints without CORS friction. Served by the
# REAL app (Deployment/api.py mounts this router), so opening it talks to the
# live router/agent/analysis/generation — not a stub. See FRONTEND_INTEGRATION.md.
# router.py lives in AgenticAudio/api/, so the package root is two parents up.
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
# Allow-listed asset paths (relative to FRONTEND_DIR), including the js/ folder.
# Choice types that cause a provider render rather than just a model turn.
_SPENDING_CHOICES = {"proposal", "candidate", "variation", "voiceover", "sfx", "compose"}


_warned_url_token = False


def _warn_url_token_once() -> None:
    """Say once that a caller is still putting its credential in the URL."""

    global _warned_url_token
    if _warned_url_token:
        return
    _warned_url_token = True
    logger.warning(
        "A client opened a socket with ?token= in the URL. The credential ends "
        "up in access logs, browser history and Referer headers. Mint a ticket "
        "with POST /tickets and connect with ?ticket= instead."
    )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _origin_allowed(origin: str) -> bool:
    """Is this browser origin allowed to open a session socket?

    Empty allow-list means same-origin-only deployments that never see a foreign
    Origin, so an unset variable does not silently permit everything: a browser
    always sends Origin, and anything not on the list is refused.
    """

    allowed = [
        o.strip().rstrip("/")
        for o in os.getenv("AGENTIC_AUDIO_ALLOWED_ORIGINS", "").split(",")
        if o.strip()
    ]
    candidate = origin.strip().rstrip("/")
    if not allowed:
        # Nothing configured: allow the console's own origin only, which is what
        # a same-origin deployment sends. A configured public base URL names it.
        base = (os.getenv("EDENN_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
        if not base:
            return True  # local development: no public identity to compare against
        return candidate == base
    return candidate in allowed


def _client_ip(request: Any) -> str:
    """The caller's address, trusting a proxy header only when one is set.

    Behind the container platform's ingress the socket peer is the proxy, so
    without this every unauthenticated caller would share one bucket again.
    """

    forwarded = (getattr(request, "headers", {}) or {}).get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    client = getattr(request, "client", None)
    return (getattr(client, "host", "") or "")[:64]


def _client_detail(exc: Exception) -> str:
    """One exception, rendered for a person to read.

    ``str(KeyError("Proposal not found: nope"))`` is ``"'Proposal not found:
    nope'"`` — Python's repr of the key, quotes and all — and that reached the
    client verbatim as its error detail. The message is the argument, not its
    repr.
    """

    args = getattr(exc, "args", ())
    text = str(args[0]) if len(args) == 1 and isinstance(args[0], str) else str(exc)
    return scrub_provider_names(text)


def _bind_log_context(
    principal: Optional[str],
    session_id: Optional[str] = None,
    *,
    request_id: Optional[str] = None,
) -> None:
    """Put who and which-session onto every log line for this request.

    Bound at the router because that is the first place both are known; the
    agent and the tools then log with the same identity without having to be
    passed a request object they have no other use for.

    ``request_id`` is for callers with no HTTP middleware above them — a socket
    frame, which is its own unit of work and gets its own id.
    """

    from .observability import bind_request

    bind_request(
        request_id or "", session_id=session_id or "", principal=principal or ""
    )


def _guard_turn(
    principal: Optional[str], *, spending: bool = False, client_ip: Optional[str] = None
) -> None:
    """Rate-limit a turn, and refuse one that would spend past the daily ceiling.

    Both checks run BEFORE the work: a limiter consulted afterwards has already
    paid for the thing it was meant to prevent.
    """

    from .limits import LimitExceeded, limiter

    lim = limiter()
    try:
        lim.check_turn(principal, client_ip)
        if spending:
            lim.check_generation(principal, client_ip)
            lim.record_generation(principal, client_ip=client_ip)
    except LimitExceeded as exc:
        headers = {"Retry-After": str(exc.retry_after_s)} if exc.retry_after_s else None
        raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=headers) from exc


def _mock_backend_allowed() -> bool:
    """True for a local run, false for anything that looks deployed.

    Detected from the deployment's own shape rather than from a flag somebody
    has to remember, matching the standalone's refuse-to-serve-publicly check.
    ``AGENTIC_AUDIO_ALLOW_MOCK`` forces it back on for a demo that wants it.
    """

    if os.getenv("AGENTIC_AUDIO_ALLOW_MOCK", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    deployed = bool(
        os.getenv("CONTAINER_APP_NAME")
        or os.getenv("WEBSITE_HOSTNAME")
        or os.getenv("EDENN_PUBLIC_BASE_URL")
    )
    return not deployed


FRONTEND_ASSETS = {
    "index.html",
    "styles.css",
    "mock-backend.js",
    "js/app.js",
    "js/library-ui.js",
    "js/studio-ui.js",
    "js/studio-state.js",
    "js/ai-elements.js",
    "js/timeline-mode.js",
    "js/canvas-mode.js",
    "js/collab-mode.js",
    "js/transform-mode.js",
}


class AgenticAudioActionResponse(BaseModel):
    session_id: str
    snapshot: AgenticAudioSessionSnapshot
    events: list[dict[str, Any]] = Field(default_factory=list)


# ---- collab-mode request models (router-local, like the response above) ----
# ``author_id``/``author_name``/``actor_id`` are honored only when auth is OFF
# (dev/mock parity); with auth on, the authenticated principal always wins.


class CollabCommentRequest(BaseModel):
    # Bounded: an unbounded body is an unbounded-storage / DoS vector.
    body: str = Field(max_length=MAX_MESSAGE_CHARS)
    mentions: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    author_id: Optional[str] = None
    author_name: Optional[str] = None


class CollabThreadCreateRequest(CollabCommentRequest):
    anchor_node_id: str
    anchor_label: Optional[str] = None
    anchor_start_s: Optional[float] = None
    anchor_end_s: Optional[float] = None


class CollabThreadStatusRequest(BaseModel):
    status: Literal["open", "resolved"]
    actor_id: Optional[str] = None
    actor_name: Optional[str] = None


class CollabCommentEditRequest(BaseModel):
    body: str = Field(max_length=MAX_MESSAGE_CHARS)
    actor_id: Optional[str] = None


class CollabReactionRequest(BaseModel):
    emoji: str
    on: bool = True
    actor_id: Optional[str] = None


class CollabParticipantRequest(BaseModel):
    user_id: str
    role: Literal["view", "comment", "iterate"]
    display_name: Optional[str] = None


class ProfileRequest(BaseModel):
    display_name: str = Field(default="", max_length=80)


class CollabShareLinkRequest(BaseModel):
    role: Literal["view", "comment", "iterate"] = "comment"


class CollabJoinRequest(BaseModel):
    grant: str
    display_name: Optional[str] = None
    # Auth-off only: the joining persona's id (with auth on, the bearer
    # principal IS the identity and this field is ignored).
    user_id: Optional[str] = None


# The agents addressable from a comment (@mention → the model picks the work
# up). One director agent today; a registry hook for more later.
COLLAB_AGENTS: list[dict[str, str]] = [
    {"id": "edenn", "name": "Edenn Director", "kind": "agent"},
]
COLLAB_AGENT_IDS = {agent["id"] for agent in COLLAB_AGENTS}


def agentic_audio_enabled() -> bool:
    return os.getenv("AGENTIC_AUDIO_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def mount_agentic_audio_router(
    app: Any,
    context: ApiContext,
    **router_kwargs: Any,
) -> bool:
    if not agentic_audio_enabled():
        return False
    # Production default: ask the user which modality to build (full audio /
    # music / voice-over) before proposing. Callers can override explicitly.
    router_kwargs.setdefault("require_intent_gate", True)
    app.include_router(create_agentic_audio_router(context, **router_kwargs))
    return True


def create_agentic_audio_router(
    context: ApiContext,
    *,
    repository: Any | None = None,
    async_repository: Any | None = None,
    queue: Any | None = None,
    tools: AgenticAudioTools | None = None,
    planner: AgenticAudioPlanner | None = None,
    llm_client: Any | None = None,
    require_intent_gate: bool = False,
    collab_repository: Any | None = None,
) -> APIRouter:
    agent_repo = repository or AgenticAudioRepository()
    collab_repo = collab_repository or CollabRepository()
    async_repo = async_repository or AsyncPipelineV2Repository()
    task_queue = queue if queue is not None else PostgresTaskQueue()
    agent_tools = tools or AgenticAudioTools(
        async_repository=async_repo,
        queue=task_queue,
        settings=context.settings,
        storage=getattr(context, "storage", None),
    )
    if planner is None:
        agent_client = llm_client if llm_client is not None else build_agentic_audio_agent_client()
        agent_planner = AgenticAudioPlanner(
            repository=agent_repo,
            tools=agent_tools,
            llm_client=agent_client,
            require_intent_gate=require_intent_gate,
        )
    else:
        agent_planner = planner
    # Identity is wired HERE because both deployments build their app through
    # this one function. Wired anywhere else, the standalone host (which is
    # production for the studio) would have kept booting with no verifier at
    # all: `identity_configured()` false, the console told "sign-in is not
    # available here", and the static token list the whole module exists to
    # retire as the only way in.
    from .identity import configure_from_env as _configure_identity

    _configure_identity()
    router = APIRouter(prefix="/api/v2/agentic/audio", tags=["agentic-audio"])

    async def _caller_or_error(
        authorization: str | None = None, token: str | None = None
    ):
        """The authenticated caller (None when auth is off); 401 on bad creds."""
        try:
            return await resolve_caller(authorization=authorization, token=token)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    async def _principal_or_error(
        authorization: str | None = None, token: str | None = None
    ) -> str | None:
        """Just the principal — what ownership and membership are keyed on."""
        caller = await _caller_or_error(authorization=authorization, token=token)
        if caller is None:
            return None
        # The studio has no sign-up of its own, so "the user exists" is learned
        # from authentication. Best-effort: a storage hiccup here must not cost
        # someone their turn.
        #
        # In a thread because psycopg2 is synchronous: this runs on EVERY
        # authenticated request, and a database that is merely slow rather than
        # down would otherwise stall the event loop for every other request in
        # the process, not just this one.
        #
        # The account is resolved here too, and cached, because this is the one
        # place that already knows a caller is real. It is best-effort for the
        # same reason the row is: nothing charges yet, and an account index
        # having a bad minute must not cost somebody their turn. When billing
        # does arrive it reads the stored link — and a NULL there means "not
        # established", which is a refusal to answer, never a licence to spend.
        account_id: str | None = None
        if account_directory_configured():
            try:
                account_id = await account_for(caller.principal)
            except Exception:  # noqa: BLE001 - see above
                logger.debug(
                    "account lookup failed for %s", caller.principal, exc_info=True
                )
        try:
            await asyncio.to_thread(
                collab_repo.touch_user,
                caller.principal,
                auth_source=caller.source,
                display_name=caller.display_name,
                account_id=account_id,
            )
        except Exception:  # noqa: BLE001 - bookkeeping, never the request
            logger.debug("touch_user failed for %s", caller.principal, exc_info=True)
        return caller.principal

    def _authorize_session(session_id: str, principal: str | None) -> Any:
        """Load the session and 403 unless the caller owns it (no-op when auth off)."""
        session = agent_repo.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        try:
            authorize_owner(session.creator_user_id, principal)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return session

    # ---- collab: membership-aware authorization ---------------------------
    # Share roles extend the owner-only model: a session participant may read
    # (view+), join threads (comment+), or branch/generate (iterate). Auth OFF
    # (the default) keeps every caller an implicit owner — byte-identical
    # behavior for existing deployments and tests.
    def _member_role(session: Any, principal: str | None) -> str | None:
        if not auth_enabled():
            return "owner"
        if principal is None:
            return None
        if (session.creator_user_id or "") == principal:
            return "owner"
        participant = collab_repo.get_participant(session.session_id, principal)
        return participant.role if participant else None

    def _authorize_member(session_id: str, principal: str | None, minimum: str) -> Any:
        session = agent_repo.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        role = _member_role(session, principal)
        if role is None:
            raise HTTPException(
                status_code=401 if principal is None else 403,
                detail="You do not have access to this session.",
            )
        if not role_at_least(role, minimum):
            raise HTTPException(
                status_code=403,
                detail=f"Your role ({role}) can't do that — needs {minimum} access.",
            )
        return session

    def _actor(
        session_id: str,
        principal: str | None,
        provided_id: str | None,
        provided_name: str | None,
    ):
        """(author_id, author_name) for a collab write. Principal wins when auth
        is on — id AND display name (resolved server-side, so a member can't
        impersonate someone else's name). The client-provided persona is
        dev/mock-mode (auth off) convenience only."""
        if principal is not None:
            participant = collab_repo.get_participant(session_id, principal)
            # Three sources, most specific first: the name on this session's
            # participant row, then the person's own profile, then the principal
            # itself. The middle one is why an OWNER stops appearing as a raw
            # uid — the owner is the creator, not a participant row, so they had
            # nothing but the fallback.
            display = (participant.display_name if participant else None) or ""
            if not display:
                try:
                    profile = collab_repo.get_user(principal) or {}
                    display = profile.get("display_name") or ""
                except Exception:  # noqa: BLE001 - a name is never worth the write
                    display = ""
            return principal, display or principal
        actor_id = (provided_id or "").strip() or "guest"
        return actor_id, (provided_name or "").strip() or actor_id

    # ---- collab: live fan-out hub -----------------------------------------
    # Every WS on a session registers here; comment/participant mutations
    # broadcast to all of them so pins, cards and rails stay in sync across
    # viewers. In-process by design (one API instance per environment today).
    ws_clients: dict[str, set[WebSocket]] = {}
    # Which principal each open socket belongs to. Without this, a removed
    # collaborator's live connection cannot be found and closed — the role is
    # resolved once at connect and never re-checked.
    ws_principals: dict[WebSocket, str] = {}

    async def _broadcast(session_id: str, event: dict[str, Any]) -> None:
        for ws in list(ws_clients.get(session_id, ())):
            try:
                await ws.send_json(event)
            except Exception:  # noqa: BLE001 - a dead socket must not break the rest
                ws_clients.get(session_id, set()).discard(ws)

    async def _deliver_remote(session_id: str, event_type: str) -> None:
        """Another replica moved this session; push the current state locally.

        The notification carries only the session id and event type — a snapshot
        is far larger than NOTIFY's payload ceiling — so this reads the session
        and sends what it finds. Slightly more work per event, and immune to a
        size limit that a payload-carrying design is not.
        """
        sockets = list(ws_clients.get(session_id, ()))
        if not sockets:
            return
        try:
            agent_planner.refresh_session_state(session_id)
            snapshot = agent_repo.build_snapshot(session_id)
        except Exception:  # noqa: BLE001 - a session that vanished is not an error
            return
        event = snapshot_opened_event(snapshot)
        for ws in sockets:
            try:
                await ws.send_json(event)
            except Exception:  # noqa: BLE001
                ws_clients.get(session_id, set()).discard(ws)

    _fanout = CrossReplicaFanout(
        client_factory=getattr(agent_repo, "_client_factory", None),
        deliver=_deliver_remote,
    )
    router.agentic_audio_fanout = _fanout  # type: ignore[attr-defined]

    async def _broadcast_peers(
        session_id: str, event: dict[str, Any], origin: WebSocket | None
    ) -> None:
        """Turn fan-out: every OTHER socket on the session hears the event, so
        collaborators watch a turn live instead of waiting for their next poll.
        The initiating socket has its own delivery path (`_send`, which aborts
        the turn on failure); a dead peer socket must never abort the turn."""
        for ws in list(ws_clients.get(session_id, ())):
            if ws is origin:
                continue
            try:
                await ws.send_json(event)
            except Exception:  # noqa: BLE001 - a dead peer must not break the turn
                ws_clients.get(session_id, set()).discard(ws)
        # …and the viewers on OTHER replicas, who otherwise watch a session that
        # appears frozen until their next poll. Fire-and-forget: the snapshot in
        # the database is the durability story, not this.
        await _fanout.publish(session_id, str(event.get("event_type") or ""))

    def _collab_event(session_id: str, event_type: EventType, payload: dict[str, Any]) -> dict[str, Any]:
        return {"event_type": event_type.value, "session_id": session_id, "payload": payload}

    def _thread_payload(thread_id: str) -> dict[str, Any]:
        thread = collab_repo.get_thread(thread_id)
        if thread is None:
            raise KeyError(thread_id)
        item = thread_to_dict(thread)
        item["comments"] = [
            comment_to_dict(c)
            for c in collab_repo.list_comments(thread.session_id)
            if c.thread_id == thread_id
        ]
        return item

    async def _reopen_if_resolved(session_id: str, thread_id: str) -> bool:
        """Replying to a resolved thread reopens it — for humans AND the agent —
        and every viewer hears about it (thread.updated broadcast)."""
        thread = collab_repo.get_thread(thread_id)
        if thread is None or thread.status != "resolved":
            return False
        collab_repo.update_thread_status(thread_id, status="open")
        await _broadcast(
            session_id,
            _collab_event(
                session_id,
                EventType.COMMENT_THREAD_UPDATED,
                {"thread": _thread_payload(thread_id)},
            ),
        )
        return True

    def _session_thread_or_404(session_id: str, thread_id: str) -> Any:
        thread = collab_repo.get_thread(thread_id)
        if thread is None or thread.session_id != session_id:
            raise HTTPException(status_code=404, detail=f"Thread not found: {thread_id}")
        return thread

    def _session_comment_or_404(session_id: str, comment_id: str) -> Any:
        comment = collab_repo.get_comment(comment_id)
        if comment is None or comment.session_id != session_id:
            raise HTTPException(status_code=404, detail=f"Comment not found: {comment_id}")
        return comment

    def _is_known_anchor(session_id: str, anchor_node_id: str) -> bool:
        """A thread anchor must be a real lineage node: the source, a proposal, a
        candidate/take, or an SFX variant."""
        if anchor_node_id in ("__source__", ""):
            return True
        session = agent_repo.get_session(session_id)
        if session is None:
            return False
        st = session.state_json or {}
        known: set[str] = set()
        for p in st.get("proposals") or []:
            if isinstance(p, dict) and p.get("proposal_id"):
                known.add(str(p["proposal_id"]))
        for c in st.get("candidates") or []:
            if isinstance(c, dict) and c.get("candidate_id"):
                known.add(str(c["candidate_id"]))
        sfx = (st.get("layers") or {}).get("sfx")
        if isinstance(sfx, dict):
            for v in sfx.get("variants") or []:
                if isinstance(v, dict) and v.get("variant_id"):
                    known.add(str(v["variant_id"]))
        return anchor_node_id in known

    # ---- collab: @agent handoff -------------------------------------------
    # Mentioning an agent in a comment hands the work to the model: the comment
    # body runs as a normal agent turn (anchored via context_refs), and the
    # agent's chat reply is mirrored back into the thread as an agent-authored
    # comment. Runs as a background task so posting stays instant; the agent's
    # per-session turn lock serializes it against live chat turns.
    def _agent_mentioned(mentions: list[dict[str, Any]]) -> bool:
        return any(
            (m.get("kind") == "agent") or (str(m.get("id") or "") in COLLAB_AGENT_IDS)
            for m in mentions or []
        )

    # Strong refs: asyncio only weakly references running tasks — without this a
    # pickup task can be garbage-collected mid-turn under a long-lived loop.
    pickup_tasks: set[asyncio.Task[Any]] = set()

    async def _agent_pickup(session_id: str, thread_id: str, body: str) -> None:
        snapshot_after = None
        try:
            thread = collab_repo.get_thread(thread_id)
            payload: dict[str, Any] = {"source": "comment", "thread_id": thread_id}
            if thread and thread.anchor_node_id and thread.anchor_node_id != "__source__":
                payload["context_refs"] = [thread.anchor_node_id]
            await agent_planner.handle_user_message(
                session_id=session_id, content=body, payload=payload
            )
            agent_planner.refresh_session_state(session_id)
            snapshot_after = agent_repo.build_snapshot(session_id)
            reply = next(
                (
                    m
                    for m in reversed(snapshot_after.messages)
                    if m.get("role") == "assistant"
                ),
                None,
            )
            reply_body = (
                scrub_provider_names(str(reply.get("content") or "").strip())
                if reply
                else "Picked this up — the result is in the session."
            )
        except Exception as exc:  # noqa: BLE001 - boundary: reply must never leak internals
            logger.exception("Collab agent pickup failed: %s", exc)
            reply_body = public_error_payload(exc)["message"]
        try:
            comment = collab_repo.add_comment(
                thread_id=thread_id,
                session_id=session_id,
                author_id=COLLAB_AGENTS[0]["id"],
                author_name=COLLAB_AGENTS[0]["name"],
                author_kind="agent",
                body=reply_body,
            )
            await _broadcast(
                session_id,
                _collab_event(
                    session_id,
                    EventType.COMMENT_CREATED,
                    {"thread_id": thread_id, "comment": comment_to_dict(comment)},
                ),
            )
            await _reopen_if_resolved(session_id, thread_id)
            if snapshot_after is not None:
                # The turn may have branched a take — refresh every open canvas.
                await _broadcast(session_id, snapshot_opened_event(snapshot_after))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Collab agent reply post failed: %s", exc)

    async def _post_agent_note(session_id: str, thread_id: str, note: str) -> None:
        comment = collab_repo.add_comment(
            thread_id=thread_id,
            session_id=session_id,
            author_id=COLLAB_AGENTS[0]["id"],
            author_name=COLLAB_AGENTS[0]["name"],
            author_kind="agent",
            body=note,
        )
        await _broadcast(
            session_id,
            _collab_event(
                session_id,
                EventType.COMMENT_CREATED,
                {"thread_id": thread_id, "comment": comment_to_dict(comment)},
            ),
        )

    def _agent_in_thread(thread_id: str, session_id: str) -> bool:
        """The agent already answered here — the thread IS a conversation with
        it, so plain replies continue it without re-@mentioning every message."""
        return any(
            c.thread_id == thread_id and c.author_kind == "agent" and c.deleted_at is None
            for c in collab_repo.list_comments(session_id)
        )

    def _maybe_dispatch_agent(
        session_id: str, thread_id: str, request: CollabCommentRequest, actor_role: str | None
    ) -> None:
        mentioned = _agent_mentioned(request.mentions)
        # Dispatch on an explicit @mention, or on any reply in a thread the
        # agent has already spoken in (conversational continuity).
        if not mentioned and not _agent_in_thread(thread_id, session_id):
            return
        # Handing work to the agent runs a full (spend-capable) turn — the same
        # bar as /messages and /choices: iterate or better. An explicit mention
        # from a lower role gets an honest in-thread answer; a plain reply in an
        # agent thread is skipped silently (no note-spam on every message).
        if not role_at_least(actor_role or "", "iterate"):
            if mentioned:
                task = asyncio.create_task(_post_agent_note(
                    session_id, thread_id,
                    "Only collaborators with iterate access can hand me work — "
                    "ask the owner to upgrade your role.",
                ))
                pickup_tasks.add(task)
                task.add_done_callback(pickup_tasks.discard)
            return
        task = asyncio.create_task(_agent_pickup(session_id, thread_id, request.body))
        pickup_tasks.add(task)
        task.add_done_callback(pickup_tasks.discard)

    @router.get("/app", include_in_schema=False)
    async def serve_console_redirect() -> RedirectResponse:
        # Canonical trailing slash so the page's relative asset paths (./app.js,
        # ./styles.css) resolve under /app/ instead of the router root.
        return RedirectResponse(url="app/")

    @router.get("/app/", include_in_schema=False)
    async def serve_console() -> FileResponse:
        index = FRONTEND_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="Console UI is not bundled.")
        return FileResponse(index, media_type="text/html")

    @router.get("/app/{asset:path}", include_in_schema=False)
    async def serve_console_asset(asset: str) -> FileResponse:
        # Allow-list known assets (incl. js/ modules); never resolve arbitrary
        # user paths off disk. Defense-in-depth: also confirm the resolved file
        # stays inside FRONTEND_DIR.
        if asset not in FRONTEND_ASSETS:
            raise HTTPException(status_code=404, detail=f"Unknown asset: {asset}")
        # The offline mock is a development affordance: it fabricates takes,
        # answers as the director, and lets ?backend=mock bypass the real
        # pipeline entirely. On a deployed console that is a second, unpoliced
        # product surface, so the script simply is not served there.
        if asset == "mock-backend.js" and not _mock_backend_allowed():
            raise HTTPException(status_code=404, detail="Unknown asset: mock-backend.js")
        path = (FRONTEND_DIR / asset).resolve()
        if FRONTEND_DIR.resolve() not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail=f"Asset not found: {asset}")
        return FileResponse(path)

    def _artifact_owner(artifact: Any) -> str | None:
        """The principal who uploaded this artifact, if we know.

        Ownership lives on the JOB that staged the artifact, not on the artifact
        row. ``None`` means unknown — an artifact staged before uploads recorded
        an owner, or by a path that has none — and is deliberately permissive:
        refusing every historical artifact would lock people out of their own
        sessions to close a hole that a known owner already closes.
        """
        job_id = getattr(artifact, "job_id", None)
        if not job_id:
            return None
        try:
            job = async_repo.get_job(job_id)
        except Exception:  # noqa: BLE001 - an unreadable job is an unknown owner
            return None
        owner = getattr(job, "creator_user_id", None) if job else None
        return str(owner) if owner else None

    @router.post("/sessions", response_model=CreateAgenticAudioSessionResponse)
    async def create_session(
        request: CreateAgenticAudioSessionRequest,
        http_request: Request,
        authorization: str | None = Header(default=None),
    ) -> CreateAgenticAudioSessionResponse:
        principal = await _principal_or_error(authorization=authorization)
        source_artifact_id = (request.source_video_artifact_id or "").strip()
        if not source_artifact_id:
            raise HTTPException(status_code=400, detail="source_video_artifact_id is required.")
        try:
            artifact = agent_tools.get_source_video_artifact(source_artifact_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Source video artifact not found: {source_artifact_id}",
            ) from exc

        # Whose video is this? Existence was the only check, so an artifact id —
        # which appears in URLs and API responses — was enough to start a
        # session over somebody else's uploaded footage, have it analysed, and
        # watch it back in the console.
        if principal is not None:
            owner = _artifact_owner(artifact)
            if owner is not None and owner != principal:
                # 404, not 403: confirming that an id exists but belongs to
                # someone else is itself a disclosure.
                raise HTTPException(
                    status_code=404,
                    detail=f"Source video artifact not found: {source_artifact_id}",
                )

        # Creating a session RUNS THE ANALYSIS — one vision call per scene, up
        # to thirty, which is the most expensive single moment in a session.
        # It was the one spending entrypoint with no limit on it at all: the
        # turn endpoint counted against the daily ceiling and this did not, so
        # a loop here spent without ever touching the thing meant to stop it.
        _guard_turn(principal, spending=True, client_ip=_client_ip(http_request))
        session = agent_repo.create_session(
            session_id=new_id("agent_session"),
            source_video_artifact_id=source_artifact_id,
            # When auth is on, the session is owned by the authenticated caller;
            # otherwise fall back to the request's (optional) creator_user_id.
            creator_user_id=(principal if principal is not None else request.creator_user_id),
            phase=AgenticAudioSessionPhase.CREATED,
            state_json={},
        )
        try:
            await agent_planner.bootstrap_session(
                session=session,
                initial_message=request.initial_message,
            )
            refreshed = agent_repo.get_session(session.session_id) or session
        except Exception as exc:
            logger.exception("Agentic audio session bootstrap failed: %s", exc)
            raise HTTPException(
                status_code=500, detail=public_error_payload(exc)["message"]
            ) from exc
        return CreateAgenticAudioSessionResponse(
            session_id=session.session_id,
            status=refreshed.status,
            phase=refreshed.phase,
            status_url=f"/api/v2/agentic/audio/sessions/{session.session_id}",
            ws_url=f"/api/v2/agentic/audio/sessions/{session.session_id}/ws",
        )

    @router.get("/sessions")
    async def list_sessions(
        creator: str | None = None, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """List the caller's sessions (most-recent first) — backs the History view.
        When auth is on, scoped to the authenticated principal; when off, an optional
        ?creator= filter applies. Sessions shared WITH the caller (participants
        table) ride along flagged `shared` — a collaborator's way back in after
        the link's tab is gone."""
        principal = await _principal_or_error(authorization=authorization)
        who = principal if principal is not None else creator
        own = agent_repo.list_sessions(creator_user_id=who, limit=50)
        if not who:
            return {"sessions": own}
        own_ids = {s["session_id"] for s in own}
        shared: list[dict[str, Any]] = []
        for sid in collab_repo.sessions_for_user(who):
            if sid in own_ids:
                continue
            session = agent_repo.get_session(sid)
            if session is None:
                continue
            participant = collab_repo.get_participant(sid, who)
            st = session.state_json or {}
            obs = st.get("observation") or {}
            fin = st.get("final_artifact") or {}
            shared.append(
                {
                    "session_id": session.session_id,
                    "status": session.status,
                    "phase": session.phase,
                    "creator_user_id": session.creator_user_id,
                    "source_video_artifact_id": session.source_video_artifact_id,
                    "updated_at": session.updated_at.isoformat() if session.updated_at else None,
                    "title": obs.get("video_title"),
                    "final_media_url": fin.get("video_url") or fin.get("audio_url"),
                    "shared": True,
                    "shared_role": participant.role if participant else "comment",
                }
            )
        sessions = own + shared
        sessions.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
        return {"sessions": sessions}

    @router.get("/sessions/{session_id}", response_model=AgenticAudioSessionSnapshot)
    async def get_session(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> AgenticAudioSessionSnapshot:
        principal = await _principal_or_error(authorization=authorization)
        # Membership-aware read: shared sessions are visible to participants
        # (view+); auth off keeps the historical implicit-owner behavior.
        _authorize_member(session_id, principal, "view")
        try:
            agent_planner.refresh_session_state(session_id)
            return agent_repo.build_snapshot(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}") from exc

    @router.post("/sessions/{session_id}/messages", response_model=AgenticAudioActionResponse)
    async def append_message(
        session_id: str,
        request: AgenticAudioMessageRequest,
        http_request: Request,
        authorization: str | None = Header(default=None),
    ) -> AgenticAudioActionResponse:
        if not request.content.strip():
            raise HTTPException(status_code=400, detail="content is required.")
        # Directing the agent changes (and can spend on) the session — owner or
        # a participant shared with the "iterate" role.
        _principal = await _principal_or_error(authorization=authorization)
        _authorize_member(session_id, _principal, "iterate")
        _bind_log_context(_principal, session_id)
        # A free-text turn always costs a model call, and the director may decide
        # to generate from it — so it counts against both limits.
        _guard_turn(_principal, spending=True, client_ip=_client_ip(http_request))
        try:
            events = await agent_planner.handle_user_message(
                session_id=session_id,
                content=request.content,
                payload=request.payload,
            )
            agent_planner.refresh_session_state(session_id)
            snapshot = agent_repo.build_snapshot(session_id)
        except SessionBusy as exc:
            # Not a fault: the previous message is still being worked on. 409 so
            # a client can tell "try again in a moment" from "this broke", and
            # so a retry does not read as a new instruction.
            raise HTTPException(
                status_code=409,
                detail=(
                    "This session is still working on your last message. "
                    "Give it a moment and try again."
                ),
            ) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_client_detail(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=_client_detail(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - boundary: never leak internals to the client
            logger.exception("Agentic audio message turn failed: %s", exc)
            raise HTTPException(
                status_code=500, detail=public_error_payload(exc)["message"]
            ) from exc
        # A REST-initiated turn (WS fallback, scripts) still reaches live viewers.
        for evt in events_payload(events):
            await _broadcast_peers(session_id, evt, None)
        await _broadcast_peers(session_id, snapshot_opened_event(snapshot), None)
        return AgenticAudioActionResponse(
            session_id=session_id,
            snapshot=snapshot,
            events=events_payload(events),
        )

    @router.post("/sessions/{session_id}/choices", response_model=AgenticAudioActionResponse)
    async def record_choice(
        session_id: str,
        request: AgenticAudioChoiceRequest,
        http_request: Request,
        authorization: str | None = Header(default=None),
    ) -> AgenticAudioActionResponse:
        # Structured actions branch/lock/generate — same bar as free-text turns.
        _principal = await _principal_or_error(authorization=authorization)
        _authorize_member(session_id, _principal, "iterate")
        _bind_log_context(_principal, session_id)
        _guard_turn(
            _principal,
            spending=request.choice_type in _SPENDING_CHOICES,
            client_ip=_client_ip(http_request),
        )
        try:
            events = await agent_planner.handle_choice(session_id=session_id, request=request)
            agent_planner.refresh_session_state(session_id)
            snapshot = agent_repo.build_snapshot(session_id)
        except SessionBusy as exc:
            # Not a fault: the previous message is still being worked on. 409 so
            # a client can tell "try again in a moment" from "this broke", and
            # so a retry does not read as a new instruction.
            raise HTTPException(
                status_code=409,
                detail=(
                    "This session is still working on your last message. "
                    "Give it a moment and try again."
                ),
            ) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_client_detail(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=_client_detail(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - boundary: never leak internals to the client
            logger.exception("Agentic audio choice turn failed: %s", exc)
            raise HTTPException(
                status_code=500, detail=public_error_payload(exc)["message"]
            ) from exc
        # Same live fan-out as the message path — choices spend and re-shape the
        # canvas, so peers must hear about them immediately.
        for evt in events_payload(events):
            await _broadcast_peers(session_id, evt, None)
        await _broadcast_peers(session_id, snapshot_opened_event(snapshot), None)
        return AgenticAudioActionResponse(
            session_id=session_id,
            snapshot=snapshot,
            events=events_payload(events),
        )

    # ======================================================================
    # Collab mode — comment threads on the lineage canvas, participants/roles.
    # ======================================================================
    @router.get("/sessions/{session_id}/collab")
    async def get_collab(
        session_id: str,
        viewer: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = await _principal_or_error(authorization=authorization)
        _authorize_member(session_id, principal, "view")
        # Auth on: unread counts are the principal's. Auth off: honor the
        # client's ?viewer= persona (same dev-trust model as author_id) so read
        # receipts work identically to the mock.
        effective_viewer = principal if principal is not None else viewer
        payload = build_collab_payload(collab_repo, session_id, effective_viewer)
        payload["agents"] = list(COLLAB_AGENTS)
        payload["viewer"] = effective_viewer
        return payload

    @router.post("/sessions/{session_id}/collab/threads")
    async def create_collab_thread(
        session_id: str,
        request: CollabThreadCreateRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not request.body.strip():
            raise HTTPException(status_code=400, detail="body is required.")
        principal = await _principal_or_error(authorization=authorization)
        session = _authorize_member(session_id, principal, "comment")
        # Anchor must be a real lineage node (source / a proposal / a candidate /
        # an SFX variant) — a comment on a node that doesn't exist is a 404, not a
        # silently-stored dangling anchor.
        if not _is_known_anchor(session_id, request.anchor_node_id):
            raise HTTPException(
                status_code=404, detail=f"Unknown anchor node: {request.anchor_node_id}"
            )
        author_id, author_name = _actor(session_id, principal, request.author_id, request.author_name)
        thread = collab_repo.create_thread(
            session_id=session_id,
            anchor_node_id=request.anchor_node_id,
            anchor_label=request.anchor_label,
            anchor_start_s=request.anchor_start_s,
            anchor_end_s=request.anchor_end_s,
            created_by=author_id,
        )
        collab_repo.add_comment(
            thread_id=thread.thread_id,
            session_id=session_id,
            author_id=author_id,
            author_name=author_name,
            body=request.body.strip(),
            mentions=request.mentions,
            attachments=request.attachments,
        )
        collab_repo.mark_read(thread.thread_id, author_id)
        payload = _thread_payload(thread.thread_id)
        await _broadcast(
            session_id,
            _collab_event(session_id, EventType.COMMENT_THREAD_CREATED, {"thread": payload}),
        )
        _maybe_dispatch_agent(session_id, thread.thread_id, request, _member_role(session, principal))
        return {"thread": payload}

    @router.post("/sessions/{session_id}/collab/threads/{thread_id}/comments")
    async def add_collab_comment(
        session_id: str,
        thread_id: str,
        request: CollabCommentRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not request.body.strip():
            raise HTTPException(status_code=400, detail="body is required.")
        principal = await _principal_or_error(authorization=authorization)
        session = _authorize_member(session_id, principal, "comment")
        _session_thread_or_404(session_id, thread_id)
        author_id, author_name = _actor(session_id, principal, request.author_id, request.author_name)
        comment = collab_repo.add_comment(
            thread_id=thread_id,
            session_id=session_id,
            author_id=author_id,
            author_name=author_name,
            body=request.body.strip(),
            mentions=request.mentions,
            attachments=request.attachments,
        )
        collab_repo.mark_read(thread_id, author_id)
        await _broadcast(
            session_id,
            _collab_event(
                session_id,
                EventType.COMMENT_CREATED,
                {"thread_id": thread_id, "comment": comment_to_dict(comment)},
            ),
        )
        await _reopen_if_resolved(session_id, thread_id)
        _maybe_dispatch_agent(session_id, thread_id, request, _member_role(session, principal))
        return {"comment": comment_to_dict(comment), "thread": _thread_payload(thread_id)}

    @router.patch("/sessions/{session_id}/collab/threads/{thread_id}")
    async def update_collab_thread(
        session_id: str,
        thread_id: str,
        request: CollabThreadStatusRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = await _principal_or_error(authorization=authorization)
        _authorize_member(session_id, principal, "comment")
        _session_thread_or_404(session_id, thread_id)
        actor_id, _ = _actor(session_id, principal, request.actor_id, request.actor_name)
        collab_repo.update_thread_status(
            thread_id,
            status=request.status,
            resolved_by=actor_id if request.status == "resolved" else None,
        )
        payload = _thread_payload(thread_id)
        await _broadcast(
            session_id,
            _collab_event(session_id, EventType.COMMENT_THREAD_UPDATED, {"thread": payload}),
        )
        return {"thread": payload}

    @router.post("/sessions/{session_id}/collab/threads/{thread_id}/read")
    async def mark_collab_thread_read(
        session_id: str,
        thread_id: str,
        request: CollabThreadStatusRequest | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = await _principal_or_error(authorization=authorization)
        _authorize_member(session_id, principal, "view")
        _session_thread_or_404(session_id, thread_id)
        actor_id, _ = _actor(
            session_id, principal, request.actor_id if request else None, None
        )
        collab_repo.mark_read(thread_id, actor_id)
        return {"ok": True}

    @router.patch("/sessions/{session_id}/collab/comments/{comment_id}")
    async def edit_collab_comment(
        session_id: str,
        comment_id: str,
        request: CollabCommentEditRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not request.body.strip():
            raise HTTPException(status_code=400, detail="body is required.")
        principal = await _principal_or_error(authorization=authorization)
        session = _authorize_member(session_id, principal, "comment")
        comment = _session_comment_or_404(session_id, comment_id)
        if comment.deleted_at is not None:
            raise HTTPException(status_code=400, detail="That comment was deleted.")
        # Only the author (or the owner) may edit; enforceable only with auth on.
        if principal is not None and comment.author_id != principal:
            role = _member_role(session, principal)
            if role != "owner":
                raise HTTPException(status_code=403, detail="Only the author can edit this comment.")
        updated = collab_repo.edit_comment(comment_id, body=request.body.strip())
        await _broadcast(
            session_id,
            _collab_event(
                session_id,
                EventType.COMMENT_UPDATED,
                {"thread_id": updated.thread_id, "comment": comment_to_dict(updated)},
            ),
        )
        return {"comment": comment_to_dict(updated)}

    @router.delete("/sessions/{session_id}/collab/comments/{comment_id}")
    async def delete_collab_comment(
        session_id: str,
        comment_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = await _principal_or_error(authorization=authorization)
        session = _authorize_member(session_id, principal, "comment")
        comment = _session_comment_or_404(session_id, comment_id)
        if principal is not None and comment.author_id != principal:
            role = _member_role(session, principal)
            if role != "owner":
                raise HTTPException(status_code=403, detail="Only the author can delete this comment.")
        updated = collab_repo.delete_comment(comment_id)
        await _broadcast(
            session_id,
            _collab_event(
                session_id,
                EventType.COMMENT_UPDATED,
                {"thread_id": updated.thread_id, "comment": comment_to_dict(updated)},
            ),
        )
        return {"comment": comment_to_dict(updated)}

    @router.put("/sessions/{session_id}/collab/comments/{comment_id}/reactions")
    async def react_collab_comment(
        session_id: str,
        comment_id: str,
        request: CollabReactionRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        emoji = request.emoji.strip()
        if not emoji or len(emoji) > 8:
            raise HTTPException(status_code=400, detail="emoji is required.")
        principal = await _principal_or_error(authorization=authorization)
        _authorize_member(session_id, principal, "comment")
        _session_comment_or_404(session_id, comment_id)
        actor_id, _ = _actor(session_id, principal, request.actor_id, None)
        updated = collab_repo.set_reaction(
            comment_id, emoji=emoji, user_id=actor_id, on=request.on
        )
        await _broadcast(
            session_id,
            _collab_event(
                session_id,
                EventType.COMMENT_UPDATED,
                {"thread_id": updated.thread_id, "comment": comment_to_dict(updated)},
            ),
        )
        return {"comment": comment_to_dict(updated)}

    @router.get("/sessions/{session_id}/collab/participants")
    async def list_collab_participants(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        principal = await _principal_or_error(authorization=authorization)
        _authorize_member(session_id, principal, "view")
        payload = build_collab_payload(collab_repo, session_id, principal)
        return {"participants": payload["participants"], "agents": list(COLLAB_AGENTS)}

    @router.post("/sessions/{session_id}/collab/participants")
    async def upsert_collab_participant(
        session_id: str,
        request: CollabParticipantRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = await _principal_or_error(authorization=authorization)
        # Only the owner shares the session / changes roles (auth off → anyone,
        # mirroring the trust model of every other dev-mode endpoint).
        session = _authorize_member(session_id, principal, "view")
        if principal is not None and _member_role(session, principal) != "owner":
            raise HTTPException(status_code=403, detail="Only the owner can manage sharing.")
        user_id = request.user_id.strip()
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required.")
        participant = collab_repo.upsert_participant(
            session_id=session_id,
            user_id=user_id,
            role=request.role,
            display_name=request.display_name,
            added_by=principal,
        )
        payload = participant_to_dict(participant)
        await _broadcast(
            session_id,
            _collab_event(session_id, EventType.PARTICIPANT_UPDATED, {"participant": payload}),
        )
        return {"participant": payload}

    def _grant_epoch(session: Any) -> int:
        return int(((session.state_json or {}).get("collab") or {}).get("grant_epoch") or 0)

    def _bump_grant_epoch(session_id: str) -> int:
        """Invalidate every invite link minted for this session so far.

        A signed capability cannot be recalled — the holder already has the
        bytes — so revocation means changing what the server will accept.
        """
        session = agent_repo.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        state = dict(session.state_json or {})
        collab = dict(state.get("collab") or {})
        collab["grant_epoch"] = int(collab.get("grant_epoch") or 0) + 1
        state["collab"] = collab
        agent_repo.update_session(session_id, state_json=state)
        return collab["grant_epoch"]

    async def _disconnect_user(session_id: str, user_id: str) -> int:
        """Close any live socket this user holds on this session.

        The member role is resolved at connect and never re-checked, so without
        this a removed collaborator keeps streaming the session until they
        happen to reload.
        """
        dropped = 0
        for socket in list(ws_clients.get(session_id) or ()):
            if ws_principals.get(socket) != user_id:
                continue
            try:
                await socket.close(code=1008)
            except Exception:  # noqa: BLE001 - already gone is the outcome we want
                pass
            dropped += 1
        return dropped

    # ---- the signed-in person ----------------------------------------------

    @router.delete("/sessions/{session_id}")
    async def delete_session(
        session_id: str,
        confirm: str = "",
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Delete a session and everything that hangs off it.

        Owner only, and it takes the session id as confirmation: a DELETE that
        fires on a mistyped URL destroys work nobody can get back. Collaborators
        cannot delete — being able to comment on something is not the same as
        being able to erase it for its owner.
        """
        principal = await _principal_or_error(authorization=authorization)
        session = _authorize_member(session_id, principal, "view")
        if principal is not None and _member_role(session, principal) != "owner":
            raise HTTPException(
                status_code=403, detail="Only the owner can delete this session."
            )
        if confirm != session_id:
            raise HTTPException(
                status_code=400,
                detail="Pass ?confirm=<session id> to confirm deletion.",
            )
        # Anyone watching should stop watching something that no longer exists.
        for socket in list(ws_clients.get(session_id) or ()):
            try:
                await socket.close(code=1008)
            except Exception:  # noqa: BLE001 - already gone is the outcome
                pass
        try:
            collab_repo.delete_session(session_id)
        except Exception:  # noqa: BLE001 - the session row cascades anyway
            logger.debug("collab cleanup failed for %s", session_id, exc_info=True)
        # Their media, before the rows that name it. Deleting a session used to
        # reclaim the database and leave the footage in a container — which
        # makes "deleted" a word the product was using untruthfully to the one
        # person entitled to rely on it.
        media_removed, media_failed, media_unsupported = retention.delete_session_media(
            session_id,
            job_repository=async_repo,
            media_store=media_store_module.active_store(),
            # The uploaded video too — it is staged before any session exists,
            # so nothing links it to one except this field. Left alone when
            # another session was started from the same upload.
            extra_refs=tuple(
                retention.source_upload_refs(
                    session_id, repository=agent_repo, job_repository=async_repo
                )
            ),
        )
        removed = agent_repo.delete_session(session_id)
        audit.record(
            "session.deleted", kind=audit.DESTRUCTIVE, actor=principal,
            session_id=session_id, outcome="ok" if removed else "not_found",
            detail={
                "media_deleted": media_removed,
                "media_failed": len(media_failed),
                "media_unsupported": media_unsupported,
            },
        )
        return {
            "deleted": bool(removed),
            "session_id": session_id,
            # Reported, not hidden: a caller that asked for deletion is owed the
            # truth about the part that did not happen.
            "media_deleted": media_removed,
            "media_pending": len(media_failed),
        }

    @router.get("/sessions/{session_id}/export")
    async def export_session(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """Everything this session holds, as one document.

        Their footage, their direction, their transcript — a person should be
        able to leave with it. Media is referenced by URL rather than inlined:
        a JSON document with a video base64'd into it is not something a browser
        can usefully hand back.
        """
        principal = await _principal_or_error(authorization=authorization)
        _authorize_member(session_id, principal, "view")
        snapshot = agent_repo.build_snapshot(session_id)
        payload = snapshot.model_dump() if hasattr(snapshot, "model_dump") else dict(snapshot)
        try:
            payload["collab"] = build_collab_payload(collab_repo, session_id, principal or "")
        except Exception:  # noqa: BLE001 - an export must not fail on an extra
            logger.debug("collab export failed for %s", session_id, exc_info=True)
        audit.record(
            "session.exported", kind=audit.ACCESS, actor=principal,
            session_id=session_id,
        )
        return {"exported_at": _now_iso(), "session": payload}

    @router.get("/me/export")
    async def export_everything(
        authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """Every session this person owns, in one request.

        The per-session export is the useful one; this exists so "give me my
        data" is a single act rather than a list the person has to walk.
        """
        principal = await _principal_or_error(authorization=authorization)
        if principal is None:
            raise HTTPException(status_code=401, detail="Sign in to export your data.")
        profile = None
        try:
            profile = collab_repo.get_user(principal)
        except Exception:  # noqa: BLE001
            logger.debug("profile lookup failed", exc_info=True)
        sessions = []
        for row in agent_repo.list_sessions(creator_user_id=principal, limit=500):
            try:
                snap = agent_repo.build_snapshot(row["session_id"])
                sessions.append(snap.model_dump() if hasattr(snap, "model_dump") else dict(snap))
            except Exception:  # noqa: BLE001 - one bad session must not sink the export
                logger.debug("export skipped %s", row.get("session_id"), exc_info=True)
        return {
            "exported_at": _now_iso(),
            "user": {"user_id": principal, **(profile or {})},
            "sessions": sessions,
        }

    @router.get("/auth/config")
    async def auth_config() -> dict[str, Any]:
        """What the console needs to offer a sign-in, and whether it can.

        Public by necessity — whoever is loading the sign-in page has no
        credential by definition, so requiring one to learn HOW to sign in
        would make signing in impossible. It carries only the identity
        provider's public client identifiers, built key by key rather than
        dumped from settings, so a field added later cannot silently become
        public.

        `configured: false` is not an error: the console renders "sign-in is not
        available here" instead of a blank page whose only explanation is in the
        browser console.
        """
        from .identity import identity_configured, legacy_tokens_enabled

        project_id = os.getenv("AGENTIC_AUDIO_IDP_PROJECT_ID", "").strip()
        api_key = os.getenv("AGENTIC_AUDIO_IDP_API_KEY", "").strip()
        ready = bool(identity_configured() and project_id and api_key)
        return {
            "auth_required": auth_enabled(),
            "configured": ready,
            "legacy_tokens_accepted": legacy_tokens_enabled(),
            # Where sign-in actually lives. The console used to hard-code the
            # platform console's path, which exists on the platform host and
            # nowhere else: on the standalone deployment — the studio's own
            # production — the Sign in button led to a 404. A deployment that
            # signs people in somewhere else says so here.
            "signin_url": (
                os.getenv("AGENTIC_AUDIO_SIGNIN_URL", "").strip()
                or "/console/signin"
            ),
            "identity": (
                {
                    "projectId": project_id,
                    "apiKey": api_key,
                    "authDomain": os.getenv("AGENTIC_AUDIO_IDP_AUTH_DOMAIN", "").strip()
                    or f"{project_id}.firebaseapp.com",
                }
                if ready
                else None
            ),
        }

    @router.post("/tickets")
    async def mint_connection_ticket(
        purpose: str = "ws",
        session_id: str = "",
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """A short-lived credential for a request that cannot carry a header.

        A WebSocket handshake and a media element both refuse an Authorization
        header, so the caller's real token was going into the query string —
        where it lands in access logs, browser history and Referer headers, and
        travels to anyone the URL is shared with. This is minted over a normal
        authenticated request and lives about a minute.
        """
        principal = await _principal_or_error(authorization=authorization)
        if principal is None:
            # Auth is off: there is no credential to keep out of the URL, and
            # handing back a ticket would imply one exists.
            return {"ticket": "", "expires_in": 0, "purpose": purpose, "required": False}
        try:
            minted = mint_ticket(principal, purpose=purpose, session_id=session_id)
        except ValueError as exc:
            # The one detail in this router that reached a client with no
            # scrubbing at all — every other boundary already goes through it.
            raise HTTPException(status_code=400, detail=_client_detail(exc)) from exc
        return {**minted, "required": True}

    @router.get("/me")
    async def whoami(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Who the caller is, as the console should render them.

        Without this the console invents a persona in localStorage, so the same
        person is a different name on every device and an owner appears to
        collaborators as a raw principal.
        """
        try:
            caller = await _caller_or_error(authorization=authorization)
        except HTTPException as exc:
            # A missing or bad credential is the honest answer to this question,
            # not an error: the console asks it precisely when nobody is signed
            # in yet. A real outage (503) still surfaces as one.
            if exc.status_code == 401:
                return {"authenticated": False}
            raise
        if caller is None:
            return {"authenticated": False}
        profile = None
        try:
            profile = collab_repo.get_user(caller.principal)
        except Exception:  # noqa: BLE001
            logger.debug("get_user failed", exc_info=True)
        return {
            "authenticated": True,
            "user_id": caller.principal,
            "display_name": (profile or {}).get("display_name") or caller.display_name or "",
            "auth_source": caller.source,
        }

    @router.put("/me")
    async def set_profile(
        request: ProfileRequest, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """Set the name collaborators see."""
        principal = await _principal_or_error(authorization=authorization)
        if principal is None:
            raise HTTPException(status_code=401, detail="Sign in to set a profile.")
        name = (request.display_name or "").strip()
        collab_repo.set_display_name(principal, name)
        return {"user_id": principal, "display_name": name}

    @router.delete("/me")
    async def delete_account(
        confirm: str = "",
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Erase the account record and every collaboration it took part in.

        Sessions the person OWNS are deliberately left alone. Deleting an
        account and destroying work other people are still collaborating on are
        different acts, and doing the second silently because someone asked for
        the first is not recoverable. The response says exactly what was left
        behind so the caller can decide about it.
        """
        principal = await _principal_or_error(authorization=authorization)
        if principal is None:
            raise HTTPException(status_code=401, detail="Sign in to delete an account.")
        if confirm != principal:
            raise HTTPException(
                status_code=400,
                detail="Pass ?confirm=<your user id> to confirm account deletion.",
            )
        owned = [
            row["session_id"]
            for row in agent_repo.list_sessions(creator_user_id=principal, limit=500)
        ]
        for session_id in owned:
            await _disconnect_user(session_id, principal)
        deleted = collab_repo.delete_user(principal)
        audit.record(
            "account.deleted", kind=audit.DESTRUCTIVE, actor=principal,
            subject=principal, detail={"sessions_retained": len(owned)},
        )
        return {
            "deleted": bool(deleted),
            "sessions_retained": owned,
            "note": (
                "Collaboration memberships were removed. Sessions you own were "
                "kept — delete them individually if you want them gone."
            ),
        }

    @router.delete("/sessions/{session_id}/collab/participants/{user_id}")
    async def remove_participant(
        session_id: str,
        user_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Remove a collaborator, and disconnect them if they are connected.

        Sharing was one-way: a session could be shared but never unshared, so
        the only way to take access back was to abandon the session.
        """
        principal = await _principal_or_error(authorization=authorization)
        session = _authorize_member(session_id, principal, "view")
        if principal is not None and _member_role(session, principal) != "owner":
            raise HTTPException(
                status_code=403, detail="Only the owner can remove a collaborator."
            )
        if (session.creator_user_id or "") == user_id:
            raise HTTPException(
                status_code=400,
                detail="The owner cannot be removed from their own session.",
            )
        removed = collab_repo.remove_participant(session_id, user_id)
        dropped = await _disconnect_user(session_id, user_id)
        audit.record(
            "collaborator.removed", kind=audit.ACCESS, actor=principal,
            session_id=session_id, subject=user_id,
            detail={"disconnected": dropped},
        )
        return {"removed": bool(removed), "disconnected": dropped}

    @router.post("/sessions/{session_id}/collab/links/revoke")
    async def revoke_share_links(
        session_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Invalidate every invite link minted for this session so far.

        People who already joined keep their access — they are participants now,
        not link holders. This only stops the link itself from working again.
        """
        principal = await _principal_or_error(authorization=authorization)
        session = _authorize_member(session_id, principal, "view")
        if principal is not None and _member_role(session, principal) != "owner":
            raise HTTPException(
                status_code=403, detail="Only the owner can revoke invite links."
            )
        epoch = _bump_grant_epoch(session_id)
        audit.record(
            "invite_links.revoked", kind=audit.ACCESS, actor=principal,
            session_id=session_id, detail={"epoch": epoch},
        )
        return {"revoked": True, "epoch": epoch}

    @router.post("/sessions/{session_id}/collab/links")
    async def mint_collab_link(
        session_id: str,
        request: CollabShareLinkRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """An owner-minted invite grant. The grant carries capability (session,
        role, expiry) — never identity; the recipient redeems it as themselves."""
        principal = await _principal_or_error(authorization=authorization)
        session = _authorize_member(session_id, principal, "view")
        if principal is not None and _member_role(session, principal) != "owner":
            raise HTTPException(status_code=403, detail="Only the owner can mint invite links.")
        audit.record(
            "invite_link.minted", kind=audit.ACCESS, actor=principal,
            session_id=session_id, detail={"role": request.role},
        )
        return {
            "grant": mint_share_grant(
                session_id, request.role, epoch=_grant_epoch(session)
            ),
            "role": request.role,
        }

    @router.post("/sessions/{session_id}/collab/join")
    async def join_via_grant(
        session_id: str,
        request: CollabJoinRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Redeem an invite grant: register the CALLER as a participant at the
        granted role. With auth on the identity is the bearer principal; the
        grant only says what a link-holder may become."""
        principal = await _principal_or_error(authorization=authorization)
        session = agent_repo.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        try:
            role = verify_share_grant(
                request.grant, session_id, epoch=_grant_epoch(session)
            )
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        user_id = principal if principal is not None else (request.user_id or "").strip()
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required when auth is off.")
        # The owner opening their own invite link keeps ownership.
        if (session.creator_user_id or "") == user_id:
            return {"participant": None, "role": "owner"}
        # A grant never demotes: an existing participant keeps the higher role.
        existing = collab_repo.get_participant(session_id, user_id)
        if existing and role_at_least(existing.role, role):
            return {"participant": participant_to_dict(existing), "role": existing.role}
        participant = collab_repo.upsert_participant(
            session_id=session_id,
            user_id=user_id,
            role=role,
            display_name=(request.display_name or "").strip() or None,
            added_by="share_link",
        )
        payload = participant_to_dict(participant)
        await _broadcast(
            session_id,
            _collab_event(session_id, EventType.PARTICIPANT_UPDATED, {"participant": payload}),
        )
        return {"participant": payload, "role": role}

    @router.websocket("/sessions/{session_id}/ws")
    async def session_ws(websocket: WebSocket, session_id: str) -> None:
        def _ws_error(code: int, message: str) -> dict[str, Any]:
            return {
                "event_type": EventType.ERROR.value,
                "session_id": session_id,
                "payload": {"message": message},
            }

        # A cross-origin page cannot be allowed to open an authenticated socket
        # against a session. The WebSocket handshake is NOT covered by the same
        # -origin policy and carries no preflight, so the Origin header is the
        # only thing standing between a user's session and any page they visit
        # while signed in.
        origin = (websocket.headers.get("origin") or "").strip()
        if origin and not _origin_allowed(origin):
            # Refuse the handshake outright: never accept and then complain.
            await websocket.close(code=1008)
            return

        # Auth: browsers cannot set WS headers, so accept a `?token=` query param
        # (also honor an Authorization header for non-browser clients).
        #
        # Resolved BEFORE accept(). Accepting first means an unauthenticated
        # peer holds an open socket while we decide, and every rejection path
        # then has to remember to close it.
        ticket = websocket.query_params.get("ticket")
        try:
            if ticket:
                # Preferred: a one-shot credential bound to this purpose and,
                # when the client supplies it, to this session.
                principal = redeem_ticket(
                    ticket, purpose=PURPOSE_WS, session_id=session_id
                )
            else:
                caller = await resolve_caller(
                    authorization=websocket.headers.get("authorization"),
                    token=websocket.query_params.get("token"),
                )
                principal = caller.principal if caller else None
                if websocket.query_params.get("token") and principal:
                    _warn_url_token_once()
        except AuthError:
            await websocket.close(code=1008)
            return

        await websocket.accept()

        try:
            session = agent_repo.get_session(session_id)
            if session is None:
                raise KeyError(session_id)
            # Membership-aware: shared sessions open for every participant
            # (view+); auth off keeps the historical owner-implicit behavior.
            member_role = _member_role(session, principal)
            if member_role is None:
                raise AuthError(403, "You do not have access to this session.")
            agent_planner.refresh_session_state(session_id)
            snapshot = agent_repo.build_snapshot(session_id)
        except AuthError as exc:
            await websocket.send_json(_ws_error(exc.status_code, exc.detail))
            await websocket.close(code=1008)
            return
        except KeyError:
            await websocket.send_json(_ws_error(404, f"Session not found: {session_id}"))
            await websocket.close(code=1008)
            return

        # Register on the collab fan-out hub BEFORE the snapshot send, so no
        # broadcast can slip into the gap between hydrate and subscribe.
        ws_clients.setdefault(session_id, set()).add(websocket)
        if principal:
            ws_principals[websocket] = principal
        try:
            await websocket.send_json(snapshot_opened_event(snapshot))
            await _session_ws_loop(websocket, session_id, member_role, principal)
        finally:
            ws_principals.pop(websocket, None)
            remaining = ws_clients.get(session_id)
            if remaining is not None:
                remaining.discard(websocket)
                if not remaining:
                    ws_clients.pop(session_id, None)

    async def _session_ws_loop(
        websocket: WebSocket,
        session_id: str,
        member_role: str,
        member_principal: Optional[str] = None,
    ) -> None:
        # A client can vanish mid-turn (close, refresh, network drop). Sending on a
        # closed socket raises, and a second send in the error branch raised an
        # unhandled RuntimeError (adversarial review P2). `_send` swallows the
        # closed-socket case and reports it so the turn aborts cleanly.
        class _SocketGone(Exception):
            pass

        def _record_turn_failure(session_id: str, message: str) -> None:
            """Put a failed turn's explanation in the transcript, not only on the wire.

            The failure path was the one turn outcome rendered ephemerally: the
            user got an error frame, and a glance away or a reconnect took the
            only account of why their turn did nothing. Every other outcome —
            an answer, a clarification, a refusal to spend — is persisted and
            re-readable. This makes the unhappy path as durable as the happy one,
            and gives the owner something to read afterwards.

            Recording must never be the reason a turn fails twice, so it cannot
            raise; the message on the wire is what the user is owed first.
            """

            try:
                agent_planner.repository.append_message(
                    session_id=session_id,
                    role=AgenticMessageRole.ASSISTANT,
                    content=message,
                    payload_json={"turn_outcome": "failed"},
                )
            except Exception:  # noqa: BLE001 — the wire already carried the truth
                logger.debug("could not persist the turn failure for %s", session_id)

        # Set from each inbound frame and echoed on everything that frame
        # produces. A client that can have more than one request in flight —
        # a user clicking again while a turn streams, a reconnect replaying —
        # otherwise has no way to tell which request a frame belongs to, and
        # guesses by arrival order, which is exactly wrong when turns overlap.
        in_flight_request_id: Optional[str] = None

        async def _send(obj: Any) -> None:
            try:
                if in_flight_request_id and isinstance(obj, dict):
                    obj = {**obj, "request_id": in_flight_request_id}
                await websocket.send_json(obj)
            except (WebSocketDisconnect, RuntimeError) as exc:
                # RuntimeError: "Cannot call send once a close message has been
                # sent" — the client is gone; stop the turn instead of crashing.
                raise _SocketGone() from exc

        while True:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                return
            # Starlette signals disconnect as a message type too.
            if message.get("type") == "websocket.disconnect":
                return
            raw = message.get("text")
            if raw is None:
                # Binary (or otherwise non-text) frame: answer gracefully and keep
                # the socket open, rather than crashing on a missing "text" key
                # (adversarial review P3). Text is the only supported frame type.
                try:
                    await _send({
                        "event_type": EventType.ERROR.value,
                        "session_id": session_id,
                        "payload": {"message": "Only text frames are supported."},
                    })
                except _SocketGone:
                    return
                continue
            # Turn frames branch/lock/generate (and spend) — same bar as the
            # REST /messages and /choices endpoints: iterate or better. The
            # role is fixed at connect time; a role change requires reconnect.
            if not role_at_least(member_role, "iterate"):
                try:
                    await _send(_collab_event(
                        session_id, EventType.ERROR,
                        {"message": f"Your role ({member_role}) can't direct the session — needs iterate access."},
                    ))
                except _SocketGone:
                    return
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"content": raw}
            # Whatever this frame produces is answered with its own id, so a
            # client with overlapping requests can match reply to request
            # instead of inferring it from arrival order. Bounded and coerced:
            # it is client-controlled and rides back out on every frame.
            in_flight_request_id = (
                str(payload.get("request_id"))[:120]
                if isinstance(payload, dict) and payload.get("request_id")
                else None
            )
            # Who is acting, per FRAME. The HTTP middleware binds this for
            # ordinary requests; a socket has no middleware above it, so every
            # socket-driven turn — which is how the console actually talks to
            # the agent — logged and AUDITED with no principal at all. An audit
            # trail whose actor is blank for the main path is not a trail.
            _bind_log_context(
                member_principal,
                session_id,
                request_id=in_flight_request_id or _new_request_id(),
            )
            if not isinstance(payload, dict):
                # Valid JSON that isn't a frame object (a bare list/number/string).
                try:
                    await _send({
                        "event_type": EventType.ERROR.value,
                        "session_id": session_id,
                        "payload": {"message": "Expected a JSON object frame."},
                    })
                except _SocketGone:
                    return
                continue
            # Stream each event to the client LIVE as the turn unfolds, so the UI
            # shows real reasoning beats, one per step, rather than waiting for
            # the whole turn to finish before anything appears. Peers on the same
            # session hear the turn too (collab: watch generation live).
            async def _emit(event: Any) -> None:
                evt = event_payload(event)
                await _send(evt)
                await _broadcast_peers(session_id, evt, websocket)

            try:
                from .limits import LimitExceeded, limiter

                lim = limiter()
                spending = (
                    payload.get("choice_type") in _SPENDING_CHOICES
                    if "choice_type" in payload
                    else True
                )
                # A malformed frame still costs a TURN slot. It is cheap to send
                # and not free to handle, and metering only well-formed frames
                # would leave the socket floodable with rejects.
                try:
                    lim.check_turn(member_principal)
                except LimitExceeded as exc:
                    await _send(
                        {
                            "event_type": EventType.ERROR.value,
                            "session_id": session_id,
                            "payload": {"message": exc.detail},
                        }
                    )
                    continue

                # Validate the message BEFORE the generation ceiling is charged:
                # every non-choice frame counts as spending, so an empty or
                # oversized frame used to burn the caller's daily budget on a turn
                # that was never going to run. REST caps length through pydantic
                # (MAX_MESSAGE_CHARS); the socket had no cap at all, so a huge
                # frame landed in the transcript and rode in every prompt built
                # from it afterwards. The JSON-decode fallback above turns a raw
                # text frame into {"content": raw}, so this covers those too.
                content = ""
                choice: Optional[AgenticAudioChoiceRequest] = None
                if "choice_type" not in payload:
                    content = str(payload.get("content") or "")
                    problem = ""
                    if not content.strip():
                        problem = "content is required."
                    elif len(content) > MAX_MESSAGE_CHARS:
                        problem = (
                            f"content is too long (max {MAX_MESSAGE_CHARS} characters)."
                        )
                    if problem:
                        await _send(
                            {
                                "event_type": EventType.ERROR.value,
                                "session_id": session_id,
                                "payload": {"message": problem},
                            }
                        )
                        continue
                else:
                    # The same fix, for the frames it was never applied to. A
                    # choice frame was parsed AFTER the generation ceiling was
                    # charged, so a malformed one — an unknown choice_type, a
                    # payload of the wrong shape — spent a slot out of the
                    # caller's daily budget on a turn that could never run.
                    try:
                        choice = AgenticAudioChoiceRequest(**payload)
                    except (ValidationError, TypeError) as exc:
                        await _send(
                            {
                                "event_type": EventType.ERROR.value,
                                "session_id": session_id,
                                "payload": {
                                    "message": "That choice isn't one I can act on.",
                                    "detail": str(exc)[:300],
                                },
                            }
                        )
                        continue

                try:
                    if spending:
                        lim.check_generation(member_principal)
                        lim.record_generation(member_principal)
                except LimitExceeded as exc:
                    await _send(
                        {
                            "event_type": EventType.ERROR.value,
                            "session_id": session_id,
                            "payload": {"message": exc.detail},
                        }
                    )
                    continue
                if choice is not None:
                    # Parsed above, before the limiter charged for it.
                    await agent_planner.handle_choice(
                        session_id=session_id,
                        request=choice,
                        emit=_emit,
                    )
                else:
                    # Validated above, before the limiter charged for it.
                    await agent_planner.handle_user_message(
                        session_id=session_id,
                        content=content,
                        payload=dict(payload.get("payload") or {}),
                        emit=_emit,
                    )
                # Events were already streamed via _emit; just send the final snapshot
                # — to the initiator AND to peers, so every viewer reconciles.
                agent_planner.refresh_session_state(session_id)
                snap_evt = snapshot_opened_event(agent_repo.build_snapshot(session_id))
                await _broadcast_peers(session_id, snap_evt, websocket)
                await _send(snap_evt)
            except _SocketGone:
                # Client left mid-turn — a normal condition, not an error to send.
                return
            except SessionBusy:
                try:
                    await _send({
                        "event_type": EventType.ERROR.value,
                        "session_id": session_id,
                        "payload": {"message": (
                            "This session is still working on your last message."
                        )},
                    })
                except _SocketGone:
                    return
                continue
            except (KeyError, ValueError) as exc:
                # Crafted guidance ("Candidate not found: …", "content is
                # required.") — REST surfaces these as 400/404 detail, so mirror
                # them on WS instead of collapsing to the generic error. Includes
                # ApprovalRequiredError (a ValueError subclass). Still scrubbed.
                logger.info("Agentic audio WS turn rejected: %s", exc)
                reason = scrub_provider_names(str(exc))
                _record_turn_failure(session_id, reason)
                try:
                    await _send(
                        {
                            "event_type": EventType.ERROR.value,
                            "session_id": session_id,
                            "payload": {"message": reason},
                        }
                    )
                except _SocketGone:
                    return
            except Exception as exc:
                # Never surface the raw exception (it can name the upstream
                # provider or internal model identifiers).
                logger.exception("Agentic audio WS turn failed: %s", exc)
                payload_out = public_error_payload(exc)
                _record_turn_failure(
                    session_id,
                    str(payload_out.get("message") or "Something went wrong on that turn."),
                )
                try:
                    await _send(
                        {
                            "event_type": EventType.ERROR.value,
                            "session_id": session_id,
                            "payload": payload_out,
                        }
                    )
                except _SocketGone:
                    return

    # Published so the host can ask what is still running. A shutdown that
    # cannot see in-flight turns cannot wait for them, and a turn killed
    # mid-render has usually already spent.
    router.agentic_audio_planner = agent_planner  # type: ignore[attr-defined]
    return router


__all__ = [
    "AgenticAudioActionResponse",
    "agentic_audio_enabled",
    "create_agentic_audio_router",
    "mount_agentic_audio_router",
]
