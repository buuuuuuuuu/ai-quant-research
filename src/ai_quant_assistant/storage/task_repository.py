from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any
from uuid import uuid4

from ai_quant_assistant.storage.database import LocalDatabase

TASK_COLUMNS = (
    "task_id",
    "task_type",
    "status",
    "progress",
    "message",
    "spec_json",
    "result_json",
    "run_id",
    "error",
    "created_at",
    "started_at",
    "completed_at",
    "updated_at",
    "claim_key",
    "owner_id",
    "heartbeat_at",
)
MUTABLE_COLUMNS = {
    "status",
    "progress",
    "message",
    "result_json",
    "run_id",
    "error",
    "started_at",
    "completed_at",
}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return str(value)


class OperationTaskRepository:
    def __init__(self, database: LocalDatabase) -> None:
        self.database = database
        self.lock = RLock()

    def initialize(self) -> None:
        with self.lock:
            self.database.initialize()

    def create(
        self,
        task_type: str,
        *,
        spec: dict[str, Any] | None = None,
        message: str = "等待执行",
    ) -> dict[str, Any]:
        self.initialize()
        task_id = f"task-{uuid4().hex}"
        with self.lock, self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO operation_task (task_id, task_type, status, progress, message, spec_json)
                VALUES (?, ?, 'queued', 0, ?, ?)
                """,
                [
                    task_id,
                    task_type,
                    message,
                    json.dumps(spec or {}, ensure_ascii=False, default=str),
                ],
            )
        return self.get(task_id)

    def create_or_get_active(
        self,
        task_type: str,
        *,
        claim_key: str,
        spec: dict[str, Any],
        message: str,
        stale_after_seconds: float = 1_800,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically claim a deduplicated task or return the live owner.

        The database-level unique claim and LocalDatabase inter-process access
        lock make this safe across Uvicorn workers. Successful tasks retain the
        claim for idempotency; failed and expired tasks release it for retry.
        """

        if not claim_key.strip():
            raise ValueError("task claim key must not be empty")
        self.initialize()
        task_id = f"task-{uuid4().hex}"
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=max(1.0, stale_after_seconds))
        with self.lock, self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {', '.join(TASK_COLUMNS)} FROM operation_task WHERE claim_key = ?",
                [claim_key],
            ).fetchone()
            if row is not None:
                status = str(row[TASK_COLUMNS.index("status")])
                if status == "succeeded":
                    task = self._decode(row)
                    task["reused"] = True
                    return task, False
                if status not in {"queued", "running"}:
                    connection.execute(
                        "UPDATE operation_task SET claim_key = NULL WHERE task_id = ?",
                        [row[0]],
                    )
                    row = None
            if row is not None:
                lease_time = row[TASK_COLUMNS.index("heartbeat_at")]
                lease_time = lease_time or row[TASK_COLUMNS.index("started_at")]
                lease_time = lease_time or row[TASK_COLUMNS.index("updated_at")]
                if lease_time is not None and lease_time.tzinfo is None:
                    lease_time = lease_time.replace(tzinfo=UTC)
                if lease_time is None or lease_time <= stale_before:
                    connection.execute(
                        """
                        UPDATE operation_task
                        SET status = 'failed', progress = LEAST(progress, 99),
                            message = '任务租约超时，可安全重试',
                            error = 'task lease expired before completion',
                            result_json = NULL, completed_at = ?, updated_at = ?,
                            claim_key = NULL
                        WHERE task_id = ? AND claim_key = ?
                        """,
                        [now, now, row[0], claim_key],
                    )
                else:
                    task = self._decode(row)
                    task["reused"] = True
                    return task, False
            connection.execute(
                """
                INSERT INTO operation_task (
                    task_id, task_type, status, progress, message, spec_json, claim_key
                ) VALUES (?, ?, 'queued', 0, ?, ?, ?)
                """,
                [
                    task_id,
                    task_type,
                    message,
                    json.dumps(spec, ensure_ascii=False, default=str),
                    claim_key,
                ],
            )
        task = self.get(task_id)
        task["reused"] = False
        return task, True

    def start(self, task_id: str, *, owner_id: str, message: str) -> bool:
        now = datetime.now(UTC)
        with self.lock, self.database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE operation_task
                SET status = 'running', progress = GREATEST(progress, 1), message = ?,
                    started_at = COALESCE(started_at, ?), completed_at = NULL,
                    result_json = NULL, error = NULL, owner_id = ?, heartbeat_at = ?,
                    updated_at = ?
                WHERE task_id = ? AND status = 'queued'
                RETURNING task_id
                """,
                [message, now, owner_id, now, now, task_id],
            ).fetchone()
        return updated is not None

    def progress(self, task_id: str, *, owner_id: str, progress: int, message: str) -> bool:
        now = datetime.now(UTC)
        bounded = max(1, min(int(progress), 99))
        with self.lock, self.database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE operation_task
                SET progress = GREATEST(progress, ?), message = ?, heartbeat_at = ?,
                    updated_at = ?
                WHERE task_id = ? AND status = 'running' AND owner_id = ?
                RETURNING task_id
                """,
                [bounded, message, now, now, task_id, owner_id],
            ).fetchone()
        return updated is not None

    def succeed(
        self,
        task_id: str,
        *,
        owner_id: str,
        message: str,
        result: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> bool:
        now = datetime.now(UTC)
        with self.lock, self.database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE operation_task
                SET status = 'succeeded', progress = 100, message = ?, result_json = ?,
                    run_id = ?, error = NULL, completed_at = ?, heartbeat_at = ?,
                    updated_at = ?
                WHERE task_id = ? AND status = 'running' AND owner_id = ?
                RETURNING task_id
                """,
                [
                    message,
                    json.dumps(result, ensure_ascii=False, default=str)
                    if result is not None
                    else None,
                    run_id,
                    now,
                    now,
                    now,
                    task_id,
                    owner_id,
                ],
            ).fetchone()
        return updated is not None

    def fail(self, task_id: str, *, owner_id: str, message: str, error: str) -> bool:
        now = datetime.now(UTC)
        with self.lock, self.database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE operation_task
                SET status = 'failed', progress = LEAST(progress, 99), message = ?,
                    result_json = NULL, error = ?, completed_at = ?, heartbeat_at = ?,
                    updated_at = ?, claim_key = NULL
                WHERE task_id = ? AND status = 'running' AND owner_id = ?
                RETURNING task_id
                """,
                [message, error, now, now, now, task_id, owner_id],
            ).fetchone()
        return updated is not None

    def update(self, task_id: str, **changes: Any) -> dict[str, Any]:
        if not changes:
            return self.get(task_id)
        unexpected = set(changes) - MUTABLE_COLUMNS
        if unexpected:
            raise ValueError(f"unsupported task fields: {sorted(unexpected)}")
        serialized = dict(changes)
        if "result_json" in serialized and not isinstance(serialized["result_json"], str):
            serialized["result_json"] = json.dumps(
                serialized["result_json"], ensure_ascii=False, default=str
            )
        assignments = ", ".join(f"{column} = ?" for column in serialized)
        with self.lock, self.database.connect() as connection:
            updated = connection.execute(
                f"""
                UPDATE operation_task
                SET {assignments}, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                RETURNING task_id
                """,
                [*serialized.values(), task_id],
            ).fetchone()
        if updated is None:
            raise KeyError(task_id)
        return self.get(task_id)

    def get(self, task_id: str) -> dict[str, Any]:
        if not self.database.path.is_file():
            raise KeyError(task_id)
        with self.lock, self.database.connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT {', '.join(TASK_COLUMNS)} FROM operation_task WHERE task_id = ?",
                [task_id],
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._decode(row)

    def list(self, *, task_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not self.database.path.is_file():
            return []
        query = f"SELECT {', '.join(TASK_COLUMNS)} FROM operation_task"
        parameters: list[Any] = []
        if task_type is not None:
            query += " WHERE task_type = ?"
            parameters.append(task_type)
        query += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(max(1, min(limit, 500)))
        with self.lock, self.database.connect(read_only=True) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._decode(row) for row in rows]

    def recover_interrupted(self, *, stale_after_seconds: float = 1_800) -> int:
        if not self.database.path.is_file():
            return 0
        self.initialize()
        stale_before = datetime.now(UTC) - timedelta(seconds=max(0.0, stale_after_seconds))
        with self.lock, self.database.connect() as connection:
            rows = connection.execute(
                """
                UPDATE operation_task
                SET status = 'failed',
                    progress = LEAST(progress, 99),
                    message = '服务重启，任务已中断',
                    error = 'server restarted before the task completed',
                    result_json = NULL,
                    completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP,
                    claim_key = NULL
                WHERE status IN ('queued', 'running')
                  AND COALESCE(heartbeat_at, started_at, updated_at, created_at) <= ?
                RETURNING task_id
                """,
                [stale_before],
            ).fetchall()
        return len(rows)

    @staticmethod
    def _decode(row: tuple[Any, ...]) -> dict[str, Any]:
        payload = dict(zip(TASK_COLUMNS, row, strict=True))
        payload["spec"] = json.loads(payload.pop("spec_json"))
        result = payload.pop("result_json")
        if result is not None:
            payload["result"] = json.loads(result)
        for field in ("created_at", "started_at", "completed_at", "updated_at", "heartbeat_at"):
            payload[field] = _iso(payload.get(field))
        return payload
