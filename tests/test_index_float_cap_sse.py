from datetime import date
from decimal import Decimal

import pytest

from ai_quant_assistant.providers.index_float_cap_sse import SseMonthlyFloatCapProvider


class Response:
    def __init__(self, rows):
        self.rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return {"result": self.rows, "actionErrors": [], "fieldErrors": {}}


class Client:
    def __init__(self, rows):
        self.rows = rows

    def get(self, url, **kwargs):
        assert kwargs["params"]["sqlId"] == "COMMON_SSE_ZQZS_M_CSI300_INDEX_C"
        assert kwargs["params"]["MDATE"] == "202609"
        return Response(self.rows)


def test_monthly_cap_is_converted_from_hundred_million_yuan():
    client = Client([{"MDATE": "20260901", "CPT_NGT": "527896.32"}])
    row = SseMonthlyFloatCapProvider(client).fetch_month("000300", 2026, 9)[0]
    assert row.trade_date == date(2026, 9, 1)
    assert row.value_cny == Decimal("52789632000000")
    assert row.publication_date is None


def test_out_of_month_duplicate_and_invalid_numbers_fail():
    rows = [{"MDATE": "20260801", "CPT_NGT": "1"}]
    with pytest.raises(ValueError, match="outside"):
        SseMonthlyFloatCapProvider(Client(rows)).fetch_month("000300", 2026, 9)
    rows = [{"MDATE": "20260901", "CPT_NGT": "1"}] * 2
    with pytest.raises(ValueError, match="duplicate"):
        SseMonthlyFloatCapProvider(Client(rows)).fetch_month("000300", 2026, 9)
    rows = [{"MDATE": "20260901", "CPT_NGT": "NaN"}]
    with pytest.raises(ValueError, match="invalid"):
        SseMonthlyFloatCapProvider(Client(rows)).fetch_month("000300", 2026, 9)
