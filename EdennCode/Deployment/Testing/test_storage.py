import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
from typing import Optional, cast
from unittest.mock import MagicMock, patch

from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import (
    AzureBlobStorageService,
    TencentCosStorageService,
    create_storage_service,
)


class _FakeCosClient:
    def __init__(self) -> None:
        self.upload_file_calls: list[dict[str, object]] = []
        self.put_calls: list[dict[str, object]] = []
        self.presign_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []

    def upload_file(self, **kwargs):
        self.upload_file_calls.append(kwargs)
        return {"ETag": "fake"}

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        return {"ETag": "fake"}

    def get_presigned_url(self, **kwargs):
        self.presign_calls.append(kwargs)
        return (
            f"https://signed.example/{kwargs['Bucket']}/"
            f"{kwargs['Key']}?expires={kwargs['Expired']}"
        )

    def delete_object(self, **kwargs):
        self.delete_calls.append(kwargs)
        return {}


def _build_settings(*, provider: str = "tencent_cos", cos_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        media_storage_provider=provider,
        azure_storage_enabled=False,
        cos_storage_enabled=cos_enabled,
        storage_connection_string=None,
        storage_account_url=None,
        storage_account_name=None,
        storage_account_key=None,
        sas_ttl_minutes=120,
        upload_container="user-uploads",
        output_container="generated-media",
        audio_container="audio-media",
        audio_container_name="audio-media",
        llm_image_container="llm-images",
        cos_secret_id="secret-id" if cos_enabled else None,
        cos_secret_key="secret-key" if cos_enabled else None,
        cos_region="ap-guangzhou" if cos_enabled else None,
        cos_bucket_domain="objectstore.example.invalid" if cos_enabled else None,
        cos_video_bucket="video-1325383472" if cos_enabled else None,
        cos_audio_bucket="audio-1325383472" if cos_enabled else None,
        cos_image_bucket="image-1325383472" if cos_enabled else None,
        cos_video_domain="https://video.xingbao.chat" if cos_enabled else None,
        cos_audio_domain="https://audio.xingbao.chat" if cos_enabled else None,
        cos_image_domain="https://image.xingbao.chat" if cos_enabled else None,
    )


class TencentCosStorageServiceTests(unittest.TestCase):
    def _build_service(self) -> tuple[TencentCosStorageService, _FakeCosClient]:
        service = object.__new__(TencentCosStorageService)
        service.settings = _build_settings()
        service.enabled = True
        service._blob_bucket_cache = {}
        fake_client = _FakeCosClient()
        service._client = fake_client
        return service, fake_client

    def test_create_storage_service_uses_tencent_cos_when_selected(self) -> None:
        settings = _build_settings(provider="tencent_cos")
        fake_client = _FakeCosClient()

        with (
            patch("EdennCode.Deployment.storage.CosConfig", return_value=object()),
            patch("EdennCode.Deployment.storage.CosS3Client", return_value=fake_client),
        ):
            service = create_storage_service(settings)

        self.assertIsInstance(service, TencentCosStorageService)
        self.assertIs(service._client, fake_client)

    def test_create_storage_service_rejects_incomplete_tencent_config(self) -> None:
        settings = _build_settings(provider="tencent_cos", cos_enabled=False)

        with self.assertRaises(RuntimeError):
            create_storage_service(settings)

    def test_public_urls_use_media_specific_custom_domains(self) -> None:
        service, _ = self._build_service()

        video_url = service.generate_sas_url(
            container="generated-media",
            blob_name="jobs/job-1/video/海边晚霞.mp4",
        )
        audio_url = service.generate_sas_url(
            container="audio-media",
            blob_name="jobs/job-1/audio/theme.mp3",
        )
        image_url = service.generate_sas_url(
            container="generated-media",
            blob_name="jobs/job-1/thumbnail/海边晚霞.webp",
        )

        self.assertEqual(
            video_url,
            "https://video.xingbao.chat/jobs/job-1/video/%E6%B5%B7%E8%BE%B9%E6%99%9A%E9%9C%9E.mp4",
        )
        self.assertEqual(
            audio_url,
            "https://audio.xingbao.chat/jobs/job-1/audio/theme.mp3",
        )
        self.assertEqual(
            image_url,
            "https://image.xingbao.chat/jobs/job-1/thumbnail/%E6%B5%B7%E8%BE%B9%E6%99%9A%E9%9C%9E.webp",
        )

    def test_temporary_upload_uses_signed_bucket_url_for_images(self) -> None:
        service, fake_client = self._build_service()

        uploaded = service.upload_temporary_bytes(
            container="llm-images",
            blob_name="llm-inputs/video-123/scene.jpg",
            data=b"jpg-bytes",
            content_type="image/jpeg",
            ttl_minutes=5,
        )

        self.assertIsNotNone(uploaded)
        self.assertEqual(uploaded.container, "image-1325383472")
        self.assertEqual(uploaded.blob_name, "llm-inputs/video-123/scene.jpg")
        self.assertEqual(fake_client.put_calls[0]["Bucket"], "image-1325383472")
        self.assertEqual(fake_client.put_calls[0]["ContentType"], "image/jpeg")
        self.assertEqual(fake_client.presign_calls[0]["Bucket"], "image-1325383472")
        self.assertEqual(fake_client.presign_calls[0]["Expired"], 300)
        self.assertTrue(
            uploaded.sas_url.startswith(
                "https://signed.example/image-1325383472/llm-inputs/video-123/scene.jpg"
            )
        )

        deleted = service.delete_blob(
            container=uploaded.container,
            blob_name=uploaded.blob_name,
        )
        self.assertTrue(deleted)
        self.assertEqual(fake_client.delete_calls[0]["Bucket"], "image-1325383472")

    def test_path_upload_uses_advanced_sdk_upload(self) -> None:
        with patch("pathlib.Path.open", side_effect=AssertionError("upload_file should be used")):
            service, fake_client = self._build_service()
            uploaded = service.upload_path(
                container="audio-media",
                path=Path("/tmp/generated.wav"),
                blob_name="jobs/job-1/audio/generated.wav",
                content_type="audio/wav",
            )

        self.assertEqual(uploaded, "jobs/job-1/audio/generated.wav")
        self.assertEqual(len(fake_client.upload_file_calls), 1)
        call = fake_client.upload_file_calls[0]
        self.assertEqual(call["Bucket"], "audio-1325383472")
        self.assertEqual(call["Key"], "jobs/job-1/audio/generated.wav")
        self.assertEqual(call["LocalFilePath"], "/tmp/generated.wav")
        self.assertEqual(call["ContentType"], "audio/wav")


