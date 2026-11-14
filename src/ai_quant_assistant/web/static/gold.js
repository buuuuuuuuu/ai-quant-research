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
  const plot = document.getElementById("gold-plot");
  if (!plot) return;
  const readout = document.getElementById("gold-selected-day");
  const library = window.LightweightCharts;
  if (!library) {
    readout.textContent = "走势图加载失败，可展开下方逐日收盘价。";
    return;
  }
  const rows = JSON.parse(document.getElementById("gold-series").textContent);
  const seconds = day => Date.parse(day + "T00:00:00Z") / 1000;
  const dayAt = time => new Date(time * 1000).toISOString().slice(0, 10);
  const first = seconds(plot.parentElement.dataset.first);
  const last = seconds(rows.at(-1)[0]);
  const daySeconds = 86400;
  const byTime = new Map(rows.map(([day, close]) => [seconds(day), close]));
  const price = new Intl.NumberFormat("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const chart = library.createChart(plot, {
    autoSize: true,
    width: plot.clientWidth,
    height: plot.clientHeight,
    localization: {locale: "zh-CN", timeFormatter: dayAt, priceFormatter: value => price.format(value)},
    layout: {textColor: "#465950", attributionLogo: false},
    grid: {vertLines: {color: "#edf2ef"}, horzLines: {color: "#edf2ef"}},
    crosshair: {mode: library.CrosshairMode.Normal},
    handleScroll: {vertTouchDrag: false, horzTouchDrag: true},
    timeScale: {
      minBarSpacing: 0.01, fixLeftEdge: true, fixRightEdge: true, lockVisibleTimeRangeOnResize: true,
      tickMarkFormatter: (time, kind) => kind === library.TickMarkType.Year
        ? dayAt(time).slice(0, 4) : dayAt(time).slice(5),
    },
  });
  const lineOptions = {
    color: "#916115", lineWidth: 2, lastValueVisible: false, priceLineVisible: false,
    pointMarkersVisible: (last - first) <= 7 * daySeconds,
  };
  // Give every calendar day an axis position, including days without a quote.
  // Explicit missing closes split the price line; whitespace alone would bridge the gap.
  const calendar = [];
  const segments = [];
  let segment = [];
  for (let time = first; time <= last; time += daySeconds) {
    calendar.push({time});
    if (!byTime.has(time)) continue;
    const value = byTime.get(time);
    if (value === null) {
      if (segment.length) segments.push(segment);
      segment = [];
    } else {
      segment.push({time, value});
    }
  }
  if (segment.length) segments.push(segment);
  // The library anchors its right edge at the last non-whitespace item. A hidden
  // calendar index on its own scale keeps trailing days selectable without a price.
  calendar[calendar.length - 1] = {time: last, value: calendar.length - 1};
  chart.addSeries(library.LineSeries, {visible: false, priceScaleId: "calendar"}).setData(calendar);
  for (const points of segments) {
    chart.addSeries(library.LineSeries, {
      ...lineOptions, pointMarkersVisible: lineOptions.pointMarkersVisible || points.length === 1,
    }).setData(points);
  }
  const scale = chart.timeScale();
  const caption = plot.parentElement.querySelector("figcaption");
  scale.subscribeVisibleTimeRangeChange(range => {
    if (range) caption.textContent = "日期范围：" + dayAt(range.from) + " 至 " + dayAt(range.to);
  });
  scale.fitContent();

  let selected = null;
  function describe(time) {
    const close = byTime.get(time);
    return dayAt(time) + " · " + (close == null ? "本地无该日收盘价" : "收盘价 " + price.format(close) + " 元/克");
  }
  function pin(time) {
    selected = time;
    readout.textContent = "已选 " + describe(time);
  }
  chart.subscribeClick(({time}) => {
    if (time != null) pin(time);
  });
  chart.subscribeCrosshairMove(({time}) => {
    if (selected === null) readout.textContent = time == null ? "" : describe(time);
  });
  plot.addEventListener("keydown", event => {
    const range = scale.getVisibleRange();
    if (!range) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      const direction = event.key === "ArrowLeft" ? -1 : 1;
      const next = selected === null ? (direction < 0 ? range.to : range.from) : selected + direction * daySeconds;
      pin(Math.max(range.from, Math.min(range.to, next)));
    } else if (["+", "=", "-"].includes(event.key)) {
      const logical = scale.getVisibleLogicalRange();
      const center = (logical.from + logical.to) / 2;
      const half = (logical.to - logical.from) * (event.key === "-" ? 0.75 : 1 / 3);
      scale.setVisibleLogicalRange({from: center - half, to: center + half});
    } else if (event.key === "Escape") {
      selected = null;
      readout.textContent = "";
    } else return;
    event.preventDefault();
  });
})();
