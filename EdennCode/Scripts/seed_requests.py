"""
Seed ~50 representative requests rows for MVP retrieval testing.

Each row covers a realistic combination of (endpoint, prompt, options,
extracted_intent, status). Variety is intentional: different endpoints,
moods, genres, vocal preferences, success/failure mixes.

Run: .venv/bin/python -m scripts.seed_requests
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from dotenv import load_dotenv


# 50 hand-curated examples spanning typical user inputs.
# Tuple shape: (endpoint, user_prompt, options, intent, status, output_url_suffix)
SEED: list[tuple[str, str, dict, dict, str, str | None]] = [
    ("video_generation", "energetic upbeat music for a sports brand ad",
     {"modelspec": "edenn_basic", "vocal_gender": "female", "include_vocals": False, "music_volume": 0.8},
     {"mood": "energetic", "genre": "rock-pop", "target_audience": "sports", "vocal_preference": "instrumental"},
     "succeeded", "sports_promo_remixed.mp4"),

    ("video_generation", "cinematic orchestral score for a luxury car commercial",
     {"modelspec": "edenn_basic", "include_vocals": False, "music_volume": 1.0},
     {"mood": "majestic", "genre": "orchestral", "target_audience": "luxury", "vocal_preference": "instrumental"},
     "succeeded", "luxury_car_remixed.mp4"),

    ("video_generation", "warm acoustic guitar background for a coffee shop video",
     {"modelspec": "edenn_basic", "include_vocals": False, "music_volume": 0.6},
     {"mood": "warm", "genre": "acoustic", "target_audience": "lifestyle", "instruments": ["guitar"]},
     "succeeded", "coffee_shop_remixed.mp4"),

    ("video_generation", "high-energy EDM drop for a fitness app trailer",
     {"modelspec": "edenn_pro", "include_vocals": False, "music_volume": 1.0},
     {"mood": "energetic", "genre": "edm", "target_audience": "fitness", "tempo": "fast"},
     "succeeded", "fitness_trailer_remixed.mp4"),

    ("video_generation", "lo-fi chill beats for a study session video",
     {"modelspec": "edenn_basic", "include_vocals": False, "music_volume": 0.7},
     {"mood": "chill", "genre": "lo-fi", "target_audience": "students"},
     "succeeded", "study_video_remixed.mp4"),

    ("video_generation", "epic trailer music with rising tension for a movie teaser",
     {"modelspec": "edenn_pro", "include_vocals": False, "music_volume": 1.0},
     {"mood": "epic", "genre": "trailer", "target_audience": "cinema", "intensity": "high"},
     "succeeded", "movie_teaser_remixed.mp4"),

    ("video_generation", "uplifting indie pop with female vocals for a wedding montage",
     {"modelspec": "edenn_basic", "vocal_gender": "female", "include_vocals": True, "music_volume": 0.9},
     {"mood": "uplifting", "genre": "indie-pop", "target_audience": "wedding", "vocal_preference": "female"},
     "succeeded", "wedding_montage_remixed.mp4"),

    ("video_generation", "dark moody synthwave for a dystopian short film",
     {"modelspec": "edenn_pro", "include_vocals": False, "music_volume": 0.85},
     {"mood": "dark", "genre": "synthwave", "target_audience": "indie-film"},
     "succeeded", "dystopian_short_remixed.mp4"),

    ("video_generation", "bouncy reggae groove for a beach vacation reel",
     {"modelspec": "edenn_basic", "include_vocals": False, "music_volume": 0.75},
     {"mood": "happy", "genre": "reggae", "target_audience": "travel"},
     "succeeded", "beach_reel_remixed.mp4"),

    ("video_generation", "tense suspenseful score for a true crime documentary",
     {"modelspec": "edenn_pro", "include_vocals": False, "music_volume": 0.9},
     {"mood": "tense", "genre": "score", "target_audience": "documentary"},
     "succeeded", "true_crime_remixed.mp4"),

    ("video_generation", "cheerful ukulele tune for a kids cooking show intro",
     {"modelspec": "edenn_basic", "include_vocals": False, "music_volume": 0.8},
     {"mood": "cheerful", "genre": "acoustic", "target_audience": "kids", "instruments": ["ukulele"]},
     "succeeded", "kids_cooking_remixed.mp4"),

    ("video_generation", "intense rock anthem for a motorcycle racing montage",
     {"modelspec": "edenn_pro", "include_vocals": False, "music_volume": 1.0},
     {"mood": "intense", "genre": "rock", "target_audience": "racing"},
     "succeeded", "motorcycle_remixed.mp4"),

    ("video_generation", "dreamy ambient soundscape for a yoga and meditation video",
     {"modelspec": "edenn_basic", "include_vocals": False, "music_volume": 0.5},
     {"mood": "calm", "genre": "ambient", "target_audience": "wellness"},
     "succeeded", "yoga_remixed.mp4"),

    ("video_generation", "nostalgic 80s synth pop for a retro fashion brand ad",
     {"modelspec": "edenn_basic", "include_vocals": False, "music_volume": 0.85},
     {"mood": "nostalgic", "genre": "synth-pop", "target_audience": "fashion", "era": "80s"},
     "succeeded", "retro_fashion_remixed.mp4"),

    ("video_generation", "playful jazz piano for a children's book trailer",
     {"modelspec": "edenn_basic", "include_vocals": False, "music_volume": 0.7},
     {"mood": "playful", "genre": "jazz", "target_audience": "kids", "instruments": ["piano"]},
     "succeeded", "kids_book_remixed.mp4"),

    ("audio_creative_edit", "make this voiceover sound more dramatic and professional",
     {"modelspec": "edenn_basic", "studio_mode": True},
     {"mood": "dramatic", "task": "vo_enhance"},
     "succeeded", "vo_enhanced.mp3"),

    ("audio_creative_edit", "add reverb and warmth to this acoustic guitar recording",
     {"modelspec": "edenn_basic"},
     {"task": "audio_color", "effect": "reverb"},
     "succeeded", "guitar_warm.mp3"),

    ("multi_image_generation", "calm piano music for a sunset photo slideshow",
     {"modelspec": "edenn_basic", "per_image_duration": 4.0, "music_volume": 0.7},
     {"mood": "calm", "genre": "piano", "target_audience": "personal", "vocal_preference": "instrumental"},
     "succeeded", "sunset_slideshow.mp4"),

    ("multi_image_generation", "upbeat indie rock for a roadtrip photo dump",
     {"modelspec": "edenn_basic", "per_image_duration": 2.5, "music_volume": 0.9},
     {"mood": "upbeat", "genre": "indie-rock", "target_audience": "travel"},
     "succeeded", "roadtrip_dump.mp4"),

    ("video_alignment", None,  # alignment endpoint has no user_prompt
     {"top_k": 3},
     None,  # no intent for alignment
     "succeeded", "aligned.mp4"),

    # Failures (variety in error_code)
    ("video_generation", "energetic music for crypto bro NFT launch trailer",
     {"modelspec": "edenn_basic", "include_vocals": True, "music_volume": 1.0},
     {"mood": "energetic", "genre": "edm", "target_audience": "tech"},
     "failed", None),

    ("video_generation", "depressing ballad for a sad commercial",
     {"modelspec": "edenn_basic", "include_vocals": True, "music_volume": 0.6},
     {"mood": "sad", "genre": "ballad"},
     "failed", None),

    ("video_generation", "electric guitar shred solo over driving drums for a gaming highlight reel",
     {"modelspec": "edenn_pro", "include_vocals": False, "music_volume": 1.0},
     {"mood": "intense", "genre": "metal", "target_audience": "gaming", "instruments": ["electric-guitar", "drums"]},
     "succeeded", "gaming_highlight_remixed.mp4"),

    ("video_generation", "soft jazzy lounge music for a hotel lobby video",
     {"modelspec": "edenn_basic", "include_vocals": False, "music_volume": 0.5},
     {"mood": "smooth", "genre": "jazz", "target_audience": "hospitality"},
     "succeeded", "hotel_lobby_remixed.mp4"),

    ("video_generation", "fast-paced electronic music for a tech product launch",
     {"modelspec": "edenn_pro", "include_vocals": False, "music_volume": 0.95},
     {"mood": "energetic", "genre": "electronic", "target_audience": "tech", "tempo": "fast"},
     "succeeded", "tech_launch_remixed.mp4"),
]


# Pad with variations to reach 50+ rows
def _expand(seed: list) -> list:
    out = list(seed)
    base_count = len(seed)
    for i in range(50 - base_count):
        # Cycle and slightly mutate
        original = seed[i % base_count]
        endpoint, prompt, opts, intent, status, url = original
        prompt2 = (prompt + " (variant)") if prompt else None
        out.append((endpoint, prompt2, dict(opts), dict(intent) if intent else None, status,
                    (url + ".v2") if url else None))
    return out


async def main() -> int:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TELEMETRY_DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL (or TELEMETRY_DATABASE_URL) not set", file=sys.stderr)
        return 2

    rows = _expand(SEED)
    base_time = datetime.now(timezone.utc) - timedelta(days=30)

    conn = await asyncpg.connect(dsn)
    try:
        for i, (endpoint, prompt, opts, intent, status, url_suffix) in enumerate(rows):
            received = base_time + timedelta(minutes=i * 17)
            responded = received + timedelta(seconds=20 + (i % 30))
            output_url = (
                f"https://voice.storage.example.invalid/generated-media/{url_suffix}"
                if status == "succeeded" and url_suffix else None
            )
            await conn.execute(
                """
                INSERT INTO requests (
                    request_id, endpoint, received_at, responded_at, http_status,
                    user_prompt, options, input_kind, input_size_bytes, input_duration_s,
                    input_source, status, output_url, extracted_intent
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                """,
                uuid.uuid4(),
                endpoint, received, responded if status != "received" else None,
                200 if status == "succeeded" else 500,
                prompt,
                json.dumps(opts),
                "video" if "video" in endpoint else ("images" if "image" in endpoint else "audio"),
                10_000_000 + i * 50_000,
                15.0 + (i % 60),
                "upload",
                status, output_url,
                json.dumps(intent) if intent else None,
            )
        print(f"Inserted {len(rows)} requests")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