def _build_azure_settings(*, provider: str = "azure") -> SimpleNamespace:
    return SimpleNamespace(
        media_storage_provider=provider,
        azure_storage_enabled=True,
        cos_storage_enabled=False,
        storage_connection_string=None,
        storage_account_url="https://devaccount.blob.core.windows.net",
        storage_account_name="devaccount",
        storage_account_key="ZmFrZWtleQ==",
        sas_ttl_minutes=60,
        upload_container="user-uploads",
        output_container="generated-media",
        audio_container="audio-media",
        audio_container_name="audio-media",
        llm_image_container="llm-images",
        use_managed_identity=False,
    )


class CreateStorageServiceRoutingTests(unittest.TestCase):
    def test_create_returns_azure_when_provider_is_azure(self) -> None:
        settings = _build_azure_settings(provider="azure")
        with patch("EdennCode.Deployment.storage.AzureBlobStorageService.__init__", return_value=None):
            service = create_storage_service(settings)
        self.assertIsInstance(service, AzureBlobStorageService)

    def test_create_returns_azure_when_provider_is_auto(self) -> None:
        # "auto" is treated as Azure — kept for backwards compatibility with existing deployments.
        settings = _build_azure_settings(provider="auto")
        with patch("EdennCode.Deployment.storage.AzureBlobStorageService.__init__", return_value=None):
            service = create_storage_service(settings)
        self.assertIsInstance(service, AzureBlobStorageService)

    def test_create_returns_tencent_when_provider_is_tencent_cos(self) -> None:
        settings = _build_settings(provider="tencent_cos")
        fake_client = _FakeCosClient()
        with (
            patch("EdennCode.Deployment.storage.CosConfig", return_value=object()),
            patch("EdennCode.Deployment.storage.CosS3Client", return_value=fake_client),
        ):
            service = create_storage_service(settings)
        self.assertIsInstance(service, TencentCosStorageService)

    def test_create_raises_on_unknown_provider(self) -> None:
        settings = _build_azure_settings(provider="s3")
        with self.assertRaises(RuntimeError):
            create_storage_service(settings)


