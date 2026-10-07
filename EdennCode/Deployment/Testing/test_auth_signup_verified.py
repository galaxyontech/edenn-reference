"""Verified signup: a Firebase-proven phone number opens an account.

Two properties, both easy to regress:

* The identity anchor is the Firebase **uid**, not the phone number — customers
  change numbers, and an account that follows the number instead of the person
  would hand the account to whoever gets the recycled SIM.
* This door hands back **no API key**. Keys are minted from the console's API
  key page, so the plaintext appears only where it can be shown properly, and a
  returning customer's repeat signup cannot litter their account with keys
  nobody saved.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.auth.firebase_verifier import (
    InvalidIdentityToken,
    VerifiedIdentity,
)
from EdennCode.Deployment.auth.key_store import ApiKeyStore
from EdennCode.Deployment.auth.signup_router import (
    DEFAULT_KEY_NAME,
    SIGNUP_NOTE,
    VERIFIED_SIGNUP_NOTE,
    create_signup_router,
)
from EdennCode.Deployment.billing.account_index import (
    INDEX_KIND_EMAIL,
    INDEX_KIND_FIREBASE,
    INDEX_KIND_PHONE,
    AccountIndexStore,
    normalize_phone,
)
from EdennCode.Deployment.billing.engine import BillingEngine
from EdennCode.Deployment.billing.stores import AccountStore
from EdennCode.Deployment.Testing.test_auth_key_store import FakeTableClient
from EdennCode.Deployment.Testing.test_billing_stores import FakeBillingTable

ADMIN_SECRET = "admin-secret-for-tests"
UID = "kQ2mZ8xVbNfR4tYuIoPaSdFgHjK1"
OTHER_UID = "zZz9YyY8XxX7WwW6VvV5UuU4TtT3"
# Reserved synthetic test numbers, never real ones. The mainland number is the
# E.164 form of the "13000000000" a v0 admin would have typed nationally —
# the adoption test below only means anything while those two agree.
PHONE_E164 = "+8613000000000"
JP_PHONE_E164 = "+819012345678"
BODY = {"registered_name": "北京某某科技有限公司"}


class FakeVerifier:
    """Maps opaque test tokens to identities; anything else is rejected."""

    def __init__(self, identities: dict[str, VerifiedIdentity]) -> None:
        self.identities = identities
        self.calls: list[str] = []

    async def verify(self, token: str) -> VerifiedIdentity:
        self.calls.append(token)
        try:
            return self.identities[token]
        except KeyError:
            raise InvalidIdentityToken("unknown test token") from None


def _identity(uid: str = UID, phone: str = PHONE_E164) -> VerifiedIdentity:
    return VerifiedIdentity(uid=uid, phone_number=phone, provider="phone")


def _build(*, mode: str = "enforce", verifier: object = "default",
           with_key_store: bool = True):
    """(client, key_store, accounts, index, verifier) over table doubles."""
    logger = logging.getLogger("t")
    key_store = ApiKeyStore(FakeTableClient(), logger=logger) if with_key_store else None
    accounts = AccountStore(FakeBillingTable(), logger=logger)
    index = AccountIndexStore(FakeBillingTable(), logger=logger)
    engine = BillingEngine(mode=mode, account_store=accounts, txn_store=None,
                           pricing_store=None, logger=logger, index_store=index)
    if verifier == "default":
        verifier = FakeVerifier({"good": _identity()})
    app = FastAPI()
    app.include_router(create_signup_router(
        key_store=key_store, billing_engine=engine, logger=logger,
        firebase_verifier=verifier, admin_secret=ADMIN_SECRET))
    return TestClient(app), key_store, accounts, index, verifier


def _post(client: TestClient, token: str = "good", **body):
    return client.post("/api/v1/signup/verified",
                       json=dict(BODY, **body),
                       headers={"Authorization": f"Bearer {token}"})


class TestNewAccount:
    def test_creates_the_account_and_issues_no_key(self):
        client, key_store, accounts, index, _ = _build()
        resp = _post(client)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["balance_usd"] == 0.0
        assert body["is_new_account"] is True
        # No secret in this response, and not an empty one either: a client
        # reading a blank string where a key used to be cannot tell whether
        # signup half-failed.
        assert "api_key" not in body
        assert "key_prefix" not in body
        # The internal account id must never reach the caller (交接 §5.5).
        assert "account_id" not in body
        assert asyncio.run(key_store.list_keys()) == []

        records = asyncio.run(accounts.list_accounts())
        assert len(records) == 1
        record = records[0]
        assert record.balance_micros == 0
        assert record.registered_name == BODY["registered_name"]
        assert record.phone == PHONE_E164
        assert record.note == VERIFIED_SIGNUP_NOTE

    def test_says_where_to_get_a_key(self):
        # The account is useless without one, so the response has to point at
        # the page that mints it — otherwise the flow just dead-ends.
        assert "API key" in _post(_build()[0]).json()["message"]

    def test_indexes_both_the_uid_and_the_phone(self):
        # The uid is the anchor; the phone is indexed too so the admin's
        # /account-lookup keeps working for verified accounts (设计 D3).
        client, _, accounts, index, _ = _build()
        _post(client)
        account_id = asyncio.run(accounts.list_accounts())[0].account_id
        assert asyncio.run(index.lookup(INDEX_KIND_FIREBASE, UID)) == account_id
        assert asyncio.run(
            index.lookup(INDEX_KIND_PHONE, normalize_phone(PHONE_E164))
        ) == account_id

    def test_registered_name_is_required(self):
        client, *_ = _build()
        resp = client.post("/api/v1/signup/verified", json={},
                           headers={"Authorization": "Bearer good"})
        assert resp.status_code == 422


class TestRepeatSignup:
    def test_same_uid_merges_and_still_mints_nothing(self):
        # This is why the door stopped issuing keys: it used to add one on
        # every call, so a customer who reopened the signup screen collected
        # keys they never saw and could not tell apart.
        client, key_store, accounts, _, _ = _build()
        _post(client)
        second = _post(client).json()

        assert second["is_new_account"] is False
        assert len(asyncio.run(accounts.list_accounts())) == 1
        assert asyncio.run(key_store.list_keys()) == []

    def test_a_new_uid_on_a_new_phone_opens_its_own_account(self):
        verifier = FakeVerifier({
            "good": _identity(),
            "other": _identity(OTHER_UID, JP_PHONE_E164),
        })
        client, _, accounts, _, _ = _build(verifier=verifier)
        _post(client)
        _post(client, token="other")
        assert len(asyncio.run(accounts.list_accounts())) == 2

    def test_a_changed_phone_still_resolves_to_the_same_account(self):
        # The whole point of anchoring on uid: the customer re-binds their
        # number in Firebase and keeps their wallet.
        verifier = FakeVerifier({
            "old": _identity(UID, PHONE_E164),
            "new": _identity(UID, JP_PHONE_E164),
        })
        client, _, accounts, index, _ = _build(verifier=verifier)
        _post(client, token="old")
        second = _post(client, token="new").json()
        assert second["is_new_account"] is False
        assert len(asyncio.run(accounts.list_accounts())) == 1


class TestUpgradeFromUnverifiedEra:
    def test_a_phone_registered_before_verification_is_adopted(self):
        """The v0 account must not be orphaned when its owner finally verifies."""
        client, key_store, accounts, index, _ = _build()
        legacy = client.post(
            "/api/v1/signup",
            # The national form of PHONE_E164 — the same reserved number the
            # token proves, which is exactly what makes it adoptable.
            json={"registered_name": "老客户", "phone": "13000000000"},
            headers={"x-admin-secret": ADMIN_SECRET},
        )
        assert legacy.status_code == 200, legacy.text
        legacy_account = asyncio.run(accounts.list_accounts())[0].account_id

        verified = _post(client)
        assert verified.status_code == 200, verified.text
        assert verified.json()["is_new_account"] is False
        # No second account, and the uid index now points at the old one.
        assert len(asyncio.run(accounts.list_accounts())) == 1
        assert asyncio.run(
            index.lookup(INDEX_KIND_FIREBASE, UID)) == legacy_account

    def test_the_legacy_profile_is_not_overwritten(self):
        client, _, accounts, _, _ = _build()
        # Same reserved number as PHONE_E164: without the merge this asserts
        # nothing, because the legacy row would simply sit there untouched.
        client.post("/api/v1/signup",
                    json={"registered_name": "老客户", "phone": "13000000000",
                          "email": "ops@example.com"},
                    headers={"x-admin-secret": ADMIN_SECRET})
        _post(client)
        record = asyncio.run(accounts.list_accounts())[0]
        assert record.registered_name == "老客户"
        assert record.email == "ops@example.com"


class TestIdentityConflict:
    def test_uid_and_phone_on_different_accounts_is_refused(self):
        """Merging two wallets is a money operation; a signup must not trigger it."""
        client, _, accounts, index, _ = _build(
            verifier=FakeVerifier({"good": _identity(UID, JP_PHONE_E164)}))
        asyncio.run(accounts.create(account_id="acct_uid", registered_name="A",
                                    entity_type="individual"))
        asyncio.run(accounts.create(account_id="acct_phone", registered_name="B",
                                    entity_type="individual"))
        asyncio.run(index.claim(INDEX_KIND_FIREBASE, UID, "acct_uid"))
        asyncio.run(index.claim(INDEX_KIND_PHONE,
                                normalize_phone(JP_PHONE_E164), "acct_phone"))
        resp = _post(client)
        assert resp.status_code == 409
        assert resp.json()["code"] == "identity_conflict"

    def test_conflict_writes_nothing(self):
        client, key_store, accounts, index, _ = _build(
            verifier=FakeVerifier({"good": _identity(UID, JP_PHONE_E164)}))
        asyncio.run(accounts.create(account_id="acct_uid", registered_name="A",
                                    entity_type="individual"))
        asyncio.run(accounts.create(account_id="acct_phone", registered_name="B",
                                    entity_type="individual"))
        asyncio.run(index.claim(INDEX_KIND_FIREBASE, UID, "acct_uid"))
        asyncio.run(index.claim(INDEX_KIND_PHONE,
                                normalize_phone(JP_PHONE_E164), "acct_phone"))

        resp = _post(client)
        assert resp.status_code == 409
        assert resp.json()["code"] == "identity_conflict"
        assert asyncio.run(key_store.list_keys()) == []
        assert len(asyncio.run(accounts.list_accounts())) == 2


class TestTokenIsTheOnlySourceOfIdentity:
    def test_a_client_supplied_phone_is_ignored(self):
        # Accepting it would put us straight back to unverified signup.
        client, _, accounts, index, _ = _build()
        # A reserved number nobody owns: the assertion below only bites while
        # it is the very number the caller tried to smuggle in.
        _post(client, phone="+1 415 555 0166", email="attacker@example.com")
        record = asyncio.run(accounts.list_accounts())[0]
        assert record.phone == PHONE_E164
        assert asyncio.run(index.lookup(INDEX_KIND_PHONE, "14155550166")) is None

    def test_a_client_supplied_account_id_is_ignored(self):
        client, _, accounts, _, _ = _build()
        _post(client, account_id="acct_victim")
        stored = asyncio.run(accounts.list_accounts())[0].account_id
        assert stored != "acct_victim"
        assert stored.startswith("acct_")

    def test_a_client_supplied_email_is_not_indexed(self):
        # This round verifies phones only; an unverified email must not become
        # a merge key that another signup can collide onto.
        client, _, _, index, _ = _build()
        _post(client, email="ops@example.com")
        assert asyncio.run(index.lookup(INDEX_KIND_EMAIL, "ops@example.com")) is None


class TestRejections:
    def test_missing_token_is_401(self):
        client, *_ = _build()
        resp = client.post("/api/v1/signup/verified", json=BODY)
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_identity_token"

    def test_bad_token_is_401(self):
        client, *_ = _build()
        resp = _post(client, token="forged")
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_identity_token"

    def test_non_bearer_header_is_401(self):
        client, *_ = _build()
        resp = client.post("/api/v1/signup/verified", json=BODY,
                           headers={"Authorization": "good"})
        assert resp.status_code == 401

    def test_unconfigured_verifier_is_503(self):
        client, *_ = _build(verifier=None)
        resp = _post(client)
        assert resp.status_code == 503
        assert resp.json()["code"] == "signup_unavailable"

    def test_billing_not_enforcing_is_503(self):
        # A zero-balance wallet is what stops a self-minted key from spending;
        # without the gate, verification alone would be handing out free jobs.
        client, *_ = _build(mode="log")
        assert _post(client).status_code == 503

    def test_no_key_store_is_503(self):
        client, *_ = _build(with_key_store=False)
        assert _post(client).status_code == 503


class TestLegacyEndpointIsNowInternal:
    """A public unverified door beside the verified one voids the verification."""

    def test_unauthenticated_legacy_signup_is_401(self):
        client, _, accounts, _, _ = _build()
        resp = client.post("/api/v1/signup",
                           json={"registered_name": "x", "phone": "13800138000"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_admin_secret"
        assert asyncio.run(accounts.list_accounts()) == []

    def test_wrong_secret_is_401(self):
        client, *_ = _build()
        resp = client.post("/api/v1/signup",
                           json={"registered_name": "x", "phone": "13800138000"},
                           headers={"x-admin-secret": "not-the-secret"})
        assert resp.status_code == 401

    def test_admin_can_still_use_it(self):
        client, *_ = _build()
        resp = client.post("/api/v1/signup",
                           json={"registered_name": "x", "phone": "13800138000"},
                           headers={"x-admin-secret": ADMIN_SECRET})
        assert resp.status_code == 200
        assert resp.json()["api_key"].startswith("sk-")


class TestKeyNaming:
    """The account note records provenance; a key note is a label a customer reads.

    The verified door mints nothing now, so the only door left that can leak
    one into the other is the internal one — which is where the bug lived: the
    key inherited the account's provenance string, and every console showed a
    key called "firebase phone signup" in the one field a customer may edit.
    """

    def test_the_internal_door_names_its_key_readably(self):
        client, key_store, accounts, _, _ = _build()
        resp = client.post("/api/v1/signup",
                           json={"registered_name": "老客户",
                                 "phone": "13800138000"},
                           headers={"x-admin-secret": ADMIN_SECRET})
        assert resp.status_code == 200, resp.text
        account_id = asyncio.run(accounts.list_accounts())[0].account_id
        keys = asyncio.run(key_store.list_keys(user_id=account_id))
        assert keys[0].note == DEFAULT_KEY_NAME
        assert DEFAULT_KEY_NAME != SIGNUP_NOTE

    def test_the_account_still_records_where_it_came_from(self):
        client, _, accounts, _, _ = _build()
        _post(client)
        assert asyncio.run(accounts.list_accounts())[0].note == VERIFIED_SIGNUP_NOTE

    def test_a_caller_cannot_smuggle_a_key_name_past_the_verified_door(self):
        # The field is gone; an extra body key must be ignored, not honoured
        # into a key that this door is no longer supposed to mint at all.
        client, key_store, _, _, _ = _build()
        assert _post(client, key_name="生产环境").status_code == 200
        assert asyncio.run(key_store.list_keys()) == []
