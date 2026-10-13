/* Shared navigation only. No API calls and no execution on navigation. */
(() => {
  const routes = {
    "data-explorer": "/data",
    "managed-stock-panel": "/data",
    "stock-pools": "/data/pools",
    universe: "/data/pools",
    operations: "/system",
    copilot: "/research/copilot",
    backtest: "/backtests/new",
    results: "/backtests/new",
    "factor-lab": "/backtests/factors",
    screener: "/backtests/screen",
    "research-data-panel": "/data",
  };
  const resolve = (hash, search = "") => {
    const anchor = hash.replace(/^#/, "");
    return routes[anchor] ? `${routes[anchor]}${search}#${anchor}` : null;
  };
  if (typeof module !== "undefined") module.exports = { resolve };
  if (typeof document === "undefined") return;
  const sidebar = document.getElementById("app-navigation"),
    toggle = document.getElementById("nav-toggle"),
    backdrop = document.getElementById("nav-backdrop");
  if (!sidebar) return;
  const mobile = window.matchMedia("(max-width:1100px)");
  const setOpen = (open, focus = false) => {
    sidebar.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "关闭导航" : "打开导航");
    backdrop.hidden = !open;
    sidebar.inert = mobile.matches && !open;
    if (focus) (open ? sidebar.querySelector("a") : toggle).focus();
  };
  toggle.addEventListener("click", () =>
    setOpen(toggle.getAttribute("aria-expanded") !== "true", true),
  );
  backdrop.addEventListener("click", () => setOpen(false, true));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && toggle.getAttribute("aria-expanded") === "true")
      setOpen(false, true);
    if (
      e.key === "Tab" &&
      mobile.matches &&
      toggle.getAttribute("aria-expanded") === "true"
    ) {
      const links = [toggle, ...sidebar.querySelectorAll("a,button")];
      const first = links[0],
        last = links.at(-1);
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  });
  mobile.addEventListener("change", () => setOpen(false));
  setOpen(false);
  const path = location.pathname;
  const group = path.startsWith("/backtests")
    ? "backtests"
    : path.startsWith("/data")
      ? "data"
      : path === "/system"
        ? "system"
        : path.startsWith("/research") || path.startsWith("/stocks/")
          ? "research"
          : "overview";
  sidebar
    .querySelector(`[data-nav="${group}"]`)
    ?.setAttribute("aria-current", "page");
  const main = document.querySelector("main");
  if (main) main.id = "page-content";
  function legacy() {
    const query = new URLSearchParams(location.search);
    let target = resolve(location.hash, location.search);
    if (path === "/" && !target && query.has("stock"))
      target = `${query.has("event") ? "/data" : "/backtests/new"}${location.search}${location.hash}`;
    if (
      target &&
      new URL(target, location.origin).pathname !== location.pathname
    )
      location.replace(target);
  }
  legacy();
  window.addEventListener("hashchange", legacy);
  document.addEventListener("click", (e) => {
    const link = e.target.closest("a[href^='#']");
    if (!link) return;
    const target = resolve(link.getAttribute("href"), location.search);
    if (
      target &&
      new URL(target, location.origin).pathname !== location.pathname
    ) {
      e.preventDefault();
      location.assign(target);
    }
  });
})();
