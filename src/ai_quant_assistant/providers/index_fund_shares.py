"""ETF share observations from the Shanghai and Shenzhen exchanges."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class FundShares:
    fund_id: str
    trade_date: date
    shares: Decimal
    source: str


def _parse_shares(frame: Any, exchange: str, requested_date: date) -> list[FundShares]:
    """Normalize returned exchange data; both AkShare adapters output shares in 份."""
    date_col = "统计日期" if exchange == "SSE" else "日期"
    required = {"基金代码", "基金份额", date_col}
    if not required.issubset(frame.columns):
        raise ValueError(f"missing share fields: {sorted(required - set(frame.columns))}")
    output = []
    seen = set()
    for row in frame.to_dict("records"):
        day = row[date_col]
        if hasattr(day, "date"):
            day = day.date()
        if day != requested_date:
            raise ValueError("provider returned an unexpected trading date")
        code = str(row["基金代码"]).strip()
        if len(code) != 6 or not code.isascii() or not code.isdigit():
            raise ValueError("provider returned an invalid fund code")
        try:
            shares = Decimal(str(row["基金份额"]))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid shares for {code}") from exc
        if not shares.is_finite() or shares <= 0:
            raise ValueError(f"invalid shares for {code}")
        if code in seen:
            raise ValueError(f"duplicate shares for {code}")
        seen.add(code)
        output.append(FundShares(f"{exchange}.{code}", day, shares, f"akshare:{exchange.lower()}"))
    return output


class IndexFundShareProvider:
    def __init__(self, client: Any | None = None):
        if client is None:
            import akshare

            client = akshare
        self.client = client

    def fetch_day(self, exchange: str, trade_date: date) -> list[FundShares]:
        date_string = trade_date.strftime("%Y%m%d")
        if exchange == "SSE":
            frame = self.client.fund_etf_scale_sse(date=date_string)
        elif exchange == "SZSE":
            frame = self.client.fund_scale_daily_szse(
                start_date=date_string, end_date=date_string, symbol="ETF"
            )
        else:
            raise ValueError("unsupported exchange")
        return _parse_shares(frame, exchange, trade_date)
