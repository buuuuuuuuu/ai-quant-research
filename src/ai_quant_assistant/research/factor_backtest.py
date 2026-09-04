from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd

from ai_quant_assistant.domain.models import FactorBacktestSpec
from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS
from ai_quant_assistant.reports.backtest_writer import FactorBacktestReportWriter
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.snapshots import write_json
from ai_quant_assistant.storage.universe import UniverseRepository

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[int, str], None]


class FactorBacktestService:
    def __init__(self, *, database: LocalDatabase, artifact_dir: Path) -> None:
        self.database = database
        self.artifact_dir = artifact_dir
        self.writer = FactorBacktestReportWriter()
        self.universe = UniverseRepository(database)

    def eligible_snapshot(
        self, spec: FactorBacktestSpec, as_of: Any, available: set[str]
    ) -> pd.DataFrame:
        factors = self.factor_snapshot(spec, as_of)
        if spec.universe_mode != "explicit":
            definition = self.universe.build(
                as_of=as_of,
                mode=spec.universe_mode,
                symbols=spec.symbols,
                benchmark_symbol=spec.universe_benchmark,
                classification_system=spec.classification_system,
                require_history=spec.require_verified_industry,
            )
            available = available & set(definition["symbols"])
        factors = factors.loc[factors["symbol"].isin(available)].copy()
        industries = self.universe.industries(
            tuple(factors["symbol"]),
            as_of,
            classification_system=spec.classification_system,
            verified_only=spec.require_verified_industry,
        )
        if not industries.empty:
            factors = factors.merge(
                industries[["symbol", "industry_name", "history_mode"]], on="symbol", how="left"
            )
        else:
            factors["industry_name"] = None
            factors["history_mode"] = None
        factors["factor_value"] = pd.to_numeric(factors["factor_value"], errors="coerce")
        factors = factors.dropna(subset=["factor_value"])
        if spec.industry_neutralization == "within_industry_rank":
            factors = factors.dropna(subset=["industry_name"]).copy()
            if factors.empty:
                raise ValueError(
                    "industry neutralization requires point-in-time industry classification"
                )
            factors["raw_factor_value"] = factors["factor_value"]
            factors["factor_value"] = factors.groupby("industry_name")["factor_value"].rank(
                pct=True, method="average", ascending=spec.factor_direction == "desc"
            )
            ascending = False
        else:
            ascending = spec.factor_direction == "asc"
        factors = factors.sort_values(
            ["factor_value", "symbol"], ascending=[ascending, True]
        ).reset_index(drop=True)
        factors["rank"] = range(1, len(factors) + 1)
        if spec.max_industry_weight >= 1:
            factors["selected"] = factors["rank"] <= spec.top_n
            return factors
        per_industry = max(1, int(np.floor(spec.top_n * spec.max_industry_weight)))
        chosen: list[bool] = []
        counts: Counter[str] = Counter()
        total = 0
        for row in factors.itertuples(index=False):
            industry = "unknown" if pd.isna(row.industry_name) else str(row.industry_name)
            allow = total < spec.top_n and counts[industry] < per_industry
            chosen.append(allow)
            if allow:
                counts[industry] += 1
                total += 1
        factors["selected"] = chosen
        return factors

    @staticmethod
    def _run_id() -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"factor-backtest-{timestamp}-{uuid4().hex[:8]}"

    def factor_snapshot(self, spec: FactorBacktestSpec, as_of: Any) -> pd.DataFrame:
        if not spec.factors:
            return self.database.read_factor_snapshot(spec.symbols, spec.factor, as_of)
        snapshots: list[pd.DataFrame] = []
        for component in spec.factors:
            current = self.database.read_factor_snapshot(spec.symbols, component.code, as_of)
            if current.empty:
                current = current.copy()
            current = current.rename(columns={"factor_value": component.code})
            keep = [
                "symbol",
                component.code,
                "report_period_end",
                "report_type",
                "published_at",
                "availability_source",
                "source_batch_id",
            ]
            snapshots.append(current[keep])
        if not snapshots or (
            spec.missing_policy == "exclude" and any(frame.empty for frame in snapshots)
        ):
            return pd.DataFrame(
                columns=[
                    "symbol",
                    "factor_value",
                    "report_period_end",
                    "report_type",
                    "published_at",
                    "availability_source",
                    "source_batch_id",
                    "component_values",
                ]
            )
        metadata = [
            "report_period_end",
            "report_type",
            "published_at",
            "availability_source",
            "source_batch_id",
        ]
        result = snapshots[0][["symbol", spec.factors[0].code]].copy()
        for frame in snapshots[1:]:
            result = result.merge(
                frame.drop(columns=metadata),
                on="symbol",
                how="inner" if spec.missing_policy == "exclude" else "outer",
            )
        if result.empty:
            result["factor_value"] = pd.Series(dtype=float)
            return result
        available_metadata = [
            frame[["symbol", *metadata]] for frame in snapshots if not frame.empty
        ]
        lineage = (
            pd.concat(available_metadata, ignore_index=True)
            .sort_values(["symbol", "published_at", "report_period_end"])
            .drop_duplicates("symbol", keep="last")
        )
        result = result.merge(lineage, on="symbol", how="left")
        scores = pd.DataFrame(index=result.index)
        weights = pd.DataFrame(index=result.index)
        for component in spec.factors:
            values = pd.to_numeric(result[component.code], errors="coerce")
            percentile = values.rank(
                pct=True, method="average", ascending=component.direction == "desc"
            )
            scores[component.code] = percentile * component.weight
            weights[component.code] = values.notna().astype(float) * component.weight
        total_weight = weights.sum(axis=1)
        result["factor_value"] = scores.fillna(0).sum(axis=1).div(total_weight.replace(0, np.nan))
        result = result.dropna(subset=["factor_value"])
        result["component_values"] = result.apply(
            lambda row: json.dumps(
                {
                    component.code: None
                    if pd.isna(row[component.code])
                    else float(row[component.code])
                    for component in spec.factors
                },
                ensure_ascii=False,
            ),
            axis=1,
        )
        return result

    @staticmethod
    def _rebalance_dates(calendar: pd.Series, frequency: str) -> list[pd.Timestamp]:
        frame = pd.DataFrame({"date": pd.to_datetime(calendar).dt.normalize()})
        period = "M" if frequency == "monthly" else "Q"
        frame["period"] = frame["date"].dt.to_period(period)
        return frame.groupby("period", sort=True)["date"].first().tolist()

    @staticmethod
    def _price_limit_pct(symbol: str) -> float:
        if symbol.startswith(("4", "8", "92")):
            return 0.30
        if symbol.startswith(("300", "301", "688")):
            return 0.20
        return 0.10

    @classmethod
    def _limit_state(cls, row: Any) -> tuple[bool, bool]:
        previous = row.previous_unadjusted_close
        if previous is None or pd.isna(previous) or float(previous) <= 0:
            return False, False
        limit = cls._price_limit_pct(str(row.symbol))
        upper = float(previous) * (1.0 + limit)
        lower = float(previous) * (1.0 - limit)
        tolerance = 0.002
        one_price_up = float(row.unadjusted_low) >= upper * (1.0 - tolerance) and float(
            row.unadjusted_high
        ) >= upper * (1.0 - tolerance)
        one_price_down = float(row.unadjusted_high) <= lower * (1.0 + tolerance) and float(
            row.unadjusted_low
        ) <= lower * (1.0 + tolerance)
        return one_price_up, one_price_down

    @staticmethod
    def _performance(nav: pd.DataFrame) -> dict[str, float]:
        if nav.empty:
            return {}
        daily_returns = nav["portfolio_nav"].pct_change().fillna(0.0)
        benchmark_returns = nav["benchmark_nav"].pct_change().fillna(0.0)
        periods = max(len(nav) - 1, 1)
        annual_factor = 252.0 / periods
        final_nav = float(nav["portfolio_nav"].iloc[-1])
        final_benchmark = float(nav["benchmark_nav"].iloc[-1])
        volatility = float(daily_returns.std(ddof=1) * np.sqrt(252)) if len(nav) > 2 else 0.0
        benchmark_volatility = (
            float(benchmark_returns.std(ddof=1) * np.sqrt(252)) if len(nav) > 2 else 0.0
        )
        annual_return = final_nav**annual_factor - 1.0 if final_nav > 0 else -1.0
        benchmark_annual_return = (
            final_benchmark**annual_factor - 1.0 if final_benchmark > 0 else -1.0
        )
        return {
            "total_return": final_nav - 1.0,
            "benchmark_total_return": final_benchmark - 1.0,
            "excess_return": final_nav - final_benchmark,
            "annualized_return": annual_return,
            "benchmark_annualized_return": benchmark_annual_return,
            "annualized_volatility": volatility,
            "benchmark_annualized_volatility": benchmark_volatility,
            "sharpe_ratio": annual_return / volatility if volatility > 0 else 0.0,
            "max_drawdown": abs(float(nav["drawdown"].min())),
            "final_value": float(nav["portfolio_value"].iloc[-1]),
        }

    def run(
        self,
        spec: FactorBacktestSpec,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        def progress(value: int, message: str) -> None:
            if progress_callback is not None:
                progress_callback(value, message)

        progress(2, "正在验证本地数据库")
        unsupported = [item.code for item in spec.factors if item.code not in FINANCIAL_METRICS]
        if unsupported:
            raise ValueError(f"unsupported factors: {', '.join(unsupported)}")
        if not spec.factors and spec.factor not in FINANCIAL_METRICS:
            raise ValueError(f"unsupported factor: {spec.factor}")
        status = self.database.status()
        if status.get("schema_version", 0) < 4:
            raise ValueError("factor backtest requires an initialized Schema v4 local database")

        run_id = self._run_id()
        run_dir = self.artifact_dir / "backtests" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        started_at = datetime.now(UTC)
        benchmark = self.database.read_benchmark_prices(
            spec.benchmark_symbol, spec.start_date, spec.end_date
        )
        if benchmark.empty:
            raise ValueError("no published local benchmark prices cover the requested interval")
        benchmark = benchmark.drop_duplicates("date").sort_values("date").reset_index(drop=True)
        prices = self.database.read_backtest_prices(
            spec.symbols, spec.start_date, spec.end_date, spec.adjustment
        )
        if prices.empty:
            raise ValueError("no published local stock prices cover the requested universe")
        progress(15, "行情、基准和财务数据已就绪")

        calendar = benchmark["date"]
        calendar_dates = calendar.tolist()
        rebalance_dates = self._rebalance_dates(calendar, spec.rebalance)
        date_index = {date: index for index, date in enumerate(calendar_dates)}
        prices_by_date = {
            date: group.set_index("symbol", drop=False)
            for date, group in prices.groupby("date", sort=True)
        }
        benchmark_by_date = benchmark.set_index("date")["close"]
        first_benchmark = float(benchmark_by_date.iloc[0])
        commission_rate = spec.commission_bps / 10_000.0
        slippage_rate = spec.slippage_bps / 10_000.0
        stamp_rate = spec.stamp_duty_bps / 10_000.0

        cash = float(spec.initial_capital)
        holdings: dict[str, dict[str, Any]] = {}
        last_prices: dict[str, float] = {}
        pending: dict[pd.Timestamp, dict[str, Any]] = {}
        nav_rows: list[dict[str, Any]] = []
        trade_rows: list[dict[str, Any]] = []
        selection_rows: list[dict[str, Any]] = []
        position_rows: list[dict[str, Any]] = []
        blocked_reasons: Counter[str] = Counter()
        total_costs = 0.0

        last_progress = 15
        for day_number, current_date in enumerate(calendar_dates, start=1):
            day = prices_by_date.get(current_date, pd.DataFrame())
            day_turnover = 0.0
            order = pending.pop(current_date, None)
            if order is not None:
                targets = set(order["symbols"])
                execution_prices = {
                    symbol: float(day.loc[symbol, "open"])
                    for symbol in day.index
                    if float(day.loc[symbol, "volume"]) > 0
                }
                opening_value = cash + sum(
                    item["shares"] * execution_prices.get(symbol, last_prices.get(symbol, 0.0))
                    for symbol, item in holdings.items()
                )
                target_value = opening_value / len(targets) if targets else 0.0

                for symbol in list(holdings):
                    price = execution_prices.get(symbol)
                    desired = (
                        int(target_value / price / spec.lot_size) * spec.lot_size
                        if symbol in targets and price
                        else 0
                    )
                    quantity = int(holdings[symbol]["shares"] - desired)
                    if quantity <= 0:
                        continue
                    reason = ""
                    if price is None:
                        reason = "suspended_or_missing_bar"
                    elif holdings[symbol].get("last_buy_date") == current_date:
                        reason = "t_plus_one"
                    else:
                        _, limit_down = self._limit_state(day.loc[symbol])
                        if limit_down:
                            reason = "one_price_limit_down"
                    if reason:
                        blocked_reasons[reason] += 1
                        trade_rows.append(
                            {
                                "signal_date": order["signal_date"],
                                "date": current_date,
                                "symbol": symbol,
                                "side": "sell",
                                "status": "blocked",
                                "reason": reason,
                                "shares": quantity,
                                "price": price,
                                "notional": 0.0,
                                "cost": 0.0,
                            }
                        )
                        continue
                    notional = quantity * price
                    cost = notional * (commission_rate + slippage_rate + stamp_rate)
                    cash += notional - cost
                    holdings[symbol]["shares"] -= quantity
                    if holdings[symbol]["shares"] <= 0:
                        del holdings[symbol]
                    total_costs += cost
                    day_turnover += notional
                    trade_rows.append(
                        {
                            "signal_date": order["signal_date"],
                            "date": current_date,
                            "symbol": symbol,
                            "side": "sell",
                            "status": "executed",
                            "reason": "",
                            "shares": quantity,
                            "price": price,
                            "notional": notional,
                            "cost": cost,
                        }
                    )

                for symbol in order["symbols"]:
                    price = execution_prices.get(symbol)
                    current_shares = int(holdings.get(symbol, {}).get("shares", 0))
                    reference_price = price or last_prices.get(symbol)
                    desired = (
                        int(target_value / reference_price / spec.lot_size) * spec.lot_size
                        if reference_price
                        else spec.lot_size
                    )
                    quantity = desired - current_shares
                    if quantity <= 0:
                        continue
                    reason = ""
                    if price is None:
                        reason = "suspended_or_missing_bar"
                    else:
                        limit_up, _ = self._limit_state(day.loc[symbol])
                        if limit_up:
                            reason = "one_price_limit_up"
                    if reason:
                        blocked_reasons[reason] += 1
                        trade_rows.append(
                            {
                                "signal_date": order["signal_date"],
                                "date": current_date,
                                "symbol": symbol,
                                "side": "buy",
                                "status": "blocked",
                                "reason": reason,
                                "shares": quantity,
                                "price": price,
                                "notional": 0.0,
                                "cost": 0.0,
                            }
                        )
                        continue
                    affordable = (
                        int(
                            cash / (price * (1.0 + commission_rate + slippage_rate)) / spec.lot_size
                        )
                        * spec.lot_size
                    )
                    quantity = min(quantity, affordable)
                    if quantity <= 0:
                        blocked_reasons["insufficient_cash_or_round_lot"] += 1
                        continue
                    notional = quantity * price
                    cost = notional * (commission_rate + slippage_rate)
                    cash -= notional + cost
                    item = holdings.setdefault(symbol, {"shares": 0})
                    item["shares"] += quantity
                    item["last_buy_date"] = current_date
                    item["factor_value"] = order["factor_values"][symbol]
                    item["report_period_end"] = order["report_periods"][symbol]
                    total_costs += cost
                    day_turnover += notional
                    trade_rows.append(
                        {
                            "signal_date": order["signal_date"],
                            "date": current_date,
                            "symbol": symbol,
                            "side": "buy",
                            "status": "executed",
                            "reason": "",
                            "shares": quantity,
                            "price": price,
                            "notional": notional,
                            "cost": cost,
                        }
                    )

            if not day.empty:
                for symbol, row in day.iterrows():
                    last_prices[str(symbol)] = float(row["close"])
            market_value = sum(
                item["shares"] * last_prices.get(symbol, 0.0) for symbol, item in holdings.items()
            )
            portfolio_value = cash + market_value
            for symbol, item in holdings.items():
                value = item["shares"] * last_prices.get(symbol, 0.0)
                position_rows.append(
                    {
                        "date": current_date,
                        "symbol": symbol,
                        "shares": item["shares"],
                        "close": last_prices.get(symbol),
                        "market_value": value,
                        "weight": value / portfolio_value if portfolio_value > 0 else 0.0,
                        "factor_value": item.get("factor_value"),
                        "report_period_end": item.get("report_period_end"),
                    }
                )
            nav_rows.append(
                {
                    "date": current_date,
                    "portfolio_value": portfolio_value,
                    "portfolio_nav": portfolio_value / spec.initial_capital,
                    "benchmark_nav": float(benchmark_by_date.loc[current_date]) / first_benchmark,
                    "cash": cash,
                    "market_value": market_value,
                    "turnover_notional": day_turnover,
                    "cumulative_cost": total_costs,
                }
            )

            if current_date in rebalance_dates:
                index = date_index[current_date]
                if index + 1 >= len(calendar_dates):
                    continue
                execution_date = calendar_dates[index + 1]
                cutoff = pd.Timestamp(current_date).replace(hour=15)
                available = set(day.index) if not day.empty else set()
                factors = self.eligible_snapshot(spec, cutoff, available)
                selected = factors.loc[factors["selected"]]
                for row in factors.itertuples(index=False):
                    selection_rows.append(
                        {
                            "signal_date": current_date,
                            "execution_date": execution_date,
                            "symbol": row.symbol,
                            "factor": spec.factor,
                            "factor_value": row.factor_value,
                            "rank": row.rank,
                            "selected": row.selected,
                            "report_period_end": row.report_period_end,
                            "report_type": row.report_type,
                            "published_at": row.published_at,
                            "availability_source": row.availability_source,
                            "source_batch_id": row.source_batch_id,
                            "component_values": getattr(row, "component_values", ""),
                            "industry_name": getattr(row, "industry_name", None),
                            "industry_history_mode": getattr(row, "history_mode", None),
                            "raw_factor_value": getattr(row, "raw_factor_value", row.factor_value),
                        }
                    )
                pending[execution_date] = {
                    "signal_date": current_date,
                    "symbols": selected["symbol"].tolist(),
                    "factor_values": dict(
                        zip(selected["symbol"], selected["factor_value"], strict=True)
                    ),
                    "report_periods": {
                        row.symbol: pd.Timestamp(row.report_period_end).date().isoformat()
                        for row in selected.itertuples(index=False)
                    },
                }
            current_progress = 15 + int(day_number / len(calendar_dates) * 70)
            if current_progress >= last_progress + 5:
                last_progress = current_progress
                progress(current_progress, f"正在回放交易日 {day_number}/{len(calendar_dates)}")

        nav = pd.DataFrame(nav_rows)
        nav["daily_return"] = nav["portfolio_nav"].pct_change().fillna(0.0)
        nav["benchmark_daily_return"] = nav["benchmark_nav"].pct_change().fillna(0.0)
        nav["drawdown"] = nav["portfolio_nav"] / nav["portfolio_nav"].cummax() - 1.0
        trades = pd.DataFrame(
            trade_rows,
            columns=[
                "signal_date",
                "date",
                "symbol",
                "side",
                "status",
                "reason",
                "shares",
                "price",
                "notional",
                "cost",
            ],
        )
        selections = pd.DataFrame(selection_rows)
        positions = pd.DataFrame(position_rows)
        metrics = self._performance(nav)
        metrics.update(
            {
                "trade_count": int((trades["status"] == "executed").sum()),
                "blocked_order_count": int((trades["status"] == "blocked").sum()),
                "total_transaction_cost": total_costs,
                "rebalance_count": len(rebalance_dates),
            }
        )
        diagnostics = {
            "point_in_time_rule": "published_at strictly before signal-day 15:00",
            "factor_combination": [item.to_dict() for item in spec.factors],
            "missing_factor_policy": spec.missing_policy,
            "execution_rule": "next benchmark trading day open",
            "universe_mode": spec.universe_mode,
            "universe_benchmark": spec.universe_benchmark,
            "industry_neutralization": spec.industry_neutralization,
            "classification_system": spec.classification_system,
            "max_industry_weight": spec.max_industry_weight,
            "require_verified_industry": spec.require_verified_industry,
            "survivorship_bias_warning": None
            if spec.universe_mode == "historical_index"
            else "The requested universe does not prove historical index membership and may have survivorship bias.",
            "local_sources": [
                "published_daily_bar",
                "published_financial_report",
                "published_financial_metric",
                "published_benchmark_daily_bar",
            ],
            "blocked_order_reasons": dict(sorted(blocked_reasons.items())),
            "constraints": {
                "t_plus_one": True,
                "round_lot": spec.lot_size,
                "one_price_limit_detection": True,
                "suspension_detection": "missing bar or zero volume",
            },
        }
        progress(90, "正在生成研究报告")
        artifacts = self.writer.write(
            run_dir,
            spec=spec.to_dict(),
            metrics=metrics,
            nav=nav,
            trades=trades,
            positions=positions,
            selections=selections,
            diagnostics=diagnostics,
        )
        completed_at = datetime.now(UTC)
        payload = {
            "run_id": run_id,
            "status": "succeeded",
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_seconds": (completed_at - started_at).total_seconds(),
            "spec": spec.to_dict(),
            "metrics": metrics,
            "diagnostics": diagnostics,
            "artifacts": {name: str(path) for name, path in artifacts.items()},
        }
        manifest = run_dir / "run.json"
        payload["artifacts"]["run_manifest"] = str(manifest)
        write_json(manifest, payload)
        from ai_quant_assistant.storage.research_run_index import register_run
        register_run(self.database, "factor_backtest", payload)
        progress(100, "回测完成")
        return payload
