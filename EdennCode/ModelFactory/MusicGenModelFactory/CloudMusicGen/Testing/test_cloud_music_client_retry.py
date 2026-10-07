"""Regression tests for the cloud music client's HTTP retry path.

Background
----------
The client's ``_json_request`` retry loop logs a warning on each retryable
failure (a transient transport error or a 429/5xx response). Those two
``logger.warning`` calls referenced a module-level ``logger`` that was never
defined, so the *first time either retry branch was hit* the process raised
``NameError`` instead of retrying — silently disabling the entire
retry/backoff protection for the studio-tier music provider. See
``EdennCode/Deployment/PROD_READINESS_REMEDIATION_PLAN.md`` (finding #8).

Test strategy (real integration, no provider credits)
-----------------------------------------------------
These tests drive the *real* async HTTP client against a *real* local HTTP
server bound to a loopback port. Nothing is mocked: the actual ``httpx``
client, the actual retry loop, the actual backoff sleep, and the actual
logging call all execute end to end over real sockets. The server is
programmed to fail the first request (503, or a slow response that trips the
client's read timeout) and then succeed, so a passing test proves the retry
branch runs without ``NameError`` and recovers. Before the fix these tests
error out with ``NameError: name 'logger' is not defined``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import ProviderCApi


class _ServerState:
    """Thread-safe request counter + failure programming for the fake server."""

    def __init__(self, *, mode: str, fail_times: int, slow_seconds: float = 0.0) -> None:
        self.mode = mode
        self.fail_times = fail_times
        self.slow_seconds = slow_seconds
        self._count = 0
        self._lock = threading.Lock()

    def bump(self) -> int:
        with self._lock:
            self._count += 1
            return self._count

    @property
    def count(self) -> int:
        with self._lock:
            return self._count


class _FlakyHandler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs) -> None:  # keep test output clean
        pass

    def _safe_write(self, payload: bytes) -> None:
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, OSError):
            # The client may have already timed out and closed the socket on
            # the intentionally-slow first request; that is expected.
            pass

    def _respond(self) -> None:
        state: _ServerState = self.server.state  # type: ignore[attr-defined]
        n = state.bump()
        should_fail = n <= state.fail_times

        if should_fail and state.mode == "http_5xx":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if should_fail and state.mode == "timeout":
            # Sleep past the client's per-request timeout, then respond. The
            # client should already have given up and retried by the time this
            # write happens (hence _safe_write).
            time.sleep(state.slow_seconds)

        body = json.dumps({"code": 200, "data": {"ok": True, "attempt": n}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._safe_write(body)

    do_GET = _respond
    do_POST = _respond


def _start_server(state: _ServerState):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FlakyHandler)
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _run_probe(base_url: str, *, timeout_s: float | None = None) -> dict:
    async def _run() -> dict:
        api = ProviderCApi(
            api_key="test-key",
            base_url=base_url,
            max_retries=2,
            retry_backoff_s=0.2,
        )
        try:
            return await api._json_request("POST", "/probe", payload={"x": 1}, timeout_s=timeout_s)
        finally:
            await api.aclose()

    return asyncio.run(_run())


def test_module_exposes_logger() -> None:
    """Structural guard: the retry branches reference this module logger."""
    from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen import provider_c

    assert isinstance(provider_c.logger, logging.Logger)


def test_retry_path_recovers_from_transient_5xx() -> None:
    state = _ServerState(mode="http_5xx", fail_times=1)
    server = _start_server(state)
    try:
        port = server.server_address[1]
        data = _run_probe(f"http://127.0.0.1:{port}")
        # One 503, then a 200 -> retried exactly once and succeeded.
        assert data == {"ok": True, "attempt": 2}
        assert state.count == 2
    finally:
        server.shutdown()


def test_retry_path_recovers_from_transient_timeout() -> None:
    state = _ServerState(mode="timeout", fail_times=1, slow_seconds=1.0)
    server = _start_server(state)
    try:
        port = server.server_address[1]
        # Per-request timeout well below the first response's 1.0s sleep, so the
        # first attempt raises a real read timeout and the transport-retry
        # branch runs.
        data = _run_probe(f"http://127.0.0.1:{port}", timeout_s=0.3)
        assert data["ok"] is True
        assert state.count == 2
    finally:
        server.shutdown()
