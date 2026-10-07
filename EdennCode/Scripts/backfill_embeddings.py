"""
One-shot embedding backfill: fill NULL embedding columns from the model gateway.

Usage:
  .venv/bin/python -m scripts.backfill_embeddings --table requests
  .venv/bin/python -m scripts.backfill_embeddings --table pipeline_runs
  .venv/bin/python -m scripts.backfill_embeddings --table all
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import numpy as np

from EdennCode.Scripts._common import (
    embed_batch,
    load_env,
    make_embedding_client,
    make_pool,
)

BATCH = 64


async def backfill_requests(pool, client) -> int:
    n_total = 0
    while True:
        rows = await pool.fetch(
            """
            SELECT request_id, user_prompt FROM requests
            WHERE user_prompt IS NOT NULL AND user_prompt_embedding IS NULL
            ORDER BY received_at LIMIT $1
            """,
            BATCH,
        )
        if not rows:
            break
        embeddings = await embed_batch(client, [r["user_prompt"] for r in rows])
        for r, emb in zip(rows, embeddings):
            await pool.execute(
                "UPDATE requests SET user_prompt_embedding = $1 WHERE request_id = $2",
                np.array(emb, dtype=np.float32), r["request_id"],
            )
        n_total += len(rows)
        print(f"  requests.user_prompt_embedding: filled {n_total}")
    return n_total


async def backfill_pipeline_runs(pool, client) -> int:
    """Fill both video_summary_embedding and music_prompt_embedding."""
    total_video = 0
    while True:
        rows = await pool.fetch(
            """
            SELECT run_id, video_summary FROM pipeline_runs
            WHERE video_summary IS NOT NULL AND video_summary_embedding IS NULL
            ORDER BY started_at LIMIT $1
            """,
            BATCH,
        )
        if not rows:
            break
        embeddings = await embed_batch(client, [r["video_summary"] for r in rows])
        for r, emb in zip(rows, embeddings):
            await pool.execute(
                "UPDATE pipeline_runs SET video_summary_embedding = $1 WHERE run_id = $2",
                np.array(emb, dtype=np.float32), r["run_id"],
            )
        total_video += len(rows)
        print(f"  pipeline_runs.video_summary_embedding: filled {total_video}")

    total_music = 0
    while True:
        rows = await pool.fetch(
            """
            SELECT run_id, music_prompt FROM pipeline_runs
            WHERE music_prompt IS NOT NULL AND music_prompt_embedding IS NULL
            ORDER BY started_at LIMIT $1
            """,
            BATCH,
        )
        if not rows:
            break
        # Use the global_music_prompt text key as the input
        texts = []
        for r in rows:
            mp = r["music_prompt"]
            if isinstance(mp, str):
                import json as _json
                mp = _json.loads(mp)
            texts.append(mp.get("global_music_prompt") or mp.get("style_prompt") or "")
        embeddings = await embed_batch(client, texts)
        for r, emb in zip(rows, embeddings):
            await pool.execute(
                "UPDATE pipeline_runs SET music_prompt_embedding = $1 WHERE run_id = $2",
                np.array(emb, dtype=np.float32), r["run_id"],
            )
        total_music += len(rows)
        print(f"  pipeline_runs.music_prompt_embedding: filled {total_music}")

    return total_video + total_music


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--table", choices=["requests", "pipeline_runs", "all"], required=True)
    args = parser.parse_args()

    load_env()
    pool = await make_pool()
    client = make_embedding_client()
    try:
        if args.table in ("requests", "all"):
            await backfill_requests(pool, client)
        if args.table in ("pipeline_runs", "all"):
            await backfill_pipeline_runs(pool, client)
        print("Done.")
        return 0
    finally:
        await pool.close()
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
