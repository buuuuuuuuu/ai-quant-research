from datetime import date

import pandas as pd

from ai_quant_assistant.providers.cninfo import EVIDENCE_COLUMNS
from ai_quant_assistant.providers.suspension_announcements import (
    OfficialSuspensionAnnouncements,
    parse_official_suspension_document,
)


def announcement(identity: str, title: str) -> dict[str, str]:
    return {
        "代码": "000333",
        "简称": "美的集团",
        "公告标题": title,
        "公告时间": "2018-09-09 18:00:00",
        "公告链接": f"https://www.cninfo.com.cn/detail/{identity}",
        "附件链接": f"https://static.cninfo.com.cn/finalpage/{identity}.PDF",
        "公告ID": identity,
        "机构ID": "gssz0000333",
    }


class Client:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def fetch_keywords(self, **kwargs):
        self.calls.append(kwargs)
        return pd.DataFrame(self.rows, columns=EVIDENCE_COLUMNS)


def test_parser_requires_exact_code_and_explicit_boundaries():
    body = "证券代码：000333。公司股票自2018年9月10日开市起停牌。"
    parsed = parse_official_suspension_document("000333", body)
    assert parsed["suspension_starts"] == [date(2018, 9, 10)]
    assert parsed["resume_dates"] == []
    assert parse_official_suspension_document(
        "000333", "证券代码：000651。公司股票自2018年9月10日开市起停牌。"
    ) is None
    assert parse_official_suspension_document("000333", "000333停牌公告") is None


def test_official_start_and_resume_form_closed_interval():
    rows = [announcement("start", "重大资产重组停牌公告"), announcement("resume", "复牌公告")]
    payloads = {
        rows[0]["附件链接"]: b"%PDF-start",
        rows[1]["附件链接"]: b"%PDF-resume",
    }
    texts = {
        b"%PDF-start": "证券代码：000333。公司股票自2018年9月10日开市起停牌。",
        b"%PDF-resume": "证券代码：000333。公司股票将于2018年10月29日开市起复牌。",
    }
    requested = (date(2018, 9, 10), date(2018, 10, 26), date(2018, 10, 29))
    client = Client(rows)
    verifier = OfficialSuspensionAnnouncements(
        client,
        timeout_seconds=5,
        fetch_pdf=payloads.__getitem__,
        extract_pdf=texts.__getitem__,
    )

    result = verifier.verify("000333", requested)

    assert [item["date"] for item in result["verified"]] == ["2018-09-10", "2018-10-26"]
    assert result["verified"][0]["evidence"]["suspension_start"] == "2018-09-10"
    assert result["verified"][0]["evidence"]["resume_date"] == "2018-10-29"
    assert result["verified"][0]["evidence"]["announcement_ids"] == ["start", "resume"]
    assert result["evidence_chains"][0]["covered_dates"] == ["2018-09-10", "2018-10-26"]
    assert client.calls[0]["symbol"] == "000333"
    assert client.calls[0]["start_date"] == "20180811"
    assert client.calls[0]["end_date"] == "20181228"


def test_incomplete_or_conflicting_chain_does_not_verify():
    only_start = [announcement("start", "停牌公告")]
    verifier = OfficialSuspensionAnnouncements(
        Client(only_start),
        timeout_seconds=5,
        fetch_pdf=lambda _: b"%PDF-start",
        extract_pdf=lambda _: "证券代码：000333。公司股票自2018年9月10日开市起停牌。",
    )
    incomplete = verifier.verify("000333", (date(2018, 9, 10),))
    assert incomplete["verified"] == []
    assert "未识别到明确复牌日期" in incomplete["errors"][0]["error"]

    rows = [
        announcement("start-a", "停牌公告"),
        announcement("start-b", "停牌进展公告"),
        announcement("resume", "复牌公告"),
    ]
    texts = {
        "start-a": "证券代码：000333。公司股票自2018年9月10日开市起停牌。",
        "start-b": "证券代码：000333。公司股票自2018年9月12日开市起停牌。",
        "resume": "证券代码：000333。公司股票将于2018年10月29日开市起复牌。",
    }
    verifier = OfficialSuspensionAnnouncements(
        Client(rows),
        timeout_seconds=5,
        fetch_pdf=lambda url: f"%PDF-{url.rsplit('/', 1)[-1][:-4]}".encode(),
        extract_pdf=lambda payload: texts[payload.decode().removeprefix("%PDF-")],
    )
    conflict = verifier.verify(
        "000333", (date(2018, 9, 10), date(2018, 9, 13))
    )
    assert conflict["verified"] == []
    assert "相互冲突" in conflict["errors"][0]["error"]
