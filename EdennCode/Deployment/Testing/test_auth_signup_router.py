"""Self-serve signup: safety invariants, validation, unavailability paths."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.auth.key_store import ApiKeyStore
from EdennCode.Deployment.auth.signup_router import (
    SIGNUP_NOTE,
    create_signup_router,
)
from EdennCode.Deployment.billing.engine import BillingEngine
from EdennCode.Deployment.billing.stores import AccountStore, BillingStoreUnavailable
from EdennCode.Deployment.Testing.test_auth_key_store import FakeTableClient
from EdennCode.Deployment.Testing.test_billing_stores import FakeBillingTable

BODY = {
    "registered_name": "北京某某科技有限公司",
    "phone": "13800138000",
    "email": "ops@example.com",
}
# The unverified endpoint is an internal tool now (see test_auth_signup_verified
# for the guard itself); every test here speaks as the admin.
ADMIN_SECRET = "admin-secret-for-tests"
ADMIN_HEADERS = {"x-admin-secret": ADMIN_SECRET}


def _build(*, mode: str = "enforce", with_key_store: bool = True,
           with_account_store: bool = True, with_index: bool = True):
    """(client, key_store, accounts, key_table, index) over table doubles."""
    from EdennCode.Deployment.billing.account_index import AccountIndexStore

    logger = logging.getLogger("t")
    key_table = FakeTableClient()
    key_store = ApiKeyStore(key_table, logger=logger) if with_key_store else None
    accounts = (AccountStore(FakeBillingTable(), logger=logger)
                if with_account_store else None)
    index = (AccountIndexStore(FakeBillingTable(), logger=logger)
             if with_index else None)
    engine = BillingEngine(mode=mode, account_store=accounts, txn_store=None,
                           pricing_store=None, logger=logger, index_store=index)
    app = FastAPI()
    app.include_router(create_signup_router(
        key_store=key_store, billing_engine=engine, logger=logger,
        admin_secret=ADMIN_SECRET))
    return (TestClient(app, headers=ADMIN_HEADERS), key_store, accounts,
            key_table, index)


class TestHappyPath:
    def test_creates_zero_balance_account_with_linked_key(self):
        import asyncio

        client, key_store, accounts, _, _ = _build()
        resp = client.post("/api/v1/signup", json=BODY)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["api_key"].startswith("sk-")
        assert body["key_prefix"] == body["api_key"][:12]
        assert body["balance_usd"] == 0.0
        assert body["is_new_account"] is True
        assert body["message"]
        # The internal account id must never reach the caller.
        assert "account_id" not in body
        assert "user_id" not in body

        records = asyncio.run(accounts.list_accounts())
        assert len(records) == 1
        record = records[0]
        assert record.account_id.startswith("acct_")
        assert record.balance_micros == 0
        assert record.total_recharged_micros == 0
        assert record.registered_name == BODY["registered_name"]
        assert record.phone == BODY["phone"]
        assert record.email == BODY["email"]
        assert record.entity_type == "individual"
        assert record.note == SIGNUP_NOTE
        assert record.is_active is True

    def test_returned_key_resolves_to_the_new_account(self):
        # The whole point of user_id == account_id: billing finds the wallet
        # by the key's user_id.
        import asyncio

        client, key_store, accounts, _, _ = _build()
        body = client.post("/api/v1/signup", json=BODY).json()
        account_id = asyncio.run(accounts.list_accounts())[0].account_id
        principal = asyncio.run(key_store.lookup(body["api_key"]))
        assert principal is not None
        assert principal.user_id == account_id
        assert principal.key_prefix == body["key_prefix"]


class TestSafetyInvariants:
    def test_client_supplied_account_id_is_ignored(self):
        # Honoring it would let anyone mint a key into another tenant's wallet.
        import asyncio

        client, _, accounts, _, _ = _build()
        hijack = dict(BODY, account_id="910000000000000000")
        resp = client.post("/api/v1/signup", json=hijack)
        assert resp.status_code == 200
        stored = asyncio.run(accounts.list_accounts())[0].account_id
        assert stored != "910000000000000000"
        assert stored.startswith("acct_")

    def test_client_supplied_balance_is_ignored(self):
        import asyncio

        client, _, accounts, _, _ = _build()
        greedy = dict(BODY, balance_micros="999000000", balance_usd=999)
        assert client.post("/api/v1/signup", json=greedy).status_code == 200
        record = asyncio.run(accounts.list_accounts())[0]
        assert record.balance_micros == 0


class TestValidation:
    def test_invalid_email_400(self):
        client, *_ = _build()
        for bad in ("not-an-email", "no@dot", "two@@at.com", "spa ce@x.com"):
            resp = client.post("/api/v1/signup", json=dict(BODY, email=bad))
            assert resp.status_code == 400, bad
            assert resp.json()["code"] == "invalid_email"

    def test_missing_name_422(self):
        client, *_ = _build()
        for payload in ({"phone": "13800138000"}, dict(BODY, registered_name="")):
            resp = client.post("/api/v1/signup", json=payload)
            assert resp.status_code == 422, payload

    def test_no_contact_at_all_400(self):
        client, *_ = _build()
        for payload in ({"registered_name": "X"},
                        {"registered_name": "X", "phone": "", "email": ""},
                        {"registered_name": "X", "phone": "---"}):
            resp = client.post("/api/v1/signup", json=payload)
            assert resp.status_code == 400, payload
            assert resp.json()["code"] == "missing_contact"

    def test_phone_only_and_email_only_both_work(self):
        for payload in ({"registered_name": "X", "phone": "13800138000"},
                        {"registered_name": "X", "email": "a@b.co"}):
            client, *_ = _build()
            resp = client.post("/api/v1/signup", json=payload)
            assert resp.status_code == 200, resp.text
            assert resp.json()["is_new_account"] is True


class TestUnavailable:
    def test_non_enforce_billing_modes_503(self):
        # Availability is derived from billing enforcement, not a flag: with
        # billing off/log nothing stops a self-minted key from spending.
        for mode in ("off", "log"):
            client, *_ = _build(mode=mode)
            resp = client.post("/api/v1/signup", json=BODY)
            assert resp.status_code == 503, mode
            assert resp.json()["code"] == "signup_unavailable"

    def test_missing_stores_503(self):
        for kwargs in ({"with_key_store": False}, {"with_account_store": False},
                       {"with_index": False}):
            client, *_ = _build(**kwargs)
            resp = client.post("/api/v1/signup", json=BODY)
            assert resp.status_code == 503, kwargs
            assert resp.json()["code"] == "signup_unavailable"

    def test_account_storage_failure_503(self):
        client, _, accounts, _, _ = _build()

        async def _boom(**kwargs):
            raise BillingStoreUnavailable("table down")

        accounts.create = _boom
        resp = client.post("/api/v1/signup", json=BODY)
        assert resp.status_code == 503
        assert resp.json()["code"] == "signup_unavailable"

    def test_mint_failure_leaves_orphan_account_and_503(self):
        # Account first, key second: a mint failure leaves a zero-balance
        # account with no key (harmless, identifiable by its note) rather than
        # a key with no wallet (permanent 402 for the caller).
        import asyncio

        client, _, accounts, key_table, _ = _build()
        key_table.fail_all = True
        resp = client.post("/api/v1/signup", json=BODY)
        assert resp.status_code == 503
        assert resp.json()["code"] == "signup_unavailable"
        orphans = asyncio.run(accounts.list_accounts())
        assert len(orphans) == 1
        assert orphans[0].note == SIGNUP_NOTE
        assert orphans[0].balance_micros == 0


# -- wiring ---------------------------------------------------------------

def test_signup_path_is_auth_exempt():
    from EdennCode.Deployment.auth.middleware import is_exempt_path

    assert is_exempt_path("/api/v1/signup") is True
    # Neighbouring paths must NOT be swept in by an over-broad rule.
    assert is_exempt_path("/api/v1/signups") is False
    assert is_exempt_path("/api/v1/signup/keys") is False


class TestEndToEndThroughMiddleware:
    """Signup must be reachable with no key, and the key it hands out must be
    stopped by the wallet (402), never mistaken for a bad key (401)."""

    def _app(self):
        from fastapi import Request

        from EdennCode.Deployment.auth.middleware import create_auth_middleware
        from EdennCode.Deployment.billing import set_billing_override
        from EdennCode.Deployment.billing.account_index import AccountIndexStore
        from EdennCode.Deployment.billing.gate import create_billing_gate

        logger = logging.getLogger("t")
        key_store = ApiKeyStore(FakeTableClient(), logger=logger)
        accounts = AccountStore(FakeBillingTable(), logger=logger)
        engine = BillingEngine(mode="enforce", account_store=accounts,
                               txn_store=None, pricing_store=None, logger=logger,
                               index_store=AccountIndexStore(FakeBillingTable(),
                                                             logger=logger))
        # The gate reads the process singleton, so the override is required.
        set_billing_override(engine)

        app = FastAPI()
        app.middleware("http")(create_auth_middleware(
            mode="enforce", key_store=key_store, logger=logger,
            billing_gate=create_billing_gate(logger)))
        app.include_router(create_signup_router(
            key_store=key_store, billing_engine=engine, logger=logger,
            admin_secret=ADMIN_SECRET))

        @app.post("/api/v1/jobs/video")
        def submit(request: Request):
            return {"submitted": True}

        return app

    def test_signup_reachable_without_a_key_under_enforce(self):
        from EdennCode.Deployment.billing import set_billing_override

        try:
            client = TestClient(self._app(), headers=ADMIN_HEADERS)
            resp = client.post("/api/v1/signup", json=BODY)
            assert resp.status_code == 200, resp.text
        finally:
            set_billing_override(None)

    def test_new_key_is_blocked_by_the_wallet_with_402_not_401(self):
        from EdennCode.Deployment.billing import set_billing_override

        try:
            client = TestClient(self._app(), headers=ADMIN_HEADERS)
            key = client.post("/api/v1/signup", json=BODY).json()["api_key"]
            resp = client.post("/api/v1/jobs/video",
                               headers={"Authorization": f"Bearer {key}"})
            assert resp.status_code == 402
            assert resp.json()["code"] == "insufficient_balance"

            # Contrast: a bad key is still an auth failure, not a wallet one.
            bad = client.post("/api/v1/jobs/video",
                              headers={"Authorization": "Bearer sk-nope"})
            assert bad.status_code == 401
            assert bad.json()["code"] == "invalid_api_key"
        finally:
            set_billing_override(None)


class TestAccountIdCollision:
    def test_retries_once_on_collision(self, monkeypatch):
        import asyncio

        from EdennCode.Deployment.auth import signup_router

        # The incumbent is seeded straight into the account store, so it holds
        # no index entry: this signup takes the fresh-account path and only the
        # id collides.
        client, _, accounts, _, _ = _build()
        asyncio.run(accounts.create(account_id="acct_taken",
                                    registered_name="incumbent"))
        generated = iter(["taken", "free"])
        monkeypatch.setattr(signup_router.secrets, "token_hex",
                            lambda n: next(generated))
        resp = client.post("/api/v1/signup", json=BODY)
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_new_account"] is True
        stored = {r.account_id for r in asyncio.run(accounts.list_accounts())}
        assert stored == {"acct_taken", "acct_free"}

    def test_second_collision_gives_503_not_500(self, monkeypatch):
        import asyncio

        from EdennCode.Deployment.auth import signup_router

        client, _, accounts, _, _ = _build()
        asyncio.run(accounts.create(account_id="acct_taken",
                                    registered_name="incumbent"))
        monkeypatch.setattr(signup_router.secrets, "token_hex",
                            lambda n: "taken")
        resp = client.post("/api/v1/signup", json=BODY)
        assert resp.status_code == 503
        assert resp.json()["code"] == "signup_unavailable"


class TestContactMerge:
    """Repeat signups with a known contact land on the same account (spec D6)."""

    def _accounts_and_keys(self, accounts, key_store):
        import asyncio

        return (asyncio.run(accounts.list_accounts()),
                asyncio.run(key_store.list_keys()))

    def test_same_email_merges_and_issues_another_key(self):
        client, key_store, accounts, _, _ = _build()
        first = client.post("/api/v1/signup", json=BODY).json()
        second = client.post("/api/v1/signup", json=BODY)
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["is_new_account"] is False
        assert body["api_key"] != first["api_key"]

        records, keys = self._accounts_and_keys(accounts, key_store)
        assert len(records) == 1          # no duplicate account
        assert len(keys) == 2            # a second key under it
        assert {k.user_id for k in keys} == {records[0].account_id}

    def test_same_phone_merges(self):
        client, key_store, accounts, _, _ = _build()
        payload = {"registered_name": "X", "phone": "13800138000"}
        client.post("/api/v1/signup", json=payload)
        second = client.post("/api/v1/signup", json=payload)
        assert second.json()["is_new_account"] is False
        records, keys = self._accounts_and_keys(accounts, key_store)
        assert len(records) == 1 and len(keys) == 2

    def test_merge_is_insensitive_to_contact_formatting(self):
        client, key_store, accounts, _, _ = _build()
        client.post("/api/v1/signup", json={
            "registered_name": "X", "phone": "13800138000",
            "email": "ops@example.com"})
        second = client.post("/api/v1/signup", json={
            "registered_name": "X", "phone": "+86 138-0013-8000",
            "email": "  OPS@Example.COM "})
        assert second.json()["is_new_account"] is False
        records, _ = self._accounts_and_keys(accounts, key_store)
        assert len(records) == 1

    def test_merge_reports_the_real_balance_and_drops_the_recharge_prompt(self):
        import asyncio

        client, _, accounts, _, _ = _build()
        client.post("/api/v1/signup", json=BODY)
        account_id = asyncio.run(accounts.list_accounts())[0].account_id
        asyncio.run(accounts.adjust_balance(account_id, 25_000_000,
                                            recharge_micros=25_000_000))
        body = client.post("/api/v1/signup", json=BODY).json()
        assert body["balance_usd"] == 25.0
        assert "contact" not in body["message"].lower()

    def test_merge_backfills_the_new_contact(self):
        # First signup by phone only; second adds an email -> that email must
        # now resolve to the same account, or the next email-only signup would
        # open a duplicate.
        from EdennCode.Deployment.billing.account_index import (
            INDEX_KIND_EMAIL, normalize_email,
        )
        import asyncio

        client, _, accounts, _, index = _build()
        client.post("/api/v1/signup", json={
            "registered_name": "X", "phone": "13800138000"})
        account_id = asyncio.run(accounts.list_accounts())[0].account_id
        client.post("/api/v1/signup", json={
            "registered_name": "X", "phone": "13800138000",
            "email": "late@example.com"})
        assert asyncio.run(index.lookup(
            INDEX_KIND_EMAIL, normalize_email("late@example.com"))) == account_id

        third = client.post("/api/v1/signup", json={
            "registered_name": "X", "email": "late@example.com"})
        assert third.json()["is_new_account"] is False
        assert len(asyncio.run(accounts.list_accounts())) == 1

    def test_merge_never_overwrites_the_stored_profile(self):
        import asyncio

        client, _, accounts, _, _ = _build()
        client.post("/api/v1/signup", json=BODY)
        client.post("/api/v1/signup", json=dict(BODY, registered_name="IMPOSTOR"))
        record = asyncio.run(accounts.list_accounts())[0]
        assert record.registered_name == BODY["registered_name"]

    def test_phone_and_email_owned_by_different_accounts_conflicts(self):
        client, key_store, accounts, _, _ = _build()
        client.post("/api/v1/signup", json={
            "registered_name": "A", "phone": "13800138000"})
        client.post("/api/v1/signup", json={
            "registered_name": "B", "email": "b@example.com"})
        resp = client.post("/api/v1/signup", json={
            "registered_name": "C", "phone": "13800138000",
            "email": "b@example.com"})
        assert resp.status_code == 409
        assert resp.json()["code"] == "identity_conflict"
        assert "account_id" not in resp.json()
        # A conflict writes nothing.
        records, keys = self._accounts_and_keys(accounts, key_store)
        assert len(records) == 2 and len(keys) == 2

    def test_claim_race_falls_back_to_the_winners_account(self):
        # Simulate a concurrent signup winning the claim between our lookup and
        # our own claim: the loser must issue a key under the winner's account,
        # not fail and not open a second account.
        import asyncio

        from EdennCode.Deployment.billing.account_index import (
            INDEX_KIND_EMAIL, normalize_email,
        )

        client, key_store, accounts, _, index = _build()
        asyncio.run(accounts.create(account_id="acct_winner",
                                    registered_name="winner"))
        real_claim = index.claim
        state = {"armed": True}

        async def _claim_after_someone_else_won(kind, value, account_id):
            if state["armed"]:
                state["armed"] = False
                await real_claim(kind, value, "acct_winner")   # the other request
            return await real_claim(kind, value, account_id)

        index.claim = _claim_after_someone_else_won
        resp = client.post("/api/v1/signup",
                           json={"registered_name": "loser",
                                 "email": "race@example.com"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_new_account"] is False
        assert asyncio.run(index.lookup(
            INDEX_KIND_EMAIL,
            normalize_email("race@example.com"))) == "acct_winner"
        keys = asyncio.run(key_store.list_keys())
        assert [k.user_id for k in keys] == ["acct_winner"]

    def test_merge_race_on_a_second_contact_conflicts(self):
        # The only conflict reachable *after* a merge decision: the account is
        # found by phone, and the still-unindexed email is claimed by a
        # different account between our lookup and our claim. The two contacts
        # now genuinely disagree, so nothing may be issued.
        import asyncio

        client, key_store, accounts, _, index = _build()
        client.post("/api/v1/signup", json={
            "registered_name": "A", "phone": "13800138000"})
        asyncio.run(accounts.create(account_id="acct_other",
                                    registered_name="other"))
        real_claim = index.claim

        async def _claim_after_someone_else_won(kind, value, account_id):
            await real_claim(kind, value, "acct_other")   # the other request
            return await real_claim(kind, value, account_id)

        index.claim = _claim_after_someone_else_won
        resp = client.post("/api/v1/signup", json={
            "registered_name": "A", "phone": "13800138000",
            "email": "new@example.com"})
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "identity_conflict"
        assert "account_id" not in resp.json()
        # No extra key, no extra account: only the first signup's remain.
        records, keys = self._accounts_and_keys(accounts, key_store)
        assert len(keys) == 1
        assert len(records) == 2          # signup A + the seeded acct_other

    def test_funded_but_inactive_account_is_not_promised_as_ready(self):
        # Credit alone does not make a key usable — the gate answers 403
        # account_inactive — so the message must not say "ready to use".
        import asyncio

        client, _, accounts, _, _ = _build()
        client.post("/api/v1/signup", json=BODY)
        account_id = asyncio.run(accounts.list_accounts())[0].account_id
        asyncio.run(accounts.adjust_balance(account_id, 25_000_000,
                                            recharge_micros=25_000_000))
        asyncio.run(accounts.update_profile(account_id, {"is_active": False}))

        body = client.post("/api/v1/signup", json=BODY).json()
        assert body["balance_usd"] == 25.0        # still the honest balance
        assert "ready to use" not in body["message"].lower()
        assert "not active" in body["message"].lower()


class TestPostMintFailures:
    """Past the mint the key exists and is shown once: never 500 after it."""

    def test_balance_read_failure_still_returns_the_key(self):
        import asyncio

        client, key_store, accounts, _, _ = _build()

        async def _boom(account_id):
            raise RuntimeError("decode blew up")   # NOT BillingStoreUnavailable

        accounts.get = _boom
        resp = client.post("/api/v1/signup", json=BODY)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["api_key"].startswith("sk-")
        assert body["balance_usd"] == 0.0
        # The key really was minted, so losing it to a 500 would be permanent.
        assert asyncio.run(key_store.lookup(body["api_key"])) is not None
