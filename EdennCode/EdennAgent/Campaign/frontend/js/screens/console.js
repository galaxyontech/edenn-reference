/* ============================================================================
 * Edenn — campaign console screen (#/campaign/console).
 *
 * The "live" surface: KPI table from campaign outcomes, the market agent's
 * feed, and the day-4 proposal drawer (Gate ③). Pure renderer over
 * CampaignStore — mutations go through store.actions; the one exception is
 * the proposal-action checkboxes, which the contract binds directly to
 * `proposal.actions[i].on` (the canvas ghost node reads the same flag).
 * Transient UI state (drawer open, highlighted row) lives in module scope
 * and survives router re-renders.
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
  const PRE_LIVE = ["brief", "roles", "planned", "rendered"];

  // ---- transient UI state (re-derivable; survives re-renders) --------------
  let drawerOpen = false;
  let hlLabel = null; // highlighted outcome-row label; matches feed `evidence`

  // Latest render args so local (non-store) UI changes re-render the same
  // way the router would on a store emit.
  let mountRef = null, storeRef = null, uiRef = null;
  function refresh() {
    if (!mountRef) return;
    mountRef.innerHTML = "";
    build(mountRef, storeRef, uiRef);
  }

  // ---- pieces --------------------------------------------------------------

  /** KPI card — .cp-table over world.campaign.outcomes. */
  function kpiCard(c, ui) {
    const card = ui.el("div", "cp-card cp-grid");
    card.appendChild(ui.el("div", "cp-h", "Outcomes"));
    const table = ui.el("table", "cp-table");
    const thead = ui.el("thead");
    const trh = ui.el("tr");
    ["Variant", "Signal", "Spend · pacing", "State"].forEach(function (h) {
      trh.appendChild(ui.el("th", null, ui.esc(h)));
    });
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = ui.el("tbody");
    c.outcomes.forEach(function (row) {
      const label = row[0];
      const cls = [row[3] === "killed" ? "is-dim" : null, hlLabel === label ? "is-hl" : null]
        .filter(Boolean).join(" ");
      const tr = ui.el("tr", cls || null);
      tr.style.cursor = "pointer";
      tr.appendChild(ui.el("td", null, "<strong>" + ui.esc(label) + "</strong>"));
      tr.appendChild(ui.el("td", null, ui.esc(row[1])));
      tr.appendChild(ui.el("td", null, ui.esc(row[2])));
      const tdState = ui.el("td");
      tdState.appendChild(ui.statePill(row[3]));
      tr.appendChild(tdState);
      tr.addEventListener("click", function () {
        hlLabel = hlLabel === label ? null : label;
        refresh();
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    card.appendChild(table);
    card.appendChild(ui.el("div", "cp-tiny", "Click a row to trace it through the agent feed."));
    return card;
  }

  /** Agent feed card — .cp-feed; the proposal item opens the drawer. */
  function feedCard(c, ui) {
    const card = ui.el("div", "cp-card cp-grid");
    card.appendChild(ui.el("div", "cp-h", "Agent feed"));
    const feed = ui.el("div", "cp-feed");
    c.feed.forEach(function (f) {
      const item = ui.el("div", "cp-feed__item" + (f.proposal ? " is-proposal" : ""));
      if (hlLabel && f.evidence === hlLabel) {
        item.classList.add("is-hl");
        // .cp-feed__item.is-hl has no rule in campaign.css — minimal inline hint.
        item.style.borderColor = "var(--blue)";
        item.style.background = "var(--blue-light)";
      }
      item.appendChild(ui.el("div", "cp-feed__day", ui.esc(f.day)));
      const body = ui.el("div");
      body.appendChild(ui.el("div", null, ui.esc(f.text)));
      if (f.proposal) body.appendChild(ui.el("div", "cp-tiny", "Review → Gate ③ waits for you"));
      item.appendChild(body);
      if (f.proposal) {
        item.addEventListener("click", function () {
          drawerOpen = true;
          refresh();
        });
      }
      feed.appendChild(item);
    });
    card.appendChild(feed);
    return card;
  }

  /** Spend/pacing line, computed from outcomes + flight budget. */
  function pacingLine(c, ui) {
    const spent = c.outcomes.reduce(function (sum, row) {
      const m = /\$([\d,]+)/.exec(row[2]);
      return sum + (m ? parseInt(m[1].replace(/,/g, ""), 10) : 0);
    }, 0);
    const budget = /\$[\d,]+/.exec(c.flight);
    const text = "Spend to date $" + spent.toLocaleString("en-US")
      + (budget ? " of " + budget[0] : "") + " · " + c.launch.pacing;
    return ui.el("div", "cp-sub", ui.esc(text));
  }

  /** Proposal drawer — evidence, action checkboxes, Gate ③ approve. */
  function drawer(store, ui) {
    const c = store.get().campaign;
    const d = ui.el("div", "cp-drawer");

    const x = ui.el("button", "btn-ghost cp-drawer__x", '<i class="ti ti-x"></i>');
    x.type = "button";
    x.addEventListener("click", function () {
      drawerOpen = false;
      refresh();
    });
    d.appendChild(x);

    d.appendChild(ui.el("div", "cp-h", "Day 4 — proposal"));
    d.appendChild(ui.el("div", "cp-sub", "Drafted from live outcomes. Nothing runs until you approve."));

    d.appendChild(ui.el("div", "cp-sect", "Evidence"));
    c.proposal.evidence.forEach(function (evi) {
      d.appendChild(ui.el("div", "cp-sub", ui.esc("• " + evi)));
    });

    d.appendChild(ui.el("div", "cp-sect", "Actions"));
    c.proposal.actions.forEach(function (a) {
      const act = ui.el("label", "cp-action");
      const cb = ui.el("input");
      cb.type = "checkbox";
      cb.checked = !!a.on;
      cb.addEventListener("change", function () {
        // Contract-specified binding: the canvas ghost node reads this flag.
        a.on = cb.checked;
      });
      act.appendChild(cb);
      const body = ui.el("div");
      body.appendChild(ui.el("div", null, ui.esc(a.label)));
      const pill = ui.el("span", "cp-pill" + (a.cost === "free" ? "" : " is-blue"), ui.esc(a.cost));
      pill.style.marginTop = "4px";
      body.appendChild(pill);
      act.appendChild(body);
      d.appendChild(act);
    });

    const foot = ui.el("div", "cp-row");
    foot.style.marginTop = "auto";
    foot.appendChild(ui.el("span", "spacer"));
    const approve = ui.el("button", "btn-primary", "Approve selected — Gate ③");
    approve.type = "button";
    approve.addEventListener("click", function () {
      ui.confirmSpend(
        "Approve the proposal?",
        "iterate compiles a CreationRequest: LOCK A’s opening · LOCK B’s voice · EXCLUDE C’s close · vary only the close. Render cost applies for round 2.",
        "Approve — Gate ③"
      ).then(function (ok) {
        if (!ok) return;
        drawerOpen = false;
        store.actions.approveProposal();
        location.hash = "#/campaign";
      });
    });
    foot.appendChild(approve);
    d.appendChild(foot);
    return d;
  }

  /** Round-2 note — the loop has closed; outcomes wrote the next brief. */
  function round2Card(ui) {
    const card = ui.el("div", "cp-card");
    const row = ui.el("div", "cp-row");
    const body = ui.el("div");
    body.appendChild(ui.el("div", "cp-h", '<i class="ti ti-refresh"></i> Round 2 planned — outcomes wrote this brief'));
    body.appendChild(ui.el("div", "cp-sub", "A’s opening locked · B’s voice locked · C’s close excluded."));
    row.appendChild(body);
    row.appendChild(ui.el("span", "spacer"));
    const go = ui.el("button", "btn-primary", "Open the thread →");
    go.type = "button";
    go.addEventListener("click", function () { location.hash = "#/campaign"; });
    row.appendChild(go);
    card.appendChild(row);
    return card;
  }

  // ---- screen --------------------------------------------------------------
  function build(mount, store, ui) {
    const world = store.get();
    const c = world.campaign;

    mount.appendChild(ui.topbar(c.name, SEGS, "console"));
    mount.appendChild(ui.progress(STAGES, STAGE_IX[world.stage] == null ? 0 : STAGE_IX[world.stage]));

    const body = ui.el("div", "cp-body");
    mount.appendChild(body);

    // Guard: nothing to see until Gate ② has fired.
    if (PRE_LIVE.indexOf(world.stage) !== -1) {
      body.appendChild(ui.el("div", "cp-empty", "The console lights up at launch."));
      return;
    }

    const narrow = ui.el("div", "cp-narrow cp-grid");
    body.appendChild(narrow);

    if (world.stage === "round2") narrow.appendChild(round2Card(ui));
    narrow.appendChild(kpiCard(c, ui));
    narrow.appendChild(feedCard(c, ui));
    narrow.appendChild(pacingLine(c, ui));

    if (drawerOpen && (world.stage === "proposal" || world.stage === "live")) {
      mount.appendChild(drawer(store, ui));
    }
  }

  window.CampaignScreens.console = {
    /** @param {HTMLElement} mount @param {typeof window.CampaignStore} store
        @param {typeof window.CampaignUI} ui */
    render: function (mount, store, ui) {
      mountRef = mount;
      storeRef = store;
      uiRef = ui;
      build(mount, store, ui);
    },
  };
})();
