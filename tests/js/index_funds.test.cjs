const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = readFileSync(path.join(__dirname, "../../src/ai_quant_assistant/web/static/index-funds.js"), "utf8");

class Element {
  constructor(tag = "div") { this.tag = tag; this.children = []; this.style = {}; this.listeners = {}; this.value = ""; this.textContent = ""; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  addEventListener(name, handler) { this.listeners[name] = handler; }
}
function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }

test("daily details use the selected snapshot day and preserve missing values", async () => {
  const elements = Object.fromEntries(["index-code", "index-numerator", "index-status", "index-result", "index-sync", "index-task", "index-submit"].map(id => [id, new Element()]));
  elements["index-code"].value = "000300";
  elements["index-numerator"].value = "nav";
  const calls = [];
  const snapshot = {snapshot_id:"s", spec:{funds:["SSE.510300", "SZSE.159919"]}, inputs:{
    shares:[{date:"2026-09-01",fund_id:"SSE.510300",shares:"100"}],
    nav:[{date:"2026-09-01",fund_id:"SSE.510300",unit_nav:"2"}],
  }, result:{points:[{trade_date:"2026-09-01",numerator_cny:null,denominator_cny:"10000",ratio_fraction:null},
                    {trade_date:"2026-09-02",numerator_cny:null,denominator_cny:null,ratio_fraction:null}]}};
  const document = {getElementById:id => elements[id], createElement:tag => new Element(tag), createElementNS:(_ns, tag) => new Element(tag)};
  vm.runInNewContext(source, {document, window:{}, fetch:async url => {calls.push(url); return {ok:true, json:async () => ({snapshot})};}, setTimeout});
  await new Promise(resolve => setImmediate(resolve));
  const root = elements["index-result"];
  const chooser = descendants(root).find(e => e.tag === "select");
  assert.equal(chooser.value, "2026-09-02");
  chooser.value = "2026-09-01"; chooser.listeners.change();
  const texts = descendants(root).map(e => e.textContent);
  assert.ok(texts.includes("200.00"));
  assert.ok(texts.includes("2.0000%"));
  assert.ok(texts.includes("SZSE.159919"));
  assert.ok(texts.includes("—"));
  assert.deepEqual(calls, ["/api/v1/index-funds/000300?numerator_mode=nav"]);
});

test("chart clicks and accessible date buttons select the same snapshot day, including gaps", async () => {
  const elements = Object.fromEntries(["index-code", "index-numerator", "index-status", "index-result", "index-sync", "index-task", "index-submit"].map(id => [id, new Element()]));
  elements["index-code"].value = "000300";
  elements["index-numerator"].value = "nav";
  const snapshot = {snapshot_id:"s",spec:{funds:["SSE.510300"]},inputs:{shares:[],nav:[]},result:{points:[
    {trade_date:"2026-09-01",numerator_cny:"200",denominator_cny:"10000",ratio_fraction:"0.02",missing:[]},
    {trade_date:"2026-09-02",numerator_cny:null,denominator_cny:null,ratio_fraction:null,missing:["index:float_market_cap"]},
    {trade_date:"2026-09-03",numerator_cny:"300",denominator_cny:"10000",ratio_fraction:"0.03",missing:[]},
  ]}};
  const document = {getElementById:id => elements[id],createElement:tag => new Element(tag),createElementNS:(_ns, tag) => new Element(tag)};
  vm.runInNewContext(source,{document,window:{},fetch:async () => ({ok:true,json:async () => ({snapshot})}),setTimeout});
  await new Promise(resolve => setImmediate(resolve));
  const root = elements["index-result"], select = descendants(root).find(e => e.tag === "select");
  assert.ok(descendants(root).some(e => e.textContent.includes("可计算 2 / 3 个交易日")));
  assert.ok(descendants(root).some(e => e.textContent.includes("跨缺口较上一完整观测日（2026-09-01）+1.0000 个百分点")));
  const buttons = () => descendants(root).filter(e => e.tag === "button");
  assert.equal(select.value, "2026-09-03");
  buttons()[0].listeners.click();
  assert.equal(select.value, "2026-09-02");
  assert.ok(descendants(root).some(e => e.textContent.includes("缺失：index:float_market_cap")));
  const circle = descendants(root).find(e => e.tag === "circle");
  circle.listeners.click();
  assert.equal(select.value, "2026-09-01");
  assert.equal(buttons()[0].disabled, true);
  buttons()[1].listeners.click();
  assert.equal(select.value, "2026-09-02");
});

