from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from uuid import uuid4

import pandas as pd

from ai_quant_assistant.domain.models import normalize_benchmark_symbol
from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.universe import UniverseRepository

SCREEN_OPERATORS = {"gt", "gte", "lt", "lte", "between"}
MARKET_FIELDS = {
    "close": {"label": "最近收盘价", "unit": "CNY", "dataset": "daily_bar"},
    "amount": {"label": "最近成交额", "unit": "CNY", "dataset": "daily_bar"},
    "volume": {"label": "最近成交量", "unit": "SHARE", "dataset": "daily_bar"},
    "avg_amount_20d": {
        "label": "近20交易日平均成交额",
        "unit": "CNY",
        "dataset": "daily_bar",
    },
}
SCREEN_FIELDS = {
    **{
        code: {
            "label": definition["label"],
            "unit": definition["unit"],
            "dataset": "financial_metric",
        }
        for code, definition in FINANCIAL_METRICS.items()
    },
    **MARKET_FIELDS,
}


@dataclass(frozen=True, slots=True)
class ScreenCondition:
    field: str
    operator: str
    value: float
    upper_value: float | None = None

    def __post_init__(self) -> None:
        field = self.field.strip().lower()
        if field not in SCREEN_FIELDS:
            raise ValueError(f"unsupported screen field: {field}")
        if self.operator not in SCREEN_OPERATORS:
            raise ValueError(f"unsupported screen operator: {self.operator}")
        if not math.isfinite(self.value):
            raise ValueError("screen condition value must be finite")
        if self.operator == "between":
            if self.upper_value is None or not math.isfinite(self.upper_value):
                raise ValueError("between condition requires a finite upper_value")
            if self.upper_value < self.value:
                raise ValueError("between upper_value must not be lower than value")
        elif self.upper_value is not None:
            raise ValueError("upper_value is only valid for between conditions")
        object.__setattr__(self, "field", field)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScreenSpec:
    name: str
    as_of: date
    conditions: tuple[ScreenCondition, ...]
    symbols: tuple[str, ...] = ()
    universe_mode: str = "explicit"
    benchmark_symbol: str = "csi000300"
    classification_system: str = "provider"
    include_industries: tuple[str, ...] = ()
    exclude_industries: tuple[str, ...] = ()
    require_verified_industry: bool = False
    missing_policy: str = "show"
    sort_by: str | None = None
    sort_direction: str = "desc"
    top_n: int | None = None

    def __post_init__(self) -> None:
        symbols = tuple(dict.fromkeys(item.strip() for item in self.symbols if item.strip()))
        if any(len(item) != 6 or not item.isdigit() for item in symbols):
            raise ValueError("symbols must contain six-digit A-share codes")
        if len(symbols) > 500:
            raise ValueError("symbols must contain no more than 500 items")
        if self.universe_mode not in {
            "explicit",
            "all_available",
            "historical_index",
            "observed_index",
        }:
            raise ValueError("unsupported universe mode")
        if self.universe_mode == "explicit" and not symbols:
            raise ValueError("explicit universe mode requires symbols")
        if not self.conditions or len(self.conditions) > 12:
            raise ValueError("screen requires between one and twelve conditions")
        if len({item.field for item in self.conditions}) != len(self.conditions):
            raise ValueError("screen condition fields must be unique")
        include = tuple(
            dict.fromkeys(item.strip() for item in self.include_industries if item.strip())
        )
        exclude = tuple(
            dict.fromkeys(item.strip() for item in self.exclude_industries if item.strip())
        )
        if set(include) & set(exclude):
            raise ValueError("an industry cannot be both included and excluded")
        if self.missing_policy not in {"exclude", "show"}:
            raise ValueError("missing_policy must be exclude or show")
        sort_by = (self.sort_by or self.conditions[0].field).strip().lower()
        if sort_by not in SCREEN_FIELDS:
            raise ValueError("sort_by must be a supported screen field")
        if self.sort_direction not in {"asc", "desc"}:
            raise ValueError("sort_direction must be asc or desc")
        if self.top_n is not None and not 1 <= self.top_n <= 500:
            raise ValueError("top_n must be between one and 500")
        name = self.name.strip()
        if not name or len(name) > 120:
            raise ValueError("screen name must contain between one and 120 characters")
        if not self.classification_system.strip():
            raise ValueError("classification_system must not be empty")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "include_industries", include)
        object.__setattr__(self, "exclude_industries", exclude)
        object.__setattr__(self, "sort_by", sort_by)
        object.__setattr__(
            self, "benchmark_symbol", normalize_benchmark_symbol(self.benchmark_symbol)
        )

    @property
    def cutoff(self) -> datetime:
        return datetime.combine(self.as_of, time.max)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "as_of": self.as_of.isoformat(),
            "conditions": [item.to_dict() for item in self.conditions],
            "symbols": list(self.symbols),
            "universe_mode": self.universe_mode,
            "benchmark_symbol": self.benchmark_symbol,
            "classification_system": self.classification_system,
            "include_industries": list(self.include_industries),
            "exclude_industries": list(self.exclude_industries),
            "require_verified_industry": self.require_verified_industry,
            "missing_policy": self.missing_policy,
            "sort_by": self.sort_by,
            "sort_direction": self.sort_direction,
            "top_n": self.top_n,
        }


