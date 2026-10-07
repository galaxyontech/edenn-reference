"""Does a name — a filename, a blob key, a client-facing message — name a vendor?

The vocabulary this module asks about is deliberately not written down here.
Source is a reader-visible surface like any other, so the names to look for come
from the deployment's configuration; see
:mod:`EdennCode.Deployment.provider_vocabulary`.  This module owns the question,
not the answer, which also keeps it in lockstep with the scrubber in
:mod:`EdennCode.Deployment.error_codes`: both match the same compiled
vocabulary, so "scrubbed" and "token free" cannot drift apart and let a leak
through the gap.

Matching is the vocabulary's own, not a substring test.  A substring test reads
a vendor name inside ordinary words (it saw one in ``fake_provider_audio.wav``)
and a guard that cries wolf gets switched off.

With nothing configured there is nothing to match and every name reads as clean
— the same deliberate default as the scrubber: a deployment that has not said
which names to hide gets no free-text matching rather than a guess.
"""

from __future__ import annotations

from typing import FrozenSet

from EdennCode.Deployment import provider_vocabulary


def provider_tokens() -> FrozenSet[str]:
    """The names this deployment configured, for diagnostics and failure messages."""
    return frozenset(
        provider_vocabulary.scrub_tokens()
        + provider_vocabulary.strict_scrub_tokens()
        + provider_vocabulary.model_prefixes()
    )


def contains_provider_token(name: str) -> bool:
    """True when ``name`` still carries a configured upstream provider name."""
    pattern = provider_vocabulary.configured_pattern()
    if pattern is None:
        return False
    return bool(pattern.search(name))
