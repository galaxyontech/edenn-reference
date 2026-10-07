import os
from dataclasses import dataclass
from pathlib import Path

from openai import AzureOpenAI

# This module used to carry its own copy of the loader below, with a hardcoded
# absolute path that no longer resolved -- so the copy always fell through to the
# default dotenv search and the comment above it described a dead line.
from EdennCode.env import load_env

load_env()


@dataclass
class BaseModelClassConfig:
    SPEECH_MODEL: str = "speech-standard"


class AzureModelGatewayTTS4OMini:
    def __init__(self, model: str | None = None) -> None:
        self._model = model or BaseModelClassConfig.SPEECH_MODEL
        # NEVER print the API key/endpoint here — these debug prints leaked the
        # raw TTS credential into server stdout/log capture.
        client = AzureOpenAI(
            api_key=os.getenv("SPEECH_GATEWAY_API_KEY"),
            api_version=os.getenv("SPEECH_GATEWAY_API_VERSION"),
            azure_endpoint=os.getenv("SPEECH_GATEWAY_ENDPOINT"),
        )
        self._client = client

    def send_request_streaming(
        self,
        instructions: str,
        speed: float,
        script: str,
        voice: str,
        audio_save_path: Path,
    ) -> Path:
        """
        Generate TTS audio with timestamped output path.
        Returns the generated wav path.
        """

        with self._client.audio.speech.with_streaming_response.create(
            model="speech-standard",
            voice=voice,
            input=script,
            instructions=instructions,
            speed=speed,
            response_format="wav",
        ) as r:
            r.stream_to_file(str(audio_save_path))

        return audio_save_path


if __name__ == "__main__":
    tts_model = AzureModelGatewayTTS4OMini()
    audio_path = tts_model.send_request_streaming(
        instructions="Use a bright, youthful voice. Sound genuinely excited, as if unwrapping a gift.",
        speed=1.0,
        script="Look at that sparkle. Real luxury, at a price you will actually love.",
        voice="shimmer",
        audio_save_path=Path("./voiceover_smoke.wav"),
    )
    print(f"Generated audio saved at: {audio_path}")
