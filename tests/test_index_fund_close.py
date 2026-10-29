from datetime import date
from decimal import Decimal

import pytest

from ai_quant_assistant.providers.index_fund_close import IndexFundCloseProvider


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": self.data}


class Client:
    def __init__(self, data):
        self.data = data
        self.params = None

    def get(self, url, *, params, timeout):
        self.params = params
        assert timeout > 0
        return Response(self.data)


def test_unadjusted_close_and_untraded_gap():
    client = Client({"code": "510300", "market": 1, "klines": [
        "2026-09-01,4.6,4.684,4.7,4.6,1000", "2026-09-02,4.6,4.620,4.7,4.6,0",
    ]})
    rows = IndexFundCloseProvider(client).fetch("SSE.510300", date(2026, 9, 1), date(2026, 9, 2))
    assert client.params["fqt"] == "0" and client.params["secid"] == "1.510300"
    assert [(row.trade_date, row.close) for row in rows] == [(date(2026, 9, 1), Decimal("4.684"))]


def test_invalid_identity_and_duplicate_rejected():
    with pytest.raises(ValueError, match="identity"):
        IndexFundCloseProvider(Client({"code": "510300", "market": 0, "klines": []})).fetch("SSE.510300", date(2026, 9, 1), date(2026, 9, 1))
    line = "2026-09-01,4.6,4.684,4.7,4.6,1000"
    with pytest.raises(ValueError, match="duplicate"):
        IndexFundCloseProvider(Client({"code": "159919", "market": 0, "klines": [line, line]})).fetch("SZSE.159919", date(2026, 9, 1), date(2026, 9, 1))
