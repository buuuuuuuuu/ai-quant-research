const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = { run: null, activeTab: "trades", conversationId: null, copilotPlan: null, explorerDataset: "prices", explorerEntity: null, explorerPage: 1, explorerEntityPage: 1, explorerGeneration: 0, explorerSelection: 0, explorerRecordRequest: 0, screenRun: null, stockPools: [], stockPool: null };

const formatNumber = (value) => new Intl.NumberFormat("zh-CN").format(Number(value || 0));
const formatMoney = (value) => new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(Number(value || 0));
const formatPct = (value) => `${(Number(value || 0) * 100).toFixed(2)}%`;
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  if (response.status === 401) { window.location.assign("/login"); throw new Error("登录已过期，请重新登录"); }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail;
    const message = Array.isArray(detail)
      ? detail.map((item) => `${(item.loc || []).join(".")}: ${item.msg || "参数错误"}`).join("；")
      : typeof detail === "string" ? detail : `请求失败（${response.status}）`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return payload;
}

const datasetLabels = { daily_bar: "股票行情", benchmark_daily_bar: "指数基准", announcement: "定期公告", financial_metric: "财务指标", data_platform_fixture: "样例数据" };
const taskTypeLabels = { sync: "增量同步", backup: "数据库备份", backtest: "策略回测", factor_lab: "因子实验", copilot: "AI 研究", document_ingest: "公告文档解析", document_event_extract: "公告事件提取", stock_pool_sync: "股票池缺失数据同步" };
const documentEventTypeLabels = { performance_forecast: "业绩预告 / 快报", dividend: "分红派息", share_repurchase: "股份回购", major_contract: "重大合同", shareholder_change: "股东增减持" };
const taskStatusLabels = { queued: "等待执行", running: "正在运行", succeeded: "执行成功", failed: "执行失败", published: "已发布" };
const auditActionLabels = { security_master_refresh_requested: "请求更新证券主数据", security_master_refreshed: "证券主数据更新完成", login_succeeded: "管理员登录", login_failed: "登录失败", login_rate_limited: "登录限流", logout: "管理员退出", sync_requested: "发起增量同步", sync_completed: "增量同步完成", sync_failed: "增量同步失败", backup_requested: "发起数据库备份", backup_created: "数据库备份完成", backup_verified: "验证数据库备份", backup_restored: "恢复数据库备份", backup_pruned: "清理过期备份", backtest_requested: "发起策略回测", factor_lab_requested: "发起因子研究", copilot_conversation_created: "创建 AI 研究对话", copilot_plan_created: "生成 AI 研究计划", copilot_plan_confirmed: "确认 AI 研究计划", copilot_plan_execution_requested: "执行 AI 研究计划", point_in_time_screen_succeeded: "完成条件选股", screen_watchlist_saved: "保存候选股票池", stock_pool_created: "创建股票池", stock_pool_updated: "修改股票池", stock_pool_member_added: "添加股票池成员", stock_pool_member_removed: "移除股票池成员", stock_pool_duplicated: "复制股票池", stock_pool_archived: "归档股票池", stock_pool_default_changed: "切换默认股票池", stock_pool_sync_requested: "同步股票池缺失数据", document_event_extraction_requested: "发起公告事件提取", document_event_reviewed: "审核公告事件", document_event_study_completed: "完成公告事件研究", alert_acknowledged: "确认运行告警" };

function splitStockSymbols(value) {
  return value.split(/[\s,，;；]+/).map((item) => item.trim()).filter(Boolean);
}

function selectedPool(selectId) {
  const option = $(`${selectId} option:checked`);
  if (!option || option.value === "manual") return null;
  return {stock_pool_id: option.value, stock_pool_revision: Number(option.dataset.revision)};
}

function stockPoolOptions() {
  return '<option value="manual">手动输入股票代码</option>' + state.stockPools.filter((pool) => pool.status === "active").map((pool) => `<option value="${escapeHtml(pool.pool_id)}" data-revision="${escapeHtml(pool.current_revision)}" ${pool.is_default ? "selected" : ""}>${escapeHtml(pool.name)}${pool.is_default ? " · 默认" : ""} · ${formatNumber(pool.member_count)} 只 · r${escapeHtml(pool.current_revision)}</option>`).join("");
}

async function loadStockPools(selectPoolId = null) {
  try {
    state.stockPools = await requestJson("/api/v1/stock-pools?include_archived=true&limit=200");
    const list = $("#stock-pool-list");
    list.innerHTML = state.stockPools.length ? state.stockPools.map((pool) => `<button type="button" class="stock-pool-list-item ${pool.status === "archived" ? "archived" : ""}" data-pool-id="${escapeHtml(pool.pool_id)}"><strong>${escapeHtml(pool.name)} ${pool.is_default ? '<span class="quality-pill">默认</span>' : ""}</strong><span>${escapeHtml(pool.source_type)} · ${formatNumber(pool.member_count)} 只 · r${escapeHtml(pool.current_revision)} · ${escapeHtml(pool.status)}</span></button>`).join("") : '<p class="empty-state">暂无股票池。</p>';
    list.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => loadStockPoolDetail(button.dataset.poolId).catch((error) => setStockPoolMessage(error.message, true))));
    ["#research-stock-pool", "#screen-stock-pool"].forEach((selector) => {const current = $(selector).value; $(selector).innerHTML = stockPoolOptions(); if ([...$(selector).options].some((option) => option.value === current)) $(selector).value = current;});
    const targetId = selectPoolId || state.stockPool?.pool_id || state.stockPools.find((pool) => pool.is_default)?.pool_id;
    if (targetId) await loadStockPoolDetail(targetId);
    return true;
  } catch (error) { $("#stock-pool-list").innerHTML = `<p class="negative">${escapeHtml(error.message)}</p>`; return false; }
}

async function loadStockPoolDetail(poolId) {
  const pool = await requestJson(`/api/v1/stock-pools/${encodeURIComponent(poolId)}`);
  state.stockPool = pool;
  const members = pool.members || [];
  $("#stock-pool-detail").innerHTML = `<div class="stock-pool-detail-head"><div><p class="eyebrow">${escapeHtml(pool.source_type)} · REVISION ${escapeHtml(pool.current_revision)}</p><h3>${escapeHtml(pool.name)}</h3><p>${escapeHtml(pool.description || "暂无描述")}</p></div><span class="quality-pill">${escapeHtml(pool.status)}</span></div>${pool.warning ? `<p class="screen-warnings">${escapeHtml(pool.warning)}</p>` : ""}<div class="form-grid"><label class="field"><span>名称</span><input id="stock-pool-name" maxlength="120" value="${escapeHtml(pool.name)}"></label><label class="field"><span>描述</span><input id="stock-pool-description" maxlength="1000" value="${escapeHtml(pool.description)}"></label></div><div class="stock-pool-actions"><button id="stock-pool-save" class="secondary-button" type="button">保存元数据</button><button id="stock-pool-default" class="secondary-button" type="button" ${pool.is_default || pool.status === "archived" ? "disabled" : ""}>设为默认</button><button id="stock-pool-duplicate" class="secondary-button" type="button">复制</button><button id="stock-pool-sync" class="secondary-button" type="button" ${pool.status === "archived" ? "disabled" : ""}>同步缺失数据</button><button id="stock-pool-archive" class="secondary-button" type="button" ${pool.is_default || pool.status === "archived" ? "disabled" : ""}>归档</button></div><label class="field"><span>搜索证券代码或名称</span><input id="stock-pool-security-search" type="search" maxlength="120" placeholder="600519 或 贵州茅台"></label><div id="stock-pool-search-results"></div><label class="field"><span>批量添加六位代码</span><textarea id="stock-pool-add-symbols" rows="2" placeholder="单次最多 200 只"></textarea></label><button id="stock-pool-add" class="secondary-button" type="button" ${pool.status === "archived" ? "disabled" : ""}>添加成员</button><button id="stock-pool-remove-selected" class="secondary-button" type="button" ${pool.status === "archived" ? "disabled" : ""}>移除勾选成员</button><p id="stock-pool-member-message" class="stock-pool-message" data-stock-pool-message role="status" aria-live="polite"></p><div class="table-shell stock-pool-members"><table><thead><tr><th></th><th>代码 / 名称</th><th>交易所 / 状态</th><th>数据覆盖</th><th>备注</th><th>顺序</th><th></th></tr></thead><tbody>${members.map((member) => `<tr data-security-id="${escapeHtml(member.security_id)}"><td><input class="stock-pool-member-select" type="checkbox"></td><td>${escapeHtml(member.symbol)} · ${escapeHtml(member.name)}</td><td>${escapeHtml(member.exchange)} · ${escapeHtml(member.status)}</td><td>${escapeHtml(member.coverage.start_date || "—")} → ${escapeHtml(member.coverage.end_date || "—")} · ${formatNumber(member.coverage.rows)} 行</td><td><input class="stock-pool-member-note" maxlength="500" value="${escapeHtml(member.note)}"></td><td><input class="stock-pool-member-order" type="number" min="1" max="500" value="${escapeHtml(member.display_order)}"></td><td><button class="stock-pool-member-save" type="button" ${pool.status === "archived" ? "disabled" : ""}>保存</button><button class="stock-pool-member-remove" type="button" ${pool.status === "archived" ? "disabled" : ""}>移除</button></td></tr>`).join("")}</tbody></table></div>`;
  bindStockPoolDetail();
  const pendingMembers = members.filter((member) => member.admission_warning);
  if (pendingMembers.length) setStockPoolMessage(`旧成员保留，但本地行情待补齐：${pendingMembers.map((member) => member.symbol).join("、")}。请在数据浏览中查看。`, true);
}

function setStockPoolMessage(text, isError = false) {
  const messages = $$("[data-stock-pool-message]");
  messages.forEach((message) => {
    message.textContent = text;
    message.classList.toggle("negative", isError);
    message.setAttribute("role", isError ? "alert" : "status");
  });
  if (isError) (messages.find((message) => message.id === "stock-pool-member-message") || messages[0])?.scrollIntoView({block:"nearest", behavior:"smooth"});
}

function stockPoolErrorMessage(error) {
  if (error.message.includes("missing from security_master")) return `本地证券主数据缺少该股票，请点击“更新证券主数据”，完成后重新搜索并添加。${error.message}`;
  if (error.message.includes("not active in security_master")) return `证券处于停牌、退市或异常状态，不能添加。${error.message}`;
  if (error.status === 409) return `股票池版本已变化，请刷新股票池后重新操作；本次输入尚未保存。${error.message}`;
  return error.message;
}

async function stockPoolMutation(url, options, {refreshPool = true} = {}) {
  if (state.stockPoolSaving) return null;
  state.stockPoolSaving = true;
  const controls = $$("#stock-pool-detail button, #stock-pool-detail input, #stock-pool-detail textarea, #stock-pool-create-form button, #stock-pool-create-form input, #stock-pool-refresh, #stock-pool-list button");
  const disabled = controls.map((element) => element.disabled);
  controls.forEach((element) => {element.disabled = true;});
  $("#stock-pool-detail").setAttribute("aria-busy", "true");
  setStockPoolMessage("正在保存，请稍候…");
  try {
    const result = await requestJson(url, options);
    if (refreshPool) {
      const refreshed = await loadStockPools(result.pool_id);
      setStockPoolMessage(`已保存 revision ${result.current_revision}${refreshed ? "" : "；列表刷新失败，请手动刷新"}`, !refreshed);
    } else {
      setStockPoolMessage(`同步任务 ${result.task_id} 已提交或复用，请在“数据同步与备份”查看进度。`);
    }
    return result;
  } catch (error) {
    setStockPoolMessage(stockPoolErrorMessage(error), true);
    return null; // Event handlers must not create unhandled promise rejections.
  } finally {
    controls.forEach((element, index) => {if (element.isConnected) element.disabled = disabled[index];});
    $("#stock-pool-detail").setAttribute("aria-busy", "false");
    state.stockPoolSaving = false;
  }
}

