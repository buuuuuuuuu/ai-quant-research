/* Bounded reads, explicit parameter copying, and the existing execution APIs. */
const RunCenterUI = (() => {
  const esc = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const known = (v) =>
    v === null || v === undefined || v === "" ? "未记录" : String(v);
  const labels = {
    queued: "等待执行",
    running: "运行中",
    succeeded: "成功",
    failed: "失败",
    partial: "部分成功",
    unknown: "未知",
  };
  async function get(url, options = {}) {
    const r = await fetch(url, options);
    if (r.status === 401) {
      location.assign(
        "/login?next=" +
          encodeURIComponent(location.pathname + location.search),
      );
      throw Error("登录已过期");
    }
    const p = await r.json();
    if (!r.ok)
      throw Error(
        typeof p.detail === "string" ? p.detail : JSON.stringify(p.detail || p),
      );
    return p;
  }
  function scope(row) {
    const pool = row.pool;
    const identity = pool?.pool_id
      ? `${pool.pool_name || pool.pool_id || "未记录股票池"} · r${pool.pool_revision || pool.revision || "未知"}`
      : "";
    return `${esc(identity)}${identity ? "<br>" : ""}${row.symbols.length ? `${row.symbols.length} 只 · ${esc(row.symbols.slice(0, 6).join(" "))}${row.symbols.length > 6 ? " …" : ""}` : "历史范围未知"}`;
  }
  function table(rows) {
    if (!rows.length)
      return '<p class="empty-state">没有匹配的持久运行记录。</p>';
    return `<table class="center-table"><thead><tr><th>类型 / 身份</th><th>冻结范围</th><th>研究区间</th><th>状态 / 时间</th><th>操作</th></tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(r.label)}<small><code>${esc(r.run_id || r.record_id)}</code></small></td><td>${scope(r)}</td><td>${esc(known(r.start_date))}<small>至 ${esc(known(r.end_date))}</small></td><td>${esc(labels[r.status] || r.status)}<small>${r.started_at ? "开始" : "创建"}：${esc(known(r.started_at || r.created_at))}</small><small>完成：${esc(known(r.completed_at))}</small><small>耗时：${r.duration_seconds == null ? "未记录" : esc(r.duration_seconds) + " 秒"}</small></td><td><a href="${esc(r.detail_url)}">查看详情</a><br><a href="${esc(r.copy_url)}">复制参数</a></td></tr>`).join("")}</tbody></table>`;
  }
  const titles = {
    symbols: "冻结股票代码",
    symbol: "股票代码",
    stock_pool_id: "原股票池 ID",
    stock_pool_revision: "原股票池 revision",
    start_date: "开始日期",
    end_date: "结束日期",
    as_of: "截至时间",
    factor: "财务因子",
    factor_direction: "排序方向",
    top_n: "持仓数量",
    rebalance: "调仓周期",
    benchmark_symbol: "基准",
    adjustment: "复权口径",
    initial_capital: "初始资金",
    commission_bps: "佣金（bp）",
    slippage_bps: "滑点（bp）",
    stamp_duty_bps: "卖出印花税（bp）",
    lot_size: "买入整手",
    source: "数据来源",
    windows: "事件窗口（交易日）",
    round_trip_cost_bps: "往返成本（bp）",
    min_events: "最少事件数",
    name: "筛选名称",
    conditions: "筛选条件",
    factors: "因子配置",
    top_n_values: "持仓数量扫描",
    commission_bps_values: "佣金扫描",
    rebalance_values: "调仓周期扫描",
    quantiles: "因子分组数量",
    training_fraction: "训练集比例",
    validation_fraction: "验证集比例",
    missing_policy: "缺失值处理",
    universe_mode: "范围模式",
    universe_benchmark: "范围基准",
    classification_system: "行业分类体系",
    industry_neutralization: "行业中性化",
    max_industry_weight: "单行业持仓上限",
    require_verified_industry: "要求可信历史行业",
    walk_forward_folds: "滚动检验折数",
    max_combinations: "组合上限",
    include_industries: "包含行业",
    exclude_industries: "排除行业",
    sort_by: "排序字段",
    sort_direction: "排序方向",
  };
  function renderValue(value) {
    if (value == null) return "未知";
    if (typeof value !== "object") return esc(value);
    if (Array.isArray(value) && value.length && typeof value[0] === "object") {
      const columns = [
        ...new Set(value.slice(0, 100).flatMap((x) => Object.keys(x || {}))),
      ].slice(0, 20);
      return `<div class="table-shell"><table><thead><tr>${columns.map((k) => `<th>${esc(k)}</th>`).join("")}</tr></thead><tbody>${value
        .slice(0, 100)
        .map(
          (r) =>
            `<tr>${columns.map((k) => `<td>${esc(r[k] == null ? "未知" : typeof r[k] === "object" ? JSON.stringify(r[k]) : r[k])}</td>`).join("")}</tr>`,
        )
        .join(
          "",
        )}</tbody></table></div>${value.length > 100 ? "<p>仅展示前 100 条，请打开原结果或导出查看其余记录。</p>" : ""}`;
    }
    return `<pre>${esc(JSON.stringify(value, null, 2))}</pre>`;
  }
  function field(key, value, schema) {
    const alternatives = schema.anyOf || [schema];
    const s = alternatives.find((x) => x.type !== "null") || schema;
    const type = s.type || "string";
    const id = "copy-" + key;
    let input;
    if (type === "boolean")
      input = `<select id="${id}" data-param="${key}" data-type="boolean"><option value="true" ${value === true ? "selected" : ""}>是</option><option value="false" ${value === false ? "selected" : ""}>否</option></select>`;
    else if (s.enum)
      input = `<select id="${id}" data-param="${key}" data-type="${type}">${s.enum.map((v) => `<option value="${esc(v)}" ${v === value ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
    else if (type === "array" || type === "object")
      input = `<textarea id="${id}" data-param="${key}" data-type="json" spellcheck="false">${esc(JSON.stringify(value, null, 2))}</textarea>`;
    else
      input = `<input id="${id}" data-param="${key}" data-type="${type}" type="${["number", "integer"].includes(type) ? "number" : s.format === "date" ? "date" : "text"}" ${["number", "integer"].includes(type) ? 'step="any"' : ""} value="${esc(value ?? "")}">`;
    return `<label for="${id}">${esc(titles[key] || key)}${input}</label>`;
  }
  function readParams(form, original) {
    const params = structuredClone(original);
    for (const input of form.querySelectorAll("[data-param]")) {
      const k = input.dataset.param,
        t = input.dataset.type,
        v = input.value;
      params[k] =
        t === "json"
          ? JSON.parse(v)
          : t === "boolean"
            ? v === "true"
            : ["number", "integer"].includes(t)
              ? v === ""
                ? null
                : Number(v)
              : v;
    }
    return params;
  }
  if (typeof module !== "undefined")
    module.exports = { table, scope, renderValue, field, readParams };
  if (typeof document === "undefined") return {};
  const $ = (id) => document.getElementById(id);
  let sequence = 0,
    page = 1,
    total = 0,
    busy = false,
    submitted = false,
    copy = null,
    checked = null;
  const fail = (e) => {
    if ($("center-error")) $("center-error").textContent = e.message;
  };
  async function history() {
    const own = ++sequence;
    $("center-error").textContent = "";
    $("center-list").textContent = "正在读取…";
    $("center-prev").disabled = true;
    $("center-next").disabled = true;
    const q = new URLSearchParams({ page: String(page), page_size: "20" });
    for (const k of ["kind", "status"])
      if ($("center-" + k).value) q.set(k, $("center-" + k).value);
    historyState(q);
    try {
      const p = await get("/api/v1/run-center?" + q);
      if (own !== sequence) return;
      total = p.total;
      $("center-list").innerHTML = table(p.items);
      $("center-notice").textContent = p.notice;
      $("center-page").textContent = `第 ${page} 页 · ${total} 条`;
      $("center-prev").disabled = page <= 1;
      $("center-next").disabled = page * 20 >= total;
    } catch (e) {
      if (own === sequence) {
        $("center-list").textContent = "读取失败，请重新筛选或刷新。";
        fail(e);
      }
    }
  }
  function historyState(q) {
    window.history.replaceState(null, "", location.pathname + "?" + q);
  }
  async function detail(query) {
    const own = ++sequence;
    const row = await get("/api/v1/run-center/record?" + query);
    if (own !== sequence) return;
    $("center-title").textContent = row.label + " · 运行详情";
    $("center-identity").innerHTML =
      `<dl class="center-meta"><div><dt>运行身份</dt><dd>${esc(row.run_id || row.record_id)}</dd></div><div><dt>冻结范围</dt><dd>${scope(row)}</dd></div><div><dt>研究区间</dt><dd>${esc(known(row.start_date))} → ${esc(known(row.end_date))}</dd></div><div><dt>状态</dt><dd>${esc(labels[row.status] || row.status)}</dd></div><div><dt>${row.started_at ? "运行开始" : "记录创建"}</dt><dd>${esc(known(row.started_at || row.created_at))}</dd></div><div><dt>运行完成</dt><dd>${esc(known(row.completed_at))}</dd></div></dl>`;
    $("center-meaning").textContent = row.kind.includes("event")
      ? "事件样本与窗口收益，不代表资金组合回测。"
      : row.kind === "screen"
        ? "条件选股快照，不代表资金组合回测。"
        : "以下为原运行结果；组合指标不是单股表现。";
    $("center-detail-actions").innerHTML =
      `<a href="${esc(row.copy_url)}">复制参数</a>${row.report_url ? `<a href="${esc(row.report_url)}">原完整报告与图表</a>` : ""}${row.kind === "screen" && row.run_id ? `<a href="/api/v1/screens/runs/${encodeURIComponent(row.run_id)}/results.csv">下载筛选明细 CSV</a>` : ""}`;
    $("center-result").innerHTML =
      (row.error ? `<p class="negative">${esc(row.error)}</p>` : "") +
      (row.payload_unavailable
        ? "<p>记录超过有界读取上限，请通过原结果访问。</p>"
        : "") +
      Object.entries(row.result)
        .map(
          ([k, v]) => `<article><h4>${esc(k)}</h4>${renderValue(v)}</article>`,
        )
        .join("");
    if (!Object.keys(row.result).length && !row.error)
      $("center-result").textContent =
        "暂无已保存结果；运行中的任务可手动刷新本页。";
    $("center-raw").textContent = JSON.stringify(
      { spec: row.spec, result: row.result },
      null,
      2,
    );
    await originalDetails(row, own);
  }

  async function originalDetails(row, own) {
    if (
      !row.run_id ||
      !["factor_backtest", "factor_lab", "screen"].includes(row.kind) ||
      row.status !== "succeeded"
    )
      return;
    const endpoint =
      row.kind === "factor_backtest"
        ? "backtest-runs"
        : row.kind === "factor_lab"
          ? "factor-lab-runs"
          : "screens/runs";
    try {
      const data = await get(
        "/api/v1/" + endpoint + "/" + encodeURIComponent(row.run_id),
      );
      if (own !== sequence) return;
      const names = {
        metrics: "回测指标",
        ic_summary: "因子有效性",
        strategies: "参数实验",
        validation: "分段表现",
        trades: "交易明细",
        positions: "持仓明细",
        selections: "因子选择",
        items: "选股结果",
        summary: "结果摘要",
        diagnostics: "诊断与限制",
        segments: "分段收益",
        yearly: "年度表现",
        group_returns: "分组收益",
        walk_forward: "滚动检验",
      };
      const fragment = Object.entries(names)
        .filter(([k]) => data[k] != null)
        .map(
          ([k, label]) =>
            "<details><summary>" +
            label +
            "</summary>" +
            renderValue(data[k]) +
            "</details>",
        )
        .join("");
      $("center-result").insertAdjacentHTML("beforeend", fragment);
      if (row.kind === "factor_backtest" && data.series?.length) {
        const series = data.series.filter(
          (r) =>
            Number.isFinite(r.portfolio_nav) &&
            Number.isFinite(r.benchmark_nav),
        );
        if (series.length) {
          const values = series.flatMap((r) => [
            r.portfolio_nav,
            r.benchmark_nav,
          ]);
          const min = Math.min(...values),
            max = Math.max(...values),
            span = max - min || 1;
          const points = (k) =>
            series
              .map(
                (r, i) =>
                  15 +
                  (870 * i) / Math.max(1, series.length - 1) +
                  "," +
                  (220 - (200 * (r[k] - min)) / span),
              )
              .join(" ");
          $("center-result").insertAdjacentHTML(
            "afterbegin",
            '<article><h3>净值走势</h3><p>深绿：策略；浅灰：基准。拖动滑块查看对应日期的值。</p><svg role="img" aria-label="策略与基准净值" viewBox="0 0 900 240"><polyline fill="none" stroke="#9aafa1" stroke-width="2" points="' +
              points("benchmark_nav") +
              '"/><polyline fill="none" stroke="#24523b" stroke-width="3" points="' +
              points("portfolio_nav") +
              '"/></svg><label>查看日期 <input id="center-chart-position" type="range" min="0" max="' +
              (series.length - 1) +
              '" value="0"></label><output id="center-chart-value" aria-live="polite"></output></article>',
          );
          const show = () => {
            const r = series[Number($("center-chart-position").value)];
            $("center-chart-value").textContent =
              r.date +
              " · 策略 " +
              r.portfolio_nav +
              " · 基准 " +
              r.benchmark_nav;
          };
          $("center-chart-position").addEventListener("input", show);
          show();
        }
      }
    } catch (e) {
      if (own === sequence)
        $("center-result").insertAdjacentHTML(
          "beforeend",
          '<p class="page-notice">原结果文件暂不可用：' +
            esc(e.message) +
            "。上方保留数据库记录。</p>",
        );
    }
  }
  const readinessKinds = new Set(["factor_backtest", "factor_lab", "screen"]);
  function invalidate() {
    checked = null;
    if ($("center-submit"))
      $("center-submit").disabled =
        busy || !copy || readinessKinds.has(copy.kind);
    if ($("center-preflight-result"))
      $("center-preflight-result").textContent = "参数有变化，请重新预检。";
  }
  async function configure(query) {
    const own = ++sequence;
    const data = await get("/api/v1/run-center/copy?" + query);
    if (own !== sequence) return;
    copy = data;
    $("center-configure-title").textContent = data.label + " · 复制配置";
    $("center-copy-source").textContent =
      "参数来源：" + data.source_run_id + "；只预填，尚未执行。";
    $("center-copy-warning").textContent =
      data.warnings.join("；") ||
      "保留原运行参数及范围；执行前请核对。新运行使用当前本地数据，不保证重现旧结果。";
    const common = new Set([
      "symbol",
      "symbols",
      "stock_pool_id",
      "stock_pool_revision",
      "start_date",
      "end_date",
      "as_of",
      "factor",
      "top_n",
      "factors",
      "conditions",
      "windows",
      "source",
      "name",
    ]);
    for (const [key, value] of Object.entries(data.params)) {
      if (value === null) continue;
      const html = field(key, value, data.schema.properties[key] || {});
      $(
        common.has(key) ? "center-common-fields" : "center-advanced-fields",
      ).insertAdjacentHTML("beforeend", html);
    }
    $("center-missing-confirm").parentElement.hidden =
      !data.missing_fields.length;
    $("center-preflight").hidden = !readinessKinds.has(data.kind);
    invalidate();
  }
  async function preflight() {
    if (busy || !copy) return;
    busy = true;
    $("center-preflight").disabled = true;
    $("center-submit").disabled = true;
    try {
      const params = readParams($("center-copy-form"), copy.params);
      const p = await get("/api/v1/research-data/preflight", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ research_type: copy.kind, parameters: params }),
      });
      if (
        JSON.stringify(params) !==
        JSON.stringify(readParams($("center-copy-form"), copy.params))
      )
        return;
      $("center-preflight-result").innerHTML = renderValue(p);
      checked = { params: JSON.stringify(params), report: p };
      $("center-submit").disabled = !(p.ready === true || p.status === "ready");
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
      $("center-preflight").disabled = false;
    }
  }
  async function submit(event) {
    event.preventDefault();
    if (busy || submitted || !copy) return;
    try {
      const params = readParams($("center-copy-form"), copy.params);
      if (copy.missing_fields.length && !$("center-missing-confirm").checked)
        throw Error("请明确核对原记录缺失参数的补充值");
      if (
        readinessKinds.has(copy.kind) &&
        (!checked ||
          checked.params !== JSON.stringify(params) ||
          !(checked.report.ready === true || checked.report.status === "ready"))
      )
        throw Error("请先通过当前参数的数据预检");
      if (!window.confirm("确认使用这些参数提交新的" + copy.label + "？"))
        return;
      busy = true;
      $("center-submit").disabled = true;
      const p = await get(copy.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(params),
      });
      submitted = true;
      $("center-submit-result").textContent =
        "已提交：" +
        (p.task_id || p.run_id || p.batch_id || "完成") +
        "。可从历史入口查看；不要重复提交。";
      $("center-copy-form")
        .querySelectorAll("input,select,textarea,button")
        .forEach((x) => (x.disabled = true));
    } catch (e) {
      fail(e);
      $("center-submit").disabled = false;
    } finally {
      busy = false;
    }
  }
  async function init() {
    if ($("overview-pool")) {
      get("/api/v1/research-dashboard?page_size=1")
        .then((p) => {
          const fields = {
            announcements: "窗口公告",
            financial_metrics: "财务资料",
            accepted_events: "已接受事件",
            data_gaps: "数据缺口",
          };
          $("overview-pool").innerHTML =
            `<p>${esc(p.pool.name)} · revision ${esc(p.context.pool_revision)} · ${esc(p.symbols.length)} 只</p><p>${esc(p.context.window_start)} → ${esc(p.context.as_of)} · ${esc(p.context.timezone)}；当前本地状态，非历史重建。</p><div class="entry-grid">${Object.entries(
              fields,
            )
              .map(
                ([key, label]) =>
                  `<div class="entry-card"><span>${label}</span><h3>${esc(known(p.sections[key]?.total))}${p.sections[key]?.truncated ? "（截断）" : ""}</h3></div>`,
              )
              .join(
                "",
              )}</div><p>复用看板当前窗口；本地无记录不代表没有公告、事件或风险。</p>`;
        })
        .catch((e) => {
          $("overview-pool").textContent =
            "默认股票池摘要暂不可用：" +
            e.message +
            "。请在数据与股票池核对范围。";
        });
    }
    if ($("open-stock-form"))
      $("open-stock-form").addEventListener("submit", (e) => {
        e.preventDefault();
        const code = $("open-stock-code").value;
        if (/^[0-9]{6}$/.test(code)) location.assign("/stocks/" + code);
      });
    if ($("overview-runs")) {
      for (const [id, q] of [
        ["overview-runs", "page_size=5"],
        ["overview-tasks", "status=running&page_size=5"],
      ])
        get("/api/v1/run-center?" + q)
          .then((p) => {
            $(id).innerHTML = table(p.items);
          })
          .catch((e) => {
            $(id).textContent = e.message;
          });
    }
    if (!$("center-history")) return;
    const query = new URLSearchParams(location.search);
    if (location.pathname.endsWith("/detail")) {
      $("center-history").hidden = true;
      $("center-detail").hidden = false;
      await detail(query);
    } else if (location.pathname.endsWith("/configure")) {
      $("center-history").hidden = true;
      $("center-configure").hidden = false;
      $("center-copy-form").addEventListener("input", invalidate);
      $("center-copy-form").addEventListener("submit", submit);
      $("center-preflight").addEventListener("click", preflight);
      await configure(query);
    } else {
      for (const k of ["kind", "status"])
        $("center-" + k).value = query.get(k) || "";
      page = Math.max(1, Math.min(10000, Number(query.get("page")) || 1));
      $("center-filters").addEventListener("submit", (e) => {
        e.preventDefault();
        page = 1;
        history();
      });
      $("center-prev").addEventListener("click", () => {
        if (page > 1) {
          page--;
          history();
        }
      });
      $("center-next").addEventListener("click", () => {
        if (page * 20 < total) {
          page++;
          history();
        }
      });
      await history();
    }
  }
  init().catch(fail);
  return { history, detail, preflight, submit };
})();
