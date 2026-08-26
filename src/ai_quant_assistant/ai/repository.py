from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ai_quant_assistant.storage.database import LocalDatabase

JSON_FIELDS = {
    "spec_json": "spec",
    "assumptions_json": "assumptions",
    "warnings_json": "warnings",
    "preflight_json": "preflight",
    "result_json": "result",
    "explanation_json": "explanation",
}
PLAN_COLUMNS = (
    "plan_id",
    "conversation_id",
    "revision",
    "status",
    "question",
    "tool_name",
    "title",
    "rationale",
    *JSON_FIELDS,
    "planner",
    "model_name",
    "model_response_id",
    "prompt_version",
    "task_id",
    "run_id",
    "error",
    "created_at",
    "confirmed_at",
    "started_at",
    "completed_at",
    "updated_at",
)
MUTABLE_PLAN_FIELDS = {
    "status",
    "preflight_json",
    "task_id",
    "run_id",
    "result_json",
    "explanation_json",
    "error",
    "confirmed_at",
    "started_at",
    "completed_at",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    return str(value)


class CopilotRepository:
    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    def initialize(self) -> None:
        self.database.initialize()

    def create_conversation(self, title: str = "新研究对话") -> dict[str, Any]:
        self.initialize()
        conversation_id = f"conversation-{uuid4().hex}"
        clean_title = title.strip()[:120] or "新研究对话"
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO copilot_conversation (conversation_id, title) VALUES (?, ?)",
                [conversation_id, clean_title],
            )
        return self.get_conversation(conversation_id)

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        if not self.database.path.is_file():
            raise KeyError(conversation_id)
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT conversation_id, title, status, created_at, updated_at
                FROM copilot_conversation WHERE conversation_id = ?
                """,
                [conversation_id],
            ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return {
            "conversation_id": row[0],
            "title": row[1],
            "status": row[2],
            "created_at": _iso(row[3]),
            "updated_at": _iso(row[4]),
        }

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.database.path.is_file():
            return []
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT conversation_id, title, status, created_at, updated_at
                FROM copilot_conversation ORDER BY updated_at DESC LIMIT ?
                """,
                [max(1, min(limit, 200))],
            ).fetchall()
        return [
            {
                "conversation_id": row[0],
                "title": row[1],
                "status": row[2],
                "created_at": _iso(row[3]),
                "updated_at": _iso(row[4]),
            }
            for row in rows
        ]

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        plan_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.get_conversation(conversation_id)
        message_id = f"message-{uuid4().hex}"
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO copilot_message
                    (message_id, conversation_id, role, content, plan_id, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [message_id, conversation_id, role, content, plan_id, _json(metadata or {})],
            )
            connection.execute(
                """
                UPDATE copilot_conversation SET updated_at = CURRENT_TIMESTAMP,
                    title = CASE WHEN title = '新研究对话' AND ? = 'user'
                                 THEN LEFT(?, 120) ELSE title END
                WHERE conversation_id = ?
                """,
                [role, content, conversation_id],
            )
        return {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "plan_id": plan_id,
            "metadata": metadata or {},
        }

    def list_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        self.get_conversation(conversation_id)
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT message_id, role, content, plan_id, metadata_json, created_at
                FROM copilot_message WHERE conversation_id = ? ORDER BY created_at, message_id
                """,
                [conversation_id],
            ).fetchall()
        return [
            {
                "message_id": row[0],
                "role": row[1],
                "content": row[2],
                "plan_id": row[3],
                "metadata": json.loads(row[4]),
                "created_at": _iso(row[5]),
            }
            for row in rows
        ]

    def create_plan(
        self, conversation_id: str, question: str, draft: dict[str, Any]
    ) -> dict[str, Any]:
        plan_id = f"plan-{uuid4().hex}"
        status = "blocked" if draft.get("blocked") else "draft"
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO research_plan (
                    plan_id, conversation_id, status, question, tool_name, title, rationale,
                    spec_json, assumptions_json, warnings_json, preflight_json, planner,
                    model_name, model_response_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    plan_id,
                    conversation_id,
                    status,
                    question,
                    draft.get("tool_name"),
                    draft.get("title", "研究计划"),
                    draft.get("rationale", ""),
                    _json(draft.get("spec", {})),
                    _json(draft.get("assumptions", [])),
                    _json(draft.get("warnings", [])),
                    _json(draft.get("preflight", {})),
                    draft.get("planner", "rules"),
                    draft.get("model_name"),
                    draft.get("model_response_id"),
                ],
            )
        self.add_event(plan_id, "created", "copilot", {"status": status})
        return self.get_plan(plan_id)

    def add_event(self, plan_id: str, event_type: str, actor: str, details: Any = None) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO research_plan_event
                    (event_id, plan_id, event_type, actor, details_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [f"plan-event-{uuid4().hex}", plan_id, event_type, actor, _json(details or {})],
            )

    def update_plan(self, plan_id: str, **changes: Any) -> dict[str, Any]:
        unexpected = set(changes) - MUTABLE_PLAN_FIELDS
        if unexpected:
            raise ValueError(f"unsupported plan fields: {sorted(unexpected)}")
        serialized = dict(changes)
        for field in ("preflight_json", "result_json", "explanation_json"):
            if field in serialized and not isinstance(serialized[field], str):
                serialized[field] = _json(serialized[field])
        assignments = ", ".join(f"{field} = ?" for field in serialized)
        with self.database.connect() as connection:
            row = connection.execute(
                f"""
                UPDATE research_plan SET {assignments}, updated_at = CURRENT_TIMESTAMP
                WHERE plan_id = ? RETURNING plan_id
                """,
                [*serialized.values(), plan_id],
            ).fetchone()
        if row is None:
            raise KeyError(plan_id)
        return self.get_plan(plan_id)

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        if not self.database.path.is_file():
            raise KeyError(plan_id)
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT {', '.join(PLAN_COLUMNS)} FROM research_plan WHERE plan_id = ?",
                [plan_id],
            ).fetchone()
            events = connection.execute(
                """
                SELECT event_type, actor, details_json, created_at
                FROM research_plan_event WHERE plan_id = ? ORDER BY created_at, event_id
                """,
                [plan_id],
            ).fetchall()
        if row is None:
            raise KeyError(plan_id)
        payload = dict(zip(PLAN_COLUMNS, row, strict=True))
        for storage_name, output_name in JSON_FIELDS.items():
            value = payload.pop(storage_name)
            payload[output_name] = json.loads(value) if value else None
        for field in ("created_at", "confirmed_at", "started_at", "completed_at", "updated_at"):
            payload[field] = _iso(payload.get(field))
        payload["events"] = [
            {
                "event_type": event[0],
                "actor": event[1],
                "details": json.loads(event[2]),
                "created_at": _iso(event[3]),
            }
            for event in events
        ]
        return payload
