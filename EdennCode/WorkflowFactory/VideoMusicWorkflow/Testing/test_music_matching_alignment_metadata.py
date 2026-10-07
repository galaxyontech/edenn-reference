import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.music_matching_stage import (
    MusicMatchingStage,
    MusicMatchingStageInput,
)


class _FakeMatcher:
    def match(self, **_kwargs: Any) -> tuple[SimpleNamespace, list[Any]]:
        best = SimpleNamespace(
            music_start_s=1.25,
            score=0.87,
            details={"align_score": 0.91, "shape_score": 0.73},
        )
        return best, []


class _FakeRenderer:
    def render_audio(self, *, out_path: str, **_kwargs: Any) -> None:
        Path(out_path).write_bytes(b"matched-audio")


class MusicMatchingAlignmentMetadataTests(unittest.TestCase):
    def test_stage_output_exposes_alignment_score_and_details(self) -> None:
        with tempfile.TemporaryDirectory(prefix="music-match-alignment-") as tmp:
            tmp_dir = Path(tmp)
            video_path = tmp_dir / "video.mp4"
            music_path = tmp_dir / "music.wav"
            video_path.write_bytes(b"video")
            music_path.write_bytes(b"music")
            video_metadata = SimpleNamespace(
                path=video_path,
                temp_folder=str(tmp_dir),
                duration=2.0,
            )

            stage = MusicMatchingStage()
            with patch(
                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.music_matching_stage.EdennMusicWindowMatcher",
                return_value=_FakeMatcher(),
            ), patch(
                "EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.MusicMatchingStage.music_matching_stage.AudioWindowRenderer",
                return_value=_FakeRenderer(),
            ):
                output = asyncio.run(
                    stage.run(
                        MusicMatchingStageInput(
                            provider_c_music_provider=cast(Any, SimpleNamespace()),
                            video_metadata=cast(Any, video_metadata),
                            local_music_path=music_path,
                            timestamp_lyrics=[],
                            source_track_label="primary",
                        )
                    )
                )

            self.assertEqual(output.music_start_s, 1.25)
            self.assertEqual(output.alignment_score, 0.87)
            self.assertEqual(
                output.alignment_details,
                {"align_score": 0.91, "shape_score": 0.73},
            )
            self.assertEqual(output.used_track, "primary")
            self.assertTrue(output.reranked_music_outputs_path.exists())


if __name__ == "__main__":
    unittest.main()
