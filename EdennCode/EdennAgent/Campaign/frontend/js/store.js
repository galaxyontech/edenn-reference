/* ============================================================================
 * Edenn — campaign console store (HOOKED UP: real API, no mock world).
 *
 * Same public surface as the mock store (get / subscribe / actions /
 * selectors) so the screens are untouched; the internals are now fetches
 * against the campaign devserver:
 *   GET  /api/campaign/state   → the world (server-shaped to the screen contract)
 *   GET  /api/campaign/tree    → the campaign lineage projection (canvas)
 *   POST /api/campaign/*       → the actions; every response IS the new world
 *
 * Error contract: non-2xx responses surface via toast (409 stage errors are
 * the backend's ask-first/stage machine speaking) — never swallowed.
 * The render call is long (real local ffmpeg): the store sets optimistic
 * "rendering" states so the screens show progress during the await.
 * ========================================================================== */
(function () {
  "use strict";

  /** Minimal pre-fetch world so first paint renders before /state returns. */
  let world = {
    stage: "brief",
    brand: "…",
    loading: true,
    families: [],
    archiveNumbers: {},
    ingest: { pending: false, question: null, dedupe: "" },
    collections: [],
    assets: [],
    campaign: {
      id: "", name: "Loading…", flight: "", brief: "",
      thread: [], questions: [], roles: [], variants: [],
      launch: null, outcomes: [], feed: [], proposal: null,
    },
  };
  let tree = { nodes: [], edges: [] };

  const listeners = new Set();
  function emit() { listeners.forEach(function (fn) { fn(world); }); }

  function toast(msg) {
    if (window.CampaignUI && window.CampaignUI.toast) window.CampaignUI.toast(msg);
    else console.warn("[campaign]", msg);
  }

  async function refresh() {
    const [stateRes, treeRes] = await Promise.all([
      fetch("/api/campaign/state"),
      fetch("/api/campaign/tree"),
    ]);
    if (stateRes.ok) {
      world = await stateRes.json();
      world.loading = false;
    }
    if (treeRes.ok) tree = await treeRes.json();
    emit();
  }

  async function post(path, body) {
    let res;
    try {
      res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
    } catch (err) {
      toast("Backend unreachable — is the campaign devserver running?");
      return false;
    }
    if (!res.ok) {
      let detail = res.status + "";
      try { detail = (await res.json()).detail || detail; } catch (e) { /* keep status */ }
      toast("Refused (" + res.status + "): " + detail);
      await refresh();
      return false;
    }
    world = await res.json();
    world.loading = false;
    try { tree = await (await fetch("/api/campaign/tree")).json(); } catch (e) { /* keep old tree */ }
    emit();
    return true;
  }

  const actions = {
    ingestAnswer: function (choice) { post("/api/campaign/ingest/answer", { choice: choice }); },
    sendBrief: function () { post("/api/campaign/brief"); },
    answerQuestion: function (qid, choice) { post("/api/campaign/answer", { qid: qid, choice: choice }); },
    planVariants: function () { post("/api/campaign/plan"); },
    /** Gate ① — long call (real renders); show optimistic progress meanwhile. */
    confirmRender: function () {
      (world.campaign.variants || []).forEach(function (v) { v.state = "rendering"; });
      emit();
      post("/api/campaign/render");
    },
    confirmLaunch: function () { post("/api/campaign/launch"); },
    approveProposal: function () {
      const actionsBody = ((world.campaign.proposal || {}).actions || []).map(function (a) {
        return { id: a.id, approved: !!a.on };
      });
      post("/api/campaign/approve", { actions: actionsBody });
    },
  };

  const selectors = {
    /** The campaign lineage tree (server projection — the campaign canvas is
     *  NOT the audio session canvas; branch heads are variants, rounds are
     *  generations, halos come from sandbox outcomes). */
    tree: function () { return tree; },
  };

  window.CampaignStore = {
    get: function () { return world; },
    subscribe: function (fn) { listeners.add(fn); return function () { listeners.delete(fn); }; },
    actions: actions,
    selectors: selectors,
  };

  refresh().catch(function () {
    toast("Backend unreachable — is the campaign devserver running?");
  });
})();
