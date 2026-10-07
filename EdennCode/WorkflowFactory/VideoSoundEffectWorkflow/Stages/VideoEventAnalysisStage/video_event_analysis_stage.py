from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary

from EdennCode.ModelFactory.LanguageModelFactory import AzureMultimodalClient
from EdennCode.ModelFactory.PromptFactory.video_to_sound_effect_prompt import (
    VideoToSoundEffectPrompt,
)
from EdennCode.ModelFactory.PromptFactory.video_to_sound_effect_schema import (
    VideoToSoundEffectSchemas,
)
from EdennCode.Util.MediaUtils.ffmpeg_utils import get_video_duration
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata import VideoMetadata
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.Stages.UserPromptUnderstandingStage.user_prompt_understanding_stage import (
    UserPromptUnderstandingStageOutput,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    SoundFXEvent,
)

logger = logging.getLogger(__name__)


def _extract_labeled_frames(
    video_path: Path,
    duration: float,
    sample_fps: float,
    max_frames: int,
    frame_max_width: int,
) -> List[Tuple[float, str]]:
    """Sample frames as (timestamp, jpeg data URL); rate drops to fit max_frames."""
    if duration > 0 and duration * sample_fps > max_frames:
        sample_fps = max_frames / duration
    ffmpeg_bin = resolve_ffmpeg_binary()
    with tempfile.TemporaryDirectory() as tmp:
        pattern = Path(tmp) / "frame_%05d.jpg"
        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-i",
                str(video_path),
                "-vf",
                f"fps={sample_fps},scale='min({frame_max_width},iw)':-2",
                "-q:v",
                "6",
                str(pattern),
            ],
            check=True,
            capture_output=True,
        )
        frames: List[Tuple[float, str]] = []
        for frame_path in sorted(Path(tmp).glob("frame_*.jpg"))[:max_frames]:
            index = int(frame_path.stem.split("_")[1]) - 1  # ffmpeg is 1-indexed
            timestamp = index / sample_fps
            encoded = base64.b64encode(frame_path.read_bytes()).decode("ascii")
            frames.append((timestamp, f"data:image/jpeg;base64,{encoded}"))
    return frames


