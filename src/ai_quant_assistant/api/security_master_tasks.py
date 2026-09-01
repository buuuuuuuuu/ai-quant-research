from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

from ai_quant_assistant.storage.security_master_refresh import SecurityMasterRefreshService


class SecurityMasterTaskManager:
    def __init__(self, service: SecurityMasterRefreshService) -> None:
        self.service = service
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="security-directory")

    def submit(self, *, actor: str) -> dict[str, Any]:
        task, created = self.service.prepare(actor=actor)
        if created:
            try:
                self.executor.submit(self.service.run, task["task_id"])
            except Exception:
                owner = f"refresh-schedule-{uuid4().hex}"
                if self.service.tasks.start(
                    task["task_id"], owner_id=owner, message="任务调度失败"
                ):
                    self.service.tasks.fail(
                        task["task_id"],
                        owner_id=owner,
                        message="任务调度失败，可重试",
                        error="executor unavailable",
                    )
                raise
        return self.service.tasks.get(task["task_id"])

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
