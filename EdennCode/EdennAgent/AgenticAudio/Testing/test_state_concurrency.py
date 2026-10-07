"""Concurrent writes to a session's state, against a real database.

``state_json`` is one JSONB column holding everything about a session, and every
writer does read-modify-write on it: read the whole document, change one branch,
write the whole document back. Two writers that overlap therefore do not merge —
the second one writes a document built from a copy that predates the first, and
the first one's change is gone with no error anywhere.

This is not hypothetical. A turn writes through the per-session lock, but the
2.5-second poll path hydrates job results and writes OUTSIDE it, so "your take
finished" and "the agent recorded a plan" race on every session that is
generating something.

These tests need a real Postgres — the whole subject is what the database does
when two transactions touch one row, which no fake can tell you. They skip when
one is not configured:

    docker run -d --name edenn-test-pg -e POSTGRES_PASSWORD=test \\
      -e POSTGRES_DB=edenn_test -p 55432:5432 postgres:16-alpine
    EDENN_TEST_PG_DSN=postgresql://postgres:postgres@127.0.0.1:55432/edenn_test pytest ...
"""

from __future__ import annotations

import os
import threading
import uuid
from typing import Any, Optional

import pytest

DSN = os.getenv("EDENN_TEST_PG_DSN", "").strip()

pytestmark = pytest.mark.skipif(
    not DSN, reason="set EDENN_TEST_PG_DSN to a throwaway Postgres to run these"
)


def _client_factory():
    from EdennCode.Deployment.postgres_wrapper import (
        PostgresClient,
        PostgresConnectionConfig,
    )

    from urllib.parse import urlparse

    parsed = urlparse(DSN)

    def make() -> Any:
        return PostgresClient(
            PostgresConnectionConfig(
                host=parsed.hostname or "127.0.0.1",
                port=parsed.port or 5432,
                database=(parsed.path or "/postgres").lstrip("/"),
                user=parsed.username or "postgres",
                password=parsed.password,
                # A throwaway container has no TLS, and the default is "require".
                sslmode="disable",
                connect_timeout=10,
            )
        )

    return make


@pytest.fixture()
def repo():
    from EdennCode.EdennAgent.AgenticAudio.persistence.repositories import (
        AgenticAudioRepository,
    )

    r = AgenticAudioRepository(client_factory=_client_factory())
    r.ensure_schema()
    return r


@pytest.fixture()
def session_id(repo) -> str:
    sid = f"sess_{uuid.uuid4().hex[:12]}"
    repo.create_session(
        session_id=sid,
        source_video_artifact_id="artifact_test",
        creator_user_id="tester",
        phase="observing",
        state_json={"observation": {"duration_s": 16}, "candidates": [], "layers": {}},
    )
    return sid


# ---------------------------------------------------------------------------#
# the bug                                                                     #
# ---------------------------------------------------------------------------#


def test_two_overlapping_writers_lose_one_of_the_updates(repo, session_id) -> None:
    """The failure, reproduced deliberately.

    Both writers read the same document, change a DIFFERENT branch of it, and
    write it back. Nothing errors; one change simply is not there afterwards.
    This is what the poll path and a turn do to each other.
    """
    first = repo.get_session(session_id)
    second = repo.get_session(session_id)

    a = dict(first.state_json)
    a["candidates"] = [{"candidate_id": "c1", "status": "completed"}]
    repo.update_session(session_id, state_json=a)

    b = dict(second.state_json)          # built before the write above landed
    b["layers"] = {"voiceover": {"status": "draft"}}
    repo.update_session(session_id, state_json=b)

    final = repo.get_session(session_id).state_json
    assert final.get("layers"), "the second writer's change is missing"
    assert not final.get("candidates"), (
        "if this passes, blind overwrite no longer loses the first write"
    )


# ---------------------------------------------------------------------------#
# the fix                                                                     #
# ---------------------------------------------------------------------------#


def test_a_locked_mutation_keeps_both_changes(repo, session_id) -> None:
    """Read and write inside one transaction, with the row held.

    A read-modify-write cannot be made safe from the outside: the read and the
    write have to be in the same critical section, which is why this is a
    repository primitive and not a rule callers are asked to remember.
    """
    repo.mutate_session_state(
        session_id,
        lambda st: {**st, "candidates": [{"candidate_id": "c1", "status": "completed"}]},
    )
    repo.mutate_session_state(
        session_id, lambda st: {**st, "layers": {"voiceover": {"status": "draft"}}}
    )

    final = repo.get_session(session_id).state_json
    assert final["candidates"][0]["candidate_id"] == "c1"
    assert final["layers"]["voiceover"]["status"] == "draft"


def test_concurrent_mutations_from_real_threads_all_survive(repo, session_id) -> None:
    """Eight threads, each adding its own key, actually in parallel.

    The sequential test above proves the primitive composes; this proves the row
    lock does its job when the transactions genuinely overlap.
    """
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def add(n: int) -> None:
        try:
            barrier.wait(timeout=10)
            repo.mutate_session_state(session_id, lambda st: {**st, f"key_{n}": n})
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"a concurrent mutation failed: {errors[:2]}"
    final = repo.get_session(session_id).state_json
    missing = [n for n in range(8) if f"key_{n}" not in final]
    assert not missing, f"lost updates from threads {missing}"


def test_the_version_advances_on_every_write(repo, session_id) -> None:
    """A monotonic version makes a lost update DETECTABLE after the fact, which
    is what turns "the state looks wrong" into something you can investigate."""
    start = repo.get_session(session_id).version
    repo.mutate_session_state(session_id, lambda st: {**st, "a": 1})
    repo.mutate_session_state(session_id, lambda st: {**st, "b": 2})
    assert repo.get_session(session_id).version == start + 2


def test_a_stale_write_is_refused_when_the_caller_knows_its_version(
    repo, session_id
) -> None:
    """Callers that DO track a version get compare-and-set, so a blind overwrite
    fails loudly instead of silently winning."""
    from EdennCode.EdennAgent.AgenticAudio.persistence.repositories import (
        StaleSessionState,
    )

    stale = repo.get_session(session_id)
    repo.mutate_session_state(session_id, lambda st: {**st, "moved": True})

    with pytest.raises(StaleSessionState):
        repo.update_session(
            session_id,
            state_json={**stale.state_json, "clobber": True},
            expected_version=stale.version,
        )
    assert repo.get_session(session_id).state_json.get("moved") is True


def test_a_mutation_that_raises_leaves_the_row_untouched(repo, session_id) -> None:
    """The transaction must roll back, or a half-applied change outlives the
    failure that produced it."""
    before = repo.get_session(session_id)

    def explode(_st: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("tool blew up mid-mutation")

    with pytest.raises(RuntimeError):
        repo.mutate_session_state(session_id, explode)

    after = repo.get_session(session_id)
    assert after.state_json == before.state_json
    assert after.version == before.version


def test_mutating_a_session_that_does_not_exist_raises_key_error(repo) -> None:
    with pytest.raises(KeyError):
        repo.mutate_session_state("sess_nope", lambda st: st)


def test_returning_none_from_a_mutation_means_no_change(repo, session_id) -> None:
    """"I looked and there was nothing to do" is the common case on the poll
    path; it must not cost a write or a version bump."""
    before = repo.get_session(session_id)
    repo.mutate_session_state(session_id, lambda st: None)
    after = repo.get_session(session_id)
    assert after.version == before.version
    assert after.state_json == before.state_json
