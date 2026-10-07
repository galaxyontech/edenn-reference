/* ============================================================================
 * Edenn — launch screen (#/campaign/launch).
 *
 * The Gate ② moment: the market agent presents its launch position — budget
 * split, pacing guardrails, autonomy contract — and the ONLY thing between
 * the plan and real spend is one consent card. Launch is a thread stage, so
 * the topbar keeps the seg triple with "thread" active; the progress strip
 * pins to "Launch" (ix 3) while stage === "rendered".
 *
 * Pure renderer: reads store.get(), mutates only via store.actions, spend
 * gated only through ui.confirmSpend. No transient UI state is needed here
 * (no drawer/filter), so nothing lives in module scope beyond constants.
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
  // Shared stage → ix map; on THIS screen "rendered" pins to 3 (the Launch step).
  const STAGE_IX = { brief: 0, roles: 1, planned: 2, rendered: 3, live: 4, proposal: 4, round2: 2 };
  // Stages strictly before "rendered": nothing to launch yet — guard the body.
  const PRE_RENDER = { brief: true, roles: true, planned: true };

  window.CampaignScreens.launch = {
    /** @param {HTMLElement} mount @param {typeof window.CampaignStore} store
        @param {typeof window.CampaignUI} ui */
    render: function (mount, store, ui) {
      const world = store.get();
      const c = world.campaign;

      // Scaffold: topbar → progress → body.
      mount.appendChild(ui.topbar(c.name, SEGS, "thread"));
      mount.appendChild(ui.progress(STAGES, STAGE_IX[world.stage] != null ? STAGE_IX[world.stage] : 2));

      const body = ui.el("div", "cp-body");
      const narrow = ui.el("div", "cp-narrow cp-grid");
      body.appendChild(narrow);
      mount.appendChild(body);

      // Guard: Gate ① hasn't fired — there is nothing to launch yet.
      if (PRE_RENDER[world.stage]) {
        const empty = ui.el("div", "cp-empty");
        empty.appendChild(ui.el("div", null, "Render the variants first — Gate ① lives in the thread"));
        const back = ui.el("button", "btn-primary", "Open the thread →");
        back.type = "button";
        back.style.marginTop = "14px";
        back.addEventListener("click", function () { location.hash = "#/campaign"; });
        empty.appendChild(back);
        narrow.appendChild(empty);
        return;
      }

      // Agent intro — the market agent states its opening position.
      narrow.appendChild(ui.agentMsg(
        "Launch plan — " + c.flight + ". I weighted the split by preview signal and " +
        "armed the guardrails below; that's the contract I operate under once we're " +
        "live. Small reallocations get logged, bigger moves get asked. Nothing spends " +
        "until you approve — Gate ②."));

      // Budget split table.
      const splitCard = ui.el("div", "cp-card");
      splitCard.appendChild(ui.el("div", "cp-h", "Budget split"));
      const table = ui.el("table", "cp-table");
      let rows = "<thead><tr><th>Variant</th><th>Budget</th><th>Why</th></tr></thead><tbody>";
      c.launch.split.forEach(function (r) {
        rows += "<tr><td>" + ui.esc(r[0]) + "</td><td>" + ui.esc(r[1]) + "</td><td>" + ui.esc(r[2]) + "</td></tr>";
      });
      table.innerHTML = rows + "</tbody>";
      splitCard.appendChild(table);
      narrow.appendChild(splitCard);

      // Pacing guardrails.
      const pacingCard = ui.el("div", "cp-card");
      pacingCard.appendChild(ui.el("div", "cp-h", "Pacing"));
      const pacing = ui.el("div", "cp-sub", ui.esc(c.launch.pacing));
      pacing.style.marginTop = "6px";
      pacingCard.appendChild(pacing);
      narrow.appendChild(pacingCard);

      // Autonomy contract — what runs solo vs what asks first.
      const autoCard = ui.el("div", "cp-card");
      const autoHead = ui.el("div", "cp-row");
      autoHead.appendChild(ui.el("div", "cp-h", "What I'll do on my own vs ask"));
      autoHead.appendChild(ui.el("span", "spacer"));
      const editRules = ui.el("button", "cp-pill", '<i class="ti ti-adjustments"></i> edit rules');
      editRules.type = "button";
      editRules.addEventListener("click", function () {
        ui.toast("In product: edit the autonomy boundaries — the market agent runs inside whatever you set here.");
      });
      autoHead.appendChild(editRules);
      autoCard.appendChild(autoHead);
      const autonomy = ui.el("div", "cp-sub", ui.esc(c.launch.autonomy));
      autonomy.style.marginTop = "6px";
      autoCard.appendChild(autonomy);
      narrow.appendChild(autoCard);

      // The three rendered cuts, ready to go.
      narrow.appendChild(ui.el("div", "cp-sect", "Rendered variants"));
      const thumbs = ui.el("div", "cp-row");
      c.variants.filter(function (v) { return v.id.length === 1; }).forEach(function (v) {
        const t = ui.el("div", "cp-thumb", "<span>▶ " + ui.esc(v.id) + " · 15s</span>");
        t.title = v.label;
        t.style.cursor = "pointer";
        t.addEventListener("click", function () { ui.toast("In product: plays the rendered cut."); });
        thumbs.appendChild(t);
      });
      narrow.appendChild(thumbs);

      // Footer: adjust (free) vs Launch (Gate ② — the one that spends).
      const foot = ui.el("div", "cp-row");
      foot.style.marginTop = "4px";
      const adjust = ui.el("button", "btn-ghost", '<i class="ti ti-adjustments-horizontal"></i> Adjust plan');
      adjust.type = "button";
      adjust.addEventListener("click", function () {
        ui.toast("In product: reopens the plan in the thread — split, pacing and rules are negotiable before launch.");
      });
      foot.appendChild(adjust);
      foot.appendChild(ui.el("span", "spacer"));
      if (world.stage === "rendered") {
        const go = ui.el("button", "btn-primary", "Launch →");
        go.type = "button";
        go.addEventListener("click", function () {
          ui.confirmSpend(
            "Launch this campaign?",
            "3 variants go live on TikTok. $1,500 over 14 days from your connected ad " +
            "account. The market agent operates within the rules you just saw.",
            "Launch — spends budget"
          ).then(function (ok) {
            if (!ok) return;
            store.actions.confirmLaunch();
            location.hash = "#/campaign/console";
          });
        });
        foot.appendChild(go);
      } else {
        // Already live (or beyond): Gate ② is behind us — point at the console.
        foot.appendChild(ui.el("span", "cp-pill is-teal", '<i class="ti ti-check"></i> launched'));
        const toConsole = ui.el("button", "btn-primary", "Open the console →");
        toConsole.type = "button";
        toConsole.addEventListener("click", function () { location.hash = "#/campaign/console"; });
        foot.appendChild(toConsole);
      }
      narrow.appendChild(foot);
    },
  };
})();
