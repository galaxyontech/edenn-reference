/* Entrance: every control a person can touch before a session exists. */
"use strict";

module.exports = {
  name: "entrance controls",
  backend: "mock",
  persona: "Test Pilot",

  async run(page, t, { H }) {
    // ---- sidebar navigation -------------------------------------------------
    // The library page took over what the entrance panes used to do: sessions
    // and gallery now open it OVER the entrance, and New session closes it.
    // The panes still exist, hidden, because their wiring still binds.
    await t.step("nav: New session is where a visitor lands", async () => {
      const entrance = await page.$eval("#entrance", (n) => !n.hidden);
      const library = await page.$eval("#library-page", (n) => !n.hidden);
      const active = await page.$eval("#nav-new", (n) => n.classList.contains("is-active"));
      t.ok(entrance, "the composer is on screen");
      t.ok(!library, "the library is not covering it");
      return entrance && active ? "composer active" : `entrance=${entrance} active=${active}`;
    });

    for (const [btn, heading, label] of [
      ["#nav-sessions", /my sessions/i, "My sessions"],
      ["#nav-gallery", /gallery/i, "Gallery"],
    ]) {
      await t.step(`nav: ${label} opens the library`, async () => {
        await H.click(page, btn);
        await H.sleep(300);
        const shown = await page.$eval("#library-page", (n) => !n.hidden);
        const text = await page.$eval("#library-page", (n) => n.innerText.trim());
        t.ok(shown, `${label}: the library is visible`);
        t.ok(heading.test(text), `${label}: it opened on the right view`,
          text.split("\n")[0] || "(empty)");
        return text.split("\n")[0];
      });
    }

    await t.step("nav: New session closes the library again", async () => {
      await H.click(page, "#nav-new");
      await H.sleep(300);
      const library = await page.$eval("#library-page", (n) => !n.hidden);
      const entrance = await page.$eval("#entrance", (n) => !n.hidden);
      t.ok(!library, "the library closed");
      t.ok(entrance, "the composer is back");
      return !library && entrance ? "back at the composer" : `library=${library} entrance=${entrance}`;
    });

    // ---- library empty states ----------------------------------------------
    await t.step("sessions: the list says what is there, honestly", async () => {
      await H.click(page, "#nav-sessions");
      await H.sleep(600);
      const txt = await page.$eval("#library-page", (n) => n.innerText);
      // Either a list, an empty state, or a load in progress — never a blank
      // panel, which is what a dead view looks like to a user.
      return /session|nothing here yet|loading/i.test(txt)
        ? "the list explains itself" : `unexpected: ${txt.slice(0, 80)}`;
    });
    await t.step("gallery: the empty state is honest", async () => {
      await H.click(page, "#nav-gallery");
      await H.sleep(600);
      const txt = await page.$eval("#library-page", (n) => n.innerText);
      return /gallery|no finished mixes|lock a take|loading/i.test(txt)
        ? "empty state present" : txt.slice(0, 80);
    });

    // ---- documentation overlay ---------------------------------------------
    await t.step("docs: opens, describes the CURRENT shell, closes", async () => {
      await H.click(page, "#nav-docs");
      const open = await page.$eval("#docs-overlay", (n) => !n.hidden);
      t.must(open, "docs overlay opens");
      const body = await page.$eval(".docs-body", (n) => n.innerText);
      // The port moved the toggle into the right pane; docs must not still be
      // telling people to look in the topbar for a Chat/Canvas switch.
      t.ok(/timeline/i.test(body), "docs mention the Timeline view", "");
      t.ok(!/topbar toggle/i.test(body), "docs do not describe the old topbar toggle");
      await H.click(page, "#docs-close");
      const closed = await page.$eval("#docs-overlay", (n) => n.hidden);
      return closed ? "opened and closed" : "did not close";
    });

    // ---- settings -----------------------------------------------------------
    await t.step("settings: opens from the sidebar gear", async () => {
      await H.click(page, "#side-settings");
      const open = await page.$eval("#settings-overlay", (n) => !n.hidden);
      t.must(open, "settings overlay opens");
      await H.click(page, "#settings-cancel");
      return await page.$eval("#settings-overlay", (n) => n.hidden) ? "closed" : "stuck open";
    });

    await t.step("settings: opens from the account chip", async () => {
      await H.click(page, "#account-btn");
      const open = await page.$eval("#settings-overlay", (n) => !n.hidden);
      t.must(open, "account chip opens settings");
      return "opened";
    });

    await t.step("settings: renaming updates the chip and persists", async () => {
      await H.type(page, "#set-name", "Renamed Pilot");
      await H.click(page, "#settings-save");
      await H.sleep(300);
      const chip = await page.$eval(".account__name", (n) => n.textContent.trim());
      const stored = await page.evaluate(() => localStorage.getItem("edenn.persona"));
      t.eq(chip, "Renamed Pilot", "account chip shows the new name");
      return /Renamed Pilot/.test(stored || "") ? "persisted to localStorage" : "not persisted";
    });

    await t.step("settings: identity id survives a rename", async () => {
      const before = await page.evaluate(() => JSON.parse(localStorage.getItem("edenn.persona")).id);
      await H.click(page, "#account-btn");
      await H.type(page, "#set-name", "Third Name");
      await H.click(page, "#settings-save");
      await H.sleep(250);
      const after = await page.evaluate(() => JSON.parse(localStorage.getItem("edenn.persona")).id);
      // Identity must be stable or every past comment loses its author.
      return before === after ? `id stable (${after})` : `id CHANGED ${before} -> ${after}`;
    });

    await t.step("settings: backend segment reflects the active backend", async () => {
      await H.click(page, "#account-btn");
      const on = await page.$eval("#set-backend .is-on, #set-backend [aria-pressed='true']",
        (n) => n.getAttribute("data-b")).catch(() => null);
      const kind = await page.evaluate(() => window.__edenn.app.transport.kind);
      await H.click(page, "#settings-cancel");
      return on === kind ? `both say ${kind}` : `segment=${on} transport=${kind}`;
    });

    // ---- composer -----------------------------------------------------------
    await t.step("composer: starter chips fill the direction", async () => {
      await H.click(page, "#nav-new");
      const chips = await page.$$eval("#starters .chip[data-fill]",
        (ns) => ns.map((n) => ({ text: n.innerText.trim(), fill: n.getAttribute("data-fill") })));
      t.ok(chips.length >= 5, "starter chips present", `${chips.length} chips`);
      let allFilled = true;
      for (const c of chips) {
        await page.evaluate((f) => document.querySelector(`#starters .chip[data-fill="${f}"]`).click(), c.fill);
        await H.sleep(60);
        const v = await page.$eval("#start-text", (n) => n.value);
        if (v !== c.fill) { allFilled = false; t.ok(false, `chip "${c.text}" fills the box`, `got "${v.slice(0, 40)}"`); }
      }
      return allFilled ? `all ${chips.length} chips fill correctly` : false;
    });

    await t.step("composer: Surprise me writes a direction", async () => {
      await page.$eval("#start-text", (n) => { n.value = ""; });
      await H.click(page, "#surprise-btn");
      const v = await page.$eval("#start-text", (n) => n.value.trim());
      return v.length > 10 ? `"${v.slice(0, 42)}…"` : `too short: "${v}"`;
    });

    await t.step("composer: attach button opens the file picker path", async () => {
      // The visible button must be wired to the hidden input; clicking it in a
      // headless browser cannot open a real picker, so assert the wiring.
      const wired = await page.evaluate(() => {
        const btn = document.getElementById("attach-btn");
        const input = document.getElementById("video-file");
        if (!btn || !input) return "missing element";
        let clicked = false;
        const orig = input.click.bind(input);
        input.click = () => { clicked = true; };
        btn.click();
        input.click = orig;
        return clicked ? "ok" : "attach button does not trigger the input";
      });
      return wired === "ok" ? "attach → file input" : wired;
    });

    await t.step("composer: mic button is present and does not throw", async () => {
      const before = (H.pageErrors(page).fatal || []).length;
      await H.click(page, "#mic-btn");
      await H.sleep(300);
      await H.click(page, "#mic-btn", { force: true });   // toggle back off
      const after = (H.pageErrors(page).fatal || []).length;
      return after === before ? "no error from mic toggle" : "mic toggle threw";
    });

    await t.step("composer: Start with no video is handled honestly", async () => {
      await page.$eval("#start-text", (n) => { n.value = "just a direction, no video"; });
      await H.click(page, "#start-btn");
      await H.sleep(1200);
      // Either it starts a session (mock seeds a sample) or it explains itself.
      const started = await page.$eval("#session", (n) => !n.hidden);
      const toast = await page.$eval("#toast", (n) => n.hidden ? "" : n.textContent);
      return started ? "session started (mock sample)" : (toast ? `refused: ${toast.slice(0, 60)}` : "nothing happened");
    });
  },
};
