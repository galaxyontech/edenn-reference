/* ============================================================================
 * Edenn — in-browser MOCK backend (Stage 1).
 *
 * Mirrors the REAL contract in FRONTEND_INTEGRATION.md / models.py exactly so the
 * UI binds identically whether it talks to this mock or the live FastAPI router:
 *   - createSession  -> { session_id, status, phase, status_url, ws_url }
 *   - getSnapshot    -> AgenticAudioSessionSnapshot { ..., state, messages, ... }
 *   - message/choice -> streams the same event vocabulary, ending each turn with
 *     a fresh `session.opened` snapshot (just like the WS).
 *
 * Stage 1 implements the FRONT DOOR: create session -> observe video -> greeting
 * -> intent clarify. Later phases are stubbed (a teaser "finding directions"
 * state) so the flow never dead-ends; Stage 2 fills in proposals.
 *
 * Runs in the browser (window.MockBackend) and in Node (module.exports) so the
 * same contract logic is unit-testable headlessly.
 * ========================================================================== */
(function (root) {
  "use strict";

  let _seq = 0;
  function newId(prefix) {
    _seq += 1;
    return `${prefix}_${Date.now().toString(36)}${_seq.toString(36)}`;
  }
  const MUSIC_S = 15, VO_S = 7;      // fixture lengths (see previewWav below)
  const nowIso = () => new Date().toISOString();
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // A quiet synthesized preview WAV, so mock takes have audible, measurable audio (works in
  // the browser and in Node).
  //
  // The length matters now: the timeline reads clip intervals off the real
  // `duration` of these files, so a stand-in has to be as long as the thing it
  // stands in for. MUSIC_S covers the reel; VO_S is a plausible narration.
  function previewWav(seconds) {
    seconds = seconds || 1;
    const rate = 8000, n = rate * seconds, dataLen = n * 2;
    const buf = new Uint8Array(44 + dataLen);
    const dv = new DataView(buf.buffer);
    const str = (off, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(off + i, s.charCodeAt(i)); };
    str(0, "RIFF"); dv.setUint32(4, 36 + dataLen, true); str(8, "WAVE");
    str(12, "fmt "); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
    dv.setUint32(24, rate, true); dv.setUint32(28, rate * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
    str(36, "data"); dv.setUint32(40, dataLen, true);
    for (let i = 0; i < n; i++) {
      const t = i / rate;
      const fade = Math.min(1, t / 0.08, (seconds - t) / 0.2);
      const pulse = Math.exp(-6 * (t % 0.5));
      const swell = 0.35 + 0.65 * Math.pow(Math.sin(Math.PI * t / 3), 2);
      const tone = Math.sin(2 * Math.PI * 220 * t) + 0.3 * Math.sin(2 * Math.PI * 330 * t);
      dv.setInt16(44 + i * 2, Math.round(2400 * fade * swell * (0.2 + 0.8 * pulse) * tone), true);
    }
    let bin = ""; for (let i = 0; i < buf.length; i++) bin += String.fromCharCode(buf[i]);
    const b64 = typeof btoa !== "undefined" ? btoa(bin) : Buffer.from(buf).toString("base64");
    return "data:audio/wav;base64," + b64;
  }

  // Intent (modality) options shown on the first turn -> a faithful `clarify`.
  const INTENT_OPTIONS = [
    { id: "full_audio", label: "Full audio", hint: "Music + voiceover + SFX" },
    { id: "music_only", label: "Music only", hint: "Soundtrack first" },
    { id: "voiceover_only", label: "Voiceover only", hint: "Narration" },
    { id: "sound_design", label: "Sound FX", hint: "Effects matched to the action" },
  ];

  const PLAN_BY_INTENT = {
    full_audio: { mode: "full_e2e", layers: ["music", "voiceover", "sfx"] },
    music_only: { mode: "music_first", layers: ["music"] },
    voiceover_only: { mode: "music_first", layers: ["voiceover"] },
    sound_design: { mode: "music_first", layers: ["sfx"] },
  };

  // A believable observation in the exact shape `analyze_video` returns
  // (see _fake_analyze in the test-suite + ARCHITECTURE observation contract).
  function buildObservation(prompt) {
    return {
      duration_s: 15.0,
      width: 1080,
      height: 1920,
      video_title: "reel_v3",
      video_description: "Fast-cut lifestyle reel, four scenes, emotional close.",
      scenes: [
        { index: 0, start_s: 0.0, end_s: 3.4, label: "Opening — street, daylight" },
        { index: 1, start_s: 3.4, end_s: 7.1, label: "Action — fast cuts" },
        { index: 2, start_s: 7.1, end_s: 11.2, label: "Product hero" },
        { index: 3, start_s: 11.2, end_s: 15.0, label: "Emotional close" },
      ],
      detected_language: "en",
      detected_category: "VIDEO",
      detected_include_vocals: false,
      detected_vocal_gender: "female",
      sanitized_prompt: prompt || "",
      music_prompt: {},
      suggested_modelspec: "edenn_enhanced",
    };
  }

  function observationSummary(obs) {
    const mins = Math.floor(obs.duration_s / 60);
    const secs = String(Math.round(obs.duration_s % 60)).padStart(2, "0");
    const dur = `${mins}:${secs}`;
    const dialogue = obs.detected_include_vocals ? "with dialogue" : "no dialogue";
    return `a fast-cut ${dur} reel — ${obs.scenes.length} scenes, upbeat energy, ${dialogue}`;
  }

  function emptyState() {
    return {
      phase: "created",
      status: "active",
      observation: null,
      proposals: [],
      approved_direction: false,
      candidates: [],
      selected_candidate_id: null,
      production_plan: null,
      layers: { music: null, voiceover: null, sfx: [] },
      mix: null,
      pending_clarification: null,
      final_artifact: null,
      memory: { preferences: {}, creative_direction: "", recent_intents: [] },
      turns: [],
      // Stage-1 UI hint (mock-only): teaser that Stage 2 proposals are coming.
      awaiting_proposals: false,
    };
  }

  class MockBackend {
    /** @param {{delay?: number}} [opts] delay between streamed beats (0 in tests). */
    constructor(opts) {
      this.delay = opts && typeof opts.delay === "number" ? opts.delay : 320;
      this.sessions = {}; // id -> { session, state, messages, tool_calls, choices }
      // Unhappy-path switch: `?mockfail=1` makes generated takes hydrate to
      // "failed" instead of "completed", so the failed-candidate + retry UI is
      // exercisable offline (the real worker can also fail).
      this.failMode = (typeof location !== "undefined" && opts && opts.failMode == null)
        ? new URLSearchParams(location.search).get("mockfail") === "1"
        : !!(opts && opts.failMode);
    }
    _hydrateStatus() { return this.failMode ? "failed" : "completed"; }

    // ---- helpers ---------------------------------------------------------
    _rec(id) {
      const rec = this.sessions[id];
      if (!rec) throw new Error(`Session not found: ${id}`);
      return rec;
    }
    _addMessage(rec, role, content, payload) {
      const m = {
        message_id: newId("agent_msg"),
        session_id: rec.session.session_id,
        role,
        content,
        payload_json: payload || {},
        created_at: nowIso(),
      };
      rec.messages.push(m);
      return m;
    }
    _toolCall(rec, name, status) {
      rec.tool_calls.push({
        tool_call_id: newId("tc"),
        session_id: rec.session.session_id,
        tool_name: name,
        status,
        input_json: {},
        output_json: status === "completed" ? {} : null,
        created_at: nowIso(),
      });
    }
    snapshot(id) {
      const rec = this._rec(id);
      const s = rec.session;
      return {
        session_id: s.session_id,
        source_video_artifact_id: s.source_video_artifact_id,
        status: rec.state.status,
        phase: rec.state.phase,
        creator_user_id: s.creator_user_id,
        selected_candidate_id: rec.state.selected_candidate_id,
        linked_job_ids: [],
        state: JSON.parse(JSON.stringify(rec.state)),
        // Real snapshots serialize the message payload as `payload` (see
        // repositories.build_snapshot) — mirror that key, not payload_json.
        messages: rec.messages.map((m) => {
          const { payload_json, ...rest } = m;
          return { ...rest, payload: payload_json || {} };
        }),
        tool_calls: rec.tool_calls.map((t) => ({ ...t })),
        choices: rec.choices.map((c) => ({ ...c })),
      };
    }
    _opened(id, turnComplete = true) {
      return { event_type: "session.opened", session_id: id, payload: { snapshot: this.snapshot(id), turn_complete: turnComplete } };
    }
    _evt(id, type, payload) {
      return { event_type: type, session_id: id, payload: payload || {} };
    }

    // ---- REST: create session -------------------------------------------
    // Faithful to the real router: bootstrap runs here and POPULATES state;
    // the first WS `session.opened` then carries the greeting + observation +
    // intent clarify (no live beats on turn 1, exactly like production).
    createSession(req) {
      const sessionId = newId("agent_session");
      const rec = {
        session: {
          session_id: sessionId,
          source_video_artifact_id: req.source_video_artifact_id,
          creator_user_id: req.creator_user_id || null,
        },
        state: emptyState(),
        messages: [],
        tool_calls: [],
        choices: [],
      };
      this.sessions[sessionId] = rec;

      // ---- bootstrap turn (observe -> greet -> clarify intent) ----
      if (req.initial_message && req.initial_message.trim()) {
        this._addMessage(rec, "user", req.initial_message.trim());
      }
      this._toolCall(rec, "analyze_video", "started");
      const obs = buildObservation(req.initial_message);
      rec.state.observation = obs;
      this._toolCall(rec, "analyze_video", "completed");
      this._addMessage(
        rec,
        "assistant",
        `I’ll shape the sound around ${obs.scenes.length} scenes.`,
        { presents_observation: true }
      );
      // Shape mirrors the real backend's intent gate exactly:
      // { question, options:[{id,label,hint}], gate:"intent" }.
      rec.state.pending_clarification = {
        question: "What are you adding today?",
        options: INTENT_OPTIONS.map((o) => ({ ...o })),
        gate: "intent",
      };
      rec.state.phase = "observing";
      rec.state.memory.recent_intents.push("analyze");

      return {
        session_id: sessionId,
        status: rec.state.status,
        phase: rec.state.phase,
        status_url: `/api/v2/agentic/audio/sessions/${sessionId}`,
        ws_url: `/api/v2/agentic/audio/sessions/${sessionId}/ws`,
      };
    }

    // ---- REST: list sessions (this page's only) --------------------------
    // Shape mirrors the real GET /sessions payload (incl. the entrance
    // Sessions/Gallery display extras). Per-page by nature: the list dies
    // with the tab, and the entrance says so.
    listSessions() {
      return Object.values(this.sessions)
        .map((rec) => {
          const st = rec.state;
          const last = rec.messages[rec.messages.length - 1];
          return {
            session_id: rec.session.session_id,
            status: st.status,
            phase: st.phase,
            creator_user_id: rec.session.creator_user_id,
            source_video_artifact_id: rec.session.source_video_artifact_id,
            updated_at: last ? last.created_at : nowIso(),
            title: (st.observation && st.observation.video_title) || null,
            final_media_url:
              (st.final_artifact && (st.final_artifact.video_url || st.final_artifact.audio_url)) || null,
          };
        })
        .sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
    }

    // ---- "WS": connect. Returns a live channel. -------------------------
    connect(id, handlers) {
      const rec = this._rec(id);
      const h = handlers || {};
      // Collab mutations (and the fake agent pickup) broadcast through the
      // last live connection, mirroring the real router's WS fan-out hub.
      rec.lastHandlers = h;
      // On connect the server sends the full snapshot to rehydrate.
      Promise.resolve().then(() => {
        if (h.onOpen) h.onOpen();
        if (h.onEvent) h.onEvent(this._opened(id));
      });
      const correlated = (frame) => ({ ...h, onEvent: (event) => h.onEvent && h.onEvent({ ...event, request_id: frame.request_id }) });
      return {
        send: (frame) => this._handleMessage(id, frame.content || "", correlated(frame)),
        choose: (frame) => this._handleChoice(id, frame, correlated(frame)),
        getSnapshot: () => this.snapshot(id),
        close: () => { if (h.onClose) h.onClose(); },
      };
    }

    async _emit(h, evt) {
      if (h.onEvent) h.onEvent(evt);
      if (this.delay) await sleep(this.delay);
    }

    // ---- a structured choice (card / chip tap) --------------------------
    async _handleChoice(id, frame, h) {
      const rec = this._rec(id);
      const choice = {
        choice_id: newId("choice"),
        session_id: id,
        choice_type: frame.choice_type,
        target_id: frame.target_id,
        payload_json: frame.payload || {},
        created_at: nowIso(),
      };
      rec.choices.push(choice);
      await this._emit(h, this._evt(id, "choice.recorded", {
        choice_id: choice.choice_id, choice_type: choice.choice_type, target_id: choice.target_id,
      }));

      if (frame.choice_type === "clarification") {
        const rec0 = this._rec(id);
        const pending = (rec0.state || {}).pending_clarification || {};
        if (pending.topic === "sfx_treatment") {
          return this._answerSfxTreatment(id, frame.target_id, h);
        }
        return this._answerIntent(id, frame.target_id, h, frame.payload || {});
      }
      if (frame.choice_type === "proposal") {
        return this._approveProposal(id, frame.target_id, h, frame.payload);
      }
      if (frame.choice_type === "candidate" || frame.choice_type === "compose") {
        return this._finalize(id, frame.target_id, h);
      }
      if (frame.choice_type === "mix") {
        return this._adjustMix(id, frame.payload || {}, h);
      }
      if (frame.choice_type === "variation") {
        return this._variation(id, frame.target_id, h);
      }
      if (frame.choice_type === "voiceover") {
        return this._voiceover(id, frame.payload || {}, h);
      }
      if (frame.choice_type === "sfx") {
        return this._sfx(id, frame.payload || {}, h);
      }
      if (frame.choice_type === "spotting") {
        return this._spotting(id, frame.target_id, frame.payload || {}, h);
      }
      if (frame.choice_type === "sculpt") {
        return this._sculpt(id, frame.target_id, frame.payload || {}, h);
      }
      if (frame.choice_type === "compare") {
        return this._compare(id, frame.payload || {}, h);
      }
      await this._emit(h, this._evt(id, "agent.reasoning",
        { status: "Noted.", intent: "other", thought: "That belongs to a later step." }));
      this._addMessage(rec, "assistant", "Noted — we'll handle that in the next step.");
      if (h.onEvent) h.onEvent(this._opened(id));
    }

    // ---- structured mix / variation / voiceover (Phase 4 contract) ------
    async _adjustMix(id, payload, h) {
      const rec = this._rec(id);
      const vo = (rec.state.layers || {}).voiceover;
      const hasVo = !!(vo && vo.audio_url);
      const mix = Object.assign(
        // voiceover_start_s: the real default is 0; the fixture opens on the
        // establishing shot and brings narration in on the first cut, which is
        // both plausible and what puts music and voice on screen together.
        // (Segmented narration ignores it — the plan bakes its own timing.)
        { music_volume: 0.85, voiceover_volume: 1.0, voiceover_start_s: 3.4, duck_gain_db: -9,
          sfx_volume: 1.0, status: "completed" },
        rec.state.mix || {}, payload
      );
      // Master-mix parity: flag when a completed SFX bed joins the compose.
      const sfxL = (rec.state.layers || {}).sfx;
      mix.sfx_included = !!(sfxL && typeof sfxL === "object" && !Array.isArray(sfxL)
        && (sfxL.variants || []).some((v) => v.status === "completed" && v.audio_url));
      // The composed "video" stand-in is as long as the reel — the timeline hero
      // plays it, and a 1s stub would end the playhead 14s early.
      mix.video_url = hasVo ? previewWav(MUSIC_S) : (mix.video_url || null);
      rec.state.mix = mix;
      rec.state.last_mix_volume = mix.music_volume;
      await this._emit(h, this._evt(id, "tool.started", { tool_name: hasVo ? "compose_mix" : "adjust_remix" }));
      await this._emit(h, this._evt(id, "mix.updated", mix));
      if (h.onEvent) h.onEvent(this._opened(id));
    }

    // The user taking a moment back. Ownership governs every layer, so it is
    // reachable directly rather than only through whatever the agent infers
    // from a sentence. Mirrors set_moment_owner: the owner is recorded with
    // "user" provenance, which downstream planners are required to respect.
    async _spotting(id, momentId, payload, h) {
      const rec = this._rec(id);
      const sheet = rec.state.spotting_sheet || {};
      const moments = sheet.moments || [];
      const target = String(momentId || payload.moment_id || "");
      const owner = String(payload.owner || "");
      const hit = moments.find((m) => String(m.moment_id || m.id) === target);
      if (!hit) {
        if (h.onEvent) h.onEvent(this._evt(id, "error", { message: "No such moment." }));
        return;
      }
      hit.owner = owner;
      hit.owner_source = "user";
      if (payload.reason) hit.owner_reason = String(payload.reason);
      rec.state.spotting_sheet = Object.assign({}, sheet, { moments });
      await this._emit(h, this._evt(id, "spotting.sheet", {
        spotting_sheet: rec.state.spotting_sheet, changed: hit,
      }));
      if (h.onEvent) h.onEvent(this._opened(id));
    }

    // Re-cut a take from a different point in its own full track. Free and
    // instant on both sides. Mirrors SculptAudioTool: the window is persisted
    // ON the candidate, the epoch advances, and the previous window's
    // listen-back report goes with it — a stale report is the whole reason the
    // epoch exists.
    async _sculpt(id, candidateId, payload, h) {
      const rec = this._rec(id);
      const take = (rec.state.candidates || []).find((c) => c.candidate_id === candidateId);
      if (!take) { if (h.onEvent) h.onEvent(this._evt(id, "error", { message: "Candidate not found." })); return; }
      if (!take.complete_audio_url) {
        this._addMessage(rec, "assistant",
          "This take has no longer track behind it — there's no other window to move to.");
        if (h.onEvent) h.onEvent(this._opened(id));
        return;
      }
      const startS = Math.max(0, Number(payload.window_start_s) || 0);
      // An arrangement is pieces of the take's own track in a new order. The
      // real renderer assembles it, publishes it as a file of its own, and
      // every later render plays THAT — so the mock carries the same two
      // fields, or the console cannot be exercised against the behaviour that
      // actually broke: an arrangement discarded by the next volume nudge.
      const segments = Array.isArray(payload.segments) ? payload.segments : [];
      const arrangedS = segments.reduce(
        (total, seg) => total + (Number(seg.duration_s) || 0), 0);
      await this._emit(h, this._evt(id, "tool.started", { tool_name: "sculpt_audio" }));
      rec.state.candidates = (rec.state.candidates || []).map((c) => {
        if (c.candidate_id !== candidateId) return c;
        const epoch = Number(c.render_epoch || 0) + 1;
        const window = Object.assign({}, c.window || {},
          segments.length
            ? { start_s: 0, source: "user", segments: segments }
            : { start_s: startS, source: "user" });
        if (!segments.length) delete window.segments;
        const next = Object.assign({}, c, {
          render_epoch: epoch,
          window: window,
          // Measured at render time by the real thing; the mock simply reports
          // a clean re-cut, which is what a working seek sounds like.
          take_signals: { render_epoch: epoch, cut_duration_s: MUSIC_S,
                          leading_silence_s: 0, trailing_silence_s: 0.1 },
          listen_report: { clean: true, notes: [], observations: [],
                           measured: { cut_duration_s: MUSIC_S } },
        });
        // The arrangement is durable or it is not an arrangement: a take that
        // was spliced plays its arranged file from here on, and a plain re-cut
        // drops any arrangement it used to have.
        if (segments.length) next.arranged_audio_url = previewWav(arrangedS || MUSIC_S);
        else delete next.arranged_audio_url;
        return next;
      });
      await this._emit(h, this._evt(id, "candidate.cards", { candidates: rec.state.candidates }));
      this._addMessage(rec, "assistant", segments.length
        ? `Arranged ${segments.length} pieces of the track — have a listen.`
        : `Moved the window to ${startS.toFixed(1)}s — have a listen.`);
      if (h.onEvent) h.onEvent(this._opened(id));
    }

    // Side by side on what the takes SOUND like. Mirrors CompareTakesTool:
    // finished takes only, written to state because the scratchpad is
    // turn-local, and stamped with the epoch each row measured.
    async _compare(id, payload, h) {
      const rec = this._rec(id);
      const wanted = Array.isArray(payload.candidate_ids) ? payload.candidate_ids : null;
      const finished = (rec.state.candidates || []).filter((c) => c.status === "completed");
      const pool = wanted ? finished.filter((c) => wanted.includes(c.candidate_id)) : finished;
      await this._emit(h, this._evt(id, "tool.started", { tool_name: "compare_takes" }));
      if (!pool.length) {
        this._addMessage(rec, "assistant", "No finished takes to compare yet.");
        if (h.onEvent) h.onEvent(this._opened(id));
        return;
      }
      const rows = pool.map((c) => ({
        candidate_id: c.candidate_id,
        status: c.status,
        title: c.title,
        version: c.version,
        locked: c.candidate_id === rec.state.selected_candidate_id,
        duration_s: ((c.listen_report || {}).measured || {}).cut_duration_s,
        window_start_s: (c.window || {}).start_s,
        faults: ((c.listen_report || {}).notes) || [],
        shape: ((c.listen_report || {}).observations) || [],
      }));
      rec.state.last_comparison = {
        takes: rows,
        compared: rows.length,
        any_faults: rows.some((r) => r.faults.length),
        candidate_ids: rows.map((r) => r.candidate_id),
        candidate_epochs: pool.reduce((acc, c) => {
          acc[c.candidate_id] = Number(c.render_epoch || 0);
          return acc;
        }, {}),
      };
      this._addMessage(rec, "assistant",
        `Compared ${rows.length} take${rows.length === 1 ? "" : "s"}.`);
      if (h.onEvent) h.onEvent(this._opened(id));
    }

    async _variation(id, candidateId, h) {
      const rec = this._rec(id);
      const parent = (rec.state.candidates || []).find((c) => c.candidate_id === candidateId);
      if (!parent) { if (h.onEvent) h.onEvent(this._evt(id, "error", { message: "Candidate not found." })); return; }
      // Mirror the real CandidateGraph.next_version: max(parent, existing children) + 1,
      // so repeated branches off one parent get distinct versions/ids (no collisions).
      const sibs = (rec.state.candidates || []).filter((c) => c.parent_candidate_id === candidateId);
      const version = sibs.reduce((m, c) => Math.max(m, c.version || 1), parent.version || 1) + 1;
      const child = Object.assign({}, parent, {
        candidate_id: `${candidateId}_v${version}`, title: `${parent.title || "Take"} (new take)`,
        parent_candidate_id: candidateId, version, edit_kind: "regenerate",
        status: "queued", audio_url: null, video_url: null,
      });
      rec.state.candidates = (rec.state.candidates || []).concat([child]);
      rec.state.phase = "generating_candidates";
      await this._emit(h, this._evt(id, "tool.started", { tool_name: "edit_audio" }));
      await this._emit(h, this._evt(id, "candidate.cards", { candidates: rec.state.candidates }));
      if (h.onEvent) h.onEvent(this._opened(id));
      setTimeout(() => {
        const r2 = this.sessions[id]; if (!r2) return;
        // Honour ?mockfail=1 like first-generation takes do, so the
        // failed-retry-fails-again path is exercisable offline.
        const vstatus = this._hydrateStatus();
        r2.state.candidates = r2.state.candidates.map((c) =>
          c.candidate_id === child.candidate_id
            ? Object.assign({}, c, { status: vstatus, audio_url: vstatus === "completed" ? previewWav(MUSIC_S) : null })
            : c);
        r2.state.phase = "awaiting_candidate_choice";
        if (h.onEvent) h.onEvent(this._opened(id, false));
      }, this.delay ? 2600 : 0);
    }

    async _voiceover(id, payload, h) {
      const rec = this._rec(id);
      const layers = rec.state.layers || (rec.state.layers = {});
      const prev = layers.voiceover || {};
      // Video-informed narration parity: honor provided segments, else spot a
      // timed plan from the scenes (mirrors the real propose_script direction).
      let segments = Array.isArray(payload.narration_segments) && payload.narration_segments.length
        ? payload.narration_segments.map((s, i) => ({
            id: "seg_" + String(i + 1).padStart(2, "0"),
            text: String(s.text || "").trim(), start_s: Number(s.start_s || 0),
            delivery: String(s.delivery || "").trim(),
          })).filter((s) => s.text)
        : (prev.segments || []);
      const scenes = ((rec.state.observation || {}).scenes || []);
      if (!segments.length && !payload.script && scenes.length >= 2) {
        segments = [
          { id: "seg_01", text: "Sometimes the loudest moments are the ones without words.",
            start_s: Number(scenes[0].start_s || 0), delivery: "hushed, drawing the listener in" },
          { id: "seg_02", text: "Everything, all at once.",
            start_s: Number(scenes[scenes.length - 1].start_s || 0), delivery: "final, resolute" },
        ];
      }
      const script = String(payload.script
        || (segments.length ? segments.map((s) => s.text).join(" ") : prev.script)
        || "Narration.").trim();
      layers.voiceover = {
        script, voice_id: payload.voice_id || prev.voice_id || "warm_female",
        tone: payload.tone || prev.tone || "", status: "queued", audio_url: null,
        segments: payload.script && !payload.narration_segments ? [] : segments,
        voice_rationale: prev.voice_rationale
          || "Slow, elegant footage — a low, intimate narrator sits under the visuals.",
      };
      await this._emit(h, this._evt(id, "voiceover.script",
        { script, voice_id: layers.voiceover.voice_id, tone: layers.voiceover.tone }));
      await this._emit(h, this._evt(id, "tool.started", { tool_name: "generate_voiceover" }));
      await this._emit(h, this._evt(id, "voiceover.generating", layers.voiceover));
      if (h.onEvent) h.onEvent(this._opened(id));
      setTimeout(() => {
        const r2 = this.sessions[id]; if (!r2) return;
        r2.state.layers.voiceover = Object.assign({}, r2.state.layers.voiceover, { status: "completed", audio_url: previewWav(VO_S) });
        if (h.onEvent) h.onEvent(this._opened(id, false));
      }, this.delay ? 2200 : 0);
    }

    // ---- SFX (treatment card -> spotted plan -> rendered variants) ------
    // The ONE treatment card: register + density fused, grounded in the
    // observation, agent's pick marked (mirrors the real SFX TREATMENT rule).
    async _askSfxTreatment(id, h) {
      const rec = this._rec(id);
      const obs = rec.state.observation || {};
      const cuts = (obs.scenes || []).length;
      const dur = Math.round(Number(obs.duration_s || 15));
      const clarification = {
        question: "What should the sound effects feel like?",
        topic: "sfx_treatment",
        options: [
          { id: "editorial_accents", label: "Editorial accents",
            hint: `${Math.max(1, Math.min(3, cuts || 2))} stylized hits on the cuts, ducked under everything else`,
            recommended: true },
          { id: "diegetic", label: "Diegetic / foley",
            hint: "Only sounds the scene would really make — nothing stylized" },
          { id: "ambience_only", label: "Ambience only",
            hint: "One continuous background bed, no discrete hits" },
        ],
      };
      await this._emit(h, this._evt(id, "agent.reasoning",
        { status: "Sizing the sound design…", intent: "add_sfx",
          thought: `${dur}s, ${cuts || "a few"} cuts — deciding how much sound this footage actually wants.` }));
      this._addMessage(rec, "assistant",
        `I watched it — ${dur}s with ${cuts || "a few"} cuts, and the clip's own audio already carries some of the room. Before I spot anything: what should the effects feel like? Fewer is usually better here.`);
      rec.state.pending_clarification = clarification;
      await this._emit(h, this._evt(id, "clarify.cards", clarification));
      if (h.onEvent) h.onEvent(this._opened(id));
    }

    async _answerSfxTreatment(id, optionId, h) {
      const rec = this._rec(id);
      const pending = rec.state.pending_clarification || {};
      const opt = (pending.options || []).find((o) => o.id === optionId) || { id: optionId, label: optionId };
      this._addMessage(rec, "user", opt.label);
      rec.state.pending_clarification = null;
      rec.state.sfx_treatment = { id: opt.id, label: opt.label, hint: opt.hint || null, source: "card" };
      return this._spotAndAnnounce(id, h);
    }

    // Spot within the settled treatment + announce the plan (shared by the
    // card answer, the typed answer, a later "add sfx", and "denser").
    async _spotAndAnnounce(id, h, opts) {
      const rec = this._rec(id);
      const t = rec.state.sfx_treatment || { label: "your treatment" };
      await this._emit(h, this._evt(id, "agent.reasoning",
        { status: "Spotting sound moments…", intent: "add_sfx",
          thought: `Spotting within "${t.label}" — only moments the footage earns.` }));
      const sfx = this._spotSfxPlan(rec, opts);
      await this._emit(h, this._evt(id, "tool.started", { tool_name: "plan_sfx" }));
      await this._emit(h, this._evt(id, "sfx.plan", sfx));
      const n = sfx.events.length;
      this._addMessage(rec, "assistant",
        n === 0
          ? `${t.label} it is — no discrete hits, one continuous bed ("${sfx.ambience}"). Generate when you're ready.`
          : sfx.over_budget
            ? `Denser it is — all ${n} moments are in, which puts the plan over the usual budget (the card flags it). Prune any, then generate.`
            : `${t.label} it is — I spotted ${n} moment${n === 1 ? "" : "s"} the footage earns (the plan shows why each one). Prune anything, then generate when you're ready.`);
      if (h.onEvent) h.onEvent(this._opened(id));
    }

    // Spot a mock plan from the observed scenes (mirrors plan_sfx). Free.
    // Honors the answered treatment + the visible density budget (~1 per 5s).
    // opts.denser: keep every spotted moment (over_budget then flags, exactly
    // like the real tool — the plan is never silently trimmed there either).
    _spotSfxPlan(rec, opts) {
      const obs = rec.state.observation || {};
      const scenes = obs.scenes || [];
      const duration = Number(obs.duration_s || (scenes.length ? Number(scenes[scenes.length - 1].start_s || 0) + 3 : 15));
      const cap = Math.max(1, Math.round(duration / 5));
      const treatment = rec.state.sfx_treatment || null;
      let events = scenes.length
        ? scenes.map((s, i) => ({
            id: `sfx_event_${String(i + 1).padStart(3, "0")}`,
            label: i === scenes.length - 1 ? "Deep impact on the final frame" : "Whoosh on the scene change",
            prompt: i === scenes.length - 1 ? "deep cinematic impact boom" : "airy transition whoosh",
            start_s: Number(s.start_s || 0),
            reason: i === scenes.length - 1 ? "the close lands here" : "hard cut",
          }))
        : [{ id: "sfx_event_001", label: "Impact hit", prompt: "deep impact", start_s: 0, reason: "opening frame" }];
      if (treatment && treatment.id === "ambience_only") events = [];
      else if (!(opts && opts.denser)) events = events.slice(0, cap);
      if (!events.length && !(treatment && treatment.id === "ambience_only")) {
        events = [{ id: "sfx_event_001", label: "Impact hit", prompt: "deep impact", start_s: 0, reason: "opening frame" }];
      }
      const layers = rec.state.layers || (rec.state.layers = {});
      const prev = (layers.sfx && typeof layers.sfx === "object" && !Array.isArray(layers.sfx)) ? layers.sfx : {};
      layers.sfx = {
        status: "draft",
        summary: treatment && treatment.id === "ambience_only"
          ? "A single continuous bed — no discrete hits."
          : "Motion-matched accents only where the footage earns them.",
        ambience: treatment && treatment.id === "ambience_only" ? "soft room-energy bed" : (prev.ambience || ""),
        events,
        variants: prev.variants || [],
        selected_variant_id: prev.selected_variant_id || null,
        treatment,
        density_cap: cap,
        cap_note: events.length
          ? `Capped at ${cap} effect${cap === 1 ? "" : "s"} for ${Math.round(duration)}s — say 'denser' to override.`
          : "",
        over_budget: events.length > cap,
      };
      return layers.sfx;
    }

    async _sfx(id, payload, h) {
      const rec = this._rec(id);
      const layers = rec.state.layers || (rec.state.layers = {});
      let sfx = (layers.sfx && typeof layers.sfx === "object" && !Array.isArray(layers.sfx)) ? layers.sfx : null;
      // Bare select — free, no render.
      if (payload.select_variant_id && !(payload.sfx_events && payload.sfx_events.length)) {
        if (sfx && (sfx.variants || []).some((v) => v.variant_id === payload.select_variant_id)) {
          sfx.selected_variant_id = payload.select_variant_id;
          await this._emit(h, this._evt(id, "sfx.generating", sfx));
          if (h.onEvent) h.onEvent(this._opened(id));
        }
        return;
      }
      // Ghost suggestions (free) — deterministic canned proposals + accept/
      // reject transitions, mirroring the real hybrid-proposer contract.
      if (payload.sfx_suggest) {
        if (!sfx) sfx = this._spotSfxPlan(rec);
        const stored = sfx.suggestions || (sfx.suggestions = []);
        const canned = payload.sfx_suggest === "narrative"
          ? [{ start: 9.6, prompt: "distant crowd murmur swelling under the reveal",
               why: "an offscreen audience grounds the emotional turn" }]
          : [{ start: 3.25, prompt: "deep cinematic whoosh transition",
               why: "detected scene cut at 3.40s (cinematic style)" },
             { start: 7.0, prompt: "low riser swelling into the next shot",
               why: "detected scene cut at 7.10s (cinematic style)" }];
        canned.forEach((c, i) => {
          const id = "sug_" + String(stored.length + 1).padStart(3, "0");
          if (stored.some((s) => s.sound_prompt === c.prompt)) return;
          stored.push({ suggestion_id: id, origin: payload.sfx_suggest === "narrative" ? "narrative" : "stylistic",
            start_time: c.start, end_time: c.start + 0.7, description: c.why,
            sound_prompt: c.prompt, rationale: c.why, status: "pending" });
        });
        await this._emit(h, this._evt(id, "sfx.plan", sfx));
        if (h.onEvent) h.onEvent(this._opened(id));
        return;
      }
      if (payload.suggestion_action) {
        const stored = (sfx && sfx.suggestions) || [];
        const hit = stored.find((s) => s.suggestion_id === payload.suggestion_id);
        if (hit) {
          if (payload.suggestion_action === "accept" && hit.status !== "accepted") {
            sfx.events.push({ id: "sfx_ev_" + (sfx.events.length + 1), label: hit.sound_prompt,
              prompt: hit.sound_prompt, start_s: hit.start_time, reason: hit.rationale });
            sfx.events.sort((a, b) => (a.start_s || 0) - (b.start_s || 0));
          }
          hit.status = payload.suggestion_action === "accept" ? "accepted" : "rejected";
        }
        await this._emit(h, this._evt(id, "sfx.plan", sfx));
        if (h.onEvent) h.onEvent(this._opened(id));
        return;
      }
      // Plan edit from the editor: adopt the submitted rows (ids re-stamped),
      // keep existing variants. `plan_only` saves WITHOUT rendering — parity
      // with the real router's free plan-editor save.
      if (payload.sfx_events || payload.sfx_ambience != null) {
        if (!sfx) sfx = this._spotSfxPlan(rec);
        const evs = (payload.sfx_events || []).map((ev, i) => {
          const label = String(ev.label || `Effect ${i + 1}`);
          const out = {
            id: `sfx_ev_${i + 1}`,
            label,
            prompt: String(ev.prompt || label),
            start_s: Math.max(0, Number(ev.start_s) || 0),
          };
          if (ev.reason) out.reason = String(ev.reason);
          return out;
        });
        sfx.events = evs;
        if (payload.sfx_summary != null) sfx.summary = String(payload.sfx_summary);
        if (payload.sfx_ambience != null) sfx.ambience = String(payload.sfx_ambience);
        const cap = Math.max(2, Math.round(((rec.state.observation || {}).duration_s || 15) / 5));
        sfx.cap_note = `${evs.length} effect${evs.length === 1 ? "" : "s"} planned · budget ~${cap} for this cut`;
        sfx.over_budget = evs.length > cap;
        if (payload.plan_only) {
          await this._emit(h, this._evt(id, "sfx.plan", sfx));
          if (h.onEvent) h.onEvent(this._opened(id));
          return;
        }
      }
      // Ambience-only plans (zero events, a bed) are renderable — only a
      // session with no plan at all needs a fresh spot.
      if (!sfx || (!(sfx.events || []).length && !(sfx.ambience || "").trim())) sfx = this._spotSfxPlan(rec);
      const variants = sfx.variants || (sfx.variants = []);
      const n = variants.length + 1;
      const variantId = `sfx_variant_${n}`;
      variants.push({
        variant_id: variantId, label: `SFX take ${n}`, status: "queued",
        linked_job_id: `job_sfx_${n}`, audio_url: null, video_url: null,
      });
      sfx.status = "queued";
      // NOT selected here — mirrors the real backend, which stopped claiming a
      // choice nobody could have made: nothing has rendered, nobody has heard
      // it, and a compose in that window would mix a take with no audio.
      await this._emit(h, this._evt(id, "tool.started", { tool_name: "generate_sfx" }));
      await this._emit(h, this._evt(id, "sfx.generating", sfx));
      if (h.onEvent) h.onEvent(this._opened(id));
      setTimeout(() => {
        const r2 = this.sessions[id]; if (!r2) return;
        const layer = r2.state.layers.sfx;
        if (!layer) return;
        const v = (layer.variants || []).find((x) => x.variant_id === variantId);
        if (v) {
          v.status = "completed"; v.audio_url = previewWav(MUSIC_S); v.video_url = null;
          // What the render measured, and which product it is — the real
          // backend carries both, so the mock has to or the console is tested
          // against a take that never says anything about itself.
          v.take_signals = { bed_peak_dbfs: -6.2, bed_mean_dbfs: -22.0 };
          v.listen_report = { clean: true, notes: [], observations: [], unchecked: [] };
          v.watched_the_video = false;
          v.not_watched_reason = "the clip has no URL the engine can fetch";
        }
        layer.status = "completed";
        if (!layer.selected_variant_id) layer.selected_variant_id = variantId;
        if (h.onEvent) h.onEvent(this._opened(id, false));
      }, this.delay ? 2600 : 0);
    }

    // Approving a proposal is the spend gate: generate two candidates (queued),
    // then hydrate them to "completed" after a short beat (mirrors the worker).
    async _approveProposal(id, proposalId, h, payload) {
      const rec = this._rec(id);
      rec.state.approved_direction = true;
      rec.state.approved_proposal_id = proposalId; // mirrors the real backend
      const prop = (rec.state.proposals || []).find((p) => p.proposal_id === proposalId) || {};
      // The tier rides the approval click, and the user's pick outranks the
      // director's — the server writes it onto the proposal before generation
      // reads it, so the mock must too, or the take count and every tier badge
      // downstream tell a different story here than in production.
      const picked = (payload || {}).modelspec;
      if (picked) { prop.modelspec = picked; prop.modelspec_source = "user"; }
      await this._emit(h, this._evt(id, "tool.started", { tool_name: "generate_candidates" }));
      // Take count is model-dependent: basic → 1, enhanced/studio → 2.
      const takeCount = (prop.modelspec || "edenn_enhanced") === "edenn_basic" ? 1 : 2;
      const cands = Array.from({ length: takeCount }, (_, i) => i + 1).map((v) => ({
        candidate_id: `candidate_${proposalId}_${v}`, proposal_id: proposalId,
        title: `${prop.title || "Direction"} · take ${v}`, prompt: prop.prompt || "",
        modelspec: prop.modelspec || "edenn_enhanced", status: "queued",
        audio_url: null, video_url: null, version: v, music_volume: prop.music_volume || 0.85,
      }));
      rec.state.candidates = cands;
      rec.state.phase = "generating_candidates";
      await this._emit(h, this._evt(id, "candidate.cards", { candidates: cands }));
      this._addMessage(rec, "assistant", "Creating your takes.");
      if (h.onEvent) h.onEvent(this._opened(id));

      // hydrate after a short delay so the UI shows queued → completed (or failed)
      const status = this._hydrateStatus();
      setTimeout(() => {
        const r2 = this.sessions[id]; if (!r2) return;
        r2.state.candidates = r2.state.candidates.map((c) => ({
          ...c, status, audio_url: status === "completed" ? previewWav(MUSIC_S) : null, video_url: null,
          // The take is one window of a longer piece; without the track behind
          // it the re-cut verb has nothing to offer and the browser suite could
          // never reach it. The window the matcher chose comes with it.
          complete_audio_url: status === "completed" ? previewWav(MUSIC_S * 4) : null,
          window: status === "completed"
            ? { start_s: 0, source: "matcher", full_duration_s: MUSIC_S * 4 }
            : null,
          listen_report: status === "completed"
            ? { clean: true, notes: [], observations: ["energy builds across the take"],
                measured: { cut_duration_s: MUSIC_S, full_duration_s: MUSIC_S * 4 } }
            : null,
        }));
        r2.state.phase = "awaiting_candidate_choice";
        if (h.onEvent) h.onEvent(this._opened(id, false));
        // Collab demo: collaborators "reviewed while you generated".
        if (status === "completed") this._seedCollab(id);
      }, this.delay ? 2600 : 0);
    }

    async _finalize(id, candidateId, h) {
      const rec = this._rec(id);
      const cid = candidateId || rec.state.selected_candidate_id;
      const cand = (rec.state.candidates || []).find((c) => c.candidate_id === cid);
      if (!cand) {
        if (h.onEvent) h.onEvent(this._evt(id, "error", { message: "Candidate not found." }));
        return;
      }
      rec.state.selected_candidate_id = cid;
      await this._emit(h, this._evt(id, "candidate.selected", { candidate_id: cid }));
      rec.state.final_artifact = { video_url: null, audio_url: cand.audio_url, deliverable: "music_candidate", candidate_id: cid };
      rec.state.phase = "completed";
      await this._emit(h, this._evt(id, "final.artifact", rec.state.final_artifact));
      this._addMessage(rec, "assistant", "Take selected.");
      if (((rec.state.production_plan || {}).layers || []).includes("voiceover")) this._draftNarration(rec);
      if (h.onEvent) h.onEvent(this._opened(id));
    }

    _draftNarration(rec) {
      if (rec.state.layers.voiceover) return;
      rec.state.layers.voiceover = {
        script: "Every moment has a rhythm. Find yours, and make it count.",
        voice_id: "warm_female", tone: "warm and confident", status: "draft", audio_url: null,
      };
      this._addMessage(rec, "assistant", "Review your narration, then record it.");
    }

    async _answerIntent(id, optionId, h, payload = {}) {
      const rec = this._rec(id);
      const opt = INTENT_OPTIONS.find((o) => o.id === optionId) || INTENT_OPTIONS[0];

      // Say back what was actually ticked. The option label ("Music only") can
      // describe a different set from the one the picker sent, and this text is
      // the user's own turn in the transcript — mirrors the server.
      const NAMES = { music: "Music", voiceover: "Voice-over", sfx: "Sound effects" };
      const picked = (((payload || {}).layers) || []).filter((l) => NAMES[l]);
      this._addMessage(rec, "user",
        picked.length ? picked.map((l) => NAMES[l]).join(" + ") : opt.label);
      rec.state.pending_clarification = null;

      await this._emit(h, this._evt(id, "agent.reasoning",
        { status: "Locking the plan…", intent: "plan_audio", thought: `Sequencing for: ${opt.label.toLowerCase()}.` }));

      const plan = planForIntent(optionId, payload);
      rec.state.production_plan = { mode: plan.mode, layers: plan.layers.slice() };
      // Seed effects only inside the frontend preview.
      if (payload.layers && payload.layers.includes("sfx")) {
        rec.state.layers.sfx = seedSfx();
        if (!rec.state.production_plan.layers.includes("sfx")) rec.state.production_plan.layers.push("sfx");
      }
      this._toolCall(rec, "set_production_plan", "completed");
      await this._emit(h, this._evt(id, "production.plan", { mode: plan.mode, layers: plan.layers.slice() }));

      // SFX-only plan: no music directions — ask the ONE treatment card first
      // (mirrors the real agent's SFX TREATMENT rule), then spot within it.
      if (optionId === "sound_design") {
        rec.state.phase = "awaiting_plan_choice";
        rec.state.memory.recent_intents.push("add_sfx");
        return this._askSfxTreatment(id, h);
      }

      // Voice-over only: there are no music directions to pick, so the turn ends
      // on a narration draft the user can read and record.
      if (optionId === "voiceover_only") {
        this._draftNarration(rec);
        if (h.onEvent) h.onEvent(this._opened(id));
        return;
      }

      await this._emit(h, this._evt(id, "agent.reasoning",
        { status: "Lining up directions…", intent: "request_proposals", thought: "Sketching two options against the cuts." }));

      const planWord = optionId === "music_only" ? "music first"
        : optionId === "voiceover_only" ? "the voiceover" : "the full build";
      this._addMessage(rec, "assistant",
        `Great — ${planWord}. Two directions against your video — pick one, or tell me what you'd rather hear.`);
      rec.state.proposals = this._proposals();
      rec.state.phase = "awaiting_plan_choice";
      rec.state.memory.recent_intents.push("plan_audio");
      await this._emit(h, this._evt(id, "proposal.cards", { proposals: rec.state.proposals }));

      if (h.onEvent) h.onEvent(this._opened(id));
    }

    _proposals() {
      return [
        { proposal_id: "proposal_arc", title: "Arc-driven electronic",
          prompt: "Uptempo electronic that builds with the cuts; drops land on the scene changes.",
          modelspec: "edenn_enhanced", include_vocals: false, vocal_gender: "female", music_volume: 0.85,
          // `attributes` is what the comparison table renders. Nothing in the
          // real backend emits it yet — MusicProposalCard carries only
          // title/prompt/modelspec/include_vocals — so this is the shape to fill
          // in server-side. The table renders a row only when a proposal
          // actually has that field, so it degrades cleanly until then.
          attributes: {
            summary: "Builds with the cuts; drops land on the scene changes.",
            energy: [10, 14, 20, 42, 38, 40, 58, 52, 56, 74, 66, 78],
            energy_note: "Builds to the last cut",
            bpm: 128, tempo_label: "uptempo",
            instruments: ["Synth bass", "Arp", "Drums"],
            feel: ["Driving", "Modern", "Punchy"],
            best_for: "Fast-cut reels that need lift",
          } },
        { proposal_id: "proposal_ambient", title: "Cinematic ambient",
          prompt: "Slower, textural cinematic bed — warm, premium, emotional close.",
          modelspec: "edenn_enhanced", include_vocals: true, vocal_gender: "female", music_volume: 0.8,
          attributes: {
            summary: "A warm textural bed that stays under the picture.",
            energy: [18, 20, 24, 28, 34, 42, 50, 58, 64, 68, 70, 70],
            energy_note: "Even swell, no peaks",
            bpm: 70, tempo_label: "slow",
            instruments: ["Strings", "Pads", "Piano"],
            feel: ["Warm", "Premium", "Emotional"],
            best_for: "Slower, story-led edits",
          } },
      ];
    }

    // ---- a free-text user message ---------------------------------------
    async _handleMessage(id, content, h) {
      const rec = this._rec(id);
      if (!content.trim()) {
        if (h.onEvent) h.onEvent(this._evt(id, "error", { message: "content is required." }));
        return;
      }
      this._addMessage(rec, "user", content.trim());

      // If a clarify is pending, treat free text as answering THAT question.
      // A pending TREATMENT card answered in text becomes the treatment — it
      // must never fall through to the intent guess (which would silently
      // rewrite an sfx-only production plan to music).
      if (rec.state.pending_clarification) {
        if (rec.state.pending_clarification.topic === "sfx_treatment") {
          rec.state.pending_clarification = null;
          rec.state.sfx_treatment = {
            id: "typed",
            label: content.trim().slice(0, 60),
            notes: content.trim(),
            source: "card",
          };
          return this._spotAndAnnounce(id, h);
        }
        const lc = content.toLowerCase();
        const guess = lc.includes("voice") ? "voiceover_only"
          : /\b(sfx|sound[- ]?effects?|sound design)\b/.test(lc) ? "sound_design"
          : lc.includes("full") ? "full_audio" : "music_only";
        rec.state.pending_clarification = null;
        return this._answerIntent_fromText(id, guess, h);
      }

      // SFX intent: treatment card first (register + density are unknown until
      // the user answers); once answered, spot within it. Only with a video.
      // "denser" is the documented cap override — replan with every spotted
      // moment so the over-budget flag genuinely exercises.
      const lc = content.toLowerCase();
      if (rec.state.sfx_treatment && /\bdenser\b/.test(lc) && rec.state.observation) {
        return this._spotAndAnnounce(id, h, { denser: true });
      }
      if (/\b(sfx|sound[- ]?effects?|whoosh(es)?|impacts?|foley|swoosh(es)?)\b/.test(lc) && rec.state.observation) {
        if (!rec.state.sfx_treatment) {
          return this._askSfxTreatment(id, h);
        }
        return this._spotAndAnnounce(id, h);
      }

      await this._emit(h, this._evt(id, "agent.reasoning",
        { status: "Thinking…", intent: "other", thought: "Factoring your direction in." }));
      this._addMessage(rec, "assistant", "Noted — I'll factor that into the directions I bring back.");
      if (h.onEvent) h.onEvent(this._opened(id));
    }

    async _answerIntent_fromText(id, optionId, h) {
      // Same as _answerIntent but the user message was already appended.
      const rec = this._rec(id);
      const plan = planForIntent(optionId, null);
      await this._emit(h, this._evt(id, "agent.reasoning",
        { status: "Locking the plan…", intent: "plan_audio", thought: "Sequencing the layers." }));
      rec.state.production_plan = { mode: plan.mode, layers: plan.layers.slice() };

      await this._emit(h, this._evt(id, "production.plan", { mode: plan.mode, layers: plan.layers.slice() }));
      if (optionId === "voiceover_only") {
        this._draftNarration(rec);
        if (h.onEvent) h.onEvent(this._opened(id));
        return;
      }
      rec.state.phase = "awaiting_plan_choice";
      this._addMessage(rec, "assistant",
        "Great — I'll sketch two directions against your video and bring them back to choose from.");
      rec.state.proposals = this._proposals();
      if (h.onEvent) h.onEvent(this._opened(id));
    }

    // ---- REST: snapshot (reconnect / poll) ------------------------------
    getSnapshot(id) { return this.snapshot(id); }

    // ======================================================================
    // Collab (comment threads on the lineage canvas) — mirrors the REAL
    // /sessions/{id}/collab contract: same payload shapes, same comment.*
    // event vocabulary through the connection (the mock's "WS").
    // ======================================================================
    _collab(rec) {
      if (!rec.collab) {
        const past = (mins) => new Date(Date.now() - mins * 60000).toISOString();
        rec.collab = {
          threads: [], comments: [], reads: {}, seeded: false,
          participants: [
            { user_id: "maya", display_name: "Maya Chen", role: "iterate", added_at: past(60 * 24) },
            { user_id: "ken", display_name: "Ken Ito", role: "comment", added_at: past(60 * 20) },
          ],
        };
      }
      return rec.collab;
    }
    _agents() { return [{ id: "edenn", name: "Edenn Director", kind: "agent" }]; }

    _emitCollab(id, type, payload) {
      const rec = this.sessions[id];
      const h = rec && rec.lastHandlers;
      if (h && h.onEvent) h.onEvent(this._evt(id, type, payload));
    }

    _threadPayload(rec, threadId, viewer) {
      const cl = this._collab(rec);
      const t = cl.threads.find((x) => x.thread_id === threadId);
      if (!t) throw new Error(`Thread not found: ${threadId}`);
      const comments = cl.comments.filter((c) => c.thread_id === threadId);
      const lastRead = cl.reads[`${threadId}|${viewer || "you"}`] || null;
      const unread = viewer
        ? comments.filter((c) => c.author_id !== viewer && !c.deleted
            && (!lastRead || c.created_at > lastRead)).length
        : 0;
      return Object.assign({}, t, { comments: comments.map((c) => Object.assign({}, c)), unread });
    }

    getCollab(id, viewer) {
      const rec = this._rec(id);
      const cl = this._collab(rec);
      return {
        threads: cl.threads.map((t) => this._threadPayload(rec, t.thread_id, viewer || "you")),
        participants: cl.participants.map((p) => Object.assign({}, p)),
        agents: this._agents(),
        viewer: viewer || "you",
      };
    }

    _mkComment(rec, threadId, req) {
      const cl = this._collab(rec);
      const comment = {
        comment_id: newId("comment"),
        thread_id: threadId,
        author_id: req.author_id || "you",
        author_name: req.author_name || (req.author_id === "edenn" ? "Edenn Director" : "You"),
        author_kind: req.author_kind || "user",
        body: String(req.body || "").trim(),
        deleted: false,
        mentions: (req.mentions || []).slice(),
        attachments: (req.attachments || []).slice(),
        reactions: {},
        edited_at: null,
        created_at: req.created_at || nowIso(),
      };
      cl.comments.push(comment);
      const t = cl.threads.find((x) => x.thread_id === threadId);
      if (t) t.updated_at = comment.created_at;
      return comment;
    }

    createThread(id, req) {
      const rec = this._rec(id);
      const cl = this._collab(rec);
      const thread = {
        thread_id: newId("thread"),
        session_id: id,
        anchor_node_id: req.anchor_node_id,
        anchor_label: req.anchor_label || null,
        anchor_start_s: req.anchor_start_s != null ? req.anchor_start_s : null,
        anchor_end_s: req.anchor_end_s != null ? req.anchor_end_s : null,
        status: "open", resolved_by: null, resolved_at: null,
        created_by: req.author_id || "you",
        created_at: req.created_at || nowIso(), updated_at: req.created_at || nowIso(),
      };
      cl.threads.push(thread);
      this._mkComment(rec, thread.thread_id, req);
      this.markThreadRead(id, thread.thread_id, req.author_id || "you");
      const payload = this._threadPayload(rec, thread.thread_id, req.author_id || "you");
      if (!req._silent) {
        this._emitCollab(id, "comment.thread.created", { thread: payload });
        this._maybeAgentPickup(id, thread, req);
      }
      return { thread: payload };
    }

    addThreadComment(id, threadId, req) {
      const rec = this._rec(id);
      const cl = this._collab(rec);
      const comment = this._mkComment(rec, threadId, req);
      const t = cl.threads.find((x) => x.thread_id === threadId);
      if (t && t.status === "resolved") { t.status = "open"; t.resolved_by = null; t.resolved_at = null; }
      this.markThreadRead(id, threadId, req.author_id || "you");
      if (!req._silent) {
        this._emitCollab(id, "comment.created", { thread_id: threadId, comment: Object.assign({}, comment) });
        this._maybeAgentPickup(id, t, req);
      }
      return { comment: Object.assign({}, comment), thread: this._threadPayload(rec, threadId, req.author_id || "you") };
    }

    setThreadStatus(id, threadId, status, actorId) {
      const rec = this._rec(id);
      const t = this._collab(rec).threads.find((x) => x.thread_id === threadId);
      if (!t) throw new Error(`Thread not found: ${threadId}`);
      t.status = status;
      t.resolved_by = status === "resolved" ? (actorId || "you") : null;
      t.resolved_at = status === "resolved" ? nowIso() : null;
      t.updated_at = nowIso();
      const payload = this._threadPayload(rec, threadId, actorId || "you");
      this._emitCollab(id, "comment.thread.updated", { thread: payload });
      return { thread: payload };
    }

    markThreadRead(id, threadId, viewer) {
      const rec = this._rec(id);
      this._collab(rec).reads[`${threadId}|${viewer || "you"}`] = nowIso();
      return { ok: true };
    }

    editComment(id, commentId, body) {
      const rec = this._rec(id);
      const c = this._collab(rec).comments.find((x) => x.comment_id === commentId);
      if (!c) throw new Error(`Comment not found: ${commentId}`);
      if (c.deleted) throw new Error("That comment was deleted."); // parity with the real API
      c.body = String(body || "").trim();
      c.edited_at = nowIso();
      this._emitCollab(id, "comment.updated", { thread_id: c.thread_id, comment: Object.assign({}, c) });
      return { comment: Object.assign({}, c) };
    }

    deleteComment(id, commentId) {
      const rec = this._rec(id);
      const c = this._collab(rec).comments.find((x) => x.comment_id === commentId);
      if (!c) throw new Error(`Comment not found: ${commentId}`);
      c.deleted = true;
      c.body = "";
      this._emitCollab(id, "comment.updated", { thread_id: c.thread_id, comment: Object.assign({}, c) });
      return { comment: Object.assign({}, c) };
    }

    reactComment(id, commentId, emoji, on, actorId) {
      const rec = this._rec(id);
      const c = this._collab(rec).comments.find((x) => x.comment_id === commentId);
      if (!c) throw new Error(`Comment not found: ${commentId}`);
      const users = c.reactions[emoji] || (c.reactions[emoji] = []);
      const who = actorId || "you";
      const idx = users.indexOf(who);
      if (on && idx < 0) users.push(who);
      if (!on && idx >= 0) users.splice(idx, 1);
      if (!users.length) delete c.reactions[emoji];
      this._emitCollab(id, "comment.updated", { thread_id: c.thread_id, comment: Object.assign({}, c) });
      return { comment: Object.assign({}, c) };
    }

    upsertParticipant(id, req) {
      const rec = this._rec(id);
      const cl = this._collab(rec);
      let p = cl.participants.find((x) => x.user_id === req.user_id);
      if (p) { p.role = req.role; if (req.display_name) p.display_name = req.display_name; }
      else {
        p = { user_id: req.user_id, display_name: req.display_name || req.user_id, role: req.role, added_at: nowIso() };
        cl.participants.push(p);
      }
      this._emitCollab(id, "participant.updated", { participant: Object.assign({}, p) });
      return { participant: Object.assign({}, p) };
    }

    // Demo seed — "collaborators reviewed while you generated". Runs once, when
    // the first takes hydrate; content mirrors the design-spec cast, adapted to
    // this session's roster. All through the same contract methods (no
    // client-side ghost state).
    _seedCollab(id) {
      const rec = this.sessions[id];
      if (!rec) return;
      const cl = this._collab(rec);
      if (cl.seeded) return;
      cl.seeded = true;
      const st = rec.state;
      const takes = (st.candidates || []).filter((c) => !c.parent_candidate_id && c.status === "completed");
      const past = (mins) => new Date(Date.now() - mins * 60000).toISOString();
      if (takes[0]) {
        const { thread } = this.createThread(id, {
          anchor_node_id: takes[0].candidate_id,
          anchor_label: takes[0].title || "Take 1",
          anchor_start_s: 12, anchor_end_s: 18,
          body: "The drop at 0:07 lands right on the product cut — this one's my pick.",
          author_id: "maya", author_name: "Maya Chen",
          attachments: [{ kind: "audio", name: "ref_take.wav", duration_s: 8, url: previewWav(8) }],
          created_at: past(42), _silent: true,
        });
        this.addThreadComment(id, thread.thread_id, {
          body: "Agreed. Could we hear the close ~10% quieter before locking it?",
          author_id: "ken", author_name: "Ken Ito", created_at: past(31), _silent: true,
        });
      }
      const altProp = (st.proposals || [])[1];
      if (altProp) {
        this.createThread(id, {
          anchor_node_id: altProp.proposal_id,
          anchor_label: altProp.title || "Direction",
          body: "Client note from this morning: they want a calmer alternative for the paid placement. Worth generating this direction too?",
          author_id: "ken", author_name: "Ken Ito", created_at: past(120), _silent: true,
        });
      }
      const src = this.createThread(id, {
        anchor_node_id: "__source__",
        anchor_label: (st.observation || {}).video_title || "Source video",
        body: "Uploaded the v3 cut with the tighter ending.",
        author_id: "maya", author_name: "Maya Chen", created_at: past(26 * 60), _silent: true,
      });
      this.addThreadComment(id, src.thread.thread_id, {
        body: "Looks great — scoring this one.",
        author_id: "you", author_name: "You", created_at: past(25 * 60), _silent: true,
      });
      const t = cl.threads.find((x) => x.thread_id === src.thread.thread_id);
      if (t) { t.status = "resolved"; t.resolved_by = "you"; t.resolved_at = past(25 * 60); }
      // Broadcast every seeded thread (the real backend broadcasts each
      // creation), so a client that adopted the session before generation
      // finished still learns about all of them.
      cl.threads.forEach((t) => {
        this._emitCollab(id, "comment.thread.created",
          { thread: this._threadPayload(rec, t.thread_id, "you") });
      });
    }

    // @agent mention → the model "picks the work up": reply in-thread, and when
    // the thread anchors a finished take, branch a real variation from it so the
    // canvas shows the handoff end-to-end (exactly what the real backend does
    // via the agent loop).
    _maybeAgentPickup(id, thread, req) {
      if (!thread) return;
      // The agent's own replies route through addThreadComment too — they must
      // never re-trigger a pickup (continuity would loop forever).
      if (req.author_kind === "agent") return;
      const rec = this.sessions[id];
      if (!rec) return;
      const mentioned = (req.mentions || []).some(
        (m) => m && (m.kind === "agent" || m.id === "edenn")
      );
      // Continuity (parity with the real backend): once the agent has spoken
      // in a thread, plain replies keep the conversation going — no re-@ needed.
      const inConversation = this._collab(rec).comments.some(
        (c) => c.thread_id === thread.thread_id && c.author_kind === "agent" && !c.deleted
      );
      if (!mentioned && !inConversation) return;
      const anchored = (rec.state.candidates || []).find(
        (c) => c.candidate_id === thread.anchor_node_id && c.status === "completed"
      );
      setTimeout(() => {
        const r2 = this.sessions[id];
        if (!r2) return;
        const reply = anchored
          ? "On it — branching a fresh take from “" + (anchored.title || "this take") + "” with that note. It lands on the canvas in a moment."
          : "On it — I'll fold that into the next pass.";
        this.addThreadComment(id, thread.thread_id, {
          body: reply, author_id: "edenn", author_name: "Edenn Director", author_kind: "agent",
        });
        if (anchored && r2.lastHandlers) {
          this._variation(id, anchored.candidate_id, r2.lastHandlers);
        }
      }, this.delay ? 1400 : 0);
    }
  }

  /**
   * The plan the user actually ticked.
   *
   * There are four intent ids but seven possible layer subsets, so the id alone
   * cannot express the selection — the client sends `payload.layers` with it and
   * the SERVER honours that over the id (see the intent gate in agent.py).
   * Mirror it here, or the offline mock silently disagrees with production about
   * what the picker means.
   */
  function planForIntent(optionId, payload) {
    const canned = PLAN_BY_INTENT[optionId] || PLAN_BY_INTENT.music_only;
    const known = ["music", "voiceover", "sfx"];
    const picked = (((payload || {}).layers) || []).filter((l) => known.indexOf(l) >= 0);
    if (!picked.length) return { mode: canned.mode, layers: canned.layers.slice() };
    const layers = picked.filter((l, i) => picked.indexOf(l) === i);
    const mode = layers.indexOf("music") >= 0 && layers.indexOf("voiceover") >= 0
      ? "full_e2e" : "music_first";
    return { mode: mode, layers: layers };
  }

  /**
   * Seeded sound effects — MOCK ONLY, and the one place this file invents audio
   * the product cannot yet produce.
   *
   * No tool writes `layers.sfx`; the field exists in the state shape and is
   * always []. Against the real backend the timeline's third lane is therefore
   * empty and says so. Here it is populated so the three-lane layout, the
   * overlap treatment and the scene-to-audio relationship can be judged at all
   * — the hits sit ON the cuts from `buildObservation()` (3.4 / 7.1 / 11.2),
   * because landing effects on cuts is the thing the visualization exists to
   * make checkable.
   */
  function seedSfx() {
    return [
      { id: "sfx_whoosh", label: "Whoosh", start_s: 3.1, end_s: 3.9 },
      { id: "sfx_impact", label: "Impact", start_s: 7.1, end_s: 7.7 },
      { id: "sfx_riser", label: "Riser", start_s: 10.4, end_s: 11.3 },
    ];
  }

  const api = { MockBackend, buildObservation, observationSummary, INTENT_OPTIONS, PLAN_BY_INTENT };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.MockBackend = MockBackend;
  root.EdennMock = api;
})(typeof window !== "undefined" ? window : globalThis);
