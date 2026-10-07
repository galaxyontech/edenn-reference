from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from EdennCode.exceptions import EdennConfigurationError


def _require_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise EdennConfigurationError(f"Environment variable '{key}' must be set for Deployment.", component="deployment_settings", operation="require_env")
    return value


def _optional_env(key: str) -> Optional[str]:
    value = os.getenv(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass
class DeploymentSettings:
    azure_endpoint: str
    azure_model: str
    azure_api_key: str
    azure_api_version: str
    azure_timeout: int

    media_storage_provider: str

    upload_container: str
    output_container: str
    audio_container: str
    llm_image_container: str

    storage_connection_string: Optional[str]
    storage_account_url: Optional[str]
    storage_account_name: Optional[str]
    storage_account_key: Optional[str]
    sas_ttl_minutes: int
    llm_image_sas_ttl_minutes: int
    llm_image_cleanup_delay_seconds: float
    use_managed_identity: bool

    cos_secret_id: Optional[str]
    cos_secret_key: Optional[str]
    cos_region: Optional[str]
    cos_bucket_name: Optional[str]
    cos_bucket_domain: Optional[str]
    cos_video_bucket: Optional[str]
    cos_audio_bucket: Optional[str]
    cos_image_bucket: Optional[str]
    cos_video_domain: Optional[str]
    cos_audio_domain: Optional[str]
    cos_image_domain: Optional[str]

    workdir: Path
    preserve_original_audio: bool
    music_volume: float

    sentry_dsn: Optional[str]
    sentry_traces_sample_rate: float
    sentry_environment: str

    auth_mode: str
    auth_table_namespace: str
    api_admin_secret: Optional[str]
    appinsights_connection_string: Optional[str]
    billing_mode: str
    billing_rmb_per_usd: float
    billing_teaser_days: int
    billing_low_balance_ratio: float
    billing_database_url: Optional[str]
    billing_pg_primary: bool
    firebase_project_id: Optional[str]
    firebase_api_key: str
    firebase_auth_domain: str
    firebase_app_id: str
    firebase_messaging_sender_id: str
    admin_firebase_uids: str
    admin_phone_numbers: str

    @property
    def audio_container_name(self) -> str:
        return self.audio_container or self.output_container

    @property
    def azure_storage_enabled(self) -> bool:
        return bool(self.storage_connection_string or self.storage_account_url)

    @property
    def cos_storage_enabled(self) -> bool:
        return bool(
            self.cos_secret_id
            and self.cos_secret_key
            and self.cos_region
            and self.cos_video_bucket
            and self.cos_audio_bucket
            and self.cos_image_bucket
        )

    @classmethod
    def from_env(cls) -> "DeploymentSettings":
        """
        Load Deployment-specific configuration from environment variables.
        """
        azure_endpoint = _require_env("AZURE_ENDPOINT")
        azure_model = _require_env("AZURE_MODEL")
        azure_api_key = _require_env("AZURE_API_KEY")
        azure_api_version = os.getenv("AZURE_API_VERSION", "2024-12-01-preview").strip() or "2024-12-01-preview"
        azure_timeout = int(os.getenv("AZURE_API_TIMEOUT", "60"))
        media_storage_provider = os.getenv("MEDIA_STORAGE_PROVIDER", "azure").strip().lower() or "azure"

        upload_container = os.getenv("AZURE_STORAGE_UPLOAD_CONTAINER", "user-uploads").strip() or "user-uploads"
        output_container = os.getenv("AZURE_STORAGE_OUTPUT_CONTAINER", "generated-media").strip() or "generated-media"
        audio_container = os.getenv("AZURE_STORAGE_AUDIO_CONTAINER", "").strip() or output_container
        llm_image_container = os.getenv("AZURE_STORAGE_LLM_IMAGE_CONTAINER", "").strip() or upload_container

        storage_connection_string = _optional_env("AZURE_STORAGE_CONNECTION_STRING")
        storage_account_url = _optional_env("AZURE_STORAGE_ACCOUNT_URL")
        storage_account_name = _optional_env("AZURE_STORAGE_ACCOUNT_NAME")
        storage_account_key = _optional_env("AZURE_STORAGE_ACCOUNT_KEY")
        sas_ttl_minutes = int(os.getenv("AZURE_STORAGE_SAS_TTL_MINUTES", "120"))
        llm_image_sas_ttl_minutes = int(os.getenv("AZURE_STORAGE_LLM_IMAGE_SAS_TTL_MINUTES", "5"))
        llm_image_cleanup_delay_seconds = float(os.getenv("AZURE_STORAGE_LLM_IMAGE_CLEANUP_DELAY_SECONDS", "2.0"))
        use_managed_identity = os.getenv("AZURE_STORAGE_USE_MSI", "false").lower() in {"1", "true", "yes"}

        cos_secret_id = _optional_env("COS_SECRET_ID")
        cos_secret_key = _optional_env("COS_SECRET_KEY")
        cos_region = _optional_env("COS_REGION")
        cos_bucket_name = _optional_env("COS_BUCKET_NAME")
        cos_bucket_domain = _optional_env("COS_BUCKET_DOMAIN")
        cos_video_bucket = _optional_env("COS_VIDEO_BUCKET")
        cos_audio_bucket = _optional_env("COS_AUDIO_BUCKET")
        cos_image_bucket = _optional_env("COS_IMAGE_BUCKET")
        cos_video_domain = _optional_env("COS_VIDEO_DOMAIN")
        cos_audio_domain = _optional_env("COS_AUDIO_DOMAIN")
        cos_image_domain = _optional_env("COS_IMAGE_DOMAIN")

        workdir = Path(os.getenv("API_WORKDIR", "api_workdir")).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)

        preserve_original_audio = os.getenv("API_PRESERVE_ORIGINAL_AUDIO", "false").lower() in {"1", "true", "yes"}
        music_volume = float(os.getenv("API_GENERATED_MUSIC_VOLUME", "1.0"))

        sentry_dsn = _optional_env("SENTRY_DSN")
        sentry_traces_sample_rate = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "1.0"))
        sentry_environment = os.getenv("SENTRY_ENVIRONMENT", "development").strip() or "development"

        auth_mode = os.getenv("AUTH_MODE", "").strip()
        auth_table_namespace = os.getenv("AUTH_TABLE_NAMESPACE", "").strip()
        api_admin_secret = _optional_env("API_ADMIN_SECRET")
        billing_mode = os.getenv("BILLING_MODE", "").strip()
        billing_rmb_per_usd = float(os.getenv("BILLING_RMB_PER_USD", "7.0"))
        billing_teaser_days = int(os.getenv("BILLING_TEASER_DAYS", "90"))
        billing_low_balance_ratio = float(
            os.getenv("BILLING_LOW_BALANCE_RATIO", "0.10"))
        # Billing store migration (Table Storage -> Postgres). Two settings,
        # both temporary — delete them once the cutover is done:
        #   BILLING_DATABASE_URL present -> dual-write to Postgres as well
        #   BILLING_PG_PRIMARY truthy    -> Postgres becomes the read side
        # A reversible cutover needs a switch to reverse; deriving the read
        # side from, say, "the database looks populated" would make rollback
        # depend on data rather than on a decision.
        billing_database_url = _optional_env("BILLING_DATABASE_URL")
        billing_pg_primary = os.getenv(
            "BILLING_PG_PRIMARY", "").strip().lower() in {"1", "true", "yes", "on"}
        appinsights_connection_string = _optional_env(
            "APPLICATIONINSIGHTS_CONNECTION_STRING"
        )
        # Presence is the switch: we must know whose tokens to trust, so this
        # is required configuration rather than a feature flag. Unset means the
        # verified-signup endpoint and console sessions are simply not served.
        firebase_project_id = _optional_env("FIREBASE_PROJECT_ID")
        # Firebase's public client identifiers, served to the console at runtime
        # (see console_static). Public by design — they identify the project to
        # Google, they do not authorize anything.
        firebase_api_key = os.getenv("FIREBASE_WEB_API_KEY", "").strip()
        firebase_auth_domain = os.getenv("FIREBASE_AUTH_DOMAIN", "").strip()
        firebase_app_id = os.getenv("FIREBASE_APP_ID", "").strip()
        firebase_messaging_sender_id = os.getenv(
            "FIREBASE_MESSAGING_SENDER_ID", "").strip()
        # Who may use the admin console. Comma-separated; UID is the stronger
        # anchor (a phone number can be taken over at the carrier).
        admin_firebase_uids = os.getenv("ADMIN_FIREBASE_UIDS", "").strip()
        admin_phone_numbers = os.getenv("ADMIN_PHONE_NUMBERS", "").strip()

        return cls(
            azure_endpoint=azure_endpoint,
            azure_model=azure_model,
            azure_api_key=azure_api_key,
            azure_api_version=azure_api_version,
            azure_timeout=azure_timeout,
            media_storage_provider=media_storage_provider,
            upload_container=upload_container,
            output_container=output_container,
            audio_container=audio_container,
            llm_image_container=llm_image_container,
            storage_connection_string=storage_connection_string,
            storage_account_url=storage_account_url,
            storage_account_name=storage_account_name,
            storage_account_key=storage_account_key,
            sas_ttl_minutes=sas_ttl_minutes,
            llm_image_sas_ttl_minutes=llm_image_sas_ttl_minutes,
            llm_image_cleanup_delay_seconds=llm_image_cleanup_delay_seconds,
            use_managed_identity=use_managed_identity,
            cos_secret_id=cos_secret_id,
            cos_secret_key=cos_secret_key,
            cos_region=cos_region,
            cos_bucket_name=cos_bucket_name,
            cos_bucket_domain=cos_bucket_domain,
            cos_video_bucket=cos_video_bucket,
            cos_audio_bucket=cos_audio_bucket,
            cos_image_bucket=cos_image_bucket,
            cos_video_domain=cos_video_domain,
            cos_audio_domain=cos_audio_domain,
            cos_image_domain=cos_image_domain,
            workdir=workdir,
            preserve_original_audio=preserve_original_audio,
            music_volume=music_volume,
            sentry_dsn=sentry_dsn,
            sentry_traces_sample_rate=sentry_traces_sample_rate,
            sentry_environment=sentry_environment,
            auth_mode=auth_mode,
            auth_table_namespace=auth_table_namespace,
            api_admin_secret=api_admin_secret,
            appinsights_connection_string=appinsights_connection_string,
            billing_mode=billing_mode,
            billing_rmb_per_usd=billing_rmb_per_usd,
            billing_teaser_days=billing_teaser_days,
            billing_low_balance_ratio=billing_low_balance_ratio,
            billing_database_url=billing_database_url,
            billing_pg_primary=billing_pg_primary,
            firebase_project_id=firebase_project_id,
            firebase_api_key=firebase_api_key,
            firebase_auth_domain=firebase_auth_domain,
            firebase_app_id=firebase_app_id,
            firebase_messaging_sender_id=firebase_messaging_sender_id,
            admin_firebase_uids=admin_firebase_uids,
            admin_phone_numbers=admin_phone_numbers,
        )


__all__ = ["DeploymentSettings"]
