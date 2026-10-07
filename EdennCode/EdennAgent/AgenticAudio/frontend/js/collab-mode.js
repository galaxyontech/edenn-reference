/* ============================================================================
 * Edenn — collab mode (additive), built to the Collab design spec:
 * "Canvas Comments — Composer & Thread States" + "Comment Composer — Var. B".
 *
 * What this module renders on top of the canvas lineage view:
 *   - PINS on the tree — new (+) / unread (count) / read (avatar) / agent
 *     (square avatar) / resolved (faded check). One pin per thread, stacked
 *     per node.
 *   - A floating THREAD CARD anchored to the pin (unscaled; follows pan/zoom):
 *     composer states (pill → focused toolbar → typing → hint row), @mentions
 *     with PEOPLE + AGENTS groups (square avatar + AGENT chip), region anchor
 *     chip (label + time range, removable), attachment chips, posted thread
 *     with reactions / copy-link / resolve / kebab (edit, delete), edit mode
 *     with Cancel/Save, resolved = one quiet line + Reopen.
 *   - A THREADS RAIL on the right — collapsed 48px (avatars + unread) ⇄
 *     expanded panel (search, All/Unresolved/@You/Agents filters, rows with
 *     unread badges).
 *   - Share dialog + facepile backed by the real participants contract, and
 *     "Inherit this take" from a thread on a finished take.
 *
 * State is CONTRACT-BACKED: everything loads from transport.collab (the mock
 * backend and the real /sessions/{id}/collab endpoints serve identical
 * shapes), and live comment.* / participant.* events (real WS fan-out, or the
 * mock's connection) merge in as they arrive. @agent mentions hand the work
 * to the model server-side.
 *
 * Integration: canvas-mode calls EdennCollab.onCanvasRender/onFocusChange;
 * app.js forwards comment/participant events to EdennCollab.onEvent.
 * ========================================================================== */
