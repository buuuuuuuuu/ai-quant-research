from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pandas as pd

from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.quality import DataQualityCheck


def _db_value(value: Any) -> Any:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value


class SyncRepository:
    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    @staticmethod
    def canonical_request(request: dict[str, Any]) -> str:
        return json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def begin_batch(
        self, request: dict[str, Any], *, resume: bool
    ) -> tuple[str, bool, str]:
        request_json = self.canonical_request(request)
        with self.database.connect() as connection:
            if resume:
                row = connection.execute(
                    """
                    SELECT batch_id, status
                    FROM sync_batch
                    WHERE dataset = 'daily_bar' AND request_json = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    [request_json],
                ).fetchone()
                if row:
                    if row[1] != "published":
                        connection.execute(
                            """
                            UPDATE sync_batch
                            SET status = 'fetching', ended_at = NULL, error = NULL,
                                started_at = COALESCE(started_at, CURRENT_TIMESTAMP)
                            WHERE batch_id = ?
                            """,
                            [row[0]],
                        )
                    return str(row[0]), True, str(row[1])

            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            batch_id = f"daily-bar-{timestamp}-{uuid4().hex[:8]}"
            connection.execute(
                """
                INSERT INTO sync_batch (
                    batch_id, dataset, status, started_at, request_json
                ) VALUES (?, 'daily_bar', 'fetching', CURRENT_TIMESTAMP, ?)
                """,
                [batch_id, request_json],
            )
            return batch_id, False, "fetching"

    def fail_batch(self, batch_id: str, error: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE sync_batch
                SET status = 'failed', ended_at = CURRENT_TIMESTAMP, error = ?
                WHERE batch_id = ?
                """,
                [error[:4000], batch_id],
            )

    def upsert_security_master(
        self,
        frame: pd.DataFrame,
        batch_id: str,
        *,
        connection: Any = None,
        preserve_listing_state: bool = False,
    ) -> None:
        if frame.empty:
            return
        if connection is None:
            with self.database.connect() as owned_connection:
                self.upsert_security_master(
                    frame, batch_id, connection=owned_connection,
                    preserve_listing_state=preserve_listing_state,
                )
            return
        rows = [
            [
                row.security_id,
                row.symbol,
                row.exchange,
                row.name or row.symbol,
                _db_value(getattr(row, "list_date", None)),
                _db_value(getattr(row, "delist_date", None)),
                getattr(row, "status", "active"),
                _db_value(getattr(row, "industry", None)),
                row.source,
                batch_id,
            ]
            for row in frame.itertuples(index=False)
        ]
        # A current code/name directory cannot prove a change to a known
        # suspension/delisting. Explicit-symbol placeholders must never erase
        # a name, listing state or provenance obtained from the real directory.
        listing_update = (
            "delist_date = security_master.delist_date, status = security_master.status,"
            if preserve_listing_state
            else "delist_date = EXCLUDED.delist_date, status = EXCLUDED.status,"
        )
        incoming = pd.DataFrame(
            rows,
            columns=[
                "security_id", "symbol", "exchange", "name", "list_date", "delist_date",
                "status", "industry", "source", "source_batch_id",
            ],
            dtype=object,
        )
        # A set-based upsert must not silently choose among duplicate source
        # identities; reject the batch before touching any persisted rows.
        for key in (["security_id"], ["symbol", "exchange"]):
            if incoming.duplicated(key).any():
                raise ValueError(f"security master batch contains duplicate {', '.join(key)}")
        # Repeating this upsert per row in a transaction exhausts memory for a
        # full directory. Scan a connection-local frame in
        # one statement instead; the caller still owns its transaction/fencing.
        relation = f"_security_master_incoming_{uuid4().hex}"
        connection.register(relation, incoming)
        try:
            connection.execute(
                f"""
                INSERT INTO security_master (
                    security_id, symbol, exchange, name, list_date, delist_date,
                    status, industry, source, source_batch_id
                )
                SELECT security_id, symbol, exchange, name,
                       CAST(list_date AS DATE), CAST(delist_date AS DATE), status,
                       CAST(industry AS VARCHAR), source, source_batch_id
                FROM {relation}
                ON CONFLICT (security_id) DO UPDATE SET
                    symbol = EXCLUDED.symbol,
                    exchange = EXCLUDED.exchange,
                    name = EXCLUDED.name,
                    list_date = COALESCE(EXCLUDED.list_date, security_master.list_date),
                    {listing_update}
                    industry = COALESCE(EXCLUDED.industry, security_master.industry),
                    source = EXCLUDED.source,
                    source_batch_id = EXCLUDED.source_batch_id,
                    updated_at = now()
                WHERE EXCLUDED.source <> 'requested-symbol-fallback'
                """
            )
        finally:
            connection.unregister(relation)

    def upsert_trading_calendar(self, frame: pd.DataFrame, batch_id: str) -> None:
        if frame.empty:
            return
        rows = [
            [
                row.exchange,
                _db_value(row.trade_date),
                bool(row.is_open),
                _db_value(row.previous_open_date),
                _db_value(row.next_open_date),
                int(row.session_no),
                row.source,
                batch_id,
            ]
            for row in frame.itertuples(index=False)
        ]
        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO trading_calendar (
                    exchange, trade_date, is_open, previous_open_date, next_open_date,
                    session_no, source, source_batch_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def plan_partitions(self, batch_id: str, partitions: list[dict[str, Any]]) -> None:
        if not partitions:
            return
        rows = [
            [
                batch_id,
                item["partition_key"],
                item["security_id"],
                item["adjustment"],
                item["start_date"],
                item["end_date"],
            ]
            for item in partitions
        ]
        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT INTO sync_partition (
                    batch_id, partition_key, security_id, adjustment, start_date, end_date
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (batch_id, partition_key) DO NOTHING
                """,
                rows,
            )

    def pending_partitions(self, batch_id: str) -> list[dict[str, Any]]:
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT partition.partition_key, partition.security_id, security.symbol,
                       partition.adjustment, partition.start_date, partition.end_date,
                       partition.status, partition.attempts
                FROM sync_partition AS partition
                JOIN security_master AS security USING (security_id)
                WHERE partition.batch_id = ? AND partition.status != 'published'
                ORDER BY security.symbol,
                         CASE partition.adjustment
                           WHEN 'none' THEN 0 WHEN 'qfq' THEN 1 ELSE 2 END,
                         partition.start_date
                """,
                [batch_id],
            ).fetchall()
        return [
            {
                "partition_key": row[0],
                "security_id": row[1],
                "symbol": row[2],
                "adjustment": row[3],
                "start_date": row[4],
                "end_date": row[5],
                "status": row[6],
                "attempts": row[7],
            }
            for row in rows
        ]

    def mark_partition_fetching(self, batch_id: str, partition_key: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE sync_partition
                SET status = 'fetching', attempts = attempts + 1, error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE batch_id = ? AND partition_key = ?
                """,
                [batch_id, partition_key],
            )

    def mark_partition_failed(
        self, batch_id: str, partition_key: str, error: str
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE sync_partition
                SET status = 'failed', error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE batch_id = ? AND partition_key = ?
                """,
                [error[:4000], batch_id, partition_key],
            )

    def record_raw_artifact(
        self,
        batch_id: str,
        partition_key: str,
        raw_path: str,
        raw_sha256: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE sync_partition
                SET raw_path = ?, raw_sha256 = ?, updated_at = CURRENT_TIMESTAMP
                WHERE batch_id = ? AND partition_key = ?
                """,
                [raw_path, raw_sha256, batch_id, partition_key],
            )

    def save_quality_checks(
        self,
        batch_id: str,
        object_key: str,
        checks: list[DataQualityCheck],
    ) -> None:
        rows = [
            [
                batch_id,
                check.rule_id,
                object_key,
                check.severity,
                check.passed,
                check.observed,
                check.expected,
                check.details,
            ]
            for check in checks
        ]
        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO dq_result (
                    batch_id, rule_id, object_key, severity, passed,
                    observed, expected, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def publish_partition(
        self,
        *,
        batch_id: str,
        partition: dict[str, Any],
        frame: pd.DataFrame,
    ) -> None:
        security_id = partition["security_id"]
        adjustment = partition["adjustment"]
        start_date = partition["start_date"]
        end_date = partition["end_date"]
        rows = [
            [
                security_id,
                _db_value(row.trade_date),
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                float(row.volume),
                float(row.amount),
                str(row.source),
            ]
            for row in frame.itertuples(index=False)
        ]
        with self.database.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                if adjustment == "none" and rows:
                    connection.executemany(
                        """
                        INSERT OR REPLACE INTO daily_bar_raw (
                            security_id, trade_date, source, batch_id,
                            open, high, low, close, volume, amount
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            [row[0], row[1], row[8], batch_id, *row[2:8]]
                            for row in rows
                        ],
                    )
                if rows:
                    connection.execute(
                        """
                        DELETE FROM daily_bar
                        WHERE security_id = ? AND adjustment = ?
                          AND trade_date BETWEEN ? AND ?
                        """,
                        [security_id, adjustment, start_date, end_date],
                    )
                    connection.executemany(
                        """
                        INSERT INTO daily_bar (
                            security_id, trade_date, adjustment, open, high, low, close,
                            volume, amount, source_batch_id, quality_status, published_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'published', CURRENT_TIMESTAMP)
                        """,
                        [
                            [row[0], row[1], adjustment, *row[2:8], batch_id]
                            for row in rows
                        ],
                    )
                if adjustment != "none" and rows:
                    connection.execute(
                        """
                        DELETE FROM adjustment_ratio
                        WHERE security_id = ? AND adjustment = ?
                          AND trade_date BETWEEN ? AND ?
                        """,
                        [security_id, adjustment, start_date, end_date],
                    )
                    connection.execute(
                        """
                        INSERT INTO adjustment_ratio
                        SELECT adjusted.security_id, adjusted.trade_date, adjusted.adjustment,
                               adjusted.close / raw.close,
                               'derived_close_ratio', adjusted.source_batch_id
                        FROM daily_bar AS adjusted
                        JOIN daily_bar AS raw
                          ON raw.security_id = adjusted.security_id
                         AND raw.trade_date = adjusted.trade_date
                         AND raw.adjustment = 'none'
                        WHERE adjusted.security_id = ?
                          AND adjusted.adjustment = ?
                          AND adjusted.trade_date BETWEEN ? AND ?
                          AND raw.close > 0 AND adjusted.close > 0
                        """,
                        [security_id, adjustment, start_date, end_date],
                    )
                connection.execute(
                    """
                    INSERT INTO data_coverage (
                        dataset, security_id, adjustment, start_date, end_date, source_batch_id
                    ) VALUES ('daily_bar', ?, ?, ?, ?, ?)
                    ON CONFLICT (dataset, security_id, adjustment, start_date, end_date)
                    DO UPDATE SET source_batch_id = EXCLUDED.source_batch_id,
                                  updated_at = now()
                    """,
                    [security_id, adjustment, start_date, end_date, batch_id],
                )
                connection.execute(
                    """
                    UPDATE sync_partition
                    SET status = 'published', row_count = ?, error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE batch_id = ? AND partition_key = ?
                    """,
                    [len(rows), batch_id, partition["partition_key"]],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def complete_batch(self, batch_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            counts = dict(
                connection.execute(
                    """
                    SELECT status, COUNT(*)
                    FROM sync_partition WHERE batch_id = ? GROUP BY status
                    """,
                    [batch_id],
                ).fetchall()
            )
            row_count = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(row_count), 0)
                    FROM sync_partition WHERE batch_id = ? AND status = 'published'
                    """,
                    [batch_id],
                ).fetchone()[0]
            )
            failed = int(counts.get("failed", 0))
            status = "failed" if failed else "published"
            error = f"{failed} partitions failed; rerun with resume enabled" if failed else None
            connection.execute(
                """
                UPDATE sync_batch
                SET status = ?, ended_at = CURRENT_TIMESTAMP, row_count = ?, error = ?
                WHERE batch_id = ?
                """,
                [status, row_count, error, batch_id],
            )
        return {
            "batch_id": batch_id,
            "status": status,
            "row_count": row_count,
            "partitions": {str(key): int(value) for key, value in counts.items()},
        }

    def batch_summary(self, batch_id: str) -> dict[str, Any]:
        with self.database.connect(read_only=True) as connection:
            batch = connection.execute(
                """
                SELECT status, row_count, error FROM sync_batch WHERE batch_id = ?
                """,
                [batch_id],
            ).fetchone()
            counts = dict(
                connection.execute(
                    """
                    SELECT status, COUNT(*) FROM sync_partition
                    WHERE batch_id = ? GROUP BY status
                    """,
                    [batch_id],
                ).fetchall()
            )
        return {
            "batch_id": batch_id,
            "status": str(batch[0]),
            "row_count": int(batch[1]),
            "error": batch[2],
            "partitions": {str(key): int(value) for key, value in counts.items()},
        }

    def calendar_dates(self, start_date: date, end_date: date) -> set[date]:
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT trade_date FROM trading_calendar
                WHERE exchange = 'SSE' AND is_open
                  AND trade_date BETWEEN ? AND ?
                """,
                [start_date, end_date],
            ).fetchall()
        return {row[0] for row in rows}
