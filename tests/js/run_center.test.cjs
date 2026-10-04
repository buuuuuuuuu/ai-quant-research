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
    calls = [],
    assigned = [];
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
    assign(value) {
      if (!options.allowRedirect) throw Error("unexpected redirect");
      assigned.push(value);
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
    assigned,
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
  assert.match(h.element("center-action-feedback").textContent, /预检/);
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
  assert.match(h.element("center-action-feedback").textContent, /缺失参数/);
});
test("nulls in result tables remain unknown and untrusted text is escaped", () => {
  const html = ui.renderValue([{ value: null, source: "<img onerror=x>" }]);
  assert.match(html, /未记录/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img/);
});
test("blocked preflight explains problems and never calls it an unfilled trade", () => {
  const html = ui.renderPreflight({
    status: "blocked",
    status_label: "预检阻断",
    ready: false,
    blockers: ["<img onerror=x> 数据缺失"],
    blocking_check_count: 1,
    affected_symbols: ["300750"],
    recommendations: [
      {
        title: "研究开始日早于股票上市日",
        explanation: "300750（2024-03-01 上市）",
        action: "将开始日期调整到 2024-03-01 之后。",
      },
    ],
    checks: [
      {
        symbol: "300750",
        dataset: "daily_bar",
        adjustment: "qfq",
        status: "missing",
        required_start: "2024-03-01",
        required_end: "2024-03-08",
        actual_start: null,
        actual_end: null,
        missing_count: 5,
        list_date: "2024-03-01",
        reason: "缺少行情",
        gap_details: [{date:"2024-03-05",classification:"unverified_gap",reason:"<暂时未知>",source:"fixture"}],
      },
    ],
  });
  assert.match(html, /暂不能提交/);
  assert.match(html, /2024-03-01/);
  assert.match(html, /将开始日期调整/);
  assert.match(html, /默认检查结果/);
  assert.match(html, /暂时无法核验/);
  assert.match(html, /&lt;暂时未知&gt;/);
  assert.doesNotMatch(html, /未成交/);
  assert.doesNotMatch(html, /<img onerror/);
  assert.match(html, /&lt;img onerror=x&gt;/);
});
test("preflight screen renders actionable blocked summary and remains disabled", async () => {
  const h = harness("/backtests/configure", async (url) =>
    url.includes("/copy?")
      ? copied
      : {
          status: "blocked",
          ready: false,
          blockers: ["行情缺失"],
          recommendations: [
            {
              title: "股票日线覆盖不足",
              explanation: "需要前复权和不复权行情。",
              action: "先同步日线，再重新预检。",
            },
          ],
        },
  );
  await tick();
  await h.ui.preflight();
  assert.equal(h.element("center-submit").disabled, true);
  assert.match(h.element("center-preflight-result").innerHTML, /先同步日线/);
  assert.doesNotMatch(h.element("center-preflight-result").innerHTML, /未成交/);
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
    terms: {
      nav: {
        label: "累计净值",
        short: "把初始资金看作 1.00。",
        professional: "累计净值（NAV）",
        calculation: "账户总资产除以初始资金。",
        example: "1.08 表示增长约 8%。",
        limitations: "不是股票价格。",
        code: "portfolio_nav",
      },
    },
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
  assert.match(h.ui.factorName("unknown"), /暂无中文名称（unknown）/);
  const help = h.ui.termHelp("nav");
  assert.match(help, /aria-expanded="false"/);
  assert.match(help, /data-term-close/);
  assert.match(help, /账户总资产除以初始资金/);
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
  assert.match(h.element("center-action-feedback").textContent, /预检/);
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

test("daily templates only contain legal rule parameters and do not contain scope", () => {
  for (const template of Object.values(ui.dailyTemplates)) {
    assert.equal("symbols" in template, false);
    assert.equal("stock_pool_id" in template, false);
    assert.equal(ui.validateDailySignal({ ...template, candidate_factors: [{ code: "roe", weight: 1, direction: "desc" }], candidate_count: 2, max_positions: 1 }, 2).length, 0);
  }
});

test("single stock permits no factor while multi-stock still needs one", () => {
  const params = {
    candidate_factors: [], candidate_count: 1, max_positions: 1,
    buy_rules: ui.dailyTemplates.ma_cross.buy_rules,
    sell_rules: ui.dailyTemplates.ma_cross.sell_rules,
  };
  const issues = ui.dailySignalIssues(params, 2);
  assert.equal(issues.length, 1);
  assert.equal(issues[0].code, "empty_candidate_factors");
  assert.equal(issues[0].target, "center-daily-candidates");
  assert.match(issues[0].message, /多股范围.*增加候选排序因子/);
  assert.deepEqual(ui.dailySignalIssues(params, 1), []);
  assert.match(ui.dailySignalSummary(params, 1), /单只冻结股票直接参与每日信号检查/);
  assert.doesNotMatch(ui.dailySignalSummary(params), /合成得分/);
  assert.equal(ui.dailySignalIssues({ ...params, candidate_factors: [{ code: "roe", weight: 1, direction: "desc" }] }, 1).length, 0);
});

test("daily validation and summary explain periods, weights, and next-open execution", () => {
  const params = {
    symbols: ["600519"], start_date: "2024-01-01", end_date: "2024-12-31",
    candidate_factors: [{ code: "roe", direction: "desc", weight: 1 }],
    candidate_count: 1, max_positions: 1, candidate_refresh: "monthly",
    buy_rule_mode: "all", sell_rule_mode: "any", exit_on_candidate_drop: false,
    buy_rules: [{ kind: "ma_cross", operator: "cross_above", short_window: 20, long_window: 5 }],
    sell_rules: [],
  };
  const errors = ui.validateDailySignal(params, 1);
  assert.match(errors.join(" "), /短周期当前为 20，长周期为 5/);
  assert.match(errors.join(" "), /卖出规则为空/);
  params.buy_rules = ui.dailyTemplates.ma_cross.buy_rules;
  params.sell_rules = ui.dailyTemplates.ma_cross.sell_rules;
  const summary = ui.dailySignalSummary(params);
  assert.match(summary, /相对权重 1/);
  assert.match(summary, /候选名单更新与每日信号检查是两个过程/);
  assert.match(summary, /下一交易日开盘/);
});

test("daily issues identify their section and offer explicit non-silent fixes", () => {
  const params = {
    start_date: "2025-12-31", end_date: "2025-01-01",
    candidate_count: 20, max_positions: 25,
    buy_rules: ui.dailyTemplates.ma_cross.buy_rules,
    sell_rules: ui.dailyTemplates.ma_cross.sell_rules,
  };
  const issues = ui.dailySignalIssues(params, 11);
  assert.equal(issues.find((item) => item.code === "candidate_exceeds_scope").fix.value, 11);
  assert.equal(issues.find((item) => item.code === "positions_exceed_candidates").target, "center-execution-details");
  assert.equal(issues.find((item) => item.code === "invalid_date_range").field, "start_date");
});

test("preflight groups duplicate root causes and renders typed actions", () => {
  const report = {
    status: "blocked", ready: false, blocking_check_count: 23,
    recommendations: [
      { code: "unfinished_end_date", title: "结束日期不完整", explanation: "当前值超出边界", action: "改为完整日期", action_kind: "update_field", field: "end_date", suggested_value: "2026-09-28" },
      { code: "unfinished_end_date", title: "重复项", explanation: "不应显示", action: "重复" },
      { code: "benchmark_coverage", title: "基准缺失", explanation: "需要数据", action: "查看计划", action_kind: "data_plan", network_required_for_resolution: true },
    ],
  };
  assert.equal(ui.preflightProblems(report).length, 2);
  const html = ui.renderPreflight(report);
  assert.match(html, /2 类问题 · 23 项原始检查未通过/);
  assert.match(html, /data-field="end_date"/);
  assert.match(html, /查看处理计划（只生成计划、不联网）/);
  assert.doesNotMatch(html, /重复项/);
});

test("initial frozen pool fetches the exact revision before showing its size", async () => {
  const params = {
    stock_pool_id: "pool-fixed", stock_pool_revision: 4,
    start_date: "2025-01-01", end_date: "2025-12-31",
    candidate_factors: [{ code: "roe", weight: 1, direction: "desc" }],
    candidate_count: 2, candidate_refresh: "monthly",
    buy_rules: ui.dailyTemplates.ma_cross.buy_rules,
    sell_rules: ui.dailyTemplates.ma_cross.sell_rules,
    buy_rule_mode: "all", sell_rule_mode: "any",
    exit_on_candidate_drop: false, max_positions: 1,
  };
  const h = harness("/backtests/daily-signals", async (url) => {
    if (url.includes("/configuration?")) return {
      kind: "daily_signal", label: "日线信号策略", source_run_id: null,
      params, scope: [], schema: { properties: Object.fromEntries(Object.keys(params).map((key) => [key, {}])) },
      warnings: [], missing_fields: [], endpoint: "/api/v1/daily-signal-strategies",
    };
    if (url.includes("/stock-pools?")) return [{ pool_id: "pool-fixed", current_revision: 5, name: "股票池" }];
    if (url.endsWith("/stock-pools/pool-fixed?revision=4")) return { symbols: ["600519", "000001"] };
    throw Error("unexpected request " + url);
  });
  await tick();
  await tick();
  assert.ok(h.calls.some((call) => call.url.endsWith("/stock-pools/pool-fixed?revision=4")));
  assert.match(h.element("center-pool-preview").textContent, /r4 · 2 只/);
  assert.doesNotMatch(h.element("center-daily-errors").innerHTML, /只有 0 只/);
});

test("daily timeline links only saved identities and preserves missing values", () => {
  const rows = ui.dailySignalTimeline(
    [{ signal_date: "2024-01-02", symbol: "600519", action: "buy", signal_price: 10 }],
    [{ signal_date: "2024-01-02", symbol: "600519", side: "buy", status: "blocked", reason: "t_plus_one" }],
  );
  assert.equal(rows.length, 1);
  const html = ui.renderDailyTimeline(rows);
  assert.match(html, /t_plus_one/);
  assert.match(html, /未记录/);
  assert.doesNotMatch(html, />0</);
});

test("completed data plan restores its frozen pool before automatic recheck", async () => {
  const params = {
    symbols: null,
    stock_pool_id: "pool-frozen",
    stock_pool_revision: 2,
    start_date: "2025-01-01",
    end_date: "2025-12-31",
    candidate_factors: [{ code: "roe", weight: 1, direction: "desc" }],
    candidate_count: 1,
    candidate_refresh: "monthly",
    buy_rules: ui.dailyTemplates.ma_cross.buy_rules,
    sell_rules: ui.dailyTemplates.ma_cross.sell_rules,
    buy_rule_mode: "all",
    sell_rule_mode: "any",
    exit_on_candidate_drop: false,
    max_positions: 1,
  };
  const configuration = {
    kind: "daily_signal",
    label: "日线信号策略",
    source_run_id: null,
    params: { symbols: [] },
    schema: { properties: Object.fromEntries(Object.keys(params).map((key) => [key, {}])) },
    warnings: [],
    missing_fields: [],
    endpoint: "/api/v1/daily-signal-strategies",
    scope: [],
  };
  const plan = {
    plan_id: "data-plan-frozen",
    version: 1,
    input_sha256: "a".repeat(64),
    status: "succeeded",
    input: {
      research_type: "daily_signal",
      parameters: params,
      resolved_symbols: ["688111"],
      symbols_sha256: "b".repeat(64),
    },
    items: [{ symbol: "csi000300", dataset: "benchmark_daily_bar", status: "published" }],
    task: { status: "succeeded", progress: 100 },
  };
  const h = harness(
    "/backtests/daily-signals",
    async (url) => {
      if (url.includes("/configuration?")) return structuredClone(configuration);
      if (url.endsWith("/plans/data-plan-frozen/preflight"))
        return { ready: true, status: "ready", complete: true };
      if (url.endsWith("/plans/data-plan-frozen")) return structuredClone(plan);
      if (url.includes("/stock-pools?"))
        return [{ pool_id: "pool-frozen", current_revision: 3, name: "当前版本" }];
      if (url.endsWith("/stock-pools/pool-frozen?revision=2"))
        return { pool_id: "pool-frozen", revision: 2, symbols: ["688111"] };
      throw Error("unexpected request " + url);
    },
    { search: "?data_plan=data-plan-frozen&end_date=2026-10-01" },
  );
  await tick();
  await tick();
  assert.equal(h.element("center-error").textContent, "");
  assert.equal(h.ui.currentParams().stock_pool_id, "pool-frozen");
  assert.equal(h.ui.currentParams().stock_pool_revision, 2);
  assert.equal(h.ui.currentParams().end_date, "2025-12-31");
  assert.equal(h.ui.currentParams().symbols, undefined);
  assert.match(h.element("center-pool-preview").textContent, /r2 · 1 只：688111/);
  assert.match(h.element("center-copy-warning").textContent, /URL 中的 end_date 未覆盖该计划/);
  assert.equal(h.element("center-submit").disabled, false);
  assert.equal(
    h.calls.filter((call) => call.url.endsWith("/plans/data-plan-frozen")).length,
    1,
  );
  assert.equal(
    h.calls.filter((call) => call.url === "/api/v1/daily-signal-strategies").length,
    0,
  );
  h.element("center-copy-form").inputs = [
    { dataset: { param: "end_date", type: "string" }, value: "2026-10-01" },
  ];
  h.element("center-copy-form").listeners.input();
  assert.match(h.element("center-preflight-result").textContent, /结束日期 2025-12-31 → 2026-10-01/);
  assert.match(h.element("center-preflight-result").textContent, /旧计划仍保留，但不能代替当前参数/);
  assert.equal(h.element("center-data-plan").hidden, true);
});

test("data plan restore rejects missing or mismatched frozen identity", () => {
  const base = { params: { symbols: [] }, warnings: [] };
  assert.throws(
    () => ui.restoreDataPlanConfiguration(base, { input: { research_type: "factor_lab" } }, "daily_signal"),
    /类型/,
  );
  assert.throws(
    () => ui.restoreDataPlanConfiguration(base, { input: { research_type: "daily_signal", parameters: {} } }, "daily_signal"),
    /冻结股票范围/,
  );
  assert.deepEqual(
    ui.normalizedParameterIdentity({ stock_pool_id: "pool-a", symbols: null }),
    { stock_pool_id: "pool-a" },
  );
});

test("nav chart keeps axes, legend, end ticks, gaps, and selected markers", () => {
  const rows = [
    { date: "2024-01-02", portfolio_nav: 1, benchmark_nav: 1, cash: 1000, position_value: 0, portfolio_value: 1000, total_cost: 0 },
    { date: "2024-01-03", portfolio_nav: null, benchmark_nav: 1.01 },
    { date: "2024-01-04", portfolio_nav: 1.02, benchmark_nav: 1.01, cash: 400, position_value: 620, portfolio_value: 1020, total_cost: 2 },
  ];
  const model = ui.navChartModel(rows, 2);
  assert.equal(model.xTicks[0].label, "2024-01-02");
  assert.equal(model.xTicks.at(-1).label, "2024-01-04");
  assert.equal(model.segments.portfolio_nav.length, 2);
  assert.equal(model.yTicks.length, 6);
  const html = ui.renderNavChart(rows, 2);
  assert.match(html, /累计净值（初始值=1.00）/);
  assert.match(html, /策略累计净值/);
  assert.match(html, /基准累计净值/);
  assert.match(html, /nav-selection/);
  assert.match(html, /strategy-point/);
  assert.match(html, /benchmark-point/);
  assert.doesNotMatch(html, />价格</);
});

test("daily result separates executions, unfilled orders, and final positions", () => {
  const view = ui.dailyResultView(
    {
      series: [
        { date: "2024-01-03", cash: 380, position_value: 650, portfolio_value: 1030, portfolio_nav: 1.03, benchmark_nav: 1.01, total_cost: 2 },
      ],
      orders: [
        { symbol: "600519", side: "buy", status: "executed", shares: 100, notional: 620, cost: 2 },
        { symbol: "600519", side: "sell", status: "blocked", reason: "t_plus_one", next_action: "retry_next_trading_day" },
      ],
      positions: [
        { date: "2024-01-02", symbol: "600519", market_value: 610 },
        { date: "2024-01-03", symbol: "600519", shares: 100, price: 6.5, market_value: 650, pending_exit: true },
      ],
      diagnostics: { open_positions: ["600519"], open_exit_intents: ["600519"] },
      metrics: { total_return: 0.03 },
    },
    { initial_capital: 1000 },
  );
  assert.equal(view.executed.length, 1);
  assert.equal(view.unfilled.length, 1);
  assert.equal(view.account.buy_execution_count, 1);
  assert.equal(view.account.sell_execution_count, 0);
  assert.equal(view.finalPositions.length, 1);
  assert.equal(view.finalPositions[0].asset_ratio, 650 / 1030);
  assert.match(ui.renderExecutedTrades(view.executed), /620\.00/);
  assert.doesNotMatch(ui.renderExecutedTrades(view.executed), /t_plus_one/);
  assert.match(ui.renderUnfilledOrders(view.unfilled), /t_plus_one/);
});

test("price signal chart keeps adjusted prices separate from raw execution events", () => {
  const payload = {
    symbol: "600519",
    price_adjustment: "qfq",
    series: [
      { date: "2024-01-02", symbol: "600519", signal_close: 10, ma_2: null, price_adjustment: "qfq" },
      { date: "2024-01-03", symbol: "600519", signal_close: null, ma_2: null, price_adjustment: "qfq" },
      { date: "2024-01-04", symbol: "600519", signal_close: 12, ma_2: 11, price_adjustment: "qfq" },
      { date: "2024-01-05", symbol: "600519", signal_close: 13, ma_2: 12.5, price_adjustment: "qfq" },
    ],
    signals: [{ signal_date: "2024-01-04", action: "buy", signal_price: 12 }],
    orders: [{ planned_execution_date: "2024-01-05", execution_date: "2024-01-05", side: "buy", status: "executed", execution_price: 101 }],
  };
  const model = ui.priceChartModel(payload, 3);
  assert.equal(model.segments.signal_close.length, 2);
  assert.deepEqual(model.events.map((item) => item.kind), ["buy-signal", "planned", "executed"]);
  const html = ui.renderPriceSignalChart(payload, 3);
  assert.match(html, /价格（信号复权口径）/);
  assert.match(html, /点击曲线附近或事件圆点/);
  assert.match(html, /data-price-event-date="2024-01-05"/);
  assert.match(html, /price-selected-point/);
  assert.match(html, /price-event executed/);
  assert.doesNotMatch(html, /101\.00/);
});

test("price chart selects actual saved dates across sampling and missing prices", () => {
  const payload = {
    symbol: "600519", price_adjustment: "qfq", price_range: { minimum: 10, maximum: 16 },
    date_index: ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"],
    missing_dates: { signal_close: ["2024-01-04"], ma_2: ["2024-01-03", "2024-01-04"] },
    series: [
      { date: "2024-01-02", signal_close: 10, ma_2: null },
      { date: "2024-01-05", signal_close: 15, ma_2: 13 },
      { date: "2024-01-08", signal_close: 16, ma_2: 14 },
    ],
    signals: [{ signal_date: "2024-01-03", action: "buy" }, { signal_date: "2024-01-03", action: "sell" }],
    orders: [{ planned_execution_date: "2024-01-04", execution_date: "2024-01-05", status: "executed", execution_price: 108 }],
  };
  const model = ui.priceChartModel(payload, 1);
  assert.equal(model.selected, null); // The exact unsampled row is read by the day endpoint.
  assert.equal(model.dates[0], "2024-01-02");
  assert.equal(model.dates.at(-1), "2024-01-08");
  assert.equal(model.segments.signal_close.length, 2);
  assert.equal(model.events.filter((event) => event.date === "2024-01-03").length, 2);
  const html = ui.renderPriceSignalChart(payload, 1, 3, "all", { date: "2024-01-03", price: { signal_close: 11 } });
  assert.match(html, /price-selected-point price-close/);
  assert.match(html, /2024-01-03/);
  assert.doesNotMatch(html, /108\.00/);
  assert.equal(ui.nearestPriceDateIndex(payload.date_index, "2024-01-06"), 3);
});

test("day detail separates saved signals, planned instructions, executions and missing values", () => {
  const day = {
    date: "2024-01-03", symbol: "600519", price: { signal_close: null, ma_2: 11 },
    signals: [{ action: "buy", signal_price: null, rule_evidence: '<script>alert(1)</script>', planned_execution_date: "2024-01-04" }],
    orders: [
      { signal_date: "2024-01-02", planned_execution_date: "2024-01-03", execution_date: "2024-01-04", side: "buy", status: "executed", execution_price: 101, shares: 100 },
      { signal_date: "2024-01-02", planned_execution_date: "2024-01-03", execution_date: "2024-01-03", side: "sell", status: "unfilled", reason: "t_plus_one", shares: null },
    ], account: null,
  };
  const html = ui.renderPriceDayDetail(day);
  assert.match(html, /当天尚无实际尝试记录/);
  assert.match(html, /当日尝试但未成交/);
  assert.match(html, /当天账户快照未记录/);
  assert.match(html, /未记录/);
  assert.doesNotMatch(html, /101\.00/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(ui.renderPriceDayPreview(day), /当天产生 1 条新信号/);
  const explained = ui.renderPriceDayDetail({ ...day, signals: [{ action: "buy", rule_evidence: JSON.stringify([{ rule: { kind: "ma_cross", operator: "cross_above", short_window: 5, long_window: 20 }, matched: true, values: { ma_5: 10, ma_20: 9 } }]) }] });
  assert.match(explained, /MA5从下方向上穿越 MA20/);
  assert.match(explained, /ma_5 10/);
});

test("day selection ignores an older date response and keeps the newer card", async () => {
  let releaseOld;
  const record = { ...row("daily"), kind: "daily_signal", label: "日线信号策略", status: "succeeded", run_id: "daily-signal-20240108T000000Z-00000001", spec: { symbols: ["600519"], initial_capital: 1000 }, result: {} };
  const chart = {
    symbol: "600519", price_adjustment: "qfq", date_index: ["2024-01-02", "2024-01-03"],
    series: [{ date: "2024-01-02", signal_close: 10 }, { date: "2024-01-03", signal_close: 11 }],
    signals: [{ signal_date: "2024-01-03", action: "buy" }], orders: [],
  };
  const h = harness("/backtests/detail", (url) => {
    if (url.includes("/run-center/record?")) return record;
    if (url.endsWith("/daily-signal-20240108T000000Z-00000001")) return { spec: record.spec, series: [], orders: [], signals: [], positions: [], diagnostics: {} };
    if (url.includes("/price-signals/day")) {
      const date = new URL(url, "http://local.test").searchParams.get("date");
      if (date === "2024-01-02") return new Promise((resolve) => { releaseOld = resolve; });
      return { date, symbol: "600519", price: { signal_close: 11 }, signals: [{ action: "buy" }], orders: [], account: null };
    }
    if (url.includes("/price-signals?")) return chart;
    return {};
  });
  h.element("center-price-symbol").value = "600519";
  h.element("center-price-window").value = "all";
  h.element("center-price-events").value = "all";
  h.element("center-price-detail").hidden = true;
  await h.ui.detail("source=index&record=daily");
  await tick();
  assert.match(h.element("center-price-peek").innerHTML, /2024-01-03/);
  h.element("center-price-position").listeners.input({ target: { value: "0" } });
  await tick();
  h.element("center-price-position").listeners.input({ target: { value: "1" } });
  await tick();
  releaseOld({ date: "2024-01-02", symbol: "600519", price: { signal_close: 10 }, signals: [], orders: [], account: null });
  await tick();
  assert.match(h.element("center-price-peek").innerHTML, /2024-01-03/);
  assert.equal(h.element("center-price-position").value, "1");
  assert.equal(h.element("center-price-date").value, "2024-01-03");
  h.element("center-price-chart").listeners.click({
    clientX: 76,
    target: { closest: (selector) => selector === "[data-price-plot]" ? {} : null,
      ownerSVGElement: { getBoundingClientRect: () => ({ left: 0, width: 900 }) } },
  });
  assert.equal(h.element("center-price-position").value, "0");
  h.element("center-price-chart").listeners.click({
    target: { closest: (selector) => selector === "[data-price-event-date]" ? { getAttribute: () => "2024-01-03" } : null },
  });
  assert.equal(h.element("center-price-position").value, "1");
});

test("event-only chart does not invent a signal price", () => {
  const html = ui.renderPriceSignalChart({
    symbol: "600519", date_index: ["2024-01-02"],
    series: [{ date: "2024-01-02", signal_close: null }],
    signals: [], orders: [{ execution_date: "2024-01-02", status: "unfilled" }],
  });
  assert.match(html, /price-event blocked/);
  assert.doesNotMatch(html, /price-selected-point/);
  assert.match(html, /价格（信号复权口径）/);
});

test("trade audit and account audit use saved values and preserve unknowns", () => {
  const trade = ui.renderExecutedTrades([{ symbol: "600519", side: "buy", status: "executed", shares: 100, notional: 1000, cost: 3, cash_before: 2000, cash_after: 997, position_shares_before: 0, position_shares_after: 100, commission_cost: 1, slippage_cost: 2, stamp_duty_cost: 0, signal_id: "signal-000001" }]);
  assert.match(trade, /2,000\.00 → 997\.00/);
  assert.match(trade, /signal-000001/);
  const audit = ui.renderAuditSummary({ status: "warning", checked_dates: 1, check_count: 6, failure_count: 1, failures: [{ date: "2024-01-02", check: "cash_plus_positions_equals_total_assets", expected_value: 100, actual_value: 101, difference: 1, tolerance: 0.000001 }] });
  assert.match(audit, /完成但有差异/);
  assert.match(audit, /cash_plus_positions_equals_total_assets/);
  assert.match(ui.renderReproducibility({ git_commit_sha: null }), /未记录/);
});

test("data plan separates completed gaps from real download failures", () => {
  const completed = {
    plan_id: "data-plan-gap", version: 1, input_sha256: "a".repeat(64), status: "failed",
    input: { symbols_sha256: "b".repeat(64), resolved_symbols: ["600022"], parameters: { missing_policy: "exclude" } },
    report: { complete: false, checks: [{ dataset: "financial_metric", satisfied: false }] },
    outcome: "completed_with_gaps",
    execution_summary: { total: 2, published: 1, verified: 1, failed: 0, pending: 0, unknown: 0, retryable: 0 },
    items: [{ symbol: "600022", dataset: "financial_metric", status: "verified", reason: "21 个研究时点缺少已公开指标" }],
    task: { status: "failed", progress: 95, error: "补齐已执行，但研究数据仍有缺口" },
  };
  const html = ui.renderDataPlan(completed);
  assert.match(html, /同步已完成，但最终检查仍有数据缺口/);
  assert.match(html, /排除缺失历史时点，不会填零/);
  assert.match(html, /计划保存的历史预检快照/);
  assert.match(html, /按需查看完整历史快照/);
  assert.doesNotMatch(html, /研究数据预检：暂不能提交/);
  assert.match(html, /financial_metric/);
  assert.match(html, /执行结束（存在问题） · 100%/);
  assert.doesNotMatch(html, /95%/);
  assert.equal(ui.dataPlanExecutionSummary(completed).retryable, 0);

  const failed = {...completed,outcome:"download_failed",execution_summary:{total:2,published:1,verified:0,failed:1,pending:0,unknown:0,retryable:1}};
  assert.match(ui.renderDataPlan(failed),/存在真实同步失败/);
  assert.match(ui.renderDataPlan(failed),/已发布或已验证项目不会重新下载/);
});

test("preflight merges identical none and qfq gaps only in the display layer", () => {
  const details = [
    { date: "2026-01-02", classification: "unverified_gap" },
    { date: "2026-01-05", classification: "verified_suspension" },
  ];
  const checks = ["none", "qfq"].map((adjustment) => ({
    symbol: "000333", security_id: "SZSE.000333", dataset: "daily_bar",
    adjustment, required_start: "2025-01-01", required_end: "2026-01-31",
    satisfied: false, severity: "blocking", reason: "1 个交易日缺口仍需核验；1 个日期已核验为停牌",
    gap_details: details,
  }));
  const report = {
    ready: false, status: "blocked", checks,
    blockers: checks.map((check) => `${check.symbol} ${check.dataset}: ${check.reason}`),
  };
  const groups = ui.preflightBlockingGroups(report);
  assert.equal(groups.length, 1);
  assert.match(groups[0].text, /none\/qfq共同/);
  const html = ui.renderPreflight(report);
  assert.match(html, /1 组；底层 2 项/);
  assert.match(html, /data-exclude-problem="000333"/);
});

test("trust summary escapes excluded reasons and keeps unknown values unrecorded", () => {
  const html = ui.renderTrustSummary({
    scope: { frozen_count: 1, excluded: [{symbol: "000333", reason: '<img src=x onerror="boom()">'}] },
    per_symbol: [{symbol: "600519", buy_count: 1, sell_count: 0, buy_notional: 100, sell_notional: 0, total_cost: 2}],
    drawdown_period: null, preflight: null, limitation: "缺少数据",
  });
  assert.match(html, /&lt;img src=x onerror=&quot;boom\(\)&quot;&gt;/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /单笔盈亏和各股票收益贡献当前数据不足/);
  assert.match(html, /600519/);
  assert.match(html, /未记录/);
});

test("parameter differences name date and pool identity changes", () => {
  assert.deepEqual(
    ui.parameterDifferences(
      { start_date: "2025-01-01", end_date: "2026-10-01", stock_pool_revision: 20 },
      { start_date: "2025-01-01", end_date: "2026-09-30", stock_pool_revision: 19 },
    ),
    [
      { field: "end_date", label: "结束日期", previous: "2026-09-30", current: "2026-10-01" },
      { field: "stock_pool_revision", label: "股票池 revision", previous: 19, current: 20 },
    ],
  );
});

test("completed data plan exposes explicit bounded gap verification separately", () => {
  const plan = {
    plan_id: "data-plan-gaps",
    version: 1,
    input_sha256: "a".repeat(64),
    status: "succeeded",
    input: {
      symbols_sha256: "b".repeat(64),
      resolved_symbols: ["000333"],
      parameters: {},
    },
    report: { complete: false, checks: [] },
    outcome: "completed_with_gaps",
    execution_summary: {
      total: 1, published: 1, verified: 0, failed: 0, pending: 0,
    },
    items: [{
      symbol: "000333", dataset: "daily_bar", adjustment: "qfq",
      start_date: "2024-01-01", end_date: "2024-12-31", status: "published",
    }],
    gap_verification: {
      status: "available",
      total_dates: 2,
      verified_suspension: 0,
      missing_bar: 0,
      calendar_mismatch: 0,
      unverified_gap: 2,
      requires_confirmation: true,
      targets: [{
        symbol: "000333",
        security_id: "SZSE.000333",
        adjustments: ["none", "qfq"],
        required_start: "2024-01-01",
        required_end: "2024-12-31",
        unverified_count: 2,
        details: [{
          date: "2024-02-01",
          classification: "verified_suspension",
          adjustments: ["none", "qfq"],
          reason: "官方公告形成闭合停复牌区间",
          source: "cninfo:official_announcement_chain",
          evidence: {
            source_type: "official_announcement_chain",
            source: "cninfo:official_announcement_chain",
            suspension_start: "2024-02-01",
            resume_date: "2024-02-02",
            announcement_ids: ["safe-id"],
            announcement_titles: ["<停牌公告>"],
            attachment_urls: ["https://static.cninfo.com.cn/safe.pdf"],
            parser_version: "m12e4-v1",
            verified_at: "2024-02-03T00:00:00Z",
            covered_dates: ["2024-02-01"],
          },
        }],
        details_truncated: 1,
      }],
      tasks: [],
    },
  };
  const html = ui.renderDataPlan(plan);
  assert.match(html, /交易日缺口核验/);
  assert.match(html, /暂时无法核验 2/);
  assert.match(html, /2024-02-01/);
  assert.match(html, /查看官方公告证据链/);
  assert.match(html, /safe-id/);
  assert.match(html, /static\.cninfo\.com\.cn\/safe\.pdf/);
  assert.doesNotMatch(html, /<停牌公告>/);
  assert.match(html, /不生成K线/);
  assert.ok(html.indexOf("</table></div>") < html.indexOf("gap-verification-panel"));
  assert.ok(html.indexOf("gap-verification-panel") < html.indexOf("data-plan-final-report"));
  assert.equal(ui.gapVerificationActive(plan), false);
  plan.gap_verification.tasks = [{ status: "queued" }];
  assert.equal(ui.gapVerificationActive(plan), true);
});

test("verification-only draft plan never offers another price download", () => {
  const plan = {
    plan_id: "data-plan-verify-only", version: 1,
    input_sha256: "a".repeat(64), status: "draft", outcome: "verification_required",
    input: { symbols_sha256: "b".repeat(64), resolved_symbols: ["000333", "600022"], parameters: {} },
    report: { checked_at: "2026-10-01T00:00:00Z", blockers: ["one", "two", "three", "four"], affected_symbols: ["000333", "600022"] },
    items: [],
    available_actions: { sync_required: false, verification_required: true, calendar_attention: false, ready: false },
    gap_verification: {
      status: "available", total_dates: 107, verified_suspension: 10,
      missing_bar: 0, calendar_mismatch: 0, unverified_gap: 97,
      requires_confirmation: true, targets: [], tasks: [],
    },
  };
  const html = ui.renderDataPlan(plan);
  assert.match(html, /没有需要重复下载的行情分区/);
  assert.match(html, /等待明确确认核验缺失日期/);
  assert.match(html, /计划保存的历史预检快照/);
  assert.doesNotMatch(html, /预计18个行情分区/);
  assert.doesNotMatch(html, /研究数据预检：暂不能提交/);
});

test("unverified gap shows bounded evidence attempts without executing HTML", () => {
  const html = ui.renderGapVerification({gap_verification:{
    total_dates:1,verified_suspension:0,missing_bar:0,calendar_mismatch:0,unverified_gap:1,
    targets:[{symbol:"000333",adjustments:["none","qfq"],required_start:"2018-09-10",required_end:"2018-09-10",unverified_count:1,blocking_count:1,classification_counts:{verified_suspension:0,missing_bar:0,calendar_mismatch:0,unverified_gap:1},details:[{date:"2018-09-10",classification:"unverified_gap",adjustments:["none","qfq"],reason:"证据不足",evidence_attempts:[{source:"cninfo",status:"failed",error:"<script>bad</script>"},{source:"eastmoney",status:"completed"}]}]}],tasks:[],
  }});
  assert.match(html, /查看已尝试的证据来源/);
  assert.match(html, /查询失败/);
  assert.match(html, /&lt;script&gt;bad/);
  assert.doesNotMatch(html, /<script>bad/);
});

test("gap evidence lookup starts only after an explicit confirmation click", async () => {
  const params = {
    symbols: ["000333"],
    start_date: "2024-01-01",
    end_date: "2024-12-31",
    candidate_factors: [{ code: "return_1", weight: 1, direction: "desc" }],
    candidate_count: 1,
    candidate_refresh: "daily",
    buy_rules: [{ kind: "close_vs_ma", operator: "above", window: 2 }],
    sell_rules: [{ kind: "close_vs_ma", operator: "below", window: 2 }],
    buy_rule_mode: "all",
    sell_rule_mode: "any",
    exit_on_candidate_drop: false,
    max_positions: 1,
  };
  const configuration = {
    kind: "daily_signal",
    label: "日线信号策略",
    source_run_id: null,
    params: { symbols: [] },
    scope: [],
    schema: {
      properties: Object.fromEntries(Object.keys(params).map((key) => [key, {}])),
    },
    warnings: [],
    missing_fields: [],
    endpoint: "/api/v1/daily-signal-strategies",
  };
  const gap = {
    status: "available",
    total_dates: 1,
    verified_suspension: 0,
    missing_bar: 0,
    calendar_mismatch: 0,
    unverified_gap: 1,
    requires_confirmation: true,
    targets: [{
      symbol: "000333", security_id: "SZSE.000333", adjustments: ["none", "qfq"],
      required_start: "2024-01-01", required_end: "2024-12-31",
      unverified_count: 1, details: [], tasks: [],
    }],
    tasks: [],
  };
  const plan = {
    plan_id: "data-plan-confirm-gap",
    version: 1,
    input_sha256: "a".repeat(64),
    status: "succeeded",
    input: {
      research_type: "daily_signal", parameters: params,
      resolved_symbols: ["000333"], symbols_sha256: "b".repeat(64),
    },
    items: [{ symbol: "000333", dataset: "daily_bar", status: "published" }],
    task: { status: "succeeded", progress: 100 },
    gap_verification: gap,
  };
  const h = harness(
    "/backtests/daily-signals",
    async (url, options) => {
      if (url.includes("/configuration?")) return structuredClone(configuration);
      if (url.includes("/stock-pools?")) return [];
      if (url.endsWith("/plans/data-plan-confirm-gap/preflight"))
        return { ready: false, status: "blocked", complete: false };
      if (url.endsWith("/plans/data-plan-confirm-gap/verify-gaps")) {
        assert.equal(options.method, "POST");
        assert.equal(JSON.parse(options.body).confirm, true);
        return {
          ...structuredClone(plan),
          gap_verification: {
            ...structuredClone(gap),
            status: "running",
            requires_confirmation: false,
            tasks: [{ task_id: "task-gap", status: "queued", progress: 0 }],
          },
        };
      }
      if (url.endsWith("/plans/data-plan-confirm-gap")) return structuredClone(plan);
      throw Error("unexpected request " + url);
    },
    { search: "?data_plan=data-plan-confirm-gap" },
  );
  await tick();
  await tick();
  assert.equal(
    h.calls.filter((call) => call.url.endsWith("/verify-gaps")).length,
    0,
  );
  assert.equal(h.element("center-plan-verify-gaps").hidden, false);
  await h.element("center-plan-verify-gaps").listeners.click();
  await tick();
  assert.equal(
    h.calls.filter((call) => call.url.endsWith("/verify-gaps")).length,
    1,
  );
  assert.match(h.element("center-plan-status").innerHTML, /task-gap/);
});

test("successful daily signal submission redirects to exact highlighted history task", async () => {
  const daily = {
    kind: "daily_signal",
    label: "日线信号策略",
    source_run_id: "source-run",
    params: {
      symbols: ["600519"], start_date: "2024-01-01", end_date: "2024-12-31",
      candidate_factors: [{ code: "roe", direction: "desc", weight: 1 }],
      candidate_count: 1, candidate_refresh: "monthly",
      buy_rules: ui.dailyTemplates.ma_cross.buy_rules,
      sell_rules: ui.dailyTemplates.ma_cross.sell_rules,
      buy_rule_mode: "all", sell_rule_mode: "any", exit_on_candidate_drop: false,
      max_positions: 1,
    },
    schema: { properties: {} }, warnings: [], missing_fields: [], scope: ["600519"],
    endpoint: "/api/v1/daily-signal-strategies",
  };
  daily.schema.properties = Object.fromEntries(Object.keys(daily.params).map((key) => [key, {}]));
  const h = harness(
    "/backtests/configure",
    async (url) => url.includes("/copy?") ? structuredClone(daily) : url.includes("preflight") ? { ready: true, status: "ready" } : { task_id: "task-123" },
    { allowRedirect: true },
  );
  await tick();
  await h.ui.preflight();
  h.element("center-review").querySelector = () => null;
  await h.ui.submit({ preventDefault() {} });
  await h.ui.submit({ preventDefault() {} }, true);
  assert.deepEqual(h.assigned, ["/backtests/history?kind=daily_signal&submitted=task-123"]);
  assert.equal(h.calls.filter((call) => call.options.method === "POST" && call.url === daily.endpoint).length, 1);
});

test("submitted history uses one exact task read and highlights persisted status", async () => {
  const h = harness(
    "/backtests/history",
    async (url) => url.startsWith("/api/v1/run-center?")
      ? { items: [], total: 0, notice: "bounded" }
      : { task_id: "task-123", status: "succeeded", progress: 100, run_id: "daily-signal-run" },
    { search: "?kind=daily_signal&submitted=task-123" },
  );
  await tick();
  await tick();
  assert.equal(h.element("center-kind").value, "daily_signal");
  assert.match(h.element("center-submitted-task").innerHTML, /task-123/);
  assert.match(h.element("center-submitted-task").innerHTML, /回测已完成/);
  assert.equal(h.calls.filter((call) => call.url === "/api/v1/daily-signal-strategies/task-123").length, 1);
  assert.ok(h.calls.every((call) => !call.url.includes("daily-signal-strategies?")));
});

test("history task card refreshes the exact task without submitting another run", async () => {
  let status = "queued";
  const h = harness(
    "/backtests/history",
    async (url) => url.startsWith("/api/v1/run-center?")
      ? { items: [], total: 0, notice: "bounded" }
      : { task_id: "task-123", status, progress: status === "queued" ? 0 : 100 },
    { search: "?kind=daily_signal&submitted=task-123" },
  );
  await tick();
  await tick();
  assert.match(h.element("center-submitted-task").innerHTML, /role="progressbar"/);
  assert.match(h.element("center-submitted-task").innerHTML, /取消等待中的任务/);
  status = "succeeded";
  await h.element("center-submitted-task").listeners.click({
    target: { closest: (selector) => selector === "[data-refresh-daily-task]" ? {} : null },
  });
  assert.match(h.element("center-submitted-task").innerHTML, /回测已完成/);
  assert.match(h.element("center-submitted-task").innerHTML, /查看详情/);
  assert.equal(h.calls.filter((call) => call.url === "/api/v1/daily-signal-strategies/task-123").length, 2);
  assert.ok(h.calls.every((call) => !call.options.method));
});

test("history read errors stay beside the filters", async () => {
  const h = harness("/backtests/history", async () => ({ error: "数据库暂时不可用" }), { search: "" });
  await tick();
  await tick();
  assert.match(h.element("center-notice").textContent, /数据库暂时不可用/);
  assert.equal(h.element("center-notice").className, "history-notice-error");
  assert.equal(h.element("center-error").textContent, "");
});

test("blocked check exposes a local problem shortcut without submitting", async () => {
  const h = harness("/backtests/configure", async (url) => url.includes("/copy?")
    ? structuredClone(copied)
    : { status: "blocked", ready: false, blockers: ["缺少本地行情"] });
  await tick();
  await h.ui.preflight();
  assert.equal(h.element("center-jump-issue").hidden, false);
  assert.equal(h.element("center-jump-issue").textContent, "查看预检问题");
  assert.match(h.element("center-daily-actions").className, /state-blocked/);
  h.element("center-jump-issue").listeners.click();
  assert.equal(h.calls.length, 2);
});
