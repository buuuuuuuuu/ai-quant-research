// Local data enrollment is explicit; reading/searching never starts a download.
const managedState = {busy: false, page: 1, generation: 0, timer: null, items: [], tasks: new Map()};
const managedLabels = {ready: "可用", partial: "部分完成", unavailable: "待补齐", active: "纳入管理", archived: "已移出", queued: "等待同步", running: "同步中", succeeded: "同步完成", failed: "同步失败"};
const managedStageLabels = {listing_date:"上市日期核验", daily_bar:"行情同步", announcement:"定期公告同步"};
const managedStageStatusLabels = {pending:"等待开始", running:"进行中", succeeded:"已完成", failed:"失败", skipped:"已跳过", not_started:"尚未开始", not_requested:"未请求"};
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
  const includeAnnouncements = $("#managed-include-announcements").checked;
  if (!/^[0-9]{6}$/.test(symbol) || !start || !end || start > end) {
    managedMessage("请输入六位代码和有效起止日期。", true); return;
  }
  if (!window.confirm(`确认添加 ${symbol} 并访问公网回填 ${start} 至 ${end} 的不复权/前复权日线${includeAnnouncements ? "及一季报、中报、三季报、年报公告元数据" : "（不含公告）"}？不下载财务指标、PDF，不进行 OCR。`)) return;
  await managedMutation("/api/v1/data/managed-stocks", {symbol, start, end, confirm: true, auto_update: $("#managed-auto").checked, include_announcements: includeAnnouncements});
}

async function managedAction(asset, action) {
  if (action === "financial") {
    return openResearchReadiness("screen", {symbols: [asset.symbol], as_of: managedToday(), conditions: [{field: "roe", operator: "gte", value: 0}], missing_policy: "show"});
  }
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
  if (action === "verify-gaps") {
    const count = asset.coverage.gap_summary?.unverified_gap || 0;
    if (!window.confirm(`确认访问公网，按缺失日期核验 ${asset.symbol} 的 ${count} 个未知缺口是否有明确停牌记录？不会下载或改写K线，不会运行研究；查不到可靠证据的日期仍会保持阻断。`)) return;
    return managedMutation(`/api/v1/data/managed-stocks/${encodeURIComponent(asset.security_id)}/verify-gaps`, {expected_revision: asset.revision, confirm: true});
  }
  if (action === "sync") {
    const end = managedToday();
    const retry = asset.task?.status === "failed" && asset.task?.result?.stages;
    const includeAnnouncements = retry ? Boolean(asset.task.spec?.include_announcements) : $("#managed-include-announcements").checked;
    if (!window.confirm(`确认更新 ${asset.symbol}，补齐 ${asset.target_start} 至 ${end} 的不复权/前复权行情${includeAnnouncements ? "及一季报、中报、三季报、年报公告元数据" : "（不含公告）"}？此操作会访问公网，不下载财务指标或 PDF，不进行 OCR。`)) return;
    return managedMutation("/api/v1/data/managed-stocks", {symbol: asset.symbol, start: asset.target_start, end, expected_revision: asset.revision, confirm: true, refresh: true, auto_update: asset.auto_update, include_announcements: includeAnnouncements});
  }
  const text = action === "archive" ? "确认移出管理并停止自动更新？保留历史数据；存在活动股票池引用时将拒绝操作。" : action === "restore" ? "确认恢复管理？不会自动下载或开启定时更新；需要补齐时请单独确认。" : `确认${asset.auto_update ? "关闭" : "开启"}日常自动行情更新？`;
  if (!window.confirm(text)) return;
  return managedMutation(`/api/v1/data/managed-stocks/${encodeURIComponent(asset.security_id)}`, {action, expected_revision: asset.revision, confirm: true, ...(action === "configure" ? {auto_update: !asset.auto_update} : {})}, "PATCH");
}

function managedStageSummary(task) {
  const stages = task?.result?.stages;
  if (!stages) return "";
  return `<div class="managed-stage-list">${Object.entries(managedStageLabels).map(([key, label]) => {
    const stage = stages[key] || {status:"unknown", message:"历史任务未保存阶段信息"};
    const counts = stage.total_partitions == null ? "" : ` · 分区 ${stage.completed_partitions ?? 0}/${stage.total_partitions}`;
    const evidence = key === "listing_date" && stage.listing_date ? `<br>${escapeHtml(stage.listing_date)} · ${escapeHtml(stage.source || "来源未记录")}` : "";
    return `<div><strong>${label}</strong>：${escapeHtml(managedStageStatusLabels[stage.status] || `${stage.status || "unknown"}（暂无中文说明）`)}${escapeHtml(counts)}<br>${escapeHtml(stage.message || "说明未记录")}${evidence}</div>`;
  }).join("")}</div>`;
}

