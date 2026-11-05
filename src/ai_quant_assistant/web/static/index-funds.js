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
  function chartAxes(svg, points, low, high, flat) {
    const left = 18 + Math.max(low.length, high.length) * 8, right = 686;
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

  // Product examples, independent of historical eligibility or size rankings.
  // https://www.sse.com.cn/market/funddata/volumn/etfvolumn/
  // https://www.jsfund.cn/Services/cn/jsp/pcf/index.jsp?SiteID=1&fundcode=159919_1
  // https://www.sse.com.cn/disclosure/fund/announcement/c/new/2025-04-28/588080_20250428_99XH.pdf
  const examples = {
    "000300": [["SSE.510300", "华泰柏瑞"], ["SZSE.159919", "嘉实"], ["SSE.510310", "易方达"], ["SSE.510330", "华夏"]],
    // SSE fund report 510500_20240330_LM9Y; SZSE listing notice 2024-10-10 (159351).
    "000852": [["SSE.512100", "南方"]],
    "932000": [["SSE.563300", "华泰柏瑞"]],
    "399006": [["SZSE.159915", "易方达"]],
    "000905": [["SSE.510500", "南方"]],
    "000510": [["SZSE.159351", "嘉实"]],
    "000016": [["SSE.510050", "华夏"], ["SSE.510100", "易方达"]],
    "000688": [["SSE.588000", "华夏"], ["SSE.588080", "易方达"]],
  };
  const fundInput = document.getElementById("index-funds");
  const exampleList = document.getElementById("index-examples");
  const drafts = new Map();
  let editingIndex = select.value;
  const fundCodes = () => fundInput.value.split(/[,，\s]+/).filter(Boolean);
  function updateExampleButtons() {
    const codes = fundCodes();
    for (const button of exampleList.children) {
      button.disabled = codes.includes(button.value) || codes.length >= 10;
    }
  }
  const hasCapSource = () => ["000300", "000016", "000688"].includes(select.value);
  const button = document.getElementById("index-submit");
  const syncHint = document.getElementById("index-sync-hint");
  function showExamples() {
    syncHint.hidden = syncHint.dataset?.lixinger !== "1" && hasCapSource();
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
        if (codes.length < 10 && !codes.includes(code)) fundInput.value = [...codes, code].join(",");
        updateExampleButtons();
      });
      exampleList.append(button);
    }
    updateExampleButtons();
  }
  fundInput.addEventListener("input", updateExampleButtons);
  showExamples();
  let request = 0, displayedIndex = null;
  async function load() {
    const id = ++request, indexCode = select.value, requestedMode = modeSelect.value;
    status.textContent = displayedIndex ? `正在读取 ${indexCode}；暂时保留 ${displayedIndex} 的数据。` : "正在读取本地数据…";
    try {
      const response = await fetch(`/api/v1/index-funds/${encodeURIComponent(indexCode)}?numerator_mode=${encodeURIComponent(requestedMode)}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (id !== request) return;
      if (!payload.snapshot) { result.replaceChildren(); displayedIndex = null; status.textContent = "本地暂无数据；需要先准备基金和指数流通市值数据。"; return; }
      const allPoints = payload.snapshot.result.points || [];
      const range = document.getElementById("index-range").value;
      let points = allPoints;
      if (range === "custom") {
        const start = document.getElementById("view-start").value, end = document.getElementById("view-end").value;
        if (!start || !end || start > end) throw new Error("请填写有效的自定义开始和结束日期");
        points = allPoints.filter(p => p.trade_date >= start && p.trade_date <= end);
      } else if (["1", "3", "5"].includes(range) && allPoints.length) {
        const end = allPoints[allPoints.length - 1].trade_date;
        const cutoff = new Date(end + "T00:00:00Z"); cutoff.setUTCFullYear(cutoff.getUTCFullYear() - Number(range));
        const start = cutoff.toISOString().slice(0, 10);
        points = allPoints.filter(p => p.trade_date > start && p.trade_date <= end);
      }
      result.replaceChildren();
      displayedIndex = null;
      displayedIndex = `${indexCode}（${requestedMode === "close" ? "收盘价" : "单位净值"}）`;
      const numeratorMode = payload.snapshot.spec.numerator_mode || "nav";
      const valueName = numeratorMode === "close" ? "不复权收盘价" : "单位净值";
      const modeLabel = document.createElement("p"); modeLabel.textContent = `当前快照口径：份额 × ${valueName}${numeratorMode === "close" ? "（价格来源尚待真实历史核验）" : ""}。`; result.append(modeLabel);
      const warning = document.createElement("p"); warning.textContent = payload.snapshot.result.warning || ""; result.append(warning);
      const download = document.createElement("a"); download.textContent = "导出完整快照 CSV";
      download.href = `/api/v1/index-fund-snapshots/${encodeURIComponent(payload.snapshot.snapshot_id)}/csv`;
      download.setAttribute("download", ""); result.append(download);
      const htmlDownload = document.createElement("a"); htmlDownload.textContent = " 导出完整快照离线 HTML";
      htmlDownload.href = `/api/v1/index-fund-snapshots/${encodeURIComponent(payload.snapshot.snapshot_id)}/html`;
      htmlDownload.setAttribute("download", ""); result.append(htmlDownload);
      status.textContent = `快照 ${payload.snapshot.snapshot_id}；数据缺口显示为 —。`;
      const valid = points.filter(p => p.ratio_fraction !== null && Number.isFinite(Number(p.ratio_fraction)));
      const coverage = document.createElement("p");
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
        summary.textContent = `最新完整统计日 ${latest.trade_date}：${(Number(latest.ratio_fraction) * 100).toFixed(4)}%；${change}。基金规模 ${money(latest.numerator_cny)} 元；流通市值 ${money(latest.denominator_cny)} 元。`;
        result.append(summary);
      }
      let selectDay = () => {};
      function addTrend(parent, values, format, name) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 700 220");
        svg.setAttribute("role", "group");
        svg.setAttribute("aria-label", name + "，方向键选日期，加减键缩放");
        svg.style.width = "100%";
        svg.style.maxHeight = "280px";
        const readout = document.createElement("p");
        readout.setAttribute("aria-live", "polite");
        readout.textContent = "点击查看当日数据，滚轮缩放日期范围。";
        let left, right;
        function choose(index) {
          selectDay(points[index].trade_date);
          readout.textContent = points[index].trade_date + " · " + name + " " +
            (values[index] == null ? "缺失" : format(values[index]));
        }
        function render(first, last) {
          svg.replaceChildren();
          const visible = points.slice(first, last + 1);
          const active = values.slice(first, last + 1).filter(value => value != null);
          const min = active.length ? Math.min(...active) : 0;
          const max = active.length ? Math.max(...active) : 0;
          const xAt = chartAxes(svg, visible, active.length ? format(min) : "—",
            active.length ? format(max) : "—", min === max);
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
      }
      if (valid.length) {
        addTrend(result, points.map(p => p.ratio_fraction == null || !Number.isFinite(Number(p.ratio_fraction))
          ? null : Number(p.ratio_fraction) * 100), value => value.toFixed(2) + "%", "基金规模占比");
      }
      const table = document.createElement("table"), head = document.createElement("tr");
      for (const text of ["交易日", "基金规模（元）", "流通市值（元）", "占比"]) { const th = document.createElement("th"); th.textContent = text; head.append(th); }
      table.append(head);
      for (const point of points) { const tr = document.createElement("tr"); const ratio = point.ratio_fraction === null ? "—" : `${(Number(point.ratio_fraction) * 100).toFixed(4)}%`; for (const value of [point.trade_date, money(point.numerator_cny), money(point.denominator_cny), ratio]) { const td = document.createElement("td"); td.textContent = value; tr.append(td); } table.append(tr); }
      const tableShell = document.createElement("div"); tableShell.className = "table-shell"; tableShell.style.overflowX = "auto"; tableShell.append(table); result.append(tableShell);
      if (points.length && Array.isArray(payload.snapshot.spec.funds)) {
        const section = document.createElement("section"), label = document.createElement("label"), dateSelect = document.createElement("select"), details = document.createElement("div");
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
            for (const value of [fund, groupedDecimal(share), groupedDecimal(unit), Number.isFinite(amount) && amount > 0 ? money(amount) : "—", ratio]) { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); }
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
        label.textContent = "查看其他趋势 ";
        const options = [["numerator", "基金组合规模（元）"], ["denominator", "指数流通市值（元）"], ...payload.snapshot.spec.funds.map(fund => [fund, `${fund} 单只占比（%）`])];
        for (const [value, name] of options) { const option = document.createElement("option"); option.value = value; option.textContent = name; metric.append(option); }
        metric.value = "numerator"; label.append(metric); section.append(label, graph); result.append(section);
        const inputs = payload.snapshot.inputs || {};
        const shares = new Map((inputs.shares || []).map(row => [`${row.date}|${row.fund_id}`, row.shares]));
        const units = new Map(((numeratorMode === "close" ? inputs.close : inputs.nav) || []).map(row => [`${row.date}|${row.fund_id}`, numeratorMode === "close" ? row.close : row.unit_nav]));
        const number = value => value == null || value === "" || !Number.isFinite(Number(value)) ? null : Number(value);
        function drawMetric() {
          graph.replaceChildren();
          const selected = metric.value, name = options.find(option => option[0] === selected)?.[1];
          if (!name) return;
          const values = points.map(point => {
            if (selected === "numerator") return number(point.numerator_cny);
            if (selected === "denominator") return number(point.denominator_cny);
            const key = `${point.trade_date}|${selected}`, share = number(shares.get(key)), unit = number(units.get(key)), cap = number(point.denominator_cny);
            return share != null && unit != null && cap != null && share > 0 && unit > 0 && cap > 0 ? share * unit / cap * 100 : null;
          });
          const validValues = values.filter(value => value != null && Number.isFinite(value));
          if (!validValues.length) { const message = document.createElement("p"); message.textContent = `${name}：所选范围没有可计算的数据。`; graph.append(message); return; }
          const format = selected === "numerator" || selected === "denominator" ? money : value => value.toFixed(2);
          addTrend(graph, values, format, name);
        }
        metric.addEventListener("change", drawMetric); drawMetric();
      }
    } catch (error) { if (id === request) status.textContent = `读取 ${indexCode} 失败：${error.message}${displayedIndex ? `；保留 ${displayedIndex} 的已有数据。` : ""}`; }
  }
  const form = document.getElementById("index-sync"), taskStatus = document.getElementById("index-task");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const start = document.getElementById("index-start").value, end = document.getElementById("index-end").value;
    const numerator_mode = modeSelect.value;
    const funds = fundCodes();
    if (!window.confirm(`确认联网同步 ${select.value}，${start} 至 ${end}，基金 ${funds.join("、")}，口径 ${numerator_mode === "close" ? "不复权收盘价（来源尚待核验）" : "单位净值"}？`)) return;
    button.disabled = true; select.disabled = true; modeSelect.disabled = true; taskStatus.textContent = "正在提交同步任务…";
    try {
      const response = await fetch("/api/v1/index-funds/sync", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({index_code:select.value,start,end,funds,confirm:true,numerator_mode})});
      const task = await response.json(); if (!response.ok) throw new Error(typeof task.detail === "string" ? task.detail : "参数错误，请检查日期和基金代码");
      let current = task;
      while (["queued", "running"].includes(current.status)) {
        taskStatus.textContent = `${current.message}（${current.progress}%）`;
        await new Promise(resolve => setTimeout(resolve, 2000));
        const next = await fetch(`/api/v1/index-funds/tasks/${encodeURIComponent(task.task_id)}`);
        if (!next.ok) throw new Error("任务状态查询失败"); current = await next.json();
      }
      taskStatus.textContent = current.status === "succeeded" ? "同步完成，正在读取快照。" : `同步未完成：${current.error || current.message}`;
      if (current.status === "succeeded") await load();
    } catch(error) { taskStatus.textContent = `同步失败：${error.message}`; }
    finally { button.disabled = false; select.disabled = false; modeSelect.disabled = false; }
  });
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
