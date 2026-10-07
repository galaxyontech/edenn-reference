"""Analyze the fake-provider perf test: per-stage timing per concurrency group,
D8-vs-highcpu compression attribution, queue/distribution validation."""
import json, statistics as st, sys
sys.path.insert(0, "/path/to/repo")
from EdennCode.env import load_env
from EdennCode.Deployment.postgres_wrapper import PostgresClient
load_env()
CANON = ["video_preprocess", "analysis_and_planning", "provider_candidate_generation", "selection_ranking_remix_finalize"]
jobs = json.load(open("/tmp/perf_jobs.json"))
owners = json.load(open("/tmp/perf_owners.json"))


def avg(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else 0


def main():
    c = PostgresClient.from_env().__enter__()
    # preprocess owner -> [durations], to cluster D8 (fast) vs highcpu (slow)
    prep_by_owner = {}
    group_rows = {}
    for group, jids in jobs.items():
        rows = []
        for jid in jids:
            j = c.run_sql("SELECT created_at, finished_at, status FROM async_v2_jobs WHERE job_id=%s", params=[jid])
            if not j:
                continue
            j = j[0]
            sr = {s["stage_name"]: s for s in c.run_sql(
                "SELECT stage_name, started_at, finished_at FROM async_v2_stage_runs WHERE job_id=%s", params=[jid])}
            if not all(k in sr and sr[k]["started_at"] and sr[k]["finished_at"] for k in CANON):
                rows.append({"jid": jid, "status": j["status"], "ok": False})
                continue
            e2e = (j["finished_at"] - j["created_at"]).total_seconds()
            q0 = (sr[CANON[0]]["started_at"] - j["created_at"]).total_seconds()
            durs = {k: (sr[k]["finished_at"] - sr[k]["started_at"]).total_seconds() for k in CANON}
            prep_owner = owners.get(f"{jid}:video-preprocess", {}).get("lease_owner")
            if prep_owner:
                prep_by_owner.setdefault(prep_owner, []).append(durs["video_preprocess"])
            rows.append({"jid": jid, "ok": True, "e2e": e2e, "q0": q0, "durs": durs, "prep_owner": prep_owner})
        group_rows[group] = rows
    c.close()

    # cluster owners into D8 (2 fastest by median prep) vs highcpu (rest)
    owner_med = {o: st.median(v) for o, v in prep_by_owner.items() if v}
    ranked = sorted(owner_med.items(), key=lambda kv: kv[1])
    d8 = set(o for o, _ in ranked[:2])
    print("=== preprocess workers observed (by median compress time) ===")
    for o, m in ranked:
        print(f"  {('D8 ' if o in d8 else 'highcpu')} {o[:40]:<42} median_compress={m:.1f}s  n={len(prep_by_owner[o])}")

    print("\n=== PER-GROUP per-stage means (seconds) ===")
    print(f"{'conc':<6}{'done':<7}{'e2e':>7}{'q-wait':>8}{'prep':>7}{'analyze':>9}{'provider':>10}{'finalize':>10}")
    for group in sorted(jobs, key=int):
        ok = [r for r in group_rows[group] if r.get("ok")]
        tot = len(group_rows[group])
        if not ok:
            print(f"{group:<6}{f'0/{tot}':<7}")
            continue
        print(f"{group:<6}{f'{len(ok)}/{tot}':<7}{avg([r['e2e'] for r in ok]):7.1f}{avg([r['q0'] for r in ok]):8.1f}"
              f"{avg([r['durs']['video_preprocess'] for r in ok]):7.1f}{avg([r['durs']['analysis_and_planning'] for r in ok]):9.1f}"
              f"{avg([r['durs']['provider_candidate_generation'] for r in ok]):10.1f}{avg([r['durs']['selection_ranking_remix_finalize'] for r in ok]):10.1f}")

    # GOAL 1: distribution — preprocess start staggering at each group
    print("\n=== GOAL 1: distribution (preprocess start offsets within group) ===")
    for group in sorted(jobs, key=int):
        ok = [r for r in group_rows[group] if r.get("ok")]
        if not ok:
            continue
        c2 = PostgresClient.from_env().__enter__()
        starts = []
        for r in ok:
            s = c2.run_sql("SELECT started_at FROM async_v2_stage_runs WHERE job_id=%s AND stage_name='video_preprocess'", params=[r["jid"]])
            if s and s[0]["started_at"]:
                starts.append(s[0]["started_at"])
        c2.close()
        if starts:
            base = min(starts)
            offs = sorted((s - base).total_seconds() for s in starts)
            n_concurrent = sum(1 for o in offs if o < 5)
            print(f"  conc={group}: prep starts within 5s = {n_concurrent}/{len(offs)}  offsets={[round(o,1) for o in offs]}")

    # GOAL 2: D8 vs highcpu compression
    print("\n=== GOAL 2: D8 vs highcpu compression time ===")
    d8_durs = [d for o in d8 for d in prep_by_owner.get(o, [])]
    hc_durs = [d for o in prep_by_owner if o not in d8 for d in prep_by_owner[o]]
    if d8_durs and hc_durs:
        print(f"  D8 (8cpu):     n={len(d8_durs)} mean={avg(d8_durs):.1f}s median={st.median(d8_durs):.1f}s")
        print(f"  highcpu(2cpu): n={len(hc_durs)} mean={avg(hc_durs):.1f}s median={st.median(hc_durs):.1f}s")
        print(f"  ratio highcpu/D8 = {avg(hc_durs)/max(avg(d8_durs),0.01):.2f}x")
    else:
        print("  (insufficient owner attribution; see worker table above)")

    # GOAL 3: queue mechanism — q-wait rising past 6 slots
    print("\n=== GOAL 3: queue mechanism (q-wait by concurrency) ===")
    for group in sorted(jobs, key=int):
        ok = [r for r in group_rows[group] if r.get("ok")]
        if ok:
            qs = sorted(r["q0"] for r in ok)
            print(f"  conc={group}: q-wait min={qs[0]:.1f} max={qs[-1]:.1f} mean={avg(qs):.1f}s")


if __name__ == "__main__":
    main()
