from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from ai_quant_assistant.config import Settings
from ai_quant_assistant.domain.models import (
    DEFAULT_RESEARCH_SYMBOLS,
    REPORT_TYPES,
    AnnouncementSyncSpec,
    BenchmarkSyncSpec,
    DataSyncSpec,
    FinancialSyncSpec,
)
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.financial_sync_service import FinancialDataSyncService
from ai_quant_assistant.storage.research_sync_service import ResearchDatasetSyncService
from ai_quant_assistant.storage.sync_service import MarketDataSyncService
from ai_quant_assistant.storage.task_repository import OperationTaskRepository

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None

logger = logging.getLogger(__name__)


class ExclusiveOperationLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.local_lock = Lock()

    @contextmanager
    def hold(self) -> Iterator[None]:
        if not self.local_lock.acquire(blocking=False):
            raise RuntimeError("another synchronization, backup, or backtest is already running")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError(
                        "another synchronization, backup, or backtest is already running"
                    ) from exc
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            self.local_lock.release()


class IncrementalSyncService:
    def __init__(
        self,
        *,
        database: LocalDatabase,
        settings: Settings,
        provider: Any,
        tasks: OperationTaskRepository,
        operation_lock: ExclusiveOperationLock,
    ) -> None:
        self.database = database
        self.settings = settings
        self.provider = provider
        self.tasks = tasks
        self.operation_lock = operation_lock

    def run(self, *, as_of: date | None = None, task_id: str | None = None) -> dict[str, Any]:
        today = as_of or date.today()
        task = (
            self.tasks.get(task_id)
            if task_id is not None
            else self.tasks.create("sync", spec={"as_of": today.isoformat()}, message="等待增量同步")
        )
        task_id = task["task_id"]
        self.tasks.update(
            task_id,
            status="running",
            progress=1,
            message="正在准备增量同步",
            started_at=datetime.now(UTC).isoformat(),
        )
        try:
            with self.operation_lock.hold():
                self.database.initialize()
                symbols = self._symbols()
                market_start = self._start_date(
                    "published_daily_bar", "trade_date", today, self.settings.sync_lookback_days
                )
                benchmark_start = self._start_date(
                    "published_benchmark_daily_bar",
                    "trade_date",
                    today,
                    self.settings.sync_lookback_days,
                )
                announcement_start = self._start_date(
                    "published_announcement",
                    "announcement_date",
                    today,
                    self.settings.sync_announcement_lookback_days,
                )
                financial_start = self._start_date(
                    "published_financial_report",
                    "report_period_end",
                    today,
                    self.settings.sync_financial_lookback_days,
                )
                market = MarketDataSyncService(
                    database=self.database,
                    layout=self.database.layout,
                    provider=self.provider,
                    rate_limit_seconds=self.settings.data_sync_rate_limit_seconds,
                )
                research = ResearchDatasetSyncService(
                    database=self.database,
                    layout=self.database.layout,
                    provider=self.provider,
                    rate_limit_seconds=self.settings.data_sync_rate_limit_seconds,
                )
                financial = FinancialDataSyncService(
                    database=self.database,
                    layout=self.database.layout,
                    provider=self.provider,
                    rate_limit_seconds=self.settings.data_sync_rate_limit_seconds,
                )
                stages: dict[str, dict[str, Any]] = {}
                self.tasks.update(task_id, progress=10, message=f"同步 {len(symbols)} 只股票行情")
                stages["daily_bar"] = market.run(
                    DataSyncSpec(
                        start_date=market_start,
                        end_date=today,
                        symbols=symbols,
                        adjustments=("qfq",),
                    )
                )
                self._require_published("daily_bar", stages["daily_bar"])
                self.tasks.update(task_id, progress=35, message="同步指数基准行情")
                stages["benchmark_daily_bar"] = research.sync_benchmarks(
                    BenchmarkSyncSpec(
                        start_date=benchmark_start,
                        end_date=today,
                        symbols=(self.settings.sync_benchmark_symbol,),
                    )
                )
                self._require_published("benchmark_daily_bar", stages["benchmark_daily_bar"])
                self.tasks.update(task_id, progress=60, message="同步上市公司公告")
                stages["announcement"] = research.sync_announcements(
                    AnnouncementSyncSpec(
                        start_date=announcement_start,
                        end_date=today,
                        symbols=symbols,
                        report_types=REPORT_TYPES,
                    )
                )
                self._require_published("announcement", stages["announcement"])
                self.tasks.update(task_id, progress=80, message="同步 Point-in-Time 财务指标")
                stages["financial_metric"] = financial.run(
                    FinancialSyncSpec(start_date=financial_start, end_date=today, symbols=symbols)
                )
                self._require_published("financial_metric", stages["financial_metric"])
                result = {
                    "status": "succeeded",
                    "task_id": task_id,
                    "as_of": today.isoformat(),
                    "symbols": list(symbols),
                    "provider": self.provider.name,
                    "windows": {
                        "daily_bar": market_start.isoformat(),
                        "benchmark_daily_bar": benchmark_start.isoformat(),
                        "announcement": announcement_start.isoformat(),
                        "financial_metric": financial_start.isoformat(),
                    },
                    "stages": stages,
                }
            self.tasks.update(
                task_id,
                status="succeeded",
                progress=100,
                message="全部数据集同步完成",
                completed_at=datetime.now(UTC).isoformat(),
                result_json=result,
            )
            return result
        except Exception as exc:
            logger.exception("incremental synchronization task %s failed", task_id)
            self.tasks.update(
                task_id,
                status="failed",
                message="增量同步失败",
                error=str(exc),
                completed_at=datetime.now(UTC).isoformat(),
            )
            raise

    def _symbols(self) -> tuple[str, ...]:
        if self.settings.sync_symbols:
            return self.settings.sync_symbols
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT symbol FROM security_master WHERE status = 'active' ORDER BY symbol"
            ).fetchall()
        return tuple(str(item[0]) for item in rows) or DEFAULT_RESEARCH_SYMBOLS

    def _start_date(self, table: str, column: str, today: date, lookback_days: int) -> date:
        with self.database.connect(read_only=True) as connection:
            latest = connection.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
        anchor = min(latest, today) if latest is not None else today
        return anchor - timedelta(days=max(1, lookback_days))

    @staticmethod
    def _require_published(dataset: str, result: dict[str, Any]) -> None:
        if result.get("status") not in {"published", "succeeded"}:
            raise RuntimeError(f"{dataset} synchronization failed: {result.get('errors') or result}")


