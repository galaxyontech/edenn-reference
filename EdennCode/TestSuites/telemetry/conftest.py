import os

import asyncpg
import pytest
import pytest_asyncio
from dotenv import load_dotenv
from pgvector.asyncpg import register_vector


# Load the worktree-local .env into os.environ at collection time so individual
# test modules (and any subprocess they spawn via `env={**os.environ}`) can find
# DATABASE_URL / TELEMETRY_DATABASE_URL / MODEL_GATEWAY_*. Without this, running
# `pytest` directly without a pre-exported shell env causes opaque KeyError
# failures inside tests rather than the clean skip the fixture provides.
load_dotenv()


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))


def pytest_collection_modifyitems(config, items):
    """Auto-mark every test in THIS directory as remote_integration.

    These tests require a live Japan East Postgres (with pgvector + seeded data)
    and a working the model gateway embed-standard deployment. They are
    excluded from the CI `local-core` job (which runs `-m "not remote_integration"`)
    and run via the integration jobs that have the necessary secrets + network
    access. Adding the marker here lets every test file stay free of boilerplate.

    pytest invokes pytest_collection_modifyitems from EVERY conftest.py with
    the full session item list, so we must filter by path — otherwise this
    hook would mark unrelated tests across the repo.
    """
    for item in items:
        item_path = os.path.abspath(str(item.fspath))
        if item_path.startswith(_THIS_DIR + os.sep):
            item.add_marker(pytest.mark.remote_integration)


@pytest_asyncio.fixture
async def db_conn():
    """Per-test connection with vector type registered. Migrations must already have run.

    Resolves the DSN from DATABASE_URL (preferred) or TELEMETRY_DATABASE_URL
    (back-compat). If neither is set, cleanly skips so a developer running
    `pytest EdennCode/TestSuites/telemetry/...` without env doesn't get a confusing KeyError.
    """
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TELEMETRY_DATABASE_URL")
    if not dsn:
        pytest.skip(
            "DATABASE_URL (or TELEMETRY_DATABASE_URL) not set; "
            "telemetry tests require a live Postgres connection"
        )
    conn = await asyncpg.connect(dsn)
    try:
        await register_vector(conn)
        yield conn
    finally:
        await conn.close()
