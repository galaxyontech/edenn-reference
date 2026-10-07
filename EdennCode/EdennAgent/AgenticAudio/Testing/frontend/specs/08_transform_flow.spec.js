/* Cut-shorts (transform), and the right pane it borrows.
 *
 * A cut session is a different session TYPE, not a third position on the
 * Timeline/Canvas toggle: it takes the whole right pane over and hides the
 * toggle with it. Two things therefore have to be true, and neither is about
 * the cut itself — the pane must hand over cleanly, and it must come BACK.
 * A leaked takeover is invisible until the next audio session renders into a
 * pane nobody can see, so the borrowing is what this journey is built around.
 *
 * On the offline transport the shell refuses the flow up front (app.js
 * startSession: "Cut-a-short needs the dev backend"), which is the honest
 * thing to do and is asserted here. Everything after that drives
 * EdennTransform.start() — the module's own public entry point, the exact call
 * the shell makes when the transport is live — the same discipline the
 * timeline spec uses with EdennTimeline.render(). The creation endpoints
 * (/api/v2/creation/*, /api/v2/library/*) are mounted by the dev host itself
 * and are reached with relative paths, so they answer regardless of which
 * transport the page picked; where they do not, the checks below say so
 * instead of passing quietly.
 */
"use strict";

const AUDIO_BRIEF = "Warm lo-fi for a vlog — hazy, relaxed, easy to talk over.";
/* The starter chip asks for a 16s teaser; the seeded dev library holds ~16s of
 * source, and planning a 16s cut out of it fails server-side (see the findings).
 * The journey asks for a length the dev host can actually plan, so the pane and
 * player checks are exercised rather than skipped. */
const CUT_INTENT = "Cut an 8s teaser from my library — open on the strongest moment, calm close.";

/** Everything about who currently owns the right pane, in one read. */
const pane = (page) => page.evaluate(() => {
  const h = (id) => { const n = document.getElementById(id); return n ? !!n.hidden : "MISSING"; };
  const body = document.getElementById("xrail-body");
  return {
    session: h("session"), entrance: h("entrance"),
    rpaneHd: h("rpane-hd"), tstage: h("tstage"), cstage: h("cstage"), xrail: h("xrail"),
    badge: ((document.getElementById("xrail-badge") || {}).textContent || "").trim(),
    railContent: body ? body.innerHTML.trim().length : -1,
    railCards: document.querySelectorAll(".tf-railcard").length,
    name: ((document.getElementById("session-name") || {}).textContent || "").trim(),
  };
});

const armed = (page) => page.evaluate(() =>
  !!(window.EdennTransform && window.EdennTransform.pending));

const toastText = (page) => page
  .$eval("#toast", (n) => (n.hidden ? "" : n.textContent.trim())).catch(() => "");

/** Wait for an in-page condition; false on timeout, never throws. */
async function waitJs(page, src, ms) {
  try { await page.waitForFunction(src, { timeout: ms || 15000, polling: 250 }); return true; }
  catch (_) { return false; }
}

/** The rail preview player, as the user sees it. */
const railPlayer = (page) => page.evaluate(() => {
  const card = document.querySelector("#xrail-body .tf-railcard");
  if (!card) return null;
  const v = card.querySelector("video");
  const ph = card.querySelector(".tf-ph");
  const chatPh = document.querySelector(".tf-plancard .tf-ph");
  return {
    label: (card.querySelector("[data-tf-play]") || {}).textContent.trim() || "",
    why: (card.querySelector("[data-tf-why]") || {}).textContent.trim() || "",
    left: ph ? (parseFloat(ph.style.left) || 0) : null,
    chatLeft: chatPh ? (parseFloat(chatPh.style.left) || 0) : null,
    t: v ? v.currentTime : null,
    paused: v ? v.paused : null,
    ready: v ? v.readyState : null,
    src: v ? (v.getAttribute("src") || "") : "",
  };
});

