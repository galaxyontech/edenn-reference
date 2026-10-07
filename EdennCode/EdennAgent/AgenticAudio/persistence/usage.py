"""What a render actually consumed. Counts only; no money.

The studio spends real provider credit on every take, every line of narration
and every sound effect, and records none of it. A failed paid generation, an
interrupted one and one that was never started are indistinguishable after the
fact, and "what did this customer use" has no answer at all.

This module is the meter. It is deliberately NOT a billing system:

* **No money anywhere.** Not a price, not a rate, not a currency. Every cost
  number in this repo today is a placeholder somebody wrote down to be replaced
  by an invoice, and a meter with an empty money column is an invitation to
  fill it with one. Pricing is a separate pass over closed rows, later.
* **Never a description of the footage.** The row outlives the session by
  design — the bill has to survive a customer deleting their work — so it may
  carry counts, ids and enum members, and never a prompt, a script, a title, a
  filename, a URL or an error string.
* **Never an upstream name.** The tier that rendered is one of the studio's own
  three; which company is behind it is not this table's business and is not
  reachable from this module.

The lifecycle is two writes, and the seam is the claim. A job is claimed by one
atomic UPDATE whose own docstring carries the invariant this depends on —
everything that spends happens after it returns a row — so the claim and the
open happen in ONE transaction: if the meter cannot be written, the job is not
claimed and nothing spends. Closing is idempotent and first-close-wins, because
every double-terminal path in the system (a sweep racing a completer, two
workers holding one lease) must leave exactly one row saying what happened.

The four outcomes are the whole point. "Spent and delivered" and "spent and
delivered nothing" are different bills; "claimed, then silence" is neither, and
saying so is what lets a human reconcile it later instead of guessing.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Iterator, Optional, Sequence

from EdennCode.Deployment.postgres_wrapper import PostgresClient

from .pool import pooled_client

logger = logging.getLogger(__name__)

#: One invoice line's category. The studio's own vocabulary; a vendor's name
#: never appears here, and neither does a job type, because two job types can
#: be one kind of spend and one job type can be two.
SITE_REASONING_TURN = "reasoning_turn"
SITE_UNDERSTANDING = "understanding"
SITE_MUSIC = "music"
SITE_MUSIC_RESTYLE = "music_restyle"
SITE_NARRATION = "narration"
SITE_SFX_EVENTS = "sfx_events"
SITE_SFX_BED = "sfx_bed"

SITES: frozenset[str] = frozenset({
    SITE_REASONING_TURN, SITE_UNDERSTANDING, SITE_MUSIC, SITE_MUSIC_RESTYLE,
    SITE_NARRATION, SITE_SFX_EVENTS, SITE_SFX_BED,
})

#: Sites written once, at the end, by the same frame that made the calls. They
#: have no claim to hang an open on.
IN_PROCESS_SITES: frozenset[str] = frozenset({
    SITE_REASONING_TURN, SITE_UNDERSTANDING,
})

#: Spent, and the customer got it.
OUTCOME_DELIVERED = "delivered"
#: Spent, and the customer got nothing. The row a refund conversation starts from.
OUTCOME_FAILED_AFTER_SPEND = "failed_after_spend"
#: Claimed, then silence. Not a judgement — an instruction to reconcile.
OUTCOME_SPEND_UNKNOWN = "spend_unknown"
#: Reached the end without calling a provider at all.
OUTCOME_NO_SPEND = "no_spend"

OUTCOMES: frozenset[str] = frozenset({
    OUTCOME_DELIVERED, OUTCOME_FAILED_AFTER_SPEND, OUTCOME_SPEND_UNKNOWN,
    OUTCOME_NO_SPEND,
})

#: A frozen catalogue, because a note on a row that outlives a deletion must not
#: be free text: exception strings carry vendor identity and sometimes the
#: user's own prompt.
NOTE_CODES: frozenset[str] = frozenset({
    "call_count_excludes_lyric_calls",
    "analysis_failed_after_spend",
    "unexpected_error_after_claim",
    "terminal_write_refused",
    "startup_probe",
    "demo_bundle_replay",
    "placeholder_render",
    "swept_stale",
    "interrupted",
})

#: Must stay character-identical to the partial index predicate in
#: 007_usage_meter.sql. A test pins the two together: drifting apart costs
#: nothing at write time and silently loses the index at read time.
OPEN_STATE_PREDICATE = "state = 'open'"

_QUANTITY_FIELDS = (
    "delivered_ms", "produced_ms", "requested_ms", "source_ms",
    "provider_calls", "items", "items_reused", "text_chars",
    "lm_input_tokens", "lm_output_tokens", "lm_attempts",
)


@dataclass(frozen=True)
class Measurement:
    """What one spend unit consumed, as the render measured it.

    Every quantity defaults to ``None``, and ``None`` means UNMEASURED — never
    zero. A render that died after paying must not look free, and a zero here
    is a claim that nothing was bought. Zero is written only when a render
    reported making none.

    Milliseconds, never seconds-as-float: a float duration multiplied by a rate
    and ceilinged buys a spare block at the boundary, and an integer cannot.
    """

    delivered_ms: Optional[int] = None
    produced_ms: Optional[int] = None
    requested_ms: Optional[int] = None
    source_ms: Optional[int] = None
    provider_calls: Optional[int] = None
    items: Optional[int] = None
    items_reused: Optional[int] = None
    text_chars: Optional[int] = None
    lm_input_tokens: Optional[int] = None
    lm_output_tokens: Optional[int] = None
    lm_attempts: Optional[int] = None
    #: The tier that ACTUALLY rendered, which is not always the one requested:
    #: the server substitutes when the asked-for tier has no key on the box, and
    #: billing the requested one would be a lie.
    tier: Optional[str] = None
    tier_requested: Optional[str] = None
    #: Which synthesis route ran, where more than one exists and they are
    #: charged differently. Recorded after any runtime fallback, never as asked.
    route: Optional[str] = None
    #: Which quantity a pricing pass should read. A label, not a constraint:
    #: every dimension that was free to measure is stored anyway, because a
    #: count can be re-priced and a dimension never written cannot be
    #: re-measured once the media is deleted.
    primary_unit: Optional[str] = None
    note_code: Optional[str] = None
    #: Provenance that explains a number and is never multiplied by a price.
    #: The no-content rule applies to every key AND every value.
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.note_code is not None and self.note_code not in NOTE_CODES:
            raise ValueError(
                f"note_code {self.note_code!r} is not in the catalogue. Add it "
                "there deliberately — free text on a row that outlives a "
                "deletion is how a prompt ends up in a billing table."
            )

    def as_columns(self) -> dict[str, Any]:
        out = {name: getattr(self, name) for name in _QUANTITY_FIELDS}
        out.update({
            "tier": self.tier,
            "tier_requested": self.tier_requested,
            "route": self.route,
            "primary_unit": self.primary_unit,
            "note_code": self.note_code,
            "detail_json": dict(self.detail or {}),
        })
        return out


MEASUREMENT_FIELDS: frozenset[str] = frozenset(f.name for f in fields(Measurement))

#: Nothing bought. The one case where zeros are the truth.
NO_SPEND = Measurement(provider_calls=0, items=0, text_chars=0)


def meter_required() -> bool:
    """Whether an unreachable meter must stop the spend.

    Default ON. The alternative default is a deployment that quietly stops
    recording and only finds out at the end of the month.
    """

    raw = os.getenv("AGENTIC_AUDIO_METER_REQUIRED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def turn_key(session_id: str) -> str:
    """A key for one reasoning turn.

    Deliberately NOT the turn ordinal: the loop writes ``len(turns) + 1`` while
    capping the stored list at 100, so from turn 101 the ordinal is permanently
    101 and a key built from it silently stops recording.
    """

    return f"turn:{session_id}:{uuid.uuid4().hex}"


def analysis_key(session_id: str = "", tool_call_id: str = "") -> str:
    if tool_call_id:
        return f"analysis:{tool_call_id}"
    return f"analysis:{session_id}:{uuid.uuid4().hex}"


def job_key(job_id: str, *, site: str, attempt: int = 1) -> str:
    return f"job:{job_id}:a{int(attempt)}:{site}"


class UsageMeter:
    """The studio's own usage table. Reads nothing from the platform's billing."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], PostgresClient] = pooled_client,
        enabled: Optional[bool] = None,
    ) -> None:
        self._client_factory = client_factory
        self._ensure_lock = threading.Lock()
        self._schema_ready = False
        if enabled is None:
            from .. import config

            enabled = config.database_target() is not None
        self._enabled = bool(enabled)
        self._announced = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def announce(self) -> None:
        """Say once, at startup, whether anything is being recorded."""

        if self._announced:
            return
        self._announced = True
        if self._enabled:
            logger.info("usage meter: recording what renders consume")
        else:
            logger.warning(
                "usage meter: NO DATABASE — nothing about what this process "
                "spends will be recorded anywhere"
            )

    # ---- schema ---------------------------------------------------------

    def ensure_schema(self) -> None:
        if self._schema_ready or not self._enabled:
            return
        with self._ensure_lock:
            if self._schema_ready:
                return
            from .jobs import MIGRATION_PATH
            from .migrator import apply_all, discover

            apply_all(self._client_factory, discover(MIGRATION_PATH.parent))
            self._schema_ready = True

    @contextmanager
    def _client_context(
        self, client: Optional[PostgresClient] = None
    ) -> Iterator[PostgresClient]:
        if client is not None:
            yield client
            return
        with self._client_factory() as created:
            yield created

    # ---- the two writes -------------------------------------------------

    def open(
        self,
        *,
        meter_key: str,
        site: str,
        job_id: Optional[str] = None,
        attempt: int = 1,
        session_id: Optional[str] = None,
        group_ref: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        creator_user_id: Optional[str] = None,
        account_id: Optional[str] = None,
        runner_id: Optional[str] = None,
        tier_requested: Optional[str] = None,
        deployment: str = "standalone",
        client: Optional[PostgresClient] = None,
    ) -> None:
        """Say that something is about to spend.

        Called inside the claim's transaction, which is what makes the whole
        thing fail closed: no row, no claim, no spend.
        """

        if not self._enabled:
            return
        if site not in SITES:
            raise ValueError(f"not a metered site: {site!r}")
        self.ensure_schema()
        with self._client_context(client) as active:
            active.run_sql(
                """
                INSERT INTO agentic_audio_usage (
                    usage_id, meter_key, site, deployment, job_id, attempt,
                    group_ref, session_id, actor_user_id, creator_user_id,
                    account_id, runner_id, tier_requested, state
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open')
                ON CONFLICT (meter_key) DO NOTHING
                """,
                params=[
                    f"usage_{uuid.uuid4().hex}", meter_key, site, deployment,
                    job_id, int(attempt), group_ref, session_id, actor_user_id,
                    creator_user_id, account_id, runner_id, tier_requested,
                ],
            )

    def close(
        self,
        *,
        meter_key: str,
        outcome: str,
        measurement: Optional[Measurement] = None,
        client: Optional[PostgresClient] = None,
    ) -> bool:
        """Say what happened. First close wins; a second changes nothing.

        Returns whether THIS call was the one that closed it, so a caller can
        tell "I recorded the outcome" from "somebody already had".
        """

        if not self._enabled:
            return False
        if outcome not in OUTCOMES:
            raise ValueError(f"not an outcome: {outcome!r}")
        self.ensure_schema()
        columns = (measurement or Measurement()).as_columns()
        assignments = ", ".join(f"{name} = %s" for name in columns)
        with self._client_context(client) as active:
            rows = active.run_sql(
                f"""
                UPDATE agentic_audio_usage
                SET state = 'closed', outcome = %s, closed_at = now(),
                    {assignments}
                WHERE meter_key = %s AND {OPEN_STATE_PREDICATE}
                RETURNING usage_id
                """,
                params=[outcome, *columns.values(), meter_key],
            )
        return bool(isinstance(rows, list) and rows)

    def record_once(
        self,
        *,
        meter_key: str,
        site: str,
        outcome: str = OUTCOME_DELIVERED,
        measurement: Optional[Measurement] = None,
        client: Optional[PostgresClient] = None,
        **identity: Any,
    ) -> None:
        """One closed row for work that had no claim to hang an open on."""

        if not self._enabled:
            return
        self.open(meter_key=meter_key, site=site, client=client, **identity)
        self.close(
            meter_key=meter_key, outcome=outcome, measurement=measurement,
            client=client,
        )

    # ---- settling what nobody closed ------------------------------------

    def settle_for_jobs(
        self,
        *,
        job_ids: Sequence[str],
        outcome: str,
        note_code: Optional[str] = None,
        client: Optional[PostgresClient] = None,
    ) -> int:
        """Close every open row for these jobs. Used by the interrupted sweep.

        A render that was claimed and never came back spent money nobody can
        account for, and that is exactly the row a human needs to see.
        """

        ids = tuple(str(j) for j in job_ids)
        if not self._enabled or not ids:
            return 0
        if outcome not in OUTCOMES:
            raise ValueError(f"not an outcome: {outcome!r}")
        self.ensure_schema()
        with self._client_context(client) as active:
            rows = active.run_sql(
                f"""
                UPDATE agentic_audio_usage
                SET state = 'closed', outcome = %s, closed_at = now(),
                    note_code = COALESCE(note_code, %s)
                WHERE job_id IN %s AND {OPEN_STATE_PREDICATE}
                RETURNING usage_id
                """,
                params=[outcome, note_code, ids],
            )
        return len(rows) if isinstance(rows, list) else 0

    def open_rows_for_jobs(self, job_ids: Sequence[str]) -> list[str]:
        """Which of these jobs have a row still open. For the abandoned check."""

        ids = tuple(str(j) for j in job_ids)
        if not self._enabled or not ids:
            return []
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                f"SELECT job_id FROM agentic_audio_usage "
                f"WHERE job_id IN %s AND {OPEN_STATE_PREDICATE}",
                params=[ids],
            )
        return [str(r["job_id"]) for r in (rows if isinstance(rows, list) else [])]

    def sweep_stale(self, *, older_than_s: float = 6 * 3600.0) -> int:
        """Close rows nobody ever closed.

        A row open long after it was claimed is money that was spent and never
        accounted for. The bound is comfortably past the longest render so a
        slow one is never settled out from under itself.
        """

        if not self._enabled:
            return 0
        self.ensure_schema()
        with self._client_factory() as client:
            rows = client.run_sql(
                f"""
                UPDATE agentic_audio_usage
                SET state = 'closed', outcome = %s, closed_at = now(),
                    note_code = COALESCE(note_code, 'swept_stale')
                WHERE {OPEN_STATE_PREDICATE}
                  AND opened_at < now() - (%s || ' seconds')::interval
                RETURNING usage_id
                """,
                params=[OUTCOME_SPEND_UNKNOWN, str(int(older_than_s))],
            )
        return len(rows) if isinstance(rows, list) else 0


