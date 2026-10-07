import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Setup logging to stdout
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

from EdennCode.Util.MediaUtils import ffmpeg_utils
from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import AzureMultimodalClient


class TestLogging(unittest.IsolatedAsyncioTestCase):
    @patch("EdennCode.Util.MediaUtils.ffmpeg_utils.subprocess.run")
    def test_ffmpeg_logging(self, mock_run) -> None:
        mock_run.return_value = MagicMock(stdout="30.0", returncode=0)

        with self.assertLogs("EdennCode.Util.MediaUtils.ffmpeg_utils", level="INFO") as cm:
            ffmpeg_utils.get_video_duration(Path("dummy.mp4"))

        self.assertTrue(any("Running ffmpeg" in o for o in cm.output))
        self.assertTrue(any("Finished ffmpeg" in o for o in cm.output))

    @patch("EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway.AsyncGatewayClient")
    async def test_gpt_logging(self, mock_model_gateway_cls) -> None:
        mock_client = MagicMock()
        mock_model_gateway_cls.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content='{"foo": "bar"}'))]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        client = AzureMultimodalClient(
            azure_endpoint="https://example.com",
            azure_api_version="2023-05-15",
            azure_model="chat-advanced",
            api_key="dummy"
        )

        with self.assertLogs("EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway", level="INFO") as cm:
            await client.complete_messages([], json_schema={}, max_tokens=10)

        # The log line identifies the model via the client's display_name
        # (``label:model@host``), not the bare azure_model.
        self.assertTrue(
            any(f"GPT request to {client.display_name} took" in o for o in cm.output)
        )
        self.assertTrue(any("chat-advanced" in o for o in cm.output))
        self.assertTrue(any("Usage: {'prompt_tokens': 10" in o for o in cm.output))


if __name__ == "__main__":
    unittest.main()
