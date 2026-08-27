from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ai_quant_assistant.domain.models import (
    DEFAULT_RESEARCH_SYMBOLS,
    BatchEventStudySpec,
    EventStudySpec,
    FactorBacktestSpec,
    FactorComponent,
    FactorLabSpec,
)
from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS
from ai_quant_assistant.research.screener import (
    SCREEN_FIELDS,
    ScreenCondition,
    ScreenSpec,
)


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
    universe_mode: Literal["explicit", "all_available", "historical_index", "observed_index"] = (
        "explicit"
    )
    universe_benchmark: str = "csi000300"
    classification_system: str = "provider"
    industry_neutralization: Literal["none", "within_industry_rank"] = "none"
    max_industry_weight: float = Field(default=1.0, gt=0, le=1)
    require_verified_industry: bool = False

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
            universe_mode=self.universe_mode,
            universe_benchmark=self.universe_benchmark,
            classification_system=self.classification_system,
            industry_neutralization=self.industry_neutralization,
            max_industry_weight=self.max_industry_weight,
            require_verified_industry=self.require_verified_industry,
        )


class IncrementalSyncRequest(BaseModel):
    as_of: date = Field(default_factory=date.today)


class FactorComponentRequest(BaseModel):
    code: str
    weight: float = Field(default=1.0, gt=0, le=1_000)
    direction: Literal["asc", "desc"] = "desc"

    @field_validator("code")
    @classmethod
    def validate_factor_code(cls, value: str) -> str:
        code = value.strip().lower()
        if code not in FINANCIAL_METRICS:
            raise ValueError("factor must be a supported published financial metric")
        return code

    def to_component(self) -> FactorComponent:
        return FactorComponent(code=self.code, weight=self.weight, direction=self.direction)


class FactorLabRequest(BaseModel):
    symbols: list[str] = Field(
        default_factory=lambda: list(DEFAULT_RESEARCH_SYMBOLS), min_length=2, max_length=500
    )
    start_date: date = date(2015, 1, 1)
    end_date: date = Field(default_factory=date.today)
    factors: list[FactorComponentRequest] = Field(
        default_factory=lambda: [
            FactorComponentRequest(code="roe"),
            FactorComponentRequest(code="debt_asset_ratio", direction="asc"),
        ],
        min_length=1,
        max_length=12,
    )
    top_n_values: list[int] = Field(default_factory=lambda: [3], min_length=1, max_length=12)
    rebalance_values: list[Literal["monthly", "quarterly"]] = Field(
        default_factory=lambda: ["quarterly"], min_length=1, max_length=2
    )
    commission_bps_values: list[float] = Field(
        default_factory=lambda: [5.0], min_length=1, max_length=8
    )
    quantiles: int = Field(default=5, ge=2, le=10)
    benchmark_symbol: str = "csi000300"
    adjustment: Literal["none", "qfq", "hfq"] = "qfq"
    initial_capital: float = Field(default=1_000_000.0, gt=0, le=1_000_000_000_000)
    slippage_bps: float = Field(default=5.0, ge=0, le=500)
    stamp_duty_bps: float = Field(default=5.0, ge=0, le=500)
    lot_size: int = Field(default=100, ge=1, le=100_000)
    missing_policy: Literal["exclude", "available"] = "exclude"
    training_fraction: float = Field(default=0.6, gt=0, lt=1)
    validation_fraction: float = Field(default=0.2, gt=0, lt=1)
    walk_forward_folds: int = Field(default=3, ge=1, le=12)
    max_combinations: int = Field(default=48, ge=1, le=200)
    universe_mode: Literal["explicit", "all_available", "historical_index", "observed_index"] = (
        "explicit"
    )
    universe_benchmark: str = "csi000300"
    classification_system: str = "provider"
    industry_neutralization: Literal["none", "within_industry_rank"] = "none"
    max_industry_weight: float = Field(default=1.0, gt=0, le=1)
    require_verified_industry: bool = False

    @model_validator(mode="after")
    def validate_lab(self) -> FactorLabRequest:
        self.to_spec()
        count = (
            (len(self.factors) + (1 if len(self.factors) > 1 else 0))
            * len(set(self.top_n_values))
            * len(set(self.rebalance_values))
            * len(set(self.commission_bps_values))
        )
        if count > self.max_combinations:
            raise ValueError(
                f"parameter grid contains {count} combinations, exceeding max_combinations={self.max_combinations}"
            )
        return self

    def to_spec(self) -> FactorLabSpec:
        return FactorLabSpec(
            symbols=tuple(self.symbols),
            start_date=self.start_date,
            end_date=self.end_date,
            factors=tuple(item.to_component() for item in self.factors),
            top_n_values=tuple(self.top_n_values),
            rebalance_values=tuple(self.rebalance_values),
            commission_bps_values=tuple(self.commission_bps_values),
            quantiles=self.quantiles,
            benchmark_symbol=self.benchmark_symbol,
            adjustment=self.adjustment,
            initial_capital=self.initial_capital,
            slippage_bps=self.slippage_bps,
            stamp_duty_bps=self.stamp_duty_bps,
            lot_size=self.lot_size,
            missing_policy=self.missing_policy,
            training_fraction=self.training_fraction,
            validation_fraction=self.validation_fraction,
            walk_forward_folds=self.walk_forward_folds,
            max_combinations=self.max_combinations,
            universe_mode=self.universe_mode,
            universe_benchmark=self.universe_benchmark,
            classification_system=self.classification_system,
            industry_neutralization=self.industry_neutralization,
            max_industry_weight=self.max_industry_weight,
            require_verified_industry=self.require_verified_industry,
        )


