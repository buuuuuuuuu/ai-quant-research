from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 4


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
    Migration(
        version=2,
        name="incremental_sync_and_local_coverage",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS sync_partition (
                batch_id VARCHAR NOT NULL,
                partition_key VARCHAR NOT NULL,
                security_id VARCHAR NOT NULL,
                adjustment VARCHAR NOT NULL
                    CHECK (adjustment IN ('none', 'qfq', 'hfq')),
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'planned'
                    CHECK (status IN ('planned', 'fetching', 'published', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                row_count BIGINT NOT NULL DEFAULT 0 CHECK (row_count >= 0),
                error VARCHAR,
                raw_path VARCHAR,
                raw_sha256 VARCHAR,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (start_date <= end_date),
                PRIMARY KEY (batch_id, partition_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS adjustment_ratio (
                security_id VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                adjustment VARCHAR NOT NULL CHECK (adjustment IN ('qfq', 'hfq')),
                close_ratio DOUBLE NOT NULL CHECK (close_ratio > 0),
                method VARCHAR NOT NULL,
                source_batch_id VARCHAR NOT NULL,
                PRIMARY KEY (security_id, trade_date, adjustment)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS data_coverage (
                dataset VARCHAR NOT NULL,
                security_id VARCHAR NOT NULL,
                adjustment VARCHAR NOT NULL,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                source_batch_id VARCHAR NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (start_date <= end_date),
                PRIMARY KEY (dataset, security_id, adjustment, start_date, end_date)
            )
            """,
        ),
    ),
    Migration(
        version=3,
        name="local_benchmarks_and_point_in_time_announcements",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS benchmark_master (
                benchmark_id VARCHAR PRIMARY KEY,
                symbol VARCHAR NOT NULL UNIQUE,
                name VARCHAR NOT NULL,
                source VARCHAR NOT NULL,
                source_batch_id VARCHAR NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS benchmark_daily_bar (
                benchmark_id VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                close DOUBLE NOT NULL CHECK (close > 0),
                source VARCHAR NOT NULL,
                source_batch_id VARCHAR NOT NULL,
                quality_status VARCHAR NOT NULL DEFAULT 'published'
                    CHECK (quality_status IN ('pending', 'warning', 'published', 'rejected')),
                published_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (benchmark_id, trade_date)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS announcement (
                announcement_id VARCHAR PRIMARY KEY,
                security_id VARCHAR NOT NULL,
                company_name VARCHAR NOT NULL,
                title VARCHAR NOT NULL,
                report_type VARCHAR NOT NULL CHECK (report_type IN ('interim', 'annual')),
                report_year INTEGER NOT NULL,
                report_period_end DATE NOT NULL,
                announcement_date DATE NOT NULL,
                published_at TIMESTAMP NOT NULL,
                url VARCHAR NOT NULL DEFAULT '',
                source VARCHAR NOT NULL,
                source_batch_id VARCHAR NOT NULL,
                content_sha256 VARCHAR NOT NULL,
                quality_status VARCHAR NOT NULL DEFAULT 'published'
                    CHECK (quality_status IN ('pending', 'warning', 'published', 'rejected')),
                ingested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (announcement_date >= report_period_end)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS research_sync_partition (
                batch_id VARCHAR NOT NULL,
                dataset VARCHAR NOT NULL
                    CHECK (dataset IN ('benchmark_daily_bar', 'announcement')),
                partition_key VARCHAR NOT NULL,
                object_id VARCHAR NOT NULL,
                variant VARCHAR NOT NULL,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'planned'
                    CHECK (status IN ('planned', 'fetching', 'published', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                row_count BIGINT NOT NULL DEFAULT 0 CHECK (row_count >= 0),
                error VARCHAR,
                raw_path VARCHAR,
                raw_sha256 VARCHAR,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (start_date <= end_date),
                PRIMARY KEY (batch_id, partition_key)
            )
            """,
            """
            CREATE OR REPLACE VIEW published_benchmark_daily_bar AS
            SELECT * FROM benchmark_daily_bar WHERE quality_status = 'published'
            """,
            """
            CREATE OR REPLACE VIEW published_announcement AS
            SELECT * FROM announcement WHERE quality_status = 'published'
            """,
        ),
    ),
    Migration(
        version=4,
        name="point_in_time_financial_reports_and_quarterly_disclosures",
        statements=(
            """
            CREATE TABLE announcement_v4 (
                announcement_id VARCHAR PRIMARY KEY,
                security_id VARCHAR NOT NULL,
                company_name VARCHAR NOT NULL,
                title VARCHAR NOT NULL,
                report_type VARCHAR NOT NULL
                    CHECK (report_type IN ('q1', 'interim', 'q3', 'annual')),
                report_year INTEGER NOT NULL,
                report_period_end DATE NOT NULL,
                announcement_date DATE NOT NULL,
                published_at TIMESTAMP NOT NULL,
                url VARCHAR NOT NULL DEFAULT '',
                source VARCHAR NOT NULL,
                source_batch_id VARCHAR NOT NULL,
                content_sha256 VARCHAR NOT NULL,
                quality_status VARCHAR NOT NULL DEFAULT 'published'
                    CHECK (quality_status IN ('pending', 'warning', 'published', 'rejected')),
                ingested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (announcement_date >= report_period_end)
            )
            """,
            "INSERT INTO announcement_v4 SELECT * FROM announcement",
            "DROP VIEW published_announcement",
            "DROP TABLE announcement",
            "ALTER TABLE announcement_v4 RENAME TO announcement",
            """
            CREATE VIEW published_announcement AS
            SELECT * FROM announcement WHERE quality_status = 'published'
            """,
            """
            CREATE TABLE IF NOT EXISTS financial_report (
                report_id VARCHAR PRIMARY KEY,
                security_id VARCHAR NOT NULL,
                report_period_end DATE NOT NULL,
                report_type VARCHAR NOT NULL
                    CHECK (report_type IN ('q1', 'interim', 'q3', 'annual')),
                published_at TIMESTAMP NOT NULL,
                availability_source VARCHAR NOT NULL
                    CHECK (availability_source IN ('provider_notice', 'matched_announcement')),
                source VARCHAR NOT NULL,
                source_batch_id VARCHAR NOT NULL,
                raw_sha256 VARCHAR NOT NULL,
                quality_status VARCHAR NOT NULL DEFAULT 'published'
                    CHECK (quality_status IN ('pending', 'warning', 'published', 'rejected')),
                observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (CAST(published_at AS DATE) >= report_period_end),
                UNIQUE (security_id, report_period_end, published_at)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS financial_metric (
                report_id VARCHAR NOT NULL,
                security_id VARCHAR NOT NULL,
                report_period_end DATE NOT NULL,
                published_at TIMESTAMP NOT NULL,
                metric_code VARCHAR NOT NULL,
                metric_value DOUBLE NOT NULL,
                unit VARCHAR NOT NULL,
                source_batch_id VARCHAR NOT NULL,
                quality_status VARCHAR NOT NULL DEFAULT 'published'
                    CHECK (quality_status IN ('pending', 'warning', 'published', 'rejected')),
                PRIMARY KEY (report_id, metric_code)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS financial_sync_partition (
                batch_id VARCHAR NOT NULL,
                partition_key VARCHAR NOT NULL,
                security_id VARCHAR NOT NULL,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'planned'
                    CHECK (status IN ('planned', 'fetching', 'published', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                row_count BIGINT NOT NULL DEFAULT 0 CHECK (row_count >= 0),
                metric_count BIGINT NOT NULL DEFAULT 0 CHECK (metric_count >= 0),
                error VARCHAR,
                raw_path VARCHAR,
                raw_sha256 VARCHAR,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (start_date <= end_date),
                PRIMARY KEY (batch_id, partition_key)
            )
            """,
            """
            CREATE OR REPLACE VIEW published_financial_report AS
            SELECT * FROM financial_report WHERE quality_status = 'published'
            """,
            """
            CREATE OR REPLACE VIEW published_financial_metric AS
            SELECT * FROM financial_metric WHERE quality_status = 'published'
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