test("failed index read keeps prior snapshot labelled while older responses are ignored", async () => {
  const elements = Object.fromEntries(["index-code", "index-numerator", "index-status", "index-result", "index-sync", "index-task", "index-submit"].map(id => [id, new Element()]));
  elements["index-code"].value = "000300";
  elements["index-numerator"].value = "nav";
  let resolveOld;
  const snapshot = {snapshot_id:"old",spec:{funds:[]},inputs:{},result:{warning:"旧数据",points:[]}};
  const fetch = async url => {
    if (url.includes("/000300?")) return {ok:true,json:async () => ({snapshot})};
    if (url.includes("/000016?")) return await new Promise(resolve => {resolveOld = resolve;});
    throw new Error("连接失败");
  };
  const document = {getElementById:id => elements[id],createElement:tag => new Element(tag),createElementNS:(_ns, tag) => new Element(tag)};
  vm.runInNewContext(source,{document,window:{},fetch,setTimeout});
  await new Promise(resolve => setImmediate(resolve));
  elements["index-code"].value = "000016"; elements["index-code"].listeners.change();
  elements["index-code"].value = "000905"; elements["index-code"].listeners.change();
  await new Promise(resolve => setImmediate(resolve));
  assert.match(elements["index-status"].textContent, /读取 000905 失败/);
  assert.match(elements["index-status"].textContent, /保留 000300/);
  resolveOld({ok:true,json:async () => ({snapshot:null})});
  await new Promise(resolve => setImmediate(resolve));
  assert.match(elements["index-status"].textContent, /读取 000905 失败/);
  assert.ok(descendants(elements["index-result"]).some(e => e.textContent === "旧数据"));
});

test("mode switch reads matching local snapshot and labels close correctly", async () => {
  const elements = Object.fromEntries(["index-code", "index-numerator", "index-status", "index-result", "index-sync", "index-task", "index-submit"].map(id => [id, new Element()]));
  elements["index-code"].value = "000300";
  elements["index-numerator"].value = "nav";
  const snapshot = {snapshot_id:"price",spec:{funds:["SSE.510300"],numerator_mode:"close"},inputs:{shares:[{date:"2026-09-01",fund_id:"SSE.510300",shares:"100"}],close:[{date:"2026-09-01",fund_id:"SSE.510300",close:"3"}]},result:{points:[{trade_date:"2026-09-01",numerator_cny:"300",denominator_cny:"10000",ratio_fraction:"0.03",missing:[]}]}};
  const calls = [];
  const document = {getElementById:id => elements[id],createElement:tag => new Element(tag),createElementNS:(_ns, tag) => new Element(tag)};
  vm.runInNewContext(source,{document,window:{},fetch:async (url, options) => {calls.push({url,options}); return {ok:true,json:async () => ({snapshot:url.endsWith('=close') ? snapshot : null})};},setTimeout});
  await new Promise(resolve => setImmediate(resolve));
  elements["index-numerator"].value = "close"; elements["index-numerator"].listeners.change();
  await new Promise(resolve => setImmediate(resolve));
  const texts = descendants(elements["index-result"]).map(e => e.textContent);
  assert.ok(texts.some(s => s.includes("不复权收盘价")));
  assert.ok(texts.includes("300.00") && texts.includes("3.0000%"));
  assert.deepEqual(calls.map(c => c.url), ['/api/v1/index-funds/000300?numerator_mode=nav','/api/v1/index-funds/000300?numerator_mode=close']);
  assert.ok(calls.every(c => c.options === undefined));
});
