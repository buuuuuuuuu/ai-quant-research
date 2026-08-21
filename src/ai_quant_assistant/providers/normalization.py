from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date

import pandas as pd

from ai_quant_assistant.errors import DataValidationError

INTERIM_INCLUDE_RE = re.compile(r"(?:半年度报告|中期报告)")
INTERIM_EXCLUDE_RE = re.compile(r"(?:摘要|英文版|更正|修订|补充|问询|回复|意见|审阅报告|取消|延期)")
REPORT_YEAR_RE = re.compile(r"(20\d{2})\s*年?.{0,4}(半年度|中期)")
ANNUAL_REPORT_YEAR_RE = re.compile(r"(20\d{2})\s*年?.{0,6}(?:年度报告|年报)")
QUARTERLY_REPORT_PATTERNS = {
    "q1": re.compile(r"(?:第一季度报告|一季度报告|一季报)"),
    "q3": re.compile(r"(?:第三季度报告|三季度报告|三季报)"),
}
REPORT_PERIOD_ENDS = {
    "q1": (3, 31),
    "interim": (6, 30),
    "q3": (9, 30),
    "annual": (12, 31),
}
FINANCIAL_METRICS = {
    "revenue": {
        "label": "营业总收入",
        "unit": "CNY",
        "columns": ("TOTALOPERATEREVE", "营业总收入", "营业收入（元）", "营业收入(元)"),
    },
    "parent_net_profit": {
        "label": "归母净利润",
        "unit": "CNY",
        "columns": ("PARENTNETPROFIT", "归母净利润", "净利润（元）", "净利润(元)"),
    },
    "deducted_net_profit": {
        "label": "扣非净利润",
        "unit": "CNY",
        "columns": (
            "KCFJCXSYJLR",
            "扣非净利润",
            "扣除非经常性损益后的净利润（元）",
            "扣除非经常性损益后的净利润(元)",
        ),
    },
    "eps": {
        "label": "基本每股收益",
        "unit": "CNY_PER_SHARE",
        "columns": ("EPSJB", "摊薄每股收益（元）", "摊薄每股收益(元)", "加权每股收益（元）"),
    },
    "book_value_per_share": {
        "label": "每股净资产",
        "unit": "CNY_PER_SHARE",
        "columns": ("BPS", "每股净资产_调整后（元）", "每股净资产_调整后(元)"),
    },
    "revenue_yoy": {
        "label": "营业收入同比增长",
        "unit": "PERCENT",
        "columns": ("TOTALOPERATEREVETZ", "主营业务收入增长率(%)", "营业总收入同比增长(%)"),
    },
    "parent_net_profit_yoy": {
        "label": "归母净利润同比增长",
        "unit": "PERCENT",
        "columns": ("PARENTNETPROFITTZ", "净利润增长率(%)"),
    },
    "roe": {
        "label": "加权净资产收益率",
        "unit": "PERCENT",
        "columns": ("ROEJQ", "加权净资产收益率(%)", "净资产收益率(%)"),
    },
    "gross_margin": {
        "label": "销售毛利率",
        "unit": "PERCENT",
        "columns": ("XSMLL", "销售毛利率(%)"),
    },
    "net_margin": {
        "label": "销售净利率",
        "unit": "PERCENT",
        "columns": ("XSJLL", "销售净利率(%)"),
    },
    "debt_asset_ratio": {
        "label": "资产负债率",
        "unit": "PERCENT",
        "columns": ("ZCFZL", "资产负债率(%)"),
    },
    "operating_cash_flow_to_revenue": {
        "label": "经营净现金流与营业收入比",
        "unit": "RATIO",
        "columns": ("JYXJLYYSR", "经营现金净流量对销售收入比率(%)"),
    },
}


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
    code_col = next(
        (name for name in ["代码", "股票代码", "symbol"] if name in frame.columns), None
    )
    name_col = next(
        (name for name in ["简称", "股票简称", "company_name"] if name in frame.columns), None
    )
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


