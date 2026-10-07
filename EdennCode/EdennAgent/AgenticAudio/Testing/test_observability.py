"""Structured logs, and the thread of identity through them.

The question these exist to answer: a user says "my session failed an hour ago".
Can we find it? That needs a request id, the session, the user, and a status —
on lines the platform can search, from code that never sees the request.
"""

from __future__ import annotations

import importlib
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.EdennAgent.AgenticAudio.api.observability import (
    REQUEST_ID_HEADER,
    ContextFilter,
    JsonFormatter,
    bind_request,
    configure_logging,
    install_request_context,
    principal_var,
    request_id_var,
    session_id_var,
)


def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="edenn.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello %s", args=("world",), exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# ---------------------------------------------------------------------------#
# the formatter                                                               #
# ---------------------------------------------------------------------------#


def test_a_log_line_is_one_json_object() -> None:
    out = JsonFormatter().format(_record())
    payload = json.loads(out)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "edenn.test"
    assert payload["ts"].endswith("Z")


def test_extra_fields_survive_into_the_json() -> None:
    """Structured fields are the point — a message string cannot be aggregated."""
    out = JsonFormatter().format(_record(duration_ms=12.5, http_status=200))
    payload = json.loads(out)
    assert payload["duration_ms"] == 12.5
    assert payload["http_status"] == 200


def test_an_unserialisable_field_does_not_lose_the_line() -> None:
    """A formatter that raises takes down the thing it was observing."""
    out = JsonFormatter().format(_record(weird=object()))
    payload = json.loads(out)
    assert payload["message"] == "hello world"
    assert isinstance(payload["weird"], str)


def test_an_exception_is_captured_as_text() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


# ---------------------------------------------------------------------------#
# identity follows the work                                                   #
# ---------------------------------------------------------------------------#


def test_bound_context_lands_on_every_record() -> None:
    request_id_var.set("req_abc")
    session_id_var.set("sess_1")
    principal_var.set("alice")
    record = _record()
    ContextFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["request_id"] == "req_abc"
    assert payload["session_id"] == "sess_1"
    assert payload["principal"] == "alice"


def test_unbound_context_is_omitted_rather_than_logged_empty() -> None:
    request_id_var.set("")
    session_id_var.set("")
    principal_var.set("")
    record = _record()
    ContextFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert "request_id" not in payload
    assert "principal" not in payload


def test_binding_leaves_unset_values_alone() -> None:
    request_id_var.set("req_1")
    principal_var.set("alice")
    bind_request(session_id="sess_9")  # only the session
    assert request_id_var.get() == "req_1"
    assert principal_var.get() == "alice"
    assert session_id_var.get() == "sess_9"


# ---------------------------------------------------------------------------#
# the middleware                                                              #
# ---------------------------------------------------------------------------#


@pytest.fixture()
def app_with_context() -> FastAPI:
    app = FastAPI()
    install_request_context(app)

    @app.get("/ping")
    async def ping() -> dict:
        return {"request_id": request_id_var.get("")}

    @app.get("/sessions/{session_id}/thing")
    async def thing(session_id: str) -> dict:
        return {"session_id": session_id_var.get("")}

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("kaboom")

    return app


def test_every_request_gets_an_id_and_echoes_it_back(app_with_context: FastAPI) -> None:
    """Echoed so a user can quote the id from a failure they saw."""
    client = TestClient(app_with_context)
    r = client.get("/ping")
    assert r.status_code == 200
    assert r.headers[REQUEST_ID_HEADER]
    assert r.json()["request_id"] == r.headers[REQUEST_ID_HEADER]


def test_an_inbound_request_id_is_honoured_so_a_trace_survives_a_hop(
    app_with_context: FastAPI,
) -> None:
    client = TestClient(app_with_context)
    r = client.get("/ping", headers={REQUEST_ID_HEADER: "upstream-123"})
    assert r.json()["request_id"] == "upstream-123"
    assert r.headers[REQUEST_ID_HEADER] == "upstream-123"


def test_an_absurd_inbound_id_is_truncated_not_trusted(app_with_context: FastAPI) -> None:
    client = TestClient(app_with_context)
    r = client.get("/ping", headers={REQUEST_ID_HEADER: "x" * 500})
    assert len(r.json()["request_id"]) <= 64


def test_the_session_id_is_bound_from_the_path(app_with_context: FastAPI) -> None:
    client = TestClient(app_with_context)
    assert client.get("/sessions/sess_42/thing").json()["session_id"] == "sess_42"


