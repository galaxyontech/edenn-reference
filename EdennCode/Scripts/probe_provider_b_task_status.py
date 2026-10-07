"""
Diagnostic: fire N concurrent song tasks and watch each task's raw status
transitions over time.

Usage:
    PROVIDER_B_API_KEY=<key> python EdennCode/Scripts/probe_provider_b_task_status.py

Optional env:
    PROVIDER_B_LYRICS   - lyrics to use (default: short placeholder)
    PROVIDER_B_PROMPT   - prompt / style hint
    PROVIDER_B_MODEL    - model name (default: provider default)
    PROBE_POLL_S    - seconds between polls (default: 4)
    PROBE_TIMEOUT_S - max total wait seconds (default: 600)
    PROBE_N_TASKS   - number of concurrent tasks (default: 6)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

# Show provider-level warnings so key rotation vs backoff decisions are visible.
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_music import (
    ProviderBMusicProvider,
    ProviderBTask,
)

POLL_S = float(os.getenv("PROBE_POLL_S", "4"))
TIMEOUT_S = float(os.getenv("PROBE_TIMEOUT_S", "600"))
N_TASKS = int(os.getenv("PROBE_N_TASKS", "6"))

LYRICS = os.getenv(
    "PROVIDER_B_LYRICS",
    "[Verse]\nSunrise over the ocean wide\nFootsteps echo on the morning tide\n\n[Chorus]\nWe are alive we are alive\n",
)
PROMPT = os.getenv("PROVIDER_B_PROMPT", "upbeat coastal pop")
MODEL = os.getenv("PROVIDER_B_MODEL") or None


def ts() -> str:
    return f"{time.time():.1f}"


async def launch_task(provider: ProviderBMusicProvider, idx: int) -> Optional[ProviderBTask]:
    try:
        task = await provider.generate_song_task(lyrics=LYRICS, prompt=PROMPT, model=MODEL)
        print(f"[{ts()}] task[{idx}] launched  task_id={task.task_id!r}  initial_status={task.status!r}")
        return task
    except Exception as exc:
        print(f"[{ts()}] task[{idx}] LAUNCH FAILED: {exc}")
        return None


async def poll_all(provider: ProviderBMusicProvider, tasks: List[ProviderBTask]) -> None:
    """
    Poll all tasks together until every one reaches a terminal state or we hit
    TIMEOUT_S.  For each task we record every distinct status seen so we can
    spot transient states (e.g. failed → succeeded).
    """
    TERMINAL = {"succeeded", "success", "completed", "done", "finished",
                "failed", "error", "timeouted", "cancelled", "canceled"}

    history: Dict[str, List[str]] = {t.task_id: [t.status] for t in tasks if t}
    done: set[str] = set()
    start = time.time()

    while len(done) < len([t for t in tasks if t]):
        elapsed = time.time() - start
        if elapsed > TIMEOUT_S:
            print(f"\n[{ts()}] TIMEOUT after {elapsed:.0f}s — giving up on remaining tasks")
            break

        await asyncio.sleep(POLL_S)

        pending = [t for t in tasks if t and t.task_id not in done]
        poll_coros = [provider.query_song_task(t.task_id) for t in pending]
        results = await asyncio.gather(*poll_coros, return_exceptions=True)

        for original_task, result in zip(pending, results):
            tid = original_task.task_id
            if isinstance(result, Exception):
                print(f"[{ts()}] task[{tid}] QUERY ERROR: {result}")
                history[tid].append(f"QUERY_ERROR({type(result).__name__})")
                continue

            current_status = result.status
            prev_status = history[tid][-1] if history[tid] else ""
            if current_status != prev_status:
                print(f"[{ts()}] task[{tid}]  {prev_status!r} -> {current_status!r}"
                      f"  elapsed={elapsed:.0f}s"
                      f"  failed_reason={result.raw.get('failed_reason')!r}")
            else:
                print(f"[{ts()}] task[{tid}]  status={current_status!r}  elapsed={elapsed:.0f}s")

            history[tid].append(current_status)

            if current_status in TERMINAL:
                done.add(tid)

    print("\n" + "=" * 70)
    print("SUMMARY — full status history per task")
    print("=" * 70)
    for task in tasks:
        if not task:
            continue
        tid = task.task_id
        h = history.get(tid, [])
        transitions = " -> ".join(h)
        final = h[-1] if h else "unknown"
        print(f"  {tid}  final={final!r}  history: {transitions}")
    print("=" * 70)

    any_transient_fail = any(
        "failed" in h[:-1]
        for h in history.values()
        if len(h) > 1
    )
    if any_transient_fail:
        print("\nCONCLUSION: at least one task showed a transient `failed` before a later status.")
        print("            The premature-failure theory is CONFIRMED.")
    else:
        print("\nCONCLUSION: no transient `failed` statuses observed in this run.")


async def main() -> None:
    provider = ProviderBMusicProvider()
    print(f"Key pool size : {provider._key_pool.count}")
    for i, key in enumerate(provider._key_pool._keys):
        masked = key[:8] + "..." + key[-4:] if len(key) > 12 else key[:4] + "..."
        print(f"  key[{i}]      : {masked}")
    print(f"Tasks         : {N_TASKS}")
    print(f"Poll interval : {POLL_S}s  |  Timeout: {TIMEOUT_S}s")
    print(f"(429 behaviour: rotate key immediately for first {provider._key_pool.count} attempt(s), then back off)\n")
    print(f"Launching {N_TASKS} concurrent tasks ...\n")

    launch_coros = [launch_task(provider, i) for i in range(N_TASKS)]
    launched: List[Optional[ProviderBTask]] = await asyncio.gather(*launch_coros)

    valid = [t for t in launched if t is not None]
    print(f"\n{len(valid)}/{N_TASKS} tasks launched successfully. Starting poll loop...\n")

    if not valid:
        print("No tasks to poll. Exiting.")
        return

    await poll_all(provider, valid)


if __name__ == "__main__":
    asyncio.run(main())
