// Execute the actual workbench handlers against a controlled DOM/network, offline.
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../../src/ai_quant_assistant/web/static/workbench.js"), "utf8").split("function operationTime")[0];

class Element {
  constructor(id) {
    Object.assign(this, {id, value: "", textContent: "", innerHTML: "", disabled: false, isConnected: true, dataset: {}, attributes: {}, listeners: {}});
    const classes = new Set();
    this.classList = {
      add: (name) => classes.add(name), remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
    };
  }
  setAttribute(name, value) {this.attributes[name] = value;}
  scrollIntoView() {this.scrolled = true;}
  addEventListener(name, callback) {this.listeners[name] = callback;}
  querySelectorAll() {return [];}
}

function harness(responder, {confirm = true, immediateTimers = true} = {}) {
  const elements = new Map(), calls = [], timers = new Map();
  let nextTimer = 0;
  const element = (selector) => {
    if (!elements.has(selector)) elements.set(selector, new Element(selector.slice(1)));
    return elements.get(selector);
  };
  const messages = [element("#stock-pool-message"), element("#stock-pool-member-message")];
  const controls = [element("#stock-pool-add"), element("#stock-pool-add-symbols"), element("#stock-pool-default")];
  controls[2].disabled = true;
  const context = vm.createContext({
    document: {
      querySelector: element,
      querySelectorAll: (selector) => selector === "[data-stock-pool-message]" ? messages : selector.includes("#stock-pool-detail button") ? controls : [],
    },
    window: {confirm: () => confirm, prompt: () => null, location: {assign() {}}},
    fetch: async (url, options = {}) => {
      calls.push({url, options});
      const {status = 200, payload = {}} = await responder(url, options);
      return {status, ok: status >= 200 && status < 300, json: async () => payload};
    },
    setTimeout: (callback) => {
      const id = ++nextTimer;
      if (immediateTimers) queueMicrotask(callback); else timers.set(id, callback);
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
  });
  vm.runInContext(source, context);
  vm.runInContext('state.stockPool = {pool_id:"pool-default-research",current_revision:1,name:"默认池",member_count:10}; loadStockPools = async () => true;', context);
  context.bindStockPoolDetail();
  return {context, element, messages, controls, calls, timers};
}

test("real add click shows 422 beside button, preserves input and resolves without rejection", async () => {
  const h = harness(async () => ({status: 422, payload: {detail: "symbols missing from security_master: ['600022']"}}));
  h.element("#stock-pool-add-symbols").value = "600022";
  await assert.doesNotReject(h.element("#stock-pool-add").listeners.click());
  assert.equal(h.calls[0].url, "/api/v1/stock-pools/pool-default-research/members");
  assert.deepEqual(JSON.parse(h.calls[0].options.body), {expected_revision: 1, symbols: ["600022"]});
  for (const message of h.messages) {
    assert.match(message.textContent, /更新证券主数据/);
    assert.equal(message.attributes.role, "alert");
    assert.equal(message.classList.contains("negative"), true);
  }
  assert.equal(h.messages[1].scrolled, true);
  assert.equal(h.element("#stock-pool-add-symbols").value, "600022");
  assert.deepEqual(h.controls.map((el) => el.disabled), [false, false, true]);
});

test("busy guard sends one request; success and conflict reset visible state", async () => {
  let finish, status = 200;
  const h = harness(() => new Promise((resolve) => {finish = () => resolve({status, payload: status === 200 ? {pool_id:"pool-default-research",current_revision:2} : {detail:"revision conflict"}});}));
  const first = h.element("#stock-pool-add").listeners.click();
  assert.equal(h.element("#stock-pool-detail").attributes["aria-busy"], "true");
  assert.equal(h.controls.every((el) => el.disabled), true);
  await h.element("#stock-pool-add").listeners.click();
  assert.equal(h.calls.length, 1);
  finish(); await first;
  assert.match(h.messages[1].textContent, /已保存 revision 2/);
  assert.equal(h.messages[1].classList.contains("negative"), false);
  status = 409;
  const conflict = h.element("#stock-pool-add").listeners.click();
  finish(); await conflict;
  assert.match(h.messages[1].textContent, /版本已变化.*本次输入尚未保存/);
  assert.equal(h.element("#stock-pool-detail").attributes["aria-busy"], "false");
});

test("Pydantic details are readable and HTML error strings remain text", async () => {
  const h = harness(async () => ({status:422,payload:{detail:[{loc:["body","symbols"],msg:"at most 200 <script>"}]}}));
  await h.element("#stock-pool-add").listeners.click();
  assert.equal(h.messages[1].textContent, "body.symbols: at most 200 <script>");
  assert.equal(h.messages[1].innerHTML, "");
});

test("failed explicit sync is handled instead of an unhandled rejection", async () => {
  const h = harness(async () => ({status:500}));
  await h.context.stockPoolMutation("/sync-missing", {method:"POST"}, {refreshPool:false});
  assert.match(h.messages[1].textContent, /500/);
  assert.equal(h.controls[0].disabled, false);
});

test("cancelled directory confirmation makes no network request", async () => {
  const h = harness(async () => {assert.fail("must not fetch");}, {confirm:false});
  await h.context.refreshSecurityMaster();
  assert.equal(h.calls.length, 0);
});

test("confirmed directory refresh only submits and polls directory task, never adds members", async () => {
  const h = harness(async (url) => ({payload: url.endsWith("/refresh")
    ? {task_id:"directory-1",status:"running",progress:10}
    : {task_id:"directory-1",status:"succeeded",progress:100,result:{added_count:1,updated_count:10}}}));
  await h.context.refreshSecurityMaster();
  assert.deepEqual(h.calls.map(({url}) => url), ["/api/v1/stock-pools/securities/refresh", "/api/v1/ops/tasks/directory-1"]);
  assert.deepEqual(JSON.parse(h.calls[0].options.body), {confirm:true});
  assert.match(h.element("#security-master-message").textContent, /新增 1 只，更新 10 只/);
  assert.equal(h.element("#security-master-refresh").disabled, false);
});

test("failed directory task is visible; submit retry does not show stale task ID", async () => {
  let retry = false;
  const h = harness(async () => retry ? {status:500} : {payload:{task_id:"old-task",status:"failed",error:"provider unavailable",progress:10}});
  await h.context.refreshSecurityMaster();
  assert.match(h.element("#security-master-message").textContent, /provider unavailable.*old-task/);
  retry = true;
  await h.context.refreshSecurityMaster();
  assert.doesNotMatch(h.element("#security-master-message").textContent, /old-task/);
  assert.equal(h.element("#security-master-message").classList.contains("negative"), true);
});

test("empty directory search gives actionable hint; stale response cannot replace newer results", async () => {
  let finish;
  const h = harness((url) => url.includes("old") ? new Promise((resolve) => {finish = resolve;}) : Promise.resolve({payload:[]}), {immediateTimers:false});
  const search = h.element("#stock-pool-security-search"), results = h.element("#stock-pool-search-results");
  search.value = "old"; search.listeners.input();
  const old = [...h.timers.values()][0]();
  search.value = "600022"; search.listeners.input();
  await [...h.timers.values()].at(-1)();
  assert.match(results.textContent, /更新证券主数据/);
  finish({payload:[{symbol:"600519",name:"stale",status:"active"}]});
  await old;
  assert.equal(results.innerHTML, "");
});

test("search result names remain escaped and inactive stocks cannot be selected", async () => {
  const h = harness(async () => ({payload:[{symbol:"600022",name:'<img src=x onerror="boom()">',status:"delisted"}]}), {immediateTimers:false});
  h.element("#stock-pool-security-search").value = "600022";
  h.element("#stock-pool-security-search").listeners.input();
  await [...h.timers.values()][0]();
  const html = h.element("#stock-pool-search-results").innerHTML;
  assert.match(html, /disabled/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img/);
});
