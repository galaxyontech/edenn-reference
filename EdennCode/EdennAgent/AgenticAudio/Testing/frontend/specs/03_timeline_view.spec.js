/* The timeline view, state by state.
 *
 * The pipeline cannot reach every state on demand (a probe failure, a dead
 * video URL, a session with no duration yet), so most checks here drive the
 * module's own public entry point — EdennTimeline.render(snapshot) — with a
 * synthesized snapshot. That is the same call app.js makes on every poll, so
 * the rendering path under test is the real one; only the clock is ours.
 *
 * The honesty rule this view is built on: a length is MEASURED or it is absent.
 * A test that accepts a plausible-looking number here defeats the point. */
"use strict";

/** A silent WAV data URI of `sec` seconds — a probe-able media source. */
const WAV = (sec) => `(() => {
  const rate = 8000, n = rate * ${sec}, dataLen = n * 2;
  const buf = new ArrayBuffer(44 + dataLen), v = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + dataLen, true); w(8, "WAVEfmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data");
  v.setUint32(40, dataLen, true);
  let s = ""; const b = new Uint8Array(buf);
  for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
  return "data:audio/wav;base64," + btoa(s);
})()`;

/** Render a synthesized snapshot through the real module and settle. */
async function renderState(page, stateObj, waitMs) {
  await page.evaluate((st, wavSrc) => {
    const wav = eval(wavSrc);
    // Any string "__WAV__" in the fixture becomes a real, probe-able audio URL.
    const hydrate = (o) => {
      if (typeof o === "string") return o === "__WAV__" ? wav : o;
      if (Array.isArray(o)) return o.map(hydrate);
      if (o && typeof o === "object") {
        const r = {}; for (const k of Object.keys(o)) r[k] = hydrate(o[k]); return r;
      }
      return o;
    };
    const snap = { session_id: "spec_synth", messages: [], state: hydrate(st) };
    window.__specSnap = snap;
    window.EdennTimeline.render(snap);
  }, stateObj, WAV(11));
  await new Promise((r) => setTimeout(r, waitMs || 500));
}

const readTimeline = (page) => page.evaluate(() => {
  const q = (s) => document.querySelector(s);
  const all = (s) => Array.from(document.querySelectorAll(s));
  return {
    note: (q(".tl-tl__note") || {}).textContent || "",
    barSub: (q(".tl-bar__sub") || {}).textContent || "",
    barTitle: (q(".tl-bar__title") || {}).textContent || "",
    clock: (q(".tl-clock") || {}).textContent || "",
    screenEmpty: (q(".tl-screen__empty") || {}).innerText || "",
    hasVideo: !!q(".tl-screen video"),
    scenes: all(".tl-scene").map((n) => n.textContent.trim()),
    cuts: all(".tl-cut").length,
    moments: all(".tl-mom").length,
    ticks: all(".tl-tick").map((n) => n.textContent),
    lanes: ["music", "voiceover", "sfx"].reduce((acc, id) => {
      const row = q(".tl-row.lane-" + id);
      acc[id] = {
        hint: row ? ((row.querySelector(".tl-lane__hint") || {}).textContent || "") : "NO ROW",
        clips: row ? Array.from(row.querySelectorAll(".tl-clip")).map((c) => ({
          name: (c.querySelector(".tl-clip__nm") || {}).textContent || "",
          time: (c.querySelector(".tl-clip__t") || {}).textContent || "",
          pending: c.classList.contains("is-pending"),
          moment: c.classList.contains("is-moment"),
          left: c.style.left,
          width: c.style.width,
        })) : [],
      };
      return acc;
    }, {}),
    playDisabled: !!(q(".tl-play") || {}).disabled,
    mutes: all(".tl-mute").map((b) => ({ label: b.textContent.trim(), disabled: b.disabled })),
  };
});

