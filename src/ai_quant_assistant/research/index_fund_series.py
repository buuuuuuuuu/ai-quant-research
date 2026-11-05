"""Build a reproducible series from explicitly selected, same-day observations."""

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from ai_quant_assistant.research.index_fund_ratio import FundObservation, calculate_ratio


@dataclass(frozen=True)
class DailyFundInput:
    fund_id: str
    trade_date: date
    shares: Decimal | None
    unit_value: Decimal | None


@dataclass(frozen=True)
class DailyIndexInput:
    trade_date: date
    float_cap_cny: Decimal | None


def build_series(
    *,
    trading_days: tuple[date, ...],
    fund_ids: tuple[str, ...],
    fund_rows: tuple[DailyFundInput, ...],
    index_rows: tuple[DailyIndexInput, ...],
) -> list[dict]:
    """Compute a fixed-group history; absent dates are returned as null points.

    The caller supplies a verified calendar and fixed, explicitly labelled
    group. No historical fund selection or unavailable date is inferred.
    """
    if not trading_days or tuple(sorted(set(trading_days))) != trading_days:
        raise ValueError("trading days must be unique, sorted and nonempty")
    if not fund_ids or len(set(fund_ids)) != len(fund_ids):
        raise ValueError("fund group must be nonempty and unique")
    funds = {}
    for row in fund_rows:
        key = row.trade_date, row.fund_id
        if row.fund_id not in fund_ids or row.trade_date not in trading_days or key in funds:
            raise ValueError("unexpected or duplicated fund observation")
        funds[key] = row
    caps = {}
    for row in index_rows:
        if row.trade_date not in trading_days or row.trade_date in caps:
            raise ValueError("unexpected or duplicated index observation")
        caps[row.trade_date] = row.float_cap_cny
    points = []
    for day in trading_days:
        observations = tuple(
            FundObservation(row.fund_id, day, row.shares, row.unit_value)
            for fund_id in fund_ids
            if (row := funds.get((day, fund_id))) is not None
        )
        result = calculate_ratio(day, fund_ids, observations, day, caps.get(day))
        item = asdict(result)
        item["trade_date"] = day.isoformat()
        for field in ("numerator_cny", "denominator_cny", "ratio_fraction"):
            item[field] = str(item[field]) if item[field] is not None else None
        item["availability"] = "ready" if result.ratio_fraction is not None else "partial"
        points.append(item)
    return points
