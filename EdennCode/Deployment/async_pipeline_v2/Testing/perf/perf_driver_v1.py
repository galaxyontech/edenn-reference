"""V1 perf DRIVER (fake provider): concurrency 2/4/6/8/10 on the TEST v1 endpoint.
V1 is synchronous (POST blocks until done). Per-stage timing comes from pipeline_stages.
Saves /tmp/perf_v1_jobs.json (group -> [job_ids]).

This is a manual benchmarking driver, NOT a pytest test. All side effects live under
main() / ``if __name__ == '__main__'`` so importing it never touches the network.
Run it explicitly:  python EdennCode/Deployment/async_pipeline_v2/Testing/perf/perf_driver_v1.py
"""
import json
import subprocess
import time
import concurrent.futures as cf

BASE = "https://staging-app.worker.example.invalid"
VIDEO = "https://download.blender.org/durian/trailer/sintel_trailer-1080p.mp4"
GROUPS = [2, 4, 6, 8, 10]


def submit(i, group):
    """Synchronous v1 POST (blocks until complete); returns (job_id, wall_s)."""
    args = ["curl", "-sS", "-m", "600", "-X", "POST", BASE + "/api/v1/jobs/video",
            "-F", f"video_url={VIDEO}", "-F", "modelspec=edenn_basic",
            "-F", "compression_flag=true", "-F", "include_vocals=false",
            "-F", "user_prompt=perf", "-F", "water_mark=false"]
    t0 = time.time()
    out = subprocess.run(args, capture_output=True, text=True)
    wall = time.time() - t0
    try:
        d = json.loads(out.stdout)
        return d.get("job_id"), round(wall, 1), d.get("status")
    except Exception:
        return None, round(wall, 1), "BAD:" + out.stdout[:80]


def main():
    all_jobs = {}
    for group in GROUPS:
        print(f"\n=== V1 GROUP concurrency={group} ===", flush=True)
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=group) as ex:
            results = list(ex.map(lambda i: submit(i, group), range(group)))
        jids = [r[0] for r in results if r[0]]
        walls = [r[1] for r in results]
        statuses = [r[2] for r in results]
        all_jobs[str(group)] = jids
        ok = sum(1 for s in statuses if s == "completed")
        print(f"  {ok}/{group} completed | group wall={time.time()-t0:.1f}s | "
              f"per-job wall sorted={sorted(walls)}", flush=True)
        json.dump(all_jobs, open("/tmp/perf_v1_jobs.json", "w"))
        time.sleep(5)
    print("\nDONE", flush=True)


if __name__ == "__main__":
    main()
