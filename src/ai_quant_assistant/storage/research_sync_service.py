from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from ai_quant_assistant.domain.models import AnnouncementSyncSpec, BenchmarkSyncSpec
from ai_quant_assistant.providers.base import ResearchDataProvider
from ai_quant_assistant.providers.normalization import (
    exchange_for_symbol,
    security_id_for_symbol,
)
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.quality import DataQualityCheck, has_blocking_failure
from ai_quant_assistant.storage.sync_repository import SyncRepository, _db_value

logger = logging.getLogger(__name__)

BENCHMARK_NAMES = {
    "csi000300": "沪深300",
    "csi000905": "中证500",
    "sh000001": "上证指数",
    "sz399001": "深证成指",
}


class ResearchDatasetSyncService:
    def __init__(
        self,
        *,
        database: LocalDatabase,
        layout: DataLayout,
        provider: ResearchDataProvider,
        rate_limit_seconds: float = 0.2,
    ) -> None:
        if rate_limit_seconds < 0:
            raise ValueError("rate_limit_seconds must be non-negative")
        self.database = database
        self.layout = layout
        self.provider = provider
        self.rate_limit_seconds = rate_limit_seconds
        self.repository = SyncRepository(database)

    def sync_benchmarks(self, spec: BenchmarkSyncSpec) -> dict[str, Any]:
        return self._run("benchmark_daily_bar", spec, ("none",))

    def sync_announcements(self, spec: AnnouncementSyncSpec) -> dict[str, Any]:
        return self._run("announcement", spec, spec.report_types)

    @staticmethod
    def _partition_ranges(start_date: date, end_date: date) -> list[tuple[date, date]]:
        return [
            (
                max(start_date, date(year, 1, 1)),
                min(end_date, date(year, 12, 31)),
            )
            for year in range(start_date.year, end_date.year + 1)
        ]

    def _begin_batch(
        self,
        dataset: str,
        request: dict[str, Any],
        *,
        resume: bool,
    ) -> tuple[str, bool, str]:
        request_json = self.repository.canonical_request(request)
        with self.database.connect() as connection:
            if resume:
                row = connection.execute(
                    """
                    SELECT batch_id, status
                    FROM sync_batch
                    WHERE dataset = ? AND request_json = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    [dataset, request_json],
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
            prefix = dataset.replace("_", "-")
            batch_id = f"{prefix}-{timestamp}-{uuid4().hex[:8]}"
            connection.execute(
                """
                INSERT INTO sync_batch (
                    batch_id, dataset, status, started_at, request_json
                ) VALUES (?, ?, 'fetching', CURRENT_TIMESTAMP, ?)
                """,
                [batch_id, dataset, request_json],
            )
            return batch_id, False, "fetching"

    def _ensure_objects(self, dataset: str, symbols: tuple[str, ...], batch_id: str) -> None:
        if dataset == "benchmark_daily_bar":
            rows = [
                [
                    f"INDEX.{symbol}",
                    symbol,
                    BENCHMARK_NAMES.get(symbol, symbol),
                    "requested-benchmark",
                    batch_id,
                ]
                for symbol in symbols
            ]
            with self.database.connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO benchmark_master (
                        benchmark_id, symbol, name, source, source_batch_id
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (benchmark_id) DO UPDATE SET
                        symbol = EXCLUDED.symbol,
                        name = EXCLUDED.name,
                        source = EXCLUDED.source,
                        source_batch_id = EXCLUDED.source_batch_id,
                        updated_at = now()
                    """,
                    rows,
                )
            return

        with self.database.connect(read_only=True) as connection:
            present = {
                str(row[0])
                for row in connection.execute("SELECT symbol FROM security_master").fetchall()
            }
        missing = [
            {
                "security_id": security_id_for_symbol(symbol),
                "symbol": symbol,
                "exchange": exchange_for_symbol(symbol),
                "name": symbol,
                "source": "requested-symbol-fallback",
            }
            for symbol in symbols
            if symbol not in present
        ]
        if missing:
            self.repository.upsert_security_master(pd.DataFrame(missing), batch_id)

    def _plan_partitions(
        self,
        batch_id: str,
        dataset: str,
        symbols: tuple[str, ...],
        variants: tuple[str, ...],
        start_date: date,
        end_date: date,
    ) -> None:
        rows = []
        for symbol in symbols:
            object_id = (
                f"INDEX.{symbol}"
                if dataset == "benchmark_daily_bar"
                else security_id_for_symbol(symbol)
            )
            for variant in variants:
                for range_start, range_end in self._partition_ranges(start_date, end_date):
                    key = f"{dataset}:{object_id}:{variant}:{range_start}:{range_end}"
                    rows.append(
                        [
                            batch_id,
                            dataset,
                            key,
                            object_id,
                            variant,
                            range_start,
                            range_end,
                        ]
                    )
        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT INTO research_sync_partition (
                    batch_id, dataset, partition_key, object_id, variant,
                    start_date, end_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (batch_id, partition_key) DO NOTHING
                """,
                rows,
            )

    def _pending_partitions(self, batch_id: str) -> list[dict[str, Any]]:
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT partition_key, dataset, object_id, variant, start_date, end_date,
                       status, attempts
                FROM research_sync_partition
                WHERE batch_id = ? AND status != 'published'
                ORDER BY object_id, variant, start_date
                """,
                [batch_id],
            ).fetchall()
        return [
            {
                "partition_key": row[0],
                "dataset": row[1],
                "object_id": row[2],
                "symbol": str(row[2]).split(".", maxsplit=1)[1],
                "variant": row[3],
                "start_date": row[4],
                "end_date": row[5],
                "status": row[6],
                "attempts": row[7],
            }
            for row in rows
        ]

    def _mark_partition(
        self,
        batch_id: str,
        partition_key: str,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE research_sync_partition
                SET status = ?, error = ?,
                    attempts = attempts + CASE WHEN ? = 'fetching' THEN 1 ELSE 0 END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE batch_id = ? AND partition_key = ?
                """,
                [status, error[:4000] if error else None, status, batch_id, partition_key],
            )

    def _write_raw_partition(
        self, partition: dict[str, Any], frame: pd.DataFrame
    ) -> tuple[Path, str]:
        target_dir = (
            self.layout.raw / partition["dataset"] / partition["symbol"] / partition["variant"]
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{partition['start_date']}_{partition['end_date']}.parquet"
        frame.to_parquet(target, index=False)
        return target, hashlib.sha256(target.read_bytes()).hexdigest()

    @staticmethod
    def _benchmark_checks(frame: pd.DataFrame, partition: dict[str, Any]) -> list[DataQualityCheck]:
        if frame.empty:
            return [
                DataQualityCheck("rows_present", "warning", False, "0 rows", "one or more rows")
            ]
        dates = pd.to_datetime(frame["date"], errors="coerce")
        close = pd.to_numeric(frame["close"], errors="coerce")
        invalid_dates = int(dates.isna().sum())
        duplicates = int(dates.duplicated(keep=False).sum())
        invalid_range = int(
            ((dates.dt.date < partition["start_date"]) | (dates.dt.date > partition["end_date"]))
            .fillna(True)
            .sum()
        )
        invalid_close = int((close.isna() | (close <= 0)).sum())
        return [
            DataQualityCheck(
                "trade_date_valid", "blocking", invalid_dates == 0, str(invalid_dates), "0"
            ),
            DataQualityCheck(
                "primary_key_unique", "blocking", duplicates == 0, str(duplicates), "0"
            ),
            DataQualityCheck(
                "date_in_partition", "blocking", invalid_range == 0, str(invalid_range), "0"
            ),
            DataQualityCheck(
                "positive_close", "blocking", invalid_close == 0, str(invalid_close), "0"
            ),
        ]

    @staticmethod
    def _announcement_checks(
        frame: pd.DataFrame, partition: dict[str, Any]
    ) -> list[DataQualityCheck]:
        if frame.empty:
            return [
                DataQualityCheck(
                    "rows_present",
                    "warning",
                    False,
                    "0 announcements",
                    "zero or more announcements in the requested period",
                )
            ]
        announced = pd.to_datetime(frame["announcement_date"], errors="coerce")
        periods = pd.to_datetime(frame["report_period_end"], errors="coerce")
        published = pd.to_datetime(frame["published_at"], errors="coerce")
        invalid_dates = int((announced.isna() | periods.isna() | published.isna()).sum())
        invalid_range = int(
            (
                (announced.dt.date < partition["start_date"])
                | (announced.dt.date > partition["end_date"])
            )
            .fillna(True)
            .sum()
        )
        future_periods = int((periods > announced).fillna(True).sum())
        invalid_publication_times = int((published < announced).fillna(True).sum())
        invalid_types = int((frame["report_type"].astype(str) != partition["variant"]).sum())
        duplicates = int(
            frame.duplicated(
                ["symbol", "report_type", "report_year", "title", "announcement_date"]
            ).sum()
        )
        return [
            DataQualityCheck(
                "announcement_dates_valid", "blocking", invalid_dates == 0, str(invalid_dates), "0"
            ),
            DataQualityCheck(
                "announcement_in_partition", "blocking", invalid_range == 0, str(invalid_range), "0"
            ),
            DataQualityCheck(
                "report_period_available", "blocking", future_periods == 0, str(future_periods), "0"
            ),
            DataQualityCheck(
                "publication_time_available",
                "blocking",
                invalid_publication_times == 0,
                str(invalid_publication_times),
                "0",
            ),
            DataQualityCheck(
                "report_type_matches", "blocking", invalid_types == 0, str(invalid_types), "0"
            ),
            DataQualityCheck(
                "announcement_unique", "blocking", duplicates == 0, str(duplicates), "0"
            ),
        ]

    @staticmethod
    def _announcement_identity(security_id: str, row: Any) -> tuple[str, str]:
        payload = json.dumps(
            {
                "security_id": security_id,
                "report_type": str(row.report_type),
                "report_year": int(row.report_year),
                "report_period_end": str(pd.Timestamp(row.report_period_end).date()),
                "announcement_date": str(pd.Timestamp(row.announcement_date).date()),
                "title": str(row.title),
                "url": str(row.url),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"announcement-{digest[:24]}", digest

    def _publish_partition(
        self,
        batch_id: str,
        partition: dict[str, Any],
        frame: pd.DataFrame,
        raw_path: Path,
        raw_sha256: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                if partition["dataset"] == "benchmark_daily_bar" and not frame.empty:
                    rows = [
                        [
                            partition["object_id"],
                            _db_value(row.date),
                            float(row.close),
                            str(row.source),
                            batch_id,
                        ]
                        for row in frame.itertuples(index=False)
                    ]
                    connection.executemany(
                        """
                        INSERT INTO benchmark_daily_bar (
                            benchmark_id, trade_date, close, source, source_batch_id
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT (benchmark_id, trade_date) DO UPDATE SET
                            close = EXCLUDED.close,
                            source = EXCLUDED.source,
                            source_batch_id = EXCLUDED.source_batch_id,
                            quality_status = 'published',
                            published_at = now()
                        """,
                        rows,
                    )
                elif partition["dataset"] == "announcement" and not frame.empty:
                    rows = []
                    for item in frame.itertuples(index=False):
                        announcement_id, content_sha = self._announcement_identity(
                            partition["object_id"], item
                        )
                        rows.append(
                            [
                                announcement_id,
                                partition["object_id"],
                                str(item.company_name),
                                str(item.title),
                                str(item.report_type),
                                int(item.report_year),
                                _db_value(item.report_period_end),
                                _db_value(item.announcement_date),
                                _db_value(item.published_at),
                                str(item.url),
                                str(item.source),
                                batch_id,
                                content_sha,
                            ]
                        )
                    connection.executemany(
                        """
                        INSERT INTO announcement (
                            announcement_id, security_id, company_name, title, report_type,
                            report_year, report_period_end, announcement_date, published_at,
                            url, source, source_batch_id, content_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (announcement_id) DO UPDATE SET
                            company_name = EXCLUDED.company_name,
                            url = EXCLUDED.url,
                            source = EXCLUDED.source,
                            source_batch_id = EXCLUDED.source_batch_id,
                            content_sha256 = EXCLUDED.content_sha256,
                            quality_status = 'published'
                        """,
                        rows,
                    )

                if not frame.empty or partition["dataset"] == "announcement":
                    connection.execute(
                        """
                        INSERT INTO data_coverage (
                            dataset, security_id, adjustment, start_date, end_date,
                            source_batch_id
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT (dataset, security_id, adjustment, start_date, end_date)
                        DO UPDATE SET source_batch_id = EXCLUDED.source_batch_id,
                                      updated_at = now()
                        """,
                        [
                            partition["dataset"],
                            partition["object_id"],
                            partition["variant"],
                            partition["start_date"],
                            partition["end_date"],
                            batch_id,
                        ],
                    )
                connection.execute(
                    """
                    UPDATE research_sync_partition
                    SET status = 'published', row_count = ?, raw_path = ?, raw_sha256 = ?,
                        error = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE batch_id = ? AND partition_key = ?
                    """,
                    [len(frame), str(raw_path), raw_sha256, batch_id, partition["partition_key"]],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _batch_summary(self, batch_id: str) -> dict[str, Any]:
        with self.database.connect(read_only=True) as connection:
            batch = connection.execute(
                "SELECT dataset, status, row_count, error FROM sync_batch WHERE batch_id = ?",
                [batch_id],
            ).fetchone()
            partitions = dict(
                connection.execute(
                    """
                    SELECT status, COUNT(*) FROM research_sync_partition
                    WHERE batch_id = ? GROUP BY status
                    """,
                    [batch_id],
                ).fetchall()
            )
        return {
            "batch_id": batch_id,
            "dataset": str(batch[0]),
            "status": str(batch[1]),
            "row_count": int(batch[2]),
            "error": batch[3],
            "partitions": {str(key): int(value) for key, value in partitions.items()},
        }

    def _complete_batch(self, batch_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            partitions = dict(
                connection.execute(
                    """
                    SELECT status, COUNT(*) FROM research_sync_partition
                    WHERE batch_id = ? GROUP BY status
                    """,
                    [batch_id],
                ).fetchall()
            )
            row_count = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(row_count), 0) FROM research_sync_partition
                    WHERE batch_id = ? AND status = 'published'
                    """,
                    [batch_id],
                ).fetchone()[0]
            )
            failed = int(partitions.get("failed", 0))
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
        return self._batch_summary(batch_id)

    def _run(
        self,
        dataset: str,
        spec: BenchmarkSyncSpec | AnnouncementSyncSpec,
        variants: tuple[str, ...],
    ) -> dict[str, Any]:
        self.database.initialize()
        batch_id, resumed, previous_status = self._begin_batch(
            dataset, spec.request_dict(), resume=spec.resume
        )
        if resumed and previous_status == "published":
            return {
                **self._batch_summary(batch_id),
                "resumed": True,
                "provider": self.provider.name,
                "errors": [],
                "database_status": self.database.status(),
            }

        try:
            self._ensure_objects(dataset, spec.symbols, batch_id)
            self._plan_partitions(
                batch_id,
                dataset,
                spec.symbols,
                variants,
                spec.start_date,
                spec.end_date,
            )
        except Exception as exc:
            self.repository.fail_batch(batch_id, str(exc))
            raise

        pending = self._pending_partitions(batch_id)
        errors = []
        logger.info("sync %s batch %s: %s pending partitions", dataset, batch_id, len(pending))
        for index, partition in enumerate(pending, start=1):
            logger.info(
                "sync %s partition %s/%s: %s %s %s..%s",
                dataset,
                index,
                len(pending),
                partition["symbol"],
                partition["variant"],
                partition["start_date"],
                partition["end_date"],
            )
            self._mark_partition(batch_id, partition["partition_key"], "fetching")
            try:
                if dataset == "benchmark_daily_bar":
                    frame = self.provider.get_benchmark_prices(
                        partition["symbol"], partition["start_date"], partition["end_date"]
                    )
                    checks = self._benchmark_checks(frame, partition)
                else:
                    frame = self.provider.get_report_announcements(
                        partition["symbol"],
                        partition["start_date"],
                        partition["end_date"],
                        partition["variant"],
                    )
                    checks = self._announcement_checks(frame, partition)
                raw_path, raw_sha256 = self._write_raw_partition(partition, frame)
                self.repository.save_quality_checks(
                    batch_id, f"{dataset}:{partition['partition_key']}", checks
                )
                if has_blocking_failure(checks):
                    failed_rules = ", ".join(
                        item.rule_id
                        for item in checks
                        if item.severity == "blocking" and not item.passed
                    )
                    raise ValueError(f"blocking data-quality failure: {failed_rules}")
                self._publish_partition(batch_id, partition, frame, raw_path, raw_sha256)
            except Exception as exc:  # noqa: BLE001 - each partition is independently resumable
                logger.warning("research partition %s failed: %s", partition["partition_key"], exc)
                self._mark_partition(batch_id, partition["partition_key"], "failed", error=str(exc))
                errors.append({"partition_key": partition["partition_key"], "error": str(exc)})
            if self.rate_limit_seconds:
                time.sleep(self.rate_limit_seconds)

        return {
            **self._complete_batch(batch_id),
            "resumed": resumed,
            "provider": self.provider.name,
            "objects": len(spec.symbols),
            "variants": list(variants),
            "errors": errors[:20],
            "database_status": self.database.status(),
        }
