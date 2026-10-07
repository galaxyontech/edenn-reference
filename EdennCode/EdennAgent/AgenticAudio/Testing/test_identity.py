"""Real identity, bridged to the platform — and the legacy door closing behind it.

The studio authenticated with a list of static tokens in an environment
variable. It now accepts a signed-in identity and takes the uid as the
principal, which is what ownership, membership and quotas are already keyed on.

Everything here is hermetic: the verifier is injected, so no test reaches
Firebase, the network, or any deployed resource.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import pytest

from EdennCode.EdennAgent.AgenticAudio.api.auth import AuthError, resolve_caller
from EdennCode.EdennAgent.AgenticAudio.api import identity as ident


@dataclass
class _Identity:
    uid: str
    display_name: str = ""


class FakeVerifier:
    """Accepts tokens shaped ``id:<uid>``; everything else is not an identity."""

    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.seen: list[str] = []

    async def verify(self, token: str):
        self.seen.append(token)
        if self.unavailable:
            raise ident.IdentityUnavailable("index down")
        if not token.startswith("id:"):
            raise ValueError("not an identity token")
        return _Identity(uid=token.split(":", 1)[1], display_name="Ada")


@pytest.fixture(autouse=True)
def _clean_identity(monkeypatch: pytest.MonkeyPatch):
    ident.reset_for_tests()
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.delenv("AGENTIC_AUDIO_TOKEN_IS_USER", raising=False)
    monkeypatch.delenv("AGENTIC_AUDIO_ALLOW_LEGACY_TOKENS", raising=False)
    yield
    ident.reset_for_tests()


def _resolve(**kwargs):
    return asyncio.run(resolve_caller(**kwargs))


# ---------------------------------------------------------------------------#
# a signed-in identity                                                        #
# ---------------------------------------------------------------------------#


def test_a_verified_token_authenticates_as_its_uid() -> None:
    """The uid is the anchor — people rebind phone numbers, and an identity that
    followed the number would eventually follow a recycled SIM."""
    ident.set_verifier(FakeVerifier())
    caller = _resolve(authorization="Bearer id:uid_ada")
    assert caller is not None
    assert caller.principal == "uid_ada"
    assert caller.uid == "uid_ada"
    assert caller.source == "identity"
    assert caller.is_legacy is False


def test_the_display_name_rides_along_when_the_identity_carries_one() -> None:
    ident.set_verifier(FakeVerifier())
    assert _resolve(authorization="Bearer id:uid_ada").display_name == "Ada"


def test_a_token_query_param_works_for_clients_that_cannot_set_headers() -> None:
    """A browser cannot set a header on a WebSocket handshake."""
    ident.set_verifier(FakeVerifier())
    assert _resolve(token="id:uid_ada").principal == "uid_ada"


def test_an_unverifiable_token_is_refused() -> None:
    ident.set_verifier(FakeVerifier())
    with pytest.raises(AuthError) as exc:
        _resolve(authorization="Bearer garbage")
    assert exc.value.status_code == 401


def test_a_missing_credential_is_refused() -> None:
    ident.set_verifier(FakeVerifier())
    with pytest.raises(AuthError) as exc:
        _resolve()
    assert exc.value.status_code == 401


def test_identity_storage_being_down_fails_closed_with_503() -> None:
    """Answering "you have no account" because a lookup timed out would tell a
    signed-in person to sign up a second time."""
    ident.set_verifier(FakeVerifier(unavailable=True))
    with pytest.raises(AuthError) as exc:
        _resolve(authorization="Bearer id:uid_ada")
    assert exc.value.status_code == 503
    assert "temporarily" in exc.value.detail


# ---------------------------------------------------------------------------#
# the legacy door                                                             #
# ---------------------------------------------------------------------------#


def test_a_static_token_still_works_during_the_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")
    ident.set_verifier(FakeVerifier())
    caller = _resolve(authorization="Bearer tok_a")
    assert caller.principal == "alice"
    assert caller.is_legacy is True


def test_identity_is_tried_before_the_static_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real sign-in must never be shadowed by a token that happens to match."""
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "id:uid_ada:somebody_else")
    verifier = FakeVerifier()
    ident.set_verifier(verifier)
    caller = _resolve(authorization="Bearer id:uid_ada")
    assert caller.principal == "uid_ada", "the static list shadowed a real identity"
    assert caller.source == "identity"


