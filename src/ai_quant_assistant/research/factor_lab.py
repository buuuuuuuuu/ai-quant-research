from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd

from ai_quant_assistant.domain.models import FactorBacktestSpec, FactorComponent, FactorLabSpec
from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS
from ai_quant_assistant.reports.factor_lab_writer import FactorLabReportWriter
from ai_quant_assistant.research.factor_backtest import FactorBacktestService
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.snapshots import write_json

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[int, str], None]


def _finite(value: Any) -> float | None:
    if value is None or pd.isna(value) or not np.isfinite(float(value)):
        return None
    return float(value)


class FactorLabService:
    def __init__(self, *, database: LocalDatabase, artifact_dir: Path) -> None:
        self.database = database
        self.artifact_dir = artifact_dir
        self.backtests = FactorBacktestService(database=database, artifact_dir=artifact_dir)
        self.writer = FactorLabReportWriter()

    @staticmethod
    def _run_id() -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"factor-lab-{stamp}-{uuid4().hex[:8]}"

    @staticmethod
    def _strategy_label(components: tuple[FactorComponent, ...]) -> str:
        if len(components) == 1:
            return components[0].code
        return "composite:" + "+".join(item.code for item in components)

    def _backtest_spec(
        self,
        spec: FactorLabSpec,
        components: tuple[FactorComponent, ...],
        *,
        top_n: int,
        rebalance: str,
        commission_bps: float,
    ) -> FactorBacktestSpec:
        composite = len(components) > 1
        return FactorBacktestSpec(
            symbols=spec.symbols,
            start_date=spec.start_date,
            end_date=spec.end_date,
            factor="composite" if composite else components[0].code,
            factor_direction="desc" if composite else components[0].direction,
            factors=components if composite else (),
            missing_policy=spec.missing_policy,
            top_n=top_n,
            rebalance=rebalance,
            benchmark_symbol=spec.benchmark_symbol,
            adjustment=spec.adjustment,
            initial_capital=spec.initial_capital,
            commission_bps=commission_bps,
            slippage_bps=spec.slippage_bps,
            stamp_duty_bps=spec.stamp_duty_bps,
            lot_size=spec.lot_size,
        )

    def _cross_sections(
        self, spec: FactorLabSpec, benchmark: pd.DataFrame, prices: pd.DataFrame
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        dates = pd.to_datetime(benchmark["date"]).dt.normalize().tolist()
        signals = self.backtests._rebalance_dates(pd.Series(dates), spec.rebalance_values[0])
        date_indexes = {day: index for index, day in enumerate(dates)}
        by_day = {
            day: frame.set_index("symbol", drop=False)
            for day, frame in prices.groupby("date", sort=True)
        }
        backtest_spec = self._backtest_spec(
            spec,
            spec.factors,
            top_n=spec.top_n_values[0],
            rebalance=spec.rebalance_values[0],
            commission_bps=spec.commission_bps_values[0],
        )
        cross_rows: list[dict[str, Any]] = []
        coverage_rows: list[dict[str, Any]] = []
        previous_selected: set[str] = set()
        for signal_index, signal_day in enumerate(signals):
            index = date_indexes[signal_day]
            if index + 1 >= len(dates):
                continue
            entry_day = dates[index + 1]
            if signal_index + 1 < len(signals):
                following = date_indexes[signals[signal_index + 1]]
                exit_day = dates[min(following + 1, len(dates) - 1)]
            else:
                exit_day = dates[-1]
            if exit_day <= entry_day:
                continue
            day_bars = by_day.get(signal_day, pd.DataFrame())
            entry_bars = by_day.get(entry_day, pd.DataFrame())
            exit_bars = by_day.get(exit_day, pd.DataFrame())
            active = set(day_bars.index)
            cutoff = pd.Timestamp(signal_day).replace(hour=15)
            composite = self.backtests.factor_snapshot(backtest_spec, cutoff)
            composite = composite.loc[composite["symbol"].isin(active)].copy()
            snapshots = {
                item.code: self.database.read_factor_snapshot(spec.symbols, item.code, cutoff)
                for item in spec.factors
            }
            factor_maps = {
                code: frame.set_index("symbol")["factor_value"].to_dict()
                for code, frame in snapshots.items()
            }
            for code, frame in snapshots.items():
                count = int(frame["symbol"].isin(active).sum())
                coverage_rows.append(
                    {
                        "signal_date": signal_day,
                        "factor": code,
                        "eligible_count": len(active),
                        "covered_count": count,
                        "coverage": count / len(active) if active else 0.0,
                    }
                )
            ranked = composite.sort_values(["factor_value", "symbol"], ascending=[False, True])
            selected = set(ranked.head(spec.top_n_values[0])["symbol"])
            union = previous_selected | selected
            turnover = len(previous_selected ^ selected) / len(union) if union else 0.0
            previous_selected = selected
            for row in composite.itertuples(index=False):
                if row.symbol not in entry_bars.index or row.symbol not in exit_bars.index:
                    continue
                entry = entry_bars.loc[row.symbol]
                exit_row = exit_bars.loc[row.symbol]
                if (
                    float(entry.volume) <= 0
                    or float(exit_row.volume) <= 0
                    or float(entry.open) <= 0
                ):
                    continue
                record: dict[str, Any] = {
                    "signal_date": signal_day,
                    "entry_date": entry_day,
                    "exit_date": exit_day,
                    "symbol": row.symbol,
                    "composite_score": float(row.factor_value),
                    "forward_return": float(exit_row.open) / float(entry.open) - 1.0,
                    "published_at": row.published_at,
                    "report_period_end": row.report_period_end,
                    "selected": row.symbol in selected,
                    "selection_turnover": turnover,
                }
                for component in spec.factors:
                    record[component.code] = _finite(factor_maps[component.code].get(row.symbol))
                cross_rows.append(record)
        cross_sections = pd.DataFrame(cross_rows)
        coverage = pd.DataFrame(coverage_rows)
        ic_rows: list[dict[str, Any]] = []
        group_rows: list[dict[str, Any]] = []
        if not cross_sections.empty:
            for signal_day, frame in cross_sections.groupby("signal_date", sort=True):
                factor_names = [item.code for item in spec.factors]
                if len(spec.factors) > 1:
                    factor_names.append("composite_score")
                for name in factor_names:
                    valid = frame[[name, "forward_return"]].dropna()
                    if len(valid) < 2 or valid[name].nunique() < 2:
                        ic = rank_ic = None
                    else:
                        ic = _finite(valid[name].corr(valid["forward_return"], method="pearson"))
                        rank_ic = _finite(
                            valid[name]
                            .rank()
                            .corr(valid["forward_return"].rank(), method="pearson")
                        )
                    direction = next(
                        (item.direction for item in spec.factors if item.code == name), "desc"
                    )
                    orientation = 1.0 if direction == "desc" else -1.0
                    ic_rows.append(
                        {
                            "signal_date": signal_day,
                            "factor": name,
                            "sample_size": len(valid),
                            "ic": None if ic is None else ic * orientation,
                            "rank_ic": None if rank_ic is None else rank_ic * orientation,
                        }
                    )
                    if len(valid) < 2:
                        continue
                    ordered = valid.assign(_score=valid[name] * orientation).sort_values("_score")
                    count = min(spec.quantiles, len(ordered))
                    ordered["group"] = (
                        np.floor(np.arange(len(ordered)) * count / len(ordered)).astype(int) + 1
                    )
                    for group, members in ordered.groupby("group", sort=True):
                        group_rows.append(
                            {
                                "signal_date": signal_day,
                                "factor": name,
                                "group": int(group),
                                "group_count": count,
                                "stock_count": len(members),
                                "mean_forward_return": float(members["forward_return"].mean()),
                            }
                        )
        return cross_sections, pd.DataFrame(ic_rows), pd.DataFrame(group_rows), coverage

    @staticmethod
    def _segments(dates: list[pd.Timestamp], spec: FactorLabSpec) -> list[dict[str, Any]]:
        if len(dates) < 6:
            raise ValueError("factor lab requires at least six benchmark trading days")
        train_end = max(1, min(int(len(dates) * spec.training_fraction), len(dates) - 2))
        valid_end = max(
            train_end + 1,
            min(
                int(len(dates) * (spec.training_fraction + spec.validation_fraction)),
                len(dates) - 1,
            ),
        )
        boundaries = (
            ("train", 0, train_end),
            ("validation", train_end, valid_end),
            ("test", valid_end, len(dates)),
        )
        return [
            {
                "segment": name,
                "start_date": dates[start].date().isoformat(),
                "end_date": dates[end - 1].date().isoformat(),
                "trading_days": end - start,
            }
            for name, start, end in boundaries
        ]

    @staticmethod
    def _segment_metrics(nav: pd.DataFrame, segment: dict[str, Any]) -> dict[str, Any]:
        dates = pd.to_datetime(nav["date"])
        frame = nav.loc[(dates >= segment["start_date"]) & (dates <= segment["end_date"])].copy()
        if frame.empty:
            return {
                "total_return": None,
                "benchmark_return": None,
                "excess_return": None,
                "sharpe_ratio": None,
                "max_drawdown": None,
            }
        strategy = pd.to_numeric(frame["portfolio_nav"])
        benchmark = pd.to_numeric(frame["benchmark_nav"])
        normalized = strategy / float(strategy.iloc[0])
        returns = normalized.pct_change().dropna()
        volatility = float(returns.std(ddof=1) * np.sqrt(252)) if len(returns) > 1 else 0.0
        total = float(normalized.iloc[-1] - 1.0)
        annualized = (
            float((1.0 + total) ** (252 / max(len(frame) - 1, 1)) - 1.0) if total > -1 else -1.0
        )
        benchmark_return = float(benchmark.iloc[-1] / benchmark.iloc[0] - 1.0)
        return {
            "total_return": total,
            "benchmark_return": benchmark_return,
            "excess_return": total - benchmark_return,
            "sharpe_ratio": annualized / volatility if volatility > 0 else 0.0,
            "max_drawdown": abs(float((normalized / normalized.cummax() - 1.0).min())),
        }

    @staticmethod
    def _walk_forward(nav: pd.DataFrame, folds: int, strategy: str) -> list[dict[str, Any]]:
        boundaries = np.linspace(0, len(nav), folds + 2, dtype=int)
        rows = []
        for fold in range(1, len(boundaries) - 1):
            train = nav.iloc[: boundaries[fold]]
            test = nav.iloc[boundaries[fold] : boundaries[fold + 1]]
            if len(train) < 2 or len(test) < 2:
                continue
            rows.append(
                {
                    "strategy": strategy,
                    "fold": fold,
                    "train_start": str(train["date"].iloc[0])[:10],
                    "train_end": str(train["date"].iloc[-1])[:10],
                    "test_start": str(test["date"].iloc[0])[:10],
                    "test_end": str(test["date"].iloc[-1])[:10],
                    "train_return": float(
                        train["portfolio_nav"].iloc[-1] / train["portfolio_nav"].iloc[0] - 1
                    ),
                    "test_return": float(
                        test["portfolio_nav"].iloc[-1] / test["portfolio_nav"].iloc[0] - 1
                    ),
                }
            )
        return rows

    def run(
        self, spec: FactorLabSpec, progress_callback: ProgressCallback | None = None
    ) -> dict[str, Any]:
        def progress(value: int, message: str) -> None:
            if progress_callback:
                progress_callback(value, message)

        unsupported = [item.code for item in spec.factors if item.code not in FINANCIAL_METRICS]
        if unsupported:
            raise ValueError(f"unsupported factors: {', '.join(unsupported)}")
        if self.database.status().get("schema_version", 0) < 4:
            raise ValueError("factor lab requires an initialized Schema v4 local database")
        strategies = [(item,) for item in spec.factors]
        if len(spec.factors) > 1:
            strategies.append(spec.factors)
        combinations = list(
            product(
                strategies, spec.top_n_values, spec.rebalance_values, spec.commission_bps_values
            )
        )
        if len(combinations) > spec.max_combinations:
            raise ValueError(
                f"parameter grid contains {len(combinations)} combinations, exceeding max_combinations={spec.max_combinations}"
            )
        progress(3, "正在准备 Point-in-Time 因子截面")
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
        dates = pd.to_datetime(benchmark["date"]).dt.normalize().tolist()
        segments = self._segments(dates, spec)
        cross_sections, ic_series, group_returns, coverage = self._cross_sections(
            spec, benchmark, prices
        )
        if cross_sections.empty:
            raise ValueError(
                "no point-in-time factor cross-sections with observable forward returns"
            )
        progress(15, "因子截面、IC 与分组收益已计算")
        started = datetime.now(UTC)
        run_id = self._run_id()
        directory = self.artifact_dir / "factor-lab" / run_id
        directory.mkdir(parents=True, exist_ok=False)
        strategy_rows = []
        segment_rows = []
        walk_rows = []
        curve_rows = []
        for index, (components, top_n, rebalance, cost) in enumerate(combinations, start=1):
            label = self._strategy_label(components)
            name = f"{label}|top={top_n}|{rebalance}|commission={cost:g}"
            progress(
                15 + int((index - 1) / len(combinations) * 72),
                f"参数扫描 {index}/{len(combinations)}：{name}",
            )
            result = self.backtests.run(
                self._backtest_spec(
                    spec, components, top_n=top_n, rebalance=rebalance, commission_bps=cost
                )
            )
            nav = pd.read_csv(result["artifacts"]["daily_nav_csv"])
            metrics = result["metrics"]
            strategy_rows.append(
                {
                    "strategy": name,
                    "factor": label,
                    "top_n": top_n,
                    "rebalance": rebalance,
                    "commission_bps": cost,
                    "run_id": result["run_id"],
                    **{
                        key: metrics.get(key)
                        for key in (
                            "total_return",
                            "annualized_return",
                            "excess_return",
                            "max_drawdown",
                            "sharpe_ratio",
                            "trade_count",
                            "total_transaction_cost",
                        )
                    },
                }
            )
            for segment in segments:
                segment_rows.append(
                    {"strategy": name, **segment, **self._segment_metrics(nav, segment)}
                )
            walk_rows.extend(self._walk_forward(nav, spec.walk_forward_folds, name))
            for row in nav.itertuples(index=False):
                curve_rows.append(
                    {
                        "date": row.date,
                        "strategy": name,
                        "portfolio_nav": float(row.portfolio_nav),
                        "benchmark_nav": float(row.benchmark_nav),
                    }
                )
        strategies_frame = pd.DataFrame(strategy_rows)
        segment_frame = pd.DataFrame(segment_rows)
        validation = segment_frame.loc[segment_frame["segment"] == "validation"].sort_values(
            ["sharpe_ratio", "excess_return", "strategy"],
            ascending=[False, False, True],
            na_position="last",
        )
        selected = (
            validation.iloc[0]["strategy"]
            if not validation.empty
            else strategies_frame.iloc[0]["strategy"]
        )
        strategies_frame["selected_by_validation"] = strategies_frame["strategy"] == selected
        segment_frame["selected_by_validation"] = segment_frame["strategy"] == selected
        progress(90, "正在导出因子实验报告")
        ic_summary = self._ic_summary(ic_series)
        yearly = self._yearly_summary(ic_series)
        diagnostics = {
            "point_in_time_rule": "published_at strictly before signal-day 15:00",
            "forward_return_rule": "next trading day open to next rebalance execution open",
            "model_selection_rule": "validation-period Sharpe ratio, then validation excess return",
            "test_period_used_for_selection": False,
            "universe_mode": "explicit_symbols_not_historical_index_constituents",
            "survivorship_bias_warning": "The requested symbols are an explicit research universe, not historical index members.",
            "missing_factor_policy": spec.missing_policy,
            "combinations": len(combinations),
            "segments": segments,
        }
        artifacts = self.writer.write(
            directory,
            spec=spec.to_dict(),
            strategies=strategies_frame,
            segments=segment_frame,
            cross_sections=cross_sections,
            ic_series=ic_series,
            ic_summary=ic_summary,
            group_returns=group_returns,
            coverage=coverage,
            yearly=yearly,
            walk_forward=pd.DataFrame(walk_rows),
            curves=pd.DataFrame(curve_rows),
            diagnostics=diagnostics,
        )
        completed = datetime.now(UTC)
        payload = {
            "run_id": run_id,
            "status": "succeeded",
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "duration_seconds": (completed - started).total_seconds(),
            "spec": spec.to_dict(),
            "summary": {
                "factor_count": len(spec.factors),
                "cross_section_count": int(cross_sections["signal_date"].nunique()),
                "strategy_count": len(strategies_frame),
                "selected_strategy": selected,
                "mean_selection_turnover": _finite(
                    cross_sections.groupby("signal_date")["selection_turnover"].first().mean()
                ),
            },
            "ic_summary": ic_summary.astype(object)
            .where(pd.notna(ic_summary), None)
            .to_dict(orient="records"),
            "strategies": strategies_frame.astype(object)
            .where(pd.notna(strategies_frame), None)
            .to_dict(orient="records"),
            "validation": segment_frame.loc[segment_frame["selected_by_validation"]]
            .astype(object)
            .where(pd.notna(segment_frame.loc[segment_frame["selected_by_validation"]]), None)
            .to_dict(orient="records"),
            "diagnostics": diagnostics,
            "artifacts": {name: str(path) for name, path in artifacts.items()},
        }
        manifest = directory / "run.json"
        payload["artifacts"]["run_manifest"] = str(manifest)
        write_json(manifest, payload)
        progress(100, "因子研究完成")
        return payload

    @staticmethod
    def _ic_summary(frame: pd.DataFrame) -> pd.DataFrame:
        rows = []
        if frame.empty:
            return pd.DataFrame(
                columns=[
                    "factor",
                    "periods",
                    "mean_ic",
                    "mean_rank_ic",
                    "ic_ir",
                    "positive_ic_ratio",
                ]
            )
        for factor, group in frame.groupby("factor", sort=True):
            values = pd.to_numeric(group["ic"], errors="coerce").dropna()
            deviation = float(values.std(ddof=1)) if len(values) > 1 else 0.0
            rows.append(
                {
                    "factor": factor,
                    "periods": len(values),
                    "mean_ic": _finite(values.mean()) if len(values) else None,
                    "mean_rank_ic": _finite(
                        pd.to_numeric(group["rank_ic"], errors="coerce").mean()
                    ),
                    "ic_ir": _finite(values.mean() / deviation) if deviation > 0 else None,
                    "positive_ic_ratio": float((values > 0).mean()) if len(values) else None,
                }
            )
        return pd.DataFrame(rows)

    @staticmethod
    def _yearly_summary(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame(columns=["factor", "year", "periods", "mean_ic", "mean_rank_ic"])
        prepared = frame.copy()
        prepared["year"] = pd.to_datetime(prepared["signal_date"]).dt.year
        return (
            prepared.groupby(["factor", "year"], sort=True)
            .agg(
                periods=("signal_date", "size"),
                mean_ic=("ic", "mean"),
                mean_rank_ic=("rank_ic", "mean"),
            )
            .reset_index()
        )
