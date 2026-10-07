/* The sound-effects chain, end to end.
 *
 *   treatment card → spotted plan → edit the plan → propose ideas → density
 *   override → render → pick a variant → the SFX lane on the timeline
 *
 * Two rules run underneath all of it, and every check here exists to hold one
 * of them:
 *
 *   1. Nothing is spotted before the user says what the effects should FEEL
 *      like, and the answer is durable — the director must never re-ask.
 *   2. The console never claims sound it has not rendered. The density cap is
 *      SURFACED, never silently enforced; a plan is a plan until a variant
 *      actually exists.
 *
 * A second, deliberately short session at the end drives the ambience-only
 * treatment, because answering the card is one-way inside a session and an
 * ambience-only plan (zero discrete hits, one continuous bed) is a legal plan
 * that renders — the branch a suite is most likely to leave untested. */
"use strict";

/** Everything the SFX card is currently showing, in one read. */
const readCard = (page) => page.evaluate(() => {
  const c = document.querySelector(".sfx-card");
  if (!c) return null;
  const txt = (n) => (n ? n.textContent.trim() : null);
  const rows = Array.from(c.querySelectorAll(".sfx-plan__row"));
  const amb = c.querySelector(".sfx-plan__row--amb");
  const gen = c.querySelector(".sfx-card__gen");
  const tools = c.querySelector(".sfx-card__tools");
  return {
    head: txt(c.querySelector(".sfx-card__hd")),
    treatment: txt(c.querySelector(".sfx-card__treatment")),
    cap: txt(c.querySelector(".sfx-card__cap")),
    capOver: !!c.querySelector(".sfx-card__cap.is-over"),
    events: rows.filter((r) => !r.classList.contains("sfx-plan__row--amb")).map((r) => ({
      t: txt(r.querySelector(".sfx-plan__t")),
      label: txt(r.querySelector(".sfx-plan__label")),
      why: txt(r.querySelector(".sfx-plan__why")),
    })),
    amb: txt(amb),
    ghosts: Array.from(c.querySelectorAll(".sfx-ghost")).map((g) => ({
      t: txt(g.querySelector(".sfx-ghost__t")),
      label: txt(g.querySelector(".sfx-ghost__label")),
      why: txt(g.querySelector(".sfx-ghost__why")),
      ok: !!g.querySelector(".sfx-ghost__btn.ok"),
      no: !!g.querySelector(".sfx-ghost__btn.no"),
    })),
    variants: Array.from(c.querySelectorAll(".sfx-variant")).map((v) => ({
      title: txt(v.querySelector(".sfx-variant__title")),
      sel: v.classList.contains("sel"),
      status: txt(v.querySelector(".gstatus")),
      spinner: !!v.querySelector(".gstatus .gspin"),
      play: !!v.querySelector(".gplay"),
      pick: txt(v.querySelector(".gsel.use, .gsel.chosen")),
      pickIsUse: !!v.querySelector(".gsel.use"),
      video: !!v.querySelector(".sfx-variant__video video"),
      download: !!v.querySelector(".gsel.ghost"),
    })),
    gen: gen ? { label: gen.textContent.trim(), disabled: gen.disabled, hidden: gen.hidden } : null,
    toolsHidden: tools ? tools.hidden : null,
    tools: Array.from(c.querySelectorAll(".sfx-card__tools button")).map((b) => b.textContent.trim()),
    editorOpen: !!c.querySelector(".sfx-editor"),
  };
});

/** The plan editor's live form values (not the state it was built from). */
const readEditor = (page) => page.evaluate(() => {
  const e = document.querySelector(".sfx-editor");
  if (!e) return null;
  const plan = document.querySelector(".sfx-card .sfx-plan");
  const gen = document.querySelector(".sfx-card__gen");
  const edit = document.querySelector(".sfx-card__edit");
  return {
    rows: Array.from(e.querySelectorAll(".sfx-editor__row")).map((r) => ({
      t: (r.querySelector(".sfx-editor__t") || {}).value,
      label: (r.querySelector(".sfx-editor__label") || {}).value,
      del: !!r.querySelector(".sfx-editor__del"),
    })),
    amb: (e.querySelector(".sfx-editor__amb input") || {}).value,
    add: !!e.querySelector(".sfx-editor__add"),
    cancel: !!e.querySelector(".sfx-editor__foot .btn-ghost"),
    save: !!e.querySelector(".sfx-editor__foot .btn-primary"),
    planHidden: plan ? plan.hidden : null,
    genHidden: gen ? gen.hidden : null,
    editHidden: edit ? edit.hidden : null,
  };
});

/** The SFX row of the timeline, clip by clip. */
const readLane = (page) => page.evaluate(() => {
  const row = document.querySelector(".tl-row.lane-sfx");
  if (!row) return null;
  const mute = document.querySelector(".tl-mute.lane-sfx");
  return {
    hint: (row.querySelector(".tl-lane__hint") || {}).textContent || "",
    muteDisabled: mute ? mute.disabled : null,
    barSub: (document.querySelector(".tl-bar__sub") || {}).textContent || "",
    clips: Array.from(row.querySelectorAll(".tl-clip")).map((c) => ({
      name: (c.querySelector(".tl-clip__nm") || {}).textContent || "",
      time: (c.querySelector(".tl-clip__t") || {}).textContent || "",
      moment: c.classList.contains("is-moment"),
      bed: c.classList.contains("is-bed"),
      pending: c.classList.contains("is-pending"),
      left: parseFloat(c.style.left),
      width: c.style.width,
      title: c.title,
    })),
  };
});

