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
                return self._annotate_access(
                    local,
                    local_hit=True,
                    local_fallback_used=False,
                    fallback_reason="",
                )
            fallback_reason = "published_rows_empty"
        else:
            fallback_reason = "coverage_incomplete"
        fallback = self.fallback.get_stock_prices(symbol, start_date, end_date, adjustment)
        return self._annotate_access(
            fallback,
            local_hit=False,
            local_fallback_used=True,
            fallback_reason=fallback_reason,
        )

    def get_benchmark_prices(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        fallback = self.fallback.get_benchmark_prices(symbol, start_date, end_date)
        return self._annotate_access(
            fallback,
            local_hit=False,
            local_fallback_used=True,
            fallback_reason="dataset_not_local",
        )

    @staticmethod
    def _annotate_access(
        frame: pd.DataFrame,
        *,
        local_hit: bool,
        local_fallback_used: bool,
        fallback_reason: str,
    ) -> pd.DataFrame:
        annotated = frame.copy()
        annotated["local_hit"] = local_hit
        annotated["local_fallback_used"] = local_fallback_used
        annotated["local_fallback_reason"] = fallback_reason
        return annotated
