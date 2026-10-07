"""
Annotation event emitted after Stage 5 (VideoAudioRemix).

Records the final remix parameters and output path.  This event closes the
pipeline-run annotation chain and provides the data needed to correlate a
served output file with the full generation trace.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from EdennCode.Annotation.core.annotation_event import AnnotationEvent


@dataclass(kw_only=True)
class RemixCompletionEvent(AnnotationEvent):
    """
    Final remix output record for a pipeline run.

    Emitted after the ``VideoAudioRemixStage`` writes the remixed video file.
    Together with :class:`~EdennCode.Annotation.events.music_generation_event.MusicGenerationEvent`
    this event allows a complete audit trail from raw video input to final output.

    Attributes
    ----------
    event_type:
        Always ``"remix_completion"``.  Do not change.
    remixed_video_filename:
        Base filename of the output remixed video (no directory path).
    preserve_original_audio:
        Whether the original video audio track was preserved and mixed with
        the generated music (ducking mode), or replaced entirely.
    music_volume:
        Gain multiplier applied to the generated music track.  ``1.0`` = unity.
    duck_gain_db:
        Audio ducking attenuation in dB applied to the original audio during
        music segments.  Only meaningful when *preserve_original_audio* is ``True``.
    stage_latency_s:
        Wall-clock seconds the remix stage took.
    total_pipeline_latency_s:
        End-to-end wall-clock duration from pipeline start to remix completion,
        in seconds.  ``None`` if not instrumented.
    """

    event_type: str = "remix_completion"
    remixed_video_filename: str = ""
    preserve_original_audio: bool = False
    music_volume: float = 1.0
    duck_gain_db: float = -9.0
    stage_latency_s: float = 0.0
    total_pipeline_latency_s: Optional[float] = None
