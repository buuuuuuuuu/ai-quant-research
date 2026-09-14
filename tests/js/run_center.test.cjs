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
    search: "?source=task&record=original",
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
  const h = harness("/backtests", (url) =>
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
  const h = harness("/backtests", (url) =>
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
  assert.match(html, /未知/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img/);
});
