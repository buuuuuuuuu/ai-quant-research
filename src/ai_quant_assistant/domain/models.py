from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

REPORT_TYPES = ("q1", "interim", "q3", "annual")
DEFAULT_RESEARCH_SYMBOLS = (
    "600519",
    "000001",
    "601318",
    "000858",
    "600036",
    "000333",
    "600276",
    "601888",
    "002594",
    "300750",
)


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


@dataclass(frozen=True, slots=True)
class DataSyncSpec:
    start_date: date
    end_date: date
    symbols: tuple[str, ...] = ()
    adjustments: tuple[str, ...] = ("none", "qfq", "hfq")
    symbol_limit: int | None = None
    resume: bool = True

    def __post_init__(self) -> None:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        symbols = tuple(dict.fromkeys(symbol.strip() for symbol in self.symbols))
        if any(len(symbol) != 6 or not symbol.isdigit() for symbol in symbols):
            raise ValueError("symbols must contain six-digit A-share codes")
        requested = set(self.adjustments)
        if not requested or not requested.issubset({"none", "qfq", "hfq"}):
            raise ValueError("adjustments must contain none, qfq, or hfq")
        if requested & {"qfq", "hfq"}:
            requested.add("none")
        adjustments = tuple(item for item in ("none", "qfq", "hfq") if item in requested)
        if self.symbol_limit is not None and self.symbol_limit < 1:
            raise ValueError("symbol_limit must be positive")
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "adjustments", adjustments)

    def request_dict(self) -> dict[str, Any]:
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "symbols": list(self.symbols),
            "adjustments": list(self.adjustments),
            "symbol_limit": self.symbol_limit,
        }


def normalize_benchmark_symbol(symbol: str) -> str:
    normalized = symbol.strip().lower()
    if normalized.isdigit() and len(normalized) == 6:
        normalized = f"csi{normalized}"
    if normalized.startswith("csi"):
        code = normalized[3:]
    elif normalized.startswith(("sh", "sz")):
        code = normalized[2:]
    else:
        raise ValueError("benchmark symbols must use csi, sh, or sz prefixes")
    if len(code) != 6 or not code.isdigit():
        raise ValueError("benchmark symbols must end with a six-digit index code")
    return normalized


@dataclass(frozen=True, slots=True)
class BenchmarkSyncSpec:
    start_date: date
    end_date: date
    symbols: tuple[str, ...] = ("csi000300",)
    resume: bool = True

    def __post_init__(self) -> None:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        symbols = tuple(dict.fromkeys(normalize_benchmark_symbol(item) for item in self.symbols))
        if not symbols:
            raise ValueError("benchmark symbols must contain at least one index")
        object.__setattr__(self, "symbols", symbols)

    def request_dict(self) -> dict[str, Any]:
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "symbols": list(self.symbols),
        }


@dataclass(frozen=True, slots=True)
class AnnouncementSyncSpec:
    start_date: date
    end_date: date
    symbols: tuple[str, ...]
    report_types: tuple[str, ...] = ("interim",)
    resume: bool = True

    def __post_init__(self) -> None:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        symbols = tuple(dict.fromkeys(item.strip() for item in self.symbols))
        if not symbols or any(len(item) != 6 or not item.isdigit() for item in symbols):
            raise ValueError("symbols must contain at least one six-digit A-share code")
        report_types = tuple(dict.fromkeys(item.strip().lower() for item in self.report_types))
        if not report_types or not set(report_types).issubset(set(REPORT_TYPES)):
            raise ValueError("report_types must contain q1, interim, q3, or annual")
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "report_types", report_types)

    def request_dict(self) -> dict[str, Any]:
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "symbols": list(self.symbols),
            "report_types": list(self.report_types),
        }


@dataclass(frozen=True, slots=True)
class FinancialSyncSpec:
    start_date: date
    end_date: date
    symbols: tuple[str, ...]
    resume: bool = True

    def __post_init__(self) -> None:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        symbols = tuple(dict.fromkeys(item.strip() for item in self.symbols))
        if not symbols or any(len(item) != 6 or not item.isdigit() for item in symbols):
            raise ValueError("symbols must contain at least one six-digit A-share code")
        object.__setattr__(self, "symbols", symbols)

    def request_dict(self) -> dict[str, Any]:
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "symbols": list(self.symbols),
        }


