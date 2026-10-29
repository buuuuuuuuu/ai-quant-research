(() => {
  const select = document.getElementById("index-code"), status = document.getElementById("index-status"), result = document.getElementById("index-result");
  let request = 0;
  async function load() {
    const id = ++request;
    status.textContent = "正在读取本地数据…";
    result.replaceChildren();
    try {
      const response = await fetch(`/api/v1/index-funds/${encodeURIComponent(select.value)}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (id !== request) return;
      if (!payload.snapshot) { status.textContent = "本地暂无数据；需要先准备基金和指数流通市值数据。"; return; }
      const warning = document.createElement("p"); warning.textContent = payload.snapshot.result.warning || ""; result.append(warning);
      const points = payload.snapshot.result.points || [];
      status.textContent = `快照 ${payload.snapshot.snapshot_id}；数据缺口显示为 —。`;
      const valid = points.filter(p => p.ratio_fraction !== null && Number.isFinite(Number(p.ratio_fraction)));
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
        for (const [x, y, text] of [[4, 30, max.toFixed(2) + "%"], [4, 194, min.toFixed(2) + "%"], [40, 216, points[0].trade_date], [570, 216, points[points.length - 1].trade_date]]) { const label = document.createElementNS("http://www.w3.org/2000/svg", "text"); label.setAttribute("x", x); label.setAttribute("y", y); label.setAttribute("font-size", "12"); label.textContent = text; svg.append(label); }
        result.append(svg);
      }
      const table = document.createElement("table"), head = document.createElement("tr");
      for (const text of ["交易日", "基金规模（元）", "流通市值（元）", "占比"]) { const th = document.createElement("th"); th.textContent = text; head.append(th); }
      table.append(head);
      for (const point of points) { const tr = document.createElement("tr"); const ratio = point.ratio_fraction === null ? "—" : `${(Number(point.ratio_fraction) * 100).toFixed(4)}%`; for (const value of [point.trade_date, point.numerator_cny ?? "—", point.denominator_cny ?? "—", ratio]) { const td = document.createElement("td"); td.textContent = value; tr.append(td); } table.append(tr); }
      result.append(table);
    } catch (error) { if (id === request) status.textContent = `读取失败：${error.message}`; }
  }
  const form = document.getElementById("index-sync"), taskStatus = document.getElementById("index-task"), button = document.getElementById("index-submit");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const start = document.getElementById("index-start").value, end = document.getElementById("index-end").value;
    const funds = document.getElementById("index-funds").value.split(/[,，\s]+/).filter(Boolean);
    if (!window.confirm(`确认联网同步 ${select.value}，${start} 至 ${end}，基金 ${funds.join("、")}？`)) return;
    button.disabled = true; taskStatus.textContent = "正在提交同步任务…";
    try {
      const response = await fetch("/api/v1/index-funds/sync", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({index_code:select.value,start,end,funds,confirm:true})});
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
    finally { button.disabled = false; }
  });
  select.addEventListener("change", load); load();
})();
