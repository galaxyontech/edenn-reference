import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from EdennCode.Deployment import provider_c_callback as root_provider_c_callback

from EdennCode.Deployment.music_callback_api_deployment import provider_c_callback as deployment_provider_c_callback
from EdennCode.Deployment.music_callback_api_deployment.provider_c_callback_service import (
    CallbackEvent,
    ProviderCCallbackService,
    TaskInfo,
    TrackInfo,
)


class ProviderCCallbackTests(unittest.TestCase):
    def test_root_and_deployment_wrappers_delegate_to_shared_service(self) -> None:
        payload = {
            "callbackType": "complete",
            "taskId": "task 1",
            "tracks": [{"id": "track-1", "audioUrl": "https://example.test/audio.mp3"}],
        }

        with tempfile.TemporaryDirectory(prefix="root-provider_c-callback-") as root_tmp, tempfile.TemporaryDirectory(
            prefix="deployment-provider_c-callback-"
        ) as deployment_tmp:
            root_service = ProviderCCallbackService(
                base_dir=Path(root_tmp),
                logger=root_provider_c_callback.logger,
                provider_c_module=None,
            )
            deployment_service = ProviderCCallbackService(
                base_dir=Path(deployment_tmp),
                logger=deployment_provider_c_callback.logger,
                provider_c_module=None,
            )

            with patch.object(root_provider_c_callback, "_SERVICE", root_service), patch.object(
                deployment_provider_c_callback,
                "_SERVICE",
                deployment_service,
            ):
                root_event = root_provider_c_callback.parse_callback_payload(payload)
                deployment_event = deployment_provider_c_callback.parse_callback_payload(payload)

            self.assertEqual(root_event.callback_type, "complete")
            self.assertEqual(root_event.task_id, "task 1")
            self.assertEqual(len(root_event.tracks), 1)
            self.assertEqual(root_event.tracks[0].track_id, "track-1")
            self.assertEqual(root_event.tracks[0].audio_url, "https://example.test/audio.mp3")

            self.assertEqual(deployment_event, root_event)
            self.assertEqual(len(list(root_service.webhook_log_dir.glob("*.json"))), 1)
            self.assertEqual(len(list(deployment_service.webhook_log_dir.glob("*.json"))), 1)

    def test_service_handles_lyrics_callback_type_and_writes_sanitized_record_name(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lyrics-callback-") as tmp:
            service = ProviderCCallbackService(
                base_dir=Path(tmp),
                logger=root_provider_c_callback.logger,
                provider_c_module=None,
            )
            event = CallbackEvent(
                task=TaskInfo(task_id="task/one", callback_type="lyrics"),
                tracks=[],
                raw={},
            )

            with patch.object(
                service,
                "_maybe_create_provider_c_client",
                AsyncMock(return_value=None),
            ), patch.object(
                service,
                "_fetch_lyrics_record",
                AsyncMock(return_value={"taskId": "task/one", "status": "ready"}),
            ):
                summary = service.handle_callback_event(
                    event,
                    download=False,
                    fetch_ts_lyrics=False,
                )

            self.assertEqual(summary["errors"], [])
            self.assertEqual(summary["downloaded_files"], [])
            self.assertEqual(len(summary["timestamped_lyrics_files"]), 1)

            out_path = Path(summary["timestamped_lyrics_files"][0])
            self.assertEqual(out_path.name, "task_one_lyrics.json")
            self.assertEqual(
                json.loads(out_path.read_text(encoding="utf-8")),
                {"taskId": "task/one", "status": "ready"},
            )

    def test_service_sanitizes_task_id_for_timestamped_lyrics_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ts-lyrics-callback-") as tmp:
            service = ProviderCCallbackService(
                base_dir=Path(tmp),
                logger=root_provider_c_callback.logger,
                provider_c_module=None,
            )
            fake_client = SimpleNamespace(
                get_timestamped_lyrics=AsyncMock(
                    return_value=[
                        SimpleNamespace(text="hello", startS=0.0, endS=1.0, i=0),
                    ]
                )
            )
            event = CallbackEvent(
                task=TaskInfo(task_id="task/one", callback_type="complete"),
                tracks=[TrackInfo(track_id="track-1")],
                raw={},
            )

            with patch.object(
                service,
                "_maybe_create_provider_c_client",
                AsyncMock(return_value=fake_client),
            ):
                summary = service.handle_callback_event(
                    event,
                    download=False,
                    fetch_ts_lyrics=True,
                )

            self.assertEqual(summary["errors"], [])
            self.assertEqual(summary["downloaded_files"], [])
            self.assertEqual(len(summary["timestamped_lyrics_files"]), 1)

            out_path = Path(summary["timestamped_lyrics_files"][0])
            self.assertEqual(out_path.name, "task_one_track-1.json")
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["task_id"], "task/one")
            self.assertEqual(payload["audio_id"], "track-1")
            self.assertEqual(payload["words"][0]["text"], "hello")


if __name__ == "__main__":
    unittest.main()