def test_the_legacy_door_can_be_closed_for_good(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")
    monkeypatch.setenv("AGENTIC_AUDIO_ALLOW_LEGACY_TOKENS", "0")
    ident.set_verifier(FakeVerifier())
    with pytest.raises(AuthError):
        _resolve(authorization="Bearer tok_a")
    # …and the real door still opens.
    assert _resolve(authorization="Bearer id:uid_ada").principal == "uid_ada"


def test_using_a_legacy_token_is_announced_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Once per process, not once per request — a warning per call is noise
    nobody reads."""
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")
    ident.set_verifier(FakeVerifier())
    import logging

    with caplog.at_level(logging.WARNING):
        _resolve(authorization="Bearer tok_a")
        _resolve(authorization="Bearer tok_a")
        _resolve(authorization="Bearer tok_a")
    warnings = [r for r in caplog.records if "deprecated" in r.getMessage()]
    assert len(warnings) == 1


def test_with_no_identity_and_no_keys_everything_is_refused() -> None:
    """The Phase 0 guarantee holds once identity is in the picture."""
    with pytest.raises(AuthError) as exc:
        _resolve(authorization="Bearer anything")
    assert exc.value.status_code == 401


def test_auth_off_still_means_no_principal() -> None:
    import os

    os.environ.pop("AGENTIC_AUDIO_REQUIRE_AUTH", None)
    try:
        assert _resolve(authorization="Bearer id:uid_ada") is None
    finally:
        os.environ["AGENTIC_AUDIO_REQUIRE_AUTH"] = "1"


# ---------------------------------------------------------------------------#
# the synchronous resolver must not become a way around identity              #
# ---------------------------------------------------------------------------#


def test_the_sync_resolver_refuses_rather_than_silently_skipping_identity() -> None:
    """It cannot await a verification, so with identity as the only source it
    would reject every real user as unknown. Refusing loudly is the honest
    failure."""
    from EdennCode.EdennAgent.AgenticAudio.api.auth import resolve_principal

    ident.set_verifier(FakeVerifier())
    with pytest.raises(AuthError) as exc:
        resolve_principal(authorization="Bearer id:uid_ada")
    assert "asynchronous" in exc.value.detail


def test_the_sync_resolver_still_serves_a_static_only_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")
    from EdennCode.EdennAgent.AgenticAudio.api.auth import resolve_principal

    assert resolve_principal(authorization="Bearer tok_a") == "alice"


# ---------------------------------------------------------------------------#
# the wiring: a verifier nothing builds verifies nothing                       #
# ---------------------------------------------------------------------------#


def test_nothing_is_wired_when_no_project_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A laptop has no identity provider, and inventing one would be worse."""
    monkeypatch.delenv("AGENTIC_AUDIO_IDP_PROJECT_ID", raising=False)

    assert ident.build_env_verifier() is None
    assert ident.configure_from_env() is False
    assert ident.identity_configured() is False


def test_a_configured_project_is_actually_wired_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module was complete and nothing ever called it.

    Every piece of this existed — the resolver, the injection point, the
    console's sign-in surface, the endpoint that publishes the client config —
    and no code path built a verifier, so `identity_configured()` was false in
    every deployment. The console told each user "sign-in isn't available on
    this deployment yet", and the static token list the module exists to retire
    stayed the only way in.
    """
    monkeypatch.setenv("AGENTIC_AUDIO_IDP_PROJECT_ID", "studio-identity")

    assert ident.configure_from_env() is True
    assert ident.identity_configured() is True


def test_the_studio_reads_its_own_setting_and_not_the_platform_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same isolation rule the database config keeps: an ambient value from the
    rest of the platform must not decide whose users these are."""
    monkeypatch.delenv("AGENTIC_AUDIO_IDP_PROJECT_ID", raising=False)
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "someone-elses-project")

    assert ident.build_env_verifier() is None
