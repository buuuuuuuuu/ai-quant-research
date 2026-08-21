from __future__ import annotations

import hashlib
import logging
import time
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from ai_quant_assistant.domain.models import DataSyncSpec
from ai_quant_assistant.providers.base import SyncDataProvider
from ai_quant_assistant.providers.normalization import (
    exchange_for_symbol,
    security_id_for_symbol,
)
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.quality import evaluate_daily_bars, has_blocking_failure
from ai_quant_assistant.storage.sync_repository import SyncRepository

logger = logging.getLogger(__name__)


class MarketDataSyncService:
    def __init__(
        self,
        *,
        database: LocalDatabase,
        layout: DataLayout,
        provider: SyncDataProvider,
        rate_limit_seconds: float = 0.2,
    ) -> None:
        if rate_limit_seconds < 0:
            raise ValueError("rate_limit_seconds must be non-negative")
        self.database = database
        self.layout = layout
        self.provider = provider
        self.rate_limit_seconds = rate_limit_seconds
        self.repository = SyncRepository(database)

    @staticmethod
    def _partition_ranges(start_date: date, end_date: date) -> list[tuple[date, date]]:
        ranges = []
        for year in range(start_date.year, end_date.year + 1):
            ranges.append(
                (
                    max(start_date, date(year, 1, 1)),
                    min(end_date, date(year, 12, 31)),
                )
            )
        return ranges

    @staticmethod
    def _missing_security_rows(symbols: tuple[str, ...], master: pd.DataFrame) -> pd.DataFrame:
        present = set(master["symbol"].astype(str)) if not master.empty else set()
        rows = [
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
        return pd.DataFrame(
            rows,
            columns=["security_id", "symbol", "exchange", "name", "source"],
        )

    def _write_raw_partition(
        self, partition: dict[str, Any], frame: pd.DataFrame
    ) -> tuple[Path, str]:
        target_dir = (
            self.layout.raw
            / "daily_bar"
            / partition["symbol"]
            / partition["adjustment"]
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{partition['start_date']}_{partition['end_date']}.parquet"
        target = target_dir / filename
        frame.to_parquet(target, index=False)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return target, digest

    def run(self, spec: DataSyncSpec) -> dict[str, Any]:
        self.database.initialize()
        batch_id, resumed, previous_status = self.repository.begin_batch(
            spec.request_dict(), resume=spec.resume
        )
        if resumed and previous_status == "published":
            return {
                **self.repository.batch_summary(batch_id),
                "resumed": True,
                "provider": self.provider.name,
                "adjustments": list(spec.adjustments),
                "errors": [],
                "database_status": self.database.status(),
            }

        try:
            if spec.symbols:
                # An explicit-symbol sync only needs stable IDs and exchange mapping.
                # Avoid making it depend on AkShare's composite full-market listing,
                # where an outage in one exchange can otherwise block every symbol.
                master = self._missing_security_rows(spec.symbols, pd.DataFrame())
            else:
                master = self.provider.get_security_master()
            calendar = self.provider.get_trading_calendar()
            if master.empty:
                raise ValueError("security master returned no rows")
            if calendar.empty:
                raise ValueError("trading calendar returned no rows")
            master = master.sort_values("symbol").drop_duplicates("security_id")
            if spec.symbol_limit is not None:
                master = master.head(spec.symbol_limit)
            if master.empty:
                raise ValueError("no securities matched the sync request")

            self.repository.upsert_security_master(master, batch_id)
            self.repository.upsert_trading_calendar(calendar, batch_id)
        except Exception as exc:
            self.repository.fail_batch(batch_id, str(exc))
            raise

        partitions = [
            {
                "partition_key": (
                    f"{security.security_id}:{adjustment}:{range_start}:{range_end}"
                ),
                "security_id": security.security_id,
                "adjustment": adjustment,
                "start_date": range_start,
                "end_date": range_end,
            }
            for security in master.itertuples(index=False)
            for adjustment in spec.adjustments
            for range_start, range_end in self._partition_ranges(spec.start_date, spec.end_date)
        ]
        self.repository.plan_partitions(batch_id, partitions)
        calendar_dates = self.repository.calendar_dates(spec.start_date, spec.end_date)
        errors: list[dict[str, str]] = []
        pending = self.repository.pending_partitions(batch_id)
        logger.info(
            "sync batch %s: %s securities, %s pending partitions",
            batch_id,
            len(master),
            len(pending),
        )

        for index, partition in enumerate(pending, start=1):
            logger.info(
                "sync partition %s/%s: %s %s %s..%s",
                index,
                len(pending),
                partition["symbol"],
                partition["adjustment"],
                partition["start_date"],
                partition["end_date"],
            )
            self.repository.mark_partition_fetching(batch_id, partition["partition_key"])
            try:
                frame = self.provider.get_daily_bars(
                    partition["symbol"],
                    partition["start_date"],
                    partition["end_date"],
                    partition["adjustment"],
                )
                raw_path, raw_sha256 = self._write_raw_partition(partition, frame)
                self.repository.record_raw_artifact(
                    batch_id,
                    partition["partition_key"],
                    str(raw_path),
                    raw_sha256,
                )
                checks = evaluate_daily_bars(
                    frame,
                    start_date=partition["start_date"],
                    end_date=partition["end_date"],
                    calendar_dates=calendar_dates,
                )
                object_key = f"daily_bar:{partition['partition_key']}"
                self.repository.save_quality_checks(batch_id, object_key, checks)
                if has_blocking_failure(checks):
                    failed_rules = ", ".join(
                        check.rule_id
                        for check in checks
                        if check.severity == "blocking" and not check.passed
                    )
                    raise ValueError(f"blocking data-quality failure: {failed_rules}")
                self.repository.publish_partition(
                    batch_id=batch_id,
                    partition=partition,
                    frame=frame,
                )
            except Exception as exc:  # noqa: BLE001 - isolate one resumable partition
                logger.warning("partition %s failed: %s", partition["partition_key"], exc)
                self.repository.mark_partition_failed(
                    batch_id, partition["partition_key"], str(exc)
                )
                errors.append(
                    {
                        "partition_key": partition["partition_key"],
                        "error": str(exc),
                    }
                )
            if self.rate_limit_seconds:
                time.sleep(self.rate_limit_seconds)

        summary = self.repository.complete_batch(batch_id)
        return {
            **summary,
            "resumed": resumed,
            "provider": self.provider.name,
            "securities": len(master),
            "adjustments": list(spec.adjustments),
            "errors": errors[:20],
            "database_status": self.database.status(),
        }
