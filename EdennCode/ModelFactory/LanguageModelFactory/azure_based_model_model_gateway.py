import ast
import asyncio
import contextvars
import json
import os
import re
from contextlib import contextmanager
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Iterator, List, Dict, Any, Optional, cast, Tuple

# Bound under a neutral name so nothing OUTSIDE this adapter has to say
# the vendor's class name to reach it — the same pattern gateway_clients
# uses. The carve-out permits the import here; it does not permit the
# name to travel.
from openai import AsyncAzureOpenAI as AsyncGatewayClient, BadRequestError
import time
import logging

from EdennCode.exceptions import (
    EdennContentPolicyViolationError,
    EdennProviderImageFetchTimeoutError,
)

logger = logging.getLogger(__name__)


def _endpoint_host(endpoint: str) -> str:
    return endpoint.replace("https://", "").replace("http://", "").split("/", 1)[0]


def _normalize_api_mode(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower().replace("-", "_")
    if normalized in {"responses", "response", "responses_api"}:
        return "responses"
    return "chat_completions"


# chat-standard / o-series (reasoning) models removed `max_tokens` from chat.completions
# and require `max_completion_tokens` instead. Deployment names usually echo the
# underlying model, but not always — detection here is a best-effort default and
# the request path also falls back on the provider's 400.
_MAX_COMPLETION_TOKEN_MODEL_PREFIXES = ("chat-standard", "gpt5", "o1", "o3", "o4")


def _uses_max_completion_tokens(model: str) -> bool:
    normalized = (model or "").strip().lower()
    return any(
        normalized.startswith(prefix) or f"-{prefix}" in normalized
        for prefix in _MAX_COMPLETION_TOKEN_MODEL_PREFIXES
    )


@dataclass(frozen=True)
class AzureEndpointConfig:
    label: str
    azure_endpoint: str
    azure_api_version: str
    azure_model: str
    api_key: str
    api_mode: str = "chat_completions"
    timeout: int = 30


class AzureMultimodalClient:
    """
    Async wrapper for the model gateway multimodal calls.
    Ready for asyncio.gather batching.
    """

    def __init__(
        self,
        *,
        azure_endpoint: str,
        azure_api_version: str,
        azure_model: str,  # multimodal Deployment name
        api_key: str,
        timeout: int = 30,
        label: str = "primary",
        api_mode: str = "chat_completions",
    ) -> None:
        self.client = AsyncGatewayClient(
            api_version=azure_api_version,
            azure_endpoint=azure_endpoint,
            api_key=api_key,
        )
        self.azure_endpoint = azure_endpoint
        self.azure_api_version = azure_api_version
        self.azure_model = azure_model
        self.timeout = timeout
        self.label = label
        self.api_mode = _normalize_api_mode(api_mode)

    @property
    def display_name(self) -> str:
        return f"{self.label}:{self.azure_model}@{_endpoint_host(self.azure_endpoint)}"

    @staticmethod
    def _coerce_schema_config(json_schema: Dict[str, Any]) -> Dict[str, Any]:
        if json_schema.get("type") == "json_schema" and "json_schema" in json_schema:
            return cast(Dict[str, Any], json_schema.get("json_schema") or {})
        return json_schema

    @classmethod
    def _responses_text_config(cls, json_schema: Dict[str, Any]) -> Any:
        schema_config = cls._coerce_schema_config(json_schema)
        return cast(
            Any,
            {
                "format": {
                    "type": "json_schema",
                    "name": schema_config.get("name", "response_schema"),
                    "schema": schema_config.get("schema", schema_config),
                    "strict": bool(schema_config.get("strict", True)),
                }
            },
        )

    @staticmethod
    def _responses_content_parts(content: Any) -> List[Dict[str, Any]]:
        if isinstance(content, str):
            return [{"type": "input_text", "text": content}]
        if not isinstance(content, list):
            return [{"type": "input_text", "text": str(content)}]

        parts: List[Dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                parts.append({"type": "input_text", "text": str(part)})
                continue

            part_type = part.get("type")
            if part_type in {"text", "input_text"}:
                parts.append({"type": "input_text", "text": str(part.get("text", ""))})
                continue
            if part_type in {"image_url", "input_image"}:
                image_url = part.get("image_url")
                if isinstance(image_url, dict):
                    image_url = image_url.get("url")
                if image_url:
                    parts.append({"type": "input_image", "image_url": str(image_url)})
                continue

            text = part.get("text")
            if text is not None:
                parts.append({"type": "input_text", "text": str(text)})
        return parts

    @classmethod
    def _chat_messages_to_responses_input(cls, messages: List[dict]) -> List[dict]:
        converted: List[dict] = []
        for message in messages:
            role = str(message.get("role") or "user")
            if role == "assistant":
                role = "user"
            converted.append(
                {
                    "role": role,
                    "content": cls._responses_content_parts(message.get("content", "")),
                }
            )
        return converted

    def _map_bad_request_error(
        self,
        exc: BadRequestError,
        *,
        operation: str,
    ) -> BadRequestError | EdennContentPolicyViolationError | EdennProviderImageFetchTimeoutError:
        body = self._extract_error_body(exc)
        error = self._extract_error_section(body)
        inner_error = self._extract_inner_error_section(error)

        provider_message = self._extract_provider_message(
            error=error,
            body=body,
            exc=exc,
        )
        failed_image_url = self._extract_image_download_timeout_url(provider_message)
        if failed_image_url:
            return EdennProviderImageFetchTimeoutError(
                f"the model gateway timed out downloading input image for model {self.azure_model}: {provider_message}",
                provider_name="model_gateway",
                operation=operation,
                status_code=getattr(exc, "status_code", 400),
                failed_image_url=failed_image_url,
                retryable=True,
                context={"model": self.azure_model},
                cause=exc,
            )

        provider_error_code = error.get("code")
        policy_code = inner_error.get("code")
        is_content_filter = (
            provider_error_code == "content_filter"
            or policy_code == "ResponsibleAIPolicyViolation"
        )
        if not is_content_filter:
            return exc

        filter_results = (
            inner_error.get("content_filter_results")
            or inner_error.get("content_filter_result")
            or {}
        )
        if not isinstance(filter_results, dict):
            filter_results = {}

        provider_message = provider_message or "The request was blocked by the model gateway content policy."
        return EdennContentPolicyViolationError(
            f"the model gateway content policy violation for model {self.azure_model}: {provider_message}",
            provider_name="model_gateway",
            operation=operation,
            status_code=getattr(exc, "status_code", 400),
            policy_code=str(policy_code or ""),
            provider_error_code=str(provider_error_code or ""),
            param=error.get("param"),
            filter_results=filter_results,
            context={
                "model": self.azure_model,
                "inner_error": inner_error,
            },
            cause=exc,
        )

    @classmethod
    def map_bad_request_like_error(
        cls,
        exc: Exception,
        *,
        operation: str,
        azure_model: str,
    ) -> Exception | EdennContentPolicyViolationError | EdennProviderImageFetchTimeoutError:
        body = cls._extract_error_body(exc)
        error = cls._extract_error_section(body)
        inner_error = cls._extract_inner_error_section(error)

        provider_message = cls._extract_provider_message(
            error=error,
            body=body,
            exc=exc,
        )
        failed_image_url = cls._extract_image_download_timeout_url(provider_message)
        if failed_image_url:
            status_code = getattr(exc, "status_code", None)
            if not isinstance(status_code, int):
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", 400)
            return EdennProviderImageFetchTimeoutError(
                f"the model gateway timed out downloading input image for model {azure_model}: {provider_message}",
                provider_name="model_gateway",
                operation=operation,
                status_code=status_code,
                failed_image_url=failed_image_url,
                retryable=True,
                context={"model": azure_model},
                cause=exc,
            )

        provider_error_code = error.get("code")
        policy_code = inner_error.get("code")
        is_content_filter = (
            provider_error_code == "content_filter"
            or policy_code == "ResponsibleAIPolicyViolation"
        )
        if not is_content_filter:
            return exc

        filter_results = (
            inner_error.get("content_filter_results")
            or inner_error.get("content_filter_result")
            or {}
        )
        if not isinstance(filter_results, dict):
            filter_results = {}

        status_code = getattr(exc, "status_code", None)
        if not isinstance(status_code, int):
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", 400)

        provider_message = provider_message or "The request was blocked by the model gateway content policy."
        return EdennContentPolicyViolationError(
            f"the model gateway content policy violation for model {azure_model}: {provider_message}",
            provider_name="model_gateway",
            operation=operation,
            status_code=status_code,
            policy_code=str(policy_code or ""),
            provider_error_code=str(provider_error_code or ""),
            param=error.get("param"),
            filter_results=filter_results,
            context={
                "model": azure_model,
                "inner_error": inner_error,
            },
            cause=exc,
        )

    @classmethod
    def _extract_error_body(cls, exc: Exception) -> Dict[str, Any]:
        payload_candidates = [
            getattr(exc, "body", None),
            getattr(exc, "message", None),
            str(exc),
        ]

        response = getattr(exc, "response", None)
        if response is not None:
            try:
                payload_candidates.insert(1, response.json())
            except Exception:
                pass
            try:
                payload_candidates.insert(2, response.text)
            except Exception:
                pass

        for candidate in payload_candidates:
            payload = cls._coerce_mapping(candidate)
            if payload:
                return payload

        return {}

    @staticmethod
    def _extract_error_section(body: Dict[str, Any]) -> Dict[str, Any]:
        error = body.get("error") or {}
        if isinstance(error, dict):
            return error
        return body if isinstance(body, dict) else {}

    @classmethod
    def _is_max_tokens_unsupported_error(cls, exc: Exception) -> bool:
        """True when the provider rejected `max_tokens` and wants
        `max_completion_tokens` instead (chat-standard / o-series chat.completions)."""
        body = cls._extract_error_body(exc)
        error = cls._extract_error_section(body)
        param = str(error.get("param", "")).lower()
        code = str(error.get("code", "")).lower()
        message = str(error.get("message", "") or body.get("message", "")).lower()
        if "max_completion_tokens" in message:
            return True
        if param == "max_tokens" and ("unsupported" in code or "unsupported" in message):
            return True
        return "max_tokens" in message and "not supported" in message

    @staticmethod
    def _extract_inner_error_section(error: Dict[str, Any]) -> Dict[str, Any]:
        inner_error = (
            error.get("inner_error")
            or error.get("innerError")
            or error.get("innererror")
            or {}
        )
        if isinstance(inner_error, dict):
            return inner_error
        return {}

    @classmethod
    def _extract_provider_message(
        cls,
        *,
        error: Dict[str, Any],
        body: Dict[str, Any],
        exc: Exception,
    ) -> str:
        for candidate in (
            error.get("message"),
            body.get("message"),
            getattr(exc, "message", None),
            str(exc),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""

    @classmethod
    def _coerce_mapping(cls, value: Any) -> Dict[str, Any] | None:
        if isinstance(value, dict):
            return value
        if isinstance(value, Mapping):
            return dict(value)
        if not isinstance(value, str):
            return None

        stripped = value.strip()
        if not stripped:
            return None

        parse_candidates = [stripped]
        _prefix, separator, suffix = stripped.partition(" - ")
        if separator and suffix:
            parse_candidates.append(suffix.strip())

        for text in parse_candidates:
            for parser in (json.loads, ast.literal_eval):
                try:
                    parsed = parser(text)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return parsed
                if isinstance(parsed, Mapping):
                    return dict(parsed)
        return None

    @staticmethod
    def _extract_image_download_timeout_url(message: str) -> str:
        lowered = message.lower()
        has_image_signal = "image" in lowered
        has_fetch_signal = any(
            token in lowered
            for token in ("download", "fetch", "retrieve", "read", "access")
        )
        has_failure_signal = any(
            token in lowered
            for token in ("unable", "failed", "could not", "cannot", "can't", "timed out", "timeout", "error")
        )
        if not (has_image_signal and has_fetch_signal and has_failure_signal):
            return ""
        match = re.search(r"https?://\S+", message, flags=re.IGNORECASE)
        if not match:
            return ""
        return match.group(0).strip().rstrip(".,;:'\")]}")

    async def complete_messages(
        self,
        messages: List[dict],
        *,
        json_schema: Dict[str, Any],
        max_tokens: int = 4000,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Async: send chat messages and enforce JSON output using the provided JSON schema.
        Returns the parsed JSON object (dict).
        """
        if self.api_mode == "responses":
            return await self.complete_responses_input(
                self._chat_messages_to_responses_input(messages),
                json_schema=json_schema,
                max_output_tokens=max_tokens,
            )

        # Cast to Any so we don't fight the SDK's internal ResponseFormat* union types.
        response_format = cast(
            Any,
            {
                "type": "json_schema",
                "json_schema": json_schema,
            },
        )

        start_time = time.perf_counter()
        token_param = (
            "max_completion_tokens"
            if _uses_max_completion_tokens(self.azure_model)
            else "max_tokens"
        )
        request_params = {
            "model": self.azure_model,
            "messages": messages,
            token_param: max_tokens,
            "timeout": self.timeout,
            "response_format": response_format,
        }

        try:
            response = await self.client.chat.completions.create(**request_params)
        except BadRequestError as exc:
            # chat-standard / o-series deployments whose name didn't trip detection still
            # reject `max_tokens` at request time — swap the param and retry once.
            if token_param == "max_tokens" and self._is_max_tokens_unsupported_error(exc):
                logger.warning(
                    "Model %s rejected 'max_tokens'; retrying with 'max_completion_tokens'.",
                    self.display_name,
                )
                request_params.pop("max_tokens", None)
                request_params["max_completion_tokens"] = max_tokens
                try:
                    response = await self.client.chat.completions.create(**request_params)
                except BadRequestError as retry_exc:
                    mapped_error = self._map_bad_request_error(
                        retry_exc,
                        operation="chat.completions.create",
                    )
                    if mapped_error is not retry_exc:
                        raise mapped_error from retry_exc
                    raise
            else:
                mapped_error = self._map_bad_request_error(
                    exc,
                    operation="chat.completions.create",
                )
                if mapped_error is not exc:
                    raise mapped_error from exc
                raise
        duration = time.perf_counter() - start_time

        if not response.choices:
            return {}, {}

        message = response.choices[0].message
        content = getattr(message, "content", "")

        if not content:
            return {}, {}

        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
            logger.info(
                "GPT request to %s took %.2fs. Usage: %s",
                self.display_name,
                duration,
                usage,
            )

        # Azure/ModelGateway returns JSON as a string; parse it
        try:
            return json.loads(content), usage
        except json.JSONDecodeError:
            # Fallback: wrap raw content
            return {"_raw": content}, usage

    async def complete_responses_input(
        self,
        input_payload: List[dict],
        *,
        json_schema: Dict[str, Any],
        max_output_tokens: int = 1200,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Async: send Responses API input (e.g., with input_video) and enforce JSON schema output.
        Returns parsed JSON object and usage.
        """
        schema_config = self._coerce_schema_config(json_schema)
        response_text_config = self._responses_text_config(json_schema)

        start_time = time.perf_counter()
        request_params = {
            "model": self.azure_model,
            "input": input_payload,
            "max_output_tokens": max_output_tokens,
            "timeout": self.timeout,
        }
        try:
            response = await self.client.responses.create(
                **request_params,
                text=response_text_config,
            )
        except BadRequestError as exc:
            mapped_error = self._map_bad_request_error(
                exc,
                operation="responses.create",
            )
            if mapped_error is not exc:
                raise mapped_error from exc
            raise
        except TypeError as exc:
            if "unexpected keyword argument 'text'" not in str(exc):
                raise
            # Compatibility fallback for older SDK variants using response_format.
            try:
                response = await self.client.responses.create(
                    **request_params,
                    response_format={
                        "type": "json_schema",
                        "json_schema": schema_config,
                    },
                )
            except BadRequestError as inner_exc:
                mapped_error = self._map_bad_request_error(
                    inner_exc,
                    operation="responses.create",
                )
                if mapped_error is not inner_exc:
                    raise mapped_error from inner_exc
                raise
        duration = time.perf_counter() - start_time

        usage: Dict[str, Any] = {}
        resp_usage = getattr(response, "usage", None)
        if resp_usage:
            usage = {
                "prompt_tokens": getattr(resp_usage, "input_tokens", 0),
                "completion_tokens": getattr(resp_usage, "output_tokens", 0),
                "total_tokens": getattr(resp_usage, "total_tokens", 0),
            }
            logger.info(
                "Responses request to %s took %.2fs. Usage: %s",
                self.display_name,
                duration,
                usage,
            )

        output_text = getattr(response, "output_text", None)
        if output_text:
            try:
                return json.loads(output_text), usage
            except json.JSONDecodeError:
                return {"_raw": output_text}, usage

        output = getattr(response, "output", None)
        if output and len(output) > 0:
            block = output[0]
            content = getattr(block, "content", None)
            if content and len(content) > 0:
                text = getattr(content[0], "text", None)
                if text:
                    try:
                        return json.loads(text), usage
                    except json.JSONDecodeError:
                        return {"_raw": text}, usage

        return {}, usage


class AzureMultimodalClientPool:
    """
    Primary-first pool with sticky per-job binding and endpoint failover.

    The pool deliberately exposes the same methods used by workflow stages so
    existing stages can keep a single llm_model_client reference.
    """

    def __init__(self, clients: List[AzureMultimodalClient]) -> None:
        if not clients:
            raise ValueError("AzureMultimodalClientPool requires at least one client.")
        self._clients = clients
        self._healthy_labels = {client.label for client in clients}
        self._disabled_reasons: Dict[str, str] = {}
        self._active_client: contextvars.ContextVar[Optional[AzureMultimodalClient]] = (
            contextvars.ContextVar("edenn_active_azure_multimodal_client", default=None)
        )

    @property
    def azure_model(self) -> str:
        client = self._active_client.get() or self._clients[0]
        return client.azure_model

    @property
    def label(self) -> str:
        client = self._active_client.get() or self._clients[0]
        return client.label

    @property
    def display_name(self) -> str:
        client = self._active_client.get() or self._clients[0]
        return client.display_name

    def disabled_reasons(self) -> Dict[str, str]:
        return dict(self._disabled_reasons)

    def _healthy_clients(self) -> List[AzureMultimodalClient]:
        return [
            client for client in self._clients if client.label in self._healthy_labels
        ]

    def _choose_client(self, preferred_label: Optional[str] = None) -> AzureMultimodalClient:
        forced_label = (os.getenv("AZURE_LLM_POOL_FORCE_LABEL", "") or "").strip()
        label = (preferred_label or forced_label).strip()
        healthy = self._healthy_clients()
        if not healthy:
            raise RuntimeError(
                f"All Azure LLM endpoints are disabled: {self._disabled_reasons}"
            )

        if label:
            for client in healthy:
                if client.label == label:
                    return client
            raise RuntimeError(
                f"Requested Azure LLM endpoint '{label}' is not available. "
                f"Healthy endpoints: {[client.label for client in healthy]}; "
                f"disabled: {self._disabled_reasons}"
            )

        # Keep the configured primary endpoint as the default path. Additional
        # endpoints are fallback capacity, not equal-weight latency peers.
        return healthy[0]

    @contextmanager
    def select_for_job(
        self,
        *,
        job_id: Optional[str] = None,
        preferred_label: Optional[str] = None,
    ) -> Iterator[AzureMultimodalClient]:
        client = self._choose_client(preferred_label)
        logger.info(
            "Selected Azure LLM endpoint for job %s: %s",
            job_id or "<unknown>",
            client.display_name,
        )
        token = self._active_client.set(client)
        try:
            yield client
        finally:
            self._active_client.reset(token)

    def _active_or_next(self) -> AzureMultimodalClient:
        client = self._active_client.get()
        if client is not None and client.label in self._healthy_labels:
            return client
        return self._choose_client()

    @staticmethod
    def _should_disable_endpoint(exc: Exception) -> bool:
        if isinstance(
            exc,
            (
                EdennContentPolicyViolationError,
                EdennProviderImageFetchTimeoutError,
            ),
        ):
            return False
        text = str(exc).lower()
        permanent_markers = (
            "unsupported parameter",
            "enabled only for api-version",
            "deploymentnotfound",
            "deployment not found",
            "resource not found",
            "operation not supported",
            "does not exist",
            "invalid api-version",
            "unauthorized",
            "authentication",
            "permission",
        )
        return any(marker in text for marker in permanent_markers)

    def _disable_endpoint(self, client: AzureMultimodalClient, exc: Exception) -> None:
        if client.label not in self._healthy_labels:
            return
        self._healthy_labels.remove(client.label)
        reason = f"{type(exc).__name__}: {str(exc)[:300]}"
        self._disabled_reasons[client.label] = reason
        logger.error(
            "Disabled Azure LLM endpoint %s after failure: %s",
            client.display_name,
            reason,
        )

    async def _call_with_endpoint_failover(
        self,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        first_client = self._active_or_next()
        attempted_labels = {first_client.label}
        try:
            method = getattr(first_client, method_name)
            return await method(*args, **kwargs)
        except Exception as exc:
            if not self._should_disable_endpoint(exc):
                raise
            self._disable_endpoint(first_client, exc)
            fallback_clients = [
                client for client in self._healthy_clients()
                if client.label not in attempted_labels
            ]
            if not fallback_clients:
                raise
            fallback = self._choose_client()
            logger.warning(
                "Retrying Azure LLM call on fallback endpoint %s after %s failed",
                fallback.display_name,
                first_client.display_name,
            )
            method = getattr(fallback, method_name)
            return await method(*args, **kwargs)

    async def complete_messages(
        self,
        messages: List[dict],
        *,
        json_schema: Dict[str, Any],
        max_tokens: int = 4000,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        return await self._call_with_endpoint_failover(
            "complete_messages",
            messages,
            json_schema=json_schema,
            max_tokens=max_tokens,
        )

    async def complete_responses_input(
        self,
        input_payload: List[dict],
        *,
        json_schema: Dict[str, Any],
        max_output_tokens: int = 1200,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        return await self._call_with_endpoint_failover(
            "complete_responses_input",
            input_payload,
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
        )
