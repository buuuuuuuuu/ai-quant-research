"""Display labels only; internal research codes and data remain unchanged."""

from __future__ import annotations

import math

from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS

ENUM_LABELS = {
    "asc": "低值优先",
    "desc": "高值优先",
    "monthly": "按月",
    "quarterly": "按季度",
    "daily": "每日",
    "all": "全部满足",
    "any": "任意满足",
    "above": "高于",
    "below": "低于",
    "cross_above": "上穿",
    "cross_below": "下穿",
    "close_vs_ma": "收盘价与均线",
    "ma_compare": "双均线状态",
    "ma_cross": "双均线交叉",
    "return_threshold": "区间涨跌幅阈值",
    "buy": "买入",
    "sell": "卖出",
    "generated": "已生成",
    "executed": "已模拟成交",
    "blocked": "未成交",
    "end_of_backtest": "回测结束未执行",
    "expired": "已失效",
    "held": "继续持有",
    "closed": "已退出",
    "retry_next_trading_day": "下一交易日继续尝试退出",
    "pending": "等待处理",
    "draft": "待确认",
    "confirmed": "已确认",
    "planned": "已计划",
    "fetching": "同步中",
    "published": "同步完成",
    "verified": "已验证",
    "warning": "有提示",
    "rejected": "已拒绝",
    "queued": "等待执行",
    "running": "执行中",
    "succeeded": "已完成",
    "failed": "失败",
    "passed": "核对通过",
    "cancelled": "已取消",
    "missing_raw_open": "缺少未复权开盘数据",
    "missing_raw_open_or_previous_close": "缺少未复权开盘价或前收盘价",
    "opening_at_upper_price_limit": "开盘达到涨停限制",
    "opening_at_lower_price_limit": "开盘达到跌停限制",
    "t_plus_one": "T+1 限制",
    "already_held": "已持有，不重复买入",
    "sold_same_execution_batch": "同批次已卖出，不立即买回",
    "max_positions": "已达到持仓上限",
    "insufficient_cash_or_round_lot": "资金不足或不足一手",
    "reduced_for_opening_gap_and_cash": "开盘跳空后按可用资金缩量",
    "candidate_drop": "退出候选名单",
    "training": "训练集",
    "train": "训练集",
    "validation": "验证集",
    "test": "样本外测试集",
    "explicit": "显式股票范围",
    "all_available": "全部可用股票",
    "historical_index": "可信历史指数成分",
    "observed_index": "首次观察指数快照",
    "within_industry_rank": "行业内排名",
    "none": "不使用",
    "qfq": "前复权",
    "hfq": "后复权",
    "exclude": "排除缺失样本",
    "available": "使用有效因子并重新归一权重",
    "show": "保留缺失诊断",
    "daily_bar": "股票日线",
    "benchmark_daily_bar": "基准日线",
    "trading_calendar": "交易日历",
    "financial_metric": "财务因子",
    "announcement": "定期公告元数据",
}
FIELD_LABELS = {
    "factor": "因子",
    "code": "因子",
    "weight": "相对权重",
    "direction": "排序方向",
    "strategy": "策略参数",
    "selected_strategy": "验证集选定策略",
    "periods": "有效期数",
    "mean_ic": "IC 均值",
    "mean_rank_ic": "Rank IC 均值",
    "ic_ir": "IC 信息比率",
    "positive_ic_ratio": "正向 IC 占比",
    "factor_count": "因子数",
    "cross_section_count": "因子截面数",
    "strategy_count": "实验策略数",
    "mean_selection_turnover": "平均选择换手比例",
    "total_return": "累计收益",
    "benchmark_total_return": "基准收益",
    "excess_return": "超额收益",
    "annualized_return": "年化收益",
    "annualized_volatility": "年化波动率",
    "max_drawdown": "最大回撤",
    "sharpe_ratio": "夏普比率",
    "total_transaction_cost": "交易总成本",
    "trade_count": "交易数",
    "segment": "样本分段",
    "start_date": "开始日期",
    "end_date": "结束日期",
    "selected_by_validation": "验证集入选",
    "top_n": "持仓数量",
    "rebalance": "调仓周期",
    "commission_bps": "佣金（bp）",
    "year": "年份",
    "fold": "滚动轮次",
    "train_end": "训练结束",
    "test_start": "测试开始",
    "test_end": "测试结束",
    "test_return": "测试收益",
    "signal_date": "信号日期",
    "group": "因子分组",
    "mean_forward_return": "平均远期收益",
    "count": "样本数",
    "candidate_count": "候选数量",
    "selected_count": "入选数量",
    "stock_count": "股票样本数量",
    "group_count": "分组数量",
    "sample_size": "样本数量",
    "ic": "IC",
    "rank_ic": "Rank IC",
    "coverage": "覆盖率",
    "covered_count": "已覆盖股票数",
    "eligible_count": "符合时点条件股票数",
    "mean_excess_return": "平均窗口超额收益",
    "median_excess_return": "窗口超额收益中位数",
    "mean_stock_net_return": "平均股票窗口净收益",
    "mean_benchmark_return": "平均基准窗口收益",
    "positive_excess_ratio": "窗口超额收益为正的比例",
    "t_stat": "t 统计量",
    "event_year": "事件年份",
    "benchmark_return": "基准收益",
    "train_start": "训练开始",
    "train_return": "训练收益",
    "win_rate": "窗口超额收益胜率",
    "std_excess_return": "窗口超额收益标准差",
    "ci95_lower": "95% 区间下界",
    "ci95_upper": "95% 区间上界",
    "sample_count": "样本数",
    "event_count": "事件数",
    "window": "事件窗口",
    "mean_return": "平均窗口收益",
    "median_return": "窗口收益中位数",
    "status": "状态",
    "run_id": "运行 ID",
    "symbol": "股票代码",
    "date": "日期",
    "candidate_factors": "候选排序因子",
    "candidate_refresh": "候选更新频率",
    "buy_rules": "买入信号",
    "sell_rules": "卖出信号",
    "signal_count": "信号数量",
    "order_attempt_count": "订单尝试数量",
    "executed_order_count": "成交订单数量",
    "open_position_count": "期末持仓数量",
    "pending_exit_count": "期末待退出数量",
    "action": "动作",
    "side": "方向",
    "planned_execution_date": "计划执行日",
    "execution_date": "实际模拟成交日",
    "signal_price": "信号收盘价",
    "execution_price": "开盘参考成交价",
    "shares": "数量（股）",
    "reason": "未成交或调整原因",
    "next_action": "后续处理",
    "candidate_score": "候选得分",
    "candidate_rank": "候选排名",
    "candidate_eligible": "具备候选资格",
    "rule_evidence": "规则与触发依据",
    "portfolio_nav": "策略累计净值",
    "benchmark_nav": "基准累计净值",
    "cash": "现金",
    "position_value": "回测结束日持仓市值",
    "portfolio_value": "账户总资产",
    "total_cost": "累计交易成本",
    "final_value": "回测结束总资产",
    "notional": "成交金额",
    "cost": "本次交易成本",
    "signal_id": "关联信号 ID",
    "planned_shares": "计划数量（股）",
    "cash_before": "成交前现金",
    "cash_after": "成交后现金",
    "position_shares_before": "成交前持仓（股）",
    "position_shares_after": "成交后持仓（股）",
    "commission_cost": "佣金金额",
    "slippage_cost": "滑点成本",
    "stamp_duty_cost": "卖出印花税",
    "retry_continues": "是否继续重试",
    "parameter_sha256": "参数 SHA-256",
    "frozen_symbols_sha256": "冻结股票范围 SHA-256",
    "technical_warmup_start": "技术指标预热起点",
    "input_read_at": "输入读取时间",
    "git_commit_sha": "Git 提交 SHA",
    "schema_version": "Schema 版本",
    "price": "回测结束日估值价格",
    "market_value": "回测结束日持仓市值",
    "pending_exit": "等待退出",
}
TERM_CATALOG = {
    "nav": {
        "label": "累计净值",
        "short": "把初始资金看作 1.00，当前数值表示资金相对初始值的变化。",
        "professional": "累计净值（NAV）",
        "calculation": "账户总资产 ÷ 初始资金。",
        "example": "1.08 表示相对初始资金累计增长约 8%。",
        "limitations": "不是股票价格，也不代表可保证实现的收益。",
        "code": "portfolio_nav",
    },
    "benchmark": {
        "label": "基准",
        "short": "用于比较策略表现的参考指数。",
        "professional": "业绩比较基准",
        "calculation": "沿用运行参数中保存的基准指数净值序列。",
        "example": "策略净值 1.08、基准净值 1.05，两者差值为 0.03。",
        "limitations": "基准不是可直接成交的投资组合。",
        "code": "benchmark_nav",
    },
    "max_drawdown": {
        "label": "最大回撤",
        "short": "回测期间从历史高点到随后低点的最大跌幅。",
        "professional": "最大回撤（Maximum Drawdown）",
        "calculation": "沿用引擎对累计净值历史峰值的原计算口径。",
        "example": "10% 表示曾从此前高点最多回落约 10%。",
        "limitations": "只描述这段历史区间，不预测未来风险。",
        "code": "max_drawdown",
    },
    "annualized_return": {
        "label": "年化收益",
        "short": "把研究区间收益按一年长度换算后的数值。",
        "professional": "年化收益率",
        "calculation": "沿用引擎按 252 个交易日折算的原口径。",
        "example": "用于比较不同区间，不等于每年都能获得该收益。",
        "limitations": "短区间折算可能放大偶然波动。",
        "code": "annualized_return",
    },
    "annualized_volatility": {
        "label": "年化波动",
        "short": "净值日收益上下波动的年化程度。",
        "professional": "年化波动率",
        "calculation": "日收益标准差按 252 个交易日年化。",
        "example": "数值越高通常表示净值起伏越大。",
        "limitations": "不能覆盖流动性和极端事件等全部风险。",
        "code": "annualized_volatility",
    },
    "sharpe_ratio": {
        "label": "夏普比率",
        "short": "用收益相对波动衡量风险调整后的表现。",
        "professional": "夏普比率（Sharpe Ratio）",
        "calculation": "沿用引擎的年化收益 ÷ 年化波动率口径。",
        "example": "仅适合在相同口径和相近区间下辅助比较。",
        "limitations": "当前口径未另扣无风险利率，零波动时由引擎按原规则处理。",
        "code": "sharpe_ratio",
    },
    "slippage": {
        "label": "滑点",
        "short": "模拟成交相对开盘参考价可能产生的不利成本。",
        "professional": "滑点（Slippage）",
        "calculation": "当前系统把滑点计入交易成本，不另造一条滑点后成交价。",
        "example": "5 bp 表示按成交金额计入 0.05% 的滑点成本。",
        "limitations": "日线回测不能还原真实集合竞价成交。",
        "code": "slippage_bps",
    },
    "bp": {
        "label": "bp",
        "short": "费用比例单位，1 bp 等于 0.01%。",
        "professional": "基点（Basis Point）",
        "calculation": "100 bp = 1%。",
        "example": "5 bp = 0.05%。",
        "limitations": "字段旁的 bp 是费率，不是金额。",
        "code": "bps",
    },
    "qfq": {
        "label": "前复权",
        "short": "把历史价格按后续除权除息影响调整，用于可比的技术指标。",
        "professional": "前复权（qfq）",
        "calculation": "沿用本地数据源保存的前复权序列。",
        "example": "MA 和涨跌幅使用所选复权口径计算。",
        "limitations": "实际模拟成交和估值仍使用未复权价格。",
        "code": "qfq",
    },
    "signal": {
        "label": "信号",
        "short": "收盘后规则满足时形成的买入或卖出判断。",
        "professional": "交易信号",
        "calculation": "只使用信号日收盘后已经完整可用的数据。",
        "example": "MA5 上穿 MA20 后生成买入信号。",
        "limitations": "有信号不代表一定会成交。",
        "code": "signal",
    },
    "order": {
        "label": "指令",
        "short": "由信号产生、计划在后续交易日尝试执行的模拟订单。",
        "professional": "模拟交易指令",
        "calculation": "买单只在计划日尝试；受限卖出保留退出意图。",
        "example": "周一收盘信号可形成周二开盘执行指令。",
        "limitations": "指令可能因为资金、整手或交易限制而未成交。",
        "code": "order",
    },
    "execution": {
        "label": "成交",
        "short": "指令通过约束后记入模拟账户的交易。",
        "professional": "模拟成交",
        "calculation": "以未复权开盘参考价记账，费用和滑点按原引擎口径扣除。",
        "example": "成交数量、金额和本次总成本来自已保存订单。",
        "limitations": "不表示真实市场中保证成交。",
        "code": "executed",
    },
    "unfilled": {
        "label": "未成交",
        "short": "指令已经尝试，但因约束未进入模拟账户。",
        "professional": "受阻或失效指令",
        "calculation": "原因和后续处理直接读取已保存订单。",
        "example": "资金不足、不足一手、T+1 或价格限制。",
        "limitations": "未成交不是成交数量为零的真实交易。",
        "code": "blocked",
    },
    "position_value": {
        "label": "持仓市值",
        "short": "回测日期持有股票按当日已保存估值价格计算的合计价值。",
        "professional": "持仓市值",
        "calculation": "来自 daily_nav.position_value 或 positions.market_value。",
        "example": "页面所称期末市值是回测结束日数值，不是实时行情。",
        "limitations": "缺失估值价格时不会推算。",
        "code": "position_value",
    },
    "transaction_cost": {
        "label": "交易总成本",
        "short": "回测期间佣金、滑点和卖出印花税累计扣除的金额。",
        "professional": "累计交易成本",
        "calculation": "直接读取引擎保存的 total_transaction_cost 或 total_cost。",
        "example": "单笔只展示已保存的总成本，不拆分各费用。",
        "limitations": "当前产物没有可靠保存逐项费用拆分。",
        "code": "total_transaction_cost",
    },
    "factor_weight": {
        "label": "因子相对权重",
        "short": "用于合成候选股票得分，不代表持仓资金比例。",
        "professional": "截面因子合成权重",
        "calculation": "沿用候选排名引擎的原归一和缺失处理。",
        "example": "权重 2 与 1 只表达相对影响大小。",
        "limitations": "不能解读为 2% 或两倍仓位。",
        "code": "candidate_factors[].weight",
    },
    "t_plus_one": {
        "label": "T+1",
        "short": "当天买入的股票，最早下一个交易日才能卖出。",
        "professional": "A 股 T+1 卖出限制",
        "calculation": "沿用现有持仓可卖日期判断。",
        "example": "当天出现卖出信号，也可能因 T+1 保留退出意图。",
        "limitations": "只按当前引擎保存的交易约束解释。",
        "code": "t_plus_one",
    },
    "ma_state": {
        "label": "MA 状态",
        "short": "比较当前收盘价或两条均线此刻谁高谁低。",
        "professional": "移动平均线状态条件",
        "calculation": "例如当前 MA5 高于 MA20。",
        "example": "只要保持高于，多个交易日都可能满足。",
        "limitations": "与只在关系发生变化时触发的交叉不同。",
        "code": "ma_compare / close_vs_ma",
    },
    "ma_cross": {
        "label": "MA 交叉",
        "short": "两条均线的高低关系在相邻有效比较时点发生变化。",
        "professional": "移动平均线交叉事件",
        "calculation": "上穿：前值短线不高于长线，本期短线高于长线。",
        "example": "MA5 从下方向上穿过 MA20。",
        "limitations": "数据不足或前一有效比较值缺失时不触发。",
        "code": "ma_cross",
    },
}
IC_EXPLANATION = (
    "IC 为因子值与可观察未来收益的 Pearson 相关系数；Rank IC 为两者排名的 Pearson 相关系数。"
    "低值优先因子按方向修正，正值代表符合预期方向。未来收益从下一交易日开盘至下一调仓执行开盘。"
    "IC 信息比率沿用原 IC 均值与标准差口径；小样本或零方差可能没有有效值。"
)


