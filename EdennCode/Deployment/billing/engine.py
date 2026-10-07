"""Billing engine: pricing resolution, billed-amount computation, wallet debit.

``BILLING_MODE`` semantics (mirrors AUTH_MODE):
  off      — engine inert; usage rows and requests are untouched.
  log      — completed jobs get billed fields on their ledger rows; no wallet
             debit, no request rejection (surfaces pricing gaps safely).
  enforce  — log behavior + wallet debit at terminal recording + submission
             gate (402/403) in the auth middleware.

Client-facing charge (USD billed to the account) is deliberately independent
of the internal USD cost fields on the same rows — those remain Edenn-side
cost accounting.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from typing import Any, Optional

from EdennCode.Deployment.billing.stores import (
    BillingStoreUnavailable,
    micros_to_usd,
)

BILLING_MODES = {"off", "log", "enforce"}

# Recorded endpoint labels -> billable product. Route paths are identical to
# the labels (verified against the FastAPI decorators), so this one map serves
# both the pricing engine and the middleware gate.
ENDPOINT_PRODUCT: dict[str, str] = {
    "/api/v1/jobs/video": "video_music",
    "/api/v1/jobs/async_video_music_gen": "video_music",
    "/api/v2/jobs/video-music": "video_music",
    "/api/v1/jobs/multi-image": "image_music",
    "/api/v1/jobs/async_multi-image": "image_music",
    "/api/v2/jobs/multi-image-music": "image_music",
    "/api/v1/jobs/audio-creative-edit": "audio_edit",
}
BILLABLE_PATHS: frozenset[str] = frozenset(ENDPOINT_PRODUCT)


def resolve_billing_mode(raw: str) -> str:
    value = (raw or "").strip().lower()
    return value if value in BILLING_MODES else "off"


def billed_units_for_duration(
    duration_s: float, *, unit_seconds: int = 1, min_billable_seconds: int = 0
) -> int:
    """Billable units for one delivered video: ``⌈max(d, floor) / unit⌉``.

    The two knobs are what separate the products' published rules, and both
    live on the price row rather than in code so a commercial change is a
    pricing edit, not a deploy:

    * 视频配乐 — ``unit_seconds=30``: a 30-second block is the unit, so 59s
      bills 2 blocks and 61s bills 3.
    * 多图配乐 — ``min_billable_seconds=15``: shorter jobs bill as 15 seconds,
      longer ones bill their actual (ceiled) length.

    Decimal with an explicit ROUND_CEILING rather than ``math.ceil`` on a
    float: the rounding direction of a charge should be stated, not inherited
    from binary floating point, and ``30.000000000000004`` seconds must not
    quietly become two blocks.
    """
    unit = max(1, int(unit_seconds))
    effective = max(Decimal(str(duration_s)), Decimal(int(min_billable_seconds)))
    if effective <= 0:
        return 0
    return int((effective / Decimal(unit)).to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class BillingComputation:
    billing_mode: str
    billed_units: int
    unit_price_micros: int
    billed_amount_micros: int
    price_track: str = "standard"
    # Metering snapshot for duration-based charges — what one unit meant on the
    # day of the charge. Snapshotted for the same reason the price is: after the
    # block size or the floor changes, a historical row must still be able to
    # explain its own units. Defaults describe plain per-second billing.
    unit_seconds: int = 1
    min_billable_seconds: int = 0
    # Four-source snapshot, populated only when a PricingResolver is wired.
    # Kept optional so the Table Storage path is byte-identical to before the
    # migration: nothing downstream changes until the resolver is connected.
    price_source: Optional[str] = None
    price_ref: Optional[int] = None
    list_unit_price_micros: Optional[int] = None


class BillingEngine:
    def __init__(
        self,
        *,
        mode: str,
        account_store: Any,
        txn_store: Any,
        pricing_store: Any,
        logger: logging.Logger,
        teaser_days: int = 90,
        low_balance_ratio: float = 0.10,
        index_store: Any = None,
        pricing_resolver: Any = None,
    ) -> None:
        self.mode = resolve_billing_mode(mode)
        self.account_store = account_store
        self.txn_store = txn_store
        self.pricing_store = pricing_store
        self.index_store = index_store
        # When set, replaces the teaser-vs-list branch below with the full
        # four-source resolution (contract > teaser > volume tier > list).
        self.pricing_resolver = pricing_resolver
        self._logger = logger
        self.teaser_days = int(teaser_days)
        self.low_balance_ratio = float(low_balance_ratio)

    @property
    def computes(self) -> bool:
        return self.mode in {"log", "enforce"}

    @property
    def debits(self) -> bool:
        return self.mode == "enforce"

    async def compute(
        self,
        *,
        endpoint: str,
        status: str,
        model_spec: Optional[str],
        video_duration_s: Optional[float],
        account_id: Optional[str] = None,
    ) -> Optional[BillingComputation]:
        """Billed units/amount for one terminal job; None when nothing to bill."""
        if not self.computes or self.pricing_store is None:
            return None
        if status != "completed":
            return None
        product = ENDPOINT_PRODUCT.get(endpoint)
        if product is None:
            return None
        try:
            pricing = await self.pricing_store.resolve(product, model_spec)
        except BillingStoreUnavailable as exc:
            self._logger.warning("billing: pricing lookup failed (%s)", exc)
            return None
        if pricing is None:
            self._logger.warning(
                "billing: unpriced product '%s' (endpoint %s, model_spec %s) — "
                "billed 0; add a pricing row.",
                product, endpoint, model_spec,
            )
            return None
        resolution = await self._resolve_price(product, model_spec, account_id)
        if resolution is not None:
            unit_price_micros = resolution.unit_price_micros
            price_track = resolution.price_track
            billing_mode = resolution.billing_mode or pricing.billing_mode
        else:
            unit_price_micros = pricing.unit_price_micros
            price_track = "standard"
            billing_mode = pricing.billing_mode
            if (pricing.teaser_unit_price_micros is not None
                    and account_id and self.account_store is not None
                    and await self._in_teaser_window(account_id)):
                unit_price_micros = pricing.teaser_unit_price_micros
                price_track = "teaser"
        # Metering comes from the product's list row even when a contract or a
        # discount decided the price: what a unit *is* is a property of the
        # product, what it costs is a property of the deal.
        unit_seconds = max(1, int(getattr(pricing, "unit_seconds", 1) or 1))
        min_billable_seconds = max(
            0, int(getattr(pricing, "min_billable_seconds", 0) or 0))
        if billing_mode == "per_second":
            if video_duration_s is None or video_duration_s <= 0:
                # Deliberately not floored to the minimum: a floor turns a known
                # short video into a fair charge, but it would turn an *unknown*
                # duration into an invented one.
                self._logger.warning(
                    "billing: per_second pricing but no video duration for "
                    "endpoint %s — billed 0 units.", endpoint,
                )
                units = 0
            else:
                units = billed_units_for_duration(
                    float(video_duration_s),
                    unit_seconds=unit_seconds,
                    min_billable_seconds=min_billable_seconds,
                )
        else:  # per_request
            units = 1
            unit_seconds, min_billable_seconds = 1, 0
        return BillingComputation(
            billing_mode=billing_mode,
            billed_units=units,
            unit_price_micros=unit_price_micros,
            billed_amount_micros=units * unit_price_micros,
            price_track=price_track,
            unit_seconds=unit_seconds,
            min_billable_seconds=min_billable_seconds,
            price_source=None if resolution is None else resolution.price_source,
            price_ref=None if resolution is None else resolution.price_ref,
            list_unit_price_micros=(
                None if resolution is None else resolution.list_unit_price_micros),
        )

    async def _resolve_price(self, product, model_spec, account_id):
        """Four-source resolution, or None to fall back to the legacy branch.

        Never raises: a pricing lookup that fails must not stop a completed job
        from being billed at the market price.
        """
        if self.pricing_resolver is None:
            return None
        created_at = None
        total_recharged = 0
        if account_id and self.account_store is not None:
            try:
                snapshot = await self.account_store.get_balance_cached(account_id)
                if snapshot is not None:
                    created_at = (datetime.fromisoformat(snapshot.created_at)
                                  if snapshot.created_at else None)
                    total_recharged = snapshot.total_recharged_micros
            except Exception:  # noqa: BLE001 - doubt resolves to no discount
                self._logger.warning(
                    "billing: account snapshot unavailable for '%s'; resolving "
                    "at market price.", account_id, exc_info=True)
        try:
            return await self.pricing_resolver.resolve(
                product=product, model_spec=model_spec, account_id=account_id,
                account_created_at=created_at,
                total_recharged_micros=total_recharged,
            )
        except Exception:  # noqa: BLE001
            self._logger.warning(
                "billing: price resolution failed for %s; falling back.",
                product, exc_info=True)
            return None

    async def _in_teaser_window(self, account_id: str) -> bool:
        """True only when confidently inside the window; never raises."""
        try:
            snapshot = await self.account_store.get_balance_cached(account_id)
            if snapshot is None or not snapshot.created_at:
                return False
            created = datetime.fromisoformat(snapshot.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < created + timedelta(
                days=self.teaser_days)
        except Exception:  # noqa: BLE001 - doubt resolves to standard price
            self._logger.warning(
                "billing: teaser window check failed for '%s'; standard price.",
                account_id, exc_info=True)
            return False

    @staticmethod
    def enrich_row(row: dict, comp: Optional[BillingComputation]) -> None:
        """Add billing fields to a usage-ledger row (no-op when comp is None)."""
        if comp is None:
            return
        row["billing_mode"] = comp.billing_mode
        row["billed_units"] = comp.billed_units
        if comp.billing_mode == "per_second":
            # Only on duration-based rows: on a per-request charge these two
            # would read as a metering rule that was never applied.
            row["unit_seconds"] = comp.unit_seconds
            row["min_billable_seconds"] = comp.min_billable_seconds
        row["unit_price_usd"] = micros_to_usd(comp.unit_price_micros)
        row["billed_amount_usd"] = micros_to_usd(comp.billed_amount_micros)
        # Micros are the ground truth; string-encoded like all persisted micros.
        row["billed_amount_micros"] = str(comp.billed_amount_micros)
        row["price_track"] = comp.price_track
        # Only present once a resolver is wired; the ledger derives the legacy
        # two-track label from price_track when these are absent.
        if comp.price_source is not None:
            row["price_source"] = comp.price_source
        if comp.price_ref is not None:
            row["price_ref"] = comp.price_ref
        if comp.list_unit_price_micros is not None:
            row["list_unit_price_micros"] = str(comp.list_unit_price_micros)

    async def debit_for_job(
        self, *, account_id: str, job_id: str, comp: BillingComputation
    ) -> None:
        """Idempotent wallet debit at terminal recording. Never raises."""
        if comp.billed_amount_micros <= 0:
            return
        if self.account_store is None or self.txn_store is None:
            self._logger.warning(
                "billing: debit skipped for job %s — wallet storage unavailable.",
                job_id,
            )
            return
        try:
            if await self.txn_store.debit_exists(account_id, job_id):
                return
            new_balance = await self.account_store.adjust_balance(
                account_id, -comp.billed_amount_micros
            )
            if new_balance is None:
                self._logger.warning(
                    "billing: no account '%s' to debit for job %s "
                    "(%s micro-USD uncollected).",
                    account_id, job_id, comp.billed_amount_micros,
                )
                return
            await self.txn_store.write_debit(
                account_id, job_id, -comp.billed_amount_micros, new_balance
            )
        except Exception:  # noqa: BLE001 - billing must never break recording
            self._logger.warning(
                "billing: debit failed for job %s (account %s)",
                job_id, account_id, exc_info=True,
            )


__all__ = [
    "BILLABLE_PATHS",
    "BILLING_MODES",
    "BillingComputation",
    "BillingEngine",
    "ENDPOINT_PRODUCT",
    "billed_units_for_duration",
    "resolve_billing_mode",
]
