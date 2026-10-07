(function (root) {
  "use strict";
  class RequestRegistry {
    constructor() { this.revision = 0; this.active = null; this.requests = new Map(); }
    begin(id, retryOf = null) {
      const request = { id, retryOf, revision: ++this.revision, status: "active" };
      this.requests.set(id, request); this.active = id;
      if (this.requests.size > 200) this.requests.delete(this.requests.keys().next().value);
      return request;
    }
    accepts(id) { return !id || id === this.active; }
    finish(id, status) {
      if (id !== this.active) return false;
      const request = this.requests.get(id);
      if (request) request.status = status;
      this.active = null; this.revision++;
      return true;
    }
    refreshIsCurrent(revision) { return revision === this.revision; }
  }
  function upsertStep(steps, payload) {
    const label = payload.status || "Working";
    const id = payload.step_id || payload.tool_call_id || label.toLowerCase().replace(/[.…]+$/g, "");
    let step = steps.find(item => item.id === id);
    const state = payload.state || "active";
    if (state === "active") steps.forEach(item => { if (item.status === "active" && item.id !== id) item.status = "complete"; });
    if (!step) { step = { id, label, status: state }; steps.push(step); }
    step.label = label; step.status = ["active", "complete", "pending"].includes(state) ? state : "complete";
    if (payload.thought) step.detail = payload.thought;
    return step;
  }
  function resolveOutput(state) {
    const selected = (state.candidates || []).find(item => item.candidate_id === state.selected_candidate_id) || {};
    const final = state.final_artifact || {};
    const video = (state.mix || {}).video_url || selected.remixed_video_url || final.video_url || selected.video_url;
    return { url: video || final.audio_url || selected.audio_url || null, kind: video ? "video" : "audio" };
  }
  const api = { RequestRegistry, upsertStep, resolveOutput };
  if (typeof module !== "undefined") module.exports = api;
  root.EdennState = api;
})(typeof window !== "undefined" ? window : globalThis);
