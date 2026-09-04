from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import Lock
from typing import Any

import pandas as pd

from ai_quant_assistant.domain.models import normalize_benchmark_symbol
from ai_quant_assistant.providers.normalization import security_id_for_symbol
from ai_quant_assistant.storage.fixture_data import load_fixture_data
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.locks import InterProcessFileLock
from ai_quant_assistant.storage.schema import SCHEMA_VERSION, apply_migrations

CORE_RELATIONS = (
    "schema_migrations",
    "security_master",
    "trading_calendar",
    "daily_bar_raw",
    "daily_bar",
    "sync_batch",
    "dq_result",
    "sync_partition",
    "adjustment_ratio",
    "data_coverage",
    "published_daily_bar",
    "benchmark_master",
    "benchmark_daily_bar",
    "announcement",
    "research_sync_partition",
    "published_benchmark_daily_bar",
    "published_announcement",
    "financial_report",
    "financial_metric",
    "financial_sync_partition",
    "published_financial_report",
    "published_financial_metric",
    "operation_task",
    "database_backup",
    "operation_audit",
    "operation_alert",
    "security_industry_history",
    "index_constituent_history",
    "universe_sync_batch",
    "copilot_conversation",
    "copilot_message",
    "research_plan",
    "research_plan_event",
    "screen_run",
    "screen_result",
    "saved_watchlist",
    "announcement_document",
    "document_page",
    "document_chunk",
    "document_extraction",
    "document_question",
    "document_answer_citation",
    "document_event_extraction",
    "document_event",
    "document_event_evidence",
    "document_event_research_run",
    "stock_pool",
    "stock_pool_member",
    "stock_pool_revision",
    "managed_stock",
    "research_data_plan",
)

DATABASE_ACCESS_LOCK_TIMEOUT_SECONDS = 30.0

_CONNECTION_LOCKS: dict[Path, InterProcessFileLock] = {}
_CONNECTION_LOCKS_GUARD = Lock()


