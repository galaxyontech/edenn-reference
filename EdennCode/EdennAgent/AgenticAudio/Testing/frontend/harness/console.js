/* ============================================================================
 * Console E2E harness — drives the AgenticAudio console in a real browser.
 *
 * Two backends, deliberately:
 *
 *   mock  — the in-page offline backend (mock-backend.js). Deterministic, free,
 *           instant. This is where EVERY control gets exercised, because the
 *           question there is "is this button wired to the thing it names".
 *   real  — the dev server's live pipeline (live director, real analysis, real
 *           renders). Slow and costs model spend, so it runs the few journeys
 *           where the question is "does the product actually do this".
 *
 * A test that asserts on exact assistant wording will rot: the director is a
 * language model and its phrasing is not a contract. Assert on STATE (snapshot
 * fields) and on STRUCTURE (does the control exist, is it enabled, did the lane
 * gain a clip) — never on a sentence.
 * ========================================================================== */
"use strict";

const puppeteer = require("puppeteer-core");

const BASE = process.env.EDENN_CONSOLE_BASE || "http://localhost:8800";
const CHROME = process.env.EDENN_CHROME
  || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const HEADLESS = process.env.EDENN_HEADFUL ? false : "new";
/** Live turns wait on a model; mock turns wait on a timer. */
const TURN_MS = Number(process.env.EDENN_TURN_MS || 180000);

async function launch() {
  return puppeteer.launch({
    executablePath: CHROME,
    headless: HEADLESS,
    args: ["--no-sandbox", "--autoplay-policy=no-user-gesture-required",
           "--window-size=1440,900"],
    defaultViewport: { width: 1440, height: 900 },
  });
}

/**
 * A console page with a clean identity.
 *
 * The persona and the chat-pane width live in localStorage, so a suite that
 * reuses a profile inherits the previous spec's UI state and starts asserting
 * against a layout no fresh user would see. Every page begins blank.
 */
async function openConsole(browser, opts) {
  opts = opts || {};
  const backend = opts.backend || "mock";
  const page = await browser.newPage();
  const errors = [];
  const consoleErrors = [];
  page.on("pageerror", (e) => errors.push(String(e && e.message || e)));
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });
  // Blank slate: clear storage on the origin BEFORE the app boots.
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.evaluate(() => { try { localStorage.clear(); } catch (_) {} });
  const url = `${BASE}/?backend=${backend}` + (opts.query || "");
  await page.goto(url, { waitUntil: "networkidle2" });
  await page.waitForFunction(() => !!window.__edenn, { timeout: 30000 });
  if (opts.persona) await setPersona(page, opts.persona);
  page._edenn = { errors, consoleErrors, backend };
  return page;
}

/** Name the tester so collab surfaces have a real identity to show. */
async function setPersona(page, name) {
  await page.evaluate((nm) => {
    const cur = JSON.parse(localStorage.getItem("edenn.persona") || "{}");
    localStorage.setItem("edenn.persona", JSON.stringify(
      { id: cur.id || ("guest-" + Math.random().toString(36).slice(2, 6)), name: nm }));
  }, name);
  await page.reload({ waitUntil: "networkidle2" });
  await page.waitForFunction(() => !!window.__edenn, { timeout: 30000 });
}

/* --------------------------------------------------------------- queries */

/** The live snapshot the app is rendering (null before a session opens). */
async function snapshot(page) {
  return page.evaluate(() => {
    const s = window.__edenn && window.__edenn.app && window.__edenn.app.snapshot;
    return s ? JSON.parse(JSON.stringify(s)) : null;
  });
}
async function state(page) {
  const s = await snapshot(page);
  return (s && s.state) || null;
}

/** Visible text of the chat thread — for coarse presence checks only. */
async function threadText(page) {
  return page.$eval("#thread-inner", (n) => n.innerText);
}

async function exists(page, sel) {
  return page.evaluate((s) => !!document.querySelector(s), sel);
}

/**
 * Is this control actually usable by a person right now?
 *
 * A control can be present and still be unusable — hidden by an ancestor,
 * collapsed to zero size, disabled, or covered by an overlay. "Present in the
 * DOM" is the weakest possible claim, and asserting it is how a suite passes
 * while the UI is broken.
 */
