# agent/ — the brain (orchestration + reasoning loop)

The agent decides, conversationally, which tool to run next. Split so each concern
is testable on its own; external code imports `agentic_audio.agent`.

| File | Role |
|---|---|
| `agent.py` | `AgenticAudioAgent` — the **thin orchestrator**: public entry points (bootstrap / message / choice / refresh) and the DETERMINISTIC paths (the intent gate, `/choices` routing). Delegates model-driven turns to the loop, tool runs to the dispatcher. Also `build_agentic_audio_agent_client()`. |
| `loop.py` | `ReasoningLoop` — one bounded turn: build context → get one strict-JSON decision → stream the reasoning beat → dispatch the action (tool / propose / clarify / ask / noop) → break on a heavy tool or terminal action → persist turn + durable memory. |
| `dispatcher.py` | `ToolDispatcher` — the one place a tool name becomes a running tool (registry lookup + `ToolContext`). Used by both the orchestrator and the loop. |
| `prompts.py` | `SYSTEM_PROMPT` (its own module so both agent and loop can import it without a cycle). |
| `session_io.py` | Shared session-I/O helpers (event/message construction, the live `agent.reasoning` beat) used by both agent and loop. |

## Flow

```
create_session ─► bootstrap (intent gate OR loop)
message ─────────► ReasoningLoop.run ──► one decision/step ──► dispatch ──► persist
choice ──────────► deterministic route (approve+generate / finalize / mix / variation / voiceover / clarify) ──► loop
```

Per-turn the loop classifies `intent`, folds durable `memory`, and emits live
`agent.reasoning` beats. The cost gate lives in the generation tools; the loop
catches `ApprovalRequiredError` and degrades to a friendly re-ask (never a 400).
Tools run through the dispatcher; the agent itself holds no per-tool business logic.