async function refreshSecurityMaster() {
  if (state.securityMasterRefreshing) return;
  if (!window.confirm("确认访问公网更新证券代码和名称目录？不会下载历史行情、公告或财务数据，不会自动添加股票池成员，也不会扩大定时同步范围。")) return;
  state.securityMasterRefreshing = true;
  state.securityMasterTaskId = null;
  const button = $("#security-master-refresh"), message = $("#security-master-message");
  button.disabled = true;
  message.classList.remove("negative");
  message.textContent = "正在提交证券主数据更新…";
  try {
    let task = await requestJson("/api/v1/stock-pools/securities/refresh", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({confirm:true})});
    state.securityMasterTaskId = task.task_id;
    for (let attempt = 0; attempt < 240; attempt++) {
      message.textContent = `${task.message || "正在更新证券目录"} · ${task.progress}% · ${task.task_id}`;
      if (task.status === "failed") throw new Error(task.error || task.message || "证券目录更新失败，可重试");
      if (task.status === "succeeded") {
        message.textContent = `证券主数据更新完成：新增 ${task.result?.added_count || 0} 只，更新 ${task.result?.updated_count || 0} 只。请重新搜索并添加成员；刷新股票池可查看最新名称。`;
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
      task = await requestJson(`/api/v1/ops/tasks/${encodeURIComponent(task.task_id)}`);
    }
    message.textContent = `任务 ${task.task_id} 仍在执行，请在“数据同步与备份”查看状态。`;
  } catch (error) {
    message.textContent = `${error.message}${state.securityMasterTaskId ? `（任务 ${state.securityMasterTaskId}，可在“数据同步与备份”查看）` : ""}`;
    message.classList.add("negative");
  } finally {
    button.disabled = false;
    state.securityMasterRefreshing = false;
  }
}

function bindStockPoolDetail() {
  const pool = state.stockPool, jsonHeaders = {"Content-Type":"application/json"};
  $("#stock-pool-save").addEventListener("click", () => stockPoolMutation(`/api/v1/stock-pools/${encodeURIComponent(pool.pool_id)}`, {method:"PATCH",headers:jsonHeaders,body:JSON.stringify({expected_revision:pool.current_revision,name:$("#stock-pool-name").value,description:$("#stock-pool-description").value})}));
  $("#stock-pool-default").addEventListener("click", () => stockPoolMutation(`/api/v1/stock-pools/${encodeURIComponent(pool.pool_id)}/set-default`, {method:"POST",headers:jsonHeaders,body:JSON.stringify({expected_revision:pool.current_revision})}));
  $("#stock-pool-duplicate").addEventListener("click", () => {const name=window.prompt("新股票池名称",`${pool.name} 副本`);if(name)stockPoolMutation(`/api/v1/stock-pools/${encodeURIComponent(pool.pool_id)}/duplicate`,{method:"POST",headers:jsonHeaders,body:JSON.stringify({expected_revision:pool.current_revision,name})});});
  $("#stock-pool-archive").addEventListener("click", () => {if(window.confirm(`确认归档“${pool.name}”？历史研究仍可查询。`))stockPoolMutation(`/api/v1/stock-pools/${encodeURIComponent(pool.pool_id)}?expected_revision=${pool.current_revision}`,{method:"DELETE"});});
  $("#stock-pool-sync").addEventListener("click", () => {if (!window.confirm(`确认同步 ${pool.member_count} 只股票的缺失数据？此操作可能访问公网。`)) return; stockPoolMutation(`/api/v1/stock-pools/${encodeURIComponent(pool.pool_id)}/sync-missing`, {method:"POST",headers:jsonHeaders,body:JSON.stringify({expected_revision:pool.current_revision,as_of:new Date().toLocaleDateString("en-CA")})}, {refreshPool:false});});
  $("#stock-pool-add").addEventListener("click", () => stockPoolMutation(`/api/v1/stock-pools/${encodeURIComponent(pool.pool_id)}/members`,{method:"POST",headers:jsonHeaders,body:JSON.stringify({expected_revision:pool.current_revision,symbols:splitStockSymbols($("#stock-pool-add-symbols").value)})}));
  $("#stock-pool-remove-selected").addEventListener("click",()=>{const ids=$$(".stock-pool-member-select:checked").map((item)=>item.closest("tr").dataset.securityId);if(ids.length&&window.confirm(`确认移除 ${ids.length} 个成员？`))stockPoolMutation(`/api/v1/stock-pools/${encodeURIComponent(pool.pool_id)}/members/remove`,{method:"POST",headers:jsonHeaders,body:JSON.stringify({expected_revision:pool.current_revision,security_ids:ids})});});
  $$(".stock-pool-member-save").forEach((button)=>button.addEventListener("click",()=>{const row=button.closest("tr");stockPoolMutation(`/api/v1/stock-pools/${encodeURIComponent(pool.pool_id)}/members/${encodeURIComponent(row.dataset.securityId)}`,{method:"PATCH",headers:jsonHeaders,body:JSON.stringify({expected_revision:pool.current_revision,note:row.querySelector(".stock-pool-member-note").value,display_order:Number(row.querySelector(".stock-pool-member-order").value)})});}));
  $$(".stock-pool-member-remove").forEach((button)=>button.addEventListener("click",()=>{const row=button.closest("tr");if(window.confirm("确认移除此成员？"))stockPoolMutation(`/api/v1/stock-pools/${encodeURIComponent(pool.pool_id)}/members/${encodeURIComponent(row.dataset.securityId)}?expected_revision=${pool.current_revision}`,{method:"DELETE"});}));
  let timer, generation = 0;
  const search = $("#stock-pool-security-search"), results = $("#stock-pool-search-results");
  search.addEventListener("input", () => {
    clearTimeout(timer);
    const current = ++generation, query = search.value.trim();
    if (!query) {results.textContent = ""; return;}
    timer = setTimeout(async () => {
      try {
        const items = await requestJson(`/api/v1/stock-pools/securities/search?query=${encodeURIComponent(query)}&limit=10`);
        if (current !== generation || !search.isConnected) return;
        if (!items.length) {
          results.textContent = "本地证券目录未找到匹配项。请先点击本区域上方“更新证券主数据”，完成后重新搜索。";
          return;
        }
        results.innerHTML = items.map((item) => `<button type="button" data-symbol="${escapeHtml(item.symbol)}" ${!item.eligible ? "disabled" : ""}>${escapeHtml(item.symbol)} · ${escapeHtml(item.name)} · ${escapeHtml(item.admission_warning || "本地行情已就绪")}</button>`).join("");
        results.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => {$("#stock-pool-add-symbols").value = button.dataset.symbol;}));
      } catch (error) {
        if (current === generation && search.isConnected) results.textContent = `证券搜索失败：${error.message}`;
      }
    }, 250);
  });
}

function operationTime(value) {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "暂无记录";
}

function operationRow(title, description, isError = false) {
  return `<div class="operation-row${isError ? " negative" : ""}"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(description)}</span></div>`;
}

async function loadOperations() {
  try {
    const overview = await requestJson("/api/v1/ops/status");
    const datasets = overview.datasets || [];
    const tasks = overview.tasks || [];
    const backups = overview.backups || [];
    const alerts = overview.alerts || [];
    const audits = overview.audit || [];
    const quality = overview.quality || {};
    $("#sync-status-list").innerHTML = datasets.length ? datasets.map((item) => operationRow(`${datasetLabels[item.dataset] || item.dataset} · ${taskStatusLabels[item.status] || item.status}`, item.error || `${formatNumber(item.row_count)} 行 · ${operationTime(item.ended_at || item.started_at)}`, Boolean(item.error))).join("") : '<p class="empty-state">还没有数据同步记录。</p>';
    $("#operations-task-list").innerHTML = tasks.length ? tasks.slice(0, 5).map((item) => operationRow(`${item.spec?.dataset === "security_master" ? "证券主数据更新" : taskTypeLabels[item.task_type] || item.task_type} · ${taskStatusLabels[item.status] || item.status}`, item.error || `${item.progress}% · ${operationTime(item.created_at)}`, item.status === "failed")).join("") : '<p class="empty-state">还没有后台任务。</p>';
    $("#backup-status-list").innerHTML = backups.length ? backups.slice(0, 4).map((item) => operationRow(`${(Number(item.size_bytes) / 1024 / 1024).toFixed(1)} MB · ${item.exists ? "文件正常" : "文件缺失"}`, operationTime(item.created_at), !item.exists)).join("") : '<p class="empty-state">还没有数据库备份。</p>';
    $("#alert-status-list").innerHTML = alerts.length ? alerts.slice(0, 5).map((item) => operationRow(`${item.severity === "critical" ? "严重" : "提醒"} · ${item.title}`, item.message, item.severity === "critical")).join("") : '<p class="empty-state">当前没有运行告警。</p>';
    const checks = quality.checks || [];
    const failedPartitions = quality.failed_partitions || [];
    const checkRows = checks.map((item) => operationRow(`${item.severity === "blocking" ? "阻断规则" : "提醒规则"} · ${formatNumber(item.total)} 项`, item.failed ? `${formatNumber(item.failed)} 项未通过` : "全部通过", Boolean(item.failed)));
    if (failedPartitions.length) checkRows.push(operationRow("失败同步分区", `${formatNumber(failedPartitions.length)} 个分区需要处理`, true));
    checkRows.push(operationRow("已监控股票", `${formatNumber((quality.securities || []).length)} 只`));
    $("#quality-status-list").innerHTML = checkRows.join("");
    $("#audit-status-list").innerHTML = audits.length ? audits.slice(0, 5).map((item) => operationRow(auditActionLabels[item.action] || item.action, `${item.actor} · ${operationTime(item.created_at)}`, item.outcome === "failure")).join("") : '<p class="empty-state">还没有运维审计记录。</p>';
  } catch (error) {
    const message = $("#operations-message");
    message.textContent = error.message;
    message.classList.add("error");
  }
}

async function checkOperations() {
  const button = $("#run-health-check");
  const message = $("#operations-message");
  button.disabled = true;
  try {
    const result = await requestJson("/api/v1/ops/alerts/check", { method: "POST" });
    message.textContent = result.active_count ? `发现 ${result.active_count} 项运行告警` : "运行检查通过，没有发现活动告警";
    message.classList.toggle("error", result.status === "critical");
    await loadOperations();
  } catch (error) {
    message.textContent = error.message;
    message.classList.add("error");
  } finally {
    button.disabled = false;
  }
}