class PointInTimeScreener:
    """Evaluate a whitelist screen against data that was knowable at one close-of-day cutoff."""

    def __init__(self, database: LocalDatabase) -> None:
        self.database = database
        self.universe = UniverseRepository(database)

    @staticmethod
    def fields() -> dict[str, Any]:
        return {
            "fields": [
                {"code": code, **definition}
                for code, definition in SCREEN_FIELDS.items()
            ],
            "operators": sorted(SCREEN_OPERATORS),
            "point_in_time_rule": (
                "Only published records available by 23:59:59 on as_of are eligible; "
                "missing values never pass a condition or become zero."
            ),
        }

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
        if frame.empty:
            return []
        return frame.astype(object).where(pd.notna(frame), None).to_dict(orient="records")

    def _universe(self, spec: ScreenSpec) -> dict[str, Any]:
        return self.universe.build(
            as_of=spec.cutoff,
            mode=spec.universe_mode,
            symbols=spec.symbols,
            benchmark_symbol=spec.benchmark_symbol,
            classification_system=spec.classification_system,
            require_history=spec.require_verified_industry,
        )

    def _financial_rows(self, symbols: tuple[str, ...], cutoff: datetime) -> list[dict[str, Any]]:
        if not symbols:
            return []
        placeholders = ", ".join("?" for _ in symbols)
        with self.database.connect(read_only=True) as connection:
            frame = connection.execute(
                f"""
                WITH ranked_reports AS (
                    SELECT report.*,
                           ROW_NUMBER() OVER (
                             PARTITION BY report.security_id
                             ORDER BY report.published_at DESC,
                                      report.report_period_end DESC, report.report_id DESC
                           ) AS ranking
                    FROM published_financial_report AS report
                    WHERE report.published_at <= ?
                ), latest_reports AS (
                    SELECT * FROM ranked_reports WHERE ranking = 1
                )
                SELECT security.symbol, security.security_id, security.name,
                       report.report_id, report.report_period_end, report.report_type,
                       report.published_at, report.availability_source,
                       report.source AS provider_source, report.source_batch_id,
                       metric.metric_code, metric.metric_value, metric.unit,
                       COALESCE((
                           SELECT partition.raw_path
                           FROM financial_sync_partition AS partition
                           WHERE partition.batch_id = report.source_batch_id
                             AND partition.security_id = report.security_id
                             AND partition.status = 'published'
                           ORDER BY partition.updated_at DESC, partition.partition_key DESC
                           LIMIT 1
                       ), '') AS raw_path
                FROM security_master AS security
                LEFT JOIN latest_reports AS report USING (security_id)
                LEFT JOIN published_financial_metric AS metric
                  ON metric.report_id = report.report_id
                WHERE security.symbol IN ({placeholders})
                ORDER BY security.symbol, metric.metric_code
                """,
                [cutoff, *symbols],
            ).fetchdf()
        return self._records(frame)

    def _market_rows(self, symbols: tuple[str, ...], as_of: date) -> list[dict[str, Any]]:
        if not symbols:
            return []
        placeholders = ", ".join("?" for _ in symbols)
        with self.database.connect(read_only=True) as connection:
            frame = connection.execute(
                f"""
                WITH eligible AS (
                    SELECT security.symbol, bar.security_id, bar.trade_date, bar.close,
                           bar.volume, bar.amount, bar.published_at, bar.source_batch_id,
                           ROW_NUMBER() OVER (
                             PARTITION BY bar.security_id ORDER BY bar.trade_date DESC
                           ) AS recent_rank
                    FROM published_daily_bar AS bar
                    JOIN security_master AS security USING (security_id)
                    WHERE bar.adjustment = 'qfq' AND bar.trade_date <= ?
                      AND security.symbol IN ({placeholders})
                )
                SELECT symbol, security_id,
                       MAX(CASE WHEN recent_rank = 1 THEN trade_date END) AS trade_date,
                       MAX(CASE WHEN recent_rank = 1 THEN close END) AS close,
                       MAX(CASE WHEN recent_rank = 1 THEN volume END) AS volume,
                       MAX(CASE WHEN recent_rank = 1 THEN amount END) AS amount,
                       AVG(CASE WHEN recent_rank <= 20 THEN amount END) AS avg_amount_20d,
                       MAX(CASE WHEN recent_rank = 1 THEN published_at END) AS published_at,
                       MAX(CASE WHEN recent_rank = 1 THEN source_batch_id END) AS source_batch_id
                FROM eligible GROUP BY symbol, security_id ORDER BY symbol
                """,
                [as_of, *symbols],
            ).fetchdf()
        return self._records(frame)

    def _prepare(self, spec: ScreenSpec) -> dict[str, Any]:
        universe = self._universe(spec)
        symbols = tuple(universe["symbols"])
        financial_rows = self._financial_rows(symbols, spec.cutoff)
        market_rows = self._market_rows(symbols, spec.as_of)
        industries = self.universe.industries(
            symbols,
            spec.cutoff,
            classification_system=spec.classification_system,
            verified_only=spec.require_verified_industry,
        )
        industry_by_symbol = {
            str(row["symbol"]): row for row in self._records(industries)
        }
        financial_by_symbol: dict[str, dict[str, Any]] = {}
        for row in financial_rows:
            symbol = str(row["symbol"])
            item = financial_by_symbol.setdefault(
                symbol,
                {
                    "report_id": row.get("report_id"),
                    "report_period_end": row.get("report_period_end"),
                    "report_type": row.get("report_type"),
                    "published_at": row.get("published_at"),
                    "availability_source": row.get("availability_source"),
                    "provider_source": row.get("provider_source"),
                    "source_batch_id": row.get("source_batch_id"),
                    "raw_path": row.get("raw_path"),
                    "metrics": {},
                },
            )
            if row.get("metric_code") is not None:
                item["metrics"][str(row["metric_code"])] = {
                    "value": float(row["metric_value"]),
                    "unit": row.get("unit"),
                }
        market_by_symbol = {str(row["symbol"]): row for row in market_rows}
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            finance = financial_by_symbol.get(symbol, {})
            market = market_by_symbol.get(symbol, {})
            industry = industry_by_symbol.get(symbol, {})
            values = {
                code: details["value"]
                for code, details in finance.get("metrics", {}).items()
            }
            values.update({code: market.get(code) for code in MARKET_FIELDS})
            rows.append(
                {
                    "symbol": symbol,
                    "security_id": market.get("security_id")
                    or next(
                        (
                            row["security_id"]
                            for row in financial_rows
                            if row["symbol"] == symbol
                        ),
                        None,
                    ),
                    "name": next(
                        (row["name"] for row in financial_rows if row["symbol"] == symbol),
                        symbol,
                    ),
                    "industry": industry.get("industry_name"),
                    "industry_history_mode": industry.get("history_mode"),
                    "values": values,
                    "financial_evidence": {
                        key: finance.get(key)
                        for key in (
                            "report_id",
                            "report_period_end",
                            "report_type",
                            "published_at",
                            "availability_source",
                            "provider_source",
                            "source_batch_id",
                            "raw_path",
                        )
                    },
                    "market_evidence": {
                        key: market.get(key)
                        for key in ("trade_date", "published_at", "source_batch_id")
                    },
                }
            )
        return {"universe": universe, "rows": rows}

    @staticmethod
    def _condition_passes(actual: float, condition: ScreenCondition) -> bool:
        if condition.operator == "gt":
            return actual > condition.value
        if condition.operator == "gte":
            return actual >= condition.value
        if condition.operator == "lt":
            return actual < condition.value
        if condition.operator == "lte":
            return actual <= condition.value
        return condition.value <= actual <= float(condition.upper_value)

    def _evaluate(self, spec: ScreenSpec, prepared: dict[str, Any]) -> dict[str, Any]:
        evaluated: list[dict[str, Any]] = []
        missing_excluded = 0
        field_coverage = {item.field: 0 for item in spec.conditions}
        for row in prepared["rows"]:
            evaluations: list[dict[str, Any]] = []
            selected = True
            has_missing = False
            for condition in spec.conditions:
                actual = row["values"].get(condition.field)
                if actual is None or (isinstance(actual, float) and math.isnan(actual)):
                    passed = False
                    status = "missing"
                    has_missing = True
                else:
                    actual = float(actual)
                    field_coverage[condition.field] += 1
                    passed = self._condition_passes(actual, condition)
                    status = "passed" if passed else "failed"
                selected = selected and passed
                evaluations.append(
                    {
                        "field": condition.field,
                        "label": SCREEN_FIELDS[condition.field]["label"],
                        "operator": condition.operator,
                        "expected": condition.value,
                        "upper_value": condition.upper_value,
                        "actual": actual,
                        "status": status,
                    }
                )
            industry = row.get("industry")
            if spec.include_industries and industry not in spec.include_industries:
                selected = False
                evaluations.append(
                    {
                        "field": "industry",
                        "label": "行业包含",
                        "actual": industry,
                        "expected": list(spec.include_industries),
                        "status": "missing" if industry is None else "failed",
                    }
                )
                has_missing = has_missing or industry is None
            if spec.exclude_industries and industry in spec.exclude_industries:
                selected = False
                evaluations.append(
                    {
                        "field": "industry",
                        "label": "行业排除",
                        "actual": industry,
                        "expected": list(spec.exclude_industries),
                        "status": "failed",
                    }
                )
            if has_missing and spec.missing_policy == "exclude":
                missing_excluded += 1
                continue
            row["evaluations"] = evaluations
            row["selected"] = selected
            evaluated.append(row)

        direction = -1 if spec.sort_direction == "desc" else 1

        def sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
            value = item["values"].get(str(spec.sort_by))
            missing = value is None or (isinstance(value, float) and math.isnan(value))
            numeric = 0.0 if missing else float(value)
            return (not item["selected"], missing, direction * numeric, item["symbol"])

        evaluated.sort(key=sort_key)
        selected_rank = 0
        for item in evaluated:
            if item["selected"]:
                selected_rank += 1
                item["rank"] = selected_rank
                if spec.top_n is not None and selected_rank > spec.top_n:
                    item["selected"] = False
                    item["evaluations"].append(
                        {
                            "field": "top_n",
                            "label": "排名上限",
                            "actual": selected_rank,
                            "expected": spec.top_n,
                            "status": "failed",
                        }
                    )
            else:
                item["rank"] = None
        selected_count = sum(bool(item["selected"]) for item in evaluated)
        blockers = [
            f"{field} has no published values at the requested point in time"
            for field, covered in field_coverage.items()
            if covered == 0
        ]
        if not prepared["universe"]["security_count"]:
            blockers.append("the requested point-in-time universe is empty")
        if spec.require_verified_industry and (
            prepared["universe"]["industry_covered_count"]
            < prepared["universe"]["security_count"]
        ):
            blockers.append("verified industry history does not cover the full universe")
        warnings = []
        for field, covered in field_coverage.items():
            if 0 < covered < prepared["universe"]["security_count"]:
                warnings.append(
                    f"{field} covers {covered}/{prepared['universe']['security_count']} securities"
                )
        if prepared["universe"].get("survivorship_bias_warning"):
            warnings.append(prepared["universe"]["survivorship_bias_warning"])
        diagnostics = {
            "ready": not blockers,
            "as_of_cutoff": spec.cutoff.isoformat(),
            "point_in_time_rule": (
                "Financials use the latest report actually published by end-of-day as_of; "
                "market fields use qfq bars dated no later than as_of."
            ),
            "candidate_count": prepared["universe"]["security_count"],
            "result_count": len(evaluated),
            "selected_count": selected_count,
            "missing_excluded_count": missing_excluded,
            "field_coverage": field_coverage,
            "industry_covered_count": prepared["universe"]["industry_covered_count"],
            "industry_coverage": prepared["universe"]["industry_coverage"],
            "history_verified": prepared["universe"]["history_verified"],
            "universe_mode": spec.universe_mode,
            "blockers": blockers,
            "warnings": warnings,
        }
        return {"diagnostics": diagnostics, "items": evaluated}

    def preflight(self, spec: ScreenSpec) -> dict[str, Any]:
        self.database.initialize()
        result = self._evaluate(spec, self._prepare(spec))
        return {"spec": spec.to_dict(), **result["diagnostics"]}

    def run(self, spec: ScreenSpec) -> dict[str, Any]:
        self.database.initialize()
        run_id = f"screen-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        started = datetime.now(UTC)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO screen_run (
                    run_id, name, status, as_of, universe_mode, spec_json, started_at
                ) VALUES (?, ?, 'running', ?, ?, ?, ?)
                """,
                [
                    run_id,
                    spec.name,
                    spec.cutoff,
                    spec.universe_mode,
                    self._json(spec.to_dict()),
                    started,
                ],
            )
        try:
            evaluated = self._evaluate(spec, self._prepare(spec))
            diagnostics = evaluated["diagnostics"]
            if not diagnostics["ready"]:
                raise ValueError("; ".join(diagnostics["blockers"]))
            completed = datetime.now(UTC)
            with self.database.connect() as connection:
                for item in evaluated["items"]:
                    evidence = {
                        "financial": item["financial_evidence"],
                        "market": item["market_evidence"],
                        "industry_history_mode": item["industry_history_mode"],
                    }
                    connection.execute(
                        """
                        INSERT INTO screen_result (
                            run_id, security_id, symbol, name, industry_name, selected,
                            selection_rank, values_json, evaluations_json, evidence_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            run_id,
                            item["security_id"],
                            item["symbol"],
                            item["name"],
                            item["industry"],
                            item["selected"],
                            item["rank"],
                            self._json(item["values"]),
                            self._json(item["evaluations"]),
                            self._json(evidence),
                        ],
                    )
                connection.execute(
                    """
                    UPDATE screen_run SET status='succeeded', candidate_count=?,
                           selected_count=?, diagnostics_json=?, completed_at=?
                    WHERE run_id=?
                    """,
                    [
                        diagnostics["candidate_count"],
                        diagnostics["selected_count"],
                        self._json(diagnostics),
                        completed,
                        run_id,
                    ],
                )
            return self.get_run(run_id)
        except Exception as exc:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE screen_run SET status='failed', error=?, completed_at=? WHERE run_id=?",
                    [str(exc), datetime.now(UTC), run_id],
                )
            raise

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.database.connect(read_only=True) as connection:
            run = connection.execute(
                """
                SELECT run_id, name, status, as_of, universe_mode, spec_json,
                       candidate_count, selected_count, diagnostics_json, error,
                       created_at, started_at, completed_at
                FROM screen_run WHERE run_id=?
                """,
                [run_id],
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            rows = connection.execute(
                """
                SELECT symbol, name, industry_name, selected, selection_rank,
                       values_json, evaluations_json, evidence_json
                FROM screen_result WHERE run_id=?
                ORDER BY selected DESC, selection_rank NULLS LAST, symbol
                """,
                [run_id],
            ).fetchall()
        columns = (
            "run_id",
            "name",
            "status",
            "as_of",
            "universe_mode",
            "spec_json",
            "candidate_count",
            "selected_count",
            "diagnostics_json",
            "error",
            "created_at",
            "started_at",
            "completed_at",
        )
        payload = dict(zip(columns, run, strict=True))
        payload["spec"] = json.loads(payload.pop("spec_json"))
        payload["diagnostics"] = json.loads(payload.pop("diagnostics_json") or "{}")
        payload["items"] = [
            {
                "symbol": row[0],
                "name": row[1],
                "industry": row[2],
                "selected": row[3],
                "rank": row[4],
                "values": json.loads(row[5]),
                "evaluations": json.loads(row[6]),
                "evidence": json.loads(row[7]),
            }
            for row in rows
        ]
        return payload

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = min(100, max(1, limit))
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT run_id, name, status, as_of, universe_mode, candidate_count,
                       selected_count, error, created_at, completed_at
                FROM screen_run ORDER BY created_at DESC LIMIT ?
                """,
                [limit],
            ).fetchdf()
        return self._records(rows)

    def save_watchlist(self, run_id: str, name: str) -> dict[str, Any]:
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError("watchlist name must contain 1 to 120 characters")
        run = self.get_run(run_id)
        if run["status"] != "succeeded":
            raise ValueError("only a succeeded screen can become a watchlist")
        symbols = [item["symbol"] for item in run["items"] if item["selected"]]
        if not symbols:
            raise ValueError("the screen selected no symbols")
        watchlist_id = f"watchlist-{uuid4().hex}"
        diagnostics = run["diagnostics"]
        warning = None if diagnostics.get("history_verified") else next(
            (
                item
                for item in diagnostics.get("warnings", [])
                if "historical" in item.lower() or "survivorship" in item.lower()
            ),
            "The saved universe is not verified historical index membership.",
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO saved_watchlist (
                    watchlist_id, name, screen_run_id, as_of, symbols_json,
                    history_verified, warning
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    watchlist_id,
                    name,
                    run_id,
                    run["as_of"],
                    self._json(symbols),
                    bool(diagnostics.get("history_verified")),
                    warning,
                ],
            )
        return self.get_watchlist(watchlist_id)

    def get_watchlist(self, watchlist_id: str) -> dict[str, Any]:
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT watchlist_id, name, screen_run_id, as_of, symbols_json,
                       history_verified, warning, created_at
                FROM saved_watchlist WHERE watchlist_id=?
                """,
                [watchlist_id],
            ).fetchone()
        if row is None:
            raise KeyError(watchlist_id)
        return {
            "watchlist_id": row[0],
            "name": row[1],
            "screen_run_id": row[2],
            "as_of": row[3],
            "symbols": json.loads(row[4]),
            "history_verified": row[5],
            "warning": row[6],
            "created_at": row[7],
        }

    def list_watchlists(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = min(100, max(1, limit))
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT watchlist_id FROM saved_watchlist ORDER BY created_at DESC LIMIT ?",
                [limit],
            ).fetchall()
        return [self.get_watchlist(str(row[0])) for row in rows]
