from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from ai_quant_assistant.domain.models import FactorBacktestSpec, FactorLabSpec
from ai_quant_assistant.research.factor_backtest import FactorBacktestService
from ai_quant_assistant.research.factor_lab import FactorLabService
from ai_quant_assistant.storage.operations import ExclusiveOperationLock
from ai_quant_assistant.storage.task_repository import OperationTaskRepository

logger = logging.getLogger(__name__)


class BacktestTaskManager:
    def __init__(
        self,
        service: FactorBacktestService | FactorLabService,
        repository: OperationTaskRepository,
        operation_lock: ExclusiveOperationLock,
        *,
        max_workers: int = 2,
        task_type: str = "backtest",
    ) -> None:
        self.service = service
        self.repository = repository
        self.operation_lock = operation_lock
        self.task_type = task_type
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="backtest")

    def submit(self, spec: FactorBacktestSpec | FactorLabSpec) -> dict[str, Any]:
        payload = spec.to_dict()
        storage_type = self.task_type
        if self.task_type == "factor_lab":
            payload["research_type"] = "factor_lab"
            storage_type = "backtest"
        task = self.repository.create(storage_type, spec=payload, message="等待执行")
        task_id = task["task_id"]
        self.executor.submit(self._run, task_id, spec)
        return self.get(task_id)

    def _update(self, task_id: str, **changes: Any) -> None:
        self.repository.update(task_id, **changes)

    def _run(self, task_id: str, spec: FactorBacktestSpec | FactorLabSpec) -> None:
        self._update(
            task_id,
            status="running",
            progress=1,
            message="任务已启动",
            started_at=datetime.now(UTC).isoformat(),
        )

        def progress(value: int, message: str) -> None:
            self._update(task_id, progress=value, message=message)

        try:
            with self.operation_lock.hold():
                result = self.service.run(spec, progress_callback=progress)
            self._update(
                task_id,
                status="succeeded",
                progress=100,
                message="回测完成",
                completed_at=datetime.now(UTC).isoformat(),
                run_id=result["run_id"],
            )
        except Exception as exc:  # noqa: BLE001 - task failure is returned to the caller
            logger.exception("factor backtest task %s failed", task_id)
            self._update(
                task_id,
                status="failed",
                message="回测失败",
                completed_at=datetime.now(UTC).isoformat(),
                error=str(exc),
            )

    def get(self, task_id: str) -> dict[str, Any]:
        task = self.repository.get(task_id)
        is_factor_lab = task.get("spec", {}).get("research_type") == "factor_lab"
        expected = "factor_lab" if is_factor_lab else task["task_type"]
        if expected != self.task_type:
            raise KeyError(task_id)
        task["task_type"] = expected
        return task

    def list(self) -> list[dict[str, Any]]:
        tasks = self.repository.list(task_type="backtest")
        selected = []
        for task in tasks:
            is_factor_lab = task.get("spec", {}).get("research_type") == "factor_lab"
            expected = "factor_lab" if is_factor_lab else task["task_type"]
            if expected == self.task_type:
                task["task_type"] = expected
                selected.append(task)
        return selected

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
