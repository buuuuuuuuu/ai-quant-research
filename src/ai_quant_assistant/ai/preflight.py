from __future__ import annotations

from datetime import date
from typing import Any

from ai_quant_assistant.ai.tool_registry import RESEARCH_TOOLS, require_allowed_tool
from ai_quant_assistant.api.schemas import FactorBacktestRequest, FactorLabRequest
from ai_quant_assistant.storage.database import LocalDatabase


def validate_tool_spec(tool_name: str, spec: dict[str, Any]) -> dict[str, Any]:
    require_allowed_tool(tool_name)
    if tool_name == "list_stock_pools":
        return {}
    if tool_name == "run_factor_backtest":
        return FactorBacktestRequest.model_validate(spec).model_dump(mode="json")
    if tool_name == "run_factor_lab":
        return FactorLabRequest.model_validate(spec).model_dump(mode="json")
    if tool_name == "build_point_in_time_universe":
        as_of = date.fromisoformat(str(spec.get("as_of")))
        mode = str(spec.get("mode", "all_available"))
        if mode not in {"explicit", "all_available", "historical_index", "observed_index"}:
            raise ValueError("unsupported universe mode")
        symbols = list(dict.fromkeys(str(item).strip() for item in spec.get("symbols", [])))
        if any(len(item) != 6 or not item.isdigit() for item in symbols):
            raise ValueError("symbols must contain six-digit A-share codes")
        return {
            "as_of": as_of.isoformat(),
            "mode": mode,
            "symbols": symbols,
            "benchmark_symbol": str(spec.get("benchmark_symbol", "csi000300")),
            "classification_system": str(spec.get("classification_system", "provider")),
            "require_verified_industry": bool(spec.get("require_verified_industry", False)),
        }
    raise ValueError(f"Copilot execution is not implemented for {tool_name}")


