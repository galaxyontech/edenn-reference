"""Per-job usage/cost ledger: one Table Storage row + one OTel event per job.

Counts are ground truth; costs are computed conveniences (recomputable when
unit prices are corrected against real invoices). Recording is fire-and-forget:
failures log a warning with the job_id and never affect the request or job.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from EdennCode.Deployment.auth.key_store import Principal
from EdennCode.Deployment.auth.telemetry import emit_usage_event
from EdennCode.Deployment.pipeline_telemetry import provider_for_modelspec

USAGE_TABLE_BASE_NAME = "usage"
DEFAULT_MUSIC_UNIT_COST_USD = 0.065

# Canonical per-track generation unit cost by provider (USD) — the price table
# the ledger bills from. Resolution order in music_unit_cost_usd:
# env MUSIC_UNIT_COST_{PROVIDER} > this dict > DEFAULT_MUSIC_UNIT_COST_USD.
# All rates currently equal the historical flat rate (kept as the billing
# number per 2026-07-19 decision); correct a provider here or per deployment
# via env and ledger totals recompute from counts, which are ground truth.
PROVIDER_UNIT_COST_USD: dict[str, float] = {
    "provider_a": 0.065,  # edenn_basic
    "provider_b": 0.065,      # edenn_enhanced
    "provider_c": 0.065,        # edenn_studio
    "provider_d": 0.065,     # no modelspec mapping yet; billable via provider override
}

# Upstream vendor names must never be persisted or emitted (same policy as the
# client-facing response guardrail): rows, telemetry events, and admin reads
# all carry the Edenn-branded label instead. The raw provider name lives only
# in-process, for price-table lookup. Unmapped providers collapse to
# "edenn_other" rather than leaking a new vendor name by default.
PROVIDER_PUBLIC_LABEL: dict[str, str] = {
    "provider_a": "edenn_basic",
    "provider_b": "edenn_enhanced",
    "provider_c": "edenn_studio",
    "provider_d": "edenn_agentic",
}
_UNKNOWN_PROVIDER_LABEL = "edenn_other"
_ANONYMOUS_PARTITION = "anonymous"


def provider_public_label(provider: Optional[str]) -> str:
    """Branded, vendor-free label for a provider; '' when there is no provider."""
    if not provider:
        return ""
    return PROVIDER_PUBLIC_LABEL.get(
        provider.strip().lower(), _UNKNOWN_PROVIDER_LABEL
    )

_singleton: Optional["UsageRecorder"] = None
_singleton_resolved = False

# asyncio.Task holds only a weak reference internally; without a strong
# reference held elsewhere, a fire-and-forget task can be garbage-collected
# before it runs (the pending billing write silently never happens). Keeping
# a module-level strong-ref set, discarded via the task's done callback, is
# the standard workaround (see asyncio.create_task docs).
_background_tasks: set = set()


def music_unit_cost_usd(provider: Optional[str]) -> float:
    """Unit cost of one generation call: env override > price table > default."""
    if provider:
        raw = os.getenv(f"MUSIC_UNIT_COST_{provider.upper()}", "").strip()
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        table_rate = PROVIDER_UNIT_COST_USD.get(provider.strip().lower())
        if table_rate is not None:
            return table_rate
    return DEFAULT_MUSIC_UNIT_COST_USD


def _get(source: Any, name: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


class UsageRecorder:
    def __init__(
        self,
        table_client: Any,
        *,
        auth_mode: str,
        logger: logging.Logger,
        emit=emit_usage_event,
        mirror=None,
    ) -> None:
        self._table_client = table_client
        self._auth_mode = auth_mode
        self._logger = logger
        self._emit = emit
        # Optional async callable receiving each finished row — the Postgres
        # dual-write during the billing migration. Independent of the Table
        # Storage write above it: either store may fail without the other.
        self._mirror = mirror
        # Optional read-side override (the Postgres ledger after cutover).
        # Writes keep going through this recorder either way, so the two
        # stores stay in step and the cutover stays reversible.
        self._reader = None

    # -- construction -----------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Any, logger: logging.Logger) -> Optional["UsageRecorder"]:
        from EdennCode.Deployment.auth.key_store import build_auth_table_client
        from EdennCode.Deployment.auth.middleware import resolve_auth_mode

        namespace = getattr(settings, "auth_table_namespace", "") or ""
        table_name = USAGE_TABLE_BASE_NAME + namespace
        # Supports both the connection string and the account-key triad the
        # Japan container apps use (AZURE_STORAGE_ACCOUNT_URL/NAME/KEY).
        table_client = build_auth_table_client(settings, table_name, logger)
        if table_client is not None:
            logger.info("UsageRecorder using table '%s'", table_name)
        else:
            logger.info(
                "UsageRecorder: no usable storage credentials; telemetry-only mode."
            )
        return cls(
            table_client,
            auth_mode=resolve_auth_mode(getattr(settings, "auth_mode", "") or ""),
            logger=logger,
        )

    # -- recording --------------------------------------------------------

    def record_job(
        self,
        *,
        job_id: str,
        endpoint: str,
        status: str,
        principal: Optional[Principal],
        model_spec: Optional[str] = None,
        token_usage: Optional[dict] = None,
        cost_metadata: Any = None,
        auth_mode: Optional[str] = None,
        latency_ms: Optional[int] = None,
        provider: Optional[str] = None,
        video_duration_s: Optional[float] = None,
    ) -> None:
        """Build + persist one ledger row and telemetry event. Never raises.

        ``provider`` overrides the modelspec-derived provider for paths that
        know it directly (e.g. provider_d, which has no modelspec mapping).
        ``video_duration_s`` is the delivered video length — the unit source
        for per-second billing and a 详单 display field.
        """
        try:
            row = self._build_row(
                job_id=job_id, endpoint=endpoint, status=status,
                principal=principal, model_spec=model_spec,
                token_usage=token_usage, cost_metadata=cost_metadata,
                auth_mode=auth_mode, latency_ms=latency_ms,
                provider=provider, video_duration_s=video_duration_s,
            )
            engine = _billing_engine()
            if engine is not None and getattr(engine, "computes", False):
                # Billing active: pricing lookup is async, so the whole
                # enrich → emit → write → debit chain runs as one task
                # (inline via asyncio.run when there is no loop).
                self._schedule(self._bill_then_persist(
                    row, engine=engine, endpoint=endpoint, status=status,
                    model_spec=model_spec, video_duration_s=video_duration_s,
                    principal=principal,
                ))
                return
            # Billing off: byte-identical P1 behavior (synchronous emission).
            self._emit_event(row)
            if self._table_client is None:
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                task = loop.create_task(self._write_row_async(row))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
            else:
                self._write_row_sync(row)
        except Exception:  # noqa: BLE001
            self._logger.warning("usage: failed to record job %s", job_id, exc_info=True)

    def _schedule(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return
        task = loop.create_task(coro)
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def _bill_then_persist(
        self,
        row: dict[str, Any],
        *,
        engine: Any,
        endpoint: str,
        status: str,
        model_spec: Optional[str],
        video_duration_s: Optional[float],
        principal: Optional[Principal],
    ) -> None:
        comp = None
        try:
            comp = await engine.compute(
                endpoint=endpoint, status=status, model_spec=model_spec,
                video_duration_s=video_duration_s,
                account_id=principal.user_id if principal is not None else None,
            )
            engine.enrich_row(row, comp)
        except Exception:  # noqa: BLE001 - billing must never block persistence
            self._logger.warning(
                "usage: billing enrichment failed for job %s",
                row.get("job_id"), exc_info=True,
            )
        self._emit_event(row)
        if self._table_client is not None:
            await self._write_row_async(row)
        if (
            comp is not None
            and getattr(engine, "debits", False)
            and principal is not None
        ):
            await engine.debit_for_job(
                account_id=principal.user_id,
                job_id=str(row.get("job_id", "")),
                comp=comp,
            )

    def _build_row(
        self, *, job_id: str, endpoint: str, status: str,
        principal: Optional[Principal], model_spec: Optional[str],
        token_usage: Optional[dict], cost_metadata: Any,
        auth_mode: Optional[str], latency_ms: Optional[int],
        provider: Optional[str] = None,
        video_duration_s: Optional[float] = None,
    ) -> dict[str, Any]:
        prompt = int(_get(token_usage, "prompt_tokens", 0) or 0)
        completion = int(_get(token_usage, "completion_tokens", 0) or 0)
        total = int(_get(token_usage, "total_tokens", 0) or 0)
        if total == 0:
            total = prompt + completion
        if total == 0:
            total = int(_get(cost_metadata, "token_num", 0) or 0)
        token_cost = _get(cost_metadata, "token_cost", None)
        call_count = int(_get(cost_metadata, "creation_times", 0) or 0)
        # Raw vendor name: in-process only, for the price-table lookup below.
        # Everything stored/emitted uses the branded label.
        provider = (provider or "").strip().lower() or provider_for_modelspec(model_spec)
        unit_cost = music_unit_cost_usd(provider)
        generation_cost = round(call_count * unit_cost, 6)
        # Bill the authoritative total the response builder computed (duration-based
        # for multi-image, generation+token for video). Fall back to the breakdown
        # sum for legacy rows written before cost_metadata carried total_cost.
        explicit_total = _get(cost_metadata, "total_cost", None)
        if explicit_total is not None:
            total_cost = round(float(explicit_total), 6)
        else:
            total_cost = round(generation_cost + float(token_cost or 0.0), 6)
        now_ns = time.time_ns()
        user_id = principal.user_id if principal else None
        row: dict[str, Any] = {
            "PartitionKey": user_id or _ANONYMOUS_PARTITION,
            "RowKey": f"{10**19 - now_ns:020d}-{job_id}",
            "job_id": job_id,
            "endpoint": endpoint,
            "status": status,
            "auth_mode": auth_mode or self._auth_mode,
            "user_id": user_id or "",
            "key_prefix": principal.key_prefix if principal else "",
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "token_cost_usd": float(token_cost) if token_cost is not None else 0.0,
            "music_provider": provider_public_label(provider),
            "model_spec": model_spec or "",
            "generation_call_count": call_count,
            "generation_unit_cost_usd": unit_cost,
            "generation_cost_usd": generation_cost,
            "total_cost_usd": total_cost,
            "latency_ms": int(latency_ms) if latency_ms is not None else -1,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }
        if video_duration_s is not None:
            row["video_duration_s"] = float(video_duration_s)
        return row

    def _emit_event(self, row: dict[str, Any]) -> None:
        try:
            payload = {
                "gen_ai.usage.input_tokens": row["prompt_tokens"],
                "gen_ai.usage.output_tokens": row["completion_tokens"],
                "edenn.job_id": row["job_id"],
                "edenn.endpoint": row["endpoint"],
                "edenn.status": row["status"],
                "edenn.auth_mode": row["auth_mode"],
                "edenn.user_id": row["user_id"],
                "edenn.key_prefix": row["key_prefix"],
                "edenn.total_tokens": row["total_tokens"],
                "edenn.token_cost_usd": row["token_cost_usd"],
                "edenn.music_provider": row["music_provider"],
                "edenn.model_spec": row["model_spec"],
                "edenn.generation_call_count": row["generation_call_count"],
                "edenn.generation_cost_usd": row["generation_cost_usd"],
                "edenn.total_cost_usd": row["total_cost_usd"],
            }
            if "video_duration_s" in row:
                payload["edenn.video_duration_s"] = row["video_duration_s"]
            if "billed_amount_micros" in row:
                payload["edenn.billing_mode"] = row["billing_mode"]
                payload["edenn.billed_units"] = row["billed_units"]
                payload["edenn.unit_price_usd"] = row["unit_price_usd"]
                payload["edenn.billed_amount_usd"] = row["billed_amount_usd"]
                if "unit_seconds" in row:
                    # Without it, "3 units for a 90-second video" is not a
                    # checkable statement in the telemetry.
                    payload["edenn.unit_seconds"] = row["unit_seconds"]
                    payload["edenn.min_billable_seconds"] = \
                        row["min_billable_seconds"]
            self._emit(payload)
        except Exception:  # noqa: BLE001 - emission must never break recording
            self._logger.warning(
                "usage: event emission failed for job %s",
                row.get("job_id"),
                exc_info=True,
            )

    async def _write_row_async(self, row: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(self._table_client.upsert_entity, row)
        except Exception:  # noqa: BLE001
            self._logger.warning(
                "usage: ledger write failed for job %s", row.get("job_id"), exc_info=True
            )
        if self._mirror is not None:
            # Outside the try above on purpose: the two stores are independent,
            # so a Table Storage failure must not also skip the mirror.
            await self._mirror(row)

    def _write_row_sync(self, row: dict[str, Any]) -> None:
        try:
            self._table_client.upsert_entity(row)
        except Exception:  # noqa: BLE001
            self._logger.warning(
                "usage: ledger write failed for job %s", row.get("job_id"), exc_info=True
            )
        if self._mirror is not None:
            # This path runs only with no event loop (billing off, sync
            # caller), so there is nowhere to await the mirror. The final
            # backfill pass picks these rows up — source_row_key makes that
            # safe to re-run.
            self._logger.debug(
                "usage: mirror skipped for job %s (no event loop); the backfill "
                "will collect it", row.get("job_id"))

    def attach_billing_stores(self, stores: Any) -> None:
        """Point writes at the Postgres mirror and, after cutover, reads too.

        Called once at boot by api.py. Split from ``__init__`` because the
        billing stores are resolved after the recorder — and because reads and
        writes move independently: dual-write runs for a while before anything
        starts reading from Postgres.
        """
        if stores is None:
            return
        self._mirror = getattr(stores, "usage_mirror", None)
        self._reader = getattr(stores, "usage_reader", None)

    # -- reads (admin endpoint) -------------------------------------------

    async def query_usage(
        self, *, user_id: str, limit: Optional[int] = 200
    ) -> list[dict]:
        if self._reader is not None:
            return await self._reader.query_usage(user_id=user_id, limit=limit)
        if self._table_client is None:
            return []
        def _query() -> list[dict]:
            rows = self._table_client.query_entities(
                f"PartitionKey eq '{user_id}'"
            )
            return [dict(r) for r in rows]
        rows = await asyncio.to_thread(_query)
        rows.sort(key=lambda r: str(r.get("RowKey", "")))  # inverted ts: asc = newest first
        return rows if limit is None else rows[:limit]


def _billing_engine() -> Optional[Any]:
    """Function-level import: billing depends on auth, never the reverse."""
    try:
        from EdennCode.Deployment.billing import get_billing

        return get_billing()
    except Exception:  # noqa: BLE001
        return None


async def query_usage_window(
    recorder: "UsageRecorder",
    *,
    user_id: str,
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    key_prefix: Optional[str] = None,
) -> tuple[list[dict], dict, dict]:
    """Paginated ledger rows in a time window + whole-set totals + page info.

    Returns ``(page_rows, totals, page)``:
    - ``page_rows`` is one page: the filtered set (newest first) sliced by
      ``offset``/``limit``.
    - ``totals`` always covers the ENTIRE filtered set — jobs, token/cost/USD
      sums, and three breakdowns (``by_key``, ``by_model``, and the
      ``by_key_model`` cross-cut keyed ``"<key_prefix>|<model_spec>"``) — so a
      client paging through results sees the same grand totals on every page.
    - ``page`` is the pagination envelope: ``{limit, offset, returned,
      total_rows, has_more}``.

    ``from_ts``/``to_ts`` are string bounds compared against ISO-8601
    ``timestamp_utc`` (from inclusive, to exclusive). Because ISO-8601 sorts
    lexically, a date-only pair like ``from=2026-07-22&to=2026-07-23`` selects
    exactly that single day. ``key_prefix`` narrows to one API key of the
    account (a user may hold several against one shared wallet).
    """
    # Function-level import: billing imports auth, never the reverse.
    from EdennCode.Deployment.billing.stores import micros_to_usd

    rows = await recorder.query_usage(user_id=user_id, limit=None)
    if from_ts:
        rows = [r for r in rows if str(r.get("timestamp_utc", "")) >= from_ts]
    if to_ts:
        rows = [r for r in rows if str(r.get("timestamp_utc", "")) < to_ts]
    if key_prefix:
        rows = [r for r in rows if str(r.get("key_prefix", "")) == key_prefix]

    total_rows = len(rows)
    # Three breakdowns off one pass. by_key answers "which of my keys spent
    # this?", by_model "which model did I spend it on?", and by_key_model the
    # cross-cut a customer actually reconciles against — this key, on that
    # model. Billed amounts accumulate as integer micros and convert once at
    # the end; summing the rounded USD values would drift.
    by_key: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_key_model: dict[str, dict] = {}

    def _bucket(store: dict[str, dict], label: str) -> dict:
        return store.setdefault(label, {
            "jobs": 0, "total_tokens": 0,
            "total_cost_usd": 0.0, "total_billed_usd": 0,
        })

    for r in rows:
        key_label = str(r.get("key_prefix", "")) or "unattributed"
        # Paths that pick a provider directly (e.g. provider_d) leave model_spec
        # empty, so those rows land in an explicit bucket rather than "".
        model_label = str(r.get("model_spec", "")) or "unspecified"
        tokens = int(r.get("total_tokens", 0) or 0)
        cost = float(r.get("total_cost_usd", 0.0) or 0.0)
        billed_micros = int(r.get("billed_amount_micros", "0") or 0)
        for bucket in (
            _bucket(by_key, key_label),
            _bucket(by_model, model_label),
            _bucket(by_key_model, f"{key_label}|{model_label}"),
        ):
            bucket["jobs"] += 1
            bucket["total_tokens"] += tokens
            bucket["total_cost_usd"] += cost
            bucket["total_billed_usd"] += billed_micros
    for store in (by_key, by_model, by_key_model):
        for bucket in store.values():
            bucket["total_cost_usd"] = round(bucket["total_cost_usd"], 6)
            bucket["total_billed_usd"] = micros_to_usd(bucket["total_billed_usd"])
    totals = {
        "jobs": total_rows,
        "total_tokens": sum(int(r.get("total_tokens", 0) or 0) for r in rows),
        "total_cost_usd": round(
            sum(float(r.get("total_cost_usd", 0.0) or 0.0) for r in rows), 6
        ),
        "total_billed_usd": micros_to_usd(
            sum(int(r.get("billed_amount_micros", "0") or 0) for r in rows)
        ),
        "by_key": by_key,
        "by_model": by_model,
        "by_key_model": by_key_model,
    }
    offset = max(0, offset)
    page_rows = rows[offset:offset + limit]
    page = {
        "limit": limit,
        "offset": offset,
        "returned": len(page_rows),
        "total_rows": total_rows,
        "has_more": offset + len(page_rows) < total_rows,
    }
    return page_rows, totals, page


# -- module singleton (provider_music_callbacks pattern) -------------------

def set_usage_recorder_override(recorder: Optional[UsageRecorder]) -> None:
    global _singleton, _singleton_resolved
    _singleton = recorder
    _singleton_resolved = recorder is not None


def get_usage_recorder() -> Optional[UsageRecorder]:
    return _singleton


def resolve_usage_recorder(
    settings: Any = None, logger: Optional[logging.Logger] = None
) -> Optional[UsageRecorder]:
    """Build the process-wide recorder once (api.py and worker_main both call this)."""
    global _singleton, _singleton_resolved
    if _singleton_resolved:
        return _singleton
    log = logger or logging.getLogger(__name__)
    if settings is None:
        from EdennCode.Deployment.settings import DeploymentSettings

        try:
            settings = DeploymentSettings.from_env()
        except Exception as exc:  # noqa: BLE001
            log.warning("usage: recorder disabled (settings unavailable: %s)", exc)
            _singleton_resolved = True
            return None
    _singleton = UsageRecorder.from_settings(settings, log)
    _singleton_resolved = True
    return _singleton


# -- v2 worker helper ------------------------------------------------------

def record_v2_job_usage(
    job: Any,
    *,
    status: str,
    endpoint: str,
    result: Any = None,
    result_json: Optional[dict] = None,
) -> None:
    """Terminal-transition recording for async v2 workers. Never raises."""
    try:
        recorder = get_usage_recorder()
        if recorder is None:
            return
        request_json = getattr(job, "request_json", None) or {}
        user_id = request_json.get("auth_user_id")
        key_prefix = request_json.get("auth_key_prefix", "")
        principal = (
            Principal(user_id=str(user_id), key_prefix=str(key_prefix))
            if user_id
            else None
        )
        model_spec = None
        cost_metadata = None
        provider = None
        if result_json:
            model_spec = result_json.get("modelspec")
            cost_metadata = result_json.get("cost_metadata")
            # Explicit provider identity when the result carries one (nested
            # response_metadata or top-level); falls back to modelspec mapping.
            response_metadata = result_json.get("response_metadata") or {}
            provider = (
                result_json.get("music_provider")
                or response_metadata.get("music_provider")
                or None
            )
        if model_spec is None and result is not None:
            model_spec = getattr(result, "used_music_model_spec", None)
        token_usage = getattr(result, "token_usage", None) if result is not None else None
        video_duration_s: Optional[float] = None
        if result_json:
            video_metadata = result_json.get("video_metadata")
            geometry = (
                video_metadata.get("geometry")
                if isinstance(video_metadata, dict)
                else None
            )
            if isinstance(geometry, dict):
                raw_duration = geometry.get("duration")
                if raw_duration is None:
                    raw_duration = geometry.get("duration_s")
                try:
                    if raw_duration is not None:
                        video_duration_s = float(raw_duration)
                except (TypeError, ValueError):
                    video_duration_s = None
        recorder.record_job(
            job_id=str(getattr(job, "job_id", "") or request_json.get("job_id", "")),
            endpoint=endpoint,
            status=status,
            principal=principal,
            model_spec=model_spec,
            token_usage=token_usage if isinstance(token_usage, dict) else None,
            cost_metadata=cost_metadata,
            provider=provider,
            video_duration_s=video_duration_s,
        )
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "usage: v2 recording failed", exc_info=True
        )


__all__ = [
    "DEFAULT_MUSIC_UNIT_COST_USD",
    "PROVIDER_PUBLIC_LABEL",
    "PROVIDER_UNIT_COST_USD",
    "UsageRecorder",
    "provider_public_label",
    "get_usage_recorder",
    "music_unit_cost_usd",
    "query_usage_window",
    "record_v2_job_usage",
    "resolve_usage_recorder",
    "set_usage_recorder_override",
]
