from __future__ import annotations

import hashlib
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient
from test_data_explorer import _explorer_database
from test_web_workbench import _settings

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.documents.events import (
    DocumentEventService,
    DocumentEventTaskManager,
)
from ai_quant_assistant.documents.service import DocumentIntelligenceService
from ai_quant_assistant.providers.fixture_provider import FixtureProvider
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.operations import DatabaseBackupService, ExclusiveOperationLock
from ai_quant_assistant.storage.schema import MIGRATIONS, SCHEMA_VERSION
from ai_quant_assistant.storage.task_repository import OperationTaskRepository

PDF = b"%PDF-1.4\n% document event fixture\n%%EOF\n"
EVENT_SENTENCES = (
    "公司发布业绩预告，预计2024年1月30日归属于股东的净利润增长20%。",
    "利润分配方案为每10股派发现金红利5元（含税）。",
    "公司拟使用人民币2亿元实施股份回购。",
    "公司收到项目中标通知，重大合同金额为人民币5000万元。",
    "持股5%以上股东计划减持股份，减持比例不超过2%。",
)


def _ready_document(tmp_path, *, sentences=EVENT_SENTENCES):
    database = _explorer_database(tmp_path)
    settings = _settings(tmp_path)
    documents = DocumentIntelligenceService(database, settings)
    document = documents.archive_bytes("announcement-600519-2023", PDF)
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE announcement_document
            SET status = 'ready', page_count = ?, extraction_method = 'native',
                extracted_at = CURRENT_TIMESTAMP
            WHERE document_id = ?
            """,
            [len(sentences), document["document_id"]],
        )
        for index, sentence in enumerate(sentences, start=1):
            digest = hashlib.sha256(sentence.encode()).hexdigest()
            chunk_id = f"chunk-{index}"
            connection.execute(
                """
                INSERT INTO document_page (
                    document_id, page_number, text, text_sha256,
                    extraction_method, char_count
                ) VALUES (?, ?, ?, ?, 'native', ?)
                """,
                [document["document_id"], index, sentence, digest, len(sentence)],
            )
            connection.execute(
                """
                INSERT INTO document_chunk (
                    chunk_id, document_id, page_number, chunk_index,
                    text, text_sha256, token_estimate
                ) VALUES (?, ?, ?, 0, ?, ?, ?)
                """,
                [chunk_id, document["document_id"], index, sentence, digest, len(sentence)],
            )
    return database, document["document_id"], DocumentEventService(database)


def _wait_task(repository, task_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = repository.get(task_id)
        if task["status"] in {"succeeded", "failed"}:
            return task
        time.sleep(0.02)
    raise AssertionError("task did not finish")


def test_rules_extract_all_supported_types_with_verbatim_evidence(tmp_path) -> None:
    _, document_id, service = _ready_document(tmp_path)

    result = service.extract(document_id)

    assert {event["event_type"] for event in result["events"]} == {
        "performance_forecast",
        "dividend",
        "share_repurchase",
        "major_contract",
        "shareholder_change",
    }
    assert {event["status"] for event in result["events"]} == {"needs_review"}
    for event in result["events"]:
        assert event["evidence"]
        for evidence in event["evidence"]:
            page = service.database.connect(read_only=True)
            with page as connection:
                text = connection.execute(
                    "SELECT text FROM document_page WHERE document_id = ? AND page_number = ?",
                    [document_id, evidence["page_number"]],
                ).fetchone()[0]
            assert evidence["quote"] in text
            assert (
                evidence["quote_sha256"] == hashlib.sha256(evidence["quote"].encode()).hexdigest()
            )


def test_repeated_extraction_is_idempotent(tmp_path) -> None:
    database, document_id, service = _ready_document(tmp_path)

    first = service.extract(document_id)
    second = service.extract(document_id)

    assert [item["event_id"] for item in first["events"]] == [
        item["event_id"] for item in second["events"]
    ]
    with database.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM document_event").fetchone()[0] == 5
        evidence_count = connection.execute(
            "SELECT COUNT(*) FROM document_event_evidence"
        ).fetchone()[0]
        assert (
            evidence_count
            == connection.execute(
                "SELECT COUNT(DISTINCT event_id || ':' || evidence_order) "
                "FROM document_event_evidence"
            ).fetchone()[0]
        )


def test_invalid_quote_is_insufficient_and_cannot_be_accepted(tmp_path, monkeypatch) -> None:
    _, document_id, service = _ready_document(tmp_path, sentences=(EVENT_SENTENCES[0],))
    original = service._extract_rules

    def invalid(chunks):
        facts = original(chunks)
        facts[0]["evidence"][0]["quote"] = "正文中不存在的模型断言"
        return facts

    monkeypatch.setattr(service, "_extract_rules", invalid)

    event = service.extract(document_id)["events"][0]

    assert event["status"] == "insufficient"
    with pytest.raises(ValueError, match="requires needs_review"):
        service.review(event["event_id"], status="accepted", actor="tester")


def test_parallel_task_submission_reuses_one_claim_and_failed_task_can_retry(tmp_path) -> None:
    database, _, _ = _ready_document(tmp_path)
    repository = OperationTaskRepository(database)
    entered = Event()
    release = Event()

    class SlowService:
        def extract(self, document_id, *, progress):
            progress(20, "started")
            entered.set()
            release.wait(5)
            return {"document_id": document_id, "events": []}

    manager = DocumentEventTaskManager(SlowService(), repository)
    barrier = Barrier(8)

    def submit():
        barrier.wait()
        return manager.submit("doc-concurrent")

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            tasks = list(executor.map(lambda _index: submit(), range(8)))
        assert entered.wait(5)
        assert len({task["task_id"] for task in tasks}) == 1
        release.set()
        assert _wait_task(repository, tasks[0]["task_id"])["status"] == "succeeded"
    finally:
        release.set()
        manager.shutdown()

    repository.recover_interrupted()

    class UnexpectedService:
        def extract(self, _document_id, *, progress):
            raise AssertionError("a completed claim must not execute again")

    restarted = DocumentEventTaskManager(UnexpectedService(), repository)
    try:
        repeated = restarted.submit("doc-concurrent")
        assert repeated["task_id"] == tasks[0]["task_id"]
        assert repeated["status"] == "succeeded"
        assert repeated["reused"] is True
    finally:
        restarted.shutdown()

    class FailingService:
        def extract(self, document_id, *, progress):
            progress(20, "started")
            raise RuntimeError("controlled failure")

    failing = DocumentEventTaskManager(FailingService(), repository)
    try:
        first = failing.submit("doc-retry")
        assert _wait_task(repository, first["task_id"])["status"] == "failed"
        second = failing.submit("doc-retry")
        assert second["task_id"] != first["task_id"]
    finally:
        failing.shutdown()


def test_old_extractor_cannot_overwrite_new_result_or_review(tmp_path, monkeypatch) -> None:
    database, document_id, old_service = _ready_document(tmp_path, sentences=(EVENT_SENTENCES[0],))
    new_service = DocumentEventService(database)
    entered = Event()
    release = Event()
    original = old_service._extract_rules

    def blocked(chunks):
        entered.set()
        release.wait(5)
        return original(chunks)

    monkeypatch.setattr(old_service, "_extract_rules", blocked)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(old_service.extract, document_id)
        assert entered.wait(5)
        with database.connect() as connection:
            connection.execute(
                """
                UPDATE document_event_extraction
                SET heartbeat_at = ? WHERE status = 'running'
                """,
                [datetime.now(UTC) - timedelta(hours=2)],
            )
        current = new_service.extract(document_id)["events"][0]
        accepted = new_service.review(
            current["event_id"], status="accepted", actor="reviewer", note="verified"
        )
        release.set()
        with pytest.raises(RuntimeError, match="lease is no longer owned"):
            future.result()

    persisted = new_service.get_event(current["event_id"])
    assert persisted["status"] == accepted["status"] == "accepted"
    assert persisted["reviewed_by"] == "reviewer"


def test_point_in_time_query_excludes_later_revision(tmp_path) -> None:
    database, document_id, service = _ready_document(tmp_path, sentences=(EVENT_SENTENCES[0],))
    first = service.extract(document_id)["events"][0]
    first = service.review(first["event_id"], status="accepted", actor="tester")
    documents = DocumentIntelligenceService(database, _settings(tmp_path))
    revision = documents.archive_bytes(
        "announcement-600519-2023", b"%PDF-1.4\n% revised document\n%%EOF\n"
    )
    sentence = "公司发布业绩快报，归属于股东的净利润同比增长25%。"
    digest = hashlib.sha256(sentence.encode()).hexdigest()
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE announcement_document
            SET status = 'ready', page_count = 1, extraction_method = 'native',
                extracted_at = CURRENT_TIMESTAMP
            WHERE document_id = ?
            """,
            [revision["document_id"]],
        )
        connection.execute(
            """
            INSERT INTO document_page (
                document_id, page_number, text, text_sha256, extraction_method, char_count
            ) VALUES (?, 1, ?, ?, 'native', ?)
            """,
            [revision["document_id"], sentence, digest, len(sentence)],
        )
        connection.execute(
            """
            INSERT INTO document_chunk (
                chunk_id, document_id, page_number, chunk_index,
                text, text_sha256, token_estimate
            ) VALUES ('chunk-revision', ?, 1, 0, ?, ?, ?)
            """,
            [revision["document_id"], sentence, digest, len(sentence)],
        )
    later = service.extract(revision["document_id"])["events"][0]
    later = service.review(later["event_id"], status="accepted", actor="tester")
    first_published = datetime.fromisoformat(first["published_at"])
    first_known = datetime.fromisoformat(first["known_at"])
    later_known = datetime.fromisoformat(later["known_at"])

    historical = service.list_events(as_of=first_published + timedelta(days=1), symbol="600519")
    current = service.list_events(as_of=later_known + timedelta(seconds=1), symbol="600519")

    assert [event["event_id"] for event in historical["events"]] == [first["event_id"]]
    assert {event["event_id"] for event in current["events"]} == {
        first["event_id"],
        later["event_id"],
    }
    assert first_known == first_published
    assert later["published_at"] == first["published_at"]
    assert later_known > first_known


