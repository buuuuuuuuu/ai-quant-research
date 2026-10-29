from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ai_quant_assistant.domain.models import DEFAULT_RESEARCH_SYMBOLS

SCHEMA_VERSION = 20


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    handler: Any | None = None


def _security_id(symbol: str) -> str:
    return f"{'SSE' if symbol.startswith('6') else 'SZSE'}.{symbol}"


def _symbols_sha256(symbols: list[str]) -> str:
    canonical = json.dumps(symbols, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _migrate_stock_pools_v13(connection: Any) -> None:
    default_symbols = list(DEFAULT_RESEARCH_SYMBOLS)
    connection.execute(
        """
        INSERT INTO stock_pool (
            pool_id, name, description, source_type, history_verified, warning,
            status, is_default, current_revision
        ) SELECT ?, ?, ?, 'system_default', FALSE, ?, 'active', TRUE, 1
        WHERE NOT EXISTS (SELECT 1 FROM stock_pool WHERE pool_id = ?)
        """,
        [
            "pool-default-research",
            "默认研究股票池",
            "由 M9-A 从原 DEFAULT_RESEARCH_SYMBOLS 初始化",
            "手工维护的当前股票池不代表历史指数成分，历史研究可能存在幸存者偏差。",
            "pool-default-research",
        ],
    )
    if connection.execute(
        "SELECT COUNT(*) FROM stock_pool_revision WHERE pool_id = 'pool-default-research'"
    ).fetchone()[0] == 0:
        connection.executemany(
            """
            INSERT INTO stock_pool_member (pool_id, security_id, display_order)
            VALUES ('pool-default-research', ?, ?)
            """,
            [(_security_id(symbol), index) for index, symbol in enumerate(default_symbols, 1)],
        )
        connection.execute(
            """
            INSERT INTO stock_pool_revision (
                pool_id, revision_no, symbols_json, symbols_sha256,
                change_type, change_summary
            ) VALUES ('pool-default-research', 1, ?, ?, 'initialized', ?)
            """,
            [
                json.dumps(default_symbols, ensure_ascii=False),
                _symbols_sha256(default_symbols),
                f"从兼容常量初始化 {len(default_symbols)} 只股票",
            ],
        )

    rows = connection.execute(
        """
        SELECT watchlist_id, name, screen_run_id, as_of, symbols_json,
               history_verified, warning, created_at
        FROM saved_watchlist ORDER BY created_at, watchlist_id
        """
    ).fetchall()
    for row in rows:
        watchlist_id = str(row[0])
        if connection.execute(
            "SELECT COUNT(*) FROM stock_pool WHERE pool_id = ?", [watchlist_id]
        ).fetchone()[0]:
            continue
        try:
            decoded = json.loads(str(row[4]))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"saved_watchlist {watchlist_id} contains invalid JSON; migration aborted"
            ) from exc
        if not isinstance(decoded, list) or not decoded:
            raise ValueError(
                f"saved_watchlist {watchlist_id} must contain a non-empty symbol array"
            )
        symbols = [str(item).strip() for item in decoded]
        invalid = [item for item in symbols if len(item) != 6 or not item.isdigit()]
        if invalid:
            raise ValueError(
                f"saved_watchlist {watchlist_id} contains invalid symbols: {invalid}"
            )
        if len(set(symbols)) != len(symbols):
            raise ValueError(
                f"saved_watchlist {watchlist_id} contains duplicate symbols; migration aborted"
            )
        known = {
            str(item[0])
            for item in connection.execute(
                "SELECT symbol FROM security_master WHERE symbol IN (SELECT * FROM UNNEST(?))",
                [symbols],
            ).fetchall()
        }
        missing = [symbol for symbol in symbols if symbol not in known]
        if missing:
            raise ValueError(
                f"saved_watchlist {watchlist_id} references symbols missing from security_master: {missing}"
            )
        connection.execute(
            """
            INSERT INTO stock_pool (
                pool_id, name, description, source_type, source_screen_run_id,
                as_of, history_verified, warning, status, is_default,
                current_revision, created_at, updated_at
            ) VALUES (?, ?, '', 'screen_snapshot', ?, ?, ?, ?, 'active', FALSE, 1, ?, ?)
            """,
            [row[0], row[1], row[2], row[3], row[5], row[6], row[7], row[7]],
        )
        connection.executemany(
            """
            INSERT INTO stock_pool_member (pool_id, security_id, display_order, added_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (watchlist_id, _security_id(symbol), index, row[7], row[7])
                for index, symbol in enumerate(symbols, 1)
            ],
        )
        connection.execute(
            """
            INSERT INTO stock_pool_revision (
                pool_id, revision_no, symbols_json, symbols_sha256,
                change_type, change_summary, created_at
            ) VALUES (?, 1, ?, ?, 'watchlist_migrated', ?, ?)
            """,
            [
                watchlist_id,
                json.dumps(symbols, ensure_ascii=False),
                _symbols_sha256(symbols),
                f"由 saved_watchlist {watchlist_id} 迁移，保留原标识",
                row[7],
            ],
        )


def _migrate_managed_stocks_v14(connection: Any) -> None:
    from ai_quant_assistant.storage.managed_coverage import seed_managed_stocks

    seed_managed_stocks(connection)


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
    Migration(
        version=9,
        name="point_in_time_screen_runs_results_and_saved_watchlists",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS screen_run (
                run_id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                status VARCHAR NOT NULL
                    CHECK (status IN ('running', 'succeeded', 'failed')),
                as_of TIMESTAMP NOT NULL,
                universe_mode VARCHAR NOT NULL
                    CHECK (universe_mode IN (
                        'explicit', 'all_available', 'historical_index', 'observed_index'
                    )),
                spec_json VARCHAR NOT NULL,
                candidate_count BIGINT NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
                selected_count BIGINT NOT NULL DEFAULT 0 CHECK (selected_count >= 0),
                diagnostics_json VARCHAR NOT NULL DEFAULT '{}',
                error VARCHAR,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMPTZ NOT NULL,
                completed_at TIMESTAMPTZ
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS screen_run_created_idx
            ON screen_run (created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS screen_result (
                run_id VARCHAR NOT NULL,
                security_id VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                name VARCHAR NOT NULL,
                industry_name VARCHAR,
                selected BOOLEAN NOT NULL,
                selection_rank INTEGER,
                values_json VARCHAR NOT NULL DEFAULT '{}',
                evaluations_json VARCHAR NOT NULL DEFAULT '[]',
                evidence_json VARCHAR NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (selection_rank IS NULL OR selection_rank >= 1),
                PRIMARY KEY (run_id, security_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS screen_result_run_selected_rank_idx
            ON screen_result (run_id, selected, selection_rank)
            """,
            """
            CREATE TABLE IF NOT EXISTS saved_watchlist (
                watchlist_id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                screen_run_id VARCHAR NOT NULL,
                as_of TIMESTAMP NOT NULL,
                symbols_json VARCHAR NOT NULL,
                history_verified BOOLEAN NOT NULL DEFAULT FALSE,
                warning VARCHAR,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS saved_watchlist_created_idx
            ON saved_watchlist (created_at)
            """,
        ),
    ),
    Migration(
        version=10,
        name="announcement_document_archive_pages_search_and_cited_answers",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS announcement_document (
                document_id VARCHAR PRIMARY KEY,
                announcement_id VARCHAR NOT NULL,
                version_no INTEGER NOT NULL CHECK (version_no >= 1),
                source_url VARCHAR NOT NULL,
                storage_path VARCHAR NOT NULL,
                sha256 VARCHAR NOT NULL,
                size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
                media_type VARCHAR NOT NULL DEFAULT 'application/pdf',
                status VARCHAR NOT NULL
                    CHECK (status IN ('archived', 'extracting', 'ready', 'failed')),
                page_count INTEGER CHECK (page_count IS NULL OR page_count >= 0),
                extraction_method VARCHAR NOT NULL DEFAULT 'pending'
                    CHECK (extraction_method IN ('pending', 'native', 'ocr', 'mixed', 'failed')),
                parser_version VARCHAR NOT NULL DEFAULT 'm8a-v1',
                downloaded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                extracted_at TIMESTAMPTZ,
                error VARCHAR,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (announcement_id, sha256),
                UNIQUE (announcement_id, version_no)
            )
            """,
            """
            CREATE INDEX announcement_document_announcement_version_idx
            ON announcement_document (announcement_id, version_no)
            """,
            """
            CREATE TABLE IF NOT EXISTS document_page (
                document_id VARCHAR NOT NULL,
                page_number INTEGER NOT NULL CHECK (page_number >= 1),
                text VARCHAR NOT NULL,
                text_sha256 VARCHAR NOT NULL,
                extraction_method VARCHAR NOT NULL
                    CHECK (extraction_method IN ('native', 'ocr', 'empty')),
                char_count BIGINT NOT NULL CHECK (char_count >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (document_id, page_number)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS document_chunk (
                chunk_id VARCHAR PRIMARY KEY,
                document_id VARCHAR NOT NULL,
                page_number INTEGER NOT NULL CHECK (page_number >= 1),
                chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
                text VARCHAR NOT NULL,
                text_sha256 VARCHAR NOT NULL,
                token_estimate INTEGER NOT NULL CHECK (token_estimate >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (document_id, page_number, chunk_index)
            )
            """,
            """
            CREATE INDEX document_chunk_document_page_idx
            ON document_chunk (document_id, page_number, chunk_index)
            """,
            """
            CREATE TABLE IF NOT EXISTS document_extraction (
                extraction_id VARCHAR PRIMARY KEY,
                document_id VARCHAR NOT NULL,
                parser_version VARCHAR NOT NULL,
                status VARCHAR NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
                native_pages INTEGER NOT NULL DEFAULT 0 CHECK (native_pages >= 0),
                ocr_pages INTEGER NOT NULL DEFAULT 0 CHECK (ocr_pages >= 0),
                empty_pages INTEGER NOT NULL DEFAULT 0 CHECK (empty_pages >= 0),
                started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMPTZ,
                error VARCHAR
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS document_question (
                question_id VARCHAR PRIMARY KEY,
                scope_json VARCHAR NOT NULL,
                question VARCHAR NOT NULL,
                status VARCHAR NOT NULL
                    CHECK (status IN ('answered', 'insufficient', 'refused', 'failed')),
                answer VARCHAR NOT NULL DEFAULT '',
                provider VARCHAR NOT NULL,
                model VARCHAR,
                prompt_version VARCHAR NOT NULL DEFAULT 'm8a-v1',
                limitations_json VARCHAR NOT NULL DEFAULT '[]',
                error VARCHAR,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS document_answer_citation (
                question_id VARCHAR NOT NULL,
                citation_order INTEGER NOT NULL CHECK (citation_order >= 1),
                chunk_id VARCHAR NOT NULL,
                document_id VARCHAR NOT NULL,
                page_number INTEGER NOT NULL CHECK (page_number >= 1),
                quote VARCHAR NOT NULL,
                claim VARCHAR NOT NULL,
                PRIMARY KEY (question_id, citation_order)
            )
            """,
            """
            CREATE TABLE operation_task_v10 (
                task_id VARCHAR PRIMARY KEY,
                task_type VARCHAR NOT NULL
                    CHECK (task_type IN (
                        'backtest', 'sync', 'backup', 'copilot', 'document_ingest'
                    )),
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
            "INSERT INTO operation_task_v10 SELECT * FROM operation_task",
            "DROP INDEX operation_task_type_created_idx",
            "DROP TABLE operation_task",
            "ALTER TABLE operation_task_v10 RENAME TO operation_task",
            """
            CREATE INDEX operation_task_type_created_idx
            ON operation_task (task_type, created_at)
            """,
        ),
    ),
    Migration(
        version=11,
        name="document_ingest_claims_leases_and_extraction_fencing",
        statements=(
            "ALTER TABLE announcement_document ADD COLUMN active_extraction_id VARCHAR",
            "ALTER TABLE document_extraction ADD COLUMN heartbeat_at TIMESTAMPTZ",
            """
            CREATE TABLE operation_task_v11 (
                task_id VARCHAR PRIMARY KEY,
                task_type VARCHAR NOT NULL
                    CHECK (task_type IN (
                        'backtest', 'sync', 'backup', 'copilot', 'document_ingest'
                    )),
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
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                claim_key VARCHAR UNIQUE,
                owner_id VARCHAR,
                heartbeat_at TIMESTAMPTZ
            )
            """,
            """
            INSERT INTO operation_task_v11 (
                task_id, task_type, status, progress, message, spec_json, result_json,
                run_id, error, created_at, started_at, completed_at, updated_at,
                claim_key, owner_id, heartbeat_at
            )
            SELECT task_id, task_type, status, progress, message, spec_json, result_json,
                   run_id, error, created_at, started_at, completed_at, updated_at,
                   NULL, NULL,
                   CASE WHEN status = 'running' THEN updated_at ELSE NULL END
            FROM operation_task
            """,
            "DROP INDEX operation_task_type_created_idx",
            "DROP TABLE operation_task",
            "ALTER TABLE operation_task_v11 RENAME TO operation_task",
            """
            CREATE INDEX operation_task_type_created_idx
            ON operation_task (task_type, created_at)
            """,
            """
            CREATE INDEX operation_task_claim_status_idx
            ON operation_task (claim_key, status)
            """,
        ),
    ),
    Migration(
        version=12,
        name="document_event_intelligence_evidence_and_point_in_time_research",
        statements=(
            "ALTER TABLE announcement_document ADD COLUMN active_event_extraction_id VARCHAR",
            """
            CREATE TABLE document_event_extraction (
                extraction_id VARCHAR PRIMARY KEY,
                document_id VARCHAR NOT NULL,
                extractor VARCHAR NOT NULL,
                extractor_version VARCHAR NOT NULL,
                status VARCHAR NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
                heartbeat_at TIMESTAMPTZ,
                started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMPTZ,
                error VARCHAR
            )
            """,
            """
            CREATE INDEX document_event_extraction_document_started_idx
            ON document_event_extraction (document_id, started_at)
            """,
            """
            CREATE TABLE document_event (
                event_id VARCHAR PRIMARY KEY,
                extraction_id VARCHAR NOT NULL,
                announcement_id VARCHAR NOT NULL,
                document_id VARCHAR NOT NULL,
                security_id VARCHAR NOT NULL,
                event_type VARCHAR NOT NULL CHECK (event_type IN (
                    'performance_forecast', 'dividend', 'share_repurchase',
                    'major_contract', 'shareholder_change'
                )),
                published_at TIMESTAMPTZ NOT NULL,
                known_at TIMESTAMPTZ NOT NULL,
                event_date DATE,
                period_start DATE,
                period_end DATE,
                direction VARCHAR NOT NULL DEFAULT 'unknown'
                    CHECK (direction IN ('positive', 'negative', 'neutral', 'mixed', 'unknown')),
                amount DOUBLE,
                currency VARCHAR,
                ratio DOUBLE,
                structured_payload_json VARCHAR NOT NULL DEFAULT '{}',
                extractor VARCHAR NOT NULL,
                extractor_version VARCHAR NOT NULL,
                status VARCHAR NOT NULL CHECK (
                    status IN ('insufficient', 'needs_review', 'accepted', 'rejected')
                ),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TIMESTAMPTZ,
                reviewed_by VARCHAR,
                review_note VARCHAR,
                UNIQUE (document_id, event_type, extractor_version)
            )
            """,
            """
            CREATE INDEX document_event_pit_idx
            ON document_event (security_id, published_at, status, event_type)
            """,
            """
            CREATE TABLE document_event_evidence (
                event_id VARCHAR NOT NULL,
                evidence_order INTEGER NOT NULL CHECK (evidence_order >= 1),
                field_name VARCHAR NOT NULL,
                document_id VARCHAR NOT NULL,
                page_number INTEGER NOT NULL CHECK (page_number >= 1),
                chunk_id VARCHAR NOT NULL,
                quote VARCHAR NOT NULL,
                quote_sha256 VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (event_id, evidence_order)
            )
            """,
            """
            CREATE INDEX document_event_evidence_document_page_idx
            ON document_event_evidence (document_id, page_number, chunk_id)
            """,
            """
            CREATE TABLE document_event_research_run (
                run_id VARCHAR PRIMARY KEY,
                event_id VARCHAR NOT NULL,
                announcement_id VARCHAR NOT NULL,
                document_id VARCHAR NOT NULL,
                as_of TIMESTAMPTZ NOT NULL,
                spec_json VARCHAR NOT NULL,
                result_json VARCHAR NOT NULL,
                evidence_json VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE INDEX document_event_research_event_created_idx
            ON document_event_research_run (event_id, created_at)
            """,
            """
            CREATE TABLE operation_task_v12 (
                task_id VARCHAR PRIMARY KEY,
                task_type VARCHAR NOT NULL
                    CHECK (task_type IN (
                        'backtest', 'sync', 'backup', 'copilot', 'document_ingest',
                        'document_event_extract'
                    )),
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
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                claim_key VARCHAR UNIQUE,
                owner_id VARCHAR,
                heartbeat_at TIMESTAMPTZ
            )
            """,
            """
            INSERT INTO operation_task_v12 (
                task_id, task_type, status, progress, message, spec_json, result_json,
                run_id, error, created_at, started_at, completed_at, updated_at,
                claim_key, owner_id, heartbeat_at
            )
            SELECT task_id, task_type, status, progress, message, spec_json, result_json,
                   run_id, error, created_at, started_at, completed_at, updated_at,
                   claim_key, owner_id, heartbeat_at
            FROM operation_task
            """,
            "DROP INDEX operation_task_type_created_idx",
            "DROP INDEX operation_task_claim_status_idx",
            "DROP TABLE operation_task",
            "ALTER TABLE operation_task_v12 RENAME TO operation_task",
            """
            CREATE INDEX operation_task_type_created_idx
            ON operation_task (task_type, created_at)
            """,
            """
            CREATE INDEX operation_task_claim_status_idx
            ON operation_task (claim_key, status)
            """,
        ),
    ),
    Migration(
        version=13,
        name="versioned_custom_stock_pools_and_research_scope",
        statements=(
            """
            CREATE TABLE stock_pool (
                pool_id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                description VARCHAR NOT NULL DEFAULT '',
                source_type VARCHAR NOT NULL CHECK (source_type IN (
                    'system_default', 'manual', 'screen_snapshot', 'imported'
                )),
                source_screen_run_id VARCHAR,
                as_of TIMESTAMPTZ,
                history_verified BOOLEAN NOT NULL DEFAULT FALSE,
                warning VARCHAR,
                status VARCHAR NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'archived')),
                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                current_revision INTEGER NOT NULL DEFAULT 1 CHECK (current_revision >= 1),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE INDEX stock_pool_status_updated_idx
            ON stock_pool (status, updated_at)
            """,
            """
            CREATE TABLE stock_pool_member (
                pool_id VARCHAR NOT NULL,
                security_id VARCHAR NOT NULL,
                display_order INTEGER NOT NULL CHECK (display_order >= 1),
                note VARCHAR NOT NULL DEFAULT '',
                added_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (pool_id, security_id)
            )
            """,
            """
            CREATE INDEX stock_pool_member_order_idx
            ON stock_pool_member (pool_id, display_order, security_id)
            """,
            """
            CREATE TABLE stock_pool_revision (
                pool_id VARCHAR NOT NULL,
                revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
                symbols_json VARCHAR NOT NULL,
                symbols_sha256 VARCHAR NOT NULL,
                change_type VARCHAR NOT NULL,
                change_summary VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (pool_id, revision_no)
            )
            """,
            """
            CREATE TABLE operation_task_v13 (
                task_id VARCHAR PRIMARY KEY,
                task_type VARCHAR NOT NULL CHECK (task_type IN (
                    'backtest', 'sync', 'backup', 'copilot', 'document_ingest',
                    'document_event_extract', 'stock_pool_sync'
                )),
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
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                claim_key VARCHAR UNIQUE,
                owner_id VARCHAR,
                heartbeat_at TIMESTAMPTZ
            )
            """,
            """
            INSERT INTO operation_task_v13
            SELECT * FROM operation_task
            """,
            "DROP INDEX operation_task_type_created_idx",
            "DROP INDEX operation_task_claim_status_idx",
            "DROP TABLE operation_task",
            "ALTER TABLE operation_task_v13 RENAME TO operation_task",
            """
            CREATE INDEX operation_task_type_created_idx
            ON operation_task (task_type, created_at)
            """,
            """
            CREATE INDEX operation_task_claim_status_idx
            ON operation_task (claim_key, status)
            """,
        ),
        handler=_migrate_stock_pools_v13,
    ),
    Migration(
        version=14,
        name="managed_stock_data_and_readiness",
        statements=(
            """CREATE TABLE managed_stock (
                security_id VARCHAR PRIMARY KEY,
                status VARCHAR NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
                target_start DATE NOT NULL,
                target_end DATE NOT NULL,
                auto_update BOOLEAN NOT NULL DEFAULT TRUE,
                availability VARCHAR NOT NULL DEFAULT 'unavailable'
                    CHECK(availability IN ('unavailable','partial','ready')),
                coverage_json VARCHAR NOT NULL DEFAULT '{}',
                listing_date DATE,
                listing_source VARCHAR,
                last_task_id VARCHAR,
                last_success_at TIMESTAMPTZ,
                revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                generation INTEGER NOT NULL DEFAULT 1 CHECK(generation >= 1),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(target_start <= target_end)
            )""",
            "CREATE INDEX managed_stock_status_idx ON managed_stock(status, auto_update)",
        ),
        handler=_migrate_managed_stocks_v14,
    ),
    Migration(
        version=15,
        name="confirmed_research_data_plans",
        statements=(
            """CREATE TABLE research_data_plan (
                plan_id VARCHAR PRIMARY KEY,
                version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                input_sha256 VARCHAR NOT NULL,
                input_json VARCHAR NOT NULL,
                items_json VARCHAR NOT NULL,
                report_json VARCHAR NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'draft'
                    CHECK(status IN ('draft','confirmed','succeeded','failed')),
                actor VARCHAR NOT NULL,
                confirmed_by VARCHAR,
                confirmed_at TIMESTAMPTZ,
                last_task_id VARCHAR,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
        ),
    ),
    Migration(
        version=16,
        name="immutable_stock_research_summaries_and_run_index",
        statements=(
            """CREATE TABLE stock_research_summary (
                summary_id VARCHAR PRIMARY KEY,
                security_id VARCHAR NOT NULL,
                fingerprint VARCHAR NOT NULL UNIQUE,
                snapshot_json VARCHAR NOT NULL,
                html_content VARCHAR NOT NULL,
                html_sha256 VARCHAR NOT NULL,
                actor VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX stock_summary_security_idx ON stock_research_summary(security_id, created_at)",
            """CREATE TABLE stock_research_run (
                run_id VARCHAR PRIMARY KEY,
                research_type VARCHAR NOT NULL,
                symbols_json VARCHAR NOT NULL,
                spec_json VARCHAR NOT NULL,
                result_json VARCHAR NOT NULL,
                completed_at TIMESTAMPTZ NOT NULL
            )""",
        ),
    ),
    Migration(
        version=17,
        name="immutable_stock_pool_research_briefs",
        statements=(
            "ALTER TABLE announcement ADD COLUMN first_source_batch_id VARCHAR",
            "ALTER TABLE announcement ADD COLUMN first_visible_at TIMESTAMPTZ",
            "ALTER TABLE financial_report ADD COLUMN first_visible_at TIMESTAMPTZ",
            """CREATE TABLE research_brief (
                brief_id VARCHAR PRIMARY KEY,
                fingerprint VARCHAR NOT NULL UNIQUE,
                pool_id VARCHAR NOT NULL,
                pool_revision INTEGER NOT NULL CHECK(pool_revision >= 1),
                window_start DATE NOT NULL,
                as_of DATE NOT NULL,
                timezone VARCHAR NOT NULL,
                symbols_json VARCHAR NOT NULL,
                symbols_sha256 VARCHAR NOT NULL,
                snapshot_json VARCHAR NOT NULL,
                html_content VARCHAR NOT NULL,
                html_sha256 VARCHAR NOT NULL,
                actor VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(window_start <= as_of)
            )""",
            "CREATE INDEX research_brief_pool_created_idx ON research_brief(pool_id, pool_revision, created_at)",
            "CREATE INDEX research_brief_pool_asof_idx ON research_brief(pool_id, pool_revision, as_of)",
        ),
    ),
    Migration(
        version=18,
        name="cancelled_operation_tasks",
        statements=(
            """CREATE TABLE operation_task_v18 (
                task_id VARCHAR PRIMARY KEY,
                task_type VARCHAR NOT NULL CHECK (task_type IN (
                    'backtest', 'sync', 'backup', 'copilot', 'document_ingest',
                    'document_event_extract', 'stock_pool_sync'
                )),
                status VARCHAR NOT NULL CHECK (status IN (
                    'queued', 'running', 'succeeded', 'failed', 'cancelled'
                )),
                progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
                message VARCHAR NOT NULL DEFAULT '',
                spec_json VARCHAR NOT NULL DEFAULT '{}',
                result_json VARCHAR,
                run_id VARCHAR,
                error VARCHAR,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                claim_key VARCHAR UNIQUE,
                owner_id VARCHAR,
                heartbeat_at TIMESTAMPTZ
            )""",
            """INSERT INTO operation_task_v18 SELECT * FROM operation_task""",
            "DROP INDEX operation_task_type_created_idx",
            "DROP INDEX operation_task_claim_status_idx",
            "DROP TABLE operation_task",
            "ALTER TABLE operation_task_v18 RENAME TO operation_task",
            "CREATE INDEX operation_task_type_created_idx ON operation_task (task_type, created_at)",
            "CREATE INDEX operation_task_claim_status_idx ON operation_task (claim_key, status)",
        ),
    ),
    Migration(
        version=19,
        name="index_fund_snapshots",
        statements=(
            """CREATE TABLE index_fund_snapshot (
                snapshot_id VARCHAR PRIMARY KEY,
                index_code VARCHAR NOT NULL,
                spec_json VARCHAR NOT NULL,
                input_json VARCHAR NOT NULL,
                result_json VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX index_fund_snapshot_index_created_idx ON index_fund_snapshot(index_code, created_at)",
        ),
    ),
    Migration(
        version=20,
        name="index_fund_sync_checkpoints",
        statements=(
            """CREATE TABLE index_fund_sync_checkpoint (
                scope_id VARCHAR NOT NULL,
                partition_key VARCHAR NOT NULL,
                payload_json VARCHAR NOT NULL,
                fetched_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (scope_id, partition_key)
            )""",
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
            if migration.handler is not None:
                migration.handler(connection)
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
