from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from ai_quant_assistant.domain.models import normalize_benchmark_symbol
from ai_quant_assistant.providers.normalization import security_id_for_symbol
from ai_quant_assistant.storage.fixture_data import load_fixture_data
from ai_quant_assistant.storage.layout import DataLayout
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
)


class LocalDatabase:
    def __init__(self, path: Path, layout: DataLayout) -> None:
        self.path = path
        self.layout = layout

    @staticmethod
    def _duckdb() -> Any:
        import duckdb

        return duckdb

    def initialize(self) -> dict[str, Any]:
        directories = self.layout.ensure()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._duckdb().connect(str(self.path)) as connection:
            applied = apply_migrations(connection)
        return {
            "status": "initialized",
            "database_path": str(self.path),
            "data_root": str(self.layout.root),
            "schema_version": SCHEMA_VERSION,
            "applied_migrations": applied,
            "directories": directories,
        }

    def connect(self, *, read_only: bool = False) -> Any:
        return self._duckdb().connect(str(self.path), read_only=read_only)

    def load_fixture(self) -> dict[str, int]:
        self.initialize()
        with self._duckdb().connect(str(self.path)) as connection:
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
        with self._duckdb().connect(str(self.path), read_only=True) as connection:
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
                relation: int(connection.execute(f'SELECT COUNT(*) FROM "{relation}"').fetchone()[0])
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

    def has_coverage(
        self, symbol: str, start_date: Any, end_date: Any, adjustment: str
    ) -> bool:
        if not self.path.is_file():
            return False
        with self.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT coverage.start_date, coverage.end_date
                FROM data_coverage AS coverage
                JOIN security_master AS security USING (security_id)
                WHERE coverage.dataset = 'daily_bar'
                  AND security.symbol = ?
                  AND coverage.adjustment = ?
                  AND coverage.end_date >= ?
                  AND coverage.start_date <= ?
                ORDER BY coverage.start_date, coverage.end_date
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

    def read_benchmark_prices(
        self, symbol: str, start_date: Any, end_date: Any
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