class BackupRestoreRequest(BaseModel):
    confirm_backup_id: str = Field(min_length=1, max_length=128)


class BackupPruneRequest(BaseModel):
    apply: bool = False


class CopilotConversationRequest(BaseModel):
    title: str = Field(default="新研究对话", min_length=1, max_length=120)


class CopilotMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class CopilotConfirmRequest(BaseModel):
    revision: int = Field(default=1, ge=1)


class ScreenConditionRequest(BaseModel):
    field: str
    operator: Literal["gt", "gte", "lt", "lte", "between"]
    value: float
    upper_value: float | None = None

    @field_validator("field")
    @classmethod
    def validate_field(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in SCREEN_FIELDS:
            raise ValueError("field must be a supported published screen field")
        return normalized

    def to_condition(self) -> ScreenCondition:
        return ScreenCondition(
            field=self.field,
            operator=self.operator,
            value=self.value,
            upper_value=self.upper_value,
        )


class PointInTimeScreenRequest(BaseModel):
    name: str = Field(default="未命名条件选股", min_length=1, max_length=120)
    as_of: date = Field(default_factory=date.today)
    conditions: list[ScreenConditionRequest] = Field(min_length=1, max_length=12)
    symbols: list[str] = Field(
        default_factory=lambda: list(DEFAULT_RESEARCH_SYMBOLS), max_length=500
    )
    universe_mode: Literal[
        "explicit", "all_available", "historical_index", "observed_index"
    ] = "explicit"
    benchmark_symbol: str = "csi000300"
    classification_system: str = Field(default="provider", min_length=1, max_length=80)
    include_industries: list[str] = Field(default_factory=list, max_length=100)
    exclude_industries: list[str] = Field(default_factory=list, max_length=100)
    require_verified_industry: bool = False
    missing_policy: Literal["exclude", "show"] = "show"
    sort_by: str | None = None
    sort_direction: Literal["asc", "desc"] = "desc"
    top_n: int | None = Field(default=None, ge=1, le=500)

    @field_validator("symbols")
    @classmethod
    def validate_screen_symbols(cls, symbols: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in symbols if item.strip()))
        if any(len(item) != 6 or not item.isdigit() for item in normalized):
            raise ValueError("symbols must contain six-digit A-share codes")
        return normalized

    @field_validator("sort_by")
    @classmethod
    def validate_sort_by(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in SCREEN_FIELDS:
            raise ValueError("sort_by must be a supported published screen field")
        return normalized

    @model_validator(mode="after")
    def validate_screen(self) -> PointInTimeScreenRequest:
        self.to_spec()
        return self

    def to_spec(self) -> ScreenSpec:
        return ScreenSpec(
            name=self.name,
            as_of=self.as_of,
            conditions=tuple(item.to_condition() for item in self.conditions),
            symbols=tuple(self.symbols),
            universe_mode=self.universe_mode,
            benchmark_symbol=self.benchmark_symbol,
            classification_system=self.classification_system,
            include_industries=tuple(self.include_industries),
            exclude_industries=tuple(self.exclude_industries),
            require_verified_industry=self.require_verified_industry,
            missing_policy=self.missing_policy,
            sort_by=self.sort_by,
            sort_direction=self.sort_direction,
            top_n=self.top_n,
        )


class SaveWatchlistRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
