from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_music import (
    ProviderBMusicProvider,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import (
    ProviderCApi,
    UploadCoverParams,
)
from EdennCode.Util.MediaUtils import resolve_ffmpeg_binary, strip_provider_tail
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import (
    WordTS,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.media_tools import (
    MediaTools,
)
from EdennCode.exceptions import (
    EdennConfigurationError,
    EdennMediaProcessingError,
    EdennValidationError,
)


logger = logging.getLogger(__name__)


def _clip_words(words: List[WordTS], duration_s: float) -> List[WordTS]:
    """Drop lyric timings the tail chop just took the audio out from under."""

    if duration_s <= 0:
        return list(words or [])
    clipped: List[WordTS] = []
    for word in words or []:
        start_s = float(getattr(word, "startS", 0.0))
        end_s = min(float(getattr(word, "endS", 0.0)), duration_s)
        if start_s >= duration_s or end_s <= start_s:
            continue
        clipped.append(
            WordTS(
                text=getattr(word, "text", ""),
                startS=start_s,
                endS=end_s,
                i=getattr(word, "i", None),
            )
        )
    return clipped


@dataclass
class AudioCreativeGenerationStageInput:
    source_audio_path: Path
    source_audio_provider_url: Optional[str]
    prompt_payload: Dict[str, str]
    include_vocals: bool
    vocal_gender: str
    modelspec: str
    workdir: Path
    vocal_id: Optional[str] = None
    vocal_sample_path: Optional[Path] = None
    provider_c_custom_mode: bool = False
    provider_c_style_weight: Optional[float] = None
    provider_c_audio_weight: Optional[float] = None
    provider_c_weirdness_constraint: Optional[float] = None


@dataclass
class AudioCreativeGenerationStageOutput:
    edited_audio_path: Path
    secondary_edited_audio_path: Optional[Path] = None
    lyrics_timestamps: List[WordTS] = field(default_factory=list)
    used_modelspec: str = ""
    vocal_id_used: Optional[str] = None


class AudioCreativeGenerationStage:
    def __init__(
        self,
        *,
        provider_c_music_provider: ProviderCApi | None = None,
        provider_b_music_provider: ProviderBMusicProvider | None = None,
        provider_c_music_provider_factory: Callable[[], ProviderCApi] | None = None,
        provider_b_music_provider_factory: Callable[[], ProviderBMusicProvider] | None = None,
    ) -> None:
        self.provider_c_music_provider = provider_c_music_provider
        self.provider_b_music_provider = provider_b_music_provider
        self._provider_c_music_provider_factory = provider_c_music_provider_factory
        self._provider_b_music_provider_factory = provider_b_music_provider_factory

    def _require_provider_c_music_provider(self) -> ProviderCApi:
        if self.provider_c_music_provider is None and self._provider_c_music_provider_factory is not None:
            self.provider_c_music_provider = self._provider_c_music_provider_factory()
        if self.provider_c_music_provider is None:
            raise EdennConfigurationError(
                "PROVIDER_C_API_KEY (or PROVIDER_C_API_KEY_1..N) not set; cannot use edenn_studio creative edit",
                component="audio_creative_edit",
                operation="init_provider_c",
            )
        return self.provider_c_music_provider

    def _require_provider_b_music_provider(self) -> ProviderBMusicProvider:
        if self.provider_b_music_provider is None and self._provider_b_music_provider_factory is not None:
            self.provider_b_music_provider = self._provider_b_music_provider_factory()
        if self.provider_b_music_provider is None:
            raise EdennConfigurationError(
                "PROVIDER_B_API_KEY not set; cannot use edenn_enhanced creative edit",
                component="audio_creative_edit",
                operation="init_provider_b",
            )
        return self.provider_b_music_provider

    async def run(
        self,
        stage_input: AudioCreativeGenerationStageInput,
    ) -> AudioCreativeGenerationStageOutput:
        modelspec = (stage_input.modelspec or "").strip().lower()
        if modelspec == "edenn_studio":
            primary_path, secondary_path, words = await self._run_provider_c(stage_input)
            vocal_id_used = None
        elif modelspec == "edenn_enhanced":
            primary_path, secondary_path, words, vocal_id_used = await self._run_provider_b(stage_input)
        else:
            raise EdennValidationError(
                f"Unsupported creative edit modelspec: {modelspec}",
                component="audio_creative_edit",
                operation="generation",
            )
        # Both supported specs are paid providers that sign the end of what they
        # return, and every take here is handed straight to the customer — this
        # workflow has no window cut and no other chop to fall back on.
        primary_path, primary_duration_s = strip_provider_tail(primary_path)
        if secondary_path is not None:
            secondary_path, _ = strip_provider_tail(secondary_path)
        return AudioCreativeGenerationStageOutput(
            edited_audio_path=primary_path,
            secondary_edited_audio_path=secondary_path,
            lyrics_timestamps=_clip_words(words, primary_duration_s),
            used_modelspec=modelspec,
            vocal_id_used=vocal_id_used,
        )

    async def _run_provider_b(
        self,
        stage_input: AudioCreativeGenerationStageInput,
    ) -> tuple[Path, Optional[Path], List[WordTS], Optional[str]]:
        melody_path = self._prepare_provider_b_melody_audio(
            source_audio_path=stage_input.source_audio_path,
            workdir=stage_input.workdir,
        )
        style_prompt = (
            stage_input.prompt_payload.get("style_prompt")
            or stage_input.prompt_payload.get("edit_prompt")
            or ""
        ).strip()
        edit_prompt = (
            stage_input.prompt_payload.get("edit_prompt")
            or style_prompt
        ).strip()
        lyrics_prompt = (stage_input.prompt_payload.get("lyrics_prompt") or "").strip()
        primary_output = stage_input.workdir / "creative_edit_primary.mp3"
        provider_b_control_prompt = style_prompt or edit_prompt

        if provider_b_control_prompt:
            logger.info(
                "ProviderB melody-guided creative edit is using the source melody as the main guide and retaining a lightweight control prompt."
            )
        else:
            provider_b_control_prompt = "Instrumental cinematic reinterpretation of the source melody."

        if stage_input.include_vocals:
            provider_b_provider = self._require_provider_b_music_provider()
            resolved_vocal_id = (stage_input.vocal_id or "").strip() or None
            if stage_input.vocal_sample_path is not None and not resolved_vocal_id:
                resolved_vocal_id = await provider_b_provider.clone_vocal(stage_input.vocal_sample_path)
            if not lyrics_prompt:
                lyrics_prompt = edit_prompt or style_prompt
            primary_path, secondary_path, words = await provider_b_provider.generate_with_melody_variants(
                prompt=provider_b_control_prompt,
                lyrics_prompt=lyrics_prompt,
                melody_audio_path=melody_path,
                vocal_id=resolved_vocal_id,
                n=2,
                output_path=primary_output,
            )
            return primary_path, secondary_path, words, resolved_vocal_id

        if stage_input.vocal_id or stage_input.vocal_sample_path:
            raise EdennValidationError(
                "Vocal clone inputs require a vocal creative edit request.",
                public_message="Vocal clone inputs can only be used for vocal edenn_enhanced creative edits.",
                component="audio_creative_edit",
                operation="generation",
            )

        provider_b_provider = self._require_provider_b_music_provider()
        primary_path, secondary_path = await provider_b_provider.generate_instrumental_with_melody_variants(
            prompt=provider_b_control_prompt,
            melody_audio_path=melody_path,
            n=2,
            output_path=primary_output,
        )
        return primary_path, secondary_path, [], None

    async def _run_provider_c(
        self,
        stage_input: AudioCreativeGenerationStageInput,
    ) -> tuple[Path, Optional[Path], List[WordTS]]:
        upload_url = (stage_input.source_audio_provider_url or "").strip()
        if not upload_url:
            raise EdennConfigurationError(
                "ProviderC creative edit requires a remote source audio URL.",
                public_message="Audio cover generation is unavailable for this request.",
                component="audio_creative_edit",
                operation="provider_c_upload_cover",
            )

        edit_prompt = (
            stage_input.prompt_payload.get("edit_prompt")
            or stage_input.prompt_payload.get("style_prompt")
            or ""
        ).strip()
        style_prompt = (
            stage_input.prompt_payload.get("style_prompt")
            or edit_prompt
        ).strip()
        lyrics_prompt = (stage_input.prompt_payload.get("lyrics_prompt") or "").strip()
        title = (
            stage_input.prompt_payload.get("title")
            or stage_input.prompt_payload.get("edit_intent_summary")
            or edit_prompt
            or "Creative Edit"
        ).strip()
        instrumental = not stage_input.include_vocals

        if stage_input.provider_c_custom_mode:
            provider_c_extra = self._build_provider_c_extra(stage_input)
            custom_style = self._truncate_text(style_prompt, 1000)
            if not custom_style:
                raise EdennValidationError(
                    "ProviderC custom mode requires a non-empty style prompt.",
                    component="audio_creative_edit",
                    operation="provider_c_upload_cover",
                )
            custom_title = self._truncate_text(title, 100)
            if not custom_title:
                raise EdennValidationError(
                    "ProviderC custom mode requires a non-empty title.",
                    component="audio_creative_edit",
                    operation="provider_c_upload_cover",
                )
            custom_prompt = ""
            if not instrumental:
                custom_prompt = self._truncate_text(lyrics_prompt or edit_prompt, 5000)
                if not custom_prompt:
                    raise EdennValidationError(
                        "ProviderC custom mode requires a non-empty lyrics prompt for vocal runs.",
                        component="audio_creative_edit",
                        operation="provider_c_upload_cover",
                    )
            params = UploadCoverParams(
                prompt=custom_prompt,
                upload_url=upload_url,
                model="V5",
                custom_mode=True,
                instrumental=instrumental,
                title=custom_title,
                style=custom_style,
                style_weight=stage_input.provider_c_style_weight,
                weirdness_constraint=stage_input.provider_c_weirdness_constraint,
                audio_weight=stage_input.provider_c_audio_weight,
                extra=provider_c_extra,
            )
        else:
            simple_prompt = self._truncate_text(edit_prompt, 500)
            if not simple_prompt:
                raise EdennValidationError(
                    "Creative edit prompt cannot be empty for ProviderC upload-cover.",
                    component="audio_creative_edit",
                    operation="provider_c_upload_cover",
                )
            params = UploadCoverParams(
                prompt=simple_prompt,
                upload_url=upload_url,
                model="V5",
                custom_mode=False,
                instrumental=instrumental,
            )
        provider_c_provider = self._require_provider_c_music_provider()
        task_id, generation_result, _ = await provider_c_provider.upload_cover_and_poll_tracks(
            params,
            timeout_s=300.0,
            poll_s=10.0,
        )
        tracks = generation_result.tracks
        if not tracks:
            raise EdennValidationError(
                "ProviderC creative edit completed without returning tracks.",
                component="audio_creative_edit",
                operation="provider_c_upload_cover",
            )
        primary_track = tracks[0]
        secondary_track = next(
            (track for track in tracks[1:] if track.audio_id != primary_track.audio_id),
            None,
        )
        primary_path = stage_input.workdir / "primary.mp3"
        await provider_c_provider.download(primary_track, primary_path)
        secondary_path: Optional[Path] = None
        if secondary_track:
            secondary_path = stage_input.workdir / "secondary.mp3"
            await provider_c_provider.download(secondary_track, secondary_path)
        words: List[WordTS] = []
        if stage_input.include_vocals:
            words = await provider_c_provider.wait_for_timestamped_lyrics(
                task_id,
                primary_track.audio_id,
                timeout_s=200.0,
            )
        return primary_path, secondary_path, words

    @staticmethod
    def _truncate_text(value: str, limit: int) -> str:
        text = (value or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit].rstrip()

    @staticmethod
    def _to_provider_c_vocal_gender(vocal_gender: str) -> Optional[str]:
        normalized = (vocal_gender or "").strip().lower()
        if normalized in {"male", "m"}:
            return "m"
        if normalized in {"female", "f"}:
            return "f"
        return None

    def _build_provider_c_extra(
        self,
        stage_input: AudioCreativeGenerationStageInput,
    ) -> Optional[Dict[str, str]]:
        if not stage_input.include_vocals:
            return None
        vocal_gender = self._to_provider_c_vocal_gender(stage_input.vocal_gender)
        if not vocal_gender:
            return None
        return {"vocalGender": vocal_gender}

    @staticmethod
    def _prepare_provider_b_melody_audio(
        *,
        source_audio_path: Path,
        workdir: Path,
    ) -> Path:
        duration_s = MediaTools.duration_seconds(str(source_audio_path))
        if duration_s < 5.0:
            raise EdennValidationError(
                "ProviderB melody-guided generation requires source audio of at least 5 seconds.",
                public_message="The source audio is too short for melody-guided generation.",
                component="audio_creative_edit",
                operation="prepare_provider_b_melody_audio",
            )
        ffmpeg_bin = resolve_ffmpeg_binary()
        output_path = workdir / f"{source_audio_path.stem}_melody.m4a"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(source_audio_path),
            "-vn",
            "-t",
            "60",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise EdennMediaProcessingError(
                "Failed to prepare source audio for melody-guided generation.",
                component="audio_creative_edit",
                operation="prepare_provider_b_melody_audio",
                context={"source_audio_path": str(source_audio_path)},
                cause=exc,
            ) from exc
        return output_path
