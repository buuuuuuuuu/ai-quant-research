from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pandas as pd

from ai_quant_assistant.domain.models import normalize_benchmark_symbol
from ai_quant_assistant.storage.database import LocalDatabase

HISTORY_MODES = {"verified_history", "observed_snapshot"}


class UniverseRepository:
    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    @staticmethod
    def _stamp(value: Any) -> datetime:
        result = pd.Timestamp(value).to_pydatetime()
        return result.replace(tzinfo=None) if result.tzinfo else result

    @staticmethod
    def _record_id(*parts: Any) -> str:
        return hashlib.sha256("|".join(str(item) for item in parts).encode()).hexdigest()[:32]

    def store_industries(
        self,
        frame: pd.DataFrame,
        *,
        classification_system: str,
        history_mode: str,
        as_of: date,
        source: str,
    ) -> dict[str, Any]:
        return self._store(
            frame,
            dataset="industry_classification",
            target=classification_system,
            history_mode=history_mode,
            as_of=as_of,
            source=source,
        )

    def store_constituents(
        self,
        frame: pd.DataFrame,
        *,
        benchmark_symbol: str,
        history_mode: str,
        as_of: date,
        source: str,
    ) -> dict[str, Any]:
        return self._store(
            frame,
            dataset="index_constituents",
            target=normalize_benchmark_symbol(benchmark_symbol),
            history_mode=history_mode,
            as_of=as_of,
            source=source,
        )

    def _store(
        self,
        frame: pd.DataFrame,
        *,
        dataset: str,
        target: str,
        history_mode: str,
        as_of: date,
        source: str,
    ) -> dict[str, Any]:
        if history_mode not in HISTORY_MODES:
            raise ValueError("history_mode must be verified_history or observed_snapshot")
        self.database.initialize()
        batch_id = f"{dataset}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        rows = 0
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO universe_sync_batch (batch_id, dataset, target, history_mode, as_of, status, source) VALUES (?, ?, ?, ?, ?, 'running', ?)",
                [batch_id, dataset, target, history_mode, as_of, source],
            )
        try:
            required = {"symbol"} | (
                {"industry_name"} if dataset == "industry_classification" else set()
            )
            if not required.issubset(frame.columns):
                raise ValueError(
                    f"missing required universe columns: {sorted(required - set(frame.columns))}"
                )
            if history_mode == "verified_history" and not {
                "effective_from",
                "published_at",
            }.issubset(frame.columns):
                raise ValueError(
                    "verified historical records require effective_from and published_at"
                )
            with self.database.connect() as connection:
                securities = dict(
                    connection.execute("SELECT symbol, security_id FROM security_master").fetchall()
                )
                for item in frame.to_dict(orient="records"):
                    symbol = str(item["symbol"]).strip().zfill(6)
                    security_id = securities.get(symbol)
                    if security_id is None:
                        continue
                    effective_from = pd.Timestamp(item.get("effective_from", as_of)).date()
                    published = self._stamp(
                        item.get("published_at", datetime.combine(as_of, datetime.min.time()))
                    )
                    observed = self._stamp(item.get("knowledge_time", published))
                    if history_mode == "observed_snapshot":
                        if (
                            effective_from < as_of
                            or published.date() < as_of
                            or observed.date() < as_of
                        ):
                            raise ValueError(
                                "observed snapshots cannot be backdated or represented as verified history"
                            )
                    if observed < published:
                        raise ValueError("knowledge_time cannot precede published_at")
                    effective_until = item.get("effective_until")
                    if effective_until is not None and not pd.isna(effective_until):
                        effective_until = pd.Timestamp(effective_until).date()
                    else:
                        effective_until = None
                    if dataset == "industry_classification":
                        record_id = self._record_id(
                            dataset,
                            security_id,
                            target,
                            effective_from,
                            published,
                            item["industry_name"],
                        )
                        connection.execute(
                            "INSERT OR REPLACE INTO security_industry_history (record_id, security_id, classification_system, industry_code, industry_name, effective_from, effective_until, published_at, knowledge_time, history_mode, source, source_batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [
                                record_id,
                                security_id,
                                target,
                                None
                                if pd.isna(item.get("industry_code"))
                                else str(item.get("industry_code")),
                                str(item["industry_name"]),
                                effective_from,
                                effective_until,
                                published,
                                observed,
                                history_mode,
                                source,
                                batch_id,
                            ],
                        )
                    else:
                        record_id = self._record_id(
                            dataset, target, security_id, effective_from, published
                        )
                        weight = item.get("weight")
                        weight = None if weight is None or pd.isna(weight) else float(weight)
                        connection.execute(
                            "INSERT OR REPLACE INTO index_constituent_history (record_id, benchmark_symbol, security_id, effective_from, effective_until, published_at, knowledge_time, history_mode, weight, source, source_batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [
                                record_id,
                                target,
                                security_id,
                                effective_from,
                                effective_until,
                                published,
                                observed,
                                history_mode,
                                weight,
                                source,
                                batch_id,
                            ],
                        )
                    rows += 1
                connection.execute(
                    "UPDATE universe_sync_batch SET status='published', row_count=?, completed_at=CURRENT_TIMESTAMP WHERE batch_id=?",
                    [rows, batch_id],
                )
        except Exception as exc:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE universe_sync_batch SET status='failed', error=?, completed_at=CURRENT_TIMESTAMP WHERE batch_id=?",
                    [str(exc), batch_id],
                )
            raise
        return {
            "batch_id": batch_id,
            "dataset": dataset,
            "target": target,
            "history_mode": history_mode,
            "as_of": as_of.isoformat(),
            "status": "published",
            "row_count": rows,
            "source": source,
        }

    def industries(
        self,
        symbols: tuple[str, ...],
        as_of: Any,
        *,
        classification_system: str = "provider",
        verified_only: bool = False,
    ) -> pd.DataFrame:
        columns = [
            "symbol",
            "security_id",
            "industry_code",
            "industry_name",
            "classification_system",
            "history_mode",
            "effective_from",
            "effective_until",
            "published_at",
            "knowledge_time",
            "source",
            "source_batch_id",
        ]
        if not symbols or not self.database.path.is_file():
            return pd.DataFrame(columns=columns)
        cutoff = self._stamp(as_of)
        placeholders = ", ".join("?" for _ in symbols)
        mode_filter = "AND history.history_mode = 'verified_history'" if verified_only else ""
        with self.database.connect(read_only=True) as connection:
            result = connection.execute(
                f"""
                WITH ranked AS (
                    SELECT security.symbol, history.security_id, history.industry_code,
                           history.industry_name, history.classification_system, history.history_mode,
                           history.effective_from, history.effective_until, history.published_at,
                           history.knowledge_time, history.source, history.source_batch_id,
                           ROW_NUMBER() OVER (PARTITION BY history.security_id ORDER BY history.effective_from DESC, history.knowledge_time DESC, history.observed_at DESC) AS ranking
                    FROM security_industry_history AS history
                    JOIN security_master AS security USING (security_id)
                    WHERE security.symbol IN ({placeholders})
                      AND history.classification_system = ?
                      AND history.effective_from <= CAST(? AS DATE)
                      AND (history.effective_until IS NULL OR history.effective_until >= CAST(? AS DATE))
                      AND history.published_at < ? AND history.knowledge_time < ? {mode_filter}
                ) SELECT {", ".join(columns)} FROM ranked WHERE ranking = 1 ORDER BY symbol
            """,
                [*symbols, classification_system, cutoff, cutoff, cutoff, cutoff],
            ).fetchdf()
        return result if not result.empty else pd.DataFrame(columns=columns)

    def constituents(
        self, benchmark_symbol: str, as_of: Any, *, verified_only: bool = True
    ) -> pd.DataFrame:
        columns = [
            "symbol",
            "security_id",
            "name",
            "benchmark_symbol",
            "effective_from",
            "effective_until",
            "published_at",
            "knowledge_time",
            "history_mode",
            "weight",
            "source",
            "source_batch_id",
        ]
        cutoff = self._stamp(as_of)
        mode_filter = "AND history.history_mode = 'verified_history'" if verified_only else ""
        with self.database.connect(read_only=True) as connection:
            result = connection.execute(
                f"""
                WITH ranked AS (
                    SELECT security.symbol, history.security_id, security.name, history.benchmark_symbol,
                           history.effective_from, history.effective_until, history.published_at,
                           history.knowledge_time, history.history_mode, history.weight,
                           history.source, history.source_batch_id,
                           ROW_NUMBER() OVER (PARTITION BY history.security_id ORDER BY history.effective_from DESC, history.knowledge_time DESC) AS ranking
                    FROM index_constituent_history AS history
                    JOIN security_master AS security USING (security_id)
                    WHERE history.benchmark_symbol = ?
                      AND history.effective_from <= CAST(? AS DATE)
                      AND (history.effective_until IS NULL OR history.effective_until >= CAST(? AS DATE))
                      AND history.published_at < ? AND history.knowledge_time < ?
                      AND (security.list_date IS NULL OR security.list_date <= CAST(? AS DATE))
                      AND (security.delist_date IS NULL OR security.delist_date >= CAST(? AS DATE)) {mode_filter}
                ) SELECT {", ".join(columns)} FROM ranked WHERE ranking = 1 ORDER BY symbol
            """,
                [
                    normalize_benchmark_symbol(benchmark_symbol),
                    cutoff,
                    cutoff,
                    cutoff,
                    cutoff,
                    cutoff,
                    cutoff,
                ],
            ).fetchdf()
        return result if not result.empty else pd.DataFrame(columns=columns)

    def build(
        self,
        *,
        as_of: Any,
        mode: str = "explicit",
        symbols: tuple[str, ...] = (),
        benchmark_symbol: str = "csi000300",
        limit: int | None = None,
        classification_system: str = "provider",
        require_history: bool = False,
    ) -> dict[str, Any]:
        if mode not in {"explicit", "all_available", "historical_index", "observed_index"}:
            raise ValueError("unsupported universe mode")
        cutoff = self._stamp(as_of)
        if mode in {"historical_index", "observed_index"}:
            members = self.constituents(
                benchmark_symbol, cutoff, verified_only=mode == "historical_index"
            )
            if members.empty:
                raise ValueError(
                    "no verified point-in-time historical index constituents are available"
                    if mode == "historical_index"
                    else "no point-in-time observed index constituents are available"
                )
            candidates = members["symbol"].tolist()
        else:
            with self.database.connect(read_only=True) as connection:
                clauses = [
                    "(security.list_date IS NULL OR security.list_date <= CAST(? AS DATE))",
                    "(security.delist_date IS NULL OR security.delist_date >= CAST(? AS DATE))",
                ]
                parameters: list[Any] = [cutoff, cutoff]
                if mode == "explicit" and symbols:
                    clauses.append(f"security.symbol IN ({', '.join('?' for _ in symbols)})")
                    parameters.extend(symbols)
                candidates = [
                    row[0]
                    for row in connection.execute(
                        f"SELECT DISTINCT security.symbol FROM security_master AS security JOIN published_daily_bar AS bar ON bar.security_id=security.security_id AND bar.trade_date <= CAST(? AS DATE) WHERE {' AND '.join(clauses)} ORDER BY security.symbol",
                        [cutoff, *parameters],
                    ).fetchall()
                ]
        if symbols and mode in {"historical_index", "observed_index"}:
            candidates = [item for item in candidates if item in set(symbols)]
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be positive")
            candidates = candidates[:limit]
        industry = self.industries(
            tuple(candidates),
            cutoff,
            classification_system=classification_system,
            verified_only=require_history,
        )
        by_industry = (
            industry.groupby("industry_name")["symbol"].count().to_dict()
            if not industry.empty
            else {}
        )
        return {
            "as_of": cutoff.isoformat(),
            "mode": mode,
            "benchmark_symbol": normalize_benchmark_symbol(benchmark_symbol)
            if mode.endswith("index")
            else None,
            "symbols": candidates,
            "security_count": len(candidates),
            "industry_covered_count": len(industry),
            "industry_coverage": len(industry) / len(candidates) if candidates else 0.0,
            "industries": {str(key): int(value) for key, value in by_industry.items()},
            "classification_system": classification_system,
            "history_verified": mode == "historical_index",
            "survivorship_bias_warning": None
            if mode == "historical_index"
            else "Explicit, available or observed constituents are not verified historical index membership.",
        }

    def overview(self, as_of: Any) -> dict[str, Any]:
        with self.database.connect(read_only=True) as connection:
            systems = connection.execute(
                "SELECT classification_system, history_mode, COUNT(*) AS rows, COUNT(DISTINCT security_id) AS securities FROM security_industry_history GROUP BY classification_system, history_mode ORDER BY classification_system, history_mode"
            ).fetchdf()
            indexes = connection.execute(
                "SELECT benchmark_symbol, history_mode, COUNT(*) AS rows, COUNT(DISTINCT security_id) AS securities FROM index_constituent_history GROUP BY benchmark_symbol, history_mode ORDER BY benchmark_symbol, history_mode"
            ).fetchdf()
            batches = connection.execute(
                "SELECT batch_id, dataset, target, history_mode, as_of, status, row_count, error, source, created_at FROM universe_sync_batch ORDER BY created_at DESC LIMIT 20"
            ).fetchdf()
        return {
            "as_of": self._stamp(as_of).isoformat(),
            "classification_systems": systems.astype(object)
            .where(pd.notna(systems), None)
            .to_dict(orient="records"),
            "indexes": indexes.astype(object)
            .where(pd.notna(indexes), None)
            .to_dict(orient="records"),
            "recent_batches": batches.astype(object)
            .where(pd.notna(batches), None)
            .to_dict(orient="records"),
        }
