"""Agentic Audio owns everything on its own. This file is where that is true.

Own deployment cycle, own hardware, own database — the owner's decision, made
after the studio's migrations landed on a production server it was never meant
to touch. The cause both times was ambient configuration: a helper that reads
the repo's `.env`, which points at the platform's databases and overrides
whatever the caller passed.

These tests make the isolation structural. Some assert behaviour; the contract
ones grep the source, because the difference between "we don't call that" and
"we can't call that" is what a 3am incident turns on.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from EdennCode.EdennAgent.AgenticAudio import config as cfg

PACKAGE = Path(cfg.__file__).resolve().parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in list("AGENTIC_AUDIO_PG_HOST AGENTIC_AUDIO_PG_PORT AGENTIC_AUDIO_PG_DATABASE AGENTIC_AUDIO_PG_USER AGENTIC_AUDIO_PG_PASSWORD AGENTIC_AUDIO_PG_SSLMODE".split()):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------#
# the studio's own namespace                                                  #
# ---------------------------------------------------------------------------#


def test_no_database_configured_means_no_database(monkeypatch) -> None:
    """In-memory is a supported mode. Quietly borrowing a connection from the
    platform's environment is not."""
    monkeypatch.setenv("PGHOST", "telemetry-db.example.invalid")
    monkeypatch.setenv("PGDATABASE", "telemetry")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:REDACTED@telemetry-db:5432/telemetry")
    assert cfg.database_target() is None, (
        "the studio read the platform's PG* variables"
    )
    assert cfg.client_factory() is None


def test_the_studio_reads_only_its_own_variables(monkeypatch) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_PG_HOST", "studio-db.example")
    monkeypatch.setenv("AGENTIC_AUDIO_PG_USER", "studio")
    monkeypatch.setenv("AGENTIC_AUDIO_PG_PASSWORD", "s3cret")
    target = cfg.database_target()
    assert target is not None
    assert target.host == "studio-db.example"
    assert target.database == "agentic_audio"  # its own default, not "telemetry"


@pytest.mark.parametrize(
    "host",
    [
        "telemetry-db.example.invalid",
        "telemetry-db-dev.example.invalid",
        "billing-db.example.invalid",
        "db.example.invalid",
    ],
)
def test_the_platforms_databases_are_refused_by_name(monkeypatch, host) -> None:
    """Even a correctly-namespaced variable pointing at the platform is refused.
    Isolation is the product decision; this is where it is enforced."""
    monkeypatch.setenv("AGENTIC_AUDIO_PG_HOST", host)
    with pytest.raises(cfg.ForbiddenDatabase) as exc:
        cfg.database_target()
    assert "owns its own database" in str(exc.value)


def test_the_wrong_live_database_is_refused_before_anything_writes() -> None:
    """The one-round-trip check that would have stopped both incidents at the
    first statement instead of after four migrations."""

    class WrongClient:
        def run_sql(self, *_a, **_k):
            return [{"db": "telemetry", "su": "off"}]

        def close(self):
            self.closed = True

    target = cfg.DatabaseTarget(
        host="h", port=5432, database="agentic_audio", user="u", password="p"
    )
    client = WrongClient()
    with pytest.raises(cfg.WrongDatabase) as exc:
        cfg.assert_connected_where_intended(client, target)
    assert "telemetry" in str(exc.value)
    assert getattr(client, "closed", False), "the wrong connection was left open"


def test_the_right_live_database_passes() -> None:
    class RightClient:
        def run_sql(self, *_a, **_k):
            return [{"db": "agentic_audio", "su": "off"}]

    target = cfg.DatabaseTarget(
        host="h", port=5432, database="agentic_audio", user="u", password="p"
    )
    cfg.assert_connected_where_intended(RightClient(), target)  # must not raise


# ---------------------------------------------------------------------------#
# "can't", not "don't" — enforced against the source                          #
# ---------------------------------------------------------------------------#

_PY_FILES = [
    p for p in PACKAGE.rglob("*.py")
    if "Testing" not in p.parts and "__pycache__" not in p.parts
]


def test_nothing_in_the_package_uses_the_ambient_config() -> None:
    """`from_env()` is the function that reads the repo's `.env` — the file that
    points at production and overrides the caller. One call site anywhere under
    AgenticAudio is the whole incident, again."""
    offenders = []
    for path in _PY_FILES:
        source = path.read_text(encoding="utf-8")
        for i, line in enumerate(source.splitlines(), 1):
            if re.search(r"\bfrom_env\s*\(", line) and not line.strip().startswith("#"):
                offenders.append(f"{path.relative_to(PACKAGE)}:{i}")
    assert not offenders, (
        "ambient database config inside AgenticAudio:\n  " + "\n  ".join(offenders)
    )


def test_nothing_in_the_package_reads_the_platforms_pg_variables() -> None:
    """PGHOST/PGDATABASE/DATABASE_URL belong to the platform. The studio's own
    namespace is AGENTIC_AUDIO_*; reading the shared names reintroduces the
    ambient path with different spelling."""
    pattern = re.compile(
        r"""(getenv|environ(\.get)?\s*[\(\[])\s*['"](PGHOST|PGPORT|PGDATABASE|PGUSER|PGPASSWORD|DATABASE_URL)['"]"""
    )
    offenders = []
    for path in _PY_FILES:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and not line.strip().startswith("#"):
                offenders.append(f"{path.relative_to(PACKAGE)}:{i}")
    assert not offenders, (
        "platform PG* variables read inside AgenticAudio:\n  " + "\n  ".join(offenders)
    )


def test_the_pool_refuses_to_run_without_the_studios_own_target(monkeypatch) -> None:
    """The pooled factory must never silently fall back to anything ambient."""
    from EdennCode.EdennAgent.AgenticAudio.persistence import pool

    pool.reset_for_tests()
    monkeypatch.setenv("PGHOST", "telemetry-db.example.invalid")
    with pytest.raises(RuntimeError) as exc:
        pool.pooled_client()
    assert "AGENTIC_AUDIO_PG_HOST" in str(exc.value)
