from dataclasses import dataclass
from pathlib import Path

import tempfile

from EdennCode.env import load_env

load_env()


@dataclass
class VoiceOverAssetMetadata:
    output_temporary_folder: Path
    video_asset_path: Path


@dataclass 
class PreprocessStageInput:
    video_path: Path


@dataclass
class PreprocessStageOutput:
    metadata: VoiceOverAssetMetadata


class PreprocessStage:
    """
    Preprocess the input video for voiceover generation.
    """

    def __init__(self) -> None:
        pass

    def run(self, stage_input: PreprocessStageInput, is_local = False ) -> PreprocessStageOutput:
        temp_dir = Path(tempfile.mkdtemp(prefix="vo_preprocess_"))
        if is_local:
            temp_dir = Path( "/path/to/repo/local_outputs/voiceovers")

        metadata = VoiceOverAssetMetadata(output_temporary_folder=temp_dir, video_asset_path=stage_input.video_path)
        print(metadata)
        return PreprocessStageOutput(metadata=metadata)

