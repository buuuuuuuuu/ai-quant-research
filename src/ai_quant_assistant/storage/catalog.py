from __future__ import annotations

from pathlib import Path
from typing import Any


class DuckDBCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def register(self, run_id: str, snapshot: dict[str, Any]) -> None:
        import duckdb

        with duckdb.connect(str(self.path)) as connection:
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

