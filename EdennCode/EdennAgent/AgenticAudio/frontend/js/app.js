/* ============================================================================
 * Edenn — agentic audio console controller (Stage 1).
 *
 * Renders the session from the snapshot (source of truth) and animates live
 * `agent.reasoning` beats during a turn. Card taps -> /choices, free text ->
 * /messages — exactly the documented contract.
 *
 * Transport is pluggable: MockTransport (default, in-browser) or RealTransport
 * (WS + REST against the live router). Switch with `?backend=real`.
 * ========================================================================== */
(function () {
  // Same-origin media (<video>/<audio>/poster/downloads) cannot send an
  // Authorization header, so the real transport publishes a tokenizing helper
  // on window; render code calls this delegate. Identity when tokenless (mock
  // console, open deployments) — URLs pass through untouched.
  const mediaSrc = (u) => (window.__edennMediaSrc ? window.__edennMediaSrc(u) : u);
  "use strict";

  // ---- phase machine (mirrors AgenticAudioSessionPhase) -------------------
  // The topbar phase STRIP is gone (the chat pane already narrates every phase
  // as it happens), but the table stays: the entrance's session cards still
  // translate a phase id into a human label with it.
  const PHASES = [
    ["created", "Start"],
    ["observing", "Observe"],
    ["proposing", "Direction"],
    ["awaiting_plan_choice", "Plan"],
    ["generating_candidates", "Generate"],
    ["awaiting_candidate_choice", "Review"],
    ["composing", "Compose"],
    ["completed", "Done"],
  ];

  //: The backend's own limits, so the message a customer sees here is the
  //: message they would have got after the upload — not a second opinion.
  const UPLOAD_MAX_BYTES = 300 * 1024 * 1024;
  const UPLOAD_MAX_SECONDS = 150;
  const UPLOAD_MIN_SECONDS = 15;

  async function checkUploadable(file) {
    if (!file) return "Choose a video first.";
    if (file.size > UPLOAD_MAX_BYTES) {
      return "That video is " + (file.size / 1024 / 1024).toFixed(0) +
        "MB. The limit is 300MB — try a shorter clip or a smaller export.";
    }
    // Duration needs the browser to read the file, which it can do without
    // uploading it. A format it cannot read is not rejected here: that is the
    // backend's call, and guessing would refuse files that work.
    const seconds = await new Promise((resolve) => {
      let done = false;
      const probe = document.createElement("video");
      const finish = (value) => { if (!done) { done = true; URL.revokeObjectURL(probe.src); resolve(value); } };
      probe.preload = "metadata";
      probe.onloadedmetadata = () => finish(probe.duration);
      probe.onerror = () => finish(null);
      setTimeout(() => finish(null), 5000);
      probe.src = URL.createObjectURL(file);
    });
    if (seconds == null || !isFinite(seconds)) return "";
    if (seconds > UPLOAD_MAX_SECONDS) {
      return "That video is " + Math.round(seconds) + "s long. The limit is " +
        UPLOAD_MAX_SECONDS + "s — trim it and try again.";
    }
    if (seconds < UPLOAD_MIN_SECONDS) {
      return "That video is " + Math.round(seconds) + "s long. It needs to be " +
        "longer than " + UPLOAD_MIN_SECONDS + "s to score.";
    }
    return "";
  }

  function xhrUpload(url, body, headers, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url, true);
      Object.entries(headers || {}).forEach(([k, v]) => xhr.setRequestHeader(k, v));
      xhr.upload.onprogress = (e) => {
        if (onProgress && e.lengthComputable) onProgress(e.loaded / e.total, () => xhr.abort());
      };
      xhr.onload = () => resolve({
        ok: xhr.status >= 200 && xhr.status < 300,
        status: xhr.status,
        text: async () => xhr.responseText,
        json: async () => JSON.parse(xhr.responseText || "{}"),
      });
      xhr.onerror = () => reject(new Error("The upload did not complete."));
      xhr.onabort = () => {
        const err = new Error("Upload cancelled.");
        err.aborted = true;
        reject(err);
      };
      xhr.send(body);
    });
  }


  // ---- tiny DOM utils -----------------------------------------------------
  const $ = (id) => document.getElementById(id);
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
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 3200);
  }
  function fmtDur(s) {
    return `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
  }
  /**
   * A cue time, to the tenth — 0:03.4.
   *
   * Whole seconds are fine for a duration and wrong for a CUE: an effect
   * spotted at 3.4s printed as "0:03" reads as landing 400ms before the cut it
   * was placed on, and disagrees with the same instant on the timeline lane.
   * Rounds total tenths first so 3.96 carries to 0:04, never "0:03.10".
   */
  function fmtCue(sec) {
    const d = Math.round(Math.max(0, Number(sec) || 0) * 10);
    const tenths = d % 10;
    return fmtDur((d - tenths) / 10) + (tenths ? "." + tenths : "");
  }
  function fmtAgo(iso) {
    if (!iso) return "";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just now";
    const m = Math.floor(s / 60);
    if (m < 60) return m + "m ago";
    const h = Math.floor(m / 60);
    if (h < 24) return h + "h ago";
    return Math.floor(h / 24) + "d ago";
  }

  // ========================================================================
  // Viewer persona — a lightweight local identity so collaborators are
  // distinguishable people. Stored per-origin; the name is editable from the
  // account chip / Settings, and the id stays stable across renames so
  // comment authorship survives. When auth is ON the server resolves the
  // bearer principal instead and these fields are display-only (auth.py calls
  // the principal "the hook for a real identity provider later").
  // ========================================================================
  const PERSONA_KEY = "edenn.persona";
  function personaFromName(name) {
    const clean = String(name || "").trim() || "Guest";
    const slug = clean.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 24) || "guest";
    // The suffix keeps two "Maya"s on different machines from colliding.
    const suffix = Math.random().toString(36).slice(2, 6);
    return { id: slug + "-" + suffix, name: clean };
  }
  /**
   * Who the console believes you are.
   *
   * When the backend knows (auth on, a verified session), the SERVER is the
   * source of truth and this returns what /me said. The invented guest below is
   * for a local run with auth off — it is a placeholder for a person, not an
   * identity, and it was never meant to survive contact with real accounts.
   */
  function persona() {
    if (app.me && app.me.authenticated) {
      return { id: app.me.user_id, name: app.me.display_name || app.me.user_id };
    }
    if (app.persona) return app.persona;
    try {
      const raw = localStorage.getItem(PERSONA_KEY);
      if (raw) {
        const p = JSON.parse(raw);
        if (p && p.id && p.name) { app.persona = p; return p; }
      }
    } catch (_) { /* storage unavailable → a per-page guest is still a person */ }
    app.persona = personaFromName("Guest");
    try { localStorage.setItem(PERSONA_KEY, JSON.stringify(app.persona)); } catch (_) {}
    return app.persona;
  }
  function renamePersona(name) {
    const clean = String(name || "").trim();
    if (!clean) return persona();
    // Signed in: the name belongs to the account, so it is written to the
    // server. Otherwise it is a local label on a local guest.
    if (app.me && app.me.authenticated) {
      app.me = Object.assign({}, app.me, { display_name: clean });
      paintPersona();
      if (app.transport && app.transport.setProfile) {
        app.transport.setProfile(clean).catch(() => {
          toast("Couldn't save your name — it will show locally until you retry.");
        });
      }
      return persona();
    }
    const p = persona();
    app.persona = { id: p.id, name: clean }; // id is identity; only the label moves
    try { localStorage.setItem(PERSONA_KEY, JSON.stringify(app.persona)); } catch (_) {}
    paintPersona();
    return app.persona;
  }
  function paintPersona() {
    const p = persona();
    const av = document.querySelector(".account__avatar");
    const nm = document.querySelector(".account__name");
    if (av) av.textContent = (p.name || "?").charAt(0).toUpperCase();
    if (nm) nm.textContent = p.name;
  }

  // ========================================================================
  // Transports
  // ========================================================================
  function MockTransport() {
    const backend = new window.MockBackend({ delay: 320 });
    // Lazy: the persona can be renamed mid-page (Settings), and every later
    // write should carry the current name.
    const me = () => persona();
    // The in-browser backend has no title until the analysis lands, so the demo
    // sessions keep theirs here — enough for the entrance list to read like a
    // history instead of a column of ids.
    const titles = new Map();
    function create(req) {
      const result = backend.createSession(req);
      titles.set(result.session_id, req.initial_message || "Untitled session");
      return result;
    }
    try {
      ["Summer in motion", "A quieter morning", "The next chapter"]
        .forEach((title) => create({ initial_message: title }));
    } catch (_) { /* a mock that cannot seed still runs every live flow */ }
    return {
      kind: "mock",
      async createSession(req) { return create(req); },
      connect(id, handlers) { return backend.connect(id, handlers); },
      async getSnapshot(id) { return backend.getSnapshot(id); },
      async uploadVideo(file, onProgress) {
        // Same signature and the same prechecks as the real transport: a limit
        // the offline console does not enforce is a limit the customer meets
        // for the first time in production.
        const refusal = await checkUploadable(file);
        if (refusal) throw new Error(refusal);
        if (onProgress) onProgress(1, () => {});
        return { artifact_id: "artifact_mock_upload", metadata: { filename: file && file.name, duration: 15 } };
      },
      // The in-browser mock is per-page: list THIS page's sessions so the
      // entrance Sessions view demos honestly (they die with the tab).
      async listSessions() {
        const sessions = backend.listSessions();
        // Until the analysis names a session, show the words it started from.
        sessions.forEach((s) => { if (!s.title) s.title = titles.get(s.session_id) || null; });
        return { sessions };
      },
      // The mock has no accounts: it answers honestly rather than pretending,
      // so the console takes the same path it would with auth off.
      async whoami() { return { authenticated: false }; },
      async setProfile() { return { display_name: "" }; },
      async authConfig() { return { auth_required: false, configured: false }; },
      // Collab contract — identical shapes to the real /collab endpoints.
      collab: {
        get: async (sid) => backend.getCollab(sid, me().id),
        createThread: async (sid, req) =>
          backend.createThread(sid, Object.assign({ author_id: me().id, author_name: me().name }, req)),
        reply: async (sid, tid, req) =>
          backend.addThreadComment(sid, tid, Object.assign({ author_id: me().id, author_name: me().name }, req)),
        setStatus: async (sid, tid, status) => backend.setThreadStatus(sid, tid, status, me().id),
        markRead: async (sid, tid) => backend.markThreadRead(sid, tid, me().id),
        edit: async (sid, cid, body) => backend.editComment(sid, cid, body),
        remove: async (sid, cid) => backend.deleteComment(sid, cid),
        react: async (sid, cid, emoji, on) => backend.reactComment(sid, cid, emoji, on, me().id),
        addParticipant: async (sid, req) => backend.upsertParticipant(sid, req),
      },
    };
  }

  function RealTransport(apiBase) {
    const base = (apiBase || window.location.origin).replace(/\/$/, "");
    /**
     * fetch() that keeps the connection pill honest.
     *
     * A network failure, or a "method not supported" from a host that is really
     * just a static file server, means the live backend is not there — the pill
     * has to stop claiming otherwise. A 404 is deliberately NOT treated as down:
     * asking for a session that no longer exists is a normal answer from a
     * perfectly healthy backend.
     */
    const rfetch = async (url, init) => {
      let r;
      try {
        r = await fetch(url, init);
      } catch (err) {
        setBackendHealth(false, "The backend did not respond.");
        throw err;
      }
      if (r.status === 405 || r.status === 501) {
        setBackendHealth(false, "This page's host has no live API.");
      } else {
        setBackendHealth(true);
      }
      return r;
    };
    const PREFIX = "/api/v2/agentic/audio";
    // The token arrives on the page URL because a browser cannot put an
    // Authorization header on a WebSocket handshake or a <video> src. Those
    // are real constraints and the query-param trust model below stands.
    //
    // What does NOT need to persist is the token sitting in the address bar
    // for the rest of the session: it lands in browser history, in a
    // screenshot, and in the link a customer copies to send someone — which
    // hands over their credential along with the page. So it is read once into
    // memory and removed from the visible URL; everything downstream uses the
    // in-memory copy.
    if (!("__edennToken" in window)) {
      const fromUrl = new URLSearchParams(window.location.search);
      const supplied = fromUrl.get("token") || "";
      window.__edennToken = supplied;
      if (supplied) {
        fromUrl.delete("token");
        const query = fromUrl.toString();
        window.history.replaceState(
          null, "",
          window.location.pathname + (query ? "?" + query : "") + window.location.hash
        );
      }
    }
    // Optional bearer token (from ?token= on the page URL) so the console works
    // when the backend has auth enabled. Browsers can't set WS headers, so the
    // same token is also passed as a ?token= query param on the socket.
    const token = window.__edennToken || "";
    const authHeaders = (h) =>
      token ? Object.assign({}, h || {}, { Authorization: `Bearer ${token}` }) : (h || {});
    const wsQuery = token ? `?token=${encodeURIComponent(token)}` : "";
    // Media elements (<video>/<audio>/poster) cannot send an Authorization
    // header, so same-origin media paths carry the token as a query param —
    // the same trust the WebSocket already uses. Without this, an
    // authenticated deployment serves the console but every player 401s.
    const mediaSrc = (url) => {
      if (!url || !token) return url;
      if (!(url.startsWith("/dev/media/") || url.startsWith("/dev/uploads/"))) return url;
      return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token);
    };
    window.__edennMediaSrc = mediaSrc;
    async function requestJSON(method, path, body) {
      const r = await rfetch(base + path, {
        method,
        headers: authHeaders(body !== undefined ? { "Content-Type": "application/json" } : {}),
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
      if (!r.ok) {
        // Prefer the API's crafted detail string over raw JSON so REST-fallback
        // toasts read like the WS error path instead of `400 {"detail":...}`.
        const text = await r.text();
        let msg = `${r.status} ${text}`;
        try {
          const parsed = JSON.parse(text);
          if (parsed && typeof parsed.detail === "string") msg = parsed.detail;
          else if (parsed && typeof parsed.message === "string") msg = parsed.message;
        } catch (_) { /* not JSON — keep the raw text */ }
        const err = new Error(msg);
        err.status = r.status; // 401 → the sign-in prompt, not a dead toast
        throw err;
      }
      return r.json();
    }
    const postJSON = (path, body) => requestJSON("POST", path, body);
    return {
      kind: "real",
      // The dev server seeds a demo source video and advertises its id at
      // /dev/info; discover it so create-session doesn't 404 on a stale id.
      async getDemoArtifact() {
        try {
          const r = await fetch(`${base}/dev/info`, { headers: authHeaders() });
          if (r.ok) return (await r.json()).source_artifact || null;
        } catch (_) {}
        return null;
      },
      async uploadVideo(file, onProgress) {
        // Check what we can check HERE, before spending the customer's time.
        // The limits are the backend's own; sending a file that cannot be
        // accepted means a multi-minute upload that ends in a rejection, and
        // on a phone it means their data too.
        const tooBig = await checkUploadable(file);
        if (tooBig) throw new Error(tooBig);

        const fd = new FormData();
        fd.append("video", file);
        // XHR rather than fetch: it is the only way to report progress and to
        // let the customer abort. A blind multi-minute wait with no way out is
        // the first thing this product asks of someone.
        let r;
        try {
          r = await xhrUpload(`${base}/api/v2/assets/video`, fd, authHeaders(), onProgress);
        } catch (err) {
          if (err && err.aborted) throw err;
          setBackendHealth(false, "The backend did not respond.");
          throw err;
        }
        // A 404/405/501 here is not a bad file — it is a host with no API.
        if ([404, 405, 501].indexOf(r.status) >= 0) {
          setBackendHealth(false, "This page's host has no live API.");
        } else {
          setBackendHealth(true);
        }
        if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
        return r.json();
      },
      async createSession(req) { return postJSON(`${PREFIX}/sessions`, req); },
      async whoami() {
        const r = await rfetch(`${base}${PREFIX}/me`, { headers: authHeaders() });
        return r.ok ? r.json() : { authenticated: false };
      },
      async setProfile(displayName) {
        return requestJSON("PUT", `${PREFIX}/me`, { display_name: displayName });
      },
      async authConfig() {
        // Deliberately unauthenticated: whoever is loading the sign-in page has
        // no credential by definition.
        try {
          const r = await fetch(`${base}${PREFIX}/auth/config`);
          return r.ok ? await r.json() : { auth_required: false, configured: false };
        } catch (_) {
          return { auth_required: false, configured: false };
        }
      },
      async getSnapshot(id) {
        const r = await rfetch(`${base}${PREFIX}/sessions/${id}`, { headers: authHeaders() });
        if (!r.ok) {
          const err = new Error(`${r.status}`);
          err.status = r.status;
          throw err;
        }
        return r.json();
      },
      async listSessions() {
        // Scope to this persona's sessions in dev (auth off, shared box);
        // with auth on the server scopes to the principal and ignores this.
        const creator = encodeURIComponent(persona().id);
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 15000);
        try {
          const r = await rfetch(`${base}${PREFIX}/sessions?creator=${creator}`,
            { headers: authHeaders(), signal: controller.signal });
          if (!r.ok) throw new Error("Could not load sessions.");
          return await r.json();
        } finally { clearTimeout(timeout); }
      },
      // Collab contract (threads/comments/participants). With auth on, the
      // server derives the author from the bearer principal; the local persona
      // fields below only matter in auth-off dev mode.
      collab: {
        // ?viewer= is honored only when auth is off (dev persona parity with
        // the mock); with auth on the server uses the bearer principal.
        get: (sid) =>
          requestJSON("GET", `${PREFIX}/sessions/${sid}/collab?viewer=${encodeURIComponent(persona().id)}`),
        createThread: (sid, req) =>
          postJSON(`${PREFIX}/sessions/${sid}/collab/threads`,
            Object.assign({ author_id: persona().id, author_name: persona().name }, req)),
        reply: (sid, tid, req) =>
          postJSON(`${PREFIX}/sessions/${sid}/collab/threads/${tid}/comments`,
            Object.assign({ author_id: persona().id, author_name: persona().name }, req)),
        setStatus: (sid, tid, status) =>
          requestJSON("PATCH", `${PREFIX}/sessions/${sid}/collab/threads/${tid}`,
            { status, actor_id: persona().id }),
        markRead: (sid, tid) =>
          postJSON(`${PREFIX}/sessions/${sid}/collab/threads/${tid}/read`,
            { status: "open", actor_id: persona().id }),
        edit: (sid, cid, body) =>
          requestJSON("PATCH", `${PREFIX}/sessions/${sid}/collab/comments/${cid}`, { body }),
        remove: (sid, cid) =>
          requestJSON("DELETE", `${PREFIX}/sessions/${sid}/collab/comments/${cid}`),
        react: (sid, cid, emoji, on) =>
          requestJSON("PUT", `${PREFIX}/sessions/${sid}/collab/comments/${cid}/reactions`,
            { emoji, on, actor_id: persona().id }),
        addParticipant: (sid, req) =>
          postJSON(`${PREFIX}/sessions/${sid}/collab/participants`, req),
        // Signed invite links (auth-proof): the owner mints a grant, the
        // recipient redeems it as themselves.
        mintLink: (sid, role) =>
          postJSON(`${PREFIX}/sessions/${sid}/collab/links`, { role }),
        join: (sid, req) =>
          postJSON(`${PREFIX}/sessions/${sid}/collab/join`, req),
      },
      connect(id, h) {
        const wsBase = base.replace(/^http/, "ws");
        let ws = null, open = false, firstSnapshot = true, closed = false, fallbackStarted = false;
        async function loadFallbackSnapshot() {
          if (closed || fallbackStarted || !firstSnapshot) return;
          fallbackStarted = true;
          try {
            const response = await fetch(`${base}${PREFIX}/sessions/${id}`, { headers: authHeaders() });
            if (!response.ok) throw new Error("Could not reconnect to this session.");
            const snapshot = await response.json();
            if (!closed && firstSnapshot) {
              firstSnapshot = false;
              h.onEvent?.({ event_type: "session.opened", source: "handshake", session_id: id, payload: { snapshot } });
            }
          } catch (error) { if (!closed) h.onError?.(error); }
        }

        /**
         * Open the socket, preferring a short-lived ticket.
         *
         * A browser cannot set a header on a WebSocket handshake, so the only
         * way to authenticate is the URL — and a URL is the least private part
         * of a request: access logs, browser history, Referer. A ticket is
         * minted over a normal authenticated request, spent on use, and worth
         * nothing a minute later; the raw token is the fallback for a server
         * that has not shipped tickets yet.
         */
        const openSocket = async () => {
          let query = wsQuery;
          if (token) {
            try {
              const r = await rfetch(
                `${base}${PREFIX}/tickets?purpose=ws&session_id=${encodeURIComponent(id)}`,
                { method: "POST", headers: authHeaders() });
              if (r.ok) {
                const tk = await r.json();
                if (tk && tk.ticket) query = `?ticket=${encodeURIComponent(tk.ticket)}`;
              }
            } catch (_) { /* keep the token fallback */ }
          }
          try {
            ws = new WebSocket(`${wsBase}${PREFIX}/sessions/${id}/ws${query}`);
            ws.onopen = () => { open = true; if (h.onOpen) h.onOpen(); };
            ws.onmessage = (e) => {
              let event;
              try { event = JSON.parse(e.data); } catch (_) { return; }
              if (firstSnapshot && event.event_type === "session.opened") { event.source = "handshake"; firstSnapshot = false; }
              if (h.onEvent) h.onEvent(event);
            };
            ws.onclose = () => {
              open = false;
              if (closed) return;
              if (firstSnapshot) loadFallbackSnapshot();
              else h.onClose?.();
            };
            // The close event handles interrupted turns; an unavailable socket can
            // still load the initial snapshot and continue through the HTTP API.
            ws.onerror = () => {};
          } catch (_) { queueMicrotask(loadFallbackSnapshot); }
        };
        openSocket();
        const handshakeTimer = setTimeout(() => {
          if (firstSnapshot && !closed) { ws?.close(); loadFallbackSnapshot(); }
        }, 4000);
        async function rest(path, body) {
          const res = await postJSON(`${PREFIX}/sessions/${id}${path}`, body);
          (res.events || []).forEach((ev) => h.onEvent && h.onEvent({ ...ev, request_id: body.request_id }));
          if (h.onEvent) h.onEvent({ event_type: "session.opened", request_id: body.request_id, session_id: id, payload: { snapshot: res.snapshot } });
        }
        // A frame must NEVER be dropped silently. The `open` flag alone is stale
        // while the socket is CLOSING (or half-dead before onclose fires), and
        // ws.send() on a non-OPEN socket discards the frame without an error —
        // so gate on the live readyState and fall back to REST otherwise. REST
        // failures surface through onError (toast) instead of vanishing.
        const wsLive = () => open && ws && ws.readyState === WebSocket.OPEN;
        const deliver = (path, frame) => {
          if (wsLive()) {
            try { ws.send(JSON.stringify(frame)); return; } catch (_) { /* fall through to REST */ }
          }
          rest(path, frame).catch((err) => { if (h.onError) h.onError(err); });
        };
        return {
          send: (frame) => deliver("/messages", frame),
          choose: (frame) => deliver("/choices", frame),
          getSnapshot: async () => (await fetch(`${base}${PREFIX}/sessions/${id}`, { headers: authHeaders() })).json(),
          close: () => { closed = true; clearTimeout(handshakeTimer); if (ws) { try { ws.close(); } catch (_) {} } },
        };
      },
    };
  }

  // ========================================================================
  // App state
  // ========================================================================
  const app = {
    transport: null,
    sessionId: null,
    conn: null,
    snapshot: null,
    renderedCount: 0,
    observationRendered: false,
    thinkingRendered: false,
    clarifyNode: null,
    attached: false,
    sourceArtifactId: null, // set after a real upload
    busy: false,
    _blocks: {},     // dynamic workbench blocks (proposals/candidates/final)
    pollTimer: null, // snapshot polling while jobs are in flight
    reasoning: [],   // streamed agent.reasoning beats for the current turn
    rightView: "timeline", // which module owns the right pane
    _followLatest: true,
    _activityId: 0,
    _scrollKey: 0,
    requests: new window.EdennState.RequestRegistry(),
    requestQueue: [],
    connectionEpoch: 0,
    _narrow: false,  // chat pane under NARROW_W — widgets that reflow read this
  };

  // The chat pane is user-resizable (300–560px), so the widgets degrade in two
  // steps rather than one. TIGHT drops what is merely nice (a take's duration —
  // the waveform already shows position). NARROW drops what stops working at
  // size: a 30px waveform is noise, and a two-column table is unreadable, so the
  // comparison reflows to stacked cards.
  const TIGHT_W = 448;
  const NARROW_W = 380;

  function pickTransport() {
    const params = new URLSearchParams(window.location.search);
    // Served by the REAL app (under /api/v2/agentic/audio/app) → default to the
    // live backend. The standalone dev page defaults to the mock. Either can be
    // overridden with ?backend=real / ?backend=mock.
    const servedByRealApi = window.location.pathname.indexOf("/api/v2/agentic/audio") === 0;
    const override = params.get("backend");
    const isReal = override === "real" || (servedByRealApi && override !== "mock");
    // The connection pill doubles as a live⇄mock toggle (one click reloads).
    // BOTH badges are set here, not just the entrance one: the session badge
    // would otherwise keep whatever the page shipped with until a health
    // change happened to correct it — reading "Mock" on a live backend.
    [$("entrance-conn"), $("session-conn")].forEach((p) => {
      if (!p) return;
      p.className = "conn " + (isReal ? "live" : "mock");
      p.textContent = isReal ? "Live" : "Mock";
      p.setAttribute("aria-label", isReal ? "Live backend. Switch to mock backend" : "Mock backend. Switch to live backend");
      p.style.cursor = "pointer";
      p.title = isReal
        ? "Using the live agent loop. Click to switch to the offline mock."
        : "Offline mock. Click to connect to the real backend.";
      p.onclick = () => {
        const u = new URL(window.location.href);
        u.searchParams.set("backend", isReal ? "mock" : "real");
        window.location.href = u.toString();
      };
    });
    if (isReal) return RealTransport(params.get("api") || window.location.origin);
    // A deployed console does not serve mock-backend.js at all, so ?backend=mock
    // has nothing behind it. Say so and use the real backend rather than booting
    // a half-app whose every control silently does nothing.
    if (!window.MockBackend) {
      setTimeout(() => toast(
        "The offline demo backend isn't available here — using the live backend."), 400);
      return RealTransport(params.get("api") || window.location.origin);
    }
    return MockTransport();
  }

  /**
   * Report what the live backend is actually doing.
   *
   * Called by the real transport around every request, so the pill reflects
   * reachability instead of intent. Mock mode never calls it.
   */
  function setBackendHealth(ok, detail) {
    if (!app.transport || app.transport.kind !== "real") return;
    if (app._backendOk === ok) return;
    app._backendOk = ok;
    [$("entrance-conn"), $("session-conn")].forEach((p) => {
      if (!p) return;
      p.className = "conn " + (ok ? "live" : "down");
      p.textContent = ok ? "Live backend" : "Backend unreachable";
      p.title = ok
        ? "Using the live agent loop. Click to switch to the offline mock."
        : (detail || "No response from the backend.")
          + " Check the API base in Settings, or click to work offline.";
    });
  }

  // ========================================================================
  // Entrance
  // ========================================================================
  function wireEntrance() {
    // Real file picker → upload → bind the returned source artifact id.
    $("attach-btn").addEventListener("click", () => $("video-file").click());
    $("video-file").addEventListener("change", async (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      app.attached = true;
      $("attach-name").textContent = file.name;
      $("attach-meta").textContent = "uploading…";
      $("attach-chip").classList.add("is-on");
      $("attach-btn").classList.add("is-on");
      // A cancel affordance, live for as long as the upload is. A customer on
      // a slow connection who picked the wrong file should not have to close
      // the tab to escape it.
      let abortUpload = null;
      const cancel = el("button", "attach-cancel", "Cancel");
      cancel.type = "button";
      cancel.onclick = () => { if (abortUpload) abortUpload(); };
      $("attach-meta").after(cancel);
      const clearCancel = () => cancel.remove();

      try {
        const res = await app.transport.uploadVideo(file, (fraction, abort) => {
          abortUpload = abort;
          $("attach-meta").textContent = "uploading… " + Math.round(fraction * 100) + "%";
        });
        clearCancel();
        app.sourceArtifactId = res.artifact_id;
        const dur = res.metadata && res.metadata.duration;
        const mb = (file.size / (1024 * 1024)).toFixed(1);
        $("attach-meta").textContent = (dur ? fmtDur(dur) + " · " : "") + mb + " MB";
        if (app.transport.kind === "mock") {
          // The offline mock can't analyze the actual file — say so instead of
          // silently pretending (the canned sample drives the demo).
          $("attach-meta").textContent += " · offline demo analyzes a sample";
        }
      } catch (err) {
        clearCancel();
        app.sourceArtifactId = null;
        if (err && err.aborted) {
          // Their own decision, not a failure to apologise for.
          app.attached = false;
          $("attach-meta").textContent = "cancelled";
          $("attach-chip").classList.remove("is-on");
          $("attach-btn").classList.remove("is-on");
          e.target.value = "";
          return;
        }
        $("attach-meta").textContent = "upload failed";
        // A 404/405/501 from the page's own origin means this page is served by
        // a static file host with no live API behind it (the Live pill can be
        // on while ?api= is unset) — name the actual problem, not just "failed".
        if (app.transport.kind === "real" && /^(404|405|501)\b/.test(err.message || "")) {
          toast("Upload failed — this page's host has no live API. Open the dev server's own URL, or set the API base in Settings.");
        } else {
          toast("Upload failed: " + err.message);
        }
      }
    });
    $("attach-remove").addEventListener("click", () => {
      app.attached = false; app.sourceArtifactId = null;
      $("video-file").value = "";
      $("attach-chip").classList.remove("is-on");
      $("attach-btn").classList.remove("is-on");
    });
    const moreStarters = $("more-starters");
    if (moreStarters) moreStarters.addEventListener("click", () => {
      const open = moreStarters.getAttribute("aria-expanded") !== "true";
      moreStarters.setAttribute("aria-expanded", String(open));
      $("starters").classList.toggle("is-expanded", open);
    });
    // Voice direction — real dictation where the browser provides it; the
    // control hides (rather than lies) when speech recognition is missing.
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const mic = $("mic-btn");
    if (!SR) {
      mic.hidden = true;
    } else {
      let rec = null;
      mic.addEventListener("click", () => {
        if (rec) { rec.stop(); return; } // second tap stops; onend resets
        rec = new SR();
        rec.lang = "en-US";
        rec.interimResults = false;
        rec.continuous = false;
        const before = $("start-text").value;
        rec.onresult = (e) => {
          const said = Array.from(e.results).map((r) => r[0].transcript).join(" ").trim();
          if (said) $("start-text").value = (before ? before.replace(/\s+$/, "") + " " : "") + said;
        };
        rec.onerror = (e) => {
          toast(e.error === "not-allowed"
            ? "Microphone access was blocked — allow it to dictate."
            : "Dictation didn't catch that — try again.");
        };
        rec.onend = () => { rec = null; mic.classList.remove("is-on"); $("start-text").focus(); };
        try { rec.start(); mic.classList.add("is-on"); }
        catch (_) { rec = null; toast("Dictation is unavailable right now."); }
      });
    }
    document.querySelectorAll("#starters .chip[data-fill]").forEach((c) => {
      c.addEventListener("click", () => {
        $("start-text").value = c.getAttribute("data-fill");
        $("start-text").focus();
      });
    });
    const ideas = [
      "Cinematic and premium — slow build, emotional close.",
      "Gritty trailer energy — tense, then a hard drop on the final cut.",
      "Bright, summery pop — fun and fast, matched to the pacing.",
      "Late-night lo-fi — dusty, mellow, easy to talk over.",
    ];
    $("surprise-btn").addEventListener("click", () => {
      $("start-text").value = ideas[Math.floor(Math.random() * ideas.length)];
      $("start-text").focus();
    });
    $("start-form").addEventListener("submit", startSession);

    // ---- sidebar: panes (home / sessions / gallery), docs, settings -------
    document.querySelectorAll(".nav__item[data-pane]").forEach((b) =>
      b.addEventListener("click", () => showPane(b.getAttribute("data-pane"))));
    $("nav-docs").addEventListener("click", () => { $("docs-overlay").hidden = false; });
    $("docs-close").addEventListener("click", () => { $("docs-overlay").hidden = true; });
    $("sessions-refresh").addEventListener("click", () => renderSessionsPane());
    [$("side-settings"), $("topbar-settings"), $("account-btn")].forEach((b) =>
      b && b.addEventListener("click", openSettings));
    wireSettings();
  }

  // ---- entrance panes ------------------------------------------------------
  function showPane(name) {
    ["home", "sessions", "gallery"].forEach((k) => {
      const n = $("pane-" + k);
      if (n) n.hidden = k !== name;
    });
    document.querySelectorAll(".nav__item[data-pane]").forEach((b) =>
      b.classList.toggle("is-active", b.getAttribute("data-pane") === name));
    if (name === "sessions") renderSessionsPane();
    if (name === "gallery") renderGalleryPane();
    if (name === "home") $("start-text").focus();
  }

  async function loadSessionList(noteEl) {
    noteEl.hidden = true;
    let sessions = [];
    try {
      sessions = (await app.transport.listSessions()).sessions || [];
    } catch (e) {
      noteEl.textContent = "Couldn't load sessions — " + ((e && e.message) || "try again.");
      noteEl.hidden = false;
      return null;
    }
    if (app.transport.kind === "mock") {
      noteEl.textContent = "Offline demo — sessions live for this page only. Go live for a durable history.";
      noteEl.hidden = false;
    }
    return sessions;
  }

  async function renderSessionsPane() {
    const grid = $("sessions-grid");
    const sessions = await loadSessionList($("sessions-note"));
    if (sessions == null) { grid.innerHTML = ""; return; }
    grid.innerHTML = "";
    if (!sessions.length) {
      grid.appendChild(el("div", "cards-empty",
        "Nothing here yet — start your first session and it will show up here."));
      return;
    }
    sessions.forEach((s) => {
      const phase = (PHASES.find((p) => p[0] === s.phase) || [])[1] || s.phase;
      const card = el("button", "sess-card");
      card.type = "button";
      card.appendChild(el("div", "sess-card__title",
        esc(s.title || "Session " + String(s.session_id).slice(-6))));
      const meta = el("div", "sess-card__meta");
      meta.appendChild(el("span",
        "sess-card__phase" + (s.phase === "completed" ? " is-done" : ""), esc(phase)));
      if (s.shared) {
        meta.appendChild(el("span", "sess-card__shared",
          '<i class="ti ti-users"></i> Shared with you'));
      }
      if (s.updated_at) meta.appendChild(el("span", "sess-card__ago", esc(fmtAgo(s.updated_at))));
      card.appendChild(meta);
      card.addEventListener("click", () => resumeSession(s.session_id));
      grid.appendChild(card);
    });
  }

  async function renderGalleryPane() {
    const grid = $("gallery-grid");
    const sessions = await loadSessionList($("gallery-note"));
    if (sessions == null) { grid.innerHTML = ""; return; }
    grid.innerHTML = "";
    const done = sessions.filter((s) => s.final_media_url);
    if (!done.length) {
      grid.appendChild(el("div", "cards-empty",
        "No finished mixes yet — lock a take and it lands here."));
      return;
    }
    done.forEach((s) => {
      const card = el("div", "gal-card");
      const url = s.final_media_url;
      const isVideo = /\.(mp4|mov|webm)(\?|$)/i.test(url) || url.indexOf("data:video") === 0;
      const media = document.createElement(isVideo ? "video" : "audio");
      media.src = mediaSrc(url);
      media.controls = true;
      if (isVideo) media.preload = "metadata";
      media.className = "gal-card__media" + (isVideo ? "" : " gal-card__media--audio");
      // A player is a promise that something will play. The gallery rendered one
      // per finished session without ever checking, so the first screen a
      // returning user opened showed four dead players and no explanation. When
      // the media does not load, say so — and still offer the session, which is
      // the thing they came back for.
      media.addEventListener("error", () => {
        if (!media.parentNode) return;
        const gone = el("div", "gal-card__media gal-card__gone",
          '<i class="ti ti-alert-circle"></i> This mix is no longer available');
        media.parentNode.replaceChild(gone, media);
      });
      card.appendChild(media);
      const hd = el("div", "gal-card__hd");
      hd.appendChild(el("span", "gal-card__title",
        esc(s.title || "Session " + String(s.session_id).slice(-6))));
      const open = el("button", "btn-ghost gal-card__open", "Open session");
      open.type = "button";
      open.addEventListener("click", () => resumeSession(s.session_id));
      hd.appendChild(open);
      card.appendChild(hd);
      grid.appendChild(card);
    });
  }

  // ---- settings sheet ------------------------------------------------------
  function openSettings() {
    $("set-name").value = persona().name;
    const params = new URLSearchParams(window.location.search);
    const isReal = app.transport.kind === "real";
    document.querySelectorAll("#set-backend button").forEach((b) =>
      b.classList.toggle("is-on", (b.getAttribute("data-b") === "real") === isReal));
    $("set-api").value = params.get("api") || "";
    $("set-token").value = window.__edennToken || "";
    $("settings-overlay").hidden = false;
    $("set-name").focus();
  }

  function wireSettings() {
    let pendingBackend = null;
    document.querySelectorAll("#set-backend button").forEach((b) =>
      b.addEventListener("click", () => {
        pendingBackend = b.getAttribute("data-b");
        document.querySelectorAll("#set-backend button").forEach((x) =>
          x.classList.toggle("is-on", x === b));
      }));
    $("settings-cancel").addEventListener("click", () => {
      pendingBackend = null;
      $("settings-overlay").hidden = true;
    });
    $("settings-save").addEventListener("click", () => {
      renamePersona($("set-name").value);
      const params = new URLSearchParams(window.location.search);
      const curBackend = app.transport.kind === "real" ? "real" : "mock";
      const nextBackend = pendingBackend || curBackend;
      const api = $("set-api").value.trim();
      const token = $("set-token").value.trim();
      const changed = nextBackend !== curBackend ||
        api !== (params.get("api") || "") || token !== (window.__edennToken || "");
      $("settings-overlay").hidden = true;
      pendingBackend = null;
      if (!changed) { toast("Saved."); return; }
      // Transport identity changed ⇒ clean reload with the new selectors. The
      // token rides the URL only (never storage) — same trust model as before.
      const u = new URL(window.location.href);
      u.searchParams.set("backend", nextBackend);
      if (api) u.searchParams.set("api", api); else u.searchParams.delete("api");
      if (token) u.searchParams.set("token", token); else u.searchParams.delete("token");
      window.location.href = u.toString();
    });
    $("settings-overlay").addEventListener("click", (e) => {
      if (e.target === $("settings-overlay")) $("settings-overlay").hidden = true;
    });
    $("docs-overlay").addEventListener("click", (e) => {
      if (e.target === $("docs-overlay")) $("docs-overlay").hidden = true;
    });
  }

  async function startSession() {
    if (app.busy) return;
    // Transformation sessions (the cut-shorts flow) are driven end-to-end by
    // the additive transform module against the creation endpoints; the audio
    // path below is byte-identical whenever it is not engaged.
    if (window.EdennTransform && window.EdennTransform.pending) {
      if (app.transport.kind !== "real") {
        // Fail up-front with the reason, not after a doomed fetch — and disarm
        // the transform intent so the NEXT start runs the normal audio path.
        window.EdennTransform.pending = false;
        toast("Cut-a-short needs the dev backend — this offline page can't run it.");
        return;
      }
      window.EdennTransform.start($("start-text").value.trim());
      return;
    }
    app.busy = true;
    const direction = $("start-text").value.trim();
    app.initialDirection = direction;
    const startBtn = $("start-btn");
    startBtn.disabled = true;

    // Resolve the source video first. A real session REQUIRES the user's own
    // upload — same as production (which has no sample clip). The dev server
    // seeds a sample and advertises it at /dev/info, but we only fall back to it
    // when explicitly opted in via ?demo=1, so the default flow can't silently
    // start on a stand-in clip.
    let sourceId = app.sourceArtifactId || null;
    const allowDemo = new URLSearchParams(window.location.search).get("demo") === "1";
    try {
      if (!sourceId && allowDemo && app.transport.getDemoArtifact) {
        const discovered = await app.transport.getDemoArtifact();
        if (discovered) sourceId = discovered;
      }
    } catch (_) { /* fall through to the no-source check */ }
    if (!sourceId) {
      if (app.transport.kind === "real") {
        toast("Attach a video first — click the + to upload your clip.");
        startBtn.disabled = false;
        app.busy = false;
        return;
      }
      sourceId = "artifact_source_video"; // mock ignores the id
    }

    // Jump to the session view IMMEDIATELY. The real bootstrap runs a full video
    // analysis server-side (~10–15s of scene detection + understanding); we stream
    // that as a live "watching your video" trail instead of freezing the entrance.
    $("entrance").hidden = true;
    $("session").hidden = false;
    // Best title available now: the uploaded filename (sans extension) or the
    // first words of the direction. The analysis title replaces it when the
    // observation lands (reconcile()).
    const attachedName = app.attached && $("attach-name").textContent;
    $("session-name").textContent =
      (attachedName && attachedName.replace(/\.[a-z0-9]+$/i, "")) ||
      (direction ? direction.slice(0, 32) + (direction.length > 32 ? "…" : "") : "New session");
    if (direction) {
      inner().appendChild(renderMessage({ role: "user", content: direction }));
      app.renderedCount += 1;
    }
    startAnalyzing();

    try {
      const res = await app.transport.createSession({
        source_video_artifact_id: sourceId,
        creator_user_id: persona().id,
        initial_message: direction || undefined,
      });
      app.sessionId = res.session_id;
      setSessionUrl(res.session_id); // survive a refresh; pushes a history entry
      $("session-input").disabled = false;
      $("session-send").disabled = false;
      openConnection(); // session.opened → finalizeThinking() + render the brief
    } catch (e) {
      finalizeThinking();
      // back to the entrance so the user can retry cleanly
      resetRenderState();
      $("session").hidden = true;
      $("entrance").hidden = false;
      startBtn.disabled = false;
      if (e && e.status === 401) promptForToken("Sign in to start a session on this backend.");
      else toast("Could not start session: " + e.message);
    } finally {
      app.busy = false;
    }
  }

  // ========================================================================
  // Connection + event dispatch
  // ========================================================================
  function openConnection() {
    const epoch = ++app.connectionEpoch;
    if (app._connection) app._connection.close();
    app._connection = app.transport.connect(app.sessionId, {
      onOpen() {},
      onEvent: (event) => { if (epoch === app.connectionEpoch) onEvent(event); },
      onClose() { if (epoch === app.connectionEpoch && app._thinking) finalizeThinking("interrupted", "Connection interrupted. Check the latest results before retrying."); },
      onError(error) {
        if (epoch !== app.connectionEpoch) return;
        finalizeThinking("error", "Could not finish this request. Check the latest results before retrying.");
        toast((error && error.message) || "Connection issue — please try again.");
      },
    });
    app.conn = { send: (frame) => dispatchRequest("send", frame), choose: (frame) => dispatchRequest("choose", frame) };
    wireSessionComposer();
  }
  function dispatchRequest(kind, frame, retryOf = null) {
    if (app.requests.active) {
      const queued = frame.choice_type === "mix" && app.requestQueue.find(item => item.frame.choice_type === "mix");
      if (queued) queued.frame.payload = { ...queued.frame.payload, ...frame.payload };
      else app.requestQueue.push({ kind, frame, retryOf });
      return;
    }
    const t = app._thinking || startThinking();
    const id = crypto.randomUUID();
    app.requests.begin(id, retryOf); t.requestId = id; t.command = { kind, frame };
    app._connection[kind]({ ...frame, request_id: id });
  }
  function flushRequests() {
    if (app.requests.active || !app.requestQueue.length) return;
    const next = app.requestQueue.shift(); dispatchRequest(next.kind, next.frame, next.retryOf);
  }

  // Live tool activity -> a human-readable beat in the thinking trail, so the
  // user sees real pipeline progress DURING a turn instead of silence until the
  // reply lands. Only real, mapped events — nothing invented.
  const TOOL_BEAT = {
    analyze_video: ["Watching your video", "Scene detection and understanding are running."],
    set_production_plan: ["Locking the plan", "Sequencing the audio layers."],
    generate_candidates: ["Starting your takes", "Sending the direction to the music model."],
    edit_audio: ["Reworking the take", "Rendering the requested change."],
    propose_script: ["Drafting the narration", "Writing a script that matches the video."],
    generate_voiceover: ["Recording the narration", "Text-to-speech is rendering."],
    compose_mix: ["Balancing the mix", "Laying the audio layers onto your video."],
    adjust_remix: ["Re-balancing the music", "Re-muxing the video at the new level."],
    finalize: ["Locking it in", "Publishing your final mix."],
    plan_sfx: ["Planning sound effects", "Spotting the moments that deserve a hit."],
    generate_sfx: ["Rendering sound effects", "Generating the chosen effects."],
  };

  function onEvent(evt) {
    if (evt.session_id && app.sessionId && evt.session_id !== app.sessionId) return;
    if (evt.request_id && !app.requests.accepts(evt.request_id)) return;
    if (evt.source === "refresh" && !app.requests.refreshIsCurrent(evt.revision)) return;
    if (evt.source === "handshake" && app.requests.active) return;
    switch (evt.event_type) {
      case "session.opened":
        app.snapshot = evt.payload.snapshot;
        reconcile(app.snapshot, evt.payload.turn_complete !== false && evt.source !== "refresh");
        break;
      case "agent.reasoning":
        // Render the beat the instant it streams in — live, inline, into this
        // turn's own thinking block (per-turn activity). No buffering.
        appendThinking(evt.payload);
        break;
      case "tool.started": {
        const beat = TOOL_BEAT[(evt.payload || {}).tool_name];
        if (beat && app._thinking) appendThinking({ status: beat[0], thought: beat[1], step_id: (evt.payload || {}).tool_call_id || (evt.payload || {}).tool_name });
        break;
      }
      case "tool.completed": {
        const payload = evt.payload || {};
        const t = app._thinking;
        const id = payload.tool_call_id || payload.tool_name;
        const step = t && t.steps.find(item => item.id === id);
        if (step) { step.status = "complete"; paintActivity(t); }
        break;
      }
      case "error":
        finalizeThinking("error", (evt.payload && evt.payload.message) || "Request failed. Try again.");
        toast(evt.payload && evt.payload.message || "Something went wrong.");
        break;
      // message.created / clarify.cards / production.plan / tool.* arrive in the
      // next session.opened snapshot and are rendered by reconcile() — keeping a
      // single source of truth and avoiding double-render.
      default:
        // Collab fan-out (comments / participants) is handled by the additive
        // collab module — forward and otherwise ignore.
        if (window.EdennCollab && window.EdennCollab.onEvent &&
            /^(comment\.|participant\.)/.test(evt.event_type || "")) {
          window.EdennCollab.onEvent(evt);
        }
        break;
    }
  }

  // ========================================================================
  // Rendering
  // ========================================================================
  const inner = () => $("thread-inner");

  // Wipe ALL per-session render state so a different session renders from a clean
  // slate. reconcile() is append-only (keyed off app.renderedCount) and every
  // block is memoized, so switching sessions without this leaks the previous
  // session's thread + candidate/SFX/voiceover/mix/final cards into the new one.
  function resetRenderState() {
    finalizeThinking();
    inner().innerHTML = "";
    // The stale snapshot must die with the render state: watchPaneWidth()'s
    // width-flip replay does `if (app.snapshot) reconcile(app.snapshot)`, and a
    // leftover snapshot from session A would repopulate the just-wiped thread
    // (and pin the append-only cursor) under session B.
    app.snapshot = null;
    app.renderedCount = 0;
    // A view that failed on the last session gets a clean chance on the next.
    app._viewFailedEdennTimeline = false;
    app._viewFailedEdennCanvas = false;
    app.observationRendered = false;
    app.thinkingRendered = false;
    app.clarifyNode = null;
    app._firstAssistantRow = null;
    app._blocks = {};
    app._candBlockKey = null;
    app._sfxKey = null;
    app._voStatus = null;
    app._proposalsKey = null;
    app._dirNotesKey = null;
    app._candPendingSince = null;
    app._mixKey = null;
    app._finalKey = null;
    app._clarifyKey = null;
    // Scroll bookkeeping belongs to the thread that just died.
    app._readingAnchor = null;
    app._followLatest = true;
    releaseDisclosurePosition();
    closeTakeMenu();
    if (app._takeAudio) { try { app._takeAudio.pause(); } catch (_) {} app._takeAudio = null; }
    if (app.pollTimer) { clearTimeout(app.pollTimer); app.pollTimer = null; }
  }

  /** Render a right-pane view, surviving a failure inside it. */
  function renderView(name, snap) {
    const mod = window[name];
    if (!mod || !mod.render) return;
    try {
      mod.render(snap);
    } catch (err) {
      // Once per view per session: a broken snapshot repeats on every poll, and
      // a toast per poll would bury the conversation it is warning about.
      const key = "_viewFailed_" + name;
      if (!app[key]) {
        app[key] = true;
        console.error(name + ".render failed", err);
        toast("The " + (name === "EdennTimeline" ? "timeline" : "canvas")
          + " could not draw this update. The conversation is unaffected.");
      }
    }
  }

  function reconcile(snap, opts) {
    if (!snap) return;
    // Two callers, two shapes: the event path passes a boolean ("this snapshot
    // ends the turn"), while the width-flip replay passes an options object and
    // asks to keep a live trail open, because it is a pure re-render.
    const turnComplete = opts === true || !!(opts && opts.turnComplete);
    const st = snap.state || {};
    // The narration finishing deserves an announcement, not a silent card
    // swap: a user who clicked Re-record watched a spinner, then nothing said
    // the money had turned into audio. Announce the TRANSITION only (kept on
    // app state, so replays and reloads stay quiet).
    const voNow = ((st.layers || {}).voiceover || {}).status || "";
    if (app._voStatus && ["queued", "processing"].includes(app._voStatus)
        && voNow === "completed") {
      toast("Narration recorded — press play on the voice-over card to hear the read.");
      finalizeThinking();
    } else if (app._voStatus && ["queued", "processing"].includes(app._voStatus)
        && voNow === "failed") {
      toast("The narration render failed — nothing was kept. Try Re-record, or ask in chat.");
      finalizeThinking();
    }
    app._voStatus = voNow;
    // The analysis title is the session's real name — adopt it as soon as the
    // observation lands (replaces the filename/direction placeholder).
    const obsTitle = st.observation && st.observation.video_title;
    if (obsTitle && $("session-name").textContent !== obsTitle) {
      $("session-name").textContent = obsTitle;
    }
    // Close the bootstrap "watching your video" trail with a beat built from the
    // REAL analysis (the staged beats are generic pipeline labels) — the block
    // ends tailored to this video instead of trailing off on canned text.
    if (app._thinking && st.observation && !app.observationRendered) {
      const o = st.observation;
      const facts = [];
      if ((o.scenes || []).length) facts.push(o.scenes.length + " scenes");
      if (o.duration_s) facts.push(fmtDur(o.duration_s));
      if (o.detected_include_vocals === false) facts.push("no dialogue");
      else if (o.detected_include_vocals === true) facts.push("has dialogue");
      const title = o.video_title ? "“" + o.video_title + "” — " : "";
      if (facts.length) appendThinking({ status: "Here's what I found", thought: title + facts.join(" · ") + "." });
    }
    // This turn's reasoning has finished streaming — close its thinking block so
    // the reply renders *after* it. (A fresh block opens on the next turn.)
    if (turnComplete) finalizeThinking();
    // 1) append any new messages (index-based; messages are append-only)
    const msgs = snap.messages || [];
    let firstAssistantRow = app._firstAssistantRow || null;
    for (let i = app.renderedCount; i < msgs.length; i++) {
      // A comment-thread @agent turn is tagged source="comment": it belongs in
      // the comment thread (collab-mode renders it there), NOT the main chat —
      // otherwise its user+assistant messages interleave into the transcript and
      // scramble the visible turn order. Advance the cursor without rendering.
      if ((msgs[i].payload || {}).source === "comment") continue;
      // The clarify card below asks the question already; repeating it as the
      // tail of the reply is the same sentence twice in 40px.
      const m = { ...msgs[i] };
      const question = st.pending_clarification && st.pending_clarification.question;
      if (m.role === "assistant" && question && typeof m.content === "string" && m.content.endsWith(question)) m.content = m.content.slice(0, -question.length).trim();
      const row = renderMessage(m);
      inner().appendChild(row);
      if (msgs[i].role === "assistant" && !firstAssistantRow) firstAssistantRow = row;
    }
    // Never rewind the append-only cursor: an out-of-order (stale) snapshot from
    // a poll racing a WS turn-end would otherwise re-append the tail as duplicates.
    app.renderedCount = Math.max(app.renderedCount, msgs.length);
    app._firstAssistantRow = firstAssistantRow;

    if (st.observation) app.observationRendered = true;

    // 2) clarify (intent / quick-reply) reflects pending_clarification. Keyed
    // on the question, so back-to-back clarifies (intent card answered → the
    // SFX treatment card arrives in the SAME snapshot) mount the new card
    // instead of being swallowed by the still-referenced previous node.
    const pending = st.pending_clarification;
    const pendingKey = pending
      ? JSON.stringify([pending.question, pending.topic || "", (pending.options || []).map((o) => o.id)])
      : null;
    if (pending && (!app.clarifyNode || app._clarifyKey !== pendingKey)) {
      if (app.clarifyNode) {
        // A different question replaced the old one: retire the old card in
        // place (disabled, still reviewable) before mounting the new one.
        app.clarifyNode.querySelectorAll("button").forEach((b) => (b.disabled = true));
      }
      app.clarifyNode = renderClarify(pending);
      app._clarifyKey = pendingKey;
      inner().appendChild(app.clarifyNode);
    } else if (!pending && app.clarifyNode) {
      // Answered: KEEP the question + option cards in the scrollback (disabled)
      // instead of deleting them — the thread history must stay reviewable.
      // (Click answers already disable + mark the pick; this also covers typed answers.)
      app.clarifyNode.querySelectorAll("button").forEach((b) => (b.disabled = true));
      compactChoice(app.clarifyNode, app.clarifyNode.dataset.choiceLabel || "Layers · " + ((st.production_plan || {}).layers || []).map(layerName).join(" + "));
      app.clarifyNode = null;
      app._clarifyKey = null;
    }

    // 3b) proposals → 3c) candidates → 3d) voiceover script → 3e) mix → 3f) final
    renderProposals(st);
    renderCandidates(st);
    renderVoiceover(st);
    renderSfx(st);
    renderMixPanel(st);
    renderFinal(st);
    renderDirNotes(st);
    maybePoll(st);

    // 4) chrome
    scrollThread();

    // Both right-pane views render from this same snapshot; each is a no-op
    // while the other is showing. The chat pane on the left is always live.
    // The right-pane views render the same snapshot the thread just rendered.
    // They are downstream of the conversation, so a malformed field in one lane
    // must not abort reconcile and take the CHAT down with it — the chat is how
    // the user fixes anything that has gone wrong. Report and carry on.
    renderView("EdennTimeline", snap);
    renderView("EdennCanvas", snap);
    // Every reconcile rebuilds cards, so the role gate has to run AFTER them —
    // otherwise a freshly rendered take row hands a viewer a Use this / Branch
    // the server will refuse.
    if (window.EdennCollab && window.EdennCollab.gateByRole) window.EdennCollab.gateByRole();
  }

  // ----- dynamic media blocks (warm glass) --------------------------------
  function placeBlock(key, node) {
    const old = app._blocks[key];
    node.dataset.scrollKey = "block-" + key;
    const focused = old && old.contains(document.activeElement) ? document.activeElement : null;
    const focusKey = control => [control.closest("[data-item-id]")?.dataset.itemId || "",
      control.tagName, control.className, control.getAttribute("aria-label") || control.name || ""].join("|");
    const focusedKey = focused ? focusKey(focused) : null;
    if (old) old.replaceWith(node);
    else { node.classList.add("is-arriving"); inner().appendChild(node); }
    app._blocks[key] = node;
    if (focusedKey) {
      const replacement = Array.from(node.querySelectorAll("button,input,textarea,select,summary"))
        .find(control => focusKey(control) === focusedKey);
      if (replacement && !replacement.disabled) replacement.focus({ preventScroll: true });
    }
  }
  function clearBlock(key) {
    if (app._blocks[key]) { app._blocks[key].remove(); app._blocks[key] = null; }
  }
  function miniWave(n, dynamic) {
    const w = el("div", "gwave");
    for (let i = 0; i < n; i++) {
      const bar = document.createElement("i");
      const h = dynamic ? (i < n * 0.6 ? 6 + ((i * 7) % 8) : 2 + ((i * 3) % 4)) : 3 + ((i * 5) % 6);
      bar.style.height = h + "px";
      w.appendChild(bar);
    }
    return w;
  }
  function togglePlay(audio, btn) {
    if (!audio) return;
    if (audio.paused) {
      audio.play().catch(() => {});
      btn.innerHTML = '<i class="ti ti-player-pause"></i>';
      audio.onended = () => (btn.innerHTML = '<i class="ti ti-player-play"></i>');
    } else {
      audio.pause();
      btn.innerHTML = '<i class="ti ti-player-play"></i>';
    }
  }
  const MODEL_LABEL = { edenn_basic: "Basic", edenn_enhanced: "Enhanced", edenn_studio: "Studio" };
  // Mirrors default_candidate_count_for_modelspec on the server: basic returns
  // one take, the other two return a pair to A/B.
  const TAKES_BY_MODEL = { edenn_basic: 1, edenn_enhanced: 2, edenn_studio: 2 };
  /**
   * A tier, and the tier that was asked for when they differ.
   *
   * The server records `requested_modelspec` whenever it renders on a tier other
   * than the one requested — a box without that tier's key, or a workflow that
   * re-resolves it. Nothing rendered that field, so the substitution was
   * invisible and the user simply read a tier they never picked.
   */
  function modelText(o) {
    if (!o) return "";
    const got = MODEL_LABEL[o.modelspec] || o.modelspec || "";
    const asked = o.requested_modelspec && o.requested_modelspec !== o.modelspec
      ? (MODEL_LABEL[o.requested_modelspec] || o.requested_modelspec)
      : "";
    return asked ? got + " · you asked for " + asked : got;
  }
  const EDIT_LABEL = { regenerate: "new take", extend: "extended", creative_edit: "restyled" };
  // Fallback only — the card's voice_options (the server's roster, with real
  // character descriptions) is the source of truth when present.
  const VOICES = [
    { id: "warm_female", name: "Warm Confidante" },
    { id: "bright_female", name: "Bright Spark" },
    { id: "calm_male", name: "Grounded Anchor" },
    { id: "narrator_male", name: "Storybook Narrator" },
    { id: "neutral", name: "Clean Slate" },
  ];

  // ----- 2 · direction comparison ----------------------------------------
  // Two directions, same rows, so the eye scans down a column instead of
  // diffing two paragraphs. Every row is DATA-DRIVEN: a row only appears when at
  // least one proposal carries that field, so the table degrades to header +
  // action on a backend that ships none of them and fills in as fields land.
  // `voice` is the exception — include_vocals is real today and was never shown.
  const DIR_ROWS = [
    { key: "energy", label: "Energy", has: (a) => Array.isArray(a.energy) && a.energy.length > 1 },
    { key: "tempo", label: "Tempo", has: (a) => a.bpm != null || a.tempo_label },
    { key: "voice", label: "Voice", has: (a, p) => typeof p.include_vocals === "boolean" },
    { key: "instruments", label: "Instruments", has: (a) => (a.instruments || []).length > 0 },
    { key: "feel", label: "Feel", has: (a) => (a.feel || []).length > 0 },
    { key: "best_for", label: "Best for", has: (a) => !!a.best_for },
  ];

  function renderProposals(st) {
    const proposals = st.proposals || [];
    if (!proposals.length) { clearBlock("proposals"); return; }
    // Consumed directions STAY in the thread (scrollable history) in a disabled
    // state instead of vanishing — the user must be able to see what was picked.
    const consumed = !!st.approved_direction || (st.candidates || []).length > 0;
    const key = JSON.stringify([proposals, consumed, st.approved_proposal_id]);
    if (app._blocks.proposals && app._proposalsKey === key) return;
    app._proposalsKey = key;
    const previous = app._blocks.proposals;
    if (consumed && previous && !previous.dataset.receipt) {
      previous.querySelectorAll(".dt-use").forEach((button) => { button.disabled = true; });
      const chosen = proposals.find((p) => p.proposal_id === st.approved_proposal_id);
      compactChoice(previous, "Direction · " + (chosen ? chosen.title : "Selected"));
      return;
    }
    const box = el("div", "lane-music w-dup w-block");
    const list = el("div", "direction-options");
    proposals.forEach((p, i) => {
      const card = el("div", "direction-option");
      card.appendChild(dirHeader(p, i === 0));
      const heading = el("div", "direction-heading");
      const name = card.querySelector(".dt-nm");
      card.insertBefore(heading, name); heading.appendChild(name);
      if (i === 0) heading.appendChild(el("span", "direction-recommended", "Recommended"));
      const summary = card.querySelector(".dt-sum");
      if (summary) {
        const sentence = summary.textContent.match(/^.*?[.!?](?:\s|$)/)?.[0]?.trim() || summary.textContent;
        summary.textContent = dirAttrs(p).card_summary || (sentence.length > 120 ? sentence.slice(0, 117).replace(/\s+\S*$/, "") + "…" : sentence);
      }
      const a = dirAttrs(p);
      const energy = dirCell("energy", p, st, i === 0);
      if (energy) {
        energy.classList.add("direction-energy");
        energy.querySelector(".dt-cap")?.remove();
        energy.setAttribute("aria-label", a.suggested_energy ? "Suggested energy based on the direction description" : "Planned energy");
        card.appendChild(energy);
      }
      if (Array.isArray(a.energy) && a.energy.length > 1) {
        const observed = st.observation?.scenes || [];
        const scenes = observed.length ? observed : ["Open", "Build", "Reveal", "Close"].map(label => ({ label }));
        const section = el("div", "direction-scene-energy" + (i ? " is-alternative" : ""));
        const suggested = a.suggested_energy || !observed.length;
        section.appendChild(el("div", "direction-scene-caption", suggested ? "Suggested energy by scene" : "Planned energy by scene"));
        const strip = el("div", "direction-scene-strip");
        const values = a.energy.map(value => Math.max(0, Number(value) || 0));
        const maximum = Math.max(100, ...values);
        const duration = Number(st.observation?.duration_s) || Number(observed.at(-1)?.end_s) || 0;
        scenes.forEach((scene, index) => {
          const position = duration && Number.isFinite(Number(scene.start_s)) && Number.isFinite(Number(scene.end_s))
            ? (Number(scene.start_s) + Number(scene.end_s)) / (2 * duration)
            : (index + .5) / scenes.length;
          const value = values[Math.max(0, Math.min(values.length - 1, Math.round(position * (values.length - 1))))];
          const tile = el("div", "direction-scene-tile");
          const label = String(scene.label || "Scene " + (index + 1));
          tile.title = scene.label || label;
          const bar = el("span", "direction-scene-bar");
          bar.style.height = (6 + value / maximum * 28) + "px";
          bar.setAttribute("aria-hidden", "true");
          const name = el("span", "direction-scene-name"); name.textContent = label;
          tile.append(bar, name); strip.appendChild(tile);
        });
        section.appendChild(strip); card.appendChild(section);
      }
      if (Array.isArray(a.instruments) && a.instruments.length) {
        if (!energy) {
          const palette = el("div", "direction-palette");
          a.instruments.slice(0, 3).forEach(name => {
            const item = el("div", "", '<i class="ti ti-music" aria-hidden="true"></i>');
            item.appendChild(document.createTextNode(name)); palette.appendChild(item);
          });
          card.appendChild(palette);
        }
      }
      const actions = el("div", "direction-actions");
      actions.append(dirButton(p, st, consumed, i === 0), directionDetails(p, st));
      card.appendChild(actions);
      list.appendChild(card);
    });
    box.appendChild(list);
    if (consumed) {
      const chosen = proposals.find((p) => p.proposal_id === st.approved_proposal_id);
      compactChoice(box, "Direction · " + (chosen ? chosen.title : "Selected"));
    }
    placeBlock("proposals", box);
  }

  function directionDetails(proposal, state) {
    const wrap = el("div", "direction-detail-wrap");
    const trigger = el("button", "direction-detail-trigger", '<i class="ti ti-info-circle" aria-hidden="true"></i>');
    trigger.title = "Direction details"; trigger.setAttribute("aria-label", "Details for " + (proposal.title || "direction"));
    trigger.type = "button"; trigger.setAttribute("aria-expanded", "false"); trigger.setAttribute("aria-haspopup", "dialog");
    const pop = el("div", "direction-popover");
    pop.setAttribute("popover", "auto"); pop.setAttribute("role", "dialog"); pop.setAttribute("aria-label", (proposal.title || "Direction") + " details");
    const brief = el("p"); brief.textContent = dirAttrs(proposal).summary || proposal.prompt || ""; pop.appendChild(brief);
    for (const key of ["voice", "tempo", "instruments", "feel", "best_for"]) {
      const cell = dirCell(key, proposal, state, true);
      if (cell) { const section = el("div", "direction-popover-section"); section.append(el("span", "direction-popover-label", key === "best_for" ? "Best for" : cap(key)), cell); pop.append(section); }
    }
    const scenes = state.observation?.scenes || [], energy = dirAttrs(proposal).energy;
    if (scenes.length && Array.isArray(energy) && energy.length > 1) {
      pop.appendChild(el("div", "direction-popover-label", dirAttrs(proposal).suggested_energy ? "Suggested energy across scenes" : "Planned energy across scenes"));
      pop.appendChild(sparkline(energy, scenes, true));
      const names = el("p", "direction-scene-names"); names.textContent = scenes.map(scene => String(scene.label || "Scene").split(/[ —–]/)[0]).join(" · "); pop.appendChild(names);
    }
    let scrollController;
    function hide() { if (pop.matches(":popover-open")) pop.hidePopover(); }
    trigger.addEventListener("click", () => {
      pop.togglePopover();
      if (!pop.matches(":popover-open")) return;
      const rect = trigger.getBoundingClientRect(), height = pop.offsetHeight, width = pop.offsetWidth;
      pop.style.left = Math.max(8, Math.min(innerWidth - width - 8, rect.right - width)) + "px";
      pop.style.top = Math.max(8, rect.bottom + height + 8 < innerHeight ? rect.bottom + 6 : rect.top - height - 6) + "px";
    });
    pop.addEventListener("toggle", event => {
      const open = event.newState === "open"; trigger.setAttribute("aria-expanded", String(open));
      scrollController?.abort();
      if (open) {
        scrollController = new AbortController();
        document.addEventListener("scroll", event => { if (!pop.contains(event.target)) hide(); }, { capture: true, signal: scrollController.signal });
        window.addEventListener("resize", hide, { signal: scrollController.signal });
      }
    });
    wrap.append(trigger, pop); return wrap;
  }

  function dirAttrs(p) {
    const attrs = { ...(p.attributes || {}) };
    const text = [p.title, attrs.summary, p.prompt].filter(Boolean).join(" ").toLowerCase();
    if (!Array.isArray(attrs.energy) || attrs.energy.length < 2) {
      const restrained = /minimal|subtle|steady|ambient|spacious|restrained/.test(text);
      const building = /uplift|energetic|bright|build|driving|momentum/.test(text);
      if (restrained || building) {
        attrs.energy = restrained ? [24, 26, 29, 30, 33, 32, 34, 33] : [18, 23, 34, 31, 48, 44, 60, 65];
        attrs.energy_note = restrained ? "Suggested arc · Steady and spacious" : "Suggested arc · Builds and lifts";
        attrs.suggested_energy = true;
      }
    }
    if (!Array.isArray(attrs.instruments) || !attrs.instruments.length) {
      const matches = [[/synth/, "Synths"], [/percussion/, "Percussion"], [/drum/, "Drums"], [/piano/, "Piano"], [/guitar/, "Guitar"], [/strings/, "Strings"], [/bass/, "Bass"], [/pads?\b/, "Pads"]];
      attrs.instruments = matches.filter(([pattern]) => pattern.test(text)).map(([, name]) => name);
    }
    if (!p.attributes?.summary) {
      if (/bright|uplift/.test(text) && /synth/.test(text) && /percussion/.test(text)) attrs.card_summary = "Bright synths and light percussion with an uplifting pulse.";
      else if (/minimal|subtle/.test(text) && /electronic/.test(text)) attrs.card_summary = "A clean electronic groove with space for narration.";
    }
    return attrs;
  }
  function dirRows(proposals) {
    return DIR_ROWS.filter((r) => proposals.some((p) => r.has(dirAttrs(p), p)));
  }
  function dirChosen(p, st, consumed) {
    return consumed && st.approved_proposal_id ? p.proposal_id === st.approved_proposal_id : false;
  }

  /** One cell's worth of content for a row/proposal pair, or null if it has none. */
  function dirCell(rowKey, p, st, recommended) {
    const a = dirAttrs(p);
    if (rowKey === "energy") {
      if (!Array.isArray(a.energy) || a.energy.length < 2) return null;
      const box = el("div");
      box.appendChild(sparkline(a.energy, (st.observation || {}).scenes || [], recommended));
      if (a.energy_note) box.appendChild(el("div", "dt-cap", esc(a.energy_note)));
      return box;
    }
    if (rowKey === "tempo") {
      if (a.bpm == null && !a.tempo_label) return null;
      const box = el("div");
      box.appendChild(el("div", "dt-val",
        [a.bpm != null ? a.bpm + " BPM" : null, a.tempo_label].filter(Boolean).map(esc).join(" · ")));
      if (a.bpm != null) {
        // 180 BPM full scale — the bar is a relative read against the other
        // column, not a claim about an absolute maximum.
        const m = el("span", "dt-meter");
        m.appendChild(el("i")).style.width = Math.max(4, Math.min(100, (Number(a.bpm) / 180) * 100)) + "%";
        box.appendChild(m);
      }
      return box;
    }
    if (rowKey === "voice") {
      if (typeof p.include_vocals !== "boolean") return null;
      const label = p.include_vocals
        ? cap((p.vocal_gender || "") + " vocal").trim() || "Vocal"
        : "Instrumental";
      return el("div", "dt-val", esc(label));
    }
    if (rowKey === "instruments" || rowKey === "feel") {
      const items = a[rowKey] || [];
      if (!items.length) return null;
      const chips = el("div", "dt-chips");
      items.forEach((t) => chips.appendChild(el("span", "dt-chip", esc(t))));
      return chips;
    }
    if (rowKey === "best_for") return a.best_for ? el("div", null, esc(a.best_for)) : null;
    return null;
  }

  function dirHeader(p, recommended) {
    const box = document.createDocumentFragment();
    box.appendChild(el("div", "dt-nm", esc(p.title || "Direction")));
    const sum = dirAttrs(p).summary || p.prompt;
    if (sum) box.appendChild(el("div", "dt-sum", esc(sum)));
    return box;
  }

  /**
   * The quality tier, as a control rather than a label.
   *
   * There was no tier control anywhere in the console: the director picked, and
   * across every organic session on the deployment it picked the two premium
   * tiers 26 times out of 26 while the analysis suggested the basic one every
   * time. Saying "use the basic model" in chat was the only way to reach it.
   * The picker rides the approval click, so the tier is decided at the moment
   * the user agrees to spend.
   */
  function dirTier(p) {
    const wrap = el("label", "dt-tier");
    wrap.appendChild(el("span", "dt-tier__k", "Quality"));
    const sel = document.createElement("select");
    sel.className = "dt-tier__sel";
    Object.keys(MODEL_LABEL).forEach((id) => {
      const o = document.createElement("option");
      o.value = id;
      o.textContent = MODEL_LABEL[id];
      if (id === (p.modelspec || "edenn_basic")) o.selected = true;
      sel.appendChild(o);
    });
    wrap.appendChild(sel);
    return { wrap, sel };
  }

  function dirButton(p, st, consumed, recommended) {
    const chosen = dirChosen(p, st, consumed);
    const box = el("div", "dt-go");
    const btn = el("button", "dt-use" + (chosen ? " chosen" : recommended ? "" : " alt"),
      chosen ? "Selected" : consumed ? "Not selected" : "Generate takes");
    btn.type = "button";
    if (consumed) {
      btn.disabled = true;
      // A consumed direction shows what it RENDERED at, substitution included.
      box.appendChild(el("div", "dt-tier dt-tier--done", esc(modelText(p))));
      box.appendChild(btn);
      return box;
    }
    const tier = dirTier(p);
    // Say what is actually being bought. The dialog said only "it spends to
    // generate" — true, and uninformative at the one moment a number matters.
    // Takes and tier are facts we hold; a currency figure is not, and inventing
    // one would be worse than the silence it replaced.
    const spendBody = () => {
      const n = TAKES_BY_MODEL[tier.sel.value] || 1;
      return `This generates ${n} ${n === 1 ? "take" : "takes"} at `
        + `${MODEL_LABEL[tier.sel.value] || tier.sel.value} quality — a minute `
        + `or two, and it spends to generate.`;
    };
    btn.addEventListener("click", () =>
      confirmSpend("Generate takes?", spendBody(), () => {
        btn.disabled = true;
        tier.sel.disabled = true;
        startThinking(); // open the trail NOW — the approval turn takes seconds
        app.conn.choose({
          choice_type: "proposal",
          target_id: p.proposal_id,
          payload: { modelspec: tier.sel.value },
        });
      })
    );
    box.appendChild(tier.wrap);
    box.appendChild(btn);
    return box;
  }

  /** Wide: a real grid — label column plus one column per direction. */
  function dirTable(st, proposals, consumed) {
    const wrap = el("div", "dirtab");
    const grid = el("div", "dirtab__grid");
    const cols = proposals.slice(0, 2);
    const cellCls = (i, rec) => "dt-c" + (i === 1 ? " col-b" : "") + (rec ? " is-rec" : "");

    grid.appendChild(el("div", "dt-c dt-hd"));
    cols.forEach((p, i) => {
      const c = el("div", cellCls(i, i === 0) + " dt-hd");
      c.appendChild(dirHeader(p, i === 0));
      grid.appendChild(c);
    });

    dirRows(cols).forEach((r) => {
      grid.appendChild(el("div", "dt-c dt-k", esc(r.label)));
      cols.forEach((p, i) => {
        const c = el("div", cellCls(i, i === 0) + " dt-v");
        const content = dirCell(r.key, p, st, i === 0);
        if (content) c.appendChild(content);
        grid.appendChild(c);
      });
    });

    grid.appendChild(el("div", "dt-c dt-act"));
    cols.forEach((p, i) => {
      const c = el("div", cellCls(i, i === 0) + " dt-act");
      c.appendChild(dirButton(p, st, consumed, i === 0));
      grid.appendChild(c);
    });
    wrap.appendChild(grid);
    return wrap;
  }

  /** Narrow: the same rows, stacked per direction with inline labels. */
  function dirStack(st, proposals, consumed) {
    const wrap = el("div", "dirtab dirtab--stack");
    const rows = dirRows(proposals.slice(0, 2)).filter((row) => ["instruments", "feel", "best_for"].includes(row.key));
    proposals.slice(0, 2).forEach((p, i) => {
      const card = el("div", "dt-card" + (i === 0 ? " is-rec" : ""));
      card.appendChild(dirHeader(p, i === 0));
      rows.forEach((r) => {
        const content = dirCell(r.key, p, st, i === 0);
        if (!content || app._thinking) return;
        const line = el("div", "dt-line");
        line.appendChild(el("div", "dt-k", esc(r.label)));
        const v = el("div", "dt-v");
        v.appendChild(content);
        line.appendChild(v);
        card.appendChild(line);
      });
      const act = el("div", "dt-act");
      act.appendChild(dirButton(p, st, consumed, i === 0));
      card.appendChild(act);
      wrap.appendChild(card);
    });
    return wrap;
  }

  /**
   * Energy curve over the video's own clock, with dotted guides at the real
   * scene cuts. Drawn ONLY from a supplied curve — there is no synthesised
   * fallback, because a shape invented here would read exactly like a measured
   * one (the same rule the timeline lanes follow).
   */
  function sparkline(values, scenes, recommended) {
    const W = 100, H = 30, PAD = 2;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "dt-spark");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("aria-hidden", "true");
    const sceneEndS = (sc) => Number(sc.end_s != null ? sc.end_s : sc.end_timestamp) || 0;
    const sceneStartS = (sc) => Number(sc.start_s != null ? sc.start_s : sc.start_timestamp) || 0;
    const total = scenes.length ? sceneEndS(scenes[scenes.length - 1]) : 0;
    if (total > 0) {
      scenes.slice(1).forEach((sc) => {
        const x = (sceneStartS(sc) / total) * W;
        const ln = document.createElementNS("http://www.w3.org/2000/svg", "line");
        ln.setAttribute("x1", x); ln.setAttribute("x2", x);
        ln.setAttribute("y1", 0); ln.setAttribute("y2", H);
        ln.setAttribute("stroke", "currentColor");
        ln.setAttribute("stroke-width", "0.5");
        ln.setAttribute("stroke-dasharray", "2 2");
        ln.setAttribute("opacity", "0.28");
        svg.appendChild(ln);
      });
    }
    const max = Math.max.apply(null, values.concat([1]));
    const pts = values.map((v, i) => {
      const x = (i / (values.length - 1)) * W;
      const y = H - PAD - (Math.max(0, Number(v)) / max) * (H - PAD * 2);
      return x.toFixed(2) + "," + y.toFixed(2);
    }).join(" ");
    const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.setAttribute("points", pts);
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", recommended ? "var(--lane)" : "var(--text-tertiary)");
    line.setAttribute("stroke-width", "1.4");
    line.setAttribute("stroke-linejoin", "round");
    line.setAttribute("stroke-linecap", "round");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(line);
    return svg;
  }

  function renderCandidates(st) {
    const cands = st.candidates || [];
    if (!cands.length) { clearBlock("candidates"); app._candBlockKey = null; return; }
    const pending = cands.some((c) => c.status === "queued" || c.status === "processing");
    if (pending) { if (!app._candPendingSince) app._candPendingSince = Date.now(); }
    else { app._candPendingSince = null; }
    const elapsedMin = app._candPendingSince ? Math.floor((Date.now() - app._candPendingSince) / 60000) : 0;
    // Memoize: the rows hold live <audio> — rebuilding on every 2.5s poll would
    // restart playback mid-listen. Re-render only when material state
    // (statuses/urls/selection) changes, or the elapsed-minute hint ticks over.
    const key = JSON.stringify([
      cands.map((c) => [c.candidate_id, c.status, c.video_url, c.audio_url,
        c.stalled_seconds ? Math.floor(c.stalled_seconds / 60) : 0,
        // The listen-back note arrives on a later poll than the URL does. Left
        // out of this key it would never render — the row is memoized and the
        // other fields have already settled by then.
        (c.listen_report && (c.listen_report.notes || []).length) || 0]),
      st.selected_candidate_id, pending && elapsedMin, app._narrow,
    ]);
    if (app._blocks.candidates && app._candBlockKey === key) return;
    app._candBlockKey = key;
    const innerEl = el("div", "lane-music w-dup w-block");

    const list = el("div", "takes");
    // Comparing needs something to compare against, so the row only offers it
    // when the session actually has more than one finished take.
    const finishedTakes = cands.filter((c) => c.status === "completed").length;
    cands.forEach((c, i) => list.appendChild(
      candidateRow(c, st.selected_candidate_id, i, finishedTakes)
    ));
    innerEl.appendChild(list);
    // Honest "still working" hint: generation runs on a separate worker; show how
    // long it's actually been, and give Check-now real feedback instead of silence.
    if (pending && app._candPendingSince && Date.now() - app._candPendingSince > 25000) {
      const hint = el("div", "gen-hint");
      const anyStalled = cands.some((c) => c.stalled_seconds);
      const label = anyStalled
        ? "This is taking longer than generation usually does (~3 min) — the backend may be stuck. Retry a take above, or "
        : elapsedMin >= 1
          ? `Still composing — ${elapsedMin} min so far; this usually takes a few minutes. `
          : "Still composing — this usually takes a few minutes. ";
      hint.appendChild(el("span", "", label));
      const again = el("button", "gen-hint__btn", "Check now");
      again.addEventListener("click", async () => {
        try {
          const revision = app.requests.revision;
          const snap = await app.transport.getSnapshot(app.sessionId);
          const stillPending = ((snap.state || {}).candidates || []).some(
            (c) => c.status === "queued" || c.status === "processing"
          );
          onEvent({ event_type: "session.opened", source: "refresh", revision, session_id: app.sessionId, payload: { snapshot: snap } });
          if (stillPending) toast("Checked — still composing. I'll keep refreshing automatically.");
          else toast("Your takes are ready.");
        } catch (_) { toast("Couldn't reach the backend — retrying automatically."); }
      });
      hint.appendChild(again);
      innerEl.appendChild(hint);
    }
    placeBlock("candidates", innerEl); // re-render in place as candidates hydrate
  }
  // ----- 3 · take rows ----------------------------------------------------
  /**
   * One audio result as a single row: play, waveform, length, one action.
   *
   * Deliberately audio-only. A take's video belongs in the timeline hero on the
   * right — that player is already fed by the selected candidate — so putting a
   * second 16:9 player in a 380px column would duplicate it and shove the thread
   * around every time a take lands. Everything that is not "use this take" lives
   * behind the overflow menu.
   */
  function takeRow(o) {
    const row = el("div", "take " + (o.lane || "lane-music") + (o.selected ? " is-sel" : ""));
    if (o.tooltip) row.title = o.tooltip;
    if (o.id) row.dataset.itemId = o.id;

    const play = el("button", "take__play", '<i class="ti ti-player-play-filled"></i>');
    play.type = "button";
    play.title = "Play " + (o.title || "take");
    play.setAttribute("aria-label", play.title);
    row.appendChild(play);
    row.appendChild(el("span", "take__nm", esc(o.title || "Take")));

    if (o.status && o.status !== "ready") {
      play.disabled = true;
      const stt = el("div", "take__status" + (o.status === "error" ? " is-err" : ""));
      if (o.status === "error") stt.appendChild(el("i", "ti ti-alert-triangle"));
      else stt.appendChild(el("span", "gspin"));
      stt.appendChild(el("span", null, esc(o.statusText || "Working…")));
      row.appendChild(stt);
      if (o.onRetry) {
        const retry = el("button", "take__retry", "Try again");
        retry.type = "button";
        retry.addEventListener("click", () => o.onRetry(retry));
        row.appendChild(retry);
      }
      return row;
    }

    const wave = el("div", "take__wave");
    row.appendChild(wave);
    // Drawn from the audio itself. The URL goes through the media tokenizer:
    // on an authenticated deployment an untokenized fetch just 401s.
    requestAnimationFrame(() => window.EdennUI.waveform(wave, mediaSrc(o.url)));

    const dur = el("span", "take__dur", "0:00");
    row.appendChild(dur);

    const audio = typeof Audio !== "undefined" && o.url ? new Audio(mediaSrc(o.url)) : null;
    if (audio) {
      audio.preload = "metadata";
      audio.addEventListener("loadedmetadata", () => {
        const s = Math.round(audio.duration || 0);
        if (s > 0) dur.textContent = Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
      });
      audio.addEventListener("timeupdate", () => {
        const frac = audio.duration ? audio.currentTime / audio.duration : 0;
        wave.style.setProperty("--played", (frac * 100) + "%");
      });
      const stop = () => {
        row.classList.remove("is-playing");
        play.innerHTML = '<i class="ti ti-player-play-filled"></i>';
        play.setAttribute("aria-label", "Play " + (o.title || "take"));
      };
      audio.addEventListener("pause", stop);
      audio.addEventListener("ended", () => { stop(); wave.style.setProperty("--played", "0%"); });
      audio.addEventListener("play", () => {
        // One at a time: a second track over the first is never what you meant.
        if (app._takeAudio && app._takeAudio !== audio) { try { app._takeAudio.pause(); } catch (_) {} }
        app._takeAudio = audio;
        if (window.EdennTimeline) window.EdennTimeline.audition(o.id, audio);
        row.classList.add("is-playing");
        play.innerHTML = '<i class="ti ti-player-pause-filled"></i>';
        play.setAttribute("aria-label", "Pause " + (o.title || "take"));
      });
      play.addEventListener("click", () => {
        if (audio.paused) audio.play().catch(() => {}); else audio.pause();
      });
    } else {
      play.disabled = true;
    }

    if (o.onUse || o.selected) {
      const use = el("button", "take__use" + (o.selected ? " chosen" : ""),
        o.selected ? (o.selectedLabel || "Selected") : (o.useLabel || "Use take"));
      use.type = "button";
      if (o.selected) use.disabled = true;
      else use.addEventListener("click", () => o.onUse(use));
      row.appendChild(use);
    }

    const items = (o.menu || []).filter(Boolean);
    if (items.length) {
      const more = el("button", "take__more", '<i class="ti ti-dots"></i>');
      more.type = "button";
      more.title = "More";
      more.setAttribute("aria-label", "More actions");
      more.addEventListener("click", (e) => { e.stopPropagation(); openTakeMenu(more, items); });
      row.appendChild(more);
    }
    return row;
  }

  /** Anchored popover for a take's secondary actions. One open at a time. */
  function openTakeMenu(anchor, items) {
    closeTakeMenu();
    const menu = el("div", "tmenu");
    items.forEach((it) => {
      const b = el("button", null, `<i class="ti ${it.icon}"></i> ${esc(it.label)}`);
      b.type = "button";
      b.addEventListener("click", () => { closeTakeMenu(); it.run(); });
      menu.appendChild(b);
    });
    document.body.appendChild(menu);
    const r = anchor.getBoundingClientRect();
    const w = menu.offsetWidth, h = menu.offsetHeight;
    // Flip rather than overflow: the pane can be near either edge of the window.
    menu.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + "px";
    menu.style.top = (r.bottom + h + 8 > window.innerHeight ? r.top - h - 6 : r.bottom + 6) + "px";
    app._takeMenu = menu;
    setTimeout(() => document.addEventListener("mousedown", dismissTakeMenu), 0);
  }
  /** Outside-mousedown dismissal. Guarded by containment: a mousedown ON a menu
   *  item must not tear the menu down before the item's click can fire. */
  function dismissTakeMenu(e) {
    if (app._takeMenu && !app._takeMenu.contains(e.target)) closeTakeMenu();
  }
  function closeTakeMenu() {
    document.removeEventListener("mousedown", dismissTakeMenu);
    if (app._takeMenu) { app._takeMenu.remove(); app._takeMenu = null; }
  }

  /** A music candidate as a take row. */
  function candidateRow(c, selectedId, index, finishedTakes) {
    const done = c.status === "completed" && (c.audio_url || c.video_url);
    const isSel = c.candidate_id === selectedId;
    // Lineage / mix deltas that used to be badges now ride in the title, where
    // they read as part of the take's name instead of a second row of chrome.
    const tags = [];
    if (c.parent_candidate_id && c.version > 1) tags.push("v" + c.version);
    if (c.edit_kind) tags.push(EDIT_LABEL[c.edit_kind] || c.edit_kind);
    if (c.preserve_original_audio) tags.push("keeps original");
    if (c.placeholder) tags.push("preview tone");
    else if (done && /placeholder/.test(c.audio_url || c.video_url || "")) tags.push("preview tone");
    // Something measurable is off with the audio itself (stops early, opens or
    // ends silent, has a hole in the middle). The agent explains it in the
    // thread; the tag is so the row itself does not look fine.
    const listenNotes = (c.listen_report && c.listen_report.notes) || [];
    if (done && listenNotes.length) tags.push("worth a listen");
    // Every take in a group carries the same direction name ("Arc-driven
    // electronic · take 2"), so at pane widths the name ellipsises and the ONE
    // distinguishing part — the number — is what disappears. Lead with it; the
    // full title stays as the row tooltip.
    const title = "Take " + (index + 1) + (tags.length ? " · " + tags.join(" · ") : "");

    let status = null, statusText = null, onRetry = null;
    if (c.status === "failed" || c.status === "error") {
      status = "error"; statusText = "Generation didn't finish";
      onRetry = (btn) => requestVariation(c, btn);
    } else if (!done && c.stalled_seconds) {
      const mins = Math.max(1, Math.round(c.stalled_seconds / 60));
      status = "error"; statusText = `Stuck — ${mins} min`;
      onRetry = (btn) => requestVariation(c, btn);
    } else if (!done) {
      status = "pending"; statusText = c.status === "processing" ? "Composing…" : "Queued…";
    }

    return takeRow({
      id: c.candidate_id,
      lane: "lane-music",
      title: title,
      tooltip: c.title || null,
      url: done ? (c.audio_url || c.video_url) : null,
      status: status || "ready",
      statusText: statusText,
      onRetry: onRetry,
      selected: isSel,
      onUse: done && !isSel ? (btn) => {
        if (app._thinking) return;
        btn.disabled = true;
        startThinking(); // lock turn runs several backend ops — show life now
        app.conn.choose({ choice_type: "candidate", target_id: c.candidate_id, payload: {} });
      } : null,
      menu: done ? [
        { label: "New variation", icon: "ti-refresh", run: () => requestVariation(c, null) },
        { label: "Restyle…", icon: "ti-wand", run: () => requestRestyle(c) },
        { label: "Make it longer…", icon: "ti-arrow-bar-right", run: () => requestLonger(c) },
        // Free verbs, listed above the paid one that used to be the only way to
        // change anything about a take.
        c.complete_audio_url
          ? { label: "Move the window…", icon: "ti-arrows-horizontal", run: () => requestSculpt(c) }
          : null,
        finishedTakes > 1
          ? { label: "Compare takes", icon: "ti-columns-2", run: () => requestCompare() }
          : null,
        c.audio_url ? { label: "Download track", icon: "ti-download", run: () => deliverFile(c.audio_url, "edenn-take.mp3") } : null,
      ] : null,
    });
  }

  function renderVoiceover(st) {
    const vo = (st.layers || {}).voiceover;
    // The editable script card shows while drafting AND after the narration is
    // recorded (so the user can tweak the script/voice/tone and re-record).
    // While generation is in flight (queued/processing) it's hidden. Re-render
    // only on a status change so mid-edit typing isn't clobbered.
    const editable = vo && vo.script && (vo.status === "draft" || vo.status === "completed");
    if (editable) {
      // Keyed on the CONTENT, not just the status.
      //
      // propose_script writes status "draft" on every re-draft, so a status-only
      // key left the previous script sitting in the textarea after "make it
      // shorter" — and Generate submits whatever that textarea holds, spending a
      // paid TTS render on text the session had already replaced.
      const voKey = [
        vo.status,
        vo.script || "",
        (vo.segments || []).map((sg) => `${sg.id || ""}:${sg.start_s || 0}:${sg.text || ""}`).join("|"),
        vo.voice_id || "", vo.language || "",
      ].join("§");
      if (!app._blocks.voscript || app._voStatus !== voKey) {
        app._voStatus = voKey;
        placeBlock("voscript", voScriptCard(vo));
      }
    } else {
      clearBlock("voscript");
      app._voStatus = null;
    }
  }

  // ----- 4 · voice-over ---------------------------------------------------
  // Teal, because that is the lane it fills. The script is the thing being
  // edited so it gets the room; voice and tone are settings on it and collapse
  // to one wrapping control bar. Once narration exists it appears as a take row
  // — the same component the music takes use — so "an audio result you can play"
  // looks the same everywhere in the thread.
  function voScriptCard(vo) {
    const recorded = vo.status === "completed";
    const card = el("div", "vo lane-voiceover");

    const hd = el("div", "w-hd");
    hd.appendChild(el("span", "w-hd__ic", '<i class="ti ti-microphone"></i>'));
    hd.appendChild(el("span", "w-hd__t", recorded ? "Voice-over" : "Voice-over script"));
    card.appendChild(hd);

    if (recorded && vo.audio_url) {
      card.appendChild(takeRow({
        id: "vo:" + (vo.linked_job_id || "take"),
        lane: "lane-voiceover",
        title: vo.script ? firstWords(vo.script, 5) : "Narration",
        url: vo.audio_url,
        status: "ready",
        menu: [{ label: "Download narration", icon: "ti-download", run: () => deliverFile(vo.audio_url, "edenn-narration.wav") }],
      }));
    }

    // Video-informed narration: the director's voice rationale + the timed,
    // delivery-directed segment plan (when to speak, how to say each line).
    // The same plan draws on the timeline's voiceover lane, on the clock.
    if (vo.voice_rationale) {
      card.appendChild(el("div", "vo-card__rationale",
        '<i class="ti ti-bulb"></i> ' + esc(vo.voice_rationale)));
    }
    const segs = vo.segments || [];
    if (segs.length) {
      const plan = el("div", "vo-segs");
      segs.forEach((s) => {
        const row = el("div", "vo-seg");
        row.appendChild(el("span", "vo-seg__t", fmtCue(s.start_s || 0)));
        const body = el("div", "vo-seg__body");
        body.appendChild(el("div", "vo-seg__text", esc(s.text || "")));
        if (s.delivery) body.appendChild(el("div", "vo-seg__dir", esc(s.delivery)));
        row.appendChild(body);
        // Re-read THIS line. Every line is its own paid recording, so fixing
        // one by re-recording the script charges for the others and returns
        // subtly different readings of lines the user was happy with. Only
        // offered once there is a recording to keep.
        if (recorded && s.id) {
          const again = el("button", "vo-seg__retake");
          again.type = "button";
          again.title = "Re-read this line";
          again.setAttribute("aria-label", "Re-read this line");
          again.innerHTML = '<i class="ti ti-microphone-2"></i>';
          again.onclick = () => {
            confirmSpend(
              "Re-read this line?",
              "Only this line is recorded again — the rest are kept exactly as they are.",
              () => {
                again.disabled = true;
                startThinking();
                app.conn.choose({
                  choice_type: "voiceover",
                  payload: { segment_id: s.id },
                });
              },
              "Yes, re-read it"
            );
          };
          row.appendChild(again);
        }
        plan.appendChild(row);
      });
      card.appendChild(plan);
    }

    const editor = el("div", recorded ? "vo__divider" : null);
    editor.style.cssText = "display:flex;flex-direction:column;gap:9px";
    if (recorded) {
      editor.appendChild(el("div", "w-note", "Edit the script or change the voice, then re-record."));
    }
    const ta = el("textarea", "vo__script");
    ta.value = vo.script || "";
    ta.rows = Math.min(7, Math.max(3, Math.ceil((vo.script || "").length / 58)));
    ta.setAttribute("aria-label", "Voice-over script");
    if (segs.length) {
      // The segments above are the source of truth; the flat text stays as the
      // editable fallback (editing it re-drafts WITHOUT the timing plan).
      ta.placeholder = "Editing this text replaces the timed plan with a flat script.";
    }
    editor.appendChild(ta);

    const bar = el("div", "vo__bar");
    const voiceCtl = el("label", "vo__ctl");
    voiceCtl.appendChild(el("i", "ti ti-user"));
    const sel = el("select");
    sel.setAttribute("aria-label", "Voice");
    const roster = (vo.voice_options && vo.voice_options.length) ? vo.voice_options : VOICES;
    // NEVER silently re-cast. A voice the roster does not list still gets an
    // option of its own, selected: the alternative is a select that quietly
    // falls to its first entry, which is how a paid render was spoken by a voice
    // neither the user nor the director chose while the card kept showing the
    // rationale for the one that was discarded.
    const listed = roster.some((v) => v.id === vo.voice_id);
    const options = listed || !vo.voice_id
      ? roster
      : [{ id: vo.voice_id, name: vo.voice_id, style: "chosen by the director" }].concat(roster);
    options.forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v.id;
      opt.textContent = v.style ? `${v.name} — ${v.style}` : v.name;
      if (v.style) opt.title = v.style;
      if (v.id === vo.voice_id) opt.selected = true;
      sel.appendChild(opt);
    });
    voiceCtl.appendChild(sel);
    bar.appendChild(voiceCtl);

    const toneCtl = el("label", "vo__ctl");
    toneCtl.appendChild(el("i", "ti ti-wand"));
    const tone = el("input");
    tone.type = "text";
    tone.placeholder = "warm and confident";
    tone.value = vo.tone || "";
    tone.setAttribute("aria-label", "Tone");
    toneCtl.appendChild(tone);
    bar.appendChild(toneCtl);
    editor.appendChild(bar);

    const gen = el("button", "btn-primary vo__gen",
      '<i class="ti ti-wand"></i> ' + (recorded ? "Re-record" : "Generate voice-over"));
    gen.type = "button";
    gen.addEventListener("click", () => {
      // An empty script would silently no-op server-side — say so instead.
      if (!ta.value.trim()) { toast("Write (or keep) a script first — the narration needs words."); return; }
      // Voice-over generation spends — gate it behind the same confirm as music.
      confirmSpend(
        recorded ? "Re-record the voice-over?" : "Generate the voice-over?",
        "This uses your generation credits to record narration.",
        () => {
          gen.disabled = true;
          startThinking(); // TTS turn takes a moment — immediate feedback
          // Structured voice-over choice: the backend (re)drafts the edited script
          // then runs TTS — no free-text for the LLM to re-parse.
          app.conn.choose({
            choice_type: "voiceover",
            // The language rides the request. The server falls back to the
            // stored layer, so omitting it usually worked — but "usually" is
            // how a non-English script ends up synthesised in the language the
            // footage was detected in, with nothing on the wire to say
            // otherwise.
            payload: {
              script: ta.value.trim(),
              voice_id: sel.value,
              tone: tone.value.trim(),
              language: vo.language || "",
            },
          });
        }
      );
    });
    editor.appendChild(gen);
    card.appendChild(editor);
    return card;
  }

  function firstWords(script, n) {
    const words = String(script).trim().split(/\s+/);
    return words.slice(0, n).join(" ") + (words.length > n ? "…" : "");
  }

  // ----- SFX layer (spotted plan + rendered variants) ---------------------
  function renderSfx(st) {
    const sfx = (st.layers || {}).sfx;
    // Only a real (dict) SFX layer with a spotted plan renders; a legacy list or
    // an empty/absent layer shows nothing. An ambience-only plan (zero discrete
    // events, a continuous bed) is a real plan and must render.
    if (
      !sfx || typeof sfx !== "object" || Array.isArray(sfx) ||
      (!(sfx.events || []).length && !(sfx.ambience || "").trim())
    ) {
      clearBlock("sfx");
      app._sfxKey = null;
      return;
    }
    const variants = sfx.variants || [];
    const pending = variants.some((v) => v.status === "queued" || v.status === "processing");
    // Re-render only on material change (variant statuses/urls/selection/plan size),
    // so 2.5s polls don't restart an inline player mid-listen.
    const key = JSON.stringify([
      // Full event CONTENT (not just count) + summary + ambience, so a re-plan
      // that rewrites the spotting with the same event count still re-renders.
      (sfx.events || []).map((e) => [e.label, e.prompt, e.start_s, e.reason]),
      sfx.summary || "", sfx.ambience || "", sfx.selected_variant_id,
      (sfx.treatment || {}).label || "", sfx.cap_note || "", !!sfx.over_budget,
      (sfx.suggestions || []).map((s) => [s.suggestion_id, s.status]),
      variants.map((v) => [v.variant_id, v.status, v.audio_url, v.video_url]),
    ]);
    if (app._blocks.sfx && app._sfxKey === key) return;
    app._sfxKey = key;
    placeBlock("sfx", sfxCard(sfx, pending));
  }

  function sfxCard(sfx, pending) {
    const events = sfx.events || [];
    const variants = sfx.variants || [];
    const card = el("div", "sfx-card");
    card.appendChild(el("div", "sfx-card__hd",
      '<i class="ti ti-wave-square"></i> Sound effects' +
      (sfx.summary ? ' <span class="sfx-card__sub">— ' + esc(sfx.summary) + "</span>" : "")));
    // The answered treatment (register + density the user chose) frames the plan.
    if (sfx.treatment && sfx.treatment.label) {
      card.appendChild(el("div", "sfx-card__treatment",
        '<i class="ti ti-adjustments"></i> ' + esc(sfx.treatment.label)));
    }
    // The spotted plan: one row per timed moment, each with its on-screen reason.
    const plan = el("div", "sfx-plan");
    events.forEach((ev) => {
      const row = el("div", "sfx-plan__row");
      row.appendChild(el("span", "sfx-plan__t", fmtCue(ev.start_s || 0)));
      row.appendChild(el("span", "sfx-plan__label", esc(ev.label || ev.prompt || "Effect") +
        (ev.reason ? ' <span class="sfx-plan__why">— ' + esc(ev.reason) + "</span>" : "")));
      // Redo THIS hit. Each effect is its own paid generation, so re-rendering
      // a bed of twelve to fix one charges for eleven the user was happy with
      // and returns different versions of them. Only once a bed exists to keep.
      const rendered = ((sfx.variants || []).some((v) => v.status === "completed"));
      if (rendered && ev.id) {
        const redo = el("button", "sfx-plan__redo");
        redo.type = "button";
        redo.title = "Redo this effect";
        redo.setAttribute("aria-label", "Redo this effect");
        redo.innerHTML = '<i class="ti ti-refresh"></i>';
        redo.onclick = () => {
          confirmSpend(
            "Redo this effect?",
            "Only this one is made again — every other sound in the bed is kept as it is.",
            () => {
              redo.disabled = true;
              startThinking();
              app.conn.choose({ choice_type: "sfx", payload: { event_ids: [ev.id] } });
            },
            "Yes, redo it"
          );
        };
        row.appendChild(redo);
      }
      plan.appendChild(row);
    });
    if (sfx.ambience) {
      const amb = el("div", "sfx-plan__row sfx-plan__row--amb");
      amb.appendChild(el("span", "sfx-plan__t", '<i class="ti ti-wind"></i>'));
      amb.appendChild(el("span", "sfx-plan__label", "Ambience — " + esc(sfx.ambience)));
      plan.appendChild(amb);
    }
    card.appendChild(plan);
    // The density budget, legibly: the cap is a visible norm, not a silent trim.
    if (sfx.cap_note) {
      const note = el("div", "sfx-card__cap" + (sfx.over_budget ? " is-over" : ""),
        '<i class="ti ti-gauge"></i> ' + esc(sfx.cap_note) +
        (sfx.over_budget ? " <b>(this plan is over budget)</b>" : ""));
      card.appendChild(note);
    }

    // Ghost suggestions from the hybrid proposers — pending ideas with their
    // rationale; accepting is an explicit, FREE plan edit.
    const ghostsPending = (sfx.suggestions || []).filter((s) => s.status === "pending");
    if (ghostsPending.length) {
      card.appendChild(el("div", "sfx-ghosts__hd",
        '<i class="ti ti-sparkles"></i> Suggested — accept to add to the plan'));
      const ghosts = el("div", "sfx-ghosts");
      ghostsPending.forEach((s) => {
        const row = el("div", "sfx-ghost");
        row.appendChild(el("span", "sfx-ghost__t", fmtDur(s.start_time || 0)));
        const body = el("div", "sfx-ghost__body");
        body.appendChild(el("div", "sfx-ghost__label",
          esc(s.sound_prompt || s.description || "Idea") +
          ' <span class="sfx-ghost__origin">' + esc(s.origin || "idea") + "</span>"));
        if (s.rationale) body.appendChild(el("div", "sfx-ghost__why", esc(s.rationale)));
        row.appendChild(body);
        const act = el("div", "sfx-ghost__act");
        const ok = el("button", "sfx-ghost__btn ok", '<i class="ti ti-check"></i>');
        ok.type = "button"; ok.title = "Add to the plan";
        ok.addEventListener("click", () => {
          ok.disabled = true;
          app.conn.choose({ choice_type: "sfx",
            payload: { suggestion_id: s.suggestion_id, suggestion_action: "accept" } });
        });
        const no = el("button", "sfx-ghost__btn no", '<i class="ti ti-x"></i>');
        no.type = "button"; no.title = "Dismiss";
        no.addEventListener("click", () => {
          no.disabled = true;
          app.conn.choose({ choice_type: "sfx",
            payload: { suggestion_id: s.suggestion_id, suggestion_action: "reject" } });
        });
        act.appendChild(ok); act.appendChild(no);
        row.appendChild(act);
        ghosts.appendChild(row);
      });
      card.appendChild(ghosts);
    }

    // Rendered variants (A/B) — each playable + selectable.
    if (variants.length) {
      const grid = el("div", "sfx-variants");
      variants.forEach((v) => grid.appendChild(sfxVariantCard(v, sfx.selected_variant_id)));
      card.appendChild(grid);
    }

    // Actions: generate the first take, or add another variant to compare.
    const firstTake = !variants.length;
    const btn = el("button", "btn-primary sfx-card__gen",
      '<i class="ti ti-wand"></i> ' + (firstTake ? "Generate sound effects" : "Add another variant"));
    if (pending) { btn.disabled = true; btn.innerHTML = '<span class="gspin"></span> Rendering…'; }
    else btn.addEventListener("click", () => {
      confirmSpend(
        firstTake ? "Generate the sound effects?" : "Render another variant?",
        "This renders the spotted effects onto your video — a moment, and it spends to generate.",
        () => {
          btn.disabled = true;
          startThinking();
          app.conn.choose({ choice_type: "sfx", payload: {} });
        }
      );
    });
    card.appendChild(btn);
    // The plan is EDITABLE (the backend takes user rows via choice "sfx" +
    // sfx_events; plan_only saves free) — iterate+ viewers get the editor.
    const canDirect = !(window.EdennCollab && window.EdennCollab.viewerRole) ||
      ["iterate", "owner"].indexOf(window.EdennCollab.viewerRole()) >= 0;
    if (!pending) {
      // Rendered for everyone, VISIBLE per role — the card can render before
      // the collab module adopts the persona (resume race), and gateByRole
      // re-toggles this row once the viewer's real role is known.
      const row = el("div", "sfx-card__tools");
      row.hidden = !canDirect;
      const edit = el("button", "btn-ghost sfx-card__edit",
        '<i class="ti ti-pencil"></i> Edit plan');
      edit.type = "button";
      edit.addEventListener("click", () => openSfxEditor(card, sfx));
      row.appendChild(edit);
      // The hybrid proposers, surfaced: deterministic styled transitions on
      // the cuts, and LLM ideas beyond what's visible. Proposing is free.
      const suggT = el("button", "btn-ghost sfx-card__suggest",
        '<i class="ti ti-wand"></i> Suggest transitions');
      suggT.type = "button";
      suggT.addEventListener("click", () => {
        suggT.disabled = true;
        startThinking();
        app.conn.choose({ choice_type: "sfx",
          payload: { sfx_suggest: "stylistic", style: "cinematic" } });
      });
      row.appendChild(suggT);
      const suggN = el("button", "btn-ghost sfx-card__suggest",
        '<i class="ti ti-bulb"></i> Ideas beyond the frame');
      suggN.type = "button";
      suggN.addEventListener("click", () => {
        suggN.disabled = true;
        startThinking();
        app.conn.choose({ choice_type: "sfx", payload: { sfx_suggest: "narrative" } });
      });
      row.appendChild(suggN);
      card.appendChild(row);
    }
    return card;
  }

  // ----- SFX plan editor ----------------------------------------------------
  function openSfxEditor(card, sfx) {
    if (card.querySelector(".sfx-editor")) return;
    const plan = card.querySelector(".sfx-plan");
    const controls = card.querySelectorAll(".sfx-card__gen, .sfx-card__edit");
    if (plan) plan.hidden = true;
    controls.forEach((b) => (b.hidden = true));
    const restore = () => {
      ed.remove();
      if (plan) plan.hidden = false;
      controls.forEach((b) => (b.hidden = false));
    };
    const ed = el("div", "sfx-editor");
    const rows = el("div", "sfx-editor__rows");
    const mkRow = (ev) => {
      const row = el("div", "sfx-editor__row");
      const t = document.createElement("input");
      t.type = "number"; t.min = "0"; t.step = "0.5";
      t.value = String(ev.start_s || 0);
      t.className = "sfx-editor__t";
      t.setAttribute("aria-label", "Start time (seconds)");
      const label = document.createElement("input");
      label.type = "text";
      label.value = ev.label || ev.prompt || "";
      label.placeholder = "What should we hear?";
      label.className = "sfx-editor__label";
      label.setAttribute("aria-label", "Effect");
      const del = el("button", "sfx-editor__del", '<i class="ti ti-x"></i>');
      del.type = "button"; del.title = "Remove this effect";
      del.addEventListener("click", () => row.remove());
      row._src = ev;
      row.appendChild(t); row.appendChild(label); row.appendChild(del);
      return row;
    };
    (sfx.events || []).forEach((ev) => rows.appendChild(mkRow(ev)));
    ed.appendChild(rows);
    const add = el("button", "btn-ghost sfx-editor__add", '<i class="ti ti-plus"></i> Add effect');
    add.type = "button";
    add.addEventListener("click", () => {
      const row = mkRow({ start_s: 0, label: "" });
      rows.appendChild(row);
      row.querySelector(".sfx-editor__label").focus();
    });
    ed.appendChild(add);
    const ambRow = el("div", "sfx-editor__amb");
    ambRow.appendChild(el("span", "sfx-editor__amblb", '<i class="ti ti-wind"></i> Ambience'));
    const amb = document.createElement("input");
    amb.type = "text";
    amb.value = sfx.ambience || "";
    amb.placeholder = "Optional continuous bed (e.g. distant city hum)";
    amb.setAttribute("aria-label", "Ambience");
    ambRow.appendChild(amb);
    ed.appendChild(ambRow);
    const foot = el("div", "sfx-editor__foot");
    const cancel = el("button", "btn-ghost", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", restore);
    const save = el("button", "btn-primary", "Save plan");
    save.type = "button";
    save.addEventListener("click", () => {
      const events = [...rows.children].map((row) => {
        const src = row._src || {};
        const start = parseFloat(row.querySelector(".sfx-editor__t").value) || 0;
        const label = row.querySelector(".sfx-editor__label").value.trim();
        if (!label) return null;
        const out = { start_s: Math.max(0, start), label };
        // An untouched label keeps its crafted prompt + reason; an edited
        // label IS the new prompt.
        if (src.label === label) {
          if (src.prompt) out.prompt = src.prompt;
          if (src.reason) out.reason = src.reason;
        }
        return out;
      }).filter(Boolean);
      const ambience = amb.value.trim();
      if (!events.length && !ambience) {
        toast("Keep at least one effect — or set an ambience bed for an ambience-only plan.");
        return;
      }
      startThinking();
      app.conn.choose({
        choice_type: "sfx",
        payload: { sfx_events: events, sfx_ambience: ambience, plan_only: true },
      });
      restore(); // the refreshed snapshot repaints the card with the new rows
    });
    foot.appendChild(cancel);
    foot.appendChild(save);
    ed.appendChild(foot);
    card.appendChild(ed);
  }

  function sfxVariantCard(v, selectedId) {
    const isSel = v.variant_id === selectedId;
    const completed = v.status === "completed" && (v.audio_url || v.video_url);
    const failed = v.status === "failed" || v.status === "error";
    const card = el("div", "sfx-variant" + (isSel ? " sel" : ""));
    card.appendChild(el("div", "sfx-variant__title", esc(v.label || "SFX take")));
    if (completed && v.video_url) {
      // A rendered SFX pass is a VIDEO (effects muxed onto the clip) — show it
      // watchable inline in a compact glass well, same family as music takes.
      const thumb = el("div", "sfx-variant__video");
      const vid = document.createElement("video");
      vid.src = mediaSrc(v.video_url); vid.controls = true; vid.playsInline = true; vid.preload = "metadata";
      thumb.appendChild(vid);
      card.appendChild(thumb);
    }
    if (completed) {
      const url = v.audio_url || v.video_url;
      const row = el("div", "gplay-row");
      const play = el("button", "gplay", '<i class="ti ti-player-play"></i>');
      const audio = typeof Audio !== "undefined" ? new Audio(mediaSrc(url)) : null;
      play.addEventListener("click", () => togglePlay(audio, play));
      row.appendChild(play);
      row.appendChild(miniWave(10, true));
      card.appendChild(row);
      const pick = el("button", "gsel " + (isSel ? "chosen" : "use"), isSel ? "Selected ✓" : "Use this");
      if (!isSel) pick.addEventListener("click", () => {
        pick.disabled = true;
        app.conn.choose({ choice_type: "sfx", payload: { select_variant_id: v.variant_id } });
      });
      card.appendChild(pick);
      if (v.video_url) {
        const dl = el("button", "gsel ghost", '<i class="ti ti-download"></i> Download');
        dl.addEventListener("click", () => deliverFile(v.video_url, "edenn-sfx.mp4"));
        card.appendChild(dl);
      }
      // Which product this take is. Sound designed to the picture and sound
      // designed to a DESCRIPTION of the picture are different things, and the
      // fallback between them used to be visible only in a server log.
      if (v.watched_the_video === false) {
        const note = el("div", "sfx-variant__note",
          "Written from prompts — the engine did not watch the footage"
          + (v.not_watched_reason ? " (" + esc(v.not_watched_reason) + ")" : ""));
        card.appendChild(note);
      }
      const faults = (v.listen_report && v.listen_report.notes) || [];
      if (faults.length) {
        card.appendChild(el("div", "sfx-variant__note sfx-variant__note--warn",
          esc(faults[0])));
      }
    } else if (failed) {
      card.appendChild(el("div", "gstatus gstatus--err",
        '<i class="ti ti-alert-triangle"></i> Render didn\'t finish'));
    } else {
      const stt = el("div", "gstatus");
      stt.appendChild(el("span", "gspin"));
      stt.appendChild(document.createTextNode(v.status === "processing" ? "Rendering…" : "Queued…"));
      card.appendChild(stt);
    }
    return card;
  }

  // ----- mix panel (music + voice-over) -----------------------------------
  function renderMixPanel(st) {
    const vo = (st.layers || {}).voiceover;
    const hasVoiceover = !!(vo && vo.status === "completed");
    const sfxLayer = (st.layers || {}).sfx;
    const hasSfx = !!(sfxLayer && typeof sfxLayer === "object" && !Array.isArray(sfxLayer)
      && (sfxLayer.variants || []).some((v) => v.status === "completed" && v.audio_url));
    const cands = st.candidates || [];
    // The mix acts on ONE track: the locked/selected candidate, else the most
    // recent rendered one. Without a target there's nothing to balance — so we
    // don't show a target-less panel (which produced "no music track to adjust").
    const completed = cands.filter((c) => c.status === "completed" && (c.audio_url || c.video_url));
    const target = cands.find((c) => c.candidate_id === st.selected_candidate_id)
      || completed[completed.length - 1] || null;
    const ready = !!st.mix || hasVoiceover || hasSfx || !!target;
    if (!ready) { clearBlock("mix"); return; }
    // Re-render only on material change, and never while the panel holds focus:
    // a 2.5s poll rebuilding the card mid-drag loses the gesture.
    const key = JSON.stringify([st.mix, st.selected_candidate_id, hasVoiceover, hasSfx,
      target && target.music_volume]);
    if (app._blocks.mix && app._mixKey === key) return;
    if (app._blocks.mix && app._blocks.mix.contains(document.activeElement)) return;
    app._mixKey = key;
    placeBlock("mix", mixPanel(st, hasVoiceover, target, hasSfx));
  }

  function mixPanel(st, hasVoiceover, target, hasSfx) {
    const mix = st.mix || {};
    // Current music level / keep-original come from the composed mix when present,
    // else the target music candidate.
    const sel = target || {};
    const targetId = sel.candidate_id || null;
    const musicVol = mix.music_volume != null ? mix.music_volume
      : (sel.music_volume != null ? sel.music_volume : 0.85);
    const keepOriginal = mix.preserve_original_audio != null ? mix.preserve_original_audio
      : !!sel.preserve_original_audio;
    const card = el("div", "mix-card");
    card.appendChild(el("div", "mix-card__hd", '<i class="ti ti-adjustments-horizontal"></i> Mix'));
    // Each slider maps to ONE typed mix parameter. We send a structured `mix`
    // /choices frame (param -> value) rather than free-text the backend re-parses
    // — a deterministic, typed co-design contract. `toValue` converts the integer
    // slider position to the backend's units. The voice-over / ducking / start
    // sliders only show when a voice-over exists; music-only sessions show Music.
    // The Music slider only makes sense with a music track to balance. A
    // voice-over-only session (targetId null) would otherwise show a Music
    // slider whose change dispatches candidate_id:null — a silent no-op.
    const sliders = [];
    if (targetId) {
      sliders.push(["Music", Math.round(musicVol * 100), 0, 100, "%", "music_volume", (v) => v / 100]);
    }
    if (hasVoiceover) {
      sliders.push(
        ["Voice-over", Math.round((mix.voiceover_volume != null ? mix.voiceover_volume : 1.0) * 100), 0, 150, "%",
          "voiceover_volume", (v) => v / 100],
        ["Ducking", Math.round(mix.duck_gain_db != null ? mix.duck_gain_db : -9), -24, 0, " dB",
          "duck_gain_db", (v) => v],
      );
      // Segmented narration bakes its own timing — a global start shift would
      // fight the plan, so the slider only shows for flat scripts.
      const voLayer = (st.layers || {}).voiceover || {};
      if (!(voLayer.segments || []).length) {
        sliders.push(["Narration starts", Math.round(mix.voiceover_start_s != null ? mix.voiceover_start_s : 0), 0, 15, "s",
          "voiceover_start_s", (v) => v]);
      }
    }
    if (hasSfx) {
      sliders.push(["Sound FX", Math.round((mix.sfx_volume != null ? mix.sfx_volume : 1.0) * 100), 0, 150, "%",
        "sfx_volume", (v) => v / 100]);
    }
    sliders.forEach(([label, val, min, max, unit, paramKey, toValue]) => {
      const row = el("div", "mix-row");
      const head = el("div", "mix-row__head");
      head.appendChild(el("span", "mix-row__label", label));
      const out = el("span", "mix-row__val", val + unit);
      head.appendChild(out);
      row.appendChild(head);
      const slider = el("input", "mix-slider");
      slider.setAttribute("aria-label", label);
      slider.type = "range"; slider.min = String(min); slider.max = String(max); slider.value = String(val);
      slider.addEventListener("input", () => { out.textContent = slider.value + unit; });
      slider.addEventListener("change", () => {
        clearTimeout(slider._t);
        slider._t = setTimeout(
          () => app.conn.choose({ choice_type: "mix", payload: { candidate_id: targetId, [paramKey]: toValue(Number(slider.value)) } }),
          200
        );
      });
      row.appendChild(slider);
      card.appendChild(row);
    });
    // Keep-original-audio toggle (music sits under the video's own talking track).
    // A common ask for talking-head / vlog footage; backend already supports it.
    const toggle = el("label", "mix-toggle");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "mix-toggle__cb";
    cb.checked = !!keepOriginal;
    cb.addEventListener("change", () =>
      app.conn.choose({ choice_type: "mix", payload: { candidate_id: targetId, preserve_original_audio: cb.checked } })
    );
    toggle.appendChild(cb);
    toggle.appendChild(el("span", "mix-toggle__label", "Keep original audio"));
    card.appendChild(toggle);
    if (mix.video_url) {
      // The mix IS a video (the layers muxed onto the clip) — a compact
      // warm-glass media well, not an audio toggle and not a letterbox wall.
      const prev = el("div", "mix-preview");
      prev.appendChild(el("div", "mix-preview__hd",
        '<i class="ti ti-player-play"></i> Mix preview' +
        ' <span class="mix-preview__hint">— the layers on your video</span>'));
      const well = el("div", "mix-preview__well");
      const v = document.createElement("video");
      v.src = mediaSrc(mix.video_url); v.controls = true; v.playsInline = true; v.preload = "metadata";
      const poster = (st.observation || {}).thumbnail_url || (st.source_video || {}).poster_url;
      if (poster) v.poster = mediaSrc(poster);
      well.appendChild(v);
      prev.appendChild(well);
      card.appendChild(prev);
    }
    return card;
  }

  function renderFinal(st) {
    const output = window.EdennState.resolveOutput(st);
    const exportBtn = $("tb-export");
    if (exportBtn) {
      // Greyed out with nothing said is a refusal the user has to guess at.
      // The button stays pressable and its handler explains what is missing;
      // the title says it before they press.
      exportBtn.disabled = false;
      exportBtn.title = output.url
        ? "Download the finished mix"
        : "Finish a mix first — then you can export it.";
      exportBtn.classList.toggle("is-idle", !output.url);
    }
    if (!output.url) {
      // The backend records a final_artifact the moment a compose is PLANNED.
      // Silence here would hide a running (or failed) render behind a blank —
      // keep the receipt, and let it say what is actually happening.
      const fin = st.final_artifact;
      if (!fin) { clearBlock("final"); return; }
      const key = "pending:" + (fin.status || "working") + ":" + (fin.message || "");
      if (app._blocks.final && app._finalKey === key) return;
      app._finalKey = key;
      const box = el("div", "final completion-receipt");
      const failed = fin.status === "failed";
      box.appendChild(el("i", failed ? "ti ti-alert-triangle" : "ti ti-loader-2"));
      box.appendChild(el("span", "", failed
        ? "The final mix failed" + (fin.message ? " — " + esc(String(fin.message)) : ".")
        : "Finishing your mix — " + esc(String(fin.status || "working"))));
      placeBlock("final", box);
      return;
    }
    const key = JSON.stringify(output);
    if (app._blocks.final && app._finalKey === key) return;
    app._finalKey = key;
    const box = el("div", "final completion-receipt");
    box.appendChild(el("i", "ti ti-circle-check"));
    box.appendChild(el("span", "", output.kind === "video" ? "Video mix ready" : "Audio ready"));
    // A button, not a bare link: the file may need a token, and a blocked popup
    // that does nothing at all is how Download stopped meaning anything.
    const save = el("button", "btn-ghost", "Export");
    save.type = "button";
    save.addEventListener("click", () => deliverFile(output.url,
      output.kind === "video" ? "edenn-final-mix.mp4" : "edenn-final-mix.mp3"));
    box.appendChild(save);
    placeBlock("final", box);
  }

  function maybePoll(st) {
    const pending = (st.candidates || []).some((c) => c.status === "queued" || c.status === "processing");
    const vo = st.layers && st.layers.voiceover;
    const voPending = vo && (vo.status === "queued" || vo.status === "processing");
    const sfx = st.layers && st.layers.sfx;
    const sfxPending = sfx && typeof sfx === "object" && (sfx.variants || [])
      .some((v) => v.status === "queued" || v.status === "processing");
    if ((pending || voPending || sfxPending) && !app.pollTimer && app.transport && app.sessionId) {
      app.pollTimer = setTimeout(async () => {
        app.pollTimer = null;
        try {
          const revision = app.requests.revision;
          const snap = await app.transport.getSnapshot(app.sessionId);
          onEvent({ event_type: "session.opened", source: "refresh", revision, session_id: app.sessionId, payload: { snapshot: snap } });
        } catch (e) { /* keep the UI; next turn will refresh */ }
      }, 2500);
    }
  }

  /**
   * Hand a rendered file to the user, or say why we cannot.
   *
   * `window.open` on a media URL is a popup the browser may block outright, and
   * when it is blocked nothing happens at all — the user clicks Download and the
   * app appears to ignore them. An anchor with `download` is a real save, and if
   * the browser still refuses, we say so instead of failing silently.
   */
  function deliverFile(url, suggestedName) {
    if (!url) {
      toast("There's nothing rendered to download yet.");
      return;
    }
    try {
      url = (window.__edennMediaSrc ? window.__edennMediaSrc(url) : url);
      const a = document.createElement("a");
      a.href = url;
      a.rel = "noopener";
      a.target = "_blank";
      if (suggestedName) a.download = suggestedName;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (_) {
      const win = window.open(url, "_blank");
      if (!win) toast("Your browser blocked the download — allow pop-ups for this page, or long-press the player to save.");
    }
  }

  function confirmSpend(title, body, onConfirm, okLabel) {
    // One spend at a time: a confirm raised mid-turn queues a second generation
    // behind a turn the user has not seen the result of yet.
    if (app._thinking) { toast("Wait for the current request to finish."); return; }
    const ov = $("confirm-overlay");
    const previous = document.activeElement;
    $("confirm-title").textContent = title;
    $("confirm-body").textContent = body;
    // The button has to agree with the body. A dialog that explains a render
    // and then offers "Yes, generate" makes the user guess which one is true.
    $("confirm-ok").textContent = okLabel || "Yes, generate";
    ov.hidden = false;
    $("session").inert = true;
    $("entrance").inert = true;
    const ok = $("confirm-ok"), cancel = $("confirm-cancel");
    const close = () => {
      ov.hidden = true;
      $("session").inert = false; $("entrance").inert = false;
      ok.onclick = null; cancel.onclick = null;
      ov.removeEventListener("keydown", onKey);
      if (previous && previous.isConnected) previous.focus({ preventScroll: true });
    };
    const onKey = (event) => {
      if (event.key === "Escape") { event.preventDefault(); close(); }
      if (event.key === "Tab") {
        event.preventDefault();
        (document.activeElement === cancel ? ok : cancel).focus();
      }
    };
    ov.addEventListener("keydown", onKey);
    ok.onclick = () => { close(); onConfirm(); };
    cancel.onclick = close;
    cancel.focus();
  }

  function clockText(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    return Math.floor(total / 60) + ":" + String(total % 60).padStart(2, "0");
  }

  // Re-cut a take from a different point in its own full track. FREE — it
  // re-presents audio the session already paid for — so it gets a scrub control
  // rather than a spend confirmation. Until this existed the verb was reachable
  // only by guessing the words in chat, which is the same as not shipping it.
  function requestSculpt(candidate) {
    const ov = $("sculpt-overlay");
    const win = candidate.window || {};
    const fullTrack = Number(win.full_duration_s || 0);
    const takeLength = Number(
      (candidate.listen_report && candidate.listen_report.measured
        && candidate.listen_report.measured.cut_duration_s) || 0
    );
    // Never offer a start so late that the music runs out before the picture
    // does: that is the truncation failure, arriving through a new door.
    const latest = fullTrack && takeLength ? Math.max(0, fullTrack - takeLength) : fullTrack;
    if (!ov || !latest) {
      // No longer track behind this take (the basic tier renders video-length
      // audio directly). Say so where the user clicked.
      app.conn.send("Can this take start somewhere else in the track?");
      return;
    }

    const range = $("sculpt-range"), readout = $("sculpt-readout");
    range.max = String(latest.toFixed(1));
    range.value = String(Number(win.start_s || 0));
    const paint = () => {
      readout.textContent = "Starts at " + clockText(range.value)
        + " of " + clockText(fullTrack);
    };
    paint();
    range.oninput = paint;
    ov.hidden = false;

    const ok = $("sculpt-ok"), cancel = $("sculpt-cancel");
    const close = () => { ov.hidden = true; ok.onclick = null; cancel.onclick = null; range.oninput = null; };
    cancel.onclick = close;
    ok.onclick = () => {
      const startS = Number(range.value);
      close();
      startThinking();
      app.conn.choose({
        choice_type: "sculpt",
        target_id: candidate.candidate_id,
        payload: { window_start_s: startS },
      });
    };
  }

  // Put the finished takes side by side on what they SOUND like. Free, and it
  // answers the question a user with more than one take actually has.
  function requestCompare() {
    startThinking();
    app.conn.choose({ choice_type: "compare", payload: {} });
  }

  // Ask for one line of direction, then act on it. Returns nothing; the work
  // happens in the callback, like confirmSpend above.
  function askFor({ title, body, placeholder, okLabel, value }, onAnswer) {
    const ov = $("ask-overlay");
    if (!ov) { const typed = window.prompt(title, value || ""); if (typed) onAnswer(typed); return; }
    $("ask-title").textContent = title;
    $("ask-body").textContent = body || "";
    const input = $("ask-input");
    input.placeholder = placeholder || "";
    input.value = value || "";
    $("ask-ok").textContent = okLabel || "Go ahead";
    ov.hidden = false;
    input.focus({ preventScroll: true });
    const ok = $("ask-ok"), cancel = $("ask-cancel");
    const close = () => { ov.hidden = true; ok.onclick = null; cancel.onclick = null; input.onkeydown = null; };
    const submit = () => { const text = input.value.trim(); if (!text) return; close(); onAnswer(text); };
    ok.onclick = submit;
    cancel.onclick = close;
    input.onkeydown = (e) => { if (e.key === "Enter") submit(); if (e.key === "Escape") close(); };
  }

  // Restyle: the SAME piece of music in a different style. A real
  // audio-to-audio edit on the tiers that support it, and an honest fallback
  // to a fresh take on the ones that do not — which the take records.
  function requestRestyle(candidate) {
    askFor({
      title: "Restyle this take",
      body: "Describe the new style. It keeps this take as its starting point.",
      placeholder: "lo-fi, warm tape, mellow drums",
      okLabel: "Restyle it",
    }, (prompt) => {
      confirmSpend(
        "Restyle this take?",
        "This makes a new version from this track — a minute or two, and it spends to generate.",
        () => {
          startThinking();
          app.conn.choose({
            choice_type: "variation",
            target_id: candidate.candidate_id,
            payload: { edit_kind: "creative_edit", prompt },
          });
        }
      );
    });
  }

  // Longer. Said plainly: this is not yet an extension of THIS track, it is a
  // new take built to the longer length. Offering it without saying so is how
  // a user asks for four more seconds of their music and is handed different
  // music instead.
  function requestLonger(candidate) {
    askFor({
      title: "Make it longer",
      body: "How long should it be, in seconds?",
      placeholder: "40",
      okLabel: "Continue",
      value: "",
    }, (seconds) => {
      const target = Number(String(seconds).replace(/[^0-9.]/g, ""));
      if (!target) return;
      confirmSpend(
        "Generate a longer take?",
        "This will be a NEW take built to about " + Math.round(target) +
          "s — not this one continued. It spends to generate.",
        () => {
          startThinking();
          app.conn.choose({
            choice_type: "variation",
            target_id: candidate.candidate_id,
            payload: { edit_kind: "extend", extend_seconds: target },
          });
        },
        "Yes, generate it"
      );
    });
  }

  // Branch a fresh take from a candidate (structured 'variation' choice). Spends,
  // so it goes through the same confirm as music/voice-over generation.
  function requestVariation(candidate, btn, onSent) {
    confirmSpend(
      "Generate another take?",
      "This uses your generation credits to create another take.",
      () => {
        if (btn) btn.disabled = true;
        startThinking(); // immediate feedback while the branch turn runs
        app.conn.choose({ choice_type: "variation", target_id: candidate.candidate_id, payload: {} });
        // Fires only when the choice is actually sent (not on Cancel) — canvas mode
        // uses this to arm HEAD-advance without latching on a cancelled branch.
        if (typeof onSent === "function") onSent();
      }
    );
  }

  function renderMessage(m) {
    if (m.role === "user") {
      const row = el("div", "user-row");
      row.dataset.speaker = "user";
      row.appendChild(el("div", "ub", esc(m.content)));
      return row;
    }
    const row = el("div", "agent-row");
    row.setAttribute("aria-label", "Edenn");
    const body = el("div", "abody");
    const name = el("div", "aname", "Edenn");
    body.appendChild(name);
    const activity = app._activityForReply;
    if (m.content && activity && activity.wrap.isConnected) {
      activity.wrap.classList.add("is-inline");
      name.appendChild(activity.wrap);
      app._activityForReply = null;
    }
    const text = el("div", "atext");
    body.appendChild(text);
    window.EdennElements.message(text, String(m.content || ""));
    if (!m.content) row.hidden = true;
    row.appendChild(body);
    return row;
  }

  function renderClarify(pending) {
    const wrap = el("div", "clarify");
    const row = el("div", "agent-row");
    row.setAttribute("aria-label", "Edenn");
    const body = el("div", "abody");
    body.appendChild(el("div", "aname", "Edenn"));
    body.appendChild(el("div", "clarify__q", esc(pending.question)));

    const options = pending.options || [];
    // The real backend tags the modality gate as `gate:"intent"` and topical
    // cards (e.g. the SFX treatment) with `topic`. The intent gate is a
    // three-way-plus fork over what is really a SET of layers, so it renders
    // as a multi-select: checkboxes carry the same information as exclusive
    // cards (see layerChoiceId), and there is somewhere to put a third layer.
    // Other topical/hinted questions keep the card grid / chip fallbacks.
    const asCards = pending.topic === "sfx_treatment" ||
      (options.length <= 4 && options.some((o) => o.hint));
    if (pending.gate === "intent") {
      body.appendChild(layerPicker(options));
    } else if (asCards) {
      const grid = el("div", "cards-row");
      const icons = { full_audio: "ti-stack-2", music_only: "ti-music", voiceover_only: "ti-microphone", sound_design: "ti-wave-square" };
      const topicIcon = pending.topic === "sfx_treatment" ? "ti-wave-square" : "ti-bulb";
      options.forEach((o) => {
        const card = el("button", "intent-card");
        if (o.recommended) card.classList.add("is-recommended");
        card.appendChild(el("div", "intent-card__ic", `<i class="ti ${icons[o.id] || topicIcon}"></i>`));
        card.appendChild(el("div", "intent-card__title", esc(o.label)));
        if (o.hint) card.appendChild(el("div", "intent-card__desc", esc(o.hint)));
        if (o.recommended) card.appendChild(el("div", "intent-card__pick", "My pick"));
        card.addEventListener("click", () => chooseClarify(o.id, o.label, card, grid));
        grid.appendChild(card);
      });
      body.appendChild(grid);
    } else {
      const chips = el("div", "qchips");
      options.forEach((o) => {
        const chip = el("button", "qchip", esc(o.label) +
          (o.recommended ? ' <span class="qchip__pick">my pick</span>' : ""));
        if (o.recommended) chip.classList.add("is-recommended");
        chip.addEventListener("click", () => chooseClarify(o.id, o.label, chip, chips));
        chips.appendChild(chip);
      });
      body.appendChild(chips);
    }
    row.appendChild(body);
    wrap.appendChild(row);
    return wrap;
  }

  // ========================================================================
  // Right-pane view switching.
  //
  // The shell is fixed — chat on the left, work on the right — so the toggle
  // selects a module rather than swapping a layout. It lives here, in the
  // console controller that owns the shell, instead of inside either module:
  // when canvas-mode owned it, "turn canvas on" and "turn chat off" were the
  // same operation, and there was nowhere for a third view to attach.
  // ========================================================================
  function setRightView(view) {
    const v = view === "canvas" ? "canvas" : "timeline";
    app.rightView = v;
    // Self-healing against the transform takeover: selecting a view means the
    // toggle owns the pane again, so the header must be visible and the cut
    // rail gone (idempotent when no transform session ever ran).
    const hd = $("rpane-hd"); if (hd) hd.hidden = false;
    const xr = $("xrail"); if (xr) xr.hidden = true;
    const session = $("session");
    if (session) {
      session.classList.toggle("is-view-canvas", v === "canvas");
      session.classList.toggle("is-view-timeline", v === "timeline");
    }
    document.querySelectorAll("#view-seg .vseg__btn").forEach((b) => {
      const on = b.getAttribute("data-view") === v;
      b.classList.toggle("is-on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    if (window.EdennTimeline) window.EdennTimeline.setView(v);
    if (window.EdennCanvas) window.EdennCanvas.setView(v);
    const pane = document.querySelector(".rpane");
    if (pane && !window.EdennUI.reducedMotion()) {
      if (app._paneAnimation) app._paneAnimation.cancel();
      app._paneAnimation = pane.animate([{ opacity: .65 }, { opacity: 1 }], { duration: 160, easing: "ease-out" });
    }
  }

  // Director's notes — the session memory the model actually steers by
  // (creative direction, style keywords, avoids, preferences). Rendered so
  // what's remembered is VISIBLE; plain chat rewrites it any time. Used to live
  // in the old right rail; it is session state, not time data, so with the rail
  // gone it rides the thread as a widget like everything else.
  function renderDirNotes(st) {
    const mem = st.memory || {};
    const kw = (mem.style_keywords || []).filter(Boolean);
    const avoid = (mem.avoid || []).filter(Boolean);
    // `modelspec` is deliberately NOT listed. It is DERIVED (approved proposal ->
    // first take -> the analysis default), and printed as a key/value in a notes
    // panel it reads like a setting the user chose. On the deployed console it
    // said "edenn_basic" while the canvas beside it showed the Enhanced takes
    // that had actually rendered and billed. A take's own badge is the honest
    // place to read a tier.
    const prefs = (mem.preferences && typeof mem.preferences === "object")
      ? Object.entries(mem.preferences)
          .filter(([k]) => k !== "modelspec")
          .filter(([, v]) => v != null && String(v).trim() !== "")
      : [];
    const direction = String(mem.creative_direction || "").trim();
    if (!direction && !kw.length && !avoid.length && !prefs.length) {
      clearBlock("dirnotes");
      app._dirNotesKey = null;
      return;
    }
    const key = JSON.stringify([direction, kw, avoid, prefs]);
    if (app._blocks.dirnotes && app._dirNotesKey === key) return;
    app._dirNotesKey = key;
    const notes = el("div", "dir-notes");
    notes.appendChild(el("div", "dir-notes__hd",
      '<i class="ti ti-notebook"></i> Director’s notes'));
    if (direction) notes.appendChild(el("div", "dir-notes__line", esc(direction)));
    if (kw.length) {
      const row = el("div", "dir-notes__chips");
      kw.slice(0, 8).forEach((k) => row.appendChild(el("span", "dir-notes__chip", esc(String(k)))));
      notes.appendChild(row);
    }
    if (avoid.length) {
      const row = el("div", "dir-notes__chips");
      avoid.slice(0, 8).forEach((k) => row.appendChild(
        el("span", "dir-notes__chip is-avoid", '<i class="ti ti-ban"></i> ' + esc(String(k)))));
      notes.appendChild(row);
    }
    prefs.slice(0, 6).forEach(([k, v]) => {
      notes.appendChild(el("div", "dir-notes__pref",
        "<b>" + esc(String(k).replace(/_/g, " ")) + ":</b> " + esc(String(v))));
    });
    notes.appendChild(el("div", "dir-notes__hint", "Steer these any time — just say it in chat."));
    placeBlock("dirnotes", notes);
  }

  function wireViewToggle() {
    const seg = $("view-seg");
    if (!seg) return;
    seg.querySelectorAll(".vseg__btn").forEach((b) => {
      b.addEventListener("click", () => setRightView(b.getAttribute("data-view")));
    });
    setRightView("timeline");
  }

  // ---- live, inline, per-turn "thinking" stream (per-turn activity) -------
  // Each agent.reasoning beat is rendered the instant it streams in, into ONE
  // block for the current turn. A fresh block opens on the next turn, so every
  // Q&A gets its own visible reasoning. A turn that produces no reasoning has
  // its (empty) block removed on finalize.
  function startThinking(force = true) {
    if (app._thinking) return app._thinking;
    app._activityForReply = null;
    const wrap = el("div", "cot");
    wrap.dataset.responseStart = "true";
    wrap.setAttribute("aria-label", "Activity");
    wrap.setAttribute("aria-live", "off");
    const t = app._thinking = { wrap, id: "activity-" + (++app._activityId), steps: [],
      started: Date.now(), manual: false, open: false, outcome: "active", spinning: false,
      label: "Thinking…", expandedHistory: false };
    inner().appendChild(wrap);
    t.spinnerTimer = setTimeout(() => { if (t.outcome === "active") { t.spinning = true; paintActivity(t); } }, 150);
    t.stallTimer = setTimeout(() => {
      if (t.outcome === "active") { t.message = "Still waiting for a response. Your request is in progress."; paintActivity(t); }
    }, 45000);
    paintActivity(t);
    $("session-send").disabled = true;
    announceActivity("Working on your request");
    scrollThread(force);
    return t;
  }
  function paintActivity(t) {
    if (!window.EdennElements) return;
    window.EdennElements.activity(t.wrap, { ...t,
      onToggle: (open) => { holdDisclosurePosition(t.wrap); t.manual = true; setActivityOpen(t, open); },
      onHistory: () => { holdDisclosurePosition(t.wrap); t.manual = true; t.expandedHistory = !t.expandedHistory; paintActivity(t); },
      onRetry: t.outcome !== "active" && t.outcome !== "complete" && t.command ? () => {
        const { kind, frame } = t.command;
        if (kind === "send") { $("session-input").value = frame.content || ""; $("session-input").focus(); return; }
        confirmSpend("Retry this action?", "Review the current results first. Retrying a generation can use credits again.", () => {
          dispatchRequest(kind, { ...frame, request_id: undefined }, t.requestId);
        });
      } : undefined,
    });
  }
  function announceActivity(text) {
    clearTimeout(app._announcementTimer);
    app._announcementTimer = setTimeout(() => { $("activity-status").textContent = text; }, 120);
  }
  function setActivityOpen(t, open) {
    t.open = open; t.wrap.classList.toggle("is-collapsed", !open); paintActivity(t);
  }
  function appendThinking(payload) {
    const t = app._thinking || startThinking(false);
    window.EdennState.upsertStep(t.steps, payload || {});
    t.label = (payload && payload.status) || "Working";
    t.message = "";
    paintActivity(t); announceActivity(t.label); scrollThread();
  }
  function finalizeThinking(outcome = "complete", message = "") {
    const t = app._thinking;
    if (!t) return;
    app._thinking = null;
    clearTimeout(t.spinnerTimer); clearTimeout(t.stallTimer);
    if (t.requestId) app.requests.finish(t.requestId, outcome);
    $("session-send").disabled = false;
    t.outcome = outcome; t.spinning = false; t.message = message;
    t.steps.forEach(step => { if (step.status === "active") step.status = "complete"; });
    announceActivity(outcome === "complete" ? "Request complete" : message);
    if (!t.steps.length && outcome === "complete") t.wrap.remove();
    else {
      t.wrap.classList.add(outcome === "complete" ? "is-done" : "is-error");
      const seconds = Math.max(1, Math.round((Date.now() - t.started) / 1000));
      const elapsed = seconds < 60 ? seconds + "s" : Math.floor(seconds / 60) + "m" + (seconds % 60 ? " " + seconds % 60 + "s" : "");
      t.label = outcome === "complete" ? "Worked for " + elapsed
        : outcome === "interrupted" ? "Interrupted" : "Could not finish";
      if (outcome !== "complete") t.open = true;
      else app._activityForReply = t;
      t.wrap.classList.toggle("is-collapsed", !t.open); paintActivity(t);
    }
    if (outcome === "complete") setTimeout(flushRequests, 0);
    else app.requestQueue = [];
  }

  function startAnalyzing() {
    startThinking();
    appendThinking({ status: "Analyzing your video" });
  }

  function compactChoice(node, title) {
    if (!node || node.dataset.receipt) return;
    const previousHeight = node.isConnected ? node.getBoundingClientRect().height : null;
    node.dataset.receipt = "true";
    const details = el("details", "choice-receipt");
    details.open = previousHeight != null;
    details.appendChild(el("summary", "", esc(title || "Previous choice")));
    const content = el("div", "choice-receipt__body");
    while (node.firstChild) content.appendChild(node.firstChild);
    details.appendChild(content); node.appendChild(details);
    if (previousHeight != null) toggleDisclosure(details, details.querySelector("summary"), previousHeight);
  }
  function layerName(id) { return ({ music: "Music", voiceover: "Voiceover", sfx: "Sound effects" })[id] || id; }
  function syncIdentity() {
    let hasIdentity = false;
    Array.from(inner().children).forEach((node) => {
      if (node.dataset.speaker === "user" || node.dataset.responseStart) hasIdentity = false;
      const row = node.matches(".agent-row") ? node : node.querySelector(".agent-row");
      if (row && !row.hidden) {
        row.classList.toggle("is-continuation", hasIdentity);
        hasIdentity = true;
      }
    });
  }
  function rememberReadingAnchor() {
    const thread = $("thread"), top = thread.getBoundingClientRect().top;
    const nodes = Array.from(inner().children);
    nodes.forEach(node => { if (!node.dataset.scrollKey) node.dataset.scrollKey = "row-" + (++app._scrollKey); });
    const node = nodes.find(item => item.getBoundingClientRect().bottom > top + 2);
    app._readingAnchor = node ? { key: node.dataset.scrollKey, offset: node.getBoundingClientRect().top - top } : null;
  }
  // A disclosure is a local reading interaction, never a request to follow output.
  function holdDisclosurePosition(node) {
    const thread = $("thread");
    app._jumpAnimation = false;
    app._followLatest = false;
    app._disclosureAnchor = { node, top: node.getBoundingClientRect().top };
    // Retain space below a collapsed widget so the browser cannot clamp scrollTop
    // and pull the clicked header away from the pointer near the end of the chat.
    inner().style.minHeight = Math.max(0, thread.scrollTop + thread.clientHeight) + "px";
    rememberReadingAnchor();
  }
  function releaseDisclosurePosition() {
    app._disclosureAnchor = null;
    inner().style.minHeight = "";
  }
  function toggleDisclosure(details, summary, previousHeight = null) {
    holdDisclosurePosition(summary);
    const opening = details._disclosureOpen == null ? !details.open : !details._disclosureOpen;
    details._disclosureOpen = opening;
    const from = previousHeight == null ? details.getBoundingClientRect().height : previousHeight;
    if (details._disclosureAnimation) details._disclosureAnimation.cancel();
    details.style.height = "";
    details.open = true;
    const to = opening ? details.getBoundingClientRect().height : summary.getBoundingClientRect().height
      + parseFloat(getComputedStyle(details).borderTopWidth) + parseFloat(getComputedStyle(details).borderBottomWidth);
    const finish = () => {
      details.open = opening;
      details.style.height = "";
      details.style.overflow = "";
      details._disclosureAnimation = null;
      details._disclosureOpen = null;
      scrollThread();
    };
    if (window.EdennUI.reducedMotion() || !details.animate) { finish(); return; }
    details.style.overflow = "hidden";
    details.style.height = from + "px";
    const animation = details.animate([{ height: from + "px" }, { height: to + "px" }],
      { duration: previousHeight == null ? 180 : 320, easing: "cubic-bezier(.2, .7, .2, 1)", fill: "forwards" });
    details._disclosureAnimation = animation;
    animation.onfinish = () => { finish(); animation.cancel(); };
  }
  function scrollThread(force = false) {
    if (force) { releaseDisclosurePosition(); app._followLatest = true; }
    syncIdentity();
    const thread = $("thread");
    app._programmaticScroll = true;
    if (app._disclosureAnchor && app._disclosureAnchor.node.isConnected) {
      thread.scrollTop += app._disclosureAnchor.node.getBoundingClientRect().top - app._disclosureAnchor.top;
    } else if (app._followLatest && !app._jumpAnimation) thread.scrollTop = thread.scrollHeight;
    else if (!app._followLatest && app._readingAnchor) {
      const anchor = Array.from(inner().children).find(node => node.dataset.scrollKey === app._readingAnchor.key);
      if (anchor) thread.scrollTop += anchor.getBoundingClientRect().top - thread.getBoundingClientRect().top - app._readingAnchor.offset;
    }
    rememberReadingAnchor(); updateScrollEdges();
    requestAnimationFrame(() => { app._programmaticScroll = false; });
  }
  function updateScrollEdges() {
    const thread = $("thread"), shell = $("thread-shell");
    const remaining = thread.scrollHeight - thread.clientHeight - thread.scrollTop;
    const focused = thread.contains(document.activeElement) ? document.activeElement.getBoundingClientRect() : null;
    const rect = thread.getBoundingClientRect();
    shell.classList.toggle("has-above", thread.scrollTop > 2 && !(focused && focused.top < rect.top + 24));
    shell.classList.toggle("has-below", remaining > 2 && !(focused && focused.bottom > rect.bottom - 24));
    $("jump-latest").hidden = app._followLatest || remaining < 40;
  }
  function watchThread() {
    const thread = $("thread");
    thread.addEventListener("scroll", () => {
      if (!app._programmaticScroll && !app._jumpAnimation) {
        if (app._disclosureAnchor) releaseDisclosurePosition();
        app._followLatest = thread.scrollHeight - thread.clientHeight - thread.scrollTop < 48;
        rememberReadingAnchor();
      }
      updateScrollEdges();
    }, { passive: true });
    thread.addEventListener("click", (event) => {
      const summary = event.target.closest("summary");
      if (!summary || !thread.contains(summary) || summary.parentElement.tagName !== "DETAILS") return;
      event.preventDefault();
      toggleDisclosure(summary.parentElement, summary);
    });
    thread.addEventListener("focusin", updateScrollEdges);
    thread.addEventListener("focusout", () => requestAnimationFrame(updateScrollEdges));
    $("jump-latest").addEventListener("click", () => {
      releaseDisclosurePosition();
      app._followLatest = true;
      if (window.EdennUI.reducedMotion()) { scrollThread(); return; }
      const from = thread.scrollTop, started = performance.now();
      app._jumpAnimation = true;
      const tick = (now) => {
        if (!app._jumpAnimation) return;
        const fraction = Math.min(1, (now - started) / 220);
        thread.scrollTop = from + (thread.scrollHeight - thread.clientHeight - from) * (1 - Math.pow(1 - fraction, 3));
        updateScrollEdges();
        if (fraction < 1) requestAnimationFrame(tick);
        else { app._jumpAnimation = false; scrollThread(); }
      };
      requestAnimationFrame(tick);
    });
    const interrupt = () => { releaseDisclosurePosition(); app._jumpAnimation = false; app._programmaticScroll = false; };
    thread.addEventListener("wheel", interrupt, { passive: true });
    thread.addEventListener("touchstart", interrupt, { passive: true });
    thread.addEventListener("keydown", (event) => {
      if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"].includes(event.key)) interrupt();
    });
    new ResizeObserver(() => scrollThread()).observe(inner());
    new ResizeObserver(updateScrollEdges).observe(thread);
  }

  // ========================================================================
  // User actions
  // ========================================================================
  // ----- 1 · layer picker (the modality gate) -----------------------------
  // The three audio layers, in timeline order. `lane` is the colour contract:
  // the same class the timeline row wears, so this widget and the lane it fills
  // are the same colour. See "Lane palettes" in styles.css.
  const LAYERS = [
    { id: "music", label: "Music", hint: "Scored to your scenes", icon: "ti-music", lane: "lane-music" },
    { id: "voiceover", label: "Voiceover", hint: "I draft it, you approve", icon: "ti-microphone", lane: "lane-voiceover" },
    { id: "sfx", label: "Sound effects", hint: "Hits on the cuts", icon: "ti-wave-sine", lane: "lane-sfx" },
  ];

  /**
   * Collapse a layer SET back to the gate's exclusive option id.
   *
   * The gate offers full_audio / music_only / voiceover_only / sound_design.
   * Music+voiceover sets map exactly; an sfx-ONLY pick is the sound_design
   * flow when the gate offers it. A mixed set with sfx (e.g. music+sfx) keeps
   * the closest music/voice id and the exact set rides in `payload.layers` for
   * the backend to consume. Falls back to the first offered option if the
   * backend ever ships a different option set.
   */
  function layerChoiceId(keys, options) {
    const ids = (options || []).map((o) => o.id);
    const has = (k) => keys.indexOf(k) >= 0;
    let want;
    if (keys.length === 1 && has("sfx")) want = "sound_design";
    else if (has("music") && has("voiceover")) want = "full_audio";
    else if (has("voiceover")) want = "voiceover_only";
    else want = "music_only";
    return ids.indexOf(want) >= 0 ? want : (ids[0] || want);
  }

  function layerPicker(options) {
    const box = el("div", "lpick-wrap");
    const list = el("div", "lpick");
    // All three on by default: nothing here spends, so a permissive default costs
    // the user nothing and unchecking is cheaper than hunting for what to add.
    const on = { music: true, voiceover: true, sfx: true };
    const rows = {};

    LAYERS.forEach((L) => {
      const r = el("button", "lpick__row " + L.lane + (on[L.id] ? " is-on" : ""));
      r.type = "button";
      r.setAttribute("aria-pressed", on[L.id] ? "true" : "false");
      r.appendChild(el("span", "lpick__ic", `<i class="ti ${L.icon}"></i>`));
      const txt = el("span", "lpick__txt");
      txt.appendChild(el("span", "lpick__nm", esc(L.label)));
      // What the layer actually means, next to its name. "I draft it, you
      // approve" is the whole contract of the narration layer, and it was lost
      // in a merge — leaving three words a first-time user has to guess at
      // before committing to a paid build.
      txt.appendChild(el("span", "lpick__hint", esc(L.hint)));
      r.appendChild(txt);
      r.appendChild(el("span", "lpick__tick", '<i class="ti ti-check"></i>'));
      r.addEventListener("click", () => {
        on[L.id] = !on[L.id];
        r.classList.toggle("is-on", on[L.id]);
        r.setAttribute("aria-pressed", on[L.id] ? "true" : "false");
        sync();
      });
      rows[L.id] = r;
      list.appendChild(r);
    });
    box.appendChild(list);

    const go = el("div", "w-go");
    const start = el("button", "btn-primary", "Continue");
    start.type = "button";
    const note = el("span", "w-note");
    go.appendChild(start);
    go.appendChild(note);
    box.appendChild(go);

    function picked() { return LAYERS.filter((L) => on[L.id]).map((L) => L.id); }
    function sync() {
      const keys = picked();
      start.disabled = keys.length === 0;
      // Say how many, and say that choosing them costs nothing. Both halves
      // were here and were lost in a merge: the count is the only confirmation
      // that a tap registered, and "nothing generates yet" is what stops a
      // user treating a picker for a paid product as a commitment.
      note.textContent = keys.length === 0
        ? "Pick at least one layer."
        : keys.length + (keys.length === 1 ? " layer" : " layers")
          + " · nothing generates yet";
      if (rows.sfx) rows.sfx.title = "Sound effects";
    }
    sync();

    start.addEventListener("click", () => {
      const keys = picked();
      if (!keys.length) return;
      const names = LAYERS.filter((L) => on[L.id]).map((L) => L.label.toLowerCase());
      chooseClarify(layerChoiceId(keys, options), cap(names.join(" + ")), null, box, { layers: keys });
    });
    return box;
  }

  function cap(s) { return String(s).charAt(0).toUpperCase() + String(s).slice(1); }

  function chooseClarify(optionId, label, node, group, extra) {
    // visual lock
    if (app._thinking) return;
    if (app.clarifyNode && extra && extra.layers) app.clarifyNode.dataset.choiceLabel = "Layers · " + extra.layers.map(layerName).join(" + ");
    if (group) group.querySelectorAll("button").forEach((b) => (b.disabled = true));
    if (node) node.classList.add("is-sel");
    // Optimistic answer bubble — the backend records the same choice as a user
    // message, so index-based reconcile won't re-render it. This keeps the order
    // [your answer] → [thinking] → [reply], exactly like a typed turn.
    if (label) {
      inner().appendChild(renderMessage({ role: "user", content: label }));
      app.renderedCount += 1;
    }
    startThinking(); // show "Thinking…" immediately, before the first beat streams
    scrollThread();
    app.conn.choose({ choice_type: "clarification", target_id: optionId, payload: extra || {} });
  }

  function wireSessionComposer() {
    if (app.composerWired) return;
    app.composerWired = true;
    const input = $("session-input");
    const send = $("session-send");
    const submit = () => {
      const content = input.value.trim();
      if (!content || app._thinking) return;
      // A cut-shorts session has no director socket behind this box. Sending
      // there used to throw on app.conn and swallow what the user typed — keep
      // their words, and say why they are not going anywhere.
      if (!app.conn || !app.conn.send) {
        toast("This session type has no chat — use the controls in the panel on the right.");
        return;
      }
      input.value = "";
      // optimistic user bubble (index-based reconcile will not re-render it)
      inner().appendChild(renderMessage({ role: "user", content }));
      app.renderedCount += 1;
      startThinking(); // immediate "Thinking…", then real beats stream into it
      scrollThread();
      // Canvas mode may attach an @-referenced version as typed context; it rides in
      // the existing payload dict (context_refs), so this stays backward-compatible.
      const ctx = (window.EdennCanvas && window.EdennCanvas.getComposerContext) ? window.EdennCanvas.getComposerContext() : null;
      app.conn.send(ctx && ctx.context_refs && ctx.context_refs.length ? { content, payload: ctx } : { content });
    };
    send.addEventListener("click", submit);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
  }

  // ========================================================================
  // boot
  // ========================================================================
  // ----- session persistence + navigation ----------------------------------
  // Entrance ⇄ session ride the history stack: entering a session PUSHES an
  // entry (so the browser Back button returns home instead of leaving the
  // app), staying in one REPLACES. A consumed ?join= invite never survives
  // into the canonical session URL.
  function setSessionUrl(id, opts) {
    const u = new URL(window.location.href);
    u.searchParams.delete("page");
    u.searchParams.set("session", id);
    u.searchParams.delete("join");
    u.searchParams.delete("grant"); // a consumed invite must not re-trigger
    const inSession = history.state && history.state.view === "session";
    if ((opts && opts.replace) || inSession) history.replaceState({ view: "session" }, "", u);
    else history.pushState({ view: "session" }, "", u);
  }
  function clearSessionUrl(opts) {
    const u = new URL(window.location.href);
    u.searchParams.delete("session");
    u.searchParams.delete("thread");
    u.searchParams.delete("join");
    u.searchParams.delete("grant");
    if (opts && opts.push) history.pushState({ view: "entrance" }, "", u);
    else history.replaceState({ view: "entrance" }, "", u);
  }
  // Leave the session view for the entrance. The socket closes; the session
  // itself keeps running server-side (generation jobs are durable) and shows
  // up under My sessions. Home must never land on a dead composer, so the
  // Start button and busy flag reset here.
  function showEntrance(opts) {
    ++app.connectionEpoch; // late frames from the old socket are not this view's
    if (app._connection && app._connection.close) { try { app._connection.close(); } catch (_) {} }
    app._connection = null;
    app.conn = null;
    app.sessionId = null;
    app.requestQueue = [];
    resetRenderState();
    // The timeline hero must not keep playing under the entrance.
    if (window.EdennTimeline && window.EdennTimeline.pause) window.EdennTimeline.pause();
    // A cut session takes the right pane over wholesale (transform-mode
    // takeRightPane) — leaving it must hand the pane back, or the next audio
    // session opens with no toggle and a stale cut rail.
    if (window.EdennTransform && window.EdennTransform.release) window.EdennTransform.release();
    setRightView(app.rightView);
    const rp = $("result-page");
    if (rp) rp.hidden = true;
    $("session").hidden = true;
    $("entrance").hidden = false;
    $("start-btn").disabled = false;
    app.busy = false;
    // A visible list pane is stale the moment we come home from a session.
    if ($("pane-sessions") && !$("pane-sessions").hidden) renderSessionsPane();
    if ($("pane-gallery") && !$("pane-gallery").hidden) renderGalleryPane();
    if (!opts || !opts.fromHistory) clearSessionUrl({ push: true });
  }
  async function resumeSession(id, opts) {
    const resumeToken = app._resumeToken = (app._resumeToken || 0) + 1;
    try {
      const snap = await app.transport.getSnapshot(id); // 404s if it's gone
      // A slower earlier resume must not land on top of this one.
      if (resumeToken !== app._resumeToken) return;
      // Switching sessions must drop the old socket — otherwise its events
      // keep streaming into the new session's UI — and retire its in-flight
      // request, or the next turn is judged against a request that is gone.
      if (app.sessionId !== id) {
        ++app.connectionEpoch;
        if (app._connection && app._connection.close) { try { app._connection.close(); } catch (_) {} }
        app.requests = new window.EdennState.RequestRegistry();
        app.requestQueue = [];
      }
      // ...and wipe the previous session's rendered thread + memoized cards, or
      // reconcile()'s append-only cursor would leave the old content on screen,
      // mislabeled as the newly-opened session (frontend-flow review P1).
      resetRenderState();
      app.initialDirection = ((snap.messages || []).find((m) => m.role === "user") || {}).content || "";
      // A cut-shorts session hands the whole right pane to its own rail. Resuming
      // an audio session is leaving that session, so the pane has to come back —
      // otherwise this session renders into a hidden stage behind a stale "Cut
      // preview", with no toggle left to escape with.
      if (window.EdennTransform && window.EdennTransform.release) window.EdennTransform.release();
      setRightView(app.rightView);
      app.sessionId = id;
      const rp = $("result-page");
      if (rp) rp.hidden = true;
      $("entrance").hidden = true;
      $("session").hidden = false;
      $("session-name").textContent = ((snap.state || {}).observation || {}).video_title || "Untitled session";
      $("session-input").disabled = false;
      $("session-send").disabled = false;
      setSessionUrl(id, { replace: !!(opts && opts.fromHistory) });
      onEvent({ event_type: "session.opened", session_id: id, payload: { snapshot: snap } });
      openConnection();
      // Re-assert role-honest affordances — a role-change reconnect re-enters
      // here, and the enable-on-open above must not outrank the viewer's role.
      if (window.EdennCollab && window.EdennCollab.gateByRole) window.EdennCollab.gateByRole();
    } catch (e) {
      if (e && e.status === 401 && app.transport.kind === "real") {
        promptForToken("This session needs an access token to open.");
        return;
      }
      toast(app.transport.kind === "mock"
        ? "Couldn't restore that session — the offline demo can't open shared links. Go live to join."
        : "Couldn't restore that session — starting fresh.");
      clearSessionUrl();
      showEntrance({ fromHistory: true });
    }
  }

  // ----- sign-in (token) prompt ---------------------------------------------
  // The lightest real sign-in: the backend authenticates a bearer token
  // (AGENTIC_AUDIO_API_KEYS maps tokens to user ids). A 401 lands here instead
  // of a dead toast; the token rides the URL for this tab only — never stored.
  function promptForToken(reason) {
    if (document.getElementById("token-overlay")) return;
    const ov = el("div", "overlay");
    ov.id = "token-overlay";
    const card = el("div", "confirm-card");
    card.appendChild(el("h3", null, "Sign in to Edenn"));
    card.appendChild(el("p", null, esc(reason || "This backend requires an access token.")));
    const row = el("div", "join-name");
    row.appendChild(el("label", "join-name__lb", "Access token"));
    const input = document.createElement("input");
    input.type = "password";
    input.setAttribute("aria-label", "Access token");
    input.placeholder = "Paste your access token";
    row.appendChild(input);
    card.appendChild(row);
    const foot = el("div", "confirm-card__row");
    const cancel = el("button", "btn-ghost", "Not now");
    cancel.type = "button";
    cancel.addEventListener("click", () => {
      ov.remove();
      clearSessionUrl();
      showEntrance({ fromHistory: true });
    });
    const go = el("button", "btn-primary", "Continue");
    go.type = "button";
    const submit = () => {
      const token = input.value.trim();
      if (!token) { input.focus(); return; }
      const u = new URL(window.location.href);
      u.searchParams.set("token", token);
      window.location.href = u.toString(); // clean reboot with credentials
    };
    go.addEventListener("click", submit);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    foot.appendChild(cancel);
    foot.appendChild(go);
    card.appendChild(foot);
    ov.appendChild(card);
    document.body.appendChild(ov);
    input.focus();
  }

  // ----- shared-link join ---------------------------------------------------
  // A share link carries ?session= plus either a signed ?grant= (works under
  // real auth: the recipient redeems it AS THEMSELVES) or a plain ?join=<role>
  // (dev/mock trust). First open: a join card — who you'll appear as — then a
  // REAL registration, then resume. Owners and existing members skip in.
  async function maybeJoin(sid, role, grant) {
    let snap = null;
    try {
      snap = await app.transport.getSnapshot(sid);
    } catch (e) {
      if (e && e.status === 401) {
        promptForToken("Sign in to open this shared session.");
        return;
      }
      if (!grant) {
        // Without a grant there is no way to become a member — stop honestly.
        toast("Couldn't open that session link — " +
          ((e && e.message) || "it may need an access token (Settings)."));
        clearSessionUrl();
        return;
      }
      // With a grant, an unreadable session is EXPECTED for a not-yet-member
      // under auth — the join below is what makes them one.
    }
    const me = persona();
    if (snap) {
      const collabPayload = await app.transport.collab.get(sid).catch(() => null);
      const parts = (collabPayload && collabPayload.participants) || [];
      const viewerId = (collabPayload && collabPayload.viewer) || me.id;
      const isOwner = (snap.creator_user_id || "") === viewerId;
      if (isOwner || parts.some((p) => p.user_id === viewerId)) {
        resumeSession(sid, { fromHistory: true });
        return;
      }
    }
    const obs = ((snap || {}).state || {}).observation || {};
    const roleLabel = { view: "watch it evolve", comment: "comment on takes",
      iterate: "comment and direct new takes" }[role] || "comment on takes";
    const ov = el("div", "overlay");
    const card = el("div", "confirm-card");
    card.appendChild(el("h3", null, "Join this session"));
    card.appendChild(el("p", null,
      `<strong>${esc(obs.video_title || "A shared session")}</strong> was shared with you — join to ${esc(roleLabel)}.`));
    const row = el("div", "join-name");
    row.appendChild(el("label", "join-name__lb", "Appear as"));
    const nameInput = document.createElement("input");
    nameInput.type = "text"; nameInput.value = me.name; nameInput.maxLength = 40;
    nameInput.setAttribute("aria-label", "Your display name");
    row.appendChild(nameInput);
    card.appendChild(row);
    const foot = el("div", "confirm-card__row");
    const cancel = el("button", "btn-ghost", "Not now");
    cancel.type = "button";
    cancel.addEventListener("click", () => { ov.remove(); clearSessionUrl(); });
    const joinBtn = el("button", "btn-primary", "Join session");
    joinBtn.type = "button";
    joinBtn.addEventListener("click", async () => {
      renamePersona(nameInput.value);
      const p = persona();
      try {
        if (grant) {
          // The signed path: the server validates the grant and registers the
          // CALLER (bearer principal, or this persona in dev) at its role.
          await app.transport.collab.join(sid, {
            grant, display_name: p.name, user_id: p.id,
          });
        } else {
          await app.transport.collab.addParticipant(sid, {
            user_id: p.id, role: role || "comment", display_name: p.name,
          });
        }
      } catch (e) {
        if (e && e.status === 401) {
          ov.remove();
          promptForToken("Sign in to join this session.");
          return;
        }
        if (grant) {
          ov.remove();
          toast("Couldn't join — " + ((e && e.message) || "the invite may have expired."));
          clearSessionUrl();
          return;
        }
        // Legacy plain-role path: owner-gated on auth-on backends — reads
        // still work; writes surface their own honest errors.
      }
      ov.remove();
      resumeSession(sid, { fromHistory: true });
    });
    foot.appendChild(cancel); foot.appendChild(joinBtn);
    card.appendChild(foot);
    ov.appendChild(card);
    document.body.appendChild(ov);
    nameInput.focus();
    nameInput.select();
  }

  // ----- public result page -------------------------------------------------
  // A ?share=result(&take=) link renders a read-only player — no join, no
  // session machinery. "Open the full session" hands over to the normal
  // join/resume flow. Works on both transports (the mock can only serve its
  // own page's sessions; a foreign link fails with an honest toast).
  async function showResultPage(sid, takeId) {
    let snap = null;
    try {
      snap = await app.transport.getSnapshot(sid);
    } catch (e) {
      toast("Couldn't open that result link — " + ((e && e.message) || "it may have expired."));
      clearSessionUrl();
      return;
    }
    const st = snap.state || {};
    const fin = st.final_artifact || {};
    const cands = st.candidates || [];
    const take = (takeId && cands.find((c) => c.candidate_id === takeId)) || null;
    const doneUrls = cands
      .filter((c) => c.status === "completed" && (c.remixed_video_url || c.video_url || c.audio_url))
      .map((c) => c.remixed_video_url || c.video_url || c.audio_url);
    const url = (take && (take.remixed_video_url || take.video_url || take.audio_url)) ||
      fin.video_url || fin.audio_url || doneUrls[doneUrls.length - 1] || null;
    const obs = st.observation || {};
    $("entrance").hidden = true;
    $("session").hidden = true;
    let page = $("result-page");
    if (!page) {
      page = el("div", "result-page");
      page.id = "result-page";
      document.body.appendChild(page);
    }
    page.innerHTML = "";
    page.hidden = false;
    const card = el("div", "result-card");
    card.appendChild(el("div", "result-card__kicker",
      '<i class="ti ti-sparkles"></i> Shared from Edenn'));
    card.appendChild(el("h1", "result-card__title",
      esc((take && take.title) || obs.video_title || "A finished mix")));
    if (url) {
      const isVideo = /\.(mp4|mov|webm)(\?|$)/i.test(url) || url.indexOf("data:video") === 0;
      const media = document.createElement(isVideo ? "video" : "audio");
      media.src = mediaSrc(url);
      media.controls = true;
      media.className = "result-card__media" + (isVideo ? "" : " is-audio");
      card.appendChild(media);
    } else {
      card.appendChild(el("p", "result-card__none",
        "This session hasn't locked a take yet — ask for a fresh link once it's finished."));
    }
    const row = el("div", "result-card__row");
    const open = el("button", "btn-primary", "Open the full session");
    open.type = "button";
    open.addEventListener("click", () => {
      page.hidden = true;
      const params = new URLSearchParams(window.location.search);
      const join = params.get("join");
      if (join) maybeJoin(sid, join);
      else resumeSession(sid, { fromHistory: true });
    });
    row.appendChild(open);
    card.appendChild(row);
    page.appendChild(card);
  }

  // URL → view. Runs at boot and on every Back/Forward.
  function handleLocation(fromHistory) {
    const params = new URLSearchParams(window.location.search);
    const sid = params.get("session");
    const rp = $("result-page");
    if (rp) rp.hidden = true;
    if (!sid) {
      if (fromHistory) showEntrance({ fromHistory: true });
      return;
    }
    if (params.get("share") === "result") {
      showResultPage(sid, params.get("take"));
      return;
    }
    if (app.transport.kind !== "real") {
      // The in-page mock CAN resume its own sessions (Back/Forward); a foreign
      // shared link fails inside resumeSession with an honest explanation.
      resumeSession(sid, { fromHistory: true });
      return;
    }
    const join = params.get("join");
    const grant = params.get("grant");
    if ((join || grant) && !fromHistory) maybeJoin(sid, join, grant);
    else resumeSession(sid, { fromHistory: true });
  }

  // Toolbar: Home (back to the entrance) + Export (download the deliverable)
  // + History (list past sessions, resume).
  function wireToolbar() {
    const home = $("tb-home");
    if (home) home.addEventListener("click", () => showEntrance());
    const exp = $("tb-export");
    if (exp) exp.addEventListener("click", () => {
      const st = (app.snapshot && app.snapshot.state) || {};
      const { url, kind } = window.EdennState.resolveOutput(st);
      if (url) deliverFile(url, kind === "video" ? "edenn-final-mix.mp4" : "edenn-final-mix.mp3");
      else toast("Finish a mix first — then you can export it.");
    });
    const hist = $("tb-history");
    const pop = $("history-pop");
    if (hist && pop) hist.addEventListener("click", async () => {
      if (!pop.hidden) { pop.hidden = true; return; }
      pop.innerHTML = "";
      pop.appendChild(el("div", "history-pop__hd", "Recent sessions"));
      let sessions = [];
      try { sessions = (await app.transport.listSessions()).sessions || []; } catch (_) {}
      if (!sessions.length) {
        pop.appendChild(el("div", "history-pop__empty",
          app.transport.kind === "mock" ? "History is available with the live backend." : "No sessions yet."));
      } else {
        sessions.forEach((s) => {
          const row = el("button", "history-pop__row",
            `${esc(s.title || String(s.session_id).slice(0, 16))} · ${esc(s.phase)}${s.shared ? " · shared" : ""}`);
          row.addEventListener("click", () => {
            pop.hidden = true;
            resumeSession(s.session_id); // owns the URL push
          });
          pop.appendChild(row);
        });
      }
      pop.hidden = false;
    });
  }

  /**
   * Track the chat pane's width and re-render the widgets that reflow.
   *
   * A class alone handles the small stuff (hiding a take's duration), but the
   * direction table genuinely changes DOM shape between wide and narrow, so the
   * flip busts the two render memos and replays the last snapshot. Guarded on an
   * actual mode change: the grip fires this continuously while being dragged.
   */
  function watchPaneWidth() {
    const pane = $("chatpane");
    if (!pane) return;
    const measure = () => {
      const w = pane.getBoundingClientRect().width;
      // Width 0 means the session view is hidden (display:none), not narrow —
      // acting on it would flip narrow mode (and replay the snapshot) every
      // time the user goes home.
      if (!w) return;
      document.body.classList.toggle("is-pane-tight", w < TIGHT_W);
      const narrow = w < NARROW_W;
      if (narrow === app._narrow) return;
      app._narrow = narrow;
      document.body.classList.toggle("is-pane-narrow", narrow);
      app._proposalsKey = null;
      app._candBlockKey = null;
      // A pure re-render: crossing the width threshold must not close a live
      // thinking trail mid-turn.
      if (app.snapshot) reconcile(app.snapshot, { preserveThinking: true });
    };
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(measure).observe(pane);
    else window.addEventListener("resize", measure);
    measure();
  }

  // ========================================================================
  // Signing in
  // ========================================================================
  //
  // The console used to invent a person in localStorage and get on with it,
  // which meant the same human was a different identity on every device and
  // nobody could come back to their own work. With a real backend identity, the
  // SERVER decides who you are; this is the surface that lets you become
  // somebody in the first place.
  //
  // Three states, and the console has to be honest about which one it is in:
  //
  //   auth off              — a local run. Carry on as a guest, say nothing.
  //   auth on, signed in    — the server told us who we are; show that.
  //   auth on, signed out   — offer a way in, and do not pretend to be a guest.

  async function establishIdentity() {
    const cfg = await app.transport.authConfig();
    app.authConfig = cfg;
    if (!cfg.auth_required) {
      app.me = { authenticated: false };
      return;
    }
    app.me = await app.transport.whoami();
    if (!app.me.authenticated) showSignIn(cfg);
  }

  function showSignIn(cfg) {
    const overlay = $("signin-overlay");
    if (!overlay) return;
    const body = $("signin-body");
    body.innerHTML = "";

    if (!cfg.configured) {
      // Nothing to sign in WITH. Saying so beats a button that cannot work.
      body.appendChild(el("p", "signin__lead",
        "Sign-in isn't available on this deployment yet."));
      body.appendChild(el("p", "signin__note",
        cfg.legacy_tokens_accepted
          ? "This server still accepts an access token. Add yours in Settings to continue."
          : "Ask whoever runs this deployment to finish configuring sign-in."));
      const settings = el("button", "btn-primary", "Open settings");
      settings.type = "button";
      settings.addEventListener("click", () => { hideSignIn(); openSettings(); });
      if (cfg.legacy_tokens_accepted) body.appendChild(settings);
      overlay.hidden = false;
      return;
    }

    body.appendChild(el("p", "signin__lead", "Sign in to score your video."));
    body.appendChild(el("p", "signin__note",
      "Your sessions, takes and shared links are tied to your account, so you "
      + "can pick up where you left off on any device."));
    const go = el("button", "btn-primary", "Sign in");
    go.type = "button";
    go.addEventListener("click", () => {
      // The identity provider owns the sign-in itself. The console's job is to
      // send you there and to accept the token you come back with. WHERE that
      // is comes from the server: the path below exists on the platform host
      // and nowhere else, so hard-coding it sent every standalone user to a
      // 404 on the one button that was supposed to let them in.
      const next = encodeURIComponent(window.location.href);
      const where = (cfg && cfg.signin_url) || "/console/signin";
      const join = where.indexOf("?") >= 0 ? "&" : "?";
      window.location.href = `${where}${join}next=${next}`;
    });
    body.appendChild(go);
    overlay.hidden = false;
  }

  function hideSignIn() {
    const overlay = $("signin-overlay");
    if (overlay) overlay.hidden = true;
  }

  let homeView = null;
  function showHomePage(page) {
    homeView?.destroy(); homeView = null;
    const library = $("library-page");
    $("new-session-page").hidden = page !== "new";
    library.hidden = page === "new";
    document.querySelectorAll("[data-home]").forEach(button => {
      const active = button.dataset.home === page;
      button.classList.toggle("is-active", active);
      if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    const url = new URL(location.href); url.searchParams.delete("session");
    if (page === "new") url.searchParams.delete("page"); else url.searchParams.set("page", page);
    history.replaceState(null, "", url);
    if (page === "new") return;
    homeView = window.EdennLibrary.mount({ root: library, page, transport: app.transport,
      preview: new URLSearchParams(location.search).get("preview") === "library",
      onNew(prompt) {
        showHomePage("new");
        if (prompt) $("start-text").value = prompt;
        $("start-text").focus({ preventScroll: true });
      },
      onOpen(id) { homeView?.destroy(); homeView = null; setSessionUrl(id); resumeSession(id); },
    });
  }
  function wireHomePages() {
    const studioHome = $("studio-home");
    if (studioHome) studioHome.addEventListener("click", () => {
      const url = new URL(location.href);
      url.searchParams.delete("session");
      url.searchParams.set("page", "sessions");
      location.assign(url);
    });
    document.querySelectorAll("[data-home]").forEach(button => button.addEventListener("click", () => showHomePage(button.dataset.home)));
    const chatToggle = $("chat-toggle"), chatClose = $("chat-close");
    if (!chatToggle || !chatClose) return;
    function setChatOpen(open) {
      $("session").classList.toggle("is-chat-closed", !open);
      $("chatpane").inert = !open;
      $("rgrip").inert = !open;
      chatToggle.setAttribute("aria-expanded", String(open));
      chatToggle.hidden = open;
      if (!open) chatToggle.focus({ preventScroll: true });
      else chatClose.focus({ preventScroll: true });
    }
    chatToggle.addEventListener("click", () => setChatOpen(chatToggle.getAttribute("aria-expanded") !== "true"));
    chatClose.addEventListener("click", () => setChatOpen(false));
  }
  async function boot() {
    app.transport = pickTransport();
    // Ask the server who we are BEFORE painting a name, or the console shows an
    // invented guest for a moment and then swaps it for the real person.
    try {
      await establishIdentity();
    } catch (_) {
      app.me = { authenticated: false };
    }
    persona();
    paintPersona();
    wireEntrance();
    wireHomePages();
    wireToolbar();
    wireViewToggle();
    watchPaneWidth();
    watchThread();
    // Seed the history state so Back/Forward can tell the two views apart,
    // then route: a library page, refresh-rehydrate, shared-link join, or the
    // plain entrance.
    const params = new URLSearchParams(window.location.search);
    const hasSession = params.get("session");
    history.replaceState({ view: hasSession ? "session" : "entrance" }, "", window.location.href);
    window.addEventListener("popstate", () => handleLocation(true));
    const page = params.get("page");
    if (!hasSession && (page === "sessions" || page === "gallery")) showHomePage(page);
    else handleLocation(false);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  // expose for headless testing + the additive canvas-mode module (read-only:
  // it reuses these helpers so it acts through the exact same contract).
  window.__edenn = { app, onEvent, requestVariation, confirmSpend, toast, fmtDur, startThinking,
    setRightView, resumeSession, showEntrance, persona, establishIdentity,
    MODEL_LABEL, EDIT_LABEL, modelText, deliverFile };
})();
