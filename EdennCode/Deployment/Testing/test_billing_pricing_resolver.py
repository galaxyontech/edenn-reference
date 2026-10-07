"""Four-source price resolution: priority, no stacking, rounding, degradation."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from EdennCode.Deployment.billing.pricing_resolver import (
    ContractPrice,
    DiscountTier,
    PriceResolution,
    PricingResolver,
    apply_discount,
    in_teaser_window,
    resolve_price,
    select_tier,
)

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)

# Live seeded values: 视频配乐 ¥0.9/次 standard, ¥0.45/次 teaser (@7.0).
VIDEO = SimpleNamespace(
    price_key="video_music",
    billing_mode="per_request",
    unit_price_micros=128571,
    teaser_unit_price_micros=64286,
)
NO_TEASER = SimpleNamespace(
    price_key="audio_edit",
    billing_mode="per_request",
    unit_price_micros=200000,
    teaser_unit_price_micros=None,
)

TIERS = [
    DiscountTier(tier_id=1, min_recharged_micros=500_000_000, discount_rate=Decimal("0.03")),
    DiscountTier(tier_id=2, min_recharged_micros=2_000_000_000, discount_rate=Decimal("0.07")),
    DiscountTier(tier_id=3, min_recharged_micros=10_000_000_000, discount_rate=Decimal("0.12")),
]

ENTERPRISE = ContractPrice(
    contract_id=42,
    price_key="video_music",
    billing_mode="per_request",
    unit_price_micros=30000,
)


def _resolve(**kwargs):
    base = dict(pricing=VIDEO, now=NOW, teaser_days=90)
    base.update(kwargs)
    return resolve_price(**base)


# -- priority order ------------------------------------------------------


def test_list_price_when_nothing_else_applies():
    r = _resolve(account_created_at=NOW - timedelta(days=200))
    assert (r.price_source, r.unit_price_micros) == ("list", 128571)
    assert r.list_unit_price_micros == 128571
    assert r.price_ref is None


def test_teaser_wins_inside_the_window():
    r = _resolve(account_created_at=NOW - timedelta(days=30))
    assert (r.price_source, r.unit_price_micros) == ("teaser", 64286)


def test_teaser_expires_after_the_window():
    r = _resolve(account_created_at=NOW - timedelta(days=91))
    assert r.price_source == "list"


def test_tier_applies_only_outside_the_teaser_window():
    aged = NOW - timedelta(days=200)
    r = _resolve(account_created_at=aged, tiers=TIERS,
                 total_recharged_micros=2_000_000_000)
    assert (r.price_source, r.price_ref) == ("volume_tier", 2)
    # 7% off 128571 -> floor(119571.03) == 119571
    assert r.unit_price_micros == 119571


def test_teaser_beats_tier_no_stacking():
    """A new account that also recharged a lot gets ONE discount, the teaser."""
    r = _resolve(account_created_at=NOW - timedelta(days=10), tiers=TIERS,
                 total_recharged_micros=10_000_000_000)
    assert (r.price_source, r.unit_price_micros) == ("teaser", 64286)


def test_contract_beats_everything():
    r = _resolve(contract=ENTERPRISE, account_created_at=NOW - timedelta(days=10),
                 tiers=TIERS, total_recharged_micros=10_000_000_000)
    assert (r.price_source, r.unit_price_micros, r.price_ref) == ("contract", 30000, 42)
    # Market price is still snapshotted so the concession is computable.
    assert r.list_unit_price_micros == 128571


def test_contract_billing_mode_governs():
    per_second = ContractPrice(contract_id=9, price_key="video_music",
                               billing_mode="per_second", unit_price_micros=1000)
    assert _resolve(contract=per_second).billing_mode == "per_second"


def test_contract_without_any_list_price():
    r = resolve_price(pricing=None, contract=ENTERPRISE, now=NOW)
    assert (r.price_source, r.unit_price_micros) == ("contract", 30000)
    assert r.list_unit_price_micros is None
    assert r.discount_rate is None  # nothing to compare against


def test_unpriced_product_resolves_to_none():
    assert resolve_price(pricing=None, now=NOW) is None


def test_product_without_teaser_price_falls_through():
    r = resolve_price(pricing=NO_TEASER, account_created_at=NOW, now=NOW)
    assert r.price_source == "list"


# -- tier selection ------------------------------------------------------


def test_tier_picks_the_highest_threshold_reached():
    assert select_tier(TIERS, 10_000_000_000).tier_id == 3
    assert select_tier(TIERS, 2_000_000_000).tier_id == 2
    assert select_tier(TIERS, 1_999_999_999).tier_id == 1


def test_tier_threshold_is_inclusive():
    assert select_tier(TIERS, 500_000_000).tier_id == 1
    assert select_tier(TIERS, 499_999_999) is None


def test_no_tier_below_the_lowest_threshold():
    assert select_tier(TIERS, 0) is None
    assert select_tier([], 10_000_000_000) is None


def test_zero_rate_tier_is_ignored():
    """Otherwise a full-price charge would be labeled 'volume_tier' in 详单."""
    zero = [DiscountTier(tier_id=7, min_recharged_micros=0, discount_rate=Decimal("0"))]
    assert select_tier(zero, 1_000_000) is None


# -- rounding ------------------------------------------------------------


def test_discount_rounds_down_in_the_customers_favor():
    # 128571 * 0.93 = 119571.03 -> 119571, not 119572
    assert apply_discount(128571, Decimal("0.07")) == 119571


def test_zero_discount_is_the_list_price():
    assert apply_discount(128571, Decimal("0")) == 128571


def test_discount_never_reaches_zero():
    """A discount that floors to 0 is an arithmetic accident, not a giveaway."""
    assert apply_discount(1, Decimal("0.99")) == 1


def test_zero_list_price_stays_zero():
    assert apply_discount(0, Decimal("0.5")) == 0


# -- teaser window -------------------------------------------------------


def test_naive_created_at_is_read_as_utc():
    naive = (NOW - timedelta(days=1)).replace(tzinfo=None)
    assert in_teaser_window(naive, NOW, 90) is True


def test_window_boundary_is_exclusive():
    exactly_90 = NOW - timedelta(days=90)
    assert in_teaser_window(exactly_90, NOW, 90) is False


def test_missing_created_at_is_not_in_window():
    assert in_teaser_window(None, NOW, 90) is False


def test_zero_teaser_days_disables_the_window():
    assert in_teaser_window(NOW, NOW, 0) is False


# -- snapshot fields -----------------------------------------------------


def test_discount_rate_matches_the_ledgers_generated_column():
    r = PriceResolution(billing_mode="per_request", unit_price_micros=64286,
                        price_source="teaser", list_unit_price_micros=128571)
    assert r.discount_rate == pytest.approx(Decimal("0.5"), abs=Decimal("0.0001"))


def test_price_track_keeps_its_documented_two_track_meaning():
    def track(source):
        return PriceResolution(billing_mode="per_request", unit_price_micros=1,
                               price_source=source).price_track

    assert track("teaser") == "teaser"
    assert track("list") == "standard"
    assert track("contract") == "standard"
    assert track("volume_tier") == "standard"


# -- resolver wiring -----------------------------------------------------


class _Pricing:
    def __init__(self, record=VIDEO):
        self._record = record

    async def resolve(self, product, model_spec):
        return self._record


class _Contracts:
    def __init__(self, contract=None, boom=False):
        self._contract = contract
        self._boom = boom
        self.calls = 0

    async def get_active(self, account_id, price_key):
        self.calls += 1
        if self._boom:
            raise RuntimeError("contract table unreachable")
        return self._contract


class _Tiers:
    def __init__(self, tiers=TIERS, boom=False):
        self._tiers = tiers
        self._boom = boom
        self.calls = 0

    async def list_active(self):
        self.calls += 1
        if self._boom:
            raise RuntimeError("tier table unreachable")
        return self._tiers


def _resolver(**kwargs):
    return PricingResolver(pricing_store=_Pricing(),
                           logger=logging.getLogger("t"), **kwargs)


@pytest.mark.asyncio
async def test_unwired_resolver_reproduces_todays_behavior():
    """Migration safety: with no sources wired, nobody's price can change."""
    r = await _resolver().resolve(product="video_music", model_spec=None,
                                  account_id="acct-1",
                                  account_created_at=NOW - timedelta(days=200),
                                  total_recharged_micros=10_000_000_000, now=NOW)
    assert (r.price_source, r.unit_price_micros) == ("list", 128571)


