from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(frozen=True, slots=True)
class EventStudySpec:
    symbol: str
    start_date: date
    end_date: date
    benchmark_symbol: str = "csi000300"
    windows: tuple[int, ...] = (5, 10, 20)
    adjustment: str = "hfq"
    entry_rule: str = "next_common_trading_day_close"
    round_trip_cost_bps: float = 20.0
    min_events: int = 1

    def __post_init__(self) -> None:
        normalized_symbol = self.symbol.strip()
        if len(normalized_symbol) != 6 or not normalized_symbol.isdigit():
            raise ValueError("symbol must be a six-digit A-share code")
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        normalized_windows = tuple(sorted(set(self.windows)))
        if not normalized_windows or any(window <= 0 for window in normalized_windows):
            raise ValueError("windows must contain positive trading-day counts")
        if self.adjustment not in {"qfq", "hfq", ""}:
            raise ValueError("adjustment must be qfq, hfq, or empty")
        if self.entry_rule != "next_common_trading_day_close":
            raise ValueError("V1 only supports next_common_trading_day_close")
        if self.round_trip_cost_bps < 0:
            raise ValueError("round_trip_cost_bps must be non-negative")
        if self.min_events < 1:
            raise ValueError("min_events must be at least 1")
        object.__setattr__(self, "symbol", normalized_symbol)
        object.__setattr__(self, "windows", normalized_windows)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["start_date"] = self.start_date.isoformat()
        payload["end_date"] = self.end_date.isoformat()
        payload["windows"] = list(self.windows)
        return payload


@dataclass(slots=True)
class EventStudyResult:
    spec: EventStudySpec
    events: pd.DataFrame
    summary: pd.DataFrame
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunOutput:
    run_id: str
    run_dir: Path
    result: EventStudyResult
    artifacts: dict[str, Path]
    snapshot_refs: dict[str, dict[str, Any]]

    def api_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": "succeeded",
            "spec": self.result.spec.to_dict(),
            "summary": self.result.summary.to_dict(orient="records"),
            "diagnostics": self.result.diagnostics,
            "artifacts": {name: str(path) for name, path in self.artifacts.items()},
            "snapshots": self.snapshot_refs,
        }

