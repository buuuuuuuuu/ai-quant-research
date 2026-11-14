// Interactive charts for a single explorer page or a saved factor backtest.
window.WorkbenchCharts = (() => {
  const active = new WeakMap();
  const navLines = [
    {key: "portfolio_nav", label: "策略净值", color: "#24523b"},
    {key: "benchmark_nav", label: "基准净值", color: "#99712f"},
  ];

  function clear(host) {
    active.get(host)?.remove();
    active.delete(host);
    host.replaceChildren();
  }

  function draw(host, rows, dateKey, lines, {title, slider = false} = {}) {
    clear(host);
    const byDate = new Map();
    for (const row of rows) {
      const day = row[dateKey];
      if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) continue;
      const time = Date.parse(day + "T00:00:00Z") / 1000;
      if (!Number.isFinite(time) || new Date(time * 1000).toISOString().slice(0, 10) !== day) continue;
      const values = lines.map(({key}) => {
        const raw = row[key];
        if (typeof raw !== "number" && !(typeof raw === "string" && raw.trim())) return null;
        const value = Number(raw);
        return Number.isFinite(value) ? value : null;
      });
      const existing = byDate.get(time);
      if (existing) {
        // Conflicting versions of a report must remain visible in the records table.
        values.forEach((value, i) => { if (value !== existing[i]) existing[i] = undefined; });
      } else byDate.set(time, values);
    }
    const times = [...byDate.keys()].sort((a, b) => a - b);
    if (!times.length) {
      host.textContent = "暂无可绘制的日期记录。";
      return;
    }

    const plot = document.createElement("div");
    plot.className = "workbench-time-plot";
    plot.tabIndex = 0;
    plot.setAttribute("role", "group");
    plot.setAttribute("aria-label", title);
    const help = document.createElement("small");
    help.id = host.id + "-help";
    help.textContent = "点击锁定日期，方向键逐日查看，Esc 解除，滚轮或双指缩放。";
    const output = document.createElement("output");
    output.id = host.id + "-readout";
    plot.setAttribute("aria-describedby", help.id + " " + output.id);
    host.append(plot, help);
    const range = document.createElement("input");
    range.type = "range";
    range.min = "0";
    range.max = String(times.length - 1);
    range.value = "0";
    const label = document.createElement("label");
    label.append("查看日期 ", range);
    const lib = window.LightweightCharts;
    if (slider || !lib) host.append(label);
    host.append(output);
    const dayAt = time => new Date(time * 1000).toISOString().slice(0, 10);
    const format = new Intl.NumberFormat("zh-CN", {maximumSignificantDigits: 12});
    function describe(time) {
      const values = byDate.get(time);
      return [dayAt(time), ...lines.map((line, i) => {
        const value = values[i];
        return line.label + " " + (value === undefined ? "多条记录有差异" : value === null ? "缺失" : format.format(value));
      })].join(" · ");
    }

    let pinned = null;
    let scale;
    function pin(index) {
      pinned = index;
      range.value = String(index);
      range.setAttribute("aria-valuetext", describe(times[index]));
      output.textContent = "已选 " + describe(times[index]);
      const visible = scale?.getVisibleLogicalRange();
      if (visible && (index < visible.from || index > visible.to)) {
        const half = (visible.to - visible.from) / 2;
        scale.setVisibleLogicalRange({from: index - half, to: index + half});
      }
    }
    range.addEventListener("input", () => pin(Number(range.value)));
    const initial = slider ? 0 : times.length - 1;
    output.textContent = describe(times[initial]);
    range.value = String(initial);
    range.setAttribute("aria-valuetext", output.textContent);
    if (!lib) {
      plot.hidden = true;
      help.textContent = "走势图加载失败，可拖动滑块逐日查看。";
      return;
    }

    const axisFormat = new Intl.NumberFormat("zh-CN", {maximumSignificantDigits: 6});
    const chart = lib.createChart(plot, {
      autoSize: true,
      layout: {attributionLogo: false, textColor: "#465950"},
      grid: {vertLines: {color: "#edf2ef"}, horzLines: {color: "#edf2ef"}},
      crosshair: {mode: lib.CrosshairMode.Normal},
      localization: {locale: "zh-CN", timeFormatter: dayAt, priceFormatter: v => axisFormat.format(v)},
      handleScroll: {vertTouchDrag: false},
      timeScale: {minBarSpacing: 0.01, fixLeftEdge: true, fixRightEdge: true},
    });
    active.set(host, chart);
    // Anchor every reported date, including leading/trailing rows with missing values.
    chart.addSeries(lib.LineSeries, {visible: false, priceScaleId: "dates"})
      .setData(times.map(time => ({time, value: 1})));
    lines.forEach(({color}, column) => {
      let segment = [];
      function flush() {
        if (!segment.length) return;
        chart.addSeries(lib.LineSeries, {
          color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
          pointMarkersVisible: segment.length === 1,
        }).setData(segment);
        segment = [];
      }
      for (const time of times) {
        const value = byDate.get(time)[column];
        if (value == null) flush();
        else segment.push({time, value});
      }
      flush();
    });
    scale = chart.timeScale();
    scale.fitContent();
    const credit = document.createElement("a");
    credit.href = "https://www.tradingview.com/";
    credit.textContent = "TradingView Lightweight Charts™";
    host.append(credit);
    chart.subscribeClick(({time}) => {
      if (byDate.has(time)) pin(times.indexOf(time));
    });
    chart.subscribeCrosshairMove(({time}) => {
      if (pinned === null) output.textContent = describe(byDate.has(time) ? time : times[Number(range.value)]);
    });
    plot.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        pinned = null;
        output.textContent = describe(times[Number(range.value)]);
      } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        const next = Number(range.value) + (event.key === "ArrowLeft" ? -1 : 1);
        pin(Math.max(0, Math.min(times.length - 1, next)));
      } else return;
      event.preventDefault();
    });
  }

  function drawNav(host, rows, slider = false) {
    draw(host, rows, "date", navLines, {title: "策略与基准净值走势", slider});
  }

  return {clear, draw, drawNav};
})();
