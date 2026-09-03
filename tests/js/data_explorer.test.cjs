const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = readFileSync(path.join(__dirname, "../../src/ai_quant_assistant/web/static/workbench.js"), "utf8");
const functions = source.split('$("#screen-as-of").value=')[0];
const pagination = {page:1,page_size:30,pages:1,total:1};
const stock = {symbol:"003816",name:"中国广核",price_rows:2};
const benchmark = {symbol:"csi000300",name:"沪深300",price_rows:2};
const rows = {items:[{record_id:"fixture",trade_date:"2024-01-05",close:3.25}],pagination};

function harness(responder) {
  const elements = new Map(), calls = [];
  let managedLoads = 0;
  function element(key) {
    if (!elements.has(key)) elements.set(key, {value:"",hidden:false,disabled:false,textContent:"",innerHTML:"",scrolls:0,
      classList:{toggle() {}},querySelectorAll:() => [],addEventListener() {},
      scrollIntoView() {this.scrolls++;},removeAttribute(name) {delete this[name];}});
    return elements.get(key);
  }
  const context = vm.createContext({
    document:{querySelector:element,querySelectorAll:() => []}, URLSearchParams,
    window:{location:{assign() {assert.fail("unexpected redirect");}}},
    loadManagedStocks:async () => {managedLoads++;},
    fetch:async (url, options = {}) => {
      calls.push(url);
      assert.ok(!options.method || options.method === "GET", "initialization must only read local data");
      assert.ok(url.startsWith("/api/v1/data/"));
      const result = await responder(url);
      return {ok:!result.error,status:result.error ? 500 : 200,json:async () => result.error ? {detail:result.error} : result};
    },
  });
  vm.runInContext(functions, context);
  element("#explorer-adjustment").value = "qfq";
  return {context,element,calls,managedLoads:() => managedLoads};
}

function respond(url) {
  if (url.startsWith("/api/v1/data/securities?")) return {items:[stock],pagination};
  if (url.startsWith("/api/v1/data/benchmarks?")) return {items:[benchmark],pagination};
  if (url === "/api/v1/data/securities/003816") return stock;
  if (url.includes("/prices?")) return rows;
  throw new Error(`unexpected endpoint ${url}`);
}

test("page initialization displays existing prices without a navigation click, scroll or download", async () => {
  const h = harness(respond);
  await h.context.initializeDataExplorer();
  assert.equal(h.element("#data-explorer").hidden, false);
  assert.equal(h.element("#data-explorer").scrolls, 0);
  assert.match(h.element("#explorer-entity-list").innerHTML, /003816.*中国广核/);
  assert.match(h.element("#explorer-table").innerHTML, /2024-01-05/);
  assert.match(h.calls[0], /managed_only=true/);
  assert.equal(h.managedLoads(), 1);
  await h.context.openExplorer("benchmarks");
  assert.equal(h.managedLoads(), 1, "dataset switching does not reload the management panel");
  assert.match(h.element("#explorer-identity").innerHTML, /沪深300/);
});

test("slow initial stock list cannot overwrite a subsequently selected benchmark", async () => {
  let release;
  const h = harness((url) => url.startsWith("/api/v1/data/securities?") ? new Promise((resolve) => {release = resolve;}) : respond(url));
  const initial = h.context.initializeDataExplorer();
  await h.context.openExplorer("benchmarks", {scroll:false});
  release({items:[stock],pagination});
  await initial;
  assert.match(h.element("#explorer-entity-list").innerHTML, /沪深300/);
  assert.doesNotMatch(h.element("#explorer-entity-list").innerHTML, /中国广核/);
  assert.ok(!h.calls.includes("/api/v1/data/securities/003816"));
});

test("late records cannot overwrite a newer selection or page", async () => {
  let release, delayed = false;
  const h = harness((url) => delayed && url.includes("page=2") ? new Promise((resolve) => {release = resolve;}) : respond(url));
  await h.context.initializeDataExplorer();
  delayed = true;
  const old = h.context.loadExplorerRecords(2);
  await h.context.loadExplorerRecords(1);
  release({items:[{trade_date:"stale-page",close:999}],pagination});
  await old;
  assert.doesNotMatch(h.element("#explorer-table").innerHTML, /stale-page/);
  assert.match(h.element("#explorer-table").innerHTML, /2024-01-05/);
});

test("initial API failure is visible escaped text and does not hide the browser", async () => {
  const h = harness(() => ({error:"controlled <script>failure</script>"}));
  await h.context.initializeDataExplorer();
  assert.match(h.element("#explorer-entity-list").innerHTML, /controlled &lt;script&gt;/);
  assert.doesNotMatch(h.element("#explorer-entity-list").innerHTML, /<script>/);
  assert.equal(h.element("#data-explorer").hidden, false);
});

test("automatic local refresh preserves the selected stock instead of jumping to the first row", async () => {
  const second = {symbol:"688111",name:"金山办公",price_rows:2};
  const h = harness((url) => url.startsWith("/api/v1/data/securities?") ? {items:[stock,second],pagination}
    : url === "/api/v1/data/securities/688111" ? second : respond(url));
  await h.context.initializeDataExplorer();
  await h.context.selectExplorerEntity(second);
  await h.context.loadExplorerEntities(1);
  assert.match(h.element("#explorer-identity").innerHTML, /688111.*金山办公/);
  assert.match(h.element("#explorer-download").href, /688111\/prices/);
});
