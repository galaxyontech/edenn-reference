"""Stage 2: score candidates from creative_feature_snapshot.

Two modes:

  user-RAG mode (profile is set):
      Filter: language matches user's preferred language, normalized via
              LOWER(SPLIT_PART(language, '_', 1)) so ENGLISH_US/English/ENGLISH
              all collapse to 'english'.
      Score:  0.6 * alignment_score + 0.4 * (1 - cosine_distance)
              where cosine_distance is between user's prompt_centroid and
              creative_feature_snapshot.music_embedding.

  global mode (profile is None):
      No language filter. Score = alignment_score. Order DESC.

Snapshots without music_embedding are excluded from user-RAG mode (cosine
undefined). They're still eligible in global mode.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Optional

import asyncpg
import numpy as np
from pgvector.asyncpg import register_vector

from EdennCode.WorkflowFactory.RecommendationWorkflow.build_user_profile_stage import (
    UserProfile,
)

# Score weights for user-RAG mode.
ALIGNMENT_WEIGHT: float = 0.6
COSINE_WEIGHT: float = 0.4


@dataclass(frozen=True)
class Candidate:
    """One scored snapshot row, ordered by descending final_score."""

    creative_id: str
    final_score: float
    alignment_score: float
    cosine_similarity: Optional[float]


class RetrieveCandidatesStage:
    """Score candidates from creative_feature_snapshot for one user or globally."""

    def __init__(self, *, dsn: str) -> None:
        self._dsn = dsn

    async def run(
        self,
        *,
        profile: Optional[UserProfile],
        limit: int,
    ) -> list[Candidate]:
        if limit <= 0:
            return []

        conn = await asyncpg.connect(self._dsn)
        try:
            await register_vector(conn)
            if profile is None or profile.prompt_centroid is None:
                rows = await conn.fetch(
                    """
                    SELECT creative_id,
                           alignment_score,
                           alignment_score AS final_score
                    FROM   creative_feature_snapshot
                    ORDER  BY alignment_score DESC, refreshed_at DESC
                    LIMIT  $1
                    """,
                    limit,
                )
                return [
                    Candidate(
                        creative_id=r["creative_id"],
                        final_score=float(r["final_score"]),
                        alignment_score=float(r["alignment_score"]),
                        cosine_similarity=None,
                    )
                    for r in rows
                ]

            centroid = np.array(profile.prompt_centroid, dtype=np.float32)
            rows = await conn.fetch(
                f"""
                SELECT creative_id,
                       alignment_score,
                       1 - (music_embedding <=> $1::vector) AS cosine_similarity,
                       {ALIGNMENT_WEIGHT} * alignment_score
                       + {COSINE_WEIGHT} * (1 - (music_embedding <=> $1::vector))
                       AS final_score
                FROM   creative_feature_snapshot
                WHERE  music_embedding IS NOT NULL
                  AND  LOWER(SPLIT_PART(language, '_', 1))
                       = LOWER(SPLIT_PART($2, '_', 1))
                ORDER  BY final_score DESC
                LIMIT  $3
                """,
                centroid,
                profile.preferred_language or "",
                limit,
            )
        finally:
            await conn.close()

        return [
            Candidate(
                creative_id=r["creative_id"],
                final_score=float(r["final_score"]),
                alignment_score=float(r["alignment_score"]),
                cosine_similarity=float(r["cosine_similarity"]),
            )
            for r in rows
        ]


def _candidates_to_json(candidates: list[Candidate]) -> list[dict]:
    return [
        {
            "creative_id": c.creative_id,
            "final_score": c.final_score,
            "alignment_score": c.alignment_score,
            "cosine_similarity": c.cosine_similarity,
        }
        for c in candidates
    ]


async def _amain(argv: list[str]) -> int:
    import os
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-json",
                        help="UserProfile JSON from build_user_profile_stage --verbose. "
                             "Omit for global mode.")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    load_dotenv()
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TELEMETRY_DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    profile: Optional[UserProfile] = None
    if args.profile_json:
        raw = json.loads(args.profile_json)
        profile = UserProfile(
            user_id=raw["user_id"],
            history_count=int(raw["history_count"]),
            preferred_language=raw.get("preferred_language", ""),
            preferred_include_vocals=raw.get("preferred_include_vocals"),
            prompt_centroid=raw.get("prompt_centroid"),
        )

    stage = RetrieveCandidatesStage(dsn=dsn)
    candidates = await stage.run(profile=profile, limit=args.limit)
    print(json.dumps(_candidates_to_json(candidates), ensure_ascii=False))
    return 0


def main() -> int:
    return asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
