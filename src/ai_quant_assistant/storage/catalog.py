from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout


class DuckDBCatalog:
    def __init__(self, path: Path, *, database=None) -> None:
        self.path = path
        if database is not None and database.path.resolve() != path.resolve():
            raise ValueError("snapshot catalog and shared database paths must match")
        self.database = database or LocalDatabase(path, DataLayout.from_root(path.parent))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def register(self, run_id: str, snapshot: dict[str, Any]) -> None:
        # Shared application catalogs must participate in the same process and
        # file lock as stock snapshots and backups. Standalone catalogs use the
        # same path-keyed lock too, without initializing the application schema.
        with self.database.connect() as connection:
            connection.execute(
                """
                create table if not exists snapshot_catalog (
                    run_id varchar,
                    name varchar,
                    path varchar,
                    format varchar,
                    rows bigint,
                    sha256 varchar,
                    created_at timestamp,
                    primary key (run_id, name)
                )
                """
            )
            connection.execute(
                """
                insert or replace into snapshot_catalog
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    snapshot["name"],
                    snapshot["path"],
                    snapshot["format"],
                    snapshot["rows"],
                    snapshot["sha256"],
                    snapshot["created_at"],
                ],
            )
