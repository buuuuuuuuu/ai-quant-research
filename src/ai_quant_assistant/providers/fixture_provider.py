from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from ai_quant_assistant.providers.normalization import (
    clip_dates,
    normalize_interim_announcements,
    normalize_prices,
)


class FixtureProvider:
    """Deterministic offline provider for tests and the first local demo."""

    name = "fixture-v1"

    def __init__(self) -> None:
        self._all_dates = pd.bdate_range("2010-01-01", "2030-12-31")
        self._announcement_frame = self._build_announcements()
        self._benchmark_frame, self._stock_frame = self._build_prices()

    @staticmethod
    def _announcement_date(year: int) -> pd.Timestamp:
        base = pd.Timestamp(year=year, month=8, day=15 + (year % 7))
        return base

    def _build_announcements(self) -> pd.DataFrame:
        rows: list[dict[str, object]] = []
        for year in range(2012, 2026):
            announcement_date = self._announcement_date(year)
            rows.append(
                {
                    "代码": "600519",
                    "简称": "贵州茅台",
                    "公告标题": f"贵州茅台{year}年半年度报告",
                    "公告时间": announcement_date,
                    "公告链接": f"fixture://600519/{year}/interim-report",
                }
            )
            rows.append(
                {
                    "代码": "600519",
                    "简称": "贵州茅台",
                    "公告标题": f"贵州茅台{year}年半年度报告摘要",
                    "公告时间": announcement_date,
                    "公告链接": f"fixture://600519/{year}/summary",
                }
            )
        return pd.DataFrame(rows)

    def _build_prices(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        index = np.arange(len(self._all_dates), dtype=float)
        benchmark_returns = 0.00018 + 0.0012 * np.sin(index / 13.0)
        stock_returns = benchmark_returns + 0.00012 + 0.0018 * np.cos(index / 17.0)

        for year in range(2012, 2026):
            announcement_date = self._announcement_date(year)
            entry_pos = int(self._all_dates.searchsorted(announcement_date, side="right"))
            drift = ((year % 5) - 2) * 0.00055
            stock_returns[entry_pos + 1 : entry_pos + 21] += drift

        benchmark_close = 100 * np.cumprod(1 + benchmark_returns)
        stock_close = 1000 * np.cumprod(1 + stock_returns)
        benchmark = pd.DataFrame({"date": self._all_dates, "close": benchmark_close})
        stock = pd.DataFrame({"date": self._all_dates, "close": stock_close})
        return benchmark, stock

    def get_interim_announcements(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        normalized = normalize_interim_announcements(self._announcement_frame, symbol)
        normalized["source"] = f"{self.name}:interim"
        dates = normalized["announcement_date"].dt.date
        return normalized.loc[(dates >= start_date) & (dates <= end_date)].reset_index(drop=True)

    def get_stock_prices(
        self, symbol: str, start_date: date, end_date: date, adjustment: str
    ) -> pd.DataFrame:
        normalized = normalize_prices(self._stock_frame, symbol, f"{self.name}:{adjustment}")
        return clip_dates(normalized, start_date, end_date)

    def get_benchmark_prices(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        normalized = normalize_prices(self._benchmark_frame, symbol, self.name)
        return clip_dates(normalized, start_date, end_date)
