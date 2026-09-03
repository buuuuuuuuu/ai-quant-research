const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = readFileSync(path.join(__dirname, "../../src/ai_quant_assistant/web/static/managed-stocks.js"), "utf8");

function harness(responder, confirmed = true) {
  const elements = new Map(), calls = [], timers = new Map(), confirmations = [], refreshes = [];
  let index = 0;
  function element(key) {
    if (!elements.has(key)) elements.set(key, {value:"", checked:false, textContent:"", innerHTML:"", disabled:false, hidden:false, listeners:{},
      classList:{toggle() {}}, querySelectorAll:() => [], addEventListener(name, fn) {this.listeners[name] = fn;}});
    return elements.get(key);
  }
  const context = vm.createContext({
    $:element, URLSearchParams, window:{confirm:(message) => {confirmations.push(message); return confirmed;}}, state:{explorerDataset:"prices"},
    loadExplorerEntities:async (page) => {refreshes.push(page);},
    escapeHtml:(s) => String(s).replace(/[&<>"']/g, (v) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[v])),
    splitStockSymbols:(s) => s.trim().split(/[\s,]+/).filter(Boolean),
    explorerPagination() {},
    setTimeout:(fn) => {const id = ++index; timers.set(id, fn); return id;}, clearTimeout:(id) => timers.delete(id),
    requestJson:async (url, options = {}) => {calls.push({url, options}); return responder(url, options);},
  });
  vm.runInContext(source, context);
  element("#managed-symbol").value = "003816";
  element("#managed-start").value = "2012-01-01";
  element("#managed-end").value = "2024-01-05";
  element("#managed-include-announcements").checked = true;
  return {context, element, calls, timers, confirmations, refreshes};
}
const empty = () => ({items:[], pagination:{page:1,page_size:30,total:0}});

test("failed tasks show escaped stage without a misleading completion percentage", () => {
  const h = harness(empty);
  const asset = {security_id:"SSE.688111",symbol:"688111",name:"金山办公",status:"active",availability:"unavailable",revision:2,coverage:{adjustments:{},issues:[]}};
  for (const progress of [1,99]) {
    const html = h.context.managedRows([{...asset,task:{task_id:"task-failed",status:"failed",progress,message:"查询上市日期 <svg onload=bad>；行情下载尚未开始"}}]);
    assert.match(html, /同步失败/);
    assert.match(html, /行情下载尚未开始/);
    assert.match(html, /task-failed/);
    assert.doesNotMatch(html, /\d+%|<svg/);
    assert.match(html, /&lt;svg/);
  }
  const running = h.context.managedRows([{...asset,task:{task_id:"task-running",status:"running",progress:40,message:"历史行情分区 3\/8"}}]);
  assert.match(running, /40%/);
  assert.match(running, /历史行情分区 3\/8/);
});

test("cancelled add/archive/restore never sends a network request", async () => {
  const h = harness(() => assert.fail("no request"), false);
  await h.context.addManagedStock({preventDefault() {}});
  await h.context.managedAction({symbol:"003816",target_start:"2012-01-01",revision:1}, "sync");
  await h.context.managedAction({security_id:"SZSE.003816",revision:1}, "archive");
  await h.context.managedAction({security_id:"SZSE.003816",revision:2}, "restore");
  assert.equal(h.calls.length, 0);
});

test("confirmed asynchronous add is single-stock, has a busy guard and visible task", async () => {
  let finish;
  const h = harness((url, options) => options.method === "POST" ? new Promise((resolve) => {finish = resolve;}) : empty());
  const first = h.context.addManagedStock({preventDefault() {}});
  await h.context.addManagedStock({preventDefault() {}});
  assert.equal(h.calls.length, 1);
  assert.deepEqual(JSON.parse(h.calls[0].options.body), {symbol:"003816",start:"2012-01-01",end:"2024-01-05",confirm:true,auto_update:false,include_announcements:true});
  assert.match(h.confirmations[0], /一季报、中报、三季报、年报公告元数据/);
  assert.match(h.confirmations[0], /不下载财务指标、PDF/);
  finish({task_id:"task-one",status:"queued"});
  await first;
  assert.match(h.element("#managed-message").textContent, /task-one.*等待同步/);
  assert.equal(h.element("#managed-add").disabled, false);
});

test("update confirms the selected announcement scope; unchecked remains prices-only", async () => {
  for (const include of [true,false]) {
    const h = harness((url, options) => options.method ? {task_id:"task-update",status:"queued"} : empty());
    h.element("#managed-include-announcements").checked = include;
    await h.context.managedAction({symbol:"003816",target_start:"2012-01-01",revision:7,auto_update:true}, "sync");
    const body = JSON.parse(h.calls[0].options.body);
    assert.equal(body.include_announcements, include);
    assert.equal(body.expected_revision, 7);
    assert.equal(body.refresh, true);
    assert.match(h.confirmations[0], include ? /四类|一季报、中报、三季报、年报/ : /不含公告/);
    assert.match(h.confirmations[0], /访问公网/);
  }
});

test("poll completion refreshes stored explorer data without another download or navigation click", async () => {
  for (const status of ["succeeded","failed"]) {
    let completed = false;
    const asset = {security_id:"SZSE.003816",symbol:"003816",name:"fixture",status:"active",availability:"ready",revision:2,coverage:{adjustments:{},issues:[]}};
    const h = harness(() => ({items:[{...asset,task:{task_id:"task-poll",status:completed ? status : "running",progress:80}}],pagination:{page:1,page_size:30,total:1}}));
    await h.context.loadManagedStocks(1);
    assert.equal(h.refreshes.length, 0);
    completed = true;
    await [...h.timers.values()][0]();
    assert.deepEqual(h.refreshes, [1]);
    assert.ok(h.calls.every((call) => !call.options.method));
    await h.context.loadManagedStocks(1);
    assert.equal(h.refreshes.length, 1);
  }
});

test("announcement summary is distinct from price coverage and escapes untrusted text", () => {
  const h = harness(empty);
  const html = h.context.managedRows([{security_id:"SZSE.003816",status:"active",availability:"ready",revision:2,coverage:{adjustments:{},announcement_sync:{status:"published",row_count:0,start_date:"<svg>",end_date:"2024-01-05"}}}]);
  assert.match(html, /定期公告：已同步 · 0 条/);
  assert.match(html, /&lt;svg&gt;/);
  assert.doesNotMatch(html, /<svg>/);
});

test("422 and 409 failures are visible text, not unhandled promises or executable HTML", async () => {
  for (const status of [422,409]) {
    const h = harness(() => {throw Object.assign(new Error("<script>controlled failure</script>"), {status});});
    await assert.doesNotReject(h.context.addManagedStock({preventDefault() {}}));
    assert.match(h.element("#managed-message").textContent, /controlled failure/);
    assert.equal(h.element("#managed-message").innerHTML, "");
    assert.equal(h.element("#managed-add").disabled, false);
  }
});

test("restore carries revision but never submits a follow-up download", async () => {
  const h = harness((url, options) => options.method ? {status:"active"} : empty());
  await h.context.managedAction({security_id:"SZSE.003816",revision:2}, "restore");
  assert.equal(h.calls.filter((call) => call.options.method).length, 1);
  assert.equal(h.calls[0].options.method, "PATCH");
  assert.deepEqual(JSON.parse(h.calls[0].options.body), {action:"restore",expected_revision:2,confirm:true});
});

test("stock pool chooser only queries local ready stocks and never downloads", async () => {
  const h = harness(empty);
  h.element("#pool-ready-query").value = "中国";
  h.element("#pool-ready-query").listeners.input();
  await [...h.timers.values()][0]();
  assert.match(h.calls[0].url, /ready_only=true/);
  assert.equal(h.calls[0].options.method, undefined);
  assert.match(h.element("#pool-ready-results").innerHTML, /数据浏览/);
});

test("rows escape names, task errors and coverage; older list responses cannot win", async () => {
  const h = harness(empty);
  const html = h.context.managedRows([{security_id:"SZSE.003816",symbol:"003816",name:"<img onerror=bad>",status:"active",availability:"partial",revision:1,coverage:{adjustments:{},issues:["<svg>"]},task:{task_id:"task",status:"failed",progress:20,error:"<script>"}}]);
  assert.doesNotMatch(html, /<img|<svg|<script>/);
  assert.match(html, /&lt;img/);
  assert.match(html, /部分完成/);
  let resolve;
  const race = harness((url) => url.includes("page=1&") ? new Promise((done) => {resolve = done;}) : empty());
  const old = race.context.loadManagedStocks(1);
  await race.context.loadManagedStocks(2);
  resolve({items:[{security_id:"bad"}],pagination:{page:1,page_size:30,total:1}});
  await old;
  assert.match(race.element("#managed-list").innerHTML, /尚无匹配/);
});
