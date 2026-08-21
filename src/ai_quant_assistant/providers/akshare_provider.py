from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd

from ai_quant_assistant.errors import ProviderError
from ai_quant_assistant.providers.cache import DataFrameCache, load_or_fetch
from ai_quant_assistant.providers.normalization import (
    clip_dates,
    normalize_daily_bars,
    normalize_financial_indicators,
    normalize_interim_announcements,
    normalize_prices,
    normalize_report_announcements,
    normalize_security_master,
    normalize_trading_calendar,
)

logger = logging.getLogger(__name__)


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

    @staticmethod
    def _exchange_symbol(symbol: str) -> str:
        if len(symbol) == 8 and symbol[:2] in {"sh", "sz", "bj"} and symbol[2:].isdigit():
            return symbol
        if len(symbol) != 6 or not symbol.isdigit():
            raise ProviderError(f"unsupported A-share symbol for fallback: {symbol}")
        prefix = (
            "sh"
            if symbol.startswith("6")
            else "bj"
            if symbol.startswith(("4", "8", "92"))
            else "sz"
        )
        return f"{prefix}{symbol}"

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
            normalized = normalize_interim_announcements(raw, symbol)
            normalized["source"] = "akshare:stock_zh_a_disclosure_report_cninfo:interim"
            return normalized

        return load_or_fetch(
            cache=self.cache,
            namespace="interim_announcements",
            params=params,
            fetcher=fetch,
            retries=self.retries,
            refresh=self.refresh,
        )

    def get_report_announcements(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        report_type: str,
    ) -> pd.DataFrame:
        categories = {"q1": "一季报", "interim": "半年报", "q3": "三季报", "annual": "年报"}
        if report_type not in categories:
            raise ProviderError(f"unsupported announcement report type: {report_type}")
        params = {
            "symbol": symbol,
            "market": "沪深京",
            "category": categories[report_type],
            "start_date": self._compact(start_date),
            "end_date": self._compact(end_date),
        }

        def fetch() -> pd.DataFrame:
            raw = self._akshare().stock_zh_a_disclosure_report_cninfo(keyword="", **params)
            return normalize_report_announcements(
                raw,
                symbol,
                report_type,
                f"akshare:stock_zh_a_disclosure_report_cninfo:{report_type}",
            )

        return load_or_fetch(
            cache=self.cache,
            namespace="report_announcements",
            params=params,
            fetcher=fetch,
            retries=self.retries,
            refresh=self.refresh,
        )

    def get_financial_indicators(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        params = {
            "symbol": symbol,
            "start_date": self._compact(start_date),
            "end_date": self._compact(end_date),
        }

        def fetch() -> pd.DataFrame:
            ak = self._akshare()
            exchange_symbol = self._exchange_symbol(symbol)
            suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[exchange_symbol[:2]]
            try:
                raw = ak.stock_financial_analysis_indicator_em(
                    symbol=f"{symbol}.{suffix}", indicator="按报告期"
                )
                normalized = normalize_financial_indicators(
                    raw, symbol, "akshare:stock_financial_analysis_indicator_em"
                )
                if normalized.empty:
                    raise ProviderError("primary financial indicator endpoint returned no rows")
            except Exception as primary_error:  # noqa: BLE001 - external provider boundary
                raw = ak.stock_financial_analysis_indicator(
                    symbol=symbol, start_year=str(start_date.year)
                )
                normalized = normalize_financial_indicators(
                    raw, symbol, "akshare:stock_financial_analysis_indicator"
                )
                if normalized.empty:
                    raise ProviderError(
                        "fallback financial indicator endpoint returned no rows"
                    ) from primary_error
            dates = pd.to_datetime(normalized["report_period_end"], errors="coerce").dt.date
            return normalized.loc[(dates >= start_date) & (dates <= end_date)].reset_index(
                drop=True
            )

        return load_or_fetch(
            cache=self.cache,
            namespace="financial_indicators",
            params=params,
            fetcher=fetch,
            retries=self.retries,
            refresh=self.refresh,
        )

    def get_security_master(self) -> pd.DataFrame:
        def fetch() -> pd.DataFrame:
            ak = self._akshare()
            try:
                primary = normalize_security_master(
                    ak.stock_info_a_code_name(),
                    "akshare:stock_info_a_code_name",
                )
                if not primary.empty:
                    return primary
            except Exception:
                # stock_info_a_code_name combines three exchanges. A malformed
                # response from any one of them (notably BSE) breaks the whole call.
                pass

            fallback = normalize_security_master(
                ak.stock_zh_a_spot_em(),
                "akshare:stock_zh_a_spot_em",
            )
            if fallback.empty:
                raise ProviderError("AkShare security master endpoints returned no rows")
            return fallback

        return load_or_fetch(
            cache=self.cache,
            namespace="security_master",
            params={"market": "沪深京", "fallback": "stock_zh_a_spot_em"},
            fetcher=fetch,
            retries=self.retries,
            refresh=self.refresh,
        )

    @staticmethod
    def _industry_name(frame: pd.DataFrame) -> str:
        if frame.empty:
            raise ProviderError("endpoint returned no company information")
        for column in ("所属行业", "行业", "所属证监会行业", "industry_name", "industry"):
            if column in frame.columns:
                candidates = frame[column]
                break
        else:
            key = next(
                (name for name in ("item", "项目", "指标", "字段") if name in frame.columns), None
            )
            value = next((name for name in ("value", "值", "内容") if name in frame.columns), None)
            if key is None or value is None:
                raise ProviderError(f"industry columns not found: {list(frame.columns)}")
            matches = (
                frame[key].astype(str).str.contains(r"行业|industry|ind_name", case=False, na=False)
            )
            candidates = frame.loc[matches, value]
        for candidate in candidates:
            if pd.notna(candidate):
                industry = str(candidate).strip()
                if industry and industry.lower() not in {"-", "--", "none", "null", "nan"}:
                    return industry
        raise ProviderError("company information contains no usable industry classification")

    def get_industry_snapshot(self, symbols: tuple[str, ...] = ()) -> pd.DataFrame:
        ak = self._akshare()
        rows: list[dict[str, str]] = []
        failures: list[dict[str, object]] = []
        fallback_count = 0
        for symbol in symbols:
            attempts: list[dict[str, str]] = []
            endpoints = (
                (
                    "akshare:stock_individual_info_em",
                    lambda symbol=symbol: ak.stock_individual_info_em(
                        symbol=symbol, timeout=self.timeout_seconds
                    ),
                ),
                (
                    "akshare:stock_profile_cninfo",
                    lambda symbol=symbol: ak.stock_profile_cninfo(symbol=symbol),
                ),
                (
                    "akshare:stock_individual_basic_info_xq",
                    lambda symbol=symbol: ak.stock_individual_basic_info_xq(
                        symbol=self._exchange_symbol(symbol).upper(),
                        timeout=self.timeout_seconds,
                    ),
                ),
            )
            for index, (source, fetch) in enumerate(endpoints):
                try:
                    industry = self._industry_name(fetch())
                    rows.append({"symbol": symbol, "industry_name": industry, "source": source})
                    if index:
                        fallback_count += 1
                        logger.info("industry snapshot %s used fallback %s", symbol, source)
                    break
                except Exception as exc:  # noqa: BLE001 - external provider boundary
                    attempts.append(
                        {"source": source, "error_type": type(exc).__name__, "error": str(exc)}
                    )
            else:
                failures.append({"symbol": symbol, "attempts": attempts})
                logger.warning(
                    "industry snapshot %s failed across all providers: %s",
                    symbol,
                    "; ".join(
                        f"{item['source']}={item['error_type']}: {item['error']}"
                        for item in attempts
                    ),
                )
        if symbols and not rows:
            detail = "; ".join(
                f"{item['source']}={item['error_type']}: {item['error']}"
                for item in failures[0]["attempts"]
            )
            raise ProviderError(
                f"industry snapshot endpoints returned no classifications for "
                f"{len(failures)}/{len(symbols)} symbols; {failures[0]['symbol']}: {detail}"
            )
        frame = pd.DataFrame(rows, columns=["symbol", "industry_name", "source"])
        frame.attrs["diagnostics"] = {
            "requested_symbols": len(symbols),
            "successful_symbols": len(rows),
            "failed_symbols": failures,
            "provider_sources": sorted({item["source"] for item in rows}),
            "provider_fallback_used": fallback_count > 0,
            "provider_fallback_count": fallback_count,
        }
        return frame

    def get_index_constituent_snapshot(self, benchmark_symbol: str) -> pd.DataFrame:
        code = benchmark_symbol.removeprefix("csi").removeprefix("sh").removeprefix("sz")
        raw = self._akshare().index_stock_cons_csindex(symbol=code)
        symbol_column = next(
            (
                name
                for name in ("成分券代码", "成分证券代码", "证券代码", "symbol", "品种代码")
                if name in raw.columns
            ),
            None,
        )
        if symbol_column is None:
            raise ProviderError(f"index constituent symbol column not found: {list(raw.columns)}")
        result = pd.DataFrame(
            {"symbol": raw[symbol_column].astype(str).str.extract(r"(\d{6})", expand=False)}
        )
        weight_column = next(
            (name for name in ("权重", "权重(%)", "weight") if name in raw.columns), None
        )
        if weight_column is not None:
            result["weight"] = pd.to_numeric(raw[weight_column], errors="coerce")
        result = result.dropna(subset=["symbol"]).drop_duplicates("symbol").reset_index(drop=True)
        if result.empty:
            raise ProviderError("index constituent snapshot endpoint returned no members")
        return result

    def get_trading_calendar(self) -> pd.DataFrame:
        def fetch() -> pd.DataFrame:
            raw = self._akshare().tool_trade_date_hist_sina()
            return normalize_trading_calendar(raw, "akshare:tool_trade_date_hist_sina")

        return load_or_fetch(
            cache=self.cache,
            namespace="trading_calendar",
            params={"market": "沪深京"},
            fetcher=fetch,
            retries=self.retries,
            refresh=self.refresh,
        )

    def get_daily_bars(
        self, symbol: str, start_date: date, end_date: date, adjustment: str
    ) -> pd.DataFrame:
        provider_adjustment = "" if adjustment == "none" else adjustment
        params = {
            "symbol": symbol,
            "period": "daily",
            "start_date": self._compact(start_date),
            "end_date": self._compact(end_date),
            "adjust": provider_adjustment,
        }

        def fetch() -> pd.DataFrame:
            ak = self._akshare()
            try:
                raw = ak.stock_zh_a_hist(timeout=self.timeout_seconds, **params)
                source = f"akshare:stock_zh_a_hist:{adjustment}"
                return normalize_daily_bars(raw, symbol, source)
            except Exception as primary_error:  # noqa: BLE001 - provider fallback boundary
                raw = ak.stock_zh_a_daily(
                    symbol=self._exchange_symbol(symbol),
                    adjust=provider_adjustment,
                )
                source = f"akshare:stock_zh_a_daily:{adjustment}"
                normalized = normalize_daily_bars(raw, symbol, source)
                if normalized.empty:
                    raise ProviderError(
                        "fallback daily-bar endpoint returned no rows"
                    ) from primary_error
                dates = pd.to_datetime(normalized["trade_date"], errors="coerce").dt.date
                return normalized.loc[(dates >= start_date) & (dates <= end_date)].reset_index(
                    drop=True
                )

        return load_or_fetch(
            cache=self.cache,
            namespace="daily_bars",
            params=params,
            fetcher=fetch,
            retries=self.retries,
            refresh=self.refresh,
        )

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
                normalized = normalize_prices(raw, symbol, f"akshare:stock_zh_a_hist:{adjustment}")
                if normalized.empty:
                    raise ProviderError("primary stock endpoint returned no rows")
                return normalized
            except Exception:  # noqa: BLE001 - fallback crosses an external provider boundary
                raw = ak.stock_zh_a_daily(symbol=self._exchange_symbol(symbol), adjust=adjustment)
                normalized = normalize_prices(raw, symbol, f"akshare:stock_zh_a_daily:{adjustment}")
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

    def get_benchmark_prices(self, symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
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
                    normalized_symbol if normalized_symbol.startswith(("sh", "sz")) else f"sh{code}"
                )
                raw = ak.stock_zh_index_daily(symbol=fallback_symbol)
                normalized = normalize_prices(
                    raw, normalized_symbol, "akshare:stock_zh_index_daily"
                )
                if normalized.empty:
                    raise ProviderError("fallback benchmark endpoint returned no rows") from None
                return clip_dates(normalized, start_date, end_date)

        return load_or_fetch(
            cache=self.cache,
            namespace="benchmark_prices",
            params=params,
            fetcher=fetch,
            retries=self.retries,
            refresh=self.refresh,
        )
