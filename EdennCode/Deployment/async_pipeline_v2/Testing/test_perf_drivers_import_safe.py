"""Guards that the perf drivers never submit jobs when imported.

The perf drivers submit real jobs to a deployed endpoint. They used to be named
``perf_test*.py`` with their submit loops at module scope, so pytest collected
and imported them during a normal ``pytest`` run and fired ~30 jobs at the live
endpoint before any test executed. See
``EdennCode/Deployment/PROD_READINESS_REMEDIATION_PLAN.md`` finding #9.

This test imports each driver with a network tripwire installed and asserts the
import performs no socket I/O — proving all job-submitting side effects stay
behind ``if __name__ == '__main__'`` and can never run during collection. Before
the fix (module-scope submit loop) importing the driver trips the tripwire.
"""
from __future__ import annotations

import importlib.util
import socket
from pathlib import Path

import pytest

_PERF_DIR = Path(__file__).resolve().parent / "perf"


@pytest.mark.parametrize("driver", ["perf_driver.py", "perf_driver_v1.py"])
def test_perf_driver_import_makes_no_network_calls(driver: str, monkeypatch) -> None:
    def _tripwire(*args, **kwargs):
        raise AssertionError(f"{driver} attempted network I/O at import time")

    # Any real socket connection during import fails loudly.
    monkeypatch.setattr(socket.socket, "connect", _tripwire)
    monkeypatch.setattr(socket, "create_connection", _tripwire)

    path = _PERF_DIR / driver
    assert path.exists(), f"expected perf driver at {path}"
    spec = importlib.util.spec_from_file_location(
        f"_perf_under_test_{driver.replace('.', '_')}", path
    )
    module = importlib.util.module_from_spec(spec)
    # Executes the module top level; must NOT submit jobs / open a DB / poll.
    spec.loader.exec_module(module)

    # The driver is still a usable script: its work lives in main().
    assert callable(getattr(module, "main", None))
    assert callable(getattr(module, "submit", None))