function managedSyncLabel(asset) {
  const stages = asset.task?.result?.stages;
  if (asset.task?.status !== "failed" || !stages) return "补齐 / 更新";
  if (stages.listing_date?.status === "failed" || stages.daily_bar?.status === "not_started") return "重试上市日期并继续行情";
  if (stages.daily_bar?.status === "failed") return "重试行情失败分区";
  if (stages.announcement?.status === "failed") return "重试公告失败分区";
  return "补齐 / 更新";
}

function managedOfficialEvidence(gap) {
  const evidence = gap?.evidence;
  if (!evidence || evidence.source_type !== "official_announcement_chain") return "";
  const ids = Array.isArray(evidence.announcement_ids) ? evidence.announcement_ids : [];
  return `<details><summary>查看官方公告证据链</summary><p>开始停牌：${escapeHtml(evidence.suspension_start || "未记录")}；复牌交易日：${escapeHtml(evidence.resume_date || "未记录")}；覆盖 ${escapeHtml(evidence.covered_dates?.length ?? "未记录")} 个待核验交易日。</p><p>公告：${escapeHtml(ids.join("、") || "未记录")}；来源：${escapeHtml(evidence.source || "未记录")}；核验时间：${escapeHtml(evidence.verified_at || "未记录")}。</p></details>`;
}

function managedEvidenceAttempts(gap) {
  const attempts = Array.isArray(gap?.evidence_attempts) ? gap.evidence_attempts : [];
  if (!attempts.length) return "";
  return `<details><summary>查看已尝试的证据来源</summary><ul>${attempts.map((attempt) => `<li>${escapeHtml(attempt.source || "未记录")} · ${escapeHtml(attempt.status || "unknown")}${attempt.error ? ` · ${escapeHtml(attempt.error)}` : ""}</li>`).join("")}</ul></details>`;
}

