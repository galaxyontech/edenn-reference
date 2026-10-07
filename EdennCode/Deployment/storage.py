from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Optional, Union

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import (
    BlobClient,
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)

from EdennCode.exceptions import EdennConfigurationError
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Util.blob_paths import encode_blob_name_for_url

try:
    from qcloud_cos import CosConfig, CosS3Client
except ImportError:  # pragma: no cover - exercised when COS is not installed.
    CosConfig = None
    CosS3Client = None

logger = logging.getLogger(__name__)

_AUDIO_SUFFIXES = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".wav",
    ".wma",
}
_VIDEO_SUFFIXES = {
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
}
_IMAGE_SUFFIXES = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".webp",
}


@dataclass(frozen=True)
class TemporaryBlobUpload:
    container: str
    blob_name: str
    sas_url: str


class AzureBlobStorageService:
    """
    Thin wrapper around Azure Blob Storage that uploads user inputs / outputs
    and returns SAS URLs so clients can download the media.
    """

    def __init__(self, settings: DeploymentSettings) -> None:
        self.settings = settings
        self._client: Optional[BlobServiceClient] = None
        self._sas_key: Optional[str] = None
        self.enabled = bool(
            settings.storage_connection_string or settings.storage_account_url
        )

        if not self.enabled:
            logger.warning("Azure storage is not configured; files remain on the local disk.")
            return

        self._client = self._build_client()
        self._ensure_account_metadata()
        self._sas_key = self._resolve_sas_key()

        self._ensure_container(settings.upload_container)
        self._ensure_container(settings.output_container)
        if settings.audio_container_name != settings.output_container:
            self._ensure_container(settings.audio_container_name)
        if settings.llm_image_container not in {
            settings.upload_container,
            settings.output_container,
            settings.audio_container_name,
        }:
            self._ensure_container(settings.llm_image_container)

    def _connection_string_parts(self) -> dict[str, str]:
        conn = self.settings.storage_connection_string
        if not conn:
            return {}

        parts: dict[str, str] = {}
        for fragment in conn.split(";"):
            if "=" not in fragment:
                continue
            k, v = fragment.split("=", 1)
            parts[k] = v
        return parts

    def _build_client(self) -> BlobServiceClient:
        if self.settings.storage_connection_string:
            client = BlobServiceClient.from_connection_string(self.settings.storage_connection_string)
            return client

        if not self.settings.storage_account_url:
            raise EdennConfigurationError(
                "Set AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_URL to enable blob uploads.",
                component="azure_blob_storage",
                operation="build_client",
            )

        credential: Union[DefaultAzureCredential, str, None]
        if self.settings.use_managed_identity:
            credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        else:
            credential = self.settings.storage_account_key

        return BlobServiceClient(
            account_url=self.settings.storage_account_url,
            credential=credential,
        )

    def _ensure_account_metadata(self) -> None:
        if not self._client:
            return

        # When a connection string is present, it is the authority for the
        # account that uploads actually target. Stale explicit URL/name env vars
        # can otherwise produce SAS URLs for a different storage account.
        parts = self._connection_string_parts()
        if self.settings.storage_connection_string:
            account_name = parts.get("AccountName") or getattr(self._client, "account_name", None)
            endpoint = parts.get("BlobEndpoint") or getattr(self._client, "url", None)
            if account_name:
                self.settings.storage_account_name = account_name
            if endpoint:
                self.settings.storage_account_url = str(endpoint).rstrip("/")
            return

        if not self.settings.storage_account_name:
            self.settings.storage_account_name = self._client.account_name
        if not self.settings.storage_account_url:
            self.settings.storage_account_url = self._client.url.rstrip("/")

    def _resolve_sas_key(self) -> Optional[str]:
        conn = self.settings.storage_connection_string
        if conn:
            parts = self._connection_string_parts()
            key = parts.get("AccountKey")
            if key:
                self.settings.storage_account_key = key
            account_name = parts.get("AccountName")
            if account_name:
                self.settings.storage_account_name = account_name
            endpoint = parts.get("BlobEndpoint")
            if endpoint:
                self.settings.storage_account_url = endpoint.rstrip("/")
            return key

        if self.settings.storage_account_key:
            return self.settings.storage_account_key
        return None

    def _ensure_container(self, container_name: str) -> None:
        if not self.enabled or not self._client:
            return
        client = self._client.get_container_client(container_name)
        try:
            client.create_container()
        except ResourceExistsError:
            return

    def upload_path(
        self,
        *,
        container: str,
        path: Path,
        blob_name: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> Optional[str]:
        if not self.enabled or not self._client:
            return None
        blob = blob_name or path.name
        with path.open("rb") as fh:
            self._upload_stream(
                container=container,
                blob_name=blob,
                data=fh,
                content_type=content_type,
            )
        return blob

    def upload_bytes(
        self,
        *,
        container: str,
        blob_name: str,
        data: bytes,
        content_type: Optional[str] = None,
    ) -> Optional[str]:
        if not self.enabled or not self._client:
            return None
        self._upload_stream(
            container=container,
            blob_name=blob_name,
            data=data,
            content_type=content_type,
        )
        return blob_name

    def _upload_stream(
        self,
        *,
        container: str,
        blob_name: str,
        data: Union[IO[bytes], bytes],
        content_type: Optional[str],
    ) -> None:
        if not self._client:
            return
        blob_client: BlobClient = self._client.get_blob_client(container=container, blob=blob_name)
        content_settings = ContentSettings(content_type=content_type) if content_type else None
        blob_client.upload_blob(
            data,
            overwrite=True,
            content_settings=content_settings,
        )

    def blob_url(self, *, container: str, blob_name: str) -> Optional[str]:
        if not self.enabled:
            return None
        base = (self.settings.storage_account_url or "").rstrip("/")
        if not base and self._client:
            base = self._client.url.rstrip("/")
        if not base:
            return None
        encoded_blob_name = encode_blob_name_for_url(blob_name)
        return f"{base}/{container}/{encoded_blob_name}"

    def generate_sas_url(
        self,
        *,
        container: str,
        blob_name: str,
        ttl_minutes: Optional[int] = None,
        require_signed: bool = False,
    ) -> Optional[str]:
        base_url = self.blob_url(container=container, blob_name=blob_name)
        if not base_url:
            return None
        if not self._sas_key or not self.settings.storage_account_name:
            if require_signed:
                return None
            # Fall back to unsigned blob URL.
            return base_url

        expiry = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes or self.settings.sas_ttl_minutes)
        sas = generate_blob_sas(
            account_name=self.settings.storage_account_name,
            account_key=self._sas_key,
            container_name=container,
            blob_name=blob_name,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
        )
        return f"{base_url}?{sas}"

    def upload_temporary_bytes(
        self,
        *,
        container: str,
        blob_name: str,
        data: bytes,
        content_type: Optional[str] = None,
        ttl_minutes: Optional[int] = None,
    ) -> Optional[TemporaryBlobUpload]:
        uploaded_blob = self.upload_bytes(
            container=container,
            blob_name=blob_name,
            data=data,
            content_type=content_type,
        )
        if not uploaded_blob:
            return None
        sas_url = self.generate_sas_url(
            container=container,
            blob_name=uploaded_blob,
            ttl_minutes=ttl_minutes,
            require_signed=True,
        )
        if not sas_url:
            self.delete_blob(
                container=container,
                blob_name=uploaded_blob,
            )
            return None
        return TemporaryBlobUpload(
            container=container,
            blob_name=uploaded_blob,
            sas_url=sas_url,
        )

    def delete_blob(
        self,
        *,
        container: str,
        blob_name: str,
    ) -> bool:
        if not self.enabled or not self._client:
            return False
        blob_client: BlobClient = self._client.get_blob_client(container=container, blob=blob_name)
        try:
            blob_client.delete_blob(delete_snapshots="include")
            return True
        except ResourceNotFoundError:
            return False


