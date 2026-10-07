"""Who the caller is — bridged to the platform's identity, not reinvented.

The studio authenticated with a static token list in an environment variable:
``AGENTIC_AUDIO_API_KEYS="tok:user,..."``. There was no users table, no sign-up,
no sign-out and no revocation; rotating a credential meant a container restart,
and the "user id" was whatever string an operator typed after the colon.

Meanwhile the platform already runs a real identity stack — Firebase ID-token
verification, self-serve verified signup, an account index — and a console that
signs people in. The studio ships as its own app, so it cannot inherit that
stack's middleware; it adopts the same *credential* instead.

    A caller presents a Firebase ID token. We verify it and take the **uid** as
    the principal.

The uid is the anchor for the same reason the signup router gives: people rebind
phone numbers, and an identity that followed the number would eventually hand an
account to whoever gets the recycled SIM.

The static token list stays as a **deprecated** second source so existing
deployments and local runs keep working through the transition. It is consulted
only after the token fails to verify as an identity, and it announces itself in
the logs.

Nothing here reaches the network on its own: the verifier is injected, so tests
are hermetic and a deployment without Firebase configured simply has no identity
source (and, with auth required, refuses every request rather than inventing
one).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)


class IdentityVerifier(Protocol):
    """The half of the platform verifier this module needs."""

    async def verify(self, token: str) -> Any:  # -> object with `.uid`
        ...


@dataclass(frozen=True)
class Caller:
    """An authenticated person.

    ``principal`` is the string the rest of the studio already keys ownership on,
    so adopting real identity changed no downstream signature.
    """

    principal: str
    source: str          # "identity" | "legacy_token"
    uid: Optional[str] = None
    display_name: str = ""

    @property
    def is_legacy(self) -> bool:
        return self.source == "legacy_token"


class IdentityUnavailable(Exception):
    """Identity storage could not answer.

    The caller must fail CLOSED (503) rather than fall through to "no account":
    telling a signed-in person they are a stranger because a lookup timed out is
    worse than telling them to try again.
    """


class _Resolver:
    """Verifies a presented credential. Holds no network client of its own."""

    def __init__(self, verifier: Optional[IdentityVerifier] = None) -> None:
        self._verifier = verifier

    @property
    def configured(self) -> bool:
        return self._verifier is not None

    def set_verifier(self, verifier: Optional[IdentityVerifier]) -> None:
        self._verifier = verifier

    async def resolve(self, presented: str) -> Optional[Caller]:
        """The caller a token proves, or ``None`` when it proves nothing."""

        token = (presented or "").strip()
        if not token or self._verifier is None:
            return None
        try:
            identity = await self._verifier.verify(token)
        except IdentityUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - an invalid token is not an error
            logger.debug("identity: token rejected (%s)", getattr(exc, "reason", exc))
            return None

        uid = str(getattr(identity, "uid", "") or "").strip()
        if not uid:
            return None
        # A display name if the identity carries one; the studio falls back to
        # the participant row, and then to the principal itself.
        name = str(
            getattr(identity, "display_name", "")
            or getattr(identity, "name", "")
            or ""
        ).strip()
        return Caller(principal=uid, source="identity", uid=uid, display_name=name)


_resolver = _Resolver()


def build_env_verifier() -> Optional[IdentityVerifier]:
    """The platform's ID-token verifier, built from this studio's own settings.

    ``None`` when ``AGENTIC_AUDIO_IDP_PROJECT_ID`` is unset, which is not an
    error: a laptop run has no identity provider, and the caller decides what
    an unconfigured studio may serve. Deliberately reads the studio's OWN
    variable rather than the platform's ``FIREBASE_PROJECT_ID`` — the same
    isolation rule the database config keeps, so a stray ambient value cannot
    silently point the studio at another project's users.

    Imported lazily: the verifier pulls network and crypto dependencies that a
    test process has no reason to load.
    """

    project_id = os.getenv("AGENTIC_AUDIO_IDP_PROJECT_ID", "").strip()
    if not project_id:
        return None
    from EdennCode.Deployment.auth.firebase_verifier import FirebaseTokenVerifier

    return FirebaseTokenVerifier(project_id, logger=logger)


def configure_from_env() -> bool:
    """Wire the verifier at startup. True when identity is now available.

    Called once per process where the app is built. Without it the module sits
    at its hermetic default — no verifier — and every signed-in caller is a
    stranger: the console asks "how do I sign in?", the answer is "you cannot",
    and the only credential left is the static token list this is meant to
    retire.
    """

    verifier = build_env_verifier()
    if verifier is None:
        return False
    _resolver.set_verifier(verifier)
    logger.info("identity: verifying ID tokens for the configured project")
    return True


def resolver() -> _Resolver:
    return _resolver


def set_verifier(verifier: Optional[IdentityVerifier]) -> None:
    """Point the studio at an identity verifier (the platform's, or a fake)."""

    _resolver.set_verifier(verifier)


def identity_configured() -> bool:
    return _resolver.configured


def legacy_tokens_enabled() -> bool:
    """Static token list, deprecated.

    Defaults ON so this change breaks nothing, and off-able now so a deployment
    that has migrated can close the door for good.
    """

    raw = os.getenv("AGENTIC_AUDIO_ALLOW_LEGACY_TOKENS", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


_warned_legacy = False


def warn_legacy_once() -> None:
    """Say it once per process, not once per request."""

    global _warned_legacy
    if _warned_legacy:
        return
    _warned_legacy = True
    logger.warning(
        "A caller authenticated with a static AGENTIC_AUDIO_API_KEYS token. "
        "These are deprecated: they cannot be revoked without a restart, carry "
        "no account, and name a user id an operator typed by hand. Move callers "
        "to a signed-in identity and set AGENTIC_AUDIO_ALLOW_LEGACY_TOKENS=0."
    )


def reset_for_tests() -> None:
    global _warned_legacy
    _warned_legacy = False
    _resolver.set_verifier(None)


__all__ = [
    "build_env_verifier",
    "Caller",
    "configure_from_env",
    "IdentityUnavailable",
    "IdentityVerifier",
    "identity_configured",
    "legacy_tokens_enabled",
    "reset_for_tests",
    "resolver",
    "set_verifier",
    "warn_legacy_once",
]
