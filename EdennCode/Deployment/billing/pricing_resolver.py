"""Four-source price resolution: contract > teaser > volume tier > list.

The first source that matches wins and the rest are skipped — discounts never
stack. Stacking is the classic revenue leak in billing systems, and when a
customer disputes a charge, "we applied three overlapping discounts in this
order" is not an explanation anyone accepts.

Every resolution produces the same snapshot regardless of which source won, so
a ledger row can always answer three questions years later: what was the market
price that day, what did this account actually pay, and under which policy.

Storage-agnostic by construction. ``resolve_price`` is a pure function; the
``PricingResolver`` wrapper adds the two optional lookups (contract, tiers) and
degrades to exactly today's behavior — teaser vs list — when neither source is
wired. That degradation is what makes the Table Storage → Postgres migration
safe to run half-finished: no account's price can change until the sources are
deliberately connected.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Optional, Sequence

PRICE_SOURCES = ("contract", "teaser", "volume_tier", "list")


@dataclass(frozen=True)
class ContractPrice:
    """One special customer's absolute price for one product (e.g. a contracted enterprise account).

    Absolute, not a discount rate: when the list price is raised later, a
    contract customer's price must not silently rise with it.
    """

    contract_id: int
    price_key: str
    billing_mode: str
    unit_price_micros: int


@dataclass(frozen=True)
class DiscountTier:
    """A global volume tier keyed on lifetime recharge, not current balance.

    Lifetime recharge only ever grows, so a discount earned is never lost by
    spending the balance down.
    """

    tier_id: int
    min_recharged_micros: int
    discount_rate: Decimal  # 0.1000 == 10% off; the schema enforces 0 <= r < 1


@dataclass(frozen=True)
class PriceResolution:
    """What to charge, plus everything needed to defend the charge later."""

    billing_mode: str
    unit_price_micros: int
    price_source: str
    list_unit_price_micros: Optional[int] = None
    price_ref: Optional[int] = None

    @property
    def price_track(self) -> str:
        """Legacy two-track label carried by Table Storage rows and the 详单.

        Kept so the existing 详单 field keeps its documented meaning while
        ``price_source`` carries the finer four-way truth.
        """
        return "teaser" if self.price_source == "teaser" else "standard"

    @property
    def discount_rate(self) -> Optional[Decimal]:
        """Mirrors the ledger's generated column; None when no list price."""
        if not self.list_unit_price_micros:
            return None
        return 1 - Decimal(self.unit_price_micros) / Decimal(
            self.list_unit_price_micros
        )


def apply_discount(list_unit_price_micros: int, rate: Decimal) -> int:
    """``floor(list × (1 - rate))``, floored on purpose — rounding favors the
    customer, and an unstated rounding direction is a recurring source of
    billing disputes.

    Clamped to at least 1 micro when the list price is non-zero: a *discount*
    that reaches zero is an arithmetic accident, not a giveaway. Genuinely free
    service is expressed as a contract price of 0, which is explicit.
    """
    discounted = (Decimal(list_unit_price_micros) * (Decimal(1) - rate)).to_integral_value(
        rounding=ROUND_FLOOR
    )
    floor_micros = 1 if list_unit_price_micros > 0 else 0
    return max(int(discounted), floor_micros)


def select_tier(
    tiers: Sequence[DiscountTier], total_recharged_micros: int
) -> Optional[DiscountTier]:
    """Highest threshold at or below lifetime recharge; None when none apply.

    Zero-rate tiers are ignored: they would label a full-price charge
    ``volume_tier``, which reads as "a discount was applied" in the 详单.
    """
    eligible = [
        t
        for t in tiers
        if t.min_recharged_micros <= total_recharged_micros and t.discount_rate > 0
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda t: t.min_recharged_micros)


def in_teaser_window(
    account_created_at: Optional[datetime], now: datetime, teaser_days: int
) -> bool:
    if account_created_at is None or teaser_days <= 0:
        return False
    created = account_created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return now < created + timedelta(days=teaser_days)


