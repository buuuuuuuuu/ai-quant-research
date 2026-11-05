from decimal import Decimal
from pathlib import Path
from runpy import run_path

import pytest

compare = run_path(str(Path(__file__).resolve().parents[1] / "scripts/compare_index_fund_closes.py"))["compare"]


def test_comparison_reports_gaps_and_value_disagreements():
    result = compare(
        [("2024-01-17", Decimal("3.292")), ("2024-01-18", Decimal("3.268"))],
        [("2024-01-18", Decimal("3.269")), ("2024-01-19", Decimal("3.275"))],
    )
    assert result["only_primary"] == ["2024-01-17"]
    assert result["only_candidate"] == ["2024-01-19"]
    assert result["mismatches"] == [{"date": "2024-01-18", "primary": "3.268", "candidate": "3.269"}]
    assert result["overlap"] == 1
    assert result["status"] == "different"


def test_empty_overlap_is_not_a_claim_of_complete_history():
    result = compare([], [])
    assert result["overlap"] == 0
    assert result["status"] == "no_overlap"
    assert "不证明" in result["note"]
    assert compare([("2024-01-18", Decimal("3.0"))], [("2024-01-18", Decimal("3.00"))])["status"] == "observed_rows_match"
    with pytest.raises(ValueError, match="duplicate"):
        compare([("2024-01-18", Decimal("3")), ("2024-01-18", Decimal("4"))], [])