/** The stamp the SFX CARD prints (whole seconds — see the finding on precision). */
const stamp = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
/** The stamp the TIMELINE prints (tenths, carrying: 3.96 → "0:04"). */
const tenths = (s) => {
  const d = Math.round(Math.max(0, s || 0) * 10);
  const f = d % 10;
  return stamp((d - f) / 10) + (f ? "." + f : "");
};

/**
 * Click something that lives deep in the scrolling thread.
 *
 * The topbar floats over the top of the thread, so a control the thread has
 * scrolled up there fails the hit-test — which is a fact about where the page
 * happens to be scrolled, not about the control. Put it where a reader would
 * put it first, THEN let H.click do its full usability check.
 */
async function clickIn(page, H, sel, opts) {
  await page.$eval(sel, (n) => n.scrollIntoView({ block: "center" }));
  await H.sleep(180);
  return H.click(page, sel, opts);
}

/** state.layers.sfx, or {} — the card is a view of exactly this. */
const layer = async (H, page) => {
  const st = await H.state(page);
  const l = ((st || {}).layers || {}).sfx;
  return (l && typeof l === "object" && !Array.isArray(l)) ? l : null;
};

/** Answer the layer gate with a single layer and commit it. */
async function pickSfxOnly(page, H, t) {
  await page.waitForSelector(".lpick__row", { timeout: H.TURN_MS });
  await H.click(page, ".lpick__row.lane-music");
  await H.click(page, ".lpick__row.lane-voiceover");
  const note = await page.$eval(".w-go .w-note", (n) => n.textContent);
  await H.click(page, ".w-go button");
  await H.waitForIdle(page);
  return note;
}