def resolve_price(
    *,
    pricing: Any,
    contract: Optional[ContractPrice] = None,
    tiers: Sequence[DiscountTier] = (),
    account_created_at: Optional[datetime] = None,
    total_recharged_micros: int = 0,
    now: Optional[datetime] = None,
    teaser_days: int = 90,
) -> Optional[PriceResolution]:
    """Resolve one charge. Pure — no I/O, no clock unless ``now`` is omitted.

    ``pricing`` is any object exposing ``billing_mode``, ``unit_price_micros``
    and ``teaser_unit_price_micros`` (``PricingRecord`` today, an asyncpg row
    tomorrow), or None when the product has no list price at all.

    Returns None only when there is nothing to charge against: no list price
    and no contract. The caller bills 0 and logs — an unpriced product must
    never guess a number.
    """
    now = now or datetime.now(timezone.utc)
    list_price: Optional[int] = None
    list_mode: Optional[str] = None
    if pricing is not None:
        list_price = int(pricing.unit_price_micros)
        list_mode = str(pricing.billing_mode)

    # ① Contract — an explicit agreement outranks every automatic rule.
    if contract is not None:
        return PriceResolution(
            # The contract's own billing mode governs: the agreement defines
            # what a unit is, not the list page.
            billing_mode=contract.billing_mode or (list_mode or ""),
            unit_price_micros=int(contract.unit_price_micros),
            price_source="contract",
            list_unit_price_micros=list_price,
            price_ref=contract.contract_id,
        )

    if pricing is None or list_price is None or list_mode is None:
        return None

    # ② Teaser — the new-account introductory price.
    teaser = getattr(pricing, "teaser_unit_price_micros", None)
    if teaser is not None and in_teaser_window(account_created_at, now, teaser_days):
        return PriceResolution(
            billing_mode=list_mode,
            unit_price_micros=int(teaser),
            price_source="teaser",
            list_unit_price_micros=list_price,
        )

    # ③ Volume tier — earned by lifetime recharge.
    tier = select_tier(tiers, total_recharged_micros)
    if tier is not None:
        return PriceResolution(
            billing_mode=list_mode,
            unit_price_micros=apply_discount(list_price, tier.discount_rate),
            price_source="volume_tier",
            list_unit_price_micros=list_price,
            price_ref=tier.tier_id,
        )

    # ④ List.
    return PriceResolution(
        billing_mode=list_mode,
        unit_price_micros=list_price,
        price_source="list",
        list_unit_price_micros=list_price,
    )


class PricingResolver:
    """Wires the two optional lookups around :func:`resolve_price`.

    ``contract_source`` and ``tier_source`` are duck-typed and optional:

    * ``contract_source.get_active(account_id, price_key) -> ContractPrice|None``
    * ``tier_source.list_active() -> Sequence[DiscountTier]``

    Either being None disables that source. A lookup that *raises* also
    disables it for that one call — a pricing table being briefly unreachable
    must degrade to the market price, never block a completed job from being
    billed at all.
    """

    def __init__(
        self,
        *,
        pricing_store: Any,
        logger: Any,
        contract_source: Any = None,
        tier_source: Any = None,
        teaser_days: int = 90,
    ) -> None:
        self._pricing_store = pricing_store
        self._logger = logger
        self._contract_source = contract_source
        self._tier_source = tier_source
        self.teaser_days = int(teaser_days)

    async def resolve(
        self,
        *,
        product: str,
        model_spec: Optional[str],
        account_id: Optional[str] = None,
        account_created_at: Optional[datetime] = None,
        total_recharged_micros: int = 0,
        now: Optional[datetime] = None,
    ) -> Optional[PriceResolution]:
        pricing = await self._pricing_store.resolve(product, model_spec)
        price_key = getattr(pricing, "price_key", None) or product
        contract = None
        if account_id and self._contract_source is not None:
            contract = await self._safe(
                self._contract_source.get_active(account_id, price_key),
                "contract lookup",
            )
        tiers: Sequence[DiscountTier] = ()
        if account_id and self._tier_source is not None and contract is None:
            tiers = await self._safe(self._tier_source.list_active(), "tier lookup") or ()
        return resolve_price(
            pricing=pricing,
            contract=contract,
            tiers=tiers,
            account_created_at=account_created_at,
            total_recharged_micros=total_recharged_micros,
            now=now,
            teaser_days=self.teaser_days,
        )

    async def _safe(self, awaitable: Any, label: str) -> Any:
        try:
            return await awaitable
        except Exception:  # noqa: BLE001 - degrade to the market price, never fail the bill
            self._logger.warning("pricing: %s failed; skipping source", label,
                                 exc_info=True)
            return None


__all__ = [
    "PRICE_SOURCES",
    "ContractPrice",
    "DiscountTier",
    "PriceResolution",
    "PricingResolver",
    "apply_discount",
    "in_teaser_window",
    "resolve_price",
    "select_tier",
]
