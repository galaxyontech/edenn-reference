"""One place that decides which billing store answers, and which one mirrors.

Three states, derived from two settings rather than from a mode enum:

    BILLING_DATABASE_URL unset          -> storage      (today, unchanged)
    set                                 -> dual         (Postgres warms up)
    set + BILLING_PG_PRIMARY truthy     -> pg           (Postgres answers)

The middle state is where the migration actually lives. Both stores stay
complete in ``dual`` and ``pg`` alike, so the cutover is a single environment
variable in either direction — which is the only way a billing cutover should
ever be done.

Everything is best-effort at construction time: a missing Table Storage
credential or an unreachable database yields ``None`` for that store and a
warning, never a failed boot. The routers already treat a missing store as
503; a process that refuses to start takes the whole API down instead.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

STORE_MODES = ("storage", "dual", "pg")


@dataclass
class BillingStores:
    """What the API wires up. ``mode`` is for logging and the health check."""

    mode: str
    account_store: Any = None
    txn_store: Any = None
    pricing_store: Any = None
    index_store: Any = None
    key_store: Any = None
    usage_mirror: Any = None
    usage_reader: Any = None          # PG-backed 详单 source, when pg is primary
    contract_source: Any = None
    tier_source: Any = None
    pool: Any = None


def resolve_store_mode(settings: Any) -> str:
    dsn = getattr(settings, "billing_database_url", None)
    if not dsn:
        return "storage"
    return "pg" if getattr(settings, "billing_pg_primary", False) else "dual"


def build_billing_stores(settings: Any, logger: logging.Logger) -> BillingStores:
    from EdennCode.Deployment.auth.key_store import ApiKeyStore
    from EdennCode.Deployment.billing.account_index import AccountIndexStore
    from EdennCode.Deployment.billing.stores import (
        AccountStore, PricingStore, WalletTxnStore,
    )

    mode = resolve_store_mode(settings)

    storage = BillingStores(
        mode="storage",
        account_store=AccountStore.from_settings(settings, logger),
        txn_store=WalletTxnStore.from_settings(settings, logger),
        pricing_store=PricingStore.from_settings(settings, logger),
        index_store=AccountIndexStore.from_settings(settings, logger),
        key_store=ApiKeyStore.from_settings(settings, logger),
    )
    if mode == "storage":
        return storage

    from EdennCode.Deployment.billing.pg_key_store import (
        PgAccountIndexStore, PgApiKeyStore,
    )
    from EdennCode.Deployment.billing.pg_stores import (
        BillingPgPool, PgAccountStore, PgContractPriceSource,
        PgDiscountTierSource, PgPricingStore, PgUsageLedger, PgWalletTxnStore,
    )

    pool = BillingPgPool.from_settings(settings, logger)
    if pool is None:  # pragma: no cover - resolve_store_mode already checked
        return storage

    pg = BillingStores(
        mode="pg",
        account_store=PgAccountStore(pool, logger=logger),
        txn_store=PgWalletTxnStore(pool, logger=logger),
        pricing_store=PgPricingStore(pool, logger=logger),
        index_store=PgAccountIndexStore(pool, logger=logger),
        key_store=PgApiKeyStore(pool, logger=logger),
    )
    ledger = PgUsageLedger(pool, logger=logger)

    from EdennCode.Deployment.billing.dual_write import (
        DualAccountIndexStore, DualAccountStore, DualApiKeyStore,
        DualPricingStore, DualWalletTxnStore, build_usage_mirror,
    )

    primary, secondary = (pg, storage) if mode == "pg" else (storage, pg)

    def pair(name: str, wrapper):
        p, s = getattr(primary, name), getattr(secondary, name)
        if p is None:
            # No primary means that store is unconfigured; the secondary alone
            # is not a substitute (its data may be incomplete mid-migration).
            logger.warning("billing: %s has no primary store in mode '%s'",
                           name, mode)
            return None
        return wrapper(p, s, logger=logger)

    resolved = BillingStores(
        mode=mode,
        account_store=pair("account_store", DualAccountStore),
        txn_store=pair("txn_store", DualWalletTxnStore),
        pricing_store=pair("pricing_store", DualPricingStore),
        index_store=pair("index_store", DualAccountIndexStore),
        key_store=pair("key_store", DualApiKeyStore),
        # Usage rows always mirror INTO Postgres: the recorder writes to Table
        # Storage on its own path, so the mirror is the Postgres side in both
        # directions. After cutover the mirror keeps Table Storage's ledger
        # from being the only complete one — it is the rollback path.
        usage_mirror=build_usage_mirror(ledger, logger),
        usage_reader=ledger if mode == "pg" else None,
        contract_source=PgContractPriceSource(pool),
        tier_source=PgDiscountTierSource(pool),
        pool=pool,
    )
    logger.info(
        "billing stores: mode=%s accounts=%s wallet=%s pricing=%s index=%s "
        "keys=%s usage_reader=%s",
        mode,
        "on" if resolved.account_store else "off",
        "on" if resolved.txn_store else "off",
        "on" if resolved.pricing_store else "off",
        "on" if resolved.index_store else "off",
        "on" if resolved.key_store else "off",
        "pg" if resolved.usage_reader else "storage",
    )
    return resolved


__all__ = ["BillingStores", "STORE_MODES", "build_billing_stores",
           "resolve_store_mode"]
