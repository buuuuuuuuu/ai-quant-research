from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd

from ai_quant_assistant.domain.models import DailySignalRule, DailySignalStrategySpec
from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS
from ai_quant_assistant.reports.daily_signal_writer import (
    DailySignalReportWriter,
    daily_signal_trust_summary,
)
from ai_quant_assistant.research.daily_signal_audit import (
    EXECUTION_RULE_VERSION,
    build_account_audit,
    build_indicator_series,
    build_reproducibility,
)
from ai_quant_assistant.storage.research_run_index import register_run
from ai_quant_assistant.storage.snapshots import write_json

ProgressCallback = Callable[[int, str], None]


def technical_factor_window(code: str) -> int | None:
    for prefix in ("return_", "ma_deviation_"):
        if code.startswith(prefix) and code[len(prefix) :].isdigit():
            return int(code[len(prefix) :])
    return None


class DailySignalStrategyService:
    def __init__(self, *, database, artifact_dir: Path) -> None:
        self.database = database
        self.artifact_dir = artifact_dir
        self.writer = DailySignalReportWriter()

    @staticmethod
    def _run_id() -> str:
        return f"daily-signal-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"

    @staticmethod
    def _period(day: pd.Timestamp, frequency: str) -> str:
        if frequency == "daily":
            return day.date().isoformat()
        return str(day.to_period("M" if frequency == "monthly" else "Q"))

    @staticmethod
    def _limit(symbol: str) -> float:
        if symbol.startswith(("4", "8", "92")):
            return 0.30
        if symbol.startswith(("300", "301", "688")):
            return 0.20
        return 0.10

    @classmethod
    def _opening_block(cls, symbol: str, side: str, row: pd.Series) -> str:
        previous = row.get("previous_execution_close")
        price = row.get("execution_open")
        if pd.isna(price) or float(price) <= 0 or pd.isna(previous) or float(previous) <= 0:
            return "missing_raw_open_or_previous_close"
        limit = cls._limit(symbol)
        ratio = float(price) / float(previous) - 1
        tolerance = 0.002
        if side == "buy" and ratio >= limit - tolerance:
            return "opening_at_upper_price_limit"
        if side == "sell" and ratio <= -limit + tolerance:
            return "opening_at_lower_price_limit"
        return ""

    @staticmethod
    def _rule_values(
        rule: DailySignalRule, frame: pd.DataFrame, day: pd.Timestamp
    ) -> tuple[bool | None, dict[str, Any]]:
        current = frame.loc[day] if day in frame.index else None
        before = frame.shift(1).loc[day] if day in frame.index else None
        if current is None:
            return None, {"reason": "missing_bar"}
        if rule.kind == "close_vs_ma":
            left, right = current["signal_close"], current[f"ma_{rule.window}"]
            evidence = {"close": left, f"ma_{rule.window}": right}
            if pd.isna(left) or pd.isna(right):
                return None, evidence
            return bool(left > right if rule.operator == "above" else left < right), evidence
        if rule.kind in {"ma_compare", "ma_cross"}:
            short, long = f"ma_{rule.short_window}", f"ma_{rule.long_window}"
            evidence = {short: current[short], long: current[long]}
            if pd.isna(current[short]) or pd.isna(current[long]):
                return None, evidence
            if rule.kind == "ma_compare":
                return bool(
                    current[short] > current[long]
                    if rule.operator == "above"
                    else current[short] < current[long]
                ), evidence
            evidence.update({f"previous_{short}": before[short], f"previous_{long}": before[long]})
            if pd.isna(before[short]) or pd.isna(before[long]):
                return None, evidence
            if rule.operator == "cross_above":
                return bool(
                    before[short] <= before[long] and current[short] > current[long]
                ), evidence
            return bool(before[short] >= before[long] and current[short] < current[long]), evidence
        value = current[f"return_{rule.window}"]
        evidence = {f"return_{rule.window}": value, "threshold": rule.threshold}
        if pd.isna(value):
            return None, evidence
        return bool(
            value > rule.threshold if rule.operator == "above" else value < rule.threshold
        ), evidence

    @classmethod
    def _rules(
        cls, rules, mode: str, frame: pd.DataFrame, day: pd.Timestamp
    ) -> tuple[bool, list[dict[str, Any]]]:
        values, evidence = [], []
        for rule in rules:
            matched, details = cls._rule_values(rule, frame, day)
            values.append(matched)
            evidence.append({"rule": rule.to_dict(), "matched": matched, "values": details})
        # Missing input never becomes a matching rule, including OR groups.
        decided = [value is not None and bool(value) for value in values]
        return (all(decided) if mode == "all" else any(decided)), evidence

    @staticmethod
    def _features(
        prices: pd.DataFrame, calendar: pd.DatetimeIndex, windows: set[int]
    ) -> dict[str, pd.DataFrame]:
        result = {}
        for symbol, rows in prices.groupby("symbol"):
            frame = rows.set_index("date").reindex(calendar)
            close = pd.to_numeric(frame["signal_close"], errors="coerce")
            for window in windows:
                frame[f"ma_{window}"] = close.rolling(window, min_periods=window).mean()
                frame[f"return_{window}"] = close.div(close.shift(window)).sub(1)
                frame[f"ma_deviation_{window}"] = close.div(frame[f"ma_{window}"]).sub(1)
            result[symbol] = frame
        return result

    def _candidate_snapshot(self, spec, day, features) -> pd.DataFrame:
        if not spec.candidate_factors:
            # A frozen single-stock scope needs no cross-sectional ranking. Keep
            # score and rank absent rather than inventing a numerical factor.
            return pd.DataFrame(
                [{"symbol": spec.symbols[0], "composite_score": None, "rank": None,
                  "selected": True, "component_sources": "{}"}]
            )
        values: dict[str, pd.Series] = {}
        sources: dict[str, pd.Series] = {}
        for component in spec.candidate_factors:
            if component.code in FINANCIAL_METRICS:
                current = self.database.read_factor_snapshot(
                    spec.symbols, component.code, f"{day.date()} 15:00:00"
                )
                values[component.code] = (
                    current.set_index("symbol")["factor_value"]
                    if not current.empty
                    else pd.Series(dtype=float)
                )
                sources[component.code] = (
                    current.set_index("symbol")["source_batch_id"]
                    if not current.empty
                    else pd.Series(dtype=str)
                )
            else:
                values[component.code] = pd.Series(
                    {symbol: frame.at[day, component.code] for symbol, frame in features.items()}
                )
        # Every strategy rule depends on a price series, even when candidates
        # are ranked only by financial factors.
        raw = pd.DataFrame(index=sorted(features))
        scores, weights = pd.DataFrame(index=raw.index), pd.DataFrame(index=raw.index)
        for component in spec.candidate_factors:
            raw[component.code] = pd.to_numeric(values[component.code], errors="coerce")
            scores[component.code] = (
                raw[component.code].rank(
                    pct=True, method="average", ascending=component.direction == "desc"
                )
                * component.weight
            )
            weights[component.code] = raw[component.code].notna().astype(float) * component.weight
        if spec.missing_policy == "exclude":
            raw = raw.dropna()
            scores, weights = scores.loc[raw.index], weights.loc[raw.index]
        total = weights.sum(axis=1)
        raw["composite_score"] = scores.fillna(0).sum(axis=1).div(total.replace(0, np.nan))
        raw = raw.dropna(subset=["composite_score"]).sort_values(
            ["composite_score"], ascending=False
        )
        raw["rank"] = range(1, len(raw) + 1)
        raw["selected"] = raw["rank"] <= spec.candidate_count
        raw["component_sources"] = [
            json.dumps(
                {
                    code: source.get(symbol)
                    for code, source in sources.items()
                    if pd.notna(source.get(symbol))
                },
                ensure_ascii=False,
            )
            for symbol in raw.index
        ]
        raw.index.name = "symbol"
        return raw.reset_index()

    def run(
        self, spec: DailySignalStrategySpec, progress_callback: ProgressCallback | None = None
    ) -> dict[str, Any]:
        started = datetime.now(UTC)
        run_id = self._run_id()

        def progress(value, message):
            if progress_callback:
                progress_callback(value, message)

        progress(2, "正在准备日线与 Point-in-Time 财务数据")
        unsupported = [
            x.code
            for x in spec.candidate_factors
            if x.code not in FINANCIAL_METRICS and technical_factor_window(x.code) is None
        ]
        if unsupported:
            raise ValueError("unsupported candidate factors: " + ", ".join(unsupported))
        benchmark = self.database.read_benchmark_prices(
            spec.benchmark_symbol, date(1990, 1, 1), spec.end_date
        )
        if benchmark.empty:
            raise ValueError("no local benchmark calendar covers the requested interval")
        benchmark = benchmark.drop_duplicates("date").sort_values("date")
        full_calendar = pd.DatetimeIndex(pd.to_datetime(benchmark["date"]).dt.normalize())
        research_days = full_calendar[
            (full_calendar >= pd.Timestamp(spec.start_date))
            & (full_calendar <= pd.Timestamp(spec.end_date))
        ]
        if len(research_days) < 2:
            raise ValueError("daily signal strategy requires at least two benchmark trading days")
        prices = self.database.read_daily_signal_prices(
            spec.symbols, spec.end_date, spec.adjustment
        )
        if prices.empty:
            raise ValueError("no paired adjusted and unadjusted daily bars are available")
        windows = {technical_factor_window(x.code) for x in spec.candidate_factors}
        for rule in (*spec.buy_rules, *spec.sell_rules):
            windows.update(x for x in (rule.window, rule.short_window, rule.long_window) if x)
        windows.discard(None)
        features = self._features(prices, full_calendar, windows)
        by_day = {day: rows.set_index("symbol") for day, rows in prices.groupby("date")}
        benchmark_close = benchmark.set_index("date")["close"]
        first_benchmark = float(benchmark_close.loc[research_days[0]])
        commission, slippage, stamp = (
            spec.commission_bps / 10000,
            spec.slippage_bps / 10000,
            spec.stamp_duty_bps / 10000,
        )
        cash, total_cost = float(spec.initial_capital), 0.0
        holdings: dict[str, dict[str, Any]] = {}
        pending_exits: dict[str, dict[str, Any]] = {}
        pending_buys: list[dict[str, Any]] = []
        active_buy_signals: set[str] = set()
        last_prices: dict[str, float] = {}
        candidates: set[str] = set()
        candidate_scores: dict[str, float | None] = {}
        candidate_ranks: dict[str, int | None] = {}
        completed_period: str | None = None
        skipped_candidate_updates: list[str] = []
        signals, orders, selections, positions, nav = [], [], [], [], []
        reasons: Counter[str] = Counter()
        signal_sequence = 0

        def signal_identity() -> str:
            nonlocal signal_sequence
            signal_sequence += 1
            return f"signal-{signal_sequence:06d}"

        def record_order(
            order,
            day,
            status,
            reason="",
            price=None,
            shares=0,
            cost=0.0,
            next_action="",
            *,
            cash_before=None,
            cash_after=None,
            position_before=None,
            position_after=None,
            commission_cost=None,
            slippage_cost=None,
            stamp_duty_cost=None,
        ):
            orders.append(
                {
                    "signal_id": order.get("signal_id"),
                    "signal_date": order["signal_date"],
                    "planned_execution_date": order["planned_execution_date"],
                    "execution_date": day,
                    "symbol": order["symbol"],
                    "side": order["side"],
                    "status": status,
                    "shares": shares,
                    "signal_price": order["signal_price"],
                    "execution_price": price,
                    "notional": 0.0 if price is None else price * shares,
                    "cost": cost,
                    "reason": reason,
                    "next_action": next_action,
                    "candidate_score": order.get("candidate_score"),
                    "candidate_rank": order.get("candidate_rank"),
                    "planned_shares": order.get("planned_shares"),
                    "cash_before": cash_before,
                    "cash_after": cash_after,
                    "position_shares_before": position_before,
                    "position_shares_after": position_after,
                    "commission_cost": commission_cost,
                    "slippage_cost": slippage_cost,
                    "stamp_duty_cost": stamp_duty_cost,
                    "retry_continues": next_action == "retry_next_trading_day",
                }
            )

        for index, current in enumerate(research_days):
            day = by_day.get(current, pd.DataFrame())
            sold_today: set[str] = set()
            # Opening: persistent exit intent first.
            for symbol, order in list(pending_exits.items()):
                if current < order["planned_execution_date"]:
                    continue
                row = day.loc[symbol] if symbol in day.index else None
                reason = (
                    "missing_raw_open" if row is None else self._opening_block(symbol, "sell", row)
                )
                if holdings.get(symbol, {}).get("last_buy_date") == current:
                    reason = "t_plus_one"
                if reason:
                    reasons[reason] += 1
                    held_shares = holdings.get(symbol, {}).get("shares", 0)
                    record_order(
                        order,
                        current,
                        "blocked",
                        reason,
                        None if row is None else float(row.execution_open),
                        held_shares,
                        next_action="retry_next_trading_day",
                        cash_before=cash,
                        cash_after=cash,
                        position_before=held_shares,
                        position_after=held_shares,
                    )
                    continue
                quantity, price = holdings[symbol]["shares"], float(row.execution_open)
                cash_before = cash
                position_before = quantity
                notional = quantity * price
                commission_cost = notional * commission
                slippage_cost = notional * slippage
                stamp_duty_cost = notional * stamp
                # Preserve the original M12-A arithmetic expression exactly.
                cost = quantity * price * (commission + slippage + stamp)
                cash += quantity * price - cost
                total_cost += cost
                sold_today.add(symbol)
                record_order(
                    order,
                    current,
                    "executed",
                    price=price,
                    shares=quantity,
                    cost=cost,
                    next_action="closed",
                    cash_before=cash_before,
                    cash_after=cash,
                    position_before=position_before,
                    position_after=0,
                    commission_cost=commission_cost,
                    slippage_cost=slippage_cost,
                    stamp_duty_cost=stamp_duty_cost,
                )
                del holdings[symbol]
                del pending_exits[symbol]
            # Buy intents are one-shot on their exact planned day.
            todays = [order for order in pending_buys if order["planned_execution_date"] == current]
            pending_buys = [
                order for order in pending_buys if order["planned_execution_date"] != current
            ]
            ordered_buys = (
                sorted(todays, key=lambda x: (-x["candidate_score"], x["symbol"]))
                if spec.candidate_factors else sorted(todays, key=lambda x: x["symbol"])
            )
            for order in ordered_buys:
                symbol = order["symbol"]
                row = day.loc[symbol] if symbol in day.index else None
                reason = ""
                if symbol in holdings:
                    reason = "already_held"
                elif symbol in sold_today:
                    reason = "sold_same_execution_batch"
                elif len(holdings) >= spec.max_positions:
                    reason = "max_positions"
                elif row is None:
                    reason = "missing_raw_open"
                else:
                    reason = self._opening_block(symbol, "buy", row)
                if reason:
                    reasons[reason] += 1
                    held_shares = holdings.get(symbol, {}).get("shares", 0)
                    record_order(
                        order,
                        current,
                        "blocked",
                        reason,
                        None if row is None else float(row.execution_open),
                        order["planned_shares"],
                        next_action="expired",
                        cash_before=cash,
                        cash_after=cash,
                        position_before=held_shares,
                        position_after=held_shares,
                    )
                    continue
                price = float(row.execution_open)
                affordable = (
                    int(cash / (price * (1 + commission + slippage)) / spec.lot_size)
                    * spec.lot_size
                )
                quantity = min(order["planned_shares"], affordable)
                if quantity <= 0:
                    reason = "insufficient_cash_or_round_lot"
                    reasons[reason] += 1
                    record_order(
                        order,
                        current,
                        "blocked",
                        reason,
                        price,
                        order["planned_shares"],
                        next_action="expired",
                        cash_before=cash,
                        cash_after=cash,
                        position_before=0,
                        position_after=0,
                    )
                    continue
                cash_before = cash
                notional = quantity * price
                commission_cost = notional * commission
                slippage_cost = notional * slippage
                stamp_duty_cost = 0.0
                # Preserve the original M12-A arithmetic expression exactly.
                cost = quantity * price * (commission + slippage)
                cash -= quantity * price + cost
                total_cost += cost
                holdings[symbol] = {
                    "shares": quantity,
                    "last_buy_date": current,
                    "candidate_score": order["candidate_score"],
                    "candidate_rank": order["candidate_rank"],
                }
                record_order(
                    order,
                    current,
                    "executed",
                    reason=(
                        "reduced_for_opening_gap_and_cash"
                        if quantity < order["planned_shares"]
                        else ""
                    ),
                    price=price,
                    shares=quantity,
                    cost=cost,
                    next_action="held",
                    cash_before=cash_before,
                    cash_after=cash,
                    position_before=0,
                    position_after=quantity,
                    commission_cost=commission_cost,
                    slippage_cost=slippage_cost,
                    stamp_duty_cost=stamp_duty_cost,
                )
            # Closing data and valuation.
            for symbol in spec.symbols:
                if symbol in day.index:
                    last_prices[symbol] = float(day.loc[symbol, "execution_close"])
            value = cash + sum(
                item["shares"] * last_prices.get(symbol, 0) for symbol, item in holdings.items()
            )
            period = self._period(current, spec.candidate_refresh)
            if period != completed_period:
                snapshot = self._candidate_snapshot(spec, current, features)
                if not snapshot.empty:
                    old_candidates = candidates
                    candidates = set(snapshot.loc[snapshot.selected, "symbol"])
                    active_buy_signals.intersection_update(candidates)
                    candidate_scores = dict(
                        zip(snapshot.symbol, snapshot.composite_score, strict=True)
                    )
                    candidate_ranks = dict(zip(snapshot.symbol, snapshot["rank"], strict=True))
                    completed_period = period
                    for row in snapshot.itertuples(index=False):
                        selections.append(
                            {
                                "signal_date": current,
                                "symbol": row.symbol,
                                "composite_score": row.composite_score,
                                "rank": row.rank,
                                "selected": row.selected,
                                "component_values": json.dumps(
                                    {x.code: getattr(row, x.code) for x in spec.candidate_factors},
                                    default=str,
                                    ensure_ascii=False,
                                ),
                                "component_sources": row.component_sources,
                            }
                        )
                    if spec.exit_on_candidate_drop:
                        for symbol in sorted((old_candidates - candidates) & holdings.keys()):
                            if symbol in pending_exits:
                                continue
                            candidate_exit_day = (
                                research_days[index + 1] if index + 1 < len(research_days) else None
                            )
                            order = {
                                "signal_id": signal_identity(),
                                "signal_date": current,
                                "planned_execution_date": candidate_exit_day,
                                "symbol": symbol,
                                "side": "sell",
                                "signal_price": last_prices.get(symbol),
                                "planned_shares": holdings[symbol].get("shares"),
                                "candidate_score": holdings[symbol].get("candidate_score"),
                                "candidate_rank": holdings[symbol].get("candidate_rank"),
                                "rule_evidence": "candidate_drop",
                            }
                            signals.append(
                                {
                                    **order,
                                    "action": "sell",
                                    "status": (
                                        "generated"
                                        if candidate_exit_day is not None
                                        else "end_of_backtest"
                                    ),
                                }
                            )
                            if candidate_exit_day is not None:
                                pending_exits[symbol] = order
                elif not skipped_candidate_updates or skipped_candidate_updates[-1] != period:
                    skipped_candidate_updates.append(period)
            next_day = research_days[index + 1] if index + 1 < len(research_days) else None
            # Existing holdings always receive sell-rule evaluation.
            for symbol in sorted(holdings):
                if symbol in pending_exits:
                    continue
                matched, evidence = self._rules(
                    spec.sell_rules, spec.sell_rule_mode, features[symbol], current
                )
                if matched:
                    order = {
                        "signal_id": signal_identity(),
                        "signal_date": current,
                        "planned_execution_date": next_day,
                        "symbol": symbol,
                        "side": "sell",
                        "signal_price": last_prices.get(symbol),
                        "planned_shares": holdings[symbol].get("shares"),
                        "candidate_score": holdings[symbol].get("candidate_score"),
                        "candidate_rank": holdings[symbol].get("candidate_rank"),
                        "rule_evidence": json.dumps(evidence, default=str, ensure_ascii=False),
                    }
                    signals.append(
                        {
                            **order,
                            "action": "sell",
                            "status": "generated" if next_day is not None else "end_of_backtest",
                        }
                    )
                    if next_day is not None:
                        pending_exits[symbol] = order
            reserved = len(holdings) + len(pending_buys)
            ranked_candidates = (
                sorted(candidates, key=lambda x: (-candidate_scores[x], x))
                if spec.candidate_factors else sorted(candidates)
            )
            for symbol in ranked_candidates:
                matched, evidence = self._rules(
                    spec.buy_rules, spec.buy_rule_mode, features[symbol], current
                )
                if not matched:
                    active_buy_signals.discard(symbol)
                    continue
                if (
                    symbol in active_buy_signals
                    or reserved >= spec.max_positions
                    or symbol in holdings
                    or symbol in pending_exits
                    or any(x["symbol"] == symbol for x in pending_buys)
                ):
                    continue
                signal_price = float(features[symbol].at[current, "signal_close"])
                target_value = value / spec.max_positions
                planned = int(target_value / signal_price / spec.lot_size) * spec.lot_size
                order = {
                    "signal_id": signal_identity(),
                    "signal_date": current,
                    "planned_execution_date": next_day,
                    "symbol": symbol,
                    "side": "buy",
                    "signal_price": signal_price,
                    "candidate_score": candidate_scores[symbol],
                    "candidate_rank": candidate_ranks[symbol],
                    "candidate_eligible": True,
                    "planned_shares": planned,
                    "rule_evidence": json.dumps(evidence, default=str, ensure_ascii=False),
                }
                signals.append(
                    {
                        **order,
                        "action": "buy",
                        "status": "generated" if next_day is not None else "end_of_backtest",
                    }
                )
                active_buy_signals.add(symbol)
                if next_day is not None and planned > 0:
                    pending_buys.append(order)
                    reserved += 1
            for symbol, item in holdings.items():
                price = last_prices.get(symbol)
                positions.append(
                    {
                        "date": current,
                        "symbol": symbol,
                        "shares": item["shares"],
                        "price": price,
                        "market_value": None if price is None else item["shares"] * price,
                        "candidate_score": item["candidate_score"],
                        "candidate_rank": item.get("candidate_rank"),
                        "pending_exit": symbol in pending_exits,
                    }
                )
            benchmark_nav = float(benchmark_close.loc[current]) / first_benchmark
            nav.append(
                {
                    "date": current,
                    "cash": cash,
                    "position_value": value - cash,
                    "portfolio_value": value,
                    "portfolio_nav": value / spec.initial_capital,
                    "benchmark_nav": benchmark_nav,
                    "total_cost": total_cost,
                }
            )
            progress(
                10 + int(80 * (index + 1) / len(research_days)),
                f"日线信号 {index + 1}/{len(research_days)}",
            )
        nav_frame = pd.DataFrame(nav)
        nav_frame["drawdown"] = nav_frame.portfolio_nav.div(nav_frame.portfolio_nav.cummax()).sub(1)
        returns = nav_frame.portfolio_nav.pct_change().dropna()
        total_return = float(nav_frame.portfolio_nav.iloc[-1] - 1)
        annualized = (
            (1 + total_return) ** (252 / max(len(nav_frame) - 1, 1)) - 1
            if total_return > -1
            else -1
        )
        volatility = float(returns.std(ddof=1) * np.sqrt(252)) if len(returns) > 1 else 0.0
        signals_frame, orders_frame = pd.DataFrame(signals), pd.DataFrame(orders)
        metrics = {
            "total_return": total_return,
            "benchmark_total_return": float(nav_frame.benchmark_nav.iloc[-1] - 1),
            "excess_return": float(
                nav_frame.portfolio_nav.iloc[-1] - nav_frame.benchmark_nav.iloc[-1]
            ),
            "annualized_return": annualized,
            "annualized_volatility": volatility,
            "sharpe_ratio": annualized / volatility if volatility > 0 else 0.0,
            "max_drawdown": abs(float(nav_frame.drawdown.min())),
            "final_value": float(nav_frame.portfolio_value.iloc[-1]),
            "total_transaction_cost": total_cost,
            "signal_count": len(signals_frame),
            "order_attempt_count": len(orders_frame),
            "executed_order_count": int((orders_frame.status == "executed").sum())
            if not orders_frame.empty
            else 0,
        }
        diagnostics = {
            "timing_rule": "completed daily close creates signal; next benchmark trading-day open attempts execution",
            "indicator_price": spec.adjustment,
            "execution_and_valuation_price": "unadjusted",
            "opening_evidence": "opening price and previous unadjusted close only; no execution-day high/low/close/volume",
            "price_limit_policy": "conservatively blocked when opening price is at the board price limit",
            "price_limit_limitation": "board limit is inferred from the stock code using the existing model; historical ST status and historical rule changes are not available in the current schema",
            "corporate_action_limitation": "cash dividends and share-count adjustments are not modeled; interpret periods spanning corporate actions cautiously",
            "unfilled_buy_policy": "expires after planned execution day",
            "buy_signal_rearm_policy": "a failed or expired state signal is not retried until the combined buy condition first becomes false or the stock leaves the candidate list",
            "unfilled_sell_policy": "persistent exit intent retries each trading day",
            "open_exit_intents": sorted(pending_exits),
            "open_positions": sorted(holdings),
            "blocked_order_reasons": dict(reasons),
            "skipped_candidate_update_periods": skipped_candidate_updates,
            "suspension_limitation": "a missing raw opening bar blocks execution; the current daily schema has no historical auction-liquidity or explicit per-day suspension flag",
            "rule_version": EXECUTION_RULE_VERSION,
            "market_source_batch_ids": sorted(
                str(value) for value in prices.source_batch_id.dropna().unique()
            ),
            "benchmark_source_batch_ids": sorted(
                str(value) for value in benchmark.source_batch_id.dropna().unique()
            ),
        }
        positions_frame = pd.DataFrame(positions)
        indicator_frame = build_indicator_series(
            features=features,
            research_days=research_days,
            windows=windows,
            adjustment=spec.adjustment,
        )
        account_audit_frame, account_audit = build_account_audit(
            initial_capital=spec.initial_capital,
            nav=nav_frame,
            orders=orders_frame,
            positions=positions_frame,
        )
        financial_source_batch_ids = []
        for value in pd.DataFrame(selections).get("component_sources", pd.Series(dtype=str)):
            try:
                financial_source_batch_ids.extend(json.loads(value or "{}").values())
            except (AttributeError, TypeError, json.JSONDecodeError):
                continue
        required = max(windows, default=2) + 1
        eligible_warmup = full_calendar[full_calendar <= pd.Timestamp(spec.start_date)]
        warmup_start = eligible_warmup[-required] if len(eligible_warmup) >= required else None
        reproducibility = build_reproducibility(
            run_id=run_id,
            spec=spec.to_dict(),
            warmup_start=warmup_start,
            input_read_at=started,
            prices=prices,
            benchmark=benchmark,
            financial_source_batch_ids=financial_source_batch_ids,
        )
        diagnostics["account_audit"] = account_audit
        trust_summary = daily_signal_trust_summary(
            spec=spec.to_dict(), metrics=metrics, nav=nav_frame, orders=orders_frame,
            diagnostics=diagnostics, reproducibility=reproducibility,
        )
        directory = self.artifact_dir / "daily-signals" / run_id
        artifacts = self.writer.write(
            directory,
            spec=spec.to_dict(),
            metrics=metrics,
            nav=nav_frame,
            signals=signals_frame,
            orders=orders_frame,
            positions=positions_frame,
            candidates=pd.DataFrame(selections),
            diagnostics=diagnostics,
            account_audit=account_audit_frame,
            reproducibility=reproducibility,
            trust_summary=trust_summary,
            indicator_series=indicator_frame,
        )
        completed = datetime.now(UTC)
        payload = {
            "run_id": run_id,
            "status": "succeeded",
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "duration_seconds": (completed - started).total_seconds(),
            "spec": spec.to_dict(),
            "metrics": metrics,
            "summary": {
                "signal_count": len(signals_frame),
                "order_attempt_count": len(orders_frame),
                "executed_order_count": metrics["executed_order_count"],
                "open_position_count": len(holdings),
                "pending_exit_count": len(pending_exits),
            },
            "diagnostics": diagnostics,
            "reproducibility": reproducibility,
            "trust_summary": trust_summary,
            "artifacts": {k: str(v) for k, v in artifacts.items()},
        }
        manifest = directory / "run.json"
        payload["artifacts"]["run_manifest"] = str(manifest)
        write_json(manifest, payload)
        register_run(self.database, "daily_signal", payload)
        progress(100, "日线信号策略完成")
        return payload
