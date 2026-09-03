"""Explicit listing evidence; no inference from prices or unbounded directory downloads."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

import pandas as pd

from ai_quant_assistant.errors import ProviderError

SSE_LISTING_SOURCE = "sse:COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L:LIST_DATE"
MAX_LISTING_RESPONSE_BYTES = 128 * 1024


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
