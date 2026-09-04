from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread

from ai_quant_assistant.storage.managed_stocks import ManagedStockService

logger = logging.getLogger(__name__)


class ManagedStockTaskManager:
    """Bounded dispatcher; persisted queued tasks survive a process restart.

    Multiple dispatchers may see the same task, but only one database owner can
    start it. No schema initialization or network work just for opening a page.
    """

    def __init__(self, service: ManagedStockService):
        self.service = service
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="managed-stock")
        self.stop = Event()
        self.lock = Lock()
        self.inflight: set[str] = set()
        self.thread: Thread | None = None

    def submit(self, **kwargs):
        task, _ = self.service.request(**kwargs)
        if task["status"] == "queued":
            self.dispatch()
        return task

    def dispatch(self):
        if not self.service.database.path.is_file() or self.stop.is_set():
            return
        with self.lock:
            if self.inflight:
                return
            with self.service.database.connect(read_only=True) as connection:
                row = connection.execute(
                    """SELECT task_id FROM operation_task WHERE status='queued'
                       AND json_extract_string(spec_json, '$.dataset')=?
                       ORDER BY created_at LIMIT 1""",
                    [getattr(self.service, "dataset", "managed_stock")],
                ).fetchone()
            if row:
                self.inflight.add(row[0])
                self.executor.submit(self._run, row[0])

    def _run(self, task_id):
        try:
            self.service.run(task_id)
        except Exception:
            logger.exception("managed stock task failed: %s", task_id)
        finally:
            with self.lock:
                self.inflight.discard(task_id)

    def start(self):
        def poll():
            while not self.stop.is_set():
                try:
                    self.dispatch()
                except Exception:
                    logger.exception("managed stock queue dispatch failed")
                self.stop.wait(5)

        self.thread = Thread(target=poll, daemon=True, name="managed-stock-dispatch")
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)
        self.executor.shutdown(wait=False, cancel_futures=False)
