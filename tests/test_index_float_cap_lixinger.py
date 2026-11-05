from datetime import date
from decimal import Decimal
from unittest.mock import Mock

import pytest

from ai_quant_assistant.providers.index_float_cap_lixinger import LixingerIndexFloatCap
from ai_quant_assistant.storage.index_fund_sync import INDEXES


@pytest.mark.parametrize("code", INDEXES)
def test_range_reads_same_index_cmc_in_yuan(code):
    client = Mock()
    response = client.post.return_value
    response.status_code = 200
    response.json.return_value = {"code": 1, "data": [
        {"stockCode": code, "date": "2026-11-05T00:00:00+08:00", "cmc": "10000.25", "mc": 99999},
        {"stockCode": code, "date": "2026-11-03T16:00:00Z", "ecmc": 8888},
    ]}
    rows = LixingerIndexFloatCap("test", client).fetch_range(code, date(2026, 11, 4), date(2026, 11, 5))
    sent = client.post.call_args.kwargs
    assert sent["json"] == {"stockCodes": [code], "metricsList": ["cmc"], "token": "test",
                            "startDate": "2026-11-04", "endDate": "2026-11-05"}
    assert sent["allow_redirects"] is False
    assert [(row.trade_date, row.value_cny) for row in rows] == [(date(2026, 11, 5), Decimal("10000.25"))]


@pytest.mark.parametrize("record", [
    {"stockCode": "000905", "cmc": -1}, {"stockCode": "000300", "cmc": 10},
    {"stockCode": "000905", "cmc": "NaN"}, {"stockCode": "000905", "date": "2026-11-01"},
])
def test_rejects_invalid_daily_cap(record):
    client = Mock()
    response = client.post.return_value
    response.status_code = 200
    response.json.return_value = {"code": 1, "data": [{"date": "2026-11-05", **record}]}
    with pytest.raises(ValueError):
        LixingerIndexFloatCap("test", client).fetch_range("000905", date(2026, 11, 5), date(2026, 11, 5))
    assert client.post.call_args.kwargs["json"]["date"] == "2026-11-05"
