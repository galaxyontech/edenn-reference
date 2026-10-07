"""Account index: normalization rules and atomic first-writer-wins claims."""
from __future__ import annotations

import asyncio
import hashlib
import logging

import pytest

from EdennCode.Deployment.billing.account_index import (
    INDEX_KIND_EMAIL,
    INDEX_KIND_FIREBASE,
    INDEX_KIND_PHONE,
    AccountIndexStore,
    normalize_email,
    normalize_phone,
    normalize_uid,
)
from EdennCode.Deployment.billing.stores import BillingStoreUnavailable
from EdennCode.Deployment.Testing.test_billing_stores import FakeBillingTable


def _store() -> AccountIndexStore:
    return AccountIndexStore(FakeBillingTable(), logger=logging.getLogger("t"))


class TestNormalization:
    def test_email_trims_and_lowercases(self):
        assert normalize_email("  OPS@Example.COM ") == "ops@example.com"
        assert normalize_email("") == ""
        assert normalize_email(None) == ""

    def test_email_keeps_plus_suffix_distinct(self):
        # a+ci@x.com and a@x.com are different mailboxes as far as we care.
        assert normalize_email("a+ci@x.com") != normalize_email("a@x.com")

    def test_phone_strips_separators(self):
        for raw in ("13800138000", "138-0013-8000", "138 0013 8000",
                    "(138)00138000"):
            assert normalize_phone(raw) == "13800138000", raw

    def test_phone_drops_chinese_country_code(self):
        # 130 0000 0000 is a reserved synthetic test number — never swap in a
        # real one. All three ways of writing it must land on the same key, or
        # a returning customer whose token spells the country code out would
        # miss the account they opened without it.
        for raw in ("+86 130 0000 0000", "8613000000000", "+8613000000000"):
            assert normalize_phone(raw) == "13000000000", raw

    def test_phone_leaves_other_country_codes_alone(self):
        # Only the 13-digit 86-prefixed shape is unwrapped; documented limit.
        assert normalize_phone("+1 415 555 0100") == "14155550100"

    def test_phone_of_only_separators_is_empty(self):
        assert normalize_phone("---") == ""
        assert normalize_phone(None) == ""

    def test_uid_only_trims_whitespace(self):
        assert normalize_uid("  kQ2mZ8xVbNfR4t  ") == "kQ2mZ8xVbNfR4t"
        assert normalize_uid("") == ""
        assert normalize_uid(None) == ""

    def test_uid_keeps_case(self):
        # Firebase UIDs are case-sensitive: aB3 and Ab3 are two different
        # users, so folding case would let one claim the other's account.
        assert normalize_uid("aB3xYz") == "aB3xYz"
        assert normalize_uid("aB3xYz") != normalize_uid("ab3xyz")


class TestFirebaseKind:
    """Same table, third partition — the structure was designed for this."""

    def test_claim_then_lookup_roundtrip(self):
        store = _store()
        uid = normalize_uid("kQ2mZ8xVbNfR4tYuIoPaSdFgHjK1")
        assert asyncio.run(store.claim(INDEX_KIND_FIREBASE, uid, "acct_1")) is True
        assert asyncio.run(store.lookup(INDEX_KIND_FIREBASE, uid)) == "acct_1"

    def test_second_claim_loses_to_the_first(self):
        store = _store()
        uid = "kQ2mZ8xVbNfR4t"
        assert asyncio.run(store.claim(INDEX_KIND_FIREBASE, uid, "acct_1")) is True
        assert asyncio.run(store.claim(INDEX_KIND_FIREBASE, uid, "acct_2")) is False
        assert asyncio.run(store.lookup(INDEX_KIND_FIREBASE, uid)) == "acct_1"

    def test_kinds_share_no_namespace(self):
        # A uid and a phone number that happen to normalize alike must not
        # collide: the partition key keeps them apart.
        store = _store()
        asyncio.run(store.claim(INDEX_KIND_FIREBASE, "13800138000", "acct_uid"))
        asyncio.run(store.claim(INDEX_KIND_PHONE, "13800138000", "acct_phone"))
        assert asyncio.run(
            store.lookup(INDEX_KIND_FIREBASE, "13800138000")) == "acct_uid"
        assert asyncio.run(
            store.lookup(INDEX_KIND_PHONE, "13800138000")) == "acct_phone"

    def test_empty_uid_claims_nothing(self):
        store = _store()
        assert asyncio.run(store.claim(INDEX_KIND_FIREBASE, "", "acct_1")) is False
        assert asyncio.run(store.lookup(INDEX_KIND_FIREBASE, "")) is None


