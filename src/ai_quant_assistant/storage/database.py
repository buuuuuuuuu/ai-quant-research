from __future__ import annotations

from pathlib import Path
from typing import Any

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
