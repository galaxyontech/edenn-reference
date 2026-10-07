/* Unhappy paths: everything the console has to say "no" to.
 *
 * A production gate cares less about the happy path than about two questions:
 * does this console ever claim a capability it does not have, and does it ever
 * fail without saying so. Every check below is one of those two.
 *
 * Three rules the checks are written against:
 *   1. A refusal must be VISIBLE. A control that quietly does nothing is worse
 *      than one that errors, because the user waits.
 *   2. A cancelled spend must spend NOTHING. That is billing, not UX.
 *   3. A number the pipeline never measured must not appear anywhere. "Plausible"
 *      is the failure mode this whole file exists to catch.
 *
 * Where the offline mock stands in for real work, the check says so in its
 * detail rather than passing quietly on a stand-in's behaviour. */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

/** A small real file for the attach path. The mock never reads it — which is
 *  exactly the thing the attach chip has to admit. */
function fixtureFile() {
  const p = path.join(os.tmpdir(), "edenn_unhappy_fixture.mp4");
  if (!fs.existsSync(p)) fs.writeFileSync(p, Buffer.alloc(2048, 7));
  return p;
}

/** Start a session the way a first-time user does: nothing attached, nothing typed. */
async function startBlank(page, H) {
  await page.$eval("#start-text", (n) => { n.value = ""; });
  await H.click(page, "#start-btn", { settle: 400 });
  await page.waitForFunction(() => {
    const s = document.getElementById("session");
    return s && !s.hidden;
  }, { timeout: H.TURN_MS });
  await H.waitForIdle(page);
  return page.evaluate(() => {
    const a = window.__edenn.app;
    return (a.snapshot && a.snapshot.session_id) || a.sessionId || null;
  });
}

/** Record every window.open the app attempts — "Download"/"Export" are all opens. */
const watchOpens = (page) => page.evaluate(() => {
  window.__opened = [];
  const orig = window.open;
  window.open = function (u) { window.__opened.push(String(u).slice(0, 60)); return null; };
  window.__origOpen = orig;
});
const opens = (page) => page.evaluate(() => (window.__opened || []).slice());

/** Anything a person would read as "the console answered me". */
const feedback = (page) => page.evaluate(() => ({
  toast: document.getElementById("toast").hidden ? "" : document.getElementById("toast").textContent.trim(),
  thinking: !!document.querySelector(".think.is-live, .think__spin"),
  bubbles: document.querySelectorAll("#thread-inner .user-row, #thread-inner .agent-row").length,
}));

/** The last thing the director said, for over-claim inspection only. */
const lastReply = (page) => page.evaluate(() => {
  const rows = Array.from(document.querySelectorAll("#thread-inner .agent-row"));
  const n = rows[rows.length - 1];
  return n ? n.innerText.trim() : "";
});

/**
 * Does this sentence PROMISE the named capability?
 *
 * Not a wording assertion — the director is a language model. It only fires when
 * an affirmative and the capability appear together with no refusal anywhere in
 * the reply, which is the shape of an over-claim in any phrasing.
 */
function promises(reply, capability) {
  const affirm = /\b(i'?ll|i will|i can|i've|sure|absolutely|of course|done|on it|no problem|happy to|let me)\b/i;
  const refuse = /\b(can'?t|cannot|unable|not able|won'?t|isn'?t|is not|not possible|not supported|don'?t support|no way to|instead|but i)\b/i;
  return capability.test(reply) && affirm.test(reply) && !refuse.test(reply);
}

/** Push a synthesized snapshot through the app's own event entry point. */
async function inject(page, patch) {
  return page.evaluate((p) => {
    const app = window.__edenn.app;
    if (!app.snapshot) return false;
    const snap = JSON.parse(JSON.stringify(app.snapshot));
    Object.keys(p).forEach((k) => { snap.state[k] = p[k]; });
    window.__edenn.onEvent({
      event_type: "session.opened", session_id: snap.session_id, payload: { snapshot: snap },
    });
    return true;
  }, patch);
}

/** Drive the timeline module directly, the way app.js does on every poll. */
async function renderTimeline(page, stateObj, waitMs) {
  await page.evaluate((st) => {
    window.EdennTimeline.render({ session_id: "spec_unhappy", messages: [], state: st });
  }, stateObj);
  await new Promise((r) => setTimeout(r, waitMs || 500));
}

const readTimeline = (page) => page.evaluate(() => {
  const all = (s) => Array.from(document.querySelectorAll(s));
  const q = (s) => document.querySelector(s);
  return {
    note: (q(".tl-tl__note") || {}).textContent || "",
    ticks: all(".tl-tick").map((n) => n.textContent),
    lanes: ["music", "voiceover", "sfx"].reduce((acc, id) => {
      const row = q(".tl-row.lane-" + id);
      acc[id] = {
        hint: row ? ((row.querySelector(".tl-lane__hint") || {}).textContent || "") : "NO ROW",
        clips: row ? Array.from(row.querySelectorAll(".tl-clip")).map((c) => ({
          name: (c.querySelector(".tl-clip__nm") || {}).textContent || "",
          time: (c.querySelector(".tl-clip__t") || {}).textContent || "",
          pending: c.classList.contains("is-pending"),
          left: c.style.left, width: c.style.width,
          draggable: c.draggable === true,
        })) : [],
      };
      return acc;
    }, {}),
  };
});

