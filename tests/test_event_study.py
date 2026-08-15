from datetime import date

import pandas as pd
import pytest

from ai_quant_assistant.domain.models import EventStudySpec
from ai_quant_assistant.providers.normalization import normalize_interim_announcements
from ai_quant_assistant.research.event_study import EventStudyEngine


def test_event_study_uses_first_common_trading_day_strictly_after_announcement() -> None:
    dates = pd.bdate_range("2024-01-02", periods=12)
    stock = pd.DataFrame({"date": dates, "close": [100 + 2 * i for i in range(12)]})
    benchmark = pd.DataFrame({"date": dates, "close": [100 + i for i in range(12)]})
    announcements = pd.DataFrame(
        [
            {
                "symbol": "600519",
                "company_name": "贵州茅台",
                "title": "贵州茅台2023年半年度报告",
                "announcement_date": pd.Timestamp("2024-01-03"),
                "url": "fixture://announcement",
                "report_year": 2023,
            }
        ]
    )
    spec = EventStudySpec(
        symbol="600519",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        windows=(2, 4),
        round_trip_cost_bps=0,
    )

    result = EventStudyEngine().run(spec, announcements, stock, benchmark)

    two_day = result.events.loc[result.events["window"] == 2].iloc[0]
    assert two_day["entry_date"] == pd.Timestamp("2024-01-04")
    assert two_day["exit_date"] == pd.Timestamp("2024-01-08")
    assert two_day["stock_announcement_close"] == pytest.approx(102.0)
    assert two_day["stock_entry_close"] == pytest.approx(104.0)
    assert two_day["stock_exit_close"] == pytest.approx(108.0)
    expected_stock = 108 / 104 - 1
    expected_benchmark = 104 / 102 - 1
    assert two_day["stock_net_return"] == pytest.approx(expected_stock)
    assert two_day["benchmark_return"] == pytest.approx(expected_benchmark)
    assert two_day["excess_return"] == pytest.approx(expected_stock - expected_benchmark)


def test_normalization_keeps_original_full_report_and_drops_summary_and_revision() -> None:
    raw = pd.DataFrame(
        [
            {
                "代码": "600519",
                "简称": "贵州茅台",
                "公告标题": "贵州茅台2024年半年度报告摘要",
                "公告时间": "2024-08-08",
                "公告链接": "fixture://summary",
            },
            {
                "代码": "600519",
                "简称": "贵州茅台",
                "公告标题": "贵州茅台2024年半年度报告",
                "公告时间": "2024-08-08",
                "公告链接": "fixture://full",
            },
            {
                "代码": "600519",
                "简称": "贵州茅台",
                "公告标题": "贵州茅台2024年半年度报告（修订版）",
                "公告时间": "2024-08-15",
                "公告链接": "fixture://revision",
            },
        ]
    )

    normalized = normalize_interim_announcements(raw, "600519")

    assert len(normalized) == 1
    assert normalized.iloc[0]["title"] == "贵州茅台2024年半年度报告"
    assert normalized.iloc[0]["report_year"] == 2024

