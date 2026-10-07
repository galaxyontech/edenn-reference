# domain/ — first-class entities (User + Artifacts)

The redesign's **User / Artifacts** separation, MVP form: typed entities over the
existing `sessions.state_json` blob — no new DB tables (deferred). Reads are typed;
mutation still happens dict-side in the tools, so the persisted/wire shape is unchanged.

| File | Role |
|---|---|
| `user.py` | `User` — wraps the (optional, un-FK'd) `creator_user_id` so ownership / quotas / auth have a first-class type to hang on. |
| `artifacts.py` | `Proposal` / `Candidate` (the existing cards) + typed `Mix` / `VoiceoverLayer` / `FinalArtifact` / `ProductionPlan` (extra-allowing, so a typed view round-trips the dict losslessly), and **`CandidateGraph`** — the candidate branching graph (`find` / `children_of` / `next_version` / `resolve`) over the raw candidate dicts. |
| `session_state.py` | `SessionState` — a typed accessor over `state_json`: `candidates` / `proposals` / `graph`, typed singletons (`mix` / `voiceover` / `final_artifact` / `production_plan`), and `resolve_proposal`. |

`ToolContext` exposes `ctx.user` and routes candidate/proposal resolution + version
numbering through `CandidateGraph` / `SessionState`, so the branching graph
(parent/version/edit_kind) is a real queryable object rather than scattered dict pokes.
Unit-tested in `Testing/test_domain.py`.
