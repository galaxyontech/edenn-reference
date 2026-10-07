"""Shared fixtures for the AgenticAudio suites."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """Give every test its own rate-limit and spend counters.

    The limiter is process-global, which is what a limiter has to be — but that
    also means one test's turns count against the next one's budget, and a suite
    that sends more turns than the per-minute ceiling starts failing tests that
    have nothing to do with limits. Reset around each test so the limits are
    exercised deliberately (see test_limits.py) and never by accident.
    """
    from EdennCode.EdennAgent.AgenticAudio.api.limits import limiter

    limiter().reset()
    yield
    limiter().reset()


@pytest.fixture(autouse=True)
def _no_real_databases(request, monkeypatch):
    """Make it impossible for a test to reach a real database by accident.

    A repository built without an explicit store falls back to
    ``PostgresClient.from_env`` — and in a developer's shell (or CI with secrets)
    those environment variables point at REAL infrastructure. One test written
    with the default constructor is enough to connect to production, run DDL,
    and write rows, with nothing in the test's own text hinting that it might.

    Tests that genuinely mean to talk to remote infrastructure carry the
    ``remote_integration`` marker and are excluded from the default run.
    """
    if request.node.get_closest_marker("remote_integration"):
        return
    for var in (
        "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE",
        "DATABASE_URL", "POSTGRES_HOST", "POSTGRES_DSN",
    ):
        monkeypatch.delenv(var, raising=False)