async function pollOperation(taskId) {
  for (;;) {
    const task = await requestJson(`/api/v1/ops/tasks/${encodeURIComponent(taskId)}`);
    const message = $("#operations-message");
    message.textContent = task.error || `${task.message} · ${task.progress}%`;
    message.classList.toggle("error", task.status === "failed");
    if (task.status === "succeeded" || task.status === "failed") {
      await Promise.all([loadOperations(), loadOverview()]);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

async function runOperation(kind) {
  const button = kind === "sync" ? $("#run-sync") : $("#run-backup");
  const message = $("#operations-message");
  button.disabled = true;
  message.classList.remove("error");
  try {
    const options = { method: "POST" };
    if (kind === "sync") {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify({ as_of: new Date().toLocaleDateString("en-CA") });
    }
    const task = await requestJson(`/api/v1/ops/${kind}`, options);
    await pollOperation(task.task_id);
  } catch (error) {
    message.textContent = error.message;
    message.classList.add("error");
  } finally {
    button.disabled = false;
  }
}

async function loadOverview() {
  try {
    const overview = await requestJson("/api/v1/data/overview");
    const relations = overview.database.relations || {};
    $("#metric-prices").textContent = formatNumber(relations.published_daily_bar);
    $("#metric-benchmarks").textContent = formatNumber(relations.published_benchmark_daily_bar);
    $("#metric-financials").textContent = formatNumber(relations.published_financial_metric);
    $("#metric-announcements").textContent = formatNumber(relations.published_announcement);
    $("#database-path").textContent = overview.database.database_path || "尚未初始化";
    const status = $("#database-status");
    status.classList.toggle("ready", Boolean(overview.database.initialized));
    status.lastChild.textContent = overview.database.initialized ? `Schema v${overview.database.schema_version} 已连接` : "数据库未初始化";
    const labels = { stock: "股票行情", benchmark: "指数基准", announcement: "定期公告", financial: "财务报告" };
    $("#coverage-list").innerHTML = Object.entries(overview.coverage || {}).map(([key, value]) => `<span>${labels[key] || key} · ${escapeHtml(value.start_date || "—")} → ${escapeHtml(value.end_date || "—")}</span>`).join("");
  } catch (error) {
    $("#database-status").lastChild.textContent = "数据库连接失败";
    $("#database-path").textContent = error.message;
  }
}

async function loadUniverse() {
  try {
    const asOf = $("#end-date")?.value || new Date().toLocaleDateString("en-CA");
    const [overview, universe] = await Promise.all([requestJson(`/api/v1/universe/overview?as_of=${encodeURIComponent(asOf)}`), requestJson(`/api/v1/universe/build?as_of=${encodeURIComponent(asOf)}&mode=all_available`)]);
    const trust = (value) => value === "verified_history" ? "可信历史" : "观察快照，不能回填过去";
    $("#universe-industries").innerHTML = (overview.classification_systems || []).length ? overview.classification_systems.map((item) => operationRow(`${item.classification_system} · ${trust(item.history_mode)}`, `${formatNumber(item.securities)} 只 · ${formatNumber(item.rows)} 条记录`)).join("") : '<p class="empty-state">暂无行业分类，请先同步或导入可信历史。</p>';
    $("#universe-indexes").innerHTML = (overview.indexes || []).length ? overview.indexes.map((item) => operationRow(`${item.benchmark_symbol} · ${trust(item.history_mode)}`, `${formatNumber(item.securities)} 只 · ${formatNumber(item.rows)} 条记录`)).join("") : '<p class="empty-state">暂无指数成分；不会使用当前成分冒充历史。</p>';
    $("#universe-coverage").innerHTML = operationRow(`${formatNumber(universe.security_count)} 只可用股票`, `行业覆盖 ${formatPct(universe.industry_coverage)} · ${asOf}`) + Object.entries(universe.industries || {}).map(([industry,count]) => operationRow(industry, `${formatNumber(count)} 只`)).join("");
  } catch (error) { $("#universe-coverage").innerHTML = `<p class="negative">${escapeHtml(error.message)}</p>`; }
}

function historyCard(run) {
  const total = Number(run.metrics?.total_return || 0);
  const label = `${run.spec?.factor || "factor"} · ${run.spec?.rebalance === "monthly" ? "月度" : "季度"} · Top ${run.spec?.top_n || "—"}`;
  return `<button class="history-card" data-run-id="${escapeHtml(run.run_id)}"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(run.spec?.start_date || "")} 至 ${escapeHtml(run.spec?.end_date || "")}</span><span class="history-return ${total < 0 ? "negative" : ""}">${formatPct(total)}</span></button>`;
}

async function loadHistory() {
  const container = $("#history-list");
  try {
    const runs = await requestJson("/api/v1/backtest-runs");
    container.innerHTML = runs.length ? runs.map(historyCard).join("") : '<p class="empty-state">还没有历史回测。完成第一次研究后会出现在这里。</p>';
    $$(".history-card").forEach((button) => button.addEventListener("click", () => loadRun(button.dataset.runId)));
  } catch (error) {
    container.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
  }
}

function formPayload() {
  const symbols = $("#symbols").value.split(/[\s,，]+/).map((item) => item.trim()).filter(Boolean);
  const pool = selectedPool("#research-stock-pool");
  return {
    ...(pool || {symbols}),
    start_date: $("#start-date").value,
    end_date: $("#end-date").value,
    factor: $("#factor").value,
    factor_direction: $("#factor-direction").value,
    top_n: Number($("#top-n").value),
    rebalance: $("#rebalance").value,
    benchmark_symbol: "csi000300",
    adjustment: $("#adjustment").value,
    initial_capital: Number($("#initial-capital").value),
    commission_bps: Number($("#commission-bps").value),
    slippage_bps: Number($("#slippage-bps").value),
    stamp_duty_bps: Number($("#stamp-duty-bps").value),
    lot_size: Number($("#lot-size").value),
  };
}

function showTask(task) {
  $("#task-progress").hidden = false;
  $("#task-state").textContent = { queued: "等待执行", running: "正在回测", succeeded: "回测完成", failed: "回测失败" }[task.status] || task.status;
  $("#task-percent").textContent = `${task.progress}%`;
  $("#progress-bar").style.width = `${task.progress}%`;
  $("#task-message").textContent = task.error || task.message;
}

async function pollTask(taskId) {
  while (true) {
    const task = await requestJson(`/api/v1/backtests/${encodeURIComponent(taskId)}`);
    showTask(task);
    if (task.status === "succeeded") {
      await loadRun(task.run_id);
      await loadHistory();
      return;
    }
    if (task.status === "failed") throw new Error(task.error || "回测任务失败");
    await new Promise((resolve) => setTimeout(resolve, 750));
  }
}

async function submitBacktest(event) {
  event.preventDefault();
  const button = $("#submit-backtest");
  const errorBox = $("#form-error");
  errorBox.hidden = true;
  button.disabled = true;
  button.querySelector("span").textContent = "正在提交";
  try {
    const task = await requestJson("/api/v1/backtests", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(formPayload()) });
    showTask(task);
    $("#task-progress").scrollIntoView({ behavior: "smooth", block: "center" });
    await pollTask(task.task_id);
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "开始回测";
  }
}

function drawChart(series) {
  const target = $("#nav-chart");
  if (!series.length) { target.innerHTML = '<p class="empty-state">暂无净值数据</p>'; return; }
  const width = 1100, height = 300, left = 52, right = 18, top = 16, bottom = 30;
  const values = series.flatMap((row) => [Number(row.portfolio_nav), Number(row.benchmark_nav)]);
  let min = Math.min(...values), max = Math.max(...values); if (max === min) { max += .01; min -= .01; }
  const x = (index) => left + index * (width - left - right) / Math.max(series.length - 1, 1);
  const y = (value) => top + (max - value) * (height - top - bottom) / (max - min);
  const points = (key) => series.map((row, index) => `${x(index).toFixed(1)},${y(Number(row[key])).toFixed(1)}`).join(" ");
  const grids = [0, .25, .5, .75, 1].map((ratio) => { const value = max - ratio * (max - min); const yy = y(value); return `<line class="chart-grid" x1="${left}" y1="${yy}" x2="${width-right}" y2="${yy}"/><text class="chart-label" x="0" y="${yy+4}">${value.toFixed(2)}</text>`; }).join("");
  target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">${grids}<polyline class="benchmark-line" points="${points("benchmark_nav")}"/><polyline class="strategy-line" points="${points("portfolio_nav")}"/><text class="chart-label" x="${left}" y="${height-5}">${escapeHtml(series[0].date)}</text><text class="chart-label" text-anchor="end" x="${width-right}" y="${height-5}">${escapeHtml(series.at(-1).date)}</text></svg>`;
}

const tableConfigs = {
  trades: [["date","日期"],["symbol","股票"],["side","方向"],["status","状态"],["shares","数量"],["price","价格"],["notional","成交额"],["cost","成本"],["reason","说明"]],
  positions: [["date","日期"],["symbol","股票"],["shares","数量"],["close","收盘价"],["market_value","市值"],["weight","权重"],["factor_value","因子值"],["report_period_end","财报期"]],
  selections: [["signal_date","信号日"],["execution_date","执行日"],["symbol","股票"],["factor_value","因子值"],["rank","排名"],["selected","入选"],["report_period_end","财报期"],["published_at","公开时间"]],
};

function renderTable(name) {
  const rows = state.run?.[name] || [], columns = tableConfigs[name];
  $("#detail-head").innerHTML = `<tr>${columns.map(([, label]) => `<th>${label}</th>`).join("")}</tr>`;
  $("#detail-body").innerHTML = rows.length ? rows.slice().reverse().slice(0, 500).map((row) => `<tr>${columns.map(([key]) => { let value = row[key]; if (key === "weight") value = formatPct(value); if (["price","notional","cost","market_value"].includes(key) && value != null) value = Number(value).toFixed(2); return `<td>${escapeHtml(value ?? "—")}</td>`; }).join("")}</tr>`).join("") : `<tr><td colspan="${columns.length}">暂无记录</td></tr>`;
}

function renderRun(run) {
  state.run = run;
  const metrics = run.metrics || {};
  $("#results").hidden = false;
  $("#result-subtitle").textContent = `${run.spec.factor} · ${run.spec.start_date} 至 ${run.spec.end_date} · ${run.spec.rebalance === "monthly" ? "月度" : "季度"}调仓`;
  const metricItems = [["累计收益",formatPct(metrics.total_return),metrics.total_return],["基准收益",formatPct(metrics.benchmark_total_return),metrics.benchmark_total_return],["超额收益",formatPct(metrics.excess_return),metrics.excess_return],["年化收益",formatPct(metrics.annualized_return),metrics.annualized_return],["最大回撤",formatPct(metrics.max_drawdown),-metrics.max_drawdown],["夏普比率",Number(metrics.sharpe_ratio || 0).toFixed(2),metrics.sharpe_ratio]];
  $("#result-metrics").innerHTML = metricItems.map(([label,value,direction]) => `<article><span>${label}</span><strong class="${Number(direction) < 0 ? "negative" : "positive"}">${value}</strong></article>`).join("");
  const downloadLabels = {"backtest_result.xlsx":"下载 Excel","daily_nav.csv":"净值 CSV","trades.csv":"交易 CSV","positions.csv":"持仓 CSV","factor_selections.csv":"因子 CSV"};
  $("#result-actions").innerHTML = `<a href="${run.report_url}" target="_blank" rel="noreferrer">打开完整报告</a>` + Object.entries(downloadLabels).map(([file,label]) => `<a href="/api/v1/backtest-runs/${encodeURIComponent(run.run_id)}/downloads/${encodeURIComponent(file)}">${label}</a>`).join("");
  drawChart(run.series || []); renderTable(state.activeTab);
  $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadRun(runId) {
  try { renderRun(await requestJson(`/api/v1/backtest-runs/${encodeURIComponent(runId)}`)); }
  catch (error) { $("#form-error").textContent = error.message; $("#form-error").hidden = false; }
}

function labPayload() {
  const base = formPayload();
  const factors = $$(".factor-component").filter((row) => row.querySelector(".factor-enabled").checked).map((row) => ({code: row.querySelector(".factor-enabled").value, weight: Number(row.querySelector(".factor-weight").value), direction: row.querySelector(".factor-direction").value}));
  if (!factors.length) throw new Error("请至少选择一个财务因子");
  const parseNumbers = (selector) => $(selector).value.trim().split(/[\s,，]+/).filter(Boolean).map(Number);
  return {symbols: base.symbols, start_date: base.start_date, end_date: base.end_date, factors, top_n_values: parseNumbers("#lab-top-n"), rebalance_values: $("#lab-rebalances").value.split(" "), commission_bps_values: parseNumbers("#lab-commissions"), quantiles: Number($("#lab-quantiles").value), benchmark_symbol: base.benchmark_symbol, adjustment: base.adjustment, initial_capital: base.initial_capital, slippage_bps: base.slippage_bps, stamp_duty_bps: base.stamp_duty_bps, lot_size: base.lot_size, training_fraction: Number($("#lab-training").value), validation_fraction: Number($("#lab-validation").value), universe_mode: $("#lab-universe-mode").value, universe_benchmark: base.benchmark_symbol, industry_neutralization: $("#lab-industry-neutralization").value, max_industry_weight: Number($("#lab-max-industry-weight").value), require_verified_industry: $("#lab-require-verified").value === "true"};
}

function labTable(columns, rows) {
  if (!rows.length) return '<p class="empty-state">暂无研究结果</p>';
  return `<table><thead><tr>${columns.map(([, title]) => `<th>${escapeHtml(title)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map(([key, , kind]) => `<td>${escapeHtml(row[key] == null ? "—" : kind === "pct" ? formatPct(row[key]) : kind === "num" ? Number(row[key]).toFixed(4) : row[key])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

function drawIcChart(series) {
  const target = $("#lab-ic-chart"), values = series.filter((row) => row.rank_ic != null).slice(-80);
  if (!values.length) { target.innerHTML = '<p class="empty-state">暂无有效 Rank IC 截面</p>'; return; }
  const width = 1100, height = 180, mid = 90, step = (width - 30) / values.length;
  const bars = values.map((row, index) => {const value = Math.max(-1, Math.min(1, Number(row.rank_ic))), size = Math.abs(value) * 74; return `<rect x="${15 + index * step}" y="${value >= 0 ? mid - size : mid}" width="${Math.max(step - 2, 2)}" height="${size}" fill="${value >= 0 ? "#237a5a" : "#c65b52"}"><title>${escapeHtml(row.signal_date)} ${escapeHtml(row.factor)} Rank IC ${value.toFixed(3)}</title></rect>`;}).join("");
  target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-label="Rank IC 时间序列"><line x1="0" y1="${mid}" x2="${width}" y2="${mid}" stroke="#cdd5cc"/>${bars}</svg>`;
}

function renderLabRun(run) {
  $("#lab-results").hidden = false;
  $("#lab-selected").textContent = `验证集选择：${run.summary.selected_strategy} · ${run.summary.cross_section_count} 个截面 · ${run.summary.strategy_count} 组策略`;
  $("#lab-actions").innerHTML = `<a href="${escapeHtml(run.report_url)}" target="_blank" rel="noreferrer">完整研究报告</a><a href="/api/v1/factor-lab-runs/${encodeURIComponent(run.run_id)}/downloads/factor_lab.xlsx">下载 Excel</a><a href="/api/v1/factor-lab-runs/${encodeURIComponent(run.run_id)}/downloads/ic_series.csv">IC 数据</a>`;
  $("#lab-ic-table").innerHTML = labTable([["factor","因子"],["periods","截面数"],["mean_ic","平均 IC","num"],["mean_rank_ic","平均 Rank IC","num"],["ic_ir","IC IR","num"],["positive_ic_ratio","正向比例","pct"]], run.ic_summary || []);
  $("#lab-strategy-table").innerHTML = labTable([["strategy","策略"],["total_return","总收益","pct"],["excess_return","超额收益","pct"],["max_drawdown","最大回撤","pct"],["sharpe_ratio","夏普","num"],["selected_by_validation","验证入选"]], run.strategies || []);
  $("#lab-validation-table").innerHTML = labTable([["segment","分段"],["start_date","开始"],["end_date","结束"],["total_return","收益","pct"],["excess_return","超额","pct"],["sharpe_ratio","夏普","num"]], run.validation || []);
  drawIcChart(run.series || []);
}

async function loadLabRun(runId) { renderLabRun(await requestJson(`/api/v1/factor-lab-runs/${encodeURIComponent(runId)}`)); }

async function loadLabHistory() {
  try {
    const runs = await requestJson("/api/v1/factor-lab-runs");
    $("#lab-history").innerHTML = runs.length ? runs.slice(0, 8).map((run) => `<button class="lab-history-item" data-run="${escapeHtml(run.run_id)}">${escapeHtml(operationTime(run.completed_at))} · ${escapeHtml(run.summary?.strategy_count || 0)} 组策略 · ${escapeHtml(run.summary?.selected_strategy || "")}</button>`).join("") : '<p class="empty-state">还没有因子实验记录。</p>';
    $$(".lab-history-item").forEach((button) => button.addEventListener("click", () => loadLabRun(button.dataset.run)));
  } catch (error) { $("#lab-history").innerHTML = `<p class="negative">${escapeHtml(error.message)}</p>`; }
}

async function submitFactorLab(event) {
  event.preventDefault(); const button = $("#submit-factor-lab"), errorBox = $("#lab-error"); errorBox.hidden = true; button.disabled = true;
  try {
    const created = await requestJson("/api/v1/factor-lab", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(labPayload())});
    for (;;) {const task = await requestJson(`/api/v1/factor-lab/${encodeURIComponent(created.task_id)}`); showTask(task); if (task.status === "succeeded") {await loadLabRun(task.run_id); await loadLabHistory(); break;} if (task.status === "failed") throw new Error(task.error || "因子研究失败"); await new Promise((resolve) => setTimeout(resolve, 900));}
  } catch (error) {errorBox.textContent = error.message; errorBox.hidden = false;} finally {button.disabled = false;}
}

const copilotStatusLabels = {draft:"等待确认",blocked:"预检阻断",confirmed:"已经确认",running:"正在执行",succeeded:"研究完成",failed:"执行失败"};

function renderCopilotMessages(messages) {
  const target = $("#copilot-messages");
  target.innerHTML = messages.length ? messages.map((message) => `<div class="copilot-message ${escapeHtml(message.role)}"><span>${message.role === "user" ? "你" : message.role === "assistant" ? "研究助手" : "研究工具"}</span><p>${escapeHtml(message.content)}</p></div>`).join("") : '<p class="empty-state">输入研究问题后，计划预览会在这里出现。</p>';
  target.scrollTop = target.scrollHeight;
}

async function loadCopilotConversations(selectLatest = false) {
  const conversations = await requestJson("/api/v1/copilot/conversations");
  $("#copilot-conversations").innerHTML = conversations.length ? conversations.map((item) => `<button class="copilot-conversation${item.conversation_id === state.conversationId ? " active" : ""}" data-conversation="${escapeHtml(item.conversation_id)}"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(operationTime(item.updated_at))}</span></button>`).join("") : '<p class="empty-state">还没有研究对话。</p>';
  $$(".copilot-conversation").forEach((button) => button.addEventListener("click", () => loadCopilotConversation(button.dataset.conversation)));
  if (selectLatest && conversations.length && !state.conversationId) await loadCopilotConversation(conversations[0].conversation_id);
}

async function createCopilotConversation() {
  const conversation = await requestJson("/api/v1/copilot/conversations", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({title:"新研究对话"})});
  state.conversationId = conversation.conversation_id; state.copilotPlan = null;
  renderCopilotMessages([]); $("#copilot-plan-preview").hidden = true;
  await loadCopilotConversations();
  $("#copilot-question").focus();
  return conversation;
}

async function loadCopilotConversation(conversationId) {
  const conversation = await requestJson(`/api/v1/copilot/conversations/${encodeURIComponent(conversationId)}`);
  state.conversationId = conversationId; state.copilotPlan = null;
  renderCopilotMessages(conversation.messages || []);
  const plans = (conversation.messages || []).filter((item) => item.plan_id).map((item) => item.plan_id);
  if (plans.length) renderCopilotPlan(await requestJson(`/api/v1/copilot/plans/${encodeURIComponent(plans.at(-1))}`));
  else $("#copilot-plan-preview").hidden = true;
  await loadCopilotConversations();
}

function preflightHtml(preflight) {
  if (!preflight || !preflight.checks) return '<p class="negative">没有可用预检结果</p>';
  const rows = preflight.checks.map((check) => operationRow(`${check.status === "passed" ? "通过" : "缺失"} · ${check.dataset}`, check.rows != null ? `${formatNumber(check.rows)} 行` : check.available != null ? `${check.available}/${check.requested} 项` : check.history_mode || "已检查", check.status !== "passed"));
  (preflight.blockers || []).forEach((item) => rows.push(operationRow("阻断原因", item, true)));
  return rows.join("");
}

function renderCopilotPlan(plan) {
  state.copilotPlan = plan;
  $("#copilot-plan-preview").hidden = false;
  $("#copilot-plan-status").textContent = copilotStatusLabels[plan.status] || plan.status;
  $("#copilot-plan-status").className = `plan-status ${plan.status}`;
  $("#copilot-plan-title").textContent = plan.title;
  $("#copilot-tool").textContent = plan.tool_name || "未授权任何工具";
  $("#copilot-rationale").textContent = plan.rationale;
  $("#copilot-spec").textContent = JSON.stringify(plan.spec || {}, null, 2);
  $("#copilot-preflight").innerHTML = preflightHtml(plan.preflight);
  const notices = [...(plan.assumptions || []), ...(plan.warnings || []), ...((plan.preflight || {}).warnings || [])];
  $("#copilot-warnings").innerHTML = notices.map((item) => `<p>${escapeHtml(item)}</p>`).join("");
  $("#copilot-confirm").disabled = plan.status !== "draft" || !plan.preflight?.ready;
  $("#copilot-execute").disabled = plan.status !== "confirmed";
  $("#copilot-refresh").disabled = !["draft","blocked"].includes(plan.status) || !plan.tool_name;
  if (plan.task) showCopilotTask(plan.task);
  if (plan.explanation) renderCopilotExplanation(plan.explanation);
  else $("#copilot-explanation").hidden = true;
}

function showCopilotTask(task) {
  $("#copilot-task").hidden = false;
  $("#copilot-task-message").textContent = task.error || task.message;
  $("#copilot-task-progress").textContent = `${task.progress}%`;
  $("#copilot-task-bar").style.width = `${task.progress}%`;
}

function renderCopilotExplanation(explanation) {
  const target = $("#copilot-explanation"); target.hidden = false;
  target.innerHTML = `<h4>可追溯结果解释</h4><p class="copilot-conclusion">${escapeHtml(explanation.conclusion)}</p><h5>证据与产物</h5><pre>${escapeHtml(JSON.stringify(explanation.evidence || [], null, 2))}</pre><h5>限制</h5>${(explanation.limitations || []).map((item) => `<p>· ${escapeHtml(item)}</p>`).join("")}`;
}

async function submitCopilotQuestion(event) {
  event.preventDefault(); const error = $("#copilot-error"), button = $("#copilot-plan"); error.hidden = true; button.disabled = true;
  try {
    if (!state.conversationId) await createCopilotConversation();
    const plan = await requestJson(`/api/v1/copilot/conversations/${encodeURIComponent(state.conversationId)}/messages`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({content:$("#copilot-question").value})});
    $("#copilot-question").value = ""; renderCopilotPlan(plan);
    const conversation = await requestJson(`/api/v1/copilot/conversations/${encodeURIComponent(state.conversationId)}`); renderCopilotMessages(conversation.messages || []); await loadCopilotConversations();
  } catch (exc) {error.textContent = exc.message; error.hidden = false;} finally {button.disabled = false;}
}

async function refreshCopilotPreflight() {
  try {renderCopilotPlan(await requestJson(`/api/v1/copilot/plans/${encodeURIComponent(state.copilotPlan.plan_id)}/preflight`, {method:"POST"}));}
  catch (exc) {$("#copilot-error").textContent = exc.message; $("#copilot-error").hidden = false;}
}

async function confirmCopilotPlan() {
  try {renderCopilotPlan(await requestJson(`/api/v1/copilot/plans/${encodeURIComponent(state.copilotPlan.plan_id)}/confirm`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({revision:state.copilotPlan.revision})}));}
  catch (exc) {$("#copilot-error").textContent = exc.message; $("#copilot-error").hidden = false;}
}

async function executeCopilotPlan() {
  try {
    const task = await requestJson(`/api/v1/copilot/plans/${encodeURIComponent(state.copilotPlan.plan_id)}/execute`, {method:"POST"}); showCopilotTask(task); $("#copilot-execute").disabled = true;
    for (;;) {const plan = await requestJson(`/api/v1/copilot/plans/${encodeURIComponent(state.copilotPlan.plan_id)}`); renderCopilotPlan(plan); if (["succeeded","failed"].includes(plan.status)) {if (plan.status === "failed") throw new Error(plan.error || "研究执行失败"); break;} await new Promise((resolve) => setTimeout(resolve, 900));}
  } catch (exc) {$("#copilot-error").textContent = exc.message; $("#copilot-error").hidden = false;}
}

const screenFields = JSON.parse($("#screen-field-data").textContent || "[]");
const screenFieldMap = Object.fromEntries(screenFields.map((item) => [item.code, item]));
const screenOperatorLabels = {gt:">",gte:"≥",lt:"<",lte:"≤",between:"区间"};

function screenFieldOptions(selected) {
  return screenFields.map((item) => `<option value="${escapeHtml(item.code)}" ${item.code===selected?"selected":""}>${escapeHtml(item.label)} · ${escapeHtml(item.code)}</option>`).join("");
}

function addScreenCondition(defaults={field:"roe",operator:"gte",value:12,upper_value:null}) {
  if ($$(".screen-condition").length >= 12) return;
  const row=document.createElement("div"); row.className="screen-condition";
  row.innerHTML=`<label class="field"><span>字段</span><select class="screen-condition-field">${screenFieldOptions(defaults.field)}</select></label><label class="field"><span>关系</span><select class="screen-condition-operator">${Object.entries(screenOperatorLabels).map(([value,label])=>`<option value="${value}" ${value===defaults.operator?"selected":""}>${label}</option>`).join("")}</select></label><label class="field"><span>阈值</span><input class="screen-condition-value" type="number" step="any" value="${escapeHtml(defaults.value)}" required></label><label class="field screen-upper-field" ${defaults.operator!=="between"?"hidden":""}><span>上限</span><input class="screen-condition-upper" type="number" step="any" value="${defaults.upper_value??""}"></label><button class="screen-condition-remove" type="button" aria-label="删除条件">×</button>`;
  row.querySelector(".screen-condition-operator").addEventListener("change",(event)=>{const between=event.target.value==="between";row.querySelector(".screen-upper-field").hidden=!between;row.querySelector(".screen-condition-upper").required=between;});
  row.querySelector(".screen-condition-remove").addEventListener("click",()=>{if($$(".screen-condition").length>1)row.remove();});
  $("#screen-conditions").append(row);
}

function splitScreenList(selector) {
  return $(selector).value.split(/[\s,，;；]+/).map((item)=>item.trim()).filter(Boolean);
}

function screenPayload() {
  const conditions=$$(".screen-condition").map((row)=>{const operator=row.querySelector(".screen-condition-operator").value;return {field:row.querySelector(".screen-condition-field").value,operator,value:Number(row.querySelector(".screen-condition-value").value),upper_value:operator==="between"?Number(row.querySelector(".screen-condition-upper").value):null};});
  const topN=$("#screen-top-n").value.trim();
  const pool=selectedPool("#screen-stock-pool");
  return {name:$("#screen-name").value.trim(),as_of:$("#screen-as-of").value,conditions,...(pool||{symbols:splitScreenList("#screen-symbols")}),universe_mode:$("#screen-universe-mode").value,benchmark_symbol:$("#screen-benchmark").value.trim(),classification_system:"provider",include_industries:splitScreenList("#screen-include-industries"),exclude_industries:splitScreenList("#screen-exclude-industries"),require_verified_industry:$("#screen-require-industry").value==="true",missing_policy:$("#screen-missing-policy").value,sort_by:$("#screen-sort-by").value,sort_direction:$("#screen-sort-direction").value,top_n:topN?Number(topN):null};
}

function renderScreenPreflight(payload) {
  const target=$("#screen-preflight-result"), rows=[];
  rows.push(operationRow(payload.ready?"可以执行":"预检阻断",`${formatNumber(payload.candidate_count)} 只候选 · 行业覆盖 ${formatPct(payload.industry_coverage)}`,!payload.ready));
  Object.entries(payload.field_coverage||{}).forEach(([field,count])=>rows.push(operationRow(screenFieldMap[field]?.label||field,`${formatNumber(count)} / ${formatNumber(payload.candidate_count)} 只有可用值`,count===0)));
  (payload.blockers||[]).forEach((item)=>rows.push(operationRow("阻断原因",item,true)));
  (payload.warnings||[]).forEach((item)=>rows.push(operationRow("数据边界",item,false)));
  target.innerHTML=rows.join("");
}

async function preflightScreen() {
  const button=$("#screen-preflight");button.disabled=true;$("#screen-error").textContent="";
  try {const payload=await requestJson("/api/v1/screens/preflight",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(screenPayload())});renderScreenPreflight(payload);return payload;}
  catch(error){$("#screen-error").textContent=error.message;throw error;}finally{button.disabled=false;}
}

function screenValue(item,field) {
  const value=item.values?.[field];
  if(value==null)return "缺失";
  const unit=screenFieldMap[field]?.unit||"";
  return `${Number(value).toLocaleString("zh-CN",{maximumFractionDigits:4})}${unit==="PERCENT"?"%":""}`;
}

function renderScreenRun(run) {
  state.screenRun=run;const conditions=run.spec.conditions.map((item)=>item.field), selected=run.items.filter((item)=>item.selected);
  $("#screen-results").hidden=false;$("#screen-result-title").textContent=`${run.name} · ${run.spec.as_of}`;$("#screen-result-summary").textContent=`候选 ${formatNumber(run.candidate_count)} 只，最终入选 ${formatNumber(run.selected_count)} 只；筛选记录 ${run.run_id}`;
  $("#screen-download").href=`/api/v1/screens/runs/${encodeURIComponent(run.run_id)}/results.csv`;
  $("#screen-watchlist-name").value=`${run.name} · ${run.spec.as_of}`;
  $("#screen-warnings").innerHTML=(run.diagnostics.warnings||[]).map((item)=>`<p>· ${escapeHtml(item)}</p>`).join("");
  $("#screen-table").innerHTML=run.items.length?`<table><thead><tr><th>结果</th><th>股票</th><th>行业</th><th>条件值</th><th>通过 / 淘汰原因</th><th>报告期</th><th>实际公开时间</th></tr></thead><tbody>${run.items.map((item)=>{const evidence=item.evidence?.financial||{};return `<tr><td><span class="screen-result-pill ${item.selected?"pass":"fail"}">${item.selected?`入选 #${item.rank}`:"未入选"}</span></td><td>${escapeHtml(item.symbol)} · ${escapeHtml(item.name)}</td><td>${escapeHtml(item.industry||"缺失")}</td><td class="screen-fields-cell">${conditions.map((field)=>`${escapeHtml(screenFieldMap[field]?.label||field)}：${escapeHtml(screenValue(item,field))}`).join("<br>")}</td><td class="screen-evaluations">${item.evaluations.map((entry)=>`${entry.status==="passed"?"✓":"×"} ${escapeHtml(entry.label)}：${escapeHtml(entry.status==="missing"?"缺失":entry.actual)}`).join("<br>")}</td><td>${escapeHtml(String(evidence.report_period_end||"—").replace("T00:00:00",""))}</td><td>${escapeHtml(evidence.published_at||"—")}</td></tr>`;}).join("")}</tbody></table>`:'<p class="empty-state">没有可展示的候选记录。</p>';
  $("#screen-to-copilot").disabled=!selected.length;$("#screen-to-backtest").disabled=!selected.length;$("#screen-to-lab").disabled=selected.length<2;$("#screen-save-watchlist").disabled=!selected.length;
  $("#screen-results").scrollIntoView({behavior:"smooth",block:"start"});
}

async function runScreen(event) {
  event.preventDefault();const button=$("#screen-run");button.disabled=true;$("#screen-error").textContent="";
  try {const check=await preflightScreen();if(!check.ready)throw new Error((check.blockers||[]).join("；")||"覆盖预检未通过");const run=await requestJson("/api/v1/screens/runs",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(screenPayload())});renderScreenRun(run);await loadScreenHistory();}
  catch(error){$("#screen-error").textContent=error.message;}finally{button.disabled=false;}
}

async function loadScreenHistory() {
  const target=$("#screen-history");
  try {const runs=await requestJson("/api/v1/screens/runs?limit=10");target.innerHTML=runs.length?runs.map((run)=>`<button class="screen-history-item" type="button" data-run-id="${escapeHtml(run.run_id)}"><strong>${escapeHtml(run.name)}</strong><span>${escapeHtml(String(run.as_of).replace("T00:00:00",""))} · ${formatNumber(run.selected_count)}/${formatNumber(run.candidate_count)} 只 · ${escapeHtml(run.status)}</span></button>`).join(""):'<p class="empty-state">还没有筛选运行。</p>';target.querySelectorAll("button").forEach((button)=>button.addEventListener("click",async()=>renderScreenRun(await requestJson(`/api/v1/screens/runs/${encodeURIComponent(button.dataset.runId)}`))));}
  catch(error){target.innerHTML=`<p class="negative">${escapeHtml(error.message)}</p>`;}
}

async function saveScreenWatchlist() {
  if(!state.screenRun)return;const target=$("#screen-watchlist-message");
  try {const saved=await requestJson(`/api/v1/screens/runs/${encodeURIComponent(state.screenRun.run_id)}/watchlists`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:$("#screen-watchlist-name").value.trim()})});target.textContent=`已保存 ${saved.symbols.length} 只股票 · ${saved.watchlist_id}`;}
  catch(error){target.textContent=error.message;target.classList.add("negative");}
}

function sendScreenSymbols(target) {
  const symbols=(state.screenRun?.items||[]).filter((item)=>item.selected).map((item)=>item.symbol);if(!symbols.length)return;
  if(target==="backtest"){$("#research-stock-pool").value="manual";$("#symbols").disabled=false;$("#symbols").value=symbols.join(" ");$("#top-n").value=Math.min(Number($("#top-n").value)||1,symbols.length);$("#backtest").scrollIntoView({behavior:"smooth"});}
  else if(target==="lab"){$("#research-stock-pool").value="manual";$("#symbols").disabled=false;$("#symbols").value=symbols.join(" ");$("#factor-lab").scrollIntoView({behavior:"smooth"});}
  else {$("#copilot-question").value=`基于条件选股运行 ${state.screenRun.run_id} 的入选股票 ${symbols.join(" ")}，设计一个可复现的因子研究计划，并保留该筛选运行作为股票池来源。`;$("#copilot").scrollIntoView({behavior:"smooth"});}
}

const explorerLabels = {prices:"股票行情",benchmarks:"指数基准",announcements:"定期公告",financials:"财务指标"};
const reportTypeLabels = {q1:"一季报",interim:"中报",q3:"三季报",annual:"年报"};
const explorerColumns = {
  prices: [["trade_date","交易日"],["open","开盘"],["high","最高"],["low","最低"],["close","收盘"],["volume","成交量"],["amount","成交额"],["adjustment","复权"],["quality_status","质量"]],
  benchmarks: [["trade_date","交易日"],["close","收盘"],["quality_status","质量"],["provider_source","数据源"]],
  announcements: [["announcement_date","公告日"],["published_at","可用时间"],["report_period_end","报告期"],["report_type","类型"],["title","标题"],["quality_status","质量"]],
  financials: [["report_period_end","报告期"],["published_at","可用时间"],["report_type","类型"],["metric_label","指标"],["metric_value","值"],["unit","单位"],["availability_source","可用性依据"],["quality_status","质量"]],
};

function explorerValue(key, value) {
  if (value == null || value === "") return "—";
  if (key === "report_type") return reportTypeLabels[value] || value;
  if (["open","high","low","close","metric_value"].includes(key)) return Number(value).toLocaleString("zh-CN", {maximumFractionDigits:4});
  if (["volume","amount"].includes(key)) return formatNumber(value);
  return String(value).replace("T00:00:00", "");
}

function explorerQuery(page = state.explorerPage, output = "json") {
  const symbol = encodeURIComponent(state.explorerEntity?.symbol || "");
  let path;
  if (state.explorerDataset === "prices") path = `/api/v1/data/securities/${symbol}/prices`;
  else if (state.explorerDataset === "benchmarks") path = `/api/v1/data/benchmarks/${symbol}/prices`;
  else if (state.explorerDataset === "announcements") path = `/api/v1/data/securities/${symbol}/announcements`;
  else path = `/api/v1/data/securities/${symbol}/financial-metrics`;
  const query = new URLSearchParams({page:String(page), page_size:"50", output});
  if ($("#explorer-start").value) query.set("start_date", $("#explorer-start").value);
  if ($("#explorer-end").value) query.set("end_date", $("#explorer-end").value);
  if (state.explorerDataset === "prices") query.set("adjustment", $("#explorer-adjustment").value);
  if (state.explorerDataset === "announcements" && $("#explorer-report-type").value) query.set("report_type", $("#explorer-report-type").value);
  if (state.explorerDataset === "financials" && $("#explorer-metric-code").value) query.set("metric_code", $("#explorer-metric-code").value);
  return `${path}?${query}`;
}

function renderExplorerChart(items) {
  const target = $("#explorer-chart");
  if (!items.length || !["prices","benchmarks","financials"].includes(state.explorerDataset)) {target.innerHTML = ""; return;}
  const dateKey = state.explorerDataset === "financials" ? "report_period_end" : "trade_date";
  const valueKey = state.explorerDataset === "financials" ? "metric_value" : "close";
  const ordered = [...items].filter((item) => Number.isFinite(Number(item[valueKey]))).sort((a,b) => String(a[dateKey]).localeCompare(String(b[dateKey])));
  if (ordered.length < 2) {target.innerHTML = ""; return;}
  const width=900,height=190,left=52,right=16,top=12,bottom=28, values=ordered.map((row)=>Number(row[valueKey]));
  let min=Math.min(...values),max=Math.max(...values); if(min===max){min-=1;max+=1;}
  const x=(i)=>left+i*(width-left-right)/Math.max(ordered.length-1,1), y=(v)=>top+(max-v)*(height-top-bottom)/(max-min);
  const points=ordered.map((row,i)=>`${x(i).toFixed(1)},${y(Number(row[valueKey])).toFixed(1)}`).join(" ");
  target.innerHTML=`<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-label="当前页数据趋势"><line class="chart-grid" x1="${left}" y1="${y(max)}" x2="${width-right}" y2="${y(max)}"/><line class="chart-grid" x1="${left}" y1="${y(min)}" x2="${width-right}" y2="${y(min)}"/><polyline class="strategy-line" points="${points}"/><text class="chart-label" x="0" y="${y(max)+4}">${max.toFixed(2)}</text><text class="chart-label" x="0" y="${y(min)+4}">${min.toFixed(2)}</text><text class="chart-label" x="${left}" y="${height-4}">${escapeHtml(ordered[0][dateKey])}</text><text class="chart-label" text-anchor="end" x="${width-right}" y="${height-4}">${escapeHtml(ordered.at(-1)[dateKey])}</text></svg>`;
}

function explorerPagination(target, pagination, callback) {
  target.innerHTML = `<button type="button" data-direction="prev" ${pagination.page<=1?"disabled":""}>上一页</button><span>第 ${pagination.page} / ${pagination.pages} 页 · 共 ${formatNumber(pagination.total)} 条</span><button type="button" data-direction="next" ${pagination.page>=pagination.pages?"disabled":""}>下一页</button>`;
  target.querySelectorAll("button").forEach((button)=>button.addEventListener("click",()=>callback(button.dataset.direction === "prev" ? pagination.page-1 : pagination.page+1)));
}

function renderExplorerTable(payload) {
  const columns = explorerColumns[state.explorerDataset];
  const items = payload.items || [];
  const hasDetail = ["announcements","financials"].includes(state.explorerDataset);
  $("#explorer-table").innerHTML = items.length ? `<table><thead><tr>${columns.map(([,label])=>`<th>${label}</th>`).join("")}${hasDetail?"<th>内容</th>":""}<th>证据</th></tr></thead><tbody>${items.map((row)=>`<tr>${columns.map(([key])=>`<td>${key === "quality_status" ? `<span class="quality-pill">${escapeHtml(explorerValue(key,row[key]))}</span>` : escapeHtml(explorerValue(key,row[key]))}</td>`).join("")}${hasDetail?`<td><button class="record-detail-button" type="button" data-detail-id="${escapeHtml(state.explorerDataset==="financials"?row.report_id:row.record_id)}">${state.explorerDataset==="financials"?"查看财报":"查看公告"}</button></td>`:""}<td><button class="lineage-button" type="button" data-record-id="${escapeHtml(row.record_id)}">血缘</button></td></tr>`).join("")}</tbody></table>` : '<p class="empty-state">筛选范围内没有已发布记录；不会以供应商回退数据静默填充。</p>';
  $("#explorer-notice").textContent = payload.point_in_time_notice || `${explorerLabels[state.explorerDataset]}仅展示已经通过质量发布的数据。`;
  $("#explorer-download").href = explorerQuery(payload.pagination.page, "csv");
  renderExplorerChart(items);
  explorerPagination($("#explorer-pages"), payload.pagination, loadExplorerRecords);
  $$(".lineage-button").forEach((button)=>button.addEventListener("click",()=>showLineage(button.dataset.recordId)));
  $$(".record-detail-button").forEach((button)=>button.addEventListener("click",()=>showRecordDetail(state.explorerDataset,button.dataset.detailId)));
  if (state.explorerDataset === "financials") {
    const select=$("#explorer-metric-code"), current=select.value;
    select.innerHTML='<option value="">全部指标</option>'+(payload.metric_codes||[]).map((code)=>`<option value="${escapeHtml(code)}">${escapeHtml(code)}</option>`).join("");
    select.value=current;
  }
}

function safeExternalUrl(value) {
  if (!value) return null;
  try {const url=new URL(value);return ["http:","https:"].includes(url.protocol)?url.href:null;}
  catch (_) {return null;}
}

function detailGrid(rows) {
  return `<div class="record-detail-grid">${rows.map(([label,value])=>`<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value??"—")}</strong></div>`).join("")}</div>`;
}

function renderAnnouncementDetail(payload) {
  const item=payload.announcement||{}, documentMeta=payload.document||{}, url=safeExternalUrl(documentMeta.url);
  $("#record-detail-eyebrow").textContent="ANNOUNCEMENT DETAIL · POINT-IN-TIME";
  $("#record-detail-title").textContent=item.title||"公告详情";
  const reports=payload.related_financial_reports||[];
  $("#record-detail-content").innerHTML=`<p class="record-detail-lead">${escapeHtml(item.symbol)} · ${escapeHtml(item.name)} · ${escapeHtml(reportTypeLabels[item.report_type]||item.report_type||"—")}</p>${detailGrid([["报告期",item.report_period_end],["公告日期",item.announcement_date],["实际公开时间",item.published_at],["报告年度",item.report_year],["质量状态",item.quality_status],["供应商",item.provider_source]])}<section class="record-document"><h4>公告原文</h4><p>${escapeHtml(documentMeta.notice||"")}</p>${url?`<a class="primary-button record-document-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">打开公告原文 <b>↗</b></a>`:'<span class="quality-pill">仅有本地元数据</span>'}<code>内容摘要：${escapeHtml(documentMeta.content_sha256||"未记录")}</code></section><section class="record-detail-section"><h4>对应财务报告</h4>${reports.length?`<div class="record-related-list">${reports.map((report)=>`<button type="button" data-related-report="${escapeHtml(report.report_id)}"><strong>${escapeHtml(reportTypeLabels[report.report_type]||report.report_type)} · ${escapeHtml(report.report_period_end)}</strong><span>${escapeHtml(report.published_at)} · ${formatNumber(report.metric_count)} 项指标</span></button>`).join("")}</div>`:'<p class="empty-state">本地库尚未匹配到同报告期、同类型的财务指标记录。</p>'}</section><p class="explorer-pit-notice">${escapeHtml(payload.point_in_time_notice||"")}</p><details class="record-evidence"><summary>查看数据证据</summary><code>记录 ID：${escapeHtml(item.record_id)}\n服务层：${escapeHtml(item.serving_source)}\n批次：${escapeHtml(item.source_batch_id)}\n原始文件：${escapeHtml(item.raw_path||"未记录")}</code></details>`;
  $$("[data-related-report]").forEach((button)=>button.addEventListener("click",()=>showRecordDetail("financials",button.dataset.relatedReport)));
  const intelligence=document.createElement("section");
  intelligence.id="document-intelligence";
  intelligence.className="record-detail-section document-intelligence";
  intelligence.innerHTML='<h4>文档智能</h4><p class="empty-state">正在检查本地归档、正文和版本…</p>';
  $("#record-detail-content").appendChild(intelligence);
  loadDocumentIntelligence(item.record_id,item.symbol);
}

function renderDocumentAnswer(answer) {
  const citations=answer.citations||[];
  $("#document-answer").innerHTML=`<div class="document-answer-status ${answer.status==='answered'?'positive':'negative'}">${escapeHtml(answer.status)}</div><p>${escapeHtml(answer.answer||"没有生成答案")}</p>${citations.length?`<ol>${citations.map((item)=>`<li><button type="button" data-citation-document="${escapeHtml(item.document_id)}" data-citation-page="${escapeHtml(item.page_number)}">第 ${escapeHtml(item.page_number)} 页</button><blockquote>${escapeHtml(item.quote)}</blockquote><small>${escapeHtml(item.claim)}</small></li>`).join("")}</ol>`:""}${(answer.limitations||[]).map((item)=>`<small class="document-limitation">${escapeHtml(item)}</small>`).join("")}`;
  $$("[data-citation-page]").forEach((button)=>button.addEventListener("click",()=>loadDocumentPage(button.dataset.citationDocument,Number(button.dataset.citationPage))));
}

async function loadDocumentPage(documentId,pageNumber) {
  const target=$("#document-page-reader");
  target.innerHTML='<p class="empty-state">正在读取页码正文…</p>';
  try {
    const page=await requestJson(`/api/v1/documents/${encodeURIComponent(documentId)}/pages/${pageNumber}`);
    target.innerHTML=`<div class="document-page-head"><strong>第 ${escapeHtml(page.page_number)} 页</strong><span>${escapeHtml(page.extraction_method)} · ${formatNumber(page.char_count)} 字符</span></div><pre>${escapeHtml(page.text||"本页没有可解析正文")}</pre>`;
  } catch(error){target.innerHTML=`<p class="negative">${escapeHtml(error.message)}</p>`;}
}

function renderDocumentEvents(documentId,events) {
  const target=$("#document-events"); if(!target) return;
  target.innerHTML=`<div class="document-events-head"><div><h5>结构化事件</h5><p>离线规则提取；每项事实必须经过逐字引用校验和人工审核。</p></div><button id="document-event-extract" class="secondary-button" type="button">提取 / 刷新事件</button></div><p id="document-event-task-status" class="empty-state"></p><div class="document-event-list">${events.length?events.map((item)=>`<article class="document-event-card"><header><strong>${escapeHtml(documentEventTypeLabels[item.event_type]||item.event_type)}</strong><span class="quality-pill">${escapeHtml(item.status)}</span></header>${detailGrid([["公开时间",item.published_at],["事件日期",item.event_date||"未明确"],["方向",item.direction],["金额",item.amount==null?"未明确":formatNumber(item.amount)],["币种",item.currency||"—"],["比例",item.ratio==null?"未明确":`${(Number(item.ratio)*100).toFixed(2)}%`]])}<div class="document-event-evidence">${(item.evidence||[]).map((evidence)=>`<button type="button" data-event-page="${escapeHtml(evidence.page_number)}"><small>${escapeHtml(evidence.field_name)} · 第 ${escapeHtml(evidence.page_number)} 页</small><q>${escapeHtml(evidence.quote)}</q></button>`).join("")||'<p class="negative">没有通过逐字校验的证据。</p>'}</div><div class="document-event-actions">${item.status==="needs_review"?`<button type="button" class="secondary-button" data-event-review="accepted" data-event-id="${escapeHtml(item.event_id)}">接受</button><button type="button" class="secondary-button" data-event-review="rejected" data-event-id="${escapeHtml(item.event_id)}">拒绝</button>`:""}${item.status==="accepted"?`<button type="button" class="primary-button" data-event-study="${escapeHtml(item.event_id)}">运行事件研究 <b>→</b></button>`:""}</div><div id="event-study-${escapeHtml(item.event_id)}"></div></article>`).join(""):'<p class="empty-state">尚未提取到受支持的结构化事件。</p>'}</div>`;
  $("#document-event-extract").addEventListener("click",()=>extractDocumentEvents(documentId));
  $$('[data-event-page]').forEach((button)=>button.addEventListener("click",()=>loadDocumentPage(documentId,Number(button.dataset.eventPage))));
  $$('[data-event-review]').forEach((button)=>button.addEventListener("click",()=>reviewDocumentEvent(button.dataset.eventId,button.dataset.eventReview,documentId)));
  $$('[data-event-study]').forEach((button)=>button.addEventListener("click",()=>runDocumentEventStudy(button.dataset.eventStudy)));
}

async function loadDocumentEvents(documentId) {
  const target=$("#document-events"); if(!target) return;
  target.innerHTML='<p class="empty-state">正在读取结构化事件和证据…</p>';
  try {const payload=await requestJson(`/api/v1/documents/${encodeURIComponent(documentId)}/events`);renderDocumentEvents(documentId,payload.events||[]);}
  catch(error){target.innerHTML=`<p class="negative">${escapeHtml(error.message)}</p>`;}
}

async function extractDocumentEvents(documentId) {
  const target=$("#document-event-task-status")||$("#document-events");
  try {const task=await requestJson(`/api/v1/documents/${encodeURIComponent(documentId)}/events/extract`,{method:"POST"});for(;;){const current=await requestJson(`/api/v1/ops/tasks/${encodeURIComponent(task.task_id)}`);target.textContent=`${current.message} · ${current.progress}%`;if(["succeeded","failed"].includes(current.status)){if(current.status==="failed") throw new Error(current.error||"事件提取失败");break;}await new Promise((resolve)=>setTimeout(resolve,1000));}await loadDocumentEvents(documentId);await loadOperations();}
  catch(error){target.innerHTML=`<span class="negative">${escapeHtml(error.message)}</span>`;}
}

async function reviewDocumentEvent(eventId,status,documentId) {
  try {await requestJson(`/api/v1/document-events/${encodeURIComponent(eventId)}/review`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({status,note:"Web 工作台审核"})});await loadDocumentEvents(documentId);}
  catch(error){window.alert(error.message);}
}

