# Perf-test drivers (fake provider)

Scripts used for the v1-vs-v2 performance test. See `docs/perf-test-v1-v2.md` for the
full methodology, results, and how to reproduce.

- `perf_driver.py`   — v2 driver: fires concurrency groups 2/4/6/8/10 at the v2 split endpoint,
  captures per-task `lease_owner` (for D8-vs-highcpu attribution).
- `perf_analyze.py`  — v2 analysis: per-stage timing, distribution, D8-vs-highcpu, queue.
- `perf_driver_v1.py`— v1 driver: synchronous concurrent POSTs to `/api/v1/jobs/video`.
- `perf_compare.py`  — side-by-side v1 (`pipeline_stages`) vs v2 (`async_v2_stage_runs`).

These are **manual drivers, not pytest tests**: they submit real jobs to a deployed
endpoint. Their names (`perf_driver*`) keep them out of pytest collection, all side
effects are guarded behind `if __name__ == '__main__'`, and `conftest.py` in this
directory tells pytest to never collect anything here. Run a driver explicitly, e.g.
`python EdennCode/Deployment/async_pipeline_v2/Testing/perf/perf_driver.py`.

Requires `ASYNC_V2_FAKE_PROVIDER=1` on the relevant apps. Edit the `BASE`/`VIDEO`
constants for the target endpoint. Revert fake mode after testing.