class CoveragePreflight:
    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    def run(self, tool_name: str, spec: dict[str, Any]) -> dict[str, Any]:
        if tool_name in {"run_factor_backtest", "run_factor_lab"}:
            from ai_quant_assistant.research.readiness import ResearchReadiness

            readiness = ResearchReadiness(self.database)
            parameters = {key: value for key, value in spec.items() if key not in {"_stock_pool_context", "stock_pool"}}
            context = spec.get("_stock_pool_context") or spec.get("stock_pool")
            if context:
                parameters.pop("stock_pool_id", None)
                parameters.pop("stock_pool_revision", None)
                parameters["symbols"] = context["resolved_symbols"]
            frozen = readiness.freeze(tool_name.removeprefix("run_"), parameters)
            if context:
                frozen["stock_pool"] = context
            return readiness.check(frozen)
        required = list(RESEARCH_TOOLS[tool_name].required_datasets)
        checks: list[dict[str, Any]] = []
        blockers: list[str] = []
        warnings: list[str] = []
        status = self.database.status()
        if not status.get("initialized"):
            return {
                "status": "blocked",
                "ready": False,
                "required_datasets": required,
                "checks": [],
                "blockers": ["本地数据库尚未初始化。"],
                "warnings": [],
            }
        if status.get("schema_version", 0) < status.get("expected_schema_version", 8):
            blockers.append("数据库迁移未完成，请先运行 ai-quant data init。")
        if tool_name == "list_stock_pools":
            return {
                "status": "ready",
                "ready": True,
                "required_datasets": [],
                "checks": [],
                "blockers": [],
                "warnings": [],
                "lineage": {},
            }

        symbols = tuple(spec.get("symbols", []))
        start_date = spec.get("start_date")
        end_date = spec.get("end_date") or spec.get("as_of")
        adjustment = spec.get("adjustment", "qfq")
        factors = [spec.get("factor")] if spec.get("factor") else []
        factors.extend(item["code"] for item in spec.get("factors", []))
        factors = list(dict.fromkeys(item for item in factors if item))
        pool_context = spec.get("stock_pool") or {}
        if pool_context.get("warning"):
            warnings.append(str(pool_context["warning"]))
        with self.database.connect(read_only=True) as connection:
            if symbols:
                placeholders = ",".join("?" for _ in symbols)
                listed = connection.execute(
                    f"""
                    SELECT symbol FROM security_master WHERE symbol IN ({placeholders})
                    """,
                    list(symbols),
                ).fetchall()
                known = {str(row[0]) for row in listed}
                missing_master = sorted(set(symbols) - known)
                checks.append(
                    {
                        "dataset": "security_master",
                        "status": "passed" if not missing_master else "failed",
                        "requested": len(symbols),
                        "available": len(known),
                        "missing_symbols": missing_master,
                    }
                )
                if missing_master:
                    blockers.append(f"证券主表缺少：{', '.join(missing_master)}")

            if "daily_bar" in required and symbols:
                rows = connection.execute(
                    f"""
                    SELECT security.symbol, MIN(bar.trade_date), MAX(bar.trade_date), COUNT(*)
                    FROM security_master AS security
                    LEFT JOIN published_daily_bar AS bar
                      ON bar.security_id = security.security_id AND bar.adjustment = ?
                     AND (? IS NULL OR bar.trade_date >= CAST(? AS DATE))
                     AND (? IS NULL OR bar.trade_date <= CAST(? AS DATE))
                    WHERE security.symbol IN ({placeholders})
                    GROUP BY security.symbol ORDER BY security.symbol
                    """,
                    [adjustment, start_date, start_date, end_date, end_date, *symbols],
                ).fetchall()
                missing = sorted(str(row[0]) for row in rows if int(row[3]) == 0)
                checks.append(
                    {
                        "dataset": "daily_bar",
                        "status": "passed" if not missing else "failed",
                        "adjustment": adjustment,
                        "symbols_with_rows": sum(int(row[3]) > 0 for row in rows),
                        "requested_symbols": len(symbols),
                        "rows": sum(int(row[3]) for row in rows),
                        "start_date": min((row[1] for row in rows if row[1]), default=None),
                        "end_date": max((row[2] for row in rows if row[2]), default=None),
                        "missing_symbols": missing,
                    }
                )
                if missing:
                    blockers.append(f"本地已发布行情无可用记录：{', '.join(missing)}")

            if "benchmark_daily_bar" in required:
                benchmark = str(spec.get("benchmark_symbol", "csi000300"))
                row = connection.execute(
                    """
                    SELECT MIN(bar.trade_date), MAX(bar.trade_date), COUNT(*)
                    FROM published_benchmark_daily_bar AS bar
                    JOIN benchmark_master AS master USING (benchmark_id)
                    WHERE master.symbol = ?
                      AND (? IS NULL OR bar.trade_date >= CAST(? AS DATE))
                      AND (? IS NULL OR bar.trade_date <= CAST(? AS DATE))
                    """,
                    [benchmark, start_date, start_date, end_date, end_date],
                ).fetchone()
                count = int(row[2])
                checks.append(
                    {
                        "dataset": "benchmark_daily_bar",
                        "status": "passed" if count else "failed",
                        "benchmark_symbol": benchmark,
                        "rows": count,
                        "start_date": row[0],
                        "end_date": row[1],
                    }
                )
                if not count:
                    blockers.append(f"基准 {benchmark} 在研究区间没有本地已发布行情。")

            if "financial_metric" in required and symbols:
                metric_rows = connection.execute(
                    f"""
                    SELECT metric.metric_code, COUNT(DISTINCT security.symbol), COUNT(*)
                    FROM published_financial_metric AS metric
                    JOIN security_master AS security USING (security_id)
                    WHERE security.symbol IN ({placeholders})
                      AND metric.metric_code IN ({",".join("?" for _ in factors)})
                      AND (? IS NULL OR metric.published_at <= CAST(? AS TIMESTAMP))
                    GROUP BY metric.metric_code
                    """,
                    [*symbols, *factors, end_date, end_date],
                ).fetchall()
                coverage = {
                    str(row[0]): {"securities": int(row[1]), "rows": int(row[2])}
                    for row in metric_rows
                }
                missing_factors = [factor for factor in factors if not coverage.get(factor)]
                checks.append(
                    {
                        "dataset": "financial_metric",
                        "status": "passed" if not missing_factors else "failed",
                        "metrics": coverage,
                        "missing_factors": missing_factors,
                    }
                )
                if missing_factors:
                    blockers.append(f"本地财务指标缺少：{', '.join(missing_factors)}")
                for factor, item in coverage.items():
                    if item["securities"] < len(symbols):
                        warnings.append(
                            f"{factor} 仅覆盖 {item['securities']}/{len(symbols)} 只股票，"
                            "回测会按缺失策略排除。"
                        )

            mode = spec.get("universe_mode") or spec.get("mode")
            if mode in {"historical_index", "observed_index"}:
                history_mode = (
                    "verified_history" if mode == "historical_index" else "observed_snapshot"
                )
                count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM index_constituent_history
                        WHERE benchmark_symbol = ? AND history_mode = ?
                        """,
                        [
                            spec.get(
                                "universe_benchmark", spec.get("benchmark_symbol", "csi000300")
                            ),
                            history_mode,
                        ],
                    ).fetchone()[0]
                )
                checks.append(
                    {
                        "dataset": "index_constituent_history",
                        "status": "passed" if count else "failed",
                        "history_mode": history_mode,
                        "rows": count,
                    }
                )
                if not count:
                    blockers.append(f"股票池要求 {history_mode}，但本地没有可用成分记录。")

            batch_rows = connection.execute(
                """
                SELECT DISTINCT source_batch_id FROM data_coverage
                WHERE (? IS NULL OR start_date <= CAST(? AS DATE))
                  AND (? IS NULL OR end_date >= CAST(? AS DATE))
                ORDER BY source_batch_id LIMIT 50
                """,
                [end_date, end_date, start_date, start_date],
            ).fetchall()

        outcome = "blocked" if blockers else "warning" if warnings else "ready"
        return {
            "status": outcome,
            "ready": not blockers,
            "required_datasets": required,
            "checks": checks,
            "blockers": blockers,
            "warnings": warnings,
            "lineage": {"source_batch_ids": [str(row[0]) for row in batch_rows]},
        }