async function runDocumentEventStudy(eventId) {
  const target=$(`#event-study-${eventId}`);target.innerHTML='<p class="empty-state">正在按公开时间运行事件研究…</p>';
  try {const result=await requestJson(`/api/v1/document-events/${encodeURIComponent(eventId)}/event-study`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({source:"local",windows:[5,10,20],adjustment:"qfq",round_trip_cost_bps:20})});target.innerHTML=`<h6>事件研究结果</h6>${detailGrid((result.events||[]).map((row)=>[`${row.window} 日超额收益`,formatPct(row.excess_return)]))}<small>入场：公告公开后的首个股票与基准共同交易日；as_of ${escapeHtml(result.as_of)}</small>`;}
  catch(error){target.innerHTML=`<p class="negative">${escapeHtml(error.message)}</p>`;}
}

async function renderDocumentWorkspace(announcementId,symbol,documents) {
  const target=$("#document-intelligence"), ready=documents.filter((item)=>item.status==="ready");
  if(!documents.length){
    target.innerHTML='<h4>文档智能</h4><p>尚未在本地归档此公告 PDF。归档任务会校验文件、保存版本、解析页码并在必要时 OCR。</p><button id="document-ingest" class="primary-button" type="button">归档并解析 <b>→</b></button><p id="document-task-status" class="empty-state"></p>';
    $("#document-ingest").addEventListener("click",()=>ingestDocument(announcementId,symbol));
    return;
  }
  const selected=(ready[0]||documents[0]).document_id;
  const comparable=await requestJson(`/api/v1/documents/securities/${encodeURIComponent(symbol)}`).catch(()=>[]);
  target.innerHTML=`<div class="document-intelligence-head"><div><h4>文档智能</h4><p>本地 PDF、正文页码、全文检索及逐字引用问答</p></div><button id="document-reingest" class="secondary-button" type="button">检查新版本</button></div><label class="field"><span>本地版本</span><select id="document-version">${documents.map((item)=>`<option value="${escapeHtml(item.document_id)}">v${escapeHtml(item.version_no)} · ${escapeHtml(item.status)} · ${escapeHtml(item.extraction_method)} · ${formatNumber(item.page_count)} 页</option>`).join("")}</select></label><div id="document-version-meta"></div>${ready.length?`<div class="document-tool-grid"><form id="document-search-form"><label class="field"><span>全文搜索</span><input id="document-search-query" maxlength="200" placeholder="例如：营业收入、现金流、风险" required></label><button class="secondary-button" type="submit">搜索正文</button></form><form id="document-question-form"><label class="field"><span>引用式问答</span><textarea id="document-question" maxlength="2000" required placeholder="这份财报披露了哪些主要经营变化？"></textarea></label><button class="primary-button" type="submit">基于原文回答 <b>→</b></button></form></div><div id="document-search-results"></div><div id="document-answer"></div><div id="document-page-reader" class="document-page-reader"><p class="empty-state">点击搜索结果或答案引文查看对应页正文。</p></div><section id="document-events" class="document-events"><p class="empty-state">正在读取结构化事件和证据…</p></section><details class="document-compare"><summary>多期与修订版本比较</summary><div class="document-compare-list">${comparable.map((item)=>`<label><input type="checkbox" value="${escapeHtml(item.document_id)}" ${item.document_id===selected?'checked':''}><span>${escapeHtml(item.report_period_end)} · ${escapeHtml(reportTypeLabels[item.report_type]||item.report_type)} · v${escapeHtml(item.version_no)}</span></label>`).join("")}</div><label class="field"><span>比较重点</span><input id="document-compare-focus" maxlength="500" value="比较主要财务表现、经营变化、风险和修订差异"></label><button id="document-compare-run" class="secondary-button" type="button">比较所选文档</button></details>`:'<p class="empty-state">已有归档版本正在处理或处理失败；可查看状态并重新检查。</p>'}`;
  const selector=$("#document-version"); selector.value=selected;
  function showVersion(){const item=documents.find((row)=>row.document_id===selector.value)||documents[0];$("#document-version-meta").innerHTML=`${detailGrid([["状态",item.status],["解析",item.extraction_method],["页数",item.page_count],["大小",`${(Number(item.size_bytes)/1024/1024).toFixed(2)} MB`],["SHA-256",item.sha256],["归档时间",item.downloaded_at]])}<a class="secondary-button" target="_blank" rel="noopener" href="${escapeHtml(item.file_url)}">打开本地 PDF ↗</a>${item.error?`<p class="negative">${escapeHtml(item.error)}</p>`:""}`;}
  selector.addEventListener("change",()=>{showVersion();loadDocumentEvents(selector.value);}); showVersion();
  $("#document-reingest").addEventListener("click",()=>ingestDocument(announcementId,symbol));
  if(!ready.length) return;
  loadDocumentEvents(selected);
  $("#document-search-form").addEventListener("submit",async(event)=>{event.preventDefault();const documentId=selector.value,query=$("#document-search-query").value,resultTarget=$("#document-search-results");resultTarget.innerHTML='<p class="empty-state">正在搜索…</p>';try{const result=await requestJson(`/api/v1/documents/${encodeURIComponent(documentId)}/search?q=${encodeURIComponent(query)}`);resultTarget.innerHTML=result.items.length?`<h5>搜索结果</h5>${result.items.map((item)=>`<button class="document-search-hit" type="button" data-search-page="${escapeHtml(item.page_number)}"><strong>第 ${escapeHtml(item.page_number)} 页 · 相关度 ${escapeHtml(item.score)}</strong><span>${escapeHtml(item.text)}</span></button>`).join("")}`:'<p class="empty-state">没有找到匹配正文。</p>';$$("[data-search-page]").forEach((button)=>button.addEventListener("click",()=>loadDocumentPage(documentId,Number(button.dataset.searchPage))));}catch(error){resultTarget.innerHTML=`<p class="negative">${escapeHtml(error.message)}</p>`;}});
  $("#document-question-form").addEventListener("submit",async(event)=>{event.preventDefault();const answerTarget=$("#document-answer");answerTarget.innerHTML='<p class="empty-state">正在检索证据并校验引用…</p>';try{renderDocumentAnswer(await requestJson("/api/v1/documents/questions",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({document_ids:[selector.value],question:$("#document-question").value})}));}catch(error){answerTarget.innerHTML=`<p class="negative">${escapeHtml(error.message)}</p>`;}});
  $("#document-compare-run").addEventListener("click",async()=>{const ids=$$(".document-compare-list input:checked").map((item)=>item.value),answerTarget=$("#document-answer");if(ids.length<2){answerTarget.innerHTML='<p class="negative">至少选择两个已解析文档。</p>';return;}answerTarget.innerHTML='<p class="empty-state">正在比较多个期间或修订版本…</p>';try{renderDocumentAnswer(await requestJson("/api/v1/documents/compare",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({document_ids:ids,focus:$("#document-compare-focus").value})}));}catch(error){answerTarget.innerHTML=`<p class="negative">${escapeHtml(error.message)}</p>`;}});
}

