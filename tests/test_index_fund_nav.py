from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ai_quant_assistant.providers.index_fund_nav import IndexFundNavProvider


class FakeNavClient:
    def fund_etf_fund_info_em(self, **kwargs):
        assert kwargs == {"fund": "510300", "start_date": "20261026", "end_date": "20261027"}
        return pd.DataFrame([
            {"净值日期": date(2026, 10, 27), "单位净值": "4.1259", "累计净值": "1.8569"},
            {"净值日期": date(2026, 10, 26), "单位净值": "4.1797", "累计净值": "1.8769"},
        ])


def test_nav_uses_unit_not_accumulated_and_keeps_publication_unknown():
    rows = IndexFundNavProvider(FakeNavClient()).fetch("SSE.510300", date(2026, 10, 26), date(2026, 10, 27))
    assert [row.unit_nav for row in rows] == [Decimal("4.1797"), Decimal("4.1259")]
    assert all(row.published_at is None for row in rows)


def test_nav_duplicate_or_unknown_ids_are_rejected():
    client = FakeNavClient()
    frame = client.fund_etf_fund_info_em(fund="510300", start_date="20261026", end_date="20261027")
    client.fund_etf_fund_info_em = lambda **kwargs: pd.concat([frame, frame])
    provider = IndexFundNavProvider(client)
    with pytest.raises(ValueError, match="duplicate"):
        provider.fetch("SSE.510300", date(2026, 10, 26), date(2026, 10, 27))
    with pytest.raises(ValueError, match="invalid fund"):
        provider.fetch("other.510300", date(2026, 10, 26), date(2026, 10, 27))
