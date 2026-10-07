/* ============================================================================
 * Edenn — campaign canvas screen (#/campaign/canvas).
 *
 * The campaign projection of the product canvas: the lineage tree from
 * store.selectors.tree() drawn as absolute-positioned cp-nodes over a
 * cp-edges svg, plus the design system's cv-dock verbatim at the bottom.
 * Pure renderer: all world state comes from the store; the only module
 * state is transient UI (focused node, locked take) which survives
 * re-renders and falls back sensibly when its node disappears.
 * ========================================================================== */
(function () {
  "use strict";
  window.CampaignScreens = window.CampaignScreens || {};

  const SEGS = [
    { id: "thread", label: "Thread", icon: "ti-message-2", route: "#/campaign" },
    { id: "canvas", label: "Canvas", icon: "ti-layout-grid", route: "#/campaign/canvas" },
    { id: "console", label: "Console", icon: "ti-chart-dots", route: "#/campaign/console" },
  ];
  const STAGES = ["Brief", "Roles", "Create", "Launch", "Live"];
  const STAGE_IX = { brief: 0, roles: 1, planned: 2, rendered: 2, live: 4, proposal: 4, round2: 2 };

  // Transient UI state (module scope — survives router re-renders).
  const local = { focusId: "A", lockedId: null };

  // Fixed layout map: columns by lineage generation, stacked rows within.
  const NODE_W = 210;
  const COL_X = [30, 310, 590];
  const ROW_Y0 = 70;
  const ROW_STEP = 150;

  function estH(n) { return n.halo ? 88 : 56; }

  /** Compute {pos, w, h} for the tree: source col 0, round-1 col 1,
      round-2 col 2, overlay/ghost near their parents. */
  function layout(tree) {
    const pos = {};
    let r1 = 0, r2 = 0;
    tree.nodes.forEach(function (n) {
      let x, y;
      if (n.kind === "source") { x = COL_X[0]; y = 210; }
      else if (n.kind === "overlay") { x = COL_X[0]; y = 380; }          // ≈ relative, near src
      else if (n.kind === "ghost") { x = COL_X[2]; y = 90; }             // proposed child, near A
      else if (n.round === 2) { x = COL_X[2]; y = ROW_Y0 + (r2++) * ROW_STEP; }
      else { x = COL_X[1]; y = ROW_Y0 + (r1++) * ROW_STEP; }
      pos[n.id] = { x: x, y: y, w: NODE_W, h: estH(n) };
    });
    let w = COL_X[0] + NODE_W, h = 320;
    Object.keys(pos).forEach(function (id) {
      w = Math.max(w, pos[id].x + pos[id].w);
      h = Math.max(h, pos[id].y + pos[id].h);
    });
    return { pos: pos, w: w + 50, h: h + 50 };
  }

  function labelTail(label) {
    const parts = String(label).split("·");
    return (parts.length > 1 ? parts.slice(1).join("·") : parts[0]).trim();
  }

  /** Dock breadcrumb for a node — "launch_footage › hook-first › take 1". */
  function nodePath(n) {
    if (n.kind === "source") return "library › spring shoot › launch_footage";
    if (n.kind === "overlay") return "published archive › " + labelTail(n.label);
    if (n.kind === "ghost") return "proposal › approve in the console";
    if (n.round === 2) return "launch_footage › A › " + labelTail(n.label) + " › take 1";
    return "launch_footage › " + labelTail(n.label) + " › take 1";
  }

  function render(mount, store, ui) {
    const world = store.get();
    const tree = store.selectors.tree();
    function repaint() { mount.innerHTML = ""; render(mount, store, ui); }

    // Resolve transient state against the current tree (nodes come and go).
    if (local.lockedId && !tree.nodes.some(function (n) { return n.id === local.lockedId; })) {
      local.lockedId = null;
    }
    // Effective focus: when the focused node isn't in the tree (yet), fall
    // back for THIS render without clobbering the stored default ("A"), so
    // the default focus applies as soon as its node appears.
    let focused = tree.nodes.find(function (n) { return n.id === local.focusId; });
    if (!focused) {
      focused = tree.nodes.find(function (n) { return n.kind === "take"; }) || tree.nodes[0];
    }

    // -- scaffold: topbar → progress → body ---------------------------------
    mount.appendChild(ui.topbar(world.campaign.name, SEGS, "canvas"));
    mount.appendChild(ui.progress(STAGES, STAGE_IX[world.stage] != null ? STAGE_IX[world.stage] : 0));
    const body = ui.el("div", "cp-body cp-body--thread");
    mount.appendChild(body);

    // -- stage: legend + laid-out tree + edges ------------------------------
    const stage = ui.el("div", "cp-cstage");
    body.appendChild(stage);
    stage.appendChild(ui.el("div", "cp-legend",
      '<span style="color:var(--teal);font-weight:700;letter-spacing:-1px">——</span> locked path' +
      '&nbsp;&nbsp;<span style="color:var(--indigo);font-weight:700;letter-spacing:-1px">——</span> branches' +
      " · campaign projection"));

    const lay = layout(tree);
    const worldEl = ui.el("div", null);
    worldEl.style.cssText = "position:relative;width:" + lay.w + "px;height:" + lay.h + "px;min-width:100%";
    stage.appendChild(worldEl);

    // Generation bands above each populated column.
    const hasCol = [false, false, false];
    tree.nodes.forEach(function (n) {
      hasCol[n.kind === "source" || n.kind === "overlay" ? 0
        : n.kind === "ghost" || n.round === 2 ? 2 : 1] = true;
    });
    [["Source", 0], ["Round 1", 1], ["Round 2 · seeded by outcomes", 2]].forEach(function (b) {
      if (!hasCol[b[1]]) return;
      const band = ui.el("div", "cp-band", ui.esc(b[0]));
      band.style.cssText = "position:absolute;left:" + COL_X[b[1]] + "px;top:40px";
      worldEl.appendChild(band);
    });

    // Edges first (svg sits under the nodes; pointer-events none in css).
    const SVGNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("class", "cp-edges");
    svg.setAttribute("viewBox", "0 0 " + lay.w + " " + lay.h);
    tree.edges.forEach(function (e) {
      const p = lay.pos[e[0]], c = lay.pos[e[1]];
      if (!p || !c) return;
      const child = tree.nodes.find(function (n) { return n.id === e[1]; });
      const line = document.createElementNS(SVGNS, "line");
      line.setAttribute("x1", p.x + p.w); line.setAttribute("y1", p.y + p.h / 2);
      line.setAttribute("x2", c.x); line.setAttribute("y2", c.y + c.h / 2);
      let cls = "";
      if (child && (child.kind === "ghost" || child.kind === "overlay")) cls = "is-ghost";
      if (e[1] === local.lockedId) cls = "is-locked";
      if (cls) line.setAttribute("class", cls);
      svg.appendChild(line);
    });
    worldEl.appendChild(svg);

    // Nodes.
    tree.nodes.forEach(function (n) {
      const p = lay.pos[n.id];
      let cls = "cp-node";
      if (n.kind === "ghost" || n.kind === "overlay") cls += " is-ghost";
      if (n.state === "killed") cls += " is-dim";
      if (n.id === focused.id) cls += " is-focus";
      if (n.id === local.lockedId) cls += " is-locked";
      const box = ui.el("div", cls);
      box.style.left = p.x + "px";
      box.style.top = p.y + "px";
      box.appendChild(ui.el("div", "cp-node__t", ui.esc(n.label)));
      box.appendChild(ui.el("div", "cp-node__m", ui.esc(n.meta)));
      if (n.halo) {
        box.appendChild(ui.el("div", "cp-node__halo" + (n.state === "killed" ? " is-bad" : ""), ui.esc(n.halo)));
      }
      box.addEventListener("click", function () {
        if (n.kind === "ghost") { location.hash = "#/campaign/console"; return; }
        local.focusId = n.id;
        repaint();
      });
      worldEl.appendChild(box);
    });

    if (!world.campaign.variants.length) {
      const hint = ui.el("div", "cp-empty",
        "The tree grows as the campaign does — send the brief in the Thread and the variants branch from this source.");
      hint.style.cssText = "position:absolute;left:" + COL_X[1] + "px;top:205px;max-width:300px;text-align:left;padding:0";
      worldEl.appendChild(hint);
    }

    // -- Gate ① (mirrors the thread gate while planning) --------------------
    if (world.stage === "planned") {
      const bar = ui.el("div", "cp-row");
      bar.style.cssText = "flex-shrink:0;padding:10px 18px;border-top:1px solid var(--border-subtle);background:var(--bg-surface)";
      bar.appendChild(ui.el("span", "cp-sub", "Planning is free — nothing renders until you approve."));
      bar.appendChild(ui.el("span", "spacer"));
      const go = ui.el("button", "btn-primary",
        '<i class="ti ti-lock"></i> ' + ui.esc("Lock & render all 3 — Gate ①"));
      go.type = "button";
      // Mirror the thread gate: disabled once a render is in flight (stage
      // stays "planned" until every variant lands, so guard on states).
      if (world.campaign.variants.some(function (v) { return v.state !== "planned"; })) go.disabled = true;
      go.addEventListener("click", function () {
        ui.confirmSpend("Render 3 × 15s?",
          "Render cost applies now. No channel budget is spent until Launch.",
          "Render — Gate ①")
          .then(function (ok) { ok && store.actions.confirmRender(); });
      });
      bar.appendChild(go);
      body.appendChild(bar);
    }

    // -- dock: the design system's cv-dock, focused-node aware --------------
    // Structure mirrors the stable canvas-mode dock byte-for-byte:
    // thumb · play · main(hd(title+time) · path · scrub(fill)) · btns.
    const dock = ui.el("div", "cv-dock");
    dock.appendChild(ui.el("div", "cv-dock__thumb", '<i class="ti ti-player-play"></i>'));
    const play = ui.el("button", "cv-dock__play", '<i class="ti ti-player-play"></i>');
    play.type = "button";
    play.title = "Play";
    play.addEventListener("click", function () {
      ui.toast(focused.kind === "take" && (focused.state === "planned" || focused.state === "rendering")
        ? "Free preview — nothing renders until Gate ①"
        : "In product: plays this take in the dock");
    });
    dock.appendChild(play);

    const main = ui.el("div", "cv-dock__main");
    const hd = ui.el("div", "cv-dock__hd");
    hd.appendChild(ui.el("span", "cv-dock__title", ui.esc(focused.label)));
    hd.appendChild(ui.el("span", "cv-dock__time", ui.esc(focused.kind === "source" ? "0:00 / 3:30" : "0:00 / 0:15")));
    main.appendChild(hd);
    main.appendChild(ui.el("div", "cv-dock__path", ui.esc(nodePath(focused))));
    const scrub = ui.el("div", "cv-dock__scrub");
    const fill = ui.el("div", "cv-dock__fill");
    fill.style.width = "0%";
    scrub.appendChild(fill);
    main.appendChild(scrub);
    dock.appendChild(main);

    const btns = ui.el("div", "cv-dock__btns");
    const inUse = local.lockedId === focused.id;
    const useBtn = ui.el("button", "cv-dbtn primary", ui.esc(inUse ? "✓ In use" : "✓ Use this"));
    useBtn.type = "button";
    useBtn.disabled = focused.kind !== "take" || inUse;
    useBtn.addEventListener("click", function () {
      local.lockedId = focused.id;
      ui.toast("Locked — the campaign path runs through " + focused.label);
      repaint();
    });
    const branchBtn = ui.el("button", "cv-dbtn", ui.esc("⑂ Branch"));
    branchBtn.type = "button";
    branchBtn.addEventListener("click", function () {
      ui.toast("In product: Branch calls the /choices contract — a new take seeded from this node");
    });
    const dlBtn = ui.el("button", "cv-dbtn", ui.esc("↓ Download"));
    dlBtn.type = "button";
    dlBtn.addEventListener("click", function () {
      ui.toast("In product: downloads this take as an MP4");
    });
    btns.appendChild(useBtn);
    btns.appendChild(branchBtn);
    btns.appendChild(dlBtn);
    dock.appendChild(btns);
    body.appendChild(dock);
  }

  window.CampaignScreens.canvas = {
    /** @param {HTMLElement} mount @param {typeof window.CampaignStore} store
        @param {typeof window.CampaignUI} ui */
    render: render,
  };
})();