async function loadDocumentIntelligence(announcementId,symbol) {
  try {const payload=await requestJson(`/api/v1/documents/announcements/${encodeURIComponent(announcementId)}`);await renderDocumentWorkspace(announcementId,symbol,payload.documents||[]);}
  catch(error){$("#document-intelligence").innerHTML=`<h4>文档智能</h4><p class="negative">${escapeHtml(error.message)}</p>`;}
}

async function ingestDocument(announcementId,symbol) {
  const target=$("#document-task-status")||$("#document-intelligence");
  try {
    const task=await requestJson(`/api/v1/documents/announcements/${encodeURIComponent(announcementId)}/ingest`,{method:"POST"});
    for(;;){const current=await requestJson(`/api/v1/ops/tasks/${encodeURIComponent(task.task_id)}`);target.textContent=`${current.message} · ${current.progress}%`;if(["succeeded","failed"].includes(current.status)){if(current.status==="failed") throw new Error(current.error||"文档任务失败");break;}await new Promise((resolve)=>setTimeout(resolve,1000));}
    await loadDocumentIntelligence(announcementId,symbol); await loadOperations();
  } catch(error){target.innerHTML=`<span class="negative">${escapeHtml(error.message)}</span>`;}
}

function renderFinancialReportDetail(payload) {
  const report=payload.report||{}, metrics=payload.metrics||[], revisions=payload.revisions||[], announcements=payload.related_announcements||[];
  $("#record-detail-eyebrow").textContent="FINANCIAL REPORT · IMMUTABLE VERSION";
  $("#record-detail-title").textContent=`${report.symbol||""} · ${report.name||""} ${reportTypeLabels[report.report_type]||report.report_type||"财报"}`;
  $("#record-detail-content").innerHTML=`${detailGrid([["报告期",report.report_period_end],["实际公开时间",report.published_at],["可用时间依据",report.availability_source],["指标数量",metrics.length],["质量状态",report.quality_status],["供应商",report.provider_source]])}<section class="record-detail-section"><h4>全部财务指标</h4>${metrics.length?`<div class="table-shell record-metric-table"><table><thead><tr><th>指标</th><th>代码</th><th>数值</th><th>单位</th><th>质量</th></tr></thead><tbody>${metrics.map((metric)=>`<tr><td>${escapeHtml(metric.metric_label)}</td><td><code>${escapeHtml(metric.metric_code)}</code></td><td>${escapeHtml(explorerValue("metric_value",metric.metric_value))}</td><td>${escapeHtml(metric.unit)}</td><td><span class="quality-pill">${escapeHtml(metric.quality_status)}</span></td></tr>`).join("")}</tbody></table></div>`:'<p class="empty-state">该财报版本没有已发布指标，缺失值不会填零。</p>'}</section><section class="record-detail-columns"><div><h4>同报告期版本</h4><div class="record-version-list">${revisions.map((revision)=>`<div class="${revision.current?"current":""}"><strong>${revision.current?"当前查看 · ":""}${escapeHtml(revision.published_at)}</strong><span>${formatNumber(revision.metric_count)} 项 · ${escapeHtml(revision.availability_source)}</span></div>`).join("")}</div></div><div><h4>对应定期公告</h4>${announcements.length?`<div class="record-related-list">${announcements.map((item)=>`<button type="button" data-related-announcement="${escapeHtml(item.record_id)}"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.published_at)}</span></button>`).join("")}</div>`:'<p class="empty-state">本地库尚未匹配到同报告期公告。</p>'}</div></section><p class="explorer-pit-notice">${escapeHtml(payload.point_in_time_notice||"")}</p><details class="record-evidence"><summary>查看数据证据</summary><code>报告 ID：${escapeHtml(report.record_id)}\n服务层：${escapeHtml(report.serving_source)}\n批次：${escapeHtml(report.source_batch_id)}\n原始文件：${escapeHtml(report.raw_path||"未记录")}\n原始摘要：${escapeHtml(report.raw_sha256||"未记录")}</code></details>`;
  $$("[data-related-announcement]").forEach((button)=>button.addEventListener("click",()=>showRecordDetail("announcements",button.dataset.relatedAnnouncement)));
}

