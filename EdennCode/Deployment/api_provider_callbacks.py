from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.error_codes import scrub_provider_names
from EdennCode.Deployment.provider_music_callbacks import (
    record_provider_music_callback,
)


_SUPPORTED_PROVIDER_CALLBACKS = {"provider_c"}


def _expected_secret(provider: str) -> str:
    return (
        os.getenv(f"{provider.upper()}_WEBHOOK_SECRET")
        or os.getenv("PROVIDER_WEBHOOK_SECRET")
        or ""
    ).strip()


def _provided_secret(request: Request) -> str:
    return (
        request.query_params.get("secret")
        or request.headers.get("x-edenn-webhook-secret")
        or request.headers.get("x-webhook-secret")
        or ""
    ).strip()


def _validate_callback_secret(provider: str, request: Request) -> None:
    expected = _expected_secret(provider)
    if expected and _provided_secret(request) != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


async def _read_json_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Callback body must be JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Callback body must be a JSON object.")
    return payload


def create_provider_callbacks_router(context: ApiContext) -> APIRouter:
    router = APIRouter()

    async def handle_provider_callback(provider: str, request: Request) -> dict[str, Any]:
        normalized_provider = (provider or "").strip().lower()
        if normalized_provider not in _SUPPORTED_PROVIDER_CALLBACKS:
            raise HTTPException(status_code=404, detail="Unsupported provider callback.")
        _validate_callback_secret(normalized_provider, request)
        payload = await _read_json_payload(request)
        try:
            event = record_provider_music_callback(
                provider=normalized_provider,
                payload=payload,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=scrub_provider_names(str(exc))
            ) from exc
        context.logger.info(
            "Received %s provider callback: task_id=%s callback_type=%s",
            event.provider,
            event.task_id,
            event.callback_type or "<unknown>",
        )
        return {
            "ok": True,
            "provider": event.provider,
            "task_id": event.task_id,
            "callback_type": event.callback_type,
        }

    # Vendor-facing webhooks, not client API: hidden from the public OpenAPI
    # schema so the vendor route names never appear at /docs or /openapi.json.
    @router.post(
        "/api/v1/provider_callbacks/{provider}",
        summary="Receive music-provider generation callbacks.",
        include_in_schema=False,
    )
    async def provider_callback(provider: str, request: Request) -> dict[str, Any]:
        return await handle_provider_callback(provider, request)

    @router.post(
        "/provider_c/callback",
        include_in_schema=False,
    )
    async def provider_c_callback(request: Request) -> dict[str, Any]:
        return await handle_provider_callback("provider_c", request)

    return router


__all__ = ["create_provider_callbacks_router"]
