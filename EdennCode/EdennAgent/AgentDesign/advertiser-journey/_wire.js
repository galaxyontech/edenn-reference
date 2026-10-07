/* WIRE — interaction + annotation runtime for the advertiser-journey wireframes.
 *
 * Convention: GRAY is the product, BLUE is the annotation layer. This file adds
 * the blue layer: hover tooltips describing every click's effect, a numbered
 * hotspot legend (⚡), a walkthrough bar that steps through the demo narrative,
 * and a pulsing "story" highlight on the one click that advances the story.
 *
 * Wiring (attributes pages put on elements):
 *   data-fx    one-line description of what the click does. REQUIRED on every
 *              interactive element — this is the documentation layer.
 *   data-go    relative target page (optionally with ?params); click navigates.
 *   data-act   key into window.WIRE_ACTS — a page-defined in-page effect.
 *   data-story marks the click that advances the demo narrative (guide pulse).
 *
 * Pages declare (before this script):
 *   window.WIRE_PAGE = { title: "S3 · Brief" };
 *   window.WIRE_ACTS = { sendBrief: function (el) { ... }, ... };
 *
 *   data-more  selector of a detail block this element expands/collapses
 *              (progressive disclosure — dense info hidden until asked for).
 *   data-cap   "built:Label" | "partial:Label" | "new:Label" — which existing
 *              module powers this region. Shown by the ⚙ built-vs-new overlay.
 *
 * Click resolution order: data-more → data-act → data-go → toast("In product: " + fx).
 * Page helpers exposed as window.WIRE: toast, open, close, story, param, go.
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ */
  /* Walkthrough sequence — order IS the demo narrative.                */
  /* ------------------------------------------------------------------ */
  var SEQ = [
    { href: "s1-home.html",                 title: "S1 · Home" },
    { href: "s2b-ingest-organize.html",     title: "S2b · Ingest — organize by asking" },
    { href: "s2-library.html",              title: "S2 · Library at scale" },
    { href: "s3-brief.html",                title: "S3 · Brief → roles" },
    { href: "s4-creation.html",             title: "S4 · Create ⇄ publish — Gate ①" },
    { href: "s5-launch.html",               title: "S5 · Launch — Gate ②" },
    { href: "s6-console.html",              title: "S6 · Console → proposal" },
    { href: "s4-creation.html?seed=round2", title: "S4 · Round 2 — the loop closes" }
  ];

  var file = (location.pathname.split("/").pop() || "").toLowerCase();
  var isRound2 = /(^|[?&])seed=round2/.test(location.search);
  var stepIx = -1;
  for (var i = 0; i < SEQ.length; i++) {
    var hp = SEQ[i].href.split("?");
    var wantsRound2 = /seed=round2/.test(hp[1] || "");
    if (hp[0] === file && wantsRound2 === isRound2) { stepIx = i; break; }
  }

  function seqTitle(href) {
    var f = (href.split("?")[0] || "").toLowerCase();
    var r2 = /seed=round2/.test(href);
    for (var j = 0; j < SEQ.length; j++) {
      var p = SEQ[j].href.split("?");
      if (p[0] === f && /seed=round2/.test(p[1] || "") === r2) return SEQ[j].title;
    }
    return f.replace(/\.html$/, "");
  }

  /* ------------------------------------------------------------------ */
  /* Annotation-layer CSS (blue = annotation, matches .notes).          */
  /* ------------------------------------------------------------------ */
  var css = [
    ".wire-bar{position:fixed;left:50%;bottom:12px;transform:translateX(-50%);z-index:9000;",
    "  display:flex;align-items:center;gap:8px;background:#fff;border:1.5px solid #2b6cb0;",
    "  border-radius:999px;padding:6px 12px;font:11px/1.3 Helvetica,Arial,sans-serif;color:#2b6cb0;",
    "  box-shadow:0 4px 14px rgba(43,108,176,.18);white-space:nowrap}",
    ".wire-bar b{font-size:11px}",
    ".wire-bar .wire-btn{cursor:pointer;border:1px solid #9dbfe0;border-radius:999px;padding:3px 9px;",
    "  background:#f4f8fc;color:#2b6cb0;user-select:none}",
    ".wire-bar .wire-btn.on{background:#2b6cb0;color:#fff;border-color:#2b6cb0}",
    ".wire-bar .wire-btn.off{opacity:.45;cursor:default}",
    ".wire-tip{position:absolute;z-index:9500;max-width:300px;background:#2b6cb0;color:#fff;",
    "  border-radius:7px;padding:6px 10px;font:11px/1.4 Helvetica,Arial,sans-serif;pointer-events:none;",
    "  box-shadow:0 3px 10px rgba(0,0,0,.25)}",
    ".wire-tip .wire-tip-go{opacity:.8;font-size:10px;margin-top:2px}",
    ".wire-toast{position:fixed;left:16px;bottom:16px;z-index:9400;background:#2b6cb0;color:#fff;",
    "  border-radius:9px;padding:8px 14px;font:11.5px/1.4 Helvetica,Arial,sans-serif;max-width:340px;",
    "  box-shadow:0 4px 14px rgba(0,0,0,.25);opacity:0;transition:opacity .25s}",
    ".wire-toast.show{opacity:1}",
    "body.wire-show [data-fx]{outline:1.5px dashed #2b6cb0;outline-offset:2px}",
    ".wire-badge{position:absolute;z-index:9200;background:#2b6cb0;color:#fff;border-radius:999px;",
    "  min-width:16px;height:16px;padding:0 4px;font:700 10px/16px Helvetica,Arial,sans-serif;",
    "  text-align:center;pointer-events:none}",
    ".wire-legend{position:fixed;right:12px;top:12px;bottom:52px;width:300px;z-index:9100;overflow:auto;",
    "  background:#fff;border:1.5px solid #2b6cb0;border-radius:10px;padding:10px 12px;",
    "  font:11px/1.45 Helvetica,Arial,sans-serif;color:#333;box-shadow:0 6px 18px rgba(43,108,176,.2)}",
    ".wire-legend h4{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:#2b6cb0;margin:0 0 8px}",
    ".wire-legend .wire-row{display:flex;gap:7px;padding:4px 2px;border-bottom:1px solid #eef3f8;cursor:pointer}",
    ".wire-legend .wire-row:hover{background:#f4f8fc}",
    ".wire-legend .wire-num{flex:none;background:#2b6cb0;color:#fff;border-radius:999px;min-width:16px;",
    "  height:16px;padding:0 4px;font:700 10px/16px Helvetica,Arial,sans-serif;text-align:center}",
    ".wire-legend .wire-go{color:#2b6cb0;font-size:10px}",
    "[data-more]{cursor:pointer}",
    "body.wire-cap .wire-cap-built{outline:2px solid #2f855a;outline-offset:2px}",
    "body.wire-cap .wire-cap-partial{outline:2px dashed #b7791f;outline-offset:2px}",
    "body.wire-cap .wire-cap-new{outline:2px double #97266d;outline-offset:2px}",
    ".wire-captag{position:absolute;z-index:9200;color:#fff;border-radius:4px;padding:0 5px;",
    "  font:700 9px/14px Helvetica,Arial,sans-serif;pointer-events:none;letter-spacing:.04em}",
    ".wire-captag.built{background:#2f855a}.wire-captag.partial{background:#b7791f}.wire-captag.new{background:#97266d}",
    ".wire-legend .wire-cnum{flex:none;color:#fff;border-radius:4px;min-width:16px;height:16px;padding:0 4px;",
    "  font:700 8.5px/16px Helvetica,Arial,sans-serif;text-align:center}",
    ".wire-cnum.built{background:#2f855a}.wire-cnum.partial{background:#b7791f}.wire-cnum.new{background:#97266d}",
    "body.wire-guide [data-story]{animation:wirePulse 1.6s ease-in-out infinite;position:relative}",
    "@keyframes wirePulse{0%,100%{box-shadow:0 0 0 0 rgba(43,108,176,.55)}",
    "  50%{box-shadow:0 0 0 7px rgba(43,108,176,0)}}",
    ".wire-flash{outline:3px solid #2b6cb0 !important;outline-offset:3px;transition:outline .2s}"
  ].join("\n");
  var styleEl = document.createElement("style");
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  /* ------------------------------------------------------------------ */
  /* State (persist toggles across pages within the session).           */
  /* ------------------------------------------------------------------ */
  function sget(k, dflt) { try { var v = sessionStorage.getItem(k); return v === null ? dflt : v === "1"; } catch (e) { return dflt; } }
  function sset(k, v) { try { sessionStorage.setItem(k, v ? "1" : "0"); } catch (e) {} }
  var guideOn = sget("wire.guide", true);
  var spotsOn = sget("wire.spots", false);
  var capOn = sget("wire.cap", false);

  /* ------------------------------------------------------------------ */
  /* Toast                                                              */
  /* ------------------------------------------------------------------ */
  var toastEl = document.createElement("div");
  toastEl.className = "wire-toast";
  document.body.appendChild(toastEl);
  var toastTimer = null;
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 3000);
  }

  /* ------------------------------------------------------------------ */
  /* Tooltip (hover any [data-fx])                                      */
  /* ------------------------------------------------------------------ */
  var tip = document.createElement("div");
  tip.className = "wire-tip";
  tip.style.display = "none";
  document.body.appendChild(tip);
  document.addEventListener("mouseover", function (ev) {
    var el = ev.target.closest && ev.target.closest("[data-fx]");
    if (!el) { tip.style.display = "none"; return; }
    var fx = el.getAttribute("data-fx") || "";
    var go = el.getAttribute("data-go");
    tip.innerHTML = "";
    var main = document.createElement("div");
    main.textContent = fx;
    tip.appendChild(main);
    if (go) {
      var sub = document.createElement("div");
      sub.className = "wire-tip-go";
      sub.textContent = "→ " + seqTitle(go);
      tip.appendChild(sub);
    }
    var capEl = capOn && ev.target.closest("[data-cap]");
    if (capEl) {
      var cinfo = capParse(capEl);
      var cline = document.createElement("div");
      cline.className = "wire-tip-go";
      cline.textContent = "⚙ " + (cinfo.status === "partial" ? "PARTIAL" : cinfo.status.toUpperCase()) + " — " + cinfo.label;
      tip.appendChild(cline);
    }
    var r = el.getBoundingClientRect();
    tip.style.display = "block";
    var top = r.bottom + window.scrollY + 6;
    var left = Math.max(8, Math.min(r.left + window.scrollX, window.scrollX + document.documentElement.clientWidth - 320));
    tip.style.top = top + "px";
    tip.style.left = left + "px";
  });
  document.addEventListener("mouseout", function (ev) {
    if (ev.target.closest && ev.target.closest("[data-fx]")) tip.style.display = "none";
  });

  /* ------------------------------------------------------------------ */
  /* Hotspot badges + legend (⚡)                                       */
  /* ------------------------------------------------------------------ */
  var badgeBox = null, legend = null;
  function visible(el) {
    if (!el.offsetParent && getComputedStyle(el).position !== "fixed") return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function buildSpots() {
    killSpots();
    badgeBox = document.createElement("div");
    badgeBox.style.cssText = "position:absolute;top:0;left:0;width:100%;height:0;pointer-events:none;z-index:9200";
    document.body.appendChild(badgeBox);
    legend = document.createElement("div");
    legend.className = "wire-legend";
    var h = document.createElement("h4");
    h.textContent = "Every click on this page";
    legend.appendChild(h);
    var els = [].slice.call(document.querySelectorAll("[data-fx]")).filter(visible);
    els.forEach(function (el, ix) {
      var n = ix + 1;
      var r = el.getBoundingClientRect();
      var b = document.createElement("div");
      b.className = "wire-badge";
      b.textContent = n;
      b.style.top = (r.top + window.scrollY - 7) + "px";
      b.style.left = (r.left + window.scrollX - 7) + "px";
      badgeBox.appendChild(b);
      var row = document.createElement("div");
      row.className = "wire-row";
      var num = document.createElement("span");
      num.className = "wire-num";
      num.textContent = n;
      var txt = document.createElement("span");
      txt.textContent = el.getAttribute("data-fx") || "";
      var go = el.getAttribute("data-go");
      if (go) {
        var g = document.createElement("div");
        g.className = "wire-go";
        g.textContent = "→ " + seqTitle(go);
        txt.appendChild(g);
      }
      row.appendChild(num);
      row.appendChild(txt);
      row.addEventListener("click", function () {
        el.scrollIntoView({ block: "center", behavior: "smooth" });
        el.classList.add("wire-flash");
        setTimeout(function () { el.classList.remove("wire-flash"); }, 1400);
      });
      legend.appendChild(row);
    });
    document.body.appendChild(legend);
    document.body.classList.add("wire-show");
  }
  function killSpots() {
    if (badgeBox) { badgeBox.remove(); badgeBox = null; }
    if (legend) { legend.remove(); legend = null; }
    document.body.classList.remove("wire-show");
  }
  function setSpots(on) {
    spotsOn = on;
    sset("wire.spots", on);
    if (on && capOn) setCap(false);
    if (on) buildSpots(); else killSpots();
    syncBar();
  }

  /* ------------------------------------------------------------------ */
  /* Built-vs-new capability overlay (⚙) — how each region maps to the  */
  /* existing system. built=green solid, partial=amber dashed,          */
  /* new=magenta double.                                                */
  /* ------------------------------------------------------------------ */
  var capBox = null, capLegend = null;
  function capParse(el) {
    var raw = el.getAttribute("data-cap") || "";
    var ix = raw.indexOf(":");
    var status = ix > 0 ? raw.slice(0, ix).trim().toLowerCase() : "new";
    if (status !== "built" && status !== "partial" && status !== "new") status = "new";
    return { status: status, label: ix > 0 ? raw.slice(ix + 1).trim() : raw.trim() };
  }
  function buildCap() {
    killCap();
    capBox = document.createElement("div");
    capBox.style.cssText = "position:absolute;top:0;left:0;width:100%;height:0;pointer-events:none;z-index:9200";
    document.body.appendChild(capBox);
    capLegend = document.createElement("div");
    capLegend.className = "wire-legend";
    var els = [].slice.call(document.querySelectorAll("[data-cap]")).filter(visible);
    var counts = { built: 0, partial: 0, "new": 0 };
    els.forEach(function (el) {
      var c = capParse(el);
      counts[c.status]++;
      el.classList.add("wire-cap-" + c.status);
      var r = el.getBoundingClientRect();
      var t = document.createElement("div");
      t.className = "wire-captag " + c.status;
      t.textContent = c.status === "partial" ? "PART" : c.status.toUpperCase();
      t.style.top = (r.top + window.scrollY - 7) + "px";
      t.style.left = (r.right + window.scrollX - 34) + "px";
      capBox.appendChild(t);
    });
    var h = document.createElement("h4");
    h.textContent = "How this maps to the built system — built " + counts.built +
      " · partial " + counts.partial + " · new " + counts["new"];
    capLegend.appendChild(h);
    ["built", "partial", "new"].forEach(function (status) {
      els.forEach(function (el) {
        var c = capParse(el);
        if (c.status !== status) return;
        var row = document.createElement("div");
        row.className = "wire-row";
        var num = document.createElement("span");
        num.className = "wire-cnum " + status;
        num.textContent = status === "partial" ? "P" : status[0].toUpperCase();
        var txt = document.createElement("span");
        txt.textContent = c.label;
        row.appendChild(num);
        row.appendChild(txt);
        row.addEventListener("click", function () {
          el.scrollIntoView({ block: "center", behavior: "smooth" });
          el.classList.add("wire-flash");
          setTimeout(function () { el.classList.remove("wire-flash"); }, 1400);
        });
        capLegend.appendChild(row);
      });
    });
    document.body.appendChild(capLegend);
    document.body.classList.add("wire-cap");
  }
  function killCap() {
    if (capBox) { capBox.remove(); capBox = null; }
    if (capLegend) { capLegend.remove(); capLegend = null; }
    [].slice.call(document.querySelectorAll("[data-cap]")).forEach(function (el) {
      el.classList.remove("wire-cap-built", "wire-cap-partial", "wire-cap-new");
    });
    document.body.classList.remove("wire-cap");
  }
  function setCap(on) {
    capOn = on;
    sset("wire.cap", on);
    if (on && spotsOn) setSpots(false);
    if (on) buildCap(); else killCap();
    syncBar();
  }

  /* Reposition overlays if layout shifts (page effects add content). */
  var repositionQueued = false;
  function requeueSpots() {
    if ((!spotsOn && !capOn) || repositionQueued) return;
    repositionQueued = true;
    setTimeout(function () {
      repositionQueued = false;
      if (spotsOn) buildSpots();
      if (capOn) buildCap();
    }, 250);
  }
  window.addEventListener("resize", requeueSpots);

  /* ------------------------------------------------------------------ */
  /* Walkthrough bar                                                    */
  /* ------------------------------------------------------------------ */
  var bar = document.createElement("div");
  bar.className = "wire-bar";
  var barPrev, barNext, barSpots, barGuide, barLabel;
  function mkBtn(label, cls) {
    var b = document.createElement("span");
    b.className = "wire-btn" + (cls ? " " + cls : "");
    b.textContent = label;
    bar.appendChild(b);
    return b;
  }
  if (stepIx >= 0) {
    barPrev = mkBtn("‹");
    barLabel = document.createElement("b");
    barLabel.textContent = "Walkthrough " + (stepIx + 1) + "/" + SEQ.length + " · " + SEQ[stepIx].title;
    bar.appendChild(barLabel);
    barNext = mkBtn("›");
    barPrev.addEventListener("click", function () { if (stepIx > 0) location.href = SEQ[stepIx - 1].href; });
    barNext.addEventListener("click", function () { if (stepIx < SEQ.length - 1) location.href = SEQ[stepIx + 1].href; });
    if (stepIx === 0) barPrev.classList.add("off");
    if (stepIx === SEQ.length - 1) barNext.classList.add("off");
  } else {
    barLabel = document.createElement("b");
    barLabel.textContent = (window.WIRE_PAGE && window.WIRE_PAGE.title) || "Map";
    bar.appendChild(barLabel);
    var start = mkBtn("Start the walkthrough ▶");
    start.addEventListener("click", function () { location.href = SEQ[0].href; });
  }
  barSpots = mkBtn("⚡ hotspots");
  var barCap = mkBtn("⚙ built vs new");
  barGuide = mkBtn("● guide");
  barSpots.addEventListener("click", function () { setSpots(!spotsOn); });
  barCap.addEventListener("click", function () { setCap(!capOn); });
  barGuide.addEventListener("click", function () {
    guideOn = !guideOn;
    sset("wire.guide", guideOn);
    document.body.classList.toggle("wire-guide", guideOn);
    syncBar();
  });
  function syncBar() {
    barSpots.classList.toggle("on", spotsOn);
    barCap.classList.toggle("on", capOn);
    barGuide.classList.toggle("on", guideOn);
  }
  document.body.appendChild(bar);
  document.body.classList.toggle("wire-guide", guideOn);
  syncBar();
  if (spotsOn) buildSpots();
  if (capOn) buildCap();

  /* Keyboard: i toggles hotspots, arrows step the walkthrough. */
  document.addEventListener("keydown", function (ev) {
    if (ev.target && /INPUT|TEXTAREA/.test(ev.target.tagName)) return;
    if (ev.key === "i") setSpots(!spotsOn);
    if (stepIx >= 0 && ev.key === "ArrowRight" && stepIx < SEQ.length - 1) location.href = SEQ[stepIx + 1].href;
    if (stepIx >= 0 && ev.key === "ArrowLeft" && stepIx > 0) location.href = SEQ[stepIx - 1].href;
  });

  /* ------------------------------------------------------------------ */
  /* Click dispatch: data-act → data-go → fx toast                      */
  /* ------------------------------------------------------------------ */
  document.addEventListener("click", function (ev) {
    var more = ev.target.closest && ev.target.closest("[data-more]");
    if (more) {
      var tgt = document.querySelector(more.getAttribute("data-more"));
      if (tgt) {
        var hidden = tgt.style.display === "none" || getComputedStyle(tgt).display === "none";
        tgt.style.display = hidden ? "" : "none";
        more.textContent = hidden
          ? more.textContent.replace("▸", "▾")
          : more.textContent.replace("▾", "▸");
        requeueSpots();
        return;
      }
    }
    var el = ev.target.closest && ev.target.closest("[data-act],[data-go],[data-fx]");
    if (!el) return;
    if (el.hasAttribute("data-act")) {
      var fn = (window.WIRE_ACTS || {})[el.getAttribute("data-act")];
      if (typeof fn === "function") { fn(el, ev); requeueSpots(); return; }
    }
    if (el.hasAttribute("data-go")) { location.href = el.getAttribute("data-go"); return; }
    var fx = el.getAttribute("data-fx");
    if (fx) toast("In product: " + fx);
  });

  /* ------------------------------------------------------------------ */
  /* Page-facing helpers                                                */
  /* ------------------------------------------------------------------ */
  window.WIRE = {
    toast: toast,
    /* Show a hidden .overlay/.modal (display:none → grid). */
    open: function (sel) { var el = document.querySelector(sel); if (el) el.style.display = "grid"; requeueSpots(); },
    close: function (sel) { var el = document.querySelector(sel); if (el) el.style.display = "none"; requeueSpots(); },
    /* Move the story pulse to a new element (the next narrative click). */
    story: function (sel) {
      [].slice.call(document.querySelectorAll("[data-story]")).forEach(function (el) { el.removeAttribute("data-story"); });
      var el = typeof sel === "string" ? document.querySelector(sel) : sel;
      if (el) el.setAttribute("data-story", "");
      requeueSpots();
    },
    /* Read a ?param from the URL ("" if absent). */
    param: function (name) {
      var m = new RegExp("[?&]" + name + "=([^&]*)").exec(location.search);
      return m ? decodeURIComponent(m[1]) : "";
    },
    go: function (href) { location.href = href; },
    refreshSpots: requeueSpots
  };
})();
