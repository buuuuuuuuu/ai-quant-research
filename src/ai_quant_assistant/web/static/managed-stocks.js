// Local data enrollment is explicit; reading/searching never starts a download.
const managedState = {busy: false, page: 1, generation: 0, timer: null, items: []};
const managedLabels = {ready: "可用", partial: "部分完成", unavailable: "待补齐", active: "纳入管理", archived: "已移出", queued: "等待同步", running: "同步中", succeeded: "同步完成", failed: "同步失败"};
const managedToday = () => new Intl.DateTimeFormat("en-CA", {timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());

function managedMessage(text, error = false) {
  const node = $("#managed-message");
  node.textContent = text;
  node.classList.toggle("negative", error);
}

async function managedMutation(url, body, method = "POST") {
  if (managedState.busy) return;
  managedState.busy = true;
  $("#managed-add").disabled = true;
  try {
    const result = await requestJson(url, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    managedMessage(result.task_id ? `任务 ${result.task_id}：${managedLabels[result.status] || result.status}。可在此页或运维任务中查看。` : "管理状态已更新；已有行情与历史研究保留。");
    await loadManagedStocks(managedState.page);
    return result;
  } catch (error) {
    managedMessage(error.status === 409 ? `状态冲突：${error.message}。请刷新核对后重试，未自动覆盖。` : error.message, true);
  } finally {
    managedState.busy = false;
    $("#managed-add").disabled = false;
  }
}

async function addManagedStock(event) {
  event.preventDefault();
  if (managedState.busy) return;
  const symbol = $("#managed-symbol").value.trim(), start = $("#managed-start").value, end = $("#managed-end").value;
  if (!/^[0-9]{6}$/.test(symbol) || !start || !end || start > end) {
    managedMessage("请输入六位代码和有效起止日期。", true); return;
  }
  if (!window.confirm(`确认添加 ${symbol} 并访问公网回填 ${start} 至 ${end} 的不复权/前复权日线？不下载公告、财务或 PDF。`)) return;
  await managedMutation("/api/v1/data/managed-stocks", {symbol, start, end, confirm: true, auto_update: $("#managed-auto").checked});
}

async function managedAction(asset, action) {
  if (action === "references") {
    try {
      const pools = await requestJson(`/api/v1/data/managed-stocks/${encodeURIComponent(asset.security_id)}/references`);
      managedMessage(pools.length ? `关联股票池：${pools.map((pool) => `${pool.name}（${pool.pool_id}，revision ${pool.revision}）`).join("；")}` : "没有活动股票池引用。历史引用仍保留。");
    } catch (error) {managedMessage(error.message, true);}
    return;
  }
  if (action === "prices") {
    if (state.explorerDataset !== "prices") await openExplorer("prices");
    await selectExplorerEntity({symbol: asset.symbol});
    return;
  }
  if (action === "sync") {
    const end = managedToday();
    if (!window.confirm(`确认更新 ${asset.symbol}，补齐 ${asset.target_start} 至 ${end} 的行情？此操作会访问公网。`)) return;
    return managedMutation("/api/v1/data/managed-stocks", {symbol: asset.symbol, start: asset.target_start, end, expected_revision: asset.revision, confirm: true, refresh: true, auto_update: asset.auto_update});
  }
  const text = action === "archive" ? "确认移出管理并停止自动更新？保留历史数据；存在活动股票池引用时将拒绝操作。" : action === "restore" ? "确认恢复管理？不会自动下载或开启定时更新；需要补齐时请单独确认。" : `确认${asset.auto_update ? "关闭" : "开启"}日常自动行情更新？`;
  if (!window.confirm(text)) return;
  return managedMutation(`/api/v1/data/managed-stocks/${encodeURIComponent(asset.security_id)}`, {action, expected_revision: asset.revision, confirm: true, ...(action === "configure" ? {auto_update: !asset.auto_update} : {})}, "PATCH");
}

function managedRows(items) {
  return items.map((asset) => {
    const bars = asset.coverage.adjustments || {}, task = asset.task;
    const coverage = Object.entries(bars).map(([adjustment, row]) => `${adjustment}: ${row.start_date || "—"} → ${row.end_date || "—"} · ${row.rows} 行`).join("；");
    const issues = [...(asset.coverage.issues || []), ...(task?.error ? [task.error] : [])];
    const actions = asset.status === "archived" ? ["restore", "prices", "references"] : ["sync", "configure", "archive", "prices", "references"];
    const labels = {restore: "恢复管理", sync: "补齐 / 更新 / 重试", configure: asset.auto_update ? "停止自动更新" : "开启自动更新", archive: "移出管理", prices: "查看明细", references: "关联股票池"};
    const progress = task && task.status !== "failed" ? ` ${escapeHtml(task.progress)}%` : "";
    return `<tr data-managed-id="${escapeHtml(asset.security_id)}"><td>${escapeHtml(asset.symbol || asset.security_id)} · ${escapeHtml(asset.name || "目录待更新")}</td><td>${escapeHtml(managedLabels[asset.status])} / ${escapeHtml(managedLabels[asset.availability])}<br>revision ${escapeHtml(asset.revision)}</td><td>${escapeHtml(coverage || "暂无行情")}<br>${escapeHtml(issues.join("；"))}</td><td>${task ? `${escapeHtml(managedLabels[task.status])}${progress}<br>${escapeHtml(task.message || "")}<br>${escapeHtml(task.task_id)}` : "无任务"}</td><td>${actions.map((action) => `<button type="button" data-managed-action="${action}">${labels[action]}</button>`).join(" ")}</td></tr>`;
  }).join("");
}

async function loadManagedStocks(page = 1) {
  const generation = ++managedState.generation;
  clearTimeout(managedState.timer);
  managedState.page = page;
  try {
    const query = new URLSearchParams({query: $("#managed-filter").value.trim(), include_archived: String($("#managed-archived").checked), page: String(page), page_size: "30"});
    const payload = await requestJson(`/api/v1/data/managed-stocks?${query}`);
    if (generation !== managedState.generation) return;
    managedState.items = payload.items;
    $("#managed-list").innerHTML = payload.items.length ? `<table><thead><tr><th>股票</th><th>管理 / 可用状态</th><th>实际覆盖与诊断</th><th>最近任务</th><th>操作</th></tr></thead><tbody>${managedRows(payload.items)}</tbody></table>` : "<p>尚无匹配的本地受管理股票。</p>";
    $("#managed-list").querySelectorAll("[data-managed-action]").forEach((button) => button.addEventListener("click", async () => {
      const asset = managedState.items.find((item) => item.security_id === button.closest("tr").dataset.managedId);
      try {await managedAction(asset, button.dataset.managedAction);} catch (error) {managedMessage(error.message, true);}
    }));
    explorerPagination($("#managed-pages"), {...payload.pagination, pages: Math.max(1, Math.ceil(payload.pagination.total / payload.pagination.page_size))}, loadManagedStocks);
    if (payload.items.some((item) => ["queued", "running"].includes(item.task?.status))) {
      managedState.timer = setTimeout(() => {if (!$("#data-explorer").hidden) loadManagedStocks(page);}, 5000);
    }
  } catch (error) {if (generation === managedState.generation) managedMessage(error.message, true);}
}

function bindManagedStocks() {
  $("#managed-end").value = managedToday();
  $("#managed-add-form").addEventListener("submit", addManagedStock);
  $("#managed-refresh").addEventListener("click", () => loadManagedStocks(managedState.page));
  $("#managed-archived").addEventListener("change", () => loadManagedStocks(1));
  let searchTimer, filterTimer, searchGeneration = 0;
  let readyTimer, readyGeneration = 0;
  $("#pool-ready-query").addEventListener("input", () => {
    clearTimeout(readyTimer);
    const current = ++readyGeneration, query = $("#pool-ready-query").value.trim();
    const results = $("#pool-ready-results");
    if (!query) {results.textContent = ""; return;}
    readyTimer = setTimeout(async () => {
      try {
        const payload = await requestJson(`/api/v1/data/managed-stocks?ready_only=true&page_size=20&query=${encodeURIComponent(query)}`);
        if (current !== readyGeneration) return;
        results.innerHTML = payload.items.length ? payload.items.map((item) => `<button type="button" data-ready-symbol="${escapeHtml(item.symbol)}">${escapeHtml(item.symbol)} · ${escapeHtml(item.name)} · ${escapeHtml(item.coverage.effective_start)} → ${escapeHtml(item.coverage.effective_end)}</button>`).join(" ") : "没有已就绪股票，请先在数据浏览添加并补齐。";
        results.querySelectorAll("[data-ready-symbol]").forEach((button) => button.addEventListener("click", () => {
          const input = $("#stock-pool-create-symbols"), symbols = splitStockSymbols(input.value);
          if (!symbols.includes(button.dataset.readySymbol)) symbols.push(button.dataset.readySymbol);
          input.value = symbols.join("\n");
        }));
      } catch (error) {if (current === readyGeneration) results.textContent = error.message;}
    }, 250);
  });
  $("#managed-filter").addEventListener("input", () => {clearTimeout(filterTimer); filterTimer = setTimeout(() => loadManagedStocks(1), 250);});
  $("#managed-directory-query").addEventListener("input", () => {
    clearTimeout(searchTimer);
    const current = ++searchGeneration, query = $("#managed-directory-query").value.trim();
    const results = $("#managed-directory-results");
    if (!query) {results.textContent = ""; return;}
    searchTimer = setTimeout(async () => {
      try {
        const items = await requestJson(`/api/v1/data/managed-stocks/directory?query=${encodeURIComponent(query)}&limit=20`);
        if (current !== searchGeneration) return;
        results.innerHTML = items.length ? items.map((item) => `<button type="button" data-managed-symbol="${escapeHtml(item.symbol)}" ${item.status !== "active" ? "disabled" : ""}>${escapeHtml(item.symbol)} · ${escapeHtml(item.name)} · ${escapeHtml(item.exchange)} · ${escapeHtml(item.status)} · ${escapeHtml(item.management_status || "未纳入管理")}</button>`).join(" ") : "本地目录无匹配证券，请先更新证券主数据。";
        results.querySelectorAll("[data-managed-symbol]").forEach((button) => button.addEventListener("click", () => {$("#managed-symbol").value = button.dataset.managedSymbol;}));
      } catch (error) {if (current === searchGeneration) results.textContent = error.message;}
    }, 250);
  });
}

if ($("#managed-add-form")) bindManagedStocks();
