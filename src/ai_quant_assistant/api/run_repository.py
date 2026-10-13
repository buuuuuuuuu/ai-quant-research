from __future__ import annotations

import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

RUN_ID_RE = re.compile(r"^factor-backtest-\d{8}T\d{6}Z-[0-9a-f]{8}$")
DOWNLOAD_FILES = {
    "daily_nav.csv": "text/csv",
    "trades.csv": "text/csv",
    "positions.csv": "text/csv",
    "factor_selections.csv": "text/csv",
    "metrics.json": "application/json",
    "backtest_result.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "report.html": "text/html",
    "run.json": "application/json",
}
LAB_RUN_ID_RE = re.compile(r"^factor-lab-\d{8}T\d{6}Z-[0-9a-f]{8}$")
LAB_DOWNLOAD_FILES = {
    **{
        f"{item}.csv": "text/csv"
        for item in (
            "strategies",
            "segments",
            "cross_sections",
            "ic_series",
            "ic_summary",
            "group_returns",
            "coverage",
            "yearly",
            "walk_forward",
            "curves",
        )
    },
    "diagnostics.json": "application/json",
    "factor_lab.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "report.html": "text/html",
    "run.json": "application/json",
}
SIGNAL_RUN_ID_RE = re.compile(r"^daily-signal-\d{8}T\d{6}Z-[0-9a-f]{8}$")
SIGNAL_DOWNLOAD_FILES = {
    **{
        f"{item}.csv": "text/csv"
        for item in (
            "daily_nav",
            "signals",
            "orders",
            "positions",
            "candidate_selections",
            "account_audit",
            "indicator_series",
        )
    },
    "metrics.json": "application/json",
    "diagnostics.json": "application/json",
    "account_audit.json": "application/json",
    "reproducibility.json": "application/json",
    "trust_summary.json": "application/json",
    "daily_signal_result.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "report.html": "text/html",
    "run.json": "application/json",
}


