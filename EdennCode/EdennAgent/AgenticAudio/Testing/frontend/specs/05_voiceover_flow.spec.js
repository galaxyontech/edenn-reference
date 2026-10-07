/* The voice-over journey: gate → draft → voice/tone → record → lane → re-read.
 *
 * Narration is the one layer this product is opinionated about: it speaks only
 * where speech earns its place, and it never speaks without being asked. So the
 * checks here fall into two halves. The first half is the ordinary "is this
 * control wired to the thing it names" sweep over every control on the voice-over
 * card. The second half asks the harder question — does the card tell the truth
 * about what the backend was given, and does it show the restraint decisions the
 * backend actually made?
 *
 * Two drive modes, deliberately:
 *
 *   live mock  — the gate, the spend confirm, the generate turn, the recorded
 *                take row and the timeline lane all run through the real
 *                mock-backend session. That is where "did it change STATE" is
 *                answerable.
 *   synthesized snapshot — the DRAFT state (script proposed, nothing recorded)
 *                and the backend-computed restraint fields (segments, collisions,
 *                held_silent, alignment) are produced by the live director's
 *                propose_script. The offline mock has no path from prose to a
 *                drafted script, so those states are fed through the app's own
 *                public event entry point, window.__edenn.onEvent — the exact
 *                call the transport makes on every snapshot. The rendering path
 *                under test is the real one; only the sender is ours.
 *
 * Anything that genuinely needs the live director is called out in the check
 * detail rather than dressed up as a pass. */
"use strict";

/** The five presets the product supports. There is no sixth, and no cloning. */
const VOICE_IDS = ["warm_female", "bright_female", "calm_male", "narrator_male", "neutral"];

/** A drafted, video-informed narration plan — the shape propose_script writes. */
const DRAFT = {
  status: "draft",
  script: "Some moments do not need a stage. You feel them before anyone speaks. Held, then gone.",
  voice_id: "calm_male",
  tone: "hushed",
  voice_rationale: "Slow, elegant footage — a low, intimate narrator sits under the visuals.",
  segments: [
    { id: "seg_01", text: "Some moments do not need a stage.", start_s: 2, delivery: "hushed, drawing the listener in" },
    { id: "seg_02", text: "You feel them before anyone speaks.", start_s: 7, delivery: "even, unhurried" },
    { id: "seg_03", text: "Held, then gone.", start_s: 12, delivery: "final, resolute" },
  ],
  // The restraint bookkeeping the backend computes and hands to the client on
  // every drafted script: the moments it chose NOT to narrate, and the lines it
  // knows will land on someone else's moment (tools/impls.py:825-879).
  held_silent: [{ moment_id: "moment_02", t: 7.1, reason: "the reaction plays better unnarrated" }],
  collisions: [{ segment_id: "seg_03", moment_id: "moment_04", moment_t: 12.4, owner: "sfx",
                 what: "impact on the final cut" }],
};

/** Feed a snapshot through the app's own event entry point. */
async function pushState(page, patch) {
  await page.evaluate((p) => {
    const app = window.__edenn.app;
    const snap = JSON.parse(JSON.stringify(app.snapshot));
    snap.state = Object.assign(snap.state || {}, p);
    window.__edenn.onEvent({
      event_type: "session.opened", session_id: snap.session_id, payload: { snapshot: snap },
    });
  }, patch);
  await new Promise((r) => setTimeout(r, 450));
}

/**
 * Rebuild the card from scratch on a given draft.
 *
 * The card is memoized on vo.status (app.js:1416), so pushing the same draft
 * twice does NOT rebuild it — a step that needs a pristine card has to clear the
 * layer and put it back. Doing this explicitly also keeps each generate step
 * independent of what the previous one typed into the textarea.
 *
 * The clearing state must not be "queued": a queued/processing voice-over arms
 * maybePoll() (app.js), which fetches the BACKEND's snapshot 2.5s later and
 * replaces whatever was synthesized — mid-step, mid-keystroke.
 */
async function freshDraft(page, vo) {
  await pushState(page, { layers: { voiceover: null } });
  await pushState(page, { layers: { voiceover: vo } });
  await new Promise((r) => setTimeout(r, 250));
}

/** Re-feed a snapshot captured earlier (returns the app to real backend state). */
async function restoreSnapshot(page, snap) {
  await page.evaluate((s) => {
    window.__edenn.onEvent({ event_type: "session.opened", session_id: s.session_id, payload: { snapshot: s } });
  }, snap);
  await new Promise((r) => setTimeout(r, 700));
}