class AzureVideoEventAnalysisProvider:
    """
    the model gateway responses-based video event extractor.
    """

    def __init__(
        self,
        *,
        multimodal_client: Optional[AzureMultimodalClient] = None,
        azure_video_model: Optional[str] = None,
    ) -> None:
        model = (
            azure_video_model
            or os.getenv("AZURE_VIDEO_MODEL", "")
            or os.getenv("AZURE_MODEL", "")
        ).strip()
        if multimodal_client is not None:
            self.client = multimodal_client
            return

        endpoint = (os.getenv("AZURE_ENDPOINT", "")).strip()
        api_key = (os.getenv("AZURE_API_KEY", "")).strip()
        api_version = self._resolve_responses_api_version()
        self.client = AzureMultimodalClient(
            azure_endpoint=endpoint,
            azure_api_version=api_version,
            azure_model=model,
            api_key=api_key,
        )

    @staticmethod
    def _resolve_responses_api_version() -> str:
        min_supported = "2025-03-01-preview"
        configured = (
            os.getenv("AZURE_RESPONSES_API_VERSION", "").strip()
            or os.getenv("AZURE_API_VERSION", "").strip()
        )
        if not configured:
            return min_supported

        version_date = configured.split("-preview")[0]
        if len(version_date) == 10 and version_date < "2025-03-01":
            logger.warning(
                "Configured Azure API version %s is too old for Responses API. "
                "Using %s for video event analysis.",
                configured,
                min_supported,
            )
            return min_supported
        return configured

    async def analyze_events(
        self,
        *,
        uploaded_url: str,
        duration: float,
        user_prompt: Optional[str] = None,
    ) -> Dict:
        payload = VideoToSoundEffectPrompt.build_input(
            video_url=uploaded_url,
            duration=duration,
            user_prompt=user_prompt,
        )
        schema = VideoToSoundEffectSchemas.event_response()
        response, _usage = await self.client.complete_responses_input(
            payload,
            json_schema=schema,
            max_output_tokens=1200,
        )
        return response

    async def analyze_v2(
        self,
        *,
        uploaded_url: str,
        duration: float,
        user_prompt: Optional[str] = None,
    ) -> Dict:
        """Two-scan spotting pass: scene summary + ambience bed + timed events."""
        payload = VideoToSoundEffectPrompt.build_analysis_v2_input(
            video_url=uploaded_url,
            duration=duration,
            user_prompt=user_prompt,
        )
        schema = VideoToSoundEffectSchemas.analysis_v2_response()
        response, _usage = await self.client.complete_responses_input(
            payload,
            json_schema=schema,
            max_output_tokens=2400,
        )
        return response

    async def analyze_v2_frames(
        self,
        *,
        video_path: Path,
        duration: float,
        user_prompt: Optional[str] = None,
        sample_fps: float = 1.0,
        max_frames: int = 40,
        frame_max_width: int = 512,
    ) -> Dict:
        """
        Frame-transport variant of the v2 spotting pass for deployments without
        `input_video` support: timestamp-labeled frames sampled at ~1 fps go in
        as images. Coarse (±1/sample_fps s) timing is expected here — the local
        motion-onset refinement stage recovers precision.
        """
        frames = await asyncio.to_thread(
            _extract_labeled_frames,
            Path(video_path),
            duration,
            sample_fps,
            max_frames,
            frame_max_width,
        )
        if not frames:
            raise RuntimeError(f"could not extract analysis frames from {video_path}")

        effective_fps = len(frames) / duration if duration > 0 else sample_fps
        system_text = VideoToSoundEffectPrompt._system_text_v2(duration) + (
            f"\n\nTRANSPORT NOTE: you are given {len(frames)} frames sampled at "
            f"~{effective_fps:.2f} fps; each frame is preceded by its timestamp label. "
            "Events may start between sampled frames — interpolate timestamps from the "
            "labels and visible motion; do not quantize every event to a frame label."
        )
        user_content: List[Dict] = [
            {
                "type": "text",
                "text": (
                    "Spot this video for sound design. Return scene_summary, "
                    "ambience_description, and the event list. JSON only."
                ),
            },
            {"type": "text", "text": VideoToSoundEffectPrompt._user_notes(user_prompt)},
        ]
        for timestamp, data_url in frames:
            user_content.append({"type": "text", "text": f"frame at t={timestamp:.2f}s:"})
            user_content.append({"type": "image_url", "image_url": {"url": data_url}})

        messages = [
            {"role": "system", "content": [{"type": "text", "text": system_text}]},
            {"role": "user", "content": user_content},
        ]
        schema = VideoToSoundEffectSchemas.analysis_v2_response()
        response, _usage = await self.client.complete_messages(
            messages=messages,
            json_schema=schema,
            max_tokens=2400,
        )
        return response

    @staticmethod
    def _extract_json(response: object) -> Dict:
        output_text = getattr(response, "output_text", None)
        if output_text is not None:
            return json.loads(output_text)
        output = getattr(response, "output")
        block = output[0]
        content = getattr(block, "content")
        text = getattr(content[0], "text")
        return json.loads(text)


@dataclass
class VideoEventAnalysisStageInput:
    uploaded_url: str
    video_metadata: VideoMetadata
    prompt_understanding: Optional[UserPromptUnderstandingStageOutput] = None
    user_prompt: Optional[str] = None


@dataclass
class VideoEventAnalysisStageOutput:
    list_sound_events: List[SoundFXEvent]
    scene_summary: str = ""
    ambience_description: str = ""


