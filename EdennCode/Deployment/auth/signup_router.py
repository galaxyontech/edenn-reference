"""Self-serve signup: open or find a billing account.

Two doors, and the difference between them is the whole point:

* ``POST /api/v1/signup/verified`` — the public one. Identity comes from a
  Firebase ID token, so the phone number is proven rather than typed. The
  anchor is the Firebase **uid**, not the number: customers re-bind numbers,
  and an account that followed the number would eventually be handed to
  whoever gets the recycled SIM. It opens the account and stops there: **no
  key is issued**. Keys are minted from the console's API key page.
* ``POST /api/v1/signup`` — the original unverified flow, now behind
  ``x-admin-secret``. It stays for dev/CI convenience — and because a script
  wants an account and a working key from one call, it is the door that still
  mints one. It is no longer a public surface: leaving an unverified door open
  next to a verified one makes the verification decorative, and this
  particular door has a known read surface (see the v0 note below).

Two structural invariants hold unconditionally, independent of any feature flag:

* ``account_id`` is server-generated and never returned. Honoring a
  client-supplied id would let anyone mint a key into another tenant's wallet.
* A brand-new account starts at zero balance, so the billing gate answers 402 on
  every billable submission until an admin recharges it. That guarantee only
  holds while billing is enforcing, which is why the endpoint closes itself in
  any other billing mode.

A phone or email already on file merges onto its existing account instead of
opening a duplicate, and never overwrites that account's stored profile.

What the unverified endpoint deliberately does NOT protect: contacts are
unverified, so possession of
a contact *string* is treated as proof of owning that contact. A caller who knows
a customer's phone number or email therefore merges onto that customer's account
— they get a working key that spends that customer's wallet, and the response
tells them its real balance. Withholding ``account_id`` does not hide any of
this: ``is_new_account: false`` with a non-zero ``balance_usd`` already confirms
the customer exists and is funded. That is exactly why the endpoint now requires
the admin secret: the mitigation "nobody knows the URL" stopped being credible
the moment a verified door existed beside it.
"""
from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from EdennCode.Deployment.auth.admin_router import admin_guard

# Permissive on purpose: one "@", a dot in the domain, no whitespace.
# Deliverability is proven by sending mail, not by a regex, and Pydantic's
# EmailStr would add an `email-validator` dependency for this single field.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Account notes record provenance and are read by operators.
SIGNUP_NOTE = "self-serve signup"
VERIFIED_SIGNUP_NOTE = "firebase phone signup"
# A key's note is a *label the customer reads and edits*, so it must not be the
# account's provenance string. Reusing one constant for both put
# "firebase phone signup" in the name column of every console.
DEFAULT_KEY_NAME = "default"
_MAX_ID_ATTEMPTS = 2


class SignupRequest(BaseModel):
    registered_name: str = Field(min_length=1, max_length=512)
    phone: str = Field(default="", max_length=32)
    email: str = Field(default="", max_length=254)


class VerifiedSignupRequest(BaseModel):
    """Only the display name. The phone number comes from the token, always.

    Accepting a caller-supplied number here would reintroduce exactly the hole
    verification exists to close, so this model has no field for one — an extra
    key in the body is ignored rather than trusted.
    """

    registered_name: str = Field(min_length=1, max_length=512)


@dataclass(frozen=True)
class _Profile:
    """What a *new* account gets written with; ignored when merging."""

    registered_name: str
    email: str
    phone: str
    note: str
    key_name: str


class SignupResponse(BaseModel):
    api_key: str
    key_prefix: str
    balance_usd: float
    is_new_account: bool
    message: str


class VerifiedSignupResponse(BaseModel):
    """The public door's answer: an account, and no key.

    Deliberately a separate model rather than ``SignupResponse`` with an empty
    ``api_key``. A client reading a blank string where a secret used to be has
    to guess whether signup half-failed; an absent field cannot be misread.
    """

    balance_usd: float
    is_new_account: bool
    message: str


class _Rejected(Exception):
    """Carries the response to return, so the flow reads top-to-bottom."""

    def __init__(self, response: JSONResponse) -> None:
        self.response = response


def _error(status_code: int, detail: str, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code,
                        content={"detail": detail, "code": code})


def _unavailable_detail() -> JSONResponse:
    # One opaque message for every unavailability cause: an unauthenticated
    # caller learns nothing about our billing mode or storage health.
    return _error(503, "Signup is temporarily unavailable.",
                  "signup_unavailable")


def _conflict() -> JSONResponse:
    return _error(
        409,
        "The phone number and email belong to different accounts. "
        "Contact your administrator to sort this out.",
        "identity_conflict",
    )


def _invalid_identity() -> JSONResponse:
    # Identical for "no header", "malformed header" and "signature failed":
    # a caller probing this endpoint must not learn which check it tripped.
    from EdennCode.Deployment.auth.firebase_verifier import (
        INVALID_TOKEN_CODE,
        INVALID_TOKEN_MESSAGE,
    )

    return _error(401, INVALID_TOKEN_MESSAGE, INVALID_TOKEN_CODE)


