#!/usr/bin/env node
/* ============================================================================
 * Console E2E runner.
 *
 *   node run.js                      # every mock-backend spec (fast, free)
 *   node run.js --backend=real       # the live-pipeline journeys (slow, spends)
 *   node run.js --backend=all
 *   node run.js --only=timeline      # substring match on spec file/name
 *   node run.js --headful            # watch it drive
 *
 * Exit code is the number of failed checks (capped at 250), so CI can gate on
 * it and a human can see the count without reading the log.
 * ========================================================================== */
"use strict";

const fs = require("fs");
const path = require("path");
const H = require("./harness/console");

const argv = process.argv.slice(2);
const arg = (k, d) => {
  const hit = argv.find((a) => a.startsWith(`--${k}=`));
  return hit ? hit.split("=").slice(1).join("=") : d;
};
const flag = (k) => argv.includes(`--${k}`);

if (flag("headful")) process.env.EDENN_HEADFUL = "1";

const want = arg("backend", "mock");          // mock | real | all
const only = arg("only", "");
const SPEC_DIR = path.join(__dirname, "specs");

async function reachable() {
  // 401/403 mean a console IS there and is asking for a token — the normal state
  // of a deployed instance. A HEAD can also 404/405 on a GET-only route, so fall
  // back to GET before concluding there is nothing to drive.
  const verdict = (r) => {
    if (r.status === 401 || r.status === 403) return true;
    return r.ok || r.status === 307;
  };
  try {
    if (verdict(await fetch(H.BASE + "/", { method: "HEAD" }))) return true;
  } catch (_) { return false; }
  try {
    return verdict(await fetch(H.BASE + "/"));
  } catch (_) { return false; }
}

(async () => {
  if (!(await reachable())) {
    console.error(`\nNo console at ${H.BASE}.`);
    console.error(`Start one:  .venv/bin/python EdennCode/EdennAgent/AgenticAudio/design/devserver.py`);
    console.error(`or point EDENN_CONSOLE_BASE at a running console.\n`);
    process.exit(255);
  }

  const files = fs.readdirSync(SPEC_DIR).filter((f) => f.endsWith(".spec.js")).sort();
  const specs = files.map((f) => Object.assign(
    { file: f }, require(path.join(SPEC_DIR, f))));

  const selected = specs.filter((s) => {
    if (only && !(s.file + " " + s.name).toLowerCase().includes(only.toLowerCase())) return false;
    if (want === "all") return true;
    const b = s.backend || "mock";
    return b === want || b === "both";
  });

  if (!selected.length) {
    console.error(`no specs matched (backend=${want}${only ? `, only=${only}` : ""})`);
    process.exit(254);
  }

  console.log(`\nConsole E2E — ${selected.length} spec(s) against ${H.BASE}\n`);
  const browser = await H.launch();
  const summaries = [];
  try {
    for (const s of selected) {
      const backend = (s.backend === "both" || !s.backend)
        ? (want === "real" ? "real" : "mock")
        : s.backend;
      const { Spec } = require("./harness/spec");
      const spec = new Spec(s.name);
      let page = null;
      try {
        page = await H.openConsole(browser, {
          backend,
          persona: s.persona || "Test Pilot",
          query: s.query || "",
        });
        await s.run(page, spec, { H, backend });
        // A thrown exception anywhere in the page is a defect, always.
        const errs = H.pageErrors(page);
        spec.ok(!errs.fatal.length, "no uncaught page errors", errs.fatal.join(" | ") || "clean");
        spec.ok(!errs.console.length, "no console errors", errs.console.slice(0, 3).join(" | ") || "clean");
      } catch (e) {
        spec.ok(false, "spec aborted", e && e.message ? e.message : String(e));
      } finally {
        if (page) { try { await page.close(); } catch (_) {} }
      }
      summaries.push(spec.summary());
    }
  } finally {
    try { await browser.close(); } catch (_) {}
  }

  let failed = 0, total = 0;
  console.log("\n──────── summary ────────");
  for (const s of summaries) {
    total += s.total; failed += s.failed;
    const tag = s.failed ? `FAIL ${s.failed}/${s.total}` : `pass ${s.total}`;
    console.log(`${s.failed ? "✗" : "✓"} ${s.name.padEnd(34)} ${tag}  ${(s.ms / 1000).toFixed(1)}s`);
    s.failures.forEach((f) => console.log(`    · ${f}`));
  }
  console.log(`\n${total - failed}/${total} checks passed\n`);

  if (process.env.EDENN_REPORT_JSON) {
    fs.writeFileSync(process.env.EDENN_REPORT_JSON,
      JSON.stringify({ base: H.BASE, backend: want, summaries }, null, 2));
  }
  process.exit(Math.min(failed, 250));
})();
