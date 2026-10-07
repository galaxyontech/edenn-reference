"""Billing package: USD wallet, per-model pricing, gate, admin/user routers.

Process-wide singleton mirrors the usage-recorder pattern: ``resolve_billing``
is called once by api.py and worker_main; everything else reads
``get_billing()``. The engine is built for ANY mode (stores exist whenever
storage credentials do) so admin/user endpoints work while BILLING_MODE=off —
enrichment, debits, and the gate are what the mode controls.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from EdennCode.Deployment.billing.account_index import (
    AccountIndexStore,
    INDEX_KIND_EMAIL,
    INDEX_KIND_PHONE,
    normalize_email,
    normalize_phone,
)
from EdennCode.Deployment.billing.engine import (
    BILLABLE_PATHS,
    BILLING_MODES,
    BillingComputation,
    BillingEngine,
    ENDPOINT_PRODUCT,
    resolve_billing_mode,
)
from EdennCode.Deployment.billing.stores import (
    AccountExists,
    AccountRecord,
    AccountStore,
    BalanceSnapshot,
    BillingStoreUnavailable,
    MICROS_PER_USD,
    PricingRecord,
    PricingStore,
    WalletTxnStore,
    rmb_to_micros,
    usd_to_micros,
    micros_to_usd,
)

_singleton: Optional[BillingEngine] = None
_singleton_resolved = False
_stores: Any = None


def set_billing_override(engine: Optional[BillingEngine]) -> None:
    global _singleton, _singleton_resolved, _stores
    _singleton = engine
    _singleton_resolved = engine is not None
    if engine is None:
        _stores = None


def get_billing() -> Optional[BillingEngine]:
    return _singleton


def get_billing_stores() -> Any:
    """The resolved store set, or None before ``resolve_billing`` has run.

    api.py reads the API-key store and the usage mirror from here so there is
    exactly one place that decides Table Storage vs Postgres — two independent
    decisions would eventually disagree, and a key store that disagrees with
    the wallet store is a customer who can authenticate but cannot be billed.
    """
    return _stores


def resolve_billing(
    settings: Any = None, logger: Optional[logging.Logger] = None
) -> Optional[BillingEngine]:
    """Build the process-wide engine once (api.py and worker_main both call this)."""
    global _singleton, _singleton_resolved
    if _singleton_resolved:
        return _singleton
    log = logger or logging.getLogger(__name__)
    if settings is None:
        from EdennCode.Deployment.settings import DeploymentSettings

        try:
            settings = DeploymentSettings.from_env()
        except Exception as exc:  # noqa: BLE001
            log.warning("billing: engine disabled (settings unavailable: %s)", exc)
            _singleton_resolved = True
            return None
    from EdennCode.Deployment.billing.store_factory import build_billing_stores

    mode = resolve_billing_mode(getattr(settings, "billing_mode", "") or "")
    global _stores
    _stores = build_billing_stores(settings, log)
    teaser_days = getattr(settings, "billing_teaser_days", 90)

    resolver = None
    if _stores.contract_source is not None or _stores.tier_source is not None:
        from EdennCode.Deployment.billing.pricing_resolver import PricingResolver

        resolver = PricingResolver(
            pricing_store=_stores.pricing_store, logger=log,
            contract_source=_stores.contract_source,
            tier_source=_stores.tier_source, teaser_days=teaser_days,
        )

    engine = BillingEngine(
        mode=mode,
        account_store=_stores.account_store,
        txn_store=_stores.txn_store,
        pricing_store=_stores.pricing_store,
        logger=log,
        teaser_days=teaser_days,
        low_balance_ratio=getattr(settings, "billing_low_balance_ratio", 0.10),
        index_store=_stores.index_store,
        pricing_resolver=resolver,
    )
    log.info(
        "billing: mode=%s store=%s accounts=%s wallet=%s pricing=%s index=%s "
        "resolver=%s",
        engine.mode, _stores.mode,
        "on" if engine.account_store else "off",
        "on" if engine.txn_store else "off",
        "on" if engine.pricing_store else "off",
        "on" if engine.index_store else "off",
        "four-source" if resolver else "teaser-only",
    )
    _singleton = engine
    _singleton_resolved = True
    return engine


__all__ = [
    "AccountExists",
    "AccountIndexStore",
    "AccountRecord",
    "AccountStore",
    "BILLABLE_PATHS",
    "BILLING_MODES",
    "BalanceSnapshot",
    "BillingComputation",
    "BillingEngine",
    "BillingStoreUnavailable",
    "ENDPOINT_PRODUCT",
    "INDEX_KIND_EMAIL",
    "INDEX_KIND_PHONE",
    "MICROS_PER_USD",
    "PricingRecord",
    "PricingStore",
    "WalletTxnStore",
    "normalize_email",
    "normalize_phone",
    "rmb_to_micros",
    "usd_to_micros",
    "get_billing",
    "get_billing_stores",
    "micros_to_usd",
    "resolve_billing",
    "resolve_billing_mode",
    "set_billing_override",
]
