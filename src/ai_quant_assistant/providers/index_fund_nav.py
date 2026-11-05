"""ETF unit-NAV observations, preserving the fact that publication time is unknown."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class FundNav:
    fund_id: str
    trade_date: date
    unit_nav: Decimal
    source: str
    published_at: None = None


class IndexFundNavProvider:
    def __init__(self, client: Any | None = None):
        if client is None:
            import akshare

            client = akshare
        self.client = client

    def fetch(self, fund_id: str, start: date, end: date) -> list[FundNav]:
        parts = fund_id.split(".")
        if (len(parts) != 2 or parts[0] not in {"SSE", "SZSE"} or len(parts[1]) != 6
            or not parts[1].isascii() or not parts[1].isdigit() or start > end):
            raise ValueError("invalid fund ID or date range")
        frame = self.client.fund_etf_fund_info_em(
            fund=parts[1], start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
        )
        required = {"净值日期", "单位净值"}
        if not required.issubset(frame.columns):
            raise ValueError(f"missing NAV fields: {sorted(required - set(frame.columns))}")
        observations = []
        seen = set()
        for row in frame.to_dict("records"):
            day = row["净值日期"]
            if hasattr(day, "date"):
                day = day.date()
            if not isinstance(day, date) or not start <= day <= end:
                raise ValueError("NAV returned an unexpected date")
            try:
                nav = Decimal(str(row["单位净值"]))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("invalid unit NAV") from exc
            if not nav.is_finite() or nav <= 0 or day in seen:
                raise ValueError("invalid or duplicate unit NAV")
            seen.add(day)
            observations.append(FundNav(fund_id, day, nav, "akshare:eastmoney"))
        return sorted(observations, key=lambda item: item.trade_date)
