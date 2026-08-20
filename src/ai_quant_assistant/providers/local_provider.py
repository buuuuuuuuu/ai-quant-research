from __future__ import annotations

from datetime import date

import pandas as pd

from ai_quant_assistant.providers.base import MarketDataProvider
from ai_quant_assistant.storage.database import LocalDatabase


class LocalFirstProvider:
    name = "local-first"

    def __init__(self, database: LocalDatabase, fallback: MarketDataProvider) -> None:
        self.database = database
        self.fallback = fallback

    def get_interim_announcements(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        return self.fallback.get_interim_announcements(symbol, start_date, end_date)

    def get_stock_prices(
        self, symbol: str, start_date: date, end_date: date, adjustment: str
    ) -> pd.DataFrame:
        stored_adjustment = adjustment or "none"
        if self.database.has_coverage(symbol, start_date, end_date, stored_adjustment):
            local = self.database.read_published_prices(
                symbol, start_date, end_date, stored_adjustment
            )
            if not local.empty:
                return local
        return self.fallback.get_stock_prices(symbol, start_date, end_date, adjustment)

    def get_benchmark_prices(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        return self.fallback.get_benchmark_prices(symbol, start_date, end_date)
