from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Lock
from typing import Any
from uuid import uuid4

from ai_quant_assistant.domain.models import FactorBacktestSpec
from ai_quant_assistant.research.factor_backtest import FactorBacktestService

logger = logging.getLogger(__name__)


class BacktestTaskManager:
    def __init__(self, service: FactorBacktestService, *, max_workers: int = 2) -> None:
        self.service = service
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="backtest")
        self.lock = Lock()
        self.tasks: dict[str, dict[str, Any]] = {}

    def submit(self, spec: FactorBacktestSpec) -> dict[str, Any]:
        task_id = f"task-{uuid4().hex}"
        now = datetime.now(UTC).isoformat()
        with self.lock:
            self.tasks[task_id] = {
                "task_id": task_id,
                "status": "queued",
                "progress": 0,
                "message": "等待执行",
                "created_at": now,
                "started_at": None,
                "completed_at": None,
                "run_id": None,
                "error": None,
                "spec": spec.to_dict(),
            }
        self.executor.submit(self._run, task_id, spec)
        return self.get(task_id)

    def _update(self, task_id: str, **changes: Any) -> None:
        with self.lock:
            self.tasks[task_id].update(changes)

    def _run(self, task_id: str, spec: FactorBacktestSpec) -> None:
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
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            return dict(task)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            tasks = [dict(task) for task in self.tasks.values()]
        return sorted(tasks, key=lambda item: item["created_at"], reverse=True)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