class TencentCosStorageService:
    """
    Uploads assets to Tencent COS and returns public custom-domain URLs for
    media links while still supporting signed temporary URLs for workflow-only
    assets such as LLM image inputs.
    """

    def __init__(self, settings: DeploymentSettings) -> None:
        self.settings = settings
        self._client: Optional[Any] = None
        self._blob_bucket_cache: dict[tuple[str, str], str] = {}
        self.enabled = settings.cos_storage_enabled
        if not self.enabled:
            logger.warning("Tencent COS storage is not configured; files remain on the local disk.")
            return

        if CosConfig is None or CosS3Client is None:
            raise RuntimeError(
                "cos-python-sdk-v5 must be installed to use Tencent COS storage."
            )

        config = CosConfig(
            Region=settings.cos_region,
            SecretId=settings.cos_secret_id,
            SecretKey=settings.cos_secret_key,
            Scheme="https",
        )
        self._client = CosS3Client(config)

    def _infer_asset_kind(
        self,
        *,
        container: str,
        blob_name: str,
        content_type: Optional[str] = None,
    ) -> str:
        normalized_content_type = (content_type or "").strip().lower()
        if normalized_content_type.startswith("audio/"):
            return "audio"
        if normalized_content_type.startswith("video/"):
            return "video"
        if normalized_content_type.startswith("image/"):
            return "image"

        suffix = Path(blob_name).suffix.lower()
        if suffix in _AUDIO_SUFFIXES:
            return "audio"
        if suffix in _VIDEO_SUFFIXES:
            return "video"
        if suffix in _IMAGE_SUFFIXES:
            return "image"

        lowered_blob_name = blob_name.lower()
        if container == self.settings.audio_container_name or "/audio/" in lowered_blob_name:
            return "audio"
        if container == self.settings.llm_image_container or "/thumbnail/" in lowered_blob_name:
            return "image"
        return "video"

    def _bucket_for_kind(self, kind: str) -> str:
        if kind == "audio":
            return self.settings.cos_audio_bucket or ""
        if kind == "image":
            return self.settings.cos_image_bucket or ""
        return self.settings.cos_video_bucket or ""

    def _public_domain_for_bucket(self, bucket: str) -> Optional[str]:
        if bucket == self.settings.cos_audio_bucket:
            return self._normalize_public_domain(self.settings.cos_audio_domain)
        if bucket == self.settings.cos_image_bucket:
            return self._normalize_public_domain(self.settings.cos_image_domain)
        if bucket == self.settings.cos_video_bucket:
            return self._normalize_public_domain(self.settings.cos_video_domain)
        return None

    @staticmethod
    def _normalize_public_domain(raw_value: Optional[str]) -> Optional[str]:
        value = (raw_value or "").strip()
        if not value:
            return None
        if not value.startswith(("http://", "https://")):
            value = f"https://{value}"
        return value.rstrip("/")

    def _default_bucket_base_url(self, bucket: str) -> str:
        bucket_domain = (self.settings.cos_bucket_domain or "").strip().strip("/")
        if not bucket_domain:
            bucket_domain = f"cos.{self.settings.cos_region}.myqcloud.com"
        return f"https://{bucket}.{bucket_domain}"

    def _resolve_bucket(
        self,
        *,
        container: str,
        blob_name: str,
        content_type: Optional[str] = None,
    ) -> str:
        cached_bucket = self._blob_bucket_cache.get((container, blob_name))
        if cached_bucket:
            return cached_bucket
        kind = self._infer_asset_kind(
            container=container,
            blob_name=blob_name,
            content_type=content_type,
        )
        return self._bucket_for_kind(kind)

    def _remember_bucket(self, *, container: str, blob_name: str, bucket: str) -> None:
        self._blob_bucket_cache[(container, blob_name)] = bucket
        self._blob_bucket_cache[(bucket, blob_name)] = bucket

    def _public_blob_url(self, *, bucket: str, blob_name: str) -> str:
        base = self._public_domain_for_bucket(bucket) or self._default_bucket_base_url(bucket)
        return f"{base}/{encode_blob_name_for_url(blob_name)}"

    def upload_path(
        self,
        *,
        container: str,
        path: Path,
        blob_name: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> Optional[str]:
        if not self.enabled or not self._client:
            return None
        blob = blob_name or path.name
        bucket = self._resolve_bucket(
            container=container,
            blob_name=blob,
            content_type=content_type,
        )
        upload_kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Key": blob,
            "LocalFilePath": str(path),
            "PartSize": 1,
            "MAXThread": 5,
            "EnableMD5": False,
        }
        if content_type:
            upload_kwargs["ContentType"] = content_type
        self._client.upload_file(**upload_kwargs)
        self._remember_bucket(container=container, blob_name=blob, bucket=bucket)
        return blob

    def upload_bytes(
        self,
        *,
        container: str,
        blob_name: str,
        data: bytes,
        content_type: Optional[str] = None,
    ) -> Optional[str]:
        if not self.enabled or not self._client:
            return None
        bucket = self._resolve_bucket(
            container=container,
            blob_name=blob_name,
            content_type=content_type,
        )
        upload_kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Body": data,
            "Key": blob_name,
            "EnableMD5": False,
        }
        if content_type:
            upload_kwargs["ContentType"] = content_type
        self._client.put_object(**upload_kwargs)
        self._remember_bucket(container=container, blob_name=blob_name, bucket=bucket)
        return blob_name

    def blob_url(self, *, container: str, blob_name: str) -> Optional[str]:
        if not self.enabled:
            return None
        bucket = self._resolve_bucket(container=container, blob_name=blob_name)
        return self._public_blob_url(bucket=bucket, blob_name=blob_name)

    def generate_sas_url(
        self,
        *,
        container: str,
        blob_name: str,
        ttl_minutes: Optional[int] = None,
        require_signed: bool = False,
    ) -> Optional[str]:
        if not self.enabled:
            return None
        bucket = self._resolve_bucket(container=container, blob_name=blob_name)
        if not require_signed:
            return self._public_blob_url(bucket=bucket, blob_name=blob_name)
        if not self._client:
            return None
        expiry_seconds = int((ttl_minutes or self.settings.sas_ttl_minutes) * 60)
        return self._client.get_presigned_url(
            Method="GET",
            Bucket=bucket,
            Key=blob_name,
            Expired=expiry_seconds,
        )

    def upload_temporary_bytes(
        self,
        *,
        container: str,
        blob_name: str,
        data: bytes,
        content_type: Optional[str] = None,
        ttl_minutes: Optional[int] = None,
    ) -> Optional[TemporaryBlobUpload]:
        uploaded_blob = self.upload_bytes(
            container=container,
            blob_name=blob_name,
            data=data,
            content_type=content_type,
        )
        if not uploaded_blob:
            return None
        resolved_bucket = self._resolve_bucket(
            container=container,
            blob_name=uploaded_blob,
            content_type=content_type,
        )
        signed_url = self.generate_sas_url(
            container=resolved_bucket,
            blob_name=uploaded_blob,
            ttl_minutes=ttl_minutes,
            require_signed=True,
        )
        if not signed_url:
            self.delete_blob(container=resolved_bucket, blob_name=uploaded_blob)
            return None
        return TemporaryBlobUpload(
            container=resolved_bucket,
            blob_name=uploaded_blob,
            sas_url=signed_url,
        )

    def delete_blob(
        self,
        *,
        container: str,
        blob_name: str,
    ) -> bool:
        if not self.enabled or not self._client:
            return False
        bucket = self._resolve_bucket(container=container, blob_name=blob_name)
        try:
            self._client.delete_object(Bucket=bucket, Key=blob_name)
            self._blob_bucket_cache.pop((container, blob_name), None)
            self._blob_bucket_cache.pop((bucket, blob_name), None)
            return True
        except Exception as exc:  # pragma: no cover - SDK exception shape is provider-specific.
            status_code = getattr(exc, "status_code", None)
            if status_code == 404 or "NoSuchResource" in str(exc) or "404" in str(exc):
                return False
            raise


def create_storage_service(settings: DeploymentSettings) -> Any:
    provider = (settings.media_storage_provider or "azure").strip().lower()

    if provider in {"azure", "auto"}:
        return AzureBlobStorageService(settings)

    if provider in {"china_cos", "cos", "tencent_cos", "tencent-cos"}:
        if not settings.cos_storage_enabled:
            raise RuntimeError(
                "Tencent COS storage provider selected, but COS_* configuration is incomplete."
            )
        return TencentCosStorageService(settings)

    raise RuntimeError(
        f"Unsupported MEDIA_STORAGE_PROVIDER '{settings.media_storage_provider}'. "
        "Use 'azure' or 'tencent_cos'."
    )


__all__ = [
    "AzureBlobStorageService",
    "TencentCosStorageService",
    "TemporaryBlobUpload",
    "create_storage_service",
]
