(() => {
  const select = document.getElementById("index-code"), modeSelect = document.getElementById("index-numerator"), status = document.getElementById("index-status"), result = document.getElementById("index-result");
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
        summary.textContent = `最新完整统计日 ${latest.trade_date}：${(Number(latest.ratio_fraction) * 100).toFixed(4)}%；${change}。基金规模 ${latest.numerator_cny ?? "—"} 元；流通市值 ${latest.denominator_cny ?? "—"} 元。`;
        result.append(summary);
      }
      let selectDay = () => {};
      if (valid.length) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 700 220"); svg.setAttribute("role", "img");
        svg.setAttribute("aria-label", "基金规模占比趋势，数据缺口处断开；完整数值见下方表格");
        svg.style.width = "100%"; svg.style.maxHeight = "280px";
        const values = valid.map(p => Number(p.ratio_fraction) * 100);
        const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
        let d = "", connected = false;
        points.forEach((p, i) => { if (p.ratio_fraction === null || !Number.isFinite(Number(p.ratio_fraction))) { connected = false; return; }
          const x = 40 + i * 620 / Math.max(points.length - 1, 1), y = 190 - (Number(p.ratio_fraction) * 100 - min) * 160 / span;
          d += `${connected ? " L" : " M"}${x} ${y}`; connected = true;
        });
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", d); path.setAttribute("fill", "none"); path.setAttribute("stroke", "#32664f"); path.setAttribute("stroke-width", "2"); svg.append(path);
        points.forEach((p, i) => { if (p.ratio_fraction === null || !Number.isFinite(Number(p.ratio_fraction))) return;
          const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
          circle.setAttribute("cx", 40 + i * 620 / Math.max(points.length - 1, 1)); circle.setAttribute("cy", 190 - (Number(p.ratio_fraction) * 100 - min) * 160 / span);
          circle.setAttribute("r", "3"); circle.setAttribute("fill", "#32664f");
          const title = document.createElementNS("http://www.w3.org/2000/svg", "title"); title.textContent = `${p.trade_date}：${(Number(p.ratio_fraction) * 100).toFixed(4)}%；基金规模 ${p.numerator_cny ?? "—"} 元；流通市值 ${p.denominator_cny ?? "—"} 元。点击查看明细。`; circle.append(title); svg.append(circle);
          circle.style.cursor = "pointer";
          circle.setAttribute("tabindex", "0"); circle.setAttribute("role", "button");
          circle.setAttribute("aria-label", `${p.trade_date}：${(Number(p.ratio_fraction) * 100).toFixed(4)}%，查看明细`);
          circle.addEventListener("click", () => selectDay(p.trade_date));
          circle.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectDay(p.trade_date); } });
        });
        for (const [x, y, text] of [[4, 30, max.toFixed(2) + "%"], [4, 194, min.toFixed(2) + "%"], [40, 216, points[0].trade_date], [570, 216, points[points.length - 1].trade_date]]) { const label = document.createElementNS("http://www.w3.org/2000/svg", "text"); label.setAttribute("x", x); label.setAttribute("y", y); label.setAttribute("font-size", "12"); label.textContent = text; svg.append(label); }
        result.append(svg);
      }
      const table = document.createElement("table"), head = document.createElement("tr");
      for (const text of ["交易日", "基金规模（元）", "流通市值（元）", "占比"]) { const th = document.createElement("th"); th.textContent = text; head.append(th); }
      table.append(head);
      for (const point of points) { const tr = document.createElement("tr"); const ratio = point.ratio_fraction === null ? "—" : `${(Number(point.ratio_fraction) * 100).toFixed(4)}%`; for (const value of [point.trade_date, point.numerator_cny ?? "—", point.denominator_cny ?? "—", ratio]) { const td = document.createElement("td"); td.textContent = value; tr.append(td); } table.append(tr); }
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
            for (const value of [fund, share ?? "—", unit ?? "—", Number.isFinite(amount) && amount > 0 ? amount.toFixed(2) : "—", ratio]) { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); }
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
          const min = Math.min(...validValues), max = Math.max(...validValues), span = max - min || 1;
          const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
          svg.setAttribute("viewBox", "0 0 700 220"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", `${name}趋势，缺口处断开`); svg.style.width = "100%"; svg.style.maxHeight = "280px";
          let d = "", connected = false;
          values.forEach((value, i) => { if (value == null || !Number.isFinite(value)) { connected = false; return; } const x = 40 + i * 620 / Math.max(points.length - 1, 1), y = 190 - (value - min) * 160 / span; d += `${connected ? " L" : " M"}${x} ${y}`; connected = true; });
          const path = document.createElementNS("http://www.w3.org/2000/svg", "path"); path.setAttribute("d", d); path.setAttribute("fill", "none"); path.setAttribute("stroke", "#32664f"); path.setAttribute("stroke-width", "2"); svg.append(path);
          for (const [x, y, text] of [[4, 30, max.toFixed(2)], [4, 194, min.toFixed(2)], [40, 216, points[0].trade_date], [570, 216, points[points.length - 1].trade_date]]) { const item = document.createElementNS("http://www.w3.org/2000/svg", "text"); item.setAttribute("x", x); item.setAttribute("y", y); item.setAttribute("font-size", "12"); item.textContent = text; svg.append(item); }
          graph.append(svg);
        }
        metric.addEventListener("change", drawMetric); drawMetric();
      }
    } catch (error) { if (id === request) status.textContent = `读取 ${indexCode} 失败：${error.message}${displayedIndex ? `；保留 ${displayedIndex} 的已有数据。` : ""}`; }
  }
  const form = document.getElementById("index-sync"), taskStatus = document.getElementById("index-task"), button = document.getElementById("index-submit");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const start = document.getElementById("index-start").value, end = document.getElementById("index-end").value;
    const numerator_mode = modeSelect.value;
    const funds = document.getElementById("index-funds").value.split(/[,，\s]+/).filter(Boolean);
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
  select.addEventListener("change", load); modeSelect.addEventListener("change", load);
  document.getElementById("index-range").addEventListener("change", () => { if (document.getElementById("index-range").value !== "custom") load(); });
  document.getElementById("view-apply").addEventListener("click", load); load();
})();
