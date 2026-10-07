

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import ProviderCApi
from EdennCode.Util.MediaUtils.pipeline_util import hash_str
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.AssetMetadata import video_metadata
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.audio_window_renderer import AudioWindowRenderer
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.matching import EdennMusicWindowMatcher
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.word_ts import WordTS


@dataclass
class MusicMatchingStageInput:
    provider_c_music_provider: Optional[ProviderCApi]
    video_metadata: video_metadata.VideoMetadata
    local_music_path: Path
    timestamp_lyrics: List[WordTS]
    source_track_label: Optional[str] = None
    require_lyrics: bool = True


@dataclass
class MusicMatchingStageOutput:
    reranked_music_outputs_path: Path
    music_start_s: float = 0.0
    aligned_lyrics: List[WordTS] = field(default_factory=list)
    used_track: Optional[str] = None
    alignment_score: float = 0.0
    alignment_details: Dict[str, Any] = field(default_factory=dict)


class MusicMatchingStage:
    def __init__(self) -> None:
        pass

    @staticmethod
    def _offset_word_ts(words: List[WordTS], offset_s: float, duration_s: float) -> List[WordTS]:
        aligned: List[WordTS] = []
        for w in words:
            start = float(getattr(w, "startS", 0.0)) - offset_s
            end = float(getattr(w, "endS", 0.0)) - offset_s
            if end <= 0:
                continue
            start = max(0.0, start)
            if start >= duration_s:
                continue
            end = min(duration_s, end)
            aligned.append(
                WordTS(
                    text=getattr(w, "text", ""),
                    startS=start,
                    endS=end,
                    i=getattr(w, "i", None),
                )
            )
        return aligned

    async def run(self, stage_input: MusicMatchingStageInput) -> MusicMatchingStageOutput:

        matcher = EdennMusicWindowMatcher()
        best, _ = matcher.match(
            video_path=str(
                stage_input.video_metadata.path),
            music_path=str(
                stage_input.local_music_path),
            word_ts=stage_input.timestamp_lyrics,
            require_lyrics=stage_input.require_lyrics,
            topk=1,
        )
        # MP3 keeps the client-facing audio_url small; the remix mux re-encodes
        # to AAC anyway, so a PCM intermediate buys nothing here.
        renderer = AudioWindowRenderer(
            audio_codec="libmp3lame", audio_bitrate="192k")
        hashed_inputs = hash_str()
        matched_audio_path = Path(stage_input.video_metadata.temp_folder) / \
            f"matched_{hashed_inputs}.mp3"
        renderer.render_audio(
            music_path=str(
                stage_input.local_music_path),
            music_start_s=best.music_start_s,
            duration_s=stage_input.video_metadata.duration,
            out_path=str(matched_audio_path),
        )
        music_path_for_remix = matched_audio_path

        aligned_lyrics = self._offset_word_ts(
            stage_input.timestamp_lyrics,
            best.music_start_s,
            stage_input.video_metadata.duration,
        )

        return MusicMatchingStageOutput(
            reranked_music_outputs_path=music_path_for_remix,
            music_start_s=best.music_start_s,
            aligned_lyrics=aligned_lyrics,
            used_track=stage_input.source_track_label,
            alignment_score=float(getattr(best, "score", 0.0) or 0.0),
            alignment_details=dict(getattr(best, "details", {}) or {}),
        )
