# api/ — HTTP + WebSocket surface

The FastAPI router and everything at the network boundary. External code imports
`agentic_audio.api` (this package re-exports the router functions, so the path is
unchanged after the restructure).

| File | Role |
|---|---|
| `router.py` | `create_agentic_audio_router` / `mount_agentic_audio_router`; all REST endpoints + the `/ws` WebSocket; serves the `frontend/` console under `/app`. |
| `auth.py` | Gated auth (default off → byte-identical). `Authorization: Bearer` on REST, `?token=` on WS; optional `AGENTIC_AUDIO_API_KEYS` token→user map; session-ownership 403. |
| `serialization.py` | Turns `AgenticAudioWebSocketEvent` / snapshots into the wire dicts (`event_payload`, `events_payload`, `snapshot_opened_event`). |

## Endpoints (base `/api/v2/agentic/audio`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/sessions` | create a session for a source video (optional first message) |
| GET | `/sessions` | list the caller's sessions, most-recent first (History view; `?creator=` filter when auth is off) |
| GET | `/sessions/{id}` | full snapshot (load / reconnect) |
| POST | `/sessions/{id}/messages` | free-text user message |
| POST | `/sessions/{id}/choices` | structured choice (proposal/candidate/compose/clarification/mix/variation/voiceover) |
| WS | `/sessions/{id}/ws` | live event stream |
| GET | `/app`, `/app/{asset}` | the served console (`frontend/`) |

Errors map to HTTP status (`KeyError`→404, `ValueError`→400, `AuthError`→401/403).
The router is thin: it authenticates/authorizes, delegates to `AgenticAudioPlanner`
→ `AgenticAudioAgent`, then returns `{snapshot, events}` (REST) or streams events (WS).
