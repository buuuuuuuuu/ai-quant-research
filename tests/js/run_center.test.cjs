const assert = require("node:assert/strict");
const test = require("node:test");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const ui = require("../../src/ai_quant_assistant/web/static/run-center.js");
const shell = require("../../src/ai_quant_assistant/web/static/app-shell.js");
const code = readFileSync(
  path.join(__dirname, "../../src/ai_quant_assistant/web/static/run-center.js"),
  "utf8",
);
const tick = () => new Promise((resolve) => setImmediate(resolve));
const row = (id) => ({
  label: "财务因子回测",
  kind: "factor_backtest",
  record_id: id,
  run_id: null,
  source: "task",
  symbols: ["600519"],
  status: "running",
  detail_url: "/backtests/detail?record=" + id,
  copy_url: "/backtests/configure?record=" + id,
  spec: { symbols: ["600519"] },
  result: {},
});

function harness(
  pathname = "/backtests",
  responder = async () => ({ items: [], total: 0, notice: "fixture" }),
  options = {},
) {
  const elements = new Map(),
    calls = [];
  const element = (id) => {
    if (!elements.has(id))
      elements.set(id, {
        value: "",
        textContent: "",
        innerHTML: "",
        hidden: false,
        disabled: false,
        checked: false,
        inputs: [],
        parentElement: { hidden: false },
        listeners: {},
        scrollIntoView() {},
        addEventListener(k, f) {
          this.listeners[k] = f;
        },
        querySelectorAll() {
          return this.inputs;
        },
        insertAdjacentHTML(where, s) {
          this.innerHTML =
            where === "afterbegin" ? s + this.innerHTML : this.innerHTML + s;
        },
      });
    return elements.get(id);
  };
  const location = {
    pathname,
    search: options.search || "?source=task&record=original",
    assign() {
      throw Error("unexpected redirect");
    },
  };
  const context = vm.createContext({
    document: {
      getElementById: (id) =>
        pathname === "/"
          ? id.startsWith("overview-")
            ? element(id)
            : null
          : id.startsWith("center-")
            ? element(id)
            : null,
    },
    location,
    URLSearchParams,
    structuredClone,
    window: { confirm: () => true, history: { replaceState() {} } },
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      const p = await responder(url, options);
      return {
        ok: !p.error,
        status: p.error ? 422 : 200,
        json: async () => (p.error ? { detail: p.error } : p),
      };
    },
  });
  if (options.catalog)
    element("center-catalog").textContent = JSON.stringify(options.catalog);
  vm.runInContext(code, context);
  return {
    context,
    element,
    calls,
    ui: vm.runInContext("RunCenterUI", context),
  };
}

