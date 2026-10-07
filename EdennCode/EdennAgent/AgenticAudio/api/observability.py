"""Structured logs with a thread of identity through them.

The deployed console's entire telemetry was ``print()``. When a user says "my
session failed", there is nothing to search: no request id, no session id, no
user, no duration — and the lines that do exist are uvicorn's access log, which
knows none of those things either.

What this adds, deliberately small and dependency-free:

* **JSON lines on stdout**, which is what the container platform collects.
* **A correlation id per request**, honouring an inbound ``x-request-id`` so a
  trace survives a hop, and echoed back on the response so a user can quote it.
* **Context that follows the work**: once a handler binds the session and the
  principal, every log line for that request carries them — including lines
  written deep in the agent or the tools, which know nothing about the request.

Context lives in :mod:`contextvars`, so it is correct under async concurrency:
two turns in flight at once do not read each other's ids.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Optional

# The thread of identity. Each is set for the duration of one request.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="")
principal_var: contextvars.ContextVar[str] = contextvars.ContextVar("principal", default="")

REQUEST_ID_HEADER = "x-request-id"

_CONTEXT_KEYS = ("request_id", "session_id", "principal")

# Fields logging puts on every record; anything else the caller passed via
# `extra=` is a field we want in the JSON.
_STANDARD = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


class ContextFilter(logging.Filter):
    """Attach the current request's identity to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("")
        record.session_id = session_id_var.get("")
        record.principal = principal_var.get("")
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    Never raises: a formatter that throws takes down the thing it was supposed
    to be observing, and an unserialisable ``extra`` is not worth losing a log
    line over.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _CONTEXT_KEYS:
            value = getattr(record, key, "")
            if value:
                payload[key] = value
        for key, value in record.__dict__.items():
            # The context keys are emitted above, and only when set — without
            # this they come back through the generic sweep as empty strings.
            if key in _CONTEXT_KEYS or key in _STANDARD or key in payload or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            return json.dumps({"level": record.levelname, "message": record.getMessage()})


def configure_logging(*, level: Optional[str] = None, json_output: Optional[bool] = None) -> None:
    """Install the JSON handler on the root logger.

    Idempotent, because it runs at app build time and an app can be built more
    than once in a test session. Plain-text stays available for a local run,
    where JSON on a terminal is just harder to read.
    """

    resolved_level = (level or os.getenv("EDENN_LOG_LEVEL", "INFO")).upper()
    if json_output is None:
        json_output = os.getenv("EDENN_LOG_FORMAT", "json").strip().lower() == "json"

    root = logging.getLogger()
    root.setLevel(resolved_level)

    # Take ownership of root — not merely of our own previous handler. Several
    # workflow modules call logging.basicConfig at IMPORT time, so importing the
    # music pipeline (which this server does) installs a second root handler and
    # every line goes out twice, once plain and once as JSON. On a deployment
    # whose only log window is a 300-line kubelet tail, a duplicate halves how
    # far back an operator can see at the moment they most need to look.
    for existing in list(root.handlers):
        root.removeHandler(existing)

    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    handler.addFilter(ContextFilter())
    handler._edenn_configured = True  # type: ignore[attr-defined]
    root.addHandler(handler)

    # uvicorn installs its own handlers; let it propagate to ours instead so the
    # access log lands in the same stream, in the same shape.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


def bind_request(request_id: str = "", *, session_id: str = "", principal: str = "") -> None:
    """Bind identity for the current context. Empty values are left alone."""

    if request_id:
        request_id_var.set(request_id)
    if session_id:
        session_id_var.set(session_id)
    if principal:
        principal_var.set(principal)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


_SESSION_PATH = re.compile(r"/sessions/([^/]+)")


def _session_id_from_path(path: str) -> str:
    """The session id in a URL, before routing has parsed one out."""

    match = _SESSION_PATH.search(path or "")
    return match.group(1) if match else ""


def install_request_context(app: Any, *, logger: Optional[logging.Logger] = None) -> None:
    """Give every request an id, and log how it went.

    Registered as a middleware so it covers routes this module has never heard
    of — including the ones the devserver mounts itself.
    """

    log = logger or logging.getLogger("edenn.request")

    @app.middleware("http")
    async def _request_context(request, call_next):  # type: ignore[no-untyped-def]
        incoming = request.headers.get(REQUEST_ID_HEADER, "").strip()
        rid = incoming[:64] or new_request_id()
        request_id_var.set(rid)
        # `request.path_params` is empty here: middleware runs BEFORE routing, so
        # nothing has matched a path template yet. Read the id off the raw path
        # instead, and let the endpoint refine it later via bind_request().
        session_id_var.set(_session_id_from_path(request.url.path))
        principal_var.set("")
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers[REQUEST_ID_HEADER] = rid
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            # Health probes fire constantly and say nothing; logging them buries
            # the requests that matter.
            if request.url.path not in {"/healthz", "/readyz"}:
                log.info(
                    "%s %s -> %s",
                    request.method,
                    request.url.path,
                    status,
                    extra={
                        "http_method": request.method,
                        "http_path": request.url.path,
                        "http_status": status,
                        "duration_ms": duration_ms,
                    },
                )


__all__ = [
    "ContextFilter",
    "JsonFormatter",
    "REQUEST_ID_HEADER",
    "bind_request",
    "configure_logging",
    "install_request_context",
    "new_request_id",
    "principal_var",
    "request_id_var",
    "session_id_var",
]
