from datetime import date as Date
from decimal import Decimal

import pandas as pd
import pytest

from ai_quant_assistant.providers.index_fund_shares import IndexFundShareProvider


class FakeClient:
    def fund_etf_scale_sse(self, *, date):
        assert date == "20261027"
        return pd.DataFrame([{"基金代码": "510300", "基金份额": 1230000, "统计日期": Date(2026, 10, 27)}])

    def fund_scale_daily_szse(self, *, start_date, end_date, symbol):
        assert (start_date, end_date, symbol) == ("20261027", "20261027", "ETF")
        return pd.DataFrame([{"基金代码": "159919", "基金份额": 2000000, "日期": Date(2026, 10, 27)}])


def test_exchange_observations_are_normalized_in_shares():
    provider = IndexFundShareProvider(FakeClient())
    sh = provider.fetch_day("SSE", Date(2026, 10, 27))[0]
    sz = provider.fetch_day("SZSE", Date(2026, 10, 27))[0]
    assert (sh.fund_id, sh.shares) == ("SSE.510300", Decimal("1230000"))
    assert (sz.fund_id, sz.shares) == ("SZSE.159919", Decimal("2000000"))


def test_wrong_date_and_duplicate_are_rejected():
    provider = IndexFundShareProvider(FakeClient())
    frame = FakeClient().fund_scale_daily_szse(start_date="20261027", end_date="20261027", symbol="ETF")
    provider.client.fund_scale_daily_szse = lambda **kwargs: frame.assign(日期=Date(2026, 10, 26))
    with pytest.raises(ValueError, match="unexpected trading date"):
        provider.fetch_day("SZSE", Date(2026, 10, 27))
    provider.client.fund_scale_daily_szse = lambda **kwargs: pd.concat([frame, frame])
    with pytest.raises(ValueError, match="duplicate"):
        provider.fetch_day("SZSE", Date(2026, 10, 27))
