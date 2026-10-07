"""Stage 1: build a user-history profile from DB A.

Reads `generation_job` rows for the given `creator_user_id` and returns a
profile with the user's preferred language, vocal preference, prompt centroid
(mean of user_prompt_embedding vectors), and history count.

The centroid is the query vector the recommender ranks candidates against.
Returns history_count=0 when the user has no embedded prompts; the workflow
short-circuits to an empty result in that case (spec: cold start = empty 200).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import asyncpg
import numpy as np
from pgvector.asyncpg import register_vector


@dataclass(frozen=True)
class UserProfile:
    """Aggregated history features for one user_id."""

    user_id: str
    history_count: int
    preferred_language: str = ""
    preferred_include_vocals: Optional[bool] = None
    prompt_centroid: Optional[list[float]] = field(default=None, repr=False)


class BuildUserProfileStage:
    """Resolve a user_id into a profile by reading their generation_job rows."""

    def __init__(self, *, dsn: str) -> None:
        self._dsn = dsn

    async def run(self, *, user_id: str) -> UserProfile:
        conn = await asyncpg.connect(self._dsn)
        try:
            await register_vector(conn)
            rows = await conn.fetch(
                """
                SELECT user_requested_language,
                       include_vocals,
                       user_prompt_embedding
                FROM   generation_job
                WHERE  creator_user_id = $1
                  AND  user_prompt_embedding IS NOT NULL
                """,
                user_id,
            )
        finally:
            await conn.close()

        if not rows:
            return UserProfile(user_id=user_id, history_count=0)

        languages = [r["user_requested_language"] for r in rows if r["user_requested_language"]]
        vocals = [bool(r["include_vocals"]) for r in rows]
        embeddings = np.array(
            [list(r["user_prompt_embedding"]) for r in rows], dtype=np.float32
        )
        centroid = embeddings.mean(axis=0).tolist()

        preferred_language = Counter(languages).most_common(1)[0][0] if languages else ""
        preferred_vocals = Counter(vocals).most_common(1)[0][0] if vocals else None

        return UserProfile(
            user_id=user_id,
            history_count=len(rows),
            preferred_language=preferred_language,
            preferred_include_vocals=preferred_vocals,
            prompt_centroid=centroid,
        )


def _profile_to_json(profile: UserProfile) -> dict:
    return {
        "user_id": profile.user_id,
        "history_count": profile.history_count,
        "preferred_language": profile.preferred_language,
        "preferred_include_vocals": profile.preferred_include_vocals,
        "prompt_centroid_dim": (
            len(profile.prompt_centroid) if profile.prompt_centroid is not None else 0
        ),
        # The full centroid is only emitted in --verbose to keep stdout small
        # for the common Track-2 pipe-to-next-stage case.
    }


async def _amain(argv: list[str]) -> int:
    import os
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--verbose", action="store_true",
                        help="Include full prompt_centroid vector in stdout JSON.")
    args = parser.parse_args(argv)

    load_dotenv()
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TELEMETRY_DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    stage = BuildUserProfileStage(dsn=dsn)
    profile = await stage.run(user_id=args.user_id)
    payload = _profile_to_json(profile)
    if args.verbose and profile.prompt_centroid is not None:
        payload["prompt_centroid"] = profile.prompt_centroid
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def main() -> int:
    return asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
