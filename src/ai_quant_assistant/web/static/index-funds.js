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
      result.replaceChildren();
      displayedIndex = null;
      if (!payload.snapshot) { status.textContent = "本地暂无数据；需要先准备基金和指数流通市值数据。"; return; }
      displayedIndex = `${indexCode}（${requestedMode === "close" ? "收盘价" : "单位净值"}）`;
      const numeratorMode = payload.snapshot.spec.numerator_mode || "nav";
      const valueName = numeratorMode === "close" ? "不复权收盘价" : "单位净值";
      const modeLabel = document.createElement("p"); modeLabel.textContent = `当前快照口径：份额 × ${valueName}${numeratorMode === "close" ? "（价格来源尚待真实历史核验）" : ""}。`; result.append(modeLabel);
      const warning = document.createElement("p"); warning.textContent = payload.snapshot.result.warning || ""; result.append(warning);
      const download = document.createElement("a"); download.textContent = "导出当前快照 CSV";
      download.href = `/api/v1/index-fund-snapshots/${encodeURIComponent(payload.snapshot.snapshot_id)}/csv`;
      download.setAttribute("download", ""); result.append(download);
      const htmlDownload = document.createElement("a"); htmlDownload.textContent = " 导出离线 HTML";
      htmlDownload.href = `/api/v1/index-fund-snapshots/${encodeURIComponent(payload.snapshot.snapshot_id)}/html`;
      htmlDownload.setAttribute("download", ""); result.append(htmlDownload);
      const points = payload.snapshot.result.points || [];
      status.textContent = `快照 ${payload.snapshot.snapshot_id}；数据缺口显示为 —。`;
      const valid = points.filter(p => p.ratio_fraction !== null && Number.isFinite(Number(p.ratio_fraction)));
      const coverage = document.createElement("p");
      coverage.textContent = `可计算 ${valid.length} / ${points.length} 个交易日；其余日期保留缺口。`;
      result.append(coverage);
      if (valid.length) {
        const latest = valid[valid.length - 1], latestIndex = points.indexOf(latest), previous = valid[valid.length - 2];
        const summary = document.createElement("p");
        let change = "暂无上一完整观测日";
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
          circle.addEventListener("click", () => selectDay(p.trade_date));
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
        function showDetails() {
          details.replaceChildren();
          const day = dateSelect.value, pointIndex = points.findIndex(p => p.trade_date === day), point = points[pointIndex];
          if (!point) return;
          previous.disabled = pointIndex === 0; next.disabled = pointIndex === points.length - 1;
          const daySummary = document.createElement("p");
          const fraction = point.ratio_fraction;
          const quality = Array.isArray(point.missing) ? (point.missing.length ? "缺失：" + point.missing.join("、") : "无已记录缺口") : "缺口详情未记录";
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
  select.addEventListener("change", load); modeSelect.addEventListener("change", load); load();
})();
