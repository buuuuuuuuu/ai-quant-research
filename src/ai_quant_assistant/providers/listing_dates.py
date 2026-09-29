"""Explicit listing evidence; no inference from prices or unbounded directory downloads."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from io import BytesIO
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

import pandas as pd

from ai_quant_assistant.errors import ProviderError

SSE_LISTING_SOURCE = "sse:COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L:LIST_DATE"
SZSE_LISTING_SOURCE = "szse:ShowReport:1110:tab1:A股上市日期"
MAX_LISTING_RESPONSE_BYTES = 128 * 1024
MAX_SZSE_LISTING_RESPONSE_BYTES = 4 * 1024 * 1024


def explicit_listing_date(value: object) -> date:
    raw = str(value).strip().removesuffix(".0")
    if re.fullmatch(r"\d{8}", raw):
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise ProviderError("invalid listing-date evidence")
    try:
        listed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ProviderError("invalid listing-date evidence") from exc
    if not date(1990, 1, 1) <= listed <= datetime.now(ZoneInfo("Asia/Shanghai")).date():
        raise ProviderError("listing date out of range")
    return listed


def validate_listing_frame(frame: pd.DataFrame, symbol: str) -> date:
    if not {"item", "value"}.issubset(frame.columns):
        raise ProviderError("listing-date fields missing from company information")
    keys = frame["item"].astype(str).str.strip()
    identities = frame.loc[keys == "股票代码", "value"]
    # Require identity even for caches produced by earlier versions. Do not
    # accept a date-only response or infer identity from the requested URL.
    if len(identities) != 1 or str(identities.iloc[0]).strip() != symbol:
        raise ProviderError("missing or mismatched listing-date security identity")
    values = frame.loc[keys.isin(["上市时间", "上市日期"]), "value"]
    if len(values) != 1:
        raise ProviderError("missing or conflicting listing-date evidence")
    return explicit_listing_date(values.iloc[0])


def validate_szse_listing_directory(frame: pd.DataFrame, symbol: str) -> date:
    if not re.fullmatch(r"(?:0|2|3)\d{5}", symbol):
        raise ProviderError("SZSE listing evidence only supports Shenzhen A shares")
    if not {"symbol", "listing_date"}.issubset(frame.columns):
        raise ProviderError("SZSE listing directory fields are incomplete")
    matches = frame.loc[frame["symbol"].astype(str) == symbol, "listing_date"]
    if len(matches) != 1:
        raise ProviderError("missing or conflicting SZSE listing-date records")
    return explicit_listing_date(matches.iloc[0])


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("listing evidence endpoint redirected; response rejected")


def sse_listing_frame(symbol: str, timeout_seconds: float) -> pd.DataFrame:
    """Bounded single-stock variant of AkShare's documented SSE stock-list query.

    The upstream AkShare helper downloads the entire board without a timeout.
    Use the same fixed public endpoint/fields, filtered by stock code, without
    changing requests globally or starting a subprocess/unbounded helper thread.
    """
    if not re.fullmatch(r"6\d{5}", symbol):
        raise ProviderError("SSE listing fallback only supports Shanghai A shares")
    params = {
        "STOCK_TYPE": "8" if symbol.startswith("688") else "1",
        "STOCK_CODE": symbol,
        "REG_PROVINCE": "",
        "CSRC_CODE": "",
        "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
        "COMPANY_STATUS": "2,4,5,7,8",
        "type": "inParams",
        "isPagination": "true",
        "pageHelp.cacheSize": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.pageSize": "2",
        "pageHelp.pageNo": "1",
        "pageHelp.endPage": "1",
    }
    request = Request(
        "https://query.sse.com.cn/sseQuery/commonQuery.do?" + urlencode(params),
        headers={
            "Referer": "https://www.sse.com.cn/assortment/stock/list/share/",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    with build_opener(_NoRedirect()).open(request, timeout=timeout_seconds) as response:
        raw = response.read(MAX_LISTING_RESPONSE_BYTES + 1)
    if len(raw) > MAX_LISTING_RESPONSE_BYTES:
        raise ProviderError("listing evidence response exceeds size limit")
    payload = json.loads(raw)
    rows = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ProviderError("missing or conflicting SSE listing records")
    row = rows[0]
    if str(row.get("A_STOCK_CODE", "")).strip() != symbol:
        raise ProviderError("mismatched SSE listing-date security identity")
    listed = explicit_listing_date(row.get("LIST_DATE"))
    return pd.DataFrame({"item": ["股票代码", "上市日期"], "value": [symbol, str(listed)]})


def szse_listing_directory_frame(timeout_seconds: float) -> pd.DataFrame:
    """Download and normalize the bounded official Shenzhen A-share directory.

    SZSE exposes the listing as one XLSX document rather than a single-stock
    query. Callers cache this normalized directory by market date and select an
    exact symbol afterwards; the first local bar is never used as evidence.
    """
    params = {
        "SHOWTYPE": "xlsx",
        "CATALOGID": "1110",
        "TABKEY": "tab1",
        "random": "0.6935816432433362",
    }
    request = Request(
        "https://www.szse.cn/api/report/ShowReport?" + urlencode(params),
        headers={
            "Referer": "https://www.szse.cn/market/product/stock/list/index.html",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
    )
    with build_opener(_NoRedirect()).open(request, timeout=timeout_seconds) as response:
        headers = getattr(response, "headers", None)
        content_type = headers.get_content_type() if hasattr(headers, "get_content_type") else None
        raw = response.read(MAX_SZSE_LISTING_RESPONSE_BYTES + 1)
    if len(raw) > MAX_SZSE_LISTING_RESPONSE_BYTES:
        raise ProviderError("SZSE listing evidence response exceeds size limit")
    if content_type in {"text/html", "application/json", "text/plain"}:
        raise ProviderError(f"unexpected SZSE listing evidence content type: {content_type}")
    if not raw.startswith(b"PK\x03\x04"):
        raise ProviderError("SZSE listing evidence is not an XLSX document")
    try:
        frame = pd.read_excel(BytesIO(raw), dtype={"A股代码": str})
    except Exception as exc:  # noqa: BLE001 - reject malformed provider documents
        raise ProviderError("unable to parse SZSE listing evidence") from exc
    if not {"A股代码", "A股上市日期"}.issubset(frame.columns):
        raise ProviderError("SZSE listing evidence columns are incomplete")
    raw_symbols = frame["A股代码"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    symbols = raw_symbols.where(raw_symbols.str.fullmatch(r"\d{1,6}")).str.zfill(6)
    normalized = pd.DataFrame(
        {
            "symbol": symbols,
            "listing_date": frame["A股上市日期"],
        }
    ).dropna(subset=["symbol"])
    if normalized.empty:
        raise ProviderError("SZSE listing evidence contains no A-share records")
    return normalized.reset_index(drop=True)
