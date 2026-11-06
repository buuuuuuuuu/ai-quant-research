(() => {
  const select = document.getElementById("index-code"), modeSelect = document.getElementById("index-numerator"), status = document.getElementById("index-status"), result = document.getElementById("index-result");
  const currency = new Intl.NumberFormat("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  function money(value) {
    return value == null || value === "" || !Number.isFinite(Number(value)) ? "—" : currency.format(value);
  }
  function groupedDecimal(value) {
    if (value == null || value === "") return "—";
    const [integer, fraction] = String(value).split(".");
    const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return fraction === undefined ? grouped : grouped + "." + fraction;
  }

  // The Y labels end before the plot; X labels sit in a separate band below it.
  function chartAxes(svg, points, low, high, flat, width = 700) {
    const left = 18 + Math.max(low.length, high.length) * 8, right = width - 14;
    const xAt = i => left + i * (right - left) / Math.max(points.length - 1, 1);
    function label(x, y, anchor, text) {
      const node = document.createElementNS("http://www.w3.org/2000/svg", "text");
      node.setAttribute("x", x); node.setAttribute("y", y);
      node.setAttribute("text-anchor", anchor);
      node.setAttribute("font-size", "12"); node.setAttribute("font-family", "monospace");
      node.textContent = text; svg.append(node);
    }
    if (!flat) label(left - 12, 34, "end", high);
    label(left - 12, flat ? 114 : 194, "end", low);
    label(left, 215, "start", points[0].trade_date);
    if (points.length > 1) label(right, 215, "end", points.at(-1).trade_date);
    return xAt;
  }

  const examples = window.FundProfiles.examples;
  const fundInput = document.getElementById("index-funds");
  const exampleList = document.getElementById("index-examples");
  const fundNames = new Map(Object.values(examples).flat());
  const fundLabel = code => fundNames.has(code) ? fundNames.get(code) + " · " + code : code + "（名称待核实）";
  const selectedList = document.getElementById("index-selected");
  const selectedCount = document.getElementById("index-selected-count");
  const addExamples = document.getElementById("index-add-examples");
  const clearFunds = document.getElementById("index-clear-funds");
  const drafts = new Map();
  let editingIndex = select.value;
  const fundCodes = () => [...new Set(fundInput.value.toUpperCase().split(/[,，;；\s]+/).filter(Boolean))];
  function updateExampleButtons() {
    const codes = fundCodes();
    for (const button of exampleList.children) {
      const selected = codes.includes(button.value);
      button.disabled = codes.length >= 10 && !selected;
      button.setAttribute("aria-pressed", selected);
    }
    addExamples.disabled = codes.length >= 10 || (examples[select.value] || []).every(([code]) => codes.includes(code));
    clearFunds.disabled = !codes.length;
    selectedCount.textContent = "已选 " + codes.length + " / 10 只，统计所选基金的合计规模";
    window.FundProfiles.render(document.getElementById("index-fund-profiles"), select.value, codes);
    selectedList.replaceChildren();
    for (const code of codes) {
      const remove = document.createElement("button");
      remove.type = "button"; remove.className = "secondary-button";
      remove.textContent = fundLabel(code) + " ×";
      remove.setAttribute("aria-label", "移除 " + fundLabel(code));
      remove.addEventListener("click", () => {
        fundInput.value = fundCodes().filter(item => item !== code).join(",");
        updateExampleButtons();
      });
      selectedList.append(remove);
    }
  }
  addExamples.addEventListener("click", () => {
    const codes = new Set(fundCodes());
    for (const [code] of examples[select.value] || []) {
      if (codes.size >= 10) break;
      codes.add(code);
    }
    fundInput.value = [...codes].join(",");
    updateExampleButtons();
  });
  clearFunds.addEventListener("click", () => {
    fundInput.value = "";
    updateExampleButtons();
  });
  const usesExchangeReport = () => ["000300", "000016", "000688"].includes(select.value);
  const button = document.getElementById("index-submit");
  const syncHint = document.getElementById("index-sync-hint");
  function showExamples() {
    syncHint.hidden = usesExchangeReport();
    const group = examples[select.value] || [];
    exampleList.replaceChildren();
    exampleList.hidden = !group.length;
    for (const [code, manager] of group) {
      const button = document.createElement("button");
      button.type = "button"; button.value = code;
      button.className = "secondary-button";
      button.textContent = manager + " " + code;
      button.addEventListener("click", () => {
        const codes = fundCodes();
        if (codes.includes(code)) fundInput.value = codes.filter(item => item !== code).join(",");
        else if (codes.length < 10) fundInput.value = [...codes, code].join(",");
        updateExampleButtons();
      });
      exampleList.append(button);
    }
    updateExampleButtons();
  }
  fundInput.addEventListener("input", updateExampleButtons);
  showExamples();
  let request = 0, displayedIndex = null;
  let disconnectTrend = () => {};
  async function load() {
    const id = ++request, indexCode = select.value, requestedMode = modeSelect.value;
    status.textContent = displayedIndex ? `正在读取 ${indexCode}；暂时保留 ${displayedIndex} 的数据。` : "正在读取本地数据…";
    try {
      const response = await fetch(`/api/v1/index-funds/${encodeURIComponent(indexCode)}?numerator_mode=${encodeURIComponent(requestedMode)}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (id !== request) return;
      if (!payload.snapshot) { disconnectTrend(); result.replaceChildren(); displayedIndex = null; status.textContent = "本地暂无数据；需要先准备基金和指数流通市值数据。"; return; }
      const allPoints = payload.snapshot.result.points || [];
      const range = document.getElementById("index-range").value;
      let points = allPoints;
      if (range === "custom") {
        const start = document.getElementById("view-start").value, end = document.getElementById("view-end").value;
        if (!start || !end || start > end) throw new Error("请填写有效的自定义开始和结束日期");
        points = allPoints.filter(p => p.trade_date >= start && p.trade_date <= end);
      } else if (["7d", "30d", "90d"].includes(range) && allPoints.length) {
        const end = allPoints[allPoints.length - 1].trade_date;
        const cutoff = new Date(end + "T00:00:00Z");
        cutoff.setUTCDate(cutoff.getUTCDate() - parseInt(range, 10) + 1);
        const start = cutoff.toISOString().slice(0, 10);
        points = allPoints.filter(p => p.trade_date >= start && p.trade_date <= end);
      } else if (["1", "3", "5"].includes(range) && allPoints.length) {
        const end = allPoints[allPoints.length - 1].trade_date;
        const cutoff = new Date(end + "T00:00:00Z"); cutoff.setUTCFullYear(cutoff.getUTCFullYear() - Number(range));
        const start = cutoff.toISOString().slice(0, 10);
        points = allPoints.filter(p => p.trade_date > start && p.trade_date <= end);
      }
      disconnectTrend();
      result.replaceChildren();
      displayedIndex = null;
      displayedIndex = `${indexCode}（${requestedMode === "close" ? "收盘价" : "单位净值"}）`;
      const numeratorMode = payload.snapshot.spec.numerator_mode || "nav";
      const valueName = numeratorMode === "close" ? "不复权收盘价" : "单位净值";
      const metadata = document.createElement("details"), metadataTitle = document.createElement("summary");
      metadata.className = "market-snapshot-meta";
      metadataTitle.textContent = `当前结果：${(payload.snapshot.spec.funds || []).length}只基金 · 数据来源与导出`;
      metadata.append(metadataTitle); result.append(metadata);
      const modeLabel = document.createElement("p"); modeLabel.textContent = `当前快照口径：份额 × ${valueName}${numeratorMode === "close" ? "（价格来源尚待真实历史核验）" : ""}。快照 ${payload.snapshot.snapshot_id}`; metadata.append(modeLabel);
      const scope = document.createElement("p");
      scope.className = "fund-snapshot-scope";
      scope.textContent = "当前结果统计 " + (payload.snapshot.spec.funds || []).map(fundLabel).join("、") +
        "；同步区间 " + (payload.snapshot.spec.start || allPoints[0]?.trade_date || "未记录") +
        " 至 " + (payload.snapshot.spec.end || allPoints.at(-1)?.trade_date || "未记录") +
        "。修改“基金选择与同步”中的参数后，需点击同步才会生成新结果。";
      metadata.append(scope);
      const warning = document.createElement("p"); warning.textContent = payload.snapshot.result.warning || ""; result.append(warning);
      const download = document.createElement("a"); download.textContent = "导出完整快照 CSV";
      download.href = `/api/v1/index-fund-snapshots/${encodeURIComponent(payload.snapshot.snapshot_id)}/csv`;
      download.setAttribute("download", ""); metadata.append(download);
      const htmlDownload = document.createElement("a"); htmlDownload.textContent = " 导出完整快照离线 HTML";
      htmlDownload.href = `/api/v1/index-fund-snapshots/${encodeURIComponent(payload.snapshot.snapshot_id)}/html`;
      htmlDownload.setAttribute("download", ""); metadata.append(htmlDownload);
      status.textContent = "已读取本地快照；数据缺口显示为 —。";
      const valid = points.filter(p => p.ratio_fraction !== null && Number.isFinite(Number(p.ratio_fraction)));
      const coverage = document.createElement("p");
      coverage.className = "market-coverage";
      coverage.textContent = points.length ? `可计算 ${valid.length} / ${points.length} 个交易日；其余日期保留缺口。` : "当前快照在所选范围内没有数据。";
      result.append(coverage);
      if (valid.length) {
        const latest = valid[valid.length - 1], latestIndex = points.indexOf(latest), previous = valid[valid.length - 2];
        const summary = document.createElement("p");
        let change = "当前查看范围内无上一完整观测日";
        if (previous) {
          const difference = (Number(latest.ratio_fraction) - Number(previous.ratio_fraction)) * 100;
          change = `${points[latestIndex - 1] === previous ? "较上一交易日" : "跨缺口较上一完整观测日"}（${previous.trade_date}）${difference >= 0 ? "+" : ""}${difference.toFixed(4)} 个百分点`;
        }
        summary.textContent = `最新完整统计日 ${latest.trade_date}；${change}。`;
        const stats = document.createElement("dl"); stats.className = "market-stats";
        const compact = value => Number(value) >= 100000000 ? money(Number(value) / 100000000) + " 亿元" : money(value) + " 元";
        for (const [name, value] of [["基金规模占比", (Number(latest.ratio_fraction) * 100).toFixed(4) + "%"], ["基金合计规模", compact(latest.numerator_cny)], ["指数流通市值", compact(latest.denominator_cny)]]) {
          const item = document.createElement("div"), term = document.createElement("dt"), amount = document.createElement("dd");
          term.textContent = name; amount.textContent = value; item.append(term, amount); stats.append(item);
        }
        result.append(stats);
        result.append(summary);
      }
      let selectDay = () => {};
      function addTrend(parent, values, format, name, axisFormat = format) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 700 220");
        svg.setAttribute("role", "group");
        svg.setAttribute("aria-label", name + "，方向键选日期，加减键缩放");
        svg.style.width = "100%";
        svg.style.maxHeight = "280px";
        const readout = document.createElement("p");
        readout.setAttribute("aria-live", "polite");
        readout.textContent = "点击查看当日数据，滚轮缩放日期范围。";
        let left, right, chartWidth = 700, visibleFirst = 0, visibleLast = points.length - 1;
        function choose(index) {
          selectDay(points[index].trade_date);
          readout.textContent = points[index].trade_date + " · " + name + " " +
            (values[index] == null ? "缺失" : format(values[index]));
        }
        function render(first, last) {
          visibleFirst = first; visibleLast = last;
          svg.setAttribute("viewBox", `0 0 ${chartWidth} 220`);
          svg.replaceChildren();
          const visible = points.slice(first, last + 1);
          const active = values.slice(first, last + 1).filter(value => value != null);
          const min = active.length ? Math.min(...active) : 0;
          const max = active.length ? Math.max(...active) : 0;
          const xAt = chartAxes(svg, visible, active.length ? axisFormat(min) : "—",
            active.length ? axisFormat(max) : "—", min === max, chartWidth);
          left = xAt(0); right = xAt(last - first);
          const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
          svg.append(path);
          let d = "", connected = false;
          for (let i = first; i <= last; i++) {
            const value = values[i];
            if (value == null) { connected = false; continue; }
            const x = xAt(i - first), y = max === min ? 110 : 190 - (value - min) * 160 / (max - min);
            d += (connected ? " L" : " M") + x + " " + y;
            connected = true;
            const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
            circle.setAttribute("cx", x); circle.setAttribute("cy", y);
            circle.setAttribute("r", "3"); circle.setAttribute("fill", "#32664f");
            const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
            title.textContent = points[i].trade_date + " · " + format(value);
            circle.append(title);
            circle.setAttribute("role", "button");
            circle.setAttribute("tabindex", "-1");
            circle.setAttribute("aria-label", title.textContent);
            circle.addEventListener("click", event => { event?.stopPropagation(); selectIndex(i); });
            circle.addEventListener("keydown", event => {
              if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectIndex(i); }
            });
            svg.append(circle);
          }
          path.setAttribute("d", d); path.setAttribute("fill", "none"); path.setAttribute("stroke", "#32664f");
        }
        render(0, points.length - 1);
        const selectIndex = bindDateChart(svg, {length: points.length, fractionAt: x => (x - left) / (right - left || 1),
          render, select: choose});
        parent.append(svg, readout);
        if (window.ResizeObserver) {
          const observer = new window.ResizeObserver(() => {
            if (!svg.isConnected) { observer.disconnect(); return; }
            const width = Math.max(300, Math.min(700, Math.floor(parent.clientWidth)));
            if (width !== chartWidth) { chartWidth = width; render(visibleFirst, visibleLast); }
          });
          observer.observe(parent);
          disconnectTrend = () => observer.disconnect();
        }
      }
      const table = document.createElement("table"), head = document.createElement("tr");
      for (const text of ["交易日", "基金规模（元）", "流通市值（元）", "占比"]) { const th = document.createElement("th"); th.textContent = text; head.append(th); }
      table.append(head);
      for (const point of points) { const tr = document.createElement("tr"); const ratio = point.ratio_fraction == null || !Number.isFinite(Number(point.ratio_fraction)) ? "—" : `${(Number(point.ratio_fraction) * 100).toFixed(4)}%`; for (const value of [point.trade_date, money(point.numerator_cny), money(point.denominator_cny), ratio]) { const td = document.createElement("td"); td.textContent = value; tr.append(td); } table.append(tr); }
      const tableShell = document.createElement("div"); tableShell.className = "table-shell"; tableShell.style.overflowX = "auto"; tableShell.append(table);
      const history = document.createElement("details"), historyTitle = document.createElement("summary");
      history.className = "market-history"; historyTitle.textContent = `历史汇总 · ${points.length}个交易日`;
      history.append(historyTitle, tableShell); result.append(history);
      if (points.length && Array.isArray(payload.snapshot.spec.funds)) {
        const section = document.createElement("section"), label = document.createElement("label"), dateSelect = document.createElement("select"), details = document.createElement("div");
        section.className = "market-daily";
        label.textContent = "查看每日基金明细 ";
        for (const point of points) { const option = document.createElement("option"); option.value = point.trade_date; option.textContent = point.trade_date; dateSelect.append(option); }
        dateSelect.value = (valid.at(-1) || points.at(-1)).trade_date;
        const previous = document.createElement("button"), next = document.createElement("button");
        previous.type = next.type = "button"; previous.textContent = "上一交易日"; next.textContent = "下一交易日";
        label.append(dateSelect); section.append(label, previous, next, details); result.append(section);
        function explainMissing(reason) {
          if (reason === "index:float_market_cap") return "指数流通市值缺失";
          const match = /^(SSE|SZSE)\.([0-9]{6}):(observation|shares_or_unit_value)$/.exec(reason);
          if (match) return `${match[1]}.${match[2]}：${match[3] === "observation" ? "当日基金数据缺失" : `份额或${valueName}缺失`}`;
          return `${reason}（暂无中文说明）`;
        }
        function showDetails() {
          details.replaceChildren();
          const day = dateSelect.value, pointIndex = points.findIndex(p => p.trade_date === day), point = points[pointIndex];
          if (!point) return;
          previous.disabled = pointIndex === 0; next.disabled = pointIndex === points.length - 1;
          const daySummary = document.createElement("p");
          const fraction = point.ratio_fraction;
          const quality = Array.isArray(point.missing) ? (point.missing.length ? "缺失：" + point.missing.map(explainMissing).join("、") : "无已记录缺口") : "缺口详情未记录";
          daySummary.textContent = `${day}：整体占比 ${fraction != null && Number.isFinite(Number(fraction)) ? (Number(fraction) * 100).toFixed(4) + "%" : "—"}；${quality}`;
          details.append(daySummary);
          const description = document.createElement("p");
          description.textContent = `基金规模＝当日份额×${valueName}；单只占比＝该基金规模÷当日指数流通市值。缺少任一数据时显示 —。`;
          details.append(description);
          const detailTable = document.createElement("table"), heading = document.createElement("tr");
          for (const value of ["基金代码", "份额（份）", `${valueName}（元）`, "基金规模（元）", "单只占比"]) { const th = document.createElement("th"); th.textContent = value; heading.append(th); }
          detailTable.append(heading);
          const inputs = payload.snapshot.inputs || {};
          for (const fund of payload.snapshot.spec.funds) {
            const share = (inputs.shares || []).find(r => r.date === day && r.fund_id === fund)?.shares;
            const unit = numeratorMode === "close" ? (inputs.close || []).find(r => r.date === day && r.fund_id === fund)?.close : (inputs.nav || []).find(r => r.date === day && r.fund_id === fund)?.unit_nav;
            const amount = share != null && unit != null ? Number(share) * Number(unit) : NaN;
            const cap = Number(point.denominator_cny);
            const ratio = Number.isFinite(amount) && amount > 0 && point.denominator_cny != null && Number.isFinite(cap) && cap > 0 ? `${(amount / cap * 100).toFixed(4)}%` : "—";
            const row = document.createElement("tr");
            for (const value of [fundLabel(fund), groupedDecimal(share), groupedDecimal(unit), Number.isFinite(amount) && amount > 0 ? money(amount) : "—", ratio]) { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); }
            detailTable.append(row);
          }
          const detailShell = document.createElement("div"); detailShell.className = "table-shell"; detailShell.style.overflowX = "auto"; detailShell.append(detailTable); details.append(detailShell);
        }
        selectDay = day => { dateSelect.value = day; showDetails(); };
        previous.addEventListener("click", () => { const i = points.findIndex(p => p.trade_date === dateSelect.value); if (i > 0) selectDay(points[i - 1].trade_date); });
        next.addEventListener("click", () => { const i = points.findIndex(p => p.trade_date === dateSelect.value); if (i >= 0 && i < points.length - 1) selectDay(points[i + 1].trade_date); });
        dateSelect.addEventListener("change", showDetails); showDetails();
      }
      if (points.length && Array.isArray(payload.snapshot.spec.funds)) {
        const section = document.createElement("section"), label = document.createElement("label"), metric = document.createElement("select"), graph = document.createElement("div");
        section.className = "market-trends";
        label.textContent = "图表指标 ";
        const options = [["ratio", "基金规模占比（%）"], ["numerator", "基金组合规模（元）"], ["denominator", "指数流通市值（元）"], ...payload.snapshot.spec.funds.map(fund => [fund, `${fund} 单只占比（%）`])];
        for (const [value, name] of options) { const option = document.createElement("option"); option.value = value; option.textContent = name; metric.append(option); }
        metric.value = valid.length ? "ratio" : "numerator"; label.append(metric); section.append(label, graph); result.append(section);
        const inputs = payload.snapshot.inputs || {};
        const shares = new Map((inputs.shares || []).map(row => [`${row.date}|${row.fund_id}`, row.shares]));
        const units = new Map(((numeratorMode === "close" ? inputs.close : inputs.nav) || []).map(row => [`${row.date}|${row.fund_id}`, numeratorMode === "close" ? row.close : row.unit_nav]));
        const number = value => value == null || value === "" || !Number.isFinite(Number(value)) ? null : Number(value);
        function drawMetric() {
          disconnectTrend();
          graph.replaceChildren();
          const selected = metric.value, name = options.find(option => option[0] === selected)?.[1];
          if (!name) return;
          const values = points.map(point => {
            if (selected === "ratio") { const value = number(point.ratio_fraction); return value == null ? null : value * 100; }
            if (selected === "numerator") return number(point.numerator_cny);
            if (selected === "denominator") return number(point.denominator_cny);
            const key = `${point.trade_date}|${selected}`, share = number(shares.get(key)), unit = number(units.get(key)), cap = number(point.denominator_cny);
            return share != null && unit != null && cap != null && share > 0 && unit > 0 && cap > 0 ? share * unit / cap * 100 : null;
          });
          const validValues = values.filter(value => value != null && Number.isFinite(value));
          if (!validValues.length) { const message = document.createElement("p"); message.textContent = `${name}：所选范围没有可计算的数据。`; graph.append(message); return; }
          const format = selected === "numerator" || selected === "denominator" ? money : value => value.toFixed(2) + "%";
          const magnitude = Math.max(...validValues.map(value => Math.abs(value)));
          const unit = magnitude >= 1e12 ? [1e12, "万亿元"] : magnitude >= 1e8 ? [1e8, "亿元"] : magnitude >= 1e4 ? [1e4, "万元"] : [1, ""];
          const axisFormat = selected === "numerator" || selected === "denominator" ?
            value => money(value / unit[0]) + unit[1] : format;
          addTrend(graph, values, format, name, axisFormat);
        }
        metric.addEventListener("change", drawMetric); drawMetric();
      }
    } catch (error) { if (id === request) status.textContent = `读取 ${indexCode} 失败：${error.message}${displayedIndex ? `；保留 ${displayedIndex} 的已有数据。` : ""}`; }
  }
  const form = document.getElementById("index-sync"), taskStatus = document.getElementById("index-task");
  const resume = document.getElementById("index-resume");
  let busy = false, pendingTask = null;
  const taskKey = "index-fund-pending-task";
  function saveTask(task) {
    pendingTask = task;
    try {
      if (task) window.sessionStorage?.setItem(taskKey, JSON.stringify(task));
      else window.sessionStorage?.removeItem(taskKey);
    } catch { /* Tracking still works when browser storage is unavailable. */ }
  }
  function setBusy(value) {
    busy = value;
    const locked = value || pendingTask !== null;
    button.disabled = value; select.disabled = locked; modeSelect.disabled = locked;
    resume.disabled = value;
    button.textContent = pendingTask ? "继续查询同步进度" : "同步所选基金";
    fundInput.disabled = locked;
    document.getElementById("index-start").disabled = locked;
    document.getElementById("index-end").disabled = locked;
    updateExampleButtons();
    if (locked) {
      addExamples.disabled = clearFunds.disabled = true;
      for (const control of [...exampleList.children, ...selectedList.children]) control.disabled = true;
    }
  }
  async function followTask(task) {
    setBusy(true);
    resume.hidden = true;
    try {
      let current = task;
      while (["queued", "running"].includes(current.status)) {
        taskStatus.textContent = `指数 ${task.index_code} · ${current.message || "正在查询任务"}（${current.progress || 0}%）`;
        const response = await fetch(`/api/v1/index-funds/tasks/${encodeURIComponent(task.task_id)}`);
        if (response.status === 404) {
          saveTask(null);
          taskStatus.textContent = "原同步任务已不存在，请确认上方参数后重新同步。";
          return;
        }
        if (!response.ok) throw new Error("HTTP " + response.status);
        current = await response.json();
        if (["queued", "running"].includes(current.status)) await new Promise(resolve => setTimeout(resolve, 2000));
      }
      saveTask(null);
      taskStatus.textContent = current.status === "succeeded" ? "同步完成，请在“走势与明细”查看基金组及数据覆盖。" : `同步未完成：${current.error || current.message || current.status}`;
      if (current.status === "succeeded") await load();
    } catch (error) {
      taskStatus.textContent = `任务 ${task.task_id} 状态查询中断：${error.message}。后台可能仍在运行，请点击继续查询。`;
      resume.hidden = false;
    } finally { setBusy(false); }
  }
  resume.addEventListener("click", () => { if (!busy && pendingTask) followTask(pendingTask); });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy) return;
    if (pendingTask) { await followTask(pendingTask); return; }
    const startInput = document.getElementById("index-start"), endInput = document.getElementById("index-end");
    const start = startInput.value, end = endInput.value, numerator_mode = modeSelect.value, funds = fundCodes();
    if (!start || !end || start > end || (Date.parse(end) - Date.parse(start)) / 86400000 > 1830) {
      taskStatus.textContent = "请填写起止日期，开始日期须早于或等于结束日期，范围最多1831个自然日。";
      startInput.focus(); return;
    }
    if (!funds.length || funds.length > 10 || funds.some(code => !/^(SSE|SZSE)\.[0-9]{6}$/.test(code))) {
      taskStatus.textContent = "请选择1至10只基金，或填写交易所和六位代码，例如 SSE.510300。";
      fundInput.focus(); return;
    }
    const mismatched = funds.filter(code => Object.entries(examples).some(([index, group]) =>
      index !== select.value && group.some(([fund]) => fund === code)));
    if (mismatched.length) {
      taskStatus.textContent = "以下示例基金跟踪其他指数，请移除或切换指数：" + mismatched.map(fundLabel).join("、");
      fundInput.focus(); return;
    }
    fundInput.value = funds.join(",");
    updateExampleButtons();
    const indexName = select.selectedOptions?.[0]?.textContent || select.value;
    if (!window.confirm(`确认联网同步 ${indexName}，${start} 至 ${end}，共${funds.length}只基金：${funds.map(fundLabel).join("、")}，口径 ${numerator_mode === "close" ? "不复权收盘价（来源尚待核验）" : "单位净值"}？`)) return;
    setBusy(true);
    taskStatus.textContent = "正在提交同步任务…";
    try {
      const response = await fetch("/api/v1/index-funds/sync", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({index_code:select.value,start,end,funds,confirm:true,numerator_mode})});
      const task = await response.json();
      if (!response.ok) throw new Error(typeof task.detail === "string" ? task.detail : "参数错误，请检查日期和基金代码");
      saveTask({task_id: task.task_id, status: "queued", index_code: select.value, numerator_mode, start, end, funds});
      await followTask(pendingTask);
    } catch(error) {
      taskStatus.textContent = `提交未确认：${error.message}。可用相同参数重试，后台会复用同一活动任务。`;
    } finally { setBusy(false); }
  });
  try {
    const stored = JSON.parse(window.sessionStorage?.getItem(taskKey) || "null");
    if (stored && typeof stored.task_id === "string" && examples[stored.index_code] &&
        ["nav", "close"].includes(stored.numerator_mode)) {
      pendingTask = stored;
      select.value = stored.index_code;
      modeSelect.value = stored.numerator_mode;
      editingIndex = select.value;
      if (typeof stored.start === "string") document.getElementById("index-start").value = stored.start;
      if (typeof stored.end === "string") document.getElementById("index-end").value = stored.end;
      if (Array.isArray(stored.funds) && stored.funds.every(code => typeof code === "string")) {
        fundInput.value = stored.funds.join(",");
      }
      showExamples();
      followTask(stored);
    }
  } catch { /* Ignore unusable saved state. */ }
  select.addEventListener("change", () => {
    drafts.set(editingIndex, fundInput.value);
    editingIndex = select.value;
    fundInput.value = drafts.get(editingIndex) || "";
    showExamples();
    load();
  });
  modeSelect.addEventListener("change", load);
  document.getElementById("index-range").addEventListener("change", () => { if (document.getElementById("index-range").value !== "custom") load(); });
  document.getElementById("view-apply").addEventListener("click", load); load();
})();
