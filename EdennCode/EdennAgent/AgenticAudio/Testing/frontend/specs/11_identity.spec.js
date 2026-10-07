/* Who the console thinks you are.
 *
 * It used to invent a person in localStorage and get on with it, so the same
 * human was a different identity on every device and nobody could come back to
 * their own work. The server decides now; this checks the console actually
 * defers to it, and that a signed-out visitor is offered a way in rather than
 * quietly handed a guest account.
 *
 * Needs a backend that REQUIRES auth:
 *   AGENTIC_AUDIO_REQUIRE_AUTH=1 AGENTIC_AUDIO_API_KEYS=edenn-test-owner:test_owner \
 *     .venv/bin/python EdennCode/EdennAgent/AgenticAudio/design/devserver.py
 */
"use strict";

const TOKEN = process.env.EDENN_TEST_TOKEN || "edenn-test-owner";

module.exports = {
  name: "identity and sign-in",
  backend: "real",
  persona: "Identity Tester",

  async run(page, t, { H }) {
    // ---- the server is the one being asked -------------------------------
    await t.step("the console asks the server who it is", async () => {
      const cfg = await page.evaluate(async () =>
        (await fetch("/api/v2/agentic/audio/auth/config")).json());
      t.ok(cfg.auth_required === true,
        "this backend requires auth (otherwise this spec proves nothing)",
        JSON.stringify(cfg));
      return `configured=${cfg.configured} legacy=${cfg.legacy_tokens_accepted}`;
    });

    await t.step("the sign-in config needs no credential", async () => {
      // Whoever is loading the sign-in page has none by definition.
      const status = await page.evaluate(async () =>
        (await fetch("/api/v2/agentic/audio/auth/config")).status);
      return status === 200 ? "public" : `returned ${status}`;
    });

    // ---- signed out -------------------------------------------------------
    await t.step("a signed-out visitor is offered a way in", async () => {
      await page.goto(H.BASE + "/?backend=real", { waitUntil: "networkidle2" });
      await page.waitForFunction(() => !!window.__edenn, { timeout: 30000 });
      await H.sleep(1200);
      const state = await page.evaluate(() => ({
        gate: !document.getElementById("signin-overlay").hidden,
        text: document.getElementById("signin-body").innerText.trim(),
        me: window.__edenn.app.me,
      }));
      t.ok(state.gate, "the sign-in gate is shown");
      t.ok(state.me && state.me.authenticated === false,
        "the console does not believe it is signed in");
      t.ok(state.text.length > 0, "the gate explains itself", state.text.slice(0, 70));
      return "gate shown";
    });

    await t.step("a signed-out visitor is not handed an invented identity", async () => {
      /* The old behaviour: a localStorage guest, silently, so the console
         looked signed in and none of the work could ever be found again. */
      const invented = await page.evaluate(() => {
        const me = window.__edenn.app.me || {};
        return me.authenticated === true;
      });
      return invented ? "STILL INVENTS AN IDENTITY" : "no invented identity";
    });

    // ---- signed in --------------------------------------------------------
    await t.step("a credentialed visitor is not asked to sign in", async () => {
      await page.goto(`${H.BASE}/?backend=real&token=${TOKEN}`, { waitUntil: "networkidle2" });
      await page.waitForFunction(() => !!window.__edenn, { timeout: 30000 });
      await H.sleep(1200);
      const state = await page.evaluate(() => ({
        gate: !document.getElementById("signin-overlay").hidden,
        me: window.__edenn.app.me,
      }));
      t.ok(!state.gate, "no sign-in gate");
      t.ok(state.me && state.me.authenticated === true, "the server confirmed the caller");
      return `signed in as ${state.me.user_id}`;
    });

    await t.step("the identity shown is the server's, not a local invention", async () => {
      const shown = await page.evaluate(() => ({
        chip: document.querySelector(".account__name").textContent.trim(),
        userId: window.__edenn.app.me.user_id,
        persona: window.__edenn.persona(),
      }));
      t.ok(shown.persona.id === shown.userId,
        "the persona id is the server's principal", `${shown.persona.id} vs ${shown.userId}`);
      return `${shown.chip} (${shown.userId})`;
    });

    await t.step("the auth source is reported, so a legacy token is visible", async () => {
      /* Static tokens are deprecated; being able to see who is still on one is
         how the migration ever finishes. */
      const source = await page.evaluate(() => window.__edenn.app.me.auth_source);
      return source ? `auth_source=${source}` : "auth source not reported";
    });

    // ---- the name follows the account ------------------------------------
    await t.step("setting a name writes it to the account", async () => {
      const name = "Renamed " + Date.now().toString().slice(-4);
      await page.evaluate(async (n) => {
        await window.__edenn.app.transport.setProfile(n);
      }, name);
      const stored = await page.evaluate(async () =>
        (await window.__edenn.app.transport.whoami()).display_name);
      t.ok(stored === name, "the server kept the new name", `${stored}`);

      // …and it is still there on a fresh load, which is the whole point of an
      // account rather than a localStorage label.
      await page.goto(`${H.BASE}/?backend=real&token=${TOKEN}`, { waitUntil: "networkidle2" });
      await page.waitForFunction(() => !!window.__edenn, { timeout: 30000 });
      await H.sleep(1000);
      const after = await page.evaluate(() => window.__edenn.app.me.display_name);
      t.ok(after === name, "the name survived a reload", after);
      return name;
    });
  },
};