def create_signup_router(
    *,
    key_store: Any,
    billing_engine: Any,
    logger: logging.Logger,
    firebase_verifier: Any = None,
    admin_secret: Optional[str] = None,
) -> APIRouter:
    router = APIRouter()

    def _blocked() -> Optional[JSONResponse]:
        """503 unless we can mint a key, open a wallet, and index a contact."""
        if key_store is None:
            return _unavailable_detail()
        if billing_engine is None or billing_engine.mode != "enforce":
            return _unavailable_detail()
        if billing_engine.account_store is None:
            return _unavailable_detail()
        if billing_engine.index_store is None:
            return _unavailable_detail()
        return None

    async def _merge(
        index: Any,
        account_id: str,
        contacts: list[tuple[str, str]],
        hit_keys: set[tuple[str, str]],
    ) -> None:
        """Bind this signup's not-yet-indexed contacts to the found account."""
        for kind, value in contacts:
            if (kind, value) in hit_keys:
                continue
            if await index.claim(kind, value, account_id):
                continue
            # Someone claimed it between our lookup and our claim. Whoever won,
            # the contact must still resolve to the same account or the two
            # identities genuinely disagree.
            if await index.lookup(kind, value) != account_id:
                raise _Rejected(_conflict())

    async def _resolve_account(
        index: Any,
        accounts: Any,
        contacts: list[tuple[str, str]],
        profile: _Profile,
    ) -> tuple[str, bool]:
        """(account_id, is_new). Merges onto an existing account when possible."""
        from EdennCode.Deployment.billing.stores import AccountExists

        # Keyed by (kind, value) so `hit_keys` below can be exactly `set(hits)`:
        # the backfill skips the contacts that are already indexed.
        hits: dict[tuple[str, str], str] = {}
        for kind, value in contacts:
            found = await index.lookup(kind, value)
            if found:
                hits[(kind, value)] = found
        distinct = set(hits.values())
        if len(distinct) > 1:
            raise _Rejected(_conflict())
        if distinct:
            account_id = distinct.pop()
            await _merge(index, account_id, contacts, set(hits))
            return account_id, False

        for _ in range(_MAX_ID_ATTEMPTS):
            account_id = f"acct_{secrets.token_hex(12)}"
            try:
                await accounts.create(
                    account_id=account_id,
                    registered_name=profile.registered_name,
                    entity_type="individual",
                    email=profile.email,
                    phone=profile.phone,
                    note=profile.note,
                )
                break
            except AccountExists:
                account_id = ""  # 96 random bits: unreachable, not a 500
        if not account_id:
            logger.warning("signup: account id collided %d times",
                           _MAX_ID_ATTEMPTS)
            raise _Rejected(_unavailable_detail())

        for kind, value in contacts:
            if await index.claim(kind, value, account_id):
                continue
            # A concurrent signup won this contact. Issue under their account
            # instead of opening a duplicate; ours stays behind as a $0, keyless
            # orphan (identifiable by its note).
            winner = await index.lookup(kind, value)
            if not winner:
                raise _Rejected(_unavailable_detail())
            logger.info("signup: lost claim on %s, merging into %s",
                        kind, winner)
            return winner, False
        return account_id, True

    async def _open_and_issue(
        contacts: list[tuple[str, str]], profile: _Profile, *, issue_key: bool
    ) -> Any:
        """Resolve or open the account and describe what to do next.

        Shared by both doors so the merge, conflict and race semantics can
        never drift apart between the verified and unverified paths.

        ``issue_key`` is what separates them. The console door leaves with no
        key, the way ModelGateway's and ModelVendorAlt's consoles do: the one moment a
        plaintext key exists is then a moment the customer asked for, on a page
        built to show it once, instead of a line inside a signup response they
        were still reading. It also stops a returning customer from littering
        their own account with keys nobody ever saved — signup merges onto the
        existing account, and minting on every merge is how that happens.
        """
        from EdennCode.Deployment.billing.stores import (
            BillingStoreUnavailable,
            micros_to_usd,
        )

        accounts = billing_engine.account_store
        index = billing_engine.index_store
        try:
            account_id, is_new = await _resolve_account(
                index, accounts, contacts, profile)
        except _Rejected as rejected:
            return rejected.response
        except BillingStoreUnavailable as exc:
            logger.warning("signup: storage unavailable (%s)", exc)
            return _unavailable_detail()

        plaintext = ""
        key = None
        if issue_key:
            try:
                plaintext, key = await key_store.mint(
                    user_id=account_id, note=profile.key_name)
            except Exception:  # noqa: BLE001 - any store error is a 503
                # On a new account this leaves a $0 account with no key:
                # harmless, identifiable by its note, and a retry merges into it
                # rather than handing out a key whose wallet does not exist.
                logger.warning("signup: key mint failed for account %s",
                               account_id, exc_info=True)
                return _unavailable_detail()

        record = None
        try:
            record = await accounts.get(account_id)
        except Exception:  # noqa: BLE001 - never fail a request past the mint
            # The key is already minted and is shown exactly once, so raising
            # here would destroy it. Every failure mode — storage down, a
            # decode error, anything — must still hand the key over; reporting
            # a 0 balance is honest enough and an admin can confirm the rest.
            logger.warning("signup: balance read failed for %s", account_id,
                           exc_info=True)

        balance_micros = record.balance_micros if record else 0
        is_active = bool(record.is_active) if record else False
        balance_usd = micros_to_usd(balance_micros)
        if balance_micros <= 0:
            tail = ("Your account balance is 0; contact your administrator to "
                    "add credit before submitting jobs.")
        elif not is_active:
            # Credit alone does not make an account usable: the gate answers 403
            # account_inactive, so promising "ready to use" here would be a lie.
            tail = ("Your account is not active; contact your administrator "
                    "before submitting jobs.")
        elif issue_key:
            tail = ("Your account already has credit, so this key is ready to "
                    "use.")
        else:
            tail = "Your account already has credit."
        if key is None:
            logger.info("signup: opened account %s (new=%s), no key issued",
                        account_id, is_new)
            return VerifiedSignupResponse(
                balance_usd=balance_usd,
                is_new_account=is_new,
                message=("Your account is open. Create an API key from the "
                         f"console's API key page. {tail}"),
            )
        logger.info("signup: issued key %s for account %s (new=%s)",
                    key.key_prefix, account_id, is_new)
        return SignupResponse(
            api_key=plaintext,
            key_prefix=key.key_prefix,
            balance_usd=balance_usd,
            is_new_account=is_new,
            message=f"Save this key now — it is shown only once. {tail}",
        )

    @router.post("/api/v1/signup/verified",
                 response_model=VerifiedSignupResponse)
    async def signup_verified(body: VerifiedSignupRequest,
                              request: Request) -> Any:
        # Function-level import: billing depends on auth, never the reverse.
        from EdennCode.Deployment.billing.account_index import (
            INDEX_KIND_FIREBASE,
            INDEX_KIND_PHONE,
            normalize_phone,
            normalize_uid,
        )

        err = _blocked()
        if err is not None:
            return err
        if firebase_verifier is None:
            return _unavailable_detail()

        from EdennCode.Deployment.auth.middleware import parse_bearer

        presented, _ = parse_bearer(request.headers.get("Authorization"))
        if presented is None:
            return _invalid_identity()
        try:
            identity = await firebase_verifier.verify(presented)
        except Exception as exc:  # noqa: BLE001 - InvalidIdentityToken and kin
            logger.info("signup/verified: token rejected (%s)",
                        getattr(exc, "reason", exc))
            return _invalid_identity()

        uid = normalize_uid(identity.uid)
        if not uid:
            return _invalid_identity()
        # The uid leads. The phone rides along so the admin's /account-lookup
        # keeps working for verified accounts, and so a number registered in
        # the unverified era is adopted rather than duplicated.
        contacts: list[tuple[str, str]] = [(INDEX_KIND_FIREBASE, uid)]
        phone = normalize_phone(identity.phone_number)
        if phone:
            contacts.append((INDEX_KIND_PHONE, phone))

        return await _open_and_issue(
            contacts,
            _Profile(
                registered_name=body.registered_name,
                email="",  # this round verifies phones only
                phone=identity.phone_number.strip(),
                note=VERIFIED_SIGNUP_NOTE,
                key_name="",  # unused: this door issues no key
            ),
            issue_key=False,
        )

    @router.post("/api/v1/signup", response_model=SignupResponse)
    async def signup(body: SignupRequest,
                     x_admin_secret: Optional[str] = Header(default=None)) -> Any:
        # Internal tool since the verified door opened: an unverified public
        # endpoint next to a verified one makes the verification decorative.
        guard = admin_guard(x_admin_secret, admin_secret)
        if guard is not None:
            return guard

        from EdennCode.Deployment.billing.account_index import (
            INDEX_KIND_EMAIL,
            INDEX_KIND_PHONE,
            normalize_email,
            normalize_phone,
        )

        err = _blocked()
        if err is not None:
            return err
        if body.email.strip() and not EMAIL_RE.match(body.email.strip()):
            return _error(400, "Provide a valid email address.", "invalid_email")

        contacts: list[tuple[str, str]] = []
        phone = normalize_phone(body.phone)
        email = normalize_email(body.email)
        if phone:
            contacts.append((INDEX_KIND_PHONE, phone))
        if email:
            contacts.append((INDEX_KIND_EMAIL, email))
        if not contacts:
            return _error(400, "Provide a phone number or an email address.",
                          "missing_contact")

        return await _open_and_issue(
            contacts,
            _Profile(
                registered_name=body.registered_name,
                email=body.email.strip(),
                phone=body.phone.strip(),
                note=SIGNUP_NOTE,
                key_name=DEFAULT_KEY_NAME,
            ),
            issue_key=True,
        )

    return router


__all__ = [
    "DEFAULT_KEY_NAME",
    "EMAIL_RE",
    "SIGNUP_NOTE",
    "VERIFIED_SIGNUP_NOTE",
    "SignupRequest",
    "SignupResponse",
    "VerifiedSignupRequest",
    "VerifiedSignupResponse",
    "create_signup_router",
]
