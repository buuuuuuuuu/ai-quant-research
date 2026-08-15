from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ai_quant_assistant.domain.models import EventStudySpec


class InterimReportEventStudyRequest(BaseModel):
    source: Literal["fixture", "akshare"] = "fixture"
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
