from __future__ import annotations

import json
from datetime import UTC, datetime
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
                [task_id, task_type, message, json.dumps(spec or {}, ensure_ascii=False, default=str)],
            )
        return self.get(task_id)

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

    def recover_interrupted(self) -> int:
        if not self.database.path.is_file():
            return 0
        self.initialize()
        with self.lock, self.database.connect() as connection:
            rows = connection.execute(
                """
                UPDATE operation_task
                SET status = 'failed',
                    message = '服务重启，任务已中断',
                    error = 'server restarted before the task completed',
                    completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status IN ('queued', 'running')
                RETURNING task_id
                """
            ).fetchall()
        return len(rows)

    @staticmethod
    def _decode(row: tuple[Any, ...]) -> dict[str, Any]:
        payload = dict(zip(TASK_COLUMNS, row, strict=True))
        payload["spec"] = json.loads(payload.pop("spec_json"))
        result = payload.pop("result_json")
        if result is not None:
            payload["result"] = json.loads(result)
        for field in ("created_at", "started_at", "completed_at", "updated_at"):
            payload[field] = _iso(payload.get(field))
        return payload