class BacktestRunRepository:
    def __init__(self, artifact_dir: Path) -> None:
        self.root = (artifact_dir / "backtests").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        if not RUN_ID_RE.fullmatch(run_id):
            raise KeyError(run_id)
        directory = (self.root / run_id).resolve()
        if directory.parent != self.root or not directory.is_dir():
            raise KeyError(run_id)
        return directory

    def manifest(self, run_id: str) -> dict[str, Any]:
        path = self.run_dir(run_id) / "run.json"
        if not path.is_file():
            raise KeyError(run_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = []
        manifests = sorted(
            self.root.glob("factor-backtest-*/run.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in manifests[:limit]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                rows.append(
                    {
                        "run_id": payload["run_id"],
                        "status": payload.get("status", "unknown"),
                        "completed_at": payload.get("completed_at"),
                        "duration_seconds": payload.get("duration_seconds"),
                        "spec": payload.get("spec", {}),
                        "metrics": payload.get("metrics", {}),
                    }
                )
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
        return rows

    @staticmethod
    def _records(
        path: Path, *, limit: int = 2_000, downsample: bool = False
    ) -> list[dict[str, Any]]:
        if not path.is_file() or path.stat().st_size == 0:
            return []
        frame = pd.read_csv(path)
        if downsample and len(frame) > limit:
            indexes = pd.Series(range(len(frame))).iloc[
                [round(item) for item in pd.Series(range(limit)) * (len(frame) - 1) / (limit - 1)]
            ]
            frame = frame.iloc[indexes.to_numpy()]
        elif len(frame) > limit:
            frame = frame.tail(limit)
        frame = frame.astype(object).where(pd.notna(frame), None)
        return frame.to_dict(orient="records")

    def detail(self, run_id: str) -> dict[str, Any]:
        directory = self.run_dir(run_id)
        payload = self.manifest(run_id)
        return {
            **payload,
            "series": self._records(directory / "daily_nav.csv", downsample=True),
            "trades": self._records(directory / "trades.csv"),
            "positions": self._records(directory / "positions.csv"),
            "selections": self._records(directory / "factor_selections.csv"),
            "downloads": list(DOWNLOAD_FILES),
            "report_url": f"/reports/backtests/{run_id}",
        }

    def file(self, run_id: str, filename: str) -> tuple[Path, str]:
        media_type = DOWNLOAD_FILES.get(filename)
        if media_type is None:
            raise KeyError(filename)
        path = self.run_dir(run_id) / filename
        if not path.is_file():
            raise KeyError(filename)
        return path, media_type


class FactorLabRunRepository(BacktestRunRepository):
    def __init__(self, artifact_dir: Path) -> None:
        self.root = (artifact_dir / "factor-lab").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        if not LAB_RUN_ID_RE.fullmatch(run_id):
            raise KeyError(run_id)
        directory = (self.root / run_id).resolve()
        if directory.parent != self.root or not directory.is_dir():
            raise KeyError(run_id)
        return directory

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        results = []
        for path in sorted(
            self.root.glob("factor-lab-*/run.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[:limit]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                results.append(
                    {
                        key: payload.get(key)
                        for key in (
                            "run_id",
                            "status",
                            "completed_at",
                            "duration_seconds",
                            "spec",
                            "summary",
                            "ic_summary",
                        )
                    }
                )
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
        return results

    def detail(self, run_id: str) -> dict[str, Any]:
        directory = self.run_dir(run_id)
        payload = self.manifest(run_id)
        return {
            **payload,
            "series": self._records(directory / "ic_series.csv", limit=2000),
            "group_returns": self._records(directory / "group_returns.csv", limit=2000),
            "coverage": self._records(directory / "coverage.csv", limit=2000),
            "segments": self._records(directory / "segments.csv", limit=2000),
            "yearly": self._records(directory / "yearly.csv", limit=2000),
            "walk_forward": self._records(directory / "walk_forward.csv", limit=2000),
            "curves": self._records(directory / "curves.csv", limit=5000, downsample=True),
            "downloads": list(LAB_DOWNLOAD_FILES),
            "report_url": f"/reports/factor-lab/{run_id}",
        }

    def file(self, run_id: str, filename: str) -> tuple[Path, str]:
        media_type = LAB_DOWNLOAD_FILES.get(filename)
        if media_type is None:
            raise KeyError(filename)
        path = self.run_dir(run_id) / filename
        if not path.is_file():
            raise KeyError(filename)
        return path, media_type


class DailySignalRunRepository(BacktestRunRepository):
    MAX_CHART_DATES = 12_000
    MAX_DAY_EVENTS = 100

    def __init__(self, artifact_dir: Path) -> None:
        self.root = (artifact_dir / "daily-signals").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        if not SIGNAL_RUN_ID_RE.fullmatch(run_id):
            raise KeyError(run_id)
        directory = (self.root / run_id).resolve()
        if directory.parent != self.root or not directory.is_dir():
            raise KeyError(run_id)
        return directory

    def detail(self, run_id: str) -> dict[str, Any]:
        directory = self.run_dir(run_id)
        return {
            **self.manifest(run_id),
            "series": self._records(directory / "daily_nav.csv", downsample=True),
            "signals": self._records(directory / "signals.csv"),
            "orders": self._records(directory / "orders.csv"),
            "positions": self._records(directory / "positions.csv"),
            "candidates": self._records(directory / "candidate_selections.csv"),
            "account_audit": self._records(directory / "account_audit.csv", limit=500),
            "downloads": list(SIGNAL_DOWNLOAD_FILES),
            "report_url": f"/reports/daily-signals/{run_id}",
        }

    def executed_orders(
        self, run_id: str, *, page: int = 1, page_size: int = 50,
        symbols: tuple[str, ...] = (), side: str = "", date_from: str = "",
        date_to: str = "", min_notional: float | None = None,
        max_notional: float | None = None, sort: str = "execution_date",
        direction: str = "desc",
    ) -> dict[str, Any]:
        """Filter the complete run-scoped CSV, returning only one bounded page."""
        path = self.run_dir(run_id) / "orders.csv"
        if not 1 <= page_size <= 100 or not 1 <= page <= 100_000:
            raise ValueError("页码或每页数量超出范围")
        if side not in ("", "buy", "sell") or direction not in ("asc", "desc"):
            raise ValueError("成交筛选或排序方向无效")
        if len(symbols) > 200 or any(not re.fullmatch(r"\d{6}", s) for s in symbols):
            raise ValueError("股票筛选范围无效")
        if date_from and date_to and date_from > date_to:
            raise ValueError("起始日期不能晚于结束日期")
        if any(v is not None and not math.isfinite(v) for v in (min_notional, max_notional)):
            raise ValueError("成交金额范围无效")
        if min_notional is not None and max_notional is not None and min_notional > max_notional:
            raise ValueError("最低成交金额不能高于最高成交金额")
        sort_keys = {
            "execution_date": '"execution_date"',
            "symbol": '"symbol"',
            "side": '"side"',
            "shares": 'TRY_CAST("shares" AS DOUBLE)',
            "notional": 'TRY_CAST("notional" AS DOUBLE)',
            "cost": 'TRY_CAST("cost" AS DOUBLE)',
        }
        if sort not in sort_keys:
            raise ValueError("不支持该排序字段")
        if not path.is_file() or not path.stat().st_size:
            return {"rows": [], "total": 0, "page": page, "page_size": page_size}
        predicates = ['"status" = \'executed\'']
        parameters: list[Any] = []
        if symbols:
            predicates.append(f'"symbol" IN ({", ".join("?" for _ in symbols)})')
            parameters.extend(symbols)
        for clause, value in (
            ('"side" = ?', side), ('"execution_date" >= ?', date_from),
            ('"execution_date" <= ?', date_to),
        ):
            if value:
                predicates.append(clause)
                parameters.append(value)
        for operator, value in ((">=", min_notional), ("<=", max_notional)):
            if value is not None:
                predicates.append(f'TRY_CAST("notional" AS DOUBLE) {operator} ?')
                parameters.append(value)
        where = " AND ".join(predicates)
        # All column names and sort directions are selected from the allowlist above.
        source = "read_csv(?, header=true, all_varchar=true)"
        ordering = f"{sort_keys[sort]} {direction.upper()} NULLS LAST, "
        ordering += '"execution_date" DESC, "signal_date" DESC, "symbol" ASC, _row ASC'
        with duckdb.connect(":memory:") as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM {source} WHERE {where}", [str(path), *parameters]
            ).fetchone()[0]
            frame = connection.execute(
                f"WITH source AS (SELECT *, row_number() OVER () AS _row FROM {source}) "
                f"SELECT * EXCLUDE (_row) FROM source WHERE {where} "
                f"ORDER BY {ordering} LIMIT ? OFFSET ?",
                [str(path), *parameters, page_size, (page - 1) * page_size],
            ).df()
        numeric = (
            "shares", "signal_price", "execution_price", "notional", "cost",
            "candidate_rank", "candidate_score", "cash_before", "cash_after",
            "position_shares_before", "position_shares_after", "commission_cost",
            "slippage_cost", "stamp_duty_cost",
        )
        for column in numeric:
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.astype(object).where(pd.notna(frame), None)
        return {"rows": frame.to_dict(orient="records"), "total": total,
                "page": page, "page_size": page_size}

    @staticmethod
    def _symbol_records(path: Path, symbol: str, *, limit: int = 2_000) -> list[dict[str, Any]]:
        if not path.is_file() or path.stat().st_size == 0:
            return []
        frame = pd.DataFrame()
        for chunk in pd.read_csv(path, dtype={"symbol": str}, chunksize=50_000):
            selected = chunk.loc[chunk.get("symbol", pd.Series(dtype=str)).eq(symbol)]
            if not selected.empty:
                frame = pd.concat([frame, selected], ignore_index=True).tail(limit)
        if frame.empty:
            return []
        frame = frame.astype(object).where(pd.notna(frame), None)
        return frame.to_dict(orient="records")

    def price_signals(
        self, run_id: str, symbol: str, *, limit: int = 1_200, window: str = "all"
    ) -> dict[str, Any]:
        directory = self.run_dir(run_id)
        manifest = self.manifest(run_id)
        frozen = [str(value) for value in manifest.get("spec", {}).get("symbols", [])]
        if symbol not in frozen:
            raise KeyError(symbol)
        path = directory / "indicator_series.csv"
        if not path.is_file() or path.stat().st_size == 0:
            raise KeyError("indicator_series.csv")
        frame = pd.DataFrame()
        for chunk in pd.read_csv(path, dtype={"symbol": str}, chunksize=50_000):
            selected = chunk.loc[chunk["symbol"].eq(symbol)]
            if not selected.empty:
                frame = pd.concat([frame, selected], ignore_index=True)
                if window == "all" and len(frame) > self.MAX_CHART_DATES:
                    raise ValueError("price and signal chart exceeds the bounded date range")
                if window == "year":
                    frame = frame.tail(self.MAX_CHART_DATES)
        if frame.empty:
            raise KeyError(symbol)
        if window == "year":
            last_date = pd.to_datetime(frame["date"].iloc[-1])
            start_date = (last_date - pd.DateOffset(years=1)).date().isoformat()
            frame = frame.loc[frame["date"].astype(str).str[:10].ge(start_date)].reset_index(
                drop=True
            )
        elif window != "all":
            raise ValueError("unsupported chart date window")
        if len(frame) > self.MAX_CHART_DATES:
            raise ValueError("price and signal chart exceeds the bounded date range")
        signals = self._symbol_records(directory / "signals.csv", symbol)
        orders = self._symbol_records(directory / "orders.csv", symbol)
        if window == "year":
            first_date = str(frame["date"].iloc[0])[:10]
            last_date_text = str(frame["date"].iloc[-1])[:10]
            signals = [
                row for row in signals if first_date <= str(row.get("signal_date") or "")[:10]
                <= last_date_text
            ]
            orders = [
                row for row in orders
                if any(
                    first_date <= str(row.get(key) or "")[:10] <= last_date_text
                    for key in ("planned_execution_date", "execution_date")
                )
            ]
        key_dates = {
            str(row.get(key))[:10]
            for row in [*signals, *orders]
            for key in ("signal_date", "planned_execution_date", "execution_date")
            if row.get(key) not in (None, "", "nan")
        }
        date_index = frame["date"].astype(str).str[:10].tolist()
        line_keys = [key for key in frame if key == "signal_close" or re.fullmatch(r"ma_\d+", key)]
        numeric = frame[line_keys].apply(pd.to_numeric, errors="coerce") if line_keys else pd.DataFrame()
        price_values = numeric.to_numpy().ravel() if not numeric.empty else []
        finite_values = [float(value) for value in price_values if pd.notna(value) and math.isfinite(value)]
        missing_dates = {
            key: frame.loc[pd.to_numeric(frame[key], errors="coerce").isna(), "date"]
            .astype(str)
            .str[:10]
            .tolist()
            for key in line_keys
        }
        if len(frame) > limit:
            dates = frame["date"].astype(str).str[:10]
            required = set(frame.index[dates.isin(key_dates)]) | {0, len(frame) - 1}
            remaining = max(0, limit - len(required))
            sampled = [
                round(item * (len(frame) - 1) / max(remaining - 1, 1)) for item in range(remaining)
            ]
            optional = [index for index in sampled if index not in required][:remaining]
            frame = frame.iloc[sorted(required | set(optional))]
        frame = frame.astype(object).where(pd.notna(frame), None)
        return {
            "run_id": run_id,
            "symbol": symbol,
            "price_adjustment": next(
                (
                    row
                    for row in frame.get("price_adjustment", pd.Series(dtype=str)).dropna().unique()
                ),
                manifest.get("spec", {}).get("adjustment"),
            ),
            "series": frame.to_dict(orient="records"),
            "date_index": date_index,
            "missing_dates": missing_dates,
            "price_range": {
                "minimum": min(finite_values), "maximum": max(finite_values)
            } if finite_values else None,
            "window": window,
            "signals": signals,
            "orders": orders,
            "notice": (
                "价格和 MA 使用信号复权口径；订单开盘参考价为未复权，仅按日期关联，"
                "不绘制到同一价格轴。"
            ),
        }

    @staticmethod
    def _day_rows(
        path: Path, symbol: str, day: str, fields: tuple[str, ...]
    ) -> tuple[list[dict[str, Any]], bool]:
        if not path.is_file() or path.stat().st_size == 0:
            return [], False
        rows: list[dict[str, Any]] = []
        overflow = False
        for chunk in pd.read_csv(path, dtype={"symbol": str}, chunksize=50_000):
            if "symbol" not in chunk:
                continue
            matching = chunk["symbol"].eq(symbol)
            matching_date = pd.Series(False, index=chunk.index)
            for key in fields:
                if key in chunk:
                    matching_date |= chunk[key].astype(str).str[:10].eq(day)
            matching &= matching_date
            selected = chunk.loc[matching]
            for row in selected.to_dict(orient="records"):
                if len(rows) == DailySignalRunRepository.MAX_DAY_EVENTS:
                    overflow = True
                    break
                rows.append({key: None if pd.isna(value) else value for key, value in row.items()})
            if overflow:
                break
        return rows, overflow

    def price_signal_day(self, run_id: str, symbol: str, day: date) -> dict[str, Any]:
        directory = self.run_dir(run_id)
        manifest = self.manifest(run_id)
        if symbol not in {str(value) for value in manifest.get("spec", {}).get("symbols", [])}:
            raise KeyError(symbol)
        day_text = day.isoformat()
        price_rows, price_overflow = self._day_rows(
            directory / "indicator_series.csv", symbol, day_text, ("date",)
        )
        if price_overflow or len(price_rows) > 1:
            raise ValueError("duplicate indicator rows for the selected date")
        signals, signal_overflow = self._day_rows(
            directory / "signals.csv", symbol, day_text, ("signal_date",)
        )
        orders, order_overflow = self._day_rows(
            directory / "orders.csv", symbol, day_text,
            ("planned_execution_date", "execution_date"),
        )
        account_rows = self._records_for_day(directory / "daily_nav.csv", day_text)
        return {
            "run_id": run_id,
            "symbol": symbol,
            "date": day_text,
            "price": price_rows[0] if price_rows else None,
            "signals": signals,
            "orders": orders,
            "account": account_rows[0] if account_rows else None,
            "truncated": signal_overflow or order_overflow,
        }

    @staticmethod
    def _records_for_day(path: Path, day: str) -> list[dict[str, Any]]:
        if not path.is_file() or path.stat().st_size == 0:
            return []
        for chunk in pd.read_csv(path, chunksize=50_000):
            if "date" not in chunk:
                return []
            selected = chunk.loc[chunk["date"].astype(str).str[:10].eq(day)]
            if not selected.empty:
                return [
                    {key: None if pd.isna(value) else value for key, value in row.items()}
                    for row in selected.head(1).to_dict(orient="records")
                ]
        return []

    def file(self, run_id: str, filename: str) -> tuple[Path, str]:
        media_type = SIGNAL_DOWNLOAD_FILES.get(filename)
        if media_type is None:
            raise KeyError(filename)
        path = self.run_dir(run_id) / filename
        if not path.is_file():
            raise KeyError(filename)
        return path, media_type
