import os
import logging
from typing import List, Optional

from EdennCode.ModelFactory.LanguageModelFactory import (
    AzureEndpointConfig,
    AzureMultimodalClient,
    AzureMultimodalClientPool,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import Language
from EdennCode.exceptions import EdennConfigurationError
from EdennCode.env import load_env

load_env()
logger = logging.getLogger(__name__)


def _normalize_endpoint(endpoint: str) -> str:
    normalized = endpoint.strip()
    if normalized and not normalized.startswith("http://") and not normalized.startswith("https://"):
        normalized = "https://" + normalized.lstrip("/")
    return normalized


def _build_client_from_config(config: AzureEndpointConfig) -> AzureMultimodalClient:
    return AzureMultimodalClient(
        azure_endpoint=config.azure_endpoint,
        azure_api_version=config.azure_api_version,
        azure_model=config.azure_model,
        api_key=config.api_key,
        timeout=config.timeout,
        label=config.label,
        api_mode=config.api_mode,
    )


def _additional_azure_endpoint_configs(
    *,
    base_api_version: str,
    base_model: str,
    timeout: int,
) -> List[AzureEndpointConfig]:
    configs: List[AzureEndpointConfig] = []
    for name, raw_endpoint in sorted(os.environ.items()):
        if not name.startswith("AZURE_ENDPOINT_"):
            continue
        suffix = name.removeprefix("AZURE_ENDPOINT_")
        if not suffix:
            continue

        endpoint = _normalize_endpoint(raw_endpoint)
        api_key = os.getenv(f"AZURE_API_KEY_{suffix}", "").strip()
        if not endpoint or not api_key:
            logger.warning(
                "Skipping Azure LLM endpoint suffix %s because endpoint or key is missing.",
                suffix,
            )
            continue

        model = (
            os.getenv(f"AZURE_MODEL_{suffix}", "").strip()
            or os.getenv(f"AZURE_DEPLOYMENT_{suffix}", "").strip()
            or base_model
        )
        api_version = (
            os.getenv(f"AZURE_API_VERSION_{suffix}", "").strip()
            or base_api_version
        )
        api_mode = os.getenv(f"AZURE_API_MODE_{suffix}", "").strip() or "chat_completions"
        label = suffix.lower()
        configs.append(
            AzureEndpointConfig(
                label=label,
                azure_endpoint=endpoint,
                azure_api_version=api_version,
                azure_model=model,
                api_key=api_key,
                api_mode=api_mode,
                timeout=timeout,
            )
        )
    return configs


def build_azure_client() -> AzureMultimodalClient | AzureMultimodalClientPool:
    endpoint = os.getenv("AZURE_ENDPOINT", "").strip()
    model = os.getenv("AZURE_MODEL", "").strip()
    api_key = os.getenv("AZURE_API_KEY", "").strip()
    api_version = os.getenv("AZURE_API_VERSION", "2024-12-01-preview").strip()
    timeout = int(os.getenv("AZURE_API_TIMEOUT", "60"))

    if not endpoint:
        raise EdennConfigurationError("AZURE_ENDPOINT is not set in the environment.", component="azure_setup", operation="build_client")
    endpoint = _normalize_endpoint(endpoint)

    if not api_key:
        raise EdennConfigurationError("AZURE_API_KEY is not set in the environment.", component="azure_setup", operation="build_client")
    if not model:
        raise EdennConfigurationError("AZURE_MODEL (Deployment name) is not set in the environment.", component="azure_setup", operation="build_client")

    primary_config = AzureEndpointConfig(
        label="primary",
        azure_endpoint=endpoint,
        azure_api_version=api_version or "2024-12-01-preview",
        azure_model=model,
        api_key=api_key,
        api_mode=os.getenv("AZURE_API_MODE", "chat_completions"),
        timeout=timeout,
    )
    primary_client = _build_client_from_config(primary_config)

    pool_enabled = os.getenv("AZURE_LLM_POOL_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    if not pool_enabled:
        return primary_client

    additional_configs = _additional_azure_endpoint_configs(
        base_api_version=primary_config.azure_api_version,
        base_model=primary_config.azure_model,
        timeout=timeout,
    )
    clients = [primary_client] + [
        _build_client_from_config(config) for config in additional_configs
    ]
    logger.info(
        "Azure LLM pool enabled with endpoints: %s",
        ", ".join(client.display_name for client in clients),
    )
    return AzureMultimodalClientPool(clients)

def build_provider_a_music_prompt(
        alignment_result: dict,
        *,
        include_vocals: bool = False,
        vocal_gender: str = "female",
        lyrics_language: Optional[str] = None,
) -> str:
    """
    Convert structured global music alignment output into
    a high-quality prompt for ProviderA music generation.
    """

    prompt = alignment_result.get("global_music_prompt", "").strip()
    mood = alignment_result.get("global_mood", "").strip()
    bpm = alignment_result.get("tempo_bpm")
    instruments = alignment_result.get("instruments", [])

    # Build instrument text
    instrument_text = ", ".join(instruments) if instruments else ""

    cleaned_gender = vocal_gender.strip().lower() or "female"
    normalized_language = _normalize_lyrics_language(lyrics_language)
    if include_vocals:
        if normalized_language == Language.CN:
            lyric_instruction = "You must use Chinese lyrics, default to Mainland Mandarin (普通话), with extremely clear pronunciation (吐字清晰标准)."
        elif normalized_language == Language.EN:
            lyric_instruction = "You must use clear English lyrics with crisp diction."
        else:
            lyric_instruction = "Use clear lyrics appropriate for the requested language."
        vocal_direction = (
            f"Feature {cleaned_gender} vocals that enter right from the opening second and stay aligned with the narrative. "
            f"{lyric_instruction}"
        )
    else:
        vocal_direction = "Keep the track purely instrumental with no vocals or vocal chops."

    final_prompt = (
        f"{prompt} "
        f"Overall mood: {mood}. "
        f"Tempo approximately {bpm} BPM. "
        f"Primary instruments: {instrument_text}. "
        f"{vocal_direction} "
        f"Keep it cohesive, emotionally engaging, and production-ready. "
    ).strip()

    return final_prompt


def _normalize_lyrics_language(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = value.strip().upper().replace("-", "_")
    if normalized in {"CN", "ZH", "ZH_CN", "CHINESE", "CHINESE_MAINLAND", "MANDARIN"}:
        return Language.CN
    if normalized in {"EN", "EN_US", "ENGLISH", "ENGLISH_US"}:
        return Language.EN
    return None


import hashlib
import secrets

def hash_str() -> str:
    return  hashlib.sha256(secrets.token_bytes(32)).hexdigest()