test("legacy anchors retain query identity and never execute a request", () => {
  assert.equal(
    shell.resolve("#backtest", "?stock=600519&start_date=2024-01-01"),
    "/backtests/new?stock=600519&start_date=2024-01-01#backtest",
  );
  assert.equal(shell.resolve("#stock-pools"), "/data/pools#stock-pools");
  assert.equal(shell.resolve("#unrecognized"), null);
});
test("history escapes values and preserves zeros and unknown times", () => {
  const html = ui.table([
    { ...row("<script>"), symbols: [], duration_seconds: 0 },
  ]);
  assert.ok(html.includes("&lt;script&gt;"));
  assert.ok(html.includes("历史范围未知"));
  assert.ok(html.includes("0 秒"));
  assert.ok(!html.includes("<script>"));
});
test("overview reuses one bounded dashboard read without creating plans or choosing another pool", async () => {
  for (const error of [false, true]) {
    const h = harness("/", async (url) =>
      url.includes("research-dashboard")
        ? error
          ? { error: "default pool missing" }
          : {
              pool: { name: "<script>" },
              context: {
                pool_revision: 3,
                window_start: "2024-01-01",
                as_of: "2024-01-07",
                timezone: "Asia/Shanghai",
              },
              symbols: ["600519"],
              sections: {
                announcements: { total: 0 },
                data_gaps: { total: 2, truncated: true },
              },
            }
        : { items: [], total: 0 },
    );
    await tick();
    assert.equal(
      h.calls.filter((c) => c.url.includes("research-dashboard")).length,
      1,
    );
    assert.equal(h.calls.length, 3);
    assert.ok(h.calls.every((c) => !c.options.method));
    assert.ok(
      h.calls.some((c) => c.url === "/api/v1/research-dashboard?page_size=1"),
    );
    if (error)
      assert.match(
        h.element("overview-pool").textContent,
        /default pool missing/,
      );
    else {
      assert.match(h.element("overview-pool").innerHTML, /&lt;script&gt;/);
      assert.match(h.element("overview-pool").innerHTML, /未记录/);
      assert.match(h.element("overview-pool").innerHTML, /2（截断）/);
    }
  }
});
test("late list success cannot replace a newer type filter", async () => {
  let release;
  const h = harness("/backtests/history", (url) =>
    url.includes("kind=screen")
      ? Promise.resolve({ items: [row("new")], total: 1 })
      : new Promise((r) => (release = r)),
  );
  h.element("center-kind").value = "screen";
  await h.ui.history();
  release({ items: [row("old")], total: 1 });
  await tick();
  assert.ok(h.element("center-list").innerHTML.includes("new"));
  assert.ok(!h.element("center-list").innerHTML.includes("old"));
  assert.ok(h.calls.every((c) => !c.options.method));
});
test("late list failure cannot clear the newer page", async () => {
  let release;
  const h = harness("/backtests/history", (url) =>
    url.includes("kind=screen")
      ? Promise.resolve({ items: [row("new")], total: 1 })
      : new Promise((r) => (release = r)),
  );
  h.element("center-kind").value = "screen";
  await h.ui.history();
  release({ error: "old failure" });
  await tick();
  assert.equal(h.element("center-error").textContent, "");
});
test("late detail response cannot replace another run identity", async () => {
  let release;
  const h = harness("/backtests/detail", (url) =>
    url.includes("new")
      ? Promise.resolve(row("new"))
      : new Promise((r) => (release = r)),
  );
  await h.ui.detail("source=task&record=new");
  release(row("old"));
  await tick();
  assert.ok(h.element("center-identity").innerHTML.includes("new"));
  assert.ok(!h.element("center-identity").innerHTML.includes("old"));
});
const copied = {
  kind: "factor_backtest",
  label: "财务因子回测",
  source_run_id: "original",
  params: { symbols: ["600519"], top_n: 1, commission_bps: 0 },
  schema: {
    properties: {
      symbols: { type: "array" },
      top_n: { type: "integer" },
      commission_bps: { type: "number" },
    },
  },
  warnings: [],
  missing_fields: [],
  endpoint: "/api/v1/backtests",
};
test("copy only reads, preserves exact parameters, and requires current preflight", async () => {
  const h = harness("/backtests/configure", async (url, options) =>
    url.includes("/copy?") ? copied : { ready: true, status: "ready" },
  );
  await tick();
  assert.equal(h.calls.length, 1);
  assert.equal(h.element("center-submit").disabled, true);
  await h.ui.preflight();
  const sent = JSON.parse(h.calls[1].options.body);
  assert.deepEqual(sent, {
    research_type: "factor_backtest",
    parameters: copied.params,
  });
  assert.equal(h.element("center-submit").disabled, false);
  h.element("center-copy-form").inputs = [
    { dataset: { param: "top_n", type: "integer" }, value: "2" },
  ];
  await h.ui.submit({ preventDefault() {} });
  assert.equal(h.calls.length, 2);
  assert.match(h.element("center-error").textContent, /预检/);
});
test("cancelled copy execution never POSTs research", async () => {
  const h = harness("/backtests/configure", async (url) =>
    url.includes("/copy?") ? copied : { ready: true },
  );
  await tick();
  await h.ui.preflight();
  h.context.window.confirm = () => false;
  await h.ui.submit({ preventDefault() {} });
  assert.equal(h.calls.length, 2);
});
test("duplicate execution is blocked while pending and after success", async () => {
  let finish;
  const h = harness("/backtests/configure", async (url) =>
    url.includes("/copy?")
      ? copied
      : url.includes("/preflight")
        ? { ready: true }
        : new Promise((r) => (finish = r)),
  );
  await tick();
  await h.ui.preflight();
  const first = h.ui.submit({ preventDefault() {} });
  await h.ui.submit({ preventDefault() {} });
  assert.equal(h.calls.length, 3);
  finish({ task_id: "new-task" });
  await first;
  await h.ui.submit({ preventDefault() {} });
  assert.equal(h.calls.length, 3);
});
test("failed preflight and missing-field acknowledgment prevent execution", async () => {
  const h = harness("/backtests/configure", async (url) =>
    url.includes("/copy?")
      ? {
          ...copied,
          missing_fields: ["commission_bps"],
          warnings: ["not saved"],
        }
      : { ready: false, status: "blocked" },
  );
  await tick();
  await h.ui.preflight();
  assert.equal(h.element("center-submit").disabled, true);
  await h.ui.submit({ preventDefault() {} });
  assert.equal(h.calls.length, 2);
  assert.match(h.element("center-error").textContent, /缺失参数/);
});
test("nulls in result tables remain unknown and untrusted text is escaped", () => {
  const html = ui.renderValue([{ value: null, source: "<img onerror=x>" }]);
  assert.match(html, /未记录/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img/);
});
test("fresh lab is independent, preserves legacy range query, and only reads on entry", async () => {
  const data = {
    ...copied,
    kind: "factor_lab",
    label: "因子实验",
    source_run_id: null,
    params: {
      symbols: [],
      factors: [{ code: "roe", weight: 2, direction: "desc" }],
      start_date: "2015-01-01",
      end_date: "2024-12-31",
    },
    schema: {
      properties: {
        symbols: { type: "array" },
        factors: { type: "array" },
        start_date: { type: "string" },
        end_date: { type: "string" },
      },
    },
  };
  const h = harness(
    "/backtests/factors",
    async (url) => (url.includes("configuration") ? structuredClone(data) : []),
    { search: "?stock=600519&start_date=2024-01-01" },
  );
  await tick();
  assert.ok(h.calls.every((c) => !c.options.method));
  assert.equal(
    h.calls[0].url,
    "/api/v1/run-center/configuration?kind=factor_lab",
  );
  assert.match(h.element("center-scope-fields").innerHTML, /600519/);
  assert.match(h.element("center-scope-fields").innerHTML, /2024-01-01/);
  assert.match(
    h.element("center-common-fields").innerHTML,
    /data-factor-editor/,
  );
  assert.equal(h.element("center-submit").disabled, true);
});
test("Chinese display preserves codes and unknown factor names remain escaped", async () => {
  const catalog = {
    factors: { roe: "加权净资产收益率", debt_asset_ratio: "资产负债率" },
    enums: { desc: "高值优先", asc: "低值优先", monthly: "按月" },
    fields: { factor: "因子" },
  };
  const h = harness(
    "/backtests/detail",
    async () => ({ ...row("one"), result: { summary: { factor_count: 2 } } }),
    { catalog },
  );
  await tick();
  assert.equal(h.ui.factorName("roe"), "加权净资产收益率（roe）");
  assert.match(
    h.ui.strategyName(
      "composite:roe+debt_asset_ratio|top=1|monthly|commission=5",
    ),
    /资产负债率/,
  );
  assert.match(h.ui.factorName("unknown"), /未知因子（unknown）/);
  h.element("center-copy-form").inputs = [
    { dataset: { param: "symbols", type: "symbols" }, value: "600519 000001" },
  ];
  assert.deepEqual(
    ui.readParams(h.element("center-copy-form"), { symbols: [] }).symbols,
    ["600519", "000001"],
  );
});
test("factor rows read exact codes, directions and relative weights without percent conversion", () => {
  const original = {
    factors: [{ code: "roe", weight: 2, direction: "desc" }],
    commission_bps_values: [0, 5],
  };
  const form = {
    querySelector: () => true,
    querySelectorAll: (s) =>
      s === "[data-param]"
        ? []
        : [
            {
              querySelector: (s) => ({
                value: s.includes("code")
                  ? "debt_asset_ratio"
                  : s.includes("weight")
                    ? "3.5"
                    : "asc",
              }),
            },
          ],
  };
  const params = ui.readParams(form, original);
  assert.deepEqual(params.factors, [
    { code: "debt_asset_ratio", weight: 3.5, direction: "asc" },
  ]);
  assert.equal(original.factors[0].weight, 2);
  assert.deepEqual(params.commission_bps_values, [0, 5]);
  assert.match(
    ui.factorRow({ code: "<script>", weight: 2, direction: "asc" }),
    /&lt;script&gt;/,
  );
});
test("daily signal editors preserve internal codes and numeric rule parameters", () => {
  assert.match(ui.factorName("return_20"), /20 日涨跌幅（return_20）/);
  assert.match(ui.factorName("ma_deviation_60"), /MA\(60\).*ma_deviation_60/);
  assert.match(
    ui.candidateFactorRow({ code: "<script>", weight: 1, direction: "desc" }),
    /&lt;script&gt;/,
  );
  assert.match(
    ui.signalRuleRow({
      kind: "ma_cross",
      operator: "cross_above",
      short_window: 5,
      long_window: 20,
    }),
    /上穿.*cross_above/,
  );
  const candidate = {
    querySelector: (selector) => ({
      value: selector.includes("code")
        ? "return_20"
        : selector.includes("weight")
          ? "2.5"
          : "desc",
    }),
  };
  const rule = {
    querySelector: (selector) => ({
      value: selector.includes("kind")
        ? "ma_cross"
        : selector.includes("operator")
          ? "cross_above"
          : selector.includes("short")
            ? "5"
            : selector.includes("long")
              ? "20"
              : "",
    }),
  };
  const form = {
    querySelector: (selector) =>
      selector === "[data-candidate-editor]" ||
      selector === '[data-signal-editor="buy"]',
    querySelectorAll: (selector) =>
      selector === "[data-param]"
        ? []
        : selector === ".candidate-factor-row"
          ? [candidate]
          : selector === '[data-rule-side="buy"]'
            ? [rule]
            : [],
  };
  const params = ui.readParams(form, {
    candidate_factors: [],
    buy_rules: [],
    sell_rules: [],
  });
  assert.deepEqual(params.candidate_factors, [
    { code: "return_20", weight: 2.5, direction: "desc" },
  ]);
  assert.deepEqual(params.buy_rules, [
    {
      kind: "ma_cross",
      operator: "cross_above",
      window: null,
      short_window: 5,
      long_window: 20,
      threshold: null,
    },
  ]);
  assert.deepEqual(params.sell_rules, []);
});
test("on-page review does not execute; final execution and duplicate guard are separate", async () => {
  const h = harness("/backtests/configure", async (url) =>
    url.includes("/copy?")
      ? structuredClone(copied)
      : url.includes("preflight")
        ? { ready: true }
        : { task_id: "one" },
  );
  await tick();
  await h.ui.preflight();
  h.element("center-review").querySelector = () => null;
  await h.ui.submit({ preventDefault() {} });
  assert.equal(h.calls.length, 2);
  assert.equal(h.element("center-review").hidden, false);
  assert.match(h.element("center-review-content").innerHTML, /600519/);
  await h.ui.submit({ preventDefault() {} }, true);
  await h.ui.submit({ preventDefault() {} }, true);
  assert.equal(h.calls.length, 3);
  assert.match(
    h.element("center-submitted-link").innerHTML,
    /source=task&amp;record=one/,
  );
});
test("failed second preflight clears previous approval", async () => {
  let fail = false;
  const h = harness("/backtests/configure", async (url) =>
    url.includes("/copy?")
      ? structuredClone(copied)
      : fail
        ? { error: "offline" }
        : { ready: true },
  );
  await tick();
  await h.ui.preflight();
  fail = true;
  await h.ui.preflight();
  await h.ui.submit({ preventDefault() {} }, true);
  assert.equal(h.calls.length, 3);
  assert.match(h.element("center-error").textContent, /预检/);
});
test("late detail error cannot overwrite a newer identity", async () => {
  let release;
  const h = harness("/backtests/detail", (url) =>
    url.includes("new")
      ? Promise.resolve(row("new"))
      : new Promise((r) => (release = r)),
  );
  await h.ui.detail("source=task&record=new");
  release({ error: "old failed" });
  await tick();
  assert.equal(h.element("center-error").textContent, "");
  assert.match(h.element("center-identity").innerHTML, /new/);
});
test("fresh pool selection ignores old scope and blocks failed scope until explicit selection", async () => {
  let release;
  const h = harness("/backtests/new", async (url) => {
    if (url.includes("configuration")) return structuredClone(copied);
    if (url.endsWith("include_archived=false"))
      return [
        { pool_id: "old", current_revision: 1, name: "old" },
        { pool_id: "new", current_revision: 2, name: "new" },
      ];
    if (url.includes("/old?")) return new Promise((r) => (release = r));
    if (url.includes("/bad?")) return { error: "revision missing" };
    return { symbols: ["000001", "300750"] };
  });
  await tick();
  const select = h.element("center-select-pool");
  select.value = "old:1";
  const old = select.listeners.change();
  assert.throws(() => h.ui.currentParams(), /读取中/);
  select.value = "new:2";
  await select.listeners.change();
  release({ symbols: ["600519"] });
  await old;
  assert.equal(h.ui.currentParams().stock_pool_id, "new");
  assert.equal(h.ui.currentParams().stock_pool_revision, 2);
  assert.equal(h.ui.currentParams().symbols, undefined);
  assert.match(h.element("center-pool-preview").textContent, /000001 300750/);
  select.value = "bad:3";
  await select.listeners.change();
  assert.throws(() => h.ui.currentParams(), /revision missing/);
  select.value = "manual";
  await select.listeners.change();
  assert.equal(h.ui.currentParams().stock_pool_id, null);
});
test("copy configuration late failure cannot replace a newer record", async () => {
  let release;
  const h = harness("/backtests/configure", (url) =>
    url.includes("new")
      ? Promise.resolve(structuredClone({ ...copied, source_run_id: "new" }))
      : new Promise((r) => (release = r)),
  );
  await h.ui.configure("source=index&record=new");
  release({ error: "old missing" });
  await tick();
  assert.equal(h.element("center-error").textContent, "");
  assert.match(h.element("center-copy-source").textContent, /new/);
});