def factor_label(code):
    if code == "composite":
        return "多因子组合（composite）"
    if isinstance(code, str) and code.startswith("composite:"):
        return "多因子组合：" + " + ".join(factor_label(x) for x in code[10:].split("+"))
    text = str(code)
    if text.startswith("return_") and text[7:].isdigit():
        return f"{text[7:]} 日涨跌幅（{text}）"
    if text.startswith("ma_deviation_") and text[13:].isdigit():
        return f"收盘价相对 MA({text[13:]}) 偏离率（{text}）"
    definition = FINANCIAL_METRICS.get(text)
    return f"{definition['label']}（{code}）" if definition else f"暂无中文名称（{code}）"


def enum_label(code):
    return f"{ENUM_LABELS[code]}（{code}）" if code in ENUM_LABELS else f"{code}（暂无中文说明）"


def field_label(code):
    return FIELD_LABELS.get(str(code), str(code))


def rule_label(rule):
    if not isinstance(rule, dict):
        return str(rule)
    kind, operator = rule.get("kind"), rule.get("operator")
    if kind == "close_vs_ma":
        text = f"收盘价 {ENUM_LABELS.get(operator, operator)} MA({rule.get('window')})"
    elif kind in {"ma_compare", "ma_cross"}:
        text = (
            f"MA({rule.get('short_window')}) {ENUM_LABELS.get(operator, operator)} "
            f"MA({rule.get('long_window')})"
        )
    elif kind == "return_threshold":
        threshold = rule.get("threshold")
        readable = f"{threshold * 100:.2f}%" if isinstance(threshold, (int, float)) else threshold
        text = (
            f"{rule.get('window')} 日涨跌幅 {ENUM_LABELS.get(operator, operator)} "
            f"{readable}（原值 {threshold}）"
        )
    else:
        return f"未识别规则（{kind}/{operator}）"
    return f"{text}（{kind}/{operator}）"


