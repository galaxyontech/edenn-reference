"""
Seed pipeline_runs and pipeline_stages rows for MVP retrieval testing.

For each succeeded `requests` row from Track A's seed, create:
  - one pipeline_runs row with realistic video_summary, music_prompt, music_provider
  - 4-6 pipeline_stages rows mirroring the actual workflow (preprocess, scene_segmentation,
    video_understanding, music_prompt_orchestration, music_generation, video_audio_remix)

Run AFTER scripts.seed_requests:
    .venv/bin/python -m scripts.seed_requests
    .venv/bin/python -m scripts.seed_pipeline_runs
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import uuid
from datetime import timedelta

import asyncpg
from dotenv import load_dotenv


# Six stages per video_music run, with realistic provider/model/token shapes.
VIDEO_MUSIC_STAGES = [
    {"name": "user_prompt_preprocessor",   "provider": "model_gateway", "model": "chat-advanced", "input": 180,  "output": 95,  "duration_ms": 850},
    {"name": "scene_segmentation",         "provider": "model_gateway", "model": "chat-advanced", "input": 2100, "output": 480, "duration_ms": 4200},
    {"name": "video_understanding",        "provider": "model_gateway", "model": "chat-advanced", "input": 1750, "output": 320, "duration_ms": 3100},
    {"name": "music_prompt_orchestration", "provider": "model_gateway", "model": "chat-advanced", "input": 790,  "output": 261, "duration_ms": 1600},
    {"name": "music_generation",           "provider": "provider_a",   "model": "eleven_music_v1", "input": None, "output": None, "duration_ms": 17800},
    {"name": "video_audio_remix",          "provider": None,           "model": None,    "input": None, "output": None, "duration_ms": 1450},
]


def video_summary_for(prompt: str | None) -> str:
    """Synthesize a plausible video_summary from a user_prompt."""
    if not prompt:
        return "A short video without an associated user prompt."
    seed_words = (prompt or "").lower()
    if "sports" in seed_words:
        return "A 30-second product showcase of athletic apparel, with quick cuts of athletes running, jumping, and stretching outdoors."
    if "luxury" in seed_words or "car" in seed_words:
        return "A 45-second cinematic spot featuring a luxury sedan driving through mountain roads at sunset."
    if "coffee" in seed_words:
        return "A 25-second video showing a barista preparing pour-over coffee in a sunlit cafe."
    if "fitness" in seed_words:
        return "A 30-second high-energy montage of people working out in a modern gym."
    if "study" in seed_words or "lo-fi" in seed_words:
        return "A 20-second video of a student studying at a desk with bookshelves and a city window view."
    if "trailer" in seed_words or "movie" in seed_words:
        return "A 60-second trailer with rapid cuts, dramatic lighting, and rising tension."
    if "wedding" in seed_words:
        return "A 90-second montage of a wedding ceremony and reception, with smiling faces and hand-held shots."
    if "synthwave" in seed_words or "dystopian" in seed_words:
        return "A 40-second video with neon-lit urban scenes, slow pans, and rain-soaked streets."
    if "beach" in seed_words or "reggae" in seed_words:
        return "A 30-second beach reel with palm trees, ocean waves, and people walking on the sand."
    if "yoga" in seed_words or "meditation" in seed_words:
        return "A 60-second video of a yoga class in a softly-lit studio with slow camera movement."
    return f"A 30-second video matching the brief: {prompt[:120]}"


def music_prompt_for(intent: dict | None, prompt: str) -> dict:
    intent = intent or {}
    return {
        "global_music_prompt": f"{intent.get('mood', 'upbeat')} {intent.get('genre', 'pop')} track for {intent.get('target_audience', 'general')} content",
        "style_prompt": intent.get("genre", "pop"),
        "instruments": intent.get("instruments", ["synth", "drums"]),
        "tempo_bpm": 100 + (hash(prompt) % 60),
        "key": random.choice(["C major", "D minor", "G major", "A minor"]),
    }


async def main() -> int:
    load_dotenv()
    random.seed(42)
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TELEMETRY_DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL (or TELEMETRY_DATABASE_URL) not set", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        # Pull all succeeded requests
        requests = await conn.fetch(
            """
            SELECT request_id, endpoint, user_prompt, options, extracted_intent,
                   received_at, responded_at
            FROM requests
            WHERE status = 'succeeded'
            ORDER BY received_at
            """
        )
        if not requests:
            print("No succeeded requests found. Run scripts.seed_requests first.")
            return 1

        n_runs = 0
        n_stages = 0
        for r in requests:
            workflow_type = (
                "video_music" if r["endpoint"] == "video_generation"
                else "audio_creative_edit" if r["endpoint"] == "audio_creative_edit"
                else "multi_image" if r["endpoint"] == "multi_image_generation"
                else "video_alignment"
            )
            run_id = uuid.uuid4()
            started_at = r["received_at"] + timedelta(seconds=1)
            opts = json.loads(r["options"]) if isinstance(r["options"], str) else r["options"]
            modelspec = opts.get("modelspec") if isinstance(opts, dict) else None

            intent = json.loads(r["extracted_intent"]) if isinstance(r["extracted_intent"], str) else r["extracted_intent"]
            prompt = r["user_prompt"] or ""

            stages = VIDEO_MUSIC_STAGES if workflow_type == "video_music" else VIDEO_MUSIC_STAGES[:4]
            run_duration = sum(s["duration_ms"] for s in stages) + 500
            finished_at = started_at + timedelta(milliseconds=run_duration)

            # video_summary / music_prompt only for video_music + multi_image flavors
            video_summary = video_summary_for(prompt) if workflow_type in ("video_music", "multi_image") else None
            music_prompt = music_prompt_for(intent, prompt) if workflow_type in ("video_music", "multi_image") else None
            music_provider = random.choice(["provider_a", "provider_c"]) if music_prompt else None

            total_in = sum(s["input"] or 0 for s in stages)
            total_out = sum(s["output"] or 0 for s in stages)

            await conn.execute(
                """
                INSERT INTO pipeline_runs (
                    run_id, request_id, workflow_type, workflow_version, modelspec,
                    started_at, finished_at, status,
                    total_input_tokens, total_output_tokens,
                    video_summary, music_prompt, music_provider
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                """,
                run_id, r["request_id"], workflow_type, "b4d59bc", modelspec,
                started_at, finished_at, "succeeded",
                total_in, total_out,
                video_summary,
                json.dumps(music_prompt) if music_prompt else None,
                music_provider,
            )
            n_runs += 1

            # Stages
            cursor = started_at
            for idx, s in enumerate(stages):
                s_started = cursor
                s_finished = cursor + timedelta(milliseconds=s["duration_ms"])
                cursor = s_finished
                stage_output = (
                    {"summary": video_summary, "scenes": 4}
                    if s["name"] in ("scene_segmentation", "video_understanding") and video_summary
                    else None
                )
                await conn.execute(
                    """
                    INSERT INTO pipeline_stages (
                        run_id, stage_name, stage_index, started_at, finished_at, status,
                        provider, model, input_tokens, output_tokens, output
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    """,
                    run_id, s["name"], idx, s_started, s_finished, "succeeded",
                    s["provider"], s["model"], s["input"], s["output"],
                    json.dumps(stage_output) if stage_output else None,
                )
                n_stages += 1

        print(f"Inserted {n_runs} runs, {n_stages} stages")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
