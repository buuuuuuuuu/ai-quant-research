/* Bounded reads, explicit parameter copying, and the existing execution APIs. */
const RunCenterUI = (() => {
  let catalog = { factors: {}, enums: {}, fields: {} };
  if (typeof document !== "undefined") {
    const raw = document.getElementById("center-catalog")?.textContent;
    if (raw) catalog = JSON.parse(raw);
  }
  function factorName(code) {
    if (code === "composite") return "多因子组合（composite）";
    if (String(code).startsWith("composite:"))
      return (
        "多因子组合：" + code.slice(10).split("+").map(factorName).join(" + ")
      );
    if (/^return_\d+$/.test(String(code)))
      return `${String(code).slice(7)} 日涨跌幅（${code}）`;
    if (/^ma_deviation_\d+$/.test(String(code)))
      return `收盘价相对 MA(${String(code).slice(13)}) 偏离率（${code}）`;
    return catalog.factors[code]
      ? `${catalog.factors[code]}（${code}）`
      : `未知因子（${code}）`;
  }
  function enumName(value, key) {
    if (value === "none" && key === "adjustment") return "不复权（none）";
    return catalog.enums[value]
      ? `${catalog.enums[value]}（${value}）`
      : String(value);
  }
  function strategyName(value) {
    const p = String(value).split("|");
    return p.length === 4 &&
      p[1].startsWith("top=") &&
      p[3].startsWith("commission=")
      ? `${factorName(p[0])} · 持仓 ${p[1].slice(4)} 只 · ${enumName(p[2])} · 佣金 ${p[3].slice(11)} bp`
      : `未识别策略（${value}）`;
  }
  function display(value, key) {
    if (value == null || value === "") return "未记录";
    if (catalog.percent_fields?.includes(key) && typeof value === "number")
      return Number.isFinite(value) ? (value * 100).toFixed(2) + "%" : "未记录";
    if (["factor", "code"].includes(key)) return factorName(value);
    if (["strategy", "selected_strategy"].includes(key))
      return strategyName(value);
    if (typeof value === "boolean") return value ? "是" : "否";
    if (typeof value === "object") return JSON.stringify(value);
    return enumName(value, key);
  }
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
    candidate_factors: "候选排序因子",
    candidate_count: "候选股票数量",
    candidate_refresh: "候选更新频率",
    buy_rules: "买入信号",
    sell_rules: "卖出信号",
    buy_rule_mode: "买入规则组合",
    sell_rule_mode: "卖出规则组合",
    exit_on_candidate_drop: "退出候选即卖出",
    max_positions: "最大持仓数量",
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
      return `<div class="table-shell"><table><thead><tr>${columns.map((k) => `<th>${esc(catalog.fields[k] || titles[k] || k)}</th>`).join("")}</tr></thead><tbody>${value
        .slice(0, 100)
        .map(
          (r) =>
            `<tr>${columns.map((k) => `<td>${esc(display(r[k], k))}</td>`).join("")}</tr>`,
        )
        .join(
          "",
        )}</tbody></table></div>${value.length > 100 ? "<p>仅展示前 100 条，请打开原结果或导出查看其余记录。</p>" : ""}`;
    }
    if (value && !Array.isArray(value))
      return `<dl class="center-meta">${Object.entries(value)
        .map(
          ([k, v]) =>
            `<div><dt>${esc(catalog.fields[k] || titles[k] || k)}</dt><dd>${typeof v === "object" && v !== null ? `<details><summary>查看内容</summary>${renderValue(v)}</details>` : esc(display(v, k))}</dd></div>`,
        )
        .join("")}</dl>`;
    return `<pre>${esc(JSON.stringify(value, null, 2))}</pre>`;
  }
  function renderPreflight(report = {}) {
    const checks = Array.isArray(report.checks) ? report.checks : [];
    const blockers = Array.isArray(report.blockers) ? report.blockers : [];
    const warnings = Array.isArray(report.warnings) ? report.warnings : [];
    const recommendations = Array.isArray(report.recommendations)
      ? report.recommendations
      : [];
    const ready = report.ready === true || report.status === "ready";
    const tone = ready ? (warnings.length ? "warning" : "ready") : "blocked";
    const heading = ready
      ? warnings.length
        ? "研究数据预检：可以提交，但请先阅读提示"
        : "研究数据预检：可以提交"
      : "研究数据预检：暂不能提交";
    const explanation = ready
      ? "预检只核对数据与参数，不会自动运行研究、下载数据或修改配置。"
      : "当前不会执行回测。请按下面建议修改参数或补齐数据，然后重新点击“研究数据预检”。";
    const affected = Array.isArray(report.affected_symbols)
      ? report.affected_symbols
      : [];
    const count = Number.isInteger(report.blocking_check_count)
      ? report.blocking_check_count
      : blockers.length;
    const summary = ready
      ? `${checks.length} 项检查 · ${warnings.length} 项提示`
      : `${count} 项阻断${affected.length ? ` · ${affected.length} 只股票受影响` : ""}`;
    const advice = recommendations.length
      ? `<section class="preflight-recommendations" aria-label="处理建议">${recommendations
          .map(
            (item, index) =>
              `<article><h4>${index + 1}. ${esc(item.title || "需要处理")}</h4><p>${esc(item.explanation || "未提供原因")}</p><p><strong>建议：</strong>${esc(item.action || "查看阻断明细后调整")}</p></article>`,
          )
          .join("")}</section>`
      : !ready && blockers.length
        ? '<p class="preflight-no-advice">请根据下方阻断明细调整参数或补齐数据。</p>'
        : "";
    const datasetLabels = {
      benchmark_daily_bar: "基准日线",
      daily_bar: "股票日线",
      financial_metric: "财务因子",
      security_master: "证券目录",
      universe_history: "历史范围/行业",
    };
    const statusLabels = {
      ready: "已就绪",
      missing: "缺失",
      unknown: "无法确认",
      not_applicable: "不适用",
    };
    const adjustmentLabels = {
      none: "不复权（none）",
      qfq: "前复权（qfq）",
      hfq: "后复权（hfq）",
    };
    const range = (item, prefix) => {
      const start = item[prefix + "_start"];
      const end = item[prefix + "_end"];
      if (!start && !end) return "未记录";
      return `${known(start)} 至 ${known(end)}`;
    };
    const checkTable = checks.length
      ? `<section><h3>默认检查结果</h3><div class="table-shell"><table class="preflight-checks"><thead><tr><th>对象</th><th>检查项</th><th>口径 / 因子</th><th>需要覆盖</th><th>本地覆盖</th><th>状态与原因</th></tr></thead><tbody>${checks
          .slice(0, 100)
          .map((item) => {
            const detail = item.metric
              ? factorName(item.metric)
              : adjustmentLabels[item.adjustment] || known(item.adjustment);
            const localRange = range(item, "actual");
            const listing = item.list_date
              ? `<small>上市：${esc(item.list_date)}</small>`
              : "";
            const missing =
              item.missing_count == null
                ? ""
                : `<small>缺口：${esc(item.missing_count)}</small>`;
            return `<tr><td>${esc(known(item.symbol || item.security_id))}${listing}</td><td>${esc(datasetLabels[item.dataset] || item.dataset || "未知检查")}</td><td>${esc(detail)}</td><td>${esc(range(item, "required"))}</td><td>${esc(localRange)}${missing}</td><td><strong>${esc(statusLabels[item.status] || item.status || "未知")}</strong><small>${esc(item.reason || "未提供原因")}</small></td></tr>`;
          })
          .join("")}</tbody></table></div>${checks.length > 100 ? "<p>仅展示前 100 项检查。</p>" : ""}</section>`
      : "";
    const blockerDetails = blockers.length
      ? `<details ${recommendations.length ? "" : "open"}><summary>查看阻断明细（${blockers.length}）</summary><ul>${blockers.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></details>`
      : "";
    const warningDetails = warnings.length
      ? `<details><summary>查看风险提示（${warnings.length}）</summary><ul>${warnings.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></details>`
      : "";
    const diagnostic = {
      checked_at: report.checked_at,
      input: report.input,
      required_datasets: report.required_datasets,
      signal_count: report.signal_count,
      signal_cutoff: report.signal_cutoff,
      financial_warmup_start: report.financial_warmup_start,
      financial_validity_rule: report.financial_validity_rule,
      missing_policy: report.missing_policy,
      checks,
    };
    return `<section class="preflight-summary ${tone}" aria-live="polite"><header><div><p class="preflight-kicker">${esc(report.status_label || (ready ? "可以运行" : "预检阻断"))}</p><h2>${heading}</h2></div><strong class="preflight-count">${esc(summary)}</strong></header><p>${explanation}</p>${advice}${checkTable}${blockerDetails}${warningDetails}<details><summary>查看冻结参数与完整诊断</summary>${renderValue(diagnostic)}</details></section>`;
  }
  function factorRow(item = { code: "roe", weight: 1, direction: "desc" }) {
    const options = { ...catalog.factors };
    if (!options[item.code]) options[item.code] = "未知因子";
    return `<div class="factor-editor-row"><label>因子<select data-factor-code>${Object.keys(
      options,
    )
      .map(
        (k) =>
          `<option value="${esc(k)}" ${k === item.code ? "selected" : ""}>${esc(factorName(k))}</option>`,
      )
      .join(
        "",
      )}</select></label><label>相对权重<input data-factor-weight type="number" min="0.001" max="1000" step="any" value="${esc(item.weight)}"></label><label>方向<select data-factor-direction>${["desc", "asc"].map((k) => `<option value="${k}" ${k === item.direction ? "selected" : ""}>${esc(enumName(k))}</option>`).join("")}</select></label><button data-factor-remove type="button">移除</button></div>`;
  }
  function candidateFactorRow(item = { code: "roe", weight: 1, direction: "desc" }) {
    return `<div class="factor-editor-row candidate-factor-row"><label>因子代码<input data-candidate-code list="candidate-factor-codes" value="${esc(item.code)}"><small>${esc(factorName(item.code))}；可用财务代码、return_N 或 ma_deviation_N</small></label><label>相对权重<input data-candidate-weight type="number" min="0.001" max="1000" step="any" value="${esc(item.weight)}"></label><label>方向<select data-candidate-direction>${["desc", "asc"].map((k) => `<option value="${k}" ${k === item.direction ? "selected" : ""}>${esc(enumName(k))}</option>`).join("")}</select></label><button data-candidate-remove type="button">移除</button></div>`;
  }
  function signalRuleRow(item = { kind: "ma_cross", operator: "cross_above", window: null, short_window: 5, long_window: 20, threshold: null }, side = "buy") {
    const kinds = { close_vs_ma: "收盘价与均线", ma_compare: "双均线状态", ma_cross: "双均线交叉", return_threshold: "区间涨跌幅阈值" };
    const operators = { above: "高于", below: "低于", cross_above: "上穿", cross_below: "下穿" };
    return `<div class="signal-rule-row" data-rule-side="${side}"><label>规则<select data-rule-kind>${Object.entries(kinds).map(([k,v]) => `<option value="${k}" ${k === item.kind ? "selected" : ""}>${v}（${k}）</option>`).join("")}</select></label><label>比较<select data-rule-operator>${Object.entries(operators).map(([k,v]) => `<option value="${k}" ${k === item.operator ? "selected" : ""}>${v}（${k}）</option>`).join("")}</select></label><label>周期 N<input data-rule-window type="number" min="1" max="250" value="${esc(item.window ?? "")}"></label><label>短周期<input data-rule-short type="number" min="2" max="250" value="${esc(item.short_window ?? "")}"></label><label>长周期<input data-rule-long type="number" min="2" max="250" value="${esc(item.long_window ?? "")}"></label><label>阈值（小数）<input data-rule-threshold type="number" step="any" value="${esc(item.threshold ?? "")}"></label><button data-rule-remove type="button">移除</button></div>`;
  }
  function field(key, value, schema) {
    const alternatives = schema.anyOf || [schema];
    const s = alternatives.find((x) => x.type !== "null") || schema;
    const type = s.type || "string";
    const id = "copy-" + key;
    let input;
    if (key === "factors")
      return `<div class="factor-editor" data-factor-editor><p>因子组合：相对权重为合成得分输入，不代表资金百分比。</p><div id="center-factor-rows">${value.map(factorRow).join("")}</div><button id="center-factor-add" type="button">增加因子</button></div>`;
    if (key === "candidate_factors")
      return `<div class="factor-editor" data-candidate-editor><p>return_N 为 N 日涨跌幅；ma_deviation_N 为收盘价相对 MA(N) 偏离率。权重不是资金比例。</p><datalist id="candidate-factor-codes">${Object.keys(catalog.factors).map(k => `<option value="${esc(k)}">`).join("")}<option value="return_20"><option value="ma_deviation_20"></datalist><div id="center-candidate-factor-rows">${value.map(candidateFactorRow).join("")}</div><button id="center-candidate-factor-add" type="button">增加候选因子</button></div>`;
    if (["buy_rules", "sell_rules"].includes(key)) {
      const side = key === "buy_rules" ? "buy" : "sell";
      return `<div class="signal-rule-editor" data-signal-editor="${side}"><p>${side === "buy" ? "买入" : "卖出"}规则使用当日收盘后已完成的数据。</p><div id="center-${side}-rule-rows">${value.map(x => signalRuleRow(x, side)).join("")}</div><button data-rule-add="${side}" type="button">增加${side === "buy" ? "买入" : "卖出"}规则</button></div>`;
    }
    if (key === "symbols")
      return `<label>冻结股票代码（空格或换行分隔）<textarea data-param="symbols" data-type="symbols">${esc(value.join("\n"))}</textarea></label>`;
    if (key === "factor")
      input = `<select id="${id}" data-param="${key}" data-type="string">${Object.keys(
        { ...catalog.factors, [value]: catalog.factors[value] || "未知因子" },
      )
        .map(
          (k) =>
            `<option value="${esc(k)}" ${k === value ? "selected" : ""}>${esc(factorName(k))}</option>`,
        )
        .join("")}</select>`;
    else if (
      ["top_n_values", "commission_bps_values", "rebalance_values"].includes(
        key,
      )
    )
      input = `<input id="${id}" data-param="${key}" data-type="${key === "rebalance_values" ? "words" : "numbers"}" value="${esc(value.join(" "))}"><small>${key === "rebalance_values" ? "monthly：按月；quarterly：按季度；空格分隔" : "数字以空格分隔；沿用原组合数量限制"}</small>`;
    else if (type === "boolean")
      input = `<select id="${id}" data-param="${key}" data-type="boolean"><option value="true" ${value === true ? "selected" : ""}>是</option><option value="false" ${value === false ? "selected" : ""}>否</option></select>`;
    else if (s.enum)
      input = `<select id="${id}" data-param="${key}" data-type="${type}">${s.enum.map((v) => `<option value="${esc(v)}" ${v === value ? "selected" : ""}>${esc(enumName(v, key))}</option>`).join("")}</select>`;
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
      params[k] = ["symbols", "words", "numbers"].includes(t)
        ? v
            .trim()
            .split(/[\s,，]+/)
            .filter(Boolean)
            .map((x) => (t === "numbers" ? Number(x) : x))
        : t === "json"
          ? JSON.parse(v)
          : t === "boolean"
            ? v === "true"
            : ["number", "integer"].includes(t)
              ? v === ""
                ? null
                : Number(v)
              : v;
    }
    if (form.querySelector?.("[data-factor-editor]"))
      params.factors = [...form.querySelectorAll(".factor-editor-row")].map(
        (r) => ({
          code: r.querySelector("[data-factor-code]").value,
          weight: Number(r.querySelector("[data-factor-weight]").value),
          direction: r.querySelector("[data-factor-direction]").value,
        }),
      );
    if (form.querySelector?.("[data-candidate-editor]"))
      params.candidate_factors = [...form.querySelectorAll(".candidate-factor-row")].map(r => ({ code: r.querySelector("[data-candidate-code]").value.trim(), weight: Number(r.querySelector("[data-candidate-weight]").value), direction: r.querySelector("[data-candidate-direction]").value }));
    for (const side of ["buy", "sell"])
      if (form.querySelector?.(`[data-signal-editor="${side}"]`))
        params[side + "_rules"] = [...form.querySelectorAll(`[data-rule-side="${side}"]`)].map(r => {
          const number = key => { const value = r.querySelector(key).value; return value === "" ? null : Number(value); };
          return { kind: r.querySelector("[data-rule-kind]").value, operator: r.querySelector("[data-rule-operator]").value, window: number("[data-rule-window]"), short_window: number("[data-rule-short]"), long_window: number("[data-rule-long]"), threshold: number("[data-rule-threshold]") };
        });
    return params;
  }
  function resultSections(result) {
    const labels = {
      metrics: "组合回测指标",
      summary: "研究结果摘要",
      pooled_summary: "合并事件窗口统计",
      candidate_count: "候选数量",
      selected_count: "入选数量",
      windows: "事件窗口",
      events: "事件样本",
      samples: "样本明细",
      status: "状态",
      warning: "原运行风险提示",
      missing_statistics: "缺失统计量",
      diagnostics: "诊断与风险提示",
    };
    return Object.entries(result)
      .map(([k, v]) => {
        const heading = labels[k] || catalog.fields[k] || k;
        const content = `<h3>${esc(heading)}</h3>${renderValue(v)}`;
        return [
          "metrics",
          "summary",
          "pooled_summary",
          "candidate_count",
          "selected_count",
          "warning",
          "missing_statistics",
        ].includes(k)
          ? `<article>${content}</article>`
          : `<details><summary>${esc(heading)}</summary>${renderValue(v)}</details>`;
      })
      .join("");
  }
  if (typeof module !== "undefined")
    module.exports = {
      table,
      scope,
      renderValue,
      renderPreflight,
      field,
      readParams,
      factorRow,
      candidateFactorRow,
      signalRuleRow,
      factorName,
      strategyName,
      display,
      resultSections,
    };
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
    let row;
    try {
      row = await get("/api/v1/run-center/record?" + query);
    } catch (e) {
      if (own === sequence) fail(e);
      return;
    }
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
      resultSections(row.result);
    $("center-parameters").innerHTML =
      `<details><summary>原运行参数（中文展示，内部代码保留）</summary>${renderValue(row.spec)}</details>`;
    if (row.kind === "factor_lab")
      $("center-result").insertAdjacentHTML(
        "beforeend",
        `<p class="page-notice">${esc(catalog.ic_explanation || "IC / Rank IC 沿用原因子有效性口径；验证集选定策略，样本外不参与选择。")}</p>`,
      );
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
      !["factor_backtest", "factor_lab", "daily_signal", "screen"].includes(row.kind) ||
      row.status !== "succeeded"
    )
      return;
    const endpoint =
      row.kind === "factor_backtest"
        ? "backtest-runs"
        : row.kind === "factor_lab"
          ? "factor-lab-runs"
          : row.kind === "daily_signal"
            ? "daily-signal-runs"
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
        coverage: "因子数据覆盖",
        signals: "信号明细",
        orders: "订单与成交",
        candidates: "候选选择",
        series: "因子有效性时间序列",
      };
      const fragment = Object.entries(names)
        .filter(([k]) => data[k] != null)
        .map(
          ([k, label]) =>
            (k === "metrics" || k === "ic_summary" || k === "validation"
              ? "<details open><summary>"
              : "<details><summary>") +
            label +
            "</summary>" +
            renderValue(data[k]) +
            "</details>",
        )
        .join("");
      $("center-result").insertAdjacentHTML("beforeend", fragment);
      if (row.kind === "factor_lab" && data.series?.length) {
        const groups = new Map();
        for (const point of data.series) {
          if (!groups.has(point.factor)) groups.set(point.factor, []);
          groups.get(point.factor).push(point);
        }
        let chartIndex = 0;
        for (const [code, points] of groups) {
          const id = "center-ic-" + chartIndex++,
            n = points.length;
          const line = (key) =>
            points.map((p, i) =>
              Number.isFinite(p[key])
                ? `${15 + (870 * i) / Math.max(1, n - 1)},${120 - 100 * p[key]}`
                : null,
            );
          const paths = (key) => {
            let parts = [],
              chunk = [];
            for (const p of line(key)) {
              if (p) chunk.push(p);
              else {
                if (chunk.length) parts.push(chunk.join(" "));
                chunk = [];
              }
            }
            if (chunk.length) parts.push(chunk.join(" "));
            return parts
              .map(
                (s) =>
                  `<polyline fill="none" stroke="${key === "ic" ? "#24523b" : "#96742b"}" stroke-width="2" points="${s}"/>`,
              )
              .join("");
          };
          $("center-result").insertAdjacentHTML(
            "beforeend",
            `<article><h3>${esc(factorName(code))} · IC 时间序列</h3><p>深绿：IC；棕色：Rank IC。缺失期不连接或填零。</p><svg viewBox="0 0 900 240" role="img" aria-label="${esc(factorName(code))} IC 和 Rank IC"><line x1="15" y1="120" x2="885" y2="120" stroke="#b7c4ba"/>${paths("ic")}${paths("rank_ic")}</svg><label>查看信号日期<input id="${id}" type="range" min="0" max="${n - 1}" value="0"></label><output id="${id}-value" aria-live="polite"></output></article>`,
          );
          const show = () => {
            const p = points[Number($(id).value)];
            $(id + "-value").textContent =
              `${p.signal_date} · IC ${known(p.ic)} · Rank IC ${known(p.rank_ic)} · 样本 ${known(p.sample_size)}`;
          };
          $(id).addEventListener("input", show);
          show();
        }
      }
      if (["factor_backtest", "daily_signal"].includes(row.kind) && data.series?.length) {
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
  const readinessKinds = new Set(["factor_backtest", "factor_lab", "daily_signal", "screen"]);
  function invalidate() {
    checked = null;
    if ($("center-review")) $("center-review").hidden = true;
    if ($("center-submit"))
      $("center-submit").disabled =
        busy || !copy || readinessKinds.has(copy.kind);
    if ($("center-preflight-result"))
      $("center-preflight-result").textContent = "参数有变化，请重新预检。";
  }
  async function configure(query, freshKind = null) {
    const own = ++sequence;
    copy = null;
    checked = null;
    let data;
    try {
      data = await get(
        freshKind
          ? "/api/v1/run-center/configuration?kind=" + freshKind
          : "/api/v1/run-center/copy?" + query,
      );
    } catch (e) {
      if (own === sequence) fail(e);
      return;
    }
    if (own !== sequence) return;
    if (freshKind) {
      if (query.has("stock")) {
        if (!/^[0-9]{6}$/.test(query.get("stock")))
          throw Error("旧链接股票代码无效，请明确选择范围");
        data.params.symbols = [query.get("stock")];
        if (freshKind === "factor_backtest") data.params.top_n = 1;
        if (freshKind === "daily_signal") {
          data.params.candidate_count = 1;
          data.params.max_positions = 1;
        }
      }
      for (const key of ["start_date", "end_date", "factor", "adjustment"])
        if (query.has(key) && key in data.params)
          data.params[key] = query.get(key);
      if (query.has("benchmark"))
        data.params.benchmark_symbol = query.get("benchmark");
      if (query.has("screen_source")) {
        const run = await get(
          "/api/v1/screens/runs/" +
            encodeURIComponent(query.get("screen_source")),
        );
        if (own !== sequence) return;
        const symbols = (run.items || [])
          .filter((r) => r.selected)
          .map((r) => r.symbol);
        if (!symbols.length || symbols.length > 500)
          throw Error("来源选股没有可预填的冻结股票范围");
        data.params.symbols = symbols;
        data.scope = symbols;
        data.source_run_id = run.run_id;
        if (freshKind === "factor_backtest")
          data.params.top_n = Math.min(data.params.top_n, symbols.length);
        if (freshKind === "daily_signal") {
          data.params.candidate_count = Math.min(
            data.params.candidate_count,
            symbols.length,
          );
          data.params.max_positions = Math.min(
            data.params.max_positions,
            symbols.length,
          );
        }
      }
    }
    copy = data;
    $("center-title").textContent =
      data.label + (freshKind ? " · 新建配置" : " · 复制配置");
    $("center-configure-title").textContent =
      data.label + (freshKind ? " · 新建配置" : " · 复制配置");
    $("center-copy-source").textContent = data.source_run_id
      ? "参数来源：" + data.source_run_id + "；只预填，尚未执行。"
      : "新建配置：当前接口默认值，尚未执行。";
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
      "factor_direction",
      "rebalance",
      "top_n_values",
      "rebalance_values",
      "quantiles",
      "candidate_factors",
      "candidate_count",
      "candidate_refresh",
      "buy_rules",
      "sell_rules",
      "buy_rule_mode",
      "sell_rule_mode",
      "exit_on_candidate_drop",
      "max_positions",
    ]);
    const range = new Set([
      "symbols",
      "symbol",
      "stock_pool_id",
      "stock_pool_revision",
      "start_date",
      "end_date",
      "as_of",
      "universe_mode",
      "benchmark_symbol",
      "adjustment",
      "source",
    ]);
    const costs = new Set([
      "initial_capital",
      "commission_bps",
      "commission_bps_values",
      "slippage_bps",
      "stamp_duty_bps",
      "lot_size",
      "round_trip_cost_bps",
    ]);
    for (const id of [
      "center-scope-fields",
      "center-common-fields",
      "center-advanced-fields",
      "center-cost-fields",
    ])
      $(id).innerHTML = "";
    for (const [key, value] of Object.entries(data.params)) {
      if (value === null) continue;
      const html = field(key, value, data.schema.properties[key] || {});
      $(
        range.has(key)
          ? "center-scope-fields"
          : costs.has(key)
            ? "center-cost-fields"
            : common.has(key)
              ? "center-common-fields"
              : "center-advanced-fields",
      ).insertAdjacentHTML("beforeend", html);
    }
    if (!freshKind && data.params.stock_pool_id) {
      for (const key of ["stock_pool_id", "stock_pool_revision"])
        if ($("copy-" + key)) $("copy-" + key).readOnly = true;
    }
    if ($("center-factor-add"))
      $("center-factor-add").addEventListener("click", () => {
        if (
          $("center-factor-rows").querySelectorAll(".factor-editor-row")
            .length >= 12
        ) {
          fail(Error("最多 12 个因子"));
          return;
        }
        $("center-factor-rows").insertAdjacentHTML("beforeend", factorRow());
        invalidate();
      });
    if ($("center-candidate-factor-add"))
      $("center-candidate-factor-add").addEventListener("click", () => {
        if ($("center-candidate-factor-rows").querySelectorAll(".candidate-factor-row").length >= 12) return fail(Error("最多 12 个候选因子"));
        $("center-candidate-factor-rows").insertAdjacentHTML("beforeend", candidateFactorRow());
        invalidate();
      });
    for (const button of $("center-copy-form").querySelectorAll("[data-rule-add]"))
      button.addEventListener("click", () => {
        const side = button.dataset.ruleAdd;
        $("center-" + side + "-rule-rows").insertAdjacentHTML("beforeend", signalRuleRow(undefined, side));
        invalidate();
      });
    $("center-copy-form").addEventListener("click", (e) => {
      const button = e.target.closest?.("[data-factor-remove],[data-candidate-remove],[data-rule-remove]");
      if (!button) return;
      button.closest(".factor-editor-row,.signal-rule-row").remove();
      invalidate();
    });
    $("center-missing-confirm").parentElement.hidden =
      !data.missing_fields.length;
    $("center-preflight").hidden = !readinessKinds.has(data.kind);
    invalidate();
    if (freshKind) await loadPools(own);
  }
  async function loadPools(own) {
    try {
      const pools = await get("/api/v1/stock-pools?include_archived=false");
      if (own !== sequence) return;
      const items = Array.isArray(pools) ? pools : pools.items;
      $("center-pool-selector").innerHTML =
        `<label>范围来源<select id="center-select-pool"><option value="manual">手动股票代码（不冒充历史指数成分）</option>${items.map((p) => `<option value="${esc(p.pool_id)}:${p.current_revision}">${esc(p.name)} · r${p.current_revision}</option>`).join("")}</select></label><p id="center-pool-preview"></p>`;
      $("center-select-pool").addEventListener("change", async () => {
        const generation = ++poolSequence;
        invalidate();
        scopePending = true;
        scopeError = null;
        const value = $("center-select-pool").value;
        const manualInput = $("center-copy-form").querySelector?.('[data-param="symbols"]');
        if (manualInput) manualInput.disabled = value !== "manual";
        try {
          if (value === "manual") {
            copy.params.stock_pool_id = null;
            copy.params.stock_pool_revision = null;
            copy.scope = [];
            $("center-pool-preview").textContent = "使用明确输入的股票代码。";
            return;
          }
          const split = value.lastIndexOf(":"),
            id = value.slice(0, split),
            revision = Number(value.slice(split + 1));
          const pool = await get(
            `/api/v1/stock-pools/${encodeURIComponent(id)}?revision=${revision}`,
          );
          if (generation !== poolSequence || own !== sequence) return;
          if (!pool.symbols?.length) throw Error("所选 revision 没有冻结股票");
          copy.params.stock_pool_id = id;
          copy.params.stock_pool_revision = revision;
          copy.scope = pool.symbols;
          if (copy.kind === "daily_signal") {
            for (const key of ["candidate_count", "max_positions"]) {
              const input = $("copy-" + key);
              if (input)
                input.value = String(Math.min(Number(input.value), pool.symbols.length));
            }
          }
          $("center-pool-preview").textContent =
            `冻结 ${id} · r${revision} · ${pool.symbols.length} 只：${pool.symbols.join(" ")}`;
        } catch (e) {
          if (generation === poolSequence) {
            scopeError = e.message;
            fail(e);
          }
        } finally {
          if (generation === poolSequence) scopePending = false;
        }
      });
    } catch (e) {
      if (own === sequence)
        $("center-pool-selector").textContent =
          "股票池列表暂不可用：" + e.message + "；可明确输入股票代码。";
    }
  }
  let poolSequence = 0,
    scopePending = false,
    scopeError = null;
  function currentParams() {
    if (scopePending || scopeError)
      throw Error(scopeError || "股票池读取中，请稍候");
    const params = readParams($("center-copy-form"), copy.params);
    if (params.stock_pool_id) {
      delete params.symbols;
    } else if ("symbols" in params && !params.symbols?.length)
      throw Error("请明确输入冻结股票范围，不能使用隐式默认池");
    return params;
  }
  async function preflight() {
    if (busy || !copy) return;
    checked = null;
    busy = true;
    $("center-preflight").disabled = true;
    $("center-submit").disabled = true;
    try {
      const params = currentParams();
      const p = await get("/api/v1/research-data/preflight", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ research_type: copy.kind, parameters: params }),
      });
      if (JSON.stringify(params) !== JSON.stringify(currentParams())) return;
      $("center-preflight-result").innerHTML = renderPreflight(p);
      checked = { params: JSON.stringify(params), report: p };
      $("center-submit").disabled = !(p.ready === true || p.status === "ready");
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
      $("center-preflight").disabled = false;
    }
  }
  async function submit(event, execute = false) {
    event.preventDefault();
    if (busy || submitted || !copy) return;
    try {
      const params = currentParams();
      if (copy.missing_fields.length && !$("center-missing-confirm").checked)
        throw Error("请明确核对原记录缺失参数的补充值");
      if (
        readinessKinds.has(copy.kind) &&
        (!checked ||
          checked.params !== JSON.stringify(params) ||
          !(checked.report.ready === true || checked.report.status === "ready"))
      )
        throw Error("请先通过当前参数的数据预检");
      if (!execute && $("center-review")?.querySelector) {
        $("center-review-content").innerHTML =
          `<p>${esc(copy.label)} · ${copy.source_run_id ? "来源 " + esc(copy.source_run_id) : "新建配置"}</p><p>${params.stock_pool_id ? `冻结股票池 ${esc(params.stock_pool_id)} · revision ${esc(params.stock_pool_revision)} · ${copy.scope.length} 只` : `明确股票范围 ${esc((params.symbols || [params.symbol]).join(" "))}`}</p>${renderValue(params)}<p>成本单位 bp；相对因子权重不是资金百分比。执行假设沿用原模型。</p>`;
        $("center-review").hidden = false;
        $("center-review").scrollIntoView({ block: "nearest" });
        return;
      }
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
      const source = p.task_id
        ? "task"
        : copy.kind === "screen"
          ? "screen"
          : copy.kind === "document_event"
            ? "event"
            : "index";
      const record = p.task_id || p.run_id || p.batch_id;
      $("center-submitted-link").innerHTML = record
        ? `<a href="/backtests/detail?${esc(new URLSearchParams({ source, record }).toString())}">查看本次运行详情</a> · <a href="/backtests/history">返回历史</a>`
        : '<a href="/backtests/history">查看历史记录</a>';
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
    const configKind = location.pathname.endsWith("/factors")
      ? "factor_lab"
      : location.pathname.endsWith("/daily-signals")
        ? "daily_signal"
        : location.pathname.endsWith("/new")
          ? "factor_backtest"
          : null;
    if ($("center-types"))
      $("center-types").hidden =
        location.pathname !== "/backtests" ||
        query.has("kind") ||
        query.has("status") ||
        query.has("page");
    if ($("center-event-entry"))
      $("center-event-entry").hidden = $("center-types").hidden;
    if (location.pathname.endsWith("/detail")) {
      $("center-history").hidden = true;
      $("center-detail").hidden = false;
      await detail(query);
    } else if (location.pathname.endsWith("/configure") || configKind) {
      $("center-history").hidden = true;
      $("center-configure").hidden = false;
      $("center-copy-form").addEventListener("input", invalidate);
      $("center-copy-form").addEventListener("submit", submit);
      $("center-preflight").addEventListener("click", preflight);
      $("center-copy-form").addEventListener("change", invalidate);
      $("center-execute").addEventListener("click", (e) => submit(e, true));
      $("center-review-cancel").addEventListener("click", () => {
        $("center-review").hidden = true;
      });
      await configure(query, configKind);
    } else {
      if (
        location.pathname === "/backtests" &&
        !query.has("kind") &&
        !query.has("status") &&
        !query.has("page")
      ) {
        $("center-history").hidden = true;
        return;
      }
      $("center-title").textContent = "研究与回测历史";
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
  return {
    history,
    detail,
    preflight,
    submit,
    configure,
    currentParams,
    factorName,
    strategyName,
  };
})();
