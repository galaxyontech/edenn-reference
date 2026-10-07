"""Diagnostic probe: does ONE ProviderB key handle concurrent task creation?

Fires N parallel POST /v1/song/generate calls bound to a single key, then
prints what came back. Use this to decide whether to add per-key locking,
queue control, or just keep relying on ProviderB's own rate limits.

Run with:
    RUN_REMOTE_INTEGRATION_LOCAL=1 .venv/bin/python -m pytest \\
        tests/test_provider_b_single_key_concurrency_probe.py -s

The `-s` is important — the test's value is in the printed report, not the
assertion. The assertion just confirms the probe completed.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional

import pytest

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_music import (
    ProviderBMusicProvider,
)
from EdennCode.TestSuites.helpers.integration import require_any_remote_env


CONCURRENCY = 3


def _pick_any_key() -> Optional[str]:
    for var in (
        "PROVIDER_B_API_KEY_1",
        "PROVIDER_B_API_KEY_2",
        "PROVIDER_B_API_KEY",
        "EDENN_ENHANCED_PROVIDER_B_API_KEY",
    ):
        value = (os.getenv(var) or "").strip()
        if value:
            return value
    return None


async def _fire_one(provider: ProviderBMusicProvider, index: int) -> Dict[str, Any]:
    start = time.time()
    try:
        task = await provider.generate_song_task(
            lyrics=(
                f"[verse]\nProbe run {index}, line one\nProbe run {index}, line two\n"
                f"[chorus]\nConcurrent test {index}, here we go\n"
            ),
            prompt="upbeat pop instrumental sketch",
            n=1,
        )
        return {
            "index": index,
            "ok": True,
            "status_code": 200,
            "task_id": task.task_id,
            "trace_id": task.trace_id,
            "key_label": task.api_key_label,
            "elapsed_s": round(time.time() - start, 3),
            "error": None,
        }
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        return {
            "index": index,
            "ok": False,
            "status_code": status_code,
            "task_id": None,
            "trace_id": None,
            "key_label": None,
            "elapsed_s": round(time.time() - start, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }


@pytest.mark.remote_integration
def test_provider_b_single_key_concurrency_probe() -> None:
    require_any_remote_env(
        "EDENN_ENHANCED_PROVIDER_B_API_KEY",
        "PROVIDER_B_API_KEY",
        "PROVIDER_B_API_KEY_1",
        "PROVIDER_B_API_KEY_2",
    )

    api_key = _pick_any_key()
    assert api_key, "no ProviderB API key available"

    provider = ProviderBMusicProvider(api_key=api_key)
    # Skip billing roundtrips during the probe — we want to measure /v1/song/generate
    # behavior in isolation, not amplify the test with parallel /v1/account/billing calls.
    provider._credit_cache["constructor_api_key"] = (10_000_000, time.time())

    async def _run() -> List[Dict[str, Any]]:
        return await asyncio.gather(
            *[_fire_one(provider, i) for i in range(CONCURRENCY)]
        )

    suite_start = time.time()
    results = asyncio.run(_run())
    suite_elapsed = round(time.time() - suite_start, 3)

    ok_count = sum(1 for r in results if r["ok"])
    fail_count = CONCURRENCY - ok_count

    print()
    print("=" * 72)
    print(f"ProviderB single-key concurrency probe — {CONCURRENCY} parallel POSTs")
    print(f"Suite elapsed: {suite_elapsed}s")
    print("-" * 72)
    for r in sorted(results, key=lambda x: x["index"]):
        if r["ok"]:
            print(
                f"  [{r['index']}] OK    "
                f"{r['elapsed_s']:>6}s  "
                f"task_id={r['task_id']}  "
                f"trace={r['trace_id'] or '-'}  "
                f"key={r['key_label']}"
            )
        else:
            print(
                f"  [{r['index']}] FAIL  "
                f"{r['elapsed_s']:>6}s  "
                f"status={r['status_code']}  "
                f"error={r['error']}"
            )
    print("-" * 72)
    print(f"  {ok_count}/{CONCURRENCY} succeeded, {fail_count} failed")
    print()
    rate_limited = sum(1 for r in results if r["status_code"] == 429)
    auth_failed = sum(1 for r in results if r["status_code"] in (401, 403))

    if ok_count == CONCURRENCY:
        print("VERDICT: ProviderB accepted all concurrent POSTs on one key.")
        print("         → Key supports parallel task creation. No per-key lock needed.")
    elif rate_limited == CONCURRENCY:
        print("VERDICT: ALL rate-limited (429). ProviderB rejects concurrent POSTs on one key.")
        print("         → Per-key serialization is required for safe burst use. Either")
        print("           add an asyncio.Semaphore(1) per key, queue requests per-key,")
        print("           or guarantee enough keys in the pool that random selection")
        print("           almost never collides at your peak concurrency.")
    elif rate_limited > 0 and ok_count > 0:
        print(
            f"VERDICT: Mixed — {ok_count} accepted, {rate_limited} rate-limited."
        )
        print("         → ProviderB has a small concurrency budget per key. Consider per-key")
        print("           throttling at your expected concurrency, or longer backoff.")
    elif auth_failed > 0:
        print(f"VERDICT: {auth_failed}/{CONCURRENCY} auth errors — check the API key.")
    else:
        print("VERDICT: Inconclusive — see per-request errors above.")
    print("=" * 72)

    # The probe's purpose is the printed report. Assert only that we ran cleanly.
    assert all(r["status_code"] is not None or r["ok"] for r in results), (
        "probe couldn't reach ProviderB at all — check key and network"
    )
