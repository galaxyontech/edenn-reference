# design/ — design-time artifacts (not shipped)

Things used while building/evaluating the console, separate from the served app
(which now lives in `../frontend/`).

| File | Role |
|---|---|
| `devserver.py` | A standalone dev server that serves `../frontend/` and stands in for the agent LLM (an offline "dev director") so the walkthrough runs with no API keys. Run: `python EdennCode/EdennAgent/AgenticAudio/design/devserver.py`. |
| `UX_EVALUATION.md` | The UX walkthrough + the four personas (Maya / Devon / Priya / Nadia) and the P0/P1 findings that drove the gap-fill work. |
| `setup-widget-options.html` | The designer's static explorations (round 1) for the session-setup widgets — the audit + six alternatives behind the layer picker and direction table. Open directly in a browser; not served by the app. |
| `setup-widget-options-v2.html` | Round 2 of the setup + comparison-table explorations, with in-page A/B toggles. |

The personas here are encoded as runnable scenarios in `../Testing/e2e/scenarios.py`.
