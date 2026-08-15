from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from ai_quant_assistant.errors import ProviderError
from ai_quant_assistant.providers.cache import DataFrameCache, load_or_fetch
from ai_quant_assistant.providers.normalization import (
    clip_dates,
    normalize_interim_announcements,
    normalize_prices,
)


class AkShareProvider:
    name = "akshare"

    def __init__(
        self,
        cache_dir: Path,
        retries: int = 3,
        timeout_seconds: float = 30.0,
        refresh: bool = False,
    ) -> None:
        self.cache = DataFrameCache(cache_dir)
        self.retries = retries
        self.timeout_seconds = timeout_seconds
        self.refresh = refresh

    @staticmethod
    def _akshare():
        try:
            import akshare as ak
        except ImportError as exc:
            raise ProviderError("AkShare is not installed; run `uv sync` first") from exc
        return ak

    @staticmethod
    def _compact(value: date) -> str:
        return value.strftime("%Y%m%d")

    def get_interim_announcements(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        params = {
            "symbol": symbol,
            "market": "沪深京",
            "category": "半年报",
            "start_date": self._compact(start_date),
            "end_date": self._compact(end_date),
        }

        def fetch() -> pd.DataFrame:
            ak = self._akshare()
            raw = ak.stock_zh_a_disclosure_report_cninfo(keyword="", **params)
            return normalize_interim_announcements(raw, symbol)

        return load_or_fetch(
            cache=self.cache,
            namespace="interim_announcements",
            params=params,
            fetcher=fetch,
            retries=self.retries,
            refresh=self.refresh,
        )

    @staticmethod
    def _exchange_symbol(symbol: str) -> str:
        if len(symbol) == 8 and symbol[:2] in {"sh", "sz", "bj"} and symbol[2:].isdigit():
            return symbol
        if len(symbol) != 6 or not symbol.isdigit():
            raise ProviderError(f"unsupported A-share symbol for fallback: {symbol}")
        prefix = "sh" if symbol.startswith("6") else "bj" if symbol.startswith(("4", "8")) else "sz"
        return f"{prefix}{symbol}"

    def get_stock_prices(
        self, symbol: str, start_date: date, end_date: date, adjustment: str
    ) -> pd.DataFrame:
        params = {
            "symbol": symbol,
            "period": "daily",
            "start_date": self._compact(start_date),
            "end_date": self._compact(end_date),
            "adjust": adjustment,
            "fallback": "stock_zh_a_daily",
        }

        def fetch() -> pd.DataFrame:
            ak = self._akshare()
            primary_params = {
                key: params[key] for key in ("symbol", "period", "start_date", "end_date", "adjust")
            }
            try:
                raw = ak.stock_zh_a_hist(timeout=self.timeout_seconds, **primary_params)
                normalized = normalize_prices(
                    raw, symbol, f"akshare:stock_zh_a_hist:{adjustment}"
                )
                if normalized.empty:
                    raise ProviderError("primary stock endpoint returned no rows")
                return normalized
            except Exception:  # noqa: BLE001 - fallback crosses an external provider boundary
                raw = ak.stock_zh_a_daily(
                    symbol=self._exchange_symbol(symbol), adjust=adjustment
                )
                normalized = normalize_prices(
                    raw, symbol, f"akshare:stock_zh_a_daily:{adjustment}"
                )
                if normalized.empty:
                    raise ProviderError("fallback stock endpoint returned no rows") from None
                return clip_dates(normalized, start_date, end_date)

        return load_or_fetch(
            cache=self.cache,
            namespace="stock_prices",
            params=params,
            fetcher=fetch,
            retries=self.retries,
            refresh=self.refresh,
        )

    def get_benchmark_prices(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        normalized_symbol = symbol
        if symbol.isdigit() and len(symbol) == 6:
            normalized_symbol = f"csi{symbol}"
        params = {
            "symbol": normalized_symbol,
            "start_date": self._compact(start_date),
            "end_date": self._compact(end_date),
            "fallback": "stock_zh_index_daily",
        }

        def fetch() -> pd.DataFrame:
            ak = self._akshare()
            primary_params = {key: params[key] for key in ("symbol", "start_date", "end_date")}
            try:
                raw = ak.stock_zh_index_daily_em(**primary_params)
                normalized = normalize_prices(
                    raw, normalized_symbol, "akshare:stock_zh_index_daily_em"
                )
                if normalized.empty:
                    raise ProviderError("primary benchmark endpoint returned no rows")
                return normalized
            except Exception:  # noqa: BLE001 - fallback crosses an external provider boundary
                code = normalized_symbol.removeprefix("csi")
                fallback_symbol = (
                    normalized_symbol
                    if normalized_symbol.startswith(("sh", "sz"))
                    else f"sh{code}"
                )
                raw = ak.stock_zh_index_daily(symbol=fallback_symbol)
                normalized = normalize_prices(
                    raw, normalized_symbol, "akshare:stock_zh_index_daily"
                )
                if normalized.empty:
                    raise ProviderError(
                        "fallback benchmark endpoint returned no rows"
                    ) from None
                return clip_dates(normalized, start_date, end_date)

        return load_or_fetch(
            cache=self.cache,
            namespace="benchmark_prices",
            params=params,
            fetcher=fetch,
            retries=self.retries,
            refresh=self.refresh,
        )
