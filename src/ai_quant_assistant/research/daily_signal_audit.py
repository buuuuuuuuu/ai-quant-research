from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ai_quant_assistant import __version__
from ai_quant_assistant.storage.research_run_index import canonical
from ai_quant_assistant.storage.schema import SCHEMA_VERSION

ACCOUNT_AUDIT_VERSION = "daily-signal-account-audit-v1"
FACTOR_CATALOG_VERSION = "daily-signal-presentation-v1"
REPORT_TEMPLATE_VERSION = "daily-signal-report-v2-audit"
EXECUTION_RULE_VERSION = "daily-signal-v1-close-confirm-next-open"
AUDIT_ABSOLUTE_TOLERANCE = 1e-6
AUDIT_RELATIVE_TOLERANCE = 1e-10


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def _tolerance(expected: float, actual: float) -> float:
    return max(
        AUDIT_ABSOLUTE_TOLERANCE,
        max(abs(expected), abs(actual)) * AUDIT_RELATIVE_TOLERANCE,
    )


def _check_row(
    *,
    day: Any,
    check: str,
    expected: Any,
    actual: Any,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected_number = _finite_number(expected)
    actual_number = _finite_number(actual)
    difference = (
        actual_number - expected_number
        if expected_number is not None and actual_number is not None
        else None
    )
    tolerance = (
        _tolerance(expected_number, actual_number)
        if expected_number is not None and actual_number is not None
        else None
    )
    passed = difference is not None and abs(difference) <= tolerance
    return {
        "date": str(day)[:10],
        "check": check,
        "status": "passed" if passed else "failed",
        "expected_value": expected_number,
        "actual_value": actual_number,
        "difference": difference,
        "tolerance": tolerance,
        "details": json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
    }


def build_account_audit(
    *,
    initial_capital: float,
    nav: pd.DataFrame,
    orders: pd.DataFrame,
    positions: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Reconcile saved account outputs without changing or repairing them."""
    audit_rows: list[dict[str, Any]] = []
    executed = (
        orders.loc[orders["status"].eq("executed")].copy()
        if not orders.empty and "status" in orders
        else pd.DataFrame()
    )
    if not executed.empty:
        executed["execution_date"] = executed["execution_date"].astype(str).str[:10]
        for order in executed.to_dict(orient="records"):
            day = order.get("execution_date")
            components = [
                _finite_number(order.get("commission_cost")),
                _finite_number(order.get("slippage_cost")),
                _finite_number(order.get("stamp_duty_cost")),
            ]
            audit_rows.append(
                _check_row(
                    day=day,
                    check="fee_components_equal_total_cost",
                    expected=order.get("cost"),
                    actual=sum(components)
                    if all(value is not None for value in components)
                    else None,
                    details={"signal_id": order.get("signal_id"), "symbol": order.get("symbol")},
                )
            )
            cash_before = _finite_number(order.get("cash_before"))
            notional = _finite_number(order.get("notional"))
            cost = _finite_number(order.get("cost"))
            expected_cash_after = None
            if cash_before is not None and notional is not None and cost is not None:
                expected_cash_after = (
                    cash_before - notional - cost
                    if order.get("side") == "buy"
                    else cash_before + notional - cost
                )
            audit_rows.append(
                _check_row(
                    day=day,
                    check="trade_cash_change_matches_notional_and_cost",
                    expected=expected_cash_after,
                    actual=order.get("cash_after"),
                    details={"signal_id": order.get("signal_id"), "side": order.get("side")},
                )
            )
            shares = _finite_number(order.get("shares"))
            position_before = _finite_number(order.get("position_shares_before"))
            expected_position_after = None
            if shares is not None and position_before is not None:
                expected_position_after = (
                    position_before + shares
                    if order.get("side") == "buy"
                    else position_before - shares
                )
            audit_rows.append(
                _check_row(
                    day=day,
                    check="trade_position_change_matches_shares",
                    expected=expected_position_after,
                    actual=order.get("position_shares_after"),
                    details={"signal_id": order.get("signal_id"), "side": order.get("side")},
                )
            )
    position_frame = positions.copy()
    if not position_frame.empty:
        position_frame["date"] = position_frame["date"].astype(str).str[:10]
    expected_holdings: dict[str, int] = {}
    cumulative_cost = 0.0

    for nav_row in nav.to_dict(orient="records"):
        day = str(nav_row.get("date"))[:10]
        day_orders = (
            executed.loc[executed["execution_date"].eq(day)]
            if not executed.empty
            else pd.DataFrame()
        )
        for order in day_orders.to_dict(orient="records"):
            symbol = str(order.get("symbol"))
            shares = int(order.get("shares") or 0)
            before = expected_holdings.get(symbol, 0)
            if order.get("side") == "buy":
                expected_holdings[symbol] = before + shares
            else:
                if shares > before:
                    audit_rows.append(
                        _check_row(
                            day=day,
                            check="sell_not_above_available_shares",
                            expected=before,
                            actual=shares,
                            details={"symbol": symbol, "side": "sell"},
                        )
                    )
                expected_holdings[symbol] = before - shares
                if expected_holdings[symbol] == 0:
                    expected_holdings.pop(symbol)
            cumulative_cost += float(order.get("cost") or 0.0)

        day_positions = (
            position_frame.loc[position_frame["date"].eq(day)]
            if not position_frame.empty
            else pd.DataFrame()
        )
        actual_holdings = {
            str(row["symbol"]): int(row["shares"])
            for row in day_positions.to_dict(orient="records")
            if _finite_number(row.get("shares")) is not None and int(row["shares"]) != 0
        }
        all_symbols = set(expected_holdings) | set(actual_holdings)
        quantity_difference = sum(
            abs(expected_holdings.get(symbol, 0) - actual_holdings.get(symbol, 0))
            for symbol in all_symbols
        )
        position_value = sum(
            float(value)
            for value in day_positions.get("market_value", pd.Series(dtype=float)).dropna()
        )
        cash = nav_row.get("cash")
        portfolio_value = nav_row.get("portfolio_value")
        portfolio_nav = nav_row.get("portfolio_nav")
        audit_rows.extend(
            [
                _check_row(
                    day=day,
                    check="cash_plus_positions_equals_total_assets",
                    expected=portfolio_value,
                    actual=(float(cash) + position_value) if cash is not None else None,
                ),
                _check_row(
                    day=day,
                    check="saved_position_value_equals_positions",
                    expected=nav_row.get("position_value"),
                    actual=position_value,
                ),
                _check_row(
                    day=day,
                    check="total_assets_equals_nav_times_initial_capital",
                    expected=portfolio_value,
                    actual=(
                        float(portfolio_nav) * float(initial_capital)
                        if portfolio_nav is not None
                        else None
                    ),
                ),
                _check_row(
                    day=day,
                    check="cumulative_cost_equals_executed_order_costs",
                    expected=nav_row.get("total_cost"),
                    actual=cumulative_cost,
                ),
                _check_row(
                    day=day,
                    check="positions_match_executed_orders",
                    expected=0,
                    actual=quantity_difference,
                    details={
                        "expected_holdings": expected_holdings,
                        "actual_holdings": actual_holdings,
                    },
                ),
                _check_row(
                    day=day,
                    check="cash_is_not_unexplained_negative",
                    expected=max(float(cash), 0.0) if cash is not None else None,
                    actual=cash,
                ),
            ]
        )

    frame = pd.DataFrame(audit_rows)
    failures = frame.loc[frame["status"].eq("failed")] if not frame.empty else frame
    checked_dates = int(frame["date"].nunique()) if not frame.empty else 0
    summary = {
        "version": ACCOUNT_AUDIT_VERSION,
        "status": "passed" if failures.empty else "warning",
        "checked_dates": checked_dates,
        "check_count": len(frame),
        "failure_count": len(failures),
        "absolute_tolerance": AUDIT_ABSOLUTE_TOLERANCE,
        "relative_tolerance": AUDIT_RELATIVE_TOLERANCE,
        "failures": failures.head(100).to_dict(orient="records"),
        "notice": ("核对只读取已保存结果；发现差异时不会自动修改资金、持仓或运行状态。"),
    }
    return frame, summary


def git_commit_sha(project_root: Path | None = None) -> str | None:
    for key in ("AI_QUANT_RELEASE_COMMIT", "GIT_COMMIT", "SOURCE_VERSION"):
        value = os.environ.get(key, "").strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}", value):
            return value.lower()
    root = project_root or Path(__file__).resolve().parents[3]
    try:
        value = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return value.lower() if re.fullmatch(r"[0-9a-fA-F]{40}", value) else None


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def build_reproducibility(
    *,
    run_id: str,
    spec: dict[str, Any],
    warmup_start: Any,
    input_read_at: datetime,
    prices: pd.DataFrame,
    benchmark: pd.DataFrame,
    financial_source_batch_ids: list[str],
) -> dict[str, Any]:
    symbols = list(spec.get("symbols") or [])
    pool = spec.get("stock_pool") or {}

    def coverage(frame: pd.DataFrame) -> dict[str, Any]:
        dates = pd.to_datetime(frame.get("date"), errors="coerce").dropna()
        return {
            "start": dates.min().date().isoformat() if not dates.empty else None,
            "end": dates.max().date().isoformat() if not dates.empty else None,
            "rows": len(frame),
            "source_batch_ids": sorted(
                str(value) for value in frame.get("source_batch_id", pd.Series()).dropna().unique()
            ),
        }

    return {
        "run_id": run_id,
        "parameter_sha256": digest({
            **{key: value for key, value in spec.items() if key != "preflight_snapshot"},
            "stock_pool": {key: value for key, value in pool.items() if key != "resolved_at"}
            if pool else spec.get("stock_pool"),
        }),
        "stock_pool_id": pool.get("pool_id"),
        "stock_pool_revision": pool.get("pool_revision"),
        "frozen_symbols_sha256": pool.get("symbols_sha256") or digest(symbols),
        "frozen_symbol_count": len(symbols),
        "research_start_date": spec.get("start_date"),
        "research_end_date": spec.get("end_date"),
        "technical_warmup_start": str(warmup_start)[:10] if warmup_start is not None else None,
        "benchmark_symbol": spec.get("benchmark_symbol"),
        "signal_adjustment": spec.get("adjustment"),
        "input_read_at": input_read_at.isoformat(),
        "market_data_coverage": coverage(prices),
        "benchmark_data_coverage": coverage(benchmark),
        "financial_source_batch_ids": sorted(set(financial_source_batch_ids)),
        "financial_publication_rule": "signal day 15:00 exclusive; latest publicly available report",
        "factor_catalog_version": FACTOR_CATALOG_VERSION,
        "report_template_version": REPORT_TEMPLATE_VERSION,
        "application_version": __version__,
        "git_commit_sha": git_commit_sha(),
        "schema_version": SCHEMA_VERSION,
        "timezone": "UTC",
        "execution_rule_version": EXECUTION_RULE_VERSION,
        "limitation": ("保存数据覆盖和来源批次，但当前运行产物不是数据库文件的完整不可变副本。"),
    }


def build_indicator_series(
    *,
    features: dict[str, pd.DataFrame],
    research_days: pd.DatetimeIndex,
    windows: set[int],
    adjustment: str,
) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    columns = ["signal_close", *(f"ma_{window}" for window in sorted(windows))]
    for symbol, frame in features.items():
        available = [column for column in columns if column in frame]
        selected = frame.reindex(research_days)[available].copy()
        selected.insert(0, "date", research_days)
        selected.insert(0, "symbol", symbol)
        selected["price_adjustment"] = adjustment
        rows.append(selected)
    if not rows:
        return pd.DataFrame(columns=["symbol", "date", *columns, "price_adjustment"])
    return pd.concat(rows, ignore_index=True)