_ACTIVE: Optional[UsageMeter] = None
_ACTIVE_LOCK = threading.Lock()


def active_meter() -> UsageMeter:
    """The one meter this process uses.

    Two of the three unmetered sites are reached from module-level functions
    that never see the server's own objects — the production analysis path is
    one of them, and it is the largest single spend in the product. A meter
    they cannot reach is a meter that does not measure them.
    """

    global _ACTIVE
    if _ACTIVE is None:
        with _ACTIVE_LOCK:
            if _ACTIVE is None:
                _ACTIVE = UsageMeter()
    return _ACTIVE


def set_active_meter(meter: Optional[UsageMeter]) -> None:
    """Install the meter the server built, so there is exactly one."""

    global _ACTIVE
    _ACTIVE = meter


__all__ = [
    "active_meter",
    "set_active_meter",
    "IN_PROCESS_SITES",
    "Measurement",
    "NOTE_CODES",
    "NO_SPEND",
    "OPEN_STATE_PREDICATE",
    "OUTCOMES",
    "OUTCOME_DELIVERED",
    "OUTCOME_FAILED_AFTER_SPEND",
    "OUTCOME_NO_SPEND",
    "OUTCOME_SPEND_UNKNOWN",
    "SITES",
    "SITE_MUSIC",
    "SITE_MUSIC_RESTYLE",
    "SITE_NARRATION",
    "SITE_REASONING_TURN",
    "SITE_SFX_BED",
    "SITE_SFX_EVENTS",
    "SITE_UNDERSTANDING",
    "UsageMeter",
    "analysis_key",
    "job_key",
    "meter_required",
    "turn_key",
]
