from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        version=1,
        name="data_platform_foundation",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS security_master (
                security_id VARCHAR PRIMARY KEY,
                symbol VARCHAR NOT NULL,
                exchange VARCHAR NOT NULL CHECK (exchange IN ('SSE', 'SZSE', 'BSE')),
                name VARCHAR NOT NULL,
                list_date DATE,
                delist_date DATE,
                status VARCHAR NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'suspended', 'delisted')),
                industry VARCHAR,
                source VARCHAR NOT NULL,
                source_batch_id VARCHAR NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (symbol, exchange)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS trading_calendar (
                exchange VARCHAR NOT NULL CHECK (exchange IN ('SSE', 'SZSE', 'BSE')),
                trade_date DATE NOT NULL,
                is_open BOOLEAN NOT NULL,
                previous_open_date DATE,
                next_open_date DATE,
                session_no BIGINT,
                source VARCHAR NOT NULL,
                source_batch_id VARCHAR NOT NULL,
                PRIMARY KEY (exchange, trade_date)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS daily_bar_raw (
                security_id VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                source VARCHAR NOT NULL,
                batch_id VARCHAR NOT NULL,
                open DOUBLE NOT NULL CHECK (open > 0),
                high DOUBLE NOT NULL CHECK (high > 0),
                low DOUBLE NOT NULL CHECK (low > 0),
                close DOUBLE NOT NULL CHECK (close > 0),
                volume DOUBLE NOT NULL CHECK (volume >= 0),
                amount DOUBLE NOT NULL CHECK (amount >= 0),
                ingested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (high >= open AND high >= close AND high >= low),
                CHECK (low <= open AND low <= close AND low <= high),
                PRIMARY KEY (security_id, trade_date, source, batch_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS daily_bar (
                security_id VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                adjustment VARCHAR NOT NULL
                    CHECK (adjustment IN ('none', 'qfq', 'hfq')),
                open DOUBLE NOT NULL CHECK (open > 0),
                high DOUBLE NOT NULL CHECK (high > 0),
                low DOUBLE NOT NULL CHECK (low > 0),
                close DOUBLE NOT NULL CHECK (close > 0),
                volume DOUBLE NOT NULL CHECK (volume >= 0),
                amount DOUBLE NOT NULL CHECK (amount >= 0),
                source_batch_id VARCHAR NOT NULL,
                quality_status VARCHAR NOT NULL DEFAULT 'pending'
                    CHECK (quality_status IN ('pending', 'warning', 'published', 'rejected')),
                published_at TIMESTAMP,
                CHECK (high >= open AND high >= close AND high >= low),
                CHECK (low <= open AND low <= close AND low <= high),
                PRIMARY KEY (security_id, trade_date, adjustment)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sync_batch (
                batch_id VARCHAR PRIMARY KEY,
                dataset VARCHAR NOT NULL,
                status VARCHAR NOT NULL
                    CHECK (status IN (
                        'planned', 'fetching', 'normalizing', 'validating', 'published', 'failed'
                    )),
                started_at TIMESTAMP,
                ended_at TIMESTAMP,
                request_json VARCHAR NOT NULL DEFAULT '{}',
                row_count BIGINT NOT NULL DEFAULT 0 CHECK (row_count >= 0),
                error VARCHAR,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS dq_result (
                batch_id VARCHAR NOT NULL,
                rule_id VARCHAR NOT NULL,
                object_key VARCHAR NOT NULL,
                severity VARCHAR NOT NULL CHECK (severity IN ('warning', 'blocking')),
                passed BOOLEAN NOT NULL,
                observed VARCHAR,
                expected VARCHAR,
                details VARCHAR,
                checked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (batch_id, rule_id, object_key)
            )
            """,
            """
            CREATE OR REPLACE VIEW published_daily_bar AS
            SELECT *
            FROM daily_bar
            WHERE quality_status = 'published'
            """,
        ),
    ),
)


def apply_migrations(connection: Any) -> list[int]:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {
        int(row[0])
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    newly_applied: list[int] = []
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        connection.execute("BEGIN TRANSACTION")
        try:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                [migration.version, migration.name],
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        newly_applied.append(migration.version)
    return newly_applied