def daily_signal_summary(spec):
    """Build one display-only description from saved strategy parameters."""
    if not isinstance(spec, dict):
        return "策略参数未记录，无法生成中文摘要。"
    factors = (
        "、".join(
            f"{factor_label(item.get('code'))}{'从高到低' if item.get('direction') == 'desc' else '从低到高'}"
            f"（相对权重 {item.get('weight', '未记录')}）"
            for item in spec.get("candidate_factors", [])
            if isinstance(item, dict)
        )
        or "候选因子未记录"
    )
    refresh = ENUM_LABELS.get(spec.get("candidate_refresh"), spec.get("candidate_refresh", "未知"))
    buy = "；".join(rule_label(item) for item in spec.get("buy_rules", [])) or "未配置"
    sell = "；".join(rule_label(item) for item in spec.get("sell_rules", [])) or "未配置"
    buy_mode = ENUM_LABELS.get(spec.get("buy_rule_mode"), spec.get("buy_rule_mode", "未知"))
    sell_mode = ENUM_LABELS.get(spec.get("sell_rule_mode"), spec.get("sell_rule_mode", "未知"))
    exit_text = (
        "退出候选名单时同时生成卖出指令"
        if spec.get("exit_on_candidate_drop")
        else "退出候选名单后继续持有，直到卖出信号出现"
    )
    selection = (
        "单只冻结股票直接参与每日信号检查，不使用候选因子排序；候选得分和排名不适用。"
        if spec.get("candidate_factors") == [] and len(spec.get("symbols") or []) == 1
        else f"{refresh}更新候选股票，根据{factors}合成得分，选择前 {spec.get('candidate_count', '未记录')} 只。"
    )
    return (
        selection +
        f"每天收盘后检查信号；买入规则为{buy_mode}：{buy}。满足时生成买入指令，最早在下一交易日开盘"
        "按滑点、费用、整手和资金约束尝试成交。"
        f"卖出规则为{sell_mode}：{sell}；{exit_text}。卖出受 T+1、停牌或交易限制影响时，"
        "保留退出意图并在后续交易日继续尝试；回测结束不会隐含强制平仓。"
    )


