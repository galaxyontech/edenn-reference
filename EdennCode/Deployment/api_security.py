"""Security-related configuration helpers for the public API surface.

These are small pure functions so the policy decisions — CORS and whether the
error tracker may attach personally identifiable information — are unit-testable
without importing the whole app wiring, and so the defaults are secure by
construction.
"""
from __future__ import annotations

from typing import Optional

_TRUTHY = {"1", "true", "yes", "on"}


def _is_truthy(raw: str) -> bool:
    return raw.strip().lower() in _TRUTHY


def resolve_cors_policy(raw_origins: str) -> Optional[dict]:
    """Resolve ``CORSMiddleware`` kwargs from an ``API_ALLOWED_ORIGINS`` value.

    Returns ``None`` when no origins are configured — the safe default: no CORS
    middleware is added, so only same-origin requests are allowed. A wildcard
    (``"*"``) is honored only for non-credentialed access: credentials are never
    combined with a wildcard origin, which the CORS spec forbids and which would
    otherwise cause the middleware to reflect *any* request origin back *with*
    credentials (an open, credentialed, cross-origin surface).
    """
    origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    if not origins:
        return None
    is_wildcard = origins == ["*"]
    return {
        "allow_origins": ["*"] if is_wildcard else origins,
        "allow_credentials": not is_wildcard,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }


def error_tracker_send_pii(raw_flag: str) -> bool:
    """Whether the error tracker may attach PII (user IP, request data, prompts).

    Defaults to ``False``. Only returns ``True`` when explicitly opted in — e.g.
    when the client's data-handling agreement permits it — via a truthy flag.
    """
    return _is_truthy(raw_flag)


__all__ = ["resolve_cors_policy", "error_tracker_send_pii"]
