"""Firebase ID token verification: accept exactly one shape, reject the rest.

Tokens here are signed by a throwaway key pair whose certificate is handed to
the verifier through the injected fetcher, so nothing in this module talks to
Google.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import time
from typing import Any, Optional

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from EdennCode.Deployment.auth.firebase_verifier import (
    INVALID_TOKEN_MESSAGE,
    ISSUER_PREFIX,
    FirebaseTokenVerifier,
    InvalidIdentityToken,
    parse_max_age,
)

PROJECT = "edenn-console-test"
UID = "kQ2mZ8xVbNfR4tYuIoPaSdFgHjK1"
PHONE = "+819012345678"
KID = "test-kid-1"


def _make_cert(common_name: str = "test") -> tuple[Any, str]:
    """(private_key, PEM certificate) — one self-signed pair for the module."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    return key, pem


# RSA keygen is slow enough to matter across ~25 tests; one pair for all of them.
PRIVATE_KEY, CERT_PEM = _make_cert()
OTHER_KEY, OTHER_CERT_PEM = _make_cert("impostor")


class FakeClock:
    """Real wall time plus an offset, so PyJWT's own exp check stays coherent."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


class CertFeed:
    def __init__(self, certs: Optional[dict[str, str]] = None,
                 ttl_s: float = 3600.0) -> None:
        self.certs = certs if certs is not None else {KID: CERT_PEM}
        self.ttl_s = ttl_s
        self.calls = 0
        self.fail = False

    def __call__(self) -> tuple[dict[str, str], float]:
        self.calls += 1
        if self.fail:
            raise ConnectionError("google unreachable")
        return dict(self.certs), self.ttl_s


def _verifier(feed: Optional[CertFeed] = None,
              clock: Optional[FakeClock] = None,
              **kwargs) -> tuple[FirebaseTokenVerifier, CertFeed, FakeClock]:
    feed = feed or CertFeed()
    clock = clock or FakeClock()
    verifier = FirebaseTokenVerifier(
        PROJECT, fetch_certs=feed, clock=clock,
        logger=logging.getLogger("t"), **kwargs,
    )
    return verifier, feed, clock


_DROP = object()


def _token(*, key: Any = None, kid: str = KID, alg: str = "RS256",
           **overrides: Any) -> str:
    """A well-formed phone token, with any claim overridden or ``_DROP``ped."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER_PREFIX + PROJECT,
        "aud": PROJECT,
        "auth_time": now - 30,
        "user_id": UID,
        "sub": UID,
        "iat": now - 30,
        "exp": now + 3600,
        "phone_number": PHONE,
        "firebase": {"identities": {"phone": [PHONE]},
                     "sign_in_provider": "phone"},
    }
    claims.update(overrides)
    for name, value in list(claims.items()):
        if value is _DROP:
            claims.pop(name)
    return jwt.encode(claims, key or PRIVATE_KEY, algorithm=alg,
                      headers={"kid": kid})


def _verify(verifier: FirebaseTokenVerifier, token: str):
    return asyncio.run(verifier.verify(token))


class TestHappyPath:
    def test_valid_token_yields_uid_and_phone(self):
        verifier, _, _ = _verifier()
        identity = _verify(verifier, _token())
        assert identity.uid == UID
        assert identity.phone_number == PHONE
        assert identity.provider == "phone"

    def test_bearer_whitespace_is_tolerated(self):
        verifier, _, _ = _verifier()
        assert _verify(verifier, f"  {_token()}  ").uid == UID


class TestAlgorithmConfusion:
    """The classic JWT break: make the server verify with the wrong scheme."""

    def test_alg_none_is_rejected(self):
        verifier, _, _ = _verifier()
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "none", "kid": KID}).encode()
        ).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(
            json.dumps({"iss": ISSUER_PREFIX + PROJECT, "aud": PROJECT,
                        "sub": UID, "iat": int(time.time()),
                        "exp": int(time.time()) + 3600}).encode()
        ).rstrip(b"=").decode()
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, f"{header}.{payload}.")

    def test_hs256_signed_token_is_rejected(self):
        # An attacker who knows the public key must not be able to present it
        # as an HMAC secret.
        verifier, _, _ = _verifier()
        forged = jwt.encode(
            {"iss": ISSUER_PREFIX + PROJECT, "aud": PROJECT, "sub": UID,
             "iat": int(time.time()), "exp": int(time.time()) + 3600,
             "phone_number": PHONE,
             "firebase": {"sign_in_provider": "phone"}},
            "attacker-chosen-secret", algorithm="HS256",
            headers={"kid": KID},
        )
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, forged)

    def test_token_signed_by_another_key_is_rejected(self):
        verifier, _, _ = _verifier()
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, _token(key=OTHER_KEY))


