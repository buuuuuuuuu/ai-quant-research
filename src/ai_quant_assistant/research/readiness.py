"""Read-only, bounded checks against the actual research engines' PIT rules.

Management availability is deliberately neither read as proof nor written here.
The report describes missing samples even when the engine permits their exclusion.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.managed_coverage import completed_date, market_today
from ai_quant_assistant.storage.stock_pools import StockPoolService


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class ResearchReadiness:
    def __init__(self, database: LocalDatabase):
        self.database = database

    def freeze(self, research_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if not self.database.path.is_file():
            raise RuntimeError("本地数据库未初始化，请先运行 ai-quant data init；预检不会执行迁移")
        # Lazy import avoids coupling the engines to FastAPI during module load.
        from ai_quant_assistant.api.schemas import (
            DailySignalStrategyRequest,
            FactorBacktestRequest,
            FactorLabRequest,
            PointInTimeScreenRequest,
        )

        models = {
            "screen": PointInTimeScreenRequest,
            "factor_backtest": FactorBacktestRequest,
            "factor_lab": FactorLabRequest,
            "daily_signal": DailySignalStrategyRequest,
        }
        if research_type not in models:
            raise ValueError("unsupported research type")
        model = models[research_type]
        unexpected = set(parameters) - set(model.model_fields)
        if unexpected:
            raise ValueError(f"unsupported research parameters: {sorted(unexpected)}")
        request = model.model_validate(parameters)
        if request.symbols is None:
            context = StockPoolService(self.database).resolve(
                request.stock_pool_id,
                revision=request.stock_pool_revision,
                initialize=False,
            )
        else:
            symbols = list(request.symbols)
            context = {
                "pool_id": None,
                "pool_revision": None,
                "resolved_symbols": symbols,
                "symbols_sha256": hashlib.sha256(
                    json.dumps(symbols, ensure_ascii=False, separators=(",", ":")).encode()
                ).hexdigest(),
                "resolved_at": datetime.now(UTC).isoformat(),
                "source_type": "explicit",
                "history_verified": False,
                "warning": "手工股票范围可能存在幸存者偏差。",
            }
        if not 1 <= len(context["resolved_symbols"]) <= 500:
            raise ValueError("research requires 1..500 explicitly resolved stocks")
        request.to_spec(resolved_symbols=context["resolved_symbols"], stock_pool=context)
        params = request.model_dump(mode="json")
        start = date.fromisoformat(params.get("start_date") or params["as_of"])
        end = date.fromisoformat(params.get("end_date") or params["as_of"])
        if not date(1990, 1, 1) <= start <= end <= market_today():
            raise ValueError("research dates must be between 1990-01-01 and today")
        if len(str(params.get("benchmark_symbol", ""))) > 32:
            raise ValueError("benchmark code is too long")
        return {
            "research_type": research_type,
            "parameters": params,
            "stock_pool": context,
            "resolved_symbols": list(context["resolved_symbols"]),
            "symbols_sha256": context["symbols_sha256"],
        }

    def run(self, research_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return self.check(self.freeze(research_type, parameters))

    @staticmethod
    def _calendar(connection, exchange, start, end):
        bounds = connection.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM trading_calendar WHERE exchange=?",
            [exchange],
        ).fetchone()
        known = bool(bounds[0] and bounds[0] <= start and bounds[1] >= end)
        days = [
            row[0]
            for row in connection.execute(
                "SELECT trade_date FROM trading_calendar WHERE exchange=? AND is_open "
                "AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                [exchange, start, end],
            ).fetchall()
        ]
        return days, known

    @staticmethod
    def _price_check(
        connection,
        symbol,
        security_id,
        exchange,
        start,
        end,
        adjustment,
        listed,
        delisted,
        screen_rows=0,
        benchmark=False,
    ):
        if screen_rows:
            recent = connection.execute(
                "SELECT trade_date FROM trading_calendar WHERE exchange=? AND is_open "
                "AND trade_date<=? ORDER BY trade_date DESC LIMIT ?",
                [exchange, end, screen_rows],
            ).fetchall()
            start = min((row[0] for row in recent), default=end - timedelta(days=60))
        if listed:
            start = max(start, listed)
        if delisted:
            end = min(end, delisted)
        days, known = ResearchReadiness._calendar(connection, exchange, start, end)
        dataset = "benchmark_daily_bar" if benchmark else "daily_bar"
        if benchmark:
            rows = connection.execute(
                "SELECT trade_date FROM published_benchmark_daily_bar b JOIN benchmark_master m "
                "USING(benchmark_id) WHERE m.symbol=? AND trade_date BETWEEN ? AND ?",
                [symbol, start, end],
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT trade_date FROM published_daily_bar WHERE security_id=? AND adjustment=? "
                "AND trade_date BETWEEN ? AND ?",
                [security_id, adjustment, start, end],
            ).fetchall()
        observed = {row[0] for row in rows}
        missing = sorted(set(days) - observed)
        unknown = not known or (not benchmark and listed is None) or end > completed_date(end)
        enough = len(observed) >= (screen_rows or 2)
        outside_lifetime = listed is not None and start > end
        status = (
            "not_applicable"
            if outside_lifetime
            else "unknown"
            if unknown
            else ("ready" if not missing and enough else "missing")
        )
        reason = (
            "研究区间在证券生命周期之外"
            if outside_lifetime
            else "日历边界、上市日期或最新完整交易日无法验证"
            if unknown
            else f"{len(missing)} 个未解释交易日缺口；实际 {len(observed)} 行，所需至少 {screen_rows or 2} 行"
        )
        return {
            "symbol": symbol,
            "security_id": security_id,
            "dataset": dataset,
            "adjustment": adjustment,
            "status": status,
            "satisfied": status in {"ready", "not_applicable"},
            "severity": "info" if status in {"ready", "not_applicable"} else "blocking",
            "required_start": str(start),
            "required_end": str(end),
            "rows": len(observed),
            "actual_start": str(min(observed)) if observed else None,
            "actual_end": str(max(observed)) if observed else None,
            "missing_count": len(missing) if known else None,
            "missing_dates": [str(day) for day in missing[:20]],
            "reason": reason,
            "can_backfill": not outside_lifetime,
            "cannot_backfill_reason": None,
            "list_date": str(listed) if listed else None,
            "delist_date": str(delisted) if delisted else None,
            "latest_complete_date": str(completed_date(end)),
        }

    def check(self, frozen: dict[str, Any]) -> dict[str, Any]:
        params, kind = frozen["parameters"], frozen["research_type"]
        symbols = frozen["resolved_symbols"]
        start = date.fromisoformat(params.get("start_date") or params["as_of"])
        end = date.fromisoformat(params.get("end_date") or params["as_of"])
        screen = kind == "screen"
        fields = (
            [item["field"] for item in params["conditions"]]
            if screen
            else [item["code"] for item in params["candidate_factors"]]
            if kind == "daily_signal"
            else (
                [params["factor"]]
                if kind == "factor_backtest"
                else [item["code"] for item in params["factors"]]
            )
        )
        if screen and params.get("sort_by"):
            fields.append(params["sort_by"])
        metrics = sorted(set(fields) & set(FINANCIAL_METRICS))
        market_fields = set(fields) - set(FINANCIAL_METRICS)
        screen_rows = 20 if "avg_amount_20d" in market_fields else 1
        checks, blockers, warnings = [], [], []
        market_start = start
        if kind == "daily_signal":
            windows = []
            for field in market_fields:
                suffix = field.rsplit("_", 1)[-1]
                if suffix.isdigit():
                    windows.append(int(suffix))
            for rule in [*params["buy_rules"], *params["sell_rules"]]:
                windows.extend(
                    int(value)
                    for key in ("window", "short_window", "long_window")
                    if (value := rule.get(key)) is not None
                )
            required = max(windows, default=2) + 1
            with self.database.connect(read_only=True) as connection:
                history = connection.execute(
                    "SELECT trade_date FROM trading_calendar WHERE exchange='SSE' AND is_open "
                    "AND trade_date<=? ORDER BY trade_date DESC LIMIT ?",
                    [start, required],
                ).fetchall()
            if len(history) < required:
                blockers.append(
                    f"技术指标需要研究开始日前共 {required} 个可验证交易日（含交叉前值）。"
                )
            elif history:
                market_start = history[-1][0]
        if frozen["stock_pool"].get("warning"):
            warnings.append(frozen["stock_pool"]["warning"])
        financial_start = date(max(1990, start.year - 2), 1, 1)
        if screen:
            signals = [datetime.combine(end, time.max)]
        else:
            with self.database.connect(read_only=True) as connection:
                bench = self._price_check(
                    connection,
                    params["benchmark_symbol"],
                    None,
                    "SSE",
                    market_start,
                    end,
                    "none",
                    None,
                    None,
                    benchmark=True,
                )
                checks.append(bench)
                calendar = [
                    row[0]
                    for row in connection.execute(
                        "SELECT trade_date FROM published_benchmark_daily_bar b JOIN benchmark_master m "
                        "USING(benchmark_id) WHERE symbol=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                        [params["benchmark_symbol"], start, end],
                    ).fetchall()
                ]
            frequencies = params.get(
                "rebalance_values",
                [params.get("candidate_refresh") or params.get("rebalance", "quarterly")],
            )
            signal_days = set()
            for frequency in frequencies:
                periods = set()
                for day in calendar[:-1]:
                    key = (
                        day.year,
                        day.timetuple().tm_yday
                        if frequency == "daily"
                        else day.month
                        if frequency == "monthly"
                        else (day.month - 1) // 3,
                    )
                    if key not in periods:
                        signal_days.add(day)
                        periods.add(key)
            signals = [datetime.combine(day, time(15)) for day in sorted(signal_days)]
            if not signals:
                blockers.append("没有可验证的基准调仓日历；不能确认财务取数时点。")
                signals = [datetime.combine(start, time(15))]
        eligible = {signal: [] for signal in signals}
        metric_eligible = {(metric, signal): 0 for metric in metrics for signal in signals}
        for symbol in symbols:
            # One stock at a time; no whole-database financial DataFrame or date cartesian product.
            with self.database.connect(read_only=True) as connection:
                security = connection.execute(
                    "SELECT s.security_id,s.exchange,COALESCE(s.list_date,m.listing_date),s.delist_date,"
                    "m.status,m.generation FROM security_master s LEFT JOIN managed_stock m USING(security_id) "
                    "WHERE s.symbol=?",
                    [symbol],
                ).fetchone()
                if not security:
                    checks.append(
                        {
                            "symbol": symbol,
                            "dataset": "security_master",
                            "status": "missing",
                            "severity": "blocking",
                            "satisfied": False,
                            "can_backfill": False,
                            "reason": "证券目录不存在，先在数据浏览验证并纳入管理",
                        }
                    )
                    continue
                security_id, exchange, listed, delisted, managed_status, generation = security
                active = managed_status == "active"
                if not screen or market_fields:
                    for adjustment in ["qfq"] if screen else sorted({"none", params["adjustment"]}):
                        check = self._price_check(
                            connection,
                            symbol,
                            security_id,
                            exchange,
                            market_start,
                            end,
                            adjustment,
                            listed,
                            delisted,
                            screen_rows if screen else 0,
                        )
                        check.update(managed_status=managed_status, generation=generation)
                        check["can_backfill"] = active and check["can_backfill"]
                        if not active:
                            check["cannot_backfill_reason"] = (
                                "未纳入管理或已移出，请先在数据浏览处理"
                            )
                        checks.append(check)
                financial = (
                    connection.execute(
                        "SELECT r.report_id,r.report_period_end,r.published_at,r.availability_source,"
                        "r.observed_at,m.metric_code,m.metric_value,r.source_batch_id "
                        "FROM published_financial_report r LEFT JOIN published_financial_metric m USING(report_id) "
                        "WHERE r.security_id=? AND r.published_at<=? ORDER BY r.published_at DESC,r.report_id DESC "
                        "LIMIT 20001",
                        [security_id, datetime.combine(end, time.max)],
                    ).fetchall()
                    if metrics
                    else []
                )
            missing = {metric: [] for metric in metrics}
            evidence = {metric: [] for metric in metrics}
            excessive = len(financial) > 20000
            for signal in signals:
                if (listed and signal.date() < listed) or (delisted and signal.date() > delisted):
                    continue
                available = [
                    row for row in financial if (row[2] <= signal if screen else row[2] < signal)
                ]
                if screen and available:
                    # Screener chooses the latest whole report, not a separate old report per metric.
                    latest = max(available, key=lambda row: (row[2], row[1], row[0]))[0]
                    available = [row for row in available if row[0] == latest]
                found = []
                for metric in metrics:
                    candidates = [
                        row
                        for row in available
                        if row[5] == metric
                        and row[6] is not None
                        and math.isfinite(row[6])
                        and row[3] in {"provider_notice", "matched_announcement"}
                    ]
                    if candidates and not excessive:
                        row = max(candidates, key=lambda row: (row[1], row[2], row[4]))
                        found.append(metric)
                        metric_eligible[(metric, signal)] += 1
                        if len(evidence[metric]) < 20:
                            evidence[metric].append(
                                {
                                    "as_of": signal.isoformat(),
                                    "report_id": row[0],
                                    "published_at": str(row[2]),
                                    "report_period_end": str(row[1]),
                                    "source_batch_id": row[7],
                                }
                            )
                    else:
                        missing[metric].append(signal.isoformat())
                if len(found) == len(metrics) or (
                    params.get("missing_policy") == "available" and found
                ):
                    eligible[signal].append(symbol)
            for metric in metrics:
                checks.append(
                    {
                        "symbol": symbol,
                        "security_id": security_id,
                        "dataset": "financial_metric",
                        "metric": metric,
                        "status": "unknown"
                        if excessive
                        else "missing"
                        if missing[metric]
                        else "ready",
                        "satisfied": not missing[metric] and not excessive,
                        "severity": "warning" if missing[metric] or excessive else "info",
                        "required_start": str(financial_start),
                        "required_end": str(end),
                        "range_semantics": "report_period_end; publication must precede each research cutoff",
                        "missing_count": len(missing[metric]),
                        "missing_as_of": missing[metric][:20],
                        "evidence": evidence[metric],
                        "rows": len(financial),
                        "actual_start": str(min((row[1] for row in financial), default="")) or None,
                        "actual_end": str(max((row[1] for row in financial), default="")) or None,
                        "latest_published_at": str(max((row[2] for row in financial), default=""))
                        or None,
                        "reason": "超过单股财务核验上限"
                        if excessive
                        else (
                            f"{metric}: {len(missing[metric])} 个研究时点缺少已公开指标；策略 {params.get('missing_policy', 'exclude')}；不填零"
                        ),
                        "can_backfill": active,
                        "cannot_backfill_reason": None if active else "股票未纳入管理或已移出",
                        "managed_status": managed_status,
                        "generation": generation,
                    }
                )
        if metrics and not screen:
            minimum = params.get("top_n", max(params.get("top_n_values", [1])))
            insufficient = [
                signal.isoformat() for signal, names in eligible.items() if len(names) < minimum
            ]
            if insufficient:
                blockers.append(
                    f"{len(insufficient)} 个调仓时点的合格财务样本少于 {minimum}；首个：{insufficient[0]}"
                )
            if kind == "factor_lab":
                for metric in metrics:
                    count = sum(metric_eligible[(metric, signal)] < minimum for signal in signals)
                    if count:
                        blockers.append(
                            f"因子实验包含 {metric} 单因子策略：{count} 个时点少于 {minimum} 个有效样本"
                        )
        elif metrics and screen and not any(eligible.values()):
            blockers.append("所有股票在筛选时点均缺少必要指标；不能把空财务覆盖标记为研究就绪。")
        # Historical index/industry rules remain engine-owned; do not pretend a row count verifies them.
        if (
            params.get("universe_mode", "explicit") != "explicit"
            or params.get("require_verified_industry")
            or (
                params.get("industry_neutralization", "none") != "none"
                or params.get("include_industries")
                or params.get("exclude_industries")
            )
        ):
            checks.append(
                {
                    "dataset": "universe_history",
                    "status": "unknown",
                    "satisfied": False,
                    "severity": "blocking",
                    "can_backfill": False,
                    "reason": "历史成分/行业约束需现有研究引擎逐时点预检；首期不自动补齐此类数据",
                }
            )
        for check in checks:
            if not check["satisfied"]:
                text = f"{check.get('symbol', '')} {check['dataset']}: {check['reason']}"
                (blockers if check["severity"] == "blocking" else warnings).append(text)
        recommendations = []

        def recommend(code, title, explanation, action):
            if any(item["code"] == code for item in recommendations):
                return
            recommendations.append(
                {
                    "code": code,
                    "title": title,
                    "explanation": explanation,
                    "action": action,
                }
            )

        if kind == "daily_signal":
            listing_dates = {
                item["symbol"]: item["list_date"]
                for item in checks
                if item["dataset"] == "daily_bar" and item.get("list_date")
            }
            listed_after_start = {
                symbol: listed
                for symbol, listed in listing_dates.items()
                if date.fromisoformat(listed) > start
            }
            if listed_after_start:
                examples = "、".join(
                    f"{symbol}（{listed} 上市）"
                    for symbol, listed in list(listed_after_start.items())[:5]
                )
                entirely_before_listing = len(listing_dates) == len(symbols) and all(
                    date.fromisoformat(listed) > end for listed in listing_dates.values()
                )
                if entirely_before_listing:
                    first_listing = min(listing_dates.values())
                    blockers.append(
                        f"整个研究区间早于全部冻结股票的上市日；最早上市日为 {first_listing}。"
                    )
                    action = (
                        f"将研究区间整体调整到 {first_listing} 之后并保留技术指标预热，"
                        "或加入研究期间已经上市的股票。"
                    )
                elif len(listed_after_start) == len(symbols):
                    action = (
                        f"将开始日期调整到 {max(listed_after_start.values())} 之后并保留技术指标预热，"
                        "或加入研究开始日已经上市的股票。"
                    )
                else:
                    action = "缩短研究区间，或加入研究开始日已经上市且数据完整的股票。"
                recommend(
                    "listing_after_start",
                    "研究开始日早于股票上市日",
                    f"{examples}；上市前不存在行情和财务数据，不能通过补齐生成。",
                    action,
                )
            latest_complete = completed_date(end)
            if end > latest_complete:
                recommend(
                    "unfinished_end_date",
                    "结束日期尚不是可验证的完整交易日",
                    f"当前结束日期为 {end}，保守可验证日期截至 {latest_complete}。",
                    f"将结束日期改为 {latest_complete} 或更早，再重新预检。",
                )
            benchmark_gaps = [
                item
                for item in checks
                if item["dataset"] == "benchmark_daily_bar" and not item["satisfied"]
            ]
            if benchmark_gaps:
                item = benchmark_gaps[0]
                recommend(
                    "benchmark_coverage",
                    "基准行情或交易日历覆盖不足",
                    f"{item['symbol']} 需要覆盖 {item['required_start']} 至 {item['required_end']}。",
                    "先同步交易日历和基准日线；预检不会自动下载。",
                )
            price_gaps = [
                item
                for item in checks
                if item["dataset"] == "daily_bar" and not item["satisfied"]
            ]
            if price_gaps:
                names = "、".join(sorted({item["symbol"] for item in price_gaps})[:5])
                adjustments = "、".join(
                    {
                        "none": "不复权（none）",
                        "qfq": "前复权（qfq）",
                        "hfq": "后复权（hfq）",
                    }.get(code, code)
                    for code in sorted(
                        {str(item.get("adjustment") or "none") for item in price_gaps}
                    )
                )
                recommend(
                    "price_coverage",
                    "股票日线覆盖不足",
                    f"受影响股票：{names}；所需复权口径：{adjustments}。",
                    "同步前复权信号数据和未复权开盘/记账数据，并保留最长均线及交叉前值的预热区间。",
                )
            financial_gaps = [
                item
                for item in checks
                if item["dataset"] == "financial_metric" and not item["satisfied"]
            ]
            if financial_gaps:
                metric_codes = sorted(
                    {str(item.get("metric", "未知因子")) for item in financial_gaps}
                )
                metrics = "、".join(
                    f"{FINANCIAL_METRICS[code]['label']}（{code}）"
                    if code in FINANCIAL_METRICS
                    else code
                    for code in metric_codes
                )
                recommend(
                    "financial_coverage",
                    "候选财务因子在部分时点不可用",
                    f"缺失或无法验证的因子：{metrics}；系统不会用当前值回填历史。",
                    "同步带实际公开时间的历史财务报告；若只想先验证技术链路，可明确改用 return_N 或 ma_deviation_N 后重新预检。",
                )
            if any("合格财务样本少于" in item for item in blockers):
                recommend(
                    "candidate_sample_shortage",
                    "部分候选更新日无法选满股票",
                    f"候选数量要求为 {params['candidate_count']}，但部分时点的有效样本更少。",
                    "缩短到数据完整区间、扩充当时已经上市的股票范围，或补齐已公开财务数据；不要把缺失值填零。",
                )
        blocking_check_count = len(blockers)
        return {
            "status": "blocked" if blockers else "warning" if warnings else "ready",
            "status_label": "预检阻断" if blockers else "可以运行（有提示）" if warnings else "可以运行",
            "ready": not blockers,
            "complete": all(item["satisfied"] for item in checks),
            "checked_at": datetime.now(UTC).isoformat(),
            "input": frozen,
            "checks": checks,
            "blockers": blockers,
            "warnings": warnings,
            "recommendations": recommendations,
            "blocking_check_count": blocking_check_count,
            "required_datasets": sorted({item["dataset"] for item in checks}),
            "signal_count": len(signals),
            "signal_cutoff": "as_of end-of-day inclusive"
            if screen
            else "signal day 15:00 exclusive",
            "financial_warmup_start": str(financial_start),
            "financial_validity_rule": "沿用引擎最新已公开报告，无固定季度有效期；补齐默认向前两年，不能保证缺失可补齐",
            "missing_policy": params.get("missing_policy", "exclude"),
            "affected_symbols": sorted(
                {item["symbol"] for item in checks if not item["satisfied"] and item.get("symbol")}
            ),
        }
