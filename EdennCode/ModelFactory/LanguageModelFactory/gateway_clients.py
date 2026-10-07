"""The boundary where the hosted language-model SDK is imported.

The house rule forbids naming an upstream provider anywhere a reader can see.
A vendor SDK is the one thing that cannot be renamed -- it has to be installed
and imported under the name it ships as -- so the rule allows it to be kept
isolated behind an adapter. This module is that adapter for the language-model
gateway: the vendor package is imported here, and callers ask for a client by
role without ever naming it.

Anything outside this module that imports the SDK directly is a boundary
violation, and ``tools/check_provider_names.py`` will say so.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from openai import AsyncAzureOpenAI as _AsyncGatewayClient
from openai import AzureOpenAI as _GatewayClient
from openai import BadRequestError as GatewayBadRequestError
from openai import OpenAI as _DirectClient

__all__ = [
    "AsyncGatewayClient",
    "GatewayClient",
    "GatewayBadRequestError",
    "make_async_gateway_client",
    "make_gateway_client",
    "make_direct_client",
    "embedding_deployment",
]

# Type aliases so callers can annotate without importing the vendor package.
AsyncGatewayClient = _AsyncGatewayClient
GatewayClient = _GatewayClient


def _endpoint(explicit: Optional[str] = None) -> str:
    return explicit or os.environ["MODEL_GATEWAY_ENDPOINT"]


def _key(explicit: Optional[str] = None) -> str:
    return explicit or os.environ["MODEL_GATEWAY_KEY"]


def _version(explicit: Optional[str] = None) -> str:
    return explicit or os.environ.get("MODEL_GATEWAY_API_VERSION", "2024-02-01")


def make_async_gateway_client(
    endpoint: Optional[str] = None,
    api_key: Optional[str] = None,
    api_version: Optional[str] = None,
) -> Any:
    """An async client for the hosted model gateway."""
    return _AsyncGatewayClient(
        azure_endpoint=_endpoint(endpoint),
        api_key=_key(api_key),
        api_version=_version(api_version),
    )


def make_gateway_client(
    endpoint: Optional[str] = None,
    api_key: Optional[str] = None,
    api_version: Optional[str] = None,
) -> Any:
    """A synchronous client for the hosted model gateway."""
    return _GatewayClient(
        azure_endpoint=_endpoint(endpoint),
        api_key=_key(api_key),
        api_version=_version(api_version),
    )


def make_direct_client(**kwargs: Any) -> Any:
    """A client for the provider's own endpoint rather than the gateway.

    Credentials come from the environment the SDK already reads, so this takes
    no key argument by default.
    """
    return _DirectClient(**kwargs)


def embedding_deployment() -> str:
    return os.environ.get("MODEL_GATEWAY_EMBEDDING_DEPLOYMENT", "embed-standard")
