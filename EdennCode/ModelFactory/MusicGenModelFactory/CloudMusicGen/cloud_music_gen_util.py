import logging
import os
import re
from pathlib import Path

from EdennCode.exceptions import EdennConfigurationError
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_a_compose import ProviderALyrics
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_music import ProviderBMusicProvider
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_pg_lock import (
    ProviderBKeyLock,
    NullKeyLock,
    PgKeyLock,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_key_health import (
    InMemoryProviderBKeyHealthStore,
    ProviderBKeyHealthStore,
    PgProviderBKeyHealthStore,
)

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_A_API_KEY_ENV = "PROVIDER_A_API_KEY"
NUMBERED_PROVIDER_A_API_KEY_ENV_HINT = "PROVIDER_A_API_KEY_1..N"
_NUMBERED_PROVIDER_A_API_KEY_RE = re.compile(r"^PROVIDER_A_API_KEY_(\d+)$")
EDENN_ENHANCED_PROVIDER_B_API_KEY_ENV = "EDENN_ENHANCED_PROVIDER_B_API_KEY"
DEFAULT_PROVIDER_B_API_KEY_ENV = "PROVIDER_B_API_KEY"
NUMBERED_PROVIDER_B_API_KEY_ENV_HINT = "PROVIDER_B_API_KEY_1..N"
_NUMBERED_PROVIDER_B_API_KEY_RE = re.compile(r"^PROVIDER_B_API_KEY_(\d+)$")
PROVIDER_B_PG_LOCK_ENABLED_ENV = "PROVIDER_B_PG_LOCK_ENABLED"


def _provider_b_pg_coordination_enabled_from_env() -> bool:
    raw = (os.getenv(PROVIDER_B_PG_LOCK_ENABLED_ENV) or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _has_numbered_provider_b_api_key_from_env() -> bool:
    for env_name, raw_value in os.environ.items():
        if (
            _NUMBERED_PROVIDER_B_API_KEY_RE.match(env_name)
            and raw_value.strip()
        ):
            return True
    return False


def _provider_a_api_keys_from_env() -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    seen_values: set[str] = set()

    primary = os.getenv(DEFAULT_PROVIDER_A_API_KEY_ENV, "").strip()
    if primary:
        keys.append((DEFAULT_PROVIDER_A_API_KEY_ENV, primary))
        seen_values.add(primary)

    numbered_keys: list[tuple[int, str, str]] = []
    for env_name, raw_value in os.environ.items():
        match = _NUMBERED_PROVIDER_A_API_KEY_RE.match(env_name)
        if match is None:
            continue
        key = raw_value.strip()
        if not key or key in seen_values:
            continue
        numbered_keys.append((int(match.group(1)), env_name, key))
        seen_values.add(key)
    for _index, env_name, key in sorted(numbered_keys):
        keys.append((env_name, key))

    return keys


def build_music_provider() -> ProviderALyrics:
    api_keys = _provider_a_api_keys_from_env()
    if not api_keys:
        allowed_envs = ", ".join(
            [
                DEFAULT_PROVIDER_A_API_KEY_ENV,
                NUMBERED_PROVIDER_A_API_KEY_ENV_HINT,
            ]
        )
        raise EdennConfigurationError(
            f"edenn_basic requires an ProviderA API key. Set one of: {allowed_envs}.",
            component="provider_a",
            operation="initialize",
        )
    return ProviderALyrics(
        api_keys=api_keys,
        use_stream=False,
        default_output_dir=Path(
            os.getenv("OUTPUT_AUDIO_DIR", "outputs/audio")),
        timeout=int(os.getenv("PROVIDER_A_TIMEOUT", "120")),
    )


def _build_provider_b_key_lock_from_env(
    *,
    pg_enabled: bool | None = None,
) -> ProviderBKeyLock:
    """Build the per-key advisory lock. Fails closed when enabled but PG init fails.

    - Default: enabled. Production must have a reachable Postgres.
    - ``PROVIDER_B_PG_LOCK_ENABLED=false``: returns a NullKeyLock (rollback / local
      dev escape hatch). Collision risk reverts to the pre-locking baseline.
    - Enabled but PG initialization raises: surfaces EdennConfigurationError so
      the process fails to start instead of silently degrading to no-locking.
    """
    raw = (os.getenv(PROVIDER_B_PG_LOCK_ENABLED_ENV) or "true").strip().lower()
    enabled = _provider_b_pg_coordination_enabled_from_env() if pg_enabled is None else pg_enabled
    if not enabled:
        logger.info(
            "ProviderB PG lock disabled via %s=%s; using NullKeyLock",
            PROVIDER_B_PG_LOCK_ENABLED_ENV, raw,
        )
        return NullKeyLock()

    try:
        return PgKeyLock.from_env()
    except Exception as exc:
        logger.error(
            "ProviderB PG lock is enabled but initialization failed: %s",
            exc,
            exc_info=True,
        )
        raise EdennConfigurationError(
            "ProviderB PG lock is enabled but Postgres lock initialization failed. "
            "Fix DATABASE_URL / PG* env, or set "
            f"{PROVIDER_B_PG_LOCK_ENABLED_ENV}=false for explicit rollback.",
            component="provider_b_pg_lock",
            operation="initialize",
        ) from exc


def _build_provider_b_key_health_store_from_env(
    *,
    pg_enabled: bool | None = None,
) -> ProviderBKeyHealthStore:
    """Build rate-limit cooldown storage.

    When PG coordination is enabled, cooldowns must also be shared through PG
    so all replicas and local subprocesses skip the same recently rate-limited
    key. When the explicit rollback switch disables PG coordination, use
    process-local memory to preserve local-dev behavior.
    """
    raw = (os.getenv(PROVIDER_B_PG_LOCK_ENABLED_ENV) or "true").strip().lower()
    enabled = _provider_b_pg_coordination_enabled_from_env() if pg_enabled is None else pg_enabled
    if not enabled:
        logger.info(
            "ProviderB key health uses process-local memory because %s=%s",
            PROVIDER_B_PG_LOCK_ENABLED_ENV,
            raw,
        )
        return InMemoryProviderBKeyHealthStore()

    try:
        return PgProviderBKeyHealthStore.from_env()
    except Exception as exc:
        logger.error(
            "ProviderB PG key health is enabled but initialization failed: %s",
            exc,
            exc_info=True,
        )
        raise EdennConfigurationError(
            "ProviderB PG key health is enabled but Postgres cooldown initialization failed. "
            "Fix DATABASE_URL / PG* env, or set "
            f"{PROVIDER_B_PG_LOCK_ENABLED_ENV}=false for explicit rollback.",
            component="provider_b_key_health",
            operation="initialize",
        ) from exc


def _build_provider_b_coordination_from_env() -> tuple[ProviderBKeyLock, ProviderBKeyHealthStore]:
    pg_enabled = _provider_b_pg_coordination_enabled_from_env()
    key_lock = _build_provider_b_key_lock_from_env(pg_enabled=pg_enabled)
    try:
        key_health_store = _build_provider_b_key_health_store_from_env(
            pg_enabled=pg_enabled,
        )
    except Exception:
        close = getattr(key_lock, "close", None)
        if callable(close):
            close()
        raise
    return key_lock, key_health_store


def build_edenn_enhanced_music_provider() -> ProviderBMusicProvider:
    # Let _ProviderBKeyPool.from_env() handle all PROVIDER_B_API_KEY_N / PROVIDER_B_API_KEY
    # precedence. Numbered keys are preferred over the legacy PROVIDER_B_API_KEY fallback.
    has_pool_keys = bool(
        os.getenv(DEFAULT_PROVIDER_B_API_KEY_ENV, "").strip()
        or _has_numbered_provider_b_api_key_from_env()
    )
    if has_pool_keys:
        key_lock, key_health_store = _build_provider_b_coordination_from_env()
        return ProviderBMusicProvider(
            key_lock=key_lock,
            key_health_store=key_health_store,
        )

    explicit_key = os.getenv(EDENN_ENHANCED_PROVIDER_B_API_KEY_ENV, "").strip()
    if explicit_key:
        key_lock, key_health_store = _build_provider_b_coordination_from_env()
        return ProviderBMusicProvider(
            api_key=explicit_key,
            key_lock=key_lock,
            key_health_store=key_health_store,
        )

    allowed_envs = ", ".join(
        [
            NUMBERED_PROVIDER_B_API_KEY_ENV_HINT,
            EDENN_ENHANCED_PROVIDER_B_API_KEY_ENV,
            DEFAULT_PROVIDER_B_API_KEY_ENV,
        ]
    )
    raise EdennConfigurationError(
        f"edenn_enhanced requires a ProviderB API key. Set one of: {allowed_envs}.",
        component="provider_b",
        operation="initialize",
    )