def test_event_study_uses_publication_and_preserves_evidence(tmp_path) -> None:
    database, document_id, service = _ready_document(tmp_path, sentences=(EVENT_SENTENCES[0],))
    event = service.extract(document_id)["events"][0]
    event = service.review(event["event_id"], status="accepted", actor="tester")
    published_at = datetime.fromisoformat(event["published_at"])

    result = service.run_event_study(
        event["event_id"],
        provider=FixtureProvider(),
        as_of=datetime(2025, 12, 31, tzinfo=UTC),
        benchmark_symbol="csi000300",
        windows=(5,),
        adjustment="qfq",
        round_trip_cost_bps=20,
    )

    assert result["event_id"] == event["event_id"]
    assert result["announcement_id"] == event["announcement_id"]
    assert result["document_id"] == document_id
    assert result["events"][0]["event_id"] == event["event_id"]
    assert datetime.fromisoformat(result["events"][0]["entry_date"]).date() > published_at.date()
    assert result["evidence"][0]["page_number"] == 1
    with database.connect(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM document_event_research_run WHERE run_id = ?",
                [result["run_id"]],
            ).fetchone()[0]
            == 1
        )


def test_schema_v11_to_v12_preserves_history_and_is_idempotent(tmp_path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    database = LocalDatabase(root / "catalog.duckdb", DataLayout.from_root(root))
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name VARCHAR NOT NULL,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for migration in MIGRATIONS[:11]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                [migration.version, migration.name],
            )
        connection.execute(
            """
            INSERT INTO announcement_document (
                document_id, announcement_id, version_no, source_url, storage_path,
                sha256, size_bytes, status
            ) VALUES ('doc-old', 'announcement-old', 1, 'https://example.test/old.pdf',
                      '/archive/old.pdf', 'old-digest', 12, 'ready')
            """
        )
        connection.execute(
            """
            INSERT INTO document_page (
                document_id, page_number, text, text_sha256, extraction_method, char_count
            ) VALUES ('doc-old', 1, 'old page', 'page-digest', 'native', 8)
            """
        )
        connection.execute(
            """
            INSERT INTO document_chunk (
                chunk_id, document_id, page_number, chunk_index,
                text, text_sha256, token_estimate
            ) VALUES ('chunk-old', 'doc-old', 1, 0, 'old page', 'page-digest', 2)
            """
        )
        connection.execute(
            """
            INSERT INTO operation_task (
                task_id, task_type, status, progress, message, spec_json, claim_key
            ) VALUES ('task-old', 'document_ingest', 'succeeded', 100, 'done', '{}',
                      'document_ingest:old')
            """
        )

    first = database.initialize()
    second = database.initialize()

    assert first["applied_migrations"] == [12, 13, 14]
    assert second["applied_migrations"] == []
    assert database.status()["schema_version"] == SCHEMA_VERSION == 14
    with database.connect(read_only=True) as connection:
        assert connection.execute("SELECT text FROM document_page").fetchone()[0] == "old page"
        assert connection.execute("SELECT text FROM document_chunk").fetchone()[0] == "old page"
        assert connection.execute("SELECT task_type, claim_key FROM operation_task").fetchone() == (
            "document_ingest",
            "document_ingest:old",
        )


