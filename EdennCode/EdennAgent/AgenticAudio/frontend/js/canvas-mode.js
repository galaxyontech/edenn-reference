/* ============================================================================
 * Edenn — canvas mode (Phase 1, additive, read-only).
 *
 * A second view for a session: an infinite, pannable LINEAGE TREE of the audio
 * variants, plus a focused-take DOCK player at the bottom. It renders from the
 * exact same `snapshot.state` the chat view uses — no backend change, no new
 * data. It only does work when the user switches the right pane to Canvas.
 *
 * Integration points (all additive):
 *   - app.js `reconcile()` calls `window.EdennCanvas.render(snap)` (no-op when
 *     the timeline owns the right pane), and `setView()` when the toggle moves
 *   - reuses `window.__edenn` helpers (requestVariation/confirmSpend/toast/…) so
 *     any action routes through the identical /choices contract as the cards.
 *
 * Lineage = candidates linked by `parent_candidate_id` (branch) or `proposal_id`
 * (first generation), rooted at the source video. The locked path (the
 * `selected_candidate_id` chain) is highlighted teal; branches are indigo.
 * ========================================================================== */
(function () {
  // Same-origin media carries the page token as a query param (media elements
  // cannot send headers); identity fallback keeps the mock console untouched.
  const mediaSrc = (u) => (window.__edennMediaSrc ? window.__edennMediaSrc(u) : u);
  "use strict";

  // ---- tiny DOM utils (self-contained; mirrors app.js style) --------------
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
  function fmtTime(s) {
    s = Math.max(0, Math.floor(s || 0));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }
  const SVGNS = "http://www.w3.org/2000/svg";
  function host() { return window.__edenn || {}; }

  // ---- labels (fall back to app.js exports, with local defaults) ----------
  const MODEL_LABEL = (host().MODEL_LABEL) || { edenn_basic: "Basic", edenn_enhanced: "Enhanced", edenn_studio: "Studio" };
  // Shared with the thread view: a tier reads with the tier that was asked for
  // whenever the server substituted one (see app.js modelText).
  const modelText = host().modelText
    || ((o) => (o && (MODEL_LABEL[o.modelspec] || o.modelspec)) || "");
  const EDIT_LABEL = (host().EDIT_LABEL) || { regenerate: "new take", extend: "extended", creative_edit: "restyled" };

  // ---- layout constants ---------------------------------------------------
  const NODE_W = 210, COL_GAP = 290, ROW_GAP = 206, MARGIN_X = 48, MARGIN_Y = 36, ANCHOR_Y = 76, NODE_H = 172;
  const SOURCE_ID = "__source__";
  const VO_ID = "voiceover_layer";

  // ---- module state -------------------------------------------------------
  const cv = {
    view: "chat",          // "chat" | "canvas"
    snap: null,            // latest snapshot (cached even while on chat)
    sid: null,             // session id we last laid out (reset focus/fit on change)
    focusId: null,         // currently focused node id (drives the dock)
    fitted: false,         // have we auto-fit this session yet
    tx: MARGIN_X, ty: MARGIN_Y, scale: 0.8,
    nodes: {},             // id -> layout node (rebuilt each render)
    media: null,           // current dock <audio>/<video> element (master clock)
    mediaVideo: null,      // muted source <video> slaved to the audio (P2 watch)
    dockKey: null,         // (focusId|url|status) the dock media was last built for
    userMoved: false,      // has the user panned/zoomed since the last fit()
    autoFollow: false,     // opt-in: pan to the active card when the tree exceeds the canvas
    lastFollowed: null,    // last node id auto-followed (only re-pan on focus change)
    contextRef: null,      // { id, label } — the @-referenced version (HEAD), persists across turns
    pendingBranches: [],   // [{ parent, existing:Set }] branches awaiting their child (HEAD advance)
    pendingLockId: null,   // candidate whose lock is dispatched but not yet in a snapshot
    booted: false,
  };

  // DOM refs (filled on boot)
  let stage, vp, world, svg, legend, zoomBox, empty, dock, mainPanel, cref, acmenu;

  // Menu state for the @/​/ composer autocomplete
  const menu = { open: false, mode: null, items: [], idx: 0 };

  // Slash commands map onto the existing typed /choices contract.
  const COMMANDS = [
    { id: "branch", label: "Branch a new take", hint: "from the referenced take", icon: "ti-git-branch" },
    { id: "voiceover", label: "Add a voice-over", hint: "draft + record narration", icon: "ti-microphone" },
    { id: "export", label: "Export / download", hint: "the referenced take", icon: "ti-download" },
  ];

  // ========================================================================
  // Boot — build the stage internals + wire toggle/pan/zoom once.
  // ========================================================================
  function boot() {
    stage = document.getElementById("cstage");
    if (!stage || cv.booted) return;
    cv.booted = true;

    vp = el("div", "cstage__vp"); vp.id = "cv-vp";
    world = el("div", "cstage__world"); world.id = "cv-world";
    svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("class", "cstage__svg");
    world.appendChild(svg);
    vp.appendChild(world);

    legend = el("div", "cstage__legend",
      '<span><span class="ln lock">━</span> locked path</span><span><span class="ln branch">━</span> branches</span><span>drag to pan · scroll to zoom</span>');
    vp.appendChild(legend);

    zoomBox = el("div", "cstage__zoom");
    const zin = el("button", null, '<i class="ti ti-plus"></i>'); zin.title = "Zoom in";
    const zout = el("button", null, '<i class="ti ti-minus"></i>'); zout.title = "Zoom out";
    const zfit = el("button", null, '<i class="ti ti-maximize"></i>'); zfit.title = "Fit";
    const zfollow = el("button", null, '<i class="ti ti-focus-2"></i>'); zfollow.id = "cv-follow";
    zfollow.title = "Auto-follow the active take"; zfollow.setAttribute("aria-pressed", "false");
    zin.addEventListener("click", () => zoomBy(1.18));
    zout.addEventListener("click", () => zoomBy(1 / 1.18));
    zfit.addEventListener("click", () => { fit(); });
    zfollow.addEventListener("click", () => {
      cv.autoFollow = !cv.autoFollow;
      zfollow.classList.toggle("is-on", cv.autoFollow);
      zfollow.setAttribute("aria-pressed", cv.autoFollow ? "true" : "false");
      if (cv.autoFollow) { cv.lastFollowed = null; maybeFollow(); }
    });
    zoomBox.appendChild(zin); zoomBox.appendChild(zout); zoomBox.appendChild(zfit); zoomBox.appendChild(zfollow);
    vp.appendChild(zoomBox);

    empty = el("div", "cstage__empty",
      "<span>The lineage tree fills in here as the director proposes directions, generates takes, and you branch new variations.</span>");
    empty.hidden = true;
    vp.appendChild(empty);

    // The shell owns the chat pane now, permanently, so this stage is only the
    // tree + dock. It used to build its own chat column and physically relocate
    // #thread / #session-composer into it on every toggle; with chat always on
    // screen there is nothing to move, and the move was the fiddliest part of
    // this module.
    mainPanel = el("div", "cstage__main");
    mainPanel.appendChild(vp);
    mainPanel.appendChild(buildDock());
    stage.appendChild(mainPanel);

    wirePanZoom();
    wireComposerContext();

    // Re-fit the tree on window resize (only while canvas is active; respects a
    // manual pan/zoom). Additive and scoped to the canvas view.
    let resizeT;
    window.addEventListener("resize", () => {
      if (cv.view !== "canvas") return;
      clearTimeout(resizeT);
      resizeT = setTimeout(() => { if (cv.userMoved) apply(); else fit(); }, 150);
    });
    // Canvas is entered via the right-pane toggle, per session — there is
    // deliberately no boot-time ?view=canvas deep-link, so a fresh session
    // always opens on the timeline default (a persisted deep-link is a Phase 2
    // item; see CANVAS_MODE_PLAN).
  }

  // ========================================================================
  // View switching. app.js owns the topbar toggle (it selects between two
  // right-pane modules); this only turns the canvas stage on or off and
  // restores the composer to its plain state on the way out.
  // ========================================================================
  function setView(view) {
    cv.view = view === "canvas" ? "canvas" : "chat";
    const canvasOn = cv.view === "canvas";
    if (stage) stage.hidden = !canvasOn;
    closeMenu();
    renderChip();
    const input = document.getElementById("session-input");
    if (input) {
      input.setAttribute("placeholder", canvasOn ? "Direct the take… @ to reference · / for commands" : "Give direction or ask anything…");
      // Combobox ARIA only in canvas (the input is shared; chat stays untouched).
      if (canvasOn) {
        input.setAttribute("role", "combobox");
        input.setAttribute("aria-controls", "cv-acmenu");
        input.setAttribute("aria-autocomplete", "list");
        input.setAttribute("aria-expanded", "false");
      } else {
        ["role", "aria-controls", "aria-autocomplete", "aria-expanded", "aria-activedescendant"].forEach((a) => input.removeAttribute(a));
      }
    }
    if (canvasOn) { drawNow(); scrollChat(); }
    else {
      if (cv.media) { try { cv.media.pause(); } catch (_) {} }
      if (cv.mediaVideo) { try { cv.mediaVideo.pause(); } catch (_) {} }
      setPlayIcon(false);
    }
  }

  function scrollChat() {
    const t = document.getElementById("thread");
    if (t) t.scrollTop = t.scrollHeight;
  }

  // ========================================================================
  // Public render hook — called from app.js reconcile() every snapshot.
  // ========================================================================
  function render(snap) {
    cv.snap = snap;
    // Session change must reset HEAD state in EVERY view — a session can be resumed
    // while on Chat, and a stale @reference must never ride a send in a new session.
    if (snap && snap.session_id && snap.session_id !== cv.sid) {
      cv.sid = snap.session_id;
      cv.focusId = null; cv.fitted = false; cv.dockKey = null; cv.userMoved = false;
      cv.contextRef = null; cv.pendingBranches = []; cv.pendingLockId = null; cv.lastFollowed = null; renderChip();
    }
    if (cv.view !== "canvas") return; // cached; drawn when the user switches in
    drawNow();
  }

  // ========================================================================
  // Forest construction from snapshot.state
  // ========================================================================
  function buildForest(st) {
    const nodes = {}, order = [];
    function add(id, kind, data, parentId) {
      nodes[id] = { id, kind, data, parentId, children: [], col: 0, row: 0, x: 0, y: 0 };
      order.push(id);
    }
    add(SOURCE_ID, "source", { obs: st.observation || null }, null);
    (st.proposals || []).forEach((p) => { if (p && p.proposal_id) add(p.proposal_id, "proposal", p, SOURCE_ID); });
    (st.candidates || []).forEach((c) => {
      if (!c || !c.candidate_id) return;
      // Parent precedence: an explicit candidate parent (branch) → its proposal
      // (first generation) → the source (defensive fallback).
      const pid = (c.parent_candidate_id && nodes[c.parent_candidate_id]) ? c.parent_candidate_id
        : (c.proposal_id && nodes[c.proposal_id]) ? c.proposal_id : SOURCE_ID;
      add(c.candidate_id, "candidate", c, pid);
    });
    // The voice-over is a parallel LAYER too — and it was the one layer with
    // NO node at all, so a narration-centric session drew a nearly-empty tree
    // and read as broken: the session's main deliverable was invisible.
    const vo = st.layers && st.layers.voiceover;
    if (vo && typeof vo === "object" && (vo.status || (vo.script || "").trim())) {
      add(VO_ID, "vo", { layer: vo }, SOURCE_ID);
    }
    // SFX variants are a parallel LAYER, not part of the music lineage — hang
    // each rendered variant off the source so it's visible + commentable.
    const sfx = st.layers && st.layers.sfx;
    if (sfx && typeof sfx === "object" && !Array.isArray(sfx)) {
      (sfx.variants || []).forEach((v) => {
        if (v && v.variant_id) add(v.variant_id, "sfx", { variant: v, layer: sfx }, SOURCE_ID);
      });
    }
    // wire children + columns (depth from root)
    order.forEach((id) => { const n = nodes[id]; if (n.parentId && nodes[n.parentId]) nodes[n.parentId].children.push(n); });
    order.forEach((id) => {
      let d = 0, p = nodes[id];
      while (p.parentId && nodes[p.parentId]) { d++; p = nodes[p.parentId]; }
      nodes[id].col = d;
    });
    // rows: DFS leaf allocation; internal node = midpoint of its children
    let leaf = 0;
    (function dfs(n) {
      if (!n.children.length) { n.row = leaf++; return; }
      n.children.forEach(dfs);
      n.row = (n.children[0].row + n.children[n.children.length - 1].row) / 2;
    })(nodes[SOURCE_ID]);
    // pixel positions
    let maxX = 0, maxY = 0;
    order.forEach((id) => {
      const n = nodes[id];
      n.x = MARGIN_X + n.col * COL_GAP;
      n.y = MARGIN_Y + n.row * ROW_GAP;
      if (n.x + NODE_W > maxX) maxX = n.x + NODE_W;
      if (n.y + NODE_H > maxY) maxY = n.y + NODE_H;
    });
    cv.worldW = maxX + MARGIN_X;
    cv.worldH = maxY + MARGIN_Y;
    return { nodes, order };
  }

  function lockedSet(st, nodes) {
    const ids = {}, edges = {};
    const sel = st.selected_candidate_id;
    if (sel && nodes[sel]) {
      let p = nodes[sel];
      while (p) { ids[p.id] = true; if (p.parentId) edges[p.parentId + ">" + p.id] = true; p = p.parentId ? nodes[p.parentId] : null; }
    }
    return { ids, edges };
  }

  // ========================================================================
  // Draw
  // ========================================================================
  function drawNow() {
    const snap = cv.snap;
    if (!snap || !snap.state) { showEmpty(true); return; }
    const st = snap.state;
    const hasTree = !!st.observation || (st.proposals || []).length || (st.candidates || []).length;
    if (!hasTree) { world.querySelectorAll(".cv-node").forEach((n) => n.remove()); showEmpty(true); return; }
    showEmpty(false);

    const forest = buildForest(st);
    cv.nodes = forest.nodes;

    if (cv.pendingBranches.length) advanceHead(st);

    const locked = lockedSet(st, forest.nodes);

    // edges (svg) — rebuilt each draw
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    svg.setAttribute("width", cv.worldW);
    svg.setAttribute("height", cv.worldH);
    forest.order.forEach((id) => {
      const n = forest.nodes[id];
      if (!n.parentId || !forest.nodes[n.parentId]) return;
      const p = forest.nodes[n.parentId];
      const isLocked = locked.edges[p.id + ">" + n.id];
      const isBranch = n.kind === "candidate" && !!n.data.parent_candidate_id;
      const stroke = isLocked ? "#1D9E75" : (isBranch ? "#7F77DD" : "#cdc8c0");
      const x1 = p.x + NODE_W, y1 = p.y + ANCHOR_Y, x2 = n.x, y2 = n.y + ANCHOR_Y;
      const path = document.createElementNS(SVGNS, "path");
      const dx = Math.max(28, (x2 - x1) / 2);
      path.setAttribute("d", `M ${x1} ${y1} C ${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", stroke);
      path.setAttribute("stroke-width", isLocked ? "2.5" : "2");
      if (isBranch && !isLocked) path.setAttribute("stroke-dasharray", "5 4");
      svg.appendChild(path);
    });

    // nodes
    world.querySelectorAll(".cv-node").forEach((n) => n.remove());
    forest.order.forEach((id, i) => world.appendChild(buildNode(forest.nodes[id], st, locked, i === 1)));
    world.style.width = cv.worldW + "px";
    world.style.height = cv.worldH + "px";

    // focus (keep current if still present, else a sensible default)
    if (!cv.focusId || !forest.nodes[cv.focusId]) cv.focusId = pickDefaultFocus(st, forest);
    applyFocus();
    updateDock();

    if (!cv.fitted) { fit(); cv.fitted = true; } else apply();
    maybeFollow();

    // Additive: let the collab layer decorate the freshly rebuilt tree (comment
    // pins, ownership chips). No-op when js/collab-mode.js isn't loaded.
    if (window.EdennCollab) window.EdennCollab.onCanvasRender({ snap: cv.snap, nodes: cv.nodes, world, focusId: cv.focusId });
  }

  // HEAD advance: a branch resolves when a NEW (not pre-existing) child of its parent
  // reaches "completed". Move the reference + focus onto it. Branches whose every new
  // child failed are dropped (HEAD stays on the parent — a real, branchable take).
  function advanceHead(st) {
    let advancedTo = null;
    cv.pendingBranches = cv.pendingBranches.filter((p) => {
      const kids = (st.candidates || []).filter((c) => c.parent_candidate_id === p.parent && !p.existing.has(c.candidate_id));
      const done = kids.filter((c) => c.status === "completed" && (c.audio_url || c.video_url));
      if (done.length) { advancedTo = done.reduce((a, b) => ((b.version || 0) > (a.version || 0) ? b : a)); return false; }
      const failed = kids.filter((c) => c.status === "failed" || c.status === "error");
      if (kids.length && kids.length === failed.length) return false; // all new children failed → give up
      return true; // still waiting for the child to finish
    });
    if (advancedTo) {
      cv.contextRef = { id: advancedTo.candidate_id, label: advancedTo.title || ("v" + (advancedTo.version || 2)) };
      cv.focusId = advancedTo.candidate_id;
      renderChip();
    }
  }

  function pickDefaultFocus(st, forest) {
    if (st.selected_candidate_id && forest.nodes[st.selected_candidate_id]) return st.selected_candidate_id;
    const firstDone = (st.candidates || []).find((c) => c.status === "completed" && (c.audio_url || c.video_url));
    if (firstDone) return firstDone.candidate_id;
    const firstCand = (st.candidates || [])[0];
    if (firstCand) return firstCand.candidate_id;
    const firstProp = (st.proposals || [])[0];
    if (firstProp) return firstProp.proposal_id;
    return SOURCE_ID;
  }

  // ---- node card builders -------------------------------------------------
  function candidateBadge(c) {
    // A "branch" is a candidate derived from another candidate (it carries a
    // parent_candidate_id). Sibling takes from one proposal are v1/v2/… but are
    // NOT branches — so key off the parent link, never the version number alone.
    const branch = !!c.parent_candidate_id;
    if (branch) {
      const ek = c.edit_kind ? (EDIT_LABEL[c.edit_kind] || c.edit_kind) : "new take";
      return ['<span class="cv-badge branch">v' + (c.version || 2) + " · " + esc(ek) + "</span>", true];
    }
    return ['<span class="cv-badge take">' + esc(c.title && c.title.match(/take\s*\d+/i) ? c.title.match(/take\s*\d+/i)[0] : "Take") + "</span>", false];
  }

  // Fill a node thumb with REAL footage: the rendered video's first frame when
  // one exists, else a poster image, else the icon placeholder stays visible.
  function fillThumb(thumb, videoUrl, posterUrl) {
    if (videoUrl) {
      const v = document.createElement("video");
      v.src = mediaSrc(videoUrl); v.muted = true; v.playsInline = true; v.preload = "metadata";
      if (posterUrl) v.poster = mediaSrc(posterUrl);
      // Nudge a frame decode so the thumb shows a picture, not black.
      v.addEventListener("loadedmetadata", () => { try { v.currentTime = 0.01; } catch (_) {} });
      thumb.appendChild(v);
      return true;
    }
    if (posterUrl) {
      thumb.style.backgroundImage = 'url("' + posterUrl.replaceAll('"', "%22") + '")';
      thumb.style.backgroundSize = "cover";
      thumb.style.backgroundPosition = "center";
      return true;
    }
    return false;
  }

  function sourceMedia(st) {
    const sv = st.source_video || {};
    const obs = st.observation || {};
    return { video: sv.url || null, poster: sv.poster_url || obs.thumbnail_url || null };
  }

  function buildNode(n, st, locked, isFirstProposal) {
    const div = el("div", "cv-node");
    div.style.left = n.x + "px";
    div.style.top = n.y + "px";
    div.setAttribute("data-id", n.id);

    if (n.kind === "source") {
      const obs = n.data.obs || {};
      const dur = obs.duration_s ? fmtTime(obs.duration_s) : "";
      const thumb = el("div", "cv-thumb", '<i class="ti ti-player-play"></i>' + (dur ? '<span class="cv-thumb__dur">' + esc(dur) + "</span>" : ""));
      const srcMedia = sourceMedia(st);
      fillThumb(thumb, srcMedia.video, srcMedia.poster); // the user's ACTUAL clip
      div.appendChild(thumb);
      const pad = el("div", "cv-node__pad");
      pad.appendChild(el("div", "cv-node__title", esc(obs.video_title || "Source video")));
      const scenes = (obs.scenes || []).length;
      pad.appendChild(el("div", "cv-node__desc", esc((dur ? dur : "source") + (scenes ? " · " + scenes + " scenes" : ""))));
      div.appendChild(pad);
    } else if (n.kind === "proposal") {
      const p = n.data;
      const pad = el("div", "cv-node__pad");
      const tags = el("div", "cv-tags");
      tags.innerHTML = '<span class="cv-badge ' + (isFirstProposal ? "dir" : "alt") + '">Direction</span>' +
        (isFirstProposal ? '<span class="cv-lock" style="color:var(--teal-text)"><i class="ti ti-star"></i> rec</span>' : "");
      pad.appendChild(tags);
      pad.appendChild(el("div", "cv-node__title", esc(p.title || "Direction")));
      if (p.prompt) pad.appendChild(el("div", "cv-node__desc", esc(p.prompt)));
      const model = modelText(p);
      if (model) { const r = el("div", "cv-node__row"); r.appendChild(el("span", "cv-meta", esc(model))); pad.appendChild(r); }
      div.appendChild(pad);
    } else if (n.kind === "sfx") {
      const v = n.data.variant || {};
      const layer = n.data.layer || {};
      const isSel = v.variant_id === layer.selected_variant_id;
      if (isSel) div.classList.add("is-locked");
      div.classList.add("cv-node--sfx");
      const completed = v.status === "completed" && (v.audio_url || v.video_url);
      if (completed) {
        const dur = ((st.observation || {}).duration_s) || 0;
        const thumb = el("div", "cv-thumb",
          '<i class="ti ti-player-play"></i>' + (dur ? '<span class="cv-thumb__dur">' + esc(fmtTime(dur)) + "</span>" : ""));
        const srcMedia = sourceMedia(st);
        fillThumb(thumb, v.video_url || srcMedia.video, srcMedia.poster);
        div.appendChild(thumb);
      }
      const pad = el("div", "cv-node__pad");
      const tags = el("div", "cv-tags");
      tags.innerHTML = '<span class="cv-badge sfx"><i class="ti ti-wave-square"></i> SFX</span>';
      if (v.placeholder) tags.appendChild(el("span", "cv-badge warn", "preview tone"));
      pad.appendChild(tags);
      pad.appendChild(el("div", "cv-node__title", esc(v.label || "SFX take")));
      const eventCount = (layer.events || []).length;
      if (completed) {
        const row = el("div", "cv-node__row");
        row.appendChild(el("span", "cv-meta", eventCount + (eventCount === 1 ? " effect" : " effects")));
        if (isSel) row.appendChild(el("span", "cv-lock", '<i class="ti ti-check"></i> Selected'));
        pad.appendChild(row);
      } else if (v.status === "failed" || v.status === "error") {
        pad.appendChild(el("div", "cv-status err", '<i class="ti ti-alert-triangle"></i> Render didn\'t finish'));
      } else {
        pad.appendChild(el("div", "cv-status", '<span class="cv-spin"></span> ' + (v.status === "processing" ? "Rendering…" : "Queued…")));
      }
      div.appendChild(pad);
    } else if (n.kind === "vo") {
      const layer = n.data.layer || {};
      const completed = layer.status === "completed" && layer.audio_url;
      div.classList.add("cv-node--vo");
      if (completed) {
        const dur = ((st.observation || {}).duration_s) || 0;
        const thumb = el("div", "cv-thumb",
          '<i class="ti ti-player-play"></i>' + (dur ? '<span class="cv-thumb__dur">' + esc(fmtTime(dur)) + "</span>" : ""));
        const srcMedia = sourceMedia(st);
        fillThumb(thumb, srcMedia.video, srcMedia.poster);
        div.appendChild(thumb);
      }
      const pad = el("div", "cv-node__pad");
      const tags = el("div", "cv-tags");
      tags.innerHTML = '<span class="cv-badge vo"><i class="ti ti-microphone"></i> Voice-over</span>';
      if (layer.placeholder) tags.appendChild(el("span", "cv-badge warn", "preview tone"));
      pad.appendChild(tags);
      const firstLine = ((layer.segments || [])[0] || {}).text || (layer.script || "").split(/[.!?]/)[0] || "Narration";
      pad.appendChild(el("div", "cv-node__title", esc(firstLine.slice(0, 60))));
      const segs = (layer.segments || []).length;
      if (completed) {
        const row = el("div", "cv-node__row");
        row.appendChild(el("span", "cv-meta",
          segs ? segs + (segs === 1 ? " timed line" : " timed lines") : "one read"));
        pad.appendChild(row);
      } else if (layer.status === "failed") {
        pad.appendChild(el("div", "cv-status err", '<i class="ti ti-alert-triangle"></i> Render didn\'t finish'));
      } else if (layer.status === "queued" || layer.status === "processing") {
        pad.appendChild(el("div", "cv-status", '<span class="cv-spin"></span> Recording…'));
      } else {
        pad.appendChild(el("div", "cv-status", '<i class="ti ti-pencil"></i> Draft — record it from the voice-over card'));
      }
      div.appendChild(pad);
    } else { // candidate
      const c = n.data;
      const isSel = c.candidate_id === st.selected_candidate_id;
      if (isSel) div.classList.add("is-locked");
      const completed = c.status === "completed" && (c.audio_url || c.video_url);
      const failed = c.status === "failed" || c.status === "error";

      if (completed) {
        const thumb = el("div", "cv-thumb",
          '<i class="ti ti-player-play"></i><span class="cv-thumb__dur">' + esc(fmtTime((n.data.obs || {}).duration_s || ((st.observation || {}).duration_s) || 0) || "0:00") + "</span>");
        // A rendered take shows ITS OWN first frame; an audio-only take shows
        // the source clip's frame (the footage it scores).
        const srcMedia = sourceMedia(st);
        fillThumb(thumb, c.video_url || srcMedia.video, srcMedia.poster);
        const dl = el("button", "cv-thumb__dl", '<i class="ti ti-download"></i>');
        dl.title = "Download this take";
        dl.addEventListener("click", (e) => { e.stopPropagation(); openUrl(c.video_url || c.audio_url); });
        thumb.appendChild(dl);
        div.appendChild(thumb);
      }
      const pad = el("div", "cv-node__pad");
      const [badge, isBranch] = candidateBadge(c);
      const tags = el("div", "cv-tags"); tags.innerHTML = badge;
      if (typeof c.music_volume === "number" && Math.abs(c.music_volume - 0.85) > 0.001)
        tags.appendChild(el("span", "cv-meta", "music " + Math.round(c.music_volume * 100) + "%"));
      if (c.placeholder) tags.appendChild(el("span", "cv-badge warn", "preview tone")); // dev stand-in
      pad.appendChild(tags);
      pad.appendChild(el("div", "cv-node__title", esc(c.title || ("Take " + (c.version || 1)))));

      if (completed) {
        const row = el("div", "cv-node__row");
        row.appendChild(el("span", "cv-meta", esc(modelText(c))));
        if (isSel) row.appendChild(el("span", "cv-lock", '<i class="ti ti-check"></i> Locked'));
        pad.appendChild(row);
      } else if (failed) {
        pad.appendChild(el("div", "cv-status err", '<i class="ti ti-alert-triangle"></i> Generation didn\'t finish'));
      } else if (c.stalled_seconds) {
        const mins = Math.max(1, Math.round(c.stalled_seconds / 60));
        pad.appendChild(el("div", "cv-status err",
          '<i class="ti ti-alert-triangle"></i> Stuck? ' + mins + ' min (usually <3)'));
      } else {
        pad.appendChild(el("div", "cv-status", '<span class="cv-spin"></span> ' + (c.status === "processing" ? "Composing…" : "Queued…")));
      }
      div.appendChild(pad);
    }

    // Keyboard-operable: each node is a focusable button that loads into the dock.
    div.setAttribute("tabindex", "0");
    div.setAttribute("role", "button");
    div.setAttribute("aria-label", nodeAriaLabel(n));
    div.addEventListener("click", () => focusNode(n.id));
    div.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); focusNode(n.id); }
    });
    return div;
  }

  function nodeAriaLabel(n) {
    if (n.kind === "source") return "Source video";
    if (n.kind === "proposal") return "Direction: " + (n.data.title || "");
    if (n.kind === "sfx") {
      const v = n.data.variant || {};
      const state = v.status === "completed" ? "ready" : (v.status === "failed" || v.status === "error" ? "failed" : "rendering");
      return "SFX: " + (v.label || "take") + " (" + state + ")";
    }
    const c = n.data;
    const state = c.status === "completed" ? "ready" : (c.status === "failed" || c.status === "error" ? "failed" : "generating");
    return "Take: " + (c.title || ("take " + (c.version || 1))) + " (" + state + ")";
  }

  // ---- focus + dock -------------------------------------------------------
  function focusNode(id) {
    cv.focusId = id;
    applyFocus();
    updateDock();
    maybeFollow();
  }
  function applyFocus() {
    world.querySelectorAll(".cv-node").forEach((nd) => {
      nd.classList.toggle("is-focus", nd.getAttribute("data-id") === cv.focusId);
    });
  }

  function nodePath(id) {
    const parts = [];
    let p = cv.nodes[id];
    while (p) {
      let label;
      if (p.kind === "source") label = (p.data.obs && p.data.obs.video_title) || "reel";
      else if (p.kind === "proposal") label = p.data.title || "direction";
      else if (p.kind === "sfx") label = (p.data.variant && p.data.variant.label) || "SFX";
      // Candidates use a concise label in the breadcrumb (the full title can repeat
      // the direction name): "v2" for a branch, "take N" for a sibling take.
      else label = p.data.parent_candidate_id ? ("v" + (p.data.version || 2)) : ("take " + (p.data.version || 1));
      parts.unshift(label);
      p = p.parentId ? cv.nodes[p.parentId] : null;
    }
    return parts.join(" › ");
  }

  function buildDock() {
    dock = el("div", "cv-dock"); dock.id = "cv-dock";
    dock.innerHTML =
      '<div class="cv-dock__thumb" id="cv-dock-thumb"><i class="ti ti-player-play"></i></div>' +
      '<button class="cv-dock__play" id="cv-dock-play" title="Play"><i class="ti ti-player-play"></i></button>' +
      '<div class="cv-dock__main">' +
        '<div class="cv-dock__hd"><span class="cv-dock__title" id="cv-dock-title">—</span>' +
        '<span class="cv-dock__time" id="cv-dock-time">0:00 / 0:00</span></div>' +
        '<div class="cv-dock__path" id="cv-dock-path"></div>' +
        '<div class="cv-dock__scrub" id="cv-dock-scrub"><div class="cv-dock__fill" id="cv-dock-fill"></div></div>' +
      '</div>' +
      '<div class="cv-dock__btns">' +
        '<button class="cv-dbtn primary" id="cv-dock-use" data-clb-act="iterate"></button>' +
        '<button class="cv-dbtn" id="cv-dock-branch" data-clb-act="iterate"><i class="ti ti-git-branch"></i> Branch</button>' +
        '<button class="cv-dbtn" id="cv-dock-dl"><i class="ti ti-download"></i> Download</button>' +
      '</div>';
    // wire controls
    dock.querySelector("#cv-dock-play").addEventListener("click", toggleMedia);
    dock.querySelector("#cv-dock-scrub").addEventListener("click", (e) => {
      if (!cv.media || !cv.media.duration) return;
      const r = e.currentTarget.getBoundingClientRect();
      cv.media.currentTime = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * cv.media.duration;
      syncSlavedVideo(); // seek the layered source video with the audio master
    });
    dock.querySelector("#cv-dock-dl").addEventListener("click", () => {
      const nd = cv.nodes[cv.focusId];
      if (nd && nd.kind === "sfx") { const v = nd.data.variant || {}; openUrl(v.video_url || v.audio_url); return; }
      const c = focusedCandidate();
      if (c) openUrl(c.video_url || c.audio_url);
    });
    dock.querySelector("#cv-dock-branch").addEventListener("click", () => {
      branchFrom(focusedCandidate(), dock.querySelector("#cv-dock-branch"));
    });
    // #cv-dock-use is context-aware (Generate a direction / Use this take) — its
    // handler is (re)bound per focus in updateDock().
    return dock;
  }

  function focusedCandidate() {
    const n = cv.nodes[cv.focusId];
    return n && n.kind === "candidate" ? n.data : null;
  }

  function updateDock() {
    if (!dock) return;
    const n = cv.nodes[cv.focusId];
    const title = dock.querySelector("#cv-dock-title");
    const path = dock.querySelector("#cv-dock-path");
    const playBtn = dock.querySelector("#cv-dock-play");
    const dlBtn = dock.querySelector("#cv-dock-dl");
    const branchBtn = dock.querySelector("#cv-dock-branch");
    const thumb = dock.querySelector("#cv-dock-thumb");
    const fill = dock.querySelector("#cv-dock-fill");
    const time = dock.querySelector("#cv-dock-time");

    const c = n && n.kind === "candidate" ? n.data : null;
    const url = c && (c.video_url || c.audio_url);
    const completed = !!c && c.status === "completed" && !!url;
    // Voice-over focus: the narration plays against the muted source picture.
    const voL = n && n.kind === "vo" ? (n.data.layer || {}) : null;
    const voUrl = voL && voL.audio_url;
    const voCompleted = !!voL && voL.status === "completed" && !!voUrl;
    // SFX-variant focus (a parallel layer node).
    const sfxV = n && n.kind === "sfx" ? (n.data.variant || {}) : null;
    const sfxLayer = n && n.kind === "sfx" ? (n.data.layer || {}) : null;
    const sfxUrl = sfxV && (sfxV.video_url || sfxV.audio_url);
    const sfxCompleted = !!sfxV && sfxV.status === "completed" && !!sfxUrl;

    // Cheap, idempotent text + button state — safe to refresh on every render.
    title.textContent = !n ? "—"
      : (n.kind === "source") ? "Source video"
      : (n.kind === "vo") ? "Voice-over"
      : (n.kind === "vo") ? "Voice-over"
      : (n.kind === "sfx") ? (sfxV.label || "SFX take")
      : (n.data.title || (n.kind === "candidate" ? "Take " + (n.data.version || 1) : "Direction"));
    path.textContent = n ? nodePath(n.id) : "";

    // Context-aware actions (the chat no longer carries the proposal/candidate cards
    // in canvas mode, so generate + lock live here). Branch/Download apply to takes.
    const st = (cv.snap && cv.snap.state) || {};
    const useBtn = dock.querySelector("#cv-dock-use");
    const isProposal = !!n && n.kind === "proposal";
    const selId = st.selected_candidate_id;
    branchBtn.style.display = c ? "" : "none";
    // Download applies to a finished take OR a finished SFX variant.
    dlBtn.style.display = (c || sfxV) ? "" : "none";
    dlBtn.disabled = !(completed || sfxCompleted);
    branchBtn.disabled = !completed;   // never branch/spend from an in-flight or failed take
    if (sfxV) {
      // SFX variant: select it (free), no branch/generate from the dock.
      if (sfxCompleted) {
        useBtn.style.display = "";
        if (sfxV.variant_id === (sfxLayer.selected_variant_id)) {
          useBtn.innerHTML = '<i class="ti ti-check"></i> Selected'; useBtn.disabled = true; useBtn.onclick = null;
        } else {
          useBtn.disabled = false; useBtn.innerHTML = '<i class="ti ti-circle-check"></i> Use this';
          useBtn.onclick = () => {
            useBtn.disabled = true;
            const conn = window.__edenn && window.__edenn.app && window.__edenn.app.conn;
            if (conn) conn.choose({ choice_type: "sfx", payload: { select_variant_id: sfxV.variant_id } });
          };
        }
      } else {
        useBtn.style.display = "none";
      }
    } else if (isProposal) {
      useBtn.style.display = "";
      // A direction is CONSUMED once any usable take exists: re-generating would
      // both spend again and (backend contract) REPLACE all existing takes and
      // branches. Mirror the chat view, which hides consumed proposal cards.
      const consumed = (st.candidates || []).some(
        (x) => x && x.status !== "failed" && x.status !== "error"
      );
      if (consumed) {
        useBtn.disabled = true;
        useBtn.innerHTML = '<i class="ti ti-check"></i> Generated';
        useBtn.onclick = null;
      } else {
        useBtn.disabled = false;
        useBtn.innerHTML = '<i class="ti ti-wand"></i> Generate';
        useBtn.onclick = () => {
          const h = host(); const conn = window.__edenn && window.__edenn.app && window.__edenn.app.conn;
          if (!conn) return;
          const go = () => {
            useBtn.disabled = true;
            if (h.startThinking) h.startThinking(); // instant trail while the turn runs
            conn.choose({ choice_type: "proposal", target_id: n.id, payload: {} });
          };
          if (h.confirmSpend) h.confirmSpend("Generate tracks?", "This creates the music — a minute or two, and it spends to generate.", go); else go();
        };
      }
    } else if (c && completed) {
      useBtn.style.display = "";
      if (c.candidate_id === selId) {
        useBtn.innerHTML = '<i class="ti ti-check"></i> Locked'; useBtn.disabled = true; useBtn.onclick = null;
        if (cv.pendingLockId === c.candidate_id) cv.pendingLockId = null; // lock landed
      } else if (cv.pendingLockId === c.candidate_id) {
        // Lock dispatched, snapshot not caught up yet — a poll-driven re-render
        // must NOT re-arm the button mid-dispatch (single-fire across renders).
        useBtn.disabled = true; useBtn.innerHTML = '<i class="ti ti-circle-check"></i> Use this'; useBtn.onclick = null;
      } else {
        useBtn.disabled = false; useBtn.innerHTML = '<i class="ti ti-circle-check"></i> Use this';
        useBtn.onclick = () => {
          useBtn.disabled = true; // single-fire: a double-click must not dispatch twice
          cv.pendingLockId = c.candidate_id;
          const h = host();
          if (h.startThinking) h.startThinking(); // lock turn feedback
          const conn = window.__edenn && window.__edenn.app && window.__edenn.app.conn;
          if (conn) conn.choose({ choice_type: "candidate", target_id: c.candidate_id, payload: {} });
        };
      }
    } else if (c && (c.status === "failed" || c.status === "error" || c.stalled_seconds)) {
      // Failed OR watchdog-stalled take: same retry the chat card has.
      useBtn.style.display = ""; useBtn.disabled = false;
      useBtn.innerHTML = '<i class="ti ti-refresh"></i> Try again';
      useBtn.onclick = () => {
        const h = host();
        const existing = new Set((((cv.snap || {}).state || {}).candidates || [])
          .filter((x) => x.parent_candidate_id === c.candidate_id).map((x) => x.candidate_id));
        if (h.requestVariation) h.requestVariation(c, useBtn, () => cv.pendingBranches.push({ parent: c.candidate_id, existing }));
      };
    } else {
      useBtn.style.display = "none";
    }

    // Additive: collab layer adjusts its dock actions (Comment / Inherit) to the
    // focused node — after the buttons above so it may override their visibility.
    if (window.EdennCollab) window.EdennCollab.onFocusChange(cv.focusId, n || null);

    // Media is the EXPENSIVE part. Only tear it down + rebuild when the focused take
    // (or its source/status) actually changes — so poll-driven re-renders, which fire
    // every ~2.5s while a sibling generates, don't interrupt or reset playback.
    const key = n ? (cv.focusId + "|" + (url || sfxUrl || "") + "|" + (c ? c.status : (sfxV ? sfxV.status : ""))) : "";
    if (key === cv.dockKey) { playBtn.disabled = !cv.media; return; }
    cv.dockKey = key;

    stopDockMedia();
    fill.style.width = "0";
    time.textContent = "0:00 / 0:00";
    setPlayIcon(false);

    // thumb media (P2 "watch"): a finished take with a rendered video plays it
    // directly; a finished AUDIO-ONLY take layers the muted SOURCE video under
    // the take's audio (client-side — no per-take render needed); the source
    // node plays the original clip itself.
    const srcVideo = (st.source_video || {}).url || null;
    thumb.innerHTML = "";
    function mkVideo(srcUrl, muted) {
      const v = document.createElement("video");
      v.src = mediaSrc(srcUrl); v.playsInline = true; v.muted = muted; v.preload = "metadata";
      const poster = (st.source_video || {}).poster_url;
      if (muted && poster) v.poster = mediaSrc(poster);
      thumb.appendChild(v);
      return v;
    }
    if (c && c.video_url && c.status === "completed") {
      cv.media = mkVideo(c.video_url, false);
    } else if (completed && srcVideo) {
      // watch-against-the-picture: audio is the master clock; the muted source
      // video is slaved to it (play/pause/seek + drift correction).
      cv.media = new Audio(mediaSrc(url));
      cv.mediaVideo = mkVideo(srcVideo, true);
    } else if (completed) {
      thumb.innerHTML = '<i class="ti ti-player-play"></i>';
      cv.media = new Audio(mediaSrc(url));
    } else if (voCompleted && srcVideo) {
      cv.media = new Audio(mediaSrc(voUrl));  // narration over the muted picture
      cv.mediaVideo = mkVideo(srcVideo, true);
    } else if (voCompleted) {
      thumb.innerHTML = '<i class="ti ti-microphone"></i>';
      cv.media = new Audio(mediaSrc(voUrl));
    } else if (sfxCompleted && sfxV.video_url) {
      cv.media = mkVideo(sfxV.video_url, false); // rendered SFX video plays directly
    } else if (sfxCompleted && srcVideo) {
      cv.media = new Audio(mediaSrc(sfxUrl));              // SFX audio layered over the muted source
      cv.mediaVideo = mkVideo(srcVideo, true);
    } else if (sfxCompleted) {
      thumb.innerHTML = '<i class="ti ti-wave-square"></i>';
      cv.media = new Audio(mediaSrc(sfxUrl));
    } else if (n && n.kind === "source" && srcVideo) {
      cv.media = mkVideo(srcVideo, false); // the original clip, own audio
    } else {
      thumb.innerHTML = '<i class="ti ti-' + (n && n.kind === "source" ? "movie" : n && n.kind === "sfx" ? "wave-square" : "music") + '"></i>';
    }
    if (cv.media) {
      cv.media.addEventListener("timeupdate", onMediaTime);
      cv.media.addEventListener("ended", () => { setPlayIcon(false); syncSlavedVideo(true); });
      cv.media.addEventListener("loadedmetadata", onMediaTime);
    }
    playBtn.disabled = !cv.media;
  }

  function stopDockMedia() {
    if (cv.media) { try { cv.media.pause(); } catch (_) {} cv.media = null; }
    if (cv.mediaVideo) { try { cv.mediaVideo.pause(); } catch (_) {} cv.mediaVideo = null; }
  }

  // Keep the muted source video in step with the audio master. Called on play/
  // pause/seek and (cheaply) from timeupdate to correct drift.
  function syncSlavedVideo(forcePause) {
    const a = cv.media, v = cv.mediaVideo;
    if (!a || !v) return;
    const target = Math.min(a.currentTime || 0, (v.duration || Infinity));
    if (Math.abs((v.currentTime || 0) - target) > 0.3) v.currentTime = target;
    if (forcePause || a.paused) { if (!v.paused) v.pause(); }
    else if (v.paused && !v.ended) { v.play().catch(() => {}); }
  }

  function onMediaTime() {
    if (!cv.media) return;
    const d = cv.media.duration || 0, t = cv.media.currentTime || 0;
    const fill = dock.querySelector("#cv-dock-fill");
    const time = dock.querySelector("#cv-dock-time");
    if (fill) fill.style.width = (d ? (t / d) * 100 : 0) + "%";
    if (time) time.textContent = fmtTime(t) + " / " + fmtTime(d);
    syncSlavedVideo(); // drift-correct the layered source video
  }
  function setPlayIcon(playing) {
    const b = dock && dock.querySelector("#cv-dock-play i");
    if (b) b.className = "ti " + (playing ? "ti-player-pause" : "ti-player-play");
  }
  function toggleMedia() {
    if (!cv.media) return;
    if (cv.media.paused) {
      cv.media.play().then(() => { setPlayIcon(true); syncSlavedVideo(); }).catch(() => {});
    } else {
      cv.media.pause(); setPlayIcon(false); syncSlavedVideo();
    }
  }

  // A bare window.open is a popup the browser may block, and when it blocks one
  // NOTHING happens — the user clicks Download and the app appears to ignore
  // them (observed live on the deployed console). deliverFile tokenises the URL,
  // uses a real download anchor, and says so when the browser still refuses.
  function openUrl(url, name) {
    const deliver = host().deliverFile;
    if (deliver) return deliver(url, name || "edenn-take.mp4");
    if (url) window.open(url, "_blank");
  }

  // ========================================================================
  // @ references + / commands (composer autocomplete)
  // ========================================================================
  function wireComposerContext() {
    const composer = document.querySelector(".session-composer");
    const input = document.getElementById("session-input");
    if (!composer || !input) return;
    const innerWrap = composer.querySelector(".session-composer__inner");

    cref = el("div", "cv-cref"); cref.hidden = true;
    composer.insertBefore(cref, innerWrap);
    acmenu = el("div", "cv-acmenu"); acmenu.hidden = true;
    acmenu.id = "cv-acmenu"; acmenu.setAttribute("role", "listbox");
    composer.insertBefore(acmenu, innerWrap);

    input.addEventListener("input", onComposerInput);
    // Capture phase so we intercept Enter/Arrows BEFORE app.js's submit handler
    // when the menu is open; otherwise we let app.js handle Enter normally.
    input.addEventListener("keydown", onComposerKeydown, true);
    document.addEventListener("click", (e) => { if (!composer.contains(e.target)) closeMenu(); });
  }

  function onComposerInput() {
    const input = document.getElementById("session-input");
    if (cv.view !== "canvas") { closeMenu(); return; }
    const v = input.value;
    if (v[0] === "@") openMenu("@", v.slice(1));
    else if (v[0] === "/") openMenu("/", v.slice(1));
    else closeMenu();
  }

  function onComposerKeydown(e) {
    // An open-but-empty menu must NOT intercept Enter — let app.js's submit run so a
    // message starting with @/​/ that matches nothing is still sendable.
    if (!menu.open || !menu.items.length || cv.view !== "canvas") return;
    if (e.key === "ArrowDown") { e.preventDefault(); e.stopImmediatePropagation(); menu.idx = Math.min(menu.items.length - 1, menu.idx + 1); paintMenu(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); e.stopImmediatePropagation(); menu.idx = Math.max(0, menu.idx - 1); paintMenu(); }
    else if (e.key === "Enter") { e.preventDefault(); e.stopImmediatePropagation(); if (menu.items[menu.idx]) selectMenuItem(menu.items[menu.idx]); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); closeMenu(); }
  }

  function openMenu(mode, query) {
    const q = (query || "").toLowerCase();
    let items = [];
    if (mode === "@") {
      const st = (cv.snap && cv.snap.state) || {};
      (st.candidates || []).forEach((c) => items.push({ id: c.candidate_id, label: c.title || ("take " + (c.version || 1)), sub: statusLabel(c), icon: "ti-music" }));
      const obs = st.observation || {};
      items.push({ id: SOURCE_ID, label: obs.video_title || "source video", sub: "source", icon: "ti-movie" });
      if (q) items = items.filter((it) => it.label.toLowerCase().indexOf(q) >= 0);
    } else {
      items = COMMANDS.filter((c) => !q || c.label.toLowerCase().indexOf(q) >= 0).map((c) => ({ id: c.id, label: c.label, sub: c.hint, icon: c.icon, cmd: true }));
    }
    menu.open = items.length > 0; menu.mode = mode; menu.items = items; menu.idx = 0;
    paintMenu();
    setExpanded(menu.open);
  }

  function paintMenu() {
    if (!acmenu) return;
    if (!menu.open || !menu.items.length) { acmenu.hidden = true; return; }
    acmenu.innerHTML = "";
    acmenu.appendChild(el("div", "cv-acmenu__hd", menu.mode === "@" ? "Reference a version" : "Commands"));
    menu.items.forEach((it, i) => {
      const row = el("button", "cv-acrow" + (i === menu.idx ? " is-on" : ""));
      row.id = "cv-acrow-" + i;
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", i === menu.idx ? "true" : "false");
      row.tabIndex = -1; // keep focus in the input; arrow keys navigate
      row.innerHTML = '<span class="cv-acrow__ic"><i class="ti ' + (it.icon || "ti-at") + '"></i></span>' +
        '<span class="cv-acrow__nm">' + esc(it.label) + "</span>" +
        (it.sub ? '<span class="cv-acrow__sub">' + esc(it.sub) + "</span>" : "");
      row.addEventListener("mousedown", (e) => { e.preventDefault(); selectMenuItem(it); });
      acmenu.appendChild(row);
    });
    acmenu.hidden = false;
    const inp = document.getElementById("session-input");
    if (inp) inp.setAttribute("aria-activedescendant", "cv-acrow-" + menu.idx);
  }

  function closeMenu() { menu.open = false; menu.items = []; if (acmenu) acmenu.hidden = true; setExpanded(false); }

  function setExpanded(open) {
    const inp = document.getElementById("session-input");
    if (!inp) return;
    inp.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) inp.removeAttribute("aria-activedescendant");
  }

  function selectMenuItem(it) {
    const input = document.getElementById("session-input");
    const mode = menu.mode;
    // Close BEFORE acting: executeCommand may send through the composer
    // (synthetic Enter), which must not re-enter the still-open menu.
    closeMenu();
    if (mode === "@") {
      cv.contextRef = { id: it.id, label: it.label };
      renderChip();
      if (input) { input.value = ""; input.focus(); }
    } else {
      if (input) input.value = "";
      executeCommand(it.id);
    }
  }

  function statusLabel(c) {
    if (c.status === "completed") return "ready";
    if (c.status === "failed" || c.status === "error") return "failed";
    return "generating";
  }

  function renderChip() {
    if (!cref) return;
    if (!cv.contextRef || cv.view !== "canvas") { cref.hidden = true; cref.innerHTML = ""; return; }
    cref.innerHTML =
      '<span class="cv-cref__ic"><i class="ti ti-at"></i></span>' +
      '<span class="cv-cref__nm">' + esc(cv.contextRef.label) + "</span>" +
      '<button class="cv-cref__x" title="Remove reference" type="button"><i class="ti ti-x"></i></button>';
    cref.querySelector(".cv-cref__x").addEventListener("click", () => { cv.contextRef = null; renderChip(); });
    cref.hidden = false;
  }

  function candidateById(id) {
    const st = (cv.snap && cv.snap.state) || {};
    return (st.candidates || []).find((c) => c.candidate_id === id) || null;
  }
  // The candidate an action targets: the @reference if it's a completed take, else the focused take.
  function targetCandidate() {
    const ref = cv.contextRef && candidateById(cv.contextRef.id);
    if (ref) return ref;
    return focusedCandidate();
  }

  // Branch a new take. We snapshot the parent's existing children and arm HEAD-advance
  // ONLY when the variation is actually sent (the onSent callback) — so cancelling the
  // spend dialog never latches HEAD onto a pre-existing child. Reuses the chat's confirm.
  function branchFrom(c, btn) {
    const h = host();
    if (!c) { if (h.toast) h.toast("Pick a finished take to branch from."); return; }
    if (c.status !== "completed") { if (h.toast) h.toast("That take is still generating — branch once it's ready."); return; }
    const existing = new Set(((cv.snap && cv.snap.state && cv.snap.state.candidates) || [])
      .filter((x) => x.parent_candidate_id === c.candidate_id).map((x) => x.candidate_id));
    if (h.requestVariation) h.requestVariation(c, btn || null, () => cv.pendingBranches.push({ parent: c.candidate_id, existing }));
  }

  function executeCommand(id) {
    const h = host();
    const c = targetCandidate();
    if (id === "branch") { branchFrom(c, null); return; }
    if (id === "export") {
      const url = c && (c.video_url || c.audio_url);
      if (url) openUrl(url); else if (h.toast) h.toast("Finish a take first, then export it.");
      return;
    }
    if (id === "voiceover") {
      const conn = (window.__edenn && window.__edenn.app && window.__edenn.app.conn);
      if (!conn) return;
      const st = (cv.snap && cv.snap.state) || {};
      const vo = (st.layers || {}).voiceover;
      if (vo && (vo.status === "queued" || vo.status === "processing")) {
        // Mid-flight: re-drafting now would desync script and rendered audio.
        if (h.toast) h.toast("The narration is already recording — give it a moment.");
        return;
      }
      // "completed" is included so /voiceover supports RE-recording: job hydration
      // rewrites status draft→completed permanently, and the chat card treats
      // completed as editable too (the backend re-drafts then re-runs TTS).
      if (vo && vo.script && (vo.status === "draft" || vo.status === "completed")) {
        // A script exists — generate it with that script, exactly like the
        // chat card (the backend requires a confirmed script before TTS).
        const go = () => {
          if (h.startThinking) h.startThinking(); // TTS turn feedback
          conn.choose({
            choice_type: "voiceover",
            payload: { script: vo.script, voice_id: vo.voice_id || "warm_female", tone: vo.tone || "" },
          });
        };
        if (h.confirmSpend) h.confirmSpend("Generate the voice-over?", "This records the drafted narration — a moment, and it spends to generate.", go);
        else go();
      } else {
        // No draft yet — a bare voiceover choice would be rejected by the backend
        // (ApprovalRequiredError). Ask the agent to DRAFT one instead: a normal
        // LLM turn through the composer, so the script card appears for approval.
        composerSend("Draft a short voice-over script for this video.");
      }
    }
  }

  // Send a message through the real composer path (optimistic bubble + thinking +
  // context_refs all included) — identical to the user typing it.
  function composerSend(text) {
    const inp = document.getElementById("session-input");
    if (!inp) return;
    inp.value = text;
    inp.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  }

  function getComposerContext() {
    // Only attach a reference from the canvas view (chat sends stay byte-identical),
    // and only if it still resolves in the current session (no stale/cross-session id).
    if (cv.view !== "canvas" || !cv.contextRef) return null;
    const id = cv.contextRef.id;
    if (id === SOURCE_ID || candidateById(id)) return { context_refs: [id] };
    const fc = focusedCandidate();
    return fc ? { context_refs: [fc.candidate_id] } : null;
  }

  // ---- pan / zoom ---------------------------------------------------------
  function apply() { world.style.transform = `translate(${cv.tx}px, ${cv.ty}px) scale(${cv.scale})`; }
  function fit() {
    const vw = vp.clientWidth, vh = vp.clientHeight;
    if (!vw || !cv.worldW) { apply(); return; }
    // Keep cards a comfortable size — don't shrink the whole tree to fit when it's
    // large; floor the zoom and anchor to the source (left) so the user pans/auto-
    // follows into the rest instead of squinting at tiny nodes.
    let s = Math.min(vw / (cv.worldW + 24), vh / (cv.worldH + 24), 1);
    s = Math.max(0.7, s);
    cv.scale = s;
    cv.tx = cv.worldW * s <= vw ? (vw - cv.worldW * s) / 2 : 16;
    cv.ty = cv.worldH * s <= vh ? (vh - cv.worldH * s) / 2 : 16;
    cv.userMoved = false;
    apply();
  }
  function zoomBy(f) {
    const vw = vp.clientWidth, vh = vp.clientHeight;
    const cxw = (vw / 2 - cv.tx) / cv.scale, cyw = (vh / 2 - cv.ty) / cv.scale; // viewport center in world coords
    cv.scale = Math.max(0.3, Math.min(1.6, cv.scale * f));
    cv.tx = vw / 2 - cxw * cv.scale;
    cv.ty = vh / 2 - cyw * cv.scale;
    cv.userMoved = true;
    apply();
  }
  function contentOverflows() {
    return cv.worldW * cv.scale > vp.clientWidth + 4 || cv.worldH * cv.scale > vp.clientHeight + 4;
  }
  function panToNode(id) {
    const n = cv.nodes[id]; if (!n || !vp.clientWidth) return;
    cv.tx = vp.clientWidth / 2 - (n.x + NODE_W / 2) * cv.scale;
    cv.ty = vp.clientHeight / 2 - (n.y + NODE_H / 2) * cv.scale;
    cv.userMoved = true;
    apply();
  }
  // Auto-follow: when enabled, pan the active card into view — but only when the
  // focus actually changed and the tree overflows (so it never fights manual panning).
  function maybeFollow() {
    if (!cv.autoFollow || cv.focusId === cv.lastFollowed) return;
    cv.lastFollowed = cv.focusId;
    if (contentOverflows()) panToNode(cv.focusId);
  }
  function wirePanZoom() {
    let drag = false, sx = 0, sy = 0, stx = 0, sty = 0;
    vp.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".cv-node") || e.target.closest(".cstage__zoom")) return;
      drag = true; sx = e.clientX; sy = e.clientY; stx = cv.tx; sty = cv.ty;
      vp.classList.add("is-grab");
      try { vp.setPointerCapture(e.pointerId); } catch (_) {}
    });
    vp.addEventListener("pointermove", (e) => {
      if (!drag) return;
      cv.userMoved = true;
      cv.tx = stx + (e.clientX - sx); cv.ty = sty + (e.clientY - sy); apply();
    });
    const end = () => { drag = false; vp.classList.remove("is-grab"); };
    vp.addEventListener("pointerup", end);
    vp.addEventListener("pointercancel", end);
    vp.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = vp.getBoundingClientRect();
      const mxw = (e.clientX - r.left - cv.tx) / cv.scale, myw = (e.clientY - r.top - cv.ty) / cv.scale;
      const f = e.deltaY < 0 ? 1.1 : 1 / 1.1;
      cv.scale = Math.max(0.3, Math.min(1.6, cv.scale * f));
      cv.tx = (e.clientX - r.left) - mxw * cv.scale;
      cv.ty = (e.clientY - r.top) - myw * cv.scale;
      cv.userMoved = true;
      apply();
    }, { passive: false });
  }

  function showEmpty(on) {
    if (empty) empty.hidden = !on;
    if (dock) dock.style.display = on ? "none" : "flex";
    if (legend) legend.style.display = on ? "none" : "flex";
    if (zoomBox) zoomBox.style.display = on ? "none" : "flex";
  }

  // ========================================================================
  // expose + boot
  // ========================================================================
  window.EdennCanvas = { render, setView, getComposerContext, focusNode };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
