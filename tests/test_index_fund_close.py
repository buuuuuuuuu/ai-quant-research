from datetime import date
from decimal import Decimal

import pytest
import requests

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


def test_transient_failure_retries_then_succeeds():
    class Flaky(Client):
        def __init__(self):
            super().__init__({"code": "510300", "market": 1, "klines": ["2026-09-01,4.6,4.684,4.7,4.6,1000"]})
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise requests.Timeout()
            return super().get(*args, **kwargs)

    client, waits = Flaky(), []
    rows = IndexFundCloseProvider(client, sleep=waits.append).fetch("SSE.510300", date(2026, 9, 1), date(2026, 9, 1))
    assert rows[0].close == Decimal("4.684")
    assert client.calls == 3 and waits == [0.25, 0.5]


@pytest.mark.parametrize("status,calls", [(503, 3), (429, 3), (404, 1)])
def test_http_failures_are_bounded_and_reported(status, calls):
    class Failing:
        count = 0

        def get(self, *args, **kwargs):
            self.count += 1
            response = requests.Response()
            response.status_code = status
            raise requests.HTTPError(response=response)

    client, waits = Failing(), []
    with pytest.raises(RuntimeError, match=rf"HTTP {status}（尝试 {calls} 次）"):
        IndexFundCloseProvider(client, sleep=waits.append).fetch("SSE.510300", date(2026, 9, 1), date(2026, 9, 1))
    assert client.count == calls and len(waits) == calls - 1
