from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Tuple

from EdennCode.env import load_env
from EdennCode.Util.MediaUtils import (
    build_sfx_timeline_wav,
    get_video_duration,
    overlay_music_on_video,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.SoundEffectGenerationStage.sound_effect_generation_stage import (
    SoundEffectGenerationStage,
    SoundEffectGenerationStageInput,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    SoundFXEvent,
)


VIDEO_PATH = Path("/Users/example/Downloads/bike_enhanced.mp4")
# Keep outputs deterministic regardless of working directory.
RUN_DIR = Path(__file__).resolve().parent / "outputs" / "quick_sfx_remix"
SFX_DIR = RUN_DIR / "sfx_clips"
TIMELINE_WAV = RUN_DIR / "sfx_timeline.wav"
FINAL_VIDEO_MIXED = RUN_DIR / "edenn_enhanced_with_sfx_mix.mp4"
FINAL_VIDEO_SFX_ONLY = RUN_DIR / "edenn_enhanced_sfx_only.mp4"
SAMPLE_RATE = 44_100
# Boost SFX level so they are audible over existing soundtrack.
SFX_GAIN_DB = 9.0
MIX_SFX_VOLUME = 3.0

# start_mm_ss, end_mm_ss, description
EVENT_SPECS: List[Tuple[str, str, str]] = [
    (
        "00:00",
        "00:02",
        'Cinematic whoosh: a fast, airy "whoosh" to match the 360-degree camera rotation at the start.',
    ),
    (
        "00:02",
        "00:06",
        "Bicycle chain rattle: rhythmic chain and pedaling sounds as the rider moves through the trees.",
    ),
    (
        "00:07",
        "00:09",
        "Mechanical gear click: a sharp metallic click to emphasize the close-up of gears shifting.",
    ),
    (
        "00:10",
        "00:14",
        "Dry leaves crunching: crisp crackling tire sounds over a thick layer of dry leaves on trail.",
    ),
    (
        "00:15",
        "00:17",
        "Fast wind swish: dynamic whoosh/swish effect for camera spin.",
    ),
    (
        "00:18",
        "00:22",
        "Heavy breathing and wind: subtle rider breathing with wind whistling past to convey speed and effort.",
    ),
    (
        "00:23",
        "00:25",
        "Tire skid and dirt spray: gritty sliding tire sound as tires grip dirt during turn or descent.",
    ),
    (
        "00:26",
        "00:28",
        "Forest ambience: fading bike sounds blended with birds and wind in trees at ending.",
    ),
]


def mm_ss_to_seconds(value: str) -> float:
    minutes, seconds = value.split(":")
    return int(minutes) * 60 + int(seconds)


def build_events() -> List[SoundFXEvent]:
    events: List[SoundFXEvent] = []
    for idx, (start_mm_ss, end_mm_ss, description) in enumerate(EVENT_SPECS, start=1):
        events.append(
            SoundFXEvent(
                event_id=f"event_{idx:02d}",
                start_time=mm_ss_to_seconds(start_mm_ss),
                end_time=mm_ss_to_seconds(end_mm_ss),
                event_description=description,
                sound_event_local_path="",
                confidence=1.0,
            )
        )
    return events


async def main() -> None:
    load_env()
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    stage = SoundEffectGenerationStage()
    events = build_events()

    generation_output = await stage.run(
        SoundEffectGenerationStageInput(
            list_of_generation_packages=events,
            output_directory=SFX_DIR,
            sample_rate=SAMPLE_RATE,
        )
    )

    timeline_events = [
        (Path(event.sound_event_local_path), float(event.start_time), SFX_GAIN_DB)
        for event in generation_output.generated_sound_events
    ]

    duration = get_video_duration(VIDEO_PATH)
    build_sfx_timeline_wav(
        events=timeline_events,
        output_path=TIMELINE_WAV,
        total_duration_s=duration,
        sample_rate=SAMPLE_RATE,
    )

    overlay_music_on_video(
        video_path=VIDEO_PATH,
        music_path=TIMELINE_WAV,
        output_path=FINAL_VIDEO_MIXED,
        preserve_original_audio=True,
        music_volume=MIX_SFX_VOLUME,
    )

    # Debug artifact: replace original track with SFX timeline only.
    overlay_music_on_video(
        video_path=VIDEO_PATH,
        music_path=TIMELINE_WAV,
        output_path=FINAL_VIDEO_SFX_ONLY,
        preserve_original_audio=False,
        music_volume=1.0,
    )

    print(f"SFX clips dir: {SFX_DIR}")
    print(f"Timeline WAV: {TIMELINE_WAV}")
    print(f"Final remixed video (boosted mix): {FINAL_VIDEO_MIXED}")
    print(f"Final remixed video (SFX only): {FINAL_VIDEO_SFX_ONLY}")


if __name__ == "__main__":
    asyncio.run(main())
