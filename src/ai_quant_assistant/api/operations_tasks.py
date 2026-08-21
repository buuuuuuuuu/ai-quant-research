from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from ai_quant_assistant.storage.operations import DatabaseBackupService, IncrementalSyncService
from ai_quant_assistant.storage.task_repository import OperationTaskRepository

logger = logging.getLogger(__name__)


class OperationsTaskManager:
    def __init__(
        self,
        *,
        repository: OperationTaskRepository,
        sync_service: IncrementalSyncService,
        backup_service: DatabaseBackupService,
    ) -> None:
        self.repository = repository
        self.sync_service = sync_service
        self.backup_service = backup_service
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="operations")

    def submit_sync(self, *, as_of: date | None = None) -> dict[str, Any]:
        selected_date = as_of or date.today()
        task = self.repository.create(
            "sync", spec={"as_of": selected_date.isoformat()}, message="等待增量同步"
        )
        self.executor.submit(self._run_sync, task["task_id"], selected_date)
        return self.repository.get(task["task_id"])

    def submit_backup(self) -> dict[str, Any]:
        task = self.repository.create("backup", message="等待数据库备份")
        self.executor.submit(self._run_backup, task["task_id"])
        return self.repository.get(task["task_id"])

    def _run_sync(self, task_id: str, as_of: date) -> None:
        try:
            self.sync_service.run(task_id=task_id, as_of=as_of)
        except Exception:  # noqa: BLE001 - terminal errors are already persisted
            logger.exception("background synchronization task %s failed", task_id)

    def _run_backup(self, task_id: str) -> None:
        try:
            self.backup_service.run(task_id=task_id)
        except Exception:  # noqa: BLE001 - terminal errors are already persisted
            logger.exception("background backup task %s failed", task_id)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