class AzureBlobStorageServiceTests(unittest.TestCase):
    def _build_service(self, *, sas_key: Optional[str] = "ZmFrZWtleQ==") -> AzureBlobStorageService:
        service = object.__new__(AzureBlobStorageService)
        service.settings = cast(DeploymentSettings, _build_azure_settings())
        service.enabled = True
        service._sas_key = sas_key
        service._client = MagicMock()  # type: ignore[assignment]
        return service

    def test_blob_url_constructs_correct_azure_url(self) -> None:
        service = self._build_service()
        url = service.blob_url(container="generated-media", blob_name="jobs/job-1/output/海边晚霞.mp4")
        self.assertEqual(
            url,
            "https://devaccount.blob.core.windows.net/generated-media/jobs/job-1/output/%E6%B5%B7%E8%BE%B9%E6%99%9A%E9%9C%9E.mp4",
        )

    def test_blob_url_returns_none_when_disabled(self) -> None:
        service = self._build_service()
        service.enabled = False
        self.assertIsNone(service.blob_url(container="generated-media", blob_name="out.mp4"))

    def test_generate_sas_url_returns_signed_azure_url(self) -> None:
        service = self._build_service()
        with patch(
            "EdennCode.Deployment.storage.generate_blob_sas",
            return_value="sv=2021&sig=FAKESIG",
        ) as mock_sas:
            url = service.generate_sas_url(container="generated-media", blob_name="jobs/job-1/out.mp4")

        assert url is not None
        self.assertIn("devaccount.blob.core.windows.net", url)
        self.assertIn("sv=2021&sig=FAKESIG", url)
        mock_sas.assert_called_once()
        call_kwargs = mock_sas.call_args.kwargs
        self.assertEqual(call_kwargs["account_name"], "devaccount")
        self.assertEqual(call_kwargs["container_name"], "generated-media")
        self.assertEqual(call_kwargs["blob_name"], "jobs/job-1/out.mp4")

    def test_generate_sas_url_falls_back_to_unsigned_when_no_sas_key(self) -> None:
        service = self._build_service(sas_key=None)
        url = service.generate_sas_url(container="generated-media", blob_name="jobs/job-1/out.mp4")
        assert url is not None
        self.assertNotIn("?", url)
        self.assertIn("devaccount.blob.core.windows.net/generated-media/jobs/job-1/out.mp4", url)

    def test_generate_sas_url_returns_none_when_require_signed_and_no_key(self) -> None:
        service = self._build_service(sas_key=None)
        url = service.generate_sas_url(
            container="generated-media",
            blob_name="jobs/job-1/out.mp4",
            require_signed=True,
        )
        self.assertIsNone(url)

    def test_connection_string_metadata_overrides_stale_explicit_account_metadata(self) -> None:
        settings = cast(DeploymentSettings, _build_azure_settings())
        settings.storage_connection_string = (
            "DefaultEndpointsProtocol=https;"
            "AccountName=actualaccount;"
            "AccountKey=ZmFrZWtleQ==;"
            "BlobEndpoint=https://actualaccount.blob.core.windows.net/;"
            "EndpointSuffix=core.windows.net"
        )
        settings.storage_account_name = "staleaccount"
        settings.storage_account_url = "https://staleaccount.blob.core.windows.net"
        service = object.__new__(AzureBlobStorageService)
        service.settings = settings
        service._client = SimpleNamespace(
            account_name="actualaccount",
            url="https://actualaccount.blob.core.windows.net",
        )

        service._ensure_account_metadata()

        self.assertEqual(settings.storage_account_name, "actualaccount")
        self.assertEqual(
            settings.storage_account_url,
            "https://actualaccount.blob.core.windows.net",
        )

    def test_connection_string_key_overrides_stale_explicit_account_key(self) -> None:
        settings = cast(DeploymentSettings, _build_azure_settings())
        settings.storage_connection_string = (
            "DefaultEndpointsProtocol=https;"
            "AccountName=actualaccount;"
            "AccountKey=YWN0dWFsa2V5;"
            "BlobEndpoint=https://actualaccount.blob.core.windows.net/;"
            "EndpointSuffix=core.windows.net"
        )
        settings.storage_account_name = "staleaccount"
        settings.storage_account_url = "https://staleaccount.blob.core.windows.net"
        settings.storage_account_key = "c3RhbGVrZXk="
        service = object.__new__(AzureBlobStorageService)
        service.settings = settings

        key = service._resolve_sas_key()

        self.assertEqual(key, "YWN0dWFsa2V5")
        self.assertEqual(settings.storage_account_key, "YWN0dWFsa2V5")
        self.assertEqual(settings.storage_account_name, "actualaccount")
        self.assertEqual(
            settings.storage_account_url,
            "https://actualaccount.blob.core.windows.net",
        )

    def test_upload_bytes_calls_upload_blob_with_correct_content_type(self) -> None:
        service = self._build_service()
        fake_blob_client = MagicMock()
        service._client.get_blob_client.return_value = fake_blob_client  # type: ignore[union-attr]

        result = service.upload_bytes(
            container="generated-media",
            blob_name="jobs/job-1/out.mp4",
            data=b"fake-video-bytes",
            content_type="video/mp4",
        )

        self.assertEqual(result, "jobs/job-1/out.mp4")
        service._client.get_blob_client.assert_called_once_with(  # type: ignore[union-attr]
            container="generated-media", blob="jobs/job-1/out.mp4"
        )
        fake_blob_client.upload_blob.assert_called_once()
        _, kwargs = fake_blob_client.upload_blob.call_args
        self.assertTrue(kwargs.get("overwrite"))
        self.assertEqual(kwargs["content_settings"].content_type, "video/mp4")

    def test_upload_path_calls_upload_blob(self) -> None:
        service = self._build_service()
        fake_blob_client = MagicMock()
        service._client.get_blob_client.return_value = fake_blob_client  # type: ignore[union-attr]

        with NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"fake-video")
            tmp_path = Path(f.name)

        try:
            result = service.upload_path(
                container="generated-media",
                path=tmp_path,
                blob_name="jobs/job-1/out.mp4",
                content_type="video/mp4",
            )
        finally:
            tmp_path.unlink(missing_ok=True)

        self.assertEqual(result, "jobs/job-1/out.mp4")
        fake_blob_client.upload_blob.assert_called_once()

    def test_upload_path_returns_none_when_disabled(self) -> None:
        service = self._build_service()
        service.enabled = False
        result = service.upload_path(
            container="generated-media",
            path=Path("/tmp/out.mp4"),
            blob_name="jobs/job-1/out.mp4",
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
