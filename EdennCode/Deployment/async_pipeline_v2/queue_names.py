from __future__ import annotations

import os
from typing import Any


def async_v2_queue_namespace(settings: Any = None) -> str:
    """Return the optional namespace prefix for async v2 queue names.

    Production queues intentionally default to their plain names, for example
    `video-preprocess` and `music-studio`. Integration tests and isolated
    canaries can set `ASYNC_V2_QUEUE_NAMESPACE` so API-created tasks and worker
    leases use a private queue family without changing task types, stage names,
    or business workflow behavior.
    """

    configured = (
        getattr(settings, "async_v2_queue_namespace", None)
        or os.getenv("ASYNC_V2_QUEUE_NAMESPACE")
        or os.getenv("ASYNC_V2_QUEUE_PREFIX")
        or ""
    )
    return str(configured).strip().strip(":")


def namespaced_queue_name(queue_name: str, settings: Any = None) -> str:
    """Apply the async v2 queue namespace to a base queue name when configured."""

    namespace = async_v2_queue_namespace(settings)
    if not namespace:
        return queue_name
    prefix = f"{namespace}:"
    if queue_name.startswith(prefix):
        return queue_name
    return f"{prefix}{queue_name}"

