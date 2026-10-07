"""A record of who did the things that cost money or change access.

When a bill is disputed, a session is wiped, or a collaborator sees something
they should not have, the question is always the same and the studio could not
answer it: WHO did this, and when. Sessions record what happened to them, not
who caused it, and the reasoning loop's tool calls are attributed to the agent
rather than to the person who asked.

This is a log, not a table. It goes to the process's structured logging under a
dedicated logger name, so it lands wherever the deployment already ships logs
and needs no schema, no migration, and no retention decision before it is useful.
That is a deliberate first step, not the destination: a queryable audit table is
worth building once someone needs to search it, and the fields here are chosen so
that table can be filled from these lines later.

What is recorded is the ACTOR, the ACTION, the SUBJECT, and the outcome. What is
never recorded is the content — no scripts, no prompts, no filenames. An audit
line is read by people investigating an incident, and it must not become a second
copy of the user's work sitting in a log aggregator with different access rules
from the database.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

# Its own logger so a deployment can route or retain audit lines differently
# from application noise without parsing message text.
logger = logging.getLogger("edenn.agentic_audio.audit")

# The actions worth a line. Everything here either spends money, changes who can
# see something, or destroys data — the three things someone comes asking about.
SPEND = "spend"
ACCESS = "access"
DESTRUCTIVE = "destructive"


# Free text is never safe to copy here, however short. A filter by LENGTH lets a
# forty-character script and a filename straight through — which is exactly what
# the first version of this did, and what its own test caught.
#
# So: numbers and booleans always pass (a count cannot be a script), and strings
# pass only under a key we have decided is not content.
_SAFE_STRING_KEYS = frozenset(
    {
        "role",
        "epoch",
        "reason",
        "status",
        "voice_id",
        "tool",
        "kind",
        "provider_ref",
        "variant_id",
        "candidate_id",
        "job_id",
    }
)


def _safe_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Drop anything that could be the user's own words or their filenames.

    An audit trail must not become a second copy of the user's work sitting in a
    log aggregator with different access rules from the database. Dropping is
    deliberate rather than truncating: a truncated prompt still carries the part
    that identifies it.
    """

    out: dict[str, Any] = {}
    for key, value in detail.items():
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, str) and key in _SAFE_STRING_KEYS and len(value) <= 120:
            out[key] = value
    return out


def record(
    action: str,
    *,
    actor: Optional[str],
    kind: str = SPEND,
    session_id: Optional[str] = None,
    subject: Optional[str] = None,
    outcome: str = "ok",
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Write one audit line. Never raises.

    An audit call that can fail a request would be an audit system people
    disable. If this cannot write, the action still happens — the gap is
    recoverable from the application log; a failed user action is not.
    """

    try:
        payload = {
            "ts": time.time(),
            "kind": kind,
            "action": action,
            # "unknown" rather than null: a blank actor field reads like a bug
            # in the logger rather than a fact about the request. And "unknown"
            # rather than "unauthenticated", which was an active misstatement —
            # the callers it described were signed in; it was this code that had
            # never been given their identity.
            "actor": actor or "unknown",
            "session_id": session_id,
            "subject": subject,
            "outcome": outcome,
        }
        if detail:
            payload["detail"] = _safe_detail(detail)
        logger.info("audit %s", json.dumps(payload, sort_keys=True, default=str))
    except Exception:  # noqa: BLE001 - never fail the action being audited
        pass


__all__ = ["ACCESS", "DESTRUCTIVE", "SPEND", "record"]
