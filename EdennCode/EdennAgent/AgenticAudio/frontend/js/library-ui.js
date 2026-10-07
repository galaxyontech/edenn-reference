/* Audio Studio library views. Organization is browser-local; source sessions stay intact. */
(function () {
  "use strict";
  //: What each backend phase is called in the session list. Mirrors
  //: AgenticAudioSessionPhase; a contract test fails if the enum grows a
  //: member this does not name.
  const SESSION_PHASES = {
    created: "Starting",
    observing: "Watching your video",
    proposing: "Finding a direction",
    awaiting_plan_choice: "Awaiting choice",
    generating_candidates: "Working",
    awaiting_candidate_choice: "Awaiting choice",
    composing: "Mixing",
    completed: "Done",
    failed: "Needs attention",
  };
  const directions = [
    { id: "momentum", title: "Momentum", style: "Electronic", bpm: 124, instruments: "Synth · Drums", energy: "Build", arc: [12,18,27,25,42,39,58,66], prompt: "Driving electronic music with a gradual build and accents on scene cuts." },
    { id: "first-light", title: "First light", style: "Ambient", bpm: 72, instruments: "Piano · Pads", energy: "Swell", arc: [12,15,22,31,40,47,50,48], prompt: "Warm ambient music with soft piano and a gentle swell. Keep the sound spacious." },
    { id: "open-road", title: "Open road", style: "Cinematic", bpm: 96, instruments: "Strings · Percussion", energy: "Rise", arc: [13,17,24,34,48,60,65,59], prompt: "An expansive cinematic score with strings, a restrained opening and an uplifting finish." },
    { id: "slow-sunday", title: "Slow Sunday", style: "Acoustic", bpm: 86, instruments: "Guitar · Keys", energy: "Steady", arc: [28,32,29,34,31,35,32,28], prompt: "Gentle acoustic guitar and warm understated percussion for a relaxed everyday story." },
    { id: "after-hours", title: "After hours", style: "Electronic", bpm: 110, instruments: "Bass · Keys", energy: "Pulse", arc: [28,42,30,45,32,43,31,38], prompt: "Moody electronic music with a steady bass pulse and restrained drums for night scenes." },
    { id: "small-wonders", title: "Small wonders", style: "Cinematic", bpm: 88, instruments: "Piano · Plucks", energy: "Lift", arc: [15,28,21,39,31,46,53,45], prompt: "Playful cinematic music with light percussion and delicate plucked strings, curious and optimistic." },
  ];
  function node(tag, cls, text) {
    const n = document.createElement(tag); if (cls) n.className = cls;
    if (text != null) n.textContent = text; return n;
  }
  function button(label, cls, run) {
    const b = node("button", cls, label); b.type = "button"; if (run) b.addEventListener("click", run); return b;
  }
  function icon(name) { const i = node("i", "ti ti-" + name); i.setAttribute("aria-hidden", "true"); return i; }
  function time(value) { const s = Math.max(0, Math.floor(Number(value) || 0)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }
  function energyGraph(item) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 280 80"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", item.energy + " energy direction");
    svg.classList.add("library-energy");
    const path = document.createElementNS(svg.namespaceURI, "path");
    path.setAttribute("d", item.arc.map((v, i) => (i ? "L" : "M") + (i * 40) + " " + (75 - v)).join(" "));
    path.setAttribute("fill", "none"); path.setAttribute("stroke", "currentColor"); path.setAttribute("stroke-width", "1.8"); path.setAttribute("vector-effect", "non-scaling-stroke");
    svg.append(path); return svg;
  }
  // Original, short synthesized references communicate rhythm and energy only.
  // Their energy diagrams describe the direction, rather than claiming measured loudness.
  function sketch(item) {
    const rate = 22050, duration = 12, frames = rate * duration;
    const bytes = new ArrayBuffer(44 + frames * 2), view = new DataView(bytes);
    const str = (at, text) => { for (let i = 0; i < text.length; i++) view.setUint8(at + i, text.charCodeAt(i)); };
    str(0, "RIFF"); view.setUint32(4, bytes.byteLength - 8, true); str(8, "WAVEfmt "); view.setUint32(16, 16, true);
    view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true);
    view.setUint16(32, 2, true); view.setUint16(34, 16, true); str(36, "data"); view.setUint32(40, frames * 2, true);
    const index = directions.indexOf(item), base = [130.81,146.83,110,164.81,98,174.61][index];
    const notes = [0,7,12,4,7,16,12,7];
    for (let i = 0; i < frames; i++) {
      const t = i / rate, beat = t * item.bpm / 60, phase = beat % 1;
      const step = Math.floor(beat * (item.style === "Ambient" ? .5 : 1));
      const freq = base * Math.pow(2, notes[step % notes.length] / 12);
      const pos = t / duration * 7, left = Math.floor(pos), frac = pos - left;
      const energy = (item.arc[left] * (1 - frac) + item.arc[Math.min(7, left + 1)] * frac) / 80;
      const envelope = item.style === "Ambient" ? .55 : Math.exp(-phase * (item.style === "Acoustic" ? 7 : 4));
      let sample = Math.sin(2 * Math.PI * freq * t) * envelope * .12;
      sample += Math.sin(2 * Math.PI * base * t) * .035 + Math.sin(2 * Math.PI * base * 1.5 * t) * .025;
      if (item.style === "Electronic") sample += Math.sin(2 * Math.PI * (45 * t + .8 * (1 - Math.exp(-phase * 30)))) * Math.exp(-phase * 18) * .1;
      const fade = Math.max(0, Math.min(1, t * 8, (duration - t) * 3));
      view.setInt16(44 + i * 2, Math.round(sample * (.4 + energy) * fade * 32767), true);
    }
    return new Blob([bytes], { type: "audio/wav" });
  }
  const previewSessions = [
    ["reel_v3", "Cinematic build, warm close", "Summer campaign", "Music · VO", 14, 15, "awaiting_review"],
    ["springline_teaser", "Drops on the product cuts", "Summer campaign", "Music", 6, 32, "completed"],
    ["atlas_short_02", "A little more space to breathe", "Atlas launch", "Music · VO", 3, 16, "generating"],
    ["field_notes", "Warm piano under narration", "Field notes", "Music · VO", 21, 64, "completed"],
    ["summer_cut_04", "An energetic final lift", "Summer campaign", "Music", 8, 20, "completed"],
    ["atlas_launch", "A slower, cinematic reveal", "Atlas launch", "Music", 4, 30, "awaiting_review"],
    ["morning_walk", "Acoustic textures, close and quiet", "Field notes", "Music · VO", 5, 45, "completed"],
    ["brand_signoff", "A short and memorable ending", "Atlas launch", "Music", 9, 6, "completed"],
    ["summer_first_cut", "The original campaign direction", "Summer campaign", "Music", 4, 30, "completed"],
    ["field_notes_intro", "An earlier opening", "Field notes", "Music · VO", 7, 18, "completed"],
    ["atlas_test", "A quieter alternative", "Atlas launch", "Music", 2, 15, "completed"],
  ].map((r, index) => ({ session_id: "layout-preview-" + index, title: r[0], description: r[1], folder: r[2], layers: r[3], takes: r[4], duration: r[5], phase: r[6],
    updated_at: new Date(Date.UTC(2026, 8, 10, 15) - index * 18 * 3600000).toISOString(), artwork: index % 3, archived: index > 7 }));
  function previewPoster(item) {
    const thumbnail = node("span", "library-preview-poster");
    const art = [
      '<rect width="80" height="50" fill="#ded4bc"/><circle cx="64" cy="12" r="7" fill="#f6efe0"/><path d="M0 40L25 8 52 42 67 22 80 40V50H0Z" fill="#657666"/><path d="M0 45Q35 27 80 44V50H0" fill="#aebe9e"/>',
      '<rect width="80" height="50" fill="#d6c9b2"/><path d="M0 38H80V50H0" fill="#a57654"/><rect x="31" y="11" width="19" height="30" rx="3" fill="#ede1c4"/><rect x="35" y="8" width="11" height="4" rx="1" fill="#a57654"/>',
      '<rect width="80" height="50" fill="#cbd3d2"/><path d="M0 50L31 22H52L80 50" fill="#73888e"/><path d="M6 0H21V34H6M60 5H75V38H60" fill="#a2b5b9"/><path d="M39 50L41 31" stroke="#e7e4dc" stroke-width="2"/>',
    ];
    // Static illustrative frames for layout review; never loaded from user content.
    thumbnail.innerHTML = '<svg viewBox="0 0 80 50" aria-hidden="true">' + art[item.artwork] + '</svg>';
    thumbnail.append(node("small", "library-preview-duration", time(item.duration))); return thumbnail;
  }
  function mount({ root, page, transport, onNew, onOpen, preview = false }) {
    let destroyed = false, items = [], filter = "All", folder = "", sort = "recent", query = "", loaded = false;
    let selected = new Set(), active = null, playback = "idle", playRevision = 0, objectUrl = null;
    let noticeTimer, hideTimer, lastTrigger, dragIds = [], undoAction = null;
    const storageKey = "edenn.library.v1." + (preview ? "layout-preview" : transport.kind);
    let prefs = preview ? { folders: ["Summer campaign", "Atlas launch", "Field notes"],
      placement: Object.fromEntries(previewSessions.map(s => [s.session_id, s.folder])),
      archived: previewSessions.filter(s => s.archived).map(s => s.session_id), saved: [] }
      : { folders: [], placement: {}, archived: [], saved: [] };
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (saved && Array.isArray(saved.folders) && saved.folders.every(f => typeof f === "string") && saved.placement && typeof saved.placement === "object" && Array.isArray(saved.archived) && Array.isArray(saved.saved)) prefs = saved;
    } catch (_) { /* Local preferences are optional. */ }
    const persist = () => { try { localStorage.setItem(storageKey, JSON.stringify(prefs)); } catch (_) { notify("Browser storage is unavailable. Changes last until you leave this page."); } };
    const audio = new Audio(); audio.preload = "metadata";
    const listeners = [];
    const listen = (target, type, fn) => { target.addEventListener(type, fn); listeners.push(() => target.removeEventListener(type, fn)); };
    root.replaceChildren(); root.classList.add("library-native");
    const header = node("header", "library-header"), intro = node("div");
    intro.append(node("h1", "", page === "sessions" ? "My sessions" : "Gallery"));
    if (page === "gallery") intro.append(node("p", "", "Find the sound for your next story."));
    const headerActions = node("div", "library-header-actions");
    if (page === "sessions") headerActions.append(button("New folder", "btn-ghost", createFolder));
    headerActions.append(button("New session", "btn-primary", () => onNew())); header.append(intro, headerActions);
    const toolbar = node("div", "library-controls");
    const search = node("input", "library-search"); search.type = "search";
    search.placeholder = page === "sessions" ? "Search sessions" : "Search directions"; search.setAttribute("aria-label", search.placeholder);
    const filters = node("div", "library-filters"); filters.setAttribute("aria-label", "Filter " + page);
    // The old gallery filters were the fixture styles ("Electronic",
    // "Ambient", …). Against real sessions they match nothing, so the page
    // would look empty however much work the customer had finished.
    const choices = page === "sessions" ? ["All", "Archived"] : ["All"];
    choices.forEach(value => {
      const b = button(value, "library-filter-button", () => { filter = value; selected.clear(); render(); });
      b.dataset.filter = value; filters.append(b);
    });
    const sortSelect = node("select", "library-sort"); sortSelect.setAttribute("aria-label", "Sort " + page);
    [["recent", page === "sessions" ? "Last edited" : "Curated order"], ["name", "Name A–Z"]].forEach(([value, label]) => {
      const option = node("option", "", label); option.value = value; sortSelect.append(option);
    });
    toolbar.append(search, filters, sortSelect);
    const folders = node("div", "library-folders");
    const results = node("div", page === "sessions" ? "library-results" : "library-grid");
    const status = node("div", "library-status"); status.setAttribute("role", "status");
    status.textContent = page === "sessions" ? "Loading sessions…" : "";
    status.hidden = page === "gallery";
    const bulk = node("div", "library-bulk"); bulk.hidden = true; bulk.setAttribute("aria-label", "Selected sessions");
    const bulkLabel = node("span"), moveButton = button("Move to…", "btn-ghost", () => moveDialog([...selected]));
    const archiveButton = button("Archive", "btn-ghost", archiveSelection);
    bulk.append(bulkLabel, moveButton, archiveButton, button("Clear", "btn-ghost", () => { selected.clear(); render(); }));
    const notice = node("div", "library-notice"); notice.hidden = true; notice.setAttribute("role", "status");
    const noticeText = node("span"), undoButton = button("Undo", "btn-ghost", () => { undoAction?.(); notice.hidden = true; }); notice.append(noticeText, undoButton);
    const drawer = node("aside", "library-drawer"); drawer.hidden = true; drawer.inert = true; drawer.setAttribute("aria-label", "Direction details");
    const drawerHead = node("div", "library-drawer-head");
    drawerHead.append(node("span", "", "Direction"), button("Close", "btn-ghost", closeDrawer));
    const drawerBody = node("div", "library-drawer-body"); drawer.append(drawerHead, drawerBody);
    const dialog = node("dialog", "library-dialog");
    // The preview dock drove the example directions, which no longer appear
    // here — the gallery shows the customer's own finished work. The examples
    // are still a good way to START a session and the drawer below still
    // renders them; they belong on the new-session surface, not on a page
    // whose promise is "what you made". Until they are moved there, nothing
    // drives this dock, so it stays out of the way rather than sitting there
    // offering to play something that is not on the page.
    const dock = node("div", "library-dock"); dock.hidden = true;
    const toggle = playButton("Play preview", () => active ? togglePlayback(active) : togglePlayback(directions[0]));
    const dockMeta = node("div", "library-dock-meta"), dockTitle = node("strong", "", "Choose a sound"), dockState = node("span", "", "Preview a direction");
    dockState.setAttribute("role", "status"); dockMeta.append(dockTitle, dockState);
    const seek = node("input", "library-seek"); seek.type = "range"; seek.min = "0"; seek.max = "12"; seek.step = ".05"; seek.value = "0"; seek.disabled = true; seek.setAttribute("aria-label", "Seek preview");
    const clock = node("span", "library-clock", "0:00 / 0:12");
    const use = button("Use direction", "btn-primary", () => active && onNew(active.prompt)); use.disabled = true;
    dock.append(toggle, dockMeta, seek, clock, use);
    const scrollFrame = node("div", "library-scroll-frame");
    scrollFrame.append(results);
    root.append(header, toolbar, folders, status, scrollFrame, bulk, notice, dock, drawer, dialog);
    function updateScrollEdges() {
      scrollFrame.classList.toggle("has-content-above", results.scrollTop > 1);
      scrollFrame.classList.toggle("has-content-below", results.scrollHeight - results.clientHeight - results.scrollTop > 1);
    }
    results.addEventListener("scroll", updateScrollEdges, { passive: true });
    const edgeResize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(updateScrollEdges);
    edgeResize?.observe(results);
    const edgeContent = new MutationObserver(updateScrollEdges);
    edgeContent.observe(results, { childList: true, subtree: true });
    function notify(text, undo) {
      clearTimeout(noticeTimer); noticeText.textContent = text; undoAction = undo; undoButton.hidden = !undo;
      notice.hidden = false; noticeTimer = setTimeout(() => { notice.hidden = true; }, 6000);
    }
    function playButton(label, run) {
      const b = button("", "library-play", run); b.setAttribute("aria-label", label);
      b.append(icon("player-play-filled"), icon("player-pause-filled"), icon("rotate-clockwise"), icon("loader-2")); b.dataset.state = "idle"; return b;
    }
    function updatePlayback() {
      if (destroyed) return;
      root.querySelectorAll("[data-preview]").forEach(b => {
        const item = directions.find(d => d.id === b.dataset.preview), current = active?.id === item.id;
        const state = current ? playback : "idle"; b.dataset.state = state;
        b.setAttribute("aria-label", (state === "playing" ? "Pause " : state === "finished" ? "Replay " : state === "error" ? "Retry " : "Preview ") + item.title);
        b.setAttribute("aria-pressed", String(current && playback === "playing"));
        b.closest(".library-sound")?.classList.toggle("is-current", current);
      });
      toggle.dataset.state = playback;
      toggle.setAttribute("aria-label", playback === "playing" ? "Pause preview" : playback === "finished" ? "Replay preview" : playback === "error" ? "Retry preview" : "Play preview");
      dockTitle.textContent = active?.title || "Choose a sound";
      dockState.textContent = { idle: "Preview a direction", loading: "Loading preview…", playing: "Playing · Sound sketch", paused: "Paused · Sound sketch", finished: "Finished · Replay anytime", error: "Preview unavailable · Retry" }[playback];
      use.disabled = !active; seek.disabled = !active || playback === "error";
    }
    function resetSource(item) {
      audio.pause(); if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = URL.createObjectURL(sketch(item)); audio.src = objectUrl;
      seek.value = "0"; clock.textContent = "0:00 / 0:12";
    }
    async function togglePlayback(item) {
      if (destroyed) return;
      if (active?.id === item.id && (playback === "playing" || playback === "loading")) {
        ++playRevision; audio.pause(); playback = "paused"; updatePlayback(); return;
      }
      const rev = ++playRevision, changed = active?.id !== item.id;
      const previous = playback; active = item; playback = "loading";
      updatePlayback();
      try {
        if (changed || previous === "error" || !objectUrl) resetSource(item);
        if (previous === "finished" && !changed) audio.currentTime = 0;
        await audio.play();
        if (destroyed || rev !== playRevision) return;
        playback = "playing"; updatePlayback();
      } catch (_) {
        if (destroyed || rev !== playRevision) return;
        playback = "error"; updatePlayback();
      }
    }
    listen(audio, "timeupdate", () => {
      const t = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
      seek.value = String(t); seek.setAttribute("aria-valuetext", time(t) + " of 0:12"); clock.textContent = time(t) + " / 0:12";
    });
    listen(audio, "ended", () => { ++playRevision; playback = "finished"; seek.value = "12"; clock.textContent = "0:12 / 0:12"; updatePlayback(); });
    listen(audio, "waiting", () => { if (playback === "playing") { playback = "loading"; updatePlayback(); } });
    listen(audio, "playing", () => { if (playback === "loading" && !audio.paused) { playback = "playing"; updatePlayback(); } });
    listen(audio, "error", () => { if (active) { ++playRevision; playback = "error"; updatePlayback(); } });
    listen(audio, "pause", () => { if (playback === "playing" && !audio.ended) { playback = "paused"; updatePlayback(); } });
    seek.addEventListener("input", () => {
      if (!active) return; audio.currentTime = Number(seek.value);
      if (playback === "finished") { playback = "paused"; updatePlayback(); }
      clock.textContent = time(seek.value) + " / 0:12";
    });
    function closeDrawer() {
      drawer.classList.remove("is-open"); drawer.inert = true;
      clearTimeout(hideTimer); hideTimer = setTimeout(() => { drawer.hidden = true; }, window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 180);
      (lastTrigger?.isConnected ? lastTrigger : search).focus({ preventScroll: true });
    }
    function details(item, trigger) {
      clearTimeout(hideTimer); lastTrigger = trigger; drawerBody.replaceChildren();
      drawerBody.append(node("h2", "", item.title), energyGraph(item), node("p", "library-note", item.energy + " · " + item.style));
      const dl = node("dl"); [["Tempo", item.bpm + " BPM"], ["Instruments", item.instruments], ["Energy", item.energy]].forEach(([key, value]) => {
        const row = node("div"); row.append(node("dt", "", key), node("dd", "", value)); dl.append(row);
      });
      const preview = playButton("Preview " + item.title, () => togglePlayback(item)); preview.dataset.preview = item.id;
      const actions = node("div", "library-header-actions"); actions.append(preview, button("Use direction", "btn-primary", () => onNew(item.prompt)));
      drawerBody.append(dl, node("p", "library-note", item.prompt), actions, node("p", "library-note", "An illustration of this direction, not generated music. Your soundtrack is composed for your own video."));
      drawer.hidden = false; drawer.inert = false; void drawer.offsetWidth; drawer.classList.add("is-open"); updatePlayback();
      drawerHead.querySelector("button").focus({ preventScroll: true });
    }
    listen(root, "keydown", event => { if (event.key === "Escape" && !dialog.open && !drawer.hidden) closeDrawer(); });
    function ask(title, body, action, accept) {
      dialog.replaceChildren(); const form = node("form"), heading = node("h2", "", title); heading.id = "library-dialog-title";
      dialog.setAttribute("aria-labelledby", heading.id); const actions = node("div", "library-header-actions");
      const submit = button(action, "btn-primary"); submit.type = "submit";
      actions.append(button("Cancel", "btn-ghost", () => dialog.close()), submit); form.append(heading, body, actions);
      form.addEventListener("submit", event => { event.preventDefault(); if (accept() !== false) dialog.close(); }); dialog.append(form); dialog.showModal();
    }
    function createFolder() {
      const body = node("div"), label = node("label", "", "Folder name"), input = node("input", "library-search");
      input.id = "library-folder-name"; input.required = true; input.maxLength = 60; label.htmlFor = input.id;
      body.append(label, input, node("p", "library-note", "Folders organize sessions on this browser. They do not change access."));
      ask("New folder", body, "Create folder", () => {
        const name = input.value.trim(); input.setCustomValidity(!name ? "Enter a folder name." : prefs.folders.includes(name) ? "This folder already exists." : "");
        if (!input.reportValidity()) return false;
        prefs.folders.push(name); persist(); render(); notify("Folder created on this browser.");
      }); input.addEventListener("input", () => input.setCustomValidity(""));
    }
    function moveDialog(ids) {
      if (!prefs.folders.length) { createFolder(); return; }
      const body = node("div"), select = node("select", "library-search"), label = node("label", "", "Destination");
      select.id = "library-destination"; label.htmlFor = select.id;
      ["", ...prefs.folders].forEach(f => { const o = node("option", "", f || "Unfiled"); o.value = f; select.append(o); });
      body.append(label, select, node("p", "library-note", "Organization is saved on this browser. Session content and access stay unchanged."));
      ask("Move " + ids.length + (ids.length === 1 ? " session" : " sessions"), body, "Move", () => move(ids, select.value));
    }
    function move(ids, destination) {
      const before = { ...prefs.placement };
      ids.forEach(id => { if (destination) prefs.placement[id] = destination; else delete prefs.placement[id]; });
      selected.clear(); persist(); render(); notify("Moved " + ids.length + (ids.length === 1 ? " session" : " sessions") + ".", () => { prefs.placement = before; persist(); render(); });
    }
    function archiveSelection() {
      const before = [...prefs.archived], ids = [...selected];
      prefs.archived = filter === "Archived" ? prefs.archived.filter(id => !selected.has(id)) : [...new Set([...prefs.archived, ...ids])];
      selected.clear(); persist(); render(); notify(filter === "Archived" ? "Restored to this browser’s index." : "Archived on this browser. Source sessions are retained.", () => { prefs.archived = before; persist(); render(); });
    }
    function renderFolders() {
      folders.replaceChildren(); folders.hidden = page !== "sessions";
      if (folders.hidden) return;
      ["", ...prefs.folders].forEach(name => {
        const b = button(name || "All sessions", "library-folder", () => { folder = name; selected.clear(); render(); });
        b.prepend(icon(name ? "folder" : "list")); b.setAttribute("aria-pressed", String(folder === name));
        if (name) {
          b.addEventListener("dragover", event => { if (!dragIds.length) return; event.preventDefault(); b.classList.add("is-drop-target"); });
          b.addEventListener("dragleave", () => b.classList.remove("is-drop-target"));
          b.addEventListener("drop", event => { if (!dragIds.length) return; event.preventDefault(); const ids = dragIds; dragIds = []; move(ids, name); });
        }
        folders.append(b);
      });
    }
    function matching() {
      // A gallery is where you find what you made. Only sessions that produced
      // a deliverable belong here; the rest are still in progress and live on
      // the sessions page.
      const source = page === "sessions"
        ? items
        : items.filter(s => s.final_media_url || s.final_artifact_url);
      return source.filter(item => {
        const name = title(item);
        if (!(name + " " + (item.style || "") + " " + (item.instruments || "")).toLowerCase().includes(query.toLowerCase())) return false;
        if (page === "gallery") return true;
        return (filter === "Archived" ? prefs.archived.includes(item.session_id) : !prefs.archived.includes(item.session_id)) && (!folder || prefs.placement[item.session_id] === folder);
      }).sort((a, b) => sort === "name" ? title(a).localeCompare(title(b)) : 0);
    }
    function title(item) { return item.title || item.name || "Session " + String(item.session_id).slice(-8); }
    function refreshSelection() {
      bulk.hidden = !selected.size; bulkLabel.textContent = selected.size + " selected"; archiveButton.textContent = filter === "Archived" ? "Restore" : "Archive";
      results.querySelectorAll("[data-session]").forEach(row => {
        const checked = selected.has(row.dataset.session); row.classList.toggle("is-selected", checked); row.querySelector("input").checked = checked;
      });
      const all = results.querySelector("[data-select-all]");
      if (all) { const rows = matching(); all.checked = rows.length > 0 && rows.every(s => selected.has(s.session_id)); all.indeterminate = selected.size > 0 && !all.checked; }
    }
    function render() {
      if (destroyed) return;
      filters.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.filter === filter)));
      renderFolders(); results.replaceChildren();
      if (!loaded && page === "sessions") {
        results.setAttribute("aria-busy", "true");
        for (let i = 0; i < 4; i++) { const row = node("div", "library-skeleton"); row.setAttribute("aria-hidden", "true"); results.append(row); }
        return;
      }
      results.removeAttribute("aria-busy"); const rows = matching();
      if (!rows.length) {
        const empty = node("div", "library-empty");
        const filtered = query || filter !== "All" || folder;
        const emptyFolder = folder && !query && filter === "All";
        const title = emptyFolder ? "This folder is empty"
          : filtered ? "No matches in this view"
          : page === "gallery" ? "Nothing finished yet"
          : "No sessions yet";
        const description = emptyFolder ? "Move a session here from All sessions."
          : filtered ? "Try another search or clear your filters."
          : page === "gallery"
            ? "Finish a session and its soundtrack will appear here."
            : "Start a session to create your first soundtrack.";
        empty.append(node("h2", "", title), node("p", "", description));
        empty.append(button(query || filter !== "All" || folder ? "Clear filters" : "New session", "btn-ghost", () => {
          if (!query && filter === "All" && !folder) onNew(); else { query = ""; search.value = ""; filter = "All"; folder = ""; render(); search.focus({ preventScroll: true }); }
        })); results.append(empty); refreshSelection(); return;
      }
      if (page === "gallery") rows.forEach(item => {
        // A real finished piece: the customer's own deliverable, played from
        // the file they can download. This page used to render six invented
        // titles with browser-synthesized preview audio — a product that sells
        // generated music showing a sine-wave arpeggio as if it were its own
        // work. Nothing here is invented.
        const card = node("article", "library-sound");
        const heading = node("div", "library-sound-heading"), text = node("div");
        text.append(node("span", "library-eyebrow", "Finished"),
                    node("h2", "", title(item)));
        heading.append(text);
        card.append(heading);

        const url = item.final_media_url || item.final_artifact_url;
        const media = document.createElement(
          /\.(mp4|mov|webm)(\?|$)/i.test(String(url)) ? "video" : "audio"
        );
        media.src = url; media.controls = true; media.preload = "none";
        media.className = "library-sound-media";
        // A deliverable whose file has gone is worth saying plainly: a dead
        // player is how a customer concludes the product lost their work.
        media.addEventListener("error", () => {
          media.replaceWith(node("p", "library-sound-facts",
            "This file could not be loaded. Open the session to export it again."));
        });
        card.append(media);

        const footer = node("div", "library-sound-footer");
        const open = button("Open session", "library-action",
                            () => onOpen(item.session_id));
        open.append(icon("chevron-right"));
        footer.append(open); card.append(footer); results.append(card);
      });
      else {
        const table = node("table", "library-table"), head = node("thead"), tr = node("tr"), selectCell = node("th");
        const all = node("input"); all.type = "checkbox"; all.dataset.selectAll = ""; all.setAttribute("aria-label", "Select all visible sessions");
        all.addEventListener("change", () => { rows.forEach(item => all.checked ? selected.add(item.session_id) : selected.delete(item.session_id)); refreshSelection(); }); selectCell.append(all); tr.append(selectCell);
        (preview ? ["Session", "Layers", "Takes", "Status", "Edited", ""] : ["Session", "Status", "Edited", ""]).forEach(label => tr.append(node("th", "", label))); head.append(tr); table.append(head);
        if (preview) table.classList.add("library-table--preview");
        const body = node("tbody");
        rows.forEach(item => {
          const row = node("tr"); row.dataset.session = item.session_id; row.draggable = true;
          row.addEventListener("dragstart", event => { dragIds = selected.has(item.session_id) ? [...selected] : [item.session_id]; event.dataTransfer.setData("text/plain", "sessions"); event.dataTransfer.effectAllowed = "move"; });
          row.addEventListener("dragend", () => { dragIds = []; folders.querySelectorAll(".is-drop-target").forEach(b => b.classList.remove("is-drop-target")); });
          const checkboxCell = node("td"), checkbox = node("input"); checkbox.type = "checkbox"; checkbox.setAttribute("aria-label", "Select " + title(item));
          checkbox.addEventListener("change", () => { checkbox.checked ? selected.add(item.session_id) : selected.delete(item.session_id); refreshSelection(); }); checkboxCell.append(checkbox);
          const name = node("td"), open = preview ? node("div", "library-session-open") : button("", "library-session-open", () => onOpen(item.session_id));
          const thumbnail = preview ? previewPoster(item) : node("span", "library-session-icon"); if (!preview) thumbnail.append(icon("movie"));
          const text = node("span"); text.append(node("strong", "", title(item)));
          const location = preview ? item.description : prefs.placement[item.session_id]; if (location) text.append(node("small", "", location));
          open.append(thumbnail, text); name.append(open);
          const state = node("td", "library-session-state");
          // Read off the phase the backend actually sends. The regex this
          // replaces matched words that are not in the enum ("rendering",
          // "analyzing") and missed most of the ones that are — so `completed`,
          // `composing`, `observing`, `proposing` and `created` all printed an
          // em dash. A session list that cannot say which sessions are DONE is
          // not answering the question it exists to answer.
          const label = SESSION_PHASES[String(item.phase || "")];
          const failed = /fail|error/.test(String(item.status || ""));
          state.textContent = failed ? "Needs attention" : (label || "Working");
          if (label === "Done") state.classList.add("is-done");
          else if (!failed) state.classList.add("is-working");
          const edited = node("td", "library-session-edited"); const date = item.updated_at ? new Date(item.updated_at) : null;
          edited.textContent = date && Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(date) : "—";
          if (date && Number.isFinite(date.getTime())) edited.title = date.toLocaleString();
          const more = node("td"), moveOne = button("", "library-save", () => moveDialog([item.session_id])); moveOne.append(icon("folder")); moveOne.setAttribute("aria-label", "Move " + title(item)); more.append(moveOne);
          row.append(checkboxCell, name);
          if (preview) row.append(node("td", "library-session-edited", item.layers), node("td", "library-session-edited", String(item.takes)));
          row.append(state, edited, more); body.append(row);
        }); table.append(body); results.append(table);
      }
      refreshSelection(); updatePlayback();
    }
    search.addEventListener("input", () => { query = search.value.trim(); selected.clear(); render(); });
    sortSelect.addEventListener("change", () => { sort = sortSelect.value; render(); });
    async function load() {
      loaded = false; status.textContent = "Loading sessions…"; render();
      try {
        const data = preview ? { sessions: previewSessions } : await transport.listSessions(); if (destroyed) return;
        items = data.sessions || []; loaded = true; status.textContent = ""; render();
      } catch (_) {
        if (destroyed) return; loaded = true; results.removeAttribute("aria-busy"); results.replaceChildren(); status.textContent = "Couldn’t load sessions.";
        results.append(button("Try again", "btn-ghost", load));
      }
    }
    // Both pages load real sessions. The gallery never called the backend at
    // all, which is how it came to show invented work.
    load();
    return { destroy() {
      edgeResize?.disconnect(); edgeContent.disconnect(); results.removeEventListener("scroll", updateScrollEdges);
      destroyed = true; ++playRevision; listeners.forEach(remove => remove()); audio.pause(); audio.removeAttribute("src"); audio.load();
      if (objectUrl) URL.revokeObjectURL(objectUrl); clearTimeout(noticeTimer); clearTimeout(hideTimer); if (dialog.open) dialog.close(); root.replaceChildren(); root.classList.remove("library-native");
    } };
  }
  window.EdennLibrary = { mount, directions };
})();
