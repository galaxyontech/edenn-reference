from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from dotenv import dotenv_values


ENV_PATH = Path("EdennCode/LocalEnv/.env")

SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
ACR_LOGIN_SERVER = "registry.example.invalid"
ACR_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/"
    "resourceGroups/edenn-westus/providers/Microsoft.ContainerRegistry/"
    "registries/exampleregistry"
)

BASE_SECRET_ENV_NAMES = {
    "AZURE_API_KEY",
    "PROVIDER_A_API_KEY",
    "PROVIDER_C_API_KEY",
    "PROVIDER_B_API_KEY",
    "AZURE_STORAGE_ACCOUNT_KEY",
    "EDEN_ADMIN_SECRET",
    "PROVIDER_C_WEBHOOK_SECRET",
    "COS_SECRET_ID",
    "COS_SECRET_KEY",
}

EXTRA_SECRET_KEYS = [
    "DATABASE_URL",
    "TELEMETRY_DATABASE_URL",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGSSLMODE",
    "MODEL_GATEWAY_ALT_API_KEY",
    "SUPABASE_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
]

EXTRA_SECRET_KEY_PATTERNS = [
    re.compile(r"^PROVIDER_A_API_KEY_\d+$"),
    re.compile(r"^PROVIDER_B_API_KEY_\d+$"),
    re.compile(r"^PROVIDER_C_API_KEY_\d+$"),
]

EXTRA_PLAIN_KEYS = [
    "ANNOTATION_DISPATCHER_ENABLED",
    "RECOMMENDATION_PERSISTENCE_ENABLED",
    "PROVIDER_C_BASE_URL",
    "PROVIDER_C_FALLBACK_BASE_URLS",
    "PROVIDER_C_ENDPOINT",
    "PROVIDER_C_STATUS_ENDPOINT",
    "PROVIDER_C_MODEL",
    "PROVIDER_C_CALLBACK_URL",
    "PROVIDER_C_POLL_S",
    "PROVIDER_C_MAX_POLL_S",
    "PROVIDER_C_POLL_BACKOFF",
    "PROVIDER_C_LYRICS_TIMEOUT_S",
    "PROVIDER_C_LYRICS_POLL_S",
    "PROVIDER_C_LYRICS_MAX_POLL_S",
    "PROVIDER_C_LYRICS_POLL_BACKOFF",
    "SUPABASE_URL",
    "BEAT_AWARE_ENABLED",
]


def _quote(value: str) -> str:
    return shlex.quote(value)


def _value(env: dict[str, str | None], key: str, default: str = "") -> str:
    return (env.get(key) or default).strip()


def _export(name: str, value: str) -> str:
    return f"export {name}={_quote(value)}"


def main() -> None:
    env = dotenv_values(ENV_PATH)

    provider_b_key = (
        _value(env, "PROVIDER_B_API_KEY")
        or _value(env, "EDENN_ENHANCED_PROVIDER_B_API_KEY")
        or _value(env, "PROVIDER_B_API_KEY_1")
        or _value(env, "PROVIDER_B_API_KEY_2")
        or "PASTE_PROVIDER_B_API_KEY"
    )
    provider_c_webhook_secret = _value(
        env,
        "PROVIDER_C_WEBHOOK_SECRET",
        "PASTE_PROVIDER_C_WEBHOOK_SECRET",
    )

    extra_secret_values = {
        key: _value(env, key)
        for key in EXTRA_SECRET_KEYS
        if _value(env, key) and key not in BASE_SECRET_ENV_NAMES
    }
    for key, raw_value in env.items():
        value = (raw_value or "").strip()
        if (
            value
            and key not in BASE_SECRET_ENV_NAMES
            and any(pattern.match(key) for pattern in EXTRA_SECRET_KEY_PATTERNS)
        ):
            extra_secret_values[key] = value
    extra_plain_env = {
        key: _value(env, key)
        for key in EXTRA_PLAIN_KEYS
        if _value(env, key) and key not in BASE_SECRET_ENV_NAMES
    }

    lines = [
        _export("TF_VAR_subscription_id", SUBSCRIPTION_ID),
        _export("TF_VAR_acr_login_server", ACR_LOGIN_SERVER),
        _export("TF_VAR_acr_id", ACR_ID),
        _export("TF_VAR_image_tag", "newapp-latest"),
        _export("TF_VAR_storage_account_name", "secondarystorage"),
        _export("TF_VAR_provider_a_api_key", _value(env, "PROVIDER_A_API_KEY")),
        _export("TF_VAR_provider_c_api_key", _value(env, "PROVIDER_C_API_KEY")),
        _export("TF_VAR_provider_b_api_key", provider_b_key),
        _export("TF_VAR_admin_secret", _value(env, "EDEN_ADMIN_SECRET")),
        _export("TF_VAR_provider_c_webhook_secret", provider_c_webhook_secret),
        _export("TF_VAR_cos_secret_id", _value(env, "COS_SECRET_ID")),
        _export("TF_VAR_cos_secret_key", _value(env, "COS_SECRET_KEY")),
        _export(
            "TF_VAR_extra_secret_values",
            json.dumps(extra_secret_values, separators=(",", ":")),
        ),
        _export(
            "TF_VAR_extra_plain_env",
            json.dumps(extra_plain_env, separators=(",", ":")),
        ),
    ]
    print("\n".join(lines))


if __name__ == "__main__":
    main()
