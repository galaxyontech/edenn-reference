from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, FrozenSet

from EdennCode.exceptions import (
    EdennApiError,
    EdennConfigurationError,
    EdennContentPolicyViolationError,
    EdennError,
    EdennImageDurationTooShortError,
    EdennInputVideoTooLargeError,
    EdennInputVideoTooLongError,
    EdennInputVideoTooShortError,
    EdennLyricsUnsupportedModelspecError,
    EdennMediaProcessingError,
    EdennProviderAuthenticationError,
    EdennProviderError,
    EdennProviderRateLimitError,
    EdennProviderResponseError,
    EdennProviderTimeoutError,
    EdennStorageError,
    EdennValidationError,
)
from EdennCode.Deployment import provider_vocabulary


@dataclass(frozen=True)
class EdennErrorCode:
    """A 5-digit error code entry for enterprise SLA triage.

    Scheme: AABCC
        AA = service domain (which part of the system)
        B  = failure mode   (how it failed)
        CC = specific variant
    """

    code: int
    message: str
    retryable: bool
    http_status: int


# ── 10xxx  API Gateway / Input ──────────────────────────────────────

INPUT_INVALID = EdennErrorCode(
    10001,
    "The request could not be processed. Please review the input and try again.",
    False, 400,
)
INPUT_BAD_FORMAT = EdennErrorCode(
    10002,
    "The uploaded file could not be read. Please check the format and try again.",
    False, 400,
)
INPUT_BAD_MODELSPEC = EdennErrorCode(
    10003,
    "The requested model specification is not available.",
    False, 400,
)
INPUT_CONFLICT = EdennErrorCode(
    10004,
    "The request contains conflicting parameters. Please review and try again.",
    False, 400,
)
INPUT_VIDEO_TOO_LARGE = EdennErrorCode(
    10005,
    "The source video is too large. Please provide a video no larger than 300MB.",
    False, 400,
)
INPUT_VIDEO_TOO_LONG = EdennErrorCode(
    10006,
    "The source video is too long. Please provide a video no longer than 150 seconds.",
    False, 400,
)
INPUT_VIDEO_TOO_SHORT = EdennErrorCode(
    10007,
    "The source video is too short. Please provide a video longer than 15 seconds.",
    False, 400,
)
INPUT_IMAGE_DURATION_TOO_SHORT = EdennErrorCode(
    10008,
    "Each image must be displayed for at least 3 seconds.",
    False, 400,
)
INPUT_LYRICS_UNSUPPORTED_MODELSPEC = EdennErrorCode(
    10009,
    "Lyric direction is not supported by this model specification. "
    "To use lyrics_prompt, set modelspec=edenn_enhanced or modelspec=edenn_studio.",
    False, 400,
)

# ── 11xxx  Content Safety ───────────────────────────────────────────

CONTENT_POLICY = EdennErrorCode(
    11001,
    "The request was flagged by our content safety system. "
    "Please modify your input and try again.",
    False, 400,
)

# ── 20xxx  AI Analysis (LLM) ───────────────────────────────────────

AI_RATE_LIMIT = EdennErrorCode(
    20100,
    "The service is currently at capacity. Please try again shortly.",
    True, 503,
)
AI_TIMEOUT = EdennErrorCode(
    20200,
    "The request took too long to complete. Please try again.",
    True, 504,
)
AI_AUTH = EdennErrorCode(
    20300,
    "A required service is temporarily unavailable. Please try again later.",
    True, 502,
)
AI_UNAVAILABLE = EdennErrorCode(
    20400,
    "A required service is temporarily unavailable. Please try again later.",
    True, 502,
)
AI_BAD_RESPONSE = EdennErrorCode(
    20500,
    "A required service returned an unexpected response. Please try again later.",
    True, 502,
)

# ── 30xxx  Music Generation ─────────────────────────────────────────