def strategy_label(value):
    parts = str(value).split("|")
    if len(parts) != 4 or not parts[1].startswith("top=") or not parts[3].startswith("commission="):
        return f"未识别策略（{value}）"
    return f"{factor_label(parts[0])} · 持仓 {parts[1][4:]} 只 · {enum_label(parts[2])} · 佣金 {parts[3][11:]} bp"


def presentation_catalog():
    return {
        "factors": {code: item["label"] for code, item in FINANCIAL_METRICS.items()},
        "enums": ENUM_LABELS,
        "fields": FIELD_LABELS,
        "terms": TERM_CATALOG,
        "ic_explanation": IC_EXPLANATION,
        "daily_signal_execution": {
            "signal_timing": "使用当日收盘后完整数据确认信号",
            "execution_timing": "最早在下一交易日开盘尝试成交",
            "buy_expiry": "买单在计划执行日未成交即失效",
            "sell_retry": "卖出受限时保留退出意图并逐交易日重试",
            "sell_first": "同一执行批次卖出优先",
            "no_same_batch_rebuy": "同只股票卖出后不在同一批次立即买回",
            "end_behavior": "回测结束不隐含强制平仓",
            "position_weighting": "最大持仓数量内按等权目标资金计算，不因每日目标变化强制再平衡",
        },
        "percent_fields": [
            "total_return",
            "benchmark_total_return",
            "benchmark_return",
            "excess_return",
            "annualized_return",
            "annualized_volatility",
            "max_drawdown",
            "mean_selection_turnover",
            "positive_ic_ratio",
            "coverage",
            "mean_forward_return",
            "mean_excess_return",
            "median_excess_return",
            "mean_stock_net_return",
            "mean_benchmark_return",
            "train_return",
            "test_return",
            "win_rate",
            "std_excess_return",
            "ci95_lower",
            "ci95_upper",
        ],
    }


def display_money(value):
    """Human readable yuan amount; preserve small nonzero audit costs."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "未记录"
    if value and abs(value) < 0.005:
        return f"{value:,.6f} 元" if abs(value) >= 0.0000005 else f"{value:.3e} 元"
    return f"{value:,.2f} 元"


def install_filters(environment):
    environment.filters.update(
        factor_label=factor_label,
        enum_label=enum_label,
        field_label=field_label,
        rule_label=rule_label,
        daily_signal_summary=daily_signal_summary,
        strategy_label=strategy_label,
        display_money=display_money,
    )
