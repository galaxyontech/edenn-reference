/* ============================================================================
 * Edenn — timeline view (the default right pane).
 *
 * The video, big, with its audio laid out underneath on the same clock: three
 * lanes (music / voiceover / sound effects), each clip showing the interval it
 * occupies, over a ruler of the video's own scene cuts.
 *
 * Why this replaced the old side rail: the rail could say *what* the session
 * had ("Music — locked", "Voiceover — ready") but never *when*. "The music
 * swells before the cut" and "the narration lands on top of the drop" are the
 * two things people actually want to check, and neither is expressible in a
 * list. Putting the layers on the video's clock makes both readable at a
 * glance, and makes the third question — "what is playing at the same time as
 * what" — a matter of looking down a column. That column IS the answer, which
 * is why there is no separate overlap shading: the lanes share one x-axis, so
 * simultaneity is already visible, and banding it again only added a second
 * grid of vertical edges on top of the scene cuts.
 *
 * HONESTY ABOUT TIME. Three kinds of truth are drawn, each styled as itself:
 *
 *   - MEASURED intervals — a take's real length, read off the audio the cards
 *     already play (probe()). Until the media has loaded, the clip is a dashed
 *     pending block that says so rather than guessing a width.
 *   - PLANNED moments — narration segments and spotted SFX events carry a real
 *     start and, by design, no length (`{start_s}` is the whole contract).
 *     They draw as solid nubs anchored at their time: a timed moment, not a
 *     loading state. A segment that DOES carry `duration_s` gets its interval.
 *   - CONTINUOUS beds — an SFX ambience is a bed under the whole picture; its
 *     honest extent is the full clock, drawn as a thin band, labeled.
 *
 * The scene ruler is fully backed — `observation.scenes[]` is real analysis —
 * and the spotting sheet (`state.spotting_sheet.moments[]`), when the backend
 * provides one, annotates the ruler with who owns each moment. With a clock but
 * no analysis yet the ruler shows an ILLUSTRATIVE four-beat layout, labelled as
 * such in the row header and its tooltips, and it draws no cut marks — an
 * invented cut across the lanes would read exactly like a measured one.
 *
 * Editing stays in chat, by design: clips seek and select, they do not drag.
 *
 * PRESENTATION comes from the design branch: the light transport (skip to
 * start/end, zoom, the disabled trim affordance), tracks that adapt to the
 * height and content available, the drag-adjustable video/timeline split, the
 * clip surface, and layout that survives selecting a clip. Seeking deliberately
 * stays on the lane tracks rather than a full-surface overlay — a transparent
 * sheet wide enough to catch a click anywhere on the clock also swallows every
 * clip under it, and then clips stop being clickable at all.
 *
 * Integration (additive, same pattern as canvas-mode):
 *   - app.js reconcile() calls window.EdennTimeline.render(snap)
 *   - app.js setRightView() calls window.EdennTimeline.setView(view)
 *   - app.js showEntrance() calls window.EdennTimeline.pause()
 *   - a take card's play button calls window.EdennTimeline.audition(id, audio)
 * ========================================================================== */