async function usable(page, sel) {
  return page.evaluate((s) => {
    const n = document.querySelector(s);
    if (!n) return { ok: false, why: "absent" };
    if (n.disabled) return { ok: false, why: "disabled" };
    if (n.closest("[hidden]")) return { ok: false, why: "inside [hidden]" };
    const r = n.getBoundingClientRect();
    if (!r.width || !r.height) return { ok: false, why: "zero size" };
    const cs = getComputedStyle(n);
    if (cs.visibility === "hidden" || cs.display === "none") return { ok: false, why: cs.visibility === "hidden" ? "visibility:hidden" : "display:none" };
    if (Number(cs.opacity) === 0) return { ok: false, why: "opacity:0" };
    if (cs.pointerEvents === "none") return { ok: false, why: "pointer-events:none" };
    // Hit-test the centre: catches a control sitting under an overlay.
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x >= 0 && y >= 0 && x <= innerWidth && y <= innerHeight) {
      const top = document.elementFromPoint(x, y);
      if (top && top !== n && !n.contains(top) && !top.contains(n)) {
        return { ok: false, why: "covered by " + (top.className || top.tagName) };
      }
    }
    return { ok: true, why: "" };
  }, sel);
}

/* ---------------------------------------------------------------- actions */

/**
 * Click through the real event path.
 *
 * page.click() drives a synthetic mouse at coordinates and misses anything the
 * layout has moved or covered; el.click() bypasses hit-testing entirely. We use
 * the real mouse when the element is hit-testable and fall back to a dispatched
 * click only for elements a human could reach by keyboard, so a control that is
 * genuinely unreachable still fails the test.
 */
async function click(page, sel, opts) {
  opts = opts || {};
  const u = await usable(page, sel);
  if (!u.ok && !opts.force) throw new Error(`click ${sel}: not usable (${u.why})`);
  await page.$eval(sel, (n) => n.scrollIntoView({ block: "center" }));
  try {
    await page.click(sel, { delay: 10 });
  } catch (e) {
    if (!opts.force) throw e;
    await page.$eval(sel, (n) => n.click());
  }
  if (opts.settle !== false) await sleep(opts.settle || 220);
}

async function type(page, sel, text) {
  await page.click(sel);
  await page.$eval(sel, (n) => { n.value = ""; });
  await page.type(sel, text, { delay: 4 });
}

/** Attach a real file through the real <input type=file>. */
async function attachVideo(page, filePath) {
  const input = await page.$("#video-file");
  if (!input) throw new Error("no #video-file input");
  await input.uploadFile(filePath);
  await page.evaluate(() => document.getElementById("video-file")
    .dispatchEvent(new Event("change", { bubbles: true })));
  // The chip reports progress; wait for it to settle off "uploading…".
  await page.waitForFunction(() => {
    const m = document.getElementById("attach-meta");
    return m && m.textContent && !/uploading/i.test(m.textContent);
  }, { timeout: 120000 });
  return page.$eval("#attach-meta", (n) => n.textContent.trim());
}

/**
 * Start a session from the entrance and wait until the session view is up.
 *
 * `video` is optional: the offline mock analyzes a canned sample, so a pure-UI
 * journey does not need a file. A real-backend journey must pass one — without
 * it the dev host has nothing genuine to analyze.
 */
async function startSession(page, opts) {
  opts = opts || {};
  if (opts.video) await attachVideo(page, opts.video);
  if (opts.chip) await click(page, `#starters .chip[data-fill="${opts.chip}"]`);
  await page.$eval("#start-text", (n, v) => { n.value = v; },
    opts.text || "Make it cinematic and premium.");
  await click(page, "#start-btn", { settle: 400 });
  await page.waitForFunction(() => {
    const s = document.getElementById("session");
    return s && !s.hidden;
  }, { timeout: opts.timeout || TURN_MS });
  // The bootstrap turn (analysis → brief) is the first thing a user waits on.
  if (opts.wait !== false) await waitForIdle(page, opts.timeout || TURN_MS);
  return page.evaluate(() => {
    const a = window.__edenn.app;
    return (a.snapshot && a.snapshot.session_id) || a.sessionId || null;
  });
}

