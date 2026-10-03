from __future__ import annotations

import logging
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread
from typing import Any, Protocol

from ai_quant_assistant.storage.database import LocalDatabase

logger = logging.getLogger(__name__)


class QueuedTaskService(Protocol):
    database: LocalDatabase

    def run(self, task_id: str) -> Any: ...


class ManagedStockTaskManager:
    """Bounded dispatcher; persisted queued tasks survive a process restart.

    Multiple dispatchers may see the same task, but only one database owner can
    start it. No schema initialization or network work just for opening a page.
    """

    def __init__(
        self,
        service: QueuedTaskService,
        *,
        datasets: Iterable[str],
        worker_name: str,
    ):
        selected = tuple(
            sorted(
                {
                    dataset.strip()
                    for dataset in datasets
                    if isinstance(dataset, str) and dataset.strip()
                }
            )
        )
        if not selected:
            raise ValueError("task manager requires at least one dataset")
        if not worker_name.strip():
            raise ValueError("task manager requires a worker name")
        self.service = service
        self.datasets = selected
        self.worker_name = worker_name.strip()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=self.worker_name)
        self.stop = Event()
        self.lock = Lock()
        self.inflight: set[str] = set()
        self.thread: Thread | None = None

    def submit(self, **kwargs):
        task, _ = self.service.request(**kwargs)
        if task["status"] == "queued":
            self.dispatch()
        return task

    def submit_gap_verification(self, security_id: str, **kwargs):
        task, _ = self.service.request_gap_verification(security_id, **kwargs)
        if task["status"] == "queued":
            self.dispatch()
        return task

    def dispatch(self):
        if not self.service.database.path.is_file() or self.stop.is_set():
            return
        with self.lock:
            if self.inflight:
                return
            placeholders = ",".join("?" for _ in self.datasets)
            with self.service.database.connect(read_only=True) as connection:
                row = connection.execute(
                    f"""SELECT task_id FROM operation_task WHERE status='queued'
                        AND json_extract_string(spec_json, '$.dataset') IN ({placeholders})
                        ORDER BY created_at LIMIT 1""",  # noqa: S608 - placeholders only
                    list(self.datasets),
                ).fetchone()
            if row:
                self.inflight.add(row[0])
                self.executor.submit(self._run, row[0])

    def _run(self, task_id):
        try:
            self.service.run(task_id)
        except Exception:
            logger.exception("%s task attempt failed: %s", self.worker_name, task_id)
        finally:
            with self.lock:
                self.inflight.discard(task_id)

    def start(self):
        def poll():
            while not self.stop.is_set():
                try:
                    self.dispatch()
                except Exception:
                    logger.exception("%s queue dispatch failed", self.worker_name)
                self.stop.wait(5)

        self.thread = Thread(target=poll, daemon=True, name=f"{self.worker_name}-dispatch")
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)
        self.executor.shutdown(wait=False, cancel_futures=False)
