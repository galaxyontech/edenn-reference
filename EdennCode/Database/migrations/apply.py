"""
Migration runner. Discovers `*.sql` files in this directory, applies them
in lexical order, and records each in the `schema_migrations` table.
Idempotent: skips migrations whose version is already recorded.

Run from repo root:
    .venv/bin/python -m EdennCode.Database.migrations.apply
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

MIGRATIONS_DIR = Path(__file__).parent


async def main() -> int:
    load_dotenv()
    # DATABASE_URL is the project convention; TELEMETRY_DATABASE_URL is a
    # backwards-compat fallback for env files written during the early MVP setup.
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TELEMETRY_DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL (or TELEMETRY_DATABASE_URL) not set", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        applied = {
            r["version"]
            for r in await conn.fetch("SELECT version FROM schema_migrations")
        }

        files = sorted(MIGRATIONS_DIR.glob("[0-9]*.sql"))
        if not files:
            print("No migration files found.")
            return 0

        for path in files:
            version = path.stem
            if version in applied:
                print(f"SKIP {version}")
                continue
            print(f"APPLY {version} ...")
            sql = path.read_text()
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)", version
                )
            print(f"  OK {version}")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
