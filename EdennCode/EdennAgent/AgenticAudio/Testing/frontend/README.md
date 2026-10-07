# Console E2E suite

A real Chrome drives the real console and checks every control a person can
touch. It exists because the console is where this product actually lives, and
none of it was covered: a dead button, a control hidden under an overlay, or a
card claiming a mix that never rendered all ship green through a Python suite.

## Running it

```bash
# 1. start a console (either works)
.venv/bin/python EdennCode/EdennAgent/AgenticAudio/design/devserver.py    # live pipeline
python3 -m http.server 5599 --directory EdennCode/EdennAgent/AgenticAudio/frontend

# 2. install once
cd EdennCode/EdennAgent/AgenticAudio/Testing/frontend && npm install

# 3. run
node run.js                      # every offline-mock spec — fast, free, deterministic
node run.js --only=timeline      # one spec
node run.js --headful            # watch it drive
node run.js --backend=real       # the live journey (uploads real footage, SPENDS)
```

From pytest, with the same gates CI uses:

```bash
EDENN_CONSOLE_E2E=1 pytest EdennCode/EdennAgent/AgenticAudio/Testing/test_console_e2e.py
EDENN_CONSOLE_E2E=1 EDENN_E2E_LIVE=1 pytest ...   # include the spending journey
```

The exit code is the number of failed checks. Without a console running, the
runner exits 255 and the pytest wrapper skips — a missing dev server is not a
test failure.

| variable | meaning |
| --- | --- |
| `EDENN_CONSOLE_BASE` | console origin (default `http://localhost:8800`) |
| `EDENN_CHROME` | path to Chrome |
| `EDENN_TURN_MS` | how long to wait for one turn (default 180000) |
| `EDENN_FIXTURE_VIDEO` | footage the live journey uploads |
| `EDENN_HEADFUL` / `--headful` | show the browser |
| `EDENN_SHOT_DIR` | write `shot()` screenshots here |
| `EDENN_REPORT_JSON` | write a machine-readable report |

## The two backends, and why

**`mock`** — the in-page offline backend. Deterministic, instant, free. This is
where every control gets exercised, because the question there is *"is this
button wired to the thing it names"*. Run it on every console change.

**`real`** — the dev server's live pipeline: real upload, real scene analysis,
the live director. Slow, and it spends. It runs the handful of journeys where
the question is *"does the product actually do this"* — real scenes off real
footage, the user's layer selection surviving into the real production plan, the
spotting sheet, and no provider name reaching the user.

The mock is only useful while it tells the same story as the server. When you
change a contract, change `mock-backend.js` in the same commit — there is a
worked example in `planForIntent()`, which mirrors the server's rule that the
picker's explicit layer list outranks the intent id.

## Running against a deployed console

```bash
EDENN_CONSOLE_BASE="https://<host>/api/v2/agentic/audio/app" \
EDENN_CONSOLE_TOKEN="<token>" node run.js --backend=real --only=live
```

A deployed instance requires auth, so the token rides the tab's query string;
the runner treats a 401 probe as "a console is there" rather than "nothing to
drive", and falls back to GET because HEAD 404s on a GET-only route.

**Known limitation.** The live journey uploads a multi-megabyte file, and
headless Chrome's TLS to a remote ingress fails that request intermittently
here with `ERR_SSL_BAD_RECORD_MAC_ALERT` — the same file uploads fine with
`curl` to the same URL in the same moment. When you see `upload failed` followed
by a cascade of "detached Frame" errors, check the endpoint directly before
believing the app is broken:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST "https://<host>/api/v2/assets/video" \
  -H "Authorization: Bearer <token>" -F "video=@<file>"
```

A 200 there means the deployment is healthy and the failure is the test client's
network path. Against a local dev server (no TLS) the upload is reliable, which
is why the mock and local-live runs never hit this.

## Writing a spec

```js
module.exports = {
  name: "what this journey proves",
  backend: "mock",            // "mock" | "real" | "both"
  persona: "Some Tester",
  async run(page, t, { H }) {
    await H.startSession(page, { text: "Cinematic and premium." });
    await t.step("the take reaches the music lane", async () => {
      t.must(await H.waitFor(page, ".take"), "a take row exists");
      const clips = await page.$$eval(".tl-row.lane-music .tl-clip", (n) => n.length);
      t.ok(clips >= 1, "music lane populated", String(clips));
      return "ok";
    });
  },
};
```

Rules that keep this suite worth running:

- **Assert on state and structure, never on the director's wording.** It is a
  language model; its phrasing is not a contract. `H.state(page)` is the truth.
- **`H.click` refuses controls a person could not click.** It checks disabled,
  hidden ancestors, zero size, `pointer-events`, *and* hit-tests the centre
  point — that last one is how the suite found clip selection dead under a
  transparent seek overlay. Do not reach for `{force:true}` to get past a
  failure; a control you cannot click is the finding.
- **Answer the cost gate.** Generation is spend-gated, so many controls open a
  confirm. `H.approveSpend(page)` answers it; skip it and every later step fails
  with "covered by overlay", which looks like a bug and is not one.
- **A failing check does not stop the journey.** Twenty controls after the
  broken one still get exercised. Use `t.must` only when continuing would be
  meaningless.
- **Never weaken an assertion to get green.** If the product is wrong, leave the
  red and fix the product — or assert the honest absence deliberately and say
  why in the detail string.

`specs/03_timeline_view.spec.js` shows the technique for states the pipeline
cannot reach on demand (a dead media URL, a session with no duration, a probe
failure): synthesize a snapshot and hand it to the module's own public
`render()`. The rendering path under test stays real; only the clock is ours.

## What is covered

| spec | backend | covers |
| --- | --- | --- |
| `01_entrance` | mock | nav, panes, docs, settings + identity, every composer control and starter chip |
| `02_session_shell` | mock | the pane shell, view toggle, resize grip, narrow reflow, topbar, and the navigation regressions (leaving a session, starting a second one) |
| `03_timeline_view` | mock | every timeline state — empty, unplayable video, pending/measured/failed audio, VO drafts and segment plans, SFX moments and beds, scenes, moments, mutes, seek, selection |
| `04_music_conversation` | mock | the layer gate, the direction table, takes, the overflow menu, spend gating, locking, and a long multi-turn conversation |
| `05_voiceover_flow` | mock | the voice-over card, script and segment plan, voices, generation, and the VO lane |
| `06_sfx_flow` | mock | treatment gate, plan card and its editors, variants, density cap, and the SFX lane |
| `08_transform_flow` | mock | cut-shorts arming, the right-pane handover, and handing it back |
| `09_unhappy_paths` | mock | refusals, cancelled spend, over-claim checks, layout under abuse, backend-down honesty |
| `10_live_journey` | **real** | real analysis, the real production plan, the spotting sheet, and provider-name leaks |

## When a check fails

Read the detail string first — checks are written to report what was actually
observed, not just that something differed. Then reproduce it by hand with
`--headful --only=<spec>`. If the spec is wrong, fix the spec; if the console is
wrong, fix the console and leave the check exactly as it is.
