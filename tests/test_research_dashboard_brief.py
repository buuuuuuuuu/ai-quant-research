from __future__ import annotations

import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from test_data_explorer import _explorer_database
from test_document_events import _ready_document
from test_web_workbench import _settings

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.research.dashboard_brief import ResearchDashboardBrief
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.schema import apply_migrations
from ai_quant_assistant.storage.stock_pools import StockPoolService


@pytest.fixture
def service(tmp_path):
    db = _explorer_database(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE announcement SET first_visible_at='2024-03-20 10:00:00+00'")
        c.execute(
            "UPDATE financial_report SET first_visible_at=published_at AT TIME ZONE 'Asia/Shanghai'"
        )
    return ResearchDashboardBrief(db)


def test_default_pool_readonly_pagination_and_no_network(service, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network"))
    result = service.dashboard(window_start="2024-03-01", as_of="2024-03-20", page_size=1)
    assert len(result["stocks"]["items"]) == 1
    assert result["context"]["mode"] == "historical"
    assert result["pool"]["symbols_sha256"]
    assert result["sections"]["announcements"]["total"] == 1
    assert service.dashboard(query="600519")["stocks"]["total"] == 1
    with service.database.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM research_brief").fetchone()[0] == 0
    for options in ({"page_size": 101}, {"query": "x" * 121}, {"as_of": "2099-01-01"}):
        with pytest.raises(ValueError):
            service.dashboard(**options)
    with pytest.raises(KeyError):
        service.snapshot(pool_id="missing")


def test_publication_revision_and_late_utc_boundary(service):
    assert (
        service.snapshot(window_start="2024-03-01", as_of="2024-03-19")["sections"][
            "announcements"
        ]["total"]
        == 0
    )
    original = service.generate(window_start="2024-03-01", as_of="2024-03-20")
    with service.database.connect() as c:
        # 16:30 UTC is next day 00:30 Shanghai: belongs to March 22 arrival window.
        c.execute("UPDATE announcement SET first_visible_at='2024-03-21 16:30:00+00'")
    assert (
        service.snapshot(window_start="2024-03-21", as_of="2024-03-21")["sections"][
            "announcements"
        ]["total"]
        == 0
    )
    late = service.snapshot(window_start="2024-03-22", as_of="2024-03-22")["sections"][
        "late_arrivals"
    ]["items"]
    assert len(late) == 1 and late[0]["published_at"] == "2024-03-20T18:00:00"
    assert late[0]["ingested_at"].startswith("2024-03-21T16:30:00")
    assert service.get(original["brief_id"]) == original


def test_financial_versions_units_no_growth_or_zero(service):
    before = service.snapshot(window_start="2023-12-01", as_of="2024-01-01")
    rows = before["sections"]["financial_metrics"]["items"]
    assert rows and all(x["published_at"] < "2024-01-02" for x in rows)
    assert all("unit" in x and "raw_sha256" in x for x in rows)
    assert not any("growth" in key for x in rows for key in x)
    with service.database.connect() as c:
        c.execute(
            "UPDATE financial_report SET first_visible_at='2024-03-22 00:00:00+00' WHERE report_id='maotai-revision'"
        )
    after = service.snapshot(window_start="2024-03-22", as_of="2024-03-22")
    assert any(
        x.get("report_id") == "maotai-revision" for x in after["sections"]["late_arrivals"]["items"]
    )


def test_idempotent_snapshot_preview_change_and_tampering(service):
    options = {"window_start": "2024-03-01", "as_of": "2024-03-20"}
    preview = service.preview(**options)
    first = service.generate(expected_fingerprint=preview["fingerprint"], **options)
    assert service.generate(**options)["brief_id"] == first["brief_id"]
    original_html = service.get(first["brief_id"], html=True)
    with service.database.connect() as c:
        c.execute("UPDATE announcement SET title='<script>alert(1)</script>'")
    with pytest.raises(RuntimeError, match="重新预览"):
        service.generate(expected_fingerprint=preview["fingerprint"], **options)
    second = service.generate(**options)
    assert second["brief_id"] != first["brief_id"]
    assert "<script>alert" not in service.get(second["brief_id"], html=True)
    assert service.get(first["brief_id"], html=True) == original_html
    with service.database.connect() as c:
        c.execute(
            "UPDATE research_brief SET html_content='tampered' WHERE brief_id=?",
            [first["brief_id"]],
        )
    with pytest.raises(RuntimeError, match="校验失败"):
        service.get(first["brief_id"], html=True)
    with pytest.raises(KeyError):
        service.get("../../etc/passwd")


def test_historical_pool_frozen(service):
    pools = StockPoolService(service.database)
    pool = pools.create(name="研究池", symbols=["600519", "000001"])
    first = service.generate(pool_id=pool["pool_id"], pool_revision=1)
    with service.database.connect() as c:
        c.execute("DELETE FROM stock_pool_member WHERE pool_id=?", [pool["pool_id"]])
    assert service.snapshot(pool_id=pool["pool_id"], pool_revision=1)["symbols"] == [
        "600519",
        "000001",
    ]
    assert service.get(first["brief_id"]) == first
    with pytest.raises(KeyError):
        service.snapshot(pool_id=pool["pool_id"], pool_revision=999)


def test_previous_comparable_window_and_late_after_generation(service):
    first = service.generate(as_of="2024-03-20")
    with service.database.connect() as c:
        c.execute("UPDATE research_brief SET created_at='2024-03-20 01:00:00+00'")
        c.execute("UPDATE announcement SET first_visible_at='2024-03-20 02:00:00+00'")
    next_snapshot = service.snapshot(as_of="2024-03-22")
    assert next_snapshot["context"]["window_start"] == "2024-03-21"
    assert next_snapshot["context"]["previous_brief_id"] == first["brief_id"]
    assert len(next_snapshot["sections"]["late_arrivals"]["items"]) == 1
    different = service.snapshot(as_of="2024-03-22", adjustment="none")
    assert different["context"]["previous_brief_id"] is None
    assert different["context"]["window_start"] == "2024-03-16"


def test_prices_cutoff_inputs_unknown_and_hash(service):
    result = service.snapshot(
        window_start="2024-01-01", as_of="2024-01-03", price_change_threshold=0
    )
    prices = result["sections"]["price_observations"]
    assert prices["items"] and prices["unknown"]
    assert all(x["trade_date"] <= "2024-01-03" for x in prices["evidence"])
    assert all(x["volume_ratio"] is None for x in prices["items"])
    assert result["parameters"]["volume_lookback_max"] == 20
    assert all(x["source_batch_id"] for x in prices["evidence"])
    assert all("没有事件或风险" not in x.get("reason", "") for x in prices["unknown"])


def test_concurrent_threads_one_brief_and_audit(service):
    barrier = Barrier(3)

    def generate(_):
        barrier.wait(timeout=20)
        return service.generate()["brief_id"]

    with ThreadPoolExecutor(max_workers=3) as executor:
        ids = list(executor.map(generate, range(3)))
    assert len(set(ids)) == 1
    with service.database.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM research_brief").fetchone()[0] == 1
        assert (
            c.execute(
                "SELECT COUNT(*) FROM operation_audit WHERE action='research_brief_created'"
            ).fetchone()[0]
            == 1
        )


def _generate_process(path, barrier, queue):
    db = LocalDatabase(Path(path), DataLayout.from_root(Path(path).parent))
    barrier.wait(timeout=30)
    try:
        queue.put(ResearchDashboardBrief(db).generate()["brief_id"])
    except Exception as exc:
        queue.put(f"ERROR:{exc}")


def test_independent_processes_one_brief(service):
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [
        context.Process(target=_generate_process, args=(str(service.database.path), barrier, queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    ids = [queue.get(timeout=90) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert len(set(ids)) == 1 and ids[0].startswith("brief-")


def test_audit_and_render_failures_rollback(service, monkeypatch):
    original = service._audit

    def fail(c, action, *args, **kwargs):
        if action == "research_brief_created":
            raise RuntimeError("audit failure")
        return original(c, action, *args, **kwargs)

    monkeypatch.setattr(service, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        service.generate()
    with service.database.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM research_brief").fetchone()[0] == 0
    monkeypatch.setattr(service, "_audit", original)
    monkeypatch.setattr(
        service, "render", lambda *a: (_ for _ in ()).throw(OSError("render failure"))
    )
    with pytest.raises(OSError):
        service.generate()
    assert service.list()["pagination"]["total"] == 0


def test_event_review_and_evidence(tmp_path):
    db, document, events = _ready_document(tmp_path)
    event = events.extract(document)["events"][0]
    dashboard = ResearchDashboardBrief(db)
    events.review(event["event_id"], status="accepted", actor="test", note="verified")
    assert dashboard.snapshot()["sections"]["accepted_events"]["items"]
    with db.connect() as c:
        c.execute(
            "UPDATE document_event_evidence SET quote='missing' WHERE event_id=?",
            [event["event_id"]],
        )
    assert not dashboard.snapshot()["sections"]["accepted_events"]["items"]
    assert dashboard.snapshot()["sections"]["review_events"]["items"]
    with db.connect() as c:
        c.execute("UPDATE document_event SET status='rejected'")
    assert not dashboard.snapshot()["sections"]["review_events"]["items"]


def test_api_confirmation_pagination_and_download(service, tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.get("/research-dashboard").status_code == 200
        assert client.get("/api/v1/research-dashboard/pools").status_code == 200
        assert client.get("/api/v1/research-dashboard?page_size=101").status_code == 422
        assert client.get("/api/v1/research-dashboard?stock_pool_id=missing").status_code == 404
        body = {"window_start": "2024-03-01", "as_of": "2024-03-20"}
        preview = client.post("/api/v1/research-briefs/preview", json=body)
        assert preview.status_code == 200, preview.text
        assert client.post("/api/v1/research-briefs", json=body).status_code == 422
        result = client.post(
            "/api/v1/research-briefs",
            json={**body, "confirm": True, "expected_fingerprint": preview.json()["fingerprint"]},
        )
        assert result.status_code == 201, result.text
        html = client.get(f"/api/v1/research-briefs/{result.json()['brief_id']}/html")
        assert html.status_code == 200 and "attachment" in html.headers["content-disposition"]
        assert "sandbox" in html.headers["content-security-policy"]


def test_migration_v17_retains_all_rows_repeat_and_rollback(tmp_path, monkeypatch):
    from dataclasses import replace

    from ai_quant_assistant.storage import schema

    migrations = schema.MIGRATIONS
    monkeypatch.setattr(schema, "MIGRATIONS", migrations[:-1])
    database = _explorer_database(tmp_path)
    with database.connect() as db:
        before = {
            r[0]: db.execute(f'SELECT COUNT(*) FROM "{r[0]}"').fetchone()[0]
            for r in db.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_type='BASE TABLE'"
            ).fetchall()
        }
        broken = replace(
            migrations[-1],
            statements=(*migrations[-1].statements, "SELECT * FROM intentionally_missing_table"),
        )
        monkeypatch.setattr(schema, "MIGRATIONS", (*migrations[:-1], broken))
        with pytest.raises(__import__("duckdb").CatalogException):
            apply_migrations(db)
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 16
        assert (
            db.execute(
                "SELECT COUNT(*) FROM information_schema.columns WHERE column_name='first_visible_at'"
            ).fetchone()[0]
            == 0
        )
        monkeypatch.setattr(schema, "MIGRATIONS", migrations)
        assert apply_migrations(db) == [17]
        for name, count in before.items():
            if name != "schema_migrations":
                assert db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] == count
        assert apply_migrations(db) == []
        assert db.execute("SELECT COUNT(*) FROM research_brief").fetchone()[0] == 0


def test_unknown_legacy_arrival_is_visible_without_inventing_time(service):
    with service.database.connect() as c:
        c.execute("UPDATE announcement SET first_visible_at=NULL")
    items = service.snapshot(window_start="2024-04-01", as_of="2024-04-02")["sections"][
        "late_arrivals"
    ]["items"]
    assert items and items[0]["arrival_time_unknown"] is True
    assert items[0]["ingested_at"] is None
    assert items[0]["first_source_batch_id"] is None


def test_truncated_source_change_invalidates_preview(service):
    with service.database.connect() as c:
        c.execute(
            "INSERT INTO announcement SELECT announcement_id || '-' || i, security_id,company_name,title,report_type,report_year,report_period_end,announcement_date,published_at,url,source,source_batch_id,content_sha256,quality_status,ingested_at,first_source_batch_id,first_visible_at FROM announcement CROSS JOIN range(110) t(i)"
        )
    options = {"window_start": "2024-03-01", "as_of": "2024-03-20"}
    first = service.preview(**options)
    assert first["snapshot"]["sections"]["announcements"]["truncated"]
    shown = {x["announcement_id"] for x in first["snapshot"]["sections"]["announcements"]["items"]}
    with service.database.connect() as c:
        ids = [x[0] for x in c.execute("SELECT announcement_id FROM announcement").fetchall()]
        hidden = next(x for x in ids if x not in shown)
        c.execute(
            "UPDATE announcement SET content_sha256=? WHERE announcement_id=?", ["changed", hidden]
        )
    assert service.preview(**options)["fingerprint"] != first["fingerprint"]


def test_backup_during_render_is_valid(service, monkeypatch):
    from threading import Event

    from ai_quant_assistant.storage.operations import DatabaseBackupService, ExclusiveOperationLock
    from ai_quant_assistant.storage.task_repository import OperationTaskRepository

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
        generated = executor.submit(service.generate)
        assert entered.wait(timeout=20)
        try:
            result = backup.run()
            assert backup.verify(result["backup_id"])["status"] == "verified"
        finally:
            release.set()
        assert generated.result(timeout=30)["brief_id"]


def test_500_stock_pool_bounded_set_queries(service, monkeypatch):
    import hashlib
    import json
    from contextlib import contextmanager

    symbols = [f"{i:06d}" for i in range(700000, 700500)]
    with service.database.connect() as c:
        c.execute(
            "INSERT INTO security_master(security_id,symbol,exchange,name,source,source_batch_id) SELECT 'SSE.' || i,i::VARCHAR,'SSE','Synthetic ' || i,'fixture','fixture' FROM range(700000,700500) t(i)"
        )
        c.execute(
            "INSERT INTO daily_bar(security_id,trade_date,adjustment,open,high,low,close,volume,amount,source_batch_id,quality_status) SELECT 'SSE.' || i,DATE '2024-01-01' + j::INTEGER,'qfq',100,101,99,100,1000,100000,'fixture','published' FROM range(700000,700500) t(i) CROSS JOIN range(21) d(j)"
        )
        pool_id = c.execute("SELECT pool_id FROM stock_pool WHERE is_default=TRUE").fetchone()[0]
        c.execute(
            "UPDATE stock_pool_revision SET symbols_json=?,symbols_sha256=? WHERE pool_id=?",
            [
                json.dumps(symbols),
                hashlib.sha256(json.dumps(symbols, separators=(",", ":")).encode()).hexdigest(),
                pool_id,
            ],
        )
    original = service.database.connect
    statements = []

    class Connection:
        def __init__(self, c):
            self.c = c

        def execute(self, query, *args):
            statements.append(query)
            return self.c.execute(query, *args)

    @contextmanager
    def connect(**kwargs):
        with original(**kwargs) as c:
            c.execute("SET memory_limit='128MB'")
            c.execute("SET threads=1")
            yield Connection(c)

    monkeypatch.setattr(service.database, "connect", connect)
    snapshot = service.snapshot()
    assert len(snapshot["symbols"]) == 500 and len(snapshot["stocks"]["items"]) == 500
    assert len(statements) < 35
    assert len(json.dumps(snapshot).encode()) < 4_000_000


def test_frozen_research_association_and_parameter_digest(service):
    from ai_quant_assistant.storage.research_run_index import register_run
    from ai_quant_assistant.storage.task_repository import OperationTaskRepository

    register_run(
        service.database,
        "factor_backtest",
        {
            "run_id": "frozen-run",
            "spec": {"symbols": ["600519", "000001"]},
            "status": "succeeded",
            "completed_at": "2024-03-20T10:00:00+00:00",
        },
    )
    OperationTaskRepository(service.database).create("backtest", spec={"symbols": ["600519"]})
    result = service.snapshot(window_start="2024-03-01", as_of="2024-03-20")["sections"][
        "research_runs"
    ]["items"]
    assert len(result) == 1 and result[0]["run_id"] == "frozen-run"
    assert len(result[0]["spec_sha256"]) == 64
    assert result[0]["resolved_symbols"] == ["600519", "000001"]
    assert "组合指标不是单股表现" in result[0]["scope"]


def test_dashboard_copilot_is_readonly_and_date_is_explicit(service):
    from ai_quant_assistant.ai.planner import RuleBasedPlanner
    from ai_quant_assistant.ai.preflight import CoveragePreflight, validate_tool_spec

    plan = RuleBasedPlanner().plan("查看研究看板 2024-03-20")
    assert plan["tool_name"] == "get_research_dashboard"
    spec = validate_tool_spec(plan["tool_name"], plan["spec"])
    assert spec["as_of"] == "2024-03-20"
    summary = service.copilot_summary(**spec)
    assert summary["as_of"] == "2024-03-20"
    assert service.list()["pagination"]["total"] == 0
    assert CoveragePreflight(service.database).run(plan["tool_name"], spec)["ready"]