class VideoEventAnalysisStage:
    """
    Use uploaded video URL + prompt intent to extract timeline SFX events.
    """

    def __init__(
        self,
        video_event_analysis_default_model=None,
        provider: Optional[AzureVideoEventAnalysisProvider] = None,
    ):
        self.video_event_analysis_default_model = video_event_analysis_default_model
        self.provider = provider or AzureVideoEventAnalysisProvider()

    async def run(self, stage_input: VideoEventAnalysisStageInput) -> VideoEventAnalysisStageOutput:
        duration = stage_input.video_metadata.duration
        prompt_context = self._build_prompt_context(stage_input)
        payload = await self.provider.analyze_events(
            uploaded_url=stage_input.uploaded_url,
            duration=duration,
            user_prompt=prompt_context,
        )
        events = self._normalize_events(payload.get("events", []), duration)
        return VideoEventAnalysisStageOutput(list_sound_events=events)

    async def run_v2(self, stage_input: VideoEventAnalysisStageInput) -> VideoEventAnalysisStageOutput:
        """v2 spotting pass: events plus scene summary and ambience bed prompt."""
        duration = stage_input.video_metadata.duration
        prompt_context = self._build_prompt_context(stage_input)
        transport = os.getenv("VIDEO_SFX_ANALYSIS_TRANSPORT", "auto").strip().lower()
        payload: Optional[Dict] = None
        if transport in {"auto", "url"}:
            try:
                payload = await self.provider.analyze_v2(
                    uploaded_url=stage_input.uploaded_url,
                    duration=duration,
                    user_prompt=prompt_context,
                )
            except Exception as err:
                # Deployments without `input_video` reject the URL transport with a
                # 400; fall back to timestamp-labeled frames unless URL was forced.
                if transport == "url" or "input_video" not in str(err):
                    raise
                logger.info(
                    "Video-URL analysis unsupported by deployment (%s); "
                    "falling back to frame transport.",
                    err,
                )
        if payload is None:
            payload = await self.provider.analyze_v2_frames(
                video_path=Path(stage_input.video_metadata.path),
                duration=duration,
                user_prompt=prompt_context,
            )
        events = self._normalize_events(payload.get("events", []), duration)
        return VideoEventAnalysisStageOutput(
            list_sound_events=events,
            scene_summary=str(payload.get("scene_summary", "")).strip(),
            ambience_description=str(payload.get("ambience_description", "")).strip(),
        )

    async def _run(self, stage_input: VideoEventAnalysisStageInput) -> VideoEventAnalysisStageOutput:
        # Backward compatibility alias for older call sites.
        return await self.run(stage_input)

    @staticmethod
    def _build_prompt_context(stage_input: VideoEventAnalysisStageInput) -> str:
        fragments: List[str] = []
        user_prompt = stage_input.user_prompt
        if user_prompt:
            fragments.append(user_prompt)
        user_prompt_understanding = stage_input.prompt_understanding
        if user_prompt_understanding:
            if user_prompt_understanding.extract_sfx_description.strip():
                fragments.append(f"Interpreted SFX style: {user_prompt_understanding.extract_sfx_description.strip()}")
            if user_prompt_understanding.extract_intent_where_to_add_this_event.strip():
                fragments.append(
                    "Placement intent: "
                    f"{user_prompt_understanding.extract_intent_where_to_add_this_event.strip()}"
                )
        return "\n".join(fragments).strip()

    @staticmethod
    def _normalize_events(events_raw: List[Dict], duration: float) -> List[SoundFXEvent]:
        events: List[SoundFXEvent] = []
        for item in events_raw:
            description = str(
                item.get("event_description")
                or item.get("event_descriptions")
                or item["event_description"]
            ).strip()
            start_time = float(item["start_timestamp"])
            end_time = float(item["end_timestamp"])
            confidence = float(item["confidence"])

            # Clamp to the actual timeline; drop degenerate/out-of-range events.
            start_time = max(0.0, min(start_time, duration))
            end_time = max(0.0, min(end_time, duration))
            if end_time <= start_time:
                end_time = min(duration, start_time + 0.3)
            if end_time <= start_time:
                continue

            events.append(
                SoundFXEvent(
                    event_id="",  # assigned after sorting so ids follow the timeline
                    start_time=start_time,
                    end_time=end_time,
                    event_description=description,
                    sound_event_local_path="",
                    confidence=confidence,
                    event_type=str(item.get("event_type", "")).strip().upper(),
                    sound_prompt=str(item.get("sound_prompt", "")).strip(),
                )
            )
        events.sort(key=lambda e: e.start_time)
        for idx, event in enumerate(events, start=1):
            event.event_id = f"event_{idx:03d}"
        return events
