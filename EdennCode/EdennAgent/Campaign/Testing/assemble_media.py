#!/usr/bin/env python3
"""Assemble the campaign devserver's fixture media dir (gitignored).

Copies real clips already present in the repo into Testing/media under the
campaign-fixture names the devserver's seeder expects, and writes small
hand-authored *.observation.json caches so the understanding pass has L2
semantics with zero model spend.

Run once from the repo root:  python3 EdennCode/EdennAgent/Campaign/Testing/assemble_media.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEDIA = HERE / "media"
REPO = HERE.parents[4]

# fixture name -> (source path in repo, observation payload or None)
FIXTURES: dict[str, tuple[str, dict | None]] = {
    "launch_footage.mp4": (
        "edenn_previews/rank01_t0_142.176_score_8.447.mp4",
        {
            "scenes": [
                {"start_s": 0.0, "end_s": 9.0, "summary": "opening product moment", "mood": "bright"},
                {"start_s": 9.0, "end_s": 24.0, "summary": "hands and detail close-ups", "mood": "warm"},
                {"start_s": 24.0, "end_s": 42.0, "summary": "movement and energy build", "mood": "upbeat"},
                {"start_s": 42.0, "end_s": 60.0, "summary": "calm closing image", "mood": "settled"},
            ],
            "video_summary": {"overall_mood": "bright, premium launch energy"},
            "music_prompt": {"global_mood": "uptempo, confident"},
        },
    ),
    "event_reel.mp4": (
        "slideshow.mp4",
        {
            "scenes": [
                {"start_s": 0.0, "end_s": 8.0, "summary": "gathering wide shot", "mood": "warm"},
                {"start_s": 8.0, "end_s": 16.0, "summary": "speaking moment to camera", "mood": "sincere"},
                {"start_s": 16.0, "end_s": 24.5, "summary": "toast and close", "mood": "celebratory"},
            ],
            "video_summary": {"overall_mood": "warm, human, spoken"},
            "music_prompt": {"global_mood": "gentle, supportive"},
        },
    ),
    "broll_reel.mp4": (
        "edenn_previews/rank02_t0_88.243_score_15.485.mp4",
        {
            "scenes": [
                {"start_s": float(a), "end_s": float(b), "summary": f"b-roll passage {i+1}", "mood": m}
                for i, (a, b, m) in enumerate(
                    [(0, 3.2, "bright"), (3.2, 6.4, "warm"), (6.4, 9.6, "upbeat"), (9.6, 12.5, "calm")]
                )
            ],
            "video_summary": {"overall_mood": "versatile b-roll"},
            "music_prompt": {"global_mood": "adaptable"},
        },
    ),
    "spring_track.wav": ("ace_step_test.wav", None),
    "IMG_2214.mp4": ("Workflow_Outputs_Local/multi_image_story.mp4", None),
}


def main() -> int:
    MEDIA.mkdir(parents=True, exist_ok=True)
    missing = []
    for name, (src_rel, obs) in FIXTURES.items():
        src = REPO / src_rel
        if not src.exists():
            missing.append(src_rel)
            continue
        dest = MEDIA / name
        if not dest.exists():
            shutil.copy2(src, dest)
        if obs is not None:
            (MEDIA / name).with_suffix(".observation.json").write_text(json.dumps(obs, indent=2))
        print(f"ok: {name} <- {src_rel}")
    if missing:
        print(f"MISSING sources: {missing}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