class LocalDatabase:
    def __init__(self, path: Path, layout: DataLayout) -> None:
        self.path = path
        self.layout = layout
        resolved_path = path.resolve()
        with _CONNECTION_LOCKS_GUARD:
            self.connection_lock = _CONNECTION_LOCKS.setdefault(
                resolved_path,
                InterProcessFileLock(
                    resolved_path.parent / f".{resolved_path.name}.access.lock",
                    description=f"DuckDB access lock for {resolved_path}",
                ),
            )

    @staticmethod
    def _duckdb() -> Any:
        import duckdb

        return duckdb

    def initialize(self) -> dict[str, Any]:
        directories = self.layout.ensure()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            applied = apply_migrations(connection)
        return {
            "status": "initialized",
            "database_path": str(self.path),
            "data_root": str(self.layout.root),
            "schema_version": SCHEMA_VERSION,
            "applied_migrations": applied,
            "directories": directories,
        }

    @contextmanager
    def connect(self, *, read_only: bool = False) -> Iterator[Any]:
        with self.exclusive_access():
            with self._duckdb().connect(str(self.path), read_only=read_only) as connection:
                yield connection

    @contextmanager
    def exclusive_access(
        self, *, timeout_seconds: float = DATABASE_ACCESS_LOCK_TIMEOUT_SECONDS
    ) -> Iterator[None]:
        """Serialize DuckDB opens across threads and application processes.

        DuckDB supports one read/write process at a time. Holding the same lock for
        reads also prevents a second Uvicorn worker from opening the catalog while a
        writer or a consistent file-copy backup is active.
        """

        with self.connection_lock.hold(timeout_seconds=timeout_seconds):
            yield

    def load_fixture(self) -> dict[str, int]:
        self.initialize()
        with self.connect() as connection:
            return load_fixture_data(connection)

    def status(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "initialized": False,
                "database_path": str(self.path),
                "expected_schema_version": SCHEMA_VERSION,
                "directories": self.layout.status(),
                "relations": {},
            }
        with self.connect(read_only=True) as connection:
            available = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'main'
                    UNION ALL
                    SELECT table_name FROM information_schema.views
                    WHERE table_schema = 'main'
                    """
                ).fetchall()
            }
            version = 0
            if "schema_migrations" in available:
                version = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                    ).fetchone()[0]
                )
            relations = {
                relation: int(
                    connection.execute(f'SELECT COUNT(*) FROM "{relation}"').fetchone()[0]
                )
                for relation in CORE_RELATIONS
                if relation in available
            }
        return {
            "initialized": version > 0,
            "database_path": str(self.path),
            "schema_version": version,
            "expected_schema_version": SCHEMA_VERSION,
            "directories": self.layout.status(),
            "relations": relations,
        }

    def research_overview(self) -> dict[str, Any]:
        status = self.status()
        overview: dict[str, Any] = {"database": status, "securities": [], "coverage": {}}
        if not status.get("initialized") or status.get("schema_version", 0) < 4:
            return overview
        with self.connect(read_only=True) as connection:
            securities = connection.execute(
                """
                SELECT security.symbol, security.name, security.exchange,
                       security.list_date, MIN(bar.trade_date) AS start_date,
                       MAX(bar.trade_date) AS end_date,
                       COUNT(DISTINCT bar.trade_date) AS price_rows,
                       COUNT(DISTINCT report.report_id) AS report_count
                FROM security_master AS security
                LEFT JOIN published_daily_bar AS bar
                  ON bar.security_id = security.security_id AND bar.adjustment = 'qfq'
                LEFT JOIN published_financial_report AS report
                  ON report.security_id = security.security_id
                GROUP BY security.symbol, security.name, security.exchange, security.list_date
                HAVING COUNT(bar.trade_date) > 0 OR COUNT(report.report_id) > 0
                ORDER BY security.symbol
                """
            ).fetchdf()
            coverage_rows = connection.execute(
                """
                SELECT 'stock', MIN(trade_date), MAX(trade_date), COUNT(*)
                FROM published_daily_bar
                UNION ALL
                SELECT 'benchmark', MIN(trade_date), MAX(trade_date), COUNT(*)
                FROM published_benchmark_daily_bar
                UNION ALL
                SELECT 'announcement', MIN(announcement_date), MAX(announcement_date), COUNT(*)
                FROM published_announcement
                UNION ALL
                SELECT 'financial', MIN(report_period_end), MAX(report_period_end), COUNT(*)
                FROM published_financial_report
                """
            ).fetchall()
        overview["securities"] = (
            securities.astype(object).where(pd.notna(securities), None).to_dict(orient="records")
        )
        overview["coverage"] = {
            str(dataset): {"start_date": start, "end_date": end, "rows": int(rows)}
            for dataset, start, end, rows in coverage_rows
        }
        return overview

    def read_published_prices(
        self, symbol: str, start_date: Any, end_date: Any, adjustment: str
    ) -> pd.DataFrame:
        columns = [
            "date",
            "close",
            "symbol",
            "source",
            "provider_source",
            "source_batch_id",
            "raw_path",
        ]
        if not self.path.is_file():
            return pd.DataFrame(columns=columns)
        with self.connect(read_only=True) as connection:
            frame = connection.execute(
                """
                SELECT bar.trade_date AS date, bar.close, security.symbol,
                       'local:published_daily_bar' AS source,
                       bar.source_batch_id
                FROM published_daily_bar AS bar
                JOIN security_master AS security USING (security_id)
                WHERE security.symbol = ?
                  AND bar.adjustment = ?
                  AND bar.trade_date BETWEEN ? AND ?
                ORDER BY bar.trade_date
                """,
                [symbol, adjustment, start_date, end_date],
            ).fetchdf()
            partitions = connection.execute(
                """
                SELECT partition.batch_id AS source_batch_id,
                       partition.start_date, partition.end_date, partition.raw_path
                FROM sync_partition AS partition
                JOIN security_master AS security USING (security_id)
                WHERE security.symbol = ?
                  AND partition.adjustment = ?
                  AND partition.status = 'published'
                  AND partition.end_date >= ?
                  AND partition.start_date <= ?
                ORDER BY partition.start_date, partition.end_date
                """,
                [symbol, adjustment, start_date, end_date],
            ).fetchdf()
        if frame.empty:
            return pd.DataFrame(columns=columns)
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
        frame["provider_source"] = "unknown"
        frame["raw_path"] = ""
        for partition in partitions.itertuples(index=False):
            partition_start = pd.Timestamp(partition.start_date).normalize()
            partition_end = pd.Timestamp(partition.end_date).normalize()
            mask = (
                (frame["source_batch_id"] == partition.source_batch_id)
                & (frame["date"] >= partition_start)
                & (frame["date"] <= partition_end)
            )
            if not mask.any() or not partition.raw_path:
                continue
            raw_path = Path(str(partition.raw_path))
            if not raw_path.is_absolute():
                raw_path = self.layout.root.resolve().parent / raw_path
            frame.loc[mask, "raw_path"] = str(raw_path)
            if not raw_path.is_file():
                continue
            try:
                raw = pd.read_parquet(raw_path, columns=["trade_date", "source"])
                raw["date"] = pd.to_datetime(raw["trade_date"]).dt.normalize()
                source_by_date = raw.drop_duplicates("date").set_index("date")["source"]
                frame.loc[mask, "provider_source"] = (
                    frame.loc[mask, "date"].map(source_by_date).fillna("unknown")
                )
            except Exception:  # noqa: BLE001 - lineage failure must not block local reads
                continue
        return frame[columns]

    def has_coverage(self, symbol: str, start_date: Any, end_date: Any, adjustment: str) -> bool:
        if not self.path.is_file():
            return False
        with self.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                WITH verified_intervals AS (
                    SELECT coverage.security_id, coverage.adjustment,
                           coverage.start_date, coverage.end_date
                    FROM data_coverage AS coverage
                    WHERE coverage.dataset = 'daily_bar'

                    UNION

                    SELECT partition.security_id, partition.adjustment,
                           partition.start_date, partition.end_date
                    FROM sync_partition AS partition
                    WHERE partition.status = 'published'
                      AND partition.row_count = 0
                )
                SELECT interval.start_date, interval.end_date
                FROM verified_intervals AS interval
                JOIN security_master AS security USING (security_id)
                WHERE security.symbol = ?
                  AND interval.adjustment = ?
                  AND interval.end_date >= ?
                  AND interval.start_date <= ?
                ORDER BY interval.start_date, interval.end_date
                """,
                [symbol, adjustment, start_date, end_date],
            ).fetchall()
        cursor = pd.Timestamp(start_date).date()
        requested_end = pd.Timestamp(end_date).date()
        for interval_start, interval_end in rows:
            if interval_start > cursor:
                return False
            if interval_end >= cursor:
                cursor = interval_end + timedelta(days=1)
            if cursor > requested_end:
                return True
        return cursor > requested_end

    def _has_dataset_coverage(
        self,
        dataset: str,
        object_id: str,
        variant: str,
        start_date: Any,
        end_date: Any,
    ) -> bool:
        if not self.path.is_file():
            return False
        with self.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT start_date, end_date
                FROM data_coverage
                WHERE dataset = ? AND security_id = ? AND adjustment = ?
                  AND end_date >= ? AND start_date <= ?
                ORDER BY start_date, end_date
                """,
                [dataset, object_id, variant, start_date, end_date],
            ).fetchall()
        cursor = pd.Timestamp(start_date).date()
        requested_end = pd.Timestamp(end_date).date()
        for interval_start, interval_end in rows:
            if interval_start > cursor:
                return False
            if interval_end >= cursor:
                cursor = interval_end + timedelta(days=1)
            if cursor > requested_end:
                return True
        return cursor > requested_end

    def has_benchmark_coverage(self, symbol: str, start_date: Any, end_date: Any) -> bool:
        normalized = normalize_benchmark_symbol(symbol)
        return self._has_dataset_coverage(
            "benchmark_daily_bar", f"INDEX.{normalized}", "none", start_date, end_date
        )

    def has_announcement_coverage(
        self,
        symbol: str,
        start_date: Any,
        end_date: Any,
        report_type: str = "interim",
    ) -> bool:
        return self._has_dataset_coverage(
            "announcement", security_id_for_symbol(symbol), report_type, start_date, end_date
        )

    def has_financial_coverage(self, symbol: str, start_date: Any, end_date: Any) -> bool:
        return self._has_dataset_coverage(
            "financial_metric", security_id_for_symbol(symbol), "all", start_date, end_date
        )

    def read_benchmark_prices(self, symbol: str, start_date: Any, end_date: Any) -> pd.DataFrame:
        columns = [
            "date",
            "close",
            "symbol",
            "source",
            "provider_source",
            "source_batch_id",
            "raw_path",
        ]
        if not self.path.is_file():
            return pd.DataFrame(columns=columns)
        normalized = normalize_benchmark_symbol(symbol)
        with self.connect(read_only=True) as connection:
            frame = connection.execute(
                """
                SELECT bar.trade_date AS date, bar.close, benchmark.symbol,
                       'local:published_benchmark_daily_bar' AS source,
                       bar.source AS provider_source, bar.source_batch_id,
                       COALESCE(partition.raw_path, '') AS raw_path
                FROM published_benchmark_daily_bar AS bar
                JOIN benchmark_master AS benchmark USING (benchmark_id)
                LEFT JOIN research_sync_partition AS partition
                  ON partition.batch_id = bar.source_batch_id
                 AND partition.object_id = bar.benchmark_id
                 AND partition.dataset = 'benchmark_daily_bar'
                 AND bar.trade_date BETWEEN partition.start_date AND partition.end_date
                WHERE benchmark.symbol = ?
                  AND bar.trade_date BETWEEN ? AND ?
                ORDER BY bar.trade_date
                """,
                [normalized, start_date, end_date],
            ).fetchdf()
        if frame.empty:
            return pd.DataFrame(columns=columns)
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
        return frame[columns]

    def read_published_announcements(
        self,
        symbol: str,
        start_date: Any,
        end_date: Any,
        report_type: str = "interim",
    ) -> pd.DataFrame:
        columns = [
            "symbol",
            "company_name",
            "title",
            "announcement_date",
            "url",
            "report_year",
            "report_type",
            "report_period_end",
            "published_at",
            "source",
            "provider_source",
            "source_batch_id",
            "raw_path",
        ]
        if not self.path.is_file():
            return pd.DataFrame(columns=columns)
        as_of_exclusive = pd.Timestamp(end_date).normalize().to_pydatetime() + timedelta(days=1)
        with self.connect(read_only=True) as connection:
            frame = connection.execute(
                """
                SELECT security.symbol, item.company_name, item.title,
                       item.announcement_date, item.url, item.report_year,
                       item.report_type, item.report_period_end, item.published_at,
                       'local:published_announcement' AS source,
                       item.source AS provider_source, item.source_batch_id,
                       COALESCE(partition.raw_path, '') AS raw_path
                FROM published_announcement AS item
                JOIN security_master AS security USING (security_id)
                LEFT JOIN research_sync_partition AS partition
                  ON partition.batch_id = item.source_batch_id
                 AND partition.object_id = item.security_id
                 AND partition.dataset = 'announcement'
                 AND partition.variant = item.report_type
                 AND item.announcement_date BETWEEN partition.start_date AND partition.end_date
                WHERE security.symbol = ? AND item.report_type = ?
                  AND item.announcement_date BETWEEN ? AND ?
                  AND item.published_at < ?
                ORDER BY item.announcement_date, item.report_year
                """,
                [symbol, report_type, start_date, end_date, as_of_exclusive],
            ).fetchdf()
        if frame.empty:
            return pd.DataFrame(columns=columns)
        frame["announcement_date"] = pd.to_datetime(frame["announcement_date"]).dt.normalize()
        return frame[columns]

    def read_financial_metrics(
        self,
        symbol: str,
        as_of: Any,
        *,
        start_date: Any | None = None,
        end_date: Any | None = None,
    ) -> pd.DataFrame:
        columns = [
            "symbol",
            "report_period_end",
            "report_type",
            "published_at",
            "availability_source",
            "metric_code",
            "metric_value",
            "unit",
            "source",
            "provider_source",
            "source_batch_id",
            "raw_path",
        ]
        if not self.path.is_file():
            return pd.DataFrame(columns=columns)
        cutoff = pd.Timestamp(as_of).to_pydatetime()
        if cutoff == pd.Timestamp(as_of).normalize().to_pydatetime():
            cutoff += timedelta(days=1)
        requested_start = start_date or pd.Timestamp("1900-01-01").date()
        requested_end = end_date or pd.Timestamp(as_of).date()
        with self.connect(read_only=True) as connection:
            frame = connection.execute(
                """
                WITH ranked_metrics AS (
                    SELECT security.symbol, report.report_period_end, report.report_type,
                           report.published_at, report.availability_source,
                           metric.metric_code, metric.metric_value, metric.unit,
                           'local:published_financial_metric' AS source,
                           report.source AS provider_source, report.source_batch_id,
                           COALESCE(partition.raw_path, '') AS raw_path,
                           ROW_NUMBER() OVER (
                               PARTITION BY report.security_id, report.report_period_end,
                                            metric.metric_code
                               ORDER BY report.published_at DESC, report.observed_at DESC
                           ) AS revision_rank
                    FROM published_financial_report AS report
                    JOIN published_financial_metric AS metric USING (report_id)
                    JOIN security_master AS security
                      ON security.security_id = report.security_id
                    LEFT JOIN financial_sync_partition AS partition
                      ON partition.batch_id = report.source_batch_id
                     AND partition.security_id = report.security_id
                     AND report.report_period_end
                         BETWEEN partition.start_date AND partition.end_date
                    WHERE security.symbol = ?
                      AND report.published_at < ?
                      AND report.report_period_end BETWEEN ? AND ?
                )
                SELECT symbol, report_period_end, report_type, published_at,
                       availability_source, metric_code, metric_value, unit,
                       source, provider_source, source_batch_id, raw_path
                FROM ranked_metrics
                WHERE revision_rank = 1
                ORDER BY report_period_end, metric_code
                """,
                [symbol, cutoff, requested_start, requested_end],
            ).fetchdf()
        if frame.empty:
            return pd.DataFrame(columns=columns)
        frame["report_period_end"] = pd.to_datetime(frame["report_period_end"]).dt.normalize()
        frame["published_at"] = pd.to_datetime(frame["published_at"])
        return frame[columns]

    def read_latest_financial_snapshot(self, symbol: str, as_of: Any) -> dict[str, Any]:
        metrics = self.read_financial_metrics(symbol, as_of)
        if metrics.empty:
            return {}
        latest_period = metrics["report_period_end"].max()
        latest = metrics.loc[metrics["report_period_end"] == latest_period]
        first = latest.iloc[0]
        return {
            "financial_report_period_end": pd.Timestamp(latest_period).date().isoformat(),
            "financial_published_at": pd.Timestamp(first["published_at"]).isoformat(),
            "financial_report_type": str(first["report_type"]),
            "financial_source": str(first["source"]),
            "financial_provider_source": str(first["provider_source"]),
            "financial_source_batch_id": str(first["source_batch_id"]),
            **{
                str(row.metric_code): float(row.metric_value)
                for row in latest.itertuples(index=False)
            },
        }

    def read_factor_snapshot(
        self, symbols: tuple[str, ...], factor: str, as_of: Any
    ) -> pd.DataFrame:
        columns = [
            "symbol",
            "security_id",
            "name",
            "list_date",
            "factor_value",
            "report_period_end",
            "report_type",
            "published_at",
            "availability_source",
            "provider_source",
            "source_batch_id",
        ]
        if not self.path.is_file() or not symbols:
            return pd.DataFrame(columns=columns)
        cutoff = pd.Timestamp(as_of).to_pydatetime()
        placeholders = ", ".join("?" for _ in symbols)
        with self.connect(read_only=True) as connection:
            frame = connection.execute(
                f"""
                WITH ranked_reports AS (
                    SELECT security.symbol, security.security_id, security.name,
                           security.list_date, metric.metric_value AS factor_value,
                           report.report_period_end, report.report_type, report.published_at,
                           report.availability_source, report.source AS provider_source,
                           report.source_batch_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY security.security_id
                               ORDER BY report.report_period_end DESC,
                                        report.published_at DESC, report.observed_at DESC
                           ) AS report_rank
                    FROM published_financial_report AS report
                    JOIN published_financial_metric AS metric
                      ON metric.report_id = report.report_id
                    JOIN security_master AS security
                      ON security.security_id = report.security_id
                    WHERE security.symbol IN ({placeholders})
                      AND metric.metric_code = ?
                      AND report.published_at < ?
                      AND (security.list_date IS NULL OR security.list_date <= CAST(? AS DATE))
                      AND (security.delist_date IS NULL OR security.delist_date >= CAST(? AS DATE))
                )
                SELECT symbol, security_id, name, list_date, factor_value,
                       report_period_end, report_type, published_at,
                       availability_source, provider_source, source_batch_id
                FROM ranked_reports
                WHERE report_rank = 1
                ORDER BY symbol
                """,
                [*symbols, factor, cutoff, cutoff, cutoff],
            ).fetchdf()
        if frame.empty:
            return pd.DataFrame(columns=columns)
        frame["report_period_end"] = pd.to_datetime(frame["report_period_end"]).dt.normalize()
        frame["published_at"] = pd.to_datetime(frame["published_at"])
        return frame[columns]

    def read_backtest_prices(
        self, symbols: tuple[str, ...], start_date: Any, end_date: Any, adjustment: str
    ) -> pd.DataFrame:
        columns = [
            "symbol",
            "security_id",
            "exchange",
            "list_date",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "unadjusted_open",
            "unadjusted_high",
            "unadjusted_low",
            "unadjusted_close",
            "previous_unadjusted_close",
            "source_batch_id",
        ]
        if not self.path.is_file() or not symbols:
            return pd.DataFrame(columns=columns)
        placeholders = ", ".join("?" for _ in symbols)
        with self.connect(read_only=True) as connection:
            frame = connection.execute(
                f"""
                WITH selected_bars AS (
                    SELECT security.symbol, security.security_id, security.exchange,
                           security.list_date, bar.trade_date AS date,
                           bar.open, bar.high, bar.low, bar.close, bar.volume,
                           COALESCE(raw.open, bar.open) AS unadjusted_open,
                           COALESCE(raw.high, bar.high) AS unadjusted_high,
                           COALESCE(raw.low, bar.low) AS unadjusted_low,
                           COALESCE(raw.close, bar.close) AS unadjusted_close,
                           LAG(COALESCE(raw.close, bar.close)) OVER (
                               PARTITION BY security.security_id ORDER BY bar.trade_date
                           ) AS previous_unadjusted_close,
                           bar.source_batch_id
                    FROM published_daily_bar AS bar
                    JOIN security_master AS security USING (security_id)
                    LEFT JOIN published_daily_bar AS raw
                      ON raw.security_id = bar.security_id
                     AND raw.trade_date = bar.trade_date
                     AND raw.adjustment = 'none'
                    WHERE security.symbol IN ({placeholders})
                      AND bar.adjustment = ?
                      AND bar.trade_date <= ?
                )
                SELECT * FROM selected_bars
                WHERE date >= ?
                ORDER BY date, symbol
                """,
                [*symbols, adjustment, end_date, start_date],
            ).fetchdf()
        if frame.empty:
            return pd.DataFrame(columns=columns)
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
        return frame[columns]