MUSIC_RATE_LIMIT = EdennErrorCode(
    30100,
    "The service is currently at capacity. Please try again shortly.",
    True, 503,
)
MUSIC_TIMEOUT = EdennErrorCode(
    30200,
    "The request took too long to complete. Please try again.",
    True, 504,
)
MUSIC_AUTH = EdennErrorCode(
    30300,
    "A required service is temporarily unavailable. Please try again later.",
    True, 502,
)
MUSIC_UNAVAILABLE = EdennErrorCode(
    30400,
    "A required service is temporarily unavailable. Please try again later.",
    True, 502,
)
MUSIC_BAD_RESPONSE = EdennErrorCode(
    30500,
    "A required service returned an unexpected response. Please try again later.",
    True, 502,
)

# ── 40xxx  Media Processing ─────────────────────────────────────────

MEDIA_PROCESSING = EdennErrorCode(
    40001,
    "The uploaded media could not be processed. "
    "Please check the file format and try again.",
    False, 422,
)

# ── 50xxx  Storage ──────────────────────────────────────────────────

STORAGE_UNAVAILABLE = EdennErrorCode(
    50400,
    "The output could not be saved. Please try again later.",
    True, 502,
)

# ── 60xxx  Configuration ────────────────────────────────────────────

CONFIG_ERROR = EdennErrorCode(
    60001,
    "The service is temporarily unavailable. Please try again later.",
    False, 500,
)

# ── 90xxx  Internal ─────────────────────────────────────────────────

INTERNAL_ERROR = EdennErrorCode(
    90001,
    "An unexpected error occurred. Please try again later.",
    True, 500,
)


# ── Resolver ────────────────────────────────────────────────────────

# Supplied by configuration; see ``provider_vocabulary``.
def _music_providers() -> FrozenSet[str]:
    return provider_vocabulary.music_provider_names()


def resolve(error: EdennError) -> EdennErrorCode:
    """Map an EdennError to its triage error code.

    Routing rules
    -------------
    1. Content policy is checked first (most specific subclass).
    2. Validation / API errors with client-range status → 10xxx.
    3. Provider errors use ``provider_name`` to route to 20xxx (AI) or
       30xxx (Music Generation).
    4. Storage, media, config each have their own range.
    5. Everything else → 90001.
    """
    # 11xxx — content policy (subclass of Validation, must come first)
    if isinstance(error, EdennContentPolicyViolationError):
        return CONTENT_POLICY

    # 10xxx — source-video input guardrails (subclasses of Validation, must
    # come before the generic Validation mapping)
    if isinstance(error, EdennInputVideoTooLargeError):
        return INPUT_VIDEO_TOO_LARGE
    if isinstance(error, EdennInputVideoTooLongError):
        return INPUT_VIDEO_TOO_LONG
    if isinstance(error, EdennInputVideoTooShortError):
        return INPUT_VIDEO_TOO_SHORT
    if isinstance(error, EdennImageDurationTooShortError):
        return INPUT_IMAGE_DURATION_TOO_SHORT
    if isinstance(error, EdennLyricsUnsupportedModelspecError):
        return INPUT_LYRICS_UNSUPPORTED_MODELSPEC

    # 10xxx — input validation
    if isinstance(error, EdennValidationError):
        return INPUT_INVALID

    # 10xxx — API errors with client-side status codes
    if isinstance(error, EdennApiError):
        if error.status_code is not None and error.status_code < 500:
            return INPUT_INVALID
        return INTERNAL_ERROR

    # 20xxx / 30xxx — provider errors, routed by provider_name
    if isinstance(error, EdennProviderError):
        is_music = getattr(error, "provider_name", None) in _music_providers()
        if isinstance(error, EdennProviderRateLimitError):
            return MUSIC_RATE_LIMIT if is_music else AI_RATE_LIMIT
        if isinstance(error, EdennProviderTimeoutError):
            return MUSIC_TIMEOUT if is_music else AI_TIMEOUT
        if isinstance(error, EdennProviderAuthenticationError):
            return MUSIC_AUTH if is_music else AI_AUTH
        if isinstance(error, EdennProviderResponseError):
            return MUSIC_BAD_RESPONSE if is_music else AI_BAD_RESPONSE
        return MUSIC_UNAVAILABLE if is_music else AI_UNAVAILABLE

    # 50xxx — storage
    if isinstance(error, EdennStorageError):
        return STORAGE_UNAVAILABLE

    # 40xxx — media processing
    if isinstance(error, EdennMediaProcessingError):
        return MEDIA_PROCESSING

    # 60xxx — configuration
    if isinstance(error, EdennConfigurationError):
        return CONFIG_ERROR

    return INTERNAL_ERROR


