from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from ai_quant_assistant.domain.models import EventStudyResult, EventStudySpec
from ai_quant_assistant.errors import DataValidationError, InsufficientDataError


class EventStudyEngine:
    """Pure deterministic event-study engine with no network or storage access."""

    @staticmethod
    def _values(frame: pd.DataFrame, column: str) -> list[str]:
        if column not in frame.columns:
            return []
        values = frame[column].dropna().astype(str)
        return sorted(value for value in values.unique().tolist() if value)

    @classmethod
    def _source_diagnostics(cls, frame: pd.DataFrame) -> dict[str, Any]:
        serving_sources = cls._values(frame, "source")
        provider_sources = cls._values(frame, "provider_source")
        if not provider_sources:
            provider_sources = serving_sources
        provider_sources = [source for source in provider_sources if source != "unknown"]
        fallback_sources = {
            source
            for source in provider_sources
            if source.startswith("akshare:stock_zh_a_daily:")
            or source == "akshare:stock_zh_index_daily"
        }
        local_hit = None
        if "local_hit" in frame.columns and not frame["local_hit"].empty:
            local_hit = bool(frame["local_hit"].astype(bool).any())
        local_fallback_used = None
        if "local_fallback_used" in frame.columns and not frame["local_fallback_used"].empty:
            local_fallback_used = bool(frame["local_fallback_used"].astype(bool).any())
        return {
            "serving_sources": serving_sources,
            "provider_sources": provider_sources,
            "local_hit": local_hit,
            "local_fallback_used": local_fallback_used,
            "local_fallback_reasons": cls._values(frame, "local_fallback_reason"),
            "provider_fallback_used": bool(fallback_sources),
            "provider_fallback_sources": sorted(fallback_sources),
            "source_batch_ids": cls._values(frame, "source_batch_id"),
            "raw_paths": cls._values(frame, "raw_path"),
        }

    @staticmethod
    def _calendar(stock_prices: pd.DataFrame, benchmark_prices: pd.DataFrame) -> pd.DataFrame:
        required = {"date", "close"}
        for label, frame in (("stock", stock_prices), ("benchmark", benchmark_prices)):
            missing = required - set(frame.columns)
            if missing:
                raise DataValidationError(f"{label} prices missing columns: {sorted(missing)}")

        stock = stock_prices[["date", "close"]].rename(columns={"close": "stock_close"})
        benchmark = benchmark_prices[["date", "close"]].rename(
            columns={"close": "benchmark_close"}
        )
        stock["date"] = pd.to_datetime(stock["date"]).dt.normalize()
        benchmark["date"] = pd.to_datetime(benchmark["date"]).dt.normalize()
        calendar = stock.merge(benchmark, on="date", how="inner").drop_duplicates("date")
        calendar = calendar.sort_values("date").reset_index(drop=True)
        if calendar.empty:
            raise InsufficientDataError("stock and benchmark have no common trading days")
        return calendar

    @staticmethod
    def _summary(events: pd.DataFrame) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for window, group in events.groupby("window", sort=True):
            excess = group["excess_return"].astype(float)
            sample_size = int(len(excess))
            std = float(excess.std(ddof=1)) if sample_size > 1 else float("nan")
            mean = float(excess.mean())
            margin = 1.96 * std / math.sqrt(sample_size) if sample_size > 1 else float("nan")
            rows.append(
                {
                    "window": int(window),
                    "sample_size": sample_size,
                    "mean_stock_net_return": float(group["stock_net_return"].mean()),
                    "mean_benchmark_return": float(group["benchmark_return"].mean()),
                    "mean_excess_return": mean,
                    "median_excess_return": float(excess.median()),
                    "win_rate": float((excess > 0).mean()),
                    "std_excess_return": std,
                    "ci95_lower": mean - margin if sample_size > 1 else float("nan"),
                    "ci95_upper": mean + margin if sample_size > 1 else float("nan"),
                }
            )
        return pd.DataFrame(rows).sort_values("window").reset_index(drop=True)

    def run(
        self,
        spec: EventStudySpec,
        announcements: pd.DataFrame,
        stock_prices: pd.DataFrame,
        benchmark_prices: pd.DataFrame,
    ) -> EventStudyResult:
        required_announcement_columns = {
            "announcement_date",
            "title",
            "report_year",
            "symbol",
        }
        missing = required_announcement_columns - set(announcements.columns)
        if missing:
            raise DataValidationError(f"announcements missing columns: {sorted(missing)}")
        if announcements.empty:
            raise InsufficientDataError("no qualifying interim-report announcements")

        calendar = self._calendar(stock_prices, benchmark_prices)
        calendar_dates = calendar["date"].to_numpy(dtype="datetime64[ns]")
        stock_closes = stock_prices[["date", "close"]].copy()
        stock_closes["date"] = pd.to_datetime(stock_closes["date"]).dt.normalize()
        stock_closes["close"] = pd.to_numeric(stock_closes["close"], errors="coerce")
        stock_closes = stock_closes.dropna(subset=["date", "close"])
        stock_closes = stock_closes.drop_duplicates("date", keep="last").set_index("date")[
            "close"
        ]
        rows: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        cost = spec.round_trip_cost_bps / 10_000.0

        normalized_announcements = announcements.copy()
        normalized_announcements["announcement_date"] = pd.to_datetime(
            normalized_announcements["announcement_date"]
        ).dt.normalize()
        normalized_announcements = normalized_announcements.sort_values("announcement_date")

        for announcement in normalized_announcements.to_dict(orient="records"):
            announcement_date = pd.Timestamp(announcement["announcement_date"])
            announcement_close = stock_closes.get(announcement_date, float("nan"))
            entry_pos = int(
                np.searchsorted(calendar_dates, announcement_date.to_datetime64(), side="right")
            )
            if entry_pos >= len(calendar):
                dropped.append(
                    {
                        "report_year": int(announcement["report_year"]),
                        "reason": "no_common_trading_day_after_announcement",
                    }
                )
                continue

            entry = calendar.iloc[entry_pos]
            for window in spec.windows:
                exit_pos = entry_pos + window
                if exit_pos >= len(calendar):
                    dropped.append(
                        {
                            "report_year": int(announcement["report_year"]),
                            "window": window,
                            "reason": "insufficient_forward_trading_days",
                        }
                    )
                    continue
                exit_row = calendar.iloc[exit_pos]
                stock_gross_return = float(exit_row["stock_close"] / entry["stock_close"] - 1)
                stock_net_return = stock_gross_return - cost
                benchmark_return = float(
                    exit_row["benchmark_close"] / entry["benchmark_close"] - 1
                )
                rows.append(
                    {
                        "event_id": f"{spec.symbol}-{int(announcement['report_year'])}-{window}d",
                        "symbol": spec.symbol,
                        "report_year": int(announcement["report_year"]),
                        "announcement_date": announcement_date,
                        "title": str(announcement["title"]),
                        "url": str(announcement.get("url", "")),
                        "entry_date": pd.Timestamp(entry["date"]),
                        "exit_date": pd.Timestamp(exit_row["date"]),
                        "window": window,
                        "stock_announcement_close": float(announcement_close),
                        "stock_entry_close": float(entry["stock_close"]),
                        "stock_exit_close": float(exit_row["stock_close"]),
                        "benchmark_entry_close": float(entry["benchmark_close"]),
                        "benchmark_exit_close": float(exit_row["benchmark_close"]),
                        "stock_gross_return": stock_gross_return,
                        "round_trip_cost": cost,
                        "stock_net_return": stock_net_return,
                        "benchmark_return": benchmark_return,
                        "excess_return": stock_net_return - benchmark_return,
                    }
                )

        events = pd.DataFrame(rows)
        if events.empty or events["report_year"].nunique() < spec.min_events:
            raise InsufficientDataError(
                f"only {events['report_year'].nunique() if not events.empty else 0} usable events; "
                f"minimum is {spec.min_events}"
            )
        events = events.sort_values(["report_year", "window"]).reset_index(drop=True)
        summary = self._summary(events)
        diagnostics: dict[str, Any] = {
            "announcements_received": int(len(announcements)),
            "usable_event_years": int(events["report_year"].nunique()),
            "event_window_rows": int(len(events)),
            "common_trading_days": int(len(calendar)),
            "common_calendar_start": pd.Timestamp(calendar["date"].min()).date().isoformat(),
            "common_calendar_end": pd.Timestamp(calendar["date"].max()).date().isoformat(),
            "dropped_event_windows": dropped,
            "entry_rule": spec.entry_rule,
            "confidence_interval": "normal_approximation_95pct",
            "excess_return_method": "stock_net_return_minus_benchmark_return",
            "data_sources": {
                "stock": self._source_diagnostics(stock_prices),
                "benchmark": self._source_diagnostics(benchmark_prices),
            },
        }
        return EventStudyResult(spec=spec, events=events, summary=summary, diagnostics=diagnostics)
