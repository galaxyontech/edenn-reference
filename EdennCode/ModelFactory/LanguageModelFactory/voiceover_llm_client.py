from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import AzureMultimodalClient

logger = logging.getLogger(__name__)


class VoiceoverLLMClient(ABC):
    """
    Base interface for voiceover-specific LLM clients.
    """

    @abstractmethod
    def complete_messages(
        self,
        messages: List[dict],
        *,
        json_schema: Dict[str, Any],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        raise NotImplementedError


class ModelGatewayVoiceoverClient(VoiceoverLLMClient):
    """
    Synchronous ModelGateway client wrapper for voiceover planning.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: Optional[str] = None,
        organization: Optional[str] = None,
        timeout: int = 60,
    ) -> None:
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            organization=organization,
            timeout=timeout,
        )

    @classmethod
    def from_env(cls) -> "ModelGatewayVoiceoverClient":
        api_key = _require_env("MODEL_GATEWAY_API_KEY")
        base_url = _optional_env("MODEL_GATEWAY_BASE_URL")
        organization = _optional_env("MODEL_GATEWAY_ORG")
        timeout = int(os.getenv("MODEL_GATEWAY_TIMEOUT", "60"))
        return cls(
            api_key=api_key,
            base_url=base_url,
            organization=organization,
            timeout=timeout,
        )

    def complete_messages(
        self,
        messages: List[dict],
        *,
        json_schema: Dict[str, Any],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        response = self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_schema", "json_schema": json_schema},
        )

        if not response.choices:
            return {}

        message = response.choices[0].message
        content = getattr(message, "content", "")

        if response.usage:
            logger.info(
                "Voiceover planning usage: prompt=%s completion=%s total=%s",
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
                response.usage.total_tokens,
            )

        if not content:
            return {}

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {"_raw": content}


class AzureVoiceoverClient(VoiceoverLLMClient):
    """
    the model gateway client wrapper for voiceover planning.
    """

    def __init__(self, *, azure_client: AzureMultimodalClient) -> None:
        self.azure_client = azure_client

    @classmethod
    def from_env(cls) -> "AzureVoiceoverClient":
        model = (os.getenv("AZURE_VOICEOVER_MODEL") or os.getenv("AZURE_MODEL", "")).strip()
        azure_client = AzureMultimodalClient.from_env(azure_model=model or None)
        return cls(azure_client=azure_client)

    def complete_messages(
        self,
        messages: List[dict],
        *,
        json_schema: Dict[str, Any],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        payload, _usage = self.azure_client.complete_messages_sync(
            messages,
            json_schema=json_schema,
            max_tokens=max_tokens,
            temperature=temperature,
            model_override=model,
        )
        return payload


def resolve_voiceover_model_config(
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> Tuple[str, float, int]:
    resolved_model = (
        model
        or os.getenv("AZURE_VOICEOVER_MODEL")
        or os.getenv("AZURE_MODEL")
        or os.getenv("MODEL_GATEWAY_VOICEOVER_MODEL")
        or os.getenv("MODEL_GATEWAY_MODEL")
        or ""
    ).strip()
    if not resolved_model:
        raise RuntimeError(
            "Set AZURE_VOICEOVER_MODEL/AZURE_MODEL (or MODEL_GATEWAY_VOICEOVER_MODEL/MODEL_GATEWAY_MODEL) or pass model=."
        )

    resolved_temperature = float(
        os.getenv("VOICEOVER_TEMPERATURE")
        or os.getenv("AZURE_VOICEOVER_TEMPERATURE")
        or os.getenv("MODEL_GATEWAY_VOICEOVER_TEMPERATURE", "0.4")
    )
    if temperature is not None:
        resolved_temperature = float(temperature)

    resolved_max_tokens = int(
        os.getenv("VOICEOVER_MAX_TOKENS")
        or os.getenv("AZURE_VOICEOVER_MAX_TOKENS")
        or os.getenv("MODEL_GATEWAY_VOICEOVER_MAX_TOKENS", "1800")
    )
    if max_tokens is not None:
        resolved_max_tokens = int(max_tokens)

    return resolved_model, resolved_temperature, resolved_max_tokens


def resolve_default_voiceover_client() -> VoiceoverLLMClient:
    if _has_azure_voiceover_env():
        return AzureVoiceoverClient.from_env()
    return ModelGatewayVoiceoverClient.from_env()


def _has_azure_voiceover_env() -> bool:
    endpoint = os.getenv("AZURE_ENDPOINT", "").strip()
    api_key = os.getenv("AZURE_API_KEY", "").strip()
    model = (os.getenv("AZURE_VOICEOVER_MODEL") or os.getenv("AZURE_MODEL", "")).strip()
    return bool(endpoint and api_key and model)


def _require_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(f"Set {key} in your environment or .env.")
    return value


def _optional_env(key: str) -> Optional[str]:
    value = os.getenv(key)
    if value is None:
        return None
    value = value.strip()
    return value or None
