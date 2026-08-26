const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = { run: null, activeTab: "trades", conversationId: null, copilotPlan: null };

const formatNumber = (value) => new Intl.NumberFormat("zh-CN").format(Number(value || 0));
const formatMoney = (value) => new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(Number(value || 0));
const formatPct = (value) => `${(Number(value || 0) * 100).toFixed(2)}%`;
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  if (response.status === 401) { window.location.assign("/login"); throw new Error("登录已过期，请重新登录"); }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `请求失败（${response.status}）`);
  return payload;
}

const datasetLabels = { daily_bar: "股票行情", benchmark_daily_bar: "指数基准", announcement: "定期公告", financial_metric: "财务指标", data_platform_fixture: "样例数据" };
const taskTypeLabels = { sync: "增量同步", backup: "数据库备份", backtest: "策略回测", factor_lab: "因子实验", copilot: "AI 研究" };
const taskStatusLabels = { queued: "等待执行", running: "正在运行", succeeded: "执行成功", failed: "执行失败", published: "已发布" };
const auditActionLabels = { login_succeeded: "管理员登录", login_failed: "登录失败", login_rate_limited: "登录限流", logout: "管理员退出", sync_requested: "发起增量同步", sync_completed: "增量同步完成", sync_failed: "增量同步失败", backup_requested: "发起数据库备份", backup_created: "数据库备份完成", backup_verified: "验证数据库备份", backup_restored: "恢复数据库备份", backup_pruned: "清理过期备份", backtest_requested: "发起策略回测", factor_lab_requested: "发起因子研究", copilot_conversation_created: "创建 AI 研究对话", copilot_plan_created: "生成 AI 研究计划", copilot_plan_confirmed: "确认 AI 研究计划", copilot_plan_execution_requested: "执行 AI 研究计划", alert_acknowledged: "确认运行告警" };

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
    $("#operations-task-list").innerHTML = tasks.length ? tasks.slice(0, 5).map((item) => operationRow(`${taskTypeLabels[item.task_type] || item.task_type} · ${taskStatusLabels[item.status] || item.status}`, item.error || `${item.progress}% · ${operationTime(item.created_at)}`, item.status === "failed")).join("") : '<p class="empty-state">还没有后台任务。</p>';
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
    $("#metric-securities").textContent = formatNumber(relations.security_master);
    $("#metric-prices").textContent = formatNumber(relations.published_daily_bar);
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
  return {
    symbols,
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
$$('.tab').forEach((button) => button.addEventListener("click", () => { $$('.tab').forEach((item) => item.classList.remove("active")); button.classList.add("active"); state.activeTab = button.dataset.tab; renderTable(state.activeTab); }));
loadOverview(); loadHistory(); loadOperations(); loadLabHistory(); loadUniverse(); loadCopilotConversations(true);
