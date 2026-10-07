"""Not spending twice for one request.

Every generating tool costs real provider credit, and the paths that reach one
all retry: a client resends a POST it never saw answered, a WebSocket drops
mid-turn and the console reconnects, the reasoning loop re-proposes the same
call after a transient failure. Each of those produced a second render, a second
charge, and a second take the user never asked for.

The guard is a short-memory record of "this exact generation already ran for
this session", keyed by what the request actually is rather than by a client-
supplied id — a retry that forgets to resend an idempotency key is exactly the
retry that needs catching.

Scope and honesty about it:

* The record lives in the session's own state, so it survives a process restart
  and is shared by every replica reading that session — unlike an in-process
  cache, which stops working the moment there are two of us.
* The window is minutes, not forever. Asking for "another take just like that
  one" is a real thing people do; refusing it an hour later would be wrong. The
  window only has to cover the retry storm.
* It is keyed on the tool, its arguments AND what the session has already
  produced. The arguments alone are not enough: "give me another take" sends the
  same arguments as the take before it, because which take it is is implicit. A
  retry arrives before the first call's output exists; a person asking again does
  so after seeing it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The state key. Lives inside state_json so it is durable and shared, and is
# swept rather than accumulated.
STATE_KEY = "recent_generations"

# Long enough to cover a reconnect storm and a user double-clicking, short
# enough that a deliberate "do that again" is never refused.
DEFAULT_WINDOW_S = 180

# Bounded so a long session cannot grow the state document without limit.
MAX_RECORDS = 24


def witness(state: dict[str, Any]) -> str:
    """A cheap summary of what this session has already produced.

    This is what separates a RETRY from a deliberate second ask, and the
    distinction cannot come from the arguments: "give me another take" sends the
    same arguments as the take before it, because which take it is is implicit.

    What differs is the session. A retry arrives while the first call is still in
    flight, before its output exists, so the witness is unchanged. A person
    asking again does so after seeing the first result, so it has moved.
    """

    layers = state.get("layers") or {}
    sfx = layers.get("sfx")
    voiceover = layers.get("voiceover") or {}
    return "|".join(
        str(part)
        for part in (
            len(state.get("candidates") or []),
            len((sfx or {}).get("variants") or []) if isinstance(sfx, dict) else 0,
            voiceover.get("linked_job_id") or voiceover.get("status") or "",
            bool(state.get("final_artifact")),
        )
    )


def fingerprint(
    tool_name: str, tool_args: dict[str, Any], state_witness: str = ""
) -> str:
    """What makes this generation the same as that one.

    Derived from the request and the session's current output, not from a
    client-supplied key: the retry that matters most is the one that does not
    know it is a retry.
    """

    try:
        body = json.dumps(tool_args or {}, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001 - an unserialisable arg still has a repr
        body = repr(tool_args)
    return hashlib.sha256(
        f"{tool_name}|{body}|{state_witness}".encode()
    ).hexdigest()[:32]


def _sweep(records: list[dict[str, Any]], now: float, window_s: int) -> list[dict[str, Any]]:
    fresh = [r for r in records if float(r.get("at") or 0) + window_s > now]
    return fresh[-MAX_RECORDS:]


def recent_match(
    state: dict[str, Any],
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    window_s: int = DEFAULT_WINDOW_S,
    now: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """The record of an identical generation inside the window, if there is one."""

    moment = time.time() if now is None else now
    print_ = fingerprint(tool_name, tool_args, witness(state))
    for record in reversed(list(state.get(STATE_KEY) or [])):
        if record.get("fp") != print_:
            continue
        if float(record.get("at") or 0) + window_s > moment:
            return dict(record)
        break
    return None


def remember(
    state: dict[str, Any],
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    window_s: int = DEFAULT_WINDOW_S,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Record that this generation ran. Returns the updated state."""

    moment = time.time() if now is None else now
    records = list(state.get(STATE_KEY) or [])
    records.append(
        {
            "fp": fingerprint(tool_name, tool_args, witness(state)),
            "tool": tool_name,
            "at": moment,
        }
    )
    updated = dict(state)
    updated[STATE_KEY] = _sweep(records, moment, window_s)
    return updated


def forget(
    state: dict[str, Any],
    *,
    tool_name: str,
    tool_args: dict[str, Any],
) -> dict[str, Any]:
    """Remove the most recent record for this exact call. Returns updated state.

    The dispatcher records a spend BEFORE the tool runs (the window must cover
    a slow tool while the client retries). But a call the tool REFUSED — an
    approval gate — never spent, and leaving its fingerprint in place makes the
    user's immediately-following legitimate click read as a duplicate: agent
    blocked, user presses Generate, user refused too. Refusals are forgotten.
    """

    fp = fingerprint(tool_name, tool_args, witness(state))
    records = list(state.get(STATE_KEY) or [])
    for i in range(len(records) - 1, -1, -1):
        if records[i].get("fp") == fp:
            del records[i]
            break
    updated = dict(state)
    updated[STATE_KEY] = records
    return updated


def window_seconds() -> int:
    import os

    try:
        return max(0, int(os.getenv("AGENTIC_AUDIO_SPEND_DEDUPE_WINDOW_S", str(DEFAULT_WINDOW_S))))
    except ValueError:
        return DEFAULT_WINDOW_S


__all__ = [
    "DEFAULT_WINDOW_S",
    "MAX_RECORDS",
    "STATE_KEY",
    "fingerprint",
    "recent_match",
    "witness",
    "remember",
    "window_seconds",
]
