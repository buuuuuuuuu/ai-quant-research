from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import Any
from uuid import uuid4

import pandas as pd

from ai_quant_assistant.config import Settings
from ai_quant_assistant.providers.akshare_provider import AkShareProvider
from ai_quant_assistant.providers.normalization import exchange_for_symbol, security_id_for_symbol
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.monitoring import OperationAuditRepository
from ai_quant_assistant.storage.operations import ExclusiveOperationLock
from ai_quant_assistant.storage.sync_repository import SyncRepository
from ai_quant_assistant.storage.task_repository import OperationTaskRepository

logger = logging.getLogger(__name__)
CLAIM_KEY = "security-master-refresh"
LEASE_SECONDS = 1800


class SecurityMasterRefreshService:
    """Explicit directory-only refresh; network outside locks, publication fenced.

    Reuse the existing sync task type with spec.dataset=security_master. A success
    releases the active claim so a later user request can fetch a fresh directory.
    No market/calendar/financial/document service is called here.
    """

    def __init__(
        self,
        database: LocalDatabase,
        tasks: OperationTaskRepository,
        operation_lock: ExclusiveOperationLock,
        provider_factory: Callable[[str], Any],
    ) -> None:
        self.database = database
        self.tasks = tasks
        self.operation_lock = operation_lock
        self.provider_factory = provider_factory
        self.heartbeat_seconds = 15.0

    @classmethod
    def from_settings(
        cls,
        database: LocalDatabase,
        settings: Settings,
        tasks: OperationTaskRepository | None = None,
        operation_lock: ExclusiveOperationLock | None = None,
    ) -> SecurityMasterRefreshService:
        return cls(
            database,
            tasks or OperationTaskRepository(database),
            operation_lock or ExclusiveOperationLock(database.layout.root / ".operations.lock"),
            # Separate per-attempt caches avoid races with ordinary sync or an
            # expired worker and prevent a stale cache from masquerading as fresh.
            lambda task_id: AkShareProvider(
                cache_dir=settings.cache_dir / "security-master-refresh" / task_id,
                retries=settings.akshare_retries,
                timeout_seconds=settings.akshare_timeout_seconds,
                refresh=True,
            ),
        )

    def prepare(self, *, actor: str) -> tuple[dict[str, Any], bool]:
        task, created = self.tasks.create_or_get_active(
            "sync",
            claim_key=CLAIM_KEY,
            spec={"dataset": "security_master", "actor": actor, "confirmed": True},
            message="等待更新证券主数据（不下载历史行情）",
            stale_after_seconds=LEASE_SECONDS,
        )
        if created:
            try:
                OperationAuditRepository(self.database).record(
                    "security_master_refresh_requested",
                    actor=actor,
                    object_type="operation_task",
                    object_id=task["task_id"],
                    details={"dataset": "security_master", "confirmed": True},
                )
            except Exception:
                owner = f"refresh-submit-{uuid4().hex}"
                if self.tasks.start(task["task_id"], owner_id=owner, message="提交失败"):
                    self.tasks.fail(
                        task["task_id"],
                        owner_id=owner,
                        message="证券主数据任务审计失败，可重试",
                        error="audit failed",
                    )
                raise
        return task, created

    @staticmethod
    def validate(frame: pd.DataFrame) -> pd.DataFrame:
        columns = ["security_id", "symbol", "exchange", "name", "source"]
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise ValueError("证券目录为空，未修改已有主数据")
        if frame.attrs.get("cache_status") == "stale_fallback":
            raise ValueError("证券目录刷新失败：拒绝使用过期缓存")
        if len(frame) > 20000 or not set(columns).issubset(frame.columns):
            raise ValueError("证券目录字段或数量异常，未修改已有主数据")
        result = frame[columns].copy()
        for column in columns:
            if (
                result[column].isna().any()
                or not result[column].map(lambda x: isinstance(x, str)).all()
            ):
                raise ValueError(f"证券目录 {column} 必须是非空字符串")
            result[column] = result[column].str.strip()
        for row in result.itertuples(index=False):
            if not re.fullmatch(r"[0-9]{6}", row.symbol):
                raise ValueError(f"证券目录包含无效代码：{row.symbol[:120]}")
            if row.exchange != exchange_for_symbol(
                row.symbol
            ) or row.security_id != security_id_for_symbol(row.symbol):
                raise ValueError(f"证券目录身份冲突：{row.symbol}")
            if not row.name or row.name == row.symbol or len(row.name) > 120:
                raise ValueError(f"证券目录缺少真实名称或名称过长：{row.symbol}")
            if not row.source or len(row.source) > 240 or row.source == "requested-symbol-fallback":
                raise ValueError(f"证券目录缺少可信来源：{row.symbol}")
        if result["symbol"].duplicated().any() or result["security_id"].duplicated().any():
            raise ValueError("证券目录包含重复证券，未修改已有主数据")
        return result.sort_values("security_id").reset_index(drop=True)

    def run(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if task["spec"].get("dataset") != "security_master":
            raise ValueError("not a security master refresh task")
        owner_id = f"security-master-worker-{uuid4().hex}"
        if not self.tasks.start(
            task_id, owner_id=owner_id, message="正在获取证券目录，不下载历史行情"
        ):
            return self.tasks.get(task_id)
        stop, lost = Event(), Event()

        def heartbeat() -> None:
            while not stop.wait(self.heartbeat_seconds):
                try:
                    if self.tasks.progress(
                        task_id, owner_id=owner_id, progress=10, message="正在获取或校验证券目录"
                    ):
                        continue
                except Exception:
                    logger.exception("security directory heartbeat failed: %s", task_id)
                lost.set()
                return

        thread = Thread(target=heartbeat, daemon=True, name=f"heartbeat-{task_id}")
        thread.start()
        try:
            frame = self.validate(self.provider_factory(task_id).get_security_master())
            if lost.is_set():
                raise RuntimeError("证券目录任务已失去租约，禁止写入")
            self._publish(frame, task_id=task_id, owner_id=owner_id, actor=task["spec"]["actor"])
        except Exception as exc:
            self.tasks.fail(
                task_id,
                owner_id=owner_id,
                message="证券主数据更新失败，可安全重试",
                error=str(exc)[:2000],
            )
            logger.exception("security directory refresh failed: %s", task_id)
        finally:
            stop.set()
            thread.join(timeout=1)
        return self.tasks.get(task_id)

    def _publish(self, frame: pd.DataFrame, *, task_id: str, owner_id: str, actor: str) -> None:
        canonical = json.dumps(
            frame.to_dict("records"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        batch_id = f"security-master-{task_id}"
        # Existing lock order: operation -> DB access. Only publication takes
        # these locks; never call tasks.* (its own lock) inside this transaction.
        with self.operation_lock.hold(), self.database.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                owned = connection.execute(
                    """SELECT task_id FROM operation_task
                       WHERE task_id = ? AND task_type = 'sync' AND status = 'running'
                         AND owner_id = ? AND claim_key = ? AND heartbeat_at > ?""",
                    [
                        task_id,
                        owner_id,
                        CLAIM_KEY,
                        datetime.now(UTC) - timedelta(seconds=LEASE_SECONDS),
                    ],
                ).fetchone()
                if owned is None:
                    raise RuntimeError("证券目录任务已失去租约，禁止写入")
                existing = {
                    row[0]
                    for row in connection.execute(
                        "SELECT security_id FROM security_master"
                    ).fetchall()
                }
                added = sum(item not in existing for item in frame["security_id"])
                SyncRepository(self.database).upsert_security_master(
                    frame,
                    batch_id,
                    connection=connection,
                    preserve_listing_state=True,
                )
                result = {
                    "dataset": "security_master",
                    "batch_id": batch_id,
                    "row_count": len(frame),
                    "added_count": added,
                    "updated_count": len(frame) - added,
                    "sha256": digest,
                    "provider_sources": sorted(set(frame["source"])),
                    "warning": "仅更新当前证券目录；未返回证券及已知停牌/退市状态保留，不代表历史成分。",
                }
                encoded = json.dumps(result, ensure_ascii=False)
                connection.execute(
                    """INSERT INTO sync_batch (batch_id, dataset, status, started_at, ended_at,
                           request_json, row_count)
                       VALUES (?, 'security_master', 'published', CURRENT_TIMESTAMP,
                               CURRENT_TIMESTAMP, ?, ?)""",
                    [batch_id, encoded, len(frame)],
                )
                connection.execute(
                    """INSERT INTO operation_audit
                       (event_id, action, actor, outcome, object_type, object_id, details_json)
                       VALUES (?, 'security_master_refreshed', ?, 'success', 'operation_task', ?, ?)""",
                    [f"audit-{uuid4().hex}", actor, task_id, encoded],
                )
                connection.execute(
                    """UPDATE operation_task SET status = 'succeeded', progress = 100,
                       message = '证券主数据更新完成；请重新搜索并添加成员', result_json = ?,
                       error = NULL, completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP,
                       heartbeat_at = CURRENT_TIMESTAMP, claim_key = NULL
                       WHERE task_id = ? AND owner_id = ? AND status = 'running'""",
                    [encoded, task_id, owner_id],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