async function showRecordDetail(dataset, recordId) {
  const dialog=$("#record-detail-dialog");
  if(!dialog.open) dialog.showModal();
  $("#record-detail-title").textContent="正在读取已发布记录…";
  $("#record-detail-content").innerHTML='<p class="empty-state">正在聚合元数据、指标、版本和对应公告…</p>';
  try {
    const path=dataset==="announcements"?`/api/v1/data/announcements/${encodeURIComponent(recordId)}`:`/api/v1/data/financial-reports/${encodeURIComponent(recordId)}`;
    const payload=await requestJson(path);
    if(dataset==="announcements") renderAnnouncementDetail(payload); else renderFinancialReportDetail(payload);
  } catch(error){$("#record-detail-content").innerHTML=`<p class="negative">${escapeHtml(error.message)}</p>`;}
}

async function loadExplorerRecords(page=1) {
  if (!state.explorerEntity) return;
  const generation=state.explorerGeneration, selection=state.explorerSelection, request=++state.explorerRecordRequest;
  const current=()=>generation===state.explorerGeneration && selection===state.explorerSelection && request===state.explorerRecordRequest;
  state.explorerPage=page; $("#explorer-table").innerHTML='<p class="empty-state">正在读取已发布记录…</p>';
  try {const payload=await requestJson(explorerQuery(page)); if(current()) renderExplorerTable(payload);}
  catch(error){if(current()) $("#explorer-table").innerHTML=`<p class="negative">${escapeHtml(error.message)}</p>`;}
}

