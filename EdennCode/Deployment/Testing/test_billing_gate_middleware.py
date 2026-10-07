"""Billing gate tests: verdict matrix, mode behavior, middleware consult rules."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient
import pytest

from EdennCode.Deployment.auth.key_store import Principal
from EdennCode.Deployment.auth.middleware import (
    _apply_balance_warning,
    create_auth_middleware,
    get_principal,
)
from EdennCode.Deployment.billing import set_billing_override
from EdennCode.Deployment.billing.engine import BillingEngine
from EdennCode.Deployment.billing.gate import create_billing_gate
from EdennCode.Deployment.billing.stores import AccountStore
from EdennCode.Deployment.Testing.test_auth_middleware import StubKeyStore
from EdennCode.Deployment.Testing.test_billing_stores import FakeBillingTable

PRINCIPAL = Principal(user_id="user-1", key_prefix="sk-abc123def")
_REQ = SimpleNamespace(method="POST", url=SimpleNamespace(path="/api/v1/jobs/video"))


@pytest.fixture(autouse=True)
def _reset_singleton():
    set_billing_override(None)
    yield
    set_billing_override(None)


def _engine_with_account(
    mode: str = "enforce",
    *,
    balance_micros: Optional[int] = None,
    is_active: bool = True,
    with_store: bool = True,
    fail_reads: bool = False,
):
    table = FakeBillingTable()
    accounts = AccountStore(table, logger=logging.getLogger("t")) if with_store else None
    if accounts is not None and balance_micros is not None:
        asyncio.run(accounts.create(account_id="user-1", registered_name="A"))
        asyncio.run(accounts.adjust_balance("user-1", balance_micros))
        if not is_active:
            asyncio.run(accounts.update_profile("user-1", {"is_active": False}))
    table.fail_reads = fail_reads
    engine = BillingEngine(mode=mode, account_store=accounts, txn_store=None,
                           pricing_store=None, logger=logging.getLogger("t"))
    set_billing_override(engine)
    return engine


def _verdict(principal=PRINCIPAL):
    gate = create_billing_gate(logging.getLogger("t"))
    return asyncio.run(gate(_REQ, principal))


class TestGateVerdicts:
    def test_no_engine_passes(self):
        assert _verdict() is None

    def test_off_mode_passes(self):
        _engine_with_account("off", balance_micros=0)
        assert _verdict() is None

    def test_no_principal_passes(self):
        _engine_with_account("enforce", balance_micros=0)
        assert _verdict(principal=None) is None

    def test_no_account_store_passes(self):
        _engine_with_account("enforce", with_store=False)
        assert _verdict() is None

    def test_missing_account_402(self):
        _engine_with_account("enforce")  # store exists, no account row
        resp = _verdict()
        assert isinstance(resp, JSONResponse) and resp.status_code == 402
        assert b"insufficient_balance" in resp.body

    def test_zero_and_negative_balance_402(self):
        _engine_with_account("enforce", balance_micros=0)
        assert _verdict().status_code == 402
        set_billing_override(None)
        _engine_with_account("enforce", balance_micros=-5)
        assert _verdict().status_code == 402

    def test_inactive_account_403_even_with_balance(self):
        _engine_with_account("enforce", balance_micros=1_000_000, is_active=False)
        resp = _verdict()
        assert resp.status_code == 403
        assert b"account_inactive" in resp.body

    def test_healthy_account_passes(self):
        _engine_with_account("enforce", balance_micros=1_000_000)
        assert _verdict() is None

    def test_storage_error_fails_open(self, caplog):
        _engine_with_account("enforce", balance_micros=1_000_000, fail_reads=True)
        with caplog.at_level(logging.WARNING):
            assert _verdict() is None
        assert any("failing open" in r.message for r in caplog.records)

    def test_log_mode_broke_account_passes_but_logs(self, caplog):
        _engine_with_account("log", balance_micros=0)
        with caplog.at_level(logging.INFO):
            assert _verdict() is None
        assert any("would block" in r.message for r in caplog.records)


# -- middleware integration ------------------------------------------------

class SpyGate:
    def __init__(self, response: Optional[JSONResponse] = None,
                 raise_error: bool = False):
        self.calls: list[tuple[str, Optional[str]]] = []
        self.response = response
        self.raise_error = raise_error

    async def __call__(self, request, principal):
        self.calls.append((request.url.path,
                           principal.user_id if principal else None))
        if self.raise_error:
            raise RuntimeError("gate blew up")
        return self.response


def _app(mode: str, key_store, gate) -> FastAPI:
    app = FastAPI()
    app.middleware("http")(create_auth_middleware(
        mode=mode, key_store=key_store, logger=logging.getLogger("t"),
        billing_gate=gate))

    @app.post("/api/v1/jobs/video")
    @app.get("/api/v1/jobs/video")
    @app.post("/api/v1/jobs/video/compress")
    def handler(request: Request):
        principal = get_principal(request)
        return {"principal": principal.user_id if principal else None}

    return app


class TestMiddlewareGateConsult:
    def test_billable_post_blocked_by_gate_verdict(self):
        gate = SpyGate(JSONResponse(status_code=402, content={
            "detail": "Insufficient account balance.",
            "code": "insufficient_balance"}))
        client = TestClient(_app("enforce", StubKeyStore(PRINCIPAL), gate))
        resp = client.post("/api/v1/jobs/video",
                           headers={"Authorization": "Bearer sk-good"})
        assert resp.status_code == 402
        assert resp.json() == {"detail": "Insufficient account balance.",
                               "code": "insufficient_balance"}
        assert gate.calls == [("/api/v1/jobs/video", "user-1")]

    def test_get_on_billable_path_not_consulted(self):
        gate = SpyGate()
        client = TestClient(_app("enforce", StubKeyStore(PRINCIPAL), gate))
        resp = client.get("/api/v1/jobs/video",
                          headers={"Authorization": "Bearer sk-good"})
        assert resp.status_code == 200
        assert gate.calls == []

    def test_non_billable_post_not_consulted(self):
        gate = SpyGate()
        client = TestClient(_app("enforce", StubKeyStore(PRINCIPAL), gate))
        resp = client.post("/api/v1/jobs/video/compress",
                           headers={"Authorization": "Bearer sk-good"})
        assert resp.status_code == 200
        assert gate.calls == []

    def test_gate_exception_fails_open(self):
        gate = SpyGate(raise_error=True)
        client = TestClient(_app("enforce", StubKeyStore(PRINCIPAL), gate))
        resp = client.post("/api/v1/jobs/video",
                           headers={"Authorization": "Bearer sk-good"})
        assert resp.status_code == 200

    def test_off_mode_billable_post_still_consulted_with_identity(self):
        # Off-mode requests carry principal now (spec §4.7); a log-mode billing
        # engine can therefore observe would-block lines before auth enforce.
        gate = SpyGate()
        client = TestClient(_app("off", StubKeyStore(PRINCIPAL), gate))
        resp = client.post("/api/v1/jobs/video",
                           headers={"Authorization": "Bearer sk-good"})
        assert resp.status_code == 200
        assert gate.calls == [("/api/v1/jobs/video", "user-1")]


class TestBrokeAccountIsNotAnAuthError:
    """End-to-end through the real middleware + real gate: an out-of-money
    caller must never be told their key is bad. 401 means "who are you",
    402/403 mean "your wallet" — clients branch on these codes."""

    def _client(self, **account):
        _engine_with_account("enforce", **account)
        return TestClient(_app("enforce", StubKeyStore(PRINCIPAL),
                               create_billing_gate(logging.getLogger("t"))))

    def _post(self, client, key: str = "sk-good"):
        return client.post("/api/v1/jobs/video",
                           headers={"Authorization": f"Bearer {key}"})

    def test_zero_balance_is_402_not_401(self):
        resp = self._post(self._client(balance_micros=0))
        assert resp.status_code == 402
        assert resp.json()["code"] == "insufficient_balance"

    def test_negative_balance_is_402_not_401(self):
        # An account can go negative: the gate blocks at submit, but a job
        # already in flight still settles (production has such accounts).
        resp = self._post(self._client(balance_micros=-4_000_000))
        assert resp.status_code == 402
        assert resp.json()["code"] == "insufficient_balance"

    def test_valid_key_without_billing_account_is_402_not_401(self):
        resp = self._post(self._client())  # store present, no account row
        assert resp.status_code == 402
        assert resp.json()["code"] == "insufficient_balance"

    def test_inactive_account_is_403_not_401(self):
        resp = self._post(self._client(balance_micros=1_000_000,
                                       is_active=False))
        assert resp.status_code == 403
        assert resp.json()["code"] == "account_inactive"

    def test_bad_key_is_401_even_when_the_account_is_funded(self):
        # The contrast that gives the other assertions meaning.
        _engine_with_account("enforce", balance_micros=1_000_000)
        client = TestClient(_app("enforce", StubKeyStore(None),
                                 create_billing_gate(logging.getLogger("t"))))
        resp = self._post(client, key="sk-bad")
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_api_key"

    def test_funded_account_passes(self):
        resp = self._post(self._client(balance_micros=1_000_000))
        assert resp.status_code == 200


class TestOffModePrincipalAttach:
    def test_off_mode_with_header_attaches_principal(self):
        store = StubKeyStore(PRINCIPAL)
        client = TestClient(_app("off", store, None))
        resp = client.post("/api/v1/jobs/video",
                           headers={"Authorization": "Bearer sk-good"})
        assert resp.status_code == 200
        assert resp.json() == {"principal": "user-1"}
        assert store.lookups == ["sk-good"]

    def test_off_mode_without_header_no_lookup(self):
        store = StubKeyStore(PRINCIPAL)
        client = TestClient(_app("off", store, None))
        resp = client.post("/api/v1/jobs/video")
        assert resp.status_code == 200
        assert resp.json() == {"principal": None}
        assert store.lookups == []

    def test_off_mode_invalid_key_never_rejects(self):
        client = TestClient(_app("off", StubKeyStore(None), None))
        resp = client.post("/api/v1/jobs/video",
                           headers={"Authorization": "Bearer sk-bad"})
        assert resp.status_code == 200
        assert resp.json() == {"principal": None}

    def test_off_mode_store_down_never_rejects(self):
        client = TestClient(_app("off", StubKeyStore(unavailable=True), None))
        resp = client.post("/api/v1/jobs/video",
                           headers={"Authorization": "Bearer sk-x"})
        assert resp.status_code == 200


# -- low-balance warning ---------------------------------------------------

def _request_with_state(path: str = "/api/v1/jobs/video"):
    return SimpleNamespace(method="POST", url=SimpleNamespace(path=path),
                           state=SimpleNamespace())


def _account_with_history(engine, *, recharged: int, spent: int):
    accounts = engine.account_store
    asyncio.run(accounts.create(account_id="user-1", registered_name="A"))
    asyncio.run(accounts.adjust_balance("user-1", recharged,
                                        recharge_micros=recharged))
    if spent:
        asyncio.run(accounts.adjust_balance("user-1", -spent))
    return accounts


class TestLowBalanceWarning:
    def test_gate_sets_warning_state_below_threshold(self):
        engine = _engine_with_account("enforce")
        _account_with_history(engine, recharged=10_000_000, spent=9_500_000)
        gate = create_billing_gate(logging.getLogger("t"))
        req = _request_with_state()
        assert asyncio.run(gate(req, PRINCIPAL)) is None
        warning = req.state.balance_warning
        assert warning["balance_usd"] == 0.5
        assert warning["threshold_usd"] == 1.0
        assert "recharge" in warning["message"].lower()

    def test_no_warning_at_exact_threshold(self):
        engine = _engine_with_account("enforce")
        _account_with_history(engine, recharged=10_000_000, spent=9_000_000)
        gate = create_billing_gate(logging.getLogger("t"))
        req = _request_with_state()
        assert asyncio.run(gate(req, PRINCIPAL)) is None
        assert getattr(req.state, "balance_warning", None) is None

    def test_no_warning_when_never_recharged(self):
        # balance granted without recharge_micros -> total stays 0 -> no warning
        _engine_with_account("enforce", balance_micros=500_000)
        gate = create_billing_gate(logging.getLogger("t"))
        req = _request_with_state()
        assert asyncio.run(gate(req, PRINCIPAL)) is None
        assert getattr(req.state, "balance_warning", None) is None

    def test_middleware_adds_header_and_injects_json_block(self):
        engine = _engine_with_account("enforce")
        _account_with_history(engine, recharged=10_000_000, spent=9_500_000)
        gate = create_billing_gate(logging.getLogger("t"))
        client = TestClient(_app("enforce", StubKeyStore(PRINCIPAL), gate))
        resp = client.post("/api/v1/jobs/video",
                           headers={"Authorization": "Bearer sk-good"})
        assert resp.status_code == 200
        assert resp.headers.get("X-Edenn-Balance-Warning") == "low"
        body = resp.json()
        assert body["balance_warning"]["threshold_usd"] == 1.0
        assert body["principal"] == "user-1"   # original payload preserved

    def test_healthy_account_response_untouched(self):
        engine = _engine_with_account("enforce")
        _account_with_history(engine, recharged=10_000_000, spent=0)
        gate = create_billing_gate(logging.getLogger("t"))
        client = TestClient(_app("enforce", StubKeyStore(PRINCIPAL), gate))
        resp = client.post("/api/v1/jobs/video",
                           headers={"Authorization": "Bearer sk-good"})
        assert resp.status_code == 200
        assert "X-Edenn-Balance-Warning" not in resp.headers
        assert "balance_warning" not in resp.json()


# -- low-balance warning: non-injectable bodies ----------------------------

def _app_returning(mode: str, key_store, gate, response_factory) -> FastAPI:
    """``_app`` wiring, but the billable POST route returns a caller-supplied
    Response so the non-injectable middleware branches can be exercised."""
    app = FastAPI()
    app.middleware("http")(create_auth_middleware(
        mode=mode, key_store=key_store, logger=logging.getLogger("t"),
        billing_gate=gate))

    @app.post("/api/v1/jobs/video")
    def handler():
        return response_factory()

    return app


def _low_balance_client(response_factory) -> TestClient:
    """TestClient whose account sits below the warning threshold ($0.50 left)."""
    engine = _engine_with_account("enforce")
    _account_with_history(engine, recharged=10_000_000, spent=9_500_000)
    gate = create_billing_gate(logging.getLogger("t"))
    return TestClient(_app_returning("enforce", StubKeyStore(PRINCIPAL), gate,
                                     response_factory))


def _post(client: TestClient):
    return client.post("/api/v1/jobs/video",
                       headers={"Authorization": "Bearer sk-good"})


class TestLowBalanceWarningNonInjectableBodies:
    def test_plain_text_body_untouched_header_still_added(self):
        resp = _post(_low_balance_client(lambda: PlainTextResponse("ok")))
        assert resp.status_code == 200
        assert resp.headers["X-Edenn-Balance-Warning"] == "low"
        assert resp.content == b"ok"                      # byte-identical
        assert resp.headers["content-length"] == "2"
        assert "balance_warning" not in resp.text

    def test_error_status_json_not_injected(self):
        resp = _post(_low_balance_client(
            lambda: JSONResponse(status_code=422, content={"detail": "bad"})))
        assert resp.status_code == 422
        assert resp.headers["X-Edenn-Balance-Warning"] == "low"
        assert resp.json() == {"detail": "bad"}           # no extra key
        assert resp.content == b'{"detail":"bad"}'

    def test_non_object_json_body_rebuilt_intact(self):
        # json.loads succeeds but the payload is a list -> injection aborts and
        # the reply is rebuilt from the buffered bytes (never truncated).
        resp = _post(_low_balance_client(lambda: JSONResponse(content=[1, 2, 3])))
        assert resp.status_code == 200
        assert resp.headers["X-Edenn-Balance-Warning"] == "low"
        assert resp.content == b"[1,2,3]"
        assert resp.headers["content-length"] == "7"
        assert resp.json() == [1, 2, 3]


class _ExplodingResponse:
    """Minimal streaming-response stand-in whose iterator dies mid-stream."""

    def __init__(self, first_chunk: bytes):
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self.first_chunk = first_chunk

        async def _iterator():
            yield first_chunk
            raise RuntimeError("stream died mid-body")

        self.body_iterator = _iterator()


class TestBalanceWarningIteratorFailure:
    def test_midstream_iterator_failure_rebuilds_from_buffer(self):
        original = _ExplodingResponse(b'{"principal":')
        result = asyncio.run(_apply_balance_warning(
            original, {"balance_usd": 0.5}, logging.getLogger("t")))
        # Must NOT hand back the original whose iterator is now half-drained.
        assert result is not original
        assert getattr(result, "body", None) == b'{"principal":'
        assert result.headers["X-Edenn-Balance-Warning"] == "low"
        assert result.status_code == 200