@pytest.mark.asyncio
async def test_resolver_applies_a_wired_contract():
    r = await _resolver(contract_source=_Contracts(ENTERPRISE)).resolve(
        product="video_music", model_spec=None, account_id="acct-1", now=NOW)
    assert (r.price_source, r.unit_price_micros) == ("contract", 30000)


@pytest.mark.asyncio
async def test_contract_hit_skips_the_tier_lookup():
    tiers = _Tiers()
    await _resolver(contract_source=_Contracts(ENTERPRISE), tier_source=tiers).resolve(
        product="video_music", model_spec=None, account_id="acct-1", now=NOW)
    assert tiers.calls == 0


@pytest.mark.asyncio
async def test_anonymous_request_queries_neither_source():
    contracts, tiers = _Contracts(ENTERPRISE), _Tiers()
    r = await _resolver(contract_source=contracts, tier_source=tiers).resolve(
        product="video_music", model_spec=None, account_id=None, now=NOW)
    assert (contracts.calls, tiers.calls) == (0, 0)
    assert r.price_source == "list"


@pytest.mark.asyncio
async def test_contract_source_failure_degrades_to_market_price():
    """A briefly unreachable pricing table must not stop a job being billed."""
    r = await _resolver(contract_source=_Contracts(boom=True)).resolve(
        product="video_music", model_spec=None, account_id="acct-1", now=NOW)
    assert (r.price_source, r.unit_price_micros) == ("list", 128571)


@pytest.mark.asyncio
async def test_tier_source_failure_degrades_to_market_price():
    r = await _resolver(tier_source=_Tiers(boom=True)).resolve(
        product="video_music", model_spec=None, account_id="acct-1",
        total_recharged_micros=10_000_000_000, now=NOW)
    assert (r.price_source, r.unit_price_micros) == ("list", 128571)
