/* The canvas and the people around it.
 *
 * Two halves of one session. The CANVAS is the only view that shows lineage —
 * which direction a take came from, what was branched off what, which path is
 * locked — and it is also the only place collab lives: pins, threads, the
 * agent handoff. So the journey drives them together, in the order a real
 * session reaches them: empty tree → source → directions → takes → branch →
 * lock, then comment, hand work to the director, and finally share.
 *
 * Two rules this file is built on:
 *   1. Assert on STATE and STRUCTURE. Node ids, edge colours, snapshot fields,
 *      which control is enabled — never on a sentence the director wrote.
 *   2. ROLE is a promise about money. Anything a "can view" collaborator can
 *      click that spends is a defect, and the check that finds it must be
 *      allowed to fail rather than be softened into an observation.
 *
 * Where a state cannot be reached by clicking (the empty tree only exists for
 * the first second of a session), the module's own public entry point —
 * EdennCanvas.render(snapshot) — is driven with a synthesized snapshot. That is
 * the same call app.js makes on every poll, so the path under test is real. */
"use strict";

const CANVAS = '#view-seg .vseg__btn[data-view="canvas"]';
const TIMELINE = '#view-seg .vseg__btn[data-view="timeline"]';

/** Everything the canvas is currently drawing, in one read. */
const readCanvas = (page) => page.evaluate(() => {
  const q = (s) => document.querySelector(s);
  const all = (s) => Array.from(document.querySelectorAll(s));
  const disp = (s) => { const n = q(s); return n ? getComputedStyle(n).display : "absent"; };
  return {
    stageHidden: q("#cstage") ? !!q("#cstage").hidden : "no stage",
    emptyShown: q("#cv-vp .cstage__empty") ? !q("#cv-vp .cstage__empty").hidden : false,
    emptyText: (q("#cv-vp .cstage__empty") || {}).innerText || "",
    dockDisp: disp("#cv-dock"), legendDisp: disp("#cv-vp .cstage__legend"),
    zoomDisp: disp("#cv-vp .cstage__zoom"), chipDisp: disp("#cv-vp .clb-mode"),
    legend: (q("#cv-vp .cstage__legend") || {}).innerText || "",
    nodes: all("#cv-world .cv-node").map((n) => ({
      id: n.dataset.id, cls: n.className, aria: n.getAttribute("aria-label") || "",
      locked: n.classList.contains("is-locked"), focus: n.classList.contains("is-focus"),
      dl: !!n.querySelector(".cv-thumb__dl"),
    })),
    edges: all("#cv-world svg path").map((p) =>
      (p.getAttribute("stroke") || "") + (p.getAttribute("stroke-dasharray") ? " dashed" : "")),
    dock: {
      title: (q("#cv-dock-title") || {}).textContent || "",
      path: (q("#cv-dock-path") || {}).textContent || "",
      time: (q("#cv-dock-time") || {}).textContent || "",
      playDisabled: !!(q("#cv-dock-play") || {}).disabled,
      use: q("#cv-dock-use")
        ? { txt: q("#cv-dock-use").innerText.trim(), disabled: q("#cv-dock-use").disabled,
            disp: getComputedStyle(q("#cv-dock-use")).display }
        : "absent",
      branch: q("#cv-dock-branch")
        ? { disabled: q("#cv-dock-branch").disabled, disp: getComputedStyle(q("#cv-dock-branch")).display }
        : "absent",
    },
    world: (q("#cv-world") || {}).style ? q("#cv-world").style.transform : "",
    pins: all("#cv-world .clb-pin").map((p) => p.className),
  };
});

const scaleOf = (transform) => {
  const m = /scale\(([\d.]+)\)/.exec(transform || "");
  return m ? Number(m[1]) : null;
};

/** Take a session all the way to two finished takes on the canvas. */
async function generateTakes(page, H, t) {
  await page.waitForSelector(".lpick__row", { timeout: H.TURN_MS });
  await H.click(page, ".w-go button");
  await H.waitForIdle(page);
  t.must(await H.waitFor(page, ".direction-option .dt-use", 30000), "the director proposed directions");
}