module.exports = {
  name: "timeline view states",
  backend: "mock",
  persona: "Timeline Tester",

  async run(page, t, { H }) {
    await H.startSession(page, { text: "Cinematic and premium." });
    t.must(await H.exists(page, "#tstage .tl-stage"), "timeline stage built");

    // ---- 1. nothing yet -----------------------------------------------------
    await t.step("state: empty session says what is missing, invents nothing", async () => {
      await renderState(page, { candidates: [], layers: { sfx: [] } });
      const r = await readTimeline(page);
      t.ok(/appears here once/i.test(r.screenEmpty), "screen explains the missing video", r.screenEmpty.slice(0, 50));
      t.ok(r.playDisabled, "play is disabled with no video");
      t.ok(/waiting on the video/i.test(r.note), "header admits it has no clock", r.note);
      t.ok(!r.ticks.length, "no fabricated time axis", r.ticks.join(",") || "none");
      t.ok(/analyz/i.test(r.scenes.join(" ")), "scene ruler shows analysis pending", r.scenes.join(","));
      for (const id of ["music", "voiceover", "sfx"]) {
        t.ok(r.lanes[id].hint.length > 0, `${id} lane offers a next step`, r.lanes[id].hint.slice(0, 44));
      }
      return "empty state honest";
    });

    // ---- 2. a video that will not load --------------------------------------
    await t.step("state: an unplayable video says so instead of a black rectangle", async () => {
      await renderState(page, {
        source_video: { url: "http://127.0.0.1:9/nope.mp4", duration_s: 16 },
        observation: { duration_s: 16, video_title: "broken.mp4", scenes: [] },
        candidates: [], layers: { sfx: [] },
      }, 1500);
      const r = await readTimeline(page);
      t.ok(/could not be loaded/i.test(r.screenEmpty), "explains the failure", r.screenEmpty.slice(0, 60));
      t.ok(/lanes below still describe/i.test(r.screenEmpty), "still offers the lanes");
      t.ok(r.playDisabled, "play disabled for an unplayable source");
      return "video error handled";
    });

    // ---- 3. music: generating → measuring → measured ------------------------
    await t.step("state: a generating take draws as pending, never as a guess", async () => {
      await renderState(page, {
        observation: { duration_s: 16, video_title: "reel", scenes: [] },
        candidates: [{ candidate_id: "c1", title: "Arc", status: "generating", audio_url: null }],
        layers: { sfx: [] },
      });
      const r = await readTimeline(page);
      const clip = r.lanes.music.clips[0];
      t.must(!!clip, "music lane has the take");
      t.ok(clip.pending, "clip marked pending");
      t.ok(/generating/i.test(clip.time), "clip states the real status", clip.time);
      t.ok(!/\d:\d\d–/.test(clip.time), "no invented interval", clip.time);
      return "pending honest";
    });

    await t.step("state: a completed take is drawn from its MEASURED length", async () => {
      await renderState(page, {
        observation: { duration_s: 16, video_title: "reel", scenes: [] },
        candidates: [{ candidate_id: "c2", title: "Velvet", status: "completed", audio_url: "__WAV__" }],
        selected_candidate_id: "c2",
        layers: { sfx: [] },
      }, 1800);
      const r = await readTimeline(page);
      const clip = r.lanes.music.clips[0];
      t.must(!!clip, "music clip present");
      t.ok(!clip.pending, "clip no longer pending", clip.time);
      // The fixture is 11s inside a 16s video: ~69% wide, and NOT clamped to 100.
      t.ok(/0:00–0:11/.test(clip.time), "interval matches the real audio length", clip.time);
      const w = parseFloat(clip.width);
      t.ok(w > 60 && w < 76, "width tracks the measurement", clip.width);
      return `measured ${clip.time}`;
    });

    await t.step("state: audio that cannot be probed says so, not 'measuring…' forever", async () => {
      await renderState(page, {
        observation: { duration_s: 16, video_title: "reel", scenes: [] },
        candidates: [{ candidate_id: "c3", title: "Expired", status: "completed",
                       audio_url: "http://127.0.0.1:9/gone.wav" }],
        layers: { sfx: [] },
      }, 2200);
      const r = await readTimeline(page);
      const clip = r.lanes.music.clips[0];
      t.must(!!clip, "clip present");
      t.ok(/unavailable/i.test(clip.time), "failed probe is distinguishable from a pending one", clip.time);
      return clip.time;
    });

    // ---- 4. voiceover: draft, flat take, timed plan --------------------------
    await t.step("state: a VO script with no recording says it is not recorded", async () => {
      await renderState(page, {
        observation: { duration_s: 16, video_title: "reel", scenes: [] },
        candidates: [], layers: { sfx: [], voiceover: { status: "draft", script: "Some moments do not need a stage at all." } },
        mix: { voiceover_start_s: 3.4 },
      });
      const r = await readTimeline(page);
      const clip = r.lanes.voiceover.clips[0];
      t.must(!!clip, "VO clip present");
      t.ok(clip.pending, "draft is pending");
      t.ok(/not recorded/i.test(clip.time), "says the script is not recorded", clip.time);
      return clip.time;
    });

    await t.step("state: the timed segment plan draws one clip per line", async () => {
      await renderState(page, {
        observation: { duration_s: 16, video_title: "reel", scenes: [] },
        candidates: [],
        layers: { sfx: [], voiceover: { status: "completed", segments: [
          { id: "s1", start_s: 1.2, duration_s: 2.4, text: "Some moments do not need a stage." },
          { id: "s2", start_s: 6.0, duration_s: 3.1, text: "You feel them before anyone speaks." },
          { id: "s3", start_s: 12.5, text: "Held, then gone." },
        ] } },
      });
      const r = await readTimeline(page);
      t.eq(r.lanes.voiceover.clips.length, 3, "one clip per planned line");
      const [a, b, c] = r.lanes.voiceover.clips;
      t.ok(/0:01.2–0:03.6/.test(a.time), "first line uses its own timing", a.time);
      t.ok(parseFloat(b.left) > parseFloat(a.left), "lines advance across the clock", `${a.left} → ${b.left}`);
      t.ok(c.moment || !/–/.test(c.time), "a line with no duration draws as a moment", c.time);
      return "segments drawn";
    });

    // ---- 5. sfx: planned events, ambience bed -------------------------------
    await t.step("state: spotted effects land on their timestamps", async () => {
      await renderState(page, {
        observation: { duration_s: 16, video_title: "reel", scenes: [] },
        candidates: [],
        layers: { sfx: { events: [
          { id: "e1", start_s: 3.1, description: "Whoosh" },
          { id: "e2", start_s: 7.1, description: "Impact" },
          { id: "e3", start_s: 11.4, description: "Riser" },
        ], ambience: "Room tone under the whole cut" } },
      });
      const r = await readTimeline(page);
      const clips = r.lanes.sfx.clips;
      const amb = clips.find((c) => /^ambience/i.test(c.name));
      const hits = clips.filter((c) => c !== amb);
      t.ok(hits.length >= 3, "every spotted effect is drawn", `${hits.length} hits + ${amb ? "bed" : "no bed"}`);
      const pos = hits.map((c) => parseFloat(c.left));
      t.ok(pos[0] < pos[1] && pos[1] < pos[2], "in time order", pos.join(" < "));
      // 3.1s of 16s ≈ 19%.
      t.ok(Math.abs(pos[0] - 19.4) < 2, "first hit sits on its timestamp", hits[0].left);
      t.ok(!!amb, "the ambience bed is shown as its own thing", amb ? amb.name : "missing");
      // A continuous bed must read as continuous, not as another point hit.
      t.ok(amb && parseFloat(amb.left) === 0 && parseFloat(amb.width) > 90,
        "the bed spans the whole picture", amb ? `${amb.left}+${amb.width}` : "");
      return `${hits.length} hits + bed`;
    });

    // ---- 6. scenes, cuts, moments, ticks ------------------------------------
    await t.step("state: the scene ruler, cut marks and moment strip agree", async () => {
      await renderState(page, {
        observation: { duration_s: 16, video_title: "Emotional Reactions", scenes: [
          { index: 0, start_s: 0, end_s: 3.4, label: "A close-up" },
          { index: 1, start_s: 3.4, end_s: 7.1, label: "The reaction" },
          { index: 2, start_s: 7.1, end_s: 11.2, label: "Wider" },
          { index: 3, start_s: 11.2, end_s: 16, label: "The close" },
        ] },
        spotting_sheet: { moments: [
          { t: 3.4, what: "First cut", owner: "music" },
          { t: 7.1, what: "Reaction peak", owner: "sfx" },
          { t: 12.5, what: "Held look", owner: "voiceover" },
        ] },
        candidates: [], layers: { sfx: [] },
      });
      const r = await readTimeline(page);
      t.eq(r.scenes.length, 4, "one ruler segment per scene");
      t.eq(r.cuts, 3, "a cut mark at every scene boundary but the first");
      t.eq(r.moments, 3, "every spotting-sheet moment is pinned");
      t.ok(r.ticks.length === 5 && r.ticks[4] === "0:16", "time axis spans the real duration", r.ticks.join(" "));
      t.ok(/4 scenes/.test(r.barSub), "transport bar counts the scenes", r.barSub);
      return "ruler agrees";
    });

    // ---- 7. the composed-mix claim ------------------------------------------
    await t.step("state: 'composed mix' is claimed only when composed media exists", async () => {
      await renderState(page, {
        observation: { duration_s: 16, video_title: "reel", scenes: [] },
        // The backend writes a final_artifact record BEFORE any render exists.
        final_artifact: { status: "planned", message: "queued" },
        candidates: [], layers: { sfx: [] },
      });
      let r = await readTimeline(page);
      t.ok(/source audio/i.test(r.barSub), "a planned-but-unrendered mix is not called composed", r.barSub);
      await renderState(page, {
        observation: { duration_s: 16, video_title: "reel", scenes: [] },
        final_artifact: { status: "completed", video_url: "__WAV__" },
        candidates: [], layers: { sfx: [] },
      }, 900);
      r = await readTimeline(page);
      t.ok(/composed mix/i.test(r.barSub), "a real composed mix is announced", r.barSub);
      return "claim matches reality";
    });

    // ---- 8. transport + lane controls ---------------------------------------
    await t.step("controls: mutes gate only the lanes that have content", async () => {
      await renderState(page, {
        observation: { duration_s: 16, video_title: "reel", scenes: [] },
        candidates: [{ candidate_id: "m1", title: "Bed", status: "completed", audio_url: "__WAV__" }],
        layers: { sfx: [] },
      }, 1500);
      const r = await readTimeline(page);
      const music = r.mutes.find((m) => /music/i.test(m.label));
      const sfx = r.mutes.find((m) => /sound/i.test(m.label));
      t.ok(music && !music.disabled, "music mute is live when there is music");
      t.ok(sfx && sfx.disabled, "sfx mute is disabled when the lane is empty");
      return "mute affordances match the lanes";
    });

    await t.step("controls: muting dims its lane even with no preview loaded", async () => {
      const before = await page.$eval(".tl-row.lane-music", (n) => n.style.opacity || "");
      await H.click(page, ".tl-mute.lane-music");
      const after = await page.$eval(".tl-row.lane-music", (n) => n.style.opacity || "");
      const off = await page.$eval(".tl-mute.lane-music", (n) => n.classList.contains("is-off"));
      t.ok(off, "mute button reads as off");
      t.ok(after && after !== before, "the lane visibly dims", `"${before}" → "${after}"`);
      await H.click(page, ".tl-mute.lane-music");
      return "mute is a visible filter";
    });

    // ---- 9. seek + selection (the interactions that were dead) --------------
    await t.step("controls: clicking empty lane space seeks the preview", async () => {
      await renderState(page, {
        source_video: { url: "__WAV__", duration_s: 16 },
        observation: { duration_s: 16, video_title: "reel", scenes: [] },
        candidates: [], layers: { sfx: [] },
      }, 1500);
      const u = await H.usable(page, ".tl-row.lane-sfx .tl-lane");
      t.must(u.ok, "lane track is hit-testable", u.why || "ok");
      const box = await page.$eval(".tl-row.lane-sfx .tl-lane", (n) => {
        const r = n.getBoundingClientRect();
        return { x: r.left + r.width * 0.75, y: r.top + r.height / 2 };
      });
      await page.mouse.click(box.x, box.y);
      await H.sleep(300);
      const at = await page.evaluate(() => {
        const v = document.querySelector(".tl-screen video");
        return v ? v.currentTime : null;
      });
      // 75% of a 16s clip ≈ 12s.
      t.ok(at !== null && at > 10 && at < 14, "playhead moved to the clicked point", String(at));
      return `seeked to ${at}`;
    });

    await t.step("controls: clicking a clip selects it (and a 0:00 clip seeks too)", async () => {
      await renderState(page, {
        source_video: { url: "__WAV__", duration_s: 16 },
        observation: { duration_s: 16, video_title: "reel", scenes: [] },
        candidates: [{ candidate_id: "z1", title: "From zero", status: "completed", audio_url: "__WAV__" }],
        layers: { sfx: { events: [{ id: "e9", start_s: 9.0, description: "Late hit" }] } },
      }, 1800);
      // Seek to the late effect, then back to the clip that starts at 0:00 —
      // the zero case used to select without ever moving the playhead.
      await H.click(page, ".tl-row.lane-sfx .tl-clip");
      await H.sleep(300);
      const mid = await page.evaluate(() => {
        const v = document.querySelector(".tl-screen video");
        return v ? v.currentTime : null;
      });
      await H.click(page, ".tl-row.lane-music .tl-clip");
      await H.sleep(300);
      const zero = await page.evaluate(() => {
        const v = document.querySelector(".tl-screen video");
        const sel = document.querySelector(".tl-clip.is-sel");
        return { t: v ? v.currentTime : null, selected: !!sel };
      });
      t.ok(mid !== null && mid > 5, "clicking a mid-clip seeks there", String(mid));
      t.ok(zero.t === 0, "clicking a 0:00 clip returns the playhead to the start", String(zero.t));
      t.ok(zero.selected, "the clicked clip shows as selected");
      return "clip selection + seek";
    });

    // ---- 10. tenths, and the carry that used to print 0:03.10 ---------------
    await t.step("labels: timestamps round without producing impossible tenths", async () => {
      await renderState(page, {
        observation: { duration_s: 20, video_title: "reel", scenes: [] },
        candidates: [],
        layers: { sfx: { events: [
          { id: "r1", start_s: 3.96, description: "Carry" },
          { id: "r2", start_s: 9.97, description: "Carry two" },
          { id: "r3", start_s: 5.44, description: "Plain" },
        ] } },
      });
      const times = (await readTimeline(page)).lanes.sfx.clips.map((c) => c.time);
      const bad = times.filter((s) => /\.\d\d/.test(s) || /:\d\d\.10/.test(s));
      t.ok(!bad.length, "no timestamp shows a tenth of 10", bad.join(",") || times.join(" "));
      t.ok(times.some((s) => /0:04/.test(s)), "3.96s reads as 0:04", times.join(" "));
      return times.join(" ");
    });

    // ---- 11. it must survive the view toggle --------------------------------
    await t.step("the timeline stops playing when the user switches to Canvas", async () => {
      await renderState(page, {
        source_video: { url: "__WAV__", duration_s: 16 },
        observation: { duration_s: 16, video_title: "reel", scenes: [] },
        candidates: [], layers: { sfx: [] },
      }, 1200);
      await page.evaluate(() => { const v = document.querySelector(".tl-screen video"); if (v) return v.play().catch(() => {}); });
      await H.sleep(400);
      await H.click(page, '#view-seg .vseg__btn[data-view="canvas"]');
      await H.sleep(400);
      const paused = await page.evaluate(() => {
        const v = document.querySelector(".tl-screen video");
        return v ? v.paused : true;
      });
      await H.click(page, '#view-seg .vseg__btn[data-view="timeline"]');
      return paused ? "paused on leaving the view" : "KEPT PLAYING while hidden";
    });
  },
};
