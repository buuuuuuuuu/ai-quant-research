/* Market navigation is read-only. Only the existing explicit sync forms write data. */
(() => {
  const root = document.querySelector(".market-workspace");
  if (!root) return;
  const select = document.getElementById("index-code");
  const rows = [...root.querySelectorAll(".market-watch-row")];
  const search = document.getElementById("market-search");
  const favoritesOnly = document.getElementById("market-favorites-only");
  const note = document.getElementById("market-watch-note");
  const allowed = new Set(rows.map(row => row.dataset.marketCode));
  const storageKey = "market-favorites-v1";
  let favorites = new Set();
  try {
    const saved = JSON.parse(window.localStorage.getItem(storageKey) || "[]");
    if (Array.isArray(saved)) favorites = new Set(saved.filter(code => allowed.has(code)));
  } catch { /* Navigation remains usable with blocked storage. */ }

  function filterRows() {
    const query = search.value.trim().toLowerCase().replace(/\s/g, "");
    for (const row of rows) {
      const {marketCode: code, marketName: name, marketAlias: alias} = row.dataset;
      row.hidden = !`${name}${code}${alias}`.toLowerCase().includes(query) || (favoritesOnly.checked && !favorites.has(code));
      const star = row.querySelector("[data-market-star]");
      star.textContent = favorites.has(code) ? "★" : "☆";
      star.setAttribute("aria-pressed", String(favorites.has(code)));
      star.setAttribute("aria-label", `${favorites.has(code) ? "从自选移除" : "加入自选"} ${name}`);
    }
    document.getElementById("market-no-match").hidden = rows.some(row => !row.hidden);
  }
  const tabs = [...root.querySelectorAll("[data-market-tab]")];
  function showTab(name, focus = false) {
    if (!tabs.some(tab => tab.dataset.marketTab === name)) return;
    for (const tab of tabs) {
      const active = tab.dataset.marketTab === name;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      if (active && focus) tab.focus();
    }
    for (const panel of root.querySelectorAll("[data-market-panel]")) panel.hidden = panel.dataset.marketPanel !== name;
  }
  function updateSelection() {
    const code = select ? select.value : "gold";
    for (const row of rows) {
      const link = row.querySelector("[data-market-pick]");
      if (row.dataset.marketCode === code) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
      if (select && row.dataset.marketCode !== "gold") link.setAttribute("aria-disabled", String(select.disabled));
    }
    if (select) {
      document.getElementById("market-title").textContent = select.selectedOptions[0].textContent;
      document.getElementById("market-code").textContent = code;
      const url = new URL(window.location.href);
      url.searchParams.set("index", code);
      try { window.history.replaceState(null, "", url); } catch { /* Embedded previews may disallow history. */ }
    }
  }
  for (const row of rows) {
    const code = row.dataset.marketCode;
    row.querySelector("[data-market-star]").addEventListener("click", () => {
      if (favorites.has(code)) favorites.delete(code); else favorites.add(code);
      try { window.localStorage.setItem(storageKey, JSON.stringify([...favorites])); }
      catch { note.textContent = "浏览器禁止保存自选；本次页面内仍可使用，刷新后不保留。"; }
      filterRows();
    });
    row.querySelector("[data-market-pick]").addEventListener("click", event => {
      if (!select || code === "gold" || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      if (select.disabled) {
        note.textContent = "当前正在同步或查询任务，完成后可切换指数；进度显示在右侧内容区底部。";
        return;
      }
      if (select.value !== code) {
        select.value = code;
        select.dispatchEvent(new Event("change", {bubbles: true}));
      }
      showTab("chart");
      updateSelection();
    });
  }
  search.addEventListener("input", filterRows);
  search.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault(); rows.find(row => !row.hidden)?.querySelector("[data-market-pick]").click();
    } else if (event.key === "Escape") { search.value = ""; filterRows(); }
  });
  favoritesOnly.addEventListener("change", filterRows);
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => showTab(tab.dataset.marketTab));
    tab.addEventListener("keydown", event => {
      let next;
      if (event.key === "ArrowRight") next = (i + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (i + tabs.length - 1) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else return;
      event.preventDefault(); showTab(tabs[next].dataset.marketTab, true);
    });
  });
  for (const opener of root.querySelectorAll("[data-market-open]")) opener.addEventListener("click", () => showTab(opener.dataset.marketOpen, true));
  const range = document.getElementById("index-range");
  if (range) {
    const buttons = [...root.querySelectorAll("[data-market-range]")];
    function updateRange() {
      for (const button of buttons) button.setAttribute("aria-pressed", String(button.dataset.marketRange === range.value));
      document.getElementById("market-custom-range").hidden = range.value !== "custom";
    }
    for (const button of buttons) button.addEventListener("click", () => {
      range.value = button.dataset.marketRange;
      range.dispatchEvent(new Event("change", {bubbles: true}));
    });
    range.addEventListener("change", updateRange); updateRange();
  }
  document.addEventListener("keydown", event => {
    if (event.ctrlKey || event.metaKey || event.altKey || event.isComposing ||
        event.target.closest?.("input, textarea, select, [contenteditable='true'], [role='textbox']")) return;
    if (event.key === "/") { event.preventDefault(); search.focus(); }
    else if (event.key === "F10" && select) { event.preventDefault(); showTab("profiles", true); }
  });
  if (select) {
    select.addEventListener("change", updateSelection);
    new MutationObserver(updateSelection).observe(select, {attributes: true, attributeFilter: ["disabled"]});
  }
  filterRows(); updateSelection(); showTab("chart");
})();
