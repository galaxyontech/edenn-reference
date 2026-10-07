/* ============================================================================
 * Edenn — campaign thread screen (#/campaign).
 *
 * The campaign conversation: renders `world.campaign.thread` in order (agent /
 * user / question / roles / plans / banner), the round-1 and round-2 variant
 * cards, the free plan step, spend Gate ① (render), and the brief composer.
 * Pure renderer: all state comes from the store; mutations go through
 * store.actions only; the render gate for spend is ui.confirmSpend.
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

  // ---- transient UI state (module scope — survives router re-renders) ------
  let composerText = null;   // user's in-progress edit; null → prefill the brief
  let composerFocused = false;
  let threadPinned = true;   // stick to the newest message unless scrolled up
  let threadScrollTop = 0;

  // ---- fragments -----------------------------------------------------------

  /** Roles message — a two-col card of the resolved role → source pairs. */
  function rolesCard(roles, ui) {
    const card = ui.el("div", "cp-card");
    const grid = ui.el("div", "cp-grid");
    grid.style.gridTemplateColumns = "92px 1fr";
    roles.forEach(function (r) {
      const key = ui.el("div", "cp-tiny", ui.esc(r[0]));
      key.style.textTransform = "uppercase";
      key.style.letterSpacing = ".06em";
      key.style.fontWeight = "700";
      key.style.paddingTop = "2px";
      grid.appendChild(key);
      const val = ui.el("div", null, ui.esc(r[1]));
      val.style.fontSize = "13px";
      grid.appendChild(val);
    });
    card.appendChild(grid);
    return card;
  }

  /** One variant card: head row, slot strip, published-relative line. */
  function variantCard(v, ui) {
    const card = ui.el("div", "cp-card");

    const head = ui.el("div", "cp-row");
    head.appendChild(ui.el("span", "cp-h", ui.esc(v.label)));
    head.appendChild(ui.el("span", "cp-pill", ui.esc(v.grounding)));
    head.appendChild(ui.el("span", "spacer"));
    head.appendChild(ui.statePill(v.state));
    const watch = ui.el("button", "btn-ghost", "▶ watch");
    watch.type = "button";
    watch.addEventListener("click", function () {
      ui.toast("Free preview — nothing renders until Gate ①");
    });
    head.appendChild(watch);
    card.appendChild(head);

    // Slot sketch — index 1 is the founder bite on B; no bite elsewhere.
    const strip = ui.strip(v.slots, v.id === "B" ? 1 : -1);
    strip.style.marginTop = "10px";
    card.appendChild(strip);

    const rel = ui.el("div", "cp-sub", v.relative
      ? "≈ relative: " + ui.esc(v.relative.name) + " — " + ui.esc(v.relative.note) + " · " + ui.esc(v.relative.metric)
      : "no published relative — the NEW bet");
    rel.style.marginTop = "8px";
    card.appendChild(rel);
    return card;
  }

  /** Round-2 seed banner. */
  function bannerCard(text, ui) {
    const card = ui.el("div", "cp-card cp-row");
    card.appendChild(ui.el("i", "ti ti-refresh"));
    const t = ui.el("span", null, ui.esc(text));
    t.style.fontSize = "13px";
    t.style.fontWeight = "600";
    card.appendChild(t);
    return card;
  }

  /** Gate ① — the first paid step; disabled while a render is in flight. */
  function gateRow(variants, store, ui) {
    const row = ui.el("div", "cp-row");
    const btn = ui.el("button", "btn-primary",
      '<i class="ti ti-lock"></i> ' + ui.esc("Lock & render all 3 — Gate ①"));
    btn.type = "button";
    if (variants.some(function (v) { return v.state !== "planned"; })) btn.disabled = true;
    btn.addEventListener("click", function () {
      ui.confirmSpend("Render 3 × 15s?",
        "Render cost applies now. No channel budget is spent until Launch.",
        "Render — Gate ①")
        .then(function (ok) { return ok && store.actions.confirmRender(); });
    });
    row.appendChild(btn);
    return row;
  }

  /** Bottom composer — prefilled brief while briefing, disabled after. */
  function composer(stage, campaign, store, ui) {
    const wrap = ui.el("div", "session-composer");
    const inner = ui.el("div", "session-composer__inner");
    const input = document.createElement("input");
    input.type = "text";
    input.autocomplete = "off";
    const send = ui.el("button", "send-btn", '<i class="ti ti-arrow-up"></i>');
    send.type = "button";
    send.title = "Send";
    if (stage === "brief") {
      input.value = composerText != null ? composerText : campaign.brief;
      input.addEventListener("input", function () { composerText = input.value; });
      input.addEventListener("focus", function () { composerFocused = true; });
      input.addEventListener("blur", function () { composerFocused = false; });
      const submit = function () {
        composerText = null;
        composerFocused = false;
        store.actions.sendBrief();
      };
      send.addEventListener("click", submit);
      input.addEventListener("keydown", function (e) { if (e.key === "Enter") submit(); });
    } else {
      input.disabled = true;
      input.placeholder = "Adjust anything…";
      send.disabled = true;
    }
    inner.appendChild(input);
    inner.appendChild(send);
    wrap.appendChild(inner);
    return wrap;
  }

  // ---- screen --------------------------------------------------------------
  window.CampaignScreens.thread = {
    /** @param {HTMLElement} mount @param {typeof window.CampaignStore} store
        @param {typeof window.CampaignUI} ui */
    render: function (mount, store, ui) {
      const world = store.get();
      const c = world.campaign;
      const stage = world.stage;

      mount.appendChild(ui.topbar(c.name, SEGS, "thread"));
      mount.appendChild(ui.progress(STAGES, STAGE_IX[stage] != null ? STAGE_IX[stage] : 0));

      const body = ui.el("div", "cp-body cp-body--thread");
      const threadEl = ui.el("div", "thread");
      const inner = ui.el("div", "thread__inner");
      threadEl.appendChild(inner);

      const round1 = c.variants.filter(function (v) { return v.id.length === 1; });
      const round2 = c.variants.filter(function (v) { return v.id.length > 1; });

      c.thread.forEach(function (msg) {
        if (msg.kind === "agent") {
          inner.appendChild(ui.agentMsg(msg.text));
        } else if (msg.kind === "user") {
          inner.appendChild(ui.userMsg(msg.text));
        } else if (msg.kind === "question") {
          inner.appendChild(ui.questionCard(msg, function (choice) {
            store.actions.answerQuestion(msg.id, choice);
          }));
        } else if (msg.kind === "roles") {
          inner.appendChild(rolesCard(c.roles, ui));
        } else if (msg.kind === "plans") {
          round1.forEach(function (v) { inner.appendChild(variantCard(v, ui)); });
          if (stage === "planned") inner.appendChild(gateRow(round1, store, ui));
        } else if (msg.kind === "banner") {
          inner.appendChild(bannerCard(msg.text, ui));
          if (stage === "round2") {
            round2.forEach(function (v) { inner.appendChild(variantCard(v, ui)); });
          }
        }
      });

      // Plan step: roles locked, nothing planned yet. Planning is FREE — no
      // spend gate; this is what advances roles → planned.
      if (stage === "roles" && c.roles.length) {
        const row = ui.el("div", "cp-row");
        const plan = ui.el("button", "btn-primary",
          '<i class="ti ti-list-check"></i> ' + ui.esc("Plan the 3 cuts — free"));
        plan.type = "button";
        plan.addEventListener("click", function () { store.actions.planVariants(); });
        row.appendChild(plan);
        inner.appendChild(row);
      }

      // Rendered → hand off to the Launch stage (Gate ② lives there).
      if (stage === "rendered") {
        const row = ui.el("div", "cp-row");
        const go = ui.el("button", "btn-primary", ui.esc("Go to Launch →"));
        go.type = "button";
        go.addEventListener("click", function () { location.hash = "#/campaign/launch"; });
        row.appendChild(go);
        inner.appendChild(row);
      }

      threadEl.addEventListener("scroll", function () {
        threadPinned = threadEl.scrollTop + threadEl.clientHeight >= threadEl.scrollHeight - 48;
        threadScrollTop = threadEl.scrollTop;
      });

      body.appendChild(threadEl);
      body.appendChild(composer(stage, c, store, ui));
      mount.appendChild(body);

      // Restore transient UI state now that the DOM is live.
      if (threadPinned) threadEl.scrollTop = threadEl.scrollHeight;
      else threadEl.scrollTop = threadScrollTop;
      if (stage === "brief" && composerFocused) {
        const input = body.querySelector(".session-composer input");
        if (input) {
          input.focus();
          input.setSelectionRange(input.value.length, input.value.length);
        }
      }
    },
  };
})();
