/* Transform mode — the cut-shorts (transformation) session on the STABLE surface.
 *
 * ADDITIVE module (same discipline as canvas-mode): engaged only when the user
 * picks the "Cut a short" starter; the audio session path is byte-identical
 * when not engaged. Speaks to the REAL creation endpoints the devserver mounts
 * (/api/v2/creation/*, /api/v2/library/*): resolve (ask-first) → plan (show,
 * no spend) → lock (render). Chat cards, ask-first questions, the cut rail,
 * the side-panel player and the spend-consent overlay all reuse the existing
 * design system.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  };
  const API_CREATE = "/api/v2/creation";
  const API_LIB = "/api/v2/library";

  /** Module state for the CURRENT transform session (reset on start). */
  const T = { pending: false, assets: [], items: [], intent: "", duration: 16,
              treatment: "music_only", musicMode: null, preview: null,
              player: null };

  /* ---------------------------------------------------- thread primitives */
  const inner = () => $("thread-inner");
  const scrollDown = () => { const t = $("thread"); if (t) t.scrollTop = t.scrollHeight; };

  function userMsg(text) {
    const row = el("div", "user-row");
    row.appendChild(el("div", "ub", esc(text)));
    inner().appendChild(row); scrollDown();
  }

  /** Agent message; returns the .abody so callers can append cards under it. */
  function agentMsg(html) {
    const row = el("div", "agent-row");
    row.appendChild(el("div", "av", '<i class="ti ti-sparkles"></i>'));
    const body = el("div", "abody");
    body.appendChild(el("div", "aname", "Edenn"));
    if (html != null) body.appendChild(el("div", "atext", html));
    row.appendChild(body);
    inner().appendChild(row); scrollDown();
    return body;
  }

  /* ------------------------------------------------------------ the flow */
  async function start(direction) {
    T.pending = false;
    T.intent = direction || "Cut a 16s teaser from my library.";
    const durMatch = T.intent.match(/(\d{1,2})\s*s\b/);
    T.duration = durMatch ? Math.min(60, Math.max(8, +durMatch[1])) : 16;

    let assets;
    try {
      assets = (await (await fetch(`${API_LIB}/assets`)).json()).assets;
    } catch (_) {
      window.__edenn.toast("Transform needs the dev backend — run the devserver with EDENN_CREATION_MEDIA_DIR set.");
      return;
    }
    $("entrance").hidden = true;
    $("session").hidden = false;
    $("session-name").textContent = "cut session";
    inner().innerHTML = "";
    // A transform session owns the RIGHT PANE from the start. It is a different
    // session type, not a third position on the Timeline/Canvas toggle, so it
    // takes the pane over rather than joining the switch — and the toggle is
    // hidden while it holds it, so there is no control that would eject it.
    takeRightPane();
    userMsg(T.intent);

    // Originals lead: generated VIDEO outputs are not auto-bundled as sources
    // (remixing an output is an explicit @ later, not a default); generated
    // audio stays — tracks are meant to score new cuts.
    T.assets = assets.filter((a) =>
      a.kind === "audio" || (a.kind === "video" && !a.generated));
    T.items = T.assets.map((a) => ({ ref: a.asset_id, role: null }));
    const body = agentMsg("Here's what your library gives me to work with:");
    const pills = el("div", "pills");
    T.assets.forEach((a) => pills.appendChild(el("span", "pill",
      `<i class="ti ${a.kind === "audio" ? "ti-music" : "ti-video"}"></i> ${esc(a.name)}` +
      (a.duration_s ? ` · ${window.__edenn.fmtDur ? window.__edenn.fmtDur(a.duration_s) : Math.round(a.duration_s) + "s"}` : ""))));
    body.appendChild(pills); scrollDown();

    await resolveUntilClean();
    await askTreatment();
    await planAndShow();
  }

  /** Hand the right pane to the cut rail: timeline and canvas step aside, and
   *  the view toggle goes with them (a dead toggle is worse than no toggle). */
  function takeRightPane() {
    const t = $("tstage"); if (t) t.hidden = true;
    const c = $("cstage"); if (c) c.hidden = true;
    const hd = $("rpane-hd"); if (hd) hd.hidden = true;
    const rail = $("xrail"); if (rail) rail.hidden = false;
    const body = $("xrail-body"); if (body) body.innerHTML = "";
    const badge = $("xrail-badge"); if (badge) badge.textContent = "cut preview";
  }

  /** The inverse: hand the pane back. Called by the shell (showEntrance) when
   *  the user leaves a session — without it, one cut session would leave the
   *  toggle hidden and a stale rail parked over every later audio session. */
  function releaseRightPane() {
    if (T.player) { try { T.player.stop(); } catch (_) {} T.player = null; }
    const rail = $("xrail");
    if (rail && !rail.hidden) {
      rail.hidden = true;
      const body = $("xrail-body"); if (body) body.innerHTML = "";
    }
    const hd = $("rpane-hd"); if (hd) hd.hidden = false;
    // #tstage/#cstage visibility is the view toggle's business — the shell
    // reasserts it via setRightView after this returns.
  }

  /** Resolve; render each ask-first question as chat chips; repeat until clean. */
  async function resolveUntilClean() {
    for (let round = 0; round < 6; round += 1) {
      const res = await (await fetch(`${API_CREATE}/resolve`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: T.items, intent: T.intent }) })).json();
      if (res.status !== "needs_input") return res;
      for (const q of res.questions) await askQuestion(q);
    }
    return null;
  }

  /** One AmbiguityQuestion as a chat card; resolves when the user answers. */
  function askQuestion(q) {
    return new Promise((resolve) => {
      const body = agentMsg(esc(q.question));
      const chips = el("div", "qchips");
      q.options.forEach((o) => {
        const label = o.key.startsWith("asset_") ? (nameOf(o.key) || o.label) : o.label;
        const chip = el("button", "qchip", esc(label));
        chip.title = o.description || "";
        chip.addEventListener("click", () => {
          applyAnswer(q, o.key);
          chips.querySelectorAll(".qchip").forEach((c) => { c.disabled = true; });
          chip.classList.add("is-on");
          body.appendChild(el("div", "atext", `Got it — ${esc(label)}.`));
          scrollDown(); resolve();
        });
        chips.appendChild(chip);
      });
      body.appendChild(chips); scrollDown();
    });
  }

  function applyAnswer(q, key) {
    if (q.kind === "contradiction") { T.musicMode = key; return; }
    if (key === "music" || key === "voice") {
      q.refs.forEach((ref) => {
        const item = T.items.find((i) => i.ref === ref);
        if (item) item.role = key;
      });
      return;
    }
    const item = T.items.find((i) => i.ref === key);
    if (item) item.role = "spine";
  }

  /** The treatment gate — asked, never defaulted silently (owner rule). */
  function askTreatment() {
    return new Promise((resolve) => {
      const body = agentMsg("How should this cut sound?");
      const grid = el("div", "cards-row");
      const options = [
        ["music_only", "ti-music", "Music only", "The track carries it"],
        ["keep_original", "ti-microphone", "Keep the original voice",
         "Real moments speak; music ducks under"],
        ["rephrase_original", "ti-language", "Rephrase in our voice",
         "Their message, our house voice — redub, honestly"],
      ];
      options.forEach(([id, icon, title, desc]) => {
        const card = el("button", "intent-card");
        card.appendChild(el("div", "intent-card__ic", `<i class="ti ${icon}"></i>`));
        card.appendChild(el("div", "intent-card__title", esc(title)));
        card.appendChild(el("div", "intent-card__desc", esc(desc)));
        card.addEventListener("click", () => {
          T.treatment = id;
          grid.querySelectorAll(".intent-card").forEach((c) => { c.disabled = true; });
          card.classList.add("is-on");
          resolve();
        });
        grid.appendChild(card);
      });
      body.appendChild(grid); scrollDown();
    });
  }

  async function planAndShow() {
    agentMsg("Planning against the beat grid — legality first, taste second…");
    const musicItem = T.items.find((i) =>
      (T.assets.find((a) => a.asset_id === i.ref) || {}).kind === "audio" && i.role !== "voice");
    const treatment = {
      kind: T.treatment,
      music: T.musicMode || (musicItem ? "provided" : "generate"),
      music_ref: musicItem ? musicItem.ref : null,
      knobs: { coherence_mode: "single_story" },
    };
    const req = { bundle: { items: T.items, intent: T.intent },
                  shorts: [{ hypothesis: T.intent, duration_s: T.duration, treatment }] };
    const r = await fetch(`${API_CREATE}/plan`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req) });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      // `err` is often an Error (whose JSON form is "{}") or a FastAPI body.
      // Printing the raw JSON showed users a literal "{}" and told them nothing.
      const why = (err && (err.detail || err.message))
        || (typeof err === "string" ? err : "")
        || "the planner did not say why";
      agentMsg(`Planning hit a wall: ${esc(String(why).slice(0, 220))}`);
      return;
    }
    const preview = (await r.json()).previews[0];
    T.preview = preview;
    renderPlanCard(preview);
    mountRailPlayer(preview);
  }

  /* ------------------------------------------------------- plan card + rail */
  function stripHTML(p) {
    const W = 100 / p.duration_s;
    const beats = p.beats_out.map((b) =>
      `<i class="tf-beat" style="left:${(b * W).toFixed(2)}%"></i>`).join("");
    const slots = p.slots.map((s) =>
      `<div class="tf-slot ${s.is_bite ? "bite" : ""}" title="${esc(s.why)}"
            style="left:${(s.t_out * W).toFixed(2)}%;width:${(s.dur_s * W).toFixed(2)}%">
         <small>${s.index}</small></div>`).join("");
    return `<div class="tf-strip">${beats}${slots}<div class="tf-ph" style="left:0%"></div></div>`;
  }

  function renderPlanCard(p) {
    const body = agentMsg(
      `Here's the cut — <b>${p.slots.length} slots</b> over ${p.duration_s.toFixed(1)}s at ` +
      `${Math.round(p.tempo_bpm)}bpm, every boundary on a beat. Watch it in the side panel; ` +
      `nothing renders until you lock.`);
    const card = el("div", "tf-plancard");
    card.innerHTML = stripHTML(p) +
      `<div class="tf-row">
         <button class="tf-btn" data-tf-watch>▶ Watch the cut</button>
         <span class="tf-note">${esc(p.treatment_kind.replace("_", " "))} · ${esc(p.music_mode)} music</span>
         <button class="tf-btn tf-btn--primary" data-tf-lock>Lock &amp; render</button>
       </div>`;
    body.appendChild(card); scrollDown();
    card.querySelector("[data-tf-watch]").addEventListener("click", () => {
      if (T.player) T.player.toggle();
    });
    card.querySelector("[data-tf-lock]").addEventListener("click", () => {
      const voiced = T.treatment === "rephrase_original";
      window.__edenn.confirmSpend(
        "Render this cut?",
        voiced
          ? "This transcribes the source, rewrites its message, and speaks it in the house voice (a small model + TTS spend), then renders the exact cut you previewed."
          : "This renders the exact cut you previewed (local ffmpeg — no generation spend) and saves it to your library.",
        () => lockAndRender(card),
        // The body describes a render, so the button must offer a render —
        // the default "Yes, generate" contradicted it (and implied a spend the
        // unvoiced path does not make).
        voiced ? "Yes, render it" : "Yes, render it");
    });
  }

  async function lockAndRender(card) {
    const btn = card.querySelector("[data-tf-lock]");
    btn.disabled = true; btn.textContent = "Rendering…";
    const r = await fetch(`${API_CREATE}/render`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: T.preview.plan_id }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { btn.textContent = "Failed"; agentMsg("Render failed — check the devserver log."); return; }
    btn.textContent = "Rendered ✓";
    const body = agentMsg("Done — the short is rendered and in your library, cut for cut as previewed:");
    const vid = el("video", "tf-video");
    vid.controls = true;
    vid.src = `${API_LIB}/media/${d.asset_id}`;
    body.appendChild(vid); scrollDown();
  }

  /** Side-panel player: clock-master playback of the cut-list (source video
   *  slaved to the wall-clock playhead; the track slaved likewise). */
  function mountRailPlayer(p) {
    const rail = $("xrail-body"); if (!rail) return;
    const badge = $("xrail-badge"); if (badge) badge.textContent = "cut preview";
    rail.innerHTML = "";
    const card = el("div", "tf-railcard");
    card.innerHTML = `<video muted playsinline preload="auto"></video>` + stripHTML(p) +
      `<div class="tf-row"><button class="tf-btn" data-tf-play>▶ Watch</button>
       <span class="tf-note" data-tf-why></span></div>`;
    rail.appendChild(card);

    const video = card.querySelector("video");
    const ph = card.querySelector(".tf-ph");
    const whyEl = card.querySelector("[data-tf-why]");
    const playBtn = card.querySelector("[data-tf-play]");
    const audio = p.music_url ? new Audio(p.music_url) : null;
    // Wall-clock master on an interval (NOT rAF): rAF suspends in hidden
    // tabs, which would freeze the playhead; wall-clock math keeps t honest
    // at any tick rate, and the video plays natively between corrections.
    let timer = null, t = 0, wall = 0, playing = false, cur = -1;

    const srcOf = (aid) => (p.sources.find((s) => s.asset_id === aid) || {}).media_url;
    video.src = srcOf((p.slots[0] || {}).asset_id) || "";
    const slotAt = (tt) => p.slots.find((s) => tt >= s.t_out && tt < s.t_out + s.dur_s);

    function tick() {
      t = (performance.now() - wall) / 1000;
      if (t >= p.duration_s) { stop(); return; }
      ph.style.left = `${(t / p.duration_s * 100).toFixed(2)}%`;
      const s = slotAt(t);
      if (s) {
        if (s.index !== cur) {
          cur = s.index;
          const url = srcOf(s.asset_id);
          if (url && video.dataset.aid !== s.asset_id) { video.src = url; video.dataset.aid = s.asset_id; }
          video.currentTime = s.seg_in_s + (t - s.t_out);
          if (whyEl) whyEl.textContent = s.why || "";
        } else {
          const want = s.seg_in_s + (t - s.t_out);
          if (Math.abs(video.currentTime - want) > 0.12) video.currentTime = want;
        }
      }
      if (audio) {
        const want = p.music_start_s + t;
        if (Math.abs(audio.currentTime - want) > 0.15) audio.currentTime = want;
      }
    }
    function play() {
      // Warm-up guard: never start the clock before the source can actually
      // seek — otherwise the playhead advances over a black frame.
      if (video.readyState < 2) {
        playBtn.textContent = "… loading";
        video.addEventListener("loadeddata", play, { once: true });
        video.load();
        return;
      }
      playing = true; wall = performance.now() - t * 1000; cur = -1;
      video.play().catch(() => {});
      if (audio) { audio.currentTime = p.music_start_s + t; audio.play().catch(() => {}); }
      playBtn.textContent = "❚❚ Pause";
      timer = setInterval(tick, 40);
      tick();
    }
    function stop() {
      playing = false; clearInterval(timer);
      video.pause(); if (audio) audio.pause();
      if (t >= p.duration_s) t = 0;
      playBtn.textContent = "▶ Watch";
    }
    playBtn.addEventListener("click", () => (playing ? stop() : play()));
    T.player = { toggle: () => (playing ? stop() : play()), stop };
  }

  function nameOf(assetId) {
    const a = T.assets.find((x) => x.asset_id === assetId.split("#")[0]);
    return a ? a.name : null;
  }

  /* ------------------------------------------------------------- wiring */
  function wire() {
    const chip = $("transform-chip");
    if (chip) chip.addEventListener("click", () => {
      T.pending = true;
      // Remember the words the chip itself wrote, so we can tell "still the cut
      // brief" from "the user has typed something else".
      T.armedBrief = (chip.getAttribute("data-fill") || "").trim();
    });
    // Editing the brief away from the chip's own wording means the user has
    // moved on. Without this, Start silently ran the cut flow over a freshly
    // typed audio brief — acting on a chip clicked minutes ago instead of on
    // what the box actually says.
    const brief = $("start-text");
    if (brief) {
      brief.addEventListener("input", () => {
        if (T.pending && brief.value.trim() !== (T.armedBrief || "")) T.pending = false;
      });
    }
    // Any other starter or a manual edit clears the transform intent so the
    // audio flow proceeds untouched.
    document.querySelectorAll('#starters .chip[data-fill]:not(#transform-chip)')
      .forEach((c) => c.addEventListener("click", () => { T.pending = false; }));
    const sb = $("surprise-btn");
    if (sb) sb.addEventListener("click", () => { T.pending = false; });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else { wire(); }

  window.EdennTransform = {
    get pending() { return T.pending; },
    // Settable so the host can disarm a queued transform intent (e.g. the
    // offline page declines the flow and falls back to the audio path).
    set pending(v) { T.pending = !!v; },
    start,
    release: releaseRightPane,
  };
})();