module.exports = {
  name: "canvas and collab",
  backend: "mock",
  persona: "Canvas Tester",

  async run(page, t, { H }) {
    // ======================================================================
    // CANVAS — the lineage tree
    // ======================================================================
    await H.startSession(page, { text: "Cinematic and premium — build to the close.", wait: false });

    await t.step("view: the right-pane toggle switches the pane and the composer with it", async () => {
      t.must((await H.usable(page, CANVAS)).ok, "the Canvas toggle is usable");
      await H.click(page, CANVAS);
      await H.sleep(400);
      const r = await readCanvas(page);
      t.ok(!r.stageHidden, "the canvas stage is shown");
      const inp = await page.$eval("#session-input", (n) => ({
        ph: n.getAttribute("placeholder"), role: n.getAttribute("role"),
        controls: n.getAttribute("aria-controls"),
      }));
      t.eq(inp.role, "combobox", "the composer becomes a combobox in canvas");
      t.eq(inp.controls, "cv-acmenu", "…wired to the canvas autocomplete");
      await H.click(page, TIMELINE);
      const back = await page.$eval("#session-input", (n) => n.getAttribute("role"));
      t.ok(back === null, "switching back strips the canvas ARIA", String(back));
      await H.click(page, CANVAS);
      return inp.ph;
    });

    await t.step("state: an empty tree says so and hides every control that needs one", async () => {
      // The genuinely empty canvas exists only before the first analysis lands,
      // so it is driven through the module's own render entry point.
      await page.evaluate(() => window.EdennCanvas.render(
        { session_id: "spec_empty", messages: [], state: { candidates: [], proposals: [] } }));
      await H.sleep(300);
      const r = await readCanvas(page);
      t.ok(r.emptyShown, "the empty state is shown", r.emptyText.slice(0, 60));
      t.ok(/lineage tree fills in/i.test(r.emptyText), "it explains what will fill it in");
      t.eq(r.nodes.length, 0, "no nodes drawn");
      t.eq(r.dockDisp, "none", "the dock is hidden with nothing to dock");
      t.eq(r.legendDisp, "none", "the legend is hidden");
      t.eq(r.zoomDisp, "none", "the zoom stack is hidden");
      // Every other canvas affordance stands down here; comment mode does not.
      t.eq(r.chipDisp, "none", "comment mode is not offered with nothing to anchor a thread to");
      await page.evaluate(() => window.EdennCanvas.render(window.__edenn.app.snapshot));
      await H.sleep(300);
      return "empty tree honest";
    });

    await t.step("state: the source clip is the root of the tree before anything is proposed", async () => {
      await H.waitForIdle(page);
      await H.sleep(500);
      const r = await readCanvas(page);
      t.ok(!r.emptyShown, "the empty state stood down once the video was analyzed");
      const src = r.nodes.find((n) => n.id === "__source__");
      t.must(!!src, "a source node exists", r.nodes.map((n) => n.id).join(","));
      t.eq(r.nodes.length, 1, "…and it is the only node before directions exist");
      t.ok(/source video/i.test(r.dock.title), "the dock reads the source", r.dock.title);
      t.ok(r.dock.use === "absent" || r.dock.use.disp === "none",
        "no Generate/Use action on a source node", JSON.stringify(r.dock.use));
      return r.dock.title;
    });

    await t.step("state: one node per proposed direction, hanging off the source", async () => {
      await H.click(page, TIMELINE);
      await generateTakes(page, H, t);
      await H.click(page, CANVAS);
      await H.sleep(600);
      const r = await readCanvas(page);
      const st = await H.state(page);
      const props = (st.proposals || []).map((p) => p.proposal_id);
      const drawn = r.nodes.filter((n) => props.includes(n.id));
      t.eq(drawn.length, props.length, "a node per direction in state");
      t.ok(drawn.every((n) => /^Direction:/.test(n.aria)), "each names itself a direction",
        drawn.map((n) => n.aria).join(" | "));
      t.eq(r.edges.length, props.length, "an edge from the source to each direction");
      return props.join(", ");
    });

    await t.step("controls: the dock generates from the focused direction, cost-gated", async () => {
      const st = await H.state(page);
      const pid = (st.proposals || [])[0].proposal_id;
      await H.click(page, `#cv-world .cv-node[data-id="${pid}"]`);
      await H.sleep(300);
      let r = await readCanvas(page);
      t.eq(r.nodes.filter((n) => n.focus).length, 1, "exactly one node carries focus");
      t.ok(r.nodes.find((n) => n.id === pid).focus, "the clicked node is the focused one");
      t.ok(r.dock.use !== "absent" && /generate/i.test(r.dock.use.txt),
        "the dock offers Generate for a direction", JSON.stringify(r.dock.use));
      t.ok(r.dock.path.includes("›"), "the dock shows a root-to-node path", r.dock.path);
      await H.click(page, "#cv-dock-use");
      await H.sleep(600);
      const body = await H.approveSpend(page);
      t.ok(!!body && /spend/i.test(body), "generating from the canvas asks for spend approval first",
        body ? body.slice(0, 70) : "NO CONFIRM DIALOG");
      await H.waitForIdle(page);
      await H.waitForState(page, "(st) => (st.candidates || []).some(c => c.status === 'completed')",
        H.TURN_MS, "a completed take");
      await H.sleep(1200);
      r = await readCanvas(page);
      t.ok(r.dock.use !== "absent" && r.dock.use.disabled,
        "a consumed direction cannot be generated twice", JSON.stringify(r.dock.use));
      return "generated from the canvas";
    });

    await t.step("state: every take in the snapshot is a node under its direction", async () => {
      const st = await H.state(page);
      const r = await readCanvas(page);
      const cands = st.candidates || [];
      t.ok(cands.length >= 1, "takes exist in state", String(cands.length));
      const missing = cands.filter((c) => !r.nodes.some((n) => n.id === c.candidate_id));
      t.eq(missing.length, 0, "no take is missing from the canvas",
        missing.map((c) => c.candidate_id).join(",") || "none");
      const done = cands.filter((c) => c.status === "completed");
      const withDl = r.nodes.filter((n) => n.dl).length;
      t.eq(withDl, done.length, "a download affordance on each finished take");
      t.ok(r.nodes.filter((n) => /^Take:/.test(n.aria)).every((n) => /\((ready|failed|generating)\)/.test(n.aria)),
        "each take node states its status in its accessible name",
        r.nodes.filter((n) => /^Take:/.test(n.aria)).map((n) => n.aria).join(" | "));
      // Source → direction → takes: one edge per parent link, no orphans.
      t.eq(r.edges.length, (st.proposals || []).length + cands.length, "one edge per parent link");
      return `${cands.length} takes, ${r.edges.length} edges`;
    });

    await t.step("controls: a node can be reached and opened from the keyboard", async () => {
      const target = await page.evaluate(() => {
        const ns = Array.from(document.querySelectorAll("#cv-world .cv-node"));
        const last = ns[ns.length - 1];
        return { id: last.dataset.id, tabindex: last.getAttribute("tabindex"), role: last.getAttribute("role") };
      });
      t.eq(target.tabindex, "0", "nodes are focusable");
      t.eq(target.role, "button", "…and announce themselves as buttons");
      await page.evaluate(() => {
        const ns = Array.from(document.querySelectorAll("#cv-world .cv-node"));
        ns[ns.length - 1].focus();
      });
      await page.keyboard.press("Enter");
      await H.sleep(350);
      const focused = await page.$eval("#cv-world .cv-node.is-focus", (n) => n.dataset.id);
      t.eq(focused, target.id, "Enter focuses that node into the dock");
      return target.id;
    });

    await t.step("controls: the dock plays the focused take and its scrub bar seeks", async () => {
      const before = await page.$eval("#cv-dock-time", (n) => n.textContent);
      t.ok(!(await readCanvas(page)).dock.playDisabled, "play is live for a finished take");
      await H.click(page, "#cv-dock-play");
      // Media starts asynchronously (the take is a data: URI that has to decode),
      // so wait for the clock to move rather than for a fixed moment.
      await page.waitForFunction((was) => {
        const el = document.getElementById("cv-dock-time");
        return el && el.textContent !== was;
      }, { timeout: 8000, polling: 200 }, before).catch(() => {});
      const playing = await page.evaluate(() => ({
        time: (document.getElementById("cv-dock-time") || {}).textContent,
        fill: (document.getElementById("cv-dock-fill") || {}).style.width,
        icon: (document.querySelector("#cv-dock-play i") || {}).className,
      }));
      t.ok(playing.time !== before, "the clock advances", `${before} → ${playing.time}`);
      t.ok(parseFloat(playing.fill) > 0, "the progress fill tracks it", playing.fill);
      t.ok(/pause/.test(playing.icon), "the control now offers pause", playing.icon);
      // Seek to 70% of the take.
      await page.evaluate(() => {
        const s = document.getElementById("cv-dock-scrub");
        const r = s.getBoundingClientRect();
        s.dispatchEvent(new MouseEvent("click",
          { clientX: r.left + r.width * 0.7, clientY: r.top + r.height / 2, bubbles: true }));
      });
      await H.sleep(500);
      const seeked = await page.evaluate(() => ({
        time: (document.getElementById("cv-dock-time") || {}).textContent,
        fill: (document.getElementById("cv-dock-fill") || {}).style.width,
      }));
      t.ok(Math.abs(parseFloat(seeked.fill) - 70) < 12, "the scrub bar seeks to where it was clicked", seeked.fill);
      await H.click(page, "#cv-dock-play");
      await H.sleep(300);
      const paused = await page.$eval("#cv-dock-play i", (n) => n.className);
      t.ok(/play/.test(paused), "…and pauses again", paused);
      return seeked.time;
    });

    await t.step("controls: download hands over the focused take's own media", async () => {
      // Count the DELIVERY, not the mechanism. This path used to be a bare
      // window.open — a popup the browser may block, and when it does, nothing
      // happens at all and the click looks ignored. It now goes through the
      // real download anchor, so watch both and assert a file was handed over.
      await page.evaluate(() => {
        window.__specOpened = [];
        window.__specOrigOpen = window.open;
        window.open = (u) => { window.__specOpened.push(String(u)); return null; };
        window.__specOrigClick = HTMLAnchorElement.prototype.click;
        HTMLAnchorElement.prototype.click = function () {
          if (this.hasAttribute("download")) window.__specOpened.push(String(this.href));
          else window.__specOrigClick.call(this);
        };
      });
      const focused = await page.$eval("#cv-world .cv-node.is-focus", (n) => n.dataset.id);
      await page.evaluate((id) => {
        const b = document.querySelector(`#cv-world .cv-node[data-id="${id}"] .cv-thumb__dl`);
        if (b) b.click();
      }, focused);
      await H.sleep(300);
      await H.click(page, "#cv-dock-dl");
      await H.sleep(300);
      const opened = await page.evaluate(() => {
        const o = window.__specOpened.slice();
        window.open = window.__specOrigOpen;
        HTMLAnchorElement.prototype.click = window.__specOrigClick;
        return o;
      });
      const st = await H.state(page);
      const cand = (st.candidates || []).find((c) => c.candidate_id === focused);
      t.eq(opened.length, 2, "both the node and the dock download acted");
      const want = cand.video_url || cand.audio_url;
      // The anchor href is absolute and may carry the page token, so match on
      // the media path rather than on string identity.
      t.ok(opened.every((u) => u && u.indexOf(want.replace(/^https?:\/\/[^/]+/, "")) !== -1),
        "each delivered the focused take's media", opened.map((u) => u.slice(-28)).join(" | "));
      return `delivered ${opened.length}`;
    });

    await t.step("controls: zoom in / out clamp, Fit restores, drag pans", async () => {
      const start = scaleOf((await readCanvas(page)).world);
      await H.click(page, "#cv-vp .cstage__zoom button:nth-child(1)");
      const zin = scaleOf((await readCanvas(page)).world);
      t.ok(zin > start, "zoom in scales up", `${start} → ${zin}`);
      for (let i = 0; i < 12; i++) await H.click(page, "#cv-vp .cstage__zoom button:nth-child(1)", { settle: 40 });
      const max = scaleOf((await readCanvas(page)).world);
      t.ok(max <= 1.6 + 1e-6 && max > zin, "zoom in stops at its ceiling", String(max));
      for (let i = 0; i < 20; i++) await H.click(page, "#cv-vp .cstage__zoom button:nth-child(2)", { settle: 40 });
      const min = scaleOf((await readCanvas(page)).world);
      t.ok(min >= 0.3 - 1e-6 && min < max, "zoom out stops at its floor", String(min));
      await H.click(page, "#cv-vp .cstage__zoom button:nth-child(3)");
      const fitted = scaleOf((await readCanvas(page)).world);
      t.ok(fitted > min, "Fit re-frames the tree", String(fitted));
      const before = (await readCanvas(page)).world;
      const box = await page.$eval("#cv-vp", (n) => {
        const r = n.getBoundingClientRect();
        return { x: r.left + 30, y: r.bottom - 30 };
      });
      await page.mouse.move(box.x, box.y);
      await page.mouse.down();
      await page.mouse.move(box.x + 120, box.y - 60, { steps: 8 });
      await page.mouse.up();
      await H.sleep(250);
      const after = (await readCanvas(page)).world;
      t.ok(after !== before, "dragging empty space pans the world", `${before} → ${after}`);
      await H.click(page, "#cv-vp .cstage__zoom button:nth-child(3)");
      return `scale ${min}…${max}`;
    });

    await t.step("controls: auto-follow is a real toggle, and the legend names the edges", async () => {
      const off = await page.$eval("#cv-follow", (n) => n.getAttribute("aria-pressed"));
      await H.click(page, "#cv-follow");
      const on = await page.$eval("#cv-follow", (n) => ({
        pressed: n.getAttribute("aria-pressed"), cls: n.className,
      }));
      t.eq(off, "false", "auto-follow starts off");
      t.eq(on.pressed, "true", "…and reports itself pressed once on");
      t.ok(/is-on/.test(on.cls), "…with a visible on state", on.cls);
      const legend = (await readCanvas(page)).legend;
      t.ok(/locked path/i.test(legend) && /branch/i.test(legend),
        "the legend explains both edge kinds", legend.replace(/\n/g, " · "));
      return legend.replace(/\n/g, " · ");
    });

    await t.step("lineage: branching draws a child on a branch edge and moves HEAD to it", async () => {
      const st0 = await H.state(page);
      const parent = (st0.candidates || []).find((c) => c.status === "completed");
      await H.click(page, `#cv-world .cv-node[data-id="${parent.candidate_id}"]`);
      await H.sleep(300);
      const before = (st0.candidates || []).length;
      t.must((await H.usable(page, "#cv-dock-branch")).ok, "Branch is usable on a finished take");
      await H.click(page, "#cv-dock-branch");
      await H.sleep(600);
      const body = await H.approveSpend(page);
      t.ok(!!body && /spend/i.test(body), "branching asks for spend approval",
        body ? body.slice(0, 70) : "NO CONFIRM DIALOG");
      await H.waitForIdle(page);
      await H.waitForState(page, `(st) => (st.candidates || []).length > ${before}`, H.TURN_MS, "the branch child");
      await H.sleep(1800);
      const st1 = await H.state(page);
      const child = (st1.candidates || []).find((c) => c.parent_candidate_id === parent.candidate_id);
      t.must(!!child, "the new take records its parent",
        (st1.candidates || []).map((c) => `${c.candidate_id}<${c.parent_candidate_id || "-"}`).join(" "));
      const r = await readCanvas(page);
      t.ok(r.nodes.some((n) => n.id === child.candidate_id), "the child is drawn");
      t.ok(r.edges.some((e) => /dashed/.test(e)), "a branch edge is drawn dashed", r.edges.join(" | "));
      t.ok(r.nodes.find((n) => n.id === child.candidate_id).focus,
        "HEAD advances onto the take that was just branched",
        (r.nodes.find((n) => n.focus) || {}).id);
      return child.candidate_id;
    });

    await t.step("lineage: locking a take marks it and lights its path back to the source", async () => {
      const r0 = await readCanvas(page);
      const focused = (r0.nodes.find((n) => n.focus) || {}).id;
      t.ok(r0.dock.use !== "absent" && /use this/i.test(r0.dock.use.txt),
        "the dock offers to lock the focused take", JSON.stringify(r0.dock.use));
      await H.click(page, "#cv-dock-use");
      await H.sleep(600);
      // Locking runs the mix render server-side; note whether it is gated.
      const gate = await H.approveSpend(page);
      await H.waitForIdle(page);
      await H.sleep(1500);
      const st = await H.state(page);
      t.eq(st.selected_candidate_id, focused, "the locked take is the one the dock had focused");
      const r = await readCanvas(page);
      const node = r.nodes.find((n) => n.id === focused);
      t.ok(node && node.locked, "the node reads as locked");
      t.ok(r.dock.use !== "absent" && r.dock.use.disabled && /locked/i.test(r.dock.use.txt),
        "the dock's primary action becomes an inert 'Locked'", JSON.stringify(r.dock.use));
      t.ok(r.edges.some((e) => /1D9E75/i.test(e)), "the locked path is drawn in the locked colour",
        r.edges.join(" | "));
      return gate ? `locked behind a confirm: ${gate.slice(0, 40)}` : "locked with NO spend confirm";
    });

    await t.step("agreement: the timeline is scoring the take the canvas locked", async () => {
      const canvasTitle = await page.$eval("#cv-dock-title", (n) => n.textContent.trim());
      const st = await H.state(page);
      await H.click(page, TIMELINE);
      await H.sleep(1600);
      const tl = await page.evaluate(() => ({
        title: (document.querySelector(".tl-bar__title") || {}).textContent || "",
        sub: (document.querySelector(".tl-bar__sub") || {}).textContent || "",
        clips: Array.from(document.querySelectorAll(".tl-row.lane-music .tl-clip")).map((c) => ({
          nm: (c.querySelector(".tl-clip__nm") || {}).textContent || "",
          cls: c.className,
        })),
      }));
      t.eq(tl.clips.length, 1, "the music lane draws the one active take");
      t.ok(tl.clips[0].nm.startsWith(canvasTitle),
        "…and it is the take the canvas dock is showing", `${tl.clips[0].nm} vs ${canvasTitle}`);
      t.ok(tl.title === (st.observation || {}).video_title,
        "both views name the same source video", tl.title);
      // The canvas says "Locked" on that take; the timeline must not be silent
      // about which take the mix is committed to.
      t.ok(/is-sel|is-lock/.test(tl.clips[0].cls),
        "the timeline marks the locked take the way the canvas does", tl.clips[0].cls);
      await H.click(page, CANVAS);
      await H.sleep(500);
      return `${tl.title} · ${tl.sub}`;
    });

    // ======================================================================
    // COLLAB — the people around the canvas
    // ======================================================================
    await t.step("people: the topbar shows who is on this session", async () => {
      const faces = await page.$$eval("#clb-faces .clb-av", (ns) => ns.map((n) => ({
        cls: n.className, title: n.title, txt: n.innerText.trim(),
      })));
      t.ok(faces.length >= 1, "a facepile is rendered", String(faces.length));
      t.ok(faces[0] && /you/.test(faces[0].cls), "the viewer comes first", faces[0] && faces[0].cls);
      t.eq(faces[0].title, "Canvas Tester", "…under the persona's own name");
      // This is a roster, not presence — nothing in the build broadcasts who is
      // actually here, so an absent "online" dot is not a regression.
      return faces.map((f) => f.title).join(", ");
    });

    await t.step("share: the sheet offers exactly three roles and mints a link per role", async () => {
      await H.click(page, "#tb-share");
      t.must(await H.waitFor(page, ".clb-share-card", 8000), "the share sheet opened");
      const roles = await page.$$eval(".clb-share-card select.clb-share__linkrole option",
        (ns) => ns.map((n) => n.value));
      t.eq(roles, ["view", "comment", "iterate"], "view / comment / iterate — and no owner option");
      const first = await page.$eval(".clb-share-card .clb-share__link input", (n) => n.value);
      const sid = await page.evaluate(() => window.__edenn.app.sessionId);
      t.ok(first.includes("session=" + sid), "the link carries this session", first);
      t.ok(!/token=/.test(first), "the link never carries an access token", first);
      const seen = {};
      for (const role of ["view", "comment", "iterate"]) {
        await page.select(".clb-share-card select.clb-share__linkrole", role);
        await H.sleep(300);
        seen[role] = await page.$eval(".clb-share-card .clb-share__link input", (n) => n.value);
        t.ok(seen[role].includes("join=" + role), `the ${role} link grants ${role}`, seen[role]);
      }
      t.ok(new Set(Object.values(seen)).size === 3, "each role produces a different link");
      return seen.view;
    });

    await t.step("share: copy reports what it did", async () => {
      await H.click(page, ".clb-share-card .clb-share__link .btn-ghost");
      await H.sleep(400);
      const toast = await page.evaluate(() => {
        const t2 = document.getElementById("toast");
        return { hidden: t2.hidden, txt: t2.innerText.trim() };
      });
      // Headless denies the clipboard, so the promise a user can see is the toast.
      t.ok(!toast.hidden && /link/i.test(toast.txt), "the copy affordance confirms itself", toast.txt);
      return toast.txt;
    });

    await t.step("share: the owner can set a role per collaborator, and grant by id", async () => {
      const people = await page.$$eval(".clb-share-card .clb-person", (ns) => ns.map((n) => ({
        nm: (n.querySelector(".clb-person__nm") || {}).innerText,
        sel: !!n.querySelector("select.clb-person__sel"),
        role: (n.querySelector(".clb-person__role") || {}).innerText || null,
      })));
      t.ok(people.length >= 2, "the roster lists the collaborators", String(people.length));
      t.ok(people[0].sel === false && /owner/i.test(people[0].role || ""),
        "the viewer's own row has no role control", JSON.stringify(people[0]));
      t.ok(people.slice(1).every((p) => p.sel), "every other person has a role control",
        people.map((p) => `${p.nm}:${p.sel}`).join(" "));
      // Demote a collaborator and check the roster keeps the new role.
      await page.select(".clb-share-card .clb-person select.clb-person__sel", "view");
      await H.sleep(600);
      const now = await page.$eval(".clb-share-card .clb-person select.clb-person__sel", (n) => n.value);
      t.eq(now, "view", "a demotion sticks in the sheet");
      // Granting by id: the only invite path this product has.
      const invite = await H.usable(page, ".clb-share-card .clb-share__invite input");
      t.ok(invite.ok, "an invite field is offered to the owner", invite.why || "usable");
      await H.type(page, ".clb-share-card .clb-share__invite input", "reviewer_kim");
      await H.click(page, ".clb-share-card .clb-share__invite .btn-primary");
      await H.sleep(900);
      const after = await page.evaluate(() => ({
        toast: (document.getElementById("toast") || {}).innerText.trim(),
        rows: Array.from(document.querySelectorAll(".clb-share-card .clb-person"))
          .map((n) => (n.querySelector(".clb-person__nm") || {}).innerText),
      }));
      t.ok(after.rows.some((r) => /reviewer_kim/.test(r)), "the new collaborator joins the roster",
        after.rows.join(" | "));
      t.ok(!/couldn't|could not|not defined|error/i.test(after.toast),
        "a successful invite does not report a failure", after.toast);
      return after.rows.join(" | ");
    });

    await t.step("share: the result section offers the finished take, and Done closes the sheet", async () => {
      const res = await page.evaluate(() => {
        const row = document.querySelector(".clb-share-card .clb-share__resrow");
        return row ? { title: (row.querySelector(".clb-share__restitle") || {}).innerText,
                       btns: Array.from(row.querySelectorAll("button")).map((b) => b.innerText.trim()) }
                   : { note: (document.querySelector(".clb-share-card .clb-share__result") || {}).innerText };
      });
      const st = await H.state(page);
      const locked = (st.candidates || []).find((c) => c.candidate_id === st.selected_candidate_id);
      t.ok(res.title && locked && res.title.includes((locked.title || "").trim()),
        "the shareable result names the locked take", JSON.stringify(res));
      t.ok((res.btns || []).length === 2, "it offers a link and a download", (res.btns || []).join(" | "));
      await H.click(page, ".clb-share-card .confirm-card__row .btn-primary");
      await H.sleep(400);
      const open = await page.evaluate(() => {
        const c = document.querySelector(".clb-share-card");
        return c ? !c.closest(".overlay").hidden : false;
      });
      t.ok(!open, "Done closes the sheet");
      return (res.btns || []).join(" | ");
    });

    await t.step("comments: comment mode arms, and a node click opens a thread anchored to it", async () => {
      const chip = await H.usable(page, "#cv-vp .clb-mode");
      t.must(chip.ok, "the comment chip is usable", chip.why || "usable");
      await H.click(page, "#cv-vp .clb-mode");
      const armed = await page.evaluate(() => ({
        pressed: document.querySelector("#cv-vp .clb-mode").getAttribute("aria-pressed"),
        vp: document.getElementById("cv-vp").className,
      }));
      t.eq(armed.pressed, "true", "the chip reports itself armed");
      t.ok(/clb-commenting/.test(armed.vp), "the canvas enters comment mode", armed.vp);
      const focused = await page.$eval("#cv-world .cv-node.is-focus", (n) => n.dataset.id);
      await H.click(page, `#cv-world .cv-node[data-id="${focused}"]`);
      await H.sleep(600);
      const card = await page.evaluate(() => {
        const w = document.querySelector("#cv-vp .clb-cardwrap");
        if (!w || w.hidden) return null;
        return {
          anchor: (w.querySelector(".clb-anchor__label") || {}).innerText || "",
          region: (w.querySelector(".clb-anchor__time") || {}).innerText || "",
          composer: !!w.querySelector(".clb-composer__text"),
          placeholder: (w.querySelector(".clb-composer__text") || {}).placeholder,
        };
      });
      t.must(!!card, "a draft card opened on the clicked node");
      t.ok(card.composer, "…with a composer");
      const dockTitle = await page.$eval("#cv-dock-title", (n) => n.textContent.trim());
      t.eq(card.anchor.trim(), dockTitle, "the thread is anchored to the node that was clicked");
      const off = await page.$eval("#cv-vp .clb-mode", (n) => n.getAttribute("aria-pressed"));
      t.eq(off, "false", "comment mode disarms itself after one thread");
      return `${card.anchor} ${card.region}`;
    });

    await t.step("comments: posting a thread creates a pin on that node", async () => {
      // The open draft already carries a provisional '+' pin, so only posted
      // threads are counted on both sides of the comparison.
      const posted = (r) => r.pins.filter((c) => !/is-new/.test(c)).length;
      const pinsBefore = posted(await readCanvas(page));
      await page.type(".clb-cardwrap .clb-composer__text", "The close lands a beat late for me.");
      await H.sleep(200);
      const send = await H.usable(page, ".clb-cardwrap .clb-send");
      t.ok(send.ok, "the post control is usable once there is text", send.why || "usable");
      await H.click(page, ".clb-cardwrap .clb-send");
      await H.sleep(1200);
      const card = await page.evaluate(() => {
        const w = document.querySelector("#cv-vp .clb-cardwrap");
        return {
          cls: (w.querySelector(".clb-card") || {}).className,
          comments: Array.from(w.querySelectorAll(".clb-cmt")).map((c) =>
            ((c.querySelector(".clb-cmt__body") || {}).innerText || "").trim()),
          reply: !!w.querySelector(".clb-composer.is-compact .clb-composer__text"),
        };
      });
      t.ok(/clb-card--thread/.test(card.cls), "the draft became a posted thread", card.cls);
      t.ok(card.comments.some((c) => /beat late/.test(c)), "the comment is in the thread",
        card.comments.join(" | "));
      t.ok(card.reply, "the thread offers a reply box");
      const pinsAfter = posted(await readCanvas(page));
      t.ok(pinsAfter > pinsBefore, "a pin appeared on the canvas", `${pinsBefore} → ${pinsAfter}`);
      return `${pinsAfter} pins`;
    });

    await t.step("comments: react, edit and delete act on the comment they belong to", async () => {
      // React — the strip is exactly the product's five.
      await H.click(page, ".clb-cardwrap .clb-cmt:last-child .clb-react--add", { force: true });
      await H.sleep(400);
      const strip = await page.$$eval(".clb-cardwrap .clb-react-strip .clb-react-strip__e",
        (ns) => ns.map((n) => n.innerText));
      t.eq(strip.length, 5, "five reactions offered", strip.join(" "));
      await H.click(page, ".clb-cardwrap .clb-react-strip .clb-react-strip__e", { force: true });
      await H.sleep(700);
      const chip = await page.evaluate(() => {
        const c = document.querySelector(".clb-cardwrap .clb-react:not(.clb-react--add)");
        return c ? { txt: c.innerText.replace(/\n/g, " "), on: /is-on/.test(c.className), title: c.title } : null;
      });
      t.ok(!!chip && chip.on, "the reaction is recorded as mine", JSON.stringify(chip));
      // Kebab — Edit/Delete only on my own comment.
      const menus = await page.$$eval(".clb-cardwrap .clb-cmt", (ns) => ns.map((n) => ({
        who: ((n.querySelector(".clb-cmt__who strong") || {}).innerText || "").trim(),
        rows: Array.from(n.querySelectorAll(".clb-kebab .clb-kebab__row")).map((r) => r.innerText.trim()),
      })));
      const mine = menus.filter((m) => m.who === "Canvas Tester");
      t.ok(mine.length && mine.every((m) => m.rows.some((r) => /edit/i.test(r)) && m.rows.some((r) => /delete/i.test(r))),
        "my own comment can be edited and deleted", JSON.stringify(mine));
      // Someone else's comment is checked on a thread that HAS one (the rail step).
      // Edit it.
      await H.click(page, ".clb-cardwrap .clb-cmt:last-child .clb-kebabwrap > .clb-ic", { force: true });
      await H.sleep(300);
      await page.evaluate(() => {
        const rows = Array.from(document.querySelectorAll(".clb-cardwrap .clb-kebab:not([hidden]) .clb-kebab__row"));
        const e = rows.find((r) => /edit/i.test(r.innerText));
        if (e) e.click();
      });
      await H.sleep(400);
      t.must(await H.exists(page, ".clb-cardwrap .clb-edit .clb-edit__text"), "an edit box opened");
      await page.evaluate(() => {
        const box = document.querySelector(".clb-cardwrap .clb-edit .clb-edit__text");
        box.value = "The close lands two beats late for me.";
        box.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await H.click(page, ".clb-cardwrap .clb-edit .clb-btn.primary", { force: true });
      await H.sleep(900);
      const edited = await page.evaluate(() => {
        const c = document.querySelector(".clb-cardwrap .clb-cmt:last-child");
        return { body: (c.querySelector(".clb-cmt__body") || {}).innerText, head: c.innerText };
      });
      t.ok(/two beats late/.test(edited.body), "the edit replaced the body", edited.body);
      t.ok(/edited/i.test(edited.head), "…and the comment says it was edited");
      // Delete it — one click, no confirmation, soft tombstone.
      await H.click(page, ".clb-cardwrap .clb-cmt:last-child .clb-kebabwrap > .clb-ic", { force: true });
      await H.sleep(300);
      await page.evaluate(() => {
        const rows = Array.from(document.querySelectorAll(".clb-cardwrap .clb-kebab:not([hidden]) .clb-kebab__row"));
        const d = rows.find((r) => /delete/i.test(r.innerText));
        if (d) d.click();
      });
      await H.sleep(900);
      const gone = await page.evaluate(() => {
        const c = document.querySelector(".clb-cardwrap .clb-cmt:last-child");
        return { cls: (c.querySelector(".clb-cmt__body") || {}).className,
                 txt: (c.querySelector(".clb-cmt__body") || {}).innerText };
      });
      t.ok(/is-deleted/.test(gone.cls), "the deleted comment leaves an honest tombstone", gone.txt);
      return "reacted, edited, deleted";
    });

    await t.step("comments: resolving collapses the thread and fades its pin; reopening restores it", async () => {
      await H.click(page, ".clb-cardwrap .clb-card--thread .clb-cmt:first-child .clb-cmt__head > .clb-ic", { force: true });
      await H.sleep(900);
      const resolved = await page.evaluate(() => {
        const w = document.querySelector("#cv-vp .clb-cardwrap");
        return {
          cls: (w.querySelector(".clb-card") || {}).className,
          txt: w.innerText.slice(0, 120),
          reopen: !!w.querySelector(".clb-card--resolved .clb-btn.ghost"),
          pins: Array.from(document.querySelectorAll("#cv-world .clb-pin")).map((p) => p.className),
        };
      });
      t.ok(/clb-card--resolved/.test(resolved.cls), "the card collapses to the resolved shape", resolved.cls);
      t.ok(resolved.reopen, "…and offers to reopen");
      t.ok(resolved.pins.some((c) => /is-resolved/.test(c)), "a pin reads as resolved",
        resolved.pins.join(" | "));
      await H.click(page, ".clb-cardwrap .clb-card--resolved .clb-btn.ghost", { force: true });
      await H.sleep(900);
      const back = await page.evaluate(() => ({
        cls: (document.querySelector("#cv-vp .clb-cardwrap .clb-card") || {}).className,
      }));
      t.ok(/clb-card--thread/.test(back.cls), "reopening returns the full thread", back.cls);
      return "resolve ⇄ reopen";
    });

    await t.step("comments: @-mentioning the director hands the note over, cost-gated", async () => {
      const before = ((await H.state(page)).candidates || []).length;
      await page.type(".clb-cardwrap .clb-composer.is-compact .clb-composer__text",
        "Try it warmer through the middle ");
      await H.click(page, ".clb-cardwrap .clb-composer.is-compact .clb-composer__row .clb-tool:nth-of-type(2)",
        { force: true });
      await H.sleep(400);
      const rows = await page.$$eval(".clb-cardwrap .clb-picker .clb-picker__row",
        (ns) => ns.map((n) => n.innerText.replace(/\n/g, " ")));
      t.ok(rows.length >= 2, "the mention picker lists who can be mentioned", rows.join(" | "));
      t.ok(rows.some((r) => /AGENT/.test(r)), "…including the director as an agent", rows.join(" | "));
      const agentIdx = rows.findIndex((r) => /AGENT/.test(r));
      const handles = await page.$$(".clb-cardwrap .clb-picker .clb-picker__row");
      const box = await handles[agentIdx].boundingBox();
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
      await H.sleep(400);
      const text = await page.$eval(".clb-cardwrap .clb-composer.is-compact .clb-composer__text", (n) => n.value);
      t.ok(/@/.test(text), "the mention lands in the note", text);
      await H.click(page, ".clb-cardwrap .clb-composer.is-compact .clb-send", { force: true });
      await H.sleep(800);
      const gate = await page.evaluate(() => {
        const o = document.getElementById("confirm-overlay");
        return o && !o.hidden
          ? { title: (document.getElementById("confirm-title") || {}).textContent,
              body: (document.getElementById("confirm-body") || {}).textContent,
              ok: (document.getElementById("confirm-ok") || {}).textContent.trim() }
          : null;
      });
      t.must(!!gate, "handing work to the director asks before it can spend");
      t.ok(/spend/i.test(gate.body), "the dialog says it may spend", gate.body.slice(0, 80));
      await H.approveSpend(page);
      await H.sleep(3000);
      const thread = await page.evaluate(() => Array.from(
        document.querySelectorAll("#cv-vp .clb-cardwrap .clb-cmt")).map((c) => ({
          agent: /is-agent/.test(c.className),
          body: ((c.querySelector(".clb-cmt__body") || {}).innerText || "").slice(0, 60),
        })));
      t.ok(thread.some((c) => c.agent), "the director answered inside the thread",
        thread.map((c) => (c.agent ? "AGENT:" : "") + c.body).join(" | "));
      await H.waitForState(page, `(st) => (st.candidates || []).length > ${before}`, H.TURN_MS,
        "a take from the handoff").catch(() => {});
      const after = ((await H.state(page)).candidates || []).length;
      t.ok(after > before, "…and the work it promised reached the canvas", `${before} → ${after} takes`);
      return `${before} → ${after} takes`;
    });

    await t.step("comments: the threads rail lists, filters and opens every thread", async () => {
      const railToggle = "#cstage .clb-rail .clb-rail__toggle";
      t.must(await H.exists(page, railToggle), "a threads rail exists on the canvas");
      await H.click(page, railToggle);
      await H.sleep(500);
      const rail = await page.evaluate(() => {
        const r = document.querySelector("#cstage .clb-rail");
        return {
          collapsed: /is-collapsed/.test(r.className),
          count: (r.querySelector(".clb-rail__count") || {}).innerText || "",
          filters: Array.from(r.querySelectorAll(".clb-filter")).map((f) => f.innerText.trim()),
          rows: r.querySelectorAll(".clb-rail__row").length,
          search: !!r.querySelector(".clb-rail__search input"),
        };
      });
      t.ok(!rail.collapsed, "it expands");
      t.eq(rail.filters, ["All", "Unresolved", "@You", "Agents"], "four filters offered");
      t.ok(rail.rows >= 3, "every thread on this session is listed", `${rail.rows} rows`);
      t.ok(rail.search, "the rail can be searched");
      // Filters must actually filter.
      await H.click(page, "#cstage .clb-rail .clb-filter:nth-of-type(4)");
      await H.sleep(400);
      const agents = await page.$$eval("#cstage .clb-rail .clb-rail__row", (ns) => ns.length);
      t.ok(agents >= 1 && agents < rail.rows, "the Agents filter narrows to agent threads",
        `${rail.rows} → ${agents}`);
      await H.click(page, "#cstage .clb-rail .clb-filter:nth-of-type(1)");
      await H.sleep(300);
      // Search must match on body text.
      await H.type(page, "#cstage .clb-rail .clb-rail__search input", "zzz-no-such-comment");
      await H.sleep(400);
      const none = await page.evaluate(() => ({
        rows: document.querySelectorAll("#cstage .clb-rail .clb-rail__row").length,
        empty: (document.querySelector("#cstage .clb-rail .clb-rail__empty") || {}).innerText || "",
      }));
      t.eq(none.rows, 0, "a query that matches nothing hides every row");
      t.ok(none.empty.length > 0, "…and says so", none.empty);
      await page.$eval("#cstage .clb-rail .clb-rail__search input", (n) => {
        n.value = ""; n.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await H.sleep(400);
      // A row opens its thread and focuses the node it is anchored to. Open a
      // colleague's thread specifically — the reviewers' conversation seeded on
      // the first take, which the viewer did not write.
      const idx = await page.evaluate(() => {
        const rows = Array.from(document.querySelectorAll("#cstage .clb-rail .clb-rail__row"));
        const i = rows.findIndex((r) => /quieter|drop at/i.test(r.innerText));
        return i;
      });
      t.must(idx >= 0, "a thread written by someone else is in the rail");
      const rows = await page.$$("#cstage .clb-rail .clb-rail__row");
      await rows[idx].click();
      await H.sleep(900);
      const opened = await page.evaluate(() => {
        const w = document.querySelector("#cv-vp .clb-cardwrap");
        return {
          open: !!w && !w.hidden,
          anchor: (w && (w.querySelector(".clb-anchor__label") || {}).innerText) || "",
          dock: (document.getElementById("cv-dock-title") || {}).textContent.trim(),
          authors: Array.from(document.querySelectorAll("#cv-vp .clb-cardwrap .clb-cmt")).map((c) => ({
            who: ((c.querySelector(".clb-cmt__who strong") || {}).innerText || "").trim(),
            rows: Array.from(c.querySelectorAll(".clb-kebab .clb-kebab__row")).map((r) => r.innerText.trim()),
          })),
        };
      });
      t.ok(opened.open, "clicking a row opens its thread");
      t.ok(opened.anchor && opened.dock.includes(opened.anchor.slice(0, 12)),
        "…and brings the anchored node into the dock", `${opened.anchor} / ${opened.dock}`);
      const theirs = opened.authors.filter((a) => a.who && a.who !== "Canvas Tester");
      t.must(theirs.length > 0, "the thread carries someone else's comments",
        JSON.stringify(opened.authors));
      t.ok(theirs.every((m) => !m.rows.some((r) => /edit|delete/i.test(r))),
        "someone else's comment can be neither edited nor deleted", JSON.stringify(theirs));
      return `${rail.rows} threads`;
    });

    await t.step("comments: an audio attachment on a comment can be played back", async () => {
      const att = await page.evaluate(() => {
        const a = document.querySelector("#cv-vp .clb-cardwrap .clb-att.is-playable");
        return a ? a.innerText.replace(/\n/g, " ") : null;
      });
      t.must(!!att, "the reviewers' thread carries the reference they attached");
      // The player is a detached Audio object — nothing in the DOM to look at —
      // so the constructor is wrapped for the length of this step.
      await page.evaluate(() => {
        window.__specAudio = [];
        window.__specOrigAudio = window.Audio;
        const Orig = window.Audio;
        window.Audio = function (src) { const a = new Orig(src); window.__specAudio.push(a); return a; };
        window.Audio.prototype = Orig.prototype;
      });
      await H.click(page, "#cv-vp .clb-cardwrap .clb-att.is-playable", { force: true });
      await H.sleep(900);
      const playing = await page.evaluate(() => (window.__specAudio || [])
        .some((a) => !a.paused && a.currentTime > 0));
      t.ok(playing, "the attachment plays", att);
      // Clicking the same chip again is the stop control.
      await H.click(page, "#cv-vp .clb-cardwrap .clb-att.is-playable", { force: true });
      await H.sleep(500);
      const stopped = await page.evaluate(() => {
        const all = window.__specAudio || [];
        window.Audio = window.__specOrigAudio;
        return all.every((a) => a.paused);
      });
      t.ok(stopped, "…and clicking it again stops it");
      return att;
    });

    // ======================================================================
    // ROLE — what a viewer is allowed to do
    // ======================================================================
    /* The mock always makes the tester the session creator, so "owner" is the
     * only role a click can reach here. To exercise the gate the viewer is
     * added to the roster at role "view" through the real Share invite path,
     * the session is pointed at another creator, and the app's own public
     * EdennCollab.gateByRole() is re-run — the same call the app makes when a
     * collab payload lands. Only the identity is synthesized; every gate under
     * test is the product's own. */
    const asViewer = async () => page.evaluate(() => {
      window.__edenn.app.snapshot.creator_user_id = "someone_else";
      window.EdennCollab.gateByRole();
      return window.EdennCollab.viewerRole();
    });

    await t.step("role: a view-only collaborator cannot direct the session from chat", async () => {
      const me = await page.evaluate(() => window.__edenn.persona().id);
      await page.evaluate(async (id) => {
        await window.__edenn.app.transport.collab.addParticipant(
          window.__edenn.app.sessionId, { user_id: id, role: "view", display_name: "Viewer" });
      }, me);
      await H.sleep(500);
      const role = await asViewer();
      t.must(role === "view", "the viewer now holds a view-only role", role);
      const chat = await page.evaluate(() => ({
        input: document.getElementById("session-input").disabled,
        send: document.getElementById("session-send").disabled,
        ph: document.getElementById("session-input").placeholder,
      }));
      t.ok(chat.input && chat.send, "the composer is disabled", JSON.stringify(chat));
      t.ok(/access/i.test(chat.ph), "…and says why", chat.ph);
      const chip = await page.evaluate(() => {
        const c = document.querySelector("#cv-vp .clb-mode");
        const d = document.querySelector("#cv-dock .clb-comment-btn");
        return { chip: c ? getComputedStyle(c).display : "absent",
                 dock: d ? getComputedStyle(d).display : "absent" };
      });
      t.eq(chip.chip, "none", "comment mode is withdrawn below comment access");
      t.eq(chip.dock, "none", "…and so is the dock's Comment button");
      return role;
    });

    await t.step("role: a view-only collaborator has no way to spend money", async () => {
      // Clear the thread card and the rail first: a coordinate click that lands
      // on either of them focuses whatever THEY are anchored to, and the dock
      // would then be answering a question nobody asked.
      await page.keyboard.press("Escape");
      await H.sleep(300);
      if (await H.exists(page, "#cstage .clb-rail:not(.is-collapsed) .clb-rail__hd .clb-ic")) {
        await H.click(page, "#cstage .clb-rail:not(.is-collapsed) .clb-rail__hd .clb-ic", { force: true });
        await H.sleep(300);
      }
      await H.click(page, "#cv-vp .cstage__zoom button:nth-child(3)");   // Fit
      await H.sleep(300);
      // Focus a finished take that is NOT the locked one, so the dock shows its
      // full action set to this viewer.
      const st = await H.state(page);
      const other = (st.candidates || []).find((c) =>
        c.status === "completed" && c.candidate_id !== st.selected_candidate_id);
      t.must(!!other, "a second finished take to focus");
      const nodeSel = `#cv-world .cv-node[data-id="${other.candidate_id}"]`;
      t.must((await H.usable(page, nodeSel)).ok, "that take's node is reachable");
      await H.click(page, nodeSel);
      await H.sleep(400);
      const onIt = await page.$eval("#cv-dock-title", (n) => n.textContent.trim());
      t.must(onIt === (other.title || "").trim(),
        "the dock is showing the unlocked take", `${onIt} vs ${other.title}`);
      const role = await asViewer();
      t.eq(role, "view", "still a viewer");
      const dock = await page.evaluate(() => {
        const one = (id) => {
          const n = document.getElementById(id);
          if (!n) return "absent";
          return { txt: n.innerText.trim(), disabled: n.disabled, disp: getComputedStyle(n).display };
        };
        return { use: one("cv-dock-use"), branch: one("cv-dock-branch") };
      });
      const useLive = dock.use !== "absent" && dock.use.disp !== "none" && !dock.use.disabled;
      const branchLive = dock.branch !== "absent" && dock.branch.disp !== "none" && !dock.branch.disabled;
      t.ok(!useLive, "a viewer is not offered 'Use this' — locking runs the mix render",
        JSON.stringify(dock.use));
      t.ok(!branchLive, "a viewer is not offered Branch — branching generates a new take",
        JSON.stringify(dock.branch));
      // If Branch is live, prove what it reaches: the spend dialog itself.
      if (branchLive) {
        await H.click(page, "#cv-dock-branch");
        await H.sleep(700);
        const gate = await page.evaluate(() => {
          const o = document.getElementById("confirm-overlay");
          return o && !o.hidden
            ? (document.getElementById("confirm-title") || {}).textContent + " — " +
              (document.getElementById("confirm-body") || {}).textContent
            : null;
        });
        t.ok(!gate, "…and it does not open a spend confirm for a viewer", gate || "no dialog");
        await H.approveSpend(page, { cancel: true });
      }
      return JSON.stringify(dock);
    });

    await t.step("role: a view-only collaborator cannot write in a thread either", async () => {
      await asViewer();
      // Reach a thread the way a viewer would: the rail, which is not gated.
      t.must(await H.exists(page, "#cstage .clb-rail .clb-rail__toggle"), "the rail is offered to a viewer");
      if (await H.exists(page, "#cstage .clb-rail.is-collapsed .clb-rail__toggle")) {
        await H.click(page, "#cstage .clb-rail.is-collapsed .clb-rail__toggle", { force: true });
        await H.sleep(400);
      }
      const row = await H.usable(page, "#cstage .clb-rail .clb-rail__row");
      t.ok(row.ok, "a viewer can open a thread from the rail", row.why || "usable");
      await H.click(page, "#cstage .clb-rail .clb-rail__row", { force: !row.ok });
      await H.sleep(800);
      await asViewer();
      const card = await page.evaluate(() => {
        const w = document.querySelector("#cv-vp .clb-cardwrap");
        if (!w || w.hidden) return null;
        const live = (n) => !!n && !n.disabled && getComputedStyle(n).display !== "none";
        return {
          reply: live(w.querySelector(".clb-composer__text")),
          send: live(w.querySelector(".clb-send")),
          resolve: live(w.querySelector(".clb-card--thread .clb-cmt:first-child .clb-cmt__head > .clb-ic")),
          react: w.querySelectorAll(".clb-react--add").length,
          kebab: w.querySelectorAll(".clb-kebabwrap > .clb-ic").length,
          inherit: live(w.querySelector(".clb-inherit-link")),
        };
      });
      t.must(!!card, "the thread card opened for the viewer",
        "a read-only viewer reaching the card is expected; what it offers is the question");
      t.ok(!card.reply && !card.send, "no live reply box for a viewer", JSON.stringify(card));
      t.ok(!card.resolve, "a viewer cannot resolve someone else's thread", String(card.resolve));
      t.eq(card.react, 0, "a viewer is not offered reactions");
      t.ok(!card.inherit,
        "a viewer is not offered 'Inherit this take & iterate' — that dialog spends",
        String(card.inherit));
      return JSON.stringify(card);
    });

    await t.step("role: a shared view link opens no directable session for a stranger", async () => {
      // The link the product actually mints, read back off the share sheet.
      await page.evaluate(() => {
        window.__edenn.app.snapshot.creator_user_id = window.__edenn.persona().id;
        window.EdennCollab.gateByRole();
      });
      await H.click(page, "#tb-share", { force: true });
      await H.sleep(500);
      await page.select(".clb-share-card select.clb-share__linkrole", "view");
      await H.sleep(400);
      const link = await page.$eval(".clb-share-card .clb-share__link input", (n) => n.value);
      await H.click(page, ".clb-share-card .confirm-card__row .btn-primary", { force: true });
      t.must(/join=view/.test(link), "the view link is what gets handed out", link);

      // A second browser page IS the second collaborator. It is navigated
      // straight at the mock URL: the harness's blank-slate visit to "/" would
      // be redirected to ?backend=real, and a second page through that redirect
      // never settles under this driver.
      const browser = page.browser();
      const p2 = await browser.newPage();
      const errs = [];
      p2.on("pageerror", (e) => errs.push(String(e && e.message || e)));
      try {
        await p2.goto(H.BASE + "/?backend=mock", { waitUntil: "domcontentloaded", timeout: 30000 });
        await p2.evaluate(() => {
          localStorage.clear();
          localStorage.setItem("edenn.persona",
            JSON.stringify({ id: "guest-viewer2", name: "Viewer Two" }));
        });
        await p2.goto(link, { waitUntil: "domcontentloaded", timeout: 30000 });
        await p2.waitForFunction(() => !!window.__edenn, { timeout: 30000 });
        await H.sleep(3000);
        const seen = await p2.evaluate(() => ({
          session: !(document.getElementById("session") || {}).hidden,
          entrance: !(document.getElementById("entrance") || {}).hidden,
          sid: window.__edenn.app.sessionId,
          persona: window.__edenn.persona(),
          toast: (document.getElementById("toast") || {}).innerText.trim(),
          toastShown: !(document.getElementById("toast") || {}).hidden,
          composer: document.getElementById("session-input")
            ? document.getElementById("session-input").disabled : "absent",
          // "Present in the DOM" proves nothing — the whole session shell is
          // built and hidden. Only a control a person could actually hit counts.
          // (Starting a session of one's own is not this session's spend.)
          spendControls: ["#cv-dock-use", "#cv-dock-branch", ".direction-option .dt-use", ".take .take__use"]
            .filter((s) => {
              const n = document.querySelector(s);
              if (!n || n.disabled || n.closest("[hidden]")) return false;
              const r = n.getBoundingClientRect();
              return r.width > 0 && r.height > 0 && getComputedStyle(n).visibility !== "hidden";
            }),
        }));
        t.eq(seen.persona.id, "guest-viewer2", "the second page is a different person");
        t.eq(seen.spendControls, [], "the stranger reaches no control that spends",
          seen.spendControls.join(","));
        t.ok(seen.composer === true || seen.composer === "absent",
          "…and no live composer", String(seen.composer));
        // The offline mock cannot rehydrate another page's session. What matters
        // is that it says so instead of pretending.
        if (!seen.session) {
          t.ok(seen.toastShown && seen.toast.length > 0,
            "a link that cannot be opened explains itself", seen.toast);
          t.ok(!seen.sid, "no session is adopted", String(seen.sid));
        } else {
          t.ok(false, "the offline mock opened a shared session it cannot have",
            String(seen.sid));
        }
        t.eq(errs, [], "the shared link raises no page error on the second page", errs.join(" | "));
        return seen.session
          ? "session opened"
          : `refused honestly: ${seen.toast.slice(0, 60)}`;
      } finally {
        await p2.close().catch(() => {});
      }
    });

    await t.step("role gating end-to-end could not be driven on the offline backend", async () => {
      // Said out loud rather than asserted: a real second collaborator needs a
      // backend that can serve the same session to two clients. The mock is
      // per-page (its event fan-out reaches only the last socket), so every
      // role check above is the CLIENT gate, exercised through the app's own
      // gateByRole with a synthesized identity. The server-side gate (router.py
      // role minimums) is untested by this suite.
      return "client gate only — see the note in this step";
    });
  },
};
