from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

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
        columns = ["date", "close", "symbol", "source"]
        if not self.path.is_file():
            return pd.DataFrame(columns=columns)
        with self.connect(read_only=True) as connection:
            frame = connection.execute(
                """
                SELECT bar.trade_date AS date, bar.close, security.symbol,
                       'local:published_daily_bar' AS source
                FROM published_daily_bar AS bar
                JOIN security_master AS security USING (security_id)
                WHERE security.symbol = ?
                  AND bar.adjustment = ?
                  AND bar.trade_date BETWEEN ? AND ?
                ORDER BY bar.trade_date
                """,
                [symbol, adjustment, start_date, end_date],
            ).fetchdf()
        if frame.empty:
            return pd.DataFrame(columns=columns)
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
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