class TestClaimAndLookup:
    def test_claim_then_lookup(self):
        store = _store()
        assert asyncio.run(store.claim(INDEX_KIND_EMAIL, "ops@example.com",
                                       "acct_1")) is True
        assert asyncio.run(store.lookup(INDEX_KIND_EMAIL,
                                        "ops@example.com")) == "acct_1"

    def test_lookup_miss_returns_none(self):
        store = _store()
        assert asyncio.run(store.lookup(INDEX_KIND_EMAIL, "nobody@x.com")) is None

    def test_second_claim_of_same_value_fails_without_overwriting(self):
        store = _store()
        assert asyncio.run(store.claim(INDEX_KIND_PHONE, "13800138000",
                                       "acct_first")) is True
        assert asyncio.run(store.claim(INDEX_KIND_PHONE, "13800138000",
                                       "acct_second")) is False
        assert asyncio.run(store.lookup(INDEX_KIND_PHONE,
                                        "13800138000")) == "acct_first"

    def test_same_value_under_different_kinds_does_not_collide(self):
        store = _store()
        assert asyncio.run(store.claim(INDEX_KIND_PHONE, "12345", "acct_p")) is True
        assert asyncio.run(store.claim(INDEX_KIND_EMAIL, "12345", "acct_e")) is True
        assert asyncio.run(store.lookup(INDEX_KIND_PHONE, "12345")) == "acct_p"
        assert asyncio.run(store.lookup(INDEX_KIND_EMAIL, "12345")) == "acct_e"

    def test_row_key_is_hashed_and_value_kept_for_operators(self):
        # RowKey must be a hash: Table Storage rejects `/ \ # ?` in row keys and
        # a normalized email may legally contain `#`.
        store = _store()
        asyncio.run(store.claim(INDEX_KIND_EMAIL, "od#d@example.com", "acct_1"))
        expected = hashlib.sha256(b"od#d@example.com").hexdigest()
        entity = store._table_client.entities[(INDEX_KIND_EMAIL, expected)]
        assert entity["account_id"] == "acct_1"
        assert entity["value"] == "od#d@example.com"
        assert entity["created_at"]
        assert asyncio.run(store.lookup(INDEX_KIND_EMAIL,
                                        "od#d@example.com")) == "acct_1"


class TestEmptyValueIsUnclaimable:
    """An empty normalized contact must own nothing and match nothing.

    ``normalize_phone("---")``, ``normalize_phone(None)`` and
    ``normalize_email(None)`` all return ``""``, and ``sha256("")`` is a
    perfectly valid row key — so without a guard the first contact-less signup
    would own that row and every later one would "find" it and merge onto its
    wallet.
    """

    def test_claim_of_empty_value_is_refused_and_writes_nothing(self):
        store = _store()
        assert asyncio.run(store.claim(INDEX_KIND_PHONE, "", "acct_x")) is False
        assert store._table_client.entities == {}

    def test_lookup_of_empty_value_is_none_and_reads_nothing(self):
        store = _store()
        asyncio.run(store.claim(INDEX_KIND_PHONE, "13800138000", "acct_1"))

        def _boom(*args, **kwargs):
            raise AssertionError("empty lookup must not touch storage")

        store._table_client.get_entity = _boom
        assert asyncio.run(store.lookup(INDEX_KIND_PHONE, "")) is None

    def test_two_contactless_signups_cannot_merge_onto_one_row(self):
        # The hazard the guard exists for, end to end.
        store = _store()
        assert asyncio.run(store.claim(INDEX_KIND_EMAIL, "", "acct_first")) is False
        assert asyncio.run(store.lookup(INDEX_KIND_EMAIL, "")) is None
        assert asyncio.run(store.claim(INDEX_KIND_EMAIL, "", "acct_second")) is False


class TestStorageFailure:
    def test_read_failure_raises_billing_store_unavailable(self):
        store = _store()
        store._table_client.fail_reads = True
        with pytest.raises(BillingStoreUnavailable):
            asyncio.run(store.lookup(INDEX_KIND_EMAIL, "ops@example.com"))
