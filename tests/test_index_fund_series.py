from datetime import date
from decimal import Decimal

import pytest

from ai_quant_assistant.research.index_fund_series import (
    DailyFundInput,
    DailyIndexInput,
    build_series,
)


def test_series_preserves_missing_days_without_forward_filling():
    days = (date(2026, 9, 1), date(2026, 9, 2))
    point = DailyFundInput("SSE.510300", days[0], Decimal("100"), Decimal("2"))
    caps = tuple(DailyIndexInput(day, Decimal("10000")) for day in days)
    result = build_series(trading_days=days, fund_ids=(point.fund_id,), fund_rows=(point,), index_rows=caps)
    assert result[0]["ratio_fraction"] == "0.02"
    assert result[1]["ratio_fraction"] is None
    assert result[1]["availability"] == "partial"
    assert "SSE.510300:observation" in result[1]["missing"]


def test_series_rejects_duplicate_and_unexpected_fund_observations():
    day = date(2026, 9, 1)
    point = DailyFundInput("SSE.510300", day, Decimal("100"), Decimal("2"))
    with pytest.raises(ValueError, match="duplicated"):
        build_series(trading_days=(day,), fund_ids=(point.fund_id,), fund_rows=(point, point), index_rows=())
