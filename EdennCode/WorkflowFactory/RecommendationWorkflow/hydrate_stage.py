"""Stage 3: hydrate scored candidates into the response payload.

Joins creative + music_asset + creative_feature_snapshot for each candidate
creative_id (preserving the input order) and emits either the slim or the
debug shape.

Slim:  {music_id, full_audio_url, creative_id, title, thumbnail_url, score}
Debug: + description, result_video_url, lyrics_text, alignment_score,
         cosine_similarity, music_prompt_json, created_at
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

import asyncpg

from EdennCode.WorkflowFactory.RecommendationWorkflow.retrieve_candidates_stage import (
    Candidate,
)


@dataclass(frozen=True)
class RecommendationItem:
    """One result row in the recommendation response, slim or debug."""

    payload: dict[str, Any]


class HydrateStage:
    """Join candidate creative_ids with creative + music_asset + snapshot."""

    def __init__(self, *, dsn: str) -> None:
        self._dsn = dsn

    async def run(
        self,
        *,
        candidates: list[Candidate],
        debug: bool = False,
    ) -> list[RecommendationItem]:
        if not candidates:
            return []

        creative_ids = [c.creative_id for c in candidates]
        conn = await asyncpg.connect(self._dsn)
        try:
            rows = await conn.fetch(
                """
                SELECT c.creative_id,
                       c.title,
                       c.description,
                       c.thumbnail_url,
                       c.result_video_url,
                       c.created_at,
                       c.selected_music_id      AS music_id,
                       m.full_audio_url,
                       m.matched_audio_url,
                       m.lyrics_text,
                       s.alignment_score,
                       s.music_prompt_json
                FROM   creative c
                LEFT  JOIN music_asset m ON m.music_id = c.selected_music_id
                LEFT  JOIN creative_feature_snapshot s ON s.creative_id = c.creative_id
                WHERE  c.creative_id = ANY($1::text[])
                """,
                creative_ids,
            )
        finally:
            await conn.close()

        by_id = {r["creative_id"]: r for r in rows}

        items: list[RecommendationItem] = []
        for cand in candidates:
            row = by_id.get(cand.creative_id)
            if row is None:
                # Candidate referenced a creative_id that doesn't exist anymore;
                # skip rather than emit a half-populated row. (Shouldn't happen
                # with FKs respected, but defensive — snapshot rows can outlive
                # creative rows during deletes.)
                continue
            slim: dict[str, Any] = {
                "creative_id": row["creative_id"],
                "music_id": row["music_id"],
                "full_audio_url": row["full_audio_url"] or row["matched_audio_url"],
                "title": row["title"],
                "thumbnail_url": row["thumbnail_url"],
                "score": cand.final_score,
            }
            if debug:
                slim.update(
                    description=row["description"],
                    result_video_url=row["result_video_url"],
                    lyrics_text=row["lyrics_text"],
                    alignment_score=cand.alignment_score,
                    cosine_similarity=cand.cosine_similarity,
                    music_prompt_json=row["music_prompt_json"],
                    created_at=row["created_at"].isoformat() if row["created_at"] else None,
                )
            items.append(RecommendationItem(payload=slim))
        return items


async def _amain(argv: list[str]) -> int:
    import os
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates-json", required=True,
                        help="JSON array from retrieve_candidates_stage.")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv()
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TELEMETRY_DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    raw = json.loads(args.candidates_json)
    candidates = [
        Candidate(
            creative_id=r["creative_id"],
            final_score=float(r["final_score"]),
            alignment_score=float(r["alignment_score"]),
            cosine_similarity=(
                float(r["cosine_similarity"]) if r.get("cosine_similarity") is not None else None
            ),
        )
        for r in raw
    ]

    stage = HydrateStage(dsn=dsn)
    items = await stage.run(candidates=candidates, debug=args.debug)
    print(json.dumps([item.payload for item in items], ensure_ascii=False, default=str))
    return 0


def main() -> int:
    return asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
