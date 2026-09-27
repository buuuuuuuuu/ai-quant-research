from datetime import date

from ai_quant_assistant.providers.akshare_provider import (  # noqa: F401
    AkShareProvider,
)
from ai_quant_assistant.storage.trading_gaps import blocking_gaps, classify_gaps, gap_summary


def test_gap_classification_keeps_unknown_conservative_and_accepts_explicit_evidence():
    day = date(2024, 1, 3)
    missing = {"none": {day}, "qfq": {day}}
    unknown = classify_gaps(missing, {})
    assert unknown["none"][0]["classification"] == "unverified_gap"
    assert len(blocking_gaps(unknown["qfq"])) == 1
    verified = classify_gaps(
        missing,
        {
            "dates": {
                str(day): {
                    "classification": "verified_suspension",
                    "source": "fixture",
                    "checked_at": "2024-01-04T00:00:00Z",
                }
            }
        },
    )
    assert blocking_gaps(verified["none"]) == []
    assert gap_summary(verified)["verified_suspension"] == 1


def test_one_sided_adjustment_gap_is_real_missing_bar_even_with_suspension_evidence():
    day = date(2024, 1, 3)
    result = classify_gaps(
        {"none": set(), "qfq": {day}},
        {"dates": {str(day): {"classification": "verified_suspension", "source": "bad"}}},
    )
    assert result["qfq"] == [
        {
            "date": "2024-01-03",
            "classification": "missing_bar",
            "reason": "另一复权口径存在该交易日行情，本口径缺少已发布记录",
            "source": "local_adjustment_cross_check",
        }
    ]


def test_calendar_mismatch_is_preserved_but_remains_blocking():
    day = date(2024, 1, 3)
    result = classify_gaps(
        {"none": {day}, "qfq": {day}},
        {
            "dates": {
                str(day): {
                    "classification": "calendar_mismatch",
                    "source": "fixture:official-calendar",
                }
            }
        },
    )
    assert result["none"][0]["classification"] == "calendar_mismatch"
    assert blocking_gaps(result["none"])