def test_document_event_api_extracts_reviews_queries_and_runs_study(tmp_path) -> None:
    _, document_id, _ = _ready_document(tmp_path, sentences=(EVENT_SENTENCES[0],))

    with TestClient(create_app(_settings(tmp_path))) as client:
        submitted = client.post(f"/api/v1/documents/{document_id}/events/extract")
        assert submitted.status_code == 202, submitted.text
        task_id = submitted.json()["task_id"]
        for _ in range(200):
            task = client.get(f"/api/v1/ops/tasks/{task_id}").json()
            if task["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.02)
        assert task["status"] == "succeeded", task

        event = client.get(f"/api/v1/documents/{document_id}/events").json()["events"][0]
        reviewed = client.post(
            f"/api/v1/document-events/{event['event_id']}/review",
            json={"status": "accepted", "note": "fixture verified"},
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["status"] == "accepted"

        pit = client.get(
            "/api/v1/document-events",
            params={"as_of": "2025-12-31T00:00:00Z", "symbol": "600519"},
        )
        assert pit.status_code == 200, pit.text
        assert pit.json()["events"][0]["event_id"] == event["event_id"]

        study = client.post(
            f"/api/v1/document-events/{event['event_id']}/event-study",
            json={
                "source": "fixture",
                "as_of": "2025-12-31T00:00:00Z",
                "windows": [5],
                "adjustment": "qfq",
            },
        )
        invalid = client.post(
            f"/api/v1/document-events/{event['event_id']}/review",
            json={"status": "pending"},
        )

    assert study.status_code == 201, study.text
    assert study.json()["evidence"][0]["page_number"] == 1
    assert invalid.status_code == 422


def test_backup_and_event_write_produce_a_valid_snapshot(tmp_path, monkeypatch) -> None:
    database, document_id, service = _ready_document(tmp_path, sentences=(EVENT_SENTENCES[0],))
    settings = _settings(tmp_path)
    repository = OperationTaskRepository(database)
    backups = DatabaseBackupService(
        database=database,
        backup_dir=settings.resolved_backup_dir,
        tasks=repository,
        operation_lock=ExclusiveOperationLock(settings.data_dir / ".operations.lock"),
    )
    copy_started = Event()
    release_copy = Event()
    original_copy = shutil.copy2

    def slow_copy(source, target):
        copy_started.set()
        assert release_copy.wait(10)
        return original_copy(source, target)

    monkeypatch.setattr("ai_quant_assistant.storage.operations.shutil.copy2", slow_copy)
    with ThreadPoolExecutor(max_workers=2) as executor:
        backup_future = executor.submit(backups.run)
        assert copy_started.wait(10)
        event_future = executor.submit(service.extract, document_id)
        time.sleep(0.1)
        assert not event_future.done()
        release_copy.set()
        backup = backup_future.result(timeout=20)
        extracted = event_future.result(timeout=20)

    assert extracted["events"]
    assert backups.verify(backup["backup_id"])["status"] == "verified"
    with database._duckdb().connect(backup["backup_path"], read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM document_event").fetchone()[0] == 0
