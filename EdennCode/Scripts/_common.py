"""Shared utilities for MVP scripts: pool, embedding client, dotenv."""
from __future__ import annotations

import os

import asyncpg
from dotenv import load_dotenv
from EdennCode.ModelFactory.LanguageModelFactory.gateway_clients import (
    AsyncGatewayClient,
    embedding_deployment,
    make_async_gateway_client,
)
from pgvector.asyncpg import register_vector


def load_env() -> None:
    load_dotenv()


def get_database_url() -> str | None:
    """Resolve the Postgres DSN.

    Prefers ``DATABASE_URL`` (project convention; libpq-style). Falls back to
    ``TELEMETRY_DATABASE_URL`` for backwards compatibility with earlier MVP
    env files. Returns ``None`` if neither is set so callers can decide
    between hard error and graceful skip.
    """
    return os.environ.get("DATABASE_URL") or os.environ.get("TELEMETRY_DATABASE_URL")


async def make_pool(min_size: int = 2, max_size: int = 5) -> asyncpg.Pool:
    """Create an asyncpg pool with pgvector type registered on every connection."""
    dsn = get_database_url()
    if not dsn:
        raise RuntimeError(
            "Set DATABASE_URL (preferred) or TELEMETRY_DATABASE_URL in the "
            "environment or .env to point at the telemetry Postgres."
        )

    async def _init(conn):
        await register_vector(conn)

    return await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size, init=_init)


def make_embedding_client() -> AsyncGatewayClient:
    return make_async_gateway_client()


async def embed_batch(client: AsyncGatewayClient, texts: list[str]) -> list[list[float]]:
    """Embed up to ~2048 texts in one call (the model gateway limit varies by model)."""
    if not texts:
        return []
    resp = await client.embeddings.create(input=texts, model=embedding_deployment())
    return [d.embedding for d in resp.data]
