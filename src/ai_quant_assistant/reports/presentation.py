"""Display labels only; internal research codes and data remain unchanged."""

from __future__ import annotations

from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS

ENUM_LABELS = {
    "asc": "低值优先",
    "desc": "高值优先",
    "monthly": "按月",
    "quarterly": "按季度",
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
    definition = FINANCIAL_METRICS.get(str(code))
    return f"{definition['label']}（{code}）" if definition else f"未知因子（{code}）"


def enum_label(code):
    return f"{ENUM_LABELS[code]}（{code}）" if code in ENUM_LABELS else str(code)


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
        "ic_explanation": IC_EXPLANATION,
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


def install_filters(environment):
    environment.filters.update(
        factor_label=factor_label, enum_label=enum_label, strategy_label=strategy_label
    )
