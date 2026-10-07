"""Pytest entry point for the browser-driven console suite.

The suite itself is JavaScript (it drives a real Chrome against a real console —
see ``Testing/frontend``). This wrapper exists so the browser coverage is
reachable from the same command as everything else, and so CI reports it as a
test rather than a script someone has to remember to run.

It SKIPS rather than fails when the environment cannot support it — no node, no
Chrome, no console running. A red build for "you don't have Chrome installed"
teaches people to ignore red builds.

    # start a console first (either is fine):
    #   .venv/bin/python EdennCode/EdennAgent/AgenticAudio/design/devserver.py
    #   python3 -m http.server 5599 --directory .../frontend
    pytest EdennCode/EdennAgent/AgenticAudio/Testing/test_console_e2e.py

    EDENN_CONSOLE_BASE   console origin (default http://localhost:8800)
    EDENN_CONSOLE_E2E=1  run it; unset means skip (it needs a live console)
    EDENN_E2E_LIVE=1     also run the spending, live-pipeline journey
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SUITE = Path(__file__).resolve().parent / "frontend"
BASE = os.getenv("EDENN_CONSOLE_BASE", "http://localhost:8800")
CHROME = os.getenv(
    "EDENN_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)


def _console_is_up() -> bool:
    try:
        req = urllib.request.Request(BASE + "/", method="HEAD")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status < 500
    except (urllib.error.URLError, OSError):
        return False


def _requirements_or_skip() -> None:
    if not os.getenv("EDENN_CONSOLE_E2E"):
        pytest.skip("set EDENN_CONSOLE_E2E=1 to run the browser suite")
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    if not Path(CHROME).exists():
        pytest.skip(f"Chrome not found at {CHROME} (set EDENN_CHROME)")
    if not (SUITE / "node_modules" / "puppeteer-core").is_dir():
        pytest.skip(f"run `npm install` in {SUITE}")
    if not _console_is_up():
        pytest.skip(f"no console at {BASE} (start the dev server first)")


def _run(args: list[str], report: Path) -> dict:
    env = dict(os.environ, EDENN_REPORT_JSON=str(report), EDENN_CONSOLE_BASE=BASE)
    proc = subprocess.run(
        ["node", "run.js", *args],
        cwd=SUITE, env=env, capture_output=True, text=True, timeout=3600,
    )
    print(proc.stdout[-8000:])
    if proc.stderr.strip():
        print("stderr:", proc.stderr[-2000:])
    if proc.returncode == 255:
        pytest.skip("console became unreachable mid-run")
    return json.loads(report.read_text()) if report.is_file() else {}


def _assert_green(result: dict) -> None:
    summaries = result.get("summaries", [])
    assert summaries, "the suite reported no specs"
    failures = [
        f"{s['name']}: {f}" for s in summaries for f in s.get("failures", [])
    ]
    total = sum(s["total"] for s in summaries)
    assert not failures, (
        f"{len(failures)} of {total} console checks failed:\n  "
        + "\n  ".join(failures)
    )


def test_console_controls_and_flows(tmp_path: Path) -> None:
    """Every control and journey, against the offline mock backend.

    Deterministic and free: this is the gate that should run on every change to
    the console.
    """
    _requirements_or_skip()
    _assert_green(_run(["--backend=mock"], tmp_path / "mock.json"))


@pytest.mark.skipif(
    not os.getenv("EDENN_E2E_LIVE"),
    reason="set EDENN_E2E_LIVE=1 — this journey uploads real footage and spends",
)
def test_console_against_the_live_pipeline(tmp_path: Path) -> None:
    """The live journey: real analysis, real plan, real spotting sheet."""
    _requirements_or_skip()
    _assert_green(_run(["--backend=real"], tmp_path / "live.json"))
