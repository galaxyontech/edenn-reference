"""Final response guardrail: no upstream model/provider brand reaches a client.

The response builders assemble a job response from LLM- and provider-authored
content — video summaries, scene descriptions, the music title, lyric text, the
music-description brief. None of that should ever name the upstream generator, and
this is the net that guarantees it: run once at the end of response assembly, it
scrubs any brand/model token out of the free-text fields and reports every hit so
the *root cause* can be fixed. The guardrail is the backstop, not the fix.

Deliberately narrow. It rewrites only unambiguous upstream brand / model names —
tokens with no innocent meaning in a media response. It does NOT touch words like
``azure`` (a colour) or ``capybara`` (an animal, and an internal resource name):
those collide with legitimate visual descriptions, so rewriting them would corrupt
real content. Ambiguous internal-infra tokens are the error path's concern
(``error_codes.scrub_provider_names``), where over-redaction is harmless because
error text never carries a scene description.

URLs are never rewritten — that would break the download. Blob names are
neutralised upstream (``output_naming``), so a brand token inside a media URL is a
real upstream bug: the guardrail flags it loudly rather than silently mangling the
link.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, TypeVar

from pydantic import BaseModel

from EdennCode.Deployment import provider_vocabulary

logger = logging.getLogger(__name__)

# The vocabulary of upstream brand and model names lives in configuration, not
# here: this file ships in a repository that people read, and the house rule is
# that an upstream provider is never named where a reader can see it. See
# ``provider_vocabulary`` for the three environment variables and for why the
# boundaries are not a plain ``\b``.
#
# What survives an EMPTY vocabulary matters, because a guardrail that becomes a
# complete no-op when unconfigured is a worse default than a partial one. The
# field-NAME check below is therefore vocabulary-independent: a key that
# announces upstream identity is a leak whatever this deployment configured.
# The free-text and URL passes are vocabulary-driven and do have nothing to
# match until tokens are configured.
def _brand_pattern():
    return provider_vocabulary.configured_pattern()


# Keys that carry vendor identity by their NAME alone. Kept in step with
# ``error_codes._ERROR_DROP_KEYS``, which drops the same set from error blobs.
_IDENTITY_KEYS = frozenset({
    "provider", "provider_name", "provider_audio_id", "provider_task_id",
})


_REPLACEMENT = "the generation service"


def _search(text: str):
    """Find a configured vendor token, or None when nothing is configured."""
    pattern = _brand_pattern()
    return pattern.search(text) if pattern is not None else None


def _sub(replacement: str, text: str) -> str:
    pattern = _brand_pattern()
    return pattern.sub(replacement, text) if pattern is not None else text


def _normalize(text: str) -> str:
    # NFKC folds fullwidth Latin (ＧＰＴ-４ｏ -> GPT-4o) so a JP-mixed model name
    # can't smuggle itself past the ASCII matcher. Hiragana/kanji are unaffected.
    return unicodedata.normalize("NFKC", text)


def _is_url(value: str) -> bool:
    head = value[:8].lower()
    return head.startswith("http://") or head.startswith("https://")


def guard_response_dict(payload: Any) -> tuple[Any, list[str]]:
    """Scrub brand/model tokens from every free-text field; report every hit.

    Returns ``(sanitized, leaks)``. Non-URL strings are rewritten; URLs are left
    intact (flagged when they carry a token); a token in a field NAME is flagged
    (the key is kept — dropping it could break the response shape). A non-empty
    ``leaks`` list means a builder let a brand name through and the source should be
    fixed.
    """
    leaks: list[str] = []

    def walk(value: Any, path: str) -> Any:
        if isinstance(value, dict):
            out: dict[Any, Any] = {}
            for key, child in value.items():
                key_path = f"{path}.{key}" if path else str(key)
                if isinstance(key, str) and (
                    key.lower() in _IDENTITY_KEYS or _search(_normalize(key))
                ):
                    leaks.append(f"{key_path}: brand token in field name")
                out[key] = walk(child, key_path)
            return out
        if isinstance(value, list):
            return [walk(item, f"{path}[{index}]") for index, item in enumerate(value)]
        if isinstance(value, str) and value:
            normalized = _normalize(value)
            if _is_url(value):
                # URLs are never rewritten (it would break the download); a brand
                # in one means a blob name leaked upstream — flag it loudly.
                if _search(normalized):
                    leaks.append(
                        f"{path}: brand token in URL "
                        "(left intact — blob name leaked upstream)"
                    )
                return value
            scrubbed = _sub(_REPLACEMENT, normalized)
            if scrubbed != normalized:
                leaks.append(f"{path}: scrubbed brand token from free text")
                return scrubbed
            # Nothing matched: return the ORIGINAL so legitimate formatting (and
            # non-Latin text) is preserved byte-for-byte.
            return value
        return value

    return walk(payload, ""), leaks


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def guard_response_model(response: _ModelT) -> _ModelT:
    """Apply the guardrail to an assembled response model.

    Returns the same instance untouched when clean — the common case, so the
    guardrail costs one dict walk and nothing else. When a token is found it is
    scrubbed, the leak is logged at error level (this must never happen — it means
    the builder leaked), and a corrected model is returned so the client never sees
    the token.
    """
    data = response.model_dump(mode="json")
    sanitized, leaks = guard_response_dict(data)
    if not leaks:
        return response
    logger.error(
        "response guardrail scrubbed upstream brand tokens from %s: %s",
        type(response).__name__,
        "; ".join(leaks),
    )
    return type(response).model_validate(sanitized)


__all__ = ["guard_response_dict", "guard_response_model"]
