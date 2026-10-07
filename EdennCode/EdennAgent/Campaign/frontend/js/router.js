/* ============================================================================
 * Edenn — campaign console router.
 *
 * Hash routes → screen renderers. Each screen file registers itself on
 * window.CampaignScreens as { render(mount, store, ui) } BEFORE this file
 * loads (script order in index.html). The router owns: route matching,
 * sidebar active state, re-render on store changes, and scroll reset.
 * ========================================================================== */
(function () {
  "use strict";

  const ROUTES = {
    "#/home": "home",
    "#/library": "library",
    "#/campaign": "thread",
    "#/campaign/canvas": "canvas",
    "#/campaign/launch": "launch",
    "#/campaign/console": "console",
    "#/studio": "studio",
  };
  const DEFAULT_ROUTE = "#/home";

  const mount = document.getElementById("cp-main");
  const store = window.CampaignStore;
  const ui = window.CampaignUI;
  let current = null;

  function route() {
    const hash = location.hash || DEFAULT_ROUTE;
    const name = ROUTES[hash] || ROUTES[DEFAULT_ROUTE];
    current = name;
    // The Audio studio is a persistent embedded section (owner decision: the
    // studio IS part of campaign management): toggle it instead of rendering
    // a screen, so an in-progress studio session survives navigation.
    const studio = document.getElementById("cp-studio");
    if (studio) {
      const onStudio = name === "studio";
      const frame = document.getElementById("cp-studio-frame");
      if (onStudio && frame && !frame.getAttribute("src")) {
        frame.setAttribute("src", frame.getAttribute("data-src"));
      }
      studio.hidden = !onStudio;
      mount.hidden = onStudio;
      if (onStudio) {
        document.querySelectorAll("#cp-nav .nav__item").forEach(function (b) {
          b.classList.toggle("is-active", b.getAttribute("data-route") === "#/studio");
        });
        return;
      }
    }
    render();
    // Sidebar active state: the top-level section owns sub-routes.
    document.querySelectorAll("#cp-nav .nav__item").forEach(function (b) {
      const r = b.getAttribute("data-route");
      const on = r === "#/home" ? hash === "#/home"
        : hash.indexOf(r) === 0;
      b.classList.toggle("is-active", on);
    });
    mount.scrollTop = 0;
  }

  function render() {
    if (!current) return;
    const screen = (window.CampaignScreens || {})[current];
    mount.innerHTML = "";
    if (screen && typeof screen.render === "function") {
      screen.render(mount, store, ui);
    } else {
      mount.appendChild(ui.el("div", "cp-missing", "Screen not registered: " + ui.esc(current)));
    }
  }

  document.querySelectorAll("#cp-nav .nav__item").forEach(function (b) {
    b.addEventListener("click", function () { location.hash = b.getAttribute("data-route"); });
  });
  window.addEventListener("hashchange", route);
  store.subscribe(render);
  route();
})();