# ── Provider/model-name leak prevention ─────────────────────────────
#
# Internal error text and routing fields legitimately name the upstream music/
# audio/LLM vendor (e.g. "Timed out waiting for ProviderC task ..."). That detail is
# fine for logs and Sentry but must NEVER reach a frontend or API client. The
# helpers below are the single sanitization boundary every client egress uses:
#   • ``public_error_payload`` — the ONLY error shape that may be sent to a
#     client: a generic catalog message + code, never the raw ``.message`` /
#     ``provider_name`` / ``str(exc)``.
#   • ``scrub_provider_names`` — defense-in-depth regex backstop for any free
#     text that still has to reach a client (e.g. legacy persisted error blobs).
#   • ``redact_error_blob`` / ``redact_client_keys`` — sanitize whole dicts read
#     back from storage or serialized into a snapshot.

# Vendor/model tokens that must not appear in client-facing text. Matched
# case-insensitively; only applied to error/status free text (never to media
# URLs or result blobs), so over-redaction is harmless.
# The token vocabulary covers the REASONING provider as well as the audio
# ones: the agent writes its own prose, and "do not name the model you
# are" is an instruction it can forget. Over-redaction there is a rare
# cosmetic loss against a rule the product does not get to break.
# The token vocabulary lives in configuration rather than in this file; see
# ``provider_vocabulary``.  With nothing configured the scrubber is a no-op
# on free text while the key-dropping paths below still apply.
def _provider_token_pattern():
    return provider_vocabulary.configured_pattern()

_PROVIDER_REPLACEMENT = "the generation service"

# Keys that carry vendor/internal identity and must be dropped from any error
# blob before it reaches a client.
_ERROR_DROP_KEYS: FrozenSet[str] = frozenset({
    "provider_name", "component", "operation", "context",
    "cause_type", "cause_message", "provider",
    "provider_audio_id", "provider_task_id",
})

# Keys that expose the upstream vendor (or its opaque handles) and must be
# dropped from any client-facing data payload (session snapshots, cards, ...).
_CLIENT_DROP_KEYS: FrozenSet[str] = frozenset({
    "provider", "provider_name", "provider_audio_id", "provider_task_id",
})


def scrub_provider_names(text: Any) -> Any:
    """Replace any music/audio/LLM vendor or model token with a neutral phrase.

    A no-op for non-strings, so it is safe to map over arbitrary values.
    """
    if not isinstance(text, str) or not text:
        return text
    pattern = _provider_token_pattern()
    return pattern.sub(_PROVIDER_REPLACEMENT, text) if pattern is not None else text


