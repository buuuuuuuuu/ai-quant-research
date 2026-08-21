const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = { run: null, activeTab: "trades" };

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
const taskTypeLabels = { sync: "增量同步", backup: "数据库备份", backtest: "策略回测" };
const taskStatusLabels = { queued: "等待执行", running: "正在运行", succeeded: "执行成功", failed: "执行失败", published: "已发布" };

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
    $("#sync-status-list").innerHTML = datasets.length ? datasets.map((item) => operationRow(`${datasetLabels[item.dataset] || item.dataset} · ${taskStatusLabels[item.status] || item.status}`, item.error || `${formatNumber(item.row_count)} 行 · ${operationTime(item.ended_at || item.started_at)}`, Boolean(item.error))).join("") : '<p class="empty-state">还没有数据同步记录。</p>';
    $("#operations-task-list").innerHTML = tasks.length ? tasks.slice(0, 5).map((item) => operationRow(`${taskTypeLabels[item.task_type] || item.task_type} · ${taskStatusLabels[item.status] || item.status}`, item.error || `${item.progress}% · ${operationTime(item.created_at)}`, item.status === "failed")).join("") : '<p class="empty-state">还没有后台任务。</p>';
    $("#backup-status-list").innerHTML = backups.length ? backups.slice(0, 4).map((item) => operationRow(`${(Number(item.size_bytes) / 1024 / 1024).toFixed(1)} MB · ${item.exists ? "文件正常" : "文件缺失"}`, operationTime(item.created_at), !item.exists)).join("") : '<p class="empty-state">还没有数据库备份。</p>';
  } catch (error) {
    const message = $("#operations-message");
    message.textContent = error.message;
    message.classList.add("error");
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

$("#backtest-form").addEventListener("submit", submitBacktest);
$("#refresh-history").addEventListener("click", loadHistory);
$("#refresh-operations").addEventListener("click", loadOperations);
$("#run-sync").addEventListener("click", () => runOperation("sync"));
$("#run-backup").addEventListener("click", () => runOperation("backup"));
$$('.tab').forEach((button) => button.addEventListener("click", () => { $$('.tab').forEach((item) => item.classList.remove("active")); button.classList.add("active"); state.activeTab = button.dataset.tab; renderTable(state.activeTab); }));
loadOverview(); loadHistory(); loadOperations();
