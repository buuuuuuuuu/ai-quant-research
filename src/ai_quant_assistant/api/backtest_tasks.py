from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import Any
from uuid import uuid4

from ai_quant_assistant.api.schemas import DailySignalStrategyRequest
from ai_quant_assistant.domain.models import (
    DailySignalStrategySpec,
    FactorBacktestSpec,
    FactorLabSpec,
)
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
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=task_type)
        self.max_workers = max_workers
        self.stop = Event()
        self.lock = Lock()
        self.inflight: set[str] = set()
        self.thread: Thread | None = None

    @staticmethod
    def _logical_type(task: dict[str, Any]) -> str:
        return task.get("spec", {}).get("research_type") or task["task_type"]

    def submit(
        self, spec: FactorBacktestSpec | FactorLabSpec | DailySignalStrategySpec
    ) -> dict[str, Any]:
        payload = spec.to_dict()
        storage_type = self.task_type
        if self.task_type != "backtest":
            payload["research_type"] = self.task_type
            storage_type = "backtest"
        if self.task_type == "daily_signal":
            identity_payload = {key: value for key, value in payload.items() if key != "preflight_snapshot"}
            if isinstance(identity_payload.get("stock_pool"), dict):
                identity_payload["stock_pool"] = {
                    key: value for key, value in identity_payload["stock_pool"].items()
                    if key != "resolved_at"
                }
            identity = hashlib.sha256(
                json.dumps(identity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            task, created = self.repository.create_or_get_active(
                storage_type, claim_key=f"daily-signal:{identity}",
                spec=payload, message="等待执行",
            )
            if created:
                self.dispatch()
            return {**self.get(task["task_id"]), "reused": not created}
        task = self.repository.create(storage_type, spec=payload, message="等待执行")
        task_id = task["task_id"]
        self.executor.submit(self._run, task_id, spec)
        return self.get(task_id)

    def dispatch(self) -> None:
        if self.task_type != "daily_signal" or self.stop.is_set():
            return
        with self.lock:
            capacity = self.max_workers - len(self.inflight)
            if capacity <= 0:
                return
            with self.repository.database.connect(read_only=True) as connection:
                rows = connection.execute(
                    """SELECT task_id FROM operation_task
                       WHERE task_type='backtest' AND status='queued'
                         AND json_extract_string(spec_json, '$.research_type')='daily_signal'
                       ORDER BY created_at, task_id LIMIT ?""",
                    [max(capacity * 2, 2)],
                ).fetchall()
            for (task_id,) in rows:
                if task_id in self.inflight:
                    continue
                self.inflight.add(task_id)
                self.executor.submit(self._run, task_id, None)
                capacity -= 1
                if capacity <= 0:
                    break

    def start(self) -> None:
        if self.task_type != "daily_signal" or self.thread is not None:
            return

        def poll() -> None:
            while not self.stop.is_set():
                try:
                    self.dispatch()
                except Exception:
                    logger.exception("daily signal queue dispatch failed")
                self.stop.wait(2)

        self.thread = Thread(target=poll, daemon=True, name="daily-signal-dispatch")
        self.thread.start()

    def _update(self, task_id: str, **changes: Any) -> None:
        self.repository.update(task_id, **changes)

    def _run(
        self, task_id: str, spec: FactorBacktestSpec | FactorLabSpec | DailySignalStrategySpec
    ) -> None:
        owner = uuid4().hex if self.task_type == "daily_signal" else None
        started = False
        if not owner:
            self._update(
                task_id, status="running", progress=1, message="任务已启动",
                started_at=datetime.now(UTC).isoformat(),
            )

        def progress(value: int, message: str) -> None:
            if owner:
                self.repository.progress(task_id, owner_id=owner, progress=value, message=message)
            else:
                self._update(task_id, progress=value, message=message)

        try:
            with self.operation_lock.hold():
                if owner:
                    if not self.repository.start(task_id, owner_id=owner, message="正在准备回测数据"):
                        return
                    started = True
                if spec is None:
                    payload = self.repository.get(task_id)["spec"]
                    request = DailySignalStrategyRequest.model_validate(payload)
                    spec = replace(
                        request.to_spec(stock_pool=payload.get("stock_pool")),
                        preflight_snapshot=payload.get("preflight_snapshot"),
                    )
                result = self.service.run(spec, progress_callback=progress)
            if owner:
                self.repository.succeed(task_id, owner_id=owner, message="回测完成", run_id=result["run_id"])
            else:
                self._update(
                    task_id, status="succeeded", progress=100, message="回测完成",
                    completed_at=datetime.now(UTC).isoformat(), run_id=result["run_id"],
                )
        except Exception as exc:  # noqa: BLE001 - task failure is returned to the caller
            if owner and not started:
                # The shared download/backtest slot is busy. The queued task
                # remains durable and the bounded poller will try again.
                logger.info("daily signal task %s remains queued: %s", task_id, exc)
                return
            logger.exception("%s task %s failed", self.task_type, task_id)
            if owner:
                self.repository.fail(task_id, owner_id=owner, message="回测失败", error=str(exc))
            else:
                self._update(
                    task_id, status="failed", message="回测失败",
                    completed_at=datetime.now(UTC).isoformat(), error=str(exc),
                )
        finally:
            if owner:
                with self.lock:
                    self.inflight.discard(task_id)

    def cancel(self, task_id: str) -> dict[str, Any]:
        if self.task_type != "daily_signal":
            raise ValueError("仅支持取消尚未开始的日线信号任务")
        return self.repository.cancel_queued(task_id, research_type=self.task_type)

    def get(self, task_id: str) -> dict[str, Any]:
        task = self.repository.get(task_id)
        expected = self._logical_type(task)
        if expected != self.task_type:
            raise KeyError(task_id)
        task["task_type"] = expected
        return task

    def list(self) -> list[dict[str, Any]]:
        tasks = self.repository.list(task_type="backtest")
        selected = []
        for task in tasks:
            expected = self._logical_type(task)
            if expected == self.task_type:
                task["task_type"] = expected
                selected.append(task)
        return selected

    def shutdown(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)
        self.executor.shutdown(wait=False, cancel_futures=False)
