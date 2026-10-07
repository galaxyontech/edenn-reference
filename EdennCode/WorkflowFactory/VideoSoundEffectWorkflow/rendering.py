"""
Shared render path for video→SFX projects.

Both the one-shot workflow and the iteration editor call `render_project`, so
an edited project always sounds exactly like a fresh run with the same state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from EdennCode.Util.MediaUtils import overlay_music_on_video
from EdennCode.Util.MediaUtils.sfx_timeline import SfxTimelineClip, render_sfx_timeline_wav
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    SfxProject,
)

logger = logging.getLogger(__name__)

# Short protective fades keep generated clips click-free without softening
# transients audibly.
EVENT_FADE_IN_S = 0.005
EVENT_FADE_OUT_S = 0.08
AMBIENCE_FADE_S = 0.75
# Let one-shot foley tails ring past the event window, but not forever.
EVENT_TAIL_ALLOWANCE_S = 1.5
# Providers cap single clips (~30s); events with longer windows are sustained
# beds, so loop-fill them instead of leaving the tail of the window silent.
LOOP_FILL_EVENT_DURATION_S = 25.0


@dataclass
class RenderResult:
    mixed_audio_path: Path
    final_video_path: Path
    sfx_only_video_path: Optional[Path] = None


def build_timeline_clips(project: SfxProject) -> List[SfxTimelineClip]:
    clips: List[SfxTimelineClip] = []
    for event in project.events:
        audio_path = event.active_audio_path
        if not audio_path:
            continue
        loop_until_s = None
        if event.duration > LOOP_FILL_EVENT_DURATION_S:
            loop_until_s = event.effective_start + event.duration
        clips.append(
            SfxTimelineClip(
                audio_path=Path(audio_path),
                start_s=event.effective_start,
                gain_db=event.gain_db,
                fade_in_s=EVENT_FADE_IN_S,
                fade_out_s=EVENT_FADE_OUT_S,
                max_duration_s=event.duration + EVENT_TAIL_ALLOWANCE_S,
                loop_until_s=loop_until_s,
                muted=event.muted,
            )
        )
    ambience = project.ambience
    if ambience and ambience.enabled and ambience.active_audio_path:
        # Sink the bed under audible discrete events so a generated bed's own
        # impact attempts don't double-hit against our event layer.
        duck_windows = [
            (event.effective_start, event.effective_start + min(event.duration, 3.0))
            for event in project.events
            if not event.muted and event.active_audio_path
        ] or None
        clips.append(
            SfxTimelineClip(
                audio_path=Path(ambience.active_audio_path),
                start_s=0.0,
                gain_db=ambience.gain_db,
                fade_in_s=AMBIENCE_FADE_S,
                fade_out_s=AMBIENCE_FADE_S,
                loop_until_s=project.video_duration if ambience.loop else None,
                duck_windows=duck_windows,
                duck_db=ambience.duck_db,
                muted=False,
            )
        )
    return clips


def render_project(
    project: SfxProject,
    *,
    render_sfx_only_debug: bool = True,
) -> RenderResult:
    """
    Rebuild the SFX timeline from project state and mux it onto the source
    video. Bumps the project revision and updates its output paths; the caller
    is responsible for `project.save()`.
    """
    project_dir = Path(project.project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    project.revision += 1
    rev = project.revision

    mixed_audio_path = project_dir / f"sfx_timeline_rev{rev:03d}.wav"
    render_sfx_timeline_wav(
        build_timeline_clips(project),
        mixed_audio_path,
        total_duration_s=project.video_duration,
        sample_rate=project.mix.sample_rate,
        channels=project.mix.channels,
        master_gain_db=project.mix.sfx_master_gain_db,
    )

    final_video_path = project_dir / f"video_with_sfx_rev{rev:03d}.mp4"
    overlay_music_on_video(
        video_path=Path(project.video_path),
        music_path=mixed_audio_path,
        output_path=final_video_path,
        preserve_original_audio=project.mix.preserve_original_audio,
        music_volume=1.0,
    )

    sfx_only_video_path: Optional[Path] = None
    if render_sfx_only_debug:
        sfx_only_video_path = project_dir / f"video_sfx_only_rev{rev:03d}.mp4"
        overlay_music_on_video(
            video_path=Path(project.video_path),
            music_path=mixed_audio_path,
            output_path=sfx_only_video_path,
            preserve_original_audio=False,
            music_volume=1.0,
        )

    project.mixed_audio_path = str(mixed_audio_path)
    project.final_video_path = str(final_video_path)
    project.sfx_only_video_path = str(sfx_only_video_path) if sfx_only_video_path else ""
    logger.info("Rendered project rev %s -> %s", rev, final_video_path)
    return RenderResult(
        mixed_audio_path=mixed_audio_path,
        final_video_path=final_video_path,
        sfx_only_video_path=sfx_only_video_path,
    )
