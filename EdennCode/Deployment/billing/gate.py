"""Submission balance gate, consulted by the auth middleware for billable POSTs.

Verdicts (enforce mode): missing account or balance <= 0 → 402
``insufficient_balance``; inactive account → 403 ``account_inactive`` (inactive
wins over broke — it is the stronger admin statement). Log mode records the
would-block line and lets the request through. Every failure path fails open:
billing must never take the API down — AUTH_MODE remains the security boundary.

On the pass path the gate also stashes ``request.state.balance_warning`` when
the wallet has dropped below ``low_balance_ratio`` of lifetime recharges; the
auth middleware turns that into a response header + JSON block.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from fastapi.responses import JSONResponse

from EdennCode.Deployment.billing.stores import BillingStoreUnavailable, micros_to_usd


def create_billing_gate(
    logger: logging.Logger,
) -> Callable[[Any, Any], Awaitable[Optional[JSONResponse]]]:
    async def gate(request: Any, principal: Any) -> Optional[JSONResponse]:
        try:
            from EdennCode.Deployment.billing import get_billing

            engine = get_billing()
            if engine is None or engine.mode == "off":
                return None
            if principal is None:
                return None  # nobody to bill (only reachable when AUTH_MODE != enforce)
            store = engine.account_store
            if store is None:
                return None
            try:
                snapshot = await store.get_balance_cached(principal.user_id)
            except BillingStoreUnavailable as exc:
                logger.warning(
                    "billing gate: balance read failed (%s); failing open.", exc
                )
                return None
            verdict: Optional[tuple[int, str, str]] = None
            if snapshot is None:
                verdict = (
                    402,
                    "Insufficient account balance. Ask your administrator to "
                    "create and recharge your billing account.",
                    "insufficient_balance",
                )
            elif not snapshot.is_active:
                verdict = (403, "Account is inactive.", "account_inactive")
            elif snapshot.balance_micros <= 0:
                verdict = (
                    402,
                    "Insufficient account balance. Recharge required.",
                    "insufficient_balance",
                )
            if verdict is None:
                ratio = getattr(engine, "low_balance_ratio", 0.10)
                total = snapshot.total_recharged_micros
                if total > 0 and snapshot.balance_micros < ratio * total:
                    request.state.balance_warning = {
                        "balance_usd": micros_to_usd(snapshot.balance_micros),
                        "threshold_usd": micros_to_usd(int(ratio * total)),
                        "message": (
                            "Wallet balance is below {:.0%} of the total "
                            "recharged amount. Please recharge soon."
                        ).format(ratio),
                    }
                return None
            if engine.mode == "log":
                logger.info(
                    "billing[log]: would block %s %s for user %s (%s)",
                    getattr(request, "method", "?"),
                    getattr(getattr(request, "url", None), "path", "?"),
                    principal.user_id,
                    verdict[2],
                )
                return None
            return JSONResponse(
                status_code=verdict[0],
                content={"detail": verdict[1], "code": verdict[2]},
            )
        except Exception:  # noqa: BLE001 - the gate must never take the API down
            logger.warning("billing gate error; failing open.", exc_info=True)
            return None

    return gate


__all__ = ["create_billing_gate"]