(function () {
  // Same-origin media needs the page token as a query param (media elements
  // cannot send headers). app.js owns the helper; fall back to identity so the
  // mock console (no auth) is untouched.
  const mediaSrc = (u) => (window.__edennMediaSrc ? window.__edennMediaSrc(u) : u);
  "use strict";

  function el(tag, cls, html) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }
  function esc(v) {
    return String(v == null ? "" : v)
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  }
  function clock(s) {
    s = Math.max(0, s || 0);
    return Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0");
  }
  /** 0:03.4 — lanes need the tenth; a 0.4s SFX hit is a whole clip. */
  function stamp(s) {
    s = Math.max(0, s || 0);
    // Round to total tenths FIRST so the carry propagates: 3.96 is "0:04",
    // never the impossible "0:03.10".
    const d = Math.round(s * 10);
    const tenths = d % 10;
    return clock((d - tenths) / 10) + (tenths ? "." + tenths : "");
  }

  // The real analyzer emits `start_timestamp`/`end_timestamp`/`visual_summary`;
  // the mock (and the collab anchors) use `start_s`/`end_s`/`label`. Read both.
  function sceneStart(sc) { return Number(sc.start_s != null ? sc.start_s : sc.start_timestamp) || 0; }
  function sceneEnd(sc) { return Number(sc.end_s != null ? sc.end_s : sc.end_timestamp) || 0; }
  function sceneLabel(sc) { return sc.label || sc.visual_summary || ""; }

  /** A ruler segment is a few pixels wide: one word reads, a sentence does not.
   *  The full analysis stays in the segment's tooltip. */
  function sceneName(label) {
    const words = String(label || "").match(/[\p{L}\p{N}]+/gu) || [];
    const closing = /\b(close|closing|outro|ending)\s*[.!?]?$/i.test(String(label || ""));
    const word = closing ? "Closing" : words[0] || "Scene";
    return word.charAt(0).toUpperCase() + word.slice(1);
  }

  const LANES = [
    { id: "music", label: "Music", icon: "ti-music" },
    { id: "voiceover", label: "Voiceover", icon: "ti-microphone" },
    { id: "sfx", label: "Sound effects", icon: "ti-wave-sine" },
  ];

  // Spotting-sheet owners wear the lane palette of the layer they assign.
  const OWNER_LANE = { narrate: "lane-voiceover", sfx: "lane-sfx", music: "lane-music", silence: "" };

  const CHAT_MIN = 300, CHAT_MAX = 560, CHAT_DEFAULT = 380;
  const WIDTH_KEY = "edenn.chatWidth";
  const SHARE_KEY = "edenn.timeline.share";
  // A lane never grows past this many stacked rows: an SFX plan with twenty
  // hits would otherwise push the video off the screen.
  const MAX_SLOTS = 3;

  const tl = {
    view: "timeline",
    snap: null,
    sid: null,
    video: null,        // the hero <video> — master clock for the playhead
    videoKey: null,     // (url) the hero was last built for
    timelineKey: null,  // (json) the lanes the timeline block was last built for
    probes: {},         // url -> measured duration (or "failed")
    muted: {},          // laneId -> true when the user has muted that lane
    selected: null,     // clipId of the selected clip
    audio: null,        // an auditioned take's <audio> — it owns the clock then
    auditionId: null,   // candidate_id being auditioned
    lastSelection: null,
    frame: 0,
    booted: false,
  };

  let stage, screen, bar, tlBlock, playBtn, clockEl, barTitle, muteBox, startBtn, endBtn;

  // ========================================================================
  // Boot — build the pane once; render() only fills it.
  // ========================================================================
  function boot() {
    stage = document.getElementById("tstage");
    if (!stage || tl.booted) return;
    tl.booted = true;

    const stageWrap = el("div", "tl-stage");
    screen = el("div", "tl-screen");
    stageWrap.appendChild(screen);

    bar = el("div", "tl-bar");
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", "Preview and timeline controls");

    playBtn = el("button", "tl-play", '<i class="ti ti-player-play"></i><i class="ti ti-player-pause"></i>');
    playBtn.type = "button";
    playBtn.title = "Play / pause";
    playBtn.setAttribute("aria-label", "Play preview");
    playBtn.disabled = true;
    playBtn.addEventListener("click", togglePlay);
    clockEl = el("span", "tl-clock", "0:00 / 0:00");
    barTitle = el("div", "tl-bar__meta", '<div class="tl-bar__title"></div><div class="tl-bar__sub"></div>');
    // The compact transport carries no title block; the same summary is on the
    // bar's own tooltip so the fact is still reachable.
    barTitle.hidden = true;
    muteBox = el("div", "tl-mutes");

    function control(label, symbol, action) {
      const button = el("button", "tl-tool", '<i class="ti ti-' + symbol + '"></i>');
      button.type = "button"; button.title = label; button.setAttribute("aria-label", label);
      if (action) button.addEventListener("click", action);
      return button;
    }
    function jump(end) {
      const media = tl.audio || tl.video;
      if (!media) return;
      media.currentTime = end && Number.isFinite(media.duration) ? media.duration : 0;
      movePlayhead();
    }
    startBtn = control("Go to start", "player-skip-back", () => jump(false));
    endBtn = control("Go to end", "player-skip-forward", () => jump(true));
    // Trimming is a chat instruction, not a drag — the affordance is shown
    // disabled rather than pretending the timeline is an editor.
    const cut = control("Trim — unavailable in this preview", "cut"); cut.disabled = true;
    const separator = el("span", "tl-tool-separator"); separator.setAttribute("aria-hidden", "true");

    const zoomSlider = el("input", "tl-zoom-slider");
    zoomSlider.type = "range"; zoomSlider.min = "1"; zoomSlider.max = "4"; zoomSlider.step = ".25"; zoomSlider.value = "1";
    zoomSlider.setAttribute("aria-label", "Timeline zoom");
    const zoomReset = el("button", "tl-tool tl-zoom-value", "1×");
    zoomReset.type = "button"; zoomReset.title = "Reset timeline zoom"; zoomReset.setAttribute("aria-label", "Reset timeline zoom");
    function zoom(value) {
      const level = Math.max(1, Math.min(4, value));
      tlBlock.style.setProperty("--timeline-zoom", level);
      zoomSlider.value = String(level); zoomSlider.setAttribute("aria-valuetext", level + " times");
      zoomReset.textContent = level + "×";
      zoomOut.disabled = level === 1; zoomIn.disabled = level === 4;
      updateTimelineLayout();
    }
    const zoomIn = control("Zoom in timeline", "zoom-in", () => zoom(Number(zoomSlider.value) + .25));
    const zoomOut = control("Zoom out timeline", "zoom-out", () => zoom(Number(zoomSlider.value) - .25));
    zoomOut.disabled = true;
    zoomSlider.addEventListener("input", () => zoom(Number(zoomSlider.value)));
    zoomReset.addEventListener("click", () => zoom(1));

    bar.append(startBtn, playBtn, endBtn, clockEl, cut, separator, zoomIn, zoomOut, zoomReset, zoomSlider, barTitle, muteBox);
    stageWrap.appendChild(bar);

    tlBlock = el("div", "tl-tl");
    stage.appendChild(stageWrap);
    const split = el("div", "tl-split");
    stage.appendChild(split);
    stage.appendChild(tlBlock);
    wireTimelineSplit(split);
    if (typeof ResizeObserver !== "undefined") {
      const layoutObserver = new ResizeObserver(updateTimelineLayout);
      layoutObserver.observe(tlBlock);
      layoutObserver.observe(stage);
    }

    wireGrip();
    restoreWidth();
  }

  // ========================================================================
  // Layout — tracks take the height that is actually available, and give the
  // space an empty track cannot use to one that can.
  // ========================================================================
  function updateTimelineLayout() {
    if (!tlBlock || !tlBlock.clientHeight) return;
    const laneWrap = tlBlock.querySelector(".tl-lanewrap");
    if (!laneWrap) return;
    const lanes = Array.from(laneWrap.querySelectorAll(".tl-lane"));
    const children = Array.from(tlBlock.children).filter((child) => child.getBoundingClientRect().height > 0);
    const style = getComputedStyle(tlBlock);
    const gap = parseFloat(style.rowGap) || 0;
    const laneGap = parseFloat(getComputedStyle(laneWrap).rowGap) || 0;
    const overhead = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0)
      + gap * Math.max(0, children.length - 1)
      + children.filter((child) => !child.contains(laneWrap)).reduce((sum, child) => sum + child.getBoundingClientRect().height, 0)
      + laneGap * Math.max(0, lanes.length - 1);
    const slots = lanes.map((lane) => Number(lane.dataset.slots) || 1);
    const caps = lanes.map((lane, index) => (lane.classList.contains("is-empty") ? 56 : 112) * slots[index]);
    tlBlock.style.setProperty("--timeline-max-height", Math.ceil(overhead + caps.reduce((a, b) => a + b, 0)) + "px");
    const heights = slots.map((count) => count * 34);
    const minimum = heights.reduce((a, b) => a + b, 0);
    let available = Math.max(0, tlBlock.clientHeight - overhead - minimum);
    // Redistribute space left over by capped empty tracks to populated tracks.
    for (let pass = 0; pass < lanes.length && available > 0; pass++) {
      const growing = heights.map((height, index) => index).filter((index) => heights[index] < caps[index]);
      if (!growing.length) break;
      const portion = available / growing.length;
      growing.forEach((index) => { const delta = Math.min(portion, caps[index] - heights[index]); heights[index] += delta; available -= delta; });
    }
    lanes.forEach((lane, index) => lane.style.setProperty("--lane-height", heights[index] + "px"));
    const ticks = tlBlock.querySelector(".tl-ticks");
    if (ticks) Array.from(ticks.children).forEach((tick, index, all) => {
      tick.hidden = index !== 0 && index !== all.length - 1 && (ticks.clientWidth < 180 || (ticks.clientWidth < 360 && index % 2 === 1));
    });
    const split = stage.querySelector(".tl-split");
    if (split && stage.clientHeight) {
      const maximum = Math.min(65, (overhead + caps.reduce((a, b) => a + b, 0)) / stage.clientHeight * 100);
      split.setAttribute("aria-valuemax", String(Math.round(maximum)));
      split.setAttribute("aria-valuemin", String(Math.round(Math.min(20, maximum))));
      split.setAttribute("aria-valuenow", String(Math.round(tlBlock.clientHeight / stage.clientHeight * 100)));
      split.setAttribute("aria-valuetext", Math.round(tlBlock.clientHeight / stage.clientHeight * 100) + "% timeline");
    }
    movePlayhead();
  }

  /** The video/timeline divider — drag it to give either one more room. */
  function wireTimelineSplit(split) {
    split.tabIndex = 0;
    split.setAttribute("role", "separator");
    split.setAttribute("aria-orientation", "horizontal");
    split.setAttribute("aria-label", "Resize video and timeline");
    split.setAttribute("aria-valuemin", "20");
    split.setAttribute("aria-valuemax", "65");
    function apply(value) {
      const cap = parseFloat(tlBlock.style.getPropertyValue("--timeline-max-height"));
      const maximum = stage.clientHeight && cap ? Math.min(65, cap / stage.clientHeight * 100) : 65;
      const share = Math.max(Math.min(20, maximum), Math.min(maximum, value));
      stage.style.setProperty("--timeline-share", share + "%");
      split.setAttribute("aria-valuenow", String(Math.round(share)));
      split.setAttribute("aria-valuetext", Math.round(share) + "% timeline");
      return share;
    }
    function current() {
      return stage.clientHeight ? tlBlock.getBoundingClientRect().height / stage.clientHeight * 100 : 30;
    }
    function save() {
      try { localStorage.setItem(SHARE_KEY, stage.style.getPropertyValue("--timeline-share")); } catch (_) {}
    }
    try {
      const saved = parseFloat(localStorage.getItem(SHARE_KEY));
      if (Number.isFinite(saved)) apply(saved);
    } catch (_) {}
    let drag = null;
    split.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      drag = { y: event.clientY, share: current(), height: stage.clientHeight };
      split.setPointerCapture(event.pointerId);
      split.classList.add("is-drag");
      event.preventDefault();
    });
    split.addEventListener("pointermove", (event) => {
      if (drag && drag.height) apply(drag.share + (drag.y - event.clientY) / drag.height * 100);
    });
    function finish() { if (drag) save(); drag = null; split.classList.remove("is-drag"); }
    split.addEventListener("pointerup", finish);
    split.addEventListener("pointercancel", finish);
    split.addEventListener("lostpointercapture", finish);
    split.addEventListener("keydown", (event) => {
      const step = event.shiftKey ? 10 : 3;
      if (event.key === "ArrowUp") apply(current() + step);
      else if (event.key === "ArrowDown") apply(current() - step);
      else if (event.key === "Home") apply(20);
      else if (event.key === "End") apply(65);
      else return;
      event.preventDefault(); save();
    });
    split.addEventListener("focus", () => {
      split.setAttribute("aria-valuenow", String(Math.round(current())));
    });
  }

  // ========================================================================
  // Resizable chat pane (sketch: "draggable width with max width limit").
  // ========================================================================
  function setWidth(px) {
    const w = Math.max(CHAT_MIN, Math.min(CHAT_MAX, Math.round(px)));
    document.documentElement.style.setProperty("--chat-w", w + "px");
    const grip = document.getElementById("rgrip");
    if (grip) {
      grip.setAttribute("aria-valuemin", String(CHAT_MIN));
      grip.setAttribute("aria-valuemax", String(CHAT_MAX));
      grip.setAttribute("aria-valuenow", String(w));
      grip.setAttribute("aria-valuetext", w + " pixels");
    }
    return w;
  }
  function restoreWidth() {
    let saved = 0;
    try { saved = parseInt(localStorage.getItem(WIDTH_KEY) || "", 10); } catch (_) {}
    setWidth(saved || CHAT_DEFAULT);
  }
  function wireGrip() {
    const grip = document.getElementById("rgrip");
    const body = document.querySelector(".session__body");
    if (!grip || !body) return;
    let dragging = false;
    const move = (e) => {
      if (!dragging) return;
      e.preventDefault();
      setWidth(e.clientX - body.getBoundingClientRect().left);
    };
    const up = () => {
      if (!dragging) return;
      dragging = false;
      grip.classList.remove("is-drag");
      document.body.style.userSelect = "";
      const w = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--chat-w"), 10);
      try { localStorage.setItem(WIDTH_KEY, String(w)); } catch (_) {}
    };
    grip.addEventListener("mousedown", (e) => {
      dragging = true;
      grip.classList.add("is-drag");
      // Without this a drag across the thread selects every message it crosses.
      document.body.style.userSelect = "none";
      e.preventDefault();
    });
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    // Keyboard: the grip is a focusable separator, so it has to be operable.
    grip.addEventListener("keydown", (e) => {
      const step = e.shiftKey ? 40 : 12;
      const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--chat-w"), 10) || CHAT_DEFAULT;
      if (e.key === "Home") { setWidth(CHAT_MIN); e.preventDefault(); }
      else if (e.key === "End") { setWidth(CHAT_MAX); e.preventDefault(); }
      else if (e.key === "ArrowLeft") { setWidth(cur - step); e.preventDefault(); }
      else if (e.key === "ArrowRight") { setWidth(cur + step); e.preventDefault(); }
      else return;
      try { localStorage.setItem(WIDTH_KEY, getComputedStyle(document.documentElement).getPropertyValue("--chat-w").trim().replace("px", "")); } catch (_) {}
    });
  }

  // ========================================================================
  // Measurement — the only honest source of clip lengths.
  // ========================================================================
  /**
   * Real duration for a media URL, or null until it is known.
   *
   * One probe per URL for the life of the page. A miss returns null and the
   * caller draws a pending clip; it must never fall back to a plausible number,
   * because a plausible number here reads exactly like a measured one.
   */
  function probe(url) {
    if (!url) return null;
    if (Object.prototype.hasOwnProperty.call(tl.probes, url)) {
      const v = tl.probes[url];
      return typeof v === "number" ? v : null;
    }
    tl.probes[url] = null;
    const a = document.createElement("audio");
    a.preload = "metadata";
    a.addEventListener("loadedmetadata", () => {
      const d = Number(a.duration);
      tl.probes[url] = Number.isFinite(d) && d > 0 ? d : "failed";
      if (tl.snap) draw(tl.snap);
    });
    // A failed load is a fact worth showing, not a measurement in progress —
    // remember it as its own state so the clip can say what actually happened.
    a.addEventListener("error", () => { tl.probes[url] = "failed"; if (tl.snap) draw(tl.snap); });
    a.src = mediaSrc(url);
    return null;
  }
  function probeFailed(url) {
    return !!url && tl.probes[url] === "failed";
  }

  // ========================================================================
  // Snapshot -> lanes. Everything below reads state that already exists.
  // ========================================================================
  function activeCandidate(st) {
    const cs = st.candidates || [];
    // An audition is the user pointing at a take: that take is the subject
    // until they stop, whatever the session has selected.
    const auditioned = cs.find((c) => c.candidate_id === tl.auditionId);
    if (auditioned) return auditioned;
    if (st.selected_candidate_id) {
      const hit = cs.find((c) => c.candidate_id === st.selected_candidate_id);
      if (hit) return hit;
    }
    // Nothing locked yet: the newest completed take is what the mix would use.
    const done = cs.filter((c) => c.status === "completed");
    return done.length ? done[done.length - 1] : (cs[cs.length - 1] || null);
  }

  function buildLanes(st, duration) {
    const mix = st.mix || {};
    const layers = st.layers || {};
    const out = {};
    // Clip to the video's length — but only when that length is actually known.
    // With no duration yet, Math.min(x, 0) would collapse every clip to zero and
    // draw the session as empty; an unknown ceiling means no ceiling.
    const cap = (v) => (duration > 0 ? Math.min(v, duration) : v);

    // ---- music: one bed. No interval in the data, so it is the take's own
    // measured length, anchored at 0 and clipped to the video.
    const cand = activeCandidate(st);
    out.music = [];
    if (cand) {
      const url = cand.audio_url || null;
      const measured = probe(url);
      const ready = cand.status === "completed" && measured != null;
      const broke = ["failed", "error"].includes(cand.status) || !!cand.stalled_seconds || probeFailed(url);
      out.music.push({
        id: "music:" + cand.candidate_id,
        start: 0,
        end: ready ? cap(measured) : null,
        title: cand.title || "Music",
        pending: !ready,
        failed: broke,
        note: cand.status === "completed"
          ? (probeFailed(url) ? "audio unavailable — the take's link may have expired" : "measuring…")
          : cand.status,
        locked: st.selected_candidate_id === cand.candidate_id,
        url: url,
      });
    }

    // ---- voiceover. Two shapes, drawn as what each one is:
    //   segments[] — the timed plan (video-informed narration). Each start is
    //     real; a segment that carries duration_s gets its interval, the rest
    //     draw as timed moments. The plan bakes its own timing, so the mix's
    //     global voiceover_start_s does not apply here.
    //   flat script — one take: start from mix.voiceover_start_s, end measured
    //     off the recorded audio.
    out.voiceover = [];
    const vo = layers.voiceover;
    if (vo && (vo.segments || []).length) {
      vo.segments.forEach((s, i) => {
        const start = Number(s.start_s || 0) || 0;
        const durS = Number(s.duration_s || 0) || 0;
        out.voiceover.push({
          id: "vo:" + (s.id || "seg_" + i),
          start: start,
          end: durS > 0 ? cap(start + durS) : null,
          title: firstWords(s.text || "Line", 4),
          moment: durS <= 0,
          note: s.delivery || (vo.status === "completed" ? "" : vo.status),
          url: null,
        });
      });
    } else if (vo) {
      const start = Number(mix.voiceover_start_s || 0) || 0;
      const measured = probe(vo.audio_url || null);
      const ready = vo.status === "completed" && measured != null;
      out.voiceover.push({
        id: "vo:" + (vo.linked_job_id || "draft"),
        start: start,
        end: ready ? cap(start + measured) : null,
        title: vo.script ? firstWords(vo.script, 4) : "Voiceover",
        pending: !ready,
        failed: ["failed", "error"].includes(vo.status) || probeFailed(vo.audio_url),
        note: vo.status === "draft" ? "script only — not recorded"
          : (vo.status === "completed" && probeFailed(vo.audio_url) ? "audio unavailable" : vo.status),
        url: vo.audio_url || null,
      });
    }

    // ---- sfx. The planned layer is a dict: spotted events (a start each, by
    // design no length) and an optional continuous ambience bed. A legacy list
    // of {start_s, end_s} clips still draws as intervals.
    out.sfx = [];
    const sfx = layers.sfx;
    if (sfx && typeof sfx === "object" && !Array.isArray(sfx)) {
      if ((sfx.ambience || "").trim()) {
        out.sfx.push({
          id: "sfx:ambience",
          start: 0,
          // A bed runs under the whole picture — but only if we know how long
          // the picture is. With no duration yet this printed "0:00–0:00",
          // which is an interval the view never measured.
          end: duration > 0 ? duration : null,
          title: "Ambience — " + sfx.ambience.trim(),
          bed: true,
          url: null,
        });
      }
      (sfx.events || []).forEach((ev, i) => {
        out.sfx.push({
          id: "sfx:" + (ev.id || i),
          start: Number(ev.start_s || 0) || 0,
          end: null,
          title: ev.label || ev.prompt || "Effect",
          moment: true,
          note: ev.reason || "",
          url: null,
        });
      });
    } else if (Array.isArray(sfx)) {
      sfx.forEach((s, i) => {
        const start = Number(s.start_s || 0) || 0;
        const measured = s.end_s == null ? probe(s.audio_url || null) : null;
        const end = s.end_s != null ? Number(s.end_s)
          : (measured != null ? start + measured : null);
        out.sfx.push({
          id: "sfx:" + (s.id || i),
          start: start,
          end: end == null ? null : cap(end),
          title: s.label || s.name || "Effect",
          pending: end == null,
          note: "loading…",
          url: s.audio_url || null,
        });
      });
    }

    return out;
  }

  function firstWords(script, n) {
    const words = String(script).trim().split(/\s+/);
    return words.slice(0, n).join(" ") + (words.length > n ? "…" : "");
  }

  // ========================================================================
  // Render
  // ========================================================================
  function render(snap) {
    // Locking a take ends the audition it came from: the session's own choice
    // is now the subject, and the take's <audio> is no longer the clock.
    const selected = ((snap || {}).state || {}).selected_candidate_id;
    if (selected !== tl.lastSelection) {
      if (tl.audio) { try { tl.audio.pause(); } catch (_) {} }
      tl.audio = null; tl.auditionId = null; tl.lastSelection = selected;
    }
    tl.snap = snap;
    if (!tl.booted) boot();
    if (snap && snap.session_id && snap.session_id !== tl.sid) {
      tl.sid = snap.session_id;
      tl.videoKey = null; tl.timelineKey = null; tl.selected = null; tl.muted = {};
      stopLayerAudio(); tl.layerMedia = {};
      // Failed probes get a fresh chance per session — a re-opened session may
      // carry re-signed URLs for the same media.
      Object.keys(tl.probes).forEach((u) => { if (tl.probes[u] === "failed") delete tl.probes[u]; });
    }
    if (tl.view !== "timeline") return;   // canvas is up; nothing to paint
    draw(snap);
  }

  // ----- layer audio: the lanes must be HEARABLE, not just visible --------
  // Before a mix exists, the hero is the raw source video and the layers were
  // silent scenery — a lit "Voiceover" chip over audio you could not hear. The
  // engine plays each audible lane's timeline-aligned track in lockstep with
  // the hero. Once the hero IS a take preview or the composed mix, that file
  // already carries the layer audio, and playing it twice would phase.
  function ensureLayerAudio(st) {
    const layers = st.layers || {};
    const vo = layers.voiceover || {};
    const sfx = layers.sfx || {};
    const cand = activeCandidate(st) || {};
    const heroUrl = tl.videoKey || "";
    const heroIsMix = !!((st.final_artifact || {}).video_url || (st.mix || {}).video_url);
    const heroIsTake = !heroIsMix && !!heroUrl &&
      (heroUrl === (cand.remixed_video_url || "") || heroUrl === (cand.video_url || ""));
    const wanted = {};
    if (!heroIsMix) {
      if (!heroIsTake && cand.status === "completed" && cand.audio_url) wanted.music = cand.audio_url;
      if (vo.status === "completed" && vo.audio_url) wanted.voiceover = vo.audio_url;
      if (sfx && sfx.audio_url) wanted.sfx = sfx.audio_url;
    }
    tl.layerMedia = tl.layerMedia || {};
    Object.entries(wanted).forEach(([lane, url]) => {
      const cur = tl.layerMedia[lane];
      if (cur && cur.url === url) return;
      if (cur) { try { cur.audio.pause(); } catch (_) {} }
      const audio = new Audio(mediaSrc(url));
      audio.preload = "auto";
      tl.layerMedia[lane] = { url, audio };
    });
    Object.keys(tl.layerMedia).forEach((lane) => {
      if (!wanted[lane]) {
        try { tl.layerMedia[lane].audio.pause(); } catch (_) {}
        delete tl.layerMedia[lane];
      }
    });
  }

  function syncLayerAudio() {
    const v = tl.video;
    if (!v) return;
    Object.entries(tl.layerMedia || {}).forEach(([lane, m]) => {
      const a = m.audio;
      a.muted = !!tl.muted[lane];
      try {
        if (v.paused || v.ended) { if (!a.paused) a.pause(); return; }
        if (Math.abs((a.currentTime || 0) - v.currentTime) > 0.3) a.currentTime = v.currentTime;
        if (a.paused) a.play().catch(() => {});
      } catch (_) { /* an unloadable track must not break the hero */ }
    });
  }

  function stopLayerAudio() {
    Object.values(tl.layerMedia || {}).forEach((m) => { try { m.audio.pause(); } catch (_) {} });
  }

  function draw(snap) {
    if (!snap || !tl.booted) return;
    const st = snap.state || {};
    const obs = st.observation || null;
    // Prefer the analysis, then the source metadata, then the hero's own
    // measured length — the lanes and the playhead must share one clock.
    const duration = Number((obs && obs.duration_s) || 0) || sourceDuration(st)
      || (tl.video && Number.isFinite(tl.video.duration) ? tl.video.duration : 0) || 0;
    drawScreen(st, obs);
    ensureLayerAudio(st);
    const lanes = buildLanes(st, duration);
    drawBar(st, lanes, duration);
    drawTimeline(st, obs, lanes, duration);
    // The timeline block may have skipped a rebuild (nothing changed but the
    // selection); the mute filter and the playhead still have to be current.
    applyMutes();
    movePlayhead();
  }

  function sourceDuration(st) {
    const v = (st.source_video || {}).duration_s;
    return Number(v || 0) || 0;
  }

  /** The hero video. Memoized on its URL — rebuilding it would restart playback
   *  on every 2.5s snapshot poll while a generation is in flight. */
  function drawScreen(st, obs) {
    const src = st.source_video || {};
    // Prefer the composed result: once there is a mix, "the video" IS the mix,
    // and watching the silent source while a timeline shows audio under it
    // would be the console lying about what you are hearing. While a take is
    // being auditioned, that take's own picture wins — it is what is playing.
    const fin = st.final_artifact || {};
    const mix = st.mix || {};
    const cand = activeCandidate(st) || {};
    const url = (!tl.auditionId && (fin.video_url || mix.video_url))
      || cand.remixed_video_url || cand.video_url || src.url || "";
    if (tl.videoKey === url) return;
    tl.videoKey = url;
    screen.innerHTML = "";
    if (!url) {
      screen.appendChild(el("div", "tl-screen__empty",
        '<i class="ti ti-movie"></i><span>Your video appears here once it has been uploaded and analyzed.</span>'));
      tl.video = null;
      playBtn.disabled = true;
      return;
    }
    const v = document.createElement("video");
    v.src = mediaSrc(url);
    v.playsInline = true;
    v.preload = "metadata";
    if (src.poster_url || (obs && obs.thumbnail_url)) v.poster = mediaSrc(src.poster_url || (obs && obs.thumbnail_url));
    v.addEventListener("timeupdate", () => { movePlayhead(); syncLayerAudio(); });
    v.addEventListener("loadedmetadata", () => { movePlayhead(); if (tl.snap) draw(tl.snap); });
    v.addEventListener("play", () => {
      setPlayIcon(true);
      // One thing plays at a time: the hero takes the clock back from a take
      // card's audition.
      const host = window.__edenn;
      if (host && host.app && host.app._takeAudio) { try { host.app._takeAudio.pause(); } catch (_) {} }
      syncLayerAudio();
      // timeupdate fires ~4×/s; a frame loop is what makes the playhead glide.
      cancelAnimationFrame(tl.frame);
      const tick = () => {
        movePlayhead();
        if (tl.video === v && !v.paused && tl.view === "timeline" && !document.hidden) tl.frame = requestAnimationFrame(tick);
      };
      tick();
    });
    v.addEventListener("pause", () => { setPlayIcon(false); cancelAnimationFrame(tl.frame); syncLayerAudio(); });
    v.addEventListener("seeked", syncLayerAudio);
    v.addEventListener("ended", () => { setPlayIcon(false); cancelAnimationFrame(tl.frame); stopLayerAudio(); });
    // A URL that will not load has to say so. The old side rail put the same
    // url in a <video> and left a black rectangle when it failed — survivable
    // when it was a 200px thumbnail, not when it is the centre of the screen.
    // The devserver's seeded fixture (https://cdn.test/source.mp4) is exactly
    // this case, so the state is reachable in normal local development.
    v.addEventListener("error", () => showScreenError(url));
    screen.appendChild(v);
    tl.video = v;
    playBtn.disabled = false;
  }

  function showScreenError(url) {
    tl.video = null;
    playBtn.disabled = true;
    setPlayIcon(false);
    screen.innerHTML = "";
    const box = el("div", "tl-screen__empty",
      '<i class="ti ti-alert-triangle"></i><span>This video could not be loaded, so there is nothing to play against the timeline. '
      + "The lanes below still describe the audio the session holds.</span>");
    const where = el("div", null, esc(String(url).slice(0, 90)));
    where.style.cssText = "font-size:11px;opacity:.55;word-break:break-all;max-width:420px";
    box.appendChild(where);
    screen.appendChild(box);
  }

  function setPlayIcon(on) {
    playBtn.setAttribute("aria-label", on ? "Pause preview" : "Play preview");
    playBtn.dataset.playing = String(on);
  }
  function togglePlay() {
    const media = tl.audio || tl.video;
    if (!media) return;
    if (media.paused) media.play().catch(() => {});
    else media.pause();
  }
  /** Host hook: leaving the session view must not leave anything playing. */
  function pause() {
    if (tl.video) { try { tl.video.pause(); } catch (_) {} }
    if (tl.audio) { try { tl.audio.pause(); } catch (_) {} }
    cancelAnimationFrame(tl.frame);
    stopLayerAudio();
  }

  function drawBar(st, lanes, duration) {
    const obs = st.observation || {};
    const title = barTitle.querySelector(".tl-bar__title");
    const sub = barTitle.querySelector(".tl-bar__sub");
    title.textContent = obs.video_title || "source video";
    const scenes = (obs.scenes || []).length;
    // "Composed" means composed MEDIA exists — the real backend persists a
    // final_artifact record ({status:"planned"|"queued"}) before any render,
    // and claiming a mix the hero cannot show would be the bar lying.
    const fin = st.final_artifact || {};
    const composed = fin.video_url || fin.audio_url || (st.mix || {}).video_url;
    sub.textContent = [
      duration ? clock(duration) : null,
      scenes ? scenes + " scenes" : null,
      composed ? "composed mix" : "source audio",
      tl.auditionId ? "auditioning a take" : null,
    ].filter(Boolean).join(" · ");
    // The compact transport has no room for the summary line; keep it on the
    // bar itself so hovering still answers "what am I listening to?".
    bar.title = (title.textContent ? title.textContent + " · " : "") + sub.textContent;
    playBtn.disabled = !tl.audio && !tl.video;
    startBtn.disabled = endBtn.disabled = playBtn.disabled;
    updateClock();

    // Mutes are a listening aid, not a mix edit — they gate the preview only,
    // and they only appear once there is something on a lane to gate.
    const anyClips = LANES.some((lane) => (lanes[lane.id] || []).length > 0);
    muteBox.hidden = !anyClips;
    muteBox.innerHTML = "";
    LANES.forEach((lane) => {
      const has = (lanes[lane.id] || []).length > 0;
      const b = el("button", "tl-mute lane-" + lane.id + (tl.muted[lane.id] ? " is-off" : ""));
      b.type = "button";
      b.disabled = !has;
      b.title = has ? "Mute " + lane.label.toLowerCase() + " in the preview" : lane.label + " — nothing on this lane yet";
      b.innerHTML = '<span class="tl-mute__sw"></span>' + esc(lane.label);
      b.addEventListener("click", () => {
        tl.muted[lane.id] = !tl.muted[lane.id];
        b.classList.toggle("is-off", !!tl.muted[lane.id]);
        applyMutes();
        syncLayerAudio();
      });
      muteBox.appendChild(b);
    });
    applyMutes();
  }

  /**
   * The composed video carries every layer in one audio track, so a per-lane
   * mute cannot be honoured on it. Say so rather than presenting a control that
   * silently does nothing.
   */
  function applyMutes() {
    const anyMuted = LANES.some((l) => tl.muted[l.id]);
    // Before a mix exists the lanes play as separate synced tracks, so a lane
    // mute is REAL in the preview. Once the composed mix is the hero it is one
    // baked track, and the mute goes back to being a view filter — say so.
    const heroIsMix = !!(((tl.snap || {}).state || {}).final_artifact || {}).video_url
      || !!((((tl.snap || {}).state || {}).mix || {}).video_url);
    if (tl.video && anyMuted && heroIsMix && !applyMutes._warned) {
      applyMutes._warned = true;
      const host = window.__edenn;
      if (host && host.toast) {
        host.toast("The preview plays the composed mix as one track — per-lane mute is a view filter here, not a mix change.");
      }
    }
    LANES.forEach((l) => {
      const row = tlBlock && tlBlock.querySelector(".tl-row.lane-" + l.id);
      if (row) row.style.opacity = tl.muted[l.id] ? "0.4" : "";
    });
  }

  function drawTimeline(st, obs, lanes, duration) {
    // Selecting a clip must not rebuild the block underneath the pointer: the
    // lanes are keyed on what they actually draw, and selection is applied in
    // place (see buildClip).
    const key = JSON.stringify([obs, lanes, duration, st.spotting_sheet || null,
      (st.production_plan || {}).layers || null]);
    if (tl.timelineKey === key) return;
    tl.timelineKey = key;
    const focused = tlBlock.contains(document.activeElement) ? document.activeElement.dataset.clipId : null;

    tlBlock.innerHTML = "";
    const hd = el("div", "tl-tl__hd");
    hd.appendChild(el("span", "tl-tl__title", "Timeline"));
    hd.appendChild(el("span", "spacer"));
    hd.appendChild(el("span", "tl-tl__note",
      duration ? "Intervals are measured from the audio itself" : "Waiting on the video"));
    tlBlock.appendChild(hd);

    const total = duration || 1;
    const pct = (t) => Math.max(0, Math.min(100, (t / total) * 100));
    const detected = (obs && obs.scenes) || [];
    // A clock but no analysis yet: show the shape a cut of this length usually
    // has, labelled as illustrative. It draws no cut marks — see below.
    const previewScenes = !detected.length && duration > 0;
    const scenes = previewScenes
      ? ["Opening", "Build", "Reveal", "Closing"].map((label, index) => ({
        label, start_s: duration * index / 4, end_s: duration * (index + 1) / 4,
      }))
      : detected;

    // ---- scene ruler ------------------------------------------------------
    const sceneRow = el("div", "tl-row");
    sceneRow.appendChild(el("div", "tl-row__lbl",
      '<span class="tl-row__nm" style="color:var(--text-tertiary);font-weight:700;font-size:10.5px;letter-spacing:.06em;text-transform:uppercase">Scenes</span>'));
    if (previewScenes) {
      sceneRow.querySelector(".tl-row__nm").textContent = "Scene preview";
      sceneRow.title = "Illustrative scene layout; video scene boundaries are not available yet.";
    }
    const ruler = el("div", "tl-scenes");
    if (!scenes.length) {
      ruler.appendChild(el("div", "tl-scene", "<span>analyzing…</span>"));
    } else {
      scenes.forEach((sc, i) => {
        const w = ((sceneEnd(sc) - sceneStart(sc)) / total) * 100;
        const raw = sceneLabel(sc);
        const text = raw ? sceneName(raw) : "Scene " + ((sc.index != null ? sc.index : i) + 1);
        const seg = el("div", "tl-scene", "<span>" + esc(text) + "</span>");
        seg.style.width = w + "%";
        seg.title = `${previewScenes ? "Illustrative · " : ""}${stamp(sceneStart(sc))}–${stamp(sceneEnd(sc))} · ${raw}`;
        ruler.appendChild(seg);
      });
    }
    sceneRow.appendChild(ruler);
    tlBlock.appendChild(sceneRow);

    // ---- spotting sheet (real backend only; the mock has no sheet) --------
    // One moment list, one owner per moment: the ruler's annotation layer.
    // Speech/keep-out moments carry a real window and draw as spans; the rest
    // are instants. Owners wear the lane colour of the layer they assign.
    const moments = ((st.spotting_sheet || {}).moments || []);
    if (moments.length) {
      const momRow = el("div", "tl-row");
      momRow.appendChild(el("div", "tl-row__lbl",
        '<span class="tl-row__nm" style="color:var(--text-tertiary);font-weight:700;font-size:10.5px;letter-spacing:.06em;text-transform:uppercase">Moments</span>'));
      const strip = el("div", "tl-moments");
      moments.forEach((m) => {
        const t = Number(m.t || 0) || 0;
        const win = Array.isArray(m.window) ? m.window : null;
        const isSpan = m.source === "source_audio" && win && Number(win[1]) > Number(win[0]);
        const pin = el("div", "tl-mom " + (OWNER_LANE[m.owner] || "") + (isSpan ? " tl-mom--span" : ""));
        if (isSpan) {
          pin.style.left = pct(Number(win[0])) + "%";
          pin.style.width = Math.max(0.8, pct(Number(win[1])) - pct(Number(win[0]))) + "%";
        } else {
          pin.style.left = pct(t) + "%";
        }
        pin.title = `${stamp(t)} · ${m.what || ""} · ${m.owner || ""}`
          + (m.owner_source === "footage" ? " (the footage's own sound)" : "");
        strip.appendChild(pin);
      });
      momRow.appendChild(strip);
      tlBlock.appendChild(momRow);
    }

    // ---- lanes ------------------------------------------------------------
    // Each lane is its own grid row so its label lines up with the ruler above;
    // the cut marks and playhead are absolutely positioned over the whole stack
    // so they cross every lane rather than stopping at one.
    tlBlock.appendChild(buildLaneRows(lanes, pct, total));

    const stack = tlBlock.querySelector(".tl-lanes");
    if (stack) {
      // Only MEASURED cuts cross the lanes. An illustrative ruler stays in its
      // own strip, where the header says what it is; a cut mark drawn down the
      // lanes would be indistinguishable from an analyzed one.
      (previewScenes ? [] : scenes.slice(1)).forEach((sc) => {
        const cut = el("div", "tl-cut");
        cut.style.left = pct(sceneStart(sc)) + "%";
        stack.appendChild(cut);
      });
      const ph = el("div", "tl-ph");
      ph.id = "tl-ph";
      ph.style.left = "0%";
      stack.appendChild(ph);
    }

    // ---- time ticks -------------------------------------------------------
    // Only over a real clock. With no known duration the axis would read
    // 0:00 · 0:00 · 0:00 · 0:00 · 0:01 off the `|| 1` guard above — a fabricated
    // one-second video, which is exactly the kind of plausible-looking invention
    // this view refuses to make. The header already says "Waiting on the video".
    if (duration) {
      const tickRow = el("div", "tl-row");
      tickRow.appendChild(el("div", "tl-row__lbl", ""));
      const ticks = el("div", "tl-ticks");
      const n = 4;
      for (let i = 0; i <= n; i += 1) {
        const t = (total / n) * i;
        const k = el("span", "tl-tick", clock(t));
        k.style.left = (i / n) * 100 + "%";
        ticks.appendChild(k);
      }
      tickRow.appendChild(ticks);
      tlBlock.appendChild(tickRow);
    }

    updateTimelineLayout();
    if (focused) {
      const button = Array.from(tlBlock.querySelectorAll("[data-clip-id]")).find((node) => node.dataset.clipId === focused);
      if (button) button.focus({ preventScroll: true });
    }
    movePlayhead();
    applyMutes();
  }

  function buildLaneRows(lanes, pct, total) {
    const holder = document.createDocumentFragment();
    const stack = el("div", "tl-lanes");
    const labels = el("div", "tl-lanewrap");

    LANES.forEach((lane) => {
      const clips = lanes[lane.id] || [];
      const row = el("div", "tl-row lane-" + lane.id);
      const lbl = el("div", "tl-row__lbl");
      lbl.setAttribute("aria-label", lane.label);
      lbl.title = lane.label;
      lbl.appendChild(el("span", "tl-row__ic", `<i class="ti ${lane.icon}"></i>`));
      lbl.appendChild(el("span", "tl-row__nm", esc(lane.label)));
      row.appendChild(lbl);

      const laneEl = el("div", "tl-lane" + (clips.length ? "" : " is-empty"));
      if (!clips.length) {
        laneEl.appendChild(el("div", "tl-lane__hint", esc(emptyHint())));
        laneEl.title = nextStep(lane.id);
      }
      // Clips that overlap in time stack into slots rather than hiding each
      // other; a clip with no measured length reserves the width its nub draws
      // at, so a run of spotted effects shares one row instead of one each.
      const ends = [];
      clips.slice().sort((a, b) => a.start - b.start).forEach((c) => {
        const occupies = c.end == null ? c.start + Math.max(total * 0.06, 0.5) : c.end;
        let slot = ends.findIndex((end) => end <= c.start);
        if (slot < 0) slot = Math.min(ends.length, MAX_SLOTS - 1);
        ends[slot] = occupies;
        const clip = buildClip(c, lane, pct, total);
        clip.style.setProperty("--clip-slot", slot);
        laneEl.appendChild(clip);
      });
      laneEl.dataset.slots = String(Math.max(1, Math.min(MAX_SLOTS, ends.length)));
      laneEl.style.setProperty("--lane-slots", laneEl.dataset.slots);
      // Seeking lives on the lane track itself, not on a transparent sheet laid
      // over everything: an overlay wide enough to catch a click anywhere on the
      // clock is also wide enough to swallow every clip underneath it, and then
      // clips stop being clickable at all. A clip's own handler stops
      // propagation, so a click either selects a clip or moves the playhead —
      // never both, and never neither. The label gutter is outside this element,
      // which is what keeps it out of the clock.
      laneEl.addEventListener("click", (e) => {
        const media = tl.audio || tl.video;
        if (!media || !total) return;
        const r = laneEl.getBoundingClientRect();
        if (!r.width) return;
        media.currentTime = Math.max(0, Math.min(total, ((e.clientX - r.left) / r.width) * total));
        movePlayhead();
      });
      row.appendChild(laneEl);
      labels.appendChild(row);
    });

    // The lane rows are the grid; the overlay stack sits on top of just their
    // track column (see --tl-gutter in styles.css), which is why it is nested
    // inside a full-width row rather than being a sibling of the rows.
    const wrap = el("div", "tl-row");
    wrap.style.gridTemplateColumns = "1fr";
    labels.appendChild(stack);
    wrap.appendChild(labels);
    holder.appendChild(wrap);
    return holder;
  }

  /** The lane label already names the layer; the empty state only has to say
   *  that nothing is on it. What to do about that lives in the tooltip. */
  function emptyHint() { return "Not added"; }
  function nextStep(laneId) {
    if (laneId === "music") return "No take yet — ask for music in the chat.";
    if (laneId === "voiceover") return "No narration — ask for a voice-over in the chat.";
    return "No sound design yet — ask for sound effects in the chat.";
  }

  function buildClip(c, lane, pct, total) {
    const node = el("button", "tl-clip"
      + (c.pending ? " is-pending" : "")
      + (c.failed ? " is-error" : "")
      + (c.moment ? " is-moment" : "")
      + (c.bed ? " is-bed" : "")
      + (tl.selected === c.id ? " is-sel" : ""));
    node.type = "button";
    node.dataset.clipId = c.id;
    node.setAttribute("aria-pressed", String(tl.selected === c.id));
    const start = pct(c.start);
    node.style.left = start + "%";
    if (c.end == null) {
      // Unknown length: a fixed nub at the known start. Never a guessed width
      // — .tl-clip.is-unmeasured gives it one size, whatever the clock says.
      node.classList.add("is-unmeasured");
    } else {
      node.style.width = Math.max(1.5, pct(c.end) - start) + "%";
    }

    // The paint lives on the surface, so a clip can be a hairline on the clock
    // and still carry a readable label.
    const surface = el("span", "tl-clip__surface");
    if (!c.pending && !c.moment && !c.bed && c.end != null) {
      const wave = el("div", "tl-clip__wave");
      // Deterministic from the clip id — a bar pattern that reshuffles on every
      // 2.5s poll reads as the audio changing when nothing has.
      let seed = 0;
      for (let i = 0; i < c.id.length; i += 1) seed = (seed * 31 + c.id.charCodeAt(i)) >>> 0;
      for (let i = 0; i < 40; i += 1) {
        seed = (seed * 1664525 + 1013904223) >>> 0;
        const bar = document.createElement("i");
        bar.style.height = (18 + (seed % 70)) + "%";
        wave.appendChild(bar);
      }
      surface.appendChild(wave);
    }
    surface.appendChild(el("span", "tl-clip__nm", esc(c.title)));
    surface.appendChild(el("span", "tl-clip__t",
      c.moment ? stamp(c.start)
        : c.end == null ? esc(c.note || "pending")
          : `${stamp(c.start)}–${stamp(c.end)}`));
    node.appendChild(surface);

    const description = c.moment
      ? `${lane.label} · ${stamp(c.start)} · ${c.title}` + (c.note ? ` — ${c.note}` : "")
      : c.end == null
        ? `${lane.label} · starts ${stamp(c.start)} · length not known yet (${c.note || "pending"})`
        : `${lane.label} · ${stamp(c.start)}–${stamp(c.end)} · ${c.title}`;
    node.title = description;
    node.setAttribute("aria-label", description);
    node.addEventListener("click", (e) => {
      // The lane under this clip seeks on click; without this the same click
      // would both select the clip and drag the playhead to wherever it landed.
      e.stopPropagation();
      tl.selected = tl.selected === c.id ? null : c.id;
      const media = tl.audio || tl.video;
      // Finite check, not truthiness: a clip at 0:00 seeks like any other.
      if (media && Number.isFinite(c.start)) media.currentTime = c.start;
      // Applied in place: rebuilding the block here would throw away the
      // layout (and the scroll position) the user is pointing at.
      tlBlock.querySelectorAll("[data-clip-id]").forEach((clip) => {
        const selected = clip.dataset.clipId === tl.selected;
        clip.classList.toggle("is-sel", selected);
        clip.setAttribute("aria-pressed", String(selected));
      });
      movePlayhead();
    });
    return node;
  }

  function movePlayhead() {
    const ph = document.getElementById("tl-ph");
    const st = (tl.snap && tl.snap.state) || {};
    const obs = st.observation || {};
    const total = Number(obs.duration_s || 0) || sourceDuration(st) || (tl.video && tl.video.duration) || 0;
    const media = tl.audio || tl.video;
    if (ph && total && media && ph.parentElement) {
      ph.style.transform = "translateX("
        + ((Math.min(media.currentTime, total) / total) * ph.parentElement.clientWidth) + "px)";
    }
    updateClock();
  }
  function updateClock() {
    if (!clockEl) return;
    const st = (tl.snap && tl.snap.state) || {};
    const total = Number((st.observation || {}).duration_s || 0) || (tl.video && tl.video.duration) || 0;
    const media = tl.audio || tl.video;
    clockEl.textContent = clock(media ? media.currentTime : 0) + " / " + clock(total);
  }

  // ========================================================================
  // Audition — a take card's play button hands its <audio> over, and that
  // track becomes the timeline's clock until the session selects something.
  // ========================================================================
  function audition(id, audio) {
    const candidates = ((tl.snap || {}).state || {}).candidates || [];
    if (!candidates.some((candidate) => candidate.candidate_id === id)) return;
    // The hero (and the synced lane tracks under it) stand down: one thing
    // plays at a time, or the take phases against the mix. Everything EXCEPT
    // the incoming track — resuming an audition (tab focus) passes the very
    // audio that is playing, and a blanket pause() would silence it.
    if (tl.video) { try { tl.video.pause(); } catch (_) {} }
    if (tl.audio && tl.audio !== audio) { try { tl.audio.pause(); } catch (_) {} }
    stopLayerAudio();
    tl.audio = audio; tl.auditionId = id;
    if (tl.view === "timeline" && tl.snap) draw(tl.snap);
    setPlayIcon(true);
    cancelAnimationFrame(tl.frame);
    const tick = () => {
      if (tl.audio !== audio) return;
      movePlayhead(); setPlayIcon(!audio.paused);
      if (!audio.paused && tl.view === "timeline" && !document.hidden) tl.frame = requestAnimationFrame(tick);
    };
    tick();
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && tl.audio && !tl.audio.paused) audition(tl.auditionId, tl.audio);
  });

  // ========================================================================
  // View switching (driven by app.js — it owns the toggle).
  // ========================================================================
  function setView(view) {
    tl.view = view === "canvas" ? "canvas" : "timeline";
    const on = tl.view === "timeline";
    if (stage) stage.hidden = !on;
    if (!on) pause();
    if (on && tl.snap) draw(tl.snap);
    if (on && tl.audio && !tl.audio.paused) audition(tl.auditionId, tl.audio);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  window.EdennTimeline = { render, setView, boot, pause, audition };
})();
