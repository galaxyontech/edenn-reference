"""Upstream vendor tokens, supplied by configuration rather than by source.

The product must never name an upstream provider anywhere a reader can see, and
a source file is such a place.  The scrubbers in :mod:`response_guardrail` and
:mod:`error_codes` still need a vocabulary to match against, so it is loaded at
run time from the environment instead of being written down here.

Three environment variables, each a comma-separated, case-insensitive list:

``PROVIDER_SCRUB_TOKENS``
    Plain tokens.  Matched with a trailing ``\\w*`` so version suffixes come
    with them (``acme`` also catches ``acme4``, ``acme_v2``, ``AcmeAI``).

``PROVIDER_SCRUB_TOKENS_STRICT``
    Tokens that must NOT be followed by another letter.  Use this for a name
    that is also the stem of an ordinary word: a plain token ``cloud`` would
    otherwise scrub the word ``cloudless`` out of a sentence.

``PROVIDER_SCRUB_MODEL_PREFIXES``
    Model-family prefixes that only count when a version digit follows, so
    ``foo-4o`` is scrubbed while ``foo-based summary`` is left alone.

With nothing configured the guardrails still run: keys that carry vendor
identity are still dropped and signed URLs are still flagged.  Only the
free-text token pass has nothing to match, which is the right behaviour for a
deployment that has not told the system which names to hide.
"""
from __future__ import annotations

import os
import re
from typing import FrozenSet, Optional, Pattern, Sequence, Tuple

__all__ = [
    "scrub_tokens",
    "strict_scrub_tokens",
    "model_prefixes",
    "music_provider_names",
    "build_token_pattern",
    "reset_cache",
]

_ENV_TOKENS = "PROVIDER_SCRUB_TOKENS"
_ENV_STRICT = "PROVIDER_SCRUB_TOKENS_STRICT"
_ENV_MODELS = "PROVIDER_SCRUB_MODEL_PREFIXES"
_ENV_MUSIC = "MUSIC_PROVIDER_NAMES"

_cache: dict = {}


def _split(name: str) -> Tuple[str, ...]:
    raw = os.getenv(name, "") or ""
    return tuple(t.strip().lower() for t in raw.split(",") if t.strip())


def reset_cache() -> None:
    """Drop the memoised vocabulary. Call after changing the environment."""
    _cache.clear()


def _cached(key: str, env: str) -> Tuple[str, ...]:
    if key not in _cache:
        _cache[key] = _split(env)
    return _cache[key]


def scrub_tokens() -> Tuple[str, ...]:
    return _cached("tokens", _ENV_TOKENS)


def strict_scrub_tokens() -> Tuple[str, ...]:
    return _cached("strict", _ENV_STRICT)


def model_prefixes() -> Tuple[str, ...]:
    return _cached("models", _ENV_MODELS)


def music_provider_names() -> FrozenSet[str]:
    """Names that identify a music generation upstream, for error triage."""
    if "music" not in _cache:
        _cache["music"] = frozenset(_split(_ENV_MUSIC))
    return _cache["music"]


def build_token_pattern(
    tokens: Sequence[str] = (),
    strict: Sequence[str] = (),
    models: Sequence[str] = (),
) -> Optional[Pattern[str]]:
    """Compile one case-insensitive alternation over a configured vocabulary.

    Boundaries are deliberately not a plain ``\\b``.  Free text is authored by a
    language model and by upstream services, which write a name in many surface
    forms: glued to a version digit, joined with an underscore, or camel-cased.
    ``_`` is a word character, so ``\\b`` misses ``name_v4``; a glued digit has no
    trailing boundary, so ``\\b`` misses ``name4``.  Each token is instead
    anchored with ``(?<![a-z])`` -- not glued to a preceding letter, so a token
    can never match inside a longer ordinary word -- and given a ``\\w*`` tail.

    Returns ``None`` when the vocabulary is empty, which callers must treat as
    "match nothing" rather than "match everything".
    """
    branches = []
    for tok in tokens:
        branches.append(re.escape(tok) + r"\w*")
    for tok in strict:
        # Not followed by a letter: the name is also the stem of an ordinary
        # word, and only the bare form (or a digit/underscore suffix) is a leak.
        branches.append(re.escape(tok) + r"(?![a-z])\w*")
    for pre in models:
        # Needs a version digit, so a prose mention of the family survives.
        branches.append(re.escape(pre) + r"[_ -]?\d\w*(?:\.\d\w*)*")
    if not branches:
        return None
    return re.compile(
        r"(?<![a-z])(?:" + "|".join(branches) + r")",
        re.IGNORECASE,
    )


def configured_pattern() -> Optional[Pattern[str]]:
    """The pattern for the vocabulary this deployment configured."""
    key = "pattern"
    if key not in _cache:
        _cache[key] = build_token_pattern(
            scrub_tokens(), strict_scrub_tokens(), model_prefixes()
        )
    return _cache[key]
