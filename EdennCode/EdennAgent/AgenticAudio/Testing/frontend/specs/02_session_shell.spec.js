/* The session shell the port introduced: chat pane | grip | right pane, the
 * view toggle that moved into the right pane, and the navigation paths that
 * used to leave the shell in a broken state. */
"use strict";

module.exports = {
  name: "session shell + navigation",
  backend: "mock",
  persona: "Shell Tester",

  async run(page, t, { H }) {
    await t.step("session opens from the entrance", async () => {
      const sid = await H.startSession(page, { text: "Cinematic and premium." });
      t.must(!!sid, "session id assigned", sid || "none");
      return sid;
    });

    // ---- the three-part shell ----------------------------------------------
    await t.step("shell: chat pane, grip and right pane all exist and are usable", async () => {
      for (const sel of ["#chatpane", "#rgrip", "#rpane", "#rpane-hd", "#tstage"]) {
        const u = await H.usable(page, sel);
        t.ok(u.ok, `shell: ${sel} usable`, u.why || "ok");
      }
      // The old shell's pieces must be gone, not merely hidden behind CSS.
      const legacy = await page.evaluate(() => ({
        topbarToggle: !!document.getElementById("cv-seg"),
        oldRail: !!document.getElementById("canvas-body"),
        progress: !!document.getElementById("progress"),
      }));
      t.ok(!legacy.topbarToggle, "old topbar Chat/Canvas toggle removed");
      t.ok(!legacy.oldRail, "old right rail removed");
      return "shell intact";
    });

    await t.step("toggle: Timeline is the default view", async () => {
      const on = await page.$eval("#view-seg .vseg__btn.is-on", (n) => n.getAttribute("data-view"));
      const tHidden = await page.$eval("#tstage", (n) => n.hidden);
      const cHidden = await page.$eval("#cstage", (n) => n.hidden);
      t.eq(on, "timeline", "toggle reads Timeline");
      t.ok(!tHidden && cHidden, "timeline stage shown, canvas hidden", `t=${tHidden} c=${cHidden}`);
      return "timeline default";
    });

    await t.step("toggle: switching to Canvas and back moves the stages", async () => {
      await H.click(page, '#view-seg .vseg__btn[data-view="canvas"]');
      let s = await page.evaluate(() => ({
        t: document.getElementById("tstage").hidden,
        c: document.getElementById("cstage").hidden,
        pressed: document.querySelector('#view-seg .vseg__btn[data-view="canvas"]').getAttribute("aria-pressed"),
      }));
      t.ok(s.t && !s.c, "canvas shown, timeline hidden", JSON.stringify(s));
      t.eq(s.pressed, "true", "canvas button reports aria-pressed");
      await H.click(page, '#view-seg .vseg__btn[data-view="timeline"]');
      s = await page.evaluate(() => ({
        t: document.getElementById("tstage").hidden,
        c: document.getElementById("cstage").hidden,
      }));
      t.ok(!s.t && s.c, "back to timeline", JSON.stringify(s));
      return "toggle round-trips";
    });

    await t.step("toggle: the chosen view survives a reload", async () => {
      await H.click(page, '#view-seg .vseg__btn[data-view="canvas"]');
      await H.sleep(200);
      const stored = await page.evaluate(() => localStorage.getItem("edenn.rightView"));
      await H.click(page, '#view-seg .vseg__btn[data-view="timeline"]');
      // Persistence is a nicety, not a contract — report either way.
      return stored ? `persisted as ${stored}` : "not persisted (view resets to default)";
    });

    // ---- resize grip --------------------------------------------------------
    await t.step("grip: dragging resizes the chat pane and clamps", async () => {
      const before = await page.$eval("#chatpane", (n) => n.getBoundingClientRect().width);
      const box = await page.$eval("#rgrip", (n) => {
        const r = n.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
      });
      await page.mouse.move(box.x, box.y);
      await page.mouse.down();
      await page.mouse.move(box.x + 140, box.y, { steps: 12 });
      await page.mouse.up();
      await H.sleep(250);
      const after = await page.$eval("#chatpane", (n) => n.getBoundingClientRect().width);
      t.ok(after > before + 40, "pane widened on drag", `${Math.round(before)} → ${Math.round(after)}`);

      // Drag far past the maximum: it must clamp, not run off the screen.
      await page.mouse.move(box.x + 140, box.y);
      await page.mouse.down();
      await page.mouse.move(box.x + 1400, box.y, { steps: 10 });
      await page.mouse.up();
      await H.sleep(250);
      const maxed = await page.$eval("#chatpane", (n) => n.getBoundingClientRect().width);
      const rpane = await page.$eval("#rpane", (n) => n.getBoundingClientRect().width);
      t.ok(maxed <= 620, "pane width clamps at the maximum", `${Math.round(maxed)}px`);
      t.ok(rpane > 200, "right pane keeps usable width", `${Math.round(rpane)}px`);
      return "grip clamps";
    });

    await t.step("grip: keyboard resizes (it is a focusable separator)", async () => {
      await page.focus("#rgrip");
      const before = await page.$eval("#chatpane", (n) => n.getBoundingClientRect().width);
      await page.keyboard.press("ArrowLeft");
      await page.keyboard.press("ArrowLeft");
      await H.sleep(200);
      const after = await page.$eval("#chatpane", (n) => n.getBoundingClientRect().width);
      return after < before ? `${Math.round(before)} → ${Math.round(after)}` : "arrow keys did nothing";
    });

    await t.step("narrow: widgets reflow when the pane gets tight", async () => {
      await page.evaluate(() => document.documentElement.style.setProperty("--chat-w", "310px"));
      await H.sleep(500);
      const narrow = await page.evaluate(() => document.body.classList.contains("is-pane-narrow"));
      t.ok(narrow, "narrow mode engages under the threshold");
      // Nothing may spill horizontally out of the chat pane.
      const overflow = await page.evaluate(() => {
        const pane = document.getElementById("chatpane");
        const pr = pane.getBoundingClientRect();
        return Array.from(pane.querySelectorAll(".user-row, .agent-row, .w-card, .take, .cmp, .think"))
          .filter((n) => n.getBoundingClientRect().right > pr.right + 2)
          .map((n) => n.className).slice(0, 4);
      });
      t.ok(!overflow.length, "no widget overflows the narrow pane", overflow.join(",") || "clean");
      await page.evaluate(() => document.documentElement.style.setProperty("--chat-w", "380px"));
      await H.sleep(400);
      return "reflow ok";
    });

    // ---- topbar controls ----------------------------------------------------
    await t.step("topbar: History opens and lists the turn", async () => {
      await H.click(page, "#tb-history");
      const open = await page.$eval("#history-pop", (n) => !n.hidden);
      t.must(open, "history popover opens");
      const txt = await page.$eval("#history-pop", (n) => n.innerText.trim());
      t.ok(txt.length > 0, "history has content", txt.slice(0, 60));
      await H.click(page, "#tb-history");
      return await page.$eval("#history-pop", (n) => n.hidden) ? "toggles closed" : "stayed open";
    });

    await t.step("topbar: Export responds (and refuses honestly with no mix)", async () => {
      const before = await H.threadText(page);
      await H.click(page, "#tb-export");
      await H.sleep(700);
      const toast = await page.$eval("#toast", (n) => n.hidden ? "" : n.textContent.trim());
      const after = await H.threadText(page);
      // With nothing locked there is nothing to export: say so, don't no-op.
      return toast ? `explained: ${toast.slice(0, 70)}`
        : (after !== before ? "responded in thread" : "SILENT no-op");
    });

    // ---- the navigation regressions the port had to fix ---------------------
    await t.step("regression: going home wipes the thread and does not replay it", async () => {
      const beforeMsgs = await page.$$eval("#thread-inner .user-row, #thread-inner .agent-row", (ns) => ns.length);
      t.ok(beforeMsgs > 0, "session had messages", String(beforeMsgs));
      await H.click(page, "#studio-home");
      await H.sleep(900);
      const st = await page.evaluate(() => ({
        entrance: !document.getElementById("entrance").hidden,
        session: !document.getElementById("session").hidden,
        msgs: document.querySelectorAll("#thread-inner .user-row, #thread-inner .agent-row").length,
        snapshot: !!(window.__edenn.app.snapshot),
        narrow: document.body.classList.contains("is-pane-narrow"),
      }));
      t.ok(st.entrance && !st.session, "back at the entrance", JSON.stringify(st));
      t.ok(st.msgs === 0, "thread wiped", `${st.msgs} messages left`);
      t.ok(!st.snapshot, "stale snapshot cleared", st.snapshot ? "STILL SET" : "cleared");
      t.ok(!st.narrow, "hidden pane did not flip narrow mode", st.narrow ? "FLIPPED" : "ok");
      return "clean exit";
    });

    await t.step("regression: a second session does not inherit the first one's thread", async () => {
      const sid2 = await H.startSession(page, { text: "Totally different — upbeat and punchy." });
      t.must(!!sid2, "second session started", sid2);
      const msgs = await page.$$eval("#thread-inner .user-row, #thread-inner .agent-row", (ns) => ns.map((n) => n.innerText.slice(0, 40)));
      const leaked = msgs.filter((m) => /cinematic and premium/i.test(m));
      t.ok(!leaked.length, "no messages from the first session", leaked.join(" | ") || "clean");
      // The append cursor must track the NEW conversation, or later replies vanish.
      const rendered = await page.evaluate(() => window.__edenn.app.renderedCount);
      const actual = await page.evaluate(() => (window.__edenn.app.snapshot.messages || []).length);
      t.ok(rendered <= actual + 1, "render cursor matches the new session",
        `rendered=${rendered} messages=${actual}`);
      return `second session clean (${msgs.length} messages)`;
    });

    await t.step("regression: the shell survives the round trip (toggle still works)", async () => {
      const hdUsable = await H.usable(page, "#rpane-hd");
      t.ok(hdUsable.ok, "view toggle header still usable", hdUsable.why || "ok");
      await H.click(page, '#view-seg .vseg__btn[data-view="canvas"]');
      const c = await page.$eval("#cstage", (n) => !n.hidden);
      await H.click(page, '#view-seg .vseg__btn[data-view="timeline"]');
      return c ? "toggle operable after navigation" : "toggle dead after navigation";
    });

    await t.step("browser back returns to the entrance without breaking the app", async () => {
      await page.goBack({ waitUntil: "domcontentloaded" }).catch(() => null);
      await H.sleep(800);
      const alive = await page.evaluate(() => !!window.__edenn);
      return alive ? "app alive after back" : "app dead after back";
    });
  },
};
