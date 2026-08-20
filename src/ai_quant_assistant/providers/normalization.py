from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date

import pandas as pd

from ai_quant_assistant.errors import DataValidationError

INTERIM_INCLUDE_RE = re.compile(r"(?:半年度报告|中期报告)")
INTERIM_EXCLUDE_RE = re.compile(
    r"(?:摘要|英文版|更正|修订|补充|问询|回复|意见|审阅报告|取消|延期)"
)
REPORT_YEAR_RE = re.compile(r"(20\d{2})\s*年?.{0,4}(半年度|中期)")


def exchange_for_symbol(symbol: str) -> str:
    if symbol.startswith(("4", "8", "92")):
        return "BSE"
    if symbol.startswith(("5", "6")):
        return "SSE"
    return "SZSE"


def security_id_for_symbol(symbol: str) -> str:
    return f"{exchange_for_symbol(symbol)}.{symbol}"


def _find_column(frame: pd.DataFrame, candidates: Iterable[str], label: str) -> str:
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
    raise DataValidationError(
        f"{label} column not found; candidates={list(candidates)}, columns={list(frame.columns)}"
    )


def normalize_interim_announcements(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    columns = ["symbol", "company_name", "title", "announcement_date", "url", "report_year"]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    date_col = _find_column(frame, ["公告时间", "announcement_date", "date"], "announcement date")
    title_col = _find_column(frame, ["公告标题", "title"], "announcement title")
    code_col = next((name for name in ["代码", "股票代码", "symbol"] if name in frame.columns), None)
    name_col = next((name for name in ["简称", "股票简称", "company_name"] if name in frame.columns), None)
    url_col = next((name for name in ["公告链接", "url"] if name in frame.columns), None)

    normalized = pd.DataFrame(
        {
            "symbol": (
                frame[code_col].astype(str).str.extract(r"(\d{6})", expand=False)
                if code_col
                else symbol
            ),
            "company_name": frame[name_col].astype(str).str.strip() if name_col else "",
            "title": frame[title_col].astype(str).str.strip(),
            "announcement_date": pd.to_datetime(frame[date_col], errors="coerce").dt.normalize(),
            "url": frame[url_col].fillna("").astype(str) if url_col else "",
        }
    )
    normalized["symbol"] = normalized["symbol"].fillna(symbol).replace("", symbol)
    normalized = normalized.dropna(subset=["announcement_date"])
    normalized = normalized[normalized["title"].str.contains(INTERIM_INCLUDE_RE, na=False)]
    normalized = normalized[~normalized["title"].str.contains(INTERIM_EXCLUDE_RE, na=False)]

    def report_year(row: pd.Series) -> int:
        match = REPORT_YEAR_RE.search(str(row["title"]))
        if match:
            return int(match.group(1))
        return int(pd.Timestamp(row["announcement_date"]).year)

    normalized["report_year"] = normalized.apply(report_year, axis=1)
    normalized = normalized.drop_duplicates(subset=["title", "announcement_date", "url"])
    normalized = normalized.sort_values(["report_year", "announcement_date", "title"])
    normalized = normalized.drop_duplicates(subset=["report_year"], keep="first")
    return normalized[columns].reset_index(drop=True)


def normalize_prices(frame: pd.DataFrame, symbol: str, source: str) -> pd.DataFrame:
    columns = ["date", "close", "symbol", "source"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    date_col = _find_column(frame, ["日期", "date"], "price date")
    close_col = _find_column(frame, ["收盘", "收盘价", "close"], "close")
    normalized = pd.DataFrame(
        {
            "date": pd.to_datetime(frame[date_col], errors="coerce").dt.normalize(),
            "close": pd.to_numeric(frame[close_col], errors="coerce"),
            "symbol": symbol,
            "source": source,
        }
    )
    normalized = normalized.dropna(subset=["date", "close"])
    normalized = normalized[normalized["close"] > 0]
    normalized = normalized.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    if normalized.empty:
        raise DataValidationError(f"{source} returned no positive close prices for {symbol}")
    return normalized[columns].reset_index(drop=True)


def normalize_security_master(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    columns = ["security_id", "symbol", "exchange", "name", "source"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    working = frame.reset_index(drop=True)
    code_col = _find_column(working, ["code", "代码", "symbol", "股票代码"], "security code")
    name_col = _find_column(working, ["name", "名称", "简称", "股票简称"], "security name")
    symbols = working[code_col].astype(str).str.extract(r"(\d{6})", expand=False)
    normalized = pd.DataFrame(
        {
            "symbol": symbols,
            "name": working[name_col].fillna("").astype(str).str.strip(),
        }
    ).dropna(subset=["symbol"])
    normalized["exchange"] = normalized["symbol"].map(exchange_for_symbol)
    normalized["security_id"] = normalized["symbol"].map(security_id_for_symbol)
    normalized["source"] = source
    return normalized.drop_duplicates("security_id", keep="last")[columns].reset_index(drop=True)


def normalize_trading_calendar(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    columns = [
        "exchange",
        "trade_date",
        "is_open",
        "previous_open_date",
        "next_open_date",
        "session_no",
        "source",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    date_col = _find_column(frame, ["trade_date", "日期", "date"], "trade date")
    dates = pd.to_datetime(frame[date_col], errors="coerce").dropna().dt.normalize()
    dates = dates.drop_duplicates().sort_values().reset_index(drop=True)
    rows: list[dict[str, object]] = []
    for exchange in ("SSE", "SZSE", "BSE"):
        for index, trade_date in enumerate(dates):
            rows.append(
                {
                    "exchange": exchange,
                    "trade_date": trade_date,
                    "is_open": True,
                    "previous_open_date": dates.iloc[index - 1] if index else None,
                    "next_open_date": dates.iloc[index + 1] if index + 1 < len(dates) else None,
                    "session_no": index + 1,
                    "source": source,
                }
            )
    return pd.DataFrame(rows, columns=columns)


def normalize_daily_bars(
    frame: pd.DataFrame, symbol: str, source: str
) -> pd.DataFrame:
    columns = [
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "amount_missing",
        "symbol",
        "source",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    working = frame.reset_index(drop=True)
    mappings = {
        "trade_date": ["日期", "date"],
        "open": ["开盘", "open"],
        "high": ["最高", "high"],
        "low": ["最低", "low"],
        "close": ["收盘", "收盘价", "close"],
        "volume": ["成交量", "volume"],
    }
    resolved = {
        target: _find_column(working, candidates, target)
        for target, candidates in mappings.items()
    }
    amount_col = next((name for name in ["成交额", "amount"] if name in working.columns), None)
    normalized = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(working[resolved["trade_date"]], errors="coerce").dt.normalize(),
            **{
                field: pd.to_numeric(working[column], errors="coerce")
                for field, column in resolved.items()
                if field != "trade_date"
            },
        }
    )
    normalized["amount"] = (
        pd.to_numeric(working[amount_col], errors="coerce") if amount_col else 0.0
    )
    normalized["amount_missing"] = amount_col is None
    normalized["symbol"] = symbol
    normalized["source"] = source
    return normalized.sort_values("trade_date", na_position="last")[columns].reset_index(drop=True)


def clip_dates(frame: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    dates = pd.to_datetime(frame["date"] if "date" in frame else frame["announcement_date"])
    mask = (dates.dt.date >= start_date) & (dates.dt.date <= end_date)
    return frame.loc[mask].reset_index(drop=True)
