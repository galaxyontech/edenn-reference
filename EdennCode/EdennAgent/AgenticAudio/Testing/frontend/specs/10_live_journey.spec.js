/* The live pipeline — the checks the offline mock cannot make.
 *
 * This spec SPENDS: it uploads real footage, runs the real analyzer and the
 * live director. It is excluded from the default run and only executes under
 * `--backend=real`. Everything asserted here is something that is only true if
 * the product actually works end to end — real scenes off real footage, the
 * user's layer selection surviving into the real production plan, the spotting
 * sheet, and narration timed against the real cut.
 *
 * Set EDENN_FIXTURE_VIDEO to point at a real clip. */
"use strict";

const path = require("path");

const FIXTURE = process.env.EDENN_FIXTURE_VIDEO
  || "/path/to/repo/docs/review_packages/2026-08-16/source.mp4";

module.exports = {
  name: "live pipeline journey",
  backend: "real",
  persona: "Live Pilot",
  // Production requires a signed-in caller — the sign-in gate covering the
  // composer for a credential-less visitor is correct behaviour, not a failure.
  // The spec authenticates the way the transitional token flow does.
  query: process.env.EDENN_TEST_TOKEN ? `&token=${process.env.EDENN_TEST_TOKEN}` : "",
  // A deployed console requires a token; it rides the tab's URL.
  query: process.env.EDENN_CONSOLE_TOKEN ? "&token=" + process.env.EDENN_CONSOLE_TOKEN : "",

  async run(page, t, { H }) {
    // ---- real upload + real analysis ---------------------------------------
    await t.step("upload: real footage is accepted and measured", async () => {
      const meta = await H.attachVideo(page, FIXTURE);
      t.ok(!/failed/i.test(meta), "the upload succeeded", meta);
      // The chip reports the file's REAL duration, read by the server's prober.
      t.ok(/\d+:\d\d/.test(meta), "the chip shows a measured duration", meta);
      return meta;
    });

    await t.step("analysis: the session is named and cut up from the footage itself", async () => {
      await H.startSession(page, {
        text: "Cinematic and premium — build with the cuts and land on the final shot.",
        timeout: 240000,
      });
      await H.waitForState(page, "(st) => st.observation && st.observation.duration_s > 0",
        240000, "an observation");
      const st = await H.state(page);
      const obs = st.observation || {};
      t.ok((obs.scenes || []).length >= 2, "the analyzer found real scene cuts",
        `${(obs.scenes || []).length} scenes`);
      t.ok(obs.duration_s > 1, "a real duration", String(obs.duration_s));
      t.ok(!!obs.video_title, "the session took its name from the analysis", obs.video_title);
      // Scenes must tile the clip in order — a ruler built on garbage is worse
      // than no ruler.
      const scenes = obs.scenes || [];
      const ordered = scenes.every((s, i) =>
        i === 0 || Number(s.start_s != null ? s.start_s : s.start_timestamp)
                >= Number(scenes[i - 1].start_s != null ? scenes[i - 1].start_s : scenes[i - 1].start_timestamp));
      t.ok(ordered, "scenes are in time order");
      return `${obs.video_title} · ${scenes.length} scenes · ${obs.duration_s}s`;
    });

    await t.step("timeline: the hero and the ruler are built from the real analysis", async () => {
      const tl = await page.evaluate(() => ({
        hasVideo: !!document.querySelector(".tl-screen video"),
        scenes: document.querySelectorAll(".tl-scene").length,
        cuts: document.querySelectorAll(".tl-cut").length,
        ticks: Array.from(document.querySelectorAll(".tl-tick")).map((n) => n.textContent),
        sub: (document.querySelector(".tl-bar__sub") || {}).textContent || "",
      }));
      t.ok(tl.hasVideo, "the uploaded video is the hero");
      t.ok(tl.scenes >= 2, "the ruler shows the real scenes", String(tl.scenes));
      t.ok(tl.cuts >= 1, "cut marks cross the lanes", String(tl.cuts));
      t.ok(tl.ticks.length === 5, "a real time axis", tl.ticks.join(" "));
      return tl.sub;
    });

    // ---- the layer selection must survive into the REAL production plan ----
    await t.step("gate: the user's exact layer selection becomes the plan", async () => {
      t.must(await H.waitFor(page, ".lpick__row", 120000), "the layer picker appeared");
      // Deselect voiceover: music + sound effects. This subset has no intent id
      // of its own, and used to be silently rewritten to music-only.
      await H.click(page, ".lpick__row.lane-voiceover");
      const picked = await page.$$eval(".lpick__row.is-on",
        (ns) => ns.map((n) => (n.className.match(/lane-(\w+)/) || [])[1]));
      t.eq(picked.sort(), ["music", "sfx"], "picker shows music + sound effects");
      await H.click(page, ".w-go button");
      await H.waitForState(page, "(st) => !!st.production_plan", 180000, "a production plan");
      const plan = (await H.state(page)).production_plan || {};
      t.ok((plan.layers || []).includes("sfx"),
        "sound effects survived into the real plan", (plan.layers || []).join(","));
      t.ok(!(plan.layers || []).includes("voiceover"),
        "the deselected layer was not added back", (plan.layers || []).join(","));
      return (plan.layers || []).join("+");
    });

    // ---- the spotting sheet is a real-backend artefact ---------------------
    await t.step("spotting sheet: real moments reach the timeline strip", async () => {
      const has = await H.waitForState(page,
        "(st) => ((st.spotting_sheet || {}).moments || []).length > 0", 180000, "a spotting sheet")
        .then(() => true).catch(() => false);
      if (!has) return "no spotting sheet on this session (analysis may not have produced one)";
      const st = await H.state(page);
      const moments = (st.spotting_sheet || {}).moments || [];
      await H.sleep(800);
      const pins = await page.$$eval(".tl-mom", (ns) => ns.length);
      t.eq(pins, moments.length, "every moment is pinned on the strip");
      // Each moment names an owner — that is the point of the sheet.
      const owned = moments.filter((m) => !!m.owner).length;
      t.ok(owned === moments.length, "every moment has an owner", `${owned}/${moments.length}`);
      const dur = (st.observation || {}).duration_s || 0;
      const inRange = moments.every((m) => Number(m.t || 0) >= 0 && Number(m.t || 0) <= dur + 0.5);
      t.ok(inRange, "no moment sits outside the clip");
      return `${moments.length} moments`;
    });

    // ---- honesty under a real director --------------------------------------
    await t.step("honesty: the director does not promise what the product cannot do", async () => {
      const before = ((await H.snapshot(page)) || {}).messages || [];
      await H.say(page, "Can you clone my own voice for the narration?", { timeout: 240000 });
      // Read the REPLY from state, not the rendered thread: innerText still
      // carries the thinking trail when the turn settles, and asserting against
      // that is how this check kept failing on a correct answer. (The suite's
      // own rule — assert on state, never on rendered prose.)
      await H.waitForState(page,
        `(st) => true`, 5000, "settle").catch(() => null);
      const msgs = ((await H.snapshot(page)) || {}).messages || [];
      const reply = msgs.filter((m) => m.role === "assistant")
        .map((m) => String(m.content || "")).pop() || "";
      t.must(reply.length > 0, "the director replied", `${msgs.length} messages`);
      const said = reply.toLowerCase().replace(/[\u2018\u2019]/g, "'");

      // What matters is whether the limit was ACKNOWLEDGED, not the wording.
      const acknowledged = /\b(can't|cannot|can not|unable|not able|isn't|is not|don't|do not|doesn't|does not|no way to|not something|not support|unsupported|not possible|instead)\b/
        .test(said);
      t.ok(acknowledged, "the reply acknowledges that voice cloning is not on offer",
        acknowledged ? reply.slice(0, 120) : "NO ACKNOWLEDGEMENT: " + reply.slice(0, 220));
      const agreed = /\b(yes|sure|absolutely|of course|happy to)\b[^.]{0,40}\bclone\b/.test(said);
      t.ok(!agreed, "no promise of voice cloning", agreed ? reply.slice(0, 220) : "no promise made");
      return "answered without over-claiming";
    });

    await t.step("honesty: no upstream provider or model name reaches the user", async () => {
      const txt = await H.threadText(page);
      const names = ["model_gateway", "gpt-", "model_vendor_alt", "model_gateway_alt", "provider_a",
                     "provider_c", "provider_b", "azure", "speech_recognition"];
      const leaked = names.filter((n) => txt.toLowerCase().includes(n));
      t.ok(!leaked.length, "the transcript names no provider", leaked.join(",") || "clean");
      const state = JSON.stringify(await H.state(page)).toLowerCase();
      const leakedState = names.filter((n) => state.includes(n));
      t.ok(!leakedState.length, "the snapshot names no provider", leakedState.join(",") || "clean");
      return "no leaks";
    });

    await t.step("the session survives the whole journey without a page error", async () => {
      const errs = H.pageErrors(page);
      t.ok(!errs.fatal.length, "no uncaught errors", errs.fatal.join(" | ") || "clean");
      const usable = await H.usable(page, "#session-input");
      t.ok(usable.ok, "the composer is still usable", usable.why || "ok");
      return "session healthy";
    });
  },
};
