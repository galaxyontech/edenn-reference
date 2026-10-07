"""
One-shot embedding backfill for the recommendation read path.

Fills NULL embedding columns from the model gateway (embed-standard, 1536-dim)
on the canonical recommendation tables:
  - generation_job.user_prompt_embedding   ← embed(user_prompt)
  - creative_feature_snapshot.music_embedding
                                           ← embed(music_prompt_json::text)

Idempotent: running again with everything filled is a no-op (exits 0, prints
"Done.").

Usage:
  .venv/bin/python -m scripts.backfill_recommendation_embeddings
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import numpy as np

from EdennCode.Scripts._common import (
    embed_batch,
    load_env,
    make_embedding_client,
    make_pool,
)

BATCH = 64


async def backfill_generation_job_user_prompt(pool, client) -> int:
    n_total = 0
    while True:
        rows = await pool.fetch(
            """
            SELECT job_id, user_prompt FROM generation_job
            WHERE user_prompt IS NOT NULL
              AND user_prompt <> ''
              AND user_prompt_embedding IS NULL
            ORDER BY created_at LIMIT $1
            """,
            BATCH,
        )
        if not rows:
            break
        embeddings = await embed_batch(client, [r["user_prompt"] for r in rows])
        for r, emb in zip(rows, embeddings):
            await pool.execute(
                "UPDATE generation_job SET user_prompt_embedding = $1 WHERE job_id = $2",
                np.array(emb, dtype=np.float32), r["job_id"],
            )
        n_total += len(rows)
        print(f"  generation_job.user_prompt_embedding: filled {n_total}")
    return n_total


async def backfill_snapshot_music_embedding(pool, client) -> int:
    n_total = 0
    while True:
        rows = await pool.fetch(
            """
            SELECT creative_id, music_prompt_json FROM creative_feature_snapshot
            WHERE music_prompt_json IS NOT NULL
              AND music_prompt_json::text <> '{}'::text
              AND music_embedding IS NULL
            ORDER BY refreshed_at LIMIT $1
            """,
            BATCH,
        )
        if not rows:
            break
        # asyncpg returns JSONB as a Python str; normalize to a JSON string for embedding.
        texts: list[str] = []
        for r in rows:
            mp = r["music_prompt_json"]
            if isinstance(mp, (dict, list)):
                texts.append(json.dumps(mp, ensure_ascii=False, sort_keys=True))
            else:
                texts.append(str(mp))
        embeddings = await embed_batch(client, texts)
        for r, emb in zip(rows, embeddings):
            await pool.execute(
                "UPDATE creative_feature_snapshot SET music_embedding = $1 WHERE creative_id = $2",
                np.array(emb, dtype=np.float32), r["creative_id"],
            )
        n_total += len(rows)
        print(f"  creative_feature_snapshot.music_embedding: filled {n_total}")
    return n_total


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    load_env()
    pool = await make_pool()
    client = make_embedding_client()
    try:
        await backfill_generation_job_user_prompt(pool, client)
        await backfill_snapshot_music_embedding(pool, client)
        print("Done.")
        return 0
    finally:
        await pool.close()
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
