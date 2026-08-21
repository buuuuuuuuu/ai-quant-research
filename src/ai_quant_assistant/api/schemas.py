from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ai_quant_assistant.domain.models import (
    DEFAULT_RESEARCH_SYMBOLS,
    BatchEventStudySpec,
    EventStudySpec,
    FactorBacktestSpec,
)
from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS


class InterimReportEventStudyRequest(BaseModel):
    source: Literal["fixture", "akshare", "local"] = "fixture"
    symbol: str = Field(default="600519", pattern=r"^\d{6}$")
    start_date: date = date(2012, 1, 1)
    end_date: date = date(2025, 12, 31)
    benchmark_symbol: str = "csi000300"
    windows: list[int] = Field(default_factory=lambda: [5, 10, 20], min_length=1)
    adjustment: Literal["hfq", "qfq", ""] = "hfq"
    round_trip_cost_bps: float = Field(default=20.0, ge=0, le=500)
    min_events: int = Field(default=1, ge=1)

    @field_validator("windows")
    @classmethod
    def validate_windows(cls, windows: list[int]) -> list[int]:
        normalized = sorted(set(windows))
        if any(window <= 0 or window > 250 for window in normalized):
            raise ValueError("windows must be between 1 and 250 trading days")
        return normalized

    @model_validator(mode="after")
    def validate_date_range(self) -> InterimReportEventStudyRequest:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        return self

    def to_spec(self) -> EventStudySpec:
        return EventStudySpec(
            symbol=self.symbol,
            start_date=self.start_date,
            end_date=self.end_date,
            benchmark_symbol=self.benchmark_symbol,
            windows=tuple(self.windows),
            adjustment=self.adjustment,
            round_trip_cost_bps=self.round_trip_cost_bps,
            min_events=self.min_events,
        )


class ResearchRunResponse(BaseModel):
    run_id: str
    status: Literal["succeeded"]
    spec: dict[str, Any]
    summary: list[dict[str, Any]]
    diagnostics: dict[str, Any]
    artifacts: dict[str, str]
    snapshots: dict[str, dict[str, Any]]


class BatchInterimReportEventStudyRequest(BaseModel):
    source: Literal["fixture", "akshare", "local"] = "local"
    symbols: list[str] = Field(default_factory=lambda: list(DEFAULT_RESEARCH_SYMBOLS), min_length=1)
    start_date: date = date(2012, 1, 1)
    end_date: date = Field(default_factory=date.today)
    benchmark_symbol: str = "csi000300"
    windows: list[int] = Field(default_factory=lambda: [5, 10, 20], min_length=1)
    adjustment: Literal["hfq", "qfq", ""] = "qfq"
    round_trip_cost_bps: float = Field(default=20.0, ge=0, le=500)
    min_events: int = Field(default=1, ge=1)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, symbols: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in symbols))
        if any(len(item) != 6 or not item.isdigit() for item in normalized):
            raise ValueError("symbols must contain six-digit A-share codes")
        return normalized

    @field_validator("windows")
    @classmethod
    def validate_windows(cls, windows: list[int]) -> list[int]:
        normalized = sorted(set(windows))
        if any(window <= 0 or window > 250 for window in normalized):
            raise ValueError("windows must be between 1 and 250 trading days")
        return normalized

    @model_validator(mode="after")
    def validate_date_range(self) -> BatchInterimReportEventStudyRequest:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        return self

    def to_spec(self) -> BatchEventStudySpec:
        return BatchEventStudySpec(
            symbols=tuple(self.symbols),
            start_date=self.start_date,
            end_date=self.end_date,
            benchmark_symbol=self.benchmark_symbol,
            windows=tuple(self.windows),
            adjustment=self.adjustment,
            round_trip_cost_bps=self.round_trip_cost_bps,
            min_events=self.min_events,
        )


class BatchResearchRunResponse(BaseModel):
    batch_id: str
    status: Literal["succeeded", "partial", "failed"]
    started_at: str
    completed_at: str
    duration_seconds: float
    provider: str
    spec: dict[str, Any]
    requested_symbols: int
    successful_symbols: int
    failed_symbols: int
    pooled_summary: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    artifacts: dict[str, str]


class FactorBacktestRequest(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: list(DEFAULT_RESEARCH_SYMBOLS), min_length=1)
    start_date: date = date(2015, 1, 1)
    end_date: date = Field(default_factory=date.today)
    factor: str = "roe"
    factor_direction: Literal["asc", "desc"] = "desc"
    top_n: int = Field(default=3, ge=1)
    rebalance: Literal["monthly", "quarterly"] = "quarterly"
    benchmark_symbol: str = "csi000300"
    adjustment: Literal["none", "qfq", "hfq"] = "qfq"
    initial_capital: float = Field(default=1_000_000.0, gt=0, le=1_000_000_000_000)
    commission_bps: float = Field(default=5.0, ge=0, le=500)
    slippage_bps: float = Field(default=5.0, ge=0, le=500)
    stamp_duty_bps: float = Field(default=5.0, ge=0, le=500)
    lot_size: int = Field(default=100, ge=1, le=100_000)

    @field_validator("symbols")
    @classmethod
    def validate_backtest_symbols(cls, symbols: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in symbols))
        if len(normalized) > 500:
            raise ValueError("symbols must contain no more than 500 items")
        if any(len(item) != 6 or not item.isdigit() for item in normalized):
            raise ValueError("symbols must contain six-digit A-share codes")
        return normalized

    @field_validator("factor")
    @classmethod
    def validate_factor(cls, factor: str) -> str:
        normalized = factor.strip().lower()
        if normalized not in FINANCIAL_METRICS:
            raise ValueError("factor must be a supported published financial metric")
        return normalized

    @model_validator(mode="after")
    def validate_backtest_range(self) -> FactorBacktestRequest:
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be earlier than end_date")
        if self.top_n > len(self.symbols):
            raise ValueError("top_n must not exceed the number of symbols")
        return self

    def to_spec(self) -> FactorBacktestSpec:
        return FactorBacktestSpec(
            symbols=tuple(self.symbols),
            start_date=self.start_date,
            end_date=self.end_date,
            factor=self.factor,
            factor_direction=self.factor_direction,
            top_n=self.top_n,
            rebalance=self.rebalance,
            benchmark_symbol=self.benchmark_symbol,
            adjustment=self.adjustment,
            initial_capital=self.initial_capital,
            commission_bps=self.commission_bps,
            slippage_bps=self.slippage_bps,
            stamp_duty_bps=self.stamp_duty_bps,
            lot_size=self.lot_size,
        )


class IncrementalSyncRequest(BaseModel):
    as_of: date = Field(default_factory=date.today)