/** Everything the voice-over card is showing right now. */
const readCard = (page) => page.evaluate(() => {
  const card = document.querySelector(".vo");
  if (!card) return null;
  const q = (s) => card.querySelector(s);
  const all = (s) => Array.from(card.querySelectorAll(s));
  const sel = q(".vo__bar select");
  const ta = q(".vo__script");
  return {
    title: (q(".w-hd__t") || {}).textContent || "",
    text: card.innerText || "",
    rationale: (q(".vo-card__rationale") || {}).textContent || "",
    segs: all(".vo-segs .vo-seg").map((n) => ({
      t: (n.querySelector(".vo-seg__t") || {}).textContent || "",
      text: (n.querySelector(".vo-seg__text") || {}).textContent || "",
      dir: (n.querySelector(".vo-seg__dir") || {}).textContent || "",
      controls: n.querySelectorAll("button,input,select,textarea,[contenteditable]").length,
    })),
    script: ta ? ta.value : null,
    scriptPlaceholder: ta ? ta.placeholder : null,
    voices: sel ? Array.from(sel.options).map((o) => ({ id: o.value, name: o.textContent })) : null,
    voiceValue: sel ? sel.value : null,
    tone: q('.vo__bar input[type="text"]') ? q('.vo__bar input[type="text"]').value : null,
    genLabel: q(".vo__gen") ? q(".vo__gen").innerText.trim() : null,
    hasTake: !!q(".take.lane-voiceover"),
    takeDur: (q(".take.lane-voiceover .take__dur") || {}).textContent || null,
    // Every control a person can touch inside the card, named by what it calls
    // itself — so a failure reports WHICH control appeared, not just a count.
    controls: all("button,input,select,textarea").map((n) => {
      const cls = (n.getAttribute("class") || "").trim().split(/\s+/).filter(Boolean).join(".");
      const lbl = n.getAttribute("aria-label");
      return n.tagName.toLowerCase() + (cls ? "." + cls : "") + (lbl ? `[${lbl}]` : "");
    }),
  };
});

/** The voice-over lane on the timeline, as drawn. */
const voLane = (page) => page.evaluate(() => {
  const row = document.querySelector(".tl-row.lane-voiceover");
  if (!row) return null;
  return {
    hint: (row.querySelector(".tl-lane__hint") || {}).textContent || "",
    clips: Array.from(row.querySelectorAll(".tl-clip")).map((c) => ({
      name: (c.querySelector(".tl-clip__nm") || {}).textContent || "",
      time: (c.querySelector(".tl-clip__t") || {}).textContent || "",
      pending: c.classList.contains("is-pending"),
      moment: c.classList.contains("is-moment"),
      width: c.style.width,
    })),
  };
});

/** Record every structured choice the console sends, so payloads are checkable. */
async function spyChoices(page) {
  await page.evaluate(() => {
    const app = window.__edenn.app;
    if (app.__voSpy) return;
    app.__voSpy = [];
    const orig = app.conn.choose;
    app.conn.choose = (frame) => {
      try { app.__voSpy.push(JSON.parse(JSON.stringify(frame))); } catch (_) {}
      return orig(frame);
    };
  });
}
const sentChoices = (page) => page.evaluate(() => (window.__edenn.app.__voSpy || []).slice());
const clearChoices = (page) => page.evaluate(() => { (window.__edenn.app.__voSpy || []).length = 0; });