module.exports = {
  name: "unhappy paths",
  backend: "mock",
  persona: "Unhappy Tester",

  async run(page, t, { H, backend }) {
    // ======================================================================
    // 1 · the entrance, before anything exists
    // ======================================================================

    await t.step("gallery: nothing finished yet — the shelf explains itself", async () => {
      await H.click(page, "#nav-gallery");
      await H.sleep(400);
      const r = await page.evaluate(() => ({
        cards: document.querySelectorAll("#gallery-grid .gal-card").length,
        empty: (document.querySelector("#gallery-grid .cards-empty") || {}).textContent || "",
        note: document.getElementById("gallery-note").hidden ? ""
          : document.getElementById("gallery-note").textContent.trim(),
      }));
      t.eq(r.cards, 0, "no mixes invented for an empty account");
      t.ok(r.empty.trim().length > 0, "the empty shelf says why it is empty", r.empty.slice(0, 60));
      return r.note ? `+ note: ${r.note.slice(0, 50)}` : "explained";
    });

    await t.step("sessions: an empty list explains itself AND admits the mock is not durable", async () => {
      await H.click(page, "#nav-sessions");
      await H.sleep(400);
      const r = await page.evaluate(() => ({
        empty: (document.querySelector("#sessions-grid .cards-empty") || {}).textContent || "",
        note: document.getElementById("sessions-note").hidden ? ""
          : document.getElementById("sessions-note").textContent.trim(),
      }));
      t.ok(r.empty.trim().length > 0, "the empty list says what to do", r.empty.slice(0, 60));
      // Honesty marker: on the offline transport the history is per-tab.
      t.ok(/offline|this page|not durable|demo/i.test(r.note),
        "the offline transport admits its sessions are not durable", r.note || "NO NOTE");
      return r.note.slice(0, 60) || "no note";
    });

    await t.step("attach: the offline mock admits it analyzes a sample, not your file", async () => {
      await H.click(page, "#nav-new");
      const meta = await H.attachVideo(page, fixtureFile());
      // The file is 2KB of filler with no video stream in it at all.
      t.ok(/offline|sample|demo/i.test(meta),
        "the attach chip marks the offline stand-in", meta);
      // The chip has to name what it accepted, or the marker is unattached to
      // anything the user can identify.
      const named = await page.$eval("#attach-name", (n) => n.textContent.trim());
      t.ok(named === path.basename(fixtureFile()), "the chip names the file that was accepted", named);
      // Recorded, not asserted: the duration beside it is a constant the mock
      // prints for any file (MockTransport.uploadVideo, app.js:120-122).
      return `${meta} (name: ${named})`;
    });

    await t.step("attach: removing the attachment really clears it", async () => {
      await H.click(page, "#attach-remove");
      const r = await page.evaluate(() => ({
        on: document.getElementById("attach-chip").classList.contains("is-on"),
        id: window.__edenn.app.sourceArtifactId,
        attached: window.__edenn.app.attached,
      }));
      t.ok(!r.on, "the chip is gone");
      t.ok(!r.id && !r.attached, "no orphan source id left behind", JSON.stringify(r));
      return "cleared";
    });

    // ======================================================================
    // 2 · starting with nothing: no video, no direction
    // ======================================================================

    await t.step("start: no video and an empty direction opens without inventing intent", async () => {
      const typed = await page.$eval("#start-text", (n) => n.value);
      t.eq(typed, "", "starting from a genuinely empty composer");
      const sid = await startBlank(page, H);
      t.must(!!sid, "a session opened", sid || "none");
      await watchOpens(page);

      const snap = await H.snapshot(page);
      const st = snap.state || {};
      // Nothing may be generated off an empty brief — that would be spending on
      // a guess.
      t.eq((st.candidates || []).length, 0, "nothing generated from an empty direction");
      t.eq((st.proposals || []).length, 0, "no directions invented before the user is asked");
      t.ok(!st.approved_direction, "nothing approved on the user's behalf");
      // …and it must ASK rather than assume.
      t.ok(!!st.pending_clarification, "the console asks what the user wants",
        (st.pending_clarification || {}).gate || "no question");
      t.ok(await H.exists(page, ".lpick"), "the question is on screen as the layer picker");
      const name = await page.$eval("#session-name", (n) => n.textContent.trim());
      t.ok(name.length > 0, "the session is named without a direction to name it after", name);
      return `${sid} · asked: ${(st.pending_clarification || {}).question || "-"}`;
    });

    await t.step("start: the session view says which backend it is on", async () => {
      const pill = await page.evaluate(() => {
        const p = document.getElementById("session-conn");
        return { text: p.textContent.trim(), cls: p.className, title: p.title };
      });
      // The mock fabricates a whole observation (title, scenes, duration) for a
      // session with NO file attached; the pill is the only marker that none of
      // it was measured, so it has to name the transport.
      t.ok(/mock|offline/i.test(pill.text),
        "on the offline transport the pill says so", pill.text);
      const pills = await page.evaluate(() =>
        Array.from(document.querySelectorAll(".pills .pill")).map((n) => n.innerText.trim()));
      return `${pill.text} · observation pills: ${pills.join(" / ") || "none"}`;
    });

    // ======================================================================
    // 3 · the intent gate: a multi-select that has to survive the round trip
    // ======================================================================

    await t.step("gate: the answer the console SENDS names every layer the user picked", async () => {
      // Record the outgoing frame — the layer picker is a 3-way multi-select but
      // the wire format underneath it is a single option id.
      await page.evaluate(() => {
        const conn = window.__edenn.app.conn;
        window.__frames = [];
        const orig = conn.choose.bind(conn);
        conn.choose = (f) => { window.__frames.push(JSON.parse(JSON.stringify(f))); return orig(f); };
      });
      // Music + sound effects, no voice-over.
      await H.click(page, ".lpick__row.lane-voiceover");
      const picked = await page.$$eval(".lpick__row",
        (ns) => ns.filter((n) => n.classList.contains("is-on"))
          .map((n) => (n.className.match(/lane-(\w+)/) || [])[1]));
      t.eq(picked.sort(), ["music", "sfx"], "the user's selection is music + sound effects");

      await H.click(page, ".w-go button");
      await H.waitForIdle(page);
      const frame = await page.evaluate(() => (window.__frames || [])[0] || null);
      t.must(!!frame, "the choice was sent");
      const id = String(frame.target_id || "");
      // The id is the only field the REAL backend reads (INTENT_GATE_PLAN keys
      // off target_id; payload.layers has no reader) — so it has to carry the
      // whole answer, or a layer the user asked for is dropped on the floor.
      t.ok(/sfx|sound|full/i.test(id),
        "the sent choice id still contains the sound-effects layer",
        `target_id=${id} payload.layers=${JSON.stringify((frame.payload || {}).layers)}`);

      // And the bubble the user sees must be the answer that was recorded.
      const snap = await H.snapshot(page);
      const userMsgs = (snap.messages || []).filter((m) => m.role === "user");
      const recorded = (userMsgs[userMsgs.length - 1] || {}).content || "";
      const shown = await page.evaluate(() => {
        const rows = Array.from(document.querySelectorAll("#thread-inner .user-row"));
        const n = rows[rows.length - 1];
        return n ? n.innerText.trim() : "";
      });
      t.ok(shown.toLowerCase() === recorded.toLowerCase(),
        "the answer on screen is the answer the session recorded",
        `screen "${shown}" vs recorded "${recorded}"`);
      return `sent ${id}`;
    });

    await t.step("gate: answering it generates nothing by itself", async () => {
      const st = await H.state(page);
      t.eq((st.candidates || []).length, 0, "still nothing generated after the plan is set");
      t.ok((st.proposals || []).length >= 2, "directions were proposed to choose from",
        String((st.proposals || []).length));
      t.ok(await H.waitFor(page, ".direction-options", H.TURN_MS), "the direction choices are on screen");
      return `${(st.proposals || []).length} directions, 0 takes`;
    });

    // ======================================================================
    // 4 · the composer: empty, whitespace, and far too long
    // ======================================================================

    await t.step("composer: an empty send is refused up front or explained — never swallowed", async () => {
      const before = await feedback(page);
      const beforeMsgs = ((await H.snapshot(page)).messages || []).length;
      await page.$eval("#session-input", (n) => { n.value = ""; });
      const u = await H.usable(page, "#session-send");
      let clicked = false;
      if (u.ok) { await H.click(page, "#session-send"); clicked = true; await H.sleep(700); }
      const after = await feedback(page);
      const afterMsgs = ((await H.snapshot(page)).messages || []).length;

      t.eq(afterMsgs, beforeMsgs, "an empty send records no message");
      t.eq(after.bubbles, before.bubbles, "no phantom bubble in the thread");
      t.ok(!after.thinking, "no thinking block left spinning on a turn that never happened");
      // The bar: either the control is visibly unavailable while there is nothing
      // to send, or pressing it says why nothing happened.
      const explained = !u.ok || (after.toast && after.toast !== before.toast);
      t.ok(explained, "an ignored send is visibly refused, not silently dropped",
        clicked ? `send was enabled and produced no feedback (toast="${after.toast}")` : `send disabled: ${u.why}`);
      return clicked ? "clicked an enabled, inert Send" : `Send unavailable (${u.why})`;
    });

    await t.step("composer: a whitespace-only send behaves the same as an empty one", async () => {
      const beforeMsgs = ((await H.snapshot(page)).messages || []).length;
      await page.$eval("#session-input", (n) => { n.value = "   \t  "; });
      await H.click(page, "#session-send");
      await H.sleep(700);
      const afterMsgs = ((await H.snapshot(page)).messages || []).length;
      const leftover = await page.$eval("#session-input", (n) => n.value);
      t.eq(afterMsgs, beforeMsgs, "whitespace is not a turn");
      t.ok(!(await page.$(".think.is-live")), "no open thinking block");
      // The typed whitespace should not silently vanish either — if the console
      // did nothing, the user's text should still be there to fix.
      return leftover.length ? "input preserved" : "input cleared without sending";
    });

    /** Widest overspill of a chat bubble past the pane it lives in. */
    const spill = (page) => page.evaluate(() => {
      const pane = document.getElementById("chatpane");
      const right = pane.getBoundingClientRect().right;
      const worst = Array.from(pane.querySelectorAll(".ub, .atext"))
        .map((n) => Math.round(n.getBoundingClientRect().right - right))
        .reduce((a, b) => Math.max(a, b), -9999);
      const inner = document.getElementById("thread-inner");
      return { worst, scroll: inner.scrollWidth - inner.clientWidth };
    });

    await t.step("composer: a few thousand characters are accepted whole and the pane survives", async () => {
      const LINE = "The cut around eleven seconds should feel like the room got smaller, and the low end must not swallow the dialogue. ";
      const LONG = LINE.repeat(36);   // ~4.2k chars of ordinary prose
      t.ok(LONG.length > 3000, "the paste is genuinely long", `${LONG.length} chars`);
      await page.$eval("#session-input", (n, v) => { n.value = v; }, LONG);
      await H.click(page, "#session-send", { settle: 150 });
      await H.waitForIdle(page);

      const shown = await page.evaluate(() => {
        const rows = Array.from(document.querySelectorAll("#thread-inner .user-row"));
        const n = rows[rows.length - 1];
        return n ? n.innerText.trim().length : 0;
      });
      t.ok(shown >= LONG.length - 40,
        "the whole message is shown, not silently truncated", `${shown} of ${LONG.length} chars rendered`);
      const s = await spill(page);
      t.ok(s.scroll <= 2, "the thread does not scroll sideways", `${s.scroll}px overflow`);
      t.ok(s.worst <= 2, "no bubble spills out of the chat pane", `${s.worst}px past the pane edge`);
      const u = await H.usable(page, "#session-input");
      t.ok(u.ok, "the composer is live again afterwards", u.why || "ok");
      // The server contract is 8000 chars (models.MAX_MESSAGE_CHARS) on the REST
      // path; the composer offers no counter, no maxlength and no clamp, and it
      // paints the user's bubble BEFORE the send resolves.
      const guard = await page.$eval("#session-input", (n) => n.getAttribute("maxlength"));
      return `accepted ${LONG.length} chars; maxlength=${guard || "none"}`;
    });

    await t.step("composer: one unbroken run (a pasted link) must not push the pane sideways", async () => {
      // Exactly what this product hands people back: a long signed media URL.
      const URLISH = "https://media.storage.example.invalid/renders/session-"
        + "b8f21c4e9a7d43f0/final_mix_take3_h264." + "x".repeat(180) + ".mp4?sig=" + "A9b7".repeat(30);
      await page.$eval("#session-input", (n, v) => { n.value = "Is this still valid? " + v; }, URLISH);
      await H.click(page, "#session-send", { settle: 150 });
      await H.waitForIdle(page);
      const s = await spill(page);
      const bub = await page.evaluate(() => {
        const ubs = document.querySelectorAll("#thread-inner .ub");
        const n = ubs[ubs.length - 1];
        return n ? { box: Math.round(n.getBoundingClientRect().width), content: n.scrollWidth } : null;
      });
      t.ok(s.worst <= 2, "the bubble stays inside the chat pane", `${s.worst}px past the pane edge`);
      t.ok(bub && bub.content <= bub.box + 4,
        "the text wraps inside its bubble instead of running off the end",
        bub ? `${bub.content}px of text in a ${bub.box}px bubble` : "no bubble");
      t.ok(s.scroll <= 2, "the thread does not scroll sideways", `${s.scroll}px overflow`);
      return `${URLISH.length}-char token · thread scrolls ${s.scroll}px`;
    });

    // ======================================================================
    // 5 · the spend gate — the billing-integrity check
    // ======================================================================

    await t.step("spend: the confirm states the cost before anything is spent", async () => {
      t.must(await H.waitFor(page, ".direction-option .dt-use", 20000), "a direction offers a Use action");
      await H.click(page, ".direction-option .dt-use");
      await H.sleep(500);
      const dlg = await page.evaluate(() => {
        const o = document.getElementById("confirm-overlay");
        return {
          open: !!o && !o.hidden,
          title: (document.getElementById("confirm-title") || {}).textContent || "",
          body: (document.getElementById("confirm-body") || {}).textContent || "",
        };
      });
      t.must(dlg.open, "generation is gated behind a confirm");
      t.ok(/spend|cost|generat/i.test(dlg.body + dlg.title),
        "the dialog names the spend", (dlg.title + " — " + dlg.body).slice(0, 90));
      return dlg.body.slice(0, 70);
    });

    await t.step("spend: CANCEL spends nothing — no take, no job, no approval", async () => {
      const before = await H.snapshot(page);
      const body = await H.approveSpend(page, { cancel: true });
      t.ok(body !== null, "the cancel button answered the dialog");
      await H.sleep(1800);   // a late dispatch would land inside this window
      const after = await H.snapshot(page);
      const st = after.state || {};

      t.eq((st.candidates || []).length, 0, "no take was generated");
      t.ok(!st.approved_direction, "no direction was approved",
        String(st.approved_direction));
      t.ok(!st.selected_proposal_id, "no proposal was selected", st.selected_proposal_id || "none");
      t.eq((after.linked_job_ids || []).length, 0, "no job was queued");
      t.eq((after.choices || []).length, (before.choices || []).length,
        "no choice frame reached the backend");
      return "cancelled clean";
    });

    await t.step("spend: after cancelling, the console is fully usable again", async () => {
      const overlay = await page.$eval("#confirm-overlay", (n) => n.hidden);
      t.ok(overlay, "the modal is gone");
      const retry = await H.usable(page, ".direction-option .dt-use");
      t.ok(retry.ok, "the direction can still be chosen — cancel is not a dead end", retry.why || "ok");
      const composer = await H.usable(page, "#session-input");
      t.ok(composer.ok, "the composer is usable", composer.why || "ok");
      t.ok(!(await H.exists(page, ".think.is-live")), "no thinking block left open by the cancel");
      return "usable";
    });

    // ======================================================================
    // 6 · export with nothing to export
    // ======================================================================

    await t.step("export: with no mix, Export explains instead of opening nothing", async () => {
      const st = await H.state(page);
      const deliverable = (st.final_artifact || {}).video_url || (st.mix || {}).video_url || null;
      t.ok(!deliverable, "precondition: there is genuinely nothing rendered", String(deliverable));
      await H.click(page, "#tb-export");
      await H.sleep(600);
      const fb = await feedback(page);
      const opened = await opens(page);
      t.ok(fb.toast.length > 0, "Export says why it cannot export", fb.toast || "SILENT NO-OP");
      t.eq(opened.length, 0, "no blank tab was opened at the user", JSON.stringify(opened));
      return fb.toast.slice(0, 70);
    });

    // ======================================================================
    // 7 · asking for three things the product cannot do
    // ======================================================================

    const replies = {};

    await t.step("refusal: asking to clone the user's own voice must not be promised", async () => {
      await H.say(page, "Use my own voice for the narration — clone it from the video I gave you.");
      const reply = await lastReply(page);
      replies.clone = reply;
      t.ok(!promises(reply, /\bclon(e|ing)|my own voice|your own voice\b/i),
        "the reply does not promise voice cloning", reply.slice(0, 120) || "(no reply)");
      const st = await H.state(page);
      t.ok(!((st.layers || {}).voiceover || {}).voice_clone_id,
        "no clone was recorded in the session state");
      return reply.slice(0, 80) || "(no reply)";
    });

    await t.step("refusal: the voice list offers presets only — no clone, no upload", async () => {
      // Reachable state: a drafted script (propose_script is free and writes
      // layers.voiceover with status "draft").
      const ok = await inject(page, {
        layers: Object.assign({}, (await H.state(page)).layers || {}, {
          voiceover: { script: "Some moments do not need a stage at all.", status: "draft", voice_id: "warm_female", tone: "" },
        }),
      });
      t.must(ok, "voice-over card state injected");
      t.must(await H.waitFor(page, ".vo select", 6000), "the voice picker rendered");
      const opts = await page.$$eval(".vo select option",
        (ns) => ns.map((n) => ({ v: n.value, label: n.textContent.trim() })));
      t.eq(opts.length, 5, "exactly the five supported presets are offered");
      const invented = opts.filter((o) => /clone|upload|custom|record your|your voice/i.test(o.label + o.v));
      t.ok(!invented.length, "no option offers a voice the backend cannot make",
        invented.map((o) => o.label).join(",") || "clean");
      return opts.map((o) => o.v).join(" / ");
    });

    await t.step("refusal: an empty script is refused before the spend gate, not after", async () => {
      await page.$eval(".vo .vo__script", (n) => { n.value = "   "; });
      await H.click(page, ".vo .vo__gen");
      await H.sleep(500);
      const r = await page.evaluate(() => ({
        confirm: !document.getElementById("confirm-overlay").hidden,
        toast: document.getElementById("toast").hidden ? "" : document.getElementById("toast").textContent.trim(),
      }));
      t.ok(!r.confirm, "an empty script never reaches the spend dialog",
        r.confirm ? "CONFIRM OPENED ON AN EMPTY SCRIPT" : "gated");
      t.ok(r.toast.length > 0, "the refusal is visible", r.toast || "SILENT");
      return r.toast.slice(0, 70);
    });

    await t.step("refusal: asking to drag a clip on the timeline must not be promised", async () => {
      await H.say(page, "Drag the music clip two seconds later on the timeline so it starts after the first cut.");
      const reply = await lastReply(page);
      replies.drag = reply;
      t.ok(!promises(reply, /\bdrag|move (the|that|it)|re-?time|shift the (clip|music)\b/i),
        "the reply does not promise timeline editing", reply.slice(0, 120) || "(no reply)");
      return reply.slice(0, 80) || "(no reply)";
    });

    await t.step("refusal: the timeline offers no drag affordance, and a drag changes nothing", async () => {
      // Spotted effects give real clips without needing a probe-able render.
      await renderTimeline(page, {
        observation: { duration_s: 16, video_title: "reel", scenes: [] },
        candidates: [],
        layers: { sfx: { events: [{ id: "e1", start_s: 4, label: "Whoosh" }, { id: "e2", start_s: 9, label: "Impact" }] } },
      });
      const before = await readTimeline(page);
      t.must(before.lanes.sfx.clips.length >= 2, "clips to try to drag");
      t.ok(!before.lanes.sfx.clips.some((c) => c.draggable),
        "no clip advertises itself as draggable");

      const box = await page.$eval(".tl-row.lane-sfx .tl-clip", (n) => {
        const r = n.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
      });
      await page.mouse.move(box.x, box.y);
      await page.mouse.down();
      await page.mouse.move(box.x + 120, box.y, { steps: 10 });
      await page.mouse.up();
      await H.sleep(400);
      const after = await readTimeline(page);
      t.eq(after.lanes.sfx.clips.map((c) => c.left), before.lanes.sfx.clips.map((c) => c.left),
        "dragging does not move a clip — the view never implies an edit it cannot make");
      return "no drag affordance";
    });

    await t.step("refusal: asking to cancel a running render must not be promised", async () => {
      await H.say(page, "Cancel the render that is running right now — stop it before it costs anything.");
      const reply = await lastReply(page);
      replies.cancel = reply;
      t.ok(!promises(reply, /\bcancel|stopp?(ed|ing)?|abort|kill(ed)? (it|the)\b/i),
        "the reply does not promise a cancel", reply.slice(0, 120) || "(no reply)");
      const snap = await H.snapshot(page);
      t.ok(snap.status !== "canceled", "nothing was marked cancelled", snap.status);
      // There is no cancel endpoint at all, so there must be no control offering one.
      const controls = await page.evaluate(() => Array.from(
        document.querySelectorAll("#session button"))
        .filter((b) => b.offsetParent !== null && /\bcancel|\bstop\b|abort/i.test(b.innerText))
        .map((b) => b.innerText.trim()));
      t.ok(!controls.length, "no control in the session offers to cancel a render",
        controls.join(" | ") || "none");
      return reply.slice(0, 80) || "(no reply)";
    });

    await t.step("refusal: three impossible asks get three different answers", async () => {
      const set = new Set([replies.clone, replies.drag, replies.cancel].map((s) => (s || "").trim()));
      // One canned sentence for every impossible request means the console is
      // not answering the question at all — the user's ask went nowhere and the
      // acknowledgement reads as acceptance.
      t.ok(set.size === 3, "each impossible ask is answered on its own terms",
        set.size === 1 ? `ONE canned reply for all three: "${replies.clone.slice(0, 90)}"`
          : `${set.size} distinct replies`);
      return backend === "mock"
        ? "offline backend — routing prose to a real refusal is the live director's job"
        : "live director";
    });

    // ======================================================================
    // 8 · the timeline must never invent time
    // ======================================================================

    await t.step("timeline: nothing generated means empty lanes, not placeholder clips", async () => {
      // Back to the session's real state.
      await page.evaluate(() => {
        const app = window.__edenn.app;
        if (app.snapshot) window.EdennTimeline.render(app.snapshot);
      });
      await H.sleep(600);
      const r = await readTimeline(page);
      t.eq(r.lanes.music.clips.length, 0, "no music clip before a take exists");
      t.ok(r.lanes.music.hint.length > 0, "the empty lane says what would fill it", r.lanes.music.hint);
      const st = await H.state(page);
      const dur = Number((st.observation || {}).duration_s || 0);
      // Ticks are only legitimate over a measured clock.
      t.ok(!r.ticks.length || dur > 0, "no time axis without a measured duration",
        `${r.ticks.length} ticks, duration=${dur || "unknown"}`);
      if (dur > 0 && r.ticks.length) {
        t.ok(r.ticks[r.ticks.length - 1].indexOf(String(Math.round(dur))) >= 0,
          "the axis ends at the measured duration", `${r.ticks.join(" ")} vs ${dur}s`);
      }
      return `${r.ticks.length} ticks over ${dur || "?"}s`;
    });

    await t.step("timeline: with no duration, nothing on the clock claims an interval", async () => {
      // Reachable: a plan drafted while the analysis produced no duration
      // (dev metadata-only observation, or a failed probe).
      await renderTimeline(page, {
        observation: { video_title: "unmeasured", scenes: [] },
        candidates: [{ candidate_id: "f1", title: "Take 1", status: "failed", audio_url: null }],
        layers: { sfx: { ambience: "Room tone under the whole cut", events: [] } },
      }, 900);
      const r = await readTimeline(page);
      t.ok(!r.ticks.length, "no fabricated tick axis", r.ticks.join(",") || "none");
      t.ok(/waiting on the video/i.test(r.note), "the header admits it has no clock", r.note);

      const take = r.lanes.music.clips[0];
      t.must(!!take, "the failed take is still drawn");
      t.ok(!/\d:\d\d–/.test(take.time), "a failed take shows its status, not an interval", take.time);
      t.ok(/fail/i.test(take.time), "and the status it shows is the real one", take.time);

      const bed = r.lanes.sfx.clips.find((c) => /ambience/i.test(c.name));
      t.must(!!bed, "the ambience bed is drawn");
      t.ok(!/\d:\d\d–\d:\d\d/.test(bed.time),
        "a bed with no known length does not print an interval it never measured", bed.time);
      return `bed reads "${bed.time}"`;
    });

    // ======================================================================
    // 9 · claiming a deliverable that does not exist
    // ======================================================================

    await t.step("final: a queued compose must not be announced as a finished mix", async () => {
      // The exact shape the backend writes while the compose waits on its job:
      // final_artifact exists, status queued, no media anywhere.
      const ok = await inject(page, {
        final_artifact: {
          status: "queued", video_url: null, audio_url: null,
          message: "Final compose is waiting for the linked video-music job to complete.",
        },
      });
      t.must(ok, "queued-final state injected");
      t.must(await H.waitFor(page, ".final", 6000), "the final card rendered");
      const card = await page.evaluate(() => {
        const c = document.querySelector(".final");
        return {
          head: (c.querySelector(".final__hd-title") || {}).textContent || "",
          badge: (c.querySelector(".final__hd-badge") || {}).textContent || "",
          text: c.innerText.trim(),
          media: !!c.querySelector("video, audio") || !!c.querySelector(".gplay"),
          btn: (c.querySelector(".final__btn") || {}).textContent || "",
        };
      });
      const flat = card.text.replace(/\s*\n\s*/g, " · ");
      t.ok(!/ready|done/i.test(card.head + card.badge),
        "the card does not announce a mix that has not rendered",
        `"${card.head}" / "${card.badge}" with media=${card.media}`);
      t.ok(/wait|queue|pending|composing/i.test(flat),
        "the card says what it is actually waiting for", flat.slice(0, 90));
      return flat.slice(0, 90);
    });

    await t.step("final: a Download button with nothing behind it must not be offered", async () => {
      const has = await H.exists(page, ".final .final__btn");
      if (!has) return "no download control offered — correct";
      const u = await H.usable(page, ".final .final__btn");
      t.ok(u.ok, "the offered button is clickable", u.why || "ok");
      await page.evaluate(() => { window.__opened = []; });
      await H.click(page, ".final .final__btn");
      await H.sleep(600);
      const fb = await feedback(page);
      const opened = await opens(page);
      t.ok(opened.length > 0 || fb.toast.length > 0,
        "a rendered Download either downloads or explains why it cannot",
        `opened=${JSON.stringify(opened)} toast="${fb.toast}"`);
      return `clicked; opened=${opened.length} toast="${fb.toast.slice(0, 40)}"`;
    });

    await t.step("export: the docs promise a download — check what Export actually does", async () => {
      const ok = await inject(page, {
        final_artifact: { status: "completed", video_url: "http://127.0.0.1:9/final_mix.mp4", audio_url: null },
      });
      t.must(ok, "a rendered deliverable injected");
      await page.evaluate(() => { window.__opened = []; });
      await H.click(page, "#tb-export");
      await H.sleep(500);
      const opened = await opens(page);
      t.ok(opened.length === 1, "Export acts on the rendered deliverable", JSON.stringify(opened));
      // index.html's own guide says "Export downloads the final mix on your
      // video." A new tab is not a download: no filename, no save, and a media
      // URL simply plays inline.
      const delivers = await page.evaluate(() => {
        const a = document.querySelector("a[download]");
        return { anchor: !!a, opened: (window.__opened || []).length };
      });
      const promise = await page.evaluate(() => {
        const d = document.querySelector(".docs-body");
        const m = (d ? d.textContent : "").match(/[^.]*Export[^.]*\./);
        return m ? m[0].trim() : "";
      });
      t.ok(!/download/i.test(promise) || delivers.anchor,
        "Export delivers what the in-app guide says it delivers",
        `guide: "${promise}" — but it window.open()s ${JSON.stringify(opened)} with no download anchor`);
      return `opened ${opened.length} tab(s)`;
    });

    // ======================================================================
    // 10 · the gallery and the sessions list, with an unfinished session
    // ======================================================================

    await t.step("gallery: an unfinished session is not displayed as a finished mix", async () => {
      await H.click(page, "#tb-home");
      await H.sleep(800);
      await H.click(page, "#nav-sessions");
      await H.sleep(500);
      const sessions = await page.$$eval("#sessions-grid .sess-card", (ns) => ns.length);
      t.ok(sessions >= 1, "the unfinished session is listed under My sessions", String(sessions));
      await H.click(page, "#nav-gallery");
      await H.sleep(500);
      const g = await page.evaluate(() => ({
        cards: document.querySelectorAll("#gallery-grid .gal-card").length,
        empty: (document.querySelector("#gallery-grid .cards-empty") || {}).textContent || "",
      }));
      t.eq(g.cards, 0, "no gallery card for a session that never rendered anything");
      t.ok(g.empty.trim().length > 0, "and the shelf still explains itself", g.empty.slice(0, 60));
      return `${sessions} session(s), 0 gallery cards`;
    });

    // ======================================================================
    // 11 · pointing the console at a backend that is not there
    // ======================================================================

    await t.step("settings: switching to a live backend on a dead port reloads onto it", async () => {
      // These checks live on the ENTRANCE. Earlier steps left us inside a
      // session, where the sidebar is hidden — go home first, or every click
      // below fails on "inside [hidden]" and reports a navigation problem as a
      // backend-honesty problem.
      if (await page.$eval("#session", (n) => !n.hidden)) {
        await H.click(page, "#tb-home", { settle: 800 });
      }
      await H.click(page, "#side-settings");
      t.must(await page.$eval("#settings-overlay", (n) => !n.hidden), "settings opened");
      await H.type(page, "#set-api", "http://127.0.0.1:9");
      await H.click(page, '#set-backend button[data-b="real"]');
      const nav = page.waitForNavigation({ waitUntil: "networkidle2", timeout: 30000 }).catch(() => null);
      await H.click(page, "#settings-save", { settle: false }).catch(() => null);
      await nav;
      await page.waitForFunction(() => !!window.__edenn, { timeout: 30000 });
      const where = await page.evaluate(() => ({
        search: location.search,
        kind: window.__edenn.app.transport.kind,
      }));
      t.ok(/backend=real/.test(where.search), "the URL carries the live backend", where.search);
      t.eq(where.kind, "real", "the app is on the live transport");
      return where.search;
    });

    await t.step("dead backend: the pill reports the transport, and claims to be connected", async () => {
      const pill = await page.evaluate(() => {
        const p = document.getElementById("entrance-conn");
        return { text: p.textContent.trim(), cls: p.className, title: p.title };
      });
      // Nothing has been reached at this point — no request has even been made.
      t.ok(!/connected/i.test(pill.title),
        "the pill does not claim a connection it has never made", `title="${pill.title}"`);
      return `${pill.text} · ${pill.cls} · ${pill.title.slice(0, 60)}`;
    });

    await t.step("dead backend: a failed list says so where the user is looking", async () => {
      await H.click(page, "#nav-sessions");
      await H.sleep(2500);
      const note = await page.evaluate(() => {
        const n = document.getElementById("sessions-note");
        return { hidden: n.hidden, text: n.textContent.trim() };
      });
      t.ok(!note.hidden && note.text.length > 0,
        "the sessions pane names the failure", note.text || "SILENT EMPTY LIST");
      t.ok(!/nothing here yet/i.test(
        await page.$eval("#sessions-grid", (n) => n.innerText)),
        "an unreachable backend is not drawn as an empty account",
        await page.$eval("#sessions-grid", (n) => n.innerText.slice(0, 60)));
      return note.text.slice(0, 70);
    });

    await t.step("dead backend: after every request has failed, the pill still says the same thing", async () => {
      const pill = await page.evaluate(() => {
        const p = document.getElementById("entrance-conn");
        return { text: p.textContent.trim(), cls: p.className };
      });
      // The class .conn.idle exists in the stylesheet and no code path applies it;
      // onOpen/onClose are empty. There is no connection state anywhere in the UI.
      t.ok(/unreachable|offline|no connection|error|retry/i.test(pill.text) || /idle|down|err/.test(pill.cls),
        "the connection indicator reflects that the backend is unreachable",
        `reads "${pill.text}" (class "${pill.cls}") after a failed request`);
      return `${pill.text} / ${pill.cls}`;
    });

    await t.step("dead backend: an upload that cannot land says so on the chip", async () => {
      await H.click(page, "#nav-new");
      const meta = await H.attachVideo(page, fixtureFile()).catch((e) => "THREW: " + e.message);
      t.ok(/fail|error|could not/i.test(meta), "the attach chip reports the failure", meta);
      const fb = await feedback(page);
      t.ok(fb.toast.length > 0, "and the failure is toasted with a reason", fb.toast || "SILENT");
      return `${meta} · ${fb.toast.slice(0, 60)}`;
    });

    await t.step("dead backend: starting without a real source refuses instead of faking one", async () => {
      await page.$eval("#start-text", (n) => { n.value = "Cinematic and premium."; });
      await H.click(page, "#start-btn", { settle: 400 });
      // Wait for the refusal rather than for a session that must not appear.
      await page.waitForFunction(() => {
        const tt = document.getElementById("toast");
        return tt && !tt.hidden && /attach|video/i.test(tt.textContent);
      }, { timeout: 8000 }).catch(() => null);
      const r = await page.evaluate(() => ({
        entrance: !document.getElementById("entrance").hidden,
        session: !document.getElementById("session").hidden,
        toast: document.getElementById("toast").hidden ? "" : document.getElementById("toast").textContent.trim(),
        startDisabled: document.getElementById("start-btn").disabled,
      }));
      t.ok(r.entrance && !r.session, "no session was opened on a backend that cannot take one",
        JSON.stringify(r));
      t.ok(r.toast.length > 0, "the refusal is stated", r.toast || "SILENT");
      t.ok(!r.startDisabled, "the Start button is left usable for the retry");
      return r.toast.slice(0, 70);
    });
  },
};
