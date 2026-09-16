from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ai_quant_assistant.domain.models import (
    BatchEventStudySpec,
    DailySignalRule,
    DailySignalStrategySpec,
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


class StockPoolSelectableRequest(BaseModel):
    stock_pool_id: str | None = Field(default=None, min_length=1, max_length=128)
    stock_pool_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_pool_revision(self) -> StockPoolSelectableRequest:
        if self.stock_pool_revision is not None and self.stock_pool_id is None:
            raise ValueError("stock_pool_revision requires stock_pool_id")
        symbols = getattr(self, "symbols", None)
        if self.stock_pool_id is not None and symbols is not None:
            raise ValueError("stock_pool_id and symbols cannot be provided together")
        return self


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


class BatchInterimReportEventStudyRequest(StockPoolSelectableRequest):
    source: Literal["fixture", "akshare", "local"] = "local"
    symbols: list[str] | None = Field(default=None, min_length=1, max_length=500)
    start_date: date = date(2012, 1, 1)
    end_date: date = Field(default_factory=date.today)
    benchmark_symbol: str = "csi000300"
    windows: list[int] = Field(default_factory=lambda: [5, 10, 20], min_length=1)
    adjustment: Literal["hfq", "qfq", ""] = "qfq"
    round_trip_cost_bps: float = Field(default=20.0, ge=0, le=500)
    min_events: int = Field(default=1, ge=1)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, symbols: list[str] | None) -> list[str] | None:
        if symbols is None:
            return None
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

    def to_spec(
        self,
        *,
        resolved_symbols: list[str] | tuple[str, ...] | None = None,
        stock_pool: dict[str, Any] | None = None,
    ) -> BatchEventStudySpec:
        return BatchEventStudySpec(
            symbols=tuple(resolved_symbols or self.symbols or ()),
            start_date=self.start_date,
            end_date=self.end_date,
            benchmark_symbol=self.benchmark_symbol,
            windows=tuple(self.windows),
            adjustment=self.adjustment,
            round_trip_cost_bps=self.round_trip_cost_bps,
            min_events=self.min_events,
            stock_pool=stock_pool,
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


class FactorBacktestRequest(StockPoolSelectableRequest):
    symbols: list[str] | None = Field(default=None, min_length=1, max_length=500)
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
    def validate_backtest_symbols(cls, symbols: list[str] | None) -> list[str] | None:
        if symbols is None:
            return None
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
        if self.symbols is not None and self.top_n > len(self.symbols):
            raise ValueError("top_n must not exceed the number of symbols")
        return self

    def to_spec(
        self,
        *,
        resolved_symbols: list[str] | tuple[str, ...] | None = None,
        stock_pool: dict[str, Any] | None = None,
    ) -> FactorBacktestSpec:
        return FactorBacktestSpec(
            symbols=tuple(resolved_symbols or self.symbols or ()),
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
            stock_pool=stock_pool,
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


TECHNICAL_FACTOR_RE = re.compile(
    r"^(?:return_(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|250)|ma_deviation_(?:[2-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|250))$"
)


class CandidateFactorRequest(BaseModel):
    code: str
    weight: float = Field(default=1.0, gt=0, le=1_000)
    direction: Literal["asc", "desc"] = "desc"

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        code = value.strip().lower()
        if code not in FINANCIAL_METRICS and not TECHNICAL_FACTOR_RE.fullmatch(code):
            raise ValueError("candidate factor must be financial, return_N, or ma_deviation_N")
        return code

    def to_component(self) -> FactorComponent:
        return FactorComponent(code=self.code, weight=self.weight, direction=self.direction)


class DailySignalRuleRequest(BaseModel):
    kind: Literal["close_vs_ma", "ma_compare", "ma_cross", "return_threshold"]
    operator: Literal["above", "below", "cross_above", "cross_below"]
    window: int | None = Field(default=None, ge=1, le=250)
    short_window: int | None = Field(default=None, ge=2, le=250)
    long_window: int | None = Field(default=None, ge=2, le=250)
    threshold: float | None = Field(default=None, ge=-10, le=10)

    @model_validator(mode="after")
    def validate_rule(self) -> DailySignalRuleRequest:
        self.to_rule()
        return self

    def to_rule(self) -> DailySignalRule:
        return DailySignalRule(**self.model_dump())


class DailySignalStrategyRequest(StockPoolSelectableRequest):
    symbols: list[str] | None = Field(default=None, min_length=1, max_length=500)
    start_date: date = date(2015, 1, 1)
    end_date: date = Field(default_factory=date.today)
    candidate_factors: list[CandidateFactorRequest] = Field(
        default_factory=lambda: [CandidateFactorRequest(code="roe")], min_length=1, max_length=12
    )
    candidate_count: int = Field(default=20, ge=1, le=500)
    candidate_refresh: Literal["daily", "monthly", "quarterly"] = "monthly"
    buy_rules: list[DailySignalRuleRequest] = Field(
        default_factory=lambda: [
            DailySignalRuleRequest(
                kind="ma_cross", operator="cross_above", short_window=5, long_window=20
            )
        ]
    )
    sell_rules: list[DailySignalRuleRequest] = Field(
        default_factory=lambda: [
            DailySignalRuleRequest(
                kind="ma_cross", operator="cross_below", short_window=5, long_window=20
            )
        ]
    )
    buy_rule_mode: Literal["all", "any"] = "all"
    sell_rule_mode: Literal["all", "any"] = "any"
    exit_on_candidate_drop: bool = False
    max_positions: int = Field(default=5, ge=1, le=500)
    benchmark_symbol: str = "csi000300"
    adjustment: Literal["none", "qfq", "hfq"] = "qfq"
    initial_capital: float = Field(default=1_000_000.0, gt=0, le=1_000_000_000_000)
    commission_bps: float = Field(default=5.0, ge=0, le=500)
    slippage_bps: float = Field(default=5.0, ge=0, le=500)
    stamp_duty_bps: float = Field(default=5.0, ge=0, le=500)
    lot_size: int = Field(default=100, ge=1, le=100_000)
    missing_policy: Literal["exclude", "available"] = "exclude"

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        values = list(dict.fromkeys(item.strip() for item in values))
        if any(not re.fullmatch(r"[0-9]{6}", item) for item in values):
            raise ValueError("symbols must contain six-digit A-share codes")
        return values

    @model_validator(mode="after")
    def validate_strategy(self) -> DailySignalStrategyRequest:
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be earlier than end_date")
        if self.symbols is not None:
            self.to_spec()
        return self

    def to_spec(self, *, resolved_symbols=None, stock_pool=None) -> DailySignalStrategySpec:
        return DailySignalStrategySpec(
            symbols=tuple(resolved_symbols or self.symbols or ()),
            start_date=self.start_date,
            end_date=self.end_date,
            candidate_factors=tuple(item.to_component() for item in self.candidate_factors),
            candidate_count=self.candidate_count,
            candidate_refresh=self.candidate_refresh,
            buy_rules=tuple(item.to_rule() for item in self.buy_rules),
            sell_rules=tuple(item.to_rule() for item in self.sell_rules),
            buy_rule_mode=self.buy_rule_mode,
            sell_rule_mode=self.sell_rule_mode,
            exit_on_candidate_drop=self.exit_on_candidate_drop,
            max_positions=self.max_positions,
            benchmark_symbol=self.benchmark_symbol,
            adjustment=self.adjustment,
            initial_capital=self.initial_capital,
            commission_bps=self.commission_bps,
            slippage_bps=self.slippage_bps,
            stamp_duty_bps=self.stamp_duty_bps,
            lot_size=self.lot_size,
            missing_policy=self.missing_policy,
            stock_pool=stock_pool,
        )


class FactorLabRequest(StockPoolSelectableRequest):
    symbols: list[str] | None = Field(default=None, min_length=2, max_length=500)
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
        if self.symbols is not None:
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

    def to_spec(
        self,
        *,
        resolved_symbols: list[str] | tuple[str, ...] | None = None,
        stock_pool: dict[str, Any] | None = None,
    ) -> FactorLabSpec:
        return FactorLabSpec(
            symbols=tuple(resolved_symbols or self.symbols or ()),
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
            stock_pool=stock_pool,
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


class PointInTimeScreenRequest(StockPoolSelectableRequest):
    name: str = Field(default="未命名条件选股", min_length=1, max_length=120)
    as_of: date = Field(default_factory=date.today)
    conditions: list[ScreenConditionRequest] = Field(min_length=1, max_length=12)
    symbols: list[str] | None = Field(default=None, max_length=500)
    universe_mode: Literal["explicit", "all_available", "historical_index", "observed_index"] = (
        "explicit"
    )
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
    def validate_screen_symbols(cls, symbols: list[str] | None) -> list[str] | None:
        if symbols is None:
            return None
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
        if self.symbols is not None or self.universe_mode != "explicit":
            self.to_spec()
        return self

    def to_spec(
        self,
        *,
        resolved_symbols: list[str] | tuple[str, ...] | None = None,
        stock_pool: dict[str, Any] | None = None,
    ) -> ScreenSpec:
        return ScreenSpec(
            name=self.name,
            as_of=self.as_of,
            conditions=tuple(item.to_condition() for item in self.conditions),
            symbols=tuple(resolved_symbols or self.symbols or ()),
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
            stock_pool=stock_pool,
        )


class SaveWatchlistRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class StockPoolCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    source_type: Literal["manual", "imported"] = "manual"
    symbols: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if any(not re.fullmatch(r"\d{6}", value) for value in normalized):
            raise ValueError("symbols must contain six-digit A-share codes")
        return normalized


class StockPoolUpdateRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def require_change(self) -> StockPoolUpdateRequest:
        if self.name is None and self.description is None:
            raise ValueError("name or description is required")
        return self


class StockPoolMembersAddRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    symbols: list[str] = Field(min_length=1, max_length=200)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not normalized or any(not re.fullmatch(r"\d{6}", value) for value in normalized):
            raise ValueError("symbols must contain six-digit A-share codes")
        return normalized


class StockPoolMembersRemoveRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    security_ids: list[str] = Field(min_length=1, max_length=200)


class StockPoolMemberUpdateRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=500)
    display_order: int | None = Field(default=None, ge=1, le=500)

    @model_validator(mode="after")
    def require_change(self) -> StockPoolMemberUpdateRequest:
        if self.note is None and self.display_order is None:
            raise ValueError("note or display_order is required")
        return self


class StockPoolRevisionRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class StockPoolDuplicateRequest(StockPoolRevisionRequest):
    name: str = Field(min_length=1, max_length=120)


class StockPoolSyncRequest(StockPoolRevisionRequest):
    as_of: date = Field(default_factory=date.today)


class SecurityMasterRefreshRequest(BaseModel):
    model_config = {"extra": "forbid"}
    confirm: bool = Field(strict=True)

    @field_validator("confirm")
    @classmethod
    def require_confirmation(cls, value: bool) -> bool:
        if not value:
            raise ValueError("请明确确认更新证券主数据；该操作会访问公网")
        return value


class DocumentQuestionRequest(BaseModel):
    document_ids: list[str] = Field(min_length=1, max_length=6)
    question: str = Field(min_length=1, max_length=2_000)

    @field_validator("document_ids")
    @classmethod
    def validate_document_ids(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if len(normalized) != len(values):
            raise ValueError("document_ids must be non-empty and unique")
        if any(not re.fullmatch(r"doc-[0-9a-f]{32}", value) for value in normalized):
            raise ValueError("document_ids contain an invalid identifier")
        return normalized


class DocumentCompareRequest(BaseModel):
    document_ids: list[str] = Field(min_length=2, max_length=6)
    focus: str = Field(
        default="比较主要财务表现、经营变化、风险和修订差异", min_length=1, max_length=500
    )

    @field_validator("document_ids")
    @classmethod
    def validate_document_ids(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if len(normalized) != len(values):
            raise ValueError("document_ids must be non-empty and unique")
        if any(not re.fullmatch(r"doc-[0-9a-f]{32}", value) for value in normalized):
            raise ValueError("document_ids contain an invalid identifier")
        return normalized


class DocumentEventReviewRequest(BaseModel):
    status: Literal["accepted", "rejected"]
    note: str = Field(default="", max_length=1_000)


class DocumentEventStudyRequest(BaseModel):
    source: Literal["fixture", "akshare", "local"] = "local"
    as_of: datetime = Field(default_factory=lambda: datetime.now(UTC))
    benchmark_symbol: str = Field(default="csi000300", min_length=8, max_length=16)
    windows: list[int] = Field(default_factory=lambda: [5, 10, 20], min_length=1, max_length=10)
    adjustment: Literal["hfq", "qfq", ""] = "qfq"
    round_trip_cost_bps: float = Field(default=20.0, ge=0, le=500)

    @field_validator("windows")
    @classmethod
    def validate_windows(cls, windows: list[int]) -> list[int]:
        normalized = sorted(set(windows))
        if any(window <= 0 or window > 250 for window in normalized):
            raise ValueError("windows must be between 1 and 250 trading days")
        return normalized

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("as_of must include an explicit timezone")
        return value
