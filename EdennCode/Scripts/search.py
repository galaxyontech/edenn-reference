"""
Search CLI: embed a query, run hybrid RRF SQL, print ranked results.

Usage:
  .venv/bin/python -m scripts.search "energetic music for a sports brand video"
  .venv/bin/python -m scripts.search "luxury car commercial" --top-k 5 --workflow video_music
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import numpy as np

from EdennCode.Scripts._common import (
    embed_batch,
    load_env,
    make_embedding_client,
    make_pool,
)

QUERY_PATH = Path(__file__).resolve().parents[1] / "Database" / "queries" / "hybrid_rrf.sql"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="natural-language query")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--workflow", default=None,
                        help="filter by workflow_type (e.g., video_music)")
    args = parser.parse_args()

    load_env()
    sql = QUERY_PATH.read_text()

    client = make_embedding_client()
    pool = await make_pool()
    try:
        emb_resp = await embed_batch(client, [args.query])
        if not emb_resp:
            print(
                f"ERROR: embedding API returned no data for query: {args.query!r}",
                file=sys.stderr,
            )
            return 1
        qvec = np.array(emb_resp[0], dtype=np.float32)

        rows = await pool.fetch(sql, qvec, args.query, args.workflow, args.top_k)
        if not rows:
            print("(no results)")
            return 0

        print(f"\nTop {len(rows)} results for: {args.query!r}\n")
        for i, r in enumerate(rows, 1):
            print(f"{i:>2}. score={r['score']:.4f}  request_id={r['request_id']}")
            print(f"    prompt:  {(r['user_prompt'] or '')[:100]}")
            print(f"    summary: {(r['video_summary'] or '')[:100]}")
            print(f"    modelspec={r['modelspec']}  provider={r['music_provider']}")
            print(f"    url={r['output_url']}")
            print()
        return 0
    finally:
        await pool.close()
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
