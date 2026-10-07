"""Side-by-side v1 vs v2 perf comparison (fake provider).
v1 per-stage from pipeline_stages; v2 per-stage from async_v2_stage_runs."""
import json, statistics as st, sys
sys.path.insert(0, "/path/to/repo")
from EdennCode.env import load_env
from EdennCode.Deployment.postgres_wrapper import PostgresClient
from EdennCode.Deployment.pipeline_telemetry import _as_uuid
load_env()

V1 = json.load(open("/tmp/perf_v1_jobs.json"))
V2 = json.load(open("/tmp/perf_jobs.json"))
V1_STAGES = ["user_intent", "video_preprocess", "scene_segmentation", "video_understanding",
             "music_prompt_orchestration", "music_generation", "video_audio_remix"]
V2_STAGES = ["video_preprocess", "analysis_and_planning", "provider_candidate_generation",
             "selection_ranking_remix_finalize"]


def avg(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else 0


def main():
    c = PostgresClient.from_env().__enter__()

    # ---- v1: pipeline_stages ----
    v1_group = {}
    for g, jids in V1.items():
        rows = []
        for jid in jids:
            rid = _as_uuid(jid)
            run = c.run_sql("SELECT duration_ms FROM pipeline_runs WHERE run_id=%s", params=[rid])
            stl = c.run_sql("SELECT stage_name, duration_ms FROM pipeline_stages WHERE run_id=%s", params=[rid])
            if not run or not stl:
                continue
            d = {s["stage_name"]: (s["duration_ms"] or 0) / 1000.0 for s in stl}
            rows.append({"total": (run[0]["duration_ms"] or 0) / 1000.0, "d": d})
        v1_group[g] = rows

    # ---- v2: async_v2_stage_runs ----
    v2_group = {}
    for g, jids in V2.items():
        rows = []
        for jid in jids:
            j = c.run_sql("SELECT created_at, finished_at FROM async_v2_jobs WHERE job_id=%s", params=[jid])
            if not j or not j[0]["finished_at"]:
                continue
            sr = {s["stage_name"]: s for s in c.run_sql(
                "SELECT stage_name, started_at, finished_at FROM async_v2_stage_runs WHERE job_id=%s", params=[jid])}
            d = {k: ((sr[k]["finished_at"] - sr[k]["started_at"]).total_seconds()
                     if k in sr and sr[k]["started_at"] and sr[k]["finished_at"] else 0) for k in V2_STAGES}
            e2e = (j[0]["finished_at"] - j[0]["created_at"]).total_seconds()
            q0 = (sr["video_preprocess"]["started_at"] - j[0]["created_at"]).total_seconds() if "video_preprocess" in sr and sr["video_preprocess"]["started_at"] else 0
            rows.append({"e2e": e2e, "q0": q0, "d": d})
        v2_group[g] = rows
    c.close()

    print("=== V1 per-stage (pipeline_stages), mean seconds by concurrency ===")
    hdr = "".join(f"{s.split('_')[0][:7]:>9}" for s in V1_STAGES)
    print(f"{'conc':<6}{'total':>8}{hdr}")
    for g in sorted(V1, key=int):
        r = v1_group.get(g, [])
        if not r: print(f"{g:<6} (none)"); continue
        cells = "".join(f"{avg([x['d'].get(s,0) for x in r]):9.1f}" for s in V1_STAGES)
        print(f"{g:<6}{avg([x['total'] for x in r]):8.1f}{cells}  (n={len(r)})")

    print("\n=== V2 per-stage (async_v2_stage_runs), mean seconds by concurrency ===")
    print(f"{'conc':<6}{'e2e':>8}{'q-wait':>8}{'prep':>8}{'analyze':>9}{'provider':>10}{'finalize':>10}")
    for g in sorted(V2, key=int):
        r = v2_group.get(g, [])
        if not r: print(f"{g:<6} (none)"); continue
        print(f"{g:<6}{avg([x['e2e'] for x in r]):8.1f}{avg([x['q0'] for x in r]):8.1f}"
              f"{avg([x['d']['video_preprocess'] for x in r]):8.1f}{avg([x['d']['analysis_and_planning'] for x in r]):9.1f}"
              f"{avg([x['d']['provider_candidate_generation'] for x in r]):10.1f}{avg([x['d']['selection_ranking_remix_finalize'] for x in r]):10.1f}  (n={len(r)})")

    # ---- mapped buckets side by side ----
    print("\n=== MAPPED BUCKETS: v1 vs v2 (mean s), by concurrency ===")
    print(f"{'conc':<6} {'bucket':<22}{'v1':>8}{'v2':>8}")
    for g in sorted(set(V1) & set(V2), key=int):
        r1, r2 = v1_group.get(g, []), v2_group.get(g, [])
        if not r1 or not r2:
            continue
        def v1b(keys): return avg([sum(x['d'].get(k, 0) for k in keys) for x in r1])
        def v2b(key): return avg([x['d'].get(key, 0) for x in r2])
        rows = [
            ("preprocess(dl+compress)", v1b(["video_preprocess"]), v2b("video_preprocess")),
            ("analysis(LLM)", v1b(["user_intent", "scene_segmentation", "video_understanding", "music_prompt_orchestration"]), v2b("analysis_and_planning")),
            ("gen(faked)", v1b(["music_generation"]), v2b("provider_candidate_generation")),
            ("match+remix/finalize", v1b(["video_audio_remix"]), v2b("selection_ranking_remix_finalize")),
            ("TOTAL e2e", avg([x['total'] for x in r1]), avg([x['e2e'] for x in r2])),
        ]
        for name, a, b in rows:
            print(f"{g:<6} {name:<22}{a:8.1f}{b:8.1f}")
        print()


if __name__ == "__main__":
    main()
