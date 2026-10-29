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
  const elements = Object.fromEntries(["index-code", "index-status", "index-result", "index-sync", "index-task", "index-submit"].map(id => [id, new Element()]));
  elements["index-code"].value = "000300";
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
  assert.deepEqual(calls, ["/api/v1/index-funds/000300"]);
});
