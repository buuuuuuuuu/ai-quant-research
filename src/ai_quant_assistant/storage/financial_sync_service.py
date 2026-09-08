from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from ai_quant_assistant.domain.models import FinancialSyncSpec
from ai_quant_assistant.providers.base import FinancialDataProvider
from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS, security_id_for_symbol
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.locks import serialized_download
from ai_quant_assistant.storage.quality import DataQualityCheck, has_blocking_failure
from ai_quant_assistant.storage.research_sync_service import ResearchDatasetSyncService
from ai_quant_assistant.storage.sync_repository import _db_value

logger = logging.getLogger(__name__)


class FinancialDataSyncService(ResearchDatasetSyncService):
    def __init__(
        self,
        *,
        database: LocalDatabase,
        layout: DataLayout,
        provider: FinancialDataProvider,
        rate_limit_seconds: float = 0.2,
        raw_namespace: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        require_provider_publication: bool = False,
    ) -> None:
        self.require_provider_publication = require_provider_publication
        super().__init__(
            database=database,
            layout=layout,
            provider=provider,
            rate_limit_seconds=rate_limit_seconds,
            raw_namespace=raw_namespace,
            progress_callback=progress_callback,
        )

    def _plan_financial_partitions(self, batch_id: str, spec: FinancialSyncSpec) -> None:
        rows = [
            [
                batch_id,
                f"financial_metric:{security_id_for_symbol(symbol)}:{start}:{end}",
                security_id_for_symbol(symbol),
                start,
                end,
            ]
            for symbol in spec.symbols
            for start, end in self._partition_ranges(spec.start_date, spec.end_date)
        ]
        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT INTO financial_sync_partition (
                    batch_id, partition_key, security_id, start_date, end_date
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (batch_id, partition_key) DO NOTHING
                """,
                rows,
            )

    def _pending_financial_partitions(self, batch_id: str) -> list[dict[str, Any]]:
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT partition_key, security_id, start_date, end_date
                FROM financial_sync_partition
                WHERE batch_id = ? AND status != 'published'
                ORDER BY security_id, start_date
                """,
                [batch_id],
            ).fetchall()
        return [
            {
                "partition_key": row[0],
                "security_id": row[1],
                "object_id": row[1],
                "symbol": str(row[1]).split(".", maxsplit=1)[1],
                "start_date": row[2],
                "end_date": row[3],
                "dataset": "financial_metric",
                "variant": "all",
            }
            for row in rows
        ]

    def _mark_financial_partition(
        self,
        batch_id: str,
        partition_key: str,
        status: str,
        *,
        error: str | None = None,
        raw_path: str | None = None,
        raw_sha256: str | None = None,
        count_attempt: bool = True,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE financial_sync_partition
                SET status = ?, error = ?,
                    attempts = attempts + CASE WHEN ? AND ? = 'fetching' THEN 1 ELSE 0 END,
                    raw_path = COALESCE(?, raw_path),
                    raw_sha256 = COALESCE(?, raw_sha256),
                    updated_at = CURRENT_TIMESTAMP
                WHERE batch_id = ? AND partition_key = ?
                """,
                [
                    status,
                    error[:4000] if error else None,
                    count_attempt,
                    status,
                    raw_path,
                    raw_sha256,
                    batch_id,
                    partition_key,
                ],
            )

    def _bind_availability(self, partition: dict[str, Any], frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            result = frame.copy()
            result["availability_source"] = pd.Series(dtype="object")
            return result
        result = frame.copy()
        result["published_at"] = pd.to_datetime(result["published_at"], errors="coerce")
        if self.require_provider_publication:
            # Date-only upstream notices do not prove pre-open availability.
            # Conservatively defer midnight timestamps to the end of that day.
            date_only = result["published_at"].notna() & result["published_at"].eq(
                result["published_at"].dt.normalize()
            )
            result.loc[date_only, "published_at"] = result.loc[date_only, "published_at"].map(
                lambda stamp: stamp.replace(hour=23, minute=59, second=59, microsecond=999999)
            )
        result["availability_source"] = result["published_at"].map(
            lambda value: "provider_notice" if pd.notna(value) else ""
        )
        missing = result["published_at"].isna()
        if not missing.any():
            return result
        if self.require_provider_publication:
            # A current financial value plus an old announcement does not prove
            # that this version of the value was available at that old timestamp.
            result.loc[missing, "availability_source"] = "unverified_financial_version"
            return result

        with self.database.connect(read_only=True) as connection:
            announcements = connection.execute(
                """
                SELECT report_period_end, report_type, MIN(published_at) AS published_at
                FROM published_announcement
                WHERE security_id = ?
                  AND report_period_end BETWEEN ? AND ?
                GROUP BY report_period_end, report_type
                HAVING COUNT(*) = 1
                """,
                [partition["security_id"], partition["start_date"], partition["end_date"]],
            ).fetchdf()
        if announcements.empty:
            return result
        announcements["report_period_end"] = pd.to_datetime(
            announcements["report_period_end"]
        ).dt.normalize()
        availability = {
            (pd.Timestamp(row.report_period_end), str(row.report_type)): pd.Timestamp(
                row.published_at
            )
            for row in announcements.itertuples(index=False)
        }
        for index in result.index[missing]:
            key = (
                pd.Timestamp(result.at[index, "report_period_end"]).normalize(),
                str(result.at[index, "report_type"]),
            )
            if key in availability:
                result.at[index, "published_at"] = availability[key]
                result.at[index, "availability_source"] = "matched_announcement"
        return result

    @staticmethod
    def _financial_checks(frame: pd.DataFrame, partition: dict[str, Any]) -> list[DataQualityCheck]:
        if frame.empty:
            return [
                DataQualityCheck(
                    "financial_rows_present",
                    "warning",
                    False,
                    "0 reports",
                    "zero or more reports for the requested year",
                )
            ]
        periods = pd.to_datetime(frame["report_period_end"], errors="coerce")
        published = pd.to_datetime(frame["published_at"], errors="coerce")
        missing_availability = int(published.isna().sum())
        premature = int((published < periods).fillna(False).sum())
        invalid_range = int(
            (
                (periods.dt.date < partition["start_date"])
                | (periods.dt.date > partition["end_date"])
            )
            .fillna(True)
            .sum()
        )
        duplicates = int(frame.duplicated(["report_period_end", "published_at"]).sum())
        metric_columns = [item for item in FINANCIAL_METRICS if item in frame.columns]
        no_metrics = int(frame[metric_columns].notna().sum(axis=1).eq(0).sum())
        return [
            DataQualityCheck(
                "financial_period_valid",
                "blocking",
                bool(periods.notna().all()),
                str(periods.isna().sum()),
                "0",
            ),
            DataQualityCheck(
                "financial_period_in_partition",
                "blocking",
                invalid_range == 0,
                str(invalid_range),
                "0",
            ),
            DataQualityCheck(
                "financial_publication_known",
                "blocking",
                missing_availability == 0,
                str(missing_availability),
                "0",
                "verified financial-version publication time required; current values cannot be backdated from announcement metadata alone",
            ),
            DataQualityCheck(
                "financial_publication_after_period",
                "blocking",
                premature == 0,
                str(premature),
                "0",
            ),
            DataQualityCheck(
                "financial_version_unique", "blocking", duplicates == 0, str(duplicates), "0"
            ),
            DataQualityCheck(
                "financial_metrics_present", "blocking", no_metrics == 0, str(no_metrics), "0"
            ),
        ]

    @staticmethod
    def _report_id(security_id: str, period_end: Any, published_at: Any) -> str:
        payload = (
            f"{security_id}:{pd.Timestamp(period_end).date().isoformat()}:"
            f"{pd.Timestamp(published_at).isoformat()}"
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"financial-report-{digest[:24]}"

    def _publish_financial_partition(
        self,
        batch_id: str,
        partition: dict[str, Any],
        frame: pd.DataFrame,
        raw_path: Path,
        raw_sha256: str,
    ) -> int:
        report_rows: list[list[Any]] = []
        metric_rows: list[list[Any]] = []
        for row in frame.itertuples(index=False):
            report_id = self._report_id(
                partition["security_id"], row.report_period_end, row.published_at
            )
            report_rows.append(
                [
                    report_id,
                    partition["security_id"],
                    _db_value(row.report_period_end),
                    str(row.report_type),
                    _db_value(row.published_at),
                    str(row.availability_source),
                    str(row.source),
                    batch_id,
                    raw_sha256,
                ]
            )
            for code, definition in FINANCIAL_METRICS.items():
                value = getattr(row, code, float("nan"))
                if pd.isna(value):
                    continue
                metric_rows.append(
                    [
                        report_id,
                        partition["security_id"],
                        _db_value(row.report_period_end),
                        _db_value(row.published_at),
                        code,
                        float(value),
                        str(definition["unit"]),
                        batch_id,
                    ]
                )

        with self.database.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                # A refresh is not a new publication. Never rewrite the facts or
                # their original lineage under an already published identity.
                for report in report_rows:
                    old = connection.execute(
                        "SELECT report_type FROM financial_report WHERE report_id=?",
                        [report[0]],
                    ).fetchone()
                    if old is None:
                        continue
                    previous = dict(connection.execute(
                        "SELECT metric_code, metric_value FROM financial_metric WHERE report_id=?",
                        [report[0]],
                    ).fetchall())
                    incoming = {row[4]: row[5] for row in metric_rows if row[0] == report[0]}
                    if old[0] != report[3] or previous != incoming:
                        raise ValueError(
                            f"financial version conflict: {report[0]}; changed facts require "
                            "a separately verified publication time; historical version retained"
                        )
                if report_rows:
                    connection.executemany(
                        """
                        INSERT INTO financial_report (
                            report_id, security_id, report_period_end, report_type,
                            published_at, availability_source, source,
                            source_batch_id, raw_sha256, observed_at, first_visible_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, timezone('UTC', now()), now())
                        ON CONFLICT (report_id) DO NOTHING
                        """,
                        report_rows,
                    )
                if metric_rows:
                    connection.executemany(
                        """
                        INSERT INTO financial_metric (
                            report_id, security_id, report_period_end, published_at,
                            metric_code, metric_value, unit, source_batch_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (report_id, metric_code) DO NOTHING
                        """,
                        metric_rows,
                    )
                connection.execute(
                    """
                    INSERT INTO data_coverage (
                        dataset, security_id, adjustment, start_date, end_date,
                        source_batch_id
                    ) VALUES ('financial_metric', ?, 'all', ?, ?, ?)
                    ON CONFLICT (dataset, security_id, adjustment, start_date, end_date)
                    DO UPDATE SET source_batch_id = EXCLUDED.source_batch_id,
                                  updated_at = now()
                    """,
                    [
                        partition["security_id"],
                        partition["start_date"],
                        partition["end_date"],
                        batch_id,
                    ],
                )
                connection.execute(
                    """
                    UPDATE financial_sync_partition
                    SET status = 'published', row_count = ?, metric_count = ?,
                        raw_path = ?, raw_sha256 = ?, error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE batch_id = ? AND partition_key = ?
                    """,
                    [
                        len(report_rows),
                        len(metric_rows),
                        str(raw_path),
                        raw_sha256,
                        batch_id,
                        partition["partition_key"],
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return len(metric_rows)

    def _financial_batch_summary(self, batch_id: str) -> dict[str, Any]:
        with self.database.connect(read_only=True) as connection:
            batch = connection.execute(
                "SELECT status, row_count, error FROM sync_batch WHERE batch_id = ?",
                [batch_id],
            ).fetchone()
            partitions = dict(
                connection.execute(
                    """
                    SELECT status, COUNT(*) FROM financial_sync_partition
                    WHERE batch_id = ? GROUP BY status
                    """,
                    [batch_id],
                ).fetchall()
            )
            metric_count = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(metric_count), 0)
                    FROM financial_sync_partition
                    WHERE batch_id = ? AND status = 'published'
                    """,
                    [batch_id],
                ).fetchone()[0]
            )
        return {
            "batch_id": batch_id,
            "dataset": "financial_metric",
            "status": str(batch[0]),
            "row_count": int(batch[1]),
            "metric_count": metric_count,
            "error": batch[2],
            "partitions": {str(key): int(value) for key, value in partitions.items()},
        }

    def _complete_financial_batch(self, batch_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            stats = connection.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN status = 'published' THEN row_count ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0)
                FROM financial_sync_partition
                WHERE batch_id = ?
                """,
                [batch_id],
            ).fetchone()
            row_count, failed = int(stats[0]), int(stats[1])
            status = "failed" if failed else "published"
            error = f"{failed} partitions failed; rerun with resume enabled" if failed else None
            connection.execute(
                """
                UPDATE sync_batch
                SET status = ?, row_count = ?, error = ?, ended_at = CURRENT_TIMESTAMP
                WHERE batch_id = ?
                """,
                [status, row_count, error, batch_id],
            )
        return self._financial_batch_summary(batch_id)

    @serialized_download("financial_metric")
    def run(self, spec: FinancialSyncSpec) -> dict[str, Any]:
        self.database.initialize()
        request = spec.request_dict()
        if self.require_provider_publication:
            request = {**request, "publication_policy": "provider-version-required"}
        batch_id, resumed, previous_status = self._begin_batch(
            "financial_metric", request, resume=spec.resume
        )
        if resumed and previous_status == "published":
            return {
                **self._financial_batch_summary(batch_id),
                "resumed": True,
                "provider": self.provider.name,
                "errors": [],
                "database_status": self.database.status(),
            }

        try:
            self._ensure_objects("financial_metric", spec.symbols, batch_id)
            self._plan_financial_partitions(batch_id, spec)
        except Exception as exc:
            self.repository.fail_batch(batch_id, str(exc))
            raise

        cached_frames: dict[str, pd.DataFrame] = {}
        errors: list[dict[str, str]] = []
        pending = self._pending_financial_partitions(batch_id)
        # Even a retry of one batch gets a new immutable raw evidence namespace.
        self.raw_namespace = f"{batch_id}-{uuid4().hex}"
        logger.info("sync financial batch %s: %s pending partitions", batch_id, len(pending))
        for index, partition in enumerate(pending, start=1):
            logger.info(
                "sync financial partition %s/%s: %s %s..%s",
                index,
                len(pending),
                partition["symbol"],
                partition["start_date"],
                partition["end_date"],
            )
            self._mark_financial_partition(batch_id, partition["partition_key"], "fetching")
            try:
                symbol = partition["symbol"]
                if symbol not in cached_frames:
                    cached_frames[symbol] = self.provider.get_financial_indicators(
                        symbol, spec.start_date, spec.end_date
                    )
                all_reports = cached_frames[symbol]
                if all_reports.attrs.get("cache_status") == "stale_fallback":
                    raise ValueError("financial refresh failed: stale cache is not fresh evidence")
                periods = pd.to_datetime(all_reports["report_period_end"], errors="coerce").dt.date
                selected = all_reports.loc[
                    (periods >= partition["start_date"]) & (periods <= partition["end_date"])
                ].copy()
                frame = self._bind_availability(partition, selected)
                raw_path, raw_sha256 = self._write_raw_partition(partition, frame)
                self._mark_financial_partition(
                    batch_id,
                    partition["partition_key"],
                    "fetching",
                    raw_path=str(raw_path),
                    raw_sha256=raw_sha256,
                    count_attempt=False,
                )
                checks = self._financial_checks(frame, partition)
                self.repository.save_quality_checks(
                    batch_id, f"financial_metric:{partition['partition_key']}", checks
                )
                if has_blocking_failure(checks):
                    failures = ", ".join(
                        item.rule_id
                        for item in checks
                        if item.severity == "blocking" and not item.passed
                    )
                    raise ValueError(f"blocking data-quality failure: {failures}")
                self._publish_financial_partition(batch_id, partition, frame, raw_path, raw_sha256)
            except Exception as exc:  # noqa: BLE001 - independently resumable partitions
                logger.warning("financial partition %s failed: %s", partition["partition_key"], exc)
                self._mark_financial_partition(
                    batch_id, partition["partition_key"], "failed", error=str(exc)
                )
                errors.append({"partition_key": partition["partition_key"], "error": str(exc)})
            if self.progress_callback is not None:
                self.progress_callback(index, len(pending))
            if self.rate_limit_seconds:
                time.sleep(self.rate_limit_seconds)

        return {
            **self._complete_financial_batch(batch_id),
            "resumed": resumed,
            "provider": self.provider.name,
            "securities": len(spec.symbols),
            "metrics": list(FINANCIAL_METRICS),
            "errors": errors[:20],
            "database_status": self.database.status(),
        }