function renderSecurityIdentity(item) {
  const coverage=(item.price_coverage||[]).map((row)=>`${row.adjustment}: ${row.start_date||"—"} → ${row.end_date||"—"} · ${formatNumber(row.rows)} 行${row.missing_trading_days==null?"":` · 缺 ${formatNumber(row.missing_trading_days)} 日`}`).join("；");
  $("#explorer-identity").innerHTML=`<p class="eyebrow">SECURITY DETAIL</p><h3>${escapeHtml(item.symbol)} · ${escapeHtml(item.name)}</h3><p class="muted">${escapeHtml(item.exchange)} · ${escapeHtml(item.status)}${item.industry_name?` · ${escapeHtml(item.industry_name)}`:" · 行业缺失"}</p><div class="explorer-identity-grid"><div><span>上市 / 退市</span><strong>${escapeHtml(item.list_date||"—")} / ${escapeHtml(item.delist_date||"—")}</strong></div><div><span>行情记录</span><strong>${formatNumber((item.price_coverage||[]).reduce((sum,row)=>sum+Number(row.rows||0),0))} 行</strong></div><div><span>定期公告</span><strong>${formatNumber(item.announcement_count)} 条</strong></div><div><span>财务报告 / 指标</span><strong>${formatNumber(item.report_count)} / ${formatNumber(item.metric_count)}</strong></div><div><span>未通过质量检查</span><strong class="${item.failed_quality_checks?"negative":"positive"}">${formatNumber(item.failed_quality_checks)}</strong></div></div><p class="explorer-coverage-note">${escapeHtml(coverage||"暂无行情覆盖")}</p><p class="explorer-pit-notice">${escapeHtml(item.point_in_time_notice||"")}</p>`;
}

