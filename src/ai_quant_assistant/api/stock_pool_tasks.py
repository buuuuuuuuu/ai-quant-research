from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any
from uuid import uuid4

from ai_quant_assistant.storage.monitoring import OperationAuditRepository
from ai_quant_assistant.storage.operations import IncrementalSyncService
from ai_quant_assistant.storage.stock_pools import StockPoolConflictError, StockPoolService
from ai_quant_assistant.storage.task_repository import OperationTaskRepository

logger = logging.getLogger(__name__)


class StockPoolSyncTaskManager:
    def __init__(
        self,
        *,
        pools: StockPoolService,
        repository: OperationTaskRepository,
        sync_service: IncrementalSyncService,
        audit: OperationAuditRepository,
        max_workers: int = 1,
    ) -> None:
        self.pools = pools
        self.repository = repository
        self.sync_service = sync_service
        self.audit = audit
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="stock-pool-sync"
        )

    def submit(
        self,
        pool_id: str,
        *,
        expected_revision: int,
        as_of: date,
        actor: str,
    ) -> dict[str, Any]:
        pool = self.pools.resolve(pool_id, revision=expected_revision)
        current = self.pools.get(pool_id)
        if int(current["current_revision"]) != expected_revision:
            raise StockPoolConflictError(
                f"stock pool revision conflict: expected {expected_revision}, "
                f"current {current['current_revision']}"
            )
        identity = {
            "pool_id": pool_id,
            "pool_revision": expected_revision,
            "as_of": as_of.isoformat(),
            "datasets": ["daily_bar", "benchmark_daily_bar", "announcement", "financial_metric"],
        }
        claim_key = "stock-pool-sync:" + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        task, created = self.repository.create_or_get_active(
            "stock_pool_sync",
            claim_key=claim_key,
            spec={**identity, "stock_pool": pool},
            message="等待同步股票池缺失数据",
        )
        if created:
            self.audit.record(
                "stock_pool_sync_requested",
                actor=actor,
                object_type="stock_pool",
                object_id=pool_id,
                details={"pool_id": pool_id, "revision": expected_revision, "change_count": 0},
            )
            self.executor.submit(
                self._run,
                task["task_id"],
                tuple(pool["resolved_symbols"]),
                as_of,
            )
        return self.repository.get(task["task_id"])

    def _run(self, task_id: str, symbols: tuple[str, ...], as_of: date) -> None:
        owner_id = f"stock-pool-worker-{uuid4().hex}"
        try:
            self.sync_service.run(
                task_id=task_id,
                as_of=as_of,
                owner_id=owner_id,
                symbols=symbols,
            )
        except Exception:  # noqa: BLE001 - fenced terminal state is persisted by the service
            logger.exception("stock pool synchronization task %s failed", task_id)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
