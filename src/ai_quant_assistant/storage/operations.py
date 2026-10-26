from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any
from uuid import uuid4

from ai_quant_assistant.config import Settings
from ai_quant_assistant.domain.models import (
    REPORT_TYPES,
    AnnouncementSyncSpec,
    BenchmarkSyncSpec,
    FinancialSyncSpec,
)
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.financial_sync_service import FinancialDataSyncService
from ai_quant_assistant.storage.monitoring import (
    OperationAlertRepository,
    OperationAuditRepository,
    PlatformMonitor,
)
from ai_quant_assistant.storage.research_sync_service import ResearchDatasetSyncService
from ai_quant_assistant.storage.task_repository import OperationTaskRepository

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None

logger = logging.getLogger(__name__)


def _accessible_regular_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


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
        audit: OperationAuditRepository | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.provider = provider
        self.tasks = tasks
        self.operation_lock = operation_lock
        self.audit = audit

    def run(
        self,
        *,
        as_of: date | None = None,
        task_id: str | None = None,
        owner_id: str | None = None,
        symbols: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        today = as_of or date.today()
        task = (
            self.tasks.get(task_id)
            if task_id is not None
            else self.tasks.create("sync", spec={"as_of": today.isoformat()}, message="等待增量同步")
        )
        task_id = task["task_id"]
        progress_value = 1
        progress_message = "正在准备增量同步"
        if owner_id is None:
            self.tasks.update(
                task_id,
                status="running",
                progress=progress_value,
                message=progress_message,
                started_at=datetime.now(UTC).isoformat(),
            )
        elif not self.tasks.start(task_id, owner_id=owner_id, message=progress_message):
            raise RuntimeError("stock pool sync task was already claimed by another worker")
        heartbeat_stop = Event()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(15):
                if owner_id is not None:
                    self.tasks.progress(
                        task_id,
                        owner_id=owner_id,
                        progress=progress_value,
                        message=progress_message,
                    )

        heartbeat_thread = Thread(target=heartbeat, daemon=True, name=f"heartbeat-{task_id}")
        if owner_id is not None:
            heartbeat_thread.start()

        def report(value: int, message: str) -> None:
            nonlocal progress_value, progress_message
            progress_value, progress_message = value, message
            if owner_id is None:
                self.tasks.update(task_id, progress=value, message=message)
            elif not self.tasks.progress(
                task_id, owner_id=owner_id, progress=value, message=message
            ):
                raise RuntimeError("stock pool sync task lease is no longer owned by this worker")
        try:
            with self.operation_lock.hold():
                self.database.initialize()
                selected_symbols = symbols or self._symbols()
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
                report(10, "按本地管理范围同步各股票历史缺口")
                stages["daily_bar"] = self._run_stage(
                    "daily_bar", self._managed_market_stage, today, symbols
                )
                report(35, "同步指数基准行情")
                stages["benchmark_daily_bar"] = self._run_stage(
                    "benchmark_daily_bar",
                    research.sync_benchmarks,
                    BenchmarkSyncSpec(
                        start_date=benchmark_start,
                        end_date=today,
                        symbols=(self.settings.sync_benchmark_symbol,),
                    ),
                )
                report(60, "同步上市公司公告")
                stages["announcement"] = self._run_stage(
                    "announcement",
                    research.sync_announcements,
                    AnnouncementSyncSpec(
                        start_date=announcement_start,
                        end_date=today,
                        symbols=selected_symbols,
                        report_types=REPORT_TYPES,
                    ),
                )
                report(80, "同步 Point-in-Time 财务指标")
                stages["financial_metric"] = self._run_stage(
                    "financial_metric",
                    financial.run,
                    FinancialSyncSpec(
                        start_date=financial_start,
                        end_date=today,
                        symbols=selected_symbols,
                    ),
                )
                # Every dataset publishes independently: one symbol's blocked
                # partition must not stop the remaining datasets from updating.
                failed_datasets = [
                    dataset
                    for dataset, stage in stages.items()
                    if stage.get("status") not in {"published", "succeeded"}
                ]
                if len(failed_datasets) == len(stages):
                    raise RuntimeError(
                        "no dataset could be published: "
                        + "; ".join(
                            self._stage_failure_detail(dataset, stages[dataset])
                            for dataset in failed_datasets
                        )
                    )
                failure_detail = "; ".join(
                    self._stage_failure_detail(dataset, stages[dataset])
                    for dataset in failed_datasets
                )
                result = {
                    "status": "succeeded" if not failed_datasets else "partial",
                    "task_id": task_id,
                    "as_of": today.isoformat(),
                    "symbols": list(selected_symbols),
                    "provider": self.provider.name,
                    "windows": {
                        "daily_bar": stages["daily_bar"].get("windows", {}),
                        "benchmark_daily_bar": benchmark_start.isoformat(),
                        "announcement": announcement_start.isoformat(),
                        "financial_metric": financial_start.isoformat(),
                    },
                    "stages": stages,
                    "failed_datasets": failed_datasets,
                }
            if failed_datasets:
                message = "同步完成，部分数据集存在失败分区"
                if owner_id is None:
                    self.tasks.update(
                        task_id,
                        status="failed",
                        message=message,
                        error=failure_detail,
                        completed_at=datetime.now(UTC).isoformat(),
                    )
                elif not self.tasks.fail(
                    task_id, owner_id=owner_id, message=message, error=failure_detail
                ):
                    raise RuntimeError("stock pool sync task completion was fenced")
                if self.audit is not None:
                    self.audit.record(
                        "sync_completed",
                        outcome="warning",
                        object_type="operation_task",
                        object_id=task_id,
                        details={
                            "symbols": list(selected_symbols),
                            "failed_datasets": failed_datasets,
                        },
                    )
                return result
            if owner_id is None:
                self.tasks.update(
                    task_id,
                    status="succeeded",
                    progress=100,
                    message="全部数据集同步完成",
                    completed_at=datetime.now(UTC).isoformat(),
                    result_json=result,
                )
            elif not self.tasks.succeed(
                task_id,
                owner_id=owner_id,
                message="全部数据集同步完成",
                result=result,
            ):
                raise RuntimeError("stock pool sync task completion was fenced")
            if self.audit is not None:
                self.audit.record(
                    "sync_completed",
                    object_type="operation_task",
                    object_id=task_id,
                    details={"symbols": list(selected_symbols)},
                )
            return result
        except Exception as exc:
            logger.exception("incremental synchronization task %s failed", task_id)
            if owner_id is None:
                self.tasks.update(
                    task_id,
                    status="failed",
                    message="增量同步失败",
                    error=str(exc),
                    completed_at=datetime.now(UTC).isoformat(),
                )
            else:
                self.tasks.fail(
                    task_id,
                    owner_id=owner_id,
                    message="增量同步失败",
                    error=str(exc),
                )
            if self.audit is not None:
                self.audit.record(
                    "sync_failed",
                    outcome="failure",
                    object_type="operation_task",
                    object_id=task_id,
                    details={"error": str(exc)},
                )
            raise
        finally:
            heartbeat_stop.set()
            if owner_id is not None:
                heartbeat_thread.join(timeout=1)

    def _managed_market_stage(self, today: date, symbols: tuple[str, ...] | None) -> dict[str, Any]:
        from ai_quant_assistant.providers.akshare_provider import AkShareProvider
        from ai_quant_assistant.storage.managed_stocks import ManagedStockService

        managed = ManagedStockService(
            self.database, self.settings,
            None if isinstance(self.provider, AkShareProvider) else lambda _: self.provider,
        )
        # Only price maintenance expands to the explicitly managed universe.
        # Announcement/financial scopes continue using the existing _symbols().
        selected = symbols if symbols is not None else managed.scheduled_symbols()
        tasks, errors, windows = [], [], {}
        for symbol in selected:
            with self.database.connect(read_only=True) as connection:
                row = connection.execute(
                    """SELECT m.target_start, m.revision, m.auto_update FROM managed_stock m
                       JOIN security_master s USING(security_id) WHERE s.symbol=?""", [symbol],
                ).fetchone()
            start = row[0] if row else date(2012, 1, 1)
            windows[symbol] = start.isoformat()
            try:
                task, created = managed.request(
                    symbol, start=start, end=today, expected_revision=row[1] if row else None,
                    confirm=True, auto_update=bool(row[2]) if row else False, actor="ops",
                )
                if created or task["status"] == "queued":
                    task = managed.run(task["task_id"])
                deadline = time.monotonic() + 30
                while task["status"] in {"queued", "running"} and time.monotonic() < deadline:
                    # Another worker may have claimed the same persisted task.
                    # Never duplicate the download or hold a DB connection here.
                    Event().wait(0.25)
                    task = managed.tasks.get(task["task_id"])
                    if task["status"] == "queued":
                        task = managed.run(task["task_id"])
                tasks.append(task["task_id"])
                if task["status"] != "succeeded":
                    errors.append({"symbol": symbol, "task_id": task["task_id"],
                                   "error": task.get("error") or "已有有效任务正在执行，请查看任务"})
            except (ValueError, RuntimeError) as exc:
                errors.append({"symbol": symbol, "error": str(exc)})
        return {"status": "failed" if errors else "published", "symbols": list(selected),
                "tasks": tasks, "errors": errors, "windows": windows}

    def _symbols(self) -> tuple[str, ...]:
        if self.settings.sync_symbols:
            return self.settings.sync_symbols
        # Refreshing the security directory must not silently expand the next
        # scheduled synchronization to every listed company.
        from ai_quant_assistant.storage.stock_pools import StockPoolService

        return tuple(StockPoolService(self.database).resolve()["resolved_symbols"])

    def _start_date(self, table: str, column: str, today: date, lookback_days: int) -> date:
        with self.database.connect(read_only=True) as connection:
            latest = connection.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
        anchor = min(latest, today) if latest is not None else today
        return anchor - timedelta(days=max(1, lookback_days))

    @staticmethod
    def _run_stage(
        dataset: str, action: Callable[..., dict[str, Any]], *arguments: Any
    ) -> dict[str, Any]:
        try:
            return action(*arguments)
        except Exception as exc:  # noqa: BLE001 - isolate one dataset from the others
            logger.exception("%s synchronization stage raised", dataset)
            return {"status": "failed", "errors": [{"error": str(exc)}]}

    @staticmethod
    def _stage_failure_detail(dataset: str, stage: dict[str, Any]) -> str:
        errors = stage.get("errors") or []
        if not errors:
            return f"{dataset}: status {stage.get('status')}"
        first = errors[0]
        message = first.get("error") if isinstance(first, dict) else str(first)
        return f"{dataset}: {len(errors)} failure(s) recorded, first: {message}"


class DatabaseBackupService:
    def __init__(
        self,
        *,
        database: LocalDatabase,
        backup_dir: Path,
        tasks: OperationTaskRepository,
        operation_lock: ExclusiveOperationLock,
        audit: OperationAuditRepository | None = None,
        retention_days: int = 30,
        minimum_count: int = 7,
    ) -> None:
        self.database = database
        self.backup_dir = backup_dir
        self.tasks = tasks
        self.operation_lock = operation_lock
        self.audit = audit
        self.retention_days = max(1, retention_days)
        self.minimum_count = max(1, minimum_count)

    def run(
        self,
        *,
        task_id: str | None = None,
        apply_retention: bool = False,
        actor: str = "system",
    ) -> dict[str, Any]:
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
        try:
            with self.operation_lock.hold():
                self.database.initialize()
                self.tasks.update(task_id, progress=35, message="正在复制数据库文件")
                self.tasks.update(task_id, progress=70, message="正在验证备份完整性")
                backup = self._create_backup_file()
                result = {
                    "status": "succeeded",
                    "task_id": task_id,
                    **backup,
                }
            self.tasks.update(
                task_id,
                status="succeeded",
                progress=100,
                message="数据库备份完成",
                completed_at=datetime.now(UTC).isoformat(),
                result_json=result,
            )
            if self.audit is not None:
                self.audit.record(
                    "backup_created",
                    actor=actor,
                    object_type="database_backup",
                    object_id=result["backup_id"],
                    details={"size_bytes": result["size_bytes"]},
                )
            if apply_retention:
                result["retention"] = self.prune(dry_run=False, actor=actor)
            return result
        except Exception as exc:
            self.tasks.update(
                task_id,
                status="failed",
                message="数据库备份失败",
                error=str(exc),
                completed_at=datetime.now(UTC).isoformat(),
            )
            if self.audit is not None:
                self.audit.record(
                    "backup_failed", actor=actor, outcome="failure", details={"error": str(exc)}
                )
            raise

    def _create_backup_file(self) -> dict[str, Any]:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        backup_id = f"backup-{now:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
        target = self.backup_dir / f"{backup_id}.duckdb"
        temporary = self.backup_dir / f".{backup_id}.duckdb.tmp"
        try:
            # Keep checkpoint, copy, verification, publication, and catalog
            # registration inside one cross-process database-access fence.
            with self.database.exclusive_access():
                with self.database.connect() as connection:
                    connection.execute("CHECKPOINT")
                shutil.copy2(self.database.path, temporary)
                with self.database._duckdb().connect(str(temporary), read_only=True) as connection:
                    version = int(
                        connection.execute(
                            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                        ).fetchone()[0]
                    )
                with temporary.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                os.replace(temporary, target)
                size = target.stat().st_size
                backup = {
                    "backup_id": backup_id,
                    "backup_path": str(target),
                    "size_bytes": size,
                    "sha256": digest,
                    "schema_version": version,
                }
                self._record_backup(backup)
                return backup
        finally:
            temporary.unlink(missing_ok=True)

    def _record_backup(self, backup: dict[str, Any]) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO database_backup (
                    backup_id, database_path, backup_path, size_bytes, sha256
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (backup_id) DO NOTHING
                """,
                [
                    backup["backup_id"],
                    str(self.database.path),
                    backup["backup_path"],
                    backup["size_bytes"],
                    backup["sha256"],
                ],
            )

    def list(self, *, limit: int = 50, include_deleted: bool = False) -> list[dict[str, Any]]:
        if not self.database.path.is_file():
            return []
        condition = "" if include_deleted else "WHERE deleted_at IS NULL"
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT backup_id, database_path, backup_path, size_bytes, sha256,
                       created_at, deleted_at, deletion_reason
                FROM database_backup
                {condition}
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
                "exists": _accessible_regular_file(Path(row[2])),
                "deleted_at": row[6].isoformat() if row[6] else None,
                "deletion_reason": row[7],
            }
            for row in rows
        ]

    def get(self, backup_id: str) -> dict[str, Any]:
        matches = [item for item in self.list(limit=200) if item["backup_id"] == backup_id]
        if not matches:
            raise KeyError(backup_id)
        return matches[0]

    def verify(self, backup_id: str, *, actor: str = "system") -> dict[str, Any]:
        backup = self.get(backup_id)
        path = Path(backup["backup_path"])
        if path.is_symlink() or path.resolve().parent != self.backup_dir.resolve():
            raise ValueError("backup path must be a regular file inside the configured backup directory")
        if not path.is_file():
            raise FileNotFoundError(f"database backup file does not exist: {backup_id}")
        actual_size = path.stat().st_size
        with path.open("rb") as stream:
            actual_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual_size != backup["size_bytes"] or actual_digest != backup["sha256"]:
            raise ValueError(f"database backup integrity verification failed: {backup_id}")
        with self.database._duckdb().connect(str(path), read_only=True) as connection:
            version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
            )
        result = {
            "status": "verified",
            **backup,
            "schema_version": version,
            "verified_at": datetime.now(UTC).isoformat(),
        }
        if self.audit is not None:
            self.audit.record(
                "backup_verified",
                actor=actor,
                object_type="database_backup",
                object_id=backup_id,
            )
        return result

    def prune(
        self,
        *,
        dry_run: bool = True,
        now: datetime | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        reference = now or datetime.now(UTC)
        backups = self.list(limit=200)
        candidates = [
            item
            for item in backups[self.minimum_count :]
            if datetime.fromisoformat(item["created_at"])
            <= reference - timedelta(days=self.retention_days)
        ]
        removed: list[str] = []
        if not dry_run and candidates:
            with self.operation_lock.hold():
                for candidate in candidates:
                    path = Path(candidate["backup_path"])
                    if path.is_symlink() or path.resolve().parent != self.backup_dir.resolve():
                        raise ValueError("refusing to remove a backup outside the configured directory")
                    if not path.is_file():
                        continue
                    path.unlink()
                    with self.database.connect() as connection:
                        connection.execute(
                            """
                            UPDATE database_backup
                            SET deleted_at = CURRENT_TIMESTAMP, deletion_reason = 'retention'
                            WHERE backup_id = ?
                            """,
                            [candidate["backup_id"]],
                        )
                    removed.append(candidate["backup_id"])
            if self.audit is not None:
                self.audit.record(
                    "backup_pruned",
                    actor=actor,
                    details={"removed_count": len(removed), "backup_ids": removed},
                )
        return {
            "status": "preview" if dry_run else "completed",
            "dry_run": dry_run,
            "retention_days": self.retention_days,
            "minimum_count": self.minimum_count,
            "candidate_count": len(candidates),
            "candidates": candidates,
            "removed_count": len(removed),
            "removed_backup_ids": removed,
        }

    def restore(
        self,
        backup_id: str,
        *,
        confirm_backup_id: str,
        actor: str = "system",
    ) -> dict[str, Any]:
        if not confirm_backup_id or confirm_backup_id != backup_id:
            raise ValueError("restore requires confirmation matching the exact backup ID")
        selected = self.verify(backup_id, actor=actor)
        temporary = self.database.path.parent / f".restore-{uuid4().hex}.duckdb.tmp"
        try:
            with self.operation_lock.hold():
                with self.database.exclusive_access():
                    self.verify(backup_id, actor=actor)
                    previous = self._create_backup_file()
                    shutil.copy2(selected["backup_path"], temporary)
                    with self.database._duckdb().connect(
                        str(temporary), read_only=True
                    ) as connection:
                        connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
                    wal_path = Path(f"{self.database.path}.wal")
                    if wal_path.exists():
                        raise RuntimeError(
                            "database write-ahead log exists; stop active writers before restore"
                        )
                    os.replace(temporary, self.database.path)
                    migration = self.database.initialize()
                    self._record_backup(selected)
                    self._record_backup(previous)
            result = {
                "status": "restored",
                "backup_id": backup_id,
                "database_path": str(self.database.path),
                "pre_restore_backup": previous,
                "schema_version": migration["schema_version"],
                "restored_at": datetime.now(UTC).isoformat(),
            }
            if self.audit is not None:
                self.audit.record(
                    "backup_restored",
                    actor=actor,
                    object_type="database_backup",
                    object_id=backup_id,
                    details={"pre_restore_backup_id": previous["backup_id"]},
                )
            return result
        finally:
            temporary.unlink(missing_ok=True)

    def relocate(self, old_data_dir: Path, *, actor: str = "system") -> dict[str, Any]:
        old_root = old_data_dir.absolute()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        updated: list[str] = []
        missing: list[str] = []
        with self.operation_lock.hold():
            for backup in self.list(limit=200):
                previous_path = Path(backup["backup_path"]).absolute()
                if not previous_path.is_relative_to(old_root):
                    continue
                relocated = self.backup_dir / previous_path.name
                if relocated.is_symlink() or not relocated.is_file():
                    missing.append(backup["backup_id"])
                    continue
                with self.database.connect() as connection:
                    connection.execute(
                        """
                        UPDATE database_backup
                        SET database_path = ?, backup_path = ?
                        WHERE backup_id = ?
                        """,
                        [str(self.database.path), str(relocated), backup["backup_id"]],
                    )
                updated.append(backup["backup_id"])
        result = {
            "status": "completed",
            "old_data_dir": str(old_root),
            "backup_dir": str(self.backup_dir),
            "updated_count": len(updated),
            "missing_count": len(missing),
            "updated_backup_ids": updated,
            "missing_backup_ids": missing,
        }
        if updated and self.audit is not None:
            self.audit.record("backup_paths_relocated", actor=actor, details=result)
        return result


class OperationsDashboard:
    def __init__(
        self,
        database: LocalDatabase,
        tasks: OperationTaskRepository,
        backups: DatabaseBackupService,
        alerts: OperationAlertRepository | None = None,
        audit: OperationAuditRepository | None = None,
        monitor: PlatformMonitor | None = None,
    ) -> None:
        self.database = database
        self.tasks = tasks
        self.backups = backups
        self.alerts = alerts
        self.audit = audit
        self.monitor = monitor

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
            "alerts": self.alerts.list(active_only=True, limit=20) if self.alerts else [],
            "audit": self.audit.list(limit=10) if self.audit else [],
            "quality": self.monitor.quality() if self.monitor else {},
        }