def test_a_completed_request_is_logged_with_status_and_duration(
    app_with_context: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="edenn.request"):
        TestClient(app_with_context).get("/ping")
    entry = next(r for r in caplog.records if r.name == "edenn.request")
    assert entry.http_status == 200
    assert entry.http_path == "/ping"
    assert entry.duration_ms >= 0


def test_a_failed_request_is_still_logged(
    app_with_context: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """The failures are the ones worth finding later."""
    with caplog.at_level(logging.INFO, logger="edenn.request"):
        with pytest.raises(RuntimeError):
            TestClient(app_with_context).get("/boom")
    entry = next(r for r in caplog.records if r.name == "edenn.request")
    assert entry.http_status == 500
    assert entry.http_path == "/boom"


def test_health_probes_are_not_logged(
    app_with_context: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """They fire constantly and say nothing; logging them buries the rest."""
    with caplog.at_level(logging.INFO, logger="edenn.request"):
        TestClient(app_with_context).get("/healthz")
    assert not [r for r in caplog.records if r.name == "edenn.request"]


# ---------------------------------------------------------------------------#
# wired into the deployed app                                                 #
# ---------------------------------------------------------------------------#


def test_the_devserver_logs_json_with_a_request_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """End to end on the app the container actually runs."""
    monkeypatch.setenv("EDENN_DEV_REAL_MUSIC", "0")
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")
    monkeypatch.setenv("EDENN_LOG_FORMAT", "json")
    for marker in ("CONTAINER_APP_NAME", "WEBSITE_HOSTNAME", "EDENN_PUBLIC_BASE_URL"):
        monkeypatch.delenv(marker, raising=False)

    mod = importlib.import_module("EdennCode.EdennAgent.AgenticAudio.design.devserver")
    app = mod.build_app()
    with TestClient(app) as client:
        r = client.get("/dev/info")
        assert REQUEST_ID_HEADER in r.headers

    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("{")]
    assert lines, "the deployed app emitted no structured log lines"
    entry = json.loads(lines[-1])
    assert "level" in entry and "message" in entry


# ---------------------------------------------------------------------------#
# the poll is supposed to settle                                              #
# ---------------------------------------------------------------------------#


def test_a_session_that_never_settles_says_so_in_the_log(caplog) -> None:
    """Hydration re-derives a projection: once the jobs finish it stops
    changing, the equality guard sees no difference, and nothing is written. A
    value that is not byte-stable breaks that and the session is rewritten
    every 2.5 seconds, forever, with no symptom a user could report. The last
    one was found by reading the code."""
    import logging
    from types import SimpleNamespace

    from EdennCode.EdennAgent.AgenticAudio.agent.agent import AgenticAudioAgent

    agent = SimpleNamespace(
        _hydration_writes={},
        _HYDRATION_CHURN_WRITES=AgenticAudioAgent._HYDRATION_CHURN_WRITES,
        _note_hydration=AgenticAudioAgent._note_hydration,
    )

    with caplog.at_level(logging.WARNING):
        for _ in range(AgenticAudioAgent._HYDRATION_CHURN_WRITES):
            agent._note_hydration(agent, "sess_churn", wrote=True)

    assert any("churning" in r.message for r in caplog.records), caplog.text


def test_a_poll_that_settles_clears_the_count(caplog) -> None:
    """Writes happen all the time while a take renders. Only writes that never
    STOP are a fault, so convergence has to reset the count — otherwise a long
    healthy session eventually cries wolf."""
    import logging
    from types import SimpleNamespace

    from EdennCode.EdennAgent.AgenticAudio.agent.agent import AgenticAudioAgent

    agent = SimpleNamespace(
        _hydration_writes={},
        _HYDRATION_CHURN_WRITES=AgenticAudioAgent._HYDRATION_CHURN_WRITES,
        _note_hydration=AgenticAudioAgent._note_hydration,
    )

    with caplog.at_level(logging.WARNING):
        for _ in range(AgenticAudioAgent._HYDRATION_CHURN_WRITES - 1):
            agent._note_hydration(agent, "sess_ok", wrote=True)
        agent._note_hydration(agent, "sess_ok", wrote=False)   # it settled
        for _ in range(AgenticAudioAgent._HYDRATION_CHURN_WRITES - 1):
            agent._note_hydration(agent, "sess_ok", wrote=True)

    assert not any("churning" in r.message for r in caplog.records)
    assert "sess_ok" in agent._hydration_writes