module.exports = {
  name: "sfx flow",
  backend: "mock",
  persona: "SFX Tester",

  async run(page, t, { H }) {
    // ====================================================================
    // 1 · getting to the treatment question
    // ====================================================================
    await H.startSession(page, { text: "Sound design only on this one — no music, no narration." });

    await t.step("gate: an sfx-only pick plans the sfx layer and nothing else", async () => {
      const note = await pickSfxOnly(page, H, t);
      t.ok(/1 layer/.test(note), "the gate counted a single layer", note);
      const plan = (await H.state(page)).production_plan || {};
      t.eq(plan.layers, ["sfx"], "the production plan is sfx-only");
      t.ok(!!plan.mode, "the plan has a mode", plan.mode || "none");
      return `${plan.mode}: ${(plan.layers || []).join("+")}`;
    });

    await t.step("gate: nothing is spotted before the treatment is answered", async () => {
      await H.waitForState(page,
        "(st) => (st.pending_clarification || {}).topic === 'sfx_treatment'",
        H.TURN_MS, "the sfx treatment card");
      t.ok(!(await layer(H, page)), "no sfx plan exists yet",
        JSON.stringify(((await H.state(page)).layers || {}).sfx));
      t.ok(!(await H.exists(page, ".sfx-card")), "no SFX card is drawn before the answer");
      t.ok(!(await H.exists(page, ".sfx-card__gen")),
        "no Generate button is offered before the treatment exists");
      t.ok(!(await H.state(page)).sfx_treatment, "state.sfx_treatment is still empty");
      return "the question comes first";
    });

    // ====================================================================
    // 2 · the treatment card — every option
    // ====================================================================
    let options = [];
    await t.step("treatment card: every option is a real, usable choice", async () => {
      t.must(await H.waitFor(page, ".cards-row .intent-card", 20000), "the card grid rendered");
      options = await page.$$eval(".cards-row .intent-card", (ns) => ns.map((n) => ({
        title: (n.querySelector(".intent-card__title") || {}).textContent || "",
        hint: (n.querySelector(".intent-card__desc") || {}).textContent || "",
        pick: !!n.querySelector(".intent-card__pick"),
        disabled: n.disabled,
      })));
      const st = await H.state(page);
      const backing = (st.pending_clarification || {}).options || [];
      t.eq(options.length, backing.length, "a card per offered option");
      t.ok(options.length >= 3, "the register/density fork is a real fork", String(options.length));
      t.ok(options.every((o) => o.title && o.hint),
        "every option says what it is AND what it means", options.map((o) => o.title).join(" | "));
      t.eq(options.filter((o) => o.pick).length, 1, "exactly one option is the director's pick");
      for (let i = 1; i <= options.length; i++) {
        const u = await H.usable(page, `.cards-row .intent-card:nth-of-type(${i})`);
        t.ok(u.ok, `option ${i} (${options[i - 1].title}) is clickable`, u.why || "ok");
      }
      // The answered intent gate stays in the scrollback, so the LIVE question
      // is the last one mounted — reading the first would grade the wrong card.
      const qs = await page.$$eval(".clarify__q", (ns) => ns.map((n) => n.textContent.trim()));
      const q = qs[qs.length - 1] || "";
      t.ok(q.length > 0, "the card asks a question", q.slice(0, 70));
      t.eq(q, (st.pending_clarification || {}).question,
        "the live card shows the question the session is actually waiting on");
      return options.map((o) => o.title + (o.pick ? "*" : "")).join(" | ");
    });

    await t.step("treatment card: answering it writes a durable treatment", async () => {
      // The recommended option — the one a user actually lands on.
      const idx = options.findIndex((o) => o.pick) + 1;
      await H.click(page, `.cards-row .intent-card:nth-of-type(${idx || 1})`);
      await H.waitForState(page, "(st) => !!st.sfx_treatment", H.TURN_MS, "state.sfx_treatment");
      await H.waitForIdle(page);
      const st = await H.state(page);
      const tr = st.sfx_treatment || {};
      t.ok(!!tr.label, "the answer is recorded on the session", JSON.stringify(tr));
      t.eq(tr.source, "card", "recorded as a card answer, not a guess");
      t.ok(!st.pending_clarification, "the question is cleared once answered");
      const cards = await page.$$eval(".cards-row .intent-card",
        (ns) => ns.filter((n) => !n.disabled).length);
      t.eq(cards, 0, "the answered card is retired (every option disabled)");
      return tr.label;
    });

    // ====================================================================
    // 3 · the spotted plan
    // ====================================================================
    let firstTreatment = null;
    await t.step("plan card: the spotted events render with their timestamps and reasons", async () => {
      t.must(await H.waitFor(page, ".sfx-card", H.TURN_MS), "the SFX card rendered");
      const l = await layer(H, page);
      t.must(!!l, "state.layers.sfx is a real plan layer");
      firstTreatment = JSON.stringify((await H.state(page)).sfx_treatment);
      const c = await readCard(page);
      t.eq(c.events.length, (l.events || []).length, "one plan row per spotted event");
      t.ok(c.events.length > 0, "the footage earned at least one moment", String(c.events.length));
      const stamps = (l.events || []).map((e) => stamp(e.start_s || 0));
      t.eq(c.events.map((e) => e.t), stamps, "every row prints its event's own timestamp");
      t.ok((l.events || []).every((e, i) => c.events[i].label.indexOf(e.label) === 0),
        "every row names its event", c.events.map((e) => e.label).join(" / ").slice(0, 90));
      const withWhy = c.events.filter((e) => e.why).length;
      t.ok(withWhy === (l.events || []).filter((e) => e.reason).length,
        "the on-screen reason is shown wherever the plan has one", `${withWhy}/${c.events.length}`);
      const times = (l.events || []).map((e) => e.start_s || 0);
      t.ok(times.every((v, i) => i === 0 || v >= times[i - 1]), "events are in time order", times.join(" < "));
      t.ok(!l.summary || c.head.indexOf(l.summary) >= 0,
        "the card header carries the plan's own summary", c.head);
      return `${c.events.length} events`;
    });

    await t.step("plan card: the answered treatment frames the plan", async () => {
      const c = await readCard(page);
      const l = await layer(H, page);
      const label = ((l || {}).treatment || {}).label || "";
      t.ok(!!c.treatment, "a treatment line is shown", c.treatment || "MISSING");
      t.ok(label && c.treatment.indexOf(label) >= 0,
        "it shows the treatment the plan was actually spotted under", `${c.treatment} ⊃ ${label}`);
      // Documented gap, asserted as an absence rather than pretended away: the
      // line is not a control, so a change of mind has to go through chat.
      const clickable = await page.$eval(".sfx-card__treatment",
        (n) => n.tagName === "BUTTON" || !!n.onclick || n.getAttribute("role") === "button");
      t.ok(!clickable,
        "the treatment line is honestly read-only (re-asking in chat is the only edit)",
        "no handler — deliberate");
      return c.treatment;
    });

    await t.step("plan card: the density cap is SURFACED, with its override", async () => {
      const l = await layer(H, page);
      const c = await readCard(page);
      t.ok(!!l.density_cap, "state carries a density budget", String(l.density_cap));
      t.ok(!!c.cap, "the budget is visible on the card", c.cap || "NO CAP NOTE");
      t.ok(c.cap && c.cap.indexOf(l.cap_note) >= 0,
        "the card prints the backend's own note verbatim", l.cap_note);
      t.ok(new RegExp("\\b" + l.density_cap + "\\b").test(c.cap),
        "the note names the actual cap number", `cap=${l.density_cap} · ${c.cap}`);
      t.ok(/denser/i.test(c.cap), "the note tells the user how to override it", c.cap);
      t.eq(l.over_budget, false, "a within-budget plan is not flagged over budget");
      t.ok(!c.capOver, "…and the over-budget styling is not applied");
      t.ok((l.events || []).length <= l.density_cap,
        "the plan honours its own budget", `${(l.events || []).length} ≤ ${l.density_cap}`);
      return c.cap;
    });

    await t.step("plan card: every advertised action is present and usable", async () => {
      const c = await readCard(page);
      t.eq(c.toolsHidden, false, "the tools row is visible to a director-role viewer");
      t.eq(c.tools.length, 3, "three plan tools offered");
      t.ok(/edit/i.test(c.tools[0]), "Edit plan", c.tools[0]);
      t.ok(/transition/i.test(c.tools[1]), "Suggest transitions", c.tools[1]);
      t.ok(/idea|beyond/i.test(c.tools[2]), "Ideas beyond the frame", c.tools[2]);
      for (const sel of [".sfx-card__gen", ".sfx-card__edit",
                         ".sfx-card__tools .sfx-card__suggest:nth-of-type(2)",
                         ".sfx-card__tools .sfx-card__suggest:nth-of-type(3)"]) {
        await page.$eval(sel, (n) => n.scrollIntoView({ block: "center" }));
        await H.sleep(120);
        const u = await H.usable(page, sel);
        t.ok(u.ok, `usable: ${sel}`, u.why || "ok");
      }
      t.ok(c.gen && /generate/i.test(c.gen.label),
        "the first-take button says it generates", c.gen ? c.gen.label : "MISSING");
      return c.tools.join(" | ");
    });

    await t.step("honesty: nothing is claimed as rendered before anything is", async () => {
      const l = await layer(H, page);
      t.eq((l.variants || []).length, 0, "no variants exist yet");
      t.eq((await readCard(page)).variants.length, 0, "…and none are drawn");
      const lane = await readLane(page);
      t.ok(!/composed mix/i.test(lane.barSub),
        "the transport bar does not claim a composed mix", lane.barSub);
      t.ok(lane.clips.every((c) => !c.name || !/take|render/i.test(c.time)),
        "no lane clip claims a render", lane.clips.map((c) => c.time).join(" / "));
      return "plan only, and it says so";
    });

    // ====================================================================
    // 4 · the treatment must not be re-asked
    // ====================================================================
    await t.step("durability: asking for more effects does NOT re-ask the treatment", async () => {
      const before = await page.$$eval(".cards-row", (ns) => ns.length);
      await H.say(page, "Add a couple more sound effects in there.");
      const st = await H.state(page);
      const after = await page.$$eval(".cards-row", (ns) => ns.length);
      t.ok(!st.pending_clarification, "no new clarification is pending",
        JSON.stringify(st.pending_clarification || null));
      t.eq(after, before, "no second treatment card was mounted");
      t.eq(JSON.stringify(st.sfx_treatment), firstTreatment,
        "the answered treatment survived the turn unchanged");
      const l = await layer(H, page);
      t.ok(!!((l || {}).treatment || {}).label,
        "the re-spotted plan is still stamped with that treatment",
        ((l || {}).treatment || {}).label);
      return "answered once, remembered";
    });

    // ====================================================================
    // 5 · editing the plan
    // ====================================================================
    await t.step("editor: Edit plan opens a row per event and hides the read-only view", async () => {
      await H.click(page, ".sfx-card__edit");
      t.must(await H.waitFor(page, ".sfx-editor", 6000), "the editor opened");
      const e = await readEditor(page);
      const l = await layer(H, page);
      t.eq(e.rows.length, (l.events || []).length, "one editor row per planned event");
      t.eq(e.rows.map((r) => parseFloat(r.t)), (l.events || []).map((x) => x.start_s || 0),
        "each row is loaded with its event's start time");
      t.ok(e.rows.every((r) => r.label.length > 0), "each row is loaded with its label",
        e.rows.map((r) => r.label).join(" / ").slice(0, 80));
      t.ok(e.rows.every((r) => r.del), "each row offers a remove control");
      t.ok(e.add && e.cancel && e.save, "add / cancel / save are all present");
      t.eq(e.planHidden, true, "the read-only plan is hidden while editing");
      t.eq(e.genHidden, true, "Generate is hidden while the plan is unsaved");
      t.eq(e.editHidden, true, "…and Edit plan cannot be re-entered on top of itself");
      const amb = await H.usable(page, ".sfx-editor__amb input");
      t.ok(amb.ok, "the ambience bed is editable here", amb.why || "ok");
      return `${e.rows.length} rows`;
    });

    await t.step("editor: an empty plan is REFUSED, honestly and in place", async () => {
      const before = JSON.stringify((await layer(H, page)).events || []);
      // Strip it to nothing: every event removed and no ambience bed.
      let guard = 0;
      while (await H.exists(page, ".sfx-editor__row") && guard++ < 12) {
        await H.click(page, ".sfx-editor__row .sfx-editor__del", { settle: 80 });
      }
      await page.$eval(".sfx-editor__amb input", (n) => { n.value = ""; });
      const e0 = await readEditor(page);
      t.eq(e0.rows.length, 0, "the editor is genuinely empty before saving");
      await H.click(page, ".sfx-editor__foot .btn-primary");
      await H.sleep(400);
      const toast = await page.$eval("#toast",
        (n) => (n.hidden ? "" : n.textContent.trim())).catch(() => "");
      t.ok(toast.length > 0, "the refusal is explained to the user", toast || "NO TOAST");
      t.ok(/effect|ambience/i.test(toast), "…and it names the way out", toast);
      t.ok(await H.exists(page, ".sfx-editor"), "the editor stays open on the refused save");
      const after = JSON.stringify((await layer(H, page)).events || []);
      t.eq(after, before, "nothing was written to state.layers.sfx");
      return toast;
    });

    await t.step("editor: an edited plan saves back to state — free, no cost gate", async () => {
      await H.click(page, ".sfx-editor__add");
      t.must(await H.exists(page, ".sfx-editor__row"), "Add effect created a row");
      await H.type(page, ".sfx-editor__row .sfx-editor__label", "Door latch on the close");
      await H.type(page, ".sfx-editor__row .sfx-editor__t", "5.5");
      await H.type(page, ".sfx-editor__amb input", "distant city hum");
      await H.click(page, ".sfx-editor__foot .btn-primary");
      const gated = await page.$eval("#confirm-overlay", (n) => !n.hidden).catch(() => false);
      t.ok(!gated, "saving a plan does not ask to spend");
      await H.waitForState(page,
        "(st) => (((st.layers||{}).sfx||{}).events||[]).length === 1",
        H.TURN_MS, "the saved single-event plan");
      await H.waitForIdle(page);
      const l = await layer(H, page);
      t.eq((l.events || []).length, 1, "the removed events are gone from state");
      t.eq(l.events[0].label, "Door latch on the close", "the typed label reached state");
      t.eq(l.events[0].start_s, 5.5, "the typed start time reached state");
      t.eq(l.ambience, "distant city hum", "the ambience bed reached state");
      t.ok(!(await H.exists(page, ".sfx-editor")), "the editor closed after a successful save");
      const c = await readCard(page);
      t.eq(c.events.length, 1, "the read-only plan repainted with the edit");
      t.eq(c.events[0].t, tenths(5.5), "…at the edited timestamp, to the tenth");
      t.ok(!!c.amb && /distant city hum/.test(c.amb),
        "the ambience bed has its own line", c.amb || "NO AMBIENCE ROW");
      return `${c.events.length} event + bed`;
    });

    await t.step("editor: Cancel discards the edit and restores the plan", async () => {
      const before = JSON.stringify(await layer(H, page));
      await H.click(page, ".sfx-card__edit");
      t.must(await H.waitFor(page, ".sfx-editor", 6000), "editor reopened");
      await H.type(page, ".sfx-editor__row .sfx-editor__label", "Should never be saved");
      await H.click(page, ".sfx-editor__foot .btn-ghost");
      await H.sleep(400);
      t.ok(!(await H.exists(page, ".sfx-editor")), "the editor closed");
      const c = await readCard(page);
      t.eq(c.events.length, 1, "the read-only plan came back");
      t.ok(!/never be saved/i.test(JSON.stringify(c.events)),
        "the abandoned text is nowhere on the card", c.events[0].label);
      t.eq(JSON.stringify(await layer(H, page)), before, "state is untouched by a cancelled edit");
      const gen = await H.usable(page, ".sfx-card__gen");
      t.ok(gen.ok, "Generate is usable again after cancelling", gen.why || "ok");
      return "cancelled cleanly";
    });

    // ====================================================================
    // 6 · the two proposers
    // ====================================================================
    await t.step("suggest: 'Suggest transitions' proposes stylistic ghosts, free", async () => {
      const before = ((await layer(H, page)).events || []).length;
      await H.click(page, ".sfx-card__tools .sfx-card__suggest:nth-of-type(2)");
      await H.waitForState(page,
        "(st) => (((st.layers||{}).sfx||{}).suggestions||[]).some(s => s.status === 'pending')",
        H.TURN_MS, "pending suggestions");
      await H.waitForIdle(page);
      const l = await layer(H, page);
      const pending = (l.suggestions || []).filter((s) => s.status === "pending");
      t.ok(pending.length > 0, "suggestions arrived", String(pending.length));
      t.ok(pending.every((s) => s.origin === "stylistic"),
        "this button asked for the STYLISTIC proposer", pending.map((s) => s.origin).join(","));
      t.eq(((await layer(H, page)).events || []).length, before,
        "a proposal does not silently join the plan");
      const c = await readCard(page);
      t.eq(c.ghosts.length, pending.length, "one ghost row per pending suggestion");
      t.ok(c.ghosts.every((g) => g.t && g.label), "each ghost shows a time and what to hear",
        c.ghosts.map((g) => `${g.t} ${g.label}`).join(" | ").slice(0, 100));
      t.ok(c.ghosts.every((g) => g.why), "each ghost says WHY it is proposed",
        (c.ghosts[0] || {}).why || "no rationale");
      t.ok(c.ghosts.every((g) => g.ok && g.no), "each ghost offers accept and dismiss");
      return `${pending.length} stylistic ghosts`;
    });

    await t.step("suggest: 'Ideas beyond the frame' asks the OTHER proposer", async () => {
      const before = ((await layer(H, page)).suggestions || []).length;
      await H.click(page, ".sfx-card__tools .sfx-card__suggest:nth-of-type(3)");
      await H.waitForState(page,
        `(st) => (((st.layers||{}).sfx||{}).suggestions||[]).length > ${before}`,
        H.TURN_MS, "a narrative suggestion");
      await H.waitForIdle(page);
      const l = await layer(H, page);
      const narrative = (l.suggestions || []).filter((s) => s.origin === "narrative");
      t.ok(narrative.length > 0, "the narrative proposer answered this button",
        narrative.map((s) => s.sound_prompt).join(" | ").slice(0, 80));
      t.eq((await readCard(page)).ghosts.length,
        (l.suggestions || []).filter((s) => s.status === "pending").length,
        "every pending idea is on the card");
      return `${narrative.length} narrative ideas`;
    });

    await t.step("suggest: accepting adds to the plan, dismissing does not", async () => {
      const before = ((await layer(H, page)).events || []).length;
      await H.click(page, ".sfx-card .sfx-ghost .sfx-ghost__btn.ok");
      await H.waitForState(page,
        `(st) => (((st.layers||{}).sfx||{}).events||[]).length === ${before + 1}`,
        H.TURN_MS, "the accepted event");
      const gated = await page.$eval("#confirm-overlay", (n) => !n.hidden).catch(() => false);
      t.ok(!gated, "accepting a plan edit does not ask to spend");
      const l1 = await layer(H, page);
      t.eq((l1.events || []).length, before + 1, "the accepted idea became a planned event");
      t.ok((l1.suggestions || []).some((s) => s.status === "accepted"),
        "the suggestion is marked accepted, not left pending");
      const mid = ((await layer(H, page)).events || []).length;
      await H.click(page, ".sfx-card .sfx-ghost .sfx-ghost__btn.no");
      await H.waitForState(page,
        "(st) => (((st.layers||{}).sfx||{}).suggestions||[]).some(s => s.status === 'rejected')",
        H.TURN_MS, "the rejected suggestion");
      const l2 = await layer(H, page);
      t.eq((l2.events || []).length, mid, "dismissing changed nothing in the plan");
      const stillPending = (l2.suggestions || []).filter((s) => s.status === "pending").length;
      t.eq((await readCard(page)).ghosts.length, stillPending,
        "resolved ghosts leave the card, pending ones stay");
      const times = (l2.events || []).map((e) => e.start_s || 0);
      t.ok(times.every((v, i) => i === 0 || v >= times[i - 1]),
        "the accepted event was inserted in time order", times.join(" < "));
      return `${before} → ${(l2.events || []).length} events`;
    });

    // ====================================================================
    // 7 · the cap is a norm, not a gate
    // ====================================================================
    await t.step("honesty: 'denser' overrides the cap and the plan says it is over budget", async () => {
      const l0 = await layer(H, page);
      await H.say(page, "Go denser — I want a hit on every cut.");
      const l = await layer(H, page);
      t.ok((l.events || []).length > l.density_cap,
        "the override actually produced more effects than the budget",
        `${(l.events || []).length} events vs cap ${l.density_cap}`);
      t.eq(l.over_budget, true, "state flags the plan as over budget");
      const c = await readCard(page);
      t.eq(c.events.length, (l.events || []).length,
        "the over-budget plan is shown IN FULL, never silently trimmed");
      t.ok(c.capOver, "the cap note wears its over-budget styling");
      t.ok(/over budget/i.test(c.cap || ""), "…and says so in words", c.cap);
      t.eq(l.ambience, l0.ambience, "the ambience bed survived the re-spot");
      return `${(l.events || []).length} events > cap ${l.density_cap}`;
    });

    // ====================================================================
    // 8 · rendering
    // ====================================================================
    await t.step("generate: the render is cost-gated before anything is spent", async () => {
      const before = ((await layer(H, page)).variants || []).length;
      await clickIn(page, H, ".sfx-card__gen");
      await H.sleep(400);
      const open = await page.$eval("#confirm-overlay", (n) => !n.hidden).catch(() => false);
      t.must(open, "a confirm dialog opened before generating");
      const title = await page.$eval("#confirm-title", (n) => n.textContent.trim());
      const body = await page.$eval("#confirm-body", (n) => n.textContent.trim());
      t.ok(title.length > 0 && body.length > 0, "the dialog says what it is about to do",
        `${title} — ${body.slice(0, 60)}`);
      t.ok(/spend|generat|render/i.test(body), "…and that it costs something", body.slice(0, 80));
      // Cancel first: a gate that spends anyway is not a gate.
      await H.click(page, "#confirm-cancel", { settle: 500 });
      t.eq(((await layer(H, page)).variants || []).length, before,
        "cancelling the gate renders nothing");
      const gen = await H.usable(page, ".sfx-card__gen");
      t.ok(gen.ok, "the Generate button is still usable after a cancel", gen.why || "ok");
      return `${title} (cancelled)`;
    });

    await t.step("generate: approving renders a variant, shown at its real status", async () => {
      await clickIn(page, H, ".sfx-card__gen");
      const body = await H.approveSpend(page);
      t.ok(body !== null, "the spend gate was answered", (body || "").slice(0, 50));
      await H.waitForState(page,
        "(st) => (((st.layers||{}).sfx||{}).variants||[]).length === 1",
        H.TURN_MS, "the first variant");
      const l = await layer(H, page);
      const v = l.variants[0];
      const c = await readCard(page);
      t.eq(c.variants.length, 1, "the variant is drawn as soon as it exists");
      if (v.status === "queued" || v.status === "processing") {
        t.ok(c.variants[0].spinner, "an unfinished variant shows a spinner, not a player",
          c.variants[0].status || "");
        t.ok(!c.variants[0].play, "…and offers no play control it cannot honour");
        t.ok(c.gen.disabled && /render/i.test(c.gen.label),
          "the Generate button reports the render in flight", c.gen.label);
        // The session marks a variant chosen the moment it is enqueued. Nobody
        // has heard it — there is nothing to have chosen yet.
        t.ok(!c.variants[0].sel,
          "a variant nobody can hear yet is not already marked as the chosen one",
          `status=${v.status} selected_variant_id=${l.selected_variant_id}`);
      } else {
        t.ok(true, "variant completed before it could be observed queued", v.status);
      }
      await H.waitForState(page,
        "(st) => (((st.layers||{}).sfx||{}).variants||[]).every(v => v.status === 'completed')",
        H.TURN_MS, "the variant to finish");
      await H.sleep(400);
      const done = await readCard(page);
      t.ok(done.variants[0].play, "a finished variant offers playback");
      t.ok(!!done.variants[0].pick, "…and a way to choose it", done.variants[0].pick);
      t.ok(!done.variants[0].spinner, "…and no leftover spinner");
      t.ok(done.gen && !done.gen.disabled && /another|variant/i.test(done.gen.label),
        "the button now offers a second variant", done.gen ? done.gen.label : "MISSING");
      // A rendered SFX pass is a video when the pipeline muxed one; this render
      // came back audio-only, so neither the inline player nor Download may be
      // offered — an affordance for media that does not exist is a lie.
      const l2 = await layer(H, page);
      const hasVideo = !!l2.variants[0].video_url;
      t.eq(done.variants[0].video, hasVideo, "an inline video is shown only when one was rendered");
      t.eq(done.variants[0].download, hasVideo, "Download is offered only when there is a file to hand back");
      return done.variants[0].pick;
    });

    await t.step("generate: a second variant is comparable and selectable", async () => {
      await clickIn(page, H, ".sfx-card__gen");
      await H.approveSpend(page);
      await H.waitForState(page,
        "(st) => (((st.layers||{}).sfx||{}).variants||[]).length === 2",
        H.TURN_MS, "the second variant");
      await H.waitForState(page,
        "(st) => (((st.layers||{}).sfx||{}).variants||[]).every(v => v.status === 'completed')",
        H.TURN_MS, "both variants finished");
      await H.sleep(500);
      const c = await readCard(page);
      t.eq(c.variants.length, 2, "both variants are on the card");
      t.ok(c.variants.every((v) => v.title), "each variant is named",
        c.variants.map((v) => v.title).join(" | "));
      t.eq(c.variants.filter((v) => v.sel).length, 1, "exactly one variant reads as selected");
      const l = await layer(H, page);
      const other = l.variants.find((v) => v.variant_id !== l.selected_variant_id);
      t.must(!!other, "an unselected variant to switch to");
      const idx = l.variants.indexOf(other) + 1;
      const sel = `.sfx-variants .sfx-variant:nth-of-type(${idx}) .gsel.use`;
      const u = await H.usable(page, sel);
      t.ok(u.ok, "the unselected variant offers 'Use this'", u.why || "ok");
      await H.click(page, sel);
      await H.waitForState(page,
        `(st) => ((st.layers||{}).sfx||{}).selected_variant_id === '${other.variant_id}'`,
        H.TURN_MS, "the switched selection");
      const after = await layer(H, page);
      t.eq(after.selected_variant_id, other.variant_id,
        "state.layers.sfx.selected_variant_id records the choice");
      await H.sleep(400);
      const c2 = await readCard(page);
      t.eq(c2.variants.filter((v) => v.sel).length, 1, "still exactly one selected card");
      t.ok(c2.variants[idx - 1].sel, "…and it is the one that was clicked");
      t.ok(!c2.variants[idx - 1].pickIsUse,
        "the chosen card stops offering to be chosen", c2.variants[idx - 1].pick);
      return `${after.selected_variant_id} selected`;
    });

    await t.step("generate: a rendered SFX pass reaches the mix as its own level", async () => {
      await H.sleep(600);
      const has = await H.exists(page, ".mix-card");
      if (!has) return "no mix card in an sfx-only session — mix untested here";
      const labels = await page.$$eval(".mix-card .mix-row__label", (ns) => ns.map((n) => n.textContent.trim()));
      t.ok(labels.some((l) => /sound fx/i.test(l)),
        "a Sound FX level appears once a rendered variant has audio", labels.join(" | "));
      t.ok(!labels.some((l) => /^music$/i.test(l)),
        "no Music slider is offered in a session with no music track", labels.join(" | "));
      return labels.join(" | ");
    });

    // ====================================================================
    // 9 · the SFX lane
    // ====================================================================
    await t.step("timeline: the SFX lane matches state, moment for moment", async () => {
      await H.sleep(600);
      const l = await layer(H, page);
      const st = await H.state(page);
      const dur = (st.observation || {}).duration_s;
      t.must(!!dur, "the session has a measured duration to lay the lane on");
      const lane = await readLane(page);
      t.must(!!lane, "the sfx lane row exists");
      const bed = lane.clips.filter((c) => c.bed);
      const hits = lane.clips.filter((c) => !c.bed);
      t.eq(hits.length, (l.events || []).length, "one lane moment per planned event");
      t.eq(bed.length, (l.ambience || "").trim() ? 1 : 0, "one continuous span for the ambience bed");
      t.eq(lane.clips.length, (l.events || []).length + bed.length,
        "the lane draws exactly what state holds — no more");
      t.ok(hits.every((c) => c.moment), "planned events draw as moments, not as invented intervals");
      t.eq(hits.map((c) => c.time), (l.events || []).map((e) => tenths(e.start_s || 0)),
        "each moment prints its own timestamp, to the tenth");
      const wrong = (l.events || []).map((e, i) => {
        const want = ((e.start_s || 0) / dur) * 100;
        return Math.abs(hits[i].left - want) > 1.5 ? `${e.start_s}s → ${hits[i].left}% (want ${want.toFixed(1)}%)` : null;
      }).filter(Boolean);
      t.ok(!wrong.length, "every moment sits on its timestamp", wrong.join(", ") || "all on the clock");
      t.eq(lane.muteDisabled, false, "the sfx mute is live now that the lane has content");
      return `${hits.length} moments + ${bed.length} bed`;
    });

    await t.step("timeline: the plan and the lane agree on WHEN each effect lands", async () => {
      // Two views of one spotted event. A spotting list whose clock disagrees
      // with the lane's clock cannot be checked against the picture, and the
      // rounding is not harmless: an event at 3.6s reads "0:04" in the plan and
      // "0:03.6" on the lane — a full second apart on a list of cut timings.
      const l = await layer(H, page);
      const card = await readCard(page);
      const lane = await readLane(page);
      const hits = lane.clips.filter((c) => !c.bed);
      const rows = (l.events || []).map((e, i) => ({
        at: e.start_s || 0,
        card: (card.events[i] || {}).t,
        lane: (hits[i] || {}).time,
      }));
      const disagree = rows.filter((r) => r.card !== r.lane);
      t.ok(!disagree.length, "the plan row and the lane moment print the same instant",
        disagree.map((r) => `${r.at}s: card ${r.card} vs lane ${r.lane}`).join(" · ") || "in agreement");
      const lossy = rows.filter((r) => r.at !== Math.round(r.at));
      t.ok(!lossy.some((r) => r.card === stamp(r.at) && stamp(r.at) !== tenths(r.at)),
        "a sub-second cut timing is not rounded away in the plan",
        lossy.map((r) => `${r.at}s → ${r.card}`).join(" · ") || "no sub-second events to lose");
      return `${rows.length} events compared`;
    });

    await t.step("timeline: the ambience bed reads as continuous, not as another hit", async () => {
      const l = await layer(H, page);
      const lane = await readLane(page);
      const bed = lane.clips.find((c) => c.bed);
      t.must(!!bed, "the ambience bed is drawn");
      t.ok(!bed.moment, "the bed is not drawn as a point moment");
      t.eq(bed.left, 0, "it starts at the top of the clip");
      t.ok(parseFloat(bed.width) > 90, "…and runs the whole picture", bed.width);
      t.ok(bed.name.indexOf(l.ambience) >= 0 || /ambience/i.test(bed.name),
        "it names the bed it stands for", bed.name);
      return `${bed.name} @ ${bed.left}% × ${bed.width}`;
    });

    await t.step("timeline: clicking a planned moment selects it and seeks there", async () => {
      const l = await layer(H, page);
      const target = (l.events || [])[Math.min(1, (l.events || []).length - 1)];
      const hits = await page.$$(".tl-row.lane-sfx .tl-clip:not(.is-bed)");
      t.must(hits.length > 0, "a moment to click");
      const idx = Math.min(1, hits.length - 1);
      const box = await hits[idx].boundingBox();
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
      await H.sleep(350);
      const sel = await page.$$eval(".tl-row.lane-sfx .tl-clip.is-sel", (ns) => ns.length);
      t.eq(sel, 1, "the clicked moment is selected");
      const at = await page.evaluate(() => {
        const v = document.querySelector(".tl-screen video");
        return v ? v.currentTime : null;
      });
      if (at === null) return "no preview loaded (mock has no source video) — seek untested";
      t.ok(Math.abs(at - (target.start_s || 0)) < 0.6,
        "the playhead moved to that effect's time", `${at} vs ${target.start_s}`);
      return `selected @ ${target.start_s}s`;
    });

    // ====================================================================
    // 10 · the ambience-only treatment (a fresh session — the card is one-way)
    // ====================================================================
    await t.step("ambience-only: a plan with zero discrete hits is a real plan", async () => {
      await H.click(page, "#tb-home", { settle: 600 });
      await H.startSession(page, { text: "Just a background bed under this, nothing else." });
      await pickSfxOnly(page, H, t);
      await H.waitForState(page,
        "(st) => (st.pending_clarification || {}).topic === 'sfx_treatment'",
        H.TURN_MS, "the treatment card, second session");
      const opts = await page.$$eval(".cards-row .intent-card",
        (ns) => ns.map((n) => (n.querySelector(".intent-card__title") || {}).textContent || ""));
      const idx = opts.findIndex((o) => /ambience/i.test(o)) + 1;
      t.must(idx > 0, "the ambience-only option is offered", opts.join(" | "));
      await H.click(page, `.cards-row .intent-card:nth-of-type(${idx})`);
      await H.waitForState(page, "(st) => !!((st.layers||{}).sfx||{}).ambience",
        H.TURN_MS, "an ambience-only plan");
      await H.waitForIdle(page);
      const l = await layer(H, page);
      t.eq((l.events || []).length, 0, "no discrete hits were invented under this treatment");
      t.ok(!!(l.ambience || "").trim(), "a continuous bed was planned", l.ambience);
      t.ok(await H.exists(page, ".sfx-card"),
        "the SFX card renders for an events-free plan (it is not 'empty')");
      const c = await readCard(page);
      t.eq(c.events.length, 0, "the plan shows no event rows");
      t.ok(!!c.amb, "the bed has its own row", c.amb);
      t.ok(!c.cap, "no density note is invented when there are no discrete hits", c.cap || "none");
      const gen = await H.usable(page, ".sfx-card__gen");
      t.ok(gen.ok, "an ambience-only plan can still be rendered", gen.why || "ok");
      return l.ambience;
    });

    await t.step("ambience-only: the lane draws one span and nothing else", async () => {
      await H.sleep(700);
      const lane = await readLane(page);
      t.must(!!lane, "sfx lane present");
      t.eq(lane.clips.length, 1, "exactly one clip on the lane");
      t.ok(lane.clips[0].bed, "…and it is the bed");
      t.eq(lane.clips[0].left, 0, "the bed starts at 0:00");
      t.ok(parseFloat(lane.clips[0].width) > 90, "…and spans the cut", lane.clips[0].width);
      t.ok(!/composed mix/i.test(lane.barSub),
        "still nothing claimed as composed", lane.barSub);
      return lane.clips[0].name;
    });
  },
};
