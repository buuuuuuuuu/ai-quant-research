"use strict";
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : `请求失败（${response.status}）：请检查股票、日期与参数`);
  return body;
}
const stockState = {generation:0, sequence:0, summarySequence:0, symbol:null, options:null, section:"prices", page:1, ready:false, generating:false};
const stockLabels = {trade_date:"交易日期",adjustment:"复权",close:"收盘价",open:"开盘价",volume:"成交量",amount:"金额（按来源口径）",metric_label:"指标",metric_value:"数值",unit:"单位",report_type:"报告类型",report_period_end:"报告期",published_at:"公开时间",known_at:"研究可知时间",provider_source:"数据来源",source_batch_id:"来源批次",report_id:"财报身份",record_id:"记录身份",event_id:"事件身份",document_id:"文档身份",announcement_id:"公告身份",event_type:"事件类型",event_date:"事件发生日期",status:"当前状态",reviewed_at:"审核时间",reviewed_by:"审核者",direction:"方向",currency:"币种",ratio:"比例",version_no:"文档版本",page_count:"页数",title:"标题",research_type:"研究类型",scope:"适用范围",run_id:"运行身份",recorded_at:"记录时间",evidence_diagnostic:"证据诊断"};
function stockFacts(item) {
  return `<dl class="stock-facts">${Object.entries(item).filter(([key,value]) => stockLabels[key] && (typeof value !== "object" || value === null)).map(([key,value]) => `<div><dt>${escapeHtml(stockLabels[key])}</dt><dd>${escapeHtml(value ?? "缺失 / unknown")}</dd></div>`).join("")}</dl>`;
}
function stockError(error) { $("#stock-error").textContent = error?.message || String(error || ""); }
function invalidateStock() {
  stockState.generation++; stockState.sequence++; stockState.summarySequence++;
  stockState.ready = false; stockState.generating = false;
  $("#stock-generate").disabled = true;
  for (const id of ["stock-overview","stock-section","stock-evidence","stock-summaries","stock-summary","stock-pagination"]) $("#"+id).innerHTML = "";
  $("#stock-prev").disabled = true; $("#stock-next").disabled = true;
  if (typeof beginResearchDataContext === "function") beginResearchDataContext({scroll:false});
  stockError("");
  return stockState.generation;
}
function stockInput() {
  const symbol = $("#stock-symbol").value.trim();
  if (!/^[0-9]{6}$/.test(symbol)) throw new Error("请选择一只六位股票代码，不能使用空股票范围。");
  const options = {mode:$("#stock-mode").value, start_date:$("#stock-start").value, adjustment:$("#stock-adjustment").value};
  if (options.mode === "historical") {
    options.as_of = $("#stock-as-of").value;
    if (!options.as_of) throw new Error("请选择历史截至日期。");
  }
  return {symbol, options};
}
async function loadStock(push = true) {
  const generation = invalidateStock();
  try {
    const {symbol, options} = stockInput();
    stockState.symbol = symbol; stockState.options = options; stockState.page = 1;
    if (push) history.pushState({}, "", `/stocks/${symbol}?${new URLSearchParams(options)}`);
    const overviewOptions = {...options}; delete overviewOptions.adjustment;
    const result = await requestJson(`/api/v1/stocks/${symbol}/overview?${new URLSearchParams(overviewOptions)}`);
    if (generation !== stockState.generation) return;
    const stock = result.stock;
    $("#stock-title").textContent = `${stock.symbol} · ${stock.name}`;
    $("#stock-overview").innerHTML = `<h2>股票概览</h2><p>${escapeHtml(stock.exchange)} · 上市状态 ${escapeHtml(stock.listing_status)} · 管理 ${escapeHtml(stock.management.status)} · 基础行情 ${escapeHtml(stock.management.availability)}</p><p>当前股票池：${stock.pools.map(pool => escapeHtml(`${pool.name} · revision ${pool.current_revision} · ${pool.status}`)).join("；") || "无"}${stock.pools_truncated ? "（仅展示前 100 个）" : ""}</p><p>当前任务：${escapeHtml(stock.task ? `${stock.task.task_id} · ${stock.task.status} · ${stock.task.progress}% · ${stock.task.error || stock.task.message || ""}` : "无")}</p><p>最近成功同步：${escapeHtml(stock.management.last_success_at || "未知")}</p><h3>各类数据覆盖（不是研究就绪判断）</h3><pre>${escapeHtml(JSON.stringify(stock.coverage,null,2))}</pre><a href="/#data-explorer">${stock.management.status === "unmanaged" ? "前往数据浏览纳入管理" : "查看本地管理与覆盖诊断"}</a><ul>${result.limitations.map(text => `<li>${escapeHtml(text)}</li>`).join("")}</ul>`;
    stockState.ready = true; $("#stock-generate").disabled = false;
    updateStockResearchLink();
    await Promise.all([loadStockSection(), loadStockSummaries()]);
  } catch(error) {if (generation === stockState.generation) {stockState.ready=false; $("#stock-generate").disabled=true; stockError(error);}}
}
function stockEvidenceHtml(evidence) {
  return (evidence || []).map(row => `<blockquote>${escapeHtml(row.quote)}</blockquote><p>字段 ${escapeHtml(row.field_name)} · 页 ${escapeHtml(row.page_number)} · 分块 ${escapeHtml(row.chunk_id)} · SHA-256 ${escapeHtml(row.quote_sha256)}</p><button type="button" data-page-document="${escapeHtml(row.document_id)}" data-page-number="${escapeHtml(row.page_number)}">查看原文页</button>`).join("");
}
function stockRecordHtml(item, section) {
  let links = "";
  if (item.run_id?.startsWith("factor-backtest-")) links += `<a href="/reports/backtests/${encodeURIComponent(item.run_id)}" target="_blank" rel="noopener">查看原回测报告（组合结果不是单股表现）</a> `;
  if (item.run_id?.startsWith("factor-lab-")) links += `<a href="/reports/factor-lab/${encodeURIComponent(item.run_id)}" target="_blank" rel="noopener">查看原因子实验报告</a> `;
  const documentId = item.document_id;
  if (documentId) links += `<a href="/api/v1/documents/${encodeURIComponent(documentId)}/file#page=1" target="_blank" rel="noopener">查看本地 PDF（不下载新文件）</a> `;
  if (section === "announcements") links += `<button data-source="announcements" data-record="${escapeHtml(item.record_id)}">公告详情</button> `;
  if (item.report_id || section === "financial_reports") links += `<button data-source="financial-reports" data-record="${escapeHtml(item.report_id || item.record_id)}">财报详情</button> `;
  if (item.event_id && item.status === "accepted" && !item.evidence_diagnostic) links += `<a href="/?stock=${encodeURIComponent(stockState.symbol)}&event=${encodeURIComponent(item.event_id)}#document-intelligence">前往事件研究（需确认）</a>`;
  return `<article>${stockFacts(item)}${links}${stockEvidenceHtml(item.evidence)}${item.resolved_symbols ? `<p>${escapeHtml(item.scope)}；冻结股票 ${escapeHtml(item.resolved_symbols.join("、"))}</p>` : ""}<details><summary>完整字段、版本、来源及冻结参数</summary><pre>${escapeHtml(JSON.stringify(item,null,2))}</pre></details></article>`;
}
async function loadStockSection() {
  if (!stockState.ready) return;
  const generation = stockState.generation, sequence = ++stockState.sequence;
  $("#stock-section").innerHTML = "读取本地资料…"; $("#stock-evidence").innerHTML = "";
  $("#stock-prev").disabled = true; $("#stock-next").disabled = true;
  try {
    const section = stockState.section;
    const result = await requestJson(`/api/v1/stocks/${stockState.symbol}/${section}?${new URLSearchParams({...stockState.options,page:stockState.page,page_size:20})}`);
    if (generation !== stockState.generation || sequence !== stockState.sequence) return;
    $("#stock-section").innerHTML = result.items.map(item => stockRecordHtml(item,section)).join("") || "本地没有匹配记录；不代表没有风险或没有事件。";
    $("#stock-pagination").textContent = `第 ${result.pagination.page} / ${result.pagination.pages} 页 · ${result.pagination.total} 条`;
    $("#stock-prev").disabled = result.pagination.page <= 1;
    $("#stock-next").disabled = result.pagination.page >= result.pagination.pages;
    bindStockEvidence($("#stock-section"), generation, sequence);
  } catch(error) {if (generation === stockState.generation && sequence === stockState.sequence) {$("#stock-section").innerHTML="";stockError(error);}}
}
function bindStockEvidence(element, generation, sequence) {
  element.querySelectorAll("[data-page-document], [data-source]").forEach(button => button.addEventListener("click", async () => {
    $("#stock-evidence").textContent = "";
    try {
      const url = button.dataset.pageDocument ? `/api/v1/documents/${encodeURIComponent(button.dataset.pageDocument)}/pages/${encodeURIComponent(button.dataset.pageNumber)}` : `/api/v1/data/${button.dataset.source}/${encodeURIComponent(button.dataset.record)}`;
      const result = await requestJson(url);
      if (generation !== stockState.generation || sequence !== stockState.sequence) return;
      $("#stock-evidence").innerHTML = `<h3>当前本地原文详情（历史事实请以保存摘要中的证据为准）</h3><pre>${escapeHtml(result.text || JSON.stringify(result,null,2))}</pre>`;
    } catch(error) {if (generation === stockState.generation && sequence === stockState.sequence) stockError(error);}
  }));
}
function stockPrepareInput() {
  if (!stockState.ready) throw new Error("请先加载当前股票资料。");
  const kind = $("#stock-research-type").value;
  const parameters = {symbols:[stockState.symbol]};
  const end = $("#stock-research-end").value;
  if (kind === "screen") Object.assign(parameters,{as_of:end,conditions:[{field:$("#stock-metric").value,operator:"gte",value:0}],missing_policy:"show"});
  else Object.assign(parameters,{start_date:$("#stock-research-start").value,end_date:end,factor:$("#stock-metric").value,top_n:1,benchmark_symbol:$("#stock-benchmark").value,adjustment:stockState.options.adjustment});
  return {kind,parameters};
}
function updateStockResearchLink() {
  if (!stockState.symbol) return;
  $("#stock-backtest-link").href = `/?${new URLSearchParams({stock:stockState.symbol,start_date:$("#stock-research-start").value,end_date:$("#stock-research-end").value,factor:$("#stock-metric").value,benchmark:$("#stock-benchmark").value,adjustment:stockState.options?.adjustment || "qfq"})}#backtest`;
}
function showStockSummary(summary) {
  const snapshot = summary.snapshot;
  $("#stock-summary").innerHTML = `<h3>${escapeHtml(summary.summary_id)}</h3><p>不可变快照 · ${escapeHtml(summary.created_at)} · ${escapeHtml(snapshot.stock.symbol)} · as_of ${escapeHtml(snapshot.context.as_of)}</p><a href="/api/v1/stocks/${encodeURIComponent(snapshot.stock.symbol)}/summaries/${encodeURIComponent(summary.summary_id)}/html">导出 HTML</a><p>${escapeHtml(snapshot.evidence_window)}</p><ul>${snapshot.limitations.map(text => `<li>${escapeHtml(text)}</li>`).join("")}</ul>${Object.entries(snapshot.sections).map(([key,section]) => `<h4>${escapeHtml(key)} · 匹配 ${escapeHtml(section.pagination.total)} 条</h4>${section.items.map(item => stockRecordHtml(item,key)).join("") || "缺失 / unknown"}${section.excluded?.length ? `<pre>未进入事实：${escapeHtml(JSON.stringify(section.excluded,null,2))}</pre>` : ""}`).join("")}`;
  bindStockEvidence($("#stock-summary"), stockState.generation, stockState.sequence);
}
async function generateStockSummary() {
  if (!stockState.ready || stockState.generating) return;
  const generation = stockState.generation, sequence = ++stockState.summarySequence;
  stockState.generating = true; $("#stock-generate").disabled = true; $("#stock-summary").innerHTML = "";
  try {
    const result = await requestJson(`/api/v1/stocks/${stockState.symbol}/summaries`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(stockState.options)});
    if (generation !== stockState.generation || sequence !== stockState.summarySequence) return;
    showStockSummary(result); await loadStockSummaries();
  } catch(error) {if (generation === stockState.generation && sequence === stockState.summarySequence) stockError(error);}
  finally {if (generation === stockState.generation) {stockState.generating=false; $("#stock-generate").disabled=!stockState.ready;}}
}
async function loadStockSummaries() {
  if (!stockState.ready) return;
  const generation = stockState.generation;
  try {
    const result = await requestJson(`/api/v1/stocks/${stockState.symbol}/summaries?page_size=100`);
    if (generation !== stockState.generation) return;
    $("#stock-summaries").innerHTML = `<p>最近 ${result.items.length} / ${result.pagination.total} 份摘要（更早记录可通过分页 API 查看）</p>` + result.items.map(row => `<button type="button" data-summary="${escapeHtml(row.summary_id)}">${escapeHtml(row.created_at)} · as_of ${escapeHtml(row.as_of)}</button>`).join("");
    $("#stock-summaries").querySelectorAll("[data-summary]").forEach(button => button.addEventListener("click", async () => {
      const sequence = ++stockState.summarySequence; $("#stock-summary").innerHTML = "";
      try {
        const summary = await requestJson(`/api/v1/stocks/${stockState.symbol}/summaries/${encodeURIComponent(button.dataset.summary)}`);
        if (generation === stockState.generation && sequence === stockState.summarySequence) showStockSummary(summary);
      } catch(error) {if (generation === stockState.generation && sequence === stockState.summarySequence) stockError(error);}
    }));
  } catch(error) {if (generation === stockState.generation) stockError(error);}
}
function restoreStockLocation() {
  $("#stock-symbol").value = location.pathname.split("/").at(-1);
  const query = new URLSearchParams(location.search);
  $("#stock-mode").value = query.get("mode") || "current";
  $("#stock-as-of").value = query.get("as_of") || "";
  $("#stock-as-of").disabled = $("#stock-mode").value !== "historical";
  $("#stock-start").value = query.get("start_date") || "2012-01-01";
  $("#stock-adjustment").value = query.get("adjustment") || "qfq";
}
function bindStockWorkbench() {
  restoreStockLocation();
  $("#stock-research-end").value = new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());
  $("#stock-context").addEventListener("submit", event => {event.preventDefault();loadStock();});
  $("#stock-context").addEventListener("input", () => {invalidateStock(); $("#stock-as-of").disabled = $("#stock-mode").value !== "historical";});
  $("#stock-tabs").querySelectorAll("[data-section]").forEach(button => button.addEventListener("click", () => {stockState.section=button.dataset.section;stockState.page=1;loadStockSection();}));
  $("#stock-prev").addEventListener("click", () => {stockState.page--;loadStockSection();});
  $("#stock-next").addEventListener("click", () => {stockState.page++;loadStockSection();});
  $("#stock-prepare").addEventListener("input", () => {beginResearchDataContext({scroll:false});updateStockResearchLink();});
  $("#stock-prepare").addEventListener("submit", event => {event.preventDefault();openResearchReadiness($("#stock-research-type").value, () => stockPrepareInput().parameters);});
  $("#stock-generate").addEventListener("click", generateStockSummary);
  $("#stock-summaries-refresh").addEventListener("click", loadStockSummaries);
  window.addEventListener("popstate", () => {restoreStockLocation();loadStock(false);});
  loadStock(false);
}
if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", bindStockWorkbench);
