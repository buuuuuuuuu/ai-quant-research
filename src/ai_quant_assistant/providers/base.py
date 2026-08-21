from __future__ import annotations

from datetime import date
from typing import Protocol

import pandas as pd


class MarketDataProvider(Protocol):
    name: str

    def get_interim_announcements(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame: ...

    def get_stock_prices(
        self, symbol: str, start_date: date, end_date: date, adjustment: str
    ) -> pd.DataFrame: ...

    def get_benchmark_prices(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame: ...


class SyncDataProvider(Protocol):
    name: str

    def get_security_master(self) -> pd.DataFrame: ...

    def get_trading_calendar(self) -> pd.DataFrame: ...

    def get_daily_bars(
        self, symbol: str, start_date: date, end_date: date, adjustment: str
    ) -> pd.DataFrame: ...


class ResearchDataProvider(Protocol):
    name: str

    def get_benchmark_prices(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame: ...

    def get_report_announcements(
        self, symbol: str, start_date: date, end_date: date, report_type: str
    ) -> pd.DataFrame: ...
