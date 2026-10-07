/* ============================================================================
 * Edenn — campaign console: Library screen (#/library).
 *
 * The brand's asset library as a board of family rows: each source asset ->
 * its derived children (published cuts) or its understood moments. Search
 * filters by what's IN the footage (asset name, moment labels, child names);
 * collection pills filter by collection. While ingest has a pending
 * ambiguity, a conversation card sits at the top of the body; answering it
 * (store.actions.ingestAnswer) files the asset and flips the card to the
 * "Filed" state with the campaign entry point.
 *
 * Pure renderer: reads store.get() only, mutates only via store.actions.
 * Transient UI state (search text, active collection) lives in module scope
 * so it survives the router's re-render-on-emit.
 * ========================================================================== */
(function () {
  "use strict";
  window.CampaignScreens = window.CampaignScreens || {};

  // ---- transient UI state (survives re-renders; re-derivable view state) ---
  const local = { q: "", collection: null };

  const KIND_ICON = { video: "ti-player-play", audio: "ti-music", image: "ti-photo" };

  // ---- helpers -------------------------------------------------------------
  function familyOf(w, assetId) {
    return w.families.find(function (f) { return f.sourceId === assetId; }) || null;
  }
  function collectionName(w, id) {
    const c = w.collections.find(function (x) { return x.id === id; });
    return c ? c.name : id;
  }

  /** True when the asset survives the current search + collection filters. */
  function assetMatches(w, a) {
    if (local.collection && a.collection !== local.collection) return false;
    const q = local.q.trim().toLowerCase();
    if (!q) return true;
    if (a.name.toLowerCase().indexOf(q) !== -1) return true;
    const moments = a.moments || [];
    for (let i = 0; i < moments.length; i++) {
      if (moments[i].label.toLowerCase().indexOf(q) !== -1) return true;
    }
    const fam = familyOf(w, a.id);
    if (fam) {
      for (let j = 0; j < fam.children.length; j++) {
        if (fam.children[j].name.toLowerCase().indexOf(q) !== -1) return true;
      }
    }
    return false;
  }

  /** Child boxes for a family row: derived cuts first, else understood moments. */
  function childBoxes(a, fam, ui) {
    const out = [];
    function box(html) {
      const b = ui.el("button", "cp-child", html);
      b.type = "button";
      b.addEventListener("click", function () { ui.toast("In product: opens the detail view"); });
      out.push(b);
    }
    if (fam && fam.children.length) {
      fam.children.forEach(function (ch) {
        box("<span><b>" + ui.esc(ch.name) + "</b><br>" + ui.esc(ch.note) + "</span>");
      });
    } else if (a.moments && a.moments.length) {
      a.moments.forEach(function (m) {
        box("<span><b>" + ui.esc(m.label) + "</b>" + (m.hero ? " ★" : "") +
            '<br><span class="cp-tiny">' + ui.esc(m.at) + "</span></span>");
      });
    }
    return out;
  }

  /** One board row: source thumb -> children, plus used / archive pills. */
  function assetCard(w, a, ui) {
    const card = ui.el("div", "cp-card cp-grid");
    const fam = familyOf(w, a.id);

    const row = ui.el("div", "cp-family");
    row.appendChild(ui.el("div", "cp-thumb",
      '<i class="ti ' + ui.esc(KIND_ICON[a.kind] || "ti-file") + '"></i>' +
      "<b>" + ui.esc(a.name) + "</b>" +
      '<span class="cp-tiny">' + ui.esc(a.meta) + "</span>"));
    childBoxes(a, fam, ui).forEach(function (k) { row.appendChild(k); });
    card.appendChild(row);

    const foot = ui.el("div", "cp-row");
    foot.appendChild(ui.el("span", "cp-tiny", ui.esc(collectionName(w, a.collection) + " · " + a.kind)));
    foot.appendChild(ui.el("span", "spacer"));
    if (fam && fam.used) foot.appendChild(ui.el("span", "cp-pill", ui.esc(fam.used)));
    if (a.archive && w.archiveNumbers[a.id]) {
      foot.appendChild(ui.el("span", "cp-pill is-blue", ui.esc(w.archiveNumbers[a.id])));
    }
    card.appendChild(foot);
    return card;
  }

  /** Ingest area: pending -> conversation card; answered -> "Filed" card. */
  function ingestCard(store, ui) {
    const w = store.get();
    const ing = w.ingest;
    if (ing.pending) {
      const card = ui.el("div", "cp-card cp-grid");
      card.appendChild(ui.agentMsg(
        "Files ingested and filed by what's in them — scenes, speech, product shots. One thing I can't resolve on my own:"));
      card.appendChild(ui.questionCard(ing.question, function (choice) {
        store.actions.ingestAnswer(choice);
      }));
      card.appendChild(ui.el("div", "cp-tiny", ui.esc(ing.dedupe)));
      return card;
    }
    if (ing.question.answer) {
      const card = ui.el("div", "cp-card");
      const row = ui.el("div", "cp-row");
      row.appendChild(ui.el("span", "cp-h", ui.esc("✓ Filed — the Library is ready")));
      row.appendChild(ui.el("span", "spacer"));
      const go = ui.el("button", "btn-primary", ui.esc("Start the campaign →"));
      go.type = "button";
      go.addEventListener("click", function () { location.hash = "#/campaign"; });
      row.appendChild(go);
      card.appendChild(row);
      return card;
    }
    return null;
  }

  window.CampaignScreens.library = {
    /** @param {HTMLElement} mount @param {typeof window.CampaignStore} store
        @param {typeof window.CampaignUI} ui */
    render: function (mount, store, ui) {
      mount.appendChild(ui.topbar("Library", null, null));

      const body = ui.el("div", "cp-body");
      const wrap = ui.el("div", "cp-narrow cp-grid");
      body.appendChild(wrap);
      mount.appendChild(body);

      // Ingest conversation / filed card — top of body.
      const ing = ingestCard(store, ui);
      if (ing) wrap.appendChild(ing);

      // Search bar + hint. Local filtering only — no store emit, so the
      // input (and its focus) survives; pills/board refresh in place.
      const srow = ui.el("div", "cp-row");
      const bar = ui.el("div", "cp-searchbar");
      bar.appendChild(ui.el("i", "ti ti-search"));
      const input = document.createElement("input");
      input.type = "search";
      input.placeholder = "Search the library…";
      input.value = local.q;
      input.addEventListener("input", function () { local.q = input.value; refresh(); });
      bar.appendChild(input);
      srow.appendChild(bar);
      srow.appendChild(ui.el("span", "cp-tiny", ui.esc("search by what's IN it — try “pour”")));
      wrap.appendChild(srow);

      // Collection pills + board — rebuilt on every local filter change.
      const pillsRow = ui.el("div", "pills");
      const board = ui.el("div", "cp-board");
      wrap.appendChild(pillsRow);
      wrap.appendChild(board);

      function refresh() {
        const w = store.get();

        pillsRow.innerHTML = "";
        w.collections.forEach(function (c) {
          const on = local.collection === c.id;
          const b = ui.el("button", "cp-pill" + (on ? " is-teal" : ""),
            '<i class="ti ' + ui.esc(c.icon) + '"></i> ' + ui.esc(c.name) + " · " + ui.esc(c.count));
          b.type = "button";
          b.addEventListener("click", function () {
            local.collection = local.collection === c.id ? null : c.id;
            refresh();
          });
          pillsRow.appendChild(b);
        });

        board.innerHTML = "";
        const visible = w.assets.filter(function (a) { return assetMatches(w, a); });
        if (!visible.length) {
          board.appendChild(ui.el("div", "cp-empty", ui.esc(
            "Nothing matches — clear the search or collection filter. (The prototype ships a sample of each collection.)")));
          return;
        }
        visible.forEach(function (a) { board.appendChild(assetCard(w, a, ui)); });
      }
      refresh();
    },
  };
})();