async function selectExplorerEntity(item) {
  const generation=state.explorerGeneration, selection=++state.explorerSelection;
  state.explorerEntity=item; state.explorerPage=1;
  $("#explorer-identity").innerHTML='<p class="empty-state">正在读取所选对象…</p>';
  $("#explorer-table").innerHTML='<p class="empty-state">正在读取所选对象的记录…</p>';
  $("#explorer-chart").innerHTML=""; $("#explorer-pages").innerHTML="";
  $("#explorer-download").removeAttribute("href");
  $$(".explorer-entity").forEach((button)=>button.classList.toggle("active",button.dataset.symbol===item.symbol));
  if (state.explorerDataset === "benchmarks") $("#explorer-identity").innerHTML=`<p class="eyebrow">BENCHMARK DETAIL</p><h3>${escapeHtml(item.symbol)} · ${escapeHtml(item.name)}</h3><div class="explorer-identity-grid"><div><span>覆盖日期</span><strong>${escapeHtml(item.start_date||"—")} → ${escapeHtml(item.end_date||"—")}</strong></div><div><span>行情记录</span><strong>${formatNumber(item.price_rows)} 行</strong></div><div><span>同步批次</span><strong>${formatNumber(item.batches)}</strong></div><div><span>供应商</span><strong>${escapeHtml(item.provider_source||"—")}</strong></div></div>`;
  else {
    try {
      const identity=await requestJson(`/api/v1/data/securities/${encodeURIComponent(item.symbol)}`);
      if(generation!==state.explorerGeneration || selection!==state.explorerSelection) return;
      renderSecurityIdentity(identity);
    } catch(error) {
      if(generation===state.explorerGeneration && selection===state.explorerSelection) $("#explorer-identity").innerHTML=`<p class="negative">${escapeHtml(error.message)}</p>`;
      return;
    }
  }
  await loadExplorerRecords(1);
}

async function loadExplorerEntities(page=1) {
  const generation=++state.explorerGeneration;
  const selectedSymbol=state.explorerEntity?.symbol;
  state.explorerEntityPage=page; state.explorerEntity=null;
  $("#explorer-identity").innerHTML='<p class="empty-state">从左侧选择股票或指数。</p>';
  $("#explorer-table").innerHTML='<p class="empty-state">尚未选择数据。</p>';
  $("#explorer-chart").innerHTML="";
  $("#explorer-pages").innerHTML=""; $("#explorer-entity-pages").innerHTML="";
  $("#explorer-download").removeAttribute("href");
  const target=$("#explorer-entity-list"); target.innerHTML='<p class="empty-state">正在读取可用对象…</p>';
  try {
    const query=new URLSearchParams({page:String(page),page_size:"30"});
    let url="/api/v1/data/benchmarks";
    if(state.explorerDataset!=="benchmarks"){query.set("dataset",state.explorerDataset);query.set("query",$("#explorer-search").value.trim());if(state.explorerDataset==="prices") query.set("managed_only","true");url="/api/v1/data/securities";}
    const payload=await requestJson(`${url}?${query}`), items=payload.items||[];
    if(generation!==state.explorerGeneration) return;
    target.innerHTML=items.length?items.map((item)=>`<button class="explorer-entity" type="button" data-symbol="${escapeHtml(item.symbol)}"><strong>${escapeHtml(item.symbol)} · ${escapeHtml(item.name)}</strong><span>${state.explorerDataset==="benchmarks"?`${formatNumber(item.price_rows)} 行 · ${escapeHtml(item.start_date||"—")} → ${escapeHtml(item.end_date||"—")}`:`行情 ${formatNumber(item.price_rows)} · 公告 ${formatNumber(item.announcement_count)} · 财报 ${formatNumber(item.report_count)}`}</span></button>`).join(""):'<p class="empty-state">没有匹配的已发布数据。</p>';
    target.querySelectorAll("button").forEach((button)=>button.addEventListener("click",()=>selectExplorerEntity(items.find((item)=>item.symbol===button.dataset.symbol))));
    explorerPagination($("#explorer-entity-pages"),payload.pagination,loadExplorerEntities);
    if(items.length) await selectExplorerEntity(items.find((item)=>item.symbol===selectedSymbol)||items[0]);
  } catch(error){if(generation===state.explorerGeneration) target.innerHTML=`<p class="negative">${escapeHtml(error.message)}</p>`;}
}

async function openExplorer(dataset="prices", {scroll=true}={}) {
  state.explorerDataset=dataset; state.explorerPage=1;
  $("#data-explorer").hidden=false;
  $$("[data-explorer-dataset]").forEach((button)=>button.classList.toggle("active",button.dataset.explorerDataset===dataset && button.closest(".explorer-tabs")));
  $("#explorer-adjustment-field").hidden=dataset!=="prices";
  $("#explorer-report-field").hidden=dataset!=="announcements";
  $("#explorer-metric-field").hidden=dataset!=="financials";
  $("#explorer-search").disabled=dataset==="benchmarks";
  $("#explorer-record-title").textContent=`${explorerLabels[dataset]}记录`;
  await loadExplorerEntities(1);
  if(scroll && state.explorerDataset===dataset) $("#data-explorer").scrollIntoView({behavior:"smooth",block:"start"});
}

async function initializeDataExplorer() {
  // Initial page reads are local GETs only. Do not scroll away from a deep link
  // or require a click on the navigation item to make already stored data visible.
  await Promise.all([
    openExplorer("prices", {scroll:false}),
    typeof loadManagedStocks === "function" ? loadManagedStocks(1) : Promise.resolve(),
  ]);
}

async function showLineage(recordId) {
  const dialog=$("#lineage-dialog"); dialog.showModal(); $("#lineage-content").innerHTML='<p class="empty-state">正在读取记录证据…</p>';
  try {const payload=await requestJson(`/api/v1/data/lineage/${encodeURIComponent(state.explorerDataset)}/${encodeURIComponent(recordId)}`), lineage=payload.lineage||{}; $("#lineage-content").innerHTML=`<div class="lineage-grid"><div><span>记录 ID</span><code>${escapeHtml(recordId)}</code></div><div><span>服务层来源</span><code>${escapeHtml(lineage.serving_source||"—")}</code></div><div><span>供应商来源</span><code>${escapeHtml(lineage.provider_source||"—")}</code></div><div><span>同步批次</span><code>${escapeHtml(lineage.source_batch_id||"—")}</code></div><div><span>原始文件</span><code>${escapeHtml(lineage.raw_path||"未记录")}</code></div><div><span>质量 / 发布时间</span><code>${escapeHtml(lineage.quality_status||"—")} · ${escapeHtml(lineage.published_at||"—")}</code></div></div>`;}
  catch(error){$("#lineage-content").innerHTML=`<p class="negative">${escapeHtml(error.message)}</p>`;}
}

$("#screen-as-of").value=new Date().toLocaleDateString("en-CA");
addScreenCondition({field:"roe",operator:"gte",value:12});
addScreenCondition({field:"revenue_yoy",operator:"gte",value:10});
addScreenCondition({field:"debt_asset_ratio",operator:"lt",value:60});
$("#screen-sort-by").value="roe";
$("#screen-form").addEventListener("submit",runScreen);
$("#screen-preflight").addEventListener("click",()=>preflightScreen().catch(()=>{}));
$("#screen-add-condition").addEventListener("click",()=>addScreenCondition({field:"gross_margin",operator:"gte",value:20}));
$("#screen-refresh-history").addEventListener("click",loadScreenHistory);
$("#screen-save-watchlist").addEventListener("click",saveScreenWatchlist);
$("#screen-to-copilot").addEventListener("click",()=>sendScreenSymbols("copilot"));
$("#screen-to-backtest").addEventListener("click",()=>sendScreenSymbols("backtest"));
$("#screen-to-lab").addEventListener("click",()=>sendScreenSymbols("lab"));
$("#stock-pool-refresh").addEventListener("click",()=>loadStockPools());
$("#security-master-refresh").addEventListener("click", refreshSecurityMaster);
$("#stock-pool-create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const created = await stockPoolMutation("/api/v1/stock-pools", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:$("#stock-pool-create-name").value,description:$("#stock-pool-create-description").value,symbols:splitStockSymbols($("#stock-pool-create-symbols").value),source_type:"manual"})});
  if (created) event.target.reset();
});
$("#research-stock-pool").addEventListener("change",async()=>{const pool=selectedPool("#research-stock-pool");$("#symbols").disabled=Boolean(pool);if(!pool){$("#research-stock-pool-preview").textContent="手动输入不会被视为历史指数成分。";return;}const detail=await requestJson(`/api/v1/stock-pools/${encodeURIComponent(pool.stock_pool_id)}?revision=${pool.stock_pool_revision}`);$("#research-stock-pool-preview").textContent=`冻结 r${pool.stock_pool_revision} · ${detail.symbols.length} 只：${detail.symbols.join(" ")}`;});
$("#screen-stock-pool").addEventListener("change",()=>{$("#screen-symbols").disabled=Boolean(selectedPool("#screen-stock-pool"));});
$("#backtest-form").addEventListener("submit", submitBacktest);
$("#copilot-form").addEventListener("submit", submitCopilotQuestion);
$("#copilot-new").addEventListener("click", createCopilotConversation);
$("#copilot-refresh").addEventListener("click", refreshCopilotPreflight);
$("#copilot-confirm").addEventListener("click", confirmCopilotPlan);
$("#copilot-execute").addEventListener("click", executeCopilotPlan);
$("#factor-lab-form").addEventListener("submit", submitFactorLab);
$("#refresh-lab-history").addEventListener("click", loadLabHistory);
$("#refresh-universe").addEventListener("click", loadUniverse);
$("#refresh-history").addEventListener("click", loadHistory);
$("#refresh-operations").addEventListener("click", loadOperations);
$("#run-health-check").addEventListener("click", checkOperations);
$("#run-sync").addEventListener("click", () => runOperation("sync"));
$("#run-backup").addEventListener("click", () => runOperation("backup"));
$$("[data-explorer-dataset]").forEach((button) => button.addEventListener("click", () => openExplorer(button.dataset.explorerDataset)));
$("#open-data-explorer").addEventListener("click", (event) => {event.preventDefault(); openExplorer(state.explorerDataset);});
$("#refresh-data-explorer").addEventListener("click", () => openExplorer(state.explorerDataset, {scroll:false}));
$("#explorer-filters").addEventListener("submit", (event) => {event.preventDefault(); loadExplorerRecords(1);});
let explorerSearchTimer;
$("#explorer-search").addEventListener("input", () => {clearTimeout(explorerSearchTimer); explorerSearchTimer=setTimeout(()=>loadExplorerEntities(1),250);});
$$('.tab').forEach((button) => button.addEventListener("click", () => { $$('.tab').forEach((item) => item.classList.remove("active")); button.classList.add("active"); state.activeTab = button.dataset.tab; renderTable(state.activeTab); }));
loadOverview(); loadHistory(); loadOperations(); loadLabHistory(); loadUniverse(); loadCopilotConversations(true); loadScreenHistory(); loadStockPools();
window.addEventListener("DOMContentLoaded", () => {initializeDataExplorer().catch((error)=>{$("#explorer-entity-list").textContent=error.message;});}, {once:true});