def public_error_payload(error: BaseException, *, retryable: bool | None = None) -> dict[str, Any]:
    """The only error shape that may be persisted into a client-visible field.

    Provider/model names never appear: ``EdennError`` instances are mapped
    through the generic triage catalog; anything else becomes a generic internal
    error. ``str(exc)`` / ``provider_name`` / raw ``.message`` are never used.
    """
    if isinstance(error, EdennError):
        entry = resolve(error)
        # 10xxx input errors may keep their request-specific public text (which
        # is author-controlled and never names a provider); everything else uses
        # catalog text so upstream/provider messages never become public.
        message = error.public_message if entry.code // 1000 == 10 else entry.message
        out_retryable = entry.retryable
        # A provider RESPONSE error carrying a definite client-side HTTP status
        # is a deterministic rejection (bad payload, quota, payment): the
        # catalog's blanket retryable=True would tell the client to burn
        # submissions on an outcome that cannot change. Deliberately narrow:
        # auth/rate-limit classes are excluded (ops-owned / transient), 408/429
        # stay transient, and errors whose status_code mirrors a provider BODY
        # code (marked via context.response_code) are excluded because body
        # codes are app-level, not HTTP semantics.
        status = getattr(error, "status_code", None)
        body_code = (getattr(error, "context", None) or {}).get("response_code")
        if (
            isinstance(error, EdennProviderResponseError)
            and body_code is None
            and isinstance(status, int)
            and 400 <= status < 500
            and status not in {408, 429}
        ):
            out_retryable = False
    else:
        entry = INTERNAL_ERROR
        message = INTERNAL_ERROR.message
        out_retryable = INTERNAL_ERROR.retryable
    if retryable is not None:
        out_retryable = bool(retryable)
    return {
        "error_code": entry.code,
        "message": scrub_provider_names(message),
        "retryable": out_retryable,
    }


def redact_error_blob(blob: Any) -> Any:
    """Sanitize an error/status dict read back from storage before returning it.

    Drops vendor-identity keys, prefers a generic ``public_message`` when one is
    present (legacy ``EdennError.to_dict()`` rows), and scrubs every remaining
    string. Use this on error objects only — they never carry media URLs.
    """
    if isinstance(blob, dict):
        out: dict[str, Any] = {}
        for key, value in blob.items():
            if key in _ERROR_DROP_KEYS:
                continue
            out[key] = redact_error_blob(value)
        public = out.pop("public_message", None)
        if public is not None:
            out["message"] = scrub_provider_names(public)
        return out
    if isinstance(blob, list):
        return [redact_error_blob(item) for item in blob]
    return scrub_provider_names(blob)


def redact_client_keys(value: Any) -> Any:
    """Recursively drop vendor-identity keys from a client-facing data payload.

    Drop-only (never rewrites strings), so media URLs and other values are left
    untouched. Use for session snapshots, card events, and similar payloads.
    """
    if isinstance(value, dict):
        return {
            key: redact_client_keys(child)
            for key, child in value.items()
            if key not in _CLIENT_DROP_KEYS
        }
    if isinstance(value, list):
        return [redact_client_keys(item) for item in value]
    return value


__all__ = [
    "EdennErrorCode",
    "resolve",
    "scrub_provider_names",
    "public_error_payload",
    "redact_error_blob",
    "redact_client_keys",
    "INPUT_INVALID",
    "INPUT_BAD_FORMAT",
    "INPUT_BAD_MODELSPEC",
    "INPUT_CONFLICT",
    "INPUT_VIDEO_TOO_LARGE",
    "INPUT_VIDEO_TOO_LONG",
    "INPUT_VIDEO_TOO_SHORT",
    "INPUT_IMAGE_DURATION_TOO_SHORT",
    "INPUT_LYRICS_UNSUPPORTED_MODELSPEC",
    "CONTENT_POLICY",
    "AI_RATE_LIMIT",
    "AI_TIMEOUT",
    "AI_AUTH",
    "AI_UNAVAILABLE",
    "AI_BAD_RESPONSE",
    "MUSIC_RATE_LIMIT",
    "MUSIC_TIMEOUT",
    "MUSIC_AUTH",
    "MUSIC_UNAVAILABLE",
    "MUSIC_BAD_RESPONSE",
    "MEDIA_PROCESSING",
    "STORAGE_UNAVAILABLE",
    "CONFIG_ERROR",
    "INTERNAL_ERROR",
]
