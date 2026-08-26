from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 8


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
    Migration(
        version=5,
        name="persistent_operations_tasks_and_database_backups",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS operation_task (
                task_id VARCHAR PRIMARY KEY,
                task_type VARCHAR NOT NULL
                    CHECK (task_type IN ('backtest', 'sync', 'backup')),
                status VARCHAR NOT NULL
                    CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
                progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
                message VARCHAR NOT NULL DEFAULT '',
                spec_json VARCHAR NOT NULL DEFAULT '{}',
                result_json VARCHAR,
                run_id VARCHAR,
                error VARCHAR,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS operation_task_type_created_idx
            ON operation_task (task_type, created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS database_backup (
                backup_id VARCHAR PRIMARY KEY,
                database_path VARCHAR NOT NULL,
                backup_path VARCHAR NOT NULL UNIQUE,
                size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
                sha256 VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ),
    ),
    Migration(
        version=6,
        name="release_hardening_alerts_audit_and_backup_lifecycle",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS operation_audit (
                event_id VARCHAR PRIMARY KEY,
                action VARCHAR NOT NULL,
                actor VARCHAR NOT NULL,
                outcome VARCHAR NOT NULL CHECK (outcome IN ('success', 'failure', 'warning')),
                remote_host VARCHAR,
                object_type VARCHAR,
                object_id VARCHAR,
                details_json VARCHAR NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS operation_audit_created_idx
            ON operation_audit (created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS operation_alert (
                alert_id VARCHAR PRIMARY KEY,
                alert_key VARCHAR NOT NULL UNIQUE,
                severity VARCHAR NOT NULL CHECK (severity IN ('warning', 'critical')),
                category VARCHAR NOT NULL,
                title VARCHAR NOT NULL,
                message VARCHAR NOT NULL,
                status VARCHAR NOT NULL
                    CHECK (status IN ('active', 'acknowledged', 'resolved')),
                details_json VARCHAR NOT NULL DEFAULT '{}',
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                acknowledged_at TIMESTAMPTZ,
                resolved_at TIMESTAMPTZ,
                notified_at TIMESTAMPTZ
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS operation_alert_status_seen_idx
            ON operation_alert (status, last_seen_at)
            """,
            "ALTER TABLE database_backup ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
            "ALTER TABLE database_backup ADD COLUMN IF NOT EXISTS deletion_reason VARCHAR",
        ),
    ),
    Migration(
        version=7,
        name="point_in_time_industries_constituents_and_dynamic_universe",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS security_industry_history (
                record_id VARCHAR PRIMARY KEY,
                security_id VARCHAR NOT NULL,
                classification_system VARCHAR NOT NULL,
                industry_code VARCHAR,
                industry_name VARCHAR NOT NULL,
                effective_from DATE NOT NULL,
                effective_until DATE,
                published_at TIMESTAMP NOT NULL,
                knowledge_time TIMESTAMP NOT NULL,
                history_mode VARCHAR NOT NULL
                    CHECK (history_mode IN ('verified_history', 'observed_snapshot')),
                source VARCHAR NOT NULL,
                source_batch_id VARCHAR NOT NULL,
                observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (effective_until IS NULL OR effective_until >= effective_from)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS security_industry_history_lookup_idx
            ON security_industry_history (security_id, classification_system, effective_from)
            """,
            """
            CREATE TABLE IF NOT EXISTS index_constituent_history (
                record_id VARCHAR PRIMARY KEY,
                benchmark_symbol VARCHAR NOT NULL,
                security_id VARCHAR NOT NULL,
                effective_from DATE NOT NULL,
                effective_until DATE,
                published_at TIMESTAMP NOT NULL,
                knowledge_time TIMESTAMP NOT NULL,
                history_mode VARCHAR NOT NULL
                    CHECK (history_mode IN ('verified_history', 'observed_snapshot')),
                weight DOUBLE,
                source VARCHAR NOT NULL,
                source_batch_id VARCHAR NOT NULL,
                observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (effective_until IS NULL OR effective_until >= effective_from),
                CHECK (weight IS NULL OR weight >= 0)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS index_constituent_history_lookup_idx
            ON index_constituent_history (benchmark_symbol, security_id, effective_from)
            """,
            """
            CREATE TABLE IF NOT EXISTS universe_sync_batch (
                batch_id VARCHAR PRIMARY KEY,
                dataset VARCHAR NOT NULL
                    CHECK (dataset IN ('industry_classification', 'index_constituents')),
                target VARCHAR NOT NULL,
                history_mode VARCHAR NOT NULL
                    CHECK (history_mode IN ('verified_history', 'observed_snapshot')),
                as_of DATE NOT NULL,
                status VARCHAR NOT NULL CHECK (status IN ('running', 'published', 'failed')),
                row_count BIGINT NOT NULL DEFAULT 0 CHECK (row_count >= 0),
                source VARCHAR NOT NULL,
                error VARCHAR,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
            """,
        ),
    ),
    Migration(
        version=8,
        name="ai_research_copilot_conversations_plans_and_traceability",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS copilot_conversation (
                conversation_id VARCHAR PRIMARY KEY,
                title VARCHAR NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'archived')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS copilot_message (
                message_id VARCHAR PRIMARY KEY,
                conversation_id VARCHAR NOT NULL,
                role VARCHAR NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
                content VARCHAR NOT NULL,
                plan_id VARCHAR,
                metadata_json VARCHAR NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS copilot_message_conversation_created_idx
            ON copilot_message (conversation_id, created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS research_plan (
                plan_id VARCHAR PRIMARY KEY,
                conversation_id VARCHAR NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
                status VARCHAR NOT NULL
                    CHECK (status IN (
                        'draft', 'blocked', 'confirmed', 'running',
                        'succeeded', 'failed', 'cancelled'
                    )),
                question VARCHAR NOT NULL,
                tool_name VARCHAR,
                title VARCHAR NOT NULL,
                rationale VARCHAR NOT NULL,
                spec_json VARCHAR NOT NULL DEFAULT '{}',
                assumptions_json VARCHAR NOT NULL DEFAULT '[]',
                warnings_json VARCHAR NOT NULL DEFAULT '[]',
                preflight_json VARCHAR NOT NULL DEFAULT '{}',
                planner VARCHAR NOT NULL,
                model_name VARCHAR,
                model_response_id VARCHAR,
                prompt_version VARCHAR NOT NULL DEFAULT 'm6a-v1',
                task_id VARCHAR,
                run_id VARCHAR,
                result_json VARCHAR,
                explanation_json VARCHAR,
                error VARCHAR,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TIMESTAMPTZ,
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS research_plan_conversation_created_idx
            ON research_plan (conversation_id, created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS research_plan_event (
                event_id VARCHAR PRIMARY KEY,
                plan_id VARCHAR NOT NULL,
                event_type VARCHAR NOT NULL,
                actor VARCHAR NOT NULL,
                details_json VARCHAR NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS research_plan_event_plan_created_idx
            ON research_plan_event (plan_id, created_at)
            """,
            """
            CREATE TABLE operation_task_v8 (
                task_id VARCHAR PRIMARY KEY,
                task_type VARCHAR NOT NULL
                    CHECK (task_type IN ('backtest', 'sync', 'backup', 'copilot')),
                status VARCHAR NOT NULL
                    CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
                progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
                message VARCHAR NOT NULL DEFAULT '',
                spec_json VARCHAR NOT NULL DEFAULT '{}',
                result_json VARCHAR,
                run_id VARCHAR,
                error VARCHAR,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "INSERT INTO operation_task_v8 SELECT * FROM operation_task",
            "DROP INDEX operation_task_type_created_idx",
            "DROP TABLE operation_task",
            "ALTER TABLE operation_task_v8 RENAME TO operation_task",
            """
            CREATE INDEX operation_task_type_created_idx
            ON operation_task (task_type, created_at)
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
