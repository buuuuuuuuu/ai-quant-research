from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pandas as pd

from ai_quant_assistant.domain.models import EventStudySpec
from ai_quant_assistant.providers.base import MarketDataProvider
from ai_quant_assistant.research.event_study import EventStudyEngine
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.task_repository import OperationTaskRepository

EVENT_EXTRACTOR = "rules"
EVENT_EXTRACTOR_VERSION = "m8b-rules-v1"
EVENT_EXTRACTION_LEASE_SECONDS = 1_800
EVENT_TYPES = {
    "performance_forecast": ("业绩预告", "业绩快报", "预计实现", "业绩预计"),
    "dividend": ("分红", "派息", "派发现金", "利润分配"),
    "share_repurchase": ("股份回购", "回购股份", "回购方案", "实施回购"),
    "major_contract": ("重大合同", "签订合同", "中标通知", "项目中标"),
    "shareholder_change": ("股东增持", "股东减持", "增持股份", "减持股份"),
}


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _records(cursor: Any) -> list[dict[str, Any]]:
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {key: _json_value(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


class DocumentEventService:
    """Extract reviewable facts with page-level, verbatim evidence."""

    def __init__(self, database: LocalDatabase) -> None:
        self.database = database
        self.database.initialize()

    def _document_context(self, document_id: str) -> dict[str, Any]:
        with self.database.connect(read_only=True) as connection:
            rows = _records(
                connection.execute(
                    """
                    SELECT document.document_id, document.announcement_id, document.status,
                           document.version_no, document.downloaded_at,
                           document.active_event_extraction_id,
                           announcement.security_id, announcement.announcement_date,
                           announcement.published_at,
                           announcement.title, security.symbol, security.name
                    FROM announcement_document AS document
                    JOIN published_announcement AS announcement USING (announcement_id)
                    JOIN security_master AS security USING (security_id)
                    WHERE document.document_id = ?
                    """,
                    [document_id],
                )
            )
        if not rows:
            raise KeyError(document_id)
        return rows[0]

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [item.strip() for item in re.split(r"(?<=[。！？；])|\n+", text) if item.strip()]

    @staticmethod
    def _number(text: str) -> tuple[float | None, str | None]:
        match = re.search(r"(?:人民币)?\s*([0-9]+(?:\.[0-9]+)?)\s*(亿元|万元|元)", text)
        if match is None:
            return None, None
        scale = {"亿元": 100_000_000.0, "万元": 10_000.0, "元": 1.0}[match.group(2)]
        return float(match.group(1)) * scale, "CNY"

    @staticmethod
    def _ratio(text: str) -> float | None:
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", text)
        return float(match.group(1)) / 100.0 if match else None

    @staticmethod
    def _event_date(text: str) -> str | None:
        match = re.search(
            r"(?:签订日期|实施日期|生效日期|权益登记日|回购日期)[为：:\s]*"
            r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?",
            text,
        )
        if match is None:
            return None
        try:
            return date(*(int(item) for item in match.groups())).isoformat()
        except ValueError:
            return None

    @staticmethod
    def _direction(event_type: str, text: str) -> str:
        if event_type == "shareholder_change":
            has_increase = "增持" in text
            has_decrease = "减持" in text
            if has_increase and has_decrease:
                return "mixed"
            if has_increase:
                return "positive"
            if has_decrease:
                return "negative"
        return "unknown"

    def _extract_rules(self, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for event_type, keywords in EVENT_TYPES.items():
            match: tuple[dict[str, Any], str] | None = None
            for chunk in chunks:
                for sentence in self._sentences(str(chunk["text"])):
                    if any(keyword in sentence for keyword in keywords):
                        match = (chunk, sentence[:1_000])
                        break
                if match is not None:
                    break
            if match is None:
                continue
            chunk, quote = match
            amount, currency = self._number(quote)
            if event_type == "dividend" and "每10股" in quote:
                amount, currency = None, None
            ratio = self._ratio(quote)
            event_date = self._event_date(quote)
            direction = self._direction(event_type, quote)
            payload: dict[str, Any] = {"event_type": event_type}
            evidence_fields = ["event_type"]
            for field, value in (
                ("event_date", event_date),
                ("direction", direction if direction != "unknown" else None),
                ("amount", amount),
                ("currency", currency),
                ("ratio", ratio),
            ):
                if value is not None:
                    payload[field] = value
                    evidence_fields.append(field)
            facts.append(
                {
                    "event_type": event_type,
                    "event_date": event_date,
                    "direction": direction,
                    "amount": amount,
                    "currency": currency,
                    "ratio": ratio,
                    "structured_payload": payload,
                    "evidence": [
                        {
                            "field_name": field,
                            "document_id": chunk["document_id"],
                            "page_number": int(chunk["page_number"]),
                            "chunk_id": chunk["chunk_id"],
                            "quote": quote,
                        }
                        for field in evidence_fields
                    ],
                }
            )
        return facts

    @staticmethod
    def _event_id(document_id: str, event_type: str) -> str:
        key = f"{document_id}|{event_type}|{EVENT_EXTRACTOR_VERSION}".encode()
        return f"event-{hashlib.sha256(key).hexdigest()[:32]}"

    @staticmethod
    def _validate_fact(
        fact: dict[str, Any], chunks_by_id: dict[str, dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        valid: list[dict[str, Any]] = []
        claimed_fields = {"event_type"}
        claimed_fields.update(
            field
            for field in ("event_date", "direction", "amount", "currency", "ratio")
            if fact.get(field) not in {None, "unknown"}
        )
        for evidence in fact.get("evidence", []):
            chunk = chunks_by_id.get(str(evidence.get("chunk_id")))
            quote = str(evidence.get("quote") or "").strip()
            if (
                chunk is None
                or not quote
                or quote not in str(chunk["text"])
                or evidence.get("document_id") != chunk["document_id"]
                or int(evidence.get("page_number", 0)) != int(chunk["page_number"])
            ):
                continue
            valid.append(
                {
                    **evidence,
                    "quote": quote,
                    "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                }
            )
        evidenced_fields = {str(item["field_name"]) for item in valid}
        return (
            "needs_review" if claimed_fields.issubset(evidenced_fields) else "insufficient",
            valid,
        )

    def extract(
        self,
        document_id: str,
        *,
        progress: Callable[[int, str], None] | None = None,
    ) -> dict[str, Any]:
        emit = progress or (lambda _value, _message: None)
        context = self._document_context(document_id)
        if context["status"] != "ready":
            raise ValueError("document must be ready before event extraction")
        extraction_id = f"event-extract-{uuid4().hex}"
        started_at = _now()
        with self.database.connect() as connection:
            active = connection.execute(
                "SELECT active_event_extraction_id FROM announcement_document WHERE document_id = ?",
                [document_id],
            ).fetchone()[0]
            if active:
                lease = connection.execute(
                    """
                    SELECT COALESCE(heartbeat_at, started_at)
                    FROM document_event_extraction WHERE extraction_id = ?
                    """,
                    [active],
                ).fetchone()
                lease_at = lease[0] if lease else None
                if lease_at is not None and lease_at.tzinfo is None:
                    lease_at = lease_at.replace(tzinfo=UTC)
                if (
                    lease_at
                    and (started_at - lease_at).total_seconds() < EVENT_EXTRACTION_LEASE_SECONDS
                ):
                    raise RuntimeError("document event extraction is already in progress")
                connection.execute(
                    """
                    UPDATE document_event_extraction
                    SET status = 'failed', error = 'extraction lease expired',
                        completed_at = ?, heartbeat_at = ?
                    WHERE extraction_id = ? AND status = 'running'
                    """,
                    [started_at, started_at, active],
                )
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.execute(
                    """
                    UPDATE announcement_document SET active_event_extraction_id = ?
                    WHERE document_id = ?
                    """,
                    [extraction_id, document_id],
                )
                connection.execute(
                    """
                    INSERT INTO document_event_extraction (
                        extraction_id, document_id, extractor, extractor_version,
                        status, heartbeat_at
                    ) VALUES (?, ?, ?, ?, 'running', ?)
                    """,
                    [
                        extraction_id,
                        document_id,
                        EVENT_EXTRACTOR,
                        EVENT_EXTRACTOR_VERSION,
                        started_at,
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        try:
            emit(20, "读取已解析正文")
            with self.database.connect(read_only=True) as connection:
                chunks = _records(
                    connection.execute(
                        """
                        SELECT chunk_id, document_id, page_number, chunk_index, text
                        FROM document_chunk WHERE document_id = ?
                        ORDER BY page_number, chunk_index
                        """,
                        [document_id],
                    )
                )
            emit(50, "离线提取结构化事件")
            facts = self._extract_rules(chunks)
            chunks_by_id = {str(item["chunk_id"]): item for item in chunks}
            validated = [(*self._validate_fact(fact, chunks_by_id), fact) for fact in facts]
            published_at = _as_utc(context["published_at"])
            known_at = published_at
            if int(context["version_no"]) > 1:
                known_at = max(published_at, _as_utc(context["downloaded_at"]))
            emit(80, "校验逐字引用并保存")
            with self.database.connect() as connection:
                connection.execute("BEGIN TRANSACTION")
                try:
                    active = connection.execute(
                        """
                        SELECT active_event_extraction_id FROM announcement_document
                        WHERE document_id = ?
                        """,
                        [document_id],
                    ).fetchone()[0]
                    if active != extraction_id:
                        raise RuntimeError("document event extraction lease is no longer owned")
                    for status, evidence, fact in validated:
                        event_id = self._event_id(document_id, str(fact["event_type"]))
                        inserted = connection.execute(
                            """
                            INSERT INTO document_event (
                                event_id, extraction_id, announcement_id, document_id,
                                security_id, event_type, published_at, known_at, event_date,
                                direction, amount, currency, ratio, structured_payload_json,
                                extractor, extractor_version, status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (document_id, event_type, extractor_version) DO NOTHING
                            RETURNING event_id
                            """,
                            [
                                event_id,
                                extraction_id,
                                context["announcement_id"],
                                document_id,
                                context["security_id"],
                                fact["event_type"],
                                published_at,
                                known_at,
                                fact.get("event_date"),
                                fact.get("direction", "unknown"),
                                fact.get("amount"),
                                fact.get("currency"),
                                fact.get("ratio"),
                                json.dumps(fact.get("structured_payload", {}), ensure_ascii=False),
                                EVENT_EXTRACTOR,
                                EVENT_EXTRACTOR_VERSION,
                                status,
                            ],
                        ).fetchone()
                        if inserted is None:
                            stored = connection.execute(
                                """
                                SELECT event_id FROM document_event
                                WHERE document_id = ? AND event_type = ?
                                  AND extractor_version = ?
                                """,
                                [document_id, fact["event_type"], EVENT_EXTRACTOR_VERSION],
                            ).fetchone()
                            if stored is None or str(stored[0]) != event_id:
                                raise RuntimeError(
                                    "document event business key conflicts with a different event_id"
                                )
                        for order, item in enumerate(evidence, start=1):
                            connection.execute(
                                """
                                INSERT INTO document_event_evidence (
                                    event_id, evidence_order, field_name, document_id,
                                    page_number, chunk_id, quote, quote_sha256
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT (event_id, evidence_order) DO NOTHING
                                """,
                                [
                                    event_id,
                                    order,
                                    item["field_name"],
                                    document_id,
                                    item["page_number"],
                                    item["chunk_id"],
                                    item["quote"],
                                    item["quote_sha256"],
                                ],
                            )
                    completed_at = _now()
                    connection.execute(
                        """
                        UPDATE document_event_extraction
                        SET status = 'succeeded', completed_at = ?, heartbeat_at = ?, error = NULL
                        WHERE extraction_id = ? AND status = 'running'
                        """,
                        [completed_at, completed_at, extraction_id],
                    )
                    connection.execute(
                        """
                        UPDATE announcement_document SET active_event_extraction_id = NULL
                        WHERE document_id = ? AND active_event_extraction_id = ?
                        """,
                        [document_id, extraction_id],
                    )
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            result = self.list_document_events(document_id)
            emit(100, "结构化事件提取完成")
            return result
        except Exception as exc:
            with self.database.connect() as connection:
                connection.execute(
                    """
                    UPDATE document_event_extraction
                    SET status = 'failed', completed_at = ?, heartbeat_at = ?, error = ?
                    WHERE extraction_id = ? AND status = 'running'
                    """,
                    [_now(), _now(), str(exc), extraction_id],
                )
                connection.execute(
                    """
                    UPDATE announcement_document SET active_event_extraction_id = NULL
                    WHERE document_id = ? AND active_event_extraction_id = ?
                    """,
                    [document_id, extraction_id],
                )
            raise

    @staticmethod
    def _decode_event(row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        payload["structured_payload"] = json.loads(
            str(payload.pop("structured_payload_json") or "{}")
        )
        for key, value in list(payload.items()):
            payload[key] = _json_value(value)
        return payload

    def _evidence(self, event_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not event_ids:
            return {}
        placeholders = ",".join("?" for _ in event_ids)
        with self.database.connect(read_only=True) as connection:
            rows = _records(
                connection.execute(
                    f"""
                    SELECT * FROM document_event_evidence
                    WHERE event_id IN ({placeholders})
                    ORDER BY event_id, evidence_order
                    """,
                    event_ids,
                )
            )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row.pop("event_id")), []).append(
                {key: _json_value(value) for key, value in row.items()}
            )
        return grouped

    def list_document_events(self, document_id: str) -> dict[str, Any]:
        self._document_context(document_id)
        with self.database.connect(read_only=True) as connection:
            rows = _records(
                connection.execute(
                    """
                    SELECT event.*, security.symbol, security.name,
                           announcement.announcement_date
                    FROM document_event AS event
                    JOIN security_master AS security USING (security_id)
                    JOIN published_announcement AS announcement USING (announcement_id)
                    WHERE event.document_id = ?
                    ORDER BY event.created_at, event.event_type
                    """,
                    [document_id],
                )
            )
        evidence = self._evidence([str(row["event_id"]) for row in rows])
        events = []
        for row in rows:
            item = self._decode_event(row)
            item["evidence"] = evidence.get(str(item["event_id"]), [])
            events.append(item)
        return {"document_id": document_id, "events": events}

    def get_event(self, event_id: str) -> dict[str, Any]:
        with self.database.connect(read_only=True) as connection:
            rows = _records(
                connection.execute(
                    """
                    SELECT event.*, security.symbol, security.name, announcement.title,
                           announcement.announcement_date
                    FROM document_event AS event
                    JOIN security_master AS security USING (security_id)
                    JOIN published_announcement AS announcement USING (announcement_id)
                    WHERE event.event_id = ?
                    """,
                    [event_id],
                )
            )
        if not rows:
            raise KeyError(event_id)
        event = self._decode_event(rows[0])
        event["evidence"] = self._evidence([event_id]).get(event_id, [])
        return event

    def list_events(
        self,
        *,
        as_of: datetime,
        symbol: str | None = None,
        event_type: str | None = None,
        status: str = "accepted",
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses = ["event.published_at <= ?", "event.known_at <= ?", "event.status = ?"]
        values: list[Any] = [as_of, as_of, status]
        if symbol:
            clauses.append("security.symbol = ?")
            values.append(symbol)
        if event_type:
            clauses.append("event.event_type = ?")
            values.append(event_type)
        values.append(max(1, min(limit, 200)))
        with self.database.connect(read_only=True) as connection:
            rows = _records(
                connection.execute(
                    f"""
                    SELECT event.*, security.symbol, security.name,
                           announcement.announcement_date
                    FROM document_event AS event
                    JOIN security_master AS security USING (security_id)
                    JOIN published_announcement AS announcement USING (announcement_id)
                    WHERE {" AND ".join(clauses)}
                    ORDER BY event.published_at DESC, event.created_at DESC
                    LIMIT ?
                    """,
                    values,
                )
            )
        decoded = [self._decode_event(row) for row in rows]
        evidence = self._evidence([str(item["event_id"]) for item in decoded])
        for item in decoded:
            item["evidence"] = evidence.get(str(item["event_id"]), [])
        return {
            "as_of": as_of.isoformat(),
            "events": decoded,
            "point_in_time_notice": "仅返回在 as_of 时点已经公开且符合审核状态的事件版本。",
        }

    def review(self, event_id: str, *, status: str, actor: str, note: str = "") -> dict[str, Any]:
        if status not in {"accepted", "rejected"}:
            raise ValueError("review status must be accepted or rejected")
        with self.database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE document_event
                SET status = ?, reviewed_at = ?, reviewed_by = ?, review_note = ?, updated_at = ?
                WHERE event_id = ? AND status = 'needs_review'
                RETURNING event_id
                """,
                [status, _now(), actor, note or None, _now(), event_id],
            ).fetchone()
        if updated is None:
            try:
                current = self.get_event(event_id)
            except KeyError:
                raise
            raise ValueError(
                f"event review requires needs_review status; current status is {current['status']}"
            )
        return self.get_event(event_id)

    def run_event_study(
        self,
        event_id: str,
        *,
        provider: MarketDataProvider,
        as_of: datetime,
        benchmark_symbol: str,
        windows: tuple[int, ...],
        adjustment: str,
        round_trip_cost_bps: float,
    ) -> dict[str, Any]:
        event = self.get_event(event_id)
        published_at = datetime.fromisoformat(str(event["published_at"]))
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        known_at = datetime.fromisoformat(str(event["known_at"]))
        if known_at.tzinfo is None:
            known_at = known_at.replace(tzinfo=UTC)
        if event["status"] != "accepted":
            raise ValueError("only accepted document events can be researched")
        if published_at > as_of or known_at > as_of:
            raise ValueError("event was not public at the requested as_of time")
        research_date = known_at.date()
        start_date = research_date - timedelta(days=10)
        requested_end = research_date + timedelta(days=max(windows) * 3 + 30)
        end_date = min(requested_end, as_of.date())
        spec = EventStudySpec(
            symbol=str(event["symbol"]),
            start_date=start_date,
            end_date=end_date,
            benchmark_symbol=benchmark_symbol,
            windows=windows,
            adjustment=adjustment,
            round_trip_cost_bps=round_trip_cost_bps,
            min_events=1,
        )
        announcements = pd.DataFrame(
            [
                {
                    "announcement_date": research_date,
                    "title": event["title"],
                    "report_year": research_date.year,
                    "symbol": event["symbol"],
                    "url": "",
                    "source_event_id": event_id,
                    "announcement_id": event["announcement_id"],
                    "document_id": event["document_id"],
                }
            ]
        )
        stock = provider.get_stock_prices(str(event["symbol"]), start_date, end_date, adjustment)
        benchmark = provider.get_benchmark_prices(benchmark_symbol, start_date, end_date)
        result = EventStudyEngine().run(spec, announcements, stock, benchmark)
        payload = {
            "status": "succeeded",
            "event_id": event_id,
            "announcement_id": event["announcement_id"],
            "document_id": event["document_id"],
            "as_of": as_of.isoformat(),
            "published_at": published_at.isoformat(),
            "known_at": known_at.isoformat(),
            "spec": spec.to_dict(),
            "events": _frame_records(result.events),
            "summary": _frame_records(result.summary),
            "diagnostics": result.diagnostics,
            "evidence": event["evidence"],
        }
        run_id = f"event-study-{uuid4().hex}"
        payload["run_id"] = run_id
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO document_event_research_run (
                    run_id, event_id, announcement_id, document_id, as_of,
                    spec_json, result_json, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    event_id,
                    event["announcement_id"],
                    event["document_id"],
                    as_of,
                    json.dumps(spec.to_dict(), ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False, default=str),
                    json.dumps(event["evidence"], ensure_ascii=False, default=str),
                ],
            )
        return payload


class DocumentEventTaskManager:
    def __init__(self, service: DocumentEventService, repository: OperationTaskRepository) -> None:
        self.service = service
        self.repository = repository
        self.worker_id = f"document-event-worker-{uuid4().hex}"
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="document-event")

    def submit(self, document_id: str) -> dict[str, Any]:
        task, created = self.repository.create_or_get_active(
            "document_event_extract",
            claim_key=f"document_event_extract:{document_id}:{EVENT_EXTRACTOR_VERSION}",
            spec={
                "document_id": document_id,
                "extractor": EVENT_EXTRACTOR,
                "extractor_version": EVENT_EXTRACTOR_VERSION,
            },
            message="等待提取公告结构化事件",
        )
        if created:
            self.executor.submit(self._run, task["task_id"], document_id)
        return task

    def _run(self, task_id: str, document_id: str) -> None:
        if not self.repository.start(
            task_id, owner_id=self.worker_id, message="事件提取任务已启动"
        ):
            return

        def progress(value: int, message: str) -> None:
            if not self.repository.progress(
                task_id, owner_id=self.worker_id, progress=value, message=message
            ):
                raise RuntimeError("document event task lease is no longer owned by this worker")

        try:
            result = self.service.extract(document_id, progress=progress)
            self.repository.succeed(
                task_id,
                owner_id=self.worker_id,
                message="公告结构化事件提取完成",
                result={"document_id": document_id, "event_count": len(result["events"])},
            )
        except Exception as exc:  # noqa: BLE001 - task failure is persisted for operators
            self.repository.fail(
                task_id,
                owner_id=self.worker_id,
                message="公告结构化事件提取失败",
                error=str(exc),
            )

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
