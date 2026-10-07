/* ============================================================================
 * Edenn — campaign console: home screen (#/home).
 *
 * The advertiser's landing view: the campaign table (stage cell tracks the
 * store's stage machine live), a "Needs you" card while a proposal waits,
 * and the brand-health strip. Pure renderer: reads store.get() only, mutates
 * nothing (home is navigation-only — no actions, no spend gates), and holds
 * no transient UI state, so re-renders on store emits are trivially safe.
 * ========================================================================== */
(function () {
  "use strict";

  window.CampaignScreens = window.CampaignScreens || {};

  /** Campaign stage → live status pill for the table's stage cell. */
  function stagePill(world, ui) {
    // Mid Gate ① the stage is still "planned" but takes are rendering — show it.
    const rendering = world.stage === "planned" &&
      world.campaign.variants.some(function (v) { return v.state === "rendering"; });
    if (rendering) return ui.el("span", "cp-pill is-busy", "rendering…");
    const open = world.campaign.questions.filter(function (q) { return !q.answer; }).length;
    const map = {
      brief: ["", "Brief — drafting"],
      roles: ["", "Roles — " + open + " question" + (open === 1 ? "" : "s") + " open"],
      planned: ["", "Planned — previews free"],
      rendered: ["is-teal", "Rendered — ready to launch"],
      live: ["is-teal", "LIVE · day 4"],
      proposal: ["is-blue", "LIVE · day 4 — proposal waiting"],
      round2: ["", "Round 2 — planned"],
    };
    const m = map[world.stage] || ["", world.stage];
    return ui.el("span", "cp-pill" + (m[0] ? " " + m[0] : ""), ui.esc(m[1]));
  }

  window.CampaignScreens.home = {
    /** @param {HTMLElement} mount @param {typeof window.CampaignStore} store
        @param {typeof window.CampaignUI} ui */
    render: function (mount, store, ui) {
      const world = store.get();
      const el = ui.el;
      const esc = ui.esc;

      // ---- scaffold: topbar (no segs) → body ------------------------------
      mount.appendChild(ui.topbar("Campaigns", null, null));
      const body = el("div", "cp-body");
      const narrow = el("div", "cp-narrow cp-grid");
      body.appendChild(narrow);
      mount.appendChild(body);

      // ---- body-top actions (right-aligned) -------------------------------
      const actionsRow = el("div", "cp-row");
      actionsRow.appendChild(el("span", "spacer"));
      const addBtn = el("button", "btn-ghost", '<i class="ti ti-library-photo"></i> Add assets');
      addBtn.type = "button";
      addBtn.addEventListener("click", function () { location.hash = "#/library"; });
      actionsRow.appendChild(addBtn);
      const newBtn = el("button", "btn-primary", '<i class="ti ti-plus"></i> New campaign');
      newBtn.type = "button";
      newBtn.addEventListener("click", function () { location.hash = "#/campaign"; });
      actionsRow.appendChild(newBtn);
      narrow.appendChild(actionsRow);

      // ---- campaigns table ------------------------------------------------
      const tableCard = el("div", "cp-card");
      const table = el("table", "cp-table");
      table.appendChild(el("thead", null,
        "<tr><th>Campaign</th><th>Flight</th><th>Stage</th></tr>"));
      const tbody = el("tbody");

      // Live campaign row — stage cell reflects world.stage on every emit.
      const liveRow = el("tr");
      liveRow.style.cursor = "pointer";
      liveRow.appendChild(el("td", null, "<strong>" + esc(world.campaign.name) + "</strong>"));
      liveRow.appendChild(el("td", "cp-sub", esc(world.campaign.flight)));
      const stageTd = el("td");
      stageTd.appendChild(stagePill(world, ui));
      liveRow.appendChild(stageTd);
      liveRow.addEventListener("click", function () {
        const inMarket = world.stage === "live" || world.stage === "proposal";
        location.hash = inMarket ? "#/campaign/console" : "#/campaign";
      });
      tbody.appendChild(liveRow);

      // Static draft row.
      const draftRow = el("tr");
      draftRow.style.cursor = "pointer";
      draftRow.appendChild(el("td", null, esc("Holiday teaser (draft)")));
      draftRow.appendChild(el("td", "cp-sub", "—"));
      const draftTd = el("td");
      draftTd.appendChild(el("span", "cp-pill is-dim", "draft"));
      draftRow.appendChild(draftTd);
      draftRow.addEventListener("click", function () {
        ui.toast("Prototype — only Launch week is wired end-to-end.");
      });
      tbody.appendChild(draftRow);

      table.appendChild(tbody);
      tableCard.appendChild(table);
      narrow.appendChild(tableCard);

      // ---- "Needs you" card (only while a proposal waits) -----------------
      if (world.stage === "proposal") {
        const prop = world.campaign.feed.find(function (f) { return f.proposal; });
        const oneLiner = prop ? prop.text : world.campaign.proposal.evidence[0];
        const needs = el("div", "cp-card");
        const row = el("div", "cp-row");
        const left = el("div");
        left.appendChild(el("div", "cp-h", '<i class="ti ti-bell"></i> Needs you'));
        left.appendChild(el("div", "cp-sub", esc(oneLiner)));
        row.appendChild(left);
        row.appendChild(el("span", "spacer"));
        const review = el("button", "btn-primary", "Review →");
        review.type = "button";
        review.addEventListener("click", function () { location.hash = "#/campaign/console"; });
        row.appendChild(review);
        needs.appendChild(row);
        narrow.appendChild(needs);
      }

      // ---- brand-health strip --------------------------------------------
      // flight fixture: "May 1–14 · TikTok 9:16 · $1,500" → [dates, channel, budget]
      const flightParts = String(world.campaign.flight).split(" · ");
      const health = el("div", "cp-card");
      const sect = el("div", "cp-sect", "Brand health");
      sect.style.marginTop = "0";
      health.appendChild(sect);
      const cells = el("div", "cp-grid");
      cells.style.gridTemplateColumns = "repeat(3, 1fr)";

      const lib = el("div");
      lib.appendChild(el("div", "cp-tiny", "LIBRARY"));
      lib.appendChild(el("div", "cp-h", esc(world.assets.length + " assets")));
      if (world.ingest.pending) {
        lib.appendChild(el("span", "cp-pill is-busy", "1 ingest question open"));
      } else {
        lib.appendChild(el("div", "cp-tiny", "understood · searchable"));
      }
      cells.appendChild(lib);

      const budget = el("div");
      budget.appendChild(el("div", "cp-tiny", "BUDGET GUARDRAIL"));
      budget.appendChild(el("div", "cp-h", esc(flightParts[2] || world.campaign.flight)));
      budget.appendChild(el("div", "cp-tiny", esc(world.campaign.launch.pacing)));
      cells.appendChild(budget);

      const chan = el("div");
      chan.appendChild(el("div", "cp-tiny", "CHANNELS"));
      chan.appendChild(el("div", "cp-h", esc(flightParts[1] || "—")));
      chan.appendChild(el("div", "cp-tiny", esc(flightParts[0] || "")));
      cells.appendChild(chan);

      health.appendChild(cells);
      narrow.appendChild(health);
    },
  };
})();
