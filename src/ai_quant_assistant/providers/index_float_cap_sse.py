"""Daily circulating market capitalisation from SSE monthly statistics.

The exchange publishes these observations as a monthly report, so the latest
trading days may not yet be available. Source values are rounded to 0.01 亿元.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

SQL_IDS = {
    "000300": "COMMON_SSE_ZQZS_M_CSI300_INDEX_C",
    "000016": "COMMON_SSE_ZQZS_M_SSE50_INDEX_C",
    "000688": "COMMON_SSE_ZQZS_M_KCB50_INDEX_C",
}
ENDPOINT = "https://query.sse.com.cn/commonQuery.do"


@dataclass(frozen=True)
class IndexFloatCap:
    index_code: str
    trade_date: date
    value_cny: Decimal
    source: str
    publication_date: None = None


class SseMonthlyFloatCapProvider:
    def __init__(self, client: Any = None, timeout: float = 20.0):
        if client is None:
            import requests

            client = requests
        self.client = client
        self.timeout = timeout

    def fetch_month(self, index_code: str, year: int, month: int) -> list[IndexFloatCap]:
        if index_code not in SQL_IDS:
            raise ValueError("index is not supported by the SSE monthly report adapter")
        date(year, month, 1)
        month_string = f"{year:04d}{month:02d}"
        response = self.client.get(
            ENDPOINT,
            params={"sqlId": SQL_IDS[index_code], "isPagination": "false", "MDATE": month_string},
            headers={"Referer": "https://www.sse.com.cn/", "User-Agent": "Mozilla/5.0"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
            raise ValueError("invalid SSE monthly response")
        if payload.get("actionErrors") or payload.get("fieldErrors"):
            raise ValueError("SSE monthly query returned an error")
        rows = []
        seen = set()
        for item in payload["result"]:
            try:
                raw_date = str(item["MDATE"])
                day = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8]))
                cap = Decimal(str(item["CPT_NGT"])) * Decimal("100000000")
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise ValueError("invalid SSE float market cap row") from exc
            if len(raw_date) != 8 or (day.year, day.month) != (year, month):
                raise ValueError("SSE returned a date outside the requested month")
            if day in seen or not cap.is_finite() or cap <= 0:
                raise ValueError("duplicate or invalid SSE float market cap")
            seen.add(day)
            rows.append(IndexFloatCap(index_code, day, cap, "sse:monthly_report"))
        return sorted(rows, key=lambda row: row.trade_date)
