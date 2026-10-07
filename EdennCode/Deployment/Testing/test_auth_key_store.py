"""ApiKeyStore unit tests against an in-memory fake table client."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import pytest

from EdennCode.Deployment.auth.key_store import (
    ApiKeyStore,
    KeyStoreUnavailable,
    Principal,
    display_prefix,
    generate_api_key,
    hash_api_key,
)


class FakeResourceNotFound(Exception):
    pass


class FakeTableClient:
    """Duck-typed azure.data.tables.TableClient double (see key_store contract).

    ``mode`` is honored the way the real SDK honors it: a merge writes only the
    supplied properties over the stored row. The default here stays *replace*
    (the real default is merge) because every caller that omits the argument
    writes a complete entity anyway, and replace is the stricter assertion.
    """

    def __init__(self) -> None:
        self.entities: dict[tuple[str, str], dict[str, Any]] = {}
        self.fail_all = False
        self.writes: list[tuple[dict[str, Any], Any]] = []

    def get_entity(self, partition_key: str, row_key: str) -> dict[str, Any]:
        if self.fail_all:
            raise ConnectionError("table storage unreachable")
        try:
            return dict(self.entities[(partition_key, row_key)])
        except KeyError:
            raise FakeResourceNotFound(row_key)

    def upsert_entity(self, entity: dict[str, Any], mode: Any = None) -> None:
        if self.fail_all:
            raise ConnectionError("table storage unreachable")
        self.writes.append((dict(entity), mode))
        slot = (entity["PartitionKey"], entity["RowKey"])
        if _is_merge(mode) and slot in self.entities:
            self.entities[slot].update(entity)
        else:
            self.entities[slot] = dict(entity)

    def query_entities(self, query_filter: str) -> Iterable[dict[str, Any]]:
        if self.fail_all:
            raise ConnectionError("table storage unreachable")
        return [dict(e) for e in self.entities.values()]

    def update_entity(self, entity: dict[str, Any], mode: Any = None) -> None:
        self.upsert_entity(entity, mode=mode)


def _is_merge(mode: Any) -> bool:
    return mode is not None and str(getattr(mode, "value", mode)).lower() == "merge"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _store(client=None, clock=None) -> tuple[ApiKeyStore, FakeTableClient, FakeClock]:
    client = client or FakeTableClient()
    clock = clock or FakeClock()
    return ApiKeyStore(client, clock=clock), client, clock


def test_key_material_shapes():
    key = generate_api_key()
    assert key.startswith("sk-")
    assert len(key) > 30
    assert generate_api_key() != key
    assert len(hash_api_key(key)) == 64
    assert display_prefix(key) == key[:12]


def test_mint_then_lookup_roundtrip():
    store, client, _ = _store()
    plaintext, record = asyncio.run(store.mint(user_id="user-1", note="test key"))
    assert plaintext.startswith("sk-")
    assert record.user_id == "user-1"
    # only the hash is stored
    stored = client.entities[("APIKEY", hash_api_key(plaintext))]
    assert plaintext not in str(stored)
    principal = asyncio.run(store.lookup(plaintext))
    assert principal == Principal(user_id="user-1", key_prefix=plaintext[:12])


def test_lookup_unknown_key_returns_none():
    store, _, _ = _store()
    assert asyncio.run(store.lookup("sk-does-not-exist")) is None


def test_revoked_key_fails_lookup():
    store, _, _ = _store()
    plaintext, record = asyncio.run(store.mint(user_id="user-1"))
    assert asyncio.run(store.revoke(record.key_prefix)) is True
    # a fresh store (no warm cache) must see the revocation
    store2 = ApiKeyStore(store._table_client)
    assert asyncio.run(store2.lookup(plaintext)) is None


def test_cache_serves_hit_without_second_read_and_expires():
    store, client, clock = _store()
    plaintext, _ = asyncio.run(store.mint(user_id="user-1"))
    assert asyncio.run(store.lookup(plaintext)) is not None
    client.fail_all = True  # backend gone; cache must serve
    assert asyncio.run(store.lookup(plaintext)) is not None
    clock.now += 61.0  # past positive TTL
    with pytest.raises(KeyStoreUnavailable):
        asyncio.run(store.lookup(plaintext))


def test_negative_cache_expires_faster():
    store, client, clock = _store()
    assert asyncio.run(store.lookup("sk-nope")) is None
    client.fail_all = True
    assert asyncio.run(store.lookup("sk-nope")) is None  # negative cache serves
    clock.now += 16.0  # past negative TTL (15 s)
    with pytest.raises(KeyStoreUnavailable):
        asyncio.run(store.lookup("sk-nope"))


def test_backend_error_with_no_cache_raises_unavailable():
    store, client, _ = _store()
    client.fail_all = True
    with pytest.raises(KeyStoreUnavailable):
        asyncio.run(store.lookup("sk-anything"))


def test_list_keys_filters_by_user():
    store, _, _ = _store()
    asyncio.run(store.mint(user_id="user-1"))
    asyncio.run(store.mint(user_id="user-2"))
    all_keys = asyncio.run(store.list_keys())
    user1 = asyncio.run(store.list_keys(user_id="user-1"))
    assert len(all_keys) == 2
    assert len(user1) == 1 and user1[0].user_id == "user-1"


class FakeWallClock:
    """Injected wall time — the last-used floor is measured in real seconds."""

    def __init__(self) -> None:
        self.now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _tracking_store():
    """(store, table, monotonic clock, wall clock) with last-used writes on."""
    client = FakeTableClient()
    clock = FakeClock()
    wall = FakeWallClock()
    store = ApiKeyStore(client, clock=clock, wall_clock=wall)
    return store, client, clock, wall


def _touches(client: FakeTableClient) -> list[tuple[dict[str, Any], Any]]:
    """Only the last-used stamps — mint writes the column too, as an empty."""
    return [
        w for w in client.writes
        if set(w[0]) == {"PartitionKey", "RowKey", "last_used_at"}
    ]


def _lookup(store: ApiKeyStore, presented: str):
    """Lookup + drain: the last-used write is scheduled, not awaited."""

    async def run():
        principal = await store.lookup(presented)
        await store.drain()
        return principal

    return asyncio.run(run())


class TestLastUsedTracking:
    """Nobody deletes an old key without knowing whether anything still calls it."""

    def test_minted_key_has_no_last_used_yet(self):
        store, _, _, _ = _tracking_store()
        _, record = asyncio.run(store.mint(user_id="user-1"))
        assert record.last_used_at is None

    def test_lookup_records_the_moment_of_use(self):
        store, client, _, wall = _tracking_store()
        plaintext, record = asyncio.run(store.mint(user_id="user-1"))
        assert _lookup(store, plaintext) is not None
        stored = client.entities[("APIKEY", hash_api_key(plaintext))]
        assert stored["last_used_at"] == wall.now.isoformat()
        assert asyncio.run(store.list_keys(user_id="user-1"))[0].last_used_at

    def test_writes_only_the_timestamp_so_a_concurrent_revoke_survives(self):
        # The entity we read may already be stale. Writing it back whole would
        # flip is_active back to True and hand a revoked key its life back.
        store, client, clock, wall = _tracking_store()
        plaintext, record = asyncio.run(store.mint(user_id="user-1"))
        assert _lookup(store, plaintext) is not None
        touch = _touches(client)[-1]
        entity, mode = touch
        assert set(entity) == {"PartitionKey", "RowKey", "last_used_at"}
        assert _is_merge(mode), "last-used write must MERGE, not replace"

    def test_a_stale_touch_cannot_resurrect_a_revoked_key(self):
        store, client, clock, wall = _tracking_store()
        plaintext, record = asyncio.run(store.mint(user_id="user-1"))
        assert _lookup(store, plaintext) is not None
        asyncio.run(store.revoke_for_user(record.key_prefix, "user-1"))
        # Replay the touch the way a slow background write would land.
        clock.now += 61.0
        wall.advance(600)
        fresh = ApiKeyStore(client, clock=FakeClock(), wall_clock=wall)
        assert _lookup(fresh, plaintext) is None
        stored = client.entities[("APIKEY", hash_api_key(plaintext))]
        assert stored["is_active"] is False

    def test_touch_is_throttled_below_the_floor(self):
        store, client, clock, wall = _tracking_store()
        plaintext, _ = asyncio.run(store.mint(user_id="user-1"))
        _lookup(store, plaintext)
        first = len(_touches(client))
        clock.now += 61.0        # cache expired: a second table read happens
        wall.advance(60)         # but only a minute of wall time passed
        _lookup(store, plaintext)
        after = len(_touches(client))
        assert after == first, "a hot key must not write once per cache miss"

    def test_touch_resumes_after_the_floor(self):
        store, client, clock, wall = _tracking_store()
        plaintext, _ = asyncio.run(store.mint(user_id="user-1"))
        _lookup(store, plaintext)
        clock.now += 61.0
        wall.advance(301)
        _lookup(store, plaintext)
        touches = _touches(client)
        assert len(touches) == 2
        assert touches[-1][0]["last_used_at"] == wall.now.isoformat()

    def test_a_failing_touch_never_breaks_authentication(self):
        store, client, _, _ = _tracking_store()
        plaintext, _ = asyncio.run(store.mint(user_id="user-1"))

        def explode(entity, mode=None):
            raise ConnectionError("table storage unreachable")

        client.upsert_entity = explode  # type: ignore[assignment]
        assert _lookup(store, plaintext) is not None

    def test_tracking_can_be_switched_off(self):
        client = FakeTableClient()
        store = ApiKeyStore(client, clock=FakeClock(), track_last_used=False)
        plaintext, _ = asyncio.run(store.mint(user_id="user-1"))
        _lookup(store, plaintext)
        assert not _touches(client)


class TestAccountScopedRevocation:
    """Self-serve revocation is an authorization problem, not a lookup problem."""

    def test_owner_revokes_own_key(self):
        store, _, _, _ = _tracking_store()
        plaintext, record = asyncio.run(store.mint(user_id="user-1"))
        assert asyncio.run(
            store.revoke_for_user(record.key_prefix, "user-1")) is True
        fresh = ApiKeyStore(store._table_client)
        assert asyncio.run(fresh.lookup(plaintext)) is None

    def test_another_account_cannot_revoke_it(self):
        # key_prefix shows up in 详单 rows and gets pasted into support
        # tickets — knowing one must not be enough to disable it.
        store, _, _, _ = _tracking_store()
        victim, record = asyncio.run(store.mint(user_id="victim"))
        assert asyncio.run(
            store.revoke_for_user(record.key_prefix, "attacker")) is False
        fresh = ApiKeyStore(store._table_client)
        assert asyncio.run(fresh.lookup(victim)) is not None

    def test_revoking_twice_reports_nothing_to_do(self):
        store, _, _, _ = _tracking_store()
        _, record = asyncio.run(store.mint(user_id="user-1"))
        assert asyncio.run(store.revoke_for_user(record.key_prefix, "user-1"))
        assert asyncio.run(
            store.revoke_for_user(record.key_prefix, "user-1")) is False

    def test_blank_arguments_revoke_nothing(self):
        store, _, _, _ = _tracking_store()
        _, record = asyncio.run(store.mint(user_id="user-1"))
        assert asyncio.run(store.revoke_for_user("", "user-1")) is False
        assert asyncio.run(store.revoke_for_user(record.key_prefix, "")) is False
        fresh = ApiKeyStore(store._table_client)
        assert len(asyncio.run(fresh.list_keys(user_id="user-1"))) == 1
        assert asyncio.run(fresh.list_keys(user_id="user-1"))[0].is_active

    def test_revocation_drops_the_local_cache_entry(self):
        store, _, _, _ = _tracking_store()
        plaintext, record = asyncio.run(store.mint(user_id="user-1"))
        assert _lookup(store, plaintext) is not None  # warm the cache
        asyncio.run(store.revoke_for_user(record.key_prefix, "user-1"))
        assert asyncio.run(store.lookup(plaintext)) is None


class TestActiveKeyCount:
    def test_counts_only_this_account_and_only_live_keys(self):
        store, _, _, _ = _tracking_store()
        asyncio.run(store.mint(user_id="user-1"))
        _, second = asyncio.run(store.mint(user_id="user-1"))
        asyncio.run(store.mint(user_id="user-2"))
        asyncio.run(store.revoke_for_user(second.key_prefix, "user-1"))
        assert asyncio.run(store.count_active("user-1")) == 1
        assert asyncio.run(store.count_active("user-2")) == 1
        assert asyncio.run(store.count_active("nobody")) == 0


def test_from_settings_returns_none_without_storage():
    from types import SimpleNamespace
    import logging
    settings = SimpleNamespace(
        storage_connection_string=None, storage_account_url=None,
        storage_account_name=None, storage_account_key=None,
        auth_table_namespace="",
    )
    assert ApiKeyStore.from_settings(settings, logging.getLogger("t")) is None


def test_build_auth_table_client_uses_account_key_triad(monkeypatch):
    """The Japan container apps have no connection string — only the discrete
    AZURE_STORAGE_ACCOUNT_URL/NAME/KEY triad. The shared client builder must
    accept it (both ApiKeyStore and UsageRecorder construct through it)."""
    import logging
    import sys
    import types
    from types import SimpleNamespace

    from EdennCode.Deployment.auth.key_store import build_auth_table_client

    captured: dict = {}

    class _FakeService:
        def __init__(self, *, endpoint=None, credential=None):
            captured["endpoint"] = endpoint
            captured["credential"] = credential

        @classmethod
        def from_connection_string(cls, conn):
            captured["conn"] = conn
            return cls(endpoint="conn", credential=None)

        def create_table_if_not_exists(self, table_name):
            captured["table_name"] = table_name
            return f"client:{table_name}"

    class _FakeNamedKeyCredential:
        def __init__(self, name, key):
            captured["cred_name"] = name
            captured["cred_key"] = key

    fake_tables = types.ModuleType("azure.data.tables")
    fake_tables.TableServiceClient = _FakeService
    fake_core_creds = types.ModuleType("azure.core.credentials")
    fake_core_creds.AzureNamedKeyCredential = _FakeNamedKeyCredential
    monkeypatch.setitem(sys.modules, "azure.data.tables", fake_tables)
    monkeypatch.setitem(sys.modules, "azure.core.credentials", fake_core_creds)

    settings = SimpleNamespace(
        storage_connection_string=None,
        storage_account_url="https://primary.storage.example.invalid",
        storage_account_name="primarystorage",
        storage_account_key="fake-key",
        auth_table_namespace="dev",
    )
    client = build_auth_table_client(settings, "usagedev", logging.getLogger("t"))
    assert client == "client:usagedev"
    assert captured["endpoint"] == "https://primarystorage.table.core.windows.net"
    assert captured["cred_name"] == "primarystorage"
    assert captured["table_name"] == "usagedev"


def test_usage_recorder_from_settings_uses_triad(monkeypatch):
    """UsageRecorder must NOT be telemetry-only when only the triad is set."""
    import logging

    from EdennCode.Deployment.auth import key_store as ks
    from EdennCode.Deployment.auth.usage_recorder import UsageRecorder

    monkeypatch.setattr(
        ks, "build_auth_table_client", lambda s, t, l: FakeTableClient()
    )
    from types import SimpleNamespace

    settings = SimpleNamespace(
        storage_connection_string=None,
        storage_account_url="https://primary.storage.example.invalid",
        storage_account_name="primarystorage",
        storage_account_key="fake-key",
        auth_table_namespace="dev",
        auth_mode="enforce",
    )
    recorder = UsageRecorder.from_settings(settings, logging.getLogger("t"))
    assert recorder is not None
    assert recorder._table_client is not None


class TestRenaming:
    """Renaming is the same authorization problem as revoking, minus the danger."""

    def test_owner_renames_own_key(self):
        store, _, _, _ = _tracking_store()
        _, record = asyncio.run(store.mint(user_id="user-1", note="default"))
        assert asyncio.run(
            store.rename_for_user(record.key_prefix, "user-1", "生产环境")) is True
        assert asyncio.run(store.list_keys(user_id="user-1"))[0].note == "生产环境"

    def test_another_account_cannot_rename_it(self):
        store, _, _, _ = _tracking_store()
        _, record = asyncio.run(store.mint(user_id="victim", note="prod"))
        assert asyncio.run(
            store.rename_for_user(record.key_prefix, "attacker", "pwned")) is False
        assert asyncio.run(store.list_keys(user_id="victim"))[0].note == "prod"

    def test_renaming_writes_only_the_note(self):
        # Same reason the last-used stamp merges: a full write-back would undo a
        # concurrent revocation.
        store, client, _, _ = _tracking_store()
        _, record = asyncio.run(store.mint(user_id="user-1"))
        asyncio.run(store.rename_for_user(record.key_prefix, "user-1", "x"))
        write = [w for w in client.writes if "note" in w[0] and len(w[0]) <= 3][-1]
        assert set(write[0]) == {"PartitionKey", "RowKey", "note"}
        assert _is_merge(write[1])

    def test_a_revoked_key_cannot_be_renamed(self):
        store, _, _, _ = _tracking_store()
        _, record = asyncio.run(store.mint(user_id="user-1"))
        asyncio.run(store.revoke_for_user(record.key_prefix, "user-1"))
        assert asyncio.run(
            store.rename_for_user(record.key_prefix, "user-1", "x")) is False

    def test_blank_arguments_rename_nothing(self):
        store, _, _, _ = _tracking_store()
        _, record = asyncio.run(store.mint(user_id="user-1", note="keep"))
        assert asyncio.run(store.rename_for_user("", "user-1", "x")) is False
        assert asyncio.run(store.rename_for_user(record.key_prefix, "", "x")) is False
        assert asyncio.run(store.list_keys(user_id="user-1"))[0].note == "keep"
