from datetime import date
from unittest.mock import Mock

import pytest

from ai_quant_assistant.providers.index_float_cap_public import MEMBER_COUNTS, PublicMemberCap

CODE = "000905"
DAY = date(2026, 9, 2)
MEMBERS = [("000001", "2026-01-01", None), ("600001", "2026-09-01", None)]


def page(rows, number, pages):
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return Mock(
        content=f"<title>中证500({CODE})</title>纳入日期<table>{body}</table>当前页面是第{number}页,共{pages}页".encode(
            "gb18030"
        )
    )


def test_dated_membership_including_all_pages(monkeypatch):
    monkeypatch.setitem(MEMBER_COUNTS, CODE, 2)
    client = Mock()
    client.get.side_effect = [
        page([("000001", "甲", "2026-01-01")], 1, 2),
        page([("600001", "乙", "2026-09-01")], 2, 2),
        page([("600002", "丙", "2025-09-01", "2026-08-31")], 1, 1),
    ]
    assert PublicMemberCap(client).membership(CODE) == [
        *MEMBERS,
        ("600002", "2025-09-01", "2026-08-31"),
    ]
    assert client.get.call_args_list[1].kwargs["params"] == {"indexid": CODE, "page": 2}


def values(code, value, day=DAY):
    return {
        "TRADE_DATE": f"{day} 00:00:00",
        "SECURITY_CODE": code,
        "NOTLIMITED_MARKETCAP_A": value,
        "TOTAL_MARKET_CAP": "90000",
    }


def result(rows, pages=1):
    response = Mock()
    response.json.return_value = {"success": True, "result": {"pages": pages, "data": rows}}
    return response


def test_daily_sum_only_same_day_members_and_circulating_values(monkeypatch):
    monkeypatch.setitem(MEMBER_COUNTS, CODE, 2)
    client = Mock()
    client.get.side_effect = [
        result([values("000001", "80.01")], 2),
        result([values("600001", "41.12"), values("600002", "900")], 2),
    ]
    data = PublicMemberCap(client).daily(CODE, DAY, MEMBERS)
    assert data["value"] == "121.13" and data["members"] == 2
    assert len(data["member_cap_sha256"]) == 64
    assert client.get.call_args.kwargs["params"]["filter"] == "(TRADE_DATE='2026-09-02')"


@pytest.mark.parametrize(
    "rows", [[values("600001", "52")], [values("000001", None), values("600001", "4")]]
)
def test_partial_valuations_are_gaps(monkeypatch, rows):
    monkeypatch.setitem(MEMBER_COUNTS, CODE, 2)
    client = Mock()
    client.get.return_value = result(rows)
    assert PublicMemberCap(client).daily(CODE, DAY, MEMBERS) is None


def test_incomplete_historical_members_and_removal_boundaries_are_gaps(monkeypatch):
    monkeypatch.setitem(MEMBER_COUNTS, CODE, 2)
    client = Mock()
    provider = PublicMemberCap(client)
    assert provider.daily(CODE, date(2026, 8, 1), MEMBERS) is None
    assert provider.daily(CODE, DAY, [*MEMBERS, ("600002", "2024-01-01", str(DAY))]) is None
    client.get.assert_not_called()


def test_other_trade_dates_rejected(monkeypatch):
    monkeypatch.setitem(MEMBER_COUNTS, CODE, 2)
    client = Mock()
    client.get.return_value = result([values("000001", 40, date(2026, 9, 1))])
    with pytest.raises(ValueError, match="其他交易日"):
        PublicMemberCap(client).daily(CODE, DAY, MEMBERS)