class DatabaseBackupService:
    def __init__(
        self,
        *,
        database: LocalDatabase,
        backup_dir: Path,
        tasks: OperationTaskRepository,
        operation_lock: ExclusiveOperationLock,
    ) -> None:
        self.database = database
        self.backup_dir = backup_dir
        self.tasks = tasks
        self.operation_lock = operation_lock

    def run(self, *, task_id: str | None = None) -> dict[str, Any]:
        task = (
            self.tasks.get(task_id)
            if task_id is not None
            else self.tasks.create("backup", message="等待数据库备份")
        )
        task_id = task["task_id"]
        self.tasks.update(
            task_id,
            status="running",
            progress=5,
            message="正在创建一致性备份",
            started_at=datetime.now(UTC).isoformat(),
        )
        temporary: Path | None = None
        try:
            with self.operation_lock.hold():
                self.database.initialize()
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                now = datetime.now(UTC)
                backup_id = f"backup-{now:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
                target = self.backup_dir / f"{backup_id}.duckdb"
                temporary = self.backup_dir / f".{backup_id}.duckdb.tmp"
                self.tasks.update(task_id, progress=35, message="正在复制数据库文件")
                with self.database.connect() as connection:
                    connection.execute("CHECKPOINT")
                shutil.copy2(self.database.path, temporary)
                self.tasks.update(task_id, progress=70, message="正在验证备份完整性")
                with self.database._duckdb().connect(str(temporary), read_only=True) as connection:
                    version = int(
                        connection.execute(
                            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                        ).fetchone()[0]
                    )
                with temporary.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                os.replace(temporary, target)
                temporary = None
                size = target.stat().st_size
                with self.database.connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO database_backup (
                            backup_id, database_path, backup_path, size_bytes, sha256
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        [backup_id, str(self.database.path), str(target), size, digest],
                    )
                result = {
                    "status": "succeeded",
                    "task_id": task_id,
                    "backup_id": backup_id,
                    "backup_path": str(target),
                    "size_bytes": size,
                    "sha256": digest,
                    "schema_version": version,
                }
            self.tasks.update(
                task_id,
                status="succeeded",
                progress=100,
                message="数据库备份完成",
                completed_at=datetime.now(UTC).isoformat(),
                result_json=result,
            )
            return result
        except Exception as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            self.tasks.update(
                task_id,
                status="failed",
                message="数据库备份失败",
                error=str(exc),
                completed_at=datetime.now(UTC).isoformat(),
            )
            raise

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if not self.database.path.is_file():
            return []
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT backup_id, database_path, backup_path, size_bytes, sha256, created_at
                FROM database_backup
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [max(1, min(limit, 200))],
            ).fetchall()
        return [
            {
                "backup_id": row[0],
                "database_path": row[1],
                "backup_path": row[2],
                "size_bytes": int(row[3]),
                "sha256": row[4],
                "created_at": row[5].astimezone(UTC).isoformat()
                if row[5].tzinfo is not None
                else row[5].replace(tzinfo=UTC).isoformat(),
                "exists": Path(row[2]).is_file(),
            }
            for row in rows
        ]


class OperationsDashboard:
    def __init__(
        self,
        database: LocalDatabase,
        tasks: OperationTaskRepository,
        backups: DatabaseBackupService,
    ) -> None:
        self.database = database
        self.tasks = tasks
        self.backups = backups

    def status(self) -> dict[str, Any]:
        database_status = self.database.status()
        if not database_status.get("initialized"):
            return {"database": database_status, "datasets": [], "tasks": [], "backups": []}
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT dataset, status, started_at, ended_at, row_count, error
                FROM sync_batch
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY dataset ORDER BY created_at DESC
                ) = 1
                ORDER BY dataset
                """
            ).fetchall()
        datasets = [
            {
                "dataset": row[0],
                "status": row[1],
                "started_at": row[2].replace(tzinfo=UTC).isoformat() if row[2] else None,
                "ended_at": row[3].replace(tzinfo=UTC).isoformat() if row[3] else None,
                "row_count": int(row[4]),
                "error": row[5],
            }
            for row in rows
        ]
        return {
            "database": database_status,
            "datasets": datasets,
            "tasks": self.tasks.list(limit=20),
            "backups": self.backups.list(limit=10),
        }