def normalize_report_announcements(
    frame: pd.DataFrame,
    symbol: str,
    report_type: str,
    source: str,
) -> pd.DataFrame:
    columns = [
        "symbol",
        "company_name",
        "title",
        "announcement_date",
        "url",
        "report_year",
        "report_type",
        "report_period_end",
        "published_at",
        "source",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    date_col = _find_column(frame, ["公告时间", "announcement_date", "date"], "announcement date")
    title_col = _find_column(frame, ["公告标题", "title"], "announcement title")
    url_col = next((name for name in ["公告链接", "url"] if name in frame.columns), None)
    publication_times = pd.to_datetime(frame[date_col], errors="coerce")
    publications = (
        pd.DataFrame(
            {
                "title": frame[title_col].astype(str).str.strip(),
                "announcement_date": publication_times.dt.normalize(),
                "url": frame[url_col].fillna("").astype(str) if url_col else "",
                "published_at": publication_times,
            }
        )
        .dropna(subset=["announcement_date", "published_at"])
        .drop_duplicates(subset=["title", "announcement_date", "url"], keep="first")
    )
    if report_type == "interim":
        normalized = normalize_interim_announcements(frame, symbol)
    elif report_type in {"annual", "q1", "q3"}:
        code_col = next(
            (name for name in ["代码", "股票代码", "symbol"] if name in frame.columns), None
        )
        name_col = next(
            (name for name in ["简称", "股票简称", "company_name"] if name in frame.columns),
            None,
        )
        normalized = pd.DataFrame(
            {
                "symbol": (
                    frame[code_col].astype(str).str.extract(r"(\d{6})", expand=False)
                    if code_col
                    else symbol
                ),
                "company_name": frame[name_col].astype(str).str.strip() if name_col else "",
                "title": frame[title_col].astype(str).str.strip(),
                "announcement_date": pd.to_datetime(
                    frame[date_col], errors="coerce"
                ).dt.normalize(),
                "url": frame[url_col].fillna("").astype(str) if url_col else "",
            }
        ).dropna(subset=["announcement_date"])
        normalized["symbol"] = normalized["symbol"].fillna(symbol)
        if report_type == "annual":
            normalized = normalized[
                normalized["title"].str.contains(r"(?:年度报告|年报)", na=False)
            ]
            normalized = normalized[~normalized["title"].str.contains(INTERIM_INCLUDE_RE, na=False)]
        else:
            normalized = normalized[
                normalized["title"].str.contains(QUARTERLY_REPORT_PATTERNS[report_type], na=False)
            ]
        normalized = normalized[~normalized["title"].str.contains(INTERIM_EXCLUDE_RE, na=False)]
        normalized["report_year"] = normalized.apply(
            lambda row: (
                int(match.group(1))
                if (match := re.search(r"(20\d{2})\s*年?", str(row["title"])))
                else int(
                    pd.Timestamp(row["announcement_date"]).year
                    - (1 if report_type == "annual" else 0)
                )
            ),
            axis=1,
        )
        normalized = normalized.sort_values(["report_year", "announcement_date", "title"])
        normalized = normalized.drop_duplicates(subset=["report_year"], keep="first")
    else:
        raise DataValidationError(f"unsupported report type: {report_type}")

    if normalized.empty:
        return pd.DataFrame(columns=columns)
    normalized = normalized.merge(
        publications,
        on=["title", "announcement_date", "url"],
        how="left",
        validate="one_to_one",
    )
    normalized["report_type"] = report_type
    month, day = REPORT_PERIOD_ENDS[report_type]
    normalized["report_period_end"] = normalized["report_year"].map(
        lambda year: pd.Timestamp(year=int(year), month=month, day=day)
    )
    normalized["published_at"] = normalized["published_at"].fillna(normalized["announcement_date"])
    normalized["source"] = source
    return normalized[columns].reset_index(drop=True)


def normalize_financial_indicators(frame: pd.DataFrame, symbol: str, source: str) -> pd.DataFrame:
    metric_codes = list(FINANCIAL_METRICS)
    columns = [
        "symbol",
        "company_name",
        "report_period_end",
        "report_type",
        "published_at",
        "source",
        *metric_codes,
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    date_col = _find_column(
        frame,
        ["REPORT_DATE", "日期", "报告日期", "report_period_end"],
        "financial report date",
    )
    publication_col = next(
        (
            item
            for item in (
                "NOTICE_DATE",
                "PUBLISH_DATE",
                "ANNOUNCE_DATE",
                "公告日期",
                "披露日期",
                "published_at",
            )
            if item in frame.columns
        ),
        None,
    )
    name_col = next(
        (
            item
            for item in ("SECURITY_NAME_ABBR", "股票简称", "简称", "company_name")
            if item in frame.columns
        ),
        None,
    )
    normalized = pd.DataFrame(
        {
            "symbol": symbol,
            "company_name": frame[name_col].fillna("").astype(str) if name_col else "",
            "report_period_end": pd.to_datetime(frame[date_col], errors="coerce").dt.normalize(),
            "published_at": (
                pd.to_datetime(frame[publication_col], errors="coerce")
                if publication_col
                else pd.NaT
            ),
            "source": source,
        },
        index=frame.index,
    )
    type_by_month = {3: "q1", 6: "interim", 9: "q3", 12: "annual"}
    normalized["report_type"] = normalized["report_period_end"].dt.month.map(type_by_month)
    for code, definition in FINANCIAL_METRICS.items():
        candidate = next((item for item in definition["columns"] if item in frame.columns), None)
        normalized[code] = (
            pd.to_numeric(frame[candidate], errors="coerce") if candidate else float("nan")
        )
        if code == "operating_cash_flow_to_revenue" and candidate and "%" in candidate:
            normalized[code] = normalized[code] / 100.0
    normalized = normalized.dropna(subset=["report_period_end", "report_type"])
    normalized = normalized.sort_values(["report_period_end", "published_at"], na_position="last")
    normalized = normalized.drop_duplicates(
        subset=["report_period_end", "published_at"], keep="last"
    )
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


def normalize_daily_bars(frame: pd.DataFrame, symbol: str, source: str) -> pd.DataFrame:
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
        target: _find_column(working, candidates, target) for target, candidates in mappings.items()
    }
    amount_col = next((name for name in ["成交额", "amount"] if name in working.columns), None)
    normalized = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                working[resolved["trade_date"]], errors="coerce"
            ).dt.normalize(),
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
