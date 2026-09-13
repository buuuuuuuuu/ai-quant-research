from __future__ import annotations

import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient
from test_data_explorer import _explorer_database
from test_document_events import _ready_document
from test_web_workbench import _settings

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.research.stock_workbench import StockResearchWorkbench
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.operations import DatabaseBackupService, ExclusiveOperationLock
from ai_quant_assistant.storage.research_run_index import register_run
from ai_quant_assistant.storage.schema import MIGRATIONS, apply_migrations
from ai_quant_assistant.storage.task_repository import OperationTaskRepository


@pytest.fixture
def service(tmp_path):
    return StockResearchWorkbench(_explorer_database(tmp_path))


def test_readonly_views_no_network_and_identity(service, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network"))
    overview = service.overview("600519")
    assert overview["stock"]["security_id"] == "SSE.600519"
    assert overview["research_ready"] is None
    assert overview["stock"]["management"]["status"] == "active"
    assert overview["stock"]["coverage"]["announcements"]["rows"] == 1
    for section in [
        "prices",
        "financial_reports",
        "financial_metrics",
        "announcements",
        "documents",
        "events",
        "research",
    ]:
        result = service.section("600519", section, page_size=1)
        assert len(result["items"]) <= 1
    with pytest.raises(KeyError):
        service.overview("999999")
    with pytest.raises(ValueError):
        service.overview("600519' OR 1=1")
    assert service.generate("600519")["snapshot"]["stock"]["symbol"] == "600519"
    with service.database.connect() as c:
        c.execute("DELETE FROM managed_stock WHERE security_id='SSE.600519'")
    assert service.overview("600519")["stock"]["management"]["status"] == "unmanaged"


def test_publication_cutoff_not_report_period_and_revisions(service):
    before = service.section("600519", "announcements", mode="historical", as_of="2024-03-19")
    on_day = service.section("600519", "announcements", mode="historical", as_of="2024-03-20")
    assert not before["items"] and len(on_day["items"]) == 1
    for section in ["financial_reports", "financial_metrics"]:
        values = service.section("600519", section, mode="historical", as_of="2024-02-01")["items"]
        assert all(row["published_at"] < "2024-02-02" for row in values)
    with service.database.connect() as c:
        c.execute(
            "UPDATE announcement SET published_at='2024-03-21 00:00:00' WHERE announcement_id='announcement-600519-2023'"
        )
    assert not service.section("600519", "announcements", mode="historical", as_of="2024-03-20")[
        "items"
    ]


def test_financial_missing_not_zero_units_and_price_vintage_unknown(service):
    with service.database.connect() as c:
        c.execute("DELETE FROM financial_metric WHERE security_id='SSE.600519'")
    snapshot = service.generate("600519", mode="historical", as_of="2024-03-20")["snapshot"]
    assert not snapshot["sections"]["financial_metrics"]["items"]
    assert snapshot["sections"]["prices"]["items"]
    assert any("qfq" in note and "unknown" in note for note in snapshot["limitations"])


def test_snapshot_idempotence_and_immutable_after_source_update(service):
    first = service.generate("600519")
    assert service.generate("600519")["summary_id"] == first["summary_id"]
    html = service.get_summary("600519", first["summary_id"], html=True)
    with service.database.connect() as c:
        c.execute("UPDATE financial_metric SET metric_value=123456 WHERE security_id='SSE.600519'")
    second = service.generate("600519")
    assert second["summary_id"] != first["summary_id"]
    assert service.get_summary("600519", first["summary_id"]) == first
    assert service.get_summary("600519", first["summary_id"], html=True) == html
    with pytest.raises(KeyError):
        service.get_summary("000001", first["summary_id"])


def test_event_quote_and_historical_review_gate(tmp_path):
    db, document_id, events = _ready_document(tmp_path)
    extraction = events.extract(document_id)
    event = extraction["events"][0]
    events.review(event["event_id"], status="accepted", actor="test", note="verified")
    service = StockResearchWorkbench(db)
    current = service.snapshot("600519")
    assert len(current["sections"]["events"]["items"]) == 1
    historical = service.snapshot("600519", mode="historical", as_of="2024-03-20")
    assert not historical["sections"]["events"]["items"]
    with db.connect() as c:
        c.execute(
            "UPDATE document_event_evidence SET quote='正文不存在的引文' WHERE event_id=?",
            [event["event_id"]],
        )
    broken = service.snapshot("600519")
    assert not broken["sections"]["events"]["items"]
    assert any(
        "引用校验失败" in item["reason"] for item in broken["sections"]["events"]["excluded"]
    )


def test_research_association_frozen_not_current_pool_and_portfolio_label(service):
    payload = {
        "run_id": "fixture-run",
        "spec": {
            "symbols": ["600519", "000001"],
            "stock_pool": {
                "pool_id": "old-pool",
                "pool_revision": 1,
                "resolved_symbols": ["600519", "000001"],
            },
        },
        "metrics": {"total_return": 0.2},
        "status": "succeeded",
        "completed_at": "2024-04-11T12:00:00+00:00",
    }
    register_run(service.database, "factor_backtest", payload)
    rows = service.section("600519", "research")["items"]
    assert len(rows) == 1 and "组合研究" in rows[0]["scope"]
    assert rows[0]["resolved_symbols"] == ["600519", "000001"]
    assert not service.section("300750", "research")["items"]
    assert not service.section("600519", "research", mode="historical", as_of="2024-04-10")["items"]
    with pytest.raises(ValueError, match="索引冲突"):
        register_run(
            service.database, "factor_backtest", {**payload, "metrics": {"total_return": 0}}
        )
    # Legacy task specs are read from the DB, not current membership or files.
    OperationTaskRepository(service.database).create(
        "backtest", spec={"symbols": ["600519"], "top_n": 1}
    )
    assert len(service.section("600519", "research")["items"]) == 2


def test_parallel_identical_summaries_single_insert_and_audit(service):
    barrier = Barrier(3)

    def generate(_):
        barrier.wait(timeout=10)
        return service.generate("600519")["summary_id"]

    with ThreadPoolExecutor(max_workers=3) as executor:
        ids = list(executor.map(generate, range(3)))
    assert len(set(ids)) == 1
    with service.database.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM stock_research_summary").fetchone()[0] == 1
        assert (
            c.execute(
                "SELECT COUNT(*) FROM operation_audit WHERE action='stock_research_summary_created'"
            ).fetchone()[0]
            == 1
        )


def _process_generate(path, barrier, queue):
    db = LocalDatabase(Path(path), DataLayout.from_root(Path(path).parent))
    barrier.wait(timeout=30)
    try:
        queue.put(StockResearchWorkbench(db).generate("600519")["summary_id"])
    except Exception as exc:
        queue.put(f"ERROR:{exc}")


def test_independent_process_idempotence(service):
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    workers = [
        context.Process(target=_process_generate, args=(str(service.database.path), barrier, queue))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    try:
        ids = [queue.get(timeout=60) for _ in workers]
        assert len(set(ids)) == 1 and ids[0].startswith("summary-")
    finally:
        for worker in workers:
            worker.join(timeout=10)
            if worker.is_alive():
                worker.terminate()
                worker.join()


@pytest.mark.parametrize("failure", ["render", "audit"])
def test_failure_atomic_and_retry_no_artifacts(service, monkeypatch, tmp_path, failure):
    original = getattr(service, failure)

    def fail(*args, **kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(service, failure, fail)
    with pytest.raises(RuntimeError):
        service.generate("600519")
    with service.database.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM stock_research_summary").fetchone()[0] == 0
        assert (
            c.execute(
                "SELECT COUNT(*) FROM operation_audit WHERE action='stock_research_summary_created'"
            ).fetchone()[0]
            == 0
        )
    assert not list(tmp_path.rglob("*.html")) and not list(tmp_path.rglob("*.tmp"))
    monkeypatch.setattr(service, failure, original)
    assert service.generate("600519")["summary_id"]


def test_render_outside_lock_and_concurrent_backup(service, monkeypatch):
    entered, release = Event(), Event()
    original = service.render

    def render(*args):
        entered.set()
        assert release.wait(timeout=30)
        return original(*args)

    monkeypatch.setattr(service, "render", render)
    backup = DatabaseBackupService(
        database=service.database,
        backup_dir=service.database.layout.root / "backups",
        tasks=OperationTaskRepository(service.database),
        operation_lock=ExclusiveOperationLock(service.database.layout.root / ".operations.lock"),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        summary = executor.submit(service.generate, "600519")
        assert entered.wait(timeout=20)
        try:
            created = backup.run()
            assert backup.verify(created["backup_id"])["status"] == "verified"
        finally:
            release.set()
        assert summary.result(timeout=30)["summary_id"]


def test_archived_history_readonly_and_no_management_changes(service):
    with service.database.connect() as c:
        c.execute("UPDATE managed_stock SET status='archived' WHERE security_id='SSE.600519'")
    assert service.overview("600519")["stock"]["management"]["status"] == "archived"
    assert service.generate("600519")["snapshot"]["stock"]["management"]["status"] == "archived"
    with service.database.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM operation_task").fetchone()[0] == 0


def test_html_escape_digest_and_path_boundary(service):
    with service.database.connect() as c:
        c.execute(
            "UPDATE security_master SET name='<script>alert(1)</script>' WHERE symbol='600519'"
        )
    saved = service.generate("600519")
    html = service.get_summary("600519", saved["summary_id"], html=True)
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    with pytest.raises(KeyError):
        service.get_summary("600519", "../../catalog.duckdb", html=True)
    with service.database.connect() as c:
        c.execute("UPDATE stock_research_summary SET html_content='tampered'")
    with pytest.raises(RuntimeError, match="校验失败"):
        service.get_summary("600519", saved["summary_id"], html=True)


def test_api_pages_bounds_auth_and_summary(service, tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.get("/stocks/600519").status_code == 200
        assert client.get("/stocks/999999").status_code == 404
        assert client.get("/api/v1/stocks/600519/prices?page_size=101").status_code == 422
        assert client.get("/api/v1/stocks/600519/overview?mode=historical").status_code == 422
        assert (
            client.post("/api/v1/stocks/600519/summaries", json={"as_of": "2024-01-01"}).status_code
            == 422
        )
        assert (
            client.post("/api/v1/stocks/600519/summaries", json={"path": "/etc/passwd"}).status_code
            == 422
        )
        saved = client.post("/api/v1/stocks/600519/summaries", json={})
        assert saved.status_code == 201, saved.text
        summary_id = saved.json()["summary_id"]
        exported = client.get(f"/api/v1/stocks/600519/summaries/{summary_id}/html")
        assert (
            exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
        )
        assert "sandbox" in exported.headers["content-security-policy"]
    settings = replace(
        _settings(tmp_path), web_password="test-password", web_session_secret="x" * 40
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/stocks/600519/overview").status_code == 401
        assert client.get("/stocks/600519", follow_redirects=False).status_code == 303


def test_v16_migration_preservation_idempotence_and_rollback(service, monkeypatch):
    db = service.database
    with db.connect() as c:
        c.execute("DROP TABLE stock_research_summary")
        c.execute("DROP TABLE stock_research_run")
        c.execute("DELETE FROM schema_migrations WHERE version=16")
        tables = [
            r[0]
            for r in c.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_type='BASE TABLE'"
            ).fetchall()
        ]
        before = {
            name: c.execute(f'SELECT * FROM "{name}" ORDER BY ALL').fetchall() for name in tables
        }
        import ai_quant_assistant.storage.schema as schema

        v16 = next(item for item in MIGRATIONS if item.version == 16)
        broken = replace(v16, statements=(*v16.statements, "SELECT migration_failure"))
        monkeypatch.setattr(
            schema,
            "MIGRATIONS",
            tuple(broken if item.version == 16 else item for item in MIGRATIONS),
        )
        with pytest.raises(Exception, match="migration_failure"):
            apply_migrations(c)
        assert c.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=16"
        ).fetchone()[0] == 0
        for name, rows in before.items():
            assert c.execute(f'SELECT * FROM "{name}" ORDER BY ALL').fetchall() == rows
        monkeypatch.setattr(schema, "MIGRATIONS", MIGRATIONS)
        assert apply_migrations(c) == [16]
        assert apply_migrations(c) == []
        for name, rows in before.items():
            if name != "schema_migrations":
                assert c.execute(f'SELECT * FROM "{name}" ORDER BY ALL').fetchall() == rows


def test_naive_date_boundary_explicit_shanghai():
    context, cutoff = StockResearchWorkbench.bounds("historical", "2024-03-20")
    assert context["timezone"] == "Asia/Shanghai"
    assert cutoff == datetime(2024, 3, 21)
    assert datetime.now(UTC).date().isoformat()


def test_missing_research_statistics_are_null_not_zero(service):
    register_run(service.database, "event_study", {"run_id": "run-small-sample", "spec": {"symbol": "600519"}, "summary": [{"confidence_low": float("nan")}], "completed_at": "2024-03-20T01:00:00Z"})
    result = service.section("600519", "research")["items"][0]["result"]
    assert result["summary"][0]["confidence_low"] is None
    assert result["missing_statistics"] == ["result.summary[0].confidence_low"]
    assert service.generate("600519")["summary_id"]


def test_unassociated_invalid_legacy_json_does_not_fabricate_membership(service):
    tasks = OperationTaskRepository(service.database)
    task = tasks.create("backtest", spec={})
    with service.database.connect() as c:
        c.execute("UPDATE operation_task SET spec_json='invalid-json' WHERE task_id=?", [task["task_id"]])
    assert service.section("600519", "research")["items"] == []
    assert any("缺少可靠冻结范围" in note for note in service.overview("600519")["limitations"])


def test_historical_report_metric_count_cannot_include_future_metrics(service):
    with service.database.connect() as c:
        report_id = c.execute("SELECT report_id FROM financial_report WHERE security_id='SSE.600519' ORDER BY published_at LIMIT 1").fetchone()[0]
        c.execute("UPDATE financial_metric SET published_at='2025-01-01' WHERE report_id=?", [report_id])
    rows = service.section("600519", "financial_reports", mode="historical", as_of="2024-02-01")["items"]
    assert next(row for row in rows if row["record_id"] == report_id)["metric_count"] == 0


def test_research_snapshot_catalog_uses_shared_database_lock(service, monkeypatch):
    from ai_quant_assistant.storage.catalog import DuckDBCatalog

    # An uncoordinated default-config write connection conflicts with this
    # read-only connection. The shared DB wrapper must delay opening it.
    entered, attempted = Event(), Event()
    original_connect = service.database.connect

    def connect(**kwargs):
        attempted.set()
        return original_connect(**kwargs)

    monkeypatch.setattr(service.database, "connect", connect)
    catalog = DuckDBCatalog(service.database.path, database=service.database)
    snapshot = {"name": "prices", "path": "fixture.parquet", "format": "parquet", "rows": 1, "sha256": "a"*64, "created_at": "2024-01-01T00:00:00Z"}

    def write():
        entered.set()
        catalog.register("run-fixture", snapshot)

    with ThreadPoolExecutor(1) as executor:
        with original_connect(read_only=True) as c:
            future = executor.submit(write)
            assert entered.wait(10) and attempted.wait(10)
            assert not future.done()
            assert c.execute("SELECT 1").fetchone()[0] == 1
        future.result(timeout=20)
    with original_connect(read_only=True) as c:
        assert c.execute("SELECT rows FROM snapshot_catalog WHERE run_id='run-fixture'").fetchone()[0] == 1
    with pytest.raises(ValueError, match="paths must match"):
        DuckDBCatalog(service.database.path.parent / "other.duckdb", database=service.database)