/** Send a chat turn and wait for the app to stop thinking. */
async function say(page, text, opts) {
  opts = opts || {};
  await page.waitForFunction(() => {
    const i = document.getElementById("session-input");
    return i && !i.disabled;
  }, { timeout: TURN_MS });
  await type(page, "#session-input", text);
  await click(page, "#session-send", { settle: 150 });
  if (opts.wait === false) return;
  await waitForIdle(page, opts.timeout || TURN_MS);
}

/**
 * Idle = no thinking block open and the composer is live again.
 *
 * A turn takes a moment to START. Waiting only for "not busy" therefore returns
 * instantly on the state BEFORE the action took effect, and the assertion that
 * follows reads stale state and blames the app. So: give the turn a chance to
 * begin, and only then wait for it to finish.
 */
async function waitForIdle(page, timeout) {
  const busy = () => page.evaluate(() => {
    const i = document.getElementById("session-input");
    return !!(document.querySelector(".think.is-live, .think__spin") || (i && i.disabled));
  });
  const t0 = Date.now();
  while (Date.now() - t0 < 4000) {
    if (await busy()) break;
    await sleep(200);
  }
  await page.waitForFunction(() => {
    const i = document.getElementById("session-input");
    const thinking = document.querySelector(".think.is-live, .think__spin");
    return i && !i.disabled && !thinking;
  }, { timeout: timeout || TURN_MS, polling: 400 });
  await sleep(300);
}

/**
 * Answer the spend-approval dialog if one is open.
 *
 * Generation is cost-gated, so several controls open a confirm before they do
 * anything. A journey that ignores it leaves a modal over the whole app and
 * every later step fails on "covered by overlay" — which looks like a UI bug
 * and is really just an unanswered question.
 */
async function approveSpend(page, opts) {
  opts = opts || {};
  const open = await page.evaluate(() => {
    const o = document.getElementById("confirm-overlay");
    return o && !o.hidden;
  });
  if (!open) return null;
  const body = await page.$eval("#confirm-body", (n) => n.textContent.trim()).catch(() => "");
  await click(page, opts.cancel ? "#confirm-cancel" : "#confirm-ok", { settle: 400 });
  return body;
}

/** Wait for a selector, returning false instead of throwing on timeout. */
async function waitFor(page, sel, timeout) {
  try {
    await page.waitForSelector(sel, { timeout: timeout || 30000 });
    return true;
  } catch (_) { return false; }
}

/**
 * Wait until the rendered snapshot satisfies a predicate on state.
 *
 * `predSrc` is the SOURCE of a function of state, as a string, because it is
 * compiled inside the page: `"(st) => (st.candidates || []).length >= 2"`.
 */
async function waitForState(page, predSrc, timeout, label) {
  const src = `(() => {
    const app = window.__edenn && window.__edenn.app;
    const st = app && app.snapshot && app.snapshot.state;
    if (!st) return false;
    try { return !!(${predSrc})(st); } catch (_) { return false; }
  })()`;
  try {
    await page.waitForFunction(src, { timeout: timeout || TURN_MS, polling: 500 });
  } catch (e) {
    throw new Error(`waitForState(${label || predSrc}) timed out after ${timeout || TURN_MS}ms`);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------------------------------------- reporting */

async function shot(page, name) {
  const dir = process.env.EDENN_SHOT_DIR;
  if (!dir) return null;
  const fs = require("fs");
  fs.mkdirSync(dir, { recursive: true });
  const p = `${dir}/${name}.png`;
  await page.screenshot({ path: p });
  return p;
}

/** Page errors are failures: a thrown exception mid-turn breaks the console. */
function pageErrors(page) {
  const e = page._edenn || {};
  // Media that will not decode is an environment fact, not an app defect.
  const ignorable = /Failed to load resource|net::ERR_|favicon|MEDIA_ELEMENT|autoplay/i;
  return {
    fatal: (e.errors || []),
    console: (e.consoleErrors || []).filter((m) => !ignorable.test(m)),
  };
}

module.exports = {
  BASE, TURN_MS,
  launch, openConsole, setPersona,
  snapshot, state, threadText, exists, usable,
  click, type, attachVideo, startSession, say, waitForIdle, waitFor, waitForState, approveSpend,
  sleep, shot, pageErrors,
};
