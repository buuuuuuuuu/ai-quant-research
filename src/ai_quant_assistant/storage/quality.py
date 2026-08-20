from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True, slots=True)
class DataQualityCheck:
    rule_id: str
    severity: str
    passed: bool
    observed: str
    expected: str
    details: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_daily_bars(
    frame: pd.DataFrame,
    *,
    start_date: date,
    end_date: date,
    calendar_dates: set[date],
) -> list[DataQualityCheck]:
    if frame.empty:
        return [
            DataQualityCheck(
                rule_id="rows_present",
                severity="warning",
                passed=False,
                observed="0 rows",
                expected="one or more rows when the security traded",
                details="Empty pre-listing, post-delisting, or suspension partitions are allowed.",
            )
        ]

    dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    numeric_columns = ["open", "high", "low", "close", "volume", "amount"]
    numeric = frame[numeric_columns].apply(pd.to_numeric, errors="coerce")
    duplicate_count = int(dates.duplicated(keep=False).sum())
    invalid_dates = int(dates.isna().sum())
    missing_numeric = int(numeric.isna().any(axis=1).sum())
    invalid_ranges = int(
        (
            (dates.dt.date < start_date)
            | (dates.dt.date > end_date)
        ).fillna(True).sum()
    )
    nonpositive_prices = int((numeric[["open", "high", "low", "close"]] <= 0).any(axis=1).sum())
    negative_activity = int((numeric[["volume", "amount"]] < 0).any(axis=1).sum())
    invalid_ohlc = int(
        (
            (numeric["high"] < numeric[["open", "low", "close"]].max(axis=1))
            | (numeric["low"] > numeric[["open", "high", "close"]].min(axis=1))
        ).sum()
    )
    observed_dates = set(dates.dropna().dt.date)
    expected_dates = {
        trade_date for trade_date in calendar_dates if start_date <= trade_date <= end_date
    }
    missing_dates = expected_dates - observed_dates
    amount_missing = bool(frame.get("amount_missing", pd.Series(False)).astype(bool).any())

    return [
        DataQualityCheck(
            "trade_date_valid",
            "blocking",
            invalid_dates == 0,
            f"{invalid_dates} invalid dates",
            "0 invalid dates",
        ),
        DataQualityCheck(
            "primary_key_unique",
            "blocking",
            duplicate_count == 0,
            f"{duplicate_count} duplicate rows",
            "0 duplicate rows",
        ),
        DataQualityCheck(
            "numeric_complete",
            "blocking",
            missing_numeric == 0,
            f"{missing_numeric} rows with missing numeric values",
            "0 rows with missing numeric values",
        ),
        DataQualityCheck(
            "date_in_partition",
            "blocking",
            invalid_ranges == 0,
            f"{invalid_ranges} rows outside partition",
            "0 rows outside partition",
        ),
        DataQualityCheck(
            "positive_prices",
            "blocking",
            nonpositive_prices == 0,
            f"{nonpositive_prices} rows with non-positive OHLC",
            "0 rows with non-positive OHLC",
            "AkShare documents rare negative adjusted prices; those rows are rejected.",
        ),
        DataQualityCheck(
            "nonnegative_activity",
            "blocking",
            negative_activity == 0,
            f"{negative_activity} rows with negative volume or amount",
            "0 rows with negative volume or amount",
        ),
        DataQualityCheck(
            "ohlc_consistent",
            "blocking",
            invalid_ohlc == 0,
            f"{invalid_ohlc} inconsistent OHLC rows",
            "0 inconsistent OHLC rows",
        ),
        DataQualityCheck(
            "calendar_continuity",
            "warning",
            not missing_dates,
            f"{len(missing_dates)} missing open dates",
            "0 missing open dates",
            "Suspensions can produce legitimate gaps; this rule is diagnostic only.",
        ),
        DataQualityCheck(
            "amount_available",
            "warning",
            not amount_missing,
            "missing" if amount_missing else "available",
            "provider-reported amount in CNY",
            "Fallback data without amount is stored as zero and flagged.",
        ),
    ]


def has_blocking_failure(checks: list[DataQualityCheck]) -> bool:
    return any(check.severity == "blocking" and not check.passed for check in checks)
