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


def clip_dates(frame: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    dates = pd.to_datetime(frame["date"] if "date" in frame else frame["announcement_date"])
    mask = (dates.dt.date >= start_date) & (dates.dt.date <= end_date)
    return frame.loc[mask].reset_index(drop=True)
