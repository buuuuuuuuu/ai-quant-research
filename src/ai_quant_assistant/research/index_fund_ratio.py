"""Strict daily ratio calculation for an already selected index fund group.

Inputs must be normalized by providers and refer to the same business date.
Fund selection, source validation and historical snapshots are separate concerns.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class FundObservation:
    fund_id: str
    trade_date: date
    shares: Decimal | None
    unit_value: Decimal | None


@dataclass(frozen=True)
class RatioResult:
    trade_date: date
    numerator_cny: Decimal | None
    denominator_cny: Decimal | None
    ratio_fraction: Decimal | None
    missing: tuple[str, ...]


@dataclass(frozen=True)
class RankingObservation:
    fund_id: str
    rank_date: date
    available_date: date
    shares: Decimal | None
    unit_nav: Decimal | None


def select_top_funds(
    expected_funds: tuple[str, ...],
    observations: tuple[RankingObservation, ...],
    rank_date: date,
    effective_date: date,
    top_n: int,
) -> tuple[str, ...]:
    """Rank a verified candidate universe using only available same-day data.

    The caller is responsible for verifying eligibility and candidate-universe
    completeness. An unknown observation blocks the ranking rather than being
    treated as zero. The new group cannot be used on its ranking date.
    """
    if not expected_funds or any(not fund_id for fund_id in expected_funds):
        raise ValueError("candidate universe is empty or invalid")
    if len(expected_funds) != len(set(expected_funds)):
        raise ValueError("duplicate candidate fund")
    if top_n <= 0 or effective_date <= rank_date:
        raise ValueError("invalid top_n or effective date")
    if len(observations) != len(expected_funds):
        raise ValueError("candidate ranking data is incomplete")
    observed = {item.fund_id: item for item in observations}
    if len(observed) != len(observations) or set(observed) != set(expected_funds):
        raise ValueError("candidate ranking identities do not match")
    ranked = []
    for fund_id in expected_funds:
        item = observed[fund_id]
        if item.rank_date != rank_date or not rank_date <= item.available_date < effective_date:
            raise ValueError(f"ranking date or availability is invalid: {fund_id}")
        if not _positive(item.shares) or not _positive(item.unit_nav):
            raise ValueError(f"ranking data missing or invalid: {fund_id}")
        ranked.append((fund_id, item.shares * item.unit_nav))
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return tuple(item[0] for item in ranked[:top_n])


def _positive(value: Decimal | None) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def calculate_ratio(
    trade_date: date,
    expected_funds: tuple[str, ...],
    observations: tuple[FundObservation, ...],
    denominator_date: date | None,
    denominator_cny: Decimal | None,
) -> RatioResult:
    """Calculate a complete ratio; missing inputs never become zero or stale values.

    ``unit_value`` must be the same-day unit NAV or unadjusted close, as chosen
    by the caller. The denominator must already be validated as the aggregate
    A-share circulating market cap for the effective index constituents.
    """
    if not expected_funds or any(not key for key in expected_funds):
        raise ValueError("expected_funds must contain non-empty fund identifiers")
    if len(set(expected_funds)) != len(expected_funds):
        raise ValueError("duplicate expected fund")
    by_id: dict[str, FundObservation] = {}
    for item in observations:
        if item.fund_id in by_id:
            raise ValueError("duplicate fund observation")
        if item.fund_id not in expected_funds:
            raise ValueError("unexpected fund observation")
        by_id[item.fund_id] = item

    missing: list[str] = []
    amounts: list[Decimal] = []
    for fund_id in expected_funds:
        item = by_id.get(fund_id)
        if item is None or item.trade_date != trade_date:
            missing.append(f"{fund_id}:observation")
        elif not _positive(item.shares) or not _positive(item.unit_value):
            missing.append(f"{fund_id}:shares_or_unit_value")
        else:
            try:
                amounts.append(item.shares * item.unit_value)
            except InvalidOperation as exc:
                raise ValueError("invalid numeric observation") from exc
    numerator = sum(amounts, Decimal(0)) if len(amounts) == len(expected_funds) else None
    denominator = denominator_cny if denominator_date == trade_date and _positive(denominator_cny) else None
    if denominator is None:
        missing.append("index:float_market_cap")
    ratio = numerator / denominator if numerator is not None and denominator is not None else None
    return RatioResult(trade_date, numerator, denominator, ratio, tuple(missing))
