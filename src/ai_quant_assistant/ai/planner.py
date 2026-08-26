from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ai_quant_assistant.ai.tool_registry import COPILOT_RESEARCH_TOOLS, copilot_tool_catalog
from ai_quant_assistant.config import Settings
from ai_quant_assistant.domain.models import DEFAULT_RESEARCH_SYMBOLS

PROMPT_VERSION = "m6a-v1"
FORBIDDEN_PATTERNS = (
    r"\b(?:shell|bash|powershell|subprocess|os\.system|eval|exec)\b",
    r"\b(?:curl|wget|ssh|scp)\b",
    r"(?:执行|运行).{0,8}(?:代码|命令|脚本|SQL)",
    r"(?:删除|清空|覆盖).{0,8}(?:数据库|文件|表)",
    r"(?:忽略|绕过).{0,12}(?:规则|限制|安全|白名单)",
)
FACTOR_ALIASES = {
    "roe": "roe",
    "净资产收益率": "roe",
    "营收增长": "revenue_yoy",
    "收入增长": "revenue_yoy",
    "revenue_yoy": "revenue_yoy",
    "净利润增长": "parent_net_profit_yoy",
    "parent_net_profit_yoy": "parent_net_profit_yoy",
    "毛利率": "gross_margin",
    "gross_margin": "gross_margin",
    "净利率": "net_margin",
    "net_margin": "net_margin",
    "负债率": "debt_asset_ratio",
    "资产负债率": "debt_asset_ratio",
    "debt_asset_ratio": "debt_asset_ratio",
}


class Planner(Protocol):
    def plan(self, question: str) -> dict[str, Any]: ...


def _base_spec(question: str) -> dict[str, Any]:
    symbols = list(dict.fromkeys(re.findall(r"(?<!\d)(\d{6})(?!\d)", question)))
    if not symbols:
        symbols = list(DEFAULT_RESEARCH_SYMBOLS)
    dates = re.findall(r"(?:19|20)\d{2}-\d{2}-\d{2}", question)
    start_date = dates[0] if dates else "2015-01-01"
    end_date = dates[1] if len(dates) > 1 else date.today().isoformat()
    adjustment = "hfq" if "后复权" in question or "hfq" in question.lower() else "qfq"
    benchmark = "csi000300"
    factors: list[str] = []
    lowered = question.lower()
    for alias, code in FACTOR_ALIASES.items():
        if alias in lowered and code not in factors:
            factors.append(code)
    if not factors:
        factors = ["roe"]
    top_match = re.search(r"(?:top|前)\s*(\d{1,3})", lowered, re.IGNORECASE)
    top_n = int(top_match.group(1)) if top_match else min(3, len(symbols))
    return {
        "symbols": symbols,
        "start_date": start_date,
        "end_date": end_date,
        "adjustment": adjustment,
        "benchmark_symbol": benchmark,
        "factors": factors,
        "top_n": max(1, min(top_n, len(symbols))),
    }