class TestClaimChecks:
    def test_wrong_audience(self):
        verifier, _, _ = _verifier()
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, _token(aud="some-other-project"))

    def test_wrong_issuer(self):
        verifier, _, _ = _verifier()
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, _token(iss="https://evil.example.com/" + PROJECT))

    def test_expired_token(self):
        verifier, _, _ = _verifier()
        now = int(time.time())
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, _token(iat=now - 7200, exp=now - 3600))

    def test_future_issued_at(self):
        verifier, _, _ = _verifier()
        now = int(time.time())
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, _token(iat=now + 900, auth_time=now - 30))

    def test_future_auth_time(self):
        verifier, _, _ = _verifier()
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, _token(auth_time=int(time.time()) + 900))

    def test_empty_subject(self):
        verifier, _, _ = _verifier()
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, _token(sub=""))

    def test_missing_subject(self):
        verifier, _, _ = _verifier()
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, _token(sub=_DROP))

    def test_non_phone_provider_is_rejected(self):
        # Google sign-in tokens are valid Firebase tokens; this round only
        # trusts the phone flow, and must say so rather than silently pass a
        # provider whose phone_number nobody verified.
        verifier, _, _ = _verifier()
        forged = _token(firebase={"sign_in_provider": "google.com",
                                  "identities": {}})
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, forged)

    def test_missing_firebase_claim(self):
        verifier, _, _ = _verifier()
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, _token(firebase=_DROP))

    def test_phone_provider_without_phone_number(self):
        verifier, _, _ = _verifier()
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, _token(phone_number=_DROP))

    def test_unknown_kid(self):
        verifier, _, _ = _verifier()
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, _token(kid="not-a-published-kid"))

    def test_garbage_and_empty_tokens(self):
        verifier, _, _ = _verifier()
        for junk in ("", "   ", "not.a.jwt", "onlyonesegment"):
            with pytest.raises(InvalidIdentityToken):
                _verify(verifier, junk)


class TestOpaqueFailures:
    def test_every_rejection_carries_the_same_message(self):
        """Distinguishable errors are how a forger finds the one check to beat."""
        verifier, _, _ = _verifier()
        now = int(time.time())
        bad_tokens = [
            _token(aud="other"),
            _token(iss="https://evil.example.com/x"),
            _token(iat=now - 7200, exp=now - 3600),
            _token(sub=""),
            _token(kid="unknown"),
            _token(phone_number=_DROP),
            _token(key=OTHER_KEY),
        ]
        messages = set()
        for token in bad_tokens:
            with pytest.raises(InvalidIdentityToken) as caught:
                _verify(verifier, token)
            messages.add(str(caught.value))
        assert messages == {INVALID_TOKEN_MESSAGE}

    def test_reason_is_populated_for_logs(self):
        verifier, _, _ = _verifier()
        with pytest.raises(InvalidIdentityToken) as caught:
            _verify(verifier, _token(aud="other"))
        assert caught.value.reason


class TestCertificateCache:
    def test_certificates_are_fetched_once_while_fresh(self):
        verifier, feed, _ = _verifier()
        for _ in range(3):
            assert _verify(verifier, _token()).uid == UID
        assert feed.calls == 1

    def test_cache_refreshes_after_max_age(self):
        verifier, feed, clock = _verifier(CertFeed(ttl_s=600.0))
        _verify(verifier, _token())
        clock.advance(601)
        _verify(verifier, _token())
        assert feed.calls == 2

    def test_stale_certificates_survive_a_fetch_failure(self):
        # Google being briefly unreachable must not log every customer out.
        verifier, feed, clock = _verifier(CertFeed(ttl_s=600.0))
        _verify(verifier, _token())
        feed.fail = True
        clock.advance(601)
        assert _verify(verifier, _token()).uid == UID
        assert feed.calls == 2

    def test_first_fetch_failure_rejects_rather_than_crashing(self):
        verifier, feed, _ = _verifier()
        feed.fail = True
        with pytest.raises(InvalidIdentityToken):
            _verify(verifier, _token())

    def test_unparseable_certificate_does_not_void_the_others(self):
        feed = CertFeed({KID: CERT_PEM, "broken": "-----BEGIN CERTIFICATE-----junk"})
        verifier, _, _ = _verifier(feed)
        assert _verify(verifier, _token()).uid == UID

    def test_rotation_picks_up_the_new_kid(self):
        feed = CertFeed(ttl_s=600.0)
        verifier, _, clock = _verifier(feed)
        _verify(verifier, _token())
        feed.certs = {"kid-2": OTHER_CERT_PEM}
        clock.advance(601)
        assert _verify(verifier, _token(key=OTHER_KEY, kid="kid-2")).uid == UID


class TestConstruction:
    def test_from_settings_returns_none_without_a_project_id(self):
        logger = logging.getLogger("t")

        class Settings:
            firebase_project_id = ""

        assert FirebaseTokenVerifier.from_settings(Settings(), logger) is None

    def test_from_settings_builds_when_configured(self):
        logger = logging.getLogger("t")

        class Settings:
            firebase_project_id = "  my-project  "

        verifier = FirebaseTokenVerifier.from_settings(Settings(), logger)
        assert verifier is not None
        assert verifier.project_id == "my-project"


class TestMaxAgeParsing:
    def test_reads_max_age(self):
        assert parse_max_age("public, max-age=19621, must-revalidate") == 19621.0

    def test_falls_back_to_an_hour(self):
        assert parse_max_age("") == 3600.0
        assert parse_max_age("no-cache") == 3600.0
