/* ============================================================================
 * Minimal spec/assert layer.
 *
 * Deliberately not a test framework: the suite runs one browser across a
 * handful of long journeys, and the useful unit is "a journey that reports many
 * checks", not "a thousand isolated its". A failing check records itself and
 * the journey CONTINUES — one dead button must not hide the twenty controls
 * after it, which is exactly what an exception-per-assert framework does.
 * ========================================================================== */
"use strict";

class Spec {
  constructor(name) {
    this.name = name;
    this.checks = [];
    this._t0 = Date.now();
  }
  /** Record a check. `detail` should say what was actually observed. */
  ok(pass, title, detail) {
    this.checks.push({ pass: !!pass, title, detail: detail == null ? "" : String(detail) });
    const mark = pass ? "  ok  " : " FAIL ";
    console.log(`${mark} ${this.name} :: ${title}${detail ? " — " + detail : ""}`);
    return !!pass;
  }
  eq(actual, expected, title) {
    const pass = JSON.stringify(actual) === JSON.stringify(expected);
    return this.ok(pass, title, pass ? String(actual) : `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`);
  }
  /** An assertion whose failure makes the rest of the journey meaningless. */
  must(pass, title, detail) {
    const r = this.ok(pass, title, detail);
    if (!r) throw new Error(`blocked: ${title}${detail ? " — " + detail : ""}`);
    return r;
  }
  /** Run a step; a thrown error becomes a failed check, not a dead journey. */
  async step(title, fn) {
    try {
      const r = await fn();
      if (r !== false) this.ok(true, title, typeof r === "string" ? r : "");
      else this.ok(false, title, "returned false");
      return r;
    } catch (e) {
      this.ok(false, title, e && e.message ? e.message : String(e));
      return null;
    }
  }
  get failed() { return this.checks.filter((c) => !c.pass); }
  summary() {
    return {
      name: this.name,
      total: this.checks.length,
      failed: this.failed.length,
      ms: Date.now() - this._t0,
      failures: this.failed.map((c) => `${c.title}${c.detail ? " — " + c.detail : ""}`),
    };
  }
}

module.exports = { Spec };
