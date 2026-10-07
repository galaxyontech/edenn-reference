"""Rate limits and a spend ceiling for the agentic-audio surface.

Every turn on this surface costs money — a director turn is a model call, and a
turn that generates is a music, narration or sound-effect render. There was no
limit of any kind anywhere in the repo, so a single authenticated caller could
drain the provider keys in a loop, and an unauthenticated one could do it on the
standalone.

This is deliberately the small version: a kill switch, not a billing system.

    - a sliding-window **request rate** limit, so no principal can hammer turns;
    - a per-principal **daily generation ceiling**, so a runaway client (or a
      bored user) cannot spend without bound while proper quotas are built.

Two honest limitations, both consequences of keeping it dependency-free:

    * **In-process.** The counters live in this replica's memory, so with N
      replicas the effective limit is N times the configured one. That is
      correct for the deployment this ships to (max-replicas 1, load-bearing)
      and wrong the moment it scales — which is why the storage sits behind
      :class:`_Store` and can be replaced with Redis or Postgres without the
      call sites changing.
    * **Not durable.** A restart forgets today's spend. A ceiling that resets on
      deploy is still far better than no ceiling, and the durable version
      belongs with real metering (it needs the ledger, not a counter).
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Optional


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# Turns per principal per window. A person directing a session sends a handful
# of messages a minute; anything far above that is a client in a loop.
TURN_LIMIT = _int_env("AGENTIC_AUDIO_TURNS_PER_MINUTE", 20)
TURN_WINDOW_S = _int_env("AGENTIC_AUDIO_TURN_WINDOW_S", 60)

# Generations per principal per day. Sized as "a heavy day of real work", so it
# only ever catches abuse or a bug — not a user.
DAILY_GENERATION_LIMIT = _int_env("AGENTIC_AUDIO_DAILY_GENERATIONS", 100)


class LimitExceeded(Exception):
    """A limit was hit. Carries the status and the message the client should see."""

    def __init__(self, status_code: int, detail: str, *, retry_after_s: Optional[int] = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after_s = retry_after_s


@dataclass
class _Store:
    """Where the counters live. Swap this to share limits across replicas."""

    hits: dict[str, Deque[float]]
    spend: dict[str, tuple[str, int]]


class Limiter:
    """Sliding-window rate limit plus a daily spend ceiling."""

    def __init__(
        self,
        *,
        turn_limit: int = TURN_LIMIT,
        window_s: int = TURN_WINDOW_S,
        daily_generation_limit: int = DAILY_GENERATION_LIMIT,
        clock=time.monotonic,
        day_key=None,
    ) -> None:
        self.turn_limit = turn_limit
        self.window_s = window_s
        self.daily_generation_limit = daily_generation_limit
        self._clock = clock
        # Injectable so a test can cross a day boundary without sleeping.
        self._day_key = day_key or (lambda: time.strftime("%Y-%m-%d", time.gmtime()))
        self._lock = threading.Lock()
        self._store = _Store(hits=defaultdict(deque), spend={})

    # ---- request rate ----------------------------------------------------
    @staticmethod
    def key_for(principal: Optional[str], client_ip: Optional[str] = None) -> str:
        """The bucket a caller counts against.

        The principal when there is one. Otherwise the client address — NOT a
        single shared "anonymous" bucket, which would let one abuser lock out
        every other unauthenticated caller at once.
        """

        if principal:
            return f"user:{principal}"
        if client_ip:
            return f"ip:{client_ip}"
        return "anonymous"

    def check_turn(self, principal: Optional[str], client_ip: Optional[str] = None) -> None:
        """Raise :class:`LimitExceeded` when this caller is sending too fast."""

        key = self.key_for(principal, client_ip)
        now = self._clock()
        with self._lock:
            window = self._store.hits[key]
            cutoff = now - self.window_s
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= self.turn_limit:
                retry = max(1, int(self.window_s - (now - window[0])))
                raise LimitExceeded(
                    429,
                    (
                        "That's a lot of directions at once — give the last one a "
                        f"moment to land, then try again in {retry}s."
                    ),
                    retry_after_s=retry,
                )
            window.append(now)

    # ---- spend ceiling ---------------------------------------------------
    def check_generation(self, principal: Optional[str], client_ip: Optional[str] = None) -> None:
        """Raise when this caller has already generated enough for one day.

        Checked BEFORE the spend, never after: the whole point is that the
        provider call does not happen.
        """

        key = self.key_for(principal, client_ip)
        today = self._day_key()
        with self._lock:
            day, count = self._store.spend.get(key, (today, 0))
            if day != today:
                count = 0
            if count >= self.daily_generation_limit:
                raise LimitExceeded(
                    429,
                    (
                        "You've reached today's generation limit for this account. "
                        "It resets tomorrow — everything already made stays available."
                    ),
                )

    def record_generation(
        self, principal: Optional[str], n: int = 1, client_ip: Optional[str] = None
    ) -> int:
        """Count a spend that is about to happen. Returns the running total."""

        key = self.key_for(principal, client_ip)
        today = self._day_key()
        with self._lock:
            day, count = self._store.spend.get(key, (today, 0))
            if day != today:
                count = 0
            count += n
            self._store.spend[key] = (today, count)
            return count

    def spent_today(self, principal: Optional[str], client_ip: Optional[str] = None) -> int:
        day, count = self._store.spend.get(self.key_for(principal, client_ip), ("", 0))
        return count if day == self._day_key() else 0

    def reset(self) -> None:
        with self._lock:
            self._store = _Store(hits=defaultdict(deque), spend={})


# One limiter per process. Exposed as a module global so the router and the
# WebSocket loop share the same counters rather than each keeping their own.
_limiter: Optional[Limiter] = None


def limiter() -> Limiter:
    global _limiter
    if _limiter is None:
        _limiter = Limiter()
    return _limiter


def set_limiter(value: Optional[Limiter]) -> None:
    """Replace the process limiter (tests, or a shared-store implementation)."""

    global _limiter
    _limiter = value


__all__ = [
    "DAILY_GENERATION_LIMIT",
    "LimitExceeded",
    "Limiter",
    "TURN_LIMIT",
    "TURN_WINDOW_S",
    "limiter",
    "set_limiter",
]