def validate_draft(draft: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(draft)
    tool_name = normalized.get("tool_name")
    if tool_name is not None and tool_name not in COPILOT_RESEARCH_TOOLS:
        normalized.update(
            blocked=True,
            tool_name=None,
            warnings=[
                *normalized.get("warnings", []),
                f"模型请求了未授权工具：{tool_name}。计划已阻断。",
            ],
        )
    normalized.setdefault("title", "研究计划")
    normalized.setdefault("rationale", "根据研究问题生成的结构化计划。")
    normalized.setdefault("tool_name", None)
    normalized.setdefault("spec", {})
    normalized.setdefault("assumptions", [])
    normalized.setdefault("warnings", [])
    normalized["blocked"] = bool(normalized.get("blocked"))
    return normalized


class RuleBasedPlanner:
    name = "rules"

    def plan(self, question: str) -> dict[str, Any]:
        question = question.strip()
        if not question or len(question) > 4000:
            raise ValueError("研究问题必须为 1 至 4000 个字符")
        if any(re.search(pattern, question, re.IGNORECASE) for pattern in FORBIDDEN_PATTERNS):
            return validate_draft(
                {
                    "blocked": True,
                    "title": "请求已被安全策略阻断",
                    "rationale": "请求涉及任意代码、系统命令、数据库破坏或绕过安全约束。",
                    "warnings": ["Copilot 只能生成并调用预先注册的只读研究工具。"],
                    "planner": self.name,
                }
            )

        common = _base_spec(question)
        lowered = question.lower()
        assumptions = [
            "研究只读取本地已发布数据，不会自动联网补数。",
            "财务因子按 published_at 执行 Point-in-Time 约束。",
        ]
        warnings: list[str] = []
        if "历史成分" in question:
            universe_mode = "historical_index"
        elif "观察成分" in question or "当前成分" in question:
            universe_mode = "observed_index"
            warnings.append("观察成分不代表历史真实指数成分，存在幸存者偏差。")
        elif "全部股票" in question or "全市场" in question:
            universe_mode = "all_available"
        else:
            universe_mode = "explicit"

        if any(word in lowered for word in ("股票池", "指数成分", "行业覆盖")) and not any(
            word in lowered for word in ("回测", "因子", "ic")
        ):
            tool_name = "build_point_in_time_universe"
            spec = {
                "as_of": common["end_date"],
                "mode": universe_mode if universe_mode != "explicit" else "all_available",
                "symbols": common["symbols"],
                "benchmark_symbol": common["benchmark_symbol"],
                "classification_system": "provider",
                "require_verified_industry": "可信行业" in question,
            }
            title = "Point-in-Time 股票池预览"
        elif any(
            word in lowered
            for word in ("多因子", "rank ic", "rank_ic", "ic分析", "参数扫描", "样本外")
        ):
            tool_name = "run_factor_lab"
            components = [
                {
                    "code": code,
                    "weight": 1.0,
                    "direction": "asc" if code == "debt_asset_ratio" else "desc",
                }
                for code in common["factors"]
            ]
            spec = {
                "symbols": common["symbols"],
                "start_date": common["start_date"],
                "end_date": common["end_date"],
                "factors": components,
                "top_n_values": [common["top_n"]],
                "rebalance_values": ["quarterly"],
                "commission_bps_values": [5.0],
                "quantiles": 5,
                "benchmark_symbol": common["benchmark_symbol"],
                "adjustment": common["adjustment"],
                "universe_mode": universe_mode,
                "universe_benchmark": common["benchmark_symbol"],
                "industry_neutralization": "within_industry_rank"
                if "行业中性" in question
                else "none",
                "require_verified_industry": "可信行业" in question,
            }
            title = "多因子 Point-in-Time 研究"
        else:
            tool_name = "run_factor_backtest"
            factor = common["factors"][0]
            spec = {
                "symbols": common["symbols"],
                "start_date": common["start_date"],
                "end_date": common["end_date"],
                "factor": factor,
                "factor_direction": "asc" if factor == "debt_asset_ratio" else "desc",
                "top_n": common["top_n"],
                "rebalance": "monthly" if "月度" in question else "quarterly",
                "benchmark_symbol": common["benchmark_symbol"],
                "adjustment": common["adjustment"],
                "universe_mode": universe_mode,
                "universe_benchmark": common["benchmark_symbol"],
                "industry_neutralization": "within_industry_rank"
                if "行业中性" in question
                else "none",
                "require_verified_industry": "可信行业" in question,
            }
            title = f"{factor.upper()} Point-in-Time 因子回测"

        if not re.search(r"(?:19|20)\d{2}-\d{2}-\d{2}", question):
            assumptions.append(f"未指定日期，暂按 {common['start_date']} 至 {common['end_date']}。")
        return validate_draft(
            {
                "tool_name": tool_name,
                "title": title,
                "rationale": "将自然语言目标转换为可预览、可验证、需显式确认的研究参数。",
                "spec": spec,
                "assumptions": assumptions,
                "warnings": warnings,
                "planner": self.name,
            }
        )


class OpenAIResponsesPlanner:
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when AI_QUANT_COPILOT_PLANNER=openai")
        self.settings = settings

    def plan(self, question: str) -> dict[str, Any]:
        if not question.strip() or len(question) > 4000:
            raise ValueError("研究问题必须为 1 至 4000 个字符")
        payload = {
            "model": self.settings.openai_model,
            "store": False,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "你是 A 股研究计划器。只生成计划，不执行工具。只能从给定白名单选择；"
                        "不得生成 SQL、代码、命令、联网抓取或投资承诺。信息不足时 blocked=true。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "tools": copilot_tool_catalog()},
                        ensure_ascii=False,
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "research_plan",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "blocked": {"type": "boolean"},
                            "tool_name": {
                                "type": ["string", "null"],
                                "enum": [*sorted(COPILOT_RESEARCH_TOOLS), None],
                            },
                            "title": {"type": "string"},
                            "rationale": {"type": "string"},
                            "spec": {"type": "object", "additionalProperties": True},
                            "assumptions": {"type": "array", "items": {"type": "string"}},
                            "warnings": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": [
                            "blocked",
                            "tool_name",
                            "title",
                            "rationale",
                            "spec",
                            "assumptions",
                            "warnings",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
        }
        request = Request(
            f"{self.settings.openai_base_url}/responses",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.settings.openai_timeout_seconds) as response:  # noqa: S310
                body = json.loads(response.read().decode())
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"OpenAI planner request failed: {exc}") from exc
        output_text = body.get("output_text")
        if not output_text:
            for output in body.get("output", []):
                for content in output.get("content", []):
                    if content.get("type") == "output_text":
                        output_text = content.get("text")
                        break
        if not output_text:
            raise RuntimeError("OpenAI planner returned no structured plan")
        draft = validate_draft(json.loads(output_text))
        draft.update(
            planner=self.name,
            model_name=self.settings.openai_model,
            model_response_id=body.get("id"),
        )
        return draft


def create_planner(settings: Settings) -> Planner:
    if settings.copilot_planner == "openai":
        return OpenAIResponsesPlanner(settings)
    if settings.copilot_planner != "rules":
        raise ValueError("AI_QUANT_COPILOT_PLANNER must be rules or openai")
    return RuleBasedPlanner()
