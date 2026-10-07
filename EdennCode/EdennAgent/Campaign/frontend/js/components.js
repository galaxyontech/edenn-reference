/* ============================================================================
 * Edenn — campaign console shared UI (design-system builders).
 *
 * Every builder here emits the STABLE frontend's markup patterns (agent-row,
 * ub, qchips, ps progress, glass, confirm-card, toast) so screens stay
 * byte-consistent with the product design system. Screens compose these;
 * they never hand-roll chat or consent markup.
 * ========================================================================== */
(function () {
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

  /** Agent message — the product's agent-row (avatar + EDENN label + text). */
  function agentMsg(text) {
    const row = el("div", "agent-row");
    row.appendChild(el("div", "av", '<i class="ti ti-sparkles"></i>'));
    const body = el("div", "abody");
    body.appendChild(el("div", "aname", "Edenn"));
    body.appendChild(el("div", "atext", esc(text)));
    row.appendChild(body);
    return row;
  }

  /** User message — the product's notched bubble. */
  function userMsg(text) { return el("div", "ub", esc(text)); }

  /** Ask-first question card — clarify + qchips, disabled once answered. */
  function questionCard(q, onPick) {
    const wrap = el("div", "clarify");
    const row = el("div", "agent-row");
    row.appendChild(el("div", "av", '<i class="ti ti-sparkles"></i>'));
    const body = el("div", "abody");
    body.appendChild(el("div", "aname", "Edenn"));
    body.appendChild(el("div", "clarify__q", esc(q.text)));
    const chips = el("div", "qchips");
    q.chips.forEach(function (c) {
      const b = el("button", "qchip" + (q.answer === c ? " is-on" : ""), esc(c));
      b.type = "button";
      if (q.answer) b.disabled = true;
      else b.addEventListener("click", function () { onPick(c); });
      chips.appendChild(b);
    });
    body.appendChild(chips);
    if (q.answer) body.appendChild(el("div", "cp-answered", '<i class="ti ti-check"></i> ' + esc(q.answer)));
    row.appendChild(body);
    wrap.appendChild(row);
    return wrap;
  }

  /** Stage progress strip — the product's ps dots. */
  function progress(stages, activeIx) {
    const bar = el("div", "progress");
    stages.forEach(function (s, i) {
      const ps = el("div", "ps" + (i < activeIx ? " done" : i === activeIx ? " active" : ""));
      ps.appendChild(el("span", "ps-dot"));
      ps.appendChild(el("span", null, esc(s)));
      bar.appendChild(ps);
      if (i < stages.length - 1) bar.appendChild(el("span", "ps-arr", "›"));
    });
    return bar;
  }

  /** Screen topbar — product topbar with optional view segments + actions. */
  function topbar(title, segs, activeSeg) {
    const tb = el("div", "topbar");
    tb.appendChild(el("span", "topbar__logo", '<i class="ti ti-sparkles"></i>'));
    const t = el("div");
    t.appendChild(el("div", "topbar__title", esc(title)));
    tb.appendChild(t);
    tb.appendChild(el("span", "spacer"));
    if (segs && segs.length) {
      const seg = el("div", "cv-seg");
      seg.setAttribute("role", "group");
      segs.forEach(function (s) {
        const b = el("button", "cv-seg__btn" + (s.id === activeSeg ? " is-on" : ""),
          '<i class="ti ' + s.icon + '"></i> ' + esc(s.label));
        b.type = "button";
        b.addEventListener("click", function () { location.hash = s.route; });
        seg.appendChild(b);
      });
      tb.appendChild(seg);
    }
    tb.appendChild(el("span", "conn mock", "Dev backend — real engines · sandbox channel"));
    return tb;
  }

  /** Spend-consent modal — the product's confirm-card, promise-style. */
  function confirmSpend(title, body, okLabel) {
    return new Promise(function (resolve) {
      const ov = document.getElementById("confirm-overlay");
      document.getElementById("confirm-title").textContent = title;
      document.getElementById("confirm-body").textContent = body;
      const ok = document.getElementById("confirm-ok");
      const cancel = document.getElementById("confirm-cancel");
      ok.textContent = okLabel || "Confirm";
      function done(v) {
        ov.hidden = true;
        ok.removeEventListener("click", yes);
        cancel.removeEventListener("click", no);
        resolve(v);
      }
      function yes() { done(true); }
      function no() { done(false); }
      ok.addEventListener("click", yes);
      cancel.addEventListener("click", no);
      ov.hidden = false;
    });
  }

  /** Toast — product toast element, auto-hides. */
  let toastTimer = null;
  function toast(msg) {
    const t = document.getElementById("toast");
    t.textContent = msg;
    t.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, 2800);
  }

  /** Variant strip sketch — slot boxes proportional to the plan (cp-strip). */
  function strip(slots, biteIx) {
    const s = el("div", "cp-strip");
    slots.forEach(function (w, i) {
      const b = el("i", i === biteIx ? "is-bite" : null);
      b.style.width = w + "%";
      s.appendChild(b);
    });
    return s;
  }

  /** Status pill for a variant state. */
  function statePill(state) {
    const map = {
      planned: ["pill", "planned · free"],
      rendering: ["pill is-busy", "rendering…"],
      rendered: ["pill is-teal", "✓ rendered"],
      live: ["pill is-teal", "live"],
      killed: ["pill is-dim", "killed"],
    };
    const m = map[state] || map.planned;
    return el("span", "cp-pill " + m[0], esc(m[1]));
  }

  window.CampaignUI = {
    el: el, esc: esc,
    agentMsg: agentMsg, userMsg: userMsg, questionCard: questionCard,
    progress: progress, topbar: topbar,
    confirmSpend: confirmSpend, toast: toast,
    strip: strip, statePill: statePill,
  };
})();
