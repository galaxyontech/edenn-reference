"""Perf DRIVER (fake provider): groups at concurrency 2/4/6/8/10 on the TEST v2 split.
Captures per-stage timing + which worker ran each preprocess (D8 vs highcpu).
Saves /tmp/perf_jobs.json (group -> [job_ids]) and /tmp/perf_owners.json (task -> lease_owner).

This is a manual benchmarking driver, NOT a pytest test. It submits real jobs to a
deployed endpoint, so all side effects live under main() / ``if __name__ == '__main__'``
and the module is import-safe (importing it must never touch the network or a DB). Run it
explicitly:  python EdennCode/Deployment/async_pipeline_v2/Testing/perf/perf_driver.py
"""
import json
import time
import urllib.request
import concurrent.futures as cf

BASE = "https://staging-app.worker.example.invalid"
VIDEO = "https://download.blender.org/durian/trailer/sintel_trailer-1080p.mp4"  # ~52s 1080p, stable
GROUPS = [2, 4, 6, 8, 10]


def submit(i, group):
    body = {"video_url": VIDEO, "modelspec": "edenn_basic", "mode": "split",
            "user_prompt": "perf", "compression_flag": True, "compression_max_height": 720,
            "priority": 7, "max_attempts": 2, "creator_user_id": "perf", "session_id": f"perf-g{group}-{i}"}
    req = urllib.request.Request(BASE + "/api/v2/jobs/video-music", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=40).read())["job_id"]


def status(jid):
    try:
        return json.loads(urllib.request.urlopen(BASE + f"/api/v2/jobs/{jid}", timeout=20).read()).get("status")
    except Exception:
        return ""


def sample_owners(c, jids, owners):
    rows = c.run_sql(
        "SELECT task_id, task_type, lease_owner FROM async_v2_tasks "
        "WHERE job_id IN %s AND lease_owner IS NOT NULL", params=[tuple(jids)])
    for r in rows:
        owners[r["task_id"]] = {"task_type": r["task_type"], "lease_owner": r["lease_owner"]}


def main():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
    from EdennCode.env import load_env
    from EdennCode.Deployment.postgres_wrapper import PostgresClient
    load_env()

    owners = {}  # task_id -> lease_owner (captured while leased)
    all_jobs = {}
    c = PostgresClient.from_env().__enter__()
    for group in GROUPS:
        print(f"\n=== GROUP concurrency={group} ===", flush=True)
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=group) as ex:
            jids = list(ex.map(lambda i: submit(i, group), range(group)))
        all_jobs[str(group)] = jids
        print(f"  submitted {len(jids)} in {time.time()-t0:.1f}s", flush=True)
        pending = set(jids)
        while pending and time.time() - t0 < 600:
            sample_owners(c, jids, owners)  # capture lease_owner while tasks are in flight
            for jid in list(pending):
                s = status(jid)
                if s in ("completed", "failed", "canceled"):
                    pending.remove(jid)
            if pending:
                time.sleep(1.5)
        print(f"  group drained in {time.time()-t0:.1f}s", flush=True)
        json.dump(all_jobs, open("/tmp/perf_jobs.json", "w"))
        json.dump(owners, open("/tmp/perf_owners.json", "w"))
        time.sleep(5)  # settle between groups
    c.close()
    print("\nDONE", flush=True)


if __name__ == "__main__":
    main()