(function () {
  "use strict";

  // ---- tiny DOM utils (self-contained; mirrors canvas-mode style) ---------
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
  function host() { return window.__edenn || {}; }
  function conn() { return window.__edenn && window.__edenn.app && window.__edenn.app.conn; }
  function transport() { return window.__edenn && window.__edenn.app && window.__edenn.app.transport; }
  function collabApi() { const t = transport(); return t && t.collab; }
  function isMock() { const t = transport(); return !t || t.kind === "mock"; }
  function appPersona() { return (host().persona && host().persona()) || null; }
  // The viewer's effective role, computed from what the client can see: the
  // session creator is the owner; anyone else takes their participant record's
  // role (default comment — the share-link default). Server enforcement is the
  // real gate when auth is on; this powers honest UI affordances.
  function viewerRole() {
    const snap = (host().app && host().app.snapshot) || clb.lastSnap;
    const me = clb.viewer.id;
    const creator = snap && snap.creator_user_id;
    if (!creator || creator === me) return "owner";
    const p = clb.participants.find((x) => x.user_id === me);
    return (p && p.role) || "comment";
  }
  function roleAtLeast(role, minimum) {
    const order = { view: 0, comment: 1, iterate: 2, owner: 3 };
    return (order[role] != null ? order[role] : 1) >= (order[minimum] != null ? order[minimum] : 1);
  }
  const NODE_W = 210; // must match canvas-mode's .cv-node width (pin anchoring)
  const REACTION_SET = ["👍", "❤️", "🔥", "👏", "🤔"];

  function fmtClock(s) {
    s = Math.max(0, Math.floor(s || 0));
    return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }
  function fmtAttDur(s) {
    s = Math.max(0, Math.round(s || 0));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }
  function ago(iso) {
    if (!iso) return "";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just now";
    const m = Math.floor(s / 60);
    if (m < 60) return m + "m ago";
    const h = Math.floor(m / 60);
    if (h < 24) return h + "h ago";
    return Math.floor(h / 24) + "d ago";
  }

  // ---- module state -------------------------------------------------------
  const clb = {
    sid: null,
    // Placeholder until boot — the app's persona takes over immediately
    // (resetFor / refresh), so every write carries a real, distinct person.
    viewer: { id: "you", name: "You" },
    threads: [],        // server-shaped, each with .comments[] and .unread
    participants: [],
    agents: [],
    loaded: false,
    railOpen: false,
    filter: "all",      // all | unresolved | you | agents
    search: "",
    openThreadId: null, // thread card open on the canvas
    draftAnchor: null,  // { nodeId, label, startS, endS } — composing a new thread
    commentMode: false, // click-a-node-to-comment
    editing: null,      // comment_id being edited
    drafts: {},         // draftKey -> { text, mentions:[], attachments:[] }
    inherited: {},      // candidate_id -> true (branches created via Inherit)
    lastNodes: {},
    lastSnap: null,
    lastFocus: null,
    pendingInherit: null, // { parent, existing:Set }
    cardKey: null,
    railKey: null,
    booted: false,
  };

  // DOM refs
  let rail, cardWrap, modeBtn, dockCommentBtn, shareOverlay, inheritOverlay, worldObserver;

  // ---- people helpers -----------------------------------------------------
  const AV_PALETTE = ["you", "teal", "amber", "rose", "sky"];
  function avClass(id, kind) {
    if (kind === "agent") return "agent";
    if (id === clb.viewer.id) return "you";
    let hash = 0;
    for (let i = 0; i < String(id).length; i++) hash = (hash * 31 + String(id).charCodeAt(i)) | 0;
    return AV_PALETTE[1 + (Math.abs(hash) % (AV_PALETTE.length - 1))];
  }
  function personName(id) {
    if (id === clb.viewer.id) return clb.viewer.name;
    const p = clb.participants.find((x) => x.user_id === id);
    if (p) return p.display_name || p.user_id;
    const a = clb.agents.find((x) => x.id === id);
    if (a) return a.name;
    return id || "Someone";
  }
  function avatar(id, name, kind, extraCls) {
    const cls = avClass(id, kind);
    const label = name || personName(id);
    const a = el("span",
      "clb-av " + cls + (kind === "agent" ? " clb-av--agent" : "") + (extraCls ? " " + extraCls : ""),
      esc((label || "?").charAt(0).toUpperCase()));
    a.title = label + (kind === "agent" ? " · agent" : "");
    return a;
  }

  // ---- state lookups ------------------------------------------------------
  function threadById(id) { return clb.threads.find((t) => t.thread_id === id) || null; }
  function liveComments(t) { return (t.comments || []).filter((c) => !c.deleted); }
  function threadsForNode(nodeId) { return clb.threads.filter((t) => t.anchor_node_id === nodeId); }
  function totalUnread() { return clb.threads.reduce((n, t) => n + (t.unread || 0), 0); }
  function nodeLabel(nodeId) {
    const n = clb.lastNodes[nodeId];
    if (!n) return null;
    if (n.kind === "source") return (n.data.obs && n.data.obs.video_title) || "Source video";
    if (n.kind === "proposal") return n.data.title || "Direction";
    return n.data.title || ("Take " + (n.data.version || 1));
  }

  // ========================================================================
  // Data: load + event merge (contract-backed; no client-only ghosts)
  // ========================================================================
  async function refresh() {
    const api = collabApi();
    if (!api || !clb.sid) return;
    try {
      const payload = await api.get(clb.sid);
      clb.threads = payload.threads || [];
      clb.participants = payload.participants || [];
      clb.agents = payload.agents || [];
      if (payload.viewer) {
        // Prefer the app persona's display name when the server echoes our own
        // id back (auth-off), so "you" never renders as a raw id slug.
        const per = appPersona();
        clb.viewer = {
          id: payload.viewer,
          name: (per && per.id === payload.viewer && per.name) || personName(payload.viewer),
        };
      }
      clb.loaded = true;
      paintAll();
      maybeOpenDeepLink();
    } catch (err) {
      // The canvas works without collab — but silence here made a failed load
      // indistinguishable from "no comments yet". Say it once, visibly.
      console.warn("collab load failed:", err);
      if (!clb._warnedDegraded) {
        clb._warnedDegraded = true;
        if (host().toast) host().toast("Comments are unavailable right now — the canvas still works.");
      }
    }
  }

  function upsertThread(threadPayload) {
    if (!threadPayload) return;
    const i = clb.threads.findIndex((t) => t.thread_id === threadPayload.thread_id);
    if (i >= 0) {
      // Keep the local unread counter unless the payload carries one for us.
      const unread = threadPayload.unread != null ? threadPayload.unread : clb.threads[i].unread;
      clb.threads[i] = Object.assign({}, threadPayload, { unread });
    } else {
      clb.threads.push(Object.assign({ unread: 0 }, threadPayload));
    }
  }

  function onEvent(evt) {
    // Never merge another session's events (a stale socket, a resume race).
    if (evt.session_id && clb.sid && evt.session_id !== clb.sid) return;
    const p = evt.payload || {};
    if (evt.event_type === "comment.thread.created" || evt.event_type === "comment.thread.updated") {
      // The mock re-broadcasts our own mutations too — the merge is idempotent.
      const known = threadById((p.thread || {}).thread_id);
      upsertThread(p.thread);
      const t = threadById((p.thread || {}).thread_id);
      // Fallback unread for a brand-new thread from someone else — only when
      // the payload didn't carry a per-viewer count of its own.
      if (t && evt.event_type === "comment.thread.created" && !known
          && (p.thread || {}).unread == null
          && t.created_by !== clb.viewer.id && clb.openThreadId !== t.thread_id) {
        t.unread = liveComments(t).filter((c) => c.author_id !== clb.viewer.id).length;
      }
    } else if (evt.event_type === "comment.created" || evt.event_type === "comment.updated") {
      const t = threadById(p.thread_id);
      if (!t || !p.comment) { refresh(); return; }
      const comments = t.comments || (t.comments = []);
      const i = comments.findIndex((c) => c.comment_id === p.comment.comment_id);
      if (i >= 0) comments[i] = p.comment;
      else {
        comments.push(p.comment);
        t.updated_at = p.comment.created_at || t.updated_at; // rail recency
        if (p.comment.author_id !== clb.viewer.id) {
          if (clb.openThreadId === t.thread_id) markRead(t); // reading it live
          else t.unread = (t.unread || 0) + 1;
        }
        if (t.status === "resolved" && evt.event_type === "comment.created") t.status = "open";
      }
    } else if (evt.event_type === "participant.updated") {
      const q = p.participant;
      if (q) {
        const prev = clb.participants.find((x) => x.user_id === q.user_id);
        const roleChanged = prev && prev.role !== q.role;
        const i = clb.participants.findIndex((x) => x.user_id === q.user_id);
        if (i >= 0) clb.participants[i] = q; else clb.participants.push(q);
        // My own role moved: the server pins the turn-gate role at WS connect,
        // so reconnect NOW — a promotion should not need a manual reload.
        if (roleChanged && q.user_id === clb.viewer.id) {
          if (host().toast) host().toast(
            "Your access is now “" + (ROLE_LABELS[q.role] || q.role) + "” — reconnecting.");
          if (host().resumeSession && clb.sid) host().resumeSession(clb.sid, { fromHistory: true });
        }
      }
    } else {
      return;
    }
    paintAll();
  }

  function markRead(t) {
    if (!t) return;
    t.unread = 0;
    const api = collabApi();
    if (api) api.markRead(clb.sid, t.thread_id).catch(() => {});
  }

  // ========================================================================
  // Canvas hooks
  // ========================================================================
  function onCanvasRender(ctx) {
    if (!clb.booted) boot();
    clb.lastSnap = ctx.snap;
    clb.lastNodes = ctx.nodes || {};
    const sid = ctx.snap && ctx.snap.session_id;
    if (sid && sid !== clb.sid) {
      resetFor(sid);
      refresh();
    }
    resolveInherit();
    drawPins(ctx.world);
    decorateNodes(ctx.world);
    paintRail();
    // Memoized: repaints only when the card's state key changed (e.g. the
    // anchored take finished rendering, so the Inherit link appears).
    paintCard(false);
    updateDockButtons(clb.lastFocus == null ? ctx.focusId : clb.lastFocus);
  }

  function onFocusChange(focusId) { updateDockButtons(focusId); }

  function resetFor(sid) {
    clb.sid = sid;
    // Adopt the app's persona as the viewer identity for this session.
    const per = appPersona();
    if (per) clb.viewer = { id: per.id, name: per.name };
    clb.threads = []; clb.participants = []; clb.agents = [];
    clb.loaded = false; clb.openThreadId = null; clb.draftAnchor = null;
    clb.commentMode = false; clb.editing = null; clb.drafts = {};
    clb.inherited = {}; clb.pendingInherit = null;
    clb.cardKey = null; clb.railKey = null;
    clb._warnedDegraded = false;
    closeCard();
    setCommentMode(false);
    paintFacepile();
  }

  // ========================================================================
  // Boot
  // ========================================================================
  function boot() {
    const stage = document.getElementById("cstage");
    if (!stage || clb.booted) return;
    clb.booted = true;

    // Threads rail — third stage column (chat | tree | rail).
    rail = el("aside", "clb-rail is-collapsed");
    rail.setAttribute("aria-label", "Comment threads");
    stage.appendChild(rail);

    const vp = document.getElementById("cv-vp");
    if (vp) {
      // Comment mode chip — top-right of the tree (design frame 2).
      modeBtn = el("button", "clb-mode", '<i class="ti ti-message-2"></i> Comment');
      modeBtn.type = "button";
      modeBtn.title = "Comment on a take — click a node to drop the thread";
      modeBtn.setAttribute("aria-pressed", "false");
      modeBtn.addEventListener("pointerdown", (e) => e.stopPropagation());
      modeBtn.addEventListener("click", () => setCommentMode(!clb.commentMode));
      vp.appendChild(modeBtn);

      // Floating thread-card overlay (unscaled, anchored to pins).
      cardWrap = el("div", "clb-cardwrap");
      cardWrap.hidden = true;
      cardWrap.addEventListener("pointerdown", (e) => e.stopPropagation());
      cardWrap.addEventListener("wheel", (e) => e.stopPropagation());
      vp.appendChild(cardWrap);

      // Comment-mode: a node click drops the composer on that node.
      vp.addEventListener("click", (e) => {
        if (!clb.commentMode) return;
        const node = e.target.closest(".cv-node");
        if (!node) return;
        e.stopPropagation();
        openDraft(node.getAttribute("data-id"));
        setCommentMode(false);
      }, true);

      // Keep the floating card glued to its pin through pan/zoom (canvas-mode
      // writes world.style.transform on every apply()).
      const world = document.getElementById("cv-world");
      if (world && typeof MutationObserver !== "undefined") {
        worldObserver = new MutationObserver(() => positionCard());
        worldObserver.observe(world, { attributes: true, attributeFilter: ["style"] });
      }
      window.addEventListener("resize", () => positionCard());
    }

    const dockBtns = document.querySelector("#cv-dock .cv-dock__btns");
    if (dockBtns) {
      dockCommentBtn = el("button", "cv-dbtn clb-comment-btn", '<i class="ti ti-message-2"></i> Comment');
      dockCommentBtn.type = "button";
      dockCommentBtn.addEventListener("click", () => {
        if (!clb.lastFocus) return;
        const existing = threadsForNode(clb.lastFocus).filter((t) => t.status !== "resolved");
        if (existing.length) openThread(existing[existing.length - 1].thread_id);
        else openDraft(clb.lastFocus);
      });
      dockBtns.appendChild(dockCommentBtn);
    }

    const shareBtn = document.getElementById("tb-share");
    if (shareBtn) shareBtn.addEventListener("click", () => openShare());
    buildShareDialog();
    buildInheritDialog();

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if (shareOverlay && !shareOverlay.hidden) { hideOverlay(shareOverlay); return; }
        if (inheritOverlay && !inheritOverlay.hidden) { hideOverlay(inheritOverlay); return; }
        if (clb.commentMode) { setCommentMode(false); return; }
        if (!cardWrap || cardWrap.hidden) return;
        closeCard();
      }
    });

    // Adopt the live session in EVERY view (Share works from Chat, deep links
    // land before the canvas is ever opened) — canvas renders also adopt, this
    // is the fallback heartbeat for chat-only usage.
    setInterval(() => {
      const sid = host().app && host().app.sessionId;
      if (sid && sid !== clb.sid) { resetFor(sid); refresh(); }
    }, 2000);
  }

  function setCommentMode(on) {
    clb.commentMode = !!on;
    const vp = document.getElementById("cv-vp");
    if (vp) vp.classList.toggle("clb-commenting", clb.commentMode);
    if (modeBtn) {
      modeBtn.classList.toggle("is-on", clb.commentMode);
      modeBtn.setAttribute("aria-pressed", clb.commentMode ? "true" : "false");
    }
  }

  // ---- overlays: show/hide with focus restore + a minimal Tab trap --------
  let lastTrigger = null;
  function showOverlay(ov) { lastTrigger = document.activeElement; ov.hidden = false; }
  function hideOverlay(ov) {
    ov.hidden = true;
    if (lastTrigger && lastTrigger.focus && document.contains(lastTrigger)) lastTrigger.focus();
    lastTrigger = null;
  }
  function trapTab(ov) {
    ov.addEventListener("keydown", (e) => {
      if (e.key !== "Tab") return;
      const f = ov.querySelectorAll("button, input, select, textarea, [tabindex]:not([tabindex='-1'])");
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
  }

  // ========================================================================
  // Pins
  // ========================================================================
  function drawPins(world) {
    if (!world) return;
    world.querySelectorAll(".clb-pin").forEach((n) => n.remove());
    const perNode = {};
    clb.threads.forEach((t) => {
      const n = clb.lastNodes[t.anchor_node_id];
      if (!n) return;
      const stack = perNode[t.anchor_node_id] = (perNode[t.anchor_node_id] || 0) + 1;
      const pin = el("button", "clb-pin");
      pin.type = "button";
      pin.style.left = (n.x + NODE_W - 16 - (stack - 1) * 24) + "px";
      pin.style.top = (n.y - 13) + "px";
      const comments = liveComments(t);
      const last = comments[comments.length - 1];
      if (t.status === "resolved") {
        pin.classList.add("is-resolved");
        pin.appendChild(el("span", "clb-pin__check", '<i class="ti ti-check"></i>'));
        pin.title = "Resolved — open to review or reopen";
      } else if ((t.unread || 0) > 0) {
        pin.classList.add("is-unread");
        pin.appendChild(el("span", "clb-pin__count", String(t.unread)));
        pin.title = t.unread + " unread";
      } else if (last) {
        if (last.author_kind === "agent") pin.classList.add("is-agent");
        pin.appendChild(avatar(last.author_id, last.author_name, last.author_kind));
        pin.title = "Thread — " + (last.author_name || personName(last.author_id));
      } else {
        pin.appendChild(el("span", "clb-pin__count", "…"));
      }
      if (clb.openThreadId === t.thread_id) pin.classList.add("is-open");
      pin.setAttribute("aria-label", "Comments on " + (nodeLabel(t.anchor_node_id) || "this node"));
      pin.addEventListener("pointerdown", (e) => e.stopPropagation());
      pin.addEventListener("click", (e) => { e.stopPropagation(); openThread(t.thread_id); });
      world.appendChild(pin);
    });
    // Draft pin (the "New" state — blue +) while composing on a node.
    if (clb.draftAnchor) {
      const n = clb.lastNodes[clb.draftAnchor.nodeId];
      if (n) {
        const stack = (perNode[clb.draftAnchor.nodeId] || 0) + 1;
        const pin = el("span", "clb-pin is-new");
        pin.style.left = (n.x + NODE_W - 16 - (stack - 1) * 24) + "px";
        pin.style.top = (n.y - 13) + "px";
        pin.appendChild(el("span", "clb-pin__count", "+"));
        world.appendChild(pin);
      }
    }
  }

  // "inherited" badges on branches created through Inherit (client-side note;
  // server-side authorship lands with the identity provider).
  function decorateNodes(world) {
    if (!world) return;
    Object.keys(clb.inherited).forEach((cid) => {
      const node = world.querySelector('.cv-node[data-id="' + cid + '"]');
      if (!node || node.querySelector(".clb-inh")) return;
      const tags = node.querySelector(".cv-tags");
      if (tags) {
        const b = el("span", "cv-badge clb-inh", '<i class="ti ti-git-fork"></i> inherited');
        b.title = "Inherited from a collaborator's take";
        tags.appendChild(b);
      }
    });
  }

  function updateDockButtons(focusId) {
    clb.lastFocus = focusId || null;
    const n = focusId && clb.lastNodes[focusId];
    if (dockCommentBtn) dockCommentBtn.style.display = n ? "" : "none";
  }

  // ========================================================================
  // Floating thread card (composer + thread states)
  // ========================================================================
  function worldTransform() {
    const world = document.getElementById("cv-world");
    // Numbers may serialize in exponential notation at extreme pans — accept it.
    const NUM = "([-+0-9.eE]+)";
    const re = new RegExp("translate\\(" + NUM + "px,\\s*" + NUM + "px\\)\\s*scale\\(" + NUM + "\\)");
    const m = world && re.exec(world.style.transform || "");
    return m ? { tx: +m[1], ty: +m[2], s: +m[3] } : { tx: 0, ty: 0, s: 1 };
  }

  function anchorWorldPos() {
    const anchorNode = clb.draftAnchor ? clb.draftAnchor.nodeId
      : (clb.openThreadId && (threadById(clb.openThreadId) || {}).anchor_node_id);
    const n = anchorNode && clb.lastNodes[anchorNode];
    if (!n) return null;
    return { x: n.x + NODE_W - 4, y: n.y + 8 };
  }

  function positionCard() {
    if (!cardWrap || cardWrap.hidden) return;
    const vp = document.getElementById("cv-vp");
    const pos = anchorWorldPos();
    if (!vp || !pos) return;
    // Never wider than the viewport (narrow windows with the rail expanded).
    cardWrap.style.maxWidth = Math.max(200, vp.clientWidth - 24) + "px";
    const { tx, ty, s } = worldTransform();
    let x = pos.x * s + tx + 14;
    let y = pos.y * s + ty - 8;
    const maxX = vp.clientWidth - cardWrap.offsetWidth - 12;
    const maxY = vp.clientHeight - cardWrap.offsetHeight - 12;
    // Flip to the node's left edge when the right side would clip.
    if (x > maxX) x = Math.max(12, (pos.x - NODE_W + 4) * s + tx - cardWrap.offsetWidth - 14);
    cardWrap.style.left = Math.max(12, Math.min(maxX, x)) + "px";
    cardWrap.style.top = Math.max(12, Math.min(Math.max(12, maxY), y)) + "px";
  }

  function openDraft(nodeId) {
    if (!nodeId || !clb.lastNodes[nodeId]) return;
    if (!roleAtLeast(viewerRole(), "comment")) {
      if (host().toast) host().toast("You have view access — ask the owner for comment access to join in.");
      return;
    }
    clb.openThreadId = null;
    clb.editing = null;
    clb.draftAnchor = buildAnchor(nodeId);
    paintCard(true);
    requestPinsRedraw();
  }

  function openThread(threadId) {
    const t = threadById(threadId);
    if (!t) return;
    clb.draftAnchor = null;
    clb.editing = null;
    clb.openThreadId = threadId;
    markRead(t);
    if (window.EdennCanvas && window.EdennCanvas.focusNode && clb.lastNodes[t.anchor_node_id]) {
      window.EdennCanvas.focusNode(t.anchor_node_id);
    }
    paintCard(true);
    requestPinsRedraw();
    paintRail();
  }

  function closeCard() {
    // "Esc to discard" means it: closing an unposted draft drops its text.
    if (clb.draftAnchor) delete clb.drafts["new:" + clb.draftAnchor.nodeId];
    clb.openThreadId = null;
    clb.draftAnchor = null;
    clb.editing = null;
    clb.expandResolved = null;
    clb.cardKey = null;
    stopAttachmentAudio();
    if (cardWrap) { cardWrap.hidden = true; cardWrap.innerHTML = ""; }
    requestPinsRedraw();
  }

  // One attachment plays at a time; re-renders and card close stop it.
  let attAudio = null;
  function stopAttachmentAudio() {
    if (attAudio) { try { attAudio.pause(); } catch (_) {} attAudio = null; }
  }
  function playAttachment(url) {
    if (attAudio && !attAudio.paused && attAudio.src === url) { stopAttachmentAudio(); return; }
    stopAttachmentAudio();
    attAudio = new Audio(window.__edennMediaSrc ? window.__edennMediaSrc(url) : url);
    attAudio.play().catch(() => {});
  }

  // The anchor carries the layer + the moment: node label, plus the dock's
  // current playback position (a ~6s region) when the take is loaded there.
  function buildAnchor(nodeId) {
    const n = clb.lastNodes[nodeId];
    const st = (clb.lastSnap && clb.lastSnap.state) || {};
    let label = nodeLabel(nodeId) || "This node";
    let startS = null, endS = null;
    const time = document.getElementById("cv-dock-time");
    const focusedHere = clb.lastFocus === nodeId;
    if (focusedHere && time) {
      const m = /^(\d+):(\d\d)\s*\/\s*(\d+):(\d\d)$/.exec(time.textContent.trim());
      if (m) {
        const cur = (+m[1]) * 60 + (+m[2]);
        const dur = (+m[3]) * 60 + (+m[4]);
        if (dur > 0) { startS = Math.floor(cur); endS = Math.min(dur, startS + 6); }
      }
    }
    if (n && n.kind === "source" && startS != null) {
      const scene = ((st.observation || {}).scenes || []).find(
        (sc) => startS >= sc.start_s && startS < sc.end_s
      );
      if (scene) label = "Scene " + (scene.index + 1) + " — " + (scene.label || label);
    }
    return { nodeId, label, startS, endS };
  }

  function draftFor(key) {
    return clb.drafts[key] || (clb.drafts[key] = { text: "", mentions: [], attachments: [] });
  }

  // ---- card painting ------------------------------------------------------
  function cardStateKey() {
    if (clb.draftAnchor) return "draft:" + clb.draftAnchor.nodeId;
    const t = clb.openThreadId && threadById(clb.openThreadId);
    if (!t) return "none";
    // The anchored take's readiness matters too (the Inherit link appears the
    // moment it finishes rendering).
    const anchorNode = clb.lastNodes[t.anchor_node_id];
    const cand = anchorNode && anchorNode.kind === "candidate" ? anchorNode.data : null;
    return JSON.stringify([
      t.thread_id, t.status, clb.editing, clb.expandResolved === t.thread_id,
      cand ? [cand.status, !!(cand.audio_url || cand.video_url)] : null,
      (t.comments || []).map((c) => [c.comment_id, c.body, c.deleted, c.edited_at,
        JSON.stringify(c.reactions || {})]),
    ]);
  }

  function paintCard(force) {
    if (!cardWrap) return;
    const key = cardStateKey();
    if (!force && key === clb.cardKey) { positionCard(); return; }
    clb.cardKey = key;
    stopAttachmentAudio(); // a re-render must not leave orphaned playback
    cardWrap.innerHTML = "";
    if (clb.draftAnchor) {
      cardWrap.appendChild(buildDraftCard());
      cardWrap.hidden = false;
    } else if (clb.openThreadId) {
      const t = threadById(clb.openThreadId);
      if (!t) { closeCard(); return; }
      const resolvedCollapsed = t.status === "resolved" && clb.expandResolved !== t.thread_id;
      cardWrap.appendChild(resolvedCollapsed ? buildResolvedCard(t) : buildThreadCard(t));
      cardWrap.hidden = false;
    } else {
      cardWrap.hidden = true;
      return;
    }
    positionCard();
    // Measurements (offsetWidth) settle after layout — position twice.
    requestAnimationFrame(positionCard);
  }

  function requestPinsRedraw() {
    const world = document.getElementById("cv-world");
    if (world) { drawPins(world); decorateNodes(world); }
  }

  // ---- anchor chip --------------------------------------------------------
  function anchorChip(anchor, removable, onRemove) {
    const chip = el("div", "clb-anchor");
    chip.appendChild(el("span", "clb-anchor__bar"));
    chip.appendChild(el("span", "clb-anchor__label", esc(anchor.label || "")));
    if (anchor.startS != null && anchor.endS != null) {
      chip.appendChild(el("span", "clb-anchor__time",
        esc(fmtClock(anchor.startS) + " – " + fmtClock(anchor.endS))));
    }
    if (removable) {
      const x = el("button", "clb-anchor__x", '<i class="ti ti-x"></i>');
      x.type = "button"; x.title = "Comment on the whole take instead";
      x.addEventListener("click", onRemove);
      chip.appendChild(x);
    }
    return chip;
  }

  // ---- composer (shared by new thread + reply; toolbar treatment A) -------
  function buildComposer(opts) {
    const draft = draftFor(opts.draftKey);
    const box = el("div", "clb-composer" + (opts.compact ? " is-compact" : ""));
    // Anchor chip above the text; attachment chips BETWEEN text and toolbar
    // (spec: the writing area never jumps).
    const anchorWrap = el("div", "clb-composer__chips");
    box.appendChild(anchorWrap);

    const ta = document.createElement("textarea");
    ta.className = "clb-composer__text";
    ta.placeholder = opts.placeholder || "Add a comment…";
    ta.rows = 1;
    ta.value = draft.text;
    ta.setAttribute("aria-label", opts.placeholder || "Write a comment");
    box.appendChild(ta);

    const chips = el("div", "clb-composer__chips");
    box.appendChild(chips);

    const row = el("div", "clb-composer__row");
    const attach = el("button", "clb-tool", '<i class="ti ti-plus"></i>');
    attach.type = "button"; attach.title = "Attach a reference";
    const mention = el("button", "clb-tool", '<i class="ti ti-at"></i>');
    mention.type = "button"; mention.title = "Mention a person or an agent";
    const send = el("button", "clb-send", '<i class="ti ti-send"></i>');
    send.type = "button"; send.title = "Post";
    row.appendChild(attach); row.appendChild(mention);
    row.appendChild(el("span", "clb-composer__spacer"));
    row.appendChild(send);
    box.appendChild(row);
    const hint = el("div", "clb-composer__hint", "⌘↵ to post · Esc to discard");
    hint.hidden = true;
    box.appendChild(hint);

    const picker = el("div", "clb-picker");
    picker.hidden = true;
    box.appendChild(picker);

    function grow() {
      ta.style.height = "auto";
      // scrollHeight is 0 while the card is still detached — keep auto then
      // (the CSS min-height carries it) and re-measure after attach.
      if (ta.scrollHeight > 0) ta.style.height = Math.min(120, ta.scrollHeight) + "px";
    }
    function syncState() {
      const has = !!ta.value.trim();
      send.classList.toggle("is-live", has);
      hint.hidden = !has;
      grow();
      positionCard();
    }
    function renderChips() {
      anchorWrap.innerHTML = "";
      chips.innerHTML = "";
      if (opts.anchor) {
        anchorWrap.appendChild(anchorChip(opts.anchor, opts.anchorRemovable && opts.anchor.startS != null, () => {
          opts.anchor.startS = null; opts.anchor.endS = null;
          renderChips();
        }));
      }
      anchorWrap.hidden = !anchorWrap.childNodes.length;
      draft.attachments.forEach((att, i) => {
        const chip = el("span", "clb-att");
        chip.innerHTML = (att.kind === "audio"
          ? '<i class="ti ti-wave-saw-tool"></i> '
          : '<i class="ti ti-photo"></i> ')
          + esc(att.name || att.kind)
          + (att.duration_s ? ' <span class="clb-att__dur">· ' + esc(fmtAttDur(att.duration_s)) + "</span>" : "");
        const x = el("button", "clb-att__x", '<i class="ti ti-x"></i>');
        x.type = "button";
        x.addEventListener("click", () => { draft.attachments.splice(i, 1); renderChips(); });
        chip.appendChild(x);
        chips.appendChild(chip);
      });
      chips.hidden = !chips.childNodes.length;
      positionCard();
    }

    // @mention picker — one list, two groups; agents get the square + chip.
    let pickerOpen = false;
    function openPicker(query) {
      const q = (query || "").toLowerCase();
      const people = [{ id: clb.viewer.id, name: clb.viewer.name + " (you)", kind: "user" }]
        .concat(clb.participants.map((p) => ({ id: p.user_id, name: p.display_name || p.user_id, kind: "user" })));
      const agents = clb.agents.map((a) => ({ id: a.id, name: a.name, kind: "agent" }));
      const match = (x) => !q || x.name.toLowerCase().indexOf(q) >= 0;
      const peopleHits = people.filter(match);
      const agentHits = agents.filter(match);
      picker.innerHTML = "";
      if (!peopleHits.length && !agentHits.length) { picker.hidden = true; pickerOpen = false; return; }
      if (peopleHits.length) picker.appendChild(el("div", "clb-picker__hd", "People"));
      peopleHits.forEach((r) => picker.appendChild(pickerRow(r)));
      if (agentHits.length) picker.appendChild(el("div", "clb-picker__hd", "Agents on this canvas"));
      agentHits.forEach((r) => picker.appendChild(pickerRow(r)));
      picker.hidden = false;
      pickerOpen = true;
      positionCard();
    }
    function pickerRow(r) {
      const row = el("button", "clb-picker__row");
      row.type = "button";
      row.appendChild(avatar(r.id, r.name.replace(" (you)", ""), r.kind, "clb-av--sm"));
      row.appendChild(el("span", "clb-picker__nm", esc(r.name)));
      if (r.kind === "agent") row.appendChild(el("span", "clb-agentchip", "AGENT"));
      row.addEventListener("mousedown", (e) => { e.preventDefault(); pickMention(r); });
      return row;
    }
    function closePicker() { picker.hidden = true; pickerOpen = false; positionCard(); }
    function mentionQuery() {
      const caret = ta.selectionStart == null ? ta.value.length : ta.selectionStart;
      const upToCaret = ta.value.slice(0, caret);
      const m = /(^|\s)@([\w .-]*)$/.exec(upToCaret);
      return m ? { query: m[2], start: upToCaret.length - m[2].length - 1, caret } : null;
    }
    function pickMention(person) {
      const m = mentionQuery();
      const name = person.name.replace(" (you)", "");
      let caretAfter;
      if (m) {
        const before = ta.value.slice(0, m.start);
        const after = ta.value.slice(m.caret);
        ta.value = before + "@" + name + " " + after;
        caretAfter = before.length + name.length + 2;
      } else {
        ta.value = (ta.value ? ta.value + " " : "") + "@" + name + " ";
        caretAfter = ta.value.length;
      }
      if (!draft.mentions.some((x) => x.id === person.id)) {
        draft.mentions.push({ id: person.id, name, kind: person.kind });
      }
      draft.text = ta.value;
      closePicker();
      ta.focus();
      ta.setSelectionRange(caretAfter, caretAfter); // keep typing where the mention landed
      syncState();
    }

    ta.addEventListener("input", () => {
      draft.text = ta.value;
      syncState();
      const m = mentionQuery();
      if (m) openPicker(m.query); else closePicker();
    });
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); post(); }
      else if (e.key === "Escape" && pickerOpen) { e.preventDefault(); e.stopPropagation(); closePicker(); }
    });
    mention.addEventListener("click", () => {
      ta.focus();
      if (!/@$/.test(ta.value)) ta.value += (ta.value && !/\s$/.test(ta.value) ? " @" : "@");
      draft.text = ta.value;
      openPicker("");
      syncState();
    });

    // Attach menu: reference the focused take; images stay offline-only (there
    // is no shared image store yet — honesty over a dead upload).
    const attMenu = el("div", "clb-attmenu");
    attMenu.hidden = true;
    box.appendChild(attMenu);
    attach.addEventListener("click", () => {
      if (!attMenu.hidden) { attMenu.hidden = true; return; }
      attMenu.innerHTML = "";
      const focused = clb.lastFocus && clb.lastNodes[clb.lastFocus];
      const cand = focused && focused.kind === "candidate" ? focused.data : null;
      const canRef = cand && cand.status === "completed" && (cand.audio_url || cand.video_url);
      const refRow = el("button", "clb-attmenu__row" + (canRef ? "" : " is-off"),
        '<i class="ti ti-wave-saw-tool"></i> Reference the focused take');
      refRow.type = "button";
      if (canRef) refRow.addEventListener("click", () => {
        draft.attachments.push({
          kind: "audio",
          name: (cand.title || "Take") + ".wav",
          url: cand.audio_url || cand.video_url,
        });
        attMenu.hidden = true;
        renderChips();
      });
      else refRow.title = "Focus a finished take first";
      attMenu.appendChild(refRow);
      // Camera affordance (spec 06 "Image or frame"): grab the CURRENT FRAME of
      // whatever is playing in the dock as a reference image — "this exact shot".
      const dockVideo = document.querySelector("#cv-dock-thumb video");
      const canFrame = !!(dockVideo && dockVideo.videoWidth);
      const frameRow = el("button", "clb-attmenu__row" + (canFrame ? "" : " is-off"),
        '<i class="ti ti-camera"></i> Current frame');
      frameRow.type = "button";
      if (canFrame) frameRow.addEventListener("click", () => {
        try {
          const cvs = document.createElement("canvas");
          const scale = Math.min(1, 480 / dockVideo.videoWidth);
          cvs.width = Math.round(dockVideo.videoWidth * scale);
          cvs.height = Math.round(dockVideo.videoHeight * scale);
          cvs.getContext("2d").drawImage(dockVideo, 0, 0, cvs.width, cvs.height);
          const t = Math.floor(dockVideo.currentTime || 0);
          draft.attachments.push({
            kind: "image",
            name: "frame@" + Math.floor(t / 60) + ":" + String(t % 60).padStart(2, "0") + ".jpg",
            url: cvs.toDataURL("image/jpeg", 0.82),
          });
          renderChips();
        } catch (err) {
          if (host().toast) host().toast("Couldn't grab that frame — the video may not be loaded yet.");
        }
        attMenu.hidden = true;
      });
      else frameRow.title = "Play a take in the dock first";
      attMenu.appendChild(frameRow);
      if (isMock()) {
        const imgRow = el("button", "clb-attmenu__row", '<i class="ti ti-photo"></i> Image…');
        imgRow.type = "button";
        imgRow.addEventListener("click", () => {
          const input = document.createElement("input");
          input.type = "file"; input.accept = "image/*";
          input.onchange = () => {
            const f = input.files && input.files[0];
            if (!f) return;
            const reader = new FileReader();
            reader.onload = () => {
              draft.attachments.push({ kind: "image", name: f.name, url: String(reader.result) });
              renderChips();
            };
            reader.readAsDataURL(f);
          };
          input.click();
          attMenu.hidden = true;
        });
        attMenu.appendChild(imgRow);
      }
      attMenu.hidden = false;
      positionCard();
    });

    async function post() {
      const text = ta.value.trim();
      if (!text) return;
      const mentions = draft.mentions.filter((m) => text.indexOf("@" + m.name) >= 0);
      const submit = async () => {
        send.disabled = true;
        try {
          await opts.onPost({ body: text, mentions, attachments: draft.attachments.slice() });
          delete clb.drafts[opts.draftKey];
        } catch (err) {
          if (host().toast) host().toast("Couldn't post that — " + ((err && err.message) || "try again."));
        } finally {
          send.disabled = false;
        }
      };
      // Handing a note to the director can GENERATE a take (spends), so confirm
      // first — same bar as every other generation. This fires when the comment
      // @mentions the agent, or continues a thread the agent is already in (the
      // backend dispatches a pickup in both cases).
      const dispatchesToAgent = mentions.some((m) => m.kind === "agent") || !!opts.threadHasAgent;
      const h = host();
      if (dispatchesToAgent && h.confirmSpend) {
        h.confirmSpend(
          "Hand this to the director?",
          "If your note asks for a creative change, the director will generate a new take — and that spends. A plain question or discussion is free.",
          submit
        );
      } else {
        submit();
      }
    }
    send.addEventListener("click", post);

    renderChips();
    syncState();
    requestAnimationFrame(() => {
      syncState(); // re-measure once attached (scrollHeight was 0 detached)
      if (opts.autofocus) ta.focus();
    });
    return box;
  }

  // ---- new-thread card ----------------------------------------------------
  function buildDraftCard() {
    const card = el("div", "clb-card");
    const anchor = clb.draftAnchor;
    card.appendChild(buildComposer({
      draftKey: "new:" + anchor.nodeId,
      placeholder: "Start a new thread…",
      anchor,
      anchorRemovable: true,
      autofocus: true,
      onPost: async (payload) => {
        const api = collabApi();
        if (!api) return;
        const res = await api.createThread(clb.sid, {
          anchor_node_id: anchor.nodeId,
          anchor_label: anchor.label,
          anchor_start_s: anchor.startS,
          anchor_end_s: anchor.endS,
          body: payload.body,
          mentions: payload.mentions,
          attachments: payload.attachments,
        });
        upsertThread(Object.assign({}, res.thread, { unread: 0 }));
        clb.draftAnchor = null;
        clb.openThreadId = res.thread.thread_id;
        paintAll(true);
      },
    }));
    const close = el("button", "clb-card__x", '<i class="ti ti-x"></i>');
    close.type = "button"; close.title = "Discard";
    close.addEventListener("click", closeCard);
    card.appendChild(close);
    return card;
  }

  // ---- posted thread card -------------------------------------------------
  function renderBody(comment) {
    let html = esc(comment.body || "");
    (comment.mentions || []).forEach((m) => {
      if (!m || !m.name) return;
      const token = "@" + esc(m.name);
      // Word boundary after the name so "@Ken" never highlights inside "@Kendra".
      const re = new RegExp(token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "(?![\\w-])", "g");
      html = html.replace(re,
        '<span class="clb-m' + (m.kind === "agent" ? " is-agent" : "") + '">' + token + "</span>");
    });
    return html;
  }

  function reactionRow(comment) {
    const row = el("div", "clb-reactions");
    Object.entries(comment.reactions || {}).forEach(([emoji, users]) => {
      const mine = users.indexOf(clb.viewer.id) >= 0;
      const chip = el("button", "clb-react" + (mine ? " is-on" : ""),
        esc(emoji) + " <span>" + users.length + "</span>");
      chip.type = "button";
      chip.title = users.map((u) => personName(u)).join(", ");
      chip.addEventListener("click", () => react(comment, emoji, !mine));
      row.appendChild(chip);
    });
    const add = el("button", "clb-react clb-react--add", '<i class="ti ti-mood-plus"></i>');
    add.type = "button"; add.title = "React";
    add.addEventListener("click", () => {
      let strip = row.querySelector(".clb-react-strip");
      if (strip) { strip.remove(); positionCard(); return; }
      strip = el("span", "clb-react-strip");
      REACTION_SET.forEach((emoji) => {
        const b = el("button", "clb-react-strip__e", esc(emoji));
        b.type = "button";
        b.addEventListener("click", () => {
          const users = (comment.reactions || {})[emoji] || [];
          react(comment, emoji, users.indexOf(clb.viewer.id) < 0);
        });
        strip.appendChild(b);
      });
      row.appendChild(strip);
      positionCard();
    });
    row.appendChild(add);
    return row;
  }

  async function react(comment, emoji, on) {
    const api = collabApi();
    if (!api) return;
    try {
      const res = await api.react(clb.sid, comment.comment_id, emoji, on);
      onEvent({ event_type: "comment.updated", payload: { thread_id: comment.thread_id, comment: res.comment } });
    } catch (err) {
      if (host().toast) host().toast("Reaction didn't stick — " + ((err && err.message) || "try again."));
    }
  }

  function commentKebab(t, comment) {
    const wrap = el("div", "clb-kebabwrap");
    const btn = el("button", "clb-ic", '<i class="ti ti-dots"></i>');
    btn.type = "button"; btn.title = "More";
    const menu = el("div", "clb-kebab");
    menu.hidden = true;
    const mine = comment.author_id === clb.viewer.id;
    const addRow = (icon, label, cls, fn) => {
      const row = el("button", "clb-kebab__row" + (cls ? " " + cls : ""),
        '<i class="ti ' + icon + '"></i> ' + esc(label));
      row.type = "button";
      row.addEventListener("click", () => { menu.hidden = true; fn(); });
      menu.appendChild(row);
    };
    addRow("ti-link", "Copy link", null, () => copyThreadLink(t));
    if (mine && !comment.deleted) {
      addRow("ti-pencil", "Edit", null, () => { clb.editing = comment.comment_id; paintCard(true); });
      menu.appendChild(el("div", "clb-kebab__sep"));
      addRow("ti-trash", "Delete", "is-danger", async () => {
        const api = collabApi();
        if (!api) return;
        try {
          const res = await api.remove(clb.sid, comment.comment_id);
          onEvent({ event_type: "comment.updated", payload: { thread_id: t.thread_id, comment: res.comment } });
        } catch (err) {
          if (host().toast) host().toast("Couldn't delete — " + ((err && err.message) || "try again."));
        }
      });
    }
    btn.addEventListener("click", () => { menu.hidden = !menu.hidden; positionCard(); });
    wrap.appendChild(btn);
    wrap.appendChild(menu);
    return wrap;
  }

  function commentRow(t, comment, first) {
    const row = el("div", "clb-cmt" + (comment.author_kind === "agent" ? " is-agent" : ""));
    const head = el("div", "clb-cmt__head");
    head.appendChild(avatar(comment.author_id, comment.author_name, comment.author_kind, "clb-av--sm"));
    head.appendChild(el("span", "clb-cmt__who",
      "<strong>" + esc(comment.author_name || personName(comment.author_id)) + "</strong>"
      + (comment.author_kind === "agent" ? ' <span class="clb-agentchip">AGENT</span>' : "")
      + ' <span class="clb-cmt__time">' + esc(ago(comment.created_at))
      + (comment.edited_at ? " · edited" : "") + "</span>"));
    head.appendChild(el("span", "clb-cmt__spacer"));
    if (first) {
      const resolve = el("button", "clb-ic", '<i class="ti ti-circle-check"></i>');
      resolve.type = "button"; resolve.title = "Resolve thread";
      resolve.dataset.clbAct = "comment";
      resolve.addEventListener("click", () => setStatus(t, "resolved"));
      head.appendChild(resolve);
    }
    head.appendChild(commentKebab(t, comment));
    row.appendChild(head);

    if (comment.deleted) {
      row.appendChild(el("div", "clb-cmt__body is-deleted", "This comment was deleted."));
      return row;
    }
    if (clb.editing === comment.comment_id) {
      const editKey = "edit:" + comment.comment_id;
      const editDraft = draftFor(editKey);
      const editBox = el("div", "clb-edit");
      const ta = document.createElement("textarea");
      ta.className = "clb-edit__text";
      // Draft-backed so an event-driven repaint mid-edit never reverts typing.
      ta.value = editDraft.text || comment.body;
      ta.addEventListener("input", () => { editDraft.text = ta.value; });
      editBox.appendChild(ta);
      const btns = el("div", "clb-edit__row");
      const cancel = el("button", "clb-btn ghost", "Cancel");
      cancel.type = "button";
      cancel.addEventListener("click", () => {
        delete clb.drafts[editKey];
        clb.editing = null;
        paintCard(true);
      });
      const save = el("button", "clb-btn primary", "Save");
      save.type = "button";
      save.addEventListener("click", async () => {
        const api = collabApi();
        if (!api || !ta.value.trim()) return;
        try {
          const res = await api.edit(clb.sid, comment.comment_id, ta.value.trim());
          delete clb.drafts[editKey];
          clb.editing = null;
          onEvent({ event_type: "comment.updated", payload: { thread_id: t.thread_id, comment: res.comment } });
        } catch (err) {
          if (host().toast) host().toast("Couldn't save — " + ((err && err.message) || "try again."));
        }
      });
      btns.appendChild(cancel); btns.appendChild(save);
      editBox.appendChild(btns);
      row.appendChild(editBox);
      requestAnimationFrame(() => ta.focus());
      return row;
    }
    row.appendChild(el("div", "clb-cmt__body", renderBody(comment)));
    (comment.attachments || []).forEach((att) => {
      if (att.kind === "audio" && att.url) {
        const chip = el("button", "clb-att is-playable",
          '<i class="ti ti-player-play"></i> ' + esc(att.name || "audio")
          + (att.duration_s ? ' <span class="clb-att__dur">· ' + esc(fmtAttDur(att.duration_s)) + "</span>" : ""));
        chip.type = "button";
        chip.addEventListener("click", () => playAttachment(att.url));
        row.appendChild(chip);
      } else if (att.kind === "image" && att.url) {
        const img = el("div", "clb-att-img");
        const im = document.createElement("img");
        im.src = (window.__edennMediaSrc ? window.__edennMediaSrc(att.url) : att.url); im.alt = att.name || "attachment";
        img.appendChild(im);
        row.appendChild(img);
      }
    });
    row.appendChild(reactionRow(comment));
    return row;
  }

  function buildThreadCard(t) {
    const card = el("div", "clb-card clb-card--thread");
    if (t.status === "resolved") {
      card.appendChild(el("div", "clb-resolved-line clb-resolved-line--banner",
        '<span class="clb-resolved-line__ic"><i class="ti ti-circle-check-filled"></i></span>'
        + "<span><strong>Resolved</strong> — replying reopens the thread.</span>"));
    }
    if (t.anchor_label || t.anchor_start_s != null) {
      card.appendChild(anchorChip({
        label: t.anchor_label || nodeLabel(t.anchor_node_id) || "",
        startS: t.anchor_start_s, endS: t.anchor_end_s,
      }, false));
    }
    const list = el("div", "clb-card__list");
    (t.comments || []).forEach((c, i) => list.appendChild(commentRow(t, c, i === 0)));
    card.appendChild(list);

    // Inherit — the collab bridge into iteration: continue from the take this
    // thread is about, without touching anyone's locked path.
    const anchorNode = clb.lastNodes[t.anchor_node_id];
    const cand = anchorNode && anchorNode.kind === "candidate" ? anchorNode.data : null;
    if (cand && cand.status === "completed" && (cand.audio_url || cand.video_url)) {
      const inherit = el("button", "clb-inherit-link",
        '<i class="ti ti-git-fork"></i> Inherit this take & iterate');
      inherit.type = "button";
      inherit.addEventListener("click", () => openInherit(cand));
      card.appendChild(inherit);
    }

    const replyComposer = buildComposer({
      draftKey: "reply:" + t.thread_id,
      placeholder: "Reply or @mention…",
      compact: true,
      // A plain reply in a thread the agent has spoken in ALSO dispatches a
      // pickup (conversational continuity), so it must confirm-spend too.
      threadHasAgent: (t.comments || []).some((c) => c.author_kind === "agent" && !c.deleted),
      onPost: async (payload) => {
        const api = collabApi();
        if (!api) return;
        const res = await api.reply(clb.sid, t.thread_id, payload);
        onEvent({ event_type: "comment.created", payload: { thread_id: t.thread_id, comment: res.comment } });
        if (res.thread) upsertThread(Object.assign({}, res.thread, { unread: 0 }));
        paintAll(true);
      },
    });
    // Spec 08: while a comment is being edited, only ONE field is live — the
    // reply composer dims out until the edit is saved or cancelled.
    if (clb.editing) replyComposer.classList.add("is-dimmed");
    card.appendChild(replyComposer);
    const close = el("button", "clb-card__x", '<i class="ti ti-x"></i>');
    close.type = "button"; close.title = "Close";
    close.addEventListener("click", closeCard);
    card.appendChild(close);
    return card;
  }

  function buildResolvedCard(t) {
    const card = el("div", "clb-card clb-card--resolved");
    const line = el("div", "clb-resolved-line");
    line.appendChild(el("span", "clb-resolved-line__ic", '<i class="ti ti-circle-check-filled"></i>'));
    line.appendChild(el("span", null,
      "<strong>Resolved by " + esc(personName(t.resolved_by || "")) + "</strong> · " + esc(ago(t.resolved_at))));
    card.appendChild(line);
    const first = liveComments(t)[0];
    if (first) card.appendChild(el("div", "clb-resolved-body", renderBody(first)));
    const row = el("div", "clb-resolved-row");
    const reopen = el("button", "clb-btn ghost", '<i class="ti ti-rotate-2"></i> Reopen');
    reopen.type = "button";
    reopen.addEventListener("click", () => setStatus(t, "open"));
    row.appendChild(reopen);
    const n = Math.max(0, liveComments(t).length - 1);
    if (n) {
      // Readable without reopening: expand shows the full card (still resolved;
      // replying there reopens per the lifecycle rule).
      const replies = el("button", "clb-resolved-replies", n + (n === 1 ? " reply" : " replies"));
      replies.type = "button";
      replies.title = "Read the thread";
      replies.addEventListener("click", () => { clb.expandResolved = t.thread_id; paintCard(true); });
      row.appendChild(replies);
    }
    card.appendChild(row);
    const close = el("button", "clb-card__x", '<i class="ti ti-x"></i>');
    close.type = "button"; close.title = "Close";
    close.addEventListener("click", closeCard);
    card.appendChild(close);
    return card;
  }

  async function setStatus(t, status) {
    const api = collabApi();
    if (!api) return;
    try {
      const res = await api.setStatus(clb.sid, t.thread_id, status);
      upsertThread(Object.assign({}, res.thread, { unread: 0 }));
      paintAll(true);
    } catch (err) {
      if (host().toast) host().toast("Couldn't update the thread — " + ((err && err.message) || "try again."));
    }
  }

  function copyThreadLink(t) {
    const u = new URL(sessionLink());
    u.searchParams.set("thread", t.thread_id);
    copyText(u.toString(), "Thread link copied.");
  }

  function maybeOpenDeepLink() {
    if (clb._deepLinked) return;
    const threadId = new URLSearchParams(window.location.search).get("thread");
    if (!threadId || !threadById(threadId)) return;
    clb._deepLinked = true;
    // Route through app.js's right-pane switcher so the Timeline/Canvas toggle
    // stays in sync — calling EdennCanvas.setView directly would show the stage
    // while the toggle (and the timeline module) still believe Timeline is up.
    if (host().setRightView) host().setRightView("canvas");
    else if (window.EdennCanvas && window.EdennCanvas.setView) window.EdennCanvas.setView("canvas");
    openThread(threadId);
  }

  // ========================================================================
  // Threads rail (collapsed 48px ⇄ expanded panel)
  // ========================================================================
  function railKey() {
    return JSON.stringify([
      clb.railOpen, clb.filter, clb.search, clb.openThreadId,
      clb.participants.map((p) => p.user_id + p.role),
      clb.threads.map((t) => [t.thread_id, t.status, t.unread,
        (t.comments || []).length, (liveComments(t).slice(-1)[0] || {}).body]),
    ]);
  }

  function paintRail() {
    if (!rail) return;
    const key = railKey();
    if (key === clb.railKey) return;
    clb.railKey = key;
    rail.innerHTML = "";
    rail.classList.toggle("is-collapsed", !clb.railOpen);
    if (clb.railOpen) paintRailExpanded();
    else paintRailCollapsed();
  }

  function paintRailCollapsed() {
    const open = el("button", "clb-rail__toggle", '<i class="ti ti-chevron-left"></i>');
    open.type = "button"; open.title = "Open threads";
    open.addEventListener("click", () => { clb.railOpen = true; paintRail(); });
    rail.appendChild(open);
    const unread = totalUnread();
    if (unread) {
      const badge = el("button", "clb-rail__badge", String(unread));
      badge.type = "button"; badge.title = unread + " unread";
      badge.addEventListener("click", () => { clb.railOpen = true; clb.filter = "unresolved"; paintRail(); });
      rail.appendChild(badge);
    }
    const faces = el("div", "clb-rail__faces");
    faces.appendChild(avatar(clb.viewer.id, clb.viewer.name, "user"));
    clb.participants.slice(0, 3).forEach((p) => faces.appendChild(avatar(p.user_id, p.display_name, "user")));
    clb.agents.slice(0, 1).forEach((a) => faces.appendChild(avatar(a.id, a.name, "agent")));
    rail.appendChild(faces);
  }

  function threadMatchesFilter(t) {
    const comments = liveComments(t);
    if (clb.filter === "unresolved" && t.status === "resolved") return false;
    if (clb.filter === "you") {
      const mentionsYou = comments.some((c) =>
        (c.mentions || []).some((m) => m.id === clb.viewer.id));
      if (!mentionsYou && t.created_by !== clb.viewer.id) return false;
    }
    if (clb.filter === "agents") {
      const agentish = comments.some((c) => c.author_kind === "agent"
        || (c.mentions || []).some((m) => m.kind === "agent"));
      if (!agentish) return false;
    }
    if (clb.search) {
      const q = clb.search.toLowerCase();
      const hit = comments.some((c) =>
        (c.body || "").toLowerCase().indexOf(q) >= 0
        || (c.author_name || personName(c.author_id)).toLowerCase().indexOf(q) >= 0)
        || (t.anchor_label || "").toLowerCase().indexOf(q) >= 0;
      if (!hit) return false;
    }
    return true;
  }

  function paintRailExpanded() {
    const hd = el("div", "clb-rail__hd");
    hd.appendChild(el("span", "clb-rail__title",
      "Threads <span class='clb-rail__count'>" + clb.threads.length + "</span>"));
    const collapse = el("button", "clb-ic", '<i class="ti ti-chevron-right"></i>');
    collapse.type = "button"; collapse.title = "Collapse";
    collapse.addEventListener("click", () => { clb.railOpen = false; paintRail(); });
    hd.appendChild(collapse);
    rail.appendChild(hd);

    const search = el("div", "clb-rail__search");
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "Search comments, people, agents…";
    input.value = clb.search;
    search.appendChild(el("i", "ti ti-search"));
    search.appendChild(input);
    rail.appendChild(search);

    const filters = el("div", "clb-rail__filters");
    [["all", "All"], ["unresolved", "Unresolved"], ["you", "@You"], ["agents", "Agents"]].forEach(([id, label]) => {
      const b = el("button", "clb-filter" + (clb.filter === id ? " is-on" : ""), esc(label));
      b.type = "button";
      b.addEventListener("click", () => { clb.filter = id; clb.railKey = null; paintRail(); });
      filters.appendChild(b);
    });
    rail.appendChild(filters);

    const list = el("div", "clb-rail__list");
    rail.appendChild(list);
    const paintList = () => {
      list.innerHTML = "";
      const rows = clb.threads.filter(threadMatchesFilter)
        .sort((a, b) => {
          if ((a.status === "resolved") !== (b.status === "resolved")) return a.status === "resolved" ? 1 : -1;
          return String(b.updated_at || "").localeCompare(String(a.updated_at || ""));
        });
      if (!rows.length) {
        list.appendChild(el("div", "clb-rail__empty",
          clb.threads.length
            ? "Nothing matches this filter."
            : "No threads yet. Press <strong>Comment</strong> and click a take to start one."));
        return;
      }
      rows.forEach((t) => {
        const comments = liveComments(t);
        const last = comments[comments.length - 1] || {};
        const row = el("button", "clb-rail__row" + (t.status === "resolved" ? " is-resolved" : "")
          + (clb.openThreadId === t.thread_id ? " is-open" : ""));
        row.type = "button";
        row.appendChild(avatar(last.author_id, last.author_name, last.author_kind, "clb-av--sm"));
        const txt = el("div", "clb-rail__rowtxt");
        txt.appendChild(el("div", "clb-rail__rowmeta",
          "<strong>" + esc(last.author_name || personName(last.author_id)) + "</strong>"
          + (last.author_kind === "agent" ? ' <span class="clb-agentchip">AGENT</span>' : "")
          + " · " + esc(ago(last.created_at))));
        txt.appendChild(el("div", "clb-rail__snippet", renderBody(last)));
        txt.appendChild(el("div", "clb-rail__where",
          '<i class="ti ti-map-pin"></i> ' + esc(t.anchor_label || nodeLabel(t.anchor_node_id) || "node")));
        row.appendChild(txt);
        if (t.status === "resolved") row.appendChild(el("span", "clb-rail__done", '<i class="ti ti-check"></i>'));
        else if (t.unread) row.appendChild(el("span", "clb-rail__unread", String(t.unread)));
        row.addEventListener("click", () => openThread(t.thread_id));
        list.appendChild(row);
      });
    };
    input.addEventListener("input", () => {
      clb.search = input.value;
      paintList();
      // Keep the memo key current so the next canvas render doesn't see a
      // "changed" rail and rebuild it out from under the focused input.
      clb.railKey = railKey();
    });
    paintList();
  }

  function paintAll(forceCard) {
    requestPinsRedraw();
    paintRail();
    paintCard(forceCard);
    paintFacepile();
    gateByRole();
  }

  // Role-honest affordances: viewers don't get comment controls, and only
  // iterate+ can direct the session from the composer. The server is the real
  // A role gate may only ever TAKE an affordance away. Setting `disabled`
  // outright re-enabled controls the view had deliberately turned off for its
  // own reasons — a direction already generated, a take already locked — so an
  // owner was handed back a "Generated" button that still dispatched, which is
  // a second paid render and (backend contract) replaces the takes they have.
  // Only what this gate disabled is what this gate restores.
  function gate(node, allowed, why) {
    if (!allowed) {
      node.disabled = true;
      node.dataset.clbGated = "1";
      if (why) node.title = why;
    } else if (node.dataset.clbGated) {
      delete node.dataset.clbGated;
      node.disabled = false;
      if (why && node.title === why) node.title = "";
    }
  }

  // gate (auth on); this keeps the UI from advertising actions that would 403.
  function gateByRole() {
    const role = viewerRole();
    const canComment = roleAtLeast(role, "comment");
    const canIterate = roleAtLeast(role, "iterate");
    if (modeBtn) modeBtn.hidden = !canComment;
    if (dockCommentBtn) dockCommentBtn.hidden = !canComment;
    // The SFX card's tools row renders before this module adopts the persona
    // on resume — re-toggle it here so owners aren't stuck stranger-gated.
    document.querySelectorAll(".sfx-card__tools").forEach((r) => { r.hidden = !canIterate; });
    // Controls that DIRECT the session — approving a direction, locking a take,
    // generating narration. The server refuses these below "iterate"
    // (_authorize_member in api/router.py), so leaving them live walks a viewer
    // into a spend dialog and then a failure. Gate them where they render.
    const why = "You have " + (ROLE_LABELS[role] || role).toLowerCase()
      + " access — ask the owner to make changes.";
    document.querySelectorAll('.take__use, .dt-use, .vo__gen, .sfx-plan__go, [data-clb-act="iterate"]')
      .forEach((n) => gate(n, canIterate, why));
    // Commenting is its own, lower bar: a "view" role gets neither.
    document.querySelectorAll('.clb-send, .clb-react, [data-clb-act="comment"]')
      .forEach((n) => gate(n, canComment, why));
    // Inheriting a take opens a dialog that GENERATES — an iterate action that
    // happens to live inside a comment thread.
    document.querySelectorAll(".clb-inherit-link").forEach((n) => {
      gate(n, canIterate, why);
    });
    document.querySelectorAll(".clb-reply, .clb-composer textarea, .clb-composer input")
      .forEach((n) => gate(n, canComment, ""));

    const input = document.getElementById("session-input");
    const send = document.getElementById("session-send");
    // Only gate a live, collab-loaded session (the app enables these on open).
    if (input && send && clb.loaded) {
      input.disabled = !canIterate;
      send.disabled = !canIterate;
      input.placeholder = canIterate
        ? "Give direction or ask anything…"
        : "You have " + (ROLE_LABELS[role] || role).toLowerCase() +
          " access — comment on the canvas instead.";
    }
  }

  // ========================================================================
  // Inherit (continue a collaborator's branch through the typed contract)
  // ========================================================================
  function buildInheritDialog() {
    inheritOverlay = el("div", "overlay");
    inheritOverlay.hidden = true;
    const card = el("div", "confirm-card clb-inherit-card");
    card.setAttribute("role", "alertdialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-labelledby", "clb-inherit-title");
    inheritOverlay.appendChild(card);
    inheritOverlay.addEventListener("click", (e) => { if (e.target === inheritOverlay) hideOverlay(inheritOverlay); });
    trapTab(inheritOverlay);
    document.body.appendChild(inheritOverlay);
  }

  function openInherit(c) {
    if (!inheritOverlay) return;
    const st = (clb.lastSnap && clb.lastSnap.state) || {};
    const prop = (st.proposals || []).find((p) => p.proposal_id === c.proposal_id) || {};
    const model = host().modelText
      ? host().modelText(c)
      : ((host().MODEL_LABEL || {})[c.modelspec] || c.modelspec || "");
    const card = inheritOverlay.firstChild;
    card.innerHTML = "";
    card.appendChild(el("h3", null, '<span id="clb-inherit-title">Inherit this take</span>'));
    const list = el("ul", "clb-inherit__list");
    [["ti-bulb", "Direction & prompt", prop.title || "carried over"],
     ["ti-wand", "Model", model || "carried over"],
     ["ti-volume", "Music level", (typeof c.music_volume === "number" ? Math.round(c.music_volume * 100) + "%" : "carried over")],
    ].forEach(([ic, k, v]) => {
      list.appendChild(el("li", null, '<i class="ti ' + ic + '"></i><span>' + esc(k) + "</span><em>" + esc(v) + "</em>"));
    });
    card.appendChild(list);
    card.appendChild(el("p", null,
      "Your new take branches from “" + esc(c.title || "this take") +
      "” — the locked path stays untouched. It spends to generate."));
    const row = el("div", "confirm-card__row");
    const cancel = el("button", "btn-ghost", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", () => hideOverlay(inheritOverlay));
    const ok = el("button", "btn-primary", '<i class="ti ti-git-fork"></i> Inherit & iterate');
    ok.type = "button";
    ok.addEventListener("click", () => {
      hideOverlay(inheritOverlay);
      const cn = conn();
      if (!cn) return;
      const existing = new Set(((clb.lastSnap && clb.lastSnap.state && clb.lastSnap.state.candidates) || [])
        .filter((x) => x.parent_candidate_id === c.candidate_id).map((x) => x.candidate_id));
      clb.pendingInherit = { parent: c.candidate_id, existing };
      const h = host();
      if (h.startThinking) h.startThinking();
      cn.choose({ choice_type: "variation", target_id: c.candidate_id, payload: { inherited: true } });
    });
    row.appendChild(cancel); row.appendChild(ok);
    card.appendChild(row);
    showOverlay(inheritOverlay);
    ok.focus();
  }

  function resolveInherit() {
    const p = clb.pendingInherit;
    if (!p) return;
    const st = (clb.lastSnap && clb.lastSnap.state) || {};
    const child = (st.candidates || []).find(
      (c) => c.parent_candidate_id === p.parent && !p.existing.has(c.candidate_id)
    );
    if (!child) return;
    clb.inherited[child.candidate_id] = true;
    clb.pendingInherit = null;
    if (window.EdennCanvas && window.EdennCanvas.focusNode) window.EdennCanvas.focusNode(child.candidate_id);
  }

  // ========================================================================
  // Share dialog + facepile (participants-backed)
  // ========================================================================
  function paintFacepile() {
    const faces = document.getElementById("clb-faces");
    if (!faces) return;
    faces.innerHTML = "";
    faces.appendChild(avatar(clb.viewer.id, clb.viewer.name, "user"));
    clb.participants.slice(0, 3).forEach((p) => faces.appendChild(avatar(p.user_id, p.display_name, "user")));
    const extra = clb.participants.length - 3;
    if (extra > 0) faces.appendChild(el("span", "clb-av more", "+" + extra));
  }

  function buildShareDialog() {
    shareOverlay = el("div", "overlay");
    shareOverlay.hidden = true;
    const card = el("div", "confirm-card clb-share-card");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-labelledby", "clb-share-title");
    shareOverlay.appendChild(card);
    shareOverlay.addEventListener("click", (e) => { if (e.target === shareOverlay) hideOverlay(shareOverlay); });
    trapTab(shareOverlay);
    document.body.appendChild(shareOverlay);
  }

  function sessionLink(joinRole) {
    // Built from scratch — NEVER from location.href — so credential params
    // (?token= rides as the Bearer auth on the real backend) can't leak into a
    // link handed to someone else. Only the transport selectors carry over.
    const u = new URL(window.location.pathname, window.location.origin);
    const cur = new URLSearchParams(window.location.search);
    ["backend", "api"].forEach((k) => { const v = cur.get(k); if (v) u.searchParams.set(k, v); });
    const sid = (host().app && host().app.sessionId) || clb.sid;
    if (sid) u.searchParams.set("session", sid);
    // The invite role rides the link: the recipient's first open shows a join
    // card and registers them as a participant with this role.
    if (joinRole) u.searchParams.set("join", joinRole);
    return u.toString();
  }

  function currentResult() {
    const snap = (host().app && host().app.snapshot) || clb.lastSnap;
    const st = (snap && snap.state) || {};
    const fin = st.final_artifact || {};
    const sel = (st.candidates || []).find((c) => c.candidate_id === st.selected_candidate_id);
    const done = (st.candidates || []).filter((c) => c.status === "completed" && (c.audio_url || c.video_url));
    const cand = sel || done[done.length - 1] || null;
    const url = fin.video_url || fin.audio_url || (cand && (cand.video_url || cand.audio_url)) || null;
    if (!url) return null;
    const locked = !!sel || !!st.final_artifact;
    return {
      url,
      // Titles are backend/model-derived — escaped on BOTH branches (innerHTML sink).
      title: locked ? "Locked mix — " + esc((sel || cand || {}).title || "final")
        : esc((cand && cand.title) || "Latest take"),
      locked,
      id: (fin.candidate_id || (cand && cand.candidate_id)) || null,
    };
  }

  function copyText(text, doneMsg) {
    const h = host();
    const done = () => { if (h.toast) h.toast(doneMsg || "Link copied."); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
    } else fallbackCopy(text, done);
  }
  function fallbackCopy(text, done) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.cssText = "position:fixed;left:-9999px;top:0;";
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) {}
    ta.remove();
    if (ok) { done(); return; }
    // Both paths failed, and the dialog no longer keeps a URL field standing
    // by — so show one now. A permanent field to serve a rare failure was the
    // wrong trade; appearing exactly when copying breaks is the right one.
    revealLink(text);
  }

  /** Last-resort: put the URL on screen, selected, so it can be copied by hand. */
  function revealLink(text) {
    if (host().toast) host().toast("Couldn't copy — select the link and copy it.");
    const card = shareOverlay && !shareOverlay.hidden && shareOverlay.firstChild;
    if (!card) return;
    let row = card.querySelector(".clb-share__fallback");
    if (!row) {
      row = el("div", "clb-share__fallback");
      const inp = document.createElement("input");
      inp.type = "text"; inp.readOnly = true;
      inp.setAttribute("aria-label", "Link — select and copy");
      inp.addEventListener("focus", () => inp.select());
      row.appendChild(inp);
      const foot = card.querySelector(".clb-share__foot");
      card.insertBefore(row, foot);
    }
    const inp = row.firstChild;
    inp.value = text;
    inp.focus(); inp.select();
  }

  const ROLE_LABELS = { view: "Can view", comment: "Can comment", iterate: "Can iterate" };

  function openShare(opts) {
    // Assigned only when the viewer may manage sharing (see below); read after
    // the sheet is built, so it has to live in the function's scope.
    let inviteInput = null;
    if (!shareOverlay) return;
    const appSid = host().app && host().app.sessionId;
    if (appSid && appSid !== clb.sid) { resetFor(appSid); refresh(); }
    const card = shareOverlay.firstChild;
    const myRole = viewerRole();
    const canManage = myRole === "owner";
    card.innerHTML = "";
    card.appendChild(el("h3", null, '<span id="clb-share-title">Share</span>'));

    // The link IS the invite: it carries the chosen role, and the recipient's
    // first open shows a join card under their own name. The raw URL field the
    // dialog used to lead with is gone — a read-only box holding a long string
    // was a developer artifact, and the only thing anyone did with it was copy
    // it. The link now lives on the footer button, next to the role it grants.
    let linkValue = sessionLink(canManage ? "comment" : null);
    // Live backend: mint a SIGNED grant so the link survives real auth (the
    // recipient redeems it as themselves). Mock / mint failure: plain ?join=.
    const setLink = (role) => {
      const plain = sessionLink(canManage ? role : null);
      linkValue = plain;
      const api = collabApi();
      if (isMock() || !canManage || !api || !api.mintLink || !clb.sid) return;
      api.mintLink(clb.sid, role).then((res) => {
        const u = new URL(plain);
        if (res && res.grant) u.searchParams.set("grant", res.grant);
        linkValue = u.toString();
      }).catch(() => { linkValue = plain; });
    };

    // Invite row. Leads the dialog, the way it does in Notion and Linear: the
    // reason this gets opened is almost always to add somebody, and the link is
    // the fallback rather than the headline. Owner-only — the upsert behind it
    // is a REAL participants write, by the collaborator's stable user id.
    let inviteBtn = null;
    if (canManage) {
      const invRow = el("div", "clb-share__invite");
      const email = document.createElement("input");
      email.type = "text"; email.placeholder = "Add by user id";
      email.setAttribute("aria-label", "Invite");
      // The sheet re-renders after a successful invite and asks to refocus this
      // field — from OUTSIDE this block, where `email` does not exist. That
      // ReferenceError was caught by the share handler and reported to the user
      // as "Couldn't share", on an invite that had in fact succeeded.
      inviteInput = email;
      const role = document.createElement("select");
      role.className = "clb-sel";
      Object.entries(ROLE_LABELS).forEach(([v, l]) => {
        const o = document.createElement("option"); o.value = v; o.textContent = l; role.appendChild(o);
      });
      role.value = "comment";
      const inv = el("button", "btn-primary", "Invite");
      inv.type = "button";
      inv.disabled = true; // nothing typed yet — a button that silently no-ops is worse than a disabled one
      inviteBtn = inv;
      email.addEventListener("input", () => {
        inv.disabled = !email.value.trim();
        email.classList.remove("is-bad");
      });
      const submitInvite = async () => {
        const v = email.value.trim();
        if (!v) return;
        const api = collabApi();
        if (!api) return;
        try {
          await api.addParticipant(clb.sid, { user_id: v, role: role.value, display_name: v });
          const i = clb.participants.findIndex((x) => x.user_id === v);
          const p = { user_id: v, display_name: v, role: role.value };
          if (i >= 0) clb.participants[i] = Object.assign({}, clb.participants[i], p);
          else clb.participants.push(p);
          email.value = "";
          if (host().toast) host().toast("Shared with " + v + " (" + ROLE_LABELS[role.value].toLowerCase() + ").");
          paintFacepile();
          openShare({ refocus: "invite" });
        } catch (err) {
          // Nothing usable: no repaint, so the field keeps what was typed and
          // the caret stays where the user left it.
          email.classList.add("is-bad");
          email.focus();
          if (host().toast) host().toast("Couldn't share — " + ((err && err.message) || "try again."));
        }
      };
      inv.addEventListener("click", submitInvite);
      email.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && email.value.trim()) { e.preventDefault(); submitInvite(); }
      });
      invRow.appendChild(email); invRow.appendChild(role); invRow.appendChild(inv);
      card.appendChild(invRow);
    }

    const people = el("div", "clb-share__people");
    const meRow = el("div", "clb-person");
    meRow.appendChild(avatar(clb.viewer.id, clb.viewer.name, "user", "clb-av--sm"));
    meRow.appendChild(el("span", "clb-person__nm", esc(clb.viewer.name) + " <span class='clb-person__you'>(you)</span>"));
    meRow.appendChild(el("span", "clb-person__role",
      myRole === "owner" ? "Owner" : (ROLE_LABELS[myRole] || myRole)));
    people.appendChild(meRow);
    clb.participants.filter((p) => p.user_id !== clb.viewer.id).forEach((p) => {
      const row = el("div", "clb-person");
      row.appendChild(avatar(p.user_id, p.display_name, "user", "clb-av--sm"));
      row.appendChild(el("span", "clb-person__nm", esc(p.display_name || p.user_id)));
      if (!canManage) {
        // Non-owners see the roster, not the controls.
        row.appendChild(el("span", "clb-person__role", ROLE_LABELS[p.role] || p.role));
        people.appendChild(row);
        return;
      }
      const sel = document.createElement("select");
      sel.className = "clb-person__sel";
      sel.setAttribute("aria-label", "Role for " + (p.display_name || p.user_id));
      Object.entries(ROLE_LABELS).forEach(([v, l]) => {
        const o = document.createElement("option"); o.value = v; o.textContent = l;
        if (v === p.role) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener("change", async () => {
        const api = collabApi();
        if (!api) return;
        try {
          await api.addParticipant(clb.sid, { user_id: p.user_id, role: sel.value });
          p.role = sel.value;
          if (host().toast) host().toast((p.display_name || p.user_id) + " → " + ROLE_LABELS[sel.value].toLowerCase() + ".");
        } catch (err) {
          sel.value = p.role;
          if (host().toast) host().toast("Couldn't change the role — " + ((err && err.message) || "try again."));
        }
      });
      row.appendChild(sel);
      people.appendChild(row);
    });
    card.appendChild(people);

    // The result, when there is one. No section heading and no empty state: an
    // absent take is not news, and a heading over one row was more chrome than
    // content. It simply isn't here until there is something to share.
    const res = currentResult();
    if (res) {
      const row = el("div", "clb-share__resrow");
      row.appendChild(el("span", "clb-share__resic", '<i class="ti ti-player-play"></i>'));
      row.appendChild(el("span", "clb-share__restitle", res.title));
      const copyRes = el("button", "clb-iconbtn", '<i class="ti ti-link"></i>');
      copyRes.type = "button";
      copyRes.title = "Copy link to this take";
      copyRes.setAttribute("aria-label", "Copy link to this take");
      copyRes.addEventListener("click", () => {
        const u = new URL(sessionLink());
        u.searchParams.set("share", "result");
        if (res.id) u.searchParams.set("take", res.id);
        copyText(u.toString(), "Result link copied.");
      });
      const dl = el("button", "clb-iconbtn", '<i class="ti ti-download"></i>');
      dl.type = "button";
      dl.title = "Download";
      dl.setAttribute("aria-label", "Download");
      dl.addEventListener("click", () => (host().deliverFile
        ? host().deliverFile(res.url, "edenn-final-mix.mp4")
        : window.open(res.url, "_blank")));
      row.appendChild(copyRes); row.appendChild(dl);
      card.appendChild(row);
    }

    // Footer: what the link grants on the left, Copy link on the right — the
    // shape Vercel, Notion and Figma have converged on. "Anyone with the link"
    // without a verb doesn't say whether they can edit, so the role rides next
    // to it — and here it is the REAL role the grant is minted for.
    const foot = el("div", "clb-share__foot");
    const scopeWrap = el("div", "clb-share__scope");
    let linkRole = null;
    if (canManage) {
      linkRole = document.createElement("select");
      linkRole.className = "clb-sel";
      Object.entries(ROLE_LABELS).forEach(([v, l]) => {
        const o = document.createElement("option"); o.value = v; o.textContent = l.toLowerCase(); linkRole.appendChild(o);
      });
      linkRole.value = "comment";
      linkRole.setAttribute("aria-label", "What link visitors can do");
      linkRole.addEventListener("change", () => setLink(linkRole.value));
      scopeWrap.appendChild(el("span", "clb-share__scopelbl", "Anyone with the link"));
      scopeWrap.appendChild(linkRole);
      setLink(linkRole.value);
    } else {
      scopeWrap.appendChild(el("span", "clb-share__scopelbl",
        "You have " + (ROLE_LABELS[myRole] || myRole).toLowerCase() + " access — only the owner manages sharing."));
    }
    const copy = el("button", "btn-primary", '<i class="ti ti-link"></i> Copy link');
    copy.type = "button";
    copy.addEventListener("click", () => {
      // Say what the copied link will actually do for the person who gets it,
      // rather than a bare "copied" that leaves the granted role ambiguous.
      copyText(linkValue, isMock()
        ? "Link copied — offline demo, so it can't reopen a mock session in another tab."
        : (canManage && linkRole
          ? "Link copied — anyone opening it joins as " + ROLE_LABELS[linkRole.value].toLowerCase().replace(/^can /, "") + "."
          : "Session link copied."));
    });
    foot.appendChild(scopeWrap); foot.appendChild(copy);
    card.appendChild(foot);

    if (opts && opts.keep && inviteInput) {
      inviteInput.value = opts.keep;
      inviteInput.classList.add("is-bad");
      if (inviteBtn) inviteBtn.disabled = false;
    }

    if (shareOverlay.hidden) showOverlay(shareOverlay);
    // A repaint after Invite keeps the user in the invite row; a fresh open
    // lands there too — it is what the dialog is for. Esc and a backdrop click
    // close it, so there is no Done button to land on.
    if (inviteInput) inviteInput.focus();
    else copy.focus();
  }
  // ========================================================================
  // expose + boot
  // ========================================================================
  window.EdennCollab = { onCanvasRender, onFocusChange, onEvent, gateByRole, viewerRole };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