function managedRows(items) {
  return items.map((asset) => {
    const bars = asset.coverage.adjustments || {}, task = asset.task;
    const coverage = Object.entries(bars).map(([adjustment, row]) => `${adjustment}: ${row.start_date || "—"} → ${row.end_date || "—"} · ${row.rows} 行`).join("；");
    const issues = [...new Set([...(asset.coverage.issues || []), ...(task?.error ? [task.error] : [])])];
    const grouped = new Map();
    Object.entries(bars).forEach(([adjustment, row]) => (row.gap_details || []).forEach((gap) => {
      const key = `${gap.date}|${gap.classification}|${gap.source || ""}`;
      const found = grouped.get(key) || {...gap, adjustments: []};
      found.adjustments.push(adjustment); grouped.set(key, found);
    }));
    const gapLabels = {verified_suspension:"已核验停牌",missing_bar:"真实行情缺失",calendar_mismatch:"交易日历冲突",unverified_gap:"暂时无法核验"};
    const gapRows = [...grouped.values()].slice(0, 20).map((gap) => `<li><strong>${escapeHtml(gap.date)}</strong> · ${escapeHtml([...new Set(gap.adjustments)].join("/"))} · ${escapeHtml(gapLabels[gap.classification] || `${gap.classification}（暂无中文说明）`)}<br>${escapeHtml(gap.reason || "原因未记录")}${gap.source ? `<br>证据：${escapeHtml(gap.source)}${gap.checked_at ? ` · ${escapeHtml(gap.checked_at)}` : ""}` : ""}${managedOfficialEvidence(gap)}${managedEvidenceAttempts(gap)}</li>`).join("");
    const gapMore = Object.values(bars).reduce((sum, row) => sum + (row.gap_details_truncated || 0), 0);
    const gapDetail = gapRows ? `<details><summary>查看缺失日期、分类与证据</summary><ul>${gapRows}</ul>${gapMore ? `<p>另有 ${escapeHtml(gapMore)} 条未在本页显示。</p>` : ""}</details>` : "";
    const announcement = asset.coverage.announcement_sync;
    const announcementText = announcement ? `定期公告：${announcement.status === "published" ? "已同步" : "未完整完成"} · ${announcement.row_count ?? "—"} 条 · ${announcement.start_date} → ${announcement.end_date}` : "定期公告：尚无同步记录";
    const actions = asset.status === "archived" ? ["restore", "prices", "references"] : ["sync", ...(asset.coverage.requires_gap_verification ? ["verify-gaps"] : []), "financial", "configure", "archive", "prices", "references"];
    const labels = {restore: "恢复管理", financial: "财务预检 / 补齐计划", sync: managedSyncLabel(asset), "verify-gaps": "核验缺失日期", configure: asset.auto_update ? "停止自动更新" : "开启自动更新", archive: "移出管理", prices: "查看明细", references: "关联股票池"};
    const progress = task ? ` ${escapeHtml(task.progress)}%` : "";
    const listing = asset.coverage.listing_evidence;
    const listingText = asset.listing_date ? `上市日期：${asset.listing_date} · ${asset.listing_source || "来源未记录"}${listing?.fallback_used ? "（使用备用来源）" : ""}` : "上市日期：尚未核验（不会根据第一根K线推断）";
    const financial = asset.financial_summary;
    const financialText = financial ? `财务：${financial.reports} 份报告 / ${financial.metrics} 项指标；最近公开 ${financial.latest_published_at || "—"}（不等于研究就绪）` : "财务：尚未核验";
    return `<tr data-managed-id="${escapeHtml(asset.security_id)}"><td>${escapeHtml(asset.symbol || asset.security_id)} · ${escapeHtml(asset.name || "目录待更新")}</td><td>${escapeHtml(managedLabels[asset.status])} / ${escapeHtml(managedLabels[asset.availability])}<br>revision ${escapeHtml(asset.revision)}</td><td>${escapeHtml(listingText)}${listing?.checked_at ? `<br>核验时间：${escapeHtml(listing.checked_at)}` : ""}<br>${escapeHtml(coverage || "暂无行情")}<br>${escapeHtml(announcementText)}<br>${escapeHtml(financialText)}<br>${escapeHtml(issues.join("；"))}${gapDetail}</td><td>${task ? `${escapeHtml(managedLabels[task.status])}${progress}<br>${escapeHtml(task.message || "")}${managedStageSummary(task)}<br>${escapeHtml(task.task_id)}` : "无任务"}</td><td>${actions.map((action) => `<button type="button" data-managed-action="${action}">${labels[action]}</button>`).join(" ")}</td></tr>`;
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
    const finished = payload.items.some((item) => item.task && ["queued", "running"].includes(managedState.tasks.get(item.task.task_id)) && ["succeeded", "failed"].includes(item.task.status));
    managedState.tasks = new Map(payload.items.filter((item) => item.task).map((item) => [item.task.task_id, item.task.status]));
    managedState.items = payload.items;
    $("#managed-list").innerHTML = payload.items.length ? `<table><thead><tr><th>股票</th><th>管理 / 可用状态</th><th>实际覆盖与诊断</th><th>最近任务</th><th>操作</th></tr></thead><tbody>${managedRows(payload.items)}</tbody></table>` : "<p>尚无匹配的本地受管理股票。</p>";
    $("#managed-list").querySelectorAll("[data-managed-action]").forEach((button) => button.addEventListener("click", async () => {
      const asset = managedState.items.find((item) => item.security_id === button.closest("tr").dataset.managedId);
      try {await managedAction(asset, button.dataset.managedAction);} catch (error) {managedMessage(error.message, true);}
    }));
    explorerPagination($("#managed-pages"), {...payload.pagination, pages: Math.max(1, Math.ceil(payload.pagination.total / payload.pagination.page_size))}, loadManagedStocks);
    if (payload.items.some((item) => ["queued", "running"].includes(item.task?.status))) {
      managedState.timer = setTimeout(() => {if (!$("#data-explorer").hidden) return loadManagedStocks(page);}, 5000);
    }
    if (finished && typeof loadExplorerEntities === "function" && ["prices", "announcements"].includes(state.explorerDataset)) await loadExplorerEntities(state.explorerEntityPage || 1);
  } catch (error) {if (generation === managedState.generation) managedMessage(error.message, true);}
}

function bindManagedStocks() {
  const requestedSymbol = new URLSearchParams(window.location?.search || "").get("managed");
  if (/^[0-9]{6}$/.test(requestedSymbol || "")) $("#managed-filter").value = requestedSymbol;
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
