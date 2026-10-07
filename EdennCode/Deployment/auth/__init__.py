"""API-key authentication + per-key usage tracking (P1).

Spec: docs/superpowers/specs/2026-07-16-api-key-auth-usage-tracking-design.md
"""
from EdennCode.Deployment.auth.admin_router import (
    create_admin_router,
)
from EdennCode.Deployment.auth.key_store import (
    ApiKeyRecord,
    ApiKeyStore,
    KeyStoreUnavailable,
    Principal,
    display_prefix,
    generate_api_key,
    hash_api_key,
)
from EdennCode.Deployment.auth.middleware import (
    create_auth_middleware,
    get_principal,
    is_exempt_path,
    resolve_auth_mode,
    resolve_user_id,
)
from EdennCode.Deployment.auth.telemetry import (
    emit_usage_event,
    setup_telemetry,
)
from EdennCode.Deployment.auth.usage_recorder import (
    UsageRecorder,
    get_usage_recorder,
    music_unit_cost_usd,
    record_v2_job_usage,
    resolve_usage_recorder,
    set_usage_recorder_override,
)

__all__ = [
    "ApiKeyRecord",
    "ApiKeyStore",
    "KeyStoreUnavailable",
    "Principal",
    "display_prefix",
    "generate_api_key",
    "hash_api_key",
    "create_admin_router",
    "create_auth_middleware",
    "get_principal",
    "is_exempt_path",
    "resolve_auth_mode",
    "resolve_user_id",
    "emit_usage_event",
    "setup_telemetry",
    "UsageRecorder",
    "get_usage_recorder",
    "music_unit_cost_usd",
    "record_v2_job_usage",
    "resolve_usage_recorder",
    "set_usage_recorder_override",
]