/** Click through the ask-first questions and the treatment gate. The flow
 *  BLOCKS on them by design, so nothing downstream exists until they are
 *  answered; the main journey asserts what they contain, this just walks. */
async function walkGate(page, H) {
  for (let i = 0; i < 5; i += 1) {
    const ready = await waitJs(page,
      "!!document.querySelector('.cards-row .intent-card:not([disabled])')"
      + " || !!document.querySelector('.qchips .qchip:not([disabled])')", 60000);
    if (!ready) return false;
    if (await H.exists(page, ".cards-row .intent-card:not([disabled])")) {
      await H.click(page, ".cards-row .intent-card:not([disabled])");
      return true;
    }
    await H.click(page, ".qchips .qchip:not([disabled])");
  }
  return false;
}

/** Kick the transform module the way the live shell does, without awaiting the
 *  flow — it deliberately blocks on the user's own chip clicks. */
const startCut = (page, intent) => page.evaluate((txt) => {
  window.__tf = { done: false, err: null };
  Promise.resolve(window.EdennTransform.start(txt))
    .then(() => { window.__tf.done = true; },
          (e) => { window.__tf.err = String((e && e.message) || e); });
  return true;
}, intent);

module.exports = {
  name: "transform flow",
  backend: "mock",
  persona: "Cut Tester",

  async run(page, t, { H }) {
    t.must(await H.exists(page, "#transform-chip"), "the entrance offers the cut-shorts starter");
    const api = await page.evaluate(() => ({
      pending: typeof window.EdennTransform.pending,
      start: typeof window.EdennTransform.start,
      release: typeof window.EdennTransform.release,
    }));
    t.must(api.start === "function", "EdennTransform.start is the module's entry point", api.start);
    t.ok(api.release === "function", "EdennTransform.release is exported for the shell", api.release);

    // ---- 1. arming and disarming the cut intent -----------------------------
    await t.step("arming: the chip sets the pending cut intent and fills the brief", async () => {
      await H.click(page, "#transform-chip");
      const fill = await page.$eval("#transform-chip", (n) => n.getAttribute("data-fill"));
      const text = await page.$eval("#start-text", (n) => n.value);
      t.ok(await armed(page), "EdennTransform.pending is armed");
      t.eq(text, fill, "the chip's own brief lands in the composer");
      return "armed";
    });

    await t.step("arming: picking a different starter disarms it", async () => {
      await H.click(page, "#starters .chip[data-fill]:not(#transform-chip)");
      const still = await armed(page);
      t.ok(!still, "an audio starter clears the cut intent", still ? "STILL ARMED" : "disarmed");
      return "disarmed by another starter";
    });

    await t.step("arming: Surprise me disarms it too", async () => {
      await H.click(page, "#transform-chip");
      t.must(await armed(page), "re-armed");
      await H.click(page, "#surprise-btn");
      const still = await armed(page);
      t.ok(!still, "Surprise me clears the cut intent", still ? "STILL ARMED" : "disarmed");
      return "disarmed by Surprise me";
    });

    // The known trap: only the CHIPS disarm. A user who arms the cut flow, then
    // changes their mind and types an ordinary audio brief, gets neither — the
    // typed sentence is discarded and the cut flow answers instead.
    await t.step("arming: typing an audio brief over an armed chip", async () => {
      await H.click(page, "#transform-chip");
      await H.type(page, "#start-text", AUDIO_BRIEF);
      const stillArmed = await armed(page);
      const text = await page.$eval("#start-text", (n) => n.value);
      t.eq(text, AUDIO_BRIEF, "the composer holds the user's own words");
      t.ok(!stillArmed,
        "typing a plain audio brief re-aims Start at the audio flow",
        stillArmed ? "still armed — Start will run the cut flow over the typed brief" : "disarmed");
      return stillArmed ? "armed despite the typed brief" : "disarmed";
    });

    let refused = false;
    await t.step("arming: which intent Start actually acts on", async () => {
      await H.click(page, "#start-btn", { settle: 500 });
      const msg = await toastText(page);
      const p = await pane(page);
      const sid = await page.evaluate(() => window.__edenn.app.sessionId);
      // The offline transport refuses the cut flow — and that refusal is the
      // tell: it proves the typed audio brief was never what Start acted on.
      refused = /cut/i.test(msg) && /backend|offline/i.test(msg);
      t.ok(!refused, "Start acts on the brief the user typed",
        refused ? `Start answered the armed cut chip instead: "${msg}"` : "audio path taken");
      if (refused) {
        t.ok(p.session === true && !sid, "the refusal opens no session at all",
          `session hidden=${p.session} sessionId=${sid}`);
      }
      return refused ? "cut path taken over the typed brief" : "typed brief honoured";
    });

    await t.step("recovery: the refusal disarms, so the next Start runs the audio flow", async () => {
      if (!refused) {
        await H.waitForIdle(page);
        await H.click(page, "#tb-home", { settle: 700 });
        return "no refusal to recover from";
      }
      t.ok(!(await armed(page)), "the refused cut intent was disarmed");
      await H.click(page, "#start-btn", { settle: 400 });
      const opened = await waitJs(page, "!document.getElementById('session').hidden", H.TURN_MS);
      t.must(opened, "an audio session opened on the second Start");
      await H.waitForIdle(page);
      const sid = await page.evaluate(() => window.__edenn.app.sessionId);
      t.ok(!!sid, "the audio session has a real id", sid || "none");
      await H.click(page, "#tb-home", { settle: 700 });
      return sid;
    });

    // ---- 2. the pane handover ----------------------------------------------
    const lib = await page.evaluate(async () => {
      try {
        const r = await fetch("/api/v2/library/assets");
        const j = await r.json().catch(() => ({}));
        return { status: r.status, assets: (j.assets || []).length };
      } catch (e) { return { status: 0, assets: 0, err: String(e) }; }
    });
    t.ok(lib.status === 200 && lib.assets > 0,
      "the dev host serves a creation library for the cut flow",
      `GET /api/v2/library/assets → ${lib.status}, ${lib.assets} assets`);

    // The one brief the product writes for itself. If the shipped starter can't
    // be planned, the flow's front door is broken however well the rest works.
    await t.step("the starter chip's own brief reaches a plan, or says why not", async () => {
      const brief = await page.$eval("#transform-chip", (n) => n.getAttribute("data-fill"));
      await startCut(page, brief);
      t.must(await waitJs(page, "!document.getElementById('xrail').hidden", 30000),
        "a cut session opened on the shipped brief");
      t.must(await walkGate(page, H), "the ask-first and treatment gates were answerable");
      // Settle on either outcome — a plan card, or the module giving up — so a
      // fast failure is not paid for with a two-minute wait.
      await waitJs(page, `(() => {
        if (document.querySelector(".tf-plancard .tf-strip")) return true;
        const n = document.getElementById("thread-inner");
        return !!n && /hit a wall|fail/i.test(n.innerText || "");
      })()`, 120000);
      const ok = await H.exists(page, ".tf-plancard .tf-strip");
      const tail = (await H.threadText(page)).split("\n").map((s) => s.trim())
        .filter(Boolean).pop() || "";
      t.ok(ok, "the brief the chip itself writes can be planned",
        ok ? "planned" : `the flow ended with: "${tail}"`);
      if (!ok) {
        // An error the user cannot act on is not an error message.
        const blank = /\{\}|\[\]|undefined|null|:\s*$/.test(tail);
        t.ok(!blank, "a failed plan names a reason", `"${tail}"`);
      }
      return ok ? "planned" : tail.slice(0, 70);
    });
    await H.click(page, "#tb-home", { settle: 800 }).catch(() => {});

    let tookOver = false;
    await t.step("handover: a cut session takes the right pane over wholesale", async () => {
      await startCut(page, CUT_INTENT);
      const up = await waitJs(page, "!document.getElementById('xrail').hidden", 30000);
      const p = await pane(page);
      const err = await page.evaluate(() => window.__tf && window.__tf.err);
      t.must(up, "the cut rail opened", err || JSON.stringify(p));
      tookOver = true;
      t.ok(p.rpaneHd === true, "the Timeline/Canvas header is hidden", `rpane-hd hidden=${p.rpaneHd}`);
      t.ok(p.tstage === true, "the timeline stage stepped aside", `tstage hidden=${p.tstage}`);
      t.ok(p.cstage === true, "the canvas stage stepped aside", `cstage hidden=${p.cstage}`);
      t.ok(p.xrail === false, "the cut rail owns the pane", `xrail hidden=${p.xrail}`);
      t.eq(p.badge, "cut preview", "the rail badges itself");
      t.eq(p.name, "cut session", "the topbar names the session type");
      const u = await H.usable(page, '#view-seg .vseg__btn[data-view="canvas"]');
      // Deliberate absence: no control may eject a cut session mid-flow.
      t.ok(!u.ok, "the view toggle is unreachable while the cut holds the pane",
        u.ok ? "TOGGLE STILL CLICKABLE" : u.why);
      return "pane handed over";
    });

    await t.step("handover: the chat composer belongs to no session during a cut", async () => {
      const c = await page.evaluate(() => {
        const i = document.getElementById("session-input");
        return { disabled: !!i.disabled, placeholder: i.getAttribute("placeholder") || "",
                 conn: !!window.__edenn.app.conn, sid: window.__edenn.app.sessionId };
      });
      t.ok(!c.conn && !c.sid, "a cut session has no agentic session behind it",
        `conn=${c.conn} sessionId=${c.sid}`);
      // The house rule the role gate already follows: a composer that cannot be
      // used is disabled AND says why in its own placeholder. If it is disabled
      // here, that is all there is to check.
      if (c.disabled) {
        t.ok(/cut|chip|pick|can't|cannot|not available/i.test(c.placeholder),
          "the disabled composer explains itself", `placeholder="${c.placeholder}"`);
        return `disabled ("${c.placeholder}")`;
      }
      // It is live. A live box has to survive being used — so use it.
      const errsBefore = H.pageErrors(page).fatal.length;
      await H.type(page, "#session-input", "Actually, make the open feel warmer.");
      await H.click(page, "#session-send", { settle: 900 });
      const after = await page.evaluate(() => ({
        thinking: !!document.querySelector(".think.is-live, .think__spin"),
        bubbles: document.querySelectorAll("#thread-inner .user-row").length,
      }));
      const thrown = H.pageErrors(page).fatal.slice(errsBefore);
      t.ok(!thrown.length, "sending in a cut session does not throw",
        thrown.join(" | ") || "clean");
      t.ok(!after.thinking, "no orphan 'Thinking…' is left over a message that went nowhere",
        after.thinking ? "spinner still live" : "clean");
      return `live composer, ${after.bubbles} bubbles`;
    });

    // ---- 3. the cut rail's contents ----------------------------------------
    await t.step("ask-first: the role question is asked as chips and locks when answered", async () => {
      const asked = await waitJs(page, "!!document.querySelector('.qchips .qchip:not([disabled])')", 60000);
      t.must(asked, "an ask-first question was posed before planning");
      const before = await page.evaluate(() => {
        const rows = Array.from(document.querySelectorAll(".qchips"));
        const live = rows.filter((r) => Array.from(r.querySelectorAll(".qchip")).some((c) => !c.disabled));
        const r = live[live.length - 1];
        return { chips: r.querySelectorAll(".qchip").length,
                 titled: Array.from(r.querySelectorAll(".qchip")).filter((c) => (c.title || "").length).length };
      });
      t.ok(before.chips >= 2, "the question offers real alternatives", `${before.chips} chips`);
      t.ok(before.titled === before.chips, "every option explains itself on hover",
        `${before.titled}/${before.chips} carry a description`);
      await H.click(page, ".qchips .qchip:not([disabled])");
      const after = await page.evaluate(() => {
        const r = Array.from(document.querySelectorAll(".qchips")).pop();
        const chips = Array.from(r.querySelectorAll(".qchip"));
        return { open: chips.filter((c) => !c.disabled).length, on: chips.filter((c) => c.classList.contains("is-on")).length };
      });
      t.eq(after.open, 0, "answering closes the whole card");
      t.eq(after.on, 1, "the chosen answer is marked");
      return `${before.chips} options, answered`;
    });

    await t.step("treatment: the gate is asked, never defaulted", async () => {
      // Any further ask-first rounds are answered the same way so the gate is
      // reached; the flow blocks on them by design.
      for (let i = 0; i < 4; i += 1) {
        const ready = await waitJs(page,
          "!!document.querySelector('.cards-row .intent-card') || !!document.querySelector('.qchips .qchip:not([disabled])')",
          60000);
        if (!ready) break;
        if (await H.exists(page, ".cards-row .intent-card")) break;
        await H.click(page, ".qchips .qchip:not([disabled])");
      }
      t.must(await H.exists(page, ".cards-row .intent-card"), "the treatment gate rendered");
      const cards = await page.$$eval(".cards-row .intent-card", (ns) => ns.map((n) => ({
        title: (n.querySelector(".intent-card__title") || {}).textContent || "",
        desc: (n.querySelector(".intent-card__desc") || {}).textContent || "",
        disabled: !!n.disabled,
      })));
      t.eq(cards.length, 3, "three treatments offered");
      t.ok(cards.every((c) => !c.disabled), "all three are open before a choice is made");
      t.ok(cards.every((c) => c.title && c.desc), "each treatment says what it does",
        cards.map((c) => c.title).join(" | "));
      await H.click(page, ".cards-row .intent-card");
      const after = await page.$$eval(".cards-row .intent-card", (ns) => ({
        open: ns.filter((n) => !n.disabled).length,
        on: ns.filter((n) => n.classList.contains("is-on")).length,
      }));
      t.eq(after.open, 0, "picking a treatment closes the gate");
      t.eq(after.on, 1, "the picked treatment is marked");
      return cards.map((c) => c.title).join(" | ");
    });

    let planned = false;
    await t.step("plan: the cut is shown before anything is rendered", async () => {
      const got = await waitJs(page, "!!document.querySelector('.tf-plancard .tf-strip')", 120000);
      if (!got) {
        const tail = (await H.threadText(page)).split("\n").filter(Boolean).slice(-3).join(" / ");
        t.ok(false, "the plan card rendered", `no .tf-plancard — thread ends: ${tail}`);
        return false;
      }
      planned = true;
      const strip = await page.evaluate(() => {
        const c = document.querySelector(".tf-plancard");
        const slots = Array.from(c.querySelectorAll(".tf-slot"));
        return {
          slots: slots.length,
          beats: c.querySelectorAll(".tf-beat").length,
          playhead: !!c.querySelector(".tf-ph"),
          reasons: slots.filter((s) => (s.title || "").trim().length).length,
          bounded: slots.every((s) => {
            const l = parseFloat(s.style.left), w = parseFloat(s.style.width);
            return l >= 0 && l <= 100 && w > 0 && l + w <= 101;
          }),
          note: (c.querySelector(".tf-note") || {}).textContent.trim() || "",
          rendered: document.querySelectorAll(".tf-video").length,
        };
      });
      t.ok(strip.slots >= 1, "the cut strip draws its slots", `${strip.slots} slots`);
      t.ok(strip.beats >= 2, "the beat grid is drawn under them", `${strip.beats} beats`);
      t.ok(strip.reasons === strip.slots, "every slot carries its reason",
        `${strip.reasons}/${strip.slots} have a why`);
      t.ok(strip.bounded, "no slot is drawn outside the clock");
      t.ok(strip.note.length > 0, "the card states the treatment and music mode", strip.note);
      t.eq(strip.rendered, 0, "nothing is rendered before the user locks");
      for (const sel of [".tf-plancard [data-tf-watch]", ".tf-plancard [data-tf-lock]"]) {
        const u = await H.usable(page, sel);
        t.ok(u.ok, `plan card: ${sel} usable`, u.why || "ok");
      }
      return `${strip.slots} slots · ${strip.note}`;
    });

    await t.step("rail: the cut rail carries a preview player, not a placeholder", async () => {
      if (!planned) { t.ok(false, "the rail player mounted", "no plan to preview"); return false; }
      const r = await railPlayer(page);
      t.must(!!r, "the rail holds a preview card");
      t.ok(/watch/i.test(r.label), "the player offers a play control", r.label);
      t.ok(r.src.length > 0, "the preview points at real media", r.src);
      const slots = await page.$$eval("#xrail-body .tf-railcard .tf-slot", (ns) => ns.length);
      t.ok(slots >= 1, "the rail repeats the cut strip", `${slots} slots`);
      return `${r.label} · ${slots} slots`;
    });

    await t.step("rail: Watch actually advances the preview", async () => {
      if (!planned) { t.ok(false, "the preview plays", "no plan to preview"); return false; }
      await H.click(page, "#xrail-body .tf-railcard [data-tf-play]");
      const moving = await waitJs(page, `(() => {
        const c = document.querySelector("#xrail-body .tf-railcard");
        const ph = c && c.querySelector(".tf-ph");
        return !!ph && (parseFloat(ph.style.left) || 0) > 0;
      })()`, 25000);
      const first = await railPlayer(page);
      // Give the preview a real second of playback before judging it: the point
      // of the player is that PICTURE runs under the playhead, and a frozen
      // frame with a moving marker is the failure this check exists to catch.
      await H.sleep(1600);
      const r = await railPlayer(page);
      t.ok(moving, "the playhead advances", `left=${first.left}% → ${r.left}% readyState=${r.ready}`);
      t.ok(r.t > first.t, "the picture follows the playhead",
        `currentTime ${first.t} → ${r.t} (playhead ${first.left}% → ${r.left}%)`);
      // Following is not playing: the clock re-seeks the element on drift, so a
      // PAUSED video still shows new frames — as a ~8fps slideshow.
      t.ok(r.paused === false, "the preview video is playing, not step-seeked",
        `paused=${r.paused} after ${(r.left).toFixed(1)}% of the cut`);
      t.ok(/pause/i.test(r.label), "the control flips to Pause while playing", r.label);
      t.ok(r.why.length > 0, "the rail says why the current slot is there", r.why || "(silent)");
      // The plan card in the thread draws the same strip — including a playhead.
      t.ok(r.chatLeft === null || r.chatLeft > 0,
        "the plan card's own playhead tracks the preview too", `chat playhead left=${r.chatLeft}%`);
      await H.click(page, "#xrail-body .tf-railcard [data-tf-play]");
      await H.sleep(500);
      const s1 = await railPlayer(page);
      await H.sleep(700);
      const s2 = await railPlayer(page);
      t.ok(/watch/i.test(s1.label), "Pause returns the control to Watch", s1.label);
      t.ok(s1.left === s2.left && s2.paused !== false,
        "pausing really stops the clock", `${s1.left}% → ${s2.left}% paused=${s2.paused}`);
      return `played to ${r.left}%`;
    });

    await t.step("rail: the chat card's Watch drives the same player", async () => {
      if (!planned) { t.ok(false, "Watch is wired to the rail player", "no plan"); return false; }
      const labelBefore = await page.$eval(".tf-plancard [data-tf-watch]", (n) => n.textContent.trim());
      await H.click(page, ".tf-plancard [data-tf-watch]");
      const started = await waitJs(page,
        "/pause/i.test((document.querySelector('#xrail-body .tf-railcard [data-tf-play]')||{}).textContent||'')",
        20000);
      const labelAfter = await page.$eval(".tf-plancard [data-tf-watch]", (n) => n.textContent.trim());
      t.ok(started, "the chat control starts the side-panel preview");
      t.eq(labelAfter, labelBefore, "the chat control keeps its own label (the rail owns play state)");
      await H.click(page, "#xrail-body .tf-railcard [data-tf-play]");
      return "proxied";
    });

    await t.step("lock: rendering is cost-gated, and cancelling changes nothing", async () => {
      if (!planned) { t.ok(false, "the lock is cost-gated", "no plan"); return false; }
      await H.click(page, ".tf-plancard [data-tf-lock]", { settle: 500 });
      const open = await page.$eval("#confirm-overlay", (n) => !n.hidden).catch(() => false);
      t.must(open, "Lock & render asks before it renders");
      const dlg = await page.evaluate(() => ({
        title: (document.getElementById("confirm-title") || {}).textContent.trim() || "",
        body: (document.getElementById("confirm-body") || {}).textContent.trim() || "",
        ok: (document.getElementById("confirm-ok") || {}).textContent.trim() || "",
      }));
      t.ok(dlg.body.length > 0, "the dialog states what locking costs", dlg.body.slice(0, 80));
      // Static copy, not the director's prose: the button must not promise a
      // spend the body has just ruled out.
      const contradicts = /no generation spend/i.test(dlg.body) && /generate/i.test(dlg.ok);
      t.ok(!contradicts, "the confirm button does not contradict its own body",
        `body: "…${dlg.body.slice(-40)}" · button: "${dlg.ok}"`);
      await H.approveSpend(page, { cancel: true });
      const after = await page.evaluate(() => {
        const b = document.querySelector(".tf-plancard [data-tf-lock]");
        return { disabled: !!b.disabled, label: b.textContent.trim(),
                 videos: document.querySelectorAll(".tf-video").length,
                 overlay: document.getElementById("confirm-overlay").hidden };
      });
      t.ok(after.overlay, "cancelling closes the gate");
      t.ok(!after.disabled, "cancelling leaves the lock control usable", after.label);
      t.eq(after.videos, 0, "cancelling renders nothing");
      return `gated: "${dlg.title}" / ok="${dlg.ok}"`;
    });

    // ---- 4. THE REGRESSION: the pane has to come back -----------------------
    await t.step("release: going Home hands the right pane back", async () => {
      if (!tookOver) { t.ok(false, "the pane was released", "the cut never took the pane"); return false; }
      await H.click(page, "#tb-home", { settle: 900 });
      const p = await pane(page);
      const playing = await page.evaluate(() => Array.from(document.querySelectorAll("audio, video"))
        .filter((n) => !n.paused && n.currentTime > 0).length);
      t.ok(p.entrance === false && p.session === true, "back at the entrance", JSON.stringify(p));
      t.ok(p.xrail === true, "the cut rail is hidden", `xrail hidden=${p.xrail}`);
      t.eq(p.railContent, 0, "the rail is emptied, not just hidden");
      t.eq(p.railCards, 0, "no preview card survives");
      t.ok(p.rpaneHd === false, "the view toggle is back", `rpane-hd hidden=${p.rpaneHd}`);
      t.ok(p.tstage === false, "the previously selected view is restored", `tstage hidden=${p.tstage}`);
      t.eq(playing, 0, "the preview stopped playing when the session was left");
      return "pane returned";
    });

    await t.step("regression: the next audio session gets a working right pane", async () => {
      const sid = await H.startSession(page, { text: AUDIO_BRIEF });
      t.must(!!sid, "a normal audio session opened after the cut session", sid || "none");
      const p = await pane(page);
      t.ok(p.xrail === true, "no stale cut rail over the audio session", `xrail hidden=${p.xrail}`);
      t.eq(p.railCards, 0, "no stale 'Cut preview' card");
      const u = await H.usable(page, "#rpane-hd");
      t.ok(u.ok, "the Timeline/Canvas header is usable again", u.why || "ok");
      // Operable, not merely visible — click it and watch the stages move.
      await H.click(page, '#view-seg .vseg__btn[data-view="canvas"]');
      const onCanvas = await pane(page);
      t.ok(onCanvas.cstage === false && onCanvas.tstage === true,
        "the toggle still switches to Canvas", JSON.stringify({ t: onCanvas.tstage, c: onCanvas.cstage }));
      await H.click(page, '#view-seg .vseg__btn[data-view="timeline"]');
      const back = await pane(page);
      t.ok(back.tstage === false && back.cstage === true,
        "and back to Timeline", JSON.stringify({ t: back.tstage, c: back.cstage }));
      return sid;
    });

    await t.step("release: calling it with no cut session in flight is a safe no-op", async () => {
      const before = await pane(page);
      const r = await page.evaluate(() => {
        try { window.EdennTransform.release(); window.EdennTransform.release(); return "ok"; }
        catch (e) { return "threw: " + String((e && e.message) || e); }
      });
      t.eq(r, "ok", "release() is idempotent and does not throw");
      const after = await pane(page);
      t.ok(after.rpaneHd === false, "the view header is still up", `rpane-hd hidden=${after.rpaneHd}`);
      t.ok(after.xrail === true, "the cut rail stays hidden", `xrail hidden=${after.xrail}`);
      t.eq(after.tstage, before.tstage, "the selected view is untouched");
      await H.click(page, '#view-seg .vseg__btn[data-view="canvas"]');
      const c = await pane(page);
      await H.click(page, '#view-seg .vseg__btn[data-view="timeline"]');
      t.ok(c.cstage === false, "the toggle still works after a spurious release", `cstage hidden=${c.cstage}`);
      // And at the entrance, where there is no session at all.
      await H.click(page, "#tb-home", { settle: 700 });
      const home = await page.evaluate(() => {
        try { window.EdennTransform.release(); return "ok"; }
        catch (e) { return "threw: " + String((e && e.message) || e); }
      });
      t.eq(home, "ok", "release() at the entrance is safe too");
      return "idempotent";
    });

    // The other way out of a cut session: the History popup. Home releases the
    // pane; this path has to as well, or the resumed audio session renders into
    // a pane the user cannot see or reach. Last, because it leaves the shell in
    // whatever state it actually produces.
    await t.step("regression: resuming from History also hands the pane back", async () => {
      await startCut(page, CUT_INTENT);
      const up = await waitJs(page, "!document.getElementById('xrail').hidden", 30000);
      t.must(up, "a second cut session took the pane");
      await H.click(page, "#tb-history", { settle: 500 });
      const rows = await page.$$eval("#history-pop .history-pop__row", (ns) => ns.length).catch(() => 0);
      if (!rows) {
        t.ok(false, "History lists a session to resume from a cut session",
          await page.$eval("#history-pop", (n) => n.innerText.trim()).catch(() => "no popup"));
        return false;
      }
      await H.click(page, "#history-pop .history-pop__row", { settle: 600 });
      const adopted = await waitJs(page, "!!window.__edenn.app.sessionId", 30000);
      t.must(adopted, "the audio session was resumed");
      await H.sleep(800);
      const p = await pane(page);
      t.ok(p.xrail === true, "the cut rail is gone after resuming", `xrail hidden=${p.xrail}`);
      t.ok(p.rpaneHd === false, "the view toggle came back", `rpane-hd hidden=${p.rpaneHd}`);
      t.ok(p.tstage === false || p.cstage === false,
        "the resumed session has a visible right pane",
        `tstage hidden=${p.tstage} cstage hidden=${p.cstage}`);
      const u = await H.usable(page, '#view-seg .vseg__btn[data-view="canvas"]');
      t.ok(u.ok, "and a toggle the user can reach", u.ok ? "ok" : u.why);
      return "resumed";
    });
  },
};
