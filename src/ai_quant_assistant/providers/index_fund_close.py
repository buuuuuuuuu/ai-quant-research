"""ETF daily unadjusted close from Eastmoney; no stale-price substitution."""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


@dataclass(frozen=True)
class FundClose:
    fund_id: str
    trade_date: date
    close: Decimal


class IndexFundCloseProvider:
    def __init__(self, client: Any = None, timeout: float = 20):
        if client is None:
            import requests
            client = requests
        self.client = client
        self.timeout = timeout

    def fetch(self, fund_id: str, start: date, end: date) -> list[FundClose]:
        if not re.fullmatch(r"(?:SSE|SZSE)\.[0-9]{6}", fund_id) or start > end:
            raise ValueError("invalid fund ID or date range")
        exchange, code = fund_id.split(".")
        market = 1 if exchange == "SSE" else 0
        response = self.client.get(ENDPOINT, params={
            "secid": f"{market}.{code}", "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56", "klt": "101", "fqt": "0",
            "beg": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
        }, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or "data" not in payload:
            raise ValueError("invalid ETF price response")
        data = payload["data"]
        if data is None:
            return []
        if not isinstance(data, dict) or str(data.get("code")) != code or str(data.get("market")) != str(market) or not isinstance(data.get("klines"), list):
            raise ValueError("ETF price identity or rows invalid")
        output, seen = [], set()
        for line in data["klines"]:
            try:
                values = line.split(",")
                day = date.fromisoformat(values[0])
                close, volume = Decimal(values[2]), Decimal(values[5])
            except (AttributeError, IndexError, ValueError, InvalidOperation) as exc:
                raise ValueError("invalid ETF price row") from exc
            if day in seen or not start <= day <= end or not close.is_finite() or close <= 0 or not volume.is_finite() or volume < 0:
                raise ValueError("invalid or duplicate ETF price")
            seen.add(day)
            if volume > 0:
                output.append(FundClose(fund_id, day, close))
        return sorted(output, key=lambda row: row.trade_date)
