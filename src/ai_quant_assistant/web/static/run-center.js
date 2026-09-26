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
      : `暂无中文名称（${code}）`;
  }
  function enumName(value, key) {
    if (value === "none" && key === "adjustment") return "不复权（none）";
    return catalog.enums[value]
      ? `${catalog.enums[value]}（${value}）`
      : `${String(value)}（暂无中文说明）`;
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
    if (!r.ok) {
      const detail = p.detail;
      if (Array.isArray(detail)) {
        const messages = detail.map((item) => {
          const field = (item.loc || []).filter((part) => !["body", "parameters"].includes(part)).join(" → ");
          return `${field ? field + "：" : ""}${String(item.msg || "参数不合法").replace(/^Value error,\s*/, "")}`;
        });
        throw Error(messages.join("；"));
      }
      throw Error(typeof detail === "string" ? detail : JSON.stringify(detail || p));
    }
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
    initial_capital: "初始资金（元）",
    commission_bps: "佣金（bp）",
    slippage_bps: "滑点（bp）",
    stamp_duty_bps: "卖出印花税（bp）",
    lot_size: "买入整手（股/手）",
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
      trading_calendar: "交易日历",
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
      technical_warmup_start: report.technical_warmup_start,
      technical_warmup_trading_days: report.technical_warmup_trading_days,
      technical_warmup_explanation: report.technical_warmup_explanation,
      financial_validity_rule: report.financial_validity_rule,
      missing_policy: report.missing_policy,
      checks,
    };
    return `<section class="preflight-summary ${tone}" aria-live="polite"><header><div><p class="preflight-kicker">${esc(report.status_label || (ready ? "可以运行" : "预检阻断"))}</p><h2>${heading}</h2></div><strong class="preflight-count">${esc(summary)}</strong></header><p>${explanation}</p>${advice}${checkTable}${blockerDetails}${warningDetails}<details><summary>查看冻结参数与完整诊断</summary>${renderValue(diagnostic)}</details></section>`;
  }
  function factorRow(item = { code: "roe", weight: 1, direction: "desc" }) {
    const options = { ...catalog.factors };
    if (!options[item.code]) options[item.code] = "暂无中文名称";
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
    return `<div class="factor-editor-row candidate-factor-row"><label>因子代码<input data-candidate-code list="candidate-factor-codes" value="${esc(item.code)}"><small>${esc(factorName(item.code))}；可用财务代码、return_N 或 ma_deviation_N</small></label><label>相对权重 ${termHelp("factor_weight", "")}<input data-candidate-weight type="number" min="0.001" max="1000" step="any" value="${esc(item.weight)}"><small>用于合成候选得分，不代表持仓资金比例</small></label><label>方向<select data-candidate-direction>${["desc", "asc"].map((k) => `<option value="${k}" ${k === item.direction ? "selected" : ""}>${esc(enumName(k))}</option>`).join("")}</select></label><button data-candidate-remove type="button">移除</button></div>`;
  }
  function signalRuleRow(item = { kind: "ma_cross", operator: "cross_above", window: null, short_window: 5, long_window: 20, threshold: null }, side = "buy") {
    const kinds = { close_vs_ma: "收盘价与均线", ma_compare: "双均线状态", ma_cross: "双均线交叉", return_threshold: "区间涨跌幅阈值" };
    const operators = { above: "高于", below: "低于", cross_above: "上穿", cross_below: "下穿" };
    return `<div class="signal-rule-row" data-rule-side="${side}"><label>规则类型<select data-rule-kind>${Object.entries(kinds).map(([k,v]) => `<option value="${k}" ${k === item.kind ? "selected" : ""}>${v}（${k}）</option>`).join("")}</select></label><label>比较方向<select data-rule-operator>${Object.entries(operators).map(([k,v]) => `<option value="${k}" ${k === item.operator ? "selected" : ""}>${v}（${k}）</option>`).join("")}</select></label><label>周期 N（交易日）<input data-rule-window type="number" min="1" max="250" value="${esc(item.window ?? "")}"></label><label>短周期（交易日）<input data-rule-short type="number" min="2" max="250" value="${esc(item.short_window ?? "")}"></label><label>长周期（交易日）<input data-rule-long type="number" min="2" max="250" value="${esc(item.long_window ?? "")}"></label><label>阈值（小数；0.05=5%）<input data-rule-threshold type="number" min="-10" max="10" step="any" value="${esc(item.threshold ?? "")}"></label><p class="rule-explanation">${esc(ruleText(item))}</p><button data-rule-remove type="button">移除</button></div>`;
  }

  function ruleText(rule = {}) {
    const op = { above: "高于", below: "低于", cross_above: "从下方向上穿越", cross_below: "从上方向下穿越" }[rule.operator] || rule.operator || "未选择";
    if (rule.kind === "close_vs_ma") return `收盘价${op} MA${rule.window ?? "N"}（持续状态）`;
    if (rule.kind === "ma_compare") return `MA${rule.short_window ?? "短"}${op} MA${rule.long_window ?? "长"}（持续状态）`;
    if (rule.kind === "ma_cross") return `MA${rule.short_window ?? "短"}${op} MA${rule.long_window ?? "长"}（变化事件）`;
    if (rule.kind === "return_threshold") {
      const threshold = Number.isFinite(rule.threshold) ? `${(rule.threshold * 100).toFixed(2)}%（原值 ${rule.threshold}）` : "未填写阈值";
      return `${rule.window ?? "N"} 日涨跌幅${op}${threshold}`;
    }
    return `${rule.kind || "未知规则"}（暂无中文说明）`;
  }

  const dailyTemplates = {
    ma_cross: {
      buy_rules: [{ kind: "ma_cross", operator: "cross_above", window: null, short_window: 5, long_window: 20, threshold: null }],
      sell_rules: [{ kind: "ma_cross", operator: "cross_below", window: null, short_window: 5, long_window: 20, threshold: null }],
      buy_rule_mode: "all", sell_rule_mode: "any",
    },
    trend_state: {
      buy_rules: [{ kind: "close_vs_ma", operator: "above", window: 20, short_window: null, long_window: null, threshold: null }],
      sell_rules: [{ kind: "close_vs_ma", operator: "below", window: 20, short_window: null, long_window: null, threshold: null }],
      buy_rule_mode: "all", sell_rule_mode: "any",
    },
    momentum_confirm: {
      buy_rules: [
        { kind: "return_threshold", operator: "above", window: 20, short_window: null, long_window: null, threshold: 0.05 },
        { kind: "close_vs_ma", operator: "above", window: 20, short_window: null, long_window: null, threshold: null },
      ],
      sell_rules: [
        { kind: "return_threshold", operator: "below", window: 20, short_window: null, long_window: null, threshold: -0.03 },
        { kind: "close_vs_ma", operator: "below", window: 20, short_window: null, long_window: null, threshold: null },
      ],
      buy_rule_mode: "all", sell_rule_mode: "any",
    },
  };

  function validateDailySignal(params, scopeSize) {
    const errors = [];
    if (!params.buy_rules?.length) errors.push("买入规则为空。空规则不会被解释为始终满足，请至少添加一条买入规则；修改后需要重新检查数据。");
    if (!params.sell_rules?.length) errors.push("卖出规则为空。现有引擎要求至少一条卖出规则；修改后需要重新检查数据。");
    for (const [side, rules] of [["买入", params.buy_rules || []], ["卖出", params.sell_rules || []]])
      rules.forEach((rule, index) => {
        if (["ma_compare", "ma_cross"].includes(rule.kind) && !(rule.short_window >= 2 && rule.short_window < rule.long_window && rule.long_window <= 250))
          errors.push(`${side}规则 ${index + 1}：MA 短周期当前为 ${known(rule.short_window)}，长周期为 ${known(rule.long_window)}。短周期必须小于长周期，且均在 2～250 之间，例如 MA5 与 MA20；修改后需要重新检查数据。`);
        const low = rule.kind === "return_threshold" ? 1 : 2;
        if (["close_vs_ma", "return_threshold"].includes(rule.kind) && !(rule.window >= low && rule.window <= 250))
          errors.push(`${side}规则 ${index + 1}：周期当前为 ${known(rule.window)}，合法范围为 ${low}～250 个交易日；修改后需要重新检查数据。`);
        if (rule.kind === "return_threshold" && !Number.isFinite(rule.threshold))
          errors.push(`${side}规则 ${index + 1}：涨跌幅阈值未填写，例如 0.05 表示 5%；修改后需要重新检查数据。`);
      });
    if (Number.isFinite(scopeSize) && params.candidate_count > scopeSize)
      errors.push(`候选数量当前为 ${params.candidate_count}，冻结股票范围只有 ${scopeSize} 只。请减少候选数量或扩大范围；修改后需要重新检查数据。`);
    if (params.max_positions > params.candidate_count)
      errors.push(`最大持仓数量当前为 ${params.max_positions}，候选数量为 ${params.candidate_count}。最大持仓数量不能大于候选数量；修改后需要重新检查数据。`);
    if (params.start_date && params.end_date && params.start_date >= params.end_date)
      errors.push(`开始日期 ${params.start_date} 不早于结束日期 ${params.end_date}。请调整区间；修改后需要重新检查数据。`);
    return errors;
  }

  function dailySignalSummary(params = {}) {
    const refresh = catalog.enums[params.candidate_refresh] || params.candidate_refresh || "未知频率";
    const factors = (params.candidate_factors || []).map((item) => `${factorName(item.code)}${item.direction === "desc" ? "从高到低" : "从低到高"}（相对权重 ${known(item.weight)}）`).join("、") || "候选因子未配置";
    const rules = (side) => (params[side + "_rules"] || []).map(ruleText).join("；") || "未配置";
    return `${refresh}更新候选股票，根据${factors}合成得分，选择前 ${known(params.candidate_count)} 只。候选名单更新与每日信号检查是两个过程。每天收盘后检查候选股票；买入规则${enumName(params.buy_rule_mode)}：${rules("buy")}。满足时生成买入指令，最早在下一交易日开盘按滑点、费用、整手和资金约束尝试成交。卖出规则${enumName(params.sell_rule_mode)}：${rules("sell")}；${params.exit_on_candidate_drop ? "退出候选名单时也生成卖出指令" : "退出候选名单后继续持有，直到出现卖出信号"}。卖出受限时保留退出意图并继续尝试，回测结束不会隐含强制平仓。`;
  }

  function dailySignalTimeline(signals = [], orders = []) {
    const used = new Set();
    const rows = [];
    for (const signal of signals) {
      const matches = orders.map((order, index) => ({ order, index })).filter(({ order }) =>
        order.symbol === signal.symbol &&
        String(order.signal_date || "") === String(signal.signal_date || "") &&
        String(order.side || "") === String(signal.action || ""));
      if (!matches.length) rows.push({ signal, order: null });
      for (const match of matches) {
        used.add(match.index);
        rows.push({ signal, order: match.order });
      }
    }
    orders.forEach((order, index) => {
      if (!used.has(index)) rows.push({ signal: null, order });
    });
    return rows.sort((a, b) => String(b.order?.execution_date || b.signal?.signal_date || "").localeCompare(String(a.order?.execution_date || a.signal?.signal_date || "")));
  }

  function renderDailyTimeline(rows, filters = {}) {
    const selected = rows.filter(({ signal, order }) => {
      const symbol = order?.symbol || signal?.symbol;
      const side = order?.side || signal?.action;
      const status = order?.status || signal?.status;
      const group = status === "executed" ? "executed" : ["retry_next_trading_day", "pending"].includes(order?.next_action) ? "retry" : "unfilled";
      return (!filters.symbol || symbol === filters.symbol) && (!filters.side || side === filters.side) && (!filters.status || group === filters.status) && (!filters.reason || order?.reason === filters.reason) && (!filters.execution_date || order?.execution_date === filters.execution_date);
    }).slice(0, 100);
    return `<div class="table-shell"><table class="signal-timeline"><thead><tr><th>信号</th><th>候选 / 触发依据</th><th>计划执行</th><th>成交或未成交</th><th>后续处理</th></tr></thead><tbody>${selected.map(({ signal, order }) => `<tr><td><strong>${esc(enumName(order?.side || signal?.action))}</strong> · ${esc(known(signal?.signal_date || order?.signal_date))}<small>${esc(known(order?.symbol || signal?.symbol))} · 收盘价 ${esc(known(signal?.signal_price ?? order?.signal_price))}</small></td><td>资格：${esc(display(signal?.candidate_eligible, "candidate_eligible"))}<small>排名 ${esc(known(signal?.candidate_rank ?? order?.candidate_rank))} · 得分 ${esc(known(signal?.candidate_score ?? order?.candidate_score))}</small><details><summary>触发依据</summary><code>${esc(known(signal?.rule_evidence))}</code></details></td><td>${esc(known(order?.planned_execution_date || signal?.planned_execution_date))}<small>信号确认后最早在该日开盘尝试</small></td><td><strong>${esc(enumName(order?.status || signal?.status))}</strong><small>模拟成交日 ${esc(known(order?.execution_date))} · 开盘参考价 ${esc(known(order?.execution_price))} · 数量 ${esc(known(order?.shares ?? signal?.planned_shares))}</small><small>${order?.reason ? esc(enumName(order.reason)) : "没有保存未成交原因"}</small></td><td>${esc(order?.next_action ? enumName(order.next_action) : "未记录")}</td></tr>`).join("") || '<tr><td colspan="5">没有符合筛选条件的已保存记录；不推算缺失字段。</td></tr>'}</tbody></table></div><p>当前仅展示匹配结果前 100 条；接口最多读取每类最近 2,000 条，完整数据请下载原 CSV。</p>`;
  }
  function termHelp(key, label = null) {
    const item = catalog.terms?.[key];
    if (!item)
      return `<span class="term-unknown">${esc(label || key)}（暂无中文说明）</span>`;
    const visible = label === null ? item.label : label;
    return `<details class="term-help" data-term="${esc(key)}"><summary>${esc(visible)}<button type="button" aria-label="查看${esc(item.label)}解释" aria-expanded="false">?</button></summary><div class="term-card"><button class="term-close" type="button" data-term-close aria-label="关闭${esc(item.label)}解释">关闭</button><strong>${esc(item.professional)}</strong><p>${esc(item.short)}</p><p><b>口径：</b>${esc(item.calculation)}</p><p><b>例子：</b>${esc(item.example)}</p><p><b>限制：</b>${esc(item.limitations)}</p><code>${esc(item.code)}</code></div></details>`;
  }
  function wireTermHelp(root = document) {
    root.querySelectorAll?.("details.term-help").forEach((details) => {
      const button = details.querySelector?.("button[aria-expanded]");
      if (!button) return;
      const sync = () => button.setAttribute("aria-expanded", details.open ? "true" : "false");
      details.addEventListener("toggle", sync);
      details.querySelector?.("[data-term-close]")?.addEventListener("click", () => {
        details.open = false;
        sync();
        button.focus();
      });
      sync();
    });
  }
  const finite = (value) =>
    typeof value === "number" && Number.isFinite(value) ? value : null;
  const amount = (value) =>
    finite(value) == null
      ? "未记录"
      : Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const percent = (value) =>
    finite(value) == null ? "未记录" : (Number(value) * 100).toFixed(2) + "%";
  function navChartModel(rows = [], selectedIndex = 0, width = 900, height = 340, xTickCount = 6) {
    const series = Array.isArray(rows) ? rows : [];
    const values = series.flatMap((row) => [finite(row?.portfolio_nav), finite(row?.benchmark_nav)]).filter((value) => value != null);
    if (!series.length || !values.length)
      return { empty: true, width, height, series, xTicks: [], yTicks: [], segments: {} };
    const plot = { left: 76, right: width - 24, top: 28, bottom: height - 62 };
    let low = Math.min(...values), high = Math.max(...values);
    const padding = Math.max((high - low) * 0.08, Math.max(Math.abs(low), Math.abs(high), 1) * 0.01);
    low -= padding;
    high += padding;
    const span = high - low || 1;
    const x = (index) => plot.left + ((plot.right - plot.left) * index) / Math.max(1, series.length - 1);
    const y = (value) => plot.top + ((plot.bottom - plot.top) * (high - value)) / span;
    const segments = {};
    for (const key of ["portfolio_nav", "benchmark_nav"]) {
      const chunks = [];
      let chunk = [];
      series.forEach((row, index) => {
        const value = finite(row?.[key]);
        if (value == null) {
          if (chunk.length) chunks.push(chunk.join(" "));
          chunk = [];
        } else chunk.push(`${x(index).toFixed(2)},${y(value).toFixed(2)}`);
      });
      if (chunk.length) chunks.push(chunk.join(" "));
      segments[key] = chunks;
    }
    const count = Math.min(Math.max(2, xTickCount), series.length);
    const indexes = [...new Set(Array.from({ length: count }, (_, index) => Math.round(index * (series.length - 1) / Math.max(1, count - 1))))];
    const safeIndex = Math.max(0, Math.min(series.length - 1, Number(selectedIndex) || 0));
    const selected = series[safeIndex];
    return {
      empty: false, width, height, plot, series, segments,
      xTicks: indexes.map((index) => ({ x: x(index), label: known(series[index]?.date) })),
      yTicks: Array.from({ length: 6 }, (_, index) => {
        const value = low + span * index / 5;
        return { y: y(value), label: value.toFixed(3) };
      }),
      selected: {
        index: safeIndex, row: selected, x: x(safeIndex),
        portfolioY: finite(selected?.portfolio_nav) == null ? null : y(selected.portfolio_nav),
        benchmarkY: finite(selected?.benchmark_nav) == null ? null : y(selected.benchmark_nav),
      },
    };
  }
  function renderNavChart(rows, selectedIndex = 0, xTickCount = 6) {
    const model = navChartModel(rows, selectedIndex, 900, 340, xTickCount);
    if (model.empty) return '<p class="empty-state">没有可绘制的已保存净值数据。</p>';
    const lines = (key, className) => model.segments[key].map((points) => `<polyline class="${className}" points="${esc(points)}"/>`).join("");
    const selected = model.selected;
    const row = selected.row || {};
    const difference = finite(row.portfolio_nav) != null && finite(row.benchmark_nav) != null ? row.portfolio_nav - row.benchmark_nav : null;
    return `<div class="nav-chart-heading"><div><h3>净值走势 ${termHelp("nav")}</h3><p>横轴为实际交易日期；纵轴为累计净值（初始值=1.00），不是股票价格。</p></div><div class="nav-chart-legend" aria-label="图例"><span><i class="strategy-key"></i>策略累计净值</span><span><i class="benchmark-key"></i>基准累计净值</span></div></div><div class="nav-chart-shell"><svg id="center-nav-svg" role="img" aria-label="策略与基准累计净值，横轴日期，纵轴累计净值" viewBox="0 0 ${model.width} ${model.height}">${model.yTicks.map((tick) => `<line class="nav-grid" x1="${model.plot.left}" x2="${model.plot.right}" y1="${tick.y}" y2="${tick.y}"/><text class="nav-tick" text-anchor="end" x="${model.plot.left - 9}" y="${tick.y + 4}">${esc(tick.label)}</text>`).join("")}<line class="nav-axis" x1="${model.plot.left}" x2="${model.plot.right}" y1="${model.plot.bottom}" y2="${model.plot.bottom}"/><line class="nav-axis" x1="${model.plot.left}" x2="${model.plot.left}" y1="${model.plot.top}" y2="${model.plot.bottom}"/>${model.xTicks.map((tick) => `<line class="nav-grid vertical" x1="${tick.x}" x2="${tick.x}" y1="${model.plot.top}" y2="${model.plot.bottom}"/><text class="nav-tick" text-anchor="middle" x="${tick.x}" y="${model.plot.bottom + 24}">${esc(tick.label)}</text>`).join("")}<text class="nav-axis-title" text-anchor="middle" transform="translate(17 ${(model.plot.top + model.plot.bottom) / 2}) rotate(-90)">累计净值（初始值=1.00）</text><text class="nav-axis-title" text-anchor="middle" x="${(model.plot.left + model.plot.right) / 2}" y="${model.height - 9}">日期</text>${lines("benchmark_nav", "nav-benchmark")}${lines("portfolio_nav", "nav-strategy")}<line class="nav-selection" x1="${selected.x}" x2="${selected.x}" y1="${model.plot.top}" y2="${model.plot.bottom}"/>${selected.portfolioY == null ? "" : `<circle class="nav-point strategy-point" cx="${selected.x}" cy="${selected.portfolioY}" r="6"/>`}${selected.benchmarkY == null ? "" : `<circle class="nav-point benchmark-point" cx="${selected.x}" cy="${selected.benchmarkY}" r="5"/>`}<rect class="nav-date-bg" x="${Math.max(model.plot.left, Math.min(selected.x - 47, model.plot.right - 94))}" y="${model.plot.top + 5}" width="94" height="23" rx="4"/><text class="nav-date-label" text-anchor="middle" x="${Math.max(model.plot.left + 47, Math.min(selected.x, model.plot.right - 47))}" y="${model.plot.top + 21}">${esc(known(row.date))}</text></svg></div><div class="nav-selected-card" aria-live="polite"><div><span>日期</span><strong>${esc(known(row.date))}</strong></div><div><span>策略累计净值</span><strong>${esc(known(row.portfolio_nav))}</strong></div><div><span>基准累计净值</span><strong>${esc(known(row.benchmark_nav))}</strong></div><div><span>两者差值</span><strong>${esc(known(difference))}</strong></div><div><span>账户总资产</span><strong>${esc(amount(row.portfolio_value))}</strong></div><div><span>现金</span><strong>${esc(amount(row.cash))}</strong></div><div><span>持仓市值</span><strong>${esc(amount(row.position_value))}</strong></div><div><span>累计交易成本</span><strong>${esc(amount(row.total_cost))}</strong></div></div>`;
  }

  function dailyResultView(data = {}, spec = {}) {
    const series = Array.isArray(data.series) ? data.series : [];
    const orders = Array.isArray(data.orders) ? data.orders : null;
    const positions = Array.isArray(data.positions) ? data.positions : null;
    const metrics = { ...(data.metrics || {}), ...(data.summary || {}) };
    const diagnostics = data.diagnostics || {};
    const finalNav = series.length ? series[series.length - 1] : {};
    const executed = orders?.filter((row) => row.status === "executed") || [];
    const unfilled = orders?.filter((row) => row.status !== "executed") || [];
    const finalDate = positions?.reduce((latest, row) => !row.date || latest > row.date ? latest : row.date, "") || null;
    const finalPositions = finalDate ? positions.filter((row) => row.date === finalDate).map((row) => ({ ...row, asset_ratio: finite(finalNav.portfolio_value) > 0 && finite(row.market_value) != null ? row.market_value / finalNav.portfolio_value : null })) : [];
    return {
      series, executed, unfilled, finalPositions,
      account: {
        initial_capital: spec.initial_capital,
        final_value: finalNav.portfolio_value ?? metrics.final_value,
        final_cash: finalNav.cash,
        final_position_value: finalNav.position_value,
        total_return: metrics.total_return,
        benchmark_total_return: metrics.benchmark_total_return,
        excess_return: metrics.excess_return,
        max_drawdown: metrics.max_drawdown,
        total_transaction_cost: metrics.total_transaction_cost ?? finalNav.total_cost,
        signal_count: metrics.signal_count,
        order_count: orders == null ? metrics.order_attempt_count : orders.length,
        buy_execution_count: orders == null ? null : executed.filter((row) => row.side === "buy").length,
        sell_execution_count: orders == null ? null : executed.filter((row) => row.side === "sell").length,
        unfilled_count: orders == null ? null : unfilled.length,
        expired_buy_count: orders == null ? null : unfilled.filter((row) => row.side === "buy" && row.next_action === "expired").length,
        pending_exit_count: Array.isArray(diagnostics.open_exit_intents) ? diagnostics.open_exit_intents.length : metrics.pending_exit_count,
        ending_position_count: Array.isArray(diagnostics.open_positions) ? diagnostics.open_positions.length : metrics.open_position_count,
      },
    };
  }
  function renderAccountOverview(view) {
    const a = view.account;
    const item = (label, value, help = "") => `<div><dt>${esc(label)}${help}</dt><dd>${esc(value)}</dd></div>`;
    return `<article class="daily-account-summary"><h3>账户结果概览</h3><dl class="center-meta account-grid">${item("初始资金", amount(a.initial_capital))}${item("回测结束总资产", amount(a.final_value))}${item("回测结束现金", amount(a.final_cash))}${item("回测结束日持仓市值", amount(a.final_position_value), termHelp("position_value", ""))}${item("累计收益", percent(a.total_return))}${item("基准收益", percent(a.benchmark_total_return))}${item("超额收益", percent(a.excess_return))}${item("最大回撤", percent(a.max_drawdown), termHelp("max_drawdown", ""))}${item("交易总成本", amount(a.total_transaction_cost), termHelp("transaction_cost", ""))}${item("信号 / 指令", `${known(a.signal_count)} / ${known(a.order_count)}`)}${item("买入 / 卖出成交", `${known(a.buy_execution_count)} / ${known(a.sell_execution_count)}`)}${item("未成交 / 失效买单", `${known(a.unfilled_count)} / ${known(a.expired_buy_count)}`)}${item("期末持仓 / 等待退出", `${known(a.ending_position_count)} / ${known(a.pending_exit_count)}`)}</dl><p>所有金额和数量均来自已保存结果或对已保存订单的确定性计数；缺失字段显示“未记录”。</p></article>`;
  }
  function renderExecutedTrades(rows, filters = {}) {
    const selected = rows.filter((row) => (!filters.symbol || row.symbol === filters.symbol) && (!filters.side || row.side === filters.side) && (!filters.execution_date || row.execution_date === filters.execution_date)).slice(0, 100);
    return `<div class="table-shell"><table><thead><tr><th>信号日</th><th>计划执行日</th><th>实际成交日</th><th>股票</th><th>动作</th><th>数量（股）</th><th>信号收盘价</th><th>开盘参考价</th><th>成交金额</th><th>本次交易成本</th><th>候选排名 / 得分</th><th>状态 / 后续</th><th>核对资金与持仓</th></tr></thead><tbody>${selected.map((r) => `<tr><td>${esc(known(r.signal_date))}</td><td>${esc(known(r.planned_execution_date))}</td><td>${esc(known(r.execution_date))}</td><td>${esc(known(r.symbol))}</td><td>${esc(enumName(r.side))}</td><td>${esc(known(r.shares))}</td><td>${esc(known(r.signal_price))}</td><td>${esc(known(r.execution_price))}</td><td>${esc(amount(r.notional))}</td><td>${esc(amount(r.cost))}</td><td>${esc(known(r.candidate_rank))} / ${esc(known(r.candidate_score))}</td><td>${esc(enumName(r.status))}<small>${esc(r.next_action ? enumName(r.next_action) : "未记录")}</small></td><td><details class="trade-audit"><summary>展开核对</summary><dl><div><dt>成交前 / 后现金</dt><dd>${esc(amount(r.cash_before))} → ${esc(amount(r.cash_after))}</dd></div><div><dt>成交前 / 后持仓</dt><dd>${esc(known(r.position_shares_before))} → ${esc(known(r.position_shares_after))} 股</dd></div><div><dt>佣金</dt><dd>${esc(amount(r.commission_cost))}</dd></div><div><dt>滑点成本</dt><dd>${esc(amount(r.slippage_cost))}</dd></div><div><dt>卖出印花税</dt><dd>${esc(amount(r.stamp_duty_cost))}</dd></div><div><dt>费用合计</dt><dd>${esc(amount(r.cost))}</dd></div><div><dt>关联信号</dt><dd>${esc(known(r.signal_id))}</dd></div></dl><p>成交后现金是完成本次模拟交易并扣除本次费用后的账户现金；开盘参考价仍为未复权口径。</p></details></td></tr>`).join("") || '<tr><td colspan="13">没有符合筛选条件的实际模拟成交；未成交不会放入本表。</td></tr>'}</tbody></table></div><p>当前仅展示前 100 条匹配记录；完整数据请下载 orders.csv 或 Excel。</p>`;
  }
  function renderFinalPositions(rows) {
    return `<div class="table-shell"><table><thead><tr><th>股票</th><th>数量（股）</th><th>回测结束日估值价格</th><th>回测结束日持仓市值</th><th>占期末总资产</th><th>候选排名 / 得分</th><th>等待退出</th></tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(known(r.symbol))}</td><td>${esc(known(r.shares))}</td><td>${esc(known(r.price))}</td><td>${esc(amount(r.market_value))}</td><td>${esc(percent(r.asset_ratio))}</td><td>${esc(known(r.candidate_rank))} / ${esc(known(r.candidate_score))}</td><td>${r.pending_exit === true ? "是" : r.pending_exit === false ? "否" : "未记录"}</td></tr>`).join("") || '<tr><td colspan="7">没有已保存的回测结束日持仓。</td></tr>'}</tbody></table></div>`;
  }
  function renderUnfilledOrders(rows) {
    return `<div class="table-shell"><table><thead><tr><th>股票</th><th>动作</th><th>信号日</th><th>计划执行日</th><th>计划/记录数量</th><th>未成交原因</th><th>指令状态</th><th>是否失效</th><th>是否重试</th><th>后续处理</th></tr></thead><tbody>${rows.slice(0, 100).map((r) => `<tr><td>${esc(known(r.symbol))}</td><td>${esc(enumName(r.side))}</td><td>${esc(known(r.signal_date))}</td><td>${esc(known(r.planned_execution_date))}</td><td>${esc(known(r.shares))}</td><td>${esc(r.reason ? enumName(r.reason) : "未记录")}</td><td>${esc(enumName(r.status))}</td><td>${r.next_action === "expired" ? "是" : "否"}</td><td>${r.next_action === "retry_next_trading_day" ? "是" : "否"}</td><td>${esc(r.next_action ? enumName(r.next_action) : "未记录")}</td></tr>`).join("") || '<tr><td colspan="10">没有未成交或等待重试的已保存指令。</td></tr>'}</tbody></table></div><p>未成交不是“成交数量为 0”的真实交易；原因和后续处理直接读取保存记录。</p>`;
  }
  function renderDailyAccounts(rows) {
    const selected = rows.slice(-200);
    return `<div class="table-shell"><table><thead><tr><th>日期</th><th>现金</th><th>持仓市值</th><th>账户总资产</th><th>策略累计净值</th><th>基准累计净值</th><th>累计交易成本</th></tr></thead><tbody>${selected.map((r) => `<tr><td>${esc(known(r.date))}</td><td>${esc(amount(r.cash))}</td><td>${esc(amount(r.position_value))}</td><td>${esc(amount(r.portfolio_value))}</td><td>${esc(known(r.portfolio_nav))}</td><td>${esc(known(r.benchmark_nav))}</td><td>${esc(amount(r.total_cost))}</td></tr>`).join("")}</tbody></table></div><p>页面最多展示已返回序列的最后 200 条；完整每日记录请下载 daily_nav.csv 或 Excel。</p>`;
  }
  function renderAuditSummary(summary = {}, rows = []) {
    const failures = Array.isArray(summary.failures) ? summary.failures : rows.filter((row) => row.status === "failed");
    const status = summary.status === "passed" ? "核对通过" : summary.status === "warning" ? "完成但有差异" : "历史运行未记录";
    return `<article class="audit-summary ${esc(summary.status || "unknown")}"><h3>账户审计状态：${esc(status)}</h3><dl class="center-meta account-grid"><div><dt>已核对交易日</dt><dd>${esc(known(summary.checked_dates))}</dd></div><div><dt>核对项</dt><dd>${esc(known(summary.check_count))}</dd></div><div><dt>异常项</dt><dd>${esc(known(summary.failure_count))}</dd></div><div><dt>金额容差</dt><dd>绝对 ${esc(known(summary.absolute_tolerance))}；相对 ${esc(known(summary.relative_tolerance))}</dd></div></dl><p>${esc(summary.notice || "旧运行没有账户审计产物；不根据最终结果倒推。")}</p>${failures.length ? `<div class="table-shell"><table><thead><tr><th>日期</th><th>检查项</th><th>期望</th><th>实际</th><th>差异</th><th>容差</th></tr></thead><tbody>${failures.slice(0, 100).map((r) => `<tr><td>${esc(known(r.date))}</td><td>${esc(known(r.check))}</td><td>${esc(known(r.expected_value))}</td><td>${esc(known(r.actual_value))}</td><td>${esc(known(r.difference))}</td><td>${esc(known(r.tolerance))}</td></tr>`).join("")}</tbody></table></div>` : ""}</article>`;
  }
  function renderReproducibility(info = {}) {
    const items = [
      ["运行 ID", info.run_id], ["参数 SHA-256", info.parameter_sha256],
      ["股票池 / revision", info.stock_pool_id ? `${info.stock_pool_id} / ${known(info.stock_pool_revision)}` : null],
      ["冻结范围", info.frozen_symbols_sha256 ? `${info.frozen_symbol_count} 只 · ${info.frozen_symbols_sha256}` : null],
      ["研究区间", info.research_start_date ? `${info.research_start_date} → ${known(info.research_end_date)}` : null],
      ["技术指标预热起点", info.technical_warmup_start], ["基准 / 信号复权", info.benchmark_symbol ? `${info.benchmark_symbol} / ${known(info.signal_adjustment)}` : null],
      ["输入读取时间", info.input_read_at], ["应用 / Git", info.application_version ? `${info.application_version} / ${known(info.git_commit_sha)}` : null],
      ["Schema / 时区", info.schema_version == null ? null : `${info.schema_version} / ${known(info.timezone)}`],
      ["执行规则版本", info.execution_rule_version], ["报告模板版本", info.report_template_version],
    ];
    return `<details class="reproducibility"><summary>运行可复现信息</summary><dl class="center-meta">${items.map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(known(value))}</dd></div>`).join("")}</dl><p>${esc(info.limitation || "历史运行缺少的来源字段显示为未记录，不按当前环境补写。")}</p></details>`;
  }
  function priceChartModel(payload = {}, selectedIndex = 0, width = 900, height = 380, xTickCount = 6) {
    const series = Array.isArray(payload.series) ? payload.series : [];
    const lineKeys = series.length ? Object.keys(series[0]).filter((key) => key === "signal_close" || /^ma_\d+$/.test(key)) : [];
    const values = series.flatMap((row) => lineKeys.map((key) => finite(row[key]))).filter((value) => value != null);
    if (!series.length || !values.length) return { empty: true, series, lineKeys };
    const plot = { left: 76, right: width - 24, top: 34, bottom: height - 88 };
    let low = Math.min(...values), high = Math.max(...values);
    const padding = Math.max((high - low) * .08, Math.max(Math.abs(low), Math.abs(high), 1) * .01);
    low -= padding; high += padding;
    const span = high - low || 1;
    const x = (index) => plot.left + (plot.right - plot.left) * index / Math.max(1, series.length - 1);
    const y = (value) => plot.top + (plot.bottom - plot.top) * (high - value) / span;
    const dateIndex = new Map(series.map((row, index) => [String(row.date).slice(0, 10), index]));
    const segments = {};
    lineKeys.forEach((key) => {
      let chunk = []; const chunks = [];
      series.forEach((row, index) => {
        const value = finite(row[key]);
        if (value == null) { if (chunk.length) chunks.push(chunk.join(" ")); chunk = []; }
        else chunk.push(`${x(index).toFixed(2)},${y(value).toFixed(2)}`);
      });
      if (chunk.length) chunks.push(chunk.join(" "));
      segments[key] = chunks;
    });
    const events = [];
    (payload.signals || []).forEach((row) => {
      const index = dateIndex.get(String(row.signal_date).slice(0, 10));
      if (index != null) events.push({ kind: row.action === "sell" ? "sell-signal" : "buy-signal", date: row.signal_date, index, row });
    });
    (payload.orders || []).forEach((row) => {
      for (const [field, kind] of [["planned_execution_date", "planned"], ["execution_date", row.status === "executed" ? "executed" : "blocked"]]) {
        const index = dateIndex.get(String(row[field] || "").slice(0, 10));
        if (index != null) events.push({ kind, date: row[field], index, row });
      }
    });
    const count = Math.min(Math.max(2, xTickCount), series.length);
    const indexes = [...new Set(Array.from({ length: count }, (_, index) => Math.round(index * (series.length - 1) / Math.max(1, count - 1))))];
    const safeIndex = Math.max(0, Math.min(series.length - 1, Number(selectedIndex) || 0));
    return { empty: false, width, height, plot, series, lineKeys, segments, events, selectedIndex: safeIndex, selected: series[safeIndex], x, y,
      xTicks: indexes.map((index) => ({ x: x(index), label: String(series[index].date).slice(0, 10) })),
      yTicks: Array.from({ length: 6 }, (_, index) => { const value = low + span * index / 5; return { y: y(value), label: value.toFixed(3) }; }),
    };
  }
  function renderPriceSignalChart(payload, selectedIndex = 0, xTickCount = 6) {
    const model = priceChartModel(payload, selectedIndex, 900, 380, xTickCount);
    if (model.empty) return '<p class="empty-state">没有可绘制的已保存价格指标数据。</p>';
    const colors = ["price-close", "price-ma-one", "price-ma-two", "price-ma-other"];
    const lines = model.lineKeys.map((key, keyIndex) => model.segments[key].map((points) => `<polyline class="price-line ${colors[Math.min(keyIndex, colors.length - 1)]}" points="${esc(points)}"/>`).join("")).join("");
    const eventY = { "buy-signal": model.plot.bottom + 18, "sell-signal": model.plot.bottom + 31, planned: model.plot.bottom + 44, executed: model.plot.bottom + 57, blocked: model.plot.bottom + 70 };
    const markers = model.events.map((event) => `<circle class="price-event ${esc(event.kind)}" cx="${model.x(event.index)}" cy="${eventY[event.kind]}" r="5"><title>${esc(event.kind)} · ${esc(String(event.date).slice(0, 10))}</title></circle>`).join("");
    const selectedDate = String(model.selected.date).slice(0, 10);
    const selectedEvents = model.events.filter((event) => String(event.date).slice(0, 10) === selectedDate);
    return `<div class="price-chart-heading"><div><h3>${esc(payload.symbol)} · 价格、MA 与信号</h3><p>价格与 MA 使用${esc(enumName(payload.price_adjustment, "adjustment"))}信号口径。未复权开盘参考价只在事件卡中显示，不放到本价格轴。</p></div><div class="price-chart-legend">${model.lineKeys.map((key, index) => `<span><i class="${colors[Math.min(index, colors.length - 1)]}"></i>${esc(key === "signal_close" ? "信号收盘价" : key.toUpperCase().replace("_", "(" ) + ")")}</span>`).join("")}<span><i class="event-key"></i>事件日期</span></div></div><div class="price-chart-shell"><svg role="img" aria-label="${esc(payload.symbol)} 收盘价、移动平均线和信号执行日期" viewBox="0 0 ${model.width} ${model.height}">${model.yTicks.map((tick) => `<line class="nav-grid" x1="${model.plot.left}" x2="${model.plot.right}" y1="${tick.y}" y2="${tick.y}"/><text class="nav-tick" text-anchor="end" x="${model.plot.left - 9}" y="${tick.y + 4}">${esc(tick.label)}</text>`).join("")}<line class="nav-axis" x1="${model.plot.left}" x2="${model.plot.right}" y1="${model.plot.bottom}" y2="${model.plot.bottom}"/><line class="nav-axis" x1="${model.plot.left}" x2="${model.plot.left}" y1="${model.plot.top}" y2="${model.plot.bottom}"/>${model.xTicks.map((tick) => `<text class="nav-tick" text-anchor="middle" x="${tick.x}" y="${model.plot.bottom + 84}">${esc(tick.label)}</text>`).join("")}<text class="nav-axis-title" text-anchor="middle" transform="translate(17 ${(model.plot.top + model.plot.bottom) / 2}) rotate(-90)">价格（信号复权口径）</text>${lines}${markers}<line class="nav-selection" x1="${model.x(model.selectedIndex)}" x2="${model.x(model.selectedIndex)}" y1="${model.plot.top}" y2="${model.plot.bottom + 72}"/></svg></div><div class="price-selected-card" aria-live="polite"><strong>${esc(selectedDate)}</strong>${model.lineKeys.map((key) => `<span>${esc(key === "signal_close" ? "信号收盘价" : key.toUpperCase())}：${esc(known(model.selected[key]))}</span>`).join("")}${selectedEvents.map((event) => `<span>${esc(event.kind)}：${esc(enumName(event.row.side || event.row.action || event.row.status))} · 未复权开盘参考价 ${esc(known(event.row.execution_price))} · ${esc(event.row.reason ? enumName(event.row.reason) : "无额外原因")}</span>`).join("") || "<span>该日没有已保存的信号或指令事件。</span>"}</div>`;
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
        { ...catalog.factors, [value]: catalog.factors[value] || "暂无中文名称" },
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
    const helpKey = {
      slippage_bps: "slippage",
      commission_bps: "bp",
      stamp_duty_bps: "bp",
      adjustment: value === "qfq" ? "qfq" : null,
    }[key];
    return `<label for="${id}">${esc(titles[key] || key)}${helpKey ? termHelp(helpKey, "") : ""}${input}</label>`;
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
  function dataPlanExecutionSummary(plan = {}) {
    if (plan.execution_summary) return plan.execution_summary;
    const summary = {total:0,published:0,verified:0,failed:0,pending:0,retryable:0,unknown:0};
    for (const item of plan.items || []) {
      summary.total++;
      const status = item.status || "pending";
      if (["published","verified","failed"].includes(status)) summary[status]++;
      else if (["pending","planned","fetching"].includes(status)) summary.pending++;
      else summary.unknown++;
      if (["failed","pending","planned","fetching"].includes(status)) summary.retryable++;
    }
    return summary;
  }
  function dataPlanOutcome(plan = {}) {
    if (plan.outcome) return plan.outcome;
    const summary = dataPlanExecutionSummary(plan);
    if (["queued","running"].includes(plan.task?.status)) return plan.task.status;
    if (plan.task?.status === "failed") return summary.failed ? "download_failed" : summary.retryable ? "interrupted" : "unknown";
    if (plan.task?.status === "succeeded" || plan.status === "succeeded") return plan.report?.complete ? "ready" : "completed_with_gaps";
    return "planned";
  }
  function dataPlanOutcomeNotice(plan = {}) {
    const outcome = dataPlanOutcome(plan), summary = dataPlanExecutionSummary(plan);
    const counts = `同步完成 ${summary.published || 0} 项、验证完成 ${summary.verified || 0} 项、失败 ${summary.failed || 0} 项、待处理 ${summary.pending || 0} 项${summary.unknown ? `、未知 ${summary.unknown} 项` : ""}`;
    if (outcome === "ready") return `<p class="positive"><strong>补齐完成，数据检查通过。</strong> ${esc(counts)}；策略仍需单独确认运行。</p>`;
    if (outcome === "completed_with_gaps") {
      const financialGaps = (plan.report?.checks || []).filter((check) => check.dataset === "financial_metric" && check.satisfied !== true);
      const excluded = plan.input?.parameters?.missing_policy === "exclude" && financialGaps.length
        ? "当前策略会排除缺失历史时点，不会填零；是否可运行仍由最新预检决定。"
        : "是否可运行仍由最新预检决定。";
      return `<p class="page-notice"><strong>同步已完成，但最终检查仍有数据缺口。</strong> ${esc(counts)}。这不是行情、公告接口或后台同步失败；重复下载相同范围通常无法解决。${esc(excluded)}请重新检查本地数据、查看受影响日期，或返回修改策略。</p>`;
    }
    if (["download_failed","interrupted"].includes(outcome)) return `<p class="negative"><strong>${outcome === "download_failed" ? "存在真实同步失败" : "补齐任务被中断"}。</strong> ${esc(counts)}。仅 ${esc(summary.retryable || 0)} 个失败或未处理项可重试；已发布或已验证项目不会重新下载。</p>`;
    return `<p class="page-notice">${esc(counts)}。${outcome === "unknown" ? "无法可靠判断该历史计划的执行结果，请查看原错误和检查明细。" : "尚未开始下载。"}</p>`;
  }
  function renderDataPlan(plan = {}) {
    const task = plan.task;
    const outcome = dataPlanOutcome(plan);
    const outcomeLabels = {ready:"同步完成且检查通过",completed_with_gaps:"同步完成但仍有数据缺口",download_failed:"数据同步失败",interrupted:"任务中断",queued:"等待执行",running:"正在执行",planned:"尚未执行",unknown:"结果未知"};
    const taskStatusLabels = {queued:"等待执行",running:"正在执行",succeeded:"执行完成",failed:"执行结束（存在问题）"};
    const itemRows = (plan.items || []).map((item) => `<tr><td>${esc(item.symbol || "共享")}</td><td>${esc(enumName(item.dataset))}</td><td>${esc(item.adjustment ? enumName(item.adjustment, "adjustment") : "不适用")}</td><td>${esc(known(item.start_date))} 至 ${esc(known(item.end_date))}</td><td>${esc(known(item.estimated_partitions))}</td><td>${esc(enumName(item.status))}<small>${esc(item.attempts?.at?.(-1)?.error || item.reason || "")}</small></td></tr>`).join("");
    const progress = task && ["succeeded","failed"].includes(task.status) ? 100 : task?.progress;
    const taskText = task
      ? `${taskStatusLabels[task.status] || `${task.status}（暂无中文说明）`} · ${known(progress)}% · ${task.message || task.error || "等待更新"}`
      : "尚未确认：不会联网或下载";
    return `${dataPlanOutcomeNotice(plan)}<dl class="center-meta"><div><dt>计划 ID</dt><dd><code>${esc(plan.plan_id || "未记录")}</code></dd></div><div><dt>版本 / 参数哈希</dt><dd>${esc(known(plan.version))} / <code>${esc(known(plan.input_sha256))}</code></dd></div><div><dt>冻结范围</dt><dd>${esc(known(plan.input?.symbols_sha256))} · ${esc(plan.input?.resolved_symbols?.length ?? "未知")} 只</dd></div><div><dt>补齐结果</dt><dd>${esc(outcomeLabels[outcome] || `${outcome}（暂无中文说明）`)}</dd></div><div><dt>计划状态</dt><dd>${esc(enumName(plan.status))}</dd></div><div><dt>后台任务</dt><dd>${esc(taskText)}</dd></div></dl><p>${esc(plan.network_notice || "只有明确确认后才会访问公网；不会运行策略。")}</p><div class="table-shell"><table><thead><tr><th>股票</th><th>数据集</th><th>口径</th><th>日期范围</th><th>预计分区</th><th>状态 / 原因</th></tr></thead><tbody>${itemRows || '<tr><td colspan="6">没有可自动补齐项目；请查看预检中的不可补齐原因。</td></tr>'}</tbody></table></div>`;
  }
  function restoreDataPlanConfiguration(configuration, plan, expectedKind) {
    const input = plan?.input;
    if (input?.research_type !== expectedKind)
      throw Error(`计划类型 ${known(input?.research_type)} 与当前页面 ${expectedKind} 不一致`);
    if (!input.parameters || typeof input.parameters !== "object" || Array.isArray(input.parameters))
      throw Error("补齐计划没有保存可恢复的策略参数");
    if (!Array.isArray(input.resolved_symbols) || !input.resolved_symbols.length)
      throw Error("补齐计划没有保存冻结股票范围，不能使用当前默认股票池替代");
    const params = structuredClone(input.parameters);
    // Keep an editable explicit-symbol field available without changing the
    // frozen pool identity. currentParams removes it while a pool is selected.
    if (params.stock_pool_id && params.symbols == null) params.symbols = [];
    return {
      ...configuration,
      params,
      scope: [...input.resolved_symbols],
      source_run_id: null,
      warnings: [
        ...(configuration.warnings || []),
        `已从补齐计划 ${plan.plan_id} 恢复冻结参数和 ${input.resolved_symbols.length} 只股票；尚未运行策略。`,
      ],
    };
  }
  function normalizedParameterIdentity(value) {
    const normalized = structuredClone(value || {});
    if (normalized.stock_pool_id) delete normalized.symbols;
    return normalized;
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
      ruleText,
      dailyTemplates,
      validateDailySignal,
      dailySignalSummary,
      dailySignalTimeline,
      renderDailyTimeline,
      termHelp,
      navChartModel,
      renderNavChart,
      dailyResultView,
      renderAccountOverview,
      renderExecutedTrades,
      renderFinalPositions,
      renderUnfilledOrders,
      renderDailyAccounts,
      renderAuditSummary,
      renderReproducibility,
      priceChartModel,
      renderPriceSignalChart,
      factorName,
      strategyName,
      display,
      resultSections,
      dataPlanExecutionSummary,
      dataPlanOutcome,
      dataPlanOutcomeNotice,
      renderDataPlan,
      restoreDataPlanConfiguration,
      normalizedParameterIdentity,
    };
  if (typeof document === "undefined") return {};
  const $ = (id) => document.getElementById(id);
  let sequence = 0,
    page = 1,
    total = 0,
    busy = false,
    submitted = false,
    copy = null,
    checked = null,
    dataPlan = null,
    planSequence = 0,
    planPolls = 0,
    planTimer = null,
    submittedTaskId = null,
    submittedTaskPolls = 0,
    submittedTaskTimer = null;
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
    if (submittedTaskId) q.set("submitted", submittedTaskId);
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
      if (submittedTaskId) await loadSubmittedTask(submittedTaskId, own);
    } catch (e) {
      if (own === sequence) {
        $("center-list").textContent = "读取失败，请重新筛选或刷新。";
        fail(e);
      }
    }
  }
  function renderSubmittedTask(task) {
    const status = task?.status || "unknown";
    const detail = new URLSearchParams({ source: "task", record: task?.task_id || submittedTaskId });
    const complete = ["succeeded", "partial", "failed", "cancelled"].includes(status);
    const message = status === "failed"
      ? task.error || "任务失败，请打开详情查看原因。"
      : ["succeeded", "partial"].includes(status)
        ? "回测已完成，可以查看详情。"
        : "已提交，正在等待执行或运行中。";
    return `<article class="submitted-task ${esc(status)}" tabindex="-1"><p class="preflight-kicker">刚提交的日线信号策略</p><h3>${esc(labels[status] || enumName(status))}</h3><p><code>${esc(task?.task_id || submittedTaskId)}</code></p><p>${esc(message)}</p><p>进度：${esc(known(task?.progress))}%${task?.message ? ` · ${esc(task.message)}` : ""}</p>${complete ? `<a href="/backtests/detail?${esc(detail.toString())}">查看详情</a>` : ""}</article>`;
  }
  async function loadSubmittedTask(taskId, own = sequence) {
    if (!taskId || !$(`center-submitted-task`)) return;
    try {
      const task = await get(`/api/v1/daily-signal-strategies/${encodeURIComponent(taskId)}`);
      if (own !== sequence || submittedTaskId !== taskId) return;
      $("center-submitted-task").innerHTML = renderSubmittedTask(task);
      const done = ["succeeded", "partial", "failed", "cancelled"].includes(task.status);
      if (!done && submittedTaskPolls < 60 && typeof setTimeout === "function") {
        submittedTaskPolls++;
        submittedTaskTimer = setTimeout(() => loadSubmittedTask(taskId, own), 2000);
      } else if (!done && submittedTaskPolls >= 60) {
        $("center-submitted-task").insertAdjacentHTML("beforeend", '<p class="page-notice">已停止自动刷新以避免无上限轮询；任务仍在后台运行，请稍后点击“筛选 / 刷新”。</p>');
      }
    } catch (error) {
      if (own === sequence && submittedTaskId === taskId)
        $("center-submitted-task").innerHTML = `<p class="negative">刚提交任务暂时无法读取：${esc(error.message)}</p>`;
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
      `${row.kind === "daily_signal" ? `<article class="strategy-summary"><h3>原运行策略中文摘要</h3><p>${esc(dailySignalSummary(row.spec))}</p></article>` : ""}<details><summary>原运行参数（中文展示，内部代码保留）</summary>${renderValue(row.spec)}</details>`;
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
        .filter(([k]) => data[k] != null && !(row.kind === "daily_signal" && ["signals", "orders", "positions", "series"].includes(k)))
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
      if (row.kind === "daily_signal") {
        const timeline = dailySignalTimeline(data.signals || [], data.orders || []);
        const view = dailyResultView(data, row.spec || data.spec || {});
        const reasons = [...new Set((data.orders || []).map((item) => item.reason).filter(Boolean))].sort();
        const symbols = [...new Set(timeline.map((item) => item.order?.symbol || item.signal?.symbol).filter(Boolean))].sort();
        const chartSymbols = [...new Set((data.spec?.symbols || row.spec?.symbols || symbols).map(String))].sort();
        const executionDates = [...new Set(view.executed.map((item) => item.execution_date).filter(Boolean))].sort();
        const custom = `<section class="daily-result-layout"><article class="daily-nav-panel"><div id="center-nav-chart">${renderNavChart(view.series, 0, typeof window !== "undefined" && window.innerWidth <= 480 ? 3 : 6)}</div><label class="nav-slider-label">选择交易日 <input id="center-chart-position" type="range" min="0" max="${Math.max(0, view.series.length - 1)}" value="0" step="1" aria-label="选择净值图交易日"></label></article>${renderAccountOverview(view)}${renderAuditSummary(data.diagnostics?.account_audit || {}, data.account_audit || [])}${renderReproducibility(data.reproducibility || {})}<section class="price-signal-panel"><div class="price-controls"><h3>价格与信号</h3><label>选择股票<select id="center-price-symbol">${chartSymbols.map((value) => `<option value="${esc(value)}">${esc(value)}</option>`).join("")}</select></label></div><div id="center-price-chart">${chartSymbols.length ? "正在读取有界价格指标数据…" : "该历史运行没有可选的冻结股票记录。"}</div><label class="nav-slider-label" id="center-price-slider-label" hidden>选择日期 <input id="center-price-position" type="range" min="0" max="0" value="0" step="1" aria-label="选择价格信号图日期"></label></section><section class="daily-executions"><h3>实际成交明细 ${termHelp("execution")}</h3><form id="center-execution-filters" class="center-filters"><label>股票<select id="center-execution-symbol"><option value="">全部</option>${symbols.map((value) => `<option value="${esc(value)}">${esc(value)}</option>`).join("")}</select></label><label>买入/卖出<select id="center-execution-side"><option value="">全部</option><option value="buy">买入</option><option value="sell">卖出</option></select></label><label>成交日期<select id="center-execution-date"><option value="">全部</option>${executionDates.map((value) => `<option value="${esc(value)}">${esc(value)}</option>`).join("")}</select></label></form><div id="center-execution-list">${renderExecutedTrades(view.executed)}</div></section><section class="daily-final-positions"><h3>回测结束日持仓</h3>${renderFinalPositions(view.finalPositions)}</section><section class="daily-unfilled"><h3>未成交与等待退出 ${termHelp("unfilled")} ${termHelp("t_plus_one")}</h3>${renderUnfilledOrders(view.unfilled)}</section><section class="daily-timeline"><h3>信号 → 指令 → 成交或未成交</h3><form id="center-timeline-filters" class="center-filters"><label>股票<select id="center-timeline-symbol"><option value="">全部</option>${symbols.map((value) => `<option value="${esc(value)}">${esc(value)}</option>`).join("")}</select></label><label>动作<select id="center-timeline-side"><option value="">全部</option><option value="buy">买入</option><option value="sell">卖出</option></select></label><label>结果<select id="center-timeline-status"><option value="">全部</option><option value="executed">已成交</option><option value="unfilled">未成交/失效</option><option value="retry">等待重试</option></select></label><label>实际成交日<input id="center-timeline-date" type="date"></label><label>未成交原因<select id="center-timeline-reason"><option value="">全部</option>${reasons.map((value) => `<option value="${esc(value)}">${esc(enumName(value))}</option>`).join("")}</select></label></form><div id="center-timeline-list">${renderDailyTimeline(timeline)}</div></section><details class="daily-account-table"><summary>每日账户变化</summary>${renderDailyAccounts(view.series)}</details><details><summary>每日账户核对（有界预览）</summary>${renderValue(data.account_audit || [])}</details><details><summary>原始信号、候选和完整诊断</summary>${renderValue({ signals: data.signals, candidates: data.candidates, diagnostics: data.diagnostics, reproducibility: data.reproducibility })}</details></section>`;
        $("center-result").insertAdjacentHTML("afterbegin", custom);
        const redrawChart = () => {
          const slider = $("center-chart-position");
          $("center-nav-chart").innerHTML = renderNavChart(view.series, Number(slider.value), typeof window !== "undefined" && window.innerWidth <= 480 ? 3 : window.innerWidth <= 800 ? 5 : 7);
          wireTermHelp($("center-nav-chart"));
        };
        $("center-chart-position").addEventListener("input", redrawChart);
        if (typeof window.addEventListener === "function") window.addEventListener("resize", redrawChart, { passive: true });
        let priceRequest = 0;
        let pricePayload = null;
        const redrawPriceChart = () => {
          if (!pricePayload) return;
          $("center-price-chart").innerHTML = renderPriceSignalChart(pricePayload, Number($("center-price-position").value), typeof window !== "undefined" && window.innerWidth <= 480 ? 3 : 6);
        };
        const loadPriceChart = async () => {
          const symbol = $("center-price-symbol")?.value;
          if (!symbol) return;
          const request = ++priceRequest;
          $("center-price-chart").textContent = "正在读取该股票的有界价格指标与事件…";
          $("center-price-slider-label").hidden = true;
          try {
            const payload = await get(`/api/v1/daily-signal-runs/${encodeURIComponent(row.run_id)}/price-signals?symbol=${encodeURIComponent(symbol)}&limit=1200`);
            if (own !== sequence || request !== priceRequest || $("center-price-symbol")?.value !== symbol) return;
            pricePayload = payload;
            $("center-price-position").max = String(Math.max(0, payload.series.length - 1));
            $("center-price-position").value = "0";
            $("center-price-slider-label").hidden = payload.series.length < 2;
            redrawPriceChart();
          } catch (error) {
            if (own === sequence && request === priceRequest) $("center-price-chart").innerHTML = `<p class="empty-state">${esc(error.message)}。旧运行可能没有价格指标产物；不会从当前行情倒推。</p>`;
          }
        };
        $("center-price-symbol")?.addEventListener("change", loadPriceChart);
        $("center-price-position")?.addEventListener("input", redrawPriceChart);
        if (typeof window.addEventListener === "function") window.addEventListener("resize", redrawPriceChart, { passive: true });
        if (chartSymbols.length) loadPriceChart();
        const updateExecutions = () => {
          $("center-execution-list").innerHTML = renderExecutedTrades(view.executed, {
            symbol: $("center-execution-symbol").value,
            side: $("center-execution-side").value,
            execution_date: $("center-execution-date").value,
          });
        };
        $("center-execution-filters").addEventListener("change", updateExecutions);
        const updateTimeline = () => {
          $("center-timeline-list").innerHTML = renderDailyTimeline(timeline, {
            symbol: $("center-timeline-symbol").value,
            side: $("center-timeline-side").value,
            status: $("center-timeline-status").value,
            reason: $("center-timeline-reason").value,
            execution_date: $("center-timeline-date").value,
          });
        };
        $("center-timeline-filters").addEventListener("change", updateTimeline);
        wireTermHelp($("center-result"));
      }
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
  const readinessKinds = new Set(["factor_backtest", "factor_lab", "daily_signal", "screen"]);
  function refreshDailyUX() {
    if (!copy || copy.kind !== "daily_signal" || !$(`center-daily-summary-text`)) return;
    try {
      const params = readParams($("center-copy-form"), copy.params);
      const scopeSize = params.stock_pool_id ? copy.scope?.length : params.symbols?.length;
      $("center-daily-summary-text").textContent = dailySignalSummary(params);
      const errors = validateDailySignal(params, scopeSize);
      $("center-daily-errors").innerHTML = errors.length
        ? `<div class="field-errors"><strong>请先修正 ${errors.length} 个配置问题：</strong><ul>${errors.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>`
        : '<p class="positive">字段格式检查通过；仍需执行后端本地数据预检。</p>';
      const rows = $("center-copy-form").querySelectorAll(".signal-rule-row");
      for (const row of rows) {
        const number = (selector) => {
          const value = row.querySelector(selector).value;
          return value === "" ? null : Number(value);
        };
        row.querySelector(".rule-explanation").textContent = ruleText({
          kind: row.querySelector("[data-rule-kind]").value,
          operator: row.querySelector("[data-rule-operator]").value,
          window: number("[data-rule-window]"),
          short_window: number("[data-rule-short]"),
          long_window: number("[data-rule-long]"),
          threshold: number("[data-rule-threshold]"),
        });
      }
    } catch (error) {
      $("center-daily-errors").textContent = "配置暂时无法读取：" + error.message;
    }
  }
  function invalidate() {
    checked = null;
    dataPlan = null;
    planSequence++;
    planPolls = 0;
    if (planTimer && typeof clearTimeout === "function") clearTimeout(planTimer);
    if ($("center-review")) $("center-review").hidden = true;
    if ($("center-submit"))
      $("center-submit").disabled =
        busy || !copy || readinessKinds.has(copy.kind);
    if ($("center-preflight-result"))
      $("center-preflight-result").textContent = "参数有变化，请重新预检。";
    if ($("center-data-plan")) $("center-data-plan").hidden = true;
    if ($("center-plan-status")) $("center-plan-status").textContent = "";
    refreshDailyUX();
    if ($("center-copy-form")) wireTermHelp($("center-copy-form"));
  }
  async function configure(query, freshKind = null) {
    if (typeof query === "string") query = new URLSearchParams(query);
    const own = ++sequence;
    copy = null;
    checked = null;
    let data,
      restoredPlan = null;
    try {
      data = await get(
        freshKind
          ? "/api/v1/run-center/configuration?kind=" + freshKind
          : "/api/v1/run-center/copy?" + query,
      );
      if (freshKind && query.has("data_plan")) {
        restoredPlan = await get(
          `/api/v1/research-data/plans/${encodeURIComponent(query.get("data_plan"))}`,
        );
        data = restoreDataPlanConfiguration(data, restoredPlan, freshKind);
      }
    } catch (e) {
      if (own === sequence)
        fail(
          query.has("data_plan")
            ? Error("补齐计划恢复失败：" + e.message)
            : e,
        );
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
      "center-candidate-fields",
      "center-buy-fields",
      "center-sell-fields",
      "center-advanced-fields",
      "center-cost-fields",
    ])
      $(id).innerHTML = "";
    for (const [key, value] of Object.entries(data.params)) {
      if (value === null) continue;
      const html = field(key, value, data.schema.properties[key] || {});
      let destination = range.has(key)
        ? "center-scope-fields"
        : costs.has(key) || (data.kind === "daily_signal" && key === "max_positions")
          ? "center-cost-fields"
          : common.has(key)
            ? "center-common-fields"
            : "center-advanced-fields";
      if (data.kind === "daily_signal") {
        if (["candidate_factors", "candidate_count", "candidate_refresh"].includes(key)) destination = "center-candidate-fields";
        if (["buy_rules", "buy_rule_mode"].includes(key)) destination = "center-buy-fields";
        if (["sell_rules", "sell_rule_mode", "exit_on_candidate_drop"].includes(key)) destination = "center-sell-fields";
      }
      $(destination).insertAdjacentHTML("beforeend", html);
    }
    const isDaily = data.kind === "daily_signal";
    $("center-generic-method").hidden = isDaily;
    for (const id of ["center-daily-templates", "center-daily-candidates", "center-daily-buy", "center-daily-sell", "center-daily-summary"])
      $(id).hidden = !isDaily;
    if (isDaily) {
      const assumptions = catalog.daily_signal_execution || {};
      $("center-execution-assumptions").innerHTML = Object.values(assumptions)
        .map((item) => `<li>${esc(item)}</li>`).join("");
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
    for (const button of $("center-copy-form").querySelectorAll("[data-daily-template]"))
      button.addEventListener("click", () => {
        const template = dailyTemplates[button.dataset.dailyTemplate];
        if (!template || copy.kind !== "daily_signal") return;
        for (const side of ["buy", "sell"])
          $("center-" + side + "-rule-rows").innerHTML = template[side + "_rules"]
            .map((item) => signalRuleRow(item, side)).join("");
        $("copy-buy_rule_mode").value = template.buy_rule_mode;
        $("copy-sell_rule_mode").value = template.sell_rule_mode;
        invalidate();
        $("center-copy-warning").textContent = "已套用配置示例；冻结股票范围和 revision 未改变。模板不会预检、下载或执行。";
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
    wireTermHelp($("center-copy-form"));
    if (freshKind) await loadPools(own);
    if (restoredPlan && copy.kind === "daily_signal") {
      const planOwn = planSequence;
      try {
        const plan = restoredPlan;
        if (planOwn === planSequence) {
          planPolls = 0;
          showDataPlan(plan);
          if (plan.task?.status === "succeeded") await recheckCompletedPlan(plan, planOwn);
          else schedulePlanRefresh(planOwn);
        }
      } catch (error) {
        if (planOwn === planSequence) fail(Error("补齐计划恢复失败：" + error.message));
      }
    }
  }
  async function loadPools(own) {
    try {
      const pools = await get("/api/v1/stock-pools?include_archived=false");
      if (own !== sequence) return;
      const items = Array.isArray(pools) ? pools : pools.items;
      const frozenValue = copy.params.stock_pool_id
        ? `${copy.params.stock_pool_id}:${copy.params.stock_pool_revision}`
        : null;
      const frozenListed = items.some(
        (item) => `${item.pool_id}:${item.current_revision}` === frozenValue,
      );
      $("center-pool-selector").innerHTML =
        `<label>范围来源<select id="center-select-pool"><option value="manual">手动股票代码（不冒充历史指数成分）</option>${frozenValue && !frozenListed ? `<option value="${esc(frozenValue)}">补齐计划冻结股票池 · r${esc(copy.params.stock_pool_revision)}</option>` : ""}${items.map((p) => `<option value="${esc(p.pool_id)}:${p.current_revision}">${esc(p.name)} · r${p.current_revision}</option>`).join("")}</select></label><p id="center-pool-preview"></p>`;
      if (frozenValue) {
        $("center-select-pool").value = frozenValue;
        const manualInput = $("center-copy-form").querySelector?.('[data-param="symbols"]');
        if (manualInput) manualInput.disabled = true;
        $("center-pool-preview").textContent =
          `已恢复计划冻结范围 ${copy.params.stock_pool_id} · r${copy.params.stock_pool_revision} · ${copy.scope.length} 只：${copy.scope.join(" ")}`;
      } else if (copy.scope?.length) {
        $("center-pool-preview").textContent =
          `已恢复计划明确范围 · ${copy.scope.length} 只：${copy.scope.join(" ")}`;
      }
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
    if (copy.kind === "daily_signal") {
      const errors = validateDailySignal(
        params,
        params.stock_pool_id ? copy.scope?.length : params.symbols?.length,
      );
      if (errors.length) throw Error(errors[0]);
    }
    return params;
  }
  const paramsKey = (value) => JSON.stringify(normalizedParameterIdentity(value));
  function setPlanUrl(planId) {
    const query = new URLSearchParams(location.search);
    if (planId) query.set("data_plan", planId);
    else query.delete("data_plan");
    window.history.replaceState(null, "", location.pathname + (query.size ? "?" + query : ""));
  }
  function showDataPlan(plan) {
    dataPlan = plan;
    $("center-data-plan").hidden = false;
    $("center-plan-status").innerHTML = renderDataPlan(plan);
    const taskStatus = plan.task?.status;
    $("center-plan-create").hidden = true;
    $("center-plan-confirm").hidden = !(plan.status === "draft" && plan.items?.length);
    $("center-plan-retry").hidden = taskStatus !== "failed" || dataPlanExecutionSummary(plan).retryable < 1;
    $("center-plan-refresh").hidden = !plan.plan_id;
    setPlanUrl(plan.plan_id);
  }
  async function recheckCompletedPlan(plan, own) {
    const report = await get(`/api/v1/research-data/plans/${encodeURIComponent(plan.plan_id)}/preflight`, { method: "POST" });
    if (own !== planSequence || dataPlan?.plan_id !== plan.plan_id) return;
    const current = currentParams();
    const frozen = plan.input?.parameters;
    if (paramsKey(current) !== paramsKey(frozen)) {
      $("center-plan-status").insertAdjacentHTML("afterbegin", '<p class="negative">计划已完成，但当前参数已经变化。旧结果不会覆盖当前页面，请重新检查本地数据。</p>');
      return;
    }
    checked = { params: paramsKey(current), report };
    $("center-preflight-result").innerHTML = renderPreflight(report);
    $("center-warmup-summary").hidden = !report.technical_warmup_explanation;
    $("center-warmup-summary").textContent = report.technical_warmup_explanation || "";
    $("center-submit").disabled = !(report.ready === true || report.status === "ready");
    $("center-plan-status").insertAdjacentHTML("afterbegin", '<p class="positive">补齐任务完成，已自动重新预检当前冻结参数；不会自动运行策略。</p>');
  }
  function schedulePlanRefresh(own) {
    if (planTimer && typeof clearTimeout === "function") clearTimeout(planTimer);
    if (!["queued", "running"].includes(dataPlan?.task?.status)) return;
    if (planPolls >= 120) {
      $("center-plan-status").insertAdjacentHTML("afterbegin", '<p class="page-notice">已停止自动刷新以避免无上限轮询。任务仍在后台运行，可稍后手动刷新或通过计划 ID 恢复。</p>');
      return;
    }
    planPolls++;
    if (typeof setTimeout === "function")
      planTimer = setTimeout(() => refreshPlan(own), 2000);
  }
  async function refreshPlan(own = planSequence) {
    const planId = dataPlan?.plan_id;
    if (!planId) return;
    try {
      const updated = await get(`/api/v1/research-data/plans/${encodeURIComponent(planId)}`);
      if (own !== planSequence || dataPlan?.plan_id !== planId) return;
      const previous = dataPlan?.task?.status;
      showDataPlan(updated);
      if (updated.task?.status === "succeeded" && previous !== "succeeded")
        await recheckCompletedPlan(updated, own);
      else schedulePlanRefresh(own);
    } catch (error) {
      if (own === planSequence) fail(error);
    }
  }
  async function planAction(action) {
    if (busy || !copy || copy.kind !== "daily_signal") return;
    const own = planSequence;
    busy = true;
    try {
      if (action === "create") {
        const params = currentParams();
        if (!checked || checked.params !== paramsKey(params))
          throw Error("请先检查当前参数的本地数据；参数变化后旧预检不能创建补齐计划。");
        const plan = await get("/api/v1/research-data/plans", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ research_type: copy.kind, parameters: params }),
        });
        if (own !== planSequence) return;
        planPolls = 0;
        showDataPlan(plan);
        return;
      }
      if (!dataPlan) throw Error("请先查看当前参数的补齐计划。");
      if (action === "refresh") return await refreshPlan(own);
      const retry = action === "retry";
      const verb = retry ? "重试失败分区" : "补齐缺失数据";
      if (retry && dataPlanExecutionSummary(dataPlan).retryable < 1)
        throw Error("当前计划没有真实失败或未处理分区。请重新检查本地数据；如需刷新上游数据，请基于当前参数创建新计划。");
      const detail = retry ? `仅重试 ${dataPlanExecutionSummary(dataPlan).retryable} 个真实失败或未处理项；已发布和已验证项目不会重新下载。` : "";
      if (!window.confirm(`确认${verb}？这会访问公网并启动后台任务，但不会运行策略或调用模型。${detail}`)) return;
      await get(`/api/v1/research-data/plans/${encodeURIComponent(dataPlan.plan_id)}/${retry ? "retry" : "confirm"}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version: dataPlan.version, input_sha256: dataPlan.input_sha256, confirm: true }),
      });
      if (own !== planSequence) return;
      planPolls = 0;
      await refreshPlan(own);
    } catch (error) {
      if (own === planSequence) fail(error);
    } finally {
      busy = false;
    }
  }
  async function preflight() {
    if (busy || !copy) return;
    checked = null;
    const own = ++planSequence;
    dataPlan = null;
    planPolls = 0;
    if (planTimer && typeof clearTimeout === "function") clearTimeout(planTimer);
    if ($("center-data-plan")) $("center-data-plan").hidden = true;
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
      if (own !== planSequence || paramsKey(params) !== paramsKey(currentParams())) return;
      $("center-preflight-result").innerHTML = renderPreflight(p);
      checked = { params: paramsKey(params), report: p };
      $("center-warmup-summary").hidden = !p.technical_warmup_explanation;
      $("center-warmup-summary").textContent = p.technical_warmup_explanation || "";
      $("center-submit").disabled = !(p.ready === true || p.status === "ready");
      if (copy.kind === "daily_signal" && p.complete !== true) {
        $("center-data-plan").hidden = false;
        $("center-plan-create").hidden = false;
        $("center-plan-confirm").hidden = true;
        $("center-plan-retry").hidden = true;
        $("center-plan-refresh").hidden = true;
        $("center-plan-status").innerHTML = '<p>当前预检存在缺口。点击“查看补齐计划”只会冻结计划，不联网下载。</p>';
      }
    } catch (e) {
      if (own === planSequence) fail(e);
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
          checked.params !== paramsKey(params) ||
          !(checked.report.ready === true || checked.report.status === "ready"))
      )
        throw Error("请先通过当前参数的数据预检");
      if (!execute && $("center-review")?.querySelector) {
        $("center-review-content").innerHTML =
          `<p>${esc(copy.label)} · ${copy.source_run_id ? "来源 " + esc(copy.source_run_id) : "新建配置"}</p><p>${params.stock_pool_id ? `冻结股票池 ${esc(params.stock_pool_id)} · revision ${esc(params.stock_pool_revision)} · ${copy.scope.length} 只` : `明确股票范围 ${esc((params.symbols || [params.symbol]).join(" "))}`}</p>${copy.kind === "daily_signal" ? `<h4>策略摘要</h4><p>${esc(dailySignalSummary(params))}</p>` : ""}${renderValue(params)}<p>成本单位 bp；相对因子权重不是资金百分比。${copy.kind === "daily_signal" ? "信号收盘确认、最早下一交易日开盘尝试；买单当日失效，受限卖出继续重试，期末不强平。" : "执行假设沿用原模型。"}</p>`;
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
      if (copy.kind === "daily_signal" && p.task_id) {
        const target = new URLSearchParams({ kind: "daily_signal", submitted: p.task_id });
        location.assign("/backtests/history?" + target.toString());
        return;
      }
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
    submittedTaskId = query.get("submitted");
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
      $("center-plan-create").addEventListener("click", () => planAction("create"));
      $("center-plan-confirm").addEventListener("click", () => planAction("confirm"));
      $("center-plan-retry").addEventListener("click", () => planAction("retry"));
      $("center-plan-refresh").addEventListener("click", () => planAction("refresh"));
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
      if (submittedTaskId) submittedTaskPolls = 0;
      for (const k of ["kind", "status"])
        $("center-" + k).value = query.get(k) || "";
      if (submittedTaskId) $("center-kind").value = "daily_signal";
      page = Math.max(1, Math.min(10000, Number(query.get("page")) || 1));
      $("center-filters").addEventListener("submit", (e) => {
        e.preventDefault();
        if ($("center-kind").value !== "daily_signal") {
          submittedTaskId = null;
          if (submittedTaskTimer && typeof clearTimeout === "function") clearTimeout(submittedTaskTimer);
          $("center-submitted-task").innerHTML = "";
        }
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
    termHelp,
  };
})();
