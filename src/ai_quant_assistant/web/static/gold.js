(() => {
  const button = document.getElementById("gold-refresh");
  const status = document.getElementById("gold-status");
  button.addEventListener("click", async () => {
    if (button.disabled || !window.confirm("从上海黄金交易所获取 Au99.99 历史收盘价并更新本地行情？")) return;
    button.disabled = true;
    status.textContent = "正在获取交易所日线…";
    try {
      const response = await fetch("/api/v1/gold/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "更新失败，请稍后重试。");
      if (result.status !== "ok") throw new Error("未收到更新结果，请刷新查看。");
      window.location.reload();
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });
})();

(() => {
  const figure = document.querySelector(".gold-chart");
  if (!figure) return;
  const svg = figure.querySelector("svg"), axis = figure.querySelector(".gold-axis");
  const caption = figure.querySelector("figcaption"), grid = svg.querySelector(".gold-guides");
  const cursor = svg.querySelector(".gold-cursor");
  const readout = document.getElementById("gold-selected-day");
  const rows = new Map(JSON.parse(document.getElementById("gold-series").textContent));
  const start = Date.parse(figure.dataset.first + "T00:00:00Z");
  const dayMs = 86400000;
  const length = 1 + Math.round((Date.parse(figure.dataset.last + "T00:00:00Z") - start) / dayMs);
  const dateAt = i => new Date(start + i * dayMs).toISOString().slice(0, 10);
  const xAt = i => length === 1 ? 500 : 2 + 996 * i / (length - 1);
  const currency = new Intl.NumberFormat("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  function choose(i) {
    const day = dateAt(i), price = rows.get(day);
    cursor.setAttribute("x1", xAt(i)); cursor.setAttribute("x2", xAt(i));
    cursor.style.display = "inline";
    readout.textContent = day + " · " + (price == null ? "本地无该日收盘价" : "收盘价 " + currency.format(price) + " 元/克");
  }
  function render(first, last) {
    const span = last - first;
    const width = (xAt(last) - xAt(first)) / 0.996;
    svg.setAttribute("viewBox", [xAt(first) - width * 0.002, 0, width, 200].join(" "));
    axis.replaceChildren();
    grid.replaceChildren();
    const intervals = Math.min(span, 6);
    for (let i = 0; i <= intervals; i++) {
      const offset = Math.round(i * span / intervals), day = dateAt(first + offset);
      const compact = i === 0 || i === Math.floor(intervals / 2) || i === intervals;
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("class", compact ? "gold-guide" : "gold-guide gold-wide-tick");
      line.setAttribute("x1", xAt(first + offset)); line.setAttribute("x2", xAt(first + offset));
      line.setAttribute("y1", "0"); line.setAttribute("y2", "200");
      line.setAttribute("vector-effect", "non-scaling-stroke"); grid.append(line);
      const tick = document.createElement("time");
      tick.className = compact ? "" : "gold-wide-tick";
      tick.dateTime = day;
      tick.style.left = (0.2 + 99.6 * offset / span) + "%";
      tick.textContent = span > 730 ? day.slice(0, 7) : day.slice(5);
      axis.append(tick);
    }
    caption.textContent = "日期范围：" + dateAt(first) + " 至 " + dateAt(last);
  }
  bindDateChart(svg, {length, render, select: choose,
    fractionAt: (x, first, last) => (x - xAt(first)) / (xAt(last) - xAt(first) || 1)});
})();
