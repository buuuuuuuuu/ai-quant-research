from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResearchTool:
    name: str
    title: str
    description: str
    required_datasets: tuple[str, ...]
    background: bool = True


RESEARCH_TOOLS = {
    "list_stock_pools": ResearchTool(
        "list_stock_pools",
        "查看股票池",
        "只读列出可用于研究的股票池、版本和成员数量。",
        (),
        background=False,
    ),
    "run_factor_backtest": ResearchTool(
        "run_factor_backtest",
        "财务因子历史回测",
        "按 Point-in-Time 财务因子构建组合并与基准比较。",
        ("daily_bar", "benchmark_daily_bar", "financial_metric"),
    ),
    "run_factor_lab": ResearchTool(
        "run_factor_lab",
        "多因子研究实验",
        "计算 IC、Rank IC、分组收益、参数扫描及样本外验证。",
        ("daily_bar", "benchmark_daily_bar", "financial_metric"),
    ),
    "build_point_in_time_universe": ResearchTool(
        "build_point_in_time_universe",
        "Point-in-Time 股票池预览",
        "按上市退市、指数成分和行业知识时间构建股票池。",
        ("daily_bar",),
        background=False,
    ),
    "run_interim_report_event_study": ResearchTool(
        "run_interim_report_event_study",
        "单股定期报告事件研究",
        "分析定期报告公告后的股票与基准超额收益。",
        ("daily_bar", "benchmark_daily_bar", "announcement"),
    ),
    "run_batch_interim_report_event_study": ResearchTool(
        "run_batch_interim_report_event_study",
        "批量定期报告事件研究",
        "对多只股票运行定期报告事件研究并汇总。",
        ("daily_bar", "benchmark_daily_bar", "announcement"),
    ),
}

ALLOWED_RESEARCH_TOOLS = frozenset(RESEARCH_TOOLS)
COPILOT_RESEARCH_TOOLS = frozenset(
    {
        "list_stock_pools",
        "run_factor_backtest",
        "run_factor_lab",
        "build_point_in_time_universe",
    }
)


def require_allowed_tool(name: str) -> None:
    if name not in ALLOWED_RESEARCH_TOOLS:
        raise PermissionError(f"research tool is not allowlisted: {name}")


def tool_catalog() -> list[dict[str, object]]:
    return [
        {
            "name": item.name,
            "title": item.title,
            "description": item.description,
            "required_datasets": list(item.required_datasets),
            "background": item.background,
        }
        for item in RESEARCH_TOOLS.values()
    ]


def copilot_tool_catalog() -> list[dict[str, object]]:
    return [item for item in tool_catalog() if item["name"] in COPILOT_RESEARCH_TOOLS]