@dataclass(frozen=True, slots=True)
class ResearchBackfillSpec:
    start_date: date
    end_date: date
    symbols: tuple[str, ...] = DEFAULT_RESEARCH_SYMBOLS
    benchmark_symbol: str = "csi000300"
    adjustments: tuple[str, ...] = ("qfq",)
    report_types: tuple[str, ...] = REPORT_TYPES
    windows: tuple[int, ...] = (5, 10, 20)
    resume: bool = True

    def __post_init__(self) -> None:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        symbols = tuple(dict.fromkeys(item.strip() for item in self.symbols))
        if not symbols or any(len(item) != 6 or not item.isdigit() for item in symbols):
            raise ValueError("symbols must contain at least one six-digit A-share code")
        requested_adjustments = set(self.adjustments)
        if not requested_adjustments or not requested_adjustments.issubset({"none", "qfq", "hfq"}):
            raise ValueError("adjustments must contain none, qfq, or hfq")
        report_types = tuple(dict.fromkeys(item.strip().lower() for item in self.report_types))
        if not report_types or not set(report_types).issubset(set(REPORT_TYPES)):
            raise ValueError("report_types must contain q1, interim, q3, or annual")
        windows = tuple(sorted(set(self.windows)))
        if not windows or any(item <= 0 for item in windows):
            raise ValueError("windows must contain positive trading-day counts")
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(
            self, "benchmark_symbol", normalize_benchmark_symbol(self.benchmark_symbol)
        )
        object.__setattr__(self, "report_types", report_types)
        object.__setattr__(self, "windows", windows)


@dataclass(frozen=True, slots=True)
class BatchEventStudySpec:
    symbols: tuple[str, ...]
    start_date: date
    end_date: date
    benchmark_symbol: str = "csi000300"
    windows: tuple[int, ...] = (5, 10, 20)
    adjustment: str = "qfq"
    round_trip_cost_bps: float = 20.0
    min_events: int = 1

    def __post_init__(self) -> None:
        symbols = tuple(dict.fromkeys(item.strip() for item in self.symbols))
        if not symbols or any(len(item) != 6 or not item.isdigit() for item in symbols):
            raise ValueError("symbols must contain at least one six-digit A-share code")
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        windows = tuple(sorted(set(self.windows)))
        if not windows or any(item <= 0 for item in windows):
            raise ValueError("windows must contain positive trading-day counts")
        if self.adjustment not in {"qfq", "hfq", ""}:
            raise ValueError("adjustment must be qfq, hfq, or empty")
        if self.round_trip_cost_bps < 0:
            raise ValueError("round_trip_cost_bps must be non-negative")
        if self.min_events < 1:
            raise ValueError("min_events must be at least 1")
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "windows", windows)

    def event_spec(self, symbol: str) -> EventStudySpec:
        return EventStudySpec(
            symbol=symbol,
            start_date=self.start_date,
            end_date=self.end_date,
            benchmark_symbol=self.benchmark_symbol,
            windows=self.windows,
            adjustment=self.adjustment,
            round_trip_cost_bps=self.round_trip_cost_bps,
            min_events=self.min_events,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "benchmark_symbol": self.benchmark_symbol,
            "windows": list(self.windows),
            "adjustment": self.adjustment,
            "round_trip_cost_bps": self.round_trip_cost_bps,
            "min_events": self.min_events,
        }


@dataclass(frozen=True, slots=True)
class FactorBacktestSpec:
    symbols: tuple[str, ...]
    start_date: date
    end_date: date
    factor: str = "roe"
    top_n: int = 3
    rebalance: str = "quarterly"
    benchmark_symbol: str = "csi000300"
    adjustment: str = "qfq"
    factor_direction: str = "desc"
    initial_capital: float = 1_000_000.0
    commission_bps: float = 5.0
    slippage_bps: float = 5.0
    stamp_duty_bps: float = 5.0
    lot_size: int = 100

    def __post_init__(self) -> None:
        symbols = tuple(dict.fromkeys(item.strip() for item in self.symbols))
        if not symbols or any(len(item) != 6 or not item.isdigit() for item in symbols):
            raise ValueError("symbols must contain at least one six-digit A-share code")
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be earlier than end_date")
        if not self.factor.strip():
            raise ValueError("factor must not be empty")
        if self.top_n < 1 or self.top_n > len(symbols):
            raise ValueError("top_n must be between one and the number of requested symbols")
        if self.rebalance not in {"monthly", "quarterly"}:
            raise ValueError("rebalance must be monthly or quarterly")
        if self.adjustment not in {"qfq", "hfq", "none"}:
            raise ValueError("adjustment must be qfq, hfq, or none")
        if self.factor_direction not in {"asc", "desc"}:
            raise ValueError("factor_direction must be asc or desc")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if min(self.commission_bps, self.slippage_bps, self.stamp_duty_bps) < 0:
            raise ValueError("transaction cost basis points must be non-negative")
        if self.lot_size < 1:
            raise ValueError("lot_size must be positive")
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "factor", self.factor.strip().lower())
        object.__setattr__(
            self, "benchmark_symbol", normalize_benchmark_symbol(self.benchmark_symbol)
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["symbols"] = list(self.symbols)
        payload["start_date"] = self.start_date.isoformat()
        payload["end_date"] = self.end_date.isoformat()
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
