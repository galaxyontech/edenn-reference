from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from EdennCode.Util.MediaUtils import (
    SUPPORTED_TRANSITIONS,
    build_slideshow_video,
    effective_transition_durations,
    normalize_transition_list,
    overlay_music_on_video,
    pick_random_transitions,
)

TRANSITION_MODE_NONE = "none"
TRANSITION_MODE_RANDOM = "random"


@dataclass
class SlideshowAssemblyStageInput:
    image_paths: List[Path]
    per_image_duration: float
    music_path: Path
    output_path: Path
    music_volume: float = 1.0
    durations: Optional[List[float]] = None
    music_start_s: float = 0.0
    # Resolution order: an explicit ``transitions`` list wins; otherwise
    # ``transition_mode`` is used — "none" keeps hard cuts, "random" picks a
    # weighted-random effect per boundary, and any SUPPORTED_TRANSITIONS name
    # applies that single effect everywhere.
    transition_mode: str = TRANSITION_MODE_NONE
    transitions: Optional[Sequence[str]] = None
    # A single blend length applied to every boundary, or one length per boundary.
    transition_duration_s: "float | Sequence[float]" = 0.4
    transition_seed: Optional[int] = None


@dataclass
class SlideshowAssemblyStageOutput:
    silent_video_path: Path
    final_video_path: Path
    applied_transitions: List[str] = field(default_factory=list)
    applied_transition_duration_s: float = 0.0


class SlideshowAssemblyStage:
    def __init__(self, *, fps: float = 30.0) -> None:
        self.fps = fps

    def run(self, stage_input: SlideshowAssemblyStageInput) -> SlideshowAssemblyStageOutput:
        final_path = stage_input.output_path.expanduser()
        if not final_path.suffix:
            final_path = final_path.with_suffix(".mp4")
        final_path.parent.mkdir(parents=True, exist_ok=True)

        silent_suffix = final_path.suffix or ".mp4"
        silent_path = final_path.with_name(f"{final_path.stem}_silent{silent_suffix}")

        durations = stage_input.durations
        transitions, effective_duration = self._resolve_transitions(stage_input)
        build_slideshow_video(
            stage_input.image_paths,
            stage_input.per_image_duration,
            silent_path,
            fps=self.fps,
            custom_durations=durations,
            transitions=transitions or None,
            transition_duration_s=stage_input.transition_duration_s,
        )
        merged_video = overlay_music_on_video(
            silent_path,
            stage_input.music_path,
            final_path,
            music_volume=stage_input.music_volume,
            preserve_original_audio=False,
            music_start_s=stage_input.music_start_s,
        )

        return SlideshowAssemblyStageOutput(
            silent_video_path=silent_path,
            final_video_path=merged_video,
            applied_transitions=transitions,
            applied_transition_duration_s=effective_duration,
        )

    def _resolve_transitions(
        self, stage_input: SlideshowAssemblyStageInput
    ) -> tuple[List[str], float]:
        """Resolve the request to one xfade name per image boundary.

        An explicit ``transitions`` list takes priority (validated and cycled to
        the boundary count); otherwise ``transition_mode`` decides. Returns
        ``([], 0.0)`` whenever the video will use hard cuts, including the
        degenerate case where clips are too short for the requested blend.
        """

        boundary_count = len(stage_input.image_paths) - 1
        if boundary_count <= 0:
            return [], 0.0

        mode = (stage_input.transition_mode or TRANSITION_MODE_NONE).strip().lower()
        has_explicit_list = bool(stage_input.transitions)
        if not has_explicit_list and mode == TRANSITION_MODE_NONE:
            return [], 0.0

        durations = stage_input.durations or (
            [stage_input.per_image_duration] * len(stage_input.image_paths)
        )
        # Per-boundary clamp; xfade is all-or-nothing (see build_slideshow_video),
        # so if any boundary is too short to blend the slideshow hard-cuts.
        per_boundary = effective_transition_durations(
            durations, stage_input.transition_duration_s, fps=self.fps
        )
        if not per_boundary or any(d <= 0 for d in per_boundary):
            return [], 0.0
        effective_duration = max(per_boundary)

        if has_explicit_list:
            # Validates every name and cycles the list to the boundary count.
            return (
                normalize_transition_list(stage_input.transitions, boundary_count),
                effective_duration,
            )
        if mode == TRANSITION_MODE_RANDOM:
            rng = (
                random.Random(stage_input.transition_seed)
                if stage_input.transition_seed is not None
                else None
            )
            return pick_random_transitions(boundary_count, rng=rng), effective_duration
        if mode in SUPPORTED_TRANSITIONS:
            return [mode] * boundary_count, effective_duration
        raise ValueError(
            f"Unknown transition_mode '{stage_input.transition_mode}'. Allowed: "
            f"{TRANSITION_MODE_NONE}, {TRANSITION_MODE_RANDOM}, or any of: "
            f"{', '.join(sorted(SUPPORTED_TRANSITIONS))}."
        )