module.exports = {
  name: "voiceover flow",
  backend: "mock",
  persona: "Voice Tester",

  async run(page, t, { H }) {
    await H.startSession(page, { text: "Cinematic and premium — and I want narration over it." });
    await spyChoices(page);

    // ---- 1. the gate: voice-over is a layer you opt into --------------------
    await t.step("gate: the layer picker offers voice-over as its own layer", async () => {
      t.must(await H.waitFor(page, ".lpick__row", H.TURN_MS), "the intent gate rendered");
      const vo = await page.$eval(".lpick__row.lane-voiceover", (n) => ({
        on: n.classList.contains("is-on"),
        pressed: n.getAttribute("aria-pressed"),
        label: n.innerText.trim().replace(/\s+/g, " "),
      })).catch(() => null);
      t.must(!!vo, "a voice-over row exists on the picker");
      t.ok(vo.pressed !== null, "the row reports its pressed state", String(vo.pressed));
      t.eq(vo.pressed, String(vo.on), "the ARIA state agrees with the visual one");
      t.ok(/voice|narrat/i.test(vo.label), "the row names the layer", vo.label);
      // The layer says who writes it — narration here is drafted for approval,
      // not recorded on the spot.
      t.ok(/draft|approve/i.test(vo.label), "the row says the narration is drafted for approval", vo.label);
      return vo.label;
    });

    await t.step("gate: starting with voice-over ticked puts it in the production plan", async () => {
      // Music + voice-over, no sound effects — the ordinary narrated-reel ask.
      await H.click(page, ".lpick__row.lane-sfx");
      await H.click(page, ".w-go button");
      await H.waitForIdle(page);
      const plan = (await H.state(page)).production_plan || {};
      t.ok((plan.layers || []).includes("voiceover"),
        "voiceover is in the committed plan", (plan.layers || []).join("+") || "none");
      return `${plan.mode} · ${(plan.layers || []).join("+")}`;
    });

    // ---- 2. restraint: planning narration is not generating it -------------
    await t.step("restraint: a planned voice-over layer generates nothing on its own", async () => {
      const st = await H.state(page);
      const vo = (st.layers || {}).voiceover;
      t.ok(!vo || !vo.audio_url, "no narration has been recorded yet",
        vo ? JSON.stringify(vo).slice(0, 60) : "layers.voiceover is null");
      const spent = (await sentChoices(page)).filter((f) => f.choice_type === "voiceover");
      t.ok(spent.length === 0, "nothing was sent to TTS without being asked", String(spent.length));
      return "plan committed, nothing spent";
    });

    await t.step("restraint: the empty voice-over lane invites, it does not nag", async () => {
      const lane = await voLane(page);
      t.must(!!lane, "the timeline has a voice-over lane");
      t.ok(lane.clips.length === 0, "no clips are drawn before anything is drafted",
        String(lane.clips.length));
      t.ok(lane.hint.trim().length > 0, "the empty lane says what to do next", lane.hint);
      // Coverage-for-its-own-sake would read as "your video has no narration yet",
      // a percentage, or a gap count. An invitation is a next step, not a deficit.
      t.ok(!/\d+\s*%|coverage|gaps?\b|silent stretch/i.test(lane.hint),
        "the hint does not frame silence as a hole to fill", lane.hint);
      return lane.hint;
    });

    // ---- 3. the only in-UI way to ASK for a script -------------------------
    await t.step("the composer offers a voice-over command (the ask-for-a-draft path)", async () => {
      await H.click(page, '#view-seg .vseg__btn[data-view="canvas"]');
      await H.sleep(400);
      await H.type(page, "#session-input", "/");
      await H.sleep(350);
      const items = await page.$$eval("#cv-acmenu .cv-acrow", (ns) => ns.map((n) => n.innerText.trim()));
      t.ok(items.length > 0, "the slash menu opened", items.join(" | ") || "empty");
      const idx = items.findIndex((s) => /voice|narrat/i.test(s));
      t.ok(idx >= 0, "one of the commands is the voice-over one", items.join(" | "));
      if (idx < 0) return false;
      const before = await H.threadText(page);
      await page.$$eval("#cv-acmenu .cv-acrow", (ns, i) => {
        ns[i].dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
      }, idx);
      await H.waitForIdle(page);
      const after = await H.threadText(page);
      const input = await page.$eval("#session-input", (n) => n.value);
      t.eq(input, "", "the command clears the composer instead of sending '/'");
      t.ok(after.length > before.length, "the command actually reached the director");
      const vo = ((await H.state(page)).layers || {}).voiceover;
      // The mock has no prose→propose_script route (mock-backend.js _handleMessage
      // has no narration branch), so a drafted script here needs the live director.
      return vo && vo.script
        ? "the director drafted a script"
        : "asked; a drafted script needs the live director (mock cannot route prose to propose_script)";
    });

    // ---- 4. the card in DRAFT state ---------------------------------------
    await t.step("card: a drafted narration renders script, rationale and a timed plan", async () => {
      await pushState(page, { layers: Object.assign({}, (await H.state(page)).layers, { voiceover: DRAFT }) });
      const c = await readCard(page);
      t.must(!!c, "the voice-over card appeared for a drafted script");
      t.ok(/voice/i.test(c.title), "the card names itself", c.title);
      t.ok(c.rationale.trim().length > 0, "it explains WHY this voice", c.rationale.slice(0, 60));
      t.eq(c.segs.length, DRAFT.segments.length, "one row per planned line");
      t.ok(c.script === DRAFT.script, "the script is shown verbatim",
        (c.script || "").slice(0, 50));
      return `${c.segs.length} lines · ${c.title}`;
    });

    await t.step("card: every planned line carries its own timing and direction", async () => {
      const c = await readCard(page);
      const want = ["0:02", "0:07", "0:12"];
      const got = c.segs.map((s) => s.t.trim());
      t.eq(got, want, "each line is stamped with its own start");
      t.ok(c.segs.every((s) => s.text.trim().length > 0), "each row shows its line");
      t.ok(c.segs.every((s) => s.dir.trim().length > 0),
        "each row shows the delivery direction", c.segs.map((s) => s.dir).join(" / ").slice(0, 70));
      return got.join(" ");
    });

    await t.step("card: the timed plan is READ-ONLY — deliberate absence, and a gap", async () => {
      const c = await readCard(page);
      const editable = c.segs.reduce((n, s) => n + s.controls, 0);
      // Asserted as an absence on purpose: there is no handler on .vo-seg
      // (app.js:1459-1471). A line cannot be re-timed, re-worded or dropped from
      // the plan; the only edit path is the flat textarea, which DESTROYS it.
      t.eq(editable, 0, "no per-line control exists (documented absence)");
      const before = await readCard(page);
      await page.$eval(".vo-segs .vo-seg", (n) => n.click());
      await H.sleep(250);
      const after = await readCard(page);
      t.ok(JSON.stringify(before.segs) === JSON.stringify(after.segs),
        "clicking a line does nothing at all", "no selection, no editor");
      return "plan rows are display-only";
    });

    await t.step("card: the script textarea is editable and warns what editing costs", async () => {
      const u = await H.usable(page, ".vo .vo__script");
      t.must(u.ok, "the script field is usable", u.why || "ok");
      const c = await readCard(page);
      t.ok(/replaces the timed plan|flat script/i.test(c.scriptPlaceholder || ""),
        "the field warns that flat editing discards the timing", c.scriptPlaceholder || "(no placeholder)");
      await H.type(page, ".vo .vo__script", "A single line, rewritten by hand.");
      const back = await page.$eval(".vo .vo__script", (n) => n.value);
      t.eq(back, "A single line, rewritten by hand.", "typed text is kept");
      return "editable";
    });

    await t.step("card: exactly the five preset voices, and no cloning anywhere", async () => {
      const c = await readCard(page);
      t.must(!!c.voices, "a voice control exists");
      t.eq(c.voices.map((v) => v.id), VOICE_IDS, "the offered ids are the supported presets");
      t.ok(c.voices.every((v) => v.name.trim().length > 0), "each preset has a human name",
        c.voices.map((v) => v.name).join(", "));
      t.eq(c.voiceValue, DRAFT.voice_id, "the control is pre-selected from the drafted voice");
      // Cloning is not supported (models.py constrains voice_id to the enum). A UI
      // that implied it would be promising something the product cannot do.
      t.ok(!/clone|clonin|your own voice|upload a voice|record yourself|custom voice/i.test(c.text),
        "the card never implies voice cloning", "no cloning affordance");
      t.ok(!(await H.exists(page, '.vo input[type="file"]')), "no voice-sample upload");
      // The one microphone in the product dictates a BRIEF on the entrance; it
      // must not sit in the session where it would read as "record the narration".
      const mic = await page.evaluate(() => {
        const m = document.getElementById("mic-btn");
        return m ? { inEntrance: !!m.closest("#entrance"), inSession: !!m.closest("#session") } : null;
      });
      t.ok(!mic || (mic.inEntrance && !mic.inSession),
        "the only mic is the entrance's dictation, not a narration recorder",
        mic ? JSON.stringify(mic) : "no mic in this browser");
      return c.voices.map((v) => v.id).join(", ");
    });

    await t.step("card: there is NO language control — asserted as an absence", async () => {
      const has = await page.evaluate(() => {
        const card = document.querySelector(".vo");
        if (!card) return null;
        const ctls = Array.from(card.querySelectorAll("select,input,textarea"));
        return ctls
          .filter((n) => /lang/i.test(
            (n.getAttribute("aria-label") || "") + " " + (n.placeholder || "") + " " +
            (n.name || "") + " " + (n.className || "")))
          .map((n) => n.tagName.toLowerCase());
      });
      // propose_script/generate_voiceover both accept `language`, but nothing in
      // the card can express it. The payload check below proves it never ships.
      t.eq(has, [], "no language selector is offered (documented absence)");
      return "absent — see the payload check";
    });

    await t.step("card: the tone field is a real, prefilled delivery hint", async () => {
      const c = await readCard(page);
      t.ok(c.tone === DRAFT.tone, "prefilled from the drafted tone", `"${c.tone}"`);
      await H.type(page, '.vo .vo__bar input[type="text"]', "warm and unhurried");
      const back = await page.$eval('.vo .vo__bar input[type="text"]', (n) => n.value);
      t.eq(back, "warm and unhurried", "the tone can be changed");
      return back;
    });

    await t.step("card: the draft offers exactly the controls it needs, nothing that nags", async () => {
      const c = await readCard(page);
      t.ok(/generate/i.test(c.genLabel || ""),
        "the action says it will generate, not that narration is missing", c.genLabel);
      // A restraint-respecting card has four controls: the script, the voice, the
      // tone, and one action. Anything else here would be pushing more narration.
      t.ok(c.controls.length === 4, "no extra affordances on the drafting card",
        c.controls.join(" · "));
      t.ok(c.controls.some((s) => /vo__script/.test(s)) && c.controls.some((s) => /\[Voice\]/.test(s))
        && c.controls.some((s) => /\[Tone\]/.test(s)) && c.controls.some((s) => /vo__gen/.test(s)),
        "and they are the script, the voice, the tone and one action", c.controls.join(" · "));
      t.ok(!/add (another )?line|fill|cover the|more narration|coverage/i.test(c.text),
        "nothing invites narration for its own sake", "no coverage prompt");
      return c.controls.join(" ");
    });

    await t.step("card: changing the voice or the tone spends nothing on its own", async () => {
      // Narration is paid. A settings control that fired TTS on change would spend
      // on a glance; the card must hold the choice until Generate is pressed.
      await clearChoices(page);
      await page.select(".vo .vo__bar select", "bright_female");
      await H.type(page, '.vo .vo__bar input[type="text"]', "brisk");
      await H.sleep(450);
      const sent = await sentChoices(page);
      const confirmOpen = await page.$eval("#confirm-overlay", (n) => !n.hidden);
      t.eq(sent.length, 0, "no request left the client");
      t.ok(!confirmOpen, "no spend dialog opened either");
      const vo = ((await H.state(page)).layers || {}).voiceover || {};
      t.eq(vo.voice_id, DRAFT.voice_id, "the drafted voice in state is untouched until Generate");
      return "settings stay local until the user asks";
    });

    await t.step("card: what the draft held silent, and the clash it already knows about", async () => {
      const c = await readCard(page);
      // held_silent / collisions are computed by propose_script and carried in
      // layers.voiceover. Matched on the FIXTURE's own strings — a loose keyword
      // regex passes on the script itself ("Held, then gone.") and proves nothing.
      const heldReason = DRAFT.held_silent[0].reason;
      const clashWhat = DRAFT.collisions[0].what;
      t.ok(c.text.includes(heldReason), "the moment deliberately held silent is shown",
        c.text.includes(heldReason) ? "shown" : "layers.voiceover.held_silent is dropped by the UI");
      t.ok(c.text.includes(clashWhat), "the narration/SFX collision the backend flagged is shown",
        c.text.includes(clashWhat) ? "shown" : "layers.voiceover.collisions is dropped by the UI");
      return "restraint bookkeeping checked";
    });

    await t.step("card: the listen-back report on a read that did not fit", async () => {
      // After a render, hydrate_voiceover_layer measures every line against the
      // cut (tools/media.py:362-391 → narration_alignment:1404) and ships
      // `alignment` on the layer — drift, lines that
      // straddle a cut, lines that talk over the footage's own voice, lines that
      // run past the end of the video. It is the product's own listen-back critic
      // and the only way a user learns the read did not fit short of hearing it.
      const measured = Object.assign({}, DRAFT, {
        status: "completed", audio_url: null,
        alignment: {
          clean: false, talkovers: 1, overruns: 1, straddles: 1, spoken_s: 14.2,
          notes: ["The last line runs 2.1s past the end of the video."],
          observations: ["Line two speaks over the subject's own voice."],
        },
      });
      await freshDraft(page, measured);
      const c = await readCard(page);
      t.must(!!c, "the card is up for the recorded narration");
      const note = measured.alignment.notes[0];
      const thread = await H.threadText(page);
      t.ok(c.text.includes(note) || thread.includes(note),
        "a line that runs past the end of the video is reported",
        "layers.voiceover.alignment is dropped by the UI (only a live director could mention it in prose)");
      return "alignment checked";
    });

    await t.step("card: an agent re-draft that keeps 'draft' status must update the script", async () => {
      // Start from a card the app has just built for THIS draft, so what follows
      // measures the re-draft and not a leftover edit from an earlier step.
      await freshDraft(page, DRAFT);
      const first = await readCard(page);
      t.must(first && first.script === DRAFT.script, "the card starts on the first draft",
        (first && first.script || "").slice(0, 40));
      const redraft = Object.assign({}, DRAFT, {
        script: "One line only. The rest of the cut can breathe.",
        segments: [{ id: "seg_01", text: "One line only.", start_s: 3, delivery: "quiet" }],
      });
      // A re-draft is still a draft — the status does not change. The card is
      // memoized on vo.status (app.js:1416), so this is exactly the case where a
      // stale script can stay on screen and be submitted by Generate.
      await pushState(page, { layers: { voiceover: redraft } });
      const c = await readCard(page);
      t.ok(c.script === redraft.script, "the visible script is the NEW draft",
        c.script === redraft.script ? "updated" : `still showing: "${(c.script || "").slice(0, 45)}"`);
      t.eq(c.segs.length, 1, "the plan follows the re-draft too");
      return "re-draft rendered";
    });

    // ---- 5. the generate control, its guard and its cost gate --------------
    await t.step("generate: an emptied script is refused before any spend", async () => {
      await freshDraft(page, DRAFT);
      await clearChoices(page);
      await page.$eval(".vo .vo__script", (n) => { n.value = ""; });
      await H.click(page, ".vo .vo__gen");
      await H.sleep(400);
      const toastText = await page.$eval("#toast", (n) => (n.hidden ? "" : n.textContent));
      const confirmOpen = await page.$eval("#confirm-overlay", (n) => !n.hidden);
      const sent = (await sentChoices(page)).length;
      t.ok(toastText.trim().length > 0, "the refusal is explained", toastText);
      t.ok(!confirmOpen, "no spend dialog opens for an empty script");
      t.eq(sent, 0, "nothing was sent to the backend");
      return toastText;
    });

    await t.step("generate: cancelling the cost gate spends nothing", async () => {
      await freshDraft(page, DRAFT);
      await clearChoices(page);
      await H.click(page, ".vo .vo__gen");
      await H.sleep(400);
      const body = await page.$eval("#confirm-overlay", (n) => (n.hidden ? null : n.textContent.trim()));
      t.must(body !== null, "generating opens the spend confirm");
      t.ok(/spend|cost|generat|record/i.test(body), "the dialog says what it will cost", body.slice(0, 80));
      await H.approveSpend(page, { cancel: true });
      await H.sleep(300);
      t.eq((await sentChoices(page)).length, 0, "Cancel sends no choice");
      const stillThere = await H.usable(page, ".vo .vo__gen");
      t.ok(stillThere.ok, "the Generate button is still usable after cancelling",
        stillThere.why || "usable");
      return "cancelled cleanly";
    });

    let voFrame = null;
    await t.step("generate: approving sends the script, the voice and the tone", async () => {
      await clearChoices(page);
      // Back on the timeline first, so the in-flight render is observable on the
      // lane in the step after this one.
      await H.click(page, '#view-seg .vseg__btn[data-view="timeline"]');
      // Pick a different preset and a real tone so the payload has something to
      // carry that the draft did not already contain.
      await page.select(".vo .vo__bar select", "narrator_male");
      await H.type(page, '.vo .vo__bar input[type="text"]', "measured, close to the mic");
      // A non-English line. With no language control on the card (checked above),
      // the request payload is the ONLY place the language could be expressed —
      // and propose_script resolves it as `args.language or
      // observation.detected_language` (tools/impls.py:852-854) before
      // generate_voiceover synthesises with it (impls.py:931-944). So a payload
      // without `language` records this line in whatever language the FOOTAGE was
      // detected as, not the one the user typed.
      await H.type(page, ".vo .vo__script", "静かな瞬間こそ、いちばん大きく響く。");
      await H.click(page, ".vo .vo__gen");
      const gate = await H.approveSpend(page);
      t.ok(gate !== null, "the gate was answered", (gate || "").slice(0, 50));
      await H.sleep(600);
      const frames = (await sentChoices(page)).filter((f) => f.choice_type === "voiceover");
      t.must(frames.length === 1, "exactly one voice-over choice was sent", String(frames.length));
      voFrame = frames[0];
      const p = voFrame.payload || {};
      t.ok(p.script === "静かな瞬間こそ、いちばん大きく響く。", "the edited script is what gets recorded",
        (p.script || "").slice(0, 30));
      t.eq(p.voice_id, "narrator_male", "the chosen preset reaches the request");
      t.eq(p.tone, "measured, close to the mic", "the typed tone reaches the request");
      t.ok("language" in p, "the script's language reaches the request payload",
        "language" in p ? String(p.language) : "payload keys: " + Object.keys(p).join(","));
      return Object.keys(p).join(", ");
    });

    await t.step("generate: while TTS is in flight the card steps back instead of lying", async () => {
      // Between the approved click and the recording landing, layers.voiceover is
      // queued and the editable card is deliberately withheld (app.js:1414) — an
      // in-flight render must not be edited out from under itself.
      let queued = true;
      try {
        await H.waitForState(page,
          "(st) => ['queued','processing'].indexOf((((st.layers||{}).voiceover)||{}).status) >= 0",
          6000, "voiceover queued");
      } catch (_) { queued = false; }
      if (!queued) return "the recording landed before a queued state could be observed";
      const card = await H.exists(page, ".vo");
      t.ok(!card, "no editable script card while the narration is rendering",
        card ? "the card stayed editable mid-render" : "withheld");
      // The lane is the honest surface here: a render in flight is drawn as
      // pending with its real status, never as an interval it cannot measure.
      const lane = await voLane(page);
      const clip = (lane && lane.clips[0]) || null;
      t.ok(clip && clip.pending, "the lane draws the render as pending",
        clip ? clip.time : "no clip");
      t.ok(clip && !/–/.test(clip.time), "and invents no interval for audio that does not exist yet",
        clip ? clip.time : "");
      // …but the THREAD is where the user just pressed a paid button. A queued
      // music take gets a spinner row there (takeRow status); a queued narration
      // gets nothing — its thinking block is finalized by the same snapshot that
      // reports the queue, so the only live sign is on the timeline view.
      const pendingInThread = await page.evaluate(() => !!document.querySelector(
        "#thread-inner .cot .spinner, #thread-inner .take__status, #thread-inner .gspin"));
      const trail = (await H.threadText(page)).slice(-160).replace(/\s+/g, " ");
      t.ok(pendingInThread,
        "the thread carries a pending affordance for the paid render, as a queued take does",
        pendingInThread ? "pending row" : `nothing pending; the trail already reads finished: "${trail}"`);
      return "withheld while recording";
    });

    await t.step("generate: the narration is recorded and lands in state", async () => {
      await H.waitForState(page,
        "(st) => ((st.layers||{}).voiceover||{}).status === 'completed'",
        H.TURN_MS, "voiceover completed");
      const vo = ((await H.state(page)).layers || {}).voiceover || {};
      t.ok(!!vo.audio_url, "there is recorded audio", vo.audio_url ? vo.audio_url.slice(0, 24) + "…" : "none");
      t.eq(vo.voice_id, "narrator_male", "the recorded take used the chosen voice");
      t.ok(vo.script && vo.script.length > 0, "the recorded script is stored", vo.script.slice(0, 24));
      // The placeholder warned about this: a flat-text edit drops the timed plan.
      t.eq((vo.segments || []).length, 0,
        "editing the flat script did discard the timed plan (as the field warned)");
      return `status=${vo.status} voice=${vo.voice_id}`;
    });

    // ---- 6. the recorded card ---------------------------------------------
    await t.step("recorded: the narration appears as a playable take row", async () => {
      t.must(await H.waitFor(page, ".vo .take.lane-voiceover", 15000), "a take row for the narration");
      const c = await readCard(page);
      t.ok(/re-?record/i.test(c.genLabel || ""),
        "the action flips to re-record once narration exists", c.genLabel);
      t.ok(/edit the script|re-?record/i.test(c.text),
        "the card says the script is still editable", "editor note present");
      const play = await H.usable(page, ".vo .take.lane-voiceover .take__play");
      t.ok(play.ok, "the play control is usable", play.why || "ok");
      await H.click(page, ".vo .take.lane-voiceover .take__play");
      await H.sleep(800);
      const playing = await page.evaluate(() => {
        const a = window.__edenn.app._takeAudio;
        return !!a && !a.paused;
      });
      t.ok(playing, "pressing play actually starts the narration",
        playing ? "playing" : "PLAY DID NOT START AUDIO");
      await page.evaluate(() => { const a = window.__edenn.app._takeAudio; if (a) a.pause(); });
      // The length on the row is read off the audio element, never claimed: the
      // fixture narration is 7s, so anything else here is a guess.
      const dur = (await readCard(page)).takeDur;
      t.eq(dur, "0:07", "the row shows the narration's MEASURED length");
      return "take row live";
    });

    await t.step("recorded: the take's overflow menu offers the narration download", async () => {
      const sel = ".vo .take.lane-voiceover .take__more";
      t.must(await H.exists(page, sel), "the narration row has an overflow control");
      await H.click(page, sel);
      t.must(await H.waitFor(page, ".tmenu", 5000), "the menu opened");
      const items = await page.$$eval(".tmenu button", (ns) => ns.map((n) => n.innerText.trim()));
      t.ok(items.length >= 1, "the menu has an item", items.join(" | "));
      t.ok(items.some((s) => /download/i.test(s)), "downloading the narration is offered", items.join(" | "));
      await page.mouse.click(12, 12);
      await H.sleep(300);
      t.ok(!(await H.exists(page, ".tmenu")), "the menu dismisses on an outside click");
      // Not clicked: every mock media URL is a data: URI and the handler is
      // window.open(url,'_blank'), which Chrome blocks for a top-frame data:
      // navigation. The download itself is only testable on ?backend=real.
      return items.join(" | ");
    });

    await t.step("recorded: the mix gains a voice-over balance", async () => {
      const labels = await page.$$eval(".mix-card .mix-row__label", (ns) => ns.map((n) => n.textContent.trim()))
        .catch(() => []);
      t.ok(labels.some((s) => /voice/i.test(s)), "a voice-over level appears in the mix", labels.join(" / ") || "no mix card");
      // The flat script has no segments, so a global narration start is meaningful
      // here; with a segmented plan the backend forces it to 0 and it must hide.
      t.ok(labels.some((s) => /narration starts/i.test(s)),
        "a flat script offers the narration-start control", labels.join(" / "));
      return labels.join(" / ");
    });

    await t.step("mix: a TIMED plan hides the narration start the backend would ignore", async () => {
      // compose_mix forces voiceover_start_s to 0 when segments exist
      // (tools/impls.py:1368-1369) — offering the slider anyway would be a
      // control that silently does nothing.
      const st = await H.state(page);
      const flat = ((st.layers || {}).voiceover) || {};
      await pushState(page, { layers: Object.assign({}, st.layers, {
        voiceover: Object.assign({}, flat, { segments: DRAFT.segments }) }) });
      const labels = await page.$$eval(".mix-card .mix-row__label", (ns) => ns.map((n) => n.textContent.trim()))
        .catch(() => []);
      t.ok(labels.some((s) => /voice-over/i.test(s)), "the narration level is still offered", labels.join(" / "));
      t.ok(!labels.some((s) => /narration starts/i.test(s)),
        "no global start offset for a plan that carries its own timing", labels.join(" / "));
      await pushState(page, { layers: Object.assign({}, st.layers, { voiceover: flat }) });
      return labels.join(" / ");
    });

    // ---- 7. the timeline lane: draft vs recorded ---------------------------
    const liveSnap = await page.evaluate(() => JSON.parse(JSON.stringify(window.__edenn.app.snapshot)));

    await t.step("lane: a recorded narration draws its MEASURED interval", async () => {
      await H.click(page, '#view-seg .vseg__btn[data-view="timeline"]');
      await H.sleep(1400);
      const lane = await voLane(page);
      t.must(!!lane && lane.clips.length >= 1, "the voice-over lane has a clip",
        JSON.stringify((lane || {}).clips || []));
      const clip = lane.clips[0];
      t.ok(!clip.pending, "a recorded take is not drawn as pending", clip.time);
      // The fixture narration is 7s inside a 15s reel: an interval, not a guess.
      t.ok(/0:00–0:07/.test(clip.time), "the interval matches the recorded audio", clip.time);
      const w = parseFloat(clip.width);
      t.ok(w > 40 && w < 54, "the width tracks the measurement", clip.width);
      return clip.time;
    });

    await t.step("lane: a drafted-but-unrecorded script says exactly that", async () => {
      await pushState(page, { layers: { voiceover: { status: "draft", script: DRAFT.script } }, mix: null });
      const lane = await voLane(page);
      t.must(!!lane && lane.clips.length === 1, "the draft draws one clip",
        JSON.stringify((lane || {}).clips || []));
      const clip = lane.clips[0];
      t.ok(clip.pending, "a draft is drawn as pending");
      t.ok(/not recorded/i.test(clip.time), "the lane says the script is not recorded", clip.time);
      t.ok(!/\d:\d\d–/.test(clip.time), "no interval is invented for unrecorded audio", clip.time);
      return clip.time;
    });

    await t.step("lane: a timed plan draws one clip per line, on its own clock", async () => {
      await pushState(page, { layers: { voiceover: Object.assign({}, DRAFT, { status: "completed" }) },
                              mix: { voiceover_start_s: 9 } });
      const lane = await voLane(page);
      t.eq((lane.clips || []).length, 3, "one clip per planned line");
      // Segment timings are absolute — the global narration start must NOT shift
      // them (compose_mix forces voiceover_start_s to 0 for a segmented plan).
      t.ok(lane.clips.every((c) => c.moment || !/–/.test(c.time)),
        "planned lines with no measured length draw as moments, not intervals",
        lane.clips.map((c) => c.time).join(" "));
      t.ok(/0:02/.test(lane.clips[0].time),
        "the first line keeps its own timestamp despite mix.voiceover_start_s=9",
        lane.clips[0].time);
      return lane.clips.map((c) => c.time).join(" ");
    });

    // Back to the real session — and drop the card first, or the status-keyed
    // memo (app.js:1416) would keep the last SYNTHESIZED script in the textarea
    // and the re-record below would submit the fixture instead of the recording.
    await pushState(page, { layers: { voiceover: null } });
    await restoreSnapshot(page, liveSnap);

    // ---- 8. asking for a different read ------------------------------------
    await t.step("re-read: re-recording with another preset changes what was recorded", async () => {
      await clearChoices(page);
      const before = ((await H.state(page)).layers || {}).voiceover || {};
      t.must(await H.exists(page, ".vo .vo__gen"), "the card still offers a re-record");
      await page.select(".vo .vo__bar select", "warm_female");
      await H.type(page, '.vo .vo__bar input[type="text"]', "brighter, a half-step faster");
      await H.click(page, ".vo .vo__gen");
      const gate = await H.approveSpend(page);
      t.ok(gate !== null, "re-recording is cost-gated too", (gate || "").slice(0, 46));
      await H.waitForState(page,
        "(st) => (((st.layers||{}).voiceover)||{}).voice_id === 'warm_female' && ((st.layers||{}).voiceover||{}).status === 'completed'",
        H.TURN_MS, "re-recorded with the new voice");
      const after = ((await H.state(page)).layers || {}).voiceover || {};
      t.ok(after.voice_id !== before.voice_id, "the recorded voice actually changed",
        `${before.voice_id} → ${after.voice_id}`);
      t.eq(after.tone, "brighter, a half-step faster", "the new delivery direction is stored");
      t.ok(!!after.audio_url, "there is a new recording", after.status);
      // A different READ is a different performance of the same words — a
      // re-record that quietly rewrote the approved script would be a new script
      // the user never saw.
      t.eq(after.script, before.script, "the approved words are unchanged by the re-read");
      return `${before.voice_id} → ${after.voice_id}`;
    });

    let proseGate = null;
    await t.step("re-read: asking for a different read in plain words", async () => {
      await clearChoices(page);
      const before = ((await H.state(page)).layers || {}).voiceover || {};
      const textBefore = await H.threadText(page);
      await H.say(page, "Read the last line slower, and pull the whole thing back a touch.");
      proseGate = await H.approveSpend(page);
      await H.waitForIdle(page);
      const after = ((await H.state(page)).layers || {}).voiceover || {};
      const textAfter = await H.threadText(page);
      t.ok(textAfter.length > textBefore.length, "the director answered the ask");
      // Routing prose to a re-draft + re-record is the LIVE director's job
      // (propose_script → generate_voiceover). The offline mock has no narration
      // branch in _handleMessage, so what the layer does here is REPORTED, not
      // asserted — an always-true check would be worse than no check at all.
      const moved = JSON.stringify(before) !== JSON.stringify(after);
      return moved ? "the layer changed"
        : "answered without re-recording — a prose re-read needs the live director";
    });

    await t.step("restraint: TTS never fires from a conversation without the cost gate", async () => {
      const frames = (await sentChoices(page)).filter((f) => f.choice_type === "voiceover");
      // Either the ask spent nothing, or it asked first. Silently re-recording off
      // a sentence is the failure this guards: narration is paid, and this product
      // only speaks when it is asked to.
      t.ok(frames.length === 0 || proseGate !== null,
        "no narration was recorded without an approved spend",
        `${frames.length} voiceover frame(s), gate ${proseGate ? "shown" : "not shown"}`);
      return frames.length === 0 ? "no spend at all" : "spend was gated";
    });
  },
};
