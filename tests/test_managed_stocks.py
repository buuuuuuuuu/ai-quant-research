from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier, Event

import pandas as pd
import pytest

from ai_quant_assistant.config import Settings
from ai_quant_assistant.providers.akshare_provider import AkShareProvider
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.managed_stocks import ManagedStockConflict, ManagedStockService
from ai_quant_assistant.storage.schema import MIGRATIONS, apply_migrations
from ai_quant_assistant.storage.stock_pools import StockPoolService


class Provider:
    name = "controlled-managed-fixture"

    def __init__(self, *, fail_once=False, gap=False, single=False, block=False):
        self.fail_once, self.gap, self.single = fail_once, gap, single
        self.block = block
        self.entered, self.release = Event(), Event()
        self.calls = []
        self.listed = date(2024, 1, 2)
        self.suspension_dates = set()
        self.suspension_errors = []

    def get_listing_date(self, symbol):
        return self.listed, "fixture:explicit-listing-date"

    def get_trading_calendar(self):
        days = pd.bdate_range("2024-01-02", "2024-01-12")
        return pd.DataFrame(
            [
                dict(
                    exchange=exchange,
                    trade_date=day,
                    is_open=True,
                    previous_open_date=None,
                    next_open_date=None,
                    session_no=i + 1,
                    source="fixture:calendar",
                )
                for exchange in ("SSE", "SZSE")
                for i, day in enumerate(days)
            ]
        )

    def get_daily_bars(self, symbol, start, end, adjustment):
        self.calls.append((symbol, start, end, adjustment))
        self.entered.set()
        if self.block:
            assert self.release.wait(10), "test barrier timed out"
        if self.fail_once and adjustment == "qfq":
            self.fail_once = False
            raise RuntimeError("controlled qfq failure")
        days = [day for day in pd.bdate_range(start, end) if day.date() >= self.listed]
        if self.gap:
            days = [day for day in days if day.date() != date(2024, 1, 3)]
        if self.single:
            days = days[:1]
        return pd.DataFrame(
            [
                dict(
                    trade_date=day,
                    open=10.0,
                    high=11.0,
                    low=9.0,
                    close=10.0,
                    volume=100.0,
                    amount=1000.0,
                    source="fixture:bars",
                    symbol=symbol,
                )
                for day in days
            ],
            columns=[
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "source",
                "symbol",
            ],
        )

    def get_suspension_evidence(self, symbol, dates):
        return {
            "verified": [
                {"date": str(day), "classification": "verified_suspension",
                 "source": "fixture:suspension", "checked_at": "2024-01-12T00:00:00Z",
                 "reason": "fixture explicit suspension"}
                for day in dates if day in self.suspension_dates
            ],
            "errors": list(self.suspension_errors),
            "requested": len(dates),
            "source": "fixture:suspension",
        }


@pytest.fixture
def setup(tmp_path):
    layout = DataLayout.from_root(tmp_path / "data")
    db = LocalDatabase(layout.root / "catalog.duckdb", layout)
    db.initialize()
    with db.connect() as c:
        c.execute("""INSERT INTO security_master
                     (security_id,symbol,exchange,name,status,source,source_batch_id)
                     VALUES ('SZSE.003816','003816','SZSE','测试中国广核','active','fixture','fixture')""")
    provider = Provider()
    service = ManagedStockService(db, Settings(data_sync_rate_limit_seconds=0), lambda _: provider)
    return db, provider, service


def submit(service, **kwargs):
    return service.request(
        "003816",
        start=date(2012, 1, 1),
        end=date(2024, 1, 5),
        confirm=True,
        actor="fixture",
        **kwargs,
    )


def test_enroll_backfill_and_pool_admission(setup):
    db, provider, service = setup
    task, created = submit(service)
    assert created and provider.calls == []
    pools = StockPoolService(db)
    with pytest.raises(ValueError, match="not ready"):
        pools.create("blocked", symbols=["003816"])
    result = service.run(task["task_id"])
    assert result["status"] == "succeeded", result.get("error")
    assert {call[1] for call in provider.calls} == {date(2024, 1, 2)}
    assert {call[3] for call in provider.calls} == {"none", "qfq"}
    asset = service.get("SZSE.003816")
    assert asset["eligible"] and asset["coverage"]["adjustments"]["qfq"]["rows"] == 4
    pool = pools.create("available", symbols=["003816"])
    assert pool["symbols"] == ["003816"]
    repeated, created = submit(service)
    assert not created and repeated["task_id"] == task["task_id"]
    with pytest.raises(ManagedStockConflict, match="活动股票池"):
        service.change("SZSE.003816", expected_revision=1, action="archive", confirm=True)
    frozen = pools.resolve(pool["pool_id"], revision=1)
    pools.remove_member(pool["pool_id"], "SZSE.003816", expected_revision=1)
    archived = service.change("SZSE.003816", expected_revision=1, action="archive", confirm=True)
    assert archived["status"] == "archived" and not archived["auto_update"]
    historical = pools.resolve(pool["pool_id"], revision=1)
    assert historical["resolved_symbols"] == frozen["resolved_symbols"]
    assert historical["symbols_sha256"] == frozen["symbols_sha256"]
    with db.connect(read_only=True) as c:
        assert (
            c.execute("SELECT COUNT(*) FROM daily_bar WHERE security_id='SZSE.003816'").fetchone()[
                0
            ]
            == 8
        )
    restored = service.change("SZSE.003816", expected_revision=2, action="restore", confirm=True)
    assert restored["eligible"] and not restored["auto_update"]
    assert len(provider.calls) == 2  # Restore never downloads.


@pytest.mark.parametrize("problem", ["fail_once", "gap", "single"])
def test_partial_data_not_ready_and_safe_retry(setup, problem):
    db, provider, service = setup
    setattr(provider, problem, True)
    task, _ = submit(service)
    result = service.run(task["task_id"])
    assert result["status"] == ("succeeded" if problem == "gap" else "failed")
    if problem == "gap":
        assert result["progress"] == 100
        assert "仍有日期需核验" in result["message"]
    assert service.get("SZSE.003816")["availability"] == "partial"
    with pytest.raises(ValueError, match="not ready"):
        StockPoolService(db).create("no", symbols=["003816"])
    setattr(provider, problem, False)
    retry, created = submit(service)
    assert created and retry["task_id"] != task["task_id"]
    assert service.run(retry["task_id"])["status"] == "succeeded"
    with db.connect(read_only=True) as c:
        rows = c.execute(
            "SELECT COUNT(*) FROM daily_bar WHERE security_id='SZSE.003816'"
        ).fetchone()[0]
        assert rows == (6 if problem == "gap" else 8)


def test_gap_verification_is_explicit_bounded_and_never_redownloads(setup):
    _, provider, service = setup
    provider.gap = True
    sync, _ = submit(service)
    assert service.run(sync["task_id"])["status"] == "succeeded"
    asset = service.get("SZSE.003816")
    assert asset["coverage"]["requires_gap_verification"] is True
    assert asset["coverage"]["gap_summary"]["unverified_gap"] == 1
    download_calls = list(provider.calls)
    repeated, _ = submit(service, expected_revision=1, refresh=True)
    assert service.run(repeated["task_id"])["status"] == "succeeded"
    assert provider.calls == download_calls
    with pytest.raises(ValueError, match="明确确认"):
        service.request_gap_verification(
            "SZSE.003816", expected_revision=1, confirm=False, actor="fixture"
        )
    provider.suspension_dates = {date(2024, 1, 3)}
    task, created = service.request_gap_verification(
        "SZSE.003816", expected_revision=1, confirm=True, actor="fixture"
    )
    assert created
    assert service.run(task["task_id"])["status"] == "succeeded"
    verified = service.get("SZSE.003816")
    assert verified["availability"] == "ready"
    assert verified["coverage"]["gap_summary"]["verified_suspension"] == 1
    assert provider.calls == download_calls
    with pytest.raises(ManagedStockConflict, match="没有可核验"):
        service.request_gap_verification(
            "SZSE.003816", expected_revision=1, confirm=True, actor="fixture"
        )


def test_empty_suspension_result_remains_unverified_without_download_failure(setup):
    _, provider, service = setup
    provider.gap = True
    sync, _ = submit(service)
    service.run(sync["task_id"])
    calls = list(provider.calls)
    task, _ = service.request_gap_verification(
        "SZSE.003816", expected_revision=1, confirm=True, actor="fixture"
    )
    result = service.run(task["task_id"])
    assert result["status"] == "succeeded" and result["progress"] == 100
    assert result["result"]["outcome"] == "verification_incomplete"
    assert service.get("SZSE.003816")["availability"] == "partial"
    assert provider.calls == calls


def test_concurrent_claim_and_atomic_submission(setup, monkeypatch):
    db, _, service = setup
    barrier = Barrier(2)

    def request(_):
        barrier.wait()
        return submit(ManagedStockService(db))

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(request, range(2)))
    assert len({r[0]["task_id"] for r in results}) == 1
    assert sum(r[1] for r in results) == 1
    with db.connect(read_only=True) as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM operation_audit WHERE action='managed_stock_sync_requested'"
            ).fetchone()[0]
            == 1
        )

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(service, "audit", fail)
    with db.connect() as c:
        c.execute("""INSERT INTO security_master
                     (security_id,symbol,exchange,name,status,source,source_batch_id)
                     VALUES ('SSE.600022','600022','SSE','离线测试','active','fixture','fixture')""")
    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.request("600022", confirm=True)
    with db.connect(read_only=True) as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM managed_stock WHERE security_id='SSE.600022'"
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM operation_task WHERE claim_key='managed-stock:SSE.600022'"
            ).fetchone()[0]
            == 0
        )


def test_archive_fences_inflight_worker_and_keeps_database_available(setup):
    db, provider, service = setup
    provider.block = True
    task, _ = submit(service)
    with ThreadPoolExecutor(max_workers=1) as workers:
        result = workers.submit(service.run, task["task_id"])
        assert provider.entered.wait(10)
        # Network cannot hold the database access lock.
        with db.connect(read_only=True) as c:
            assert c.execute("SELECT 42").fetchone()[0] == 42
        service.change("SZSE.003816", expected_revision=1, action="archive", confirm=True)
        provider.release.set()
        assert result.result(timeout=15)["status"] == "failed"
    assert service.get("SZSE.003816")["status"] == "archived"
    with db.connect(read_only=True) as c:
        assert (
            c.execute("SELECT COUNT(*) FROM daily_bar WHERE security_id='SZSE.003816'").fetchone()[
                0
            ]
            == 0
        )


def test_previous_ready_remains_selectable_during_refresh(setup):
    db, provider, service = setup
    task, _ = submit(service)
    service.run(task["task_id"])
    provider.fail_once = True
    retry, _ = submit(service, refresh=True, expected_revision=1)
    assert service.get("SZSE.003816")["eligible"]
    StockPoolService(db).create("during-update", symbols=["003816"])
    assert service.run(retry["task_id"])["status"] == "failed"
    assert service.get("SZSE.003816")["eligible"]


def test_migration_v14_is_repeatable_and_preserves_old_tables(tmp_path):
    layout = DataLayout.from_root(tmp_path / "migration")
    db = LocalDatabase(layout.root / "catalog.duckdb", layout)
    layout.ensure()
    with db.connect() as c:
        c.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name VARCHAR,applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        for migration in MIGRATIONS[:13]:
            for statement in migration.statements:
                c.execute(statement)
            if migration.handler:
                migration.handler(c)
            c.execute(
                "INSERT INTO schema_migrations(version,name) VALUES (?,?)",
                [migration.version, migration.name],
            )
        c.execute("""INSERT INTO security_master
                     (security_id,symbol,exchange,name,status,source,source_batch_id)
                     VALUES ('SZSE.003816','003816','SZSE','只存在目录','active','fixture','fixture')""")
        tables = [
            r[0]
            for r in c.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_type='BASE TABLE' AND table_name<>'schema_migrations'"
            ).fetchall()
        ]
        before = {
            name: c.execute(f'SELECT * FROM "{name}" ORDER BY ALL').fetchall() for name in tables
        }
        assert apply_migrations(c) == [14, 15, 16, 17]
        assert apply_migrations(c) == []
        for name, values in before.items():
            assert c.execute(f'SELECT * FROM "{name}" ORDER BY ALL').fetchall() == values
        assert (
            c.execute(
                "SELECT COUNT(*) FROM managed_stock WHERE security_id='SZSE.003816'"
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM managed_stock WHERE availability='unavailable'"
            ).fetchone()[0]
            == 10
        )


def test_other_stock_latest_date_does_not_skip_new_stock_history(setup):
    db, provider, service = setup
    with db.connect() as c:
        c.execute("""INSERT INTO daily_bar
            (security_id, trade_date, adjustment, open, high, low, close, volume,
             amount, source_batch_id, quality_status)
            VALUES ('SSE.600519', '2025-01-02', 'none', 10,11,9,10,100,1000,'unrelated','published')""")
    task, _ = submit(service)
    assert service.run(task["task_id"])["status"] == "succeeded"
    assert all(call[1] == date(2024, 1, 2) for call in provider.calls)


def test_missing_middle_and_none_qfq_are_checked_independently(setup):
    from ai_quant_assistant.storage.managed_coverage import coverage_report

    db, provider, service = setup
    task, _ = submit(service)
    service.run(task["task_id"])
    with db.connect() as c:
        c.execute(
            "DELETE FROM daily_bar WHERE security_id='SZSE.003816' AND adjustment='qfq' AND trade_date='2024-01-03'"
        )
        report = coverage_report(
            c, "SZSE.003816", date(2024, 1, 2), date(2024, 1, 5), provider.listed
        )
    assert report["availability"] == "partial"
    assert report["adjustments"]["none"]["missing_count"] == 0
    assert report["adjustments"]["qfq"]["missing_sample"] == ["2024-01-03"]
    retry, _ = submit(service, expected_revision=1, refresh=True)
    assert service.run(retry["task_id"])["status"] == "succeeded"


def test_empty_and_unknown_listing_are_not_ready(setup, monkeypatch):
    _, provider, service = setup
    monkeypatch.setattr(provider, "get_daily_bars", lambda *args: pd.DataFrame())
    task, _ = submit(service)
    assert service.run(task["task_id"])["status"] == "failed"
    assert not service.get("SZSE.003816")["eligible"]


def test_terminal_commit_failure_rolls_back_readiness_and_audit(setup, monkeypatch):
    db, _, service = setup
    task, _ = submit(service)
    original = service.audit

    def fail_completion(connection, action, *args, **kwargs):
        if action == "managed_stock_sync_completed":
            raise RuntimeError("controlled final audit failure")
        return original(connection, action, *args, **kwargs)

    monkeypatch.setattr(service, "audit", fail_completion)
    result = service.run(task["task_id"])
    assert result["status"] == "failed" and result.get("result") is None
    assert "controlled final audit" in result["error"]
    assert service.get("SZSE.003816")["availability"] == "unavailable"
    with db.connect(read_only=True) as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM operation_audit WHERE action='managed_stock_sync_completed'"
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM published_daily_bar WHERE security_id='SZSE.003816'"
            ).fetchone()[0]
            == 8
        )
    monkeypatch.setattr(service, "audit", original)
    retry, _ = submit(service)
    assert service.run(retry["task_id"])["status"] == "succeeded"


def test_expired_owner_and_restart_cannot_overwrite_completed_result(setup):
    db, provider, service = setup
    task, _ = submit(service)
    assert service.tasks.start(task["task_id"], owner_id="dead-owner", message="fixture")
    with db.connect() as c:
        c.execute(
            "UPDATE operation_task SET heartbeat_at=TIMESTAMP '2020-01-01' WHERE task_id=?",
            [task["task_id"]],
        )
    restarted = ManagedStockService(db, service.settings, lambda _: provider)
    # A restart may cross midnight: the expired claim must not block a later
    # end date when the caller supplies the current management revision.
    retry, created = restarted.request(
        "003816",
        end=date(2024, 1, 12),
        expected_revision=1,
        confirm=True,
    )
    assert created and retry["task_id"] != task["task_id"]
    assert restarted.get("SZSE.003816")["target_end"] == "2024-01-12"
    with db.connect(read_only=True) as c, pytest.raises(ManagedStockConflict):
        service.fence(c, task["task_id"], "dead-owner", task["spec"])
    done = restarted.run(retry["task_id"])
    assert done["status"] == "succeeded"
    calls = len(provider.calls)
    restarted.tasks.recover_interrupted()
    assert restarted.run(retry["task_id"])["result"] == done["result"]
    assert not service.tasks.succeed(
        task["task_id"], owner_id="dead-owner", message="stale", result={}
    )
    assert len(provider.calls) == calls


def test_pool_all_write_paths_gate_but_legacy_revisions_are_readable(setup):
    db, _, service = setup
    task, _ = submit(service)
    service.run(task["task_id"])
    pools = StockPoolService(db)
    pool = pools.create("legacy", symbols=["003816"])
    frozen = pools.resolve(pool["pool_id"], revision=1)
    with db.connect() as c:
        c.execute("UPDATE managed_stock SET availability='partial' WHERE security_id='SZSE.003816'")
    for source in ("manual", "imported", "screen_snapshot"):
        with pytest.raises(ValueError, match="not ready"):
            pools.create(
                "blocked",
                symbols=["003816"],
                source_type=source,
                source_screen_run_id="fixture-screen" if source == "screen_snapshot" else None,
            )
    empty = pools.create("empty")
    with pytest.raises(ValueError, match="not ready"):
        pools.add_members(empty["pool_id"], ["003816"], expected_revision=1)
    with pytest.raises(ValueError, match="not ready"):
        pools.duplicate(pool["pool_id"], expected_revision=1, name="blocked copy")
    assert pools.resolve(pool["pool_id"], revision=1)["symbols_sha256"] == frozen["symbols_sha256"]
    assert (
        pools.duplicate(empty["pool_id"], expected_revision=1, name="empty copy")["symbols"] == []
    )


def test_archive_and_pool_add_are_serialized(setup):
    db, _, service = setup
    task, _ = submit(service)
    service.run(task["task_id"])
    pools = StockPoolService(db)
    pool = pools.create("concurrent")
    barrier = Barrier(2)

    def archive():
        barrier.wait()
        try:
            service.change("SZSE.003816", expected_revision=1, action="archive", confirm=True)
            return "archived"
        except ManagedStockConflict:
            return "conflict"

    def add():
        barrier.wait()
        try:
            pools.add_members(pool["pool_id"], ["003816"], expected_revision=1)
            return "added"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        a, b = executor.submit(archive), executor.submit(add)
        result = (a.result(timeout=10), b.result(timeout=10))
    assert result in {("archived", "rejected"), ("conflict", "added")}


def test_backup_during_managed_download_is_valid(setup, tmp_path):
    from ai_quant_assistant.storage.operations import DatabaseBackupService, ExclusiveOperationLock

    db, provider, service = setup
    provider.block = True
    task, _ = submit(service)
    backups = DatabaseBackupService(
        database=db,
        backup_dir=tmp_path / "backups",
        tasks=service.tasks,
        operation_lock=ExclusiveOperationLock(tmp_path / ".operations.lock"),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.run, task["task_id"])
        assert provider.entered.wait(10)
        try:
            backup = backups.run()
        finally:
            provider.release.set()
        assert future.result(timeout=20)["status"] == "succeeded"
    assert backups.verify(backup["backup_id"])["status"] == "verified"
    with db._duckdb().connect(backup["backup_path"], read_only=True) as c:
        assert (
            c.execute(
                "SELECT status FROM managed_stock WHERE security_id='SZSE.003816'"
            ).fetchone()[0]
            == "active"
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM published_daily_bar WHERE security_id='SZSE.003816'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("include_announcements", [False, True])
def test_api_async_confirmation_limits_conflict_and_restore(setup, monkeypatch, include_announcements):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ai_quant_assistant.api.managed_stock_routes import managed_stock_router
    from ai_quant_assistant.api.managed_stock_tasks import ManagedStockTaskManager

    _, provider, service = setup
    manager = ManagedStockTaskManager(
        service,
        datasets=("managed_stock", "managed_stock_gap_verification"),
        worker_name="managed-stock-test",
    )
    monkeypatch.setattr(manager, "dispatch", lambda: None)
    app = FastAPI()
    app.include_router(managed_stock_router(manager, "fixture-api"))
    url = "/api/v1/data/managed-stocks"
    try:
        with TestClient(app) as client:
            body = dict(symbol="003816", start="2012-01-01", end="2024-01-05", confirm=True)
            if include_announcements:
                body["include_announcements"] = True
            assert client.post(url, json={**body, "confirm": False}).status_code == 422
            for invalid in ["true", 1, None]:
                assert client.post(url, json={**body, "include_announcements": invalid}).status_code == 422
            assert client.post(url, json={**body, "symbols": ["600022"]}).status_code == 422
            assert client.get(url + "?page_size=201").status_code == 422
            assert client.get(url + "/missing").status_code == 404
            result = client.post(url, json=body)
            assert result.status_code == 202 and result.json()["status"] == "queued"
            assert result.json()["spec"]["include_announcements"] is include_announcements
            assert client.post(url, json={**body, "include_announcements": not include_announcements}).status_code == 409
            assert provider.calls == []
            assert client.post(url, json=body).json()["task_id"] == result.json()["task_id"]
            assert client.get(url + "?ready_only=true").json()["items"] == []
            change = dict(action="archive", confirm=True, expected_revision=9)
            assert client.patch(url + "/SZSE.003816", json=change).status_code == 409
            change["expected_revision"] = 1
            assert client.patch(url + "/SZSE.003816", json=change).json()["status"] == "archived"
            change.update(action="restore", expected_revision=2)
            assert client.patch(url + "/SZSE.003816", json=change).json()["auto_update"] is False
            assert provider.calls == []
    finally:
        manager.shutdown()


@pytest.mark.parametrize("include_announcements", [False, True])
def test_cli_enqueues_and_cannot_bypass_pool_admission(setup, capsys, monkeypatch, include_announcements):
    import sys

    from ai_quant_assistant.cli import main as cli_main

    def main(args):
        monkeypatch.setattr(sys, "argv", ["ai-quant", *args])
        cli_main()

    db, provider, _ = setup
    common = ["--data-dir", str(db.layout.root)]
    main(
        [
            "data-stock",
            "add",
            "003816",
            "--end-date",
            "2024-01-05",
            "--confirm",
            "--enqueue-only",
            *(["--include-announcements"] if include_announcements else []),
            *common,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "queued"
    assert payload["spec"]["include_announcements"] is include_announcements
    main(["data-stock", "show", "SZSE.003816", *common])
    assert '"eligible": false' in capsys.readouterr().out
    with pytest.raises(ValueError, match="not ready"):
        main(["stock-pool", "create", "--name", "blocked", "--symbols", "003816", *common])
    assert provider.calls == []


@pytest.mark.parametrize("value", ["20190826", "20190826.0", "invalid", "19800101"])
def test_listing_date_requires_explicit_valid_source(tmp_path, monkeypatch, value):
    from ai_quant_assistant.errors import ProviderError

    frame = pd.DataFrame({"symbol": ["003816"], "listing_date": [value]})
    monkeypatch.setattr(
        "ai_quant_assistant.providers.akshare_provider.load_or_fetch", lambda **kwargs: frame
    )
    provider = AkShareProvider(cache_dir=tmp_path)
    if value.startswith("2019"):
        assert provider.get_listing_date("003816")[0] == date(2019, 8, 26)
    else:
        with pytest.raises(ProviderError):
            provider.get_listing_date("003816")


def test_listing_failure_preserves_stage_and_unknown_gaps_then_retry_succeeds(setup, monkeypatch):
    db, provider, service = setup
    original = provider.get_listing_date

    def unavailable(symbol):
        raise RuntimeError("controlled listing source failure")

    monkeypatch.setattr(provider, "get_listing_date", unavailable)
    task, _ = submit(service)
    failed = service.run(task["task_id"])
    assert failed["status"] == "failed" and failed["progress"] == 100
    assert failed["completed_at"] and failed["result"]["outcome"] == "listing_date_failed"
    assert failed["result"]["stages"]["listing_date"]["status"] == "failed"
    assert failed["result"]["stages"]["daily_bar"]["status"] == "not_started"
    assert "行情同步尚未开始" in failed["message"]
    assert "上市日期查询失败" in failed["error"]
    assert provider.calls == []
    asset = service.get("SZSE.003816")
    assert asset["availability"] == "unavailable" and not asset["eligible"]
    assert asset["listing_date"] is None and asset["last_success_at"] is None
    report = asset["coverage"]
    assert not report["listing_date_verified"]
    assert not any("个未解释的交易日缺口" in issue for issue in report["issues"])
    for adjustment in ("none", "qfq"):
        assert report["adjustments"][adjustment]["missing_count"] is None
        assert report["adjustments"][adjustment]["expected_rows"] is None
        assert report["adjustments"][adjustment]["rows"] == 0
    with db.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM operation_audit WHERE action='managed_stock_sync_failed'").fetchone()[0] == 1
    with pytest.raises(ValueError, match="not ready"):
        StockPoolService(db).create("blocked by missing evidence", symbols=["003816"])

    monkeypatch.setattr(provider, "get_listing_date", original)
    retry, created = submit(service, expected_revision=asset["revision"], refresh=True)
    assert created and retry["task_id"] != task["task_id"]
    assert service.run(retry["task_id"])["status"] == "succeeded"
    assert service.tasks.get(task["task_id"]) == failed
    ready = service.get("SZSE.003816")
    assert ready["eligible"] and ready["listing_source"] == "fixture:explicit-listing-date"


def test_listing_retry_skips_already_completed_announcements(setup, monkeypatch):
    _, provider, service = setup
    original_listing = provider.get_listing_date
    announcement_calls = []

    def unavailable(_symbol):
        raise RuntimeError("controlled listing source failure")

    def announcements(*args):
        announcement_calls.append(args)
        return announcement_fixture(*args)

    monkeypatch.setattr(provider, "get_listing_date", unavailable)
    monkeypatch.setattr(provider, "get_report_announcements", announcements, raising=False)
    task, _ = submit(service, include_announcements=True)
    failed = service.run(task["task_id"])
    assert failed["status"] == "failed" and failed["progress"] == 100
    assert failed["result"]["stages"]["listing_date"]["status"] == "failed"
    assert failed["result"]["stages"]["daily_bar"]["status"] == "not_started"
    assert failed["result"]["stages"]["announcement"]["status"] == "succeeded"
    initial_announcement_calls = len(announcement_calls)
    assert initial_announcement_calls > 0

    asset = service.get("SZSE.003816")
    monkeypatch.setattr(provider, "get_listing_date", original_listing)
    monkeypatch.setattr(
        provider,
        "get_report_announcements",
        lambda *args: pytest.fail("completed announcement stage must be skipped"),
        raising=False,
    )
    retry, created = submit(
        service,
        expected_revision=asset["revision"],
        refresh=True,
        include_announcements=True,
    )
    assert created and retry["spec"]["requested_stages"] == ["listing_date", "daily_bar"]
    completed = service.run(retry["task_id"])
    assert completed["status"] == "succeeded"
    assert completed["result"]["stages"]["announcement"]["status"] == "skipped"
    assert len(announcement_calls) == initial_announcement_calls


def test_conflicting_persisted_listing_sources_block_without_overwrite(setup):
    db, provider, service = setup
    task, _ = submit(service)
    with db.connect() as connection:
        connection.execute(
            "UPDATE managed_stock SET listing_date='2024-01-02', listing_source='fixture:a'"
        )
        connection.execute(
            "UPDATE security_master SET list_date='2024-01-03' WHERE security_id='SZSE.003816'"
        )
    result = service.run(task["task_id"])
    assert result["status"] == "failed" and result["progress"] == 100
    listing_stage = result["result"]["stages"]["listing_date"]
    assert listing_stage["status"] == "failed"
    assert listing_stage["managed_stock_date"] == "2024-01-02"
    assert listing_stage["security_master_date"] == "2024-01-03"
    assert provider.calls == []
    assert service.get("SZSE.003816")["listing_date"] == "2024-01-02"


def test_heartbeat_preserves_progress_stage_and_rejects_old_owner_generation(setup):
    _, _, service = setup
    task, _ = submit(service)
    assert service.tasks.start(task["task_id"], owner_id="current", message="查询上市日期")
    service._progress(task["task_id"], "current", 40, "历史行情分区 3/8")
    service._heartbeat(task["task_id"], "current", task["spec"])
    state = service.tasks.get(task["task_id"])
    assert state["progress"] == 40 and state["message"] == "历史行情分区 3/8"
    with pytest.raises(ManagedStockConflict):
        service._heartbeat(task["task_id"], "obsolete", task["spec"])
    with pytest.raises(ManagedStockConflict):
        service._heartbeat(task["task_id"], "current", {**task["spec"], "generation": -1})
    service.change("SZSE.003816", expected_revision=1, action="archive", confirm=True)
    archived = service.tasks.get(task["task_id"])
    with pytest.raises(ManagedStockConflict):
        service._heartbeat(task["task_id"], "current", task["spec"])
    assert service.tasks.get(task["task_id"]) == archived


def test_exchange_fallback_backfills_and_reuses_persisted_evidence(setup, monkeypatch, tmp_path):
    from http.client import RemoteDisconnected
    from types import SimpleNamespace

    from ai_quant_assistant.providers.listing_dates import SSE_LISTING_SOURCE

    db, controlled, service = setup
    with db.connect() as c:
        c.execute("""INSERT INTO security_master
            (security_id,symbol,exchange,name,status,source,source_batch_id)
            VALUES ('SSE.688111','688111','SSE','测试金山办公','active','fixture','fixture')""")

    def disconnected(**kwargs):
        raise RemoteDisconnected("controlled disconnection")

    monkeypatch.setattr(AkShareProvider, "_akshare", staticmethod(
        lambda: SimpleNamespace(stock_individual_info_em=disconnected)
    ))
    monkeypatch.setattr("ai_quant_assistant.providers.akshare_provider.sse_listing_frame", lambda *args: pd.DataFrame({
        "item": ["股票代码", "上市日期"], "value": ["688111", str(controlled.listed)],
    }))
    provider = AkShareProvider(tmp_path / "listing", retries=1)
    monkeypatch.setattr(provider, "get_trading_calendar", controlled.get_trading_calendar)
    monkeypatch.setattr(provider, "get_daily_bars", controlled.get_daily_bars)
    service.provider_factory = lambda _: provider
    task, _ = service.request("688111", start=date(2012, 1, 1), end=date(2024, 1, 5), confirm=True)
    result = service.run(task["task_id"])
    assert result["status"] == "succeeded", result
    asset = service.get("SSE.688111")
    assert asset["listing_source"] == SSE_LISTING_SOURCE and asset["eligible"]
    assert all(call[1] == controlled.listed for call in controlled.calls)
    assert {call[3] for call in controlled.calls} == {"none", "qfq"}
    assert result["progress"] == 100
    monkeypatch.setattr(provider, "get_listing_date", lambda _: pytest.fail("reuse persisted evidence"))
    retry, _ = service.request("688111", start=date(2012, 1, 1), end=date(2024, 1, 5), confirm=True,
                               refresh=True, expected_revision=asset["revision"])
    assert service.run(retry["task_id"])["status"] == "succeeded"


def test_szse_official_listing_evidence_is_saved_and_allows_prices(setup, monkeypatch, tmp_path):
    from ai_quant_assistant.providers.listing_dates import SZSE_LISTING_SOURCE

    db, controlled, service = setup
    with db.connect() as connection:
        connection.execute("DELETE FROM managed_stock WHERE security_id='SZSE.000333'")
        connection.execute(
            """INSERT INTO security_master
               (security_id,symbol,exchange,name,status,source,source_batch_id)
               VALUES ('SZSE.000333','000333','SZSE','测试美的集团','active','fixture','fixture')"""
        )
    monkeypatch.setattr(
        "ai_quant_assistant.providers.akshare_provider.szse_listing_directory_frame",
        lambda *_: pd.DataFrame(
            {"symbol": ["000333"], "listing_date": ["2013-09-18"]}
        ),
    )
    controlled.listed = date(2013, 9, 18)

    def calendar():
        days = pd.bdate_range("2013-09-18", "2024-01-05")
        return pd.DataFrame(
            [
                {
                    "exchange": exchange,
                    "trade_date": day,
                    "is_open": True,
                    "previous_open_date": None,
                    "next_open_date": None,
                    "session_no": index + 1,
                    "source": "fixture:calendar",
                }
                for exchange in ("SSE", "SZSE")
                for index, day in enumerate(days)
            ]
        )

    provider = AkShareProvider(tmp_path / "listing", retries=1)
    monkeypatch.setattr(provider, "get_trading_calendar", calendar)
    monkeypatch.setattr(provider, "get_daily_bars", controlled.get_daily_bars)
    service.provider_factory = lambda _: provider
    task, _ = service.request(
        "000333", start=date(2012, 1, 1), end=date(2024, 1, 5), confirm=True
    )
    result = service.run(task["task_id"])
    assert result["status"] == "succeeded", result.get("error")
    asset = service.get("SZSE.000333")
    assert asset["listing_date"] == "2013-09-18"
    assert asset["listing_source"] == SZSE_LISTING_SOURCE
    evidence = asset["coverage"]["listing_evidence"]
    assert evidence["symbol"] == "000333"
    assert evidence["source"] == SZSE_LISTING_SOURCE
    assert evidence["fallback_used"] is False
    assert result["result"]["stages"]["listing_date"]["status"] == "succeeded"


def announcement_fixture(symbol, start, end, report_type):
    announced = date(2024, 1, 4)
    if not start <= announced <= end:
        return pd.DataFrame()
    periods = {"q1": "2023-03-31", "interim": "2023-06-30", "q3": "2023-09-30", "annual": "2023-12-31"}
    return pd.DataFrame([{
        "symbol": symbol, "company_name": "受控公告公司", "title": f"2023 {report_type} 公告",
        "report_type": report_type, "report_year": 2023,
        "report_period_end": pd.Timestamp(periods[report_type]),
        "announcement_date": pd.Timestamp(announced), "published_at": pd.Timestamp("2024-01-04 18:00:00"),
        "url": f"https://example.invalid/{symbol}/{report_type}.pdf", "source": "fixture:announcement",
    }])


def test_confirmed_combined_sync_publishes_announcements_without_pdf_and_preserves_lineage(setup, monkeypatch):
    import hashlib
    from pathlib import Path

    db, provider, service = setup
    calls = []

    def fetch(*args):
        calls.append(args)
        return announcement_fixture(*args)

    monkeypatch.setattr(provider, "get_report_announcements", fetch, raising=False)
    task, _ = service.request("003816", start=date(2024, 1, 1), end=date(2024, 1, 5),
                              confirm=True, include_announcements=True)
    done = service.run(task["task_id"])
    assert done["status"] == "succeeded", done
    assert len(calls) == 4 and {call[3] for call in calls} == {"q1", "interim", "q3", "annual"}
    report = service.get("SZSE.003816")["coverage"]["announcement_sync"]
    assert report["status"] == "published" and report["row_count"] == 4
    assert done["spec"]["datasets"] == ["daily_bar", "announcement"]
    with db.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM published_announcement WHERE security_id='SZSE.003816'").fetchone()[0] == 4
        assert c.execute("SELECT COUNT(*) FROM announcement_document").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM financial_report").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM published_announcement WHERE published_at<=TIMESTAMP '2024-01-03'").fetchone()[0] == 0
        files = c.execute("SELECT raw_path,raw_sha256 FROM research_sync_partition WHERE batch_id=?", [report["batch_id"]]).fetchall()
        requested = c.execute("SELECT details_json FROM operation_audit WHERE action='managed_stock_sync_requested'").fetchone()[0]
        assert '"announcement"' in requested
    assert all(task["task_id"] in path and Path(path).is_file() for path, _ in files)
    reused, created = service.request("003816", start=date(2024, 1, 1), end=date(2024, 1, 5),
                                      confirm=True, include_announcements=True)
    assert not created and reused["task_id"] == task["task_id"]
    retry, _ = service.request("003816", start=date(2024, 1, 1), end=date(2024, 1, 5),
                               confirm=True, include_announcements=True, refresh=True, expected_revision=1)
    assert service.run(retry["task_id"])["status"] == "succeeded"
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in files)
    with db.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM published_announcement WHERE security_id='SZSE.003816'").fetchone()[0] == 4
    summary = service.get("SZSE.003816")["coverage"]["announcement_sync"]
    only_prices, _ = service.request("003816", start=date(2024, 1, 1), end=date(2024, 1, 5),
                                    confirm=True, refresh=True, expected_revision=1)
    assert service.run(only_prices["task_id"])["status"] == "succeeded"
    assert service.get("SZSE.003816")["coverage"]["announcement_sync"] == summary
    archived = service.change("SZSE.003816", action="archive", confirm=True, expected_revision=1)
    restored = service.change("SZSE.003816", action="restore", confirm=True, expected_revision=archived["revision"])
    assert archived["coverage"]["announcement_sync"] == restored["coverage"]["announcement_sync"] == summary


def test_announcement_failure_does_not_revoke_ready_prices_and_can_retry(setup, monkeypatch):
    _, provider, service = setup
    announcement_calls = []

    def failure(*args):
        announcement_calls.append(args[-1])
        if args[-1] == "annual":
            raise RuntimeError("controlled annual source failure")
        return announcement_fixture(*args)

    monkeypatch.setattr(provider, "get_report_announcements", failure, raising=False)
    task, _ = service.request("003816", start=date(2024, 1, 1), end=date(2024, 1, 5),
                              confirm=True, include_announcements=True)
    failed = service.run(task["task_id"])
    asset = service.get("SZSE.003816")
    assert failed["status"] == "failed" and "定期公告同步失败" in failed["error"]
    assert "controlled annual source failure" in failed["error"]
    assert len(failed["error"]) < 1000
    assert failed["result"]["stages"]["daily_bar"]["status"] == "succeeded"
    assert failed["result"]["stages"]["announcement"]["status"] == "failed"
    assert asset["availability"] == "ready" and asset["eligible"]
    assert asset["coverage"]["announcement_sync"]["row_count"] == 3
    price_calls = len(provider.calls)
    first_announcement_calls = list(announcement_calls)

    def recovered(*args):
        announcement_calls.append(args[-1])
        return announcement_fixture(*args)

    monkeypatch.setattr(provider, "get_report_announcements", recovered)
    retry, _ = service.request("003816", start=date(2024, 1, 1), end=date(2024, 1, 5),
                               confirm=True, include_announcements=True, expected_revision=asset["revision"])
    assert retry["task_id"] != task["task_id"]
    assert retry["spec"]["requested_stages"] == ["announcement"]
    retried = service.run(retry["task_id"])
    assert retried["status"] == "succeeded"
    assert retried["result"]["stages"]["daily_bar"]["status"] == "skipped"
    assert len(provider.calls) == price_calls
    assert sorted(first_announcement_calls) == ["annual", "interim", "q1", "q3"]
    assert announcement_calls[len(first_announcement_calls) :] == ["annual"]


def test_announcement_outage_keeps_ready_prices_and_explains_paused_partitions(setup, monkeypatch):
    from ai_quant_assistant.providers.cninfo import AnnouncementResponseError

    _, provider, service = setup

    def blocked(*args):
        raise AnnouncementResponseError("公告查询", "http", status=403)

    monkeypatch.setattr(provider, "get_report_announcements", blocked, raising=False)
    task, _ = service.request("003816", start=date(2024, 1, 1), end=date(2024, 1, 5),
                              confirm=True, include_announcements=True)
    result = service.run(task["task_id"])
    assert result["status"] == "failed"
    assert "HTTP 403" in result["error"] and "待处理 3" in result["error"]
    assert "重新确认同步" in result["error"] and len(result["error"]) < 1000
    asset = service.get("SZSE.003816")
    assert asset["availability"] == "ready" and asset["eligible"]
    report = asset["coverage"]["announcement_sync"]
    assert report["paused_after_provider_failure"] and report["partitions"] == {"failed": 1, "planned": 3}
    assert len(report["errors"]) == 1


def test_combined_claims_deduplicate_and_never_downgrade_requested_datasets(setup):
    db, _, service = setup
    barrier = Barrier(2)

    def request(_):
        barrier.wait(timeout=10)
        return submit(ManagedStockService(db), include_announcements=True)

    with ThreadPoolExecutor(max_workers=2) as workers:
        tasks = list(workers.map(request, range(2)))
    assert len({item[0]["task_id"] for item in tasks}) == 1
    assert sum(created for _, created in tasks) == 1
    with pytest.raises(ManagedStockConflict, match="数据类型"):
        submit(service, include_announcements=False)


def test_prices_only_never_downloads_announcements_and_upgrade_request_is_not_reused(setup, monkeypatch):
    _, provider, service = setup
    monkeypatch.setattr(provider, "get_report_announcements", lambda *args: pytest.fail("not authorized"), raising=False)
    task, _ = submit(service)
    assert not task["spec"]["include_announcements"]
    assert service.run(task["task_id"])["status"] == "succeeded"
    upgraded, created = submit(service, include_announcements=True)
    assert created and upgraded["task_id"] != task["task_id"]


def test_archive_during_announcement_fetch_fences_publication(setup, monkeypatch, tmp_path):
    from ai_quant_assistant.storage.operations import DatabaseBackupService, ExclusiveOperationLock

    db, provider, service = setup
    entered, release = Event(), Event()

    def fetch(*args):
        entered.set()
        assert release.wait(15)
        return announcement_fixture(*args)

    monkeypatch.setattr(provider, "get_report_announcements", fetch, raising=False)
    task, _ = service.request("003816", start=date(2024, 1, 1), end=date(2024, 1, 5),
                              confirm=True, include_announcements=True)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(service.run, task["task_id"])
        assert entered.wait(15)
        with db.connect(read_only=True) as c:
            assert c.execute("SELECT 1").fetchone()[0] == 1
        backups = DatabaseBackupService(database=db, backup_dir=tmp_path / "announcement-backups",
                                       tasks=service.tasks, operation_lock=ExclusiveOperationLock(tmp_path / ".operations.lock"))
        try:
            backup = backups.run()
            assert backups.verify(backup["backup_id"])["status"] == "verified"
        except BaseException:
            release.set()
            raise
        service.change("SZSE.003816", expected_revision=1, action="archive", confirm=True)
        release.set()
        assert future.result(timeout=15)["status"] == "failed"
    with db.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM published_announcement").fetchone()[0] == 0
    assert service.get("SZSE.003816")["status"] == "archived"


def test_listing_failure_does_not_block_explicit_announcement_phase(setup, monkeypatch):
    _, provider, service = setup

    def fail(symbol):
        raise RuntimeError("listing unavailable")

    monkeypatch.setattr(provider, "get_listing_date", fail)
    monkeypatch.setattr(provider, "get_report_announcements", announcement_fixture, raising=False)
    task, _ = service.request("003816", start=date(2024, 1, 1), end=date(2024, 1, 5),
                              confirm=True, include_announcements=True)
    assert service.run(task["task_id"])["status"] == "failed"
    asset = service.get("SZSE.003816")
    assert asset["coverage"]["announcement_sync"]["row_count"] == 4
    assert asset["availability"] == "unavailable" and provider.calls == []


@pytest.mark.parametrize("mode", ["empty", "stale", "invalid_date"])
def test_announcement_empty_is_valid_but_stale_or_invalid_publication_is_not(setup, monkeypatch, mode):
    db, provider, service = setup

    def fetch(*args):
        if mode == "empty":
            return pd.DataFrame()
        frame = announcement_fixture(*args)
        if mode == "stale":
            frame.attrs["cache_status"] = "stale_fallback"
        else:
            frame["announcement_date"] = pd.Timestamp("2025-01-04")
            frame["published_at"] = pd.Timestamp("2025-01-04 18:00:00")
        return frame

    monkeypatch.setattr(provider, "get_report_announcements", fetch, raising=False)
    task, _ = service.request("003816", start=date(2024, 1, 1), end=date(2024, 1, 5),
                              confirm=True, include_announcements=True)
    done = service.run(task["task_id"])
    assert done["status"] == ("succeeded" if mode == "empty" else "failed")
    asset = service.get("SZSE.003816")
    assert asset["eligible"] and asset["coverage"]["announcement_sync"]["row_count"] == 0
    with db.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM published_announcement").fetchone()[0] == 0


def test_complete_day_cutoff_never_imports_unfinished_today():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from ai_quant_assistant.storage.managed_coverage import completed_date

    now = datetime(2024, 1, 5, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert completed_date(date(2024, 1, 5), now) == date(2024, 1, 4)
    assert completed_date(date(2024, 1, 5), now.replace(hour=17)) == date(2024, 1, 5)


def _process_submit(root, barrier, result):
    from pathlib import Path

    layout = DataLayout.from_root(Path(root))
    service = ManagedStockService(LocalDatabase(layout.root / "catalog.duckdb", layout))
    barrier.wait(timeout=20)
    task, created = submit(service)
    result.put((task["task_id"], created))


def test_independent_processes_share_one_database_claim(setup):
    import multiprocessing

    db, _, _ = setup
    context = multiprocessing.get_context("spawn")
    barrier, output = context.Barrier(2), context.Queue()
    processes = [
        context.Process(target=_process_submit, args=(str(db.layout.root), barrier, output))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        results = [output.get(timeout=30) for _ in processes]
        assert len({row[0] for row in results}) == 1
        assert sum(row[1] for row in results) == 1
    finally:
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        output.close()
    assert all(process.exitcode == 0 for process in processes)


def test_success_reuse_rechecks_real_coverage_and_scheduled_scope(setup):
    db, _, service = setup
    task, _ = submit(service)
    assert service.run(task["task_id"])["status"] == "succeeded"
    with db.connect() as c:
        c.execute(
            "DELETE FROM daily_bar WHERE security_id='SZSE.003816' AND adjustment='qfq' AND trade_date='2024-01-03'"
        )
    retry, created = submit(service)
    assert created and retry["task_id"] != task["task_id"]
    assert "003816" in service.scheduled_symbols()
    service.change(
        "SZSE.003816", expected_revision=1, action="configure", confirm=True, auto_update=False
    )
    assert "003816" not in service.scheduled_symbols()
    with db.connect() as c:
        c.execute("""INSERT INTO security_master (security_id,symbol,exchange,name,status,source,source_batch_id)
                     VALUES ('SSE.600022','600022','SSE','仅目录','active','fixture','fixture')""")
    assert "600022" not in service.scheduled_symbols()


def test_annual_backfill_repairs_old_hole_without_redownloading_complete_none_year(
    setup, monkeypatch
):
    db, provider, service = setup
    provider.listed = date(2023, 1, 2)
    calendar = pd.DataFrame(
        [
            dict(
                exchange=exchange,
                trade_date=day,
                is_open=True,
                previous_open_date=None,
                next_open_date=None,
                session_no=index + 1,
                source="fixture",
            )
            for exchange in ("SSE", "SZSE")
            for index, day in enumerate(pd.bdate_range("2023-01-02", "2024-01-12"))
        ]
    )
    monkeypatch.setattr(provider, "get_trading_calendar", lambda: calendar)
    task, _ = service.request("003816", end=date(2024, 1, 12), confirm=True)
    assert service.run(task["task_id"])["status"] == "succeeded"
    provider.calls.clear()
    with db.connect() as c:
        c.execute(
            "DELETE FROM daily_bar WHERE security_id='SZSE.003816' AND adjustment='qfq' AND trade_date='2023-01-10'"
        )
    retry, _ = service.request("003816", end=date(2024, 1, 12), confirm=True)
    assert service.run(retry["task_id"])["status"] == "succeeded"
    assert any(call[1].year == 2023 and call[3] == "qfq" for call in provider.calls)
    assert not any(call[1].year == 2023 and call[3] == "none" for call in provider.calls)


def test_v13_migration_with_prices_financials_and_history_preserves_every_old_row(tmp_path):
    from test_factor_backtest import _database

    db = _database(tmp_path)
    pools = StockPoolService(db)
    pool = pools.create("历史测试", symbols=["600519"])
    with db.connect() as c:
        c.execute("DROP TABLE managed_stock")
        c.execute("DELETE FROM schema_migrations WHERE version=14")
        tables = [
            row[0]
            for row in c.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_type='BASE TABLE' AND table_name<>'schema_migrations'"
            ).fetchall()
        ]
        before = {
            name: c.execute(f'SELECT * FROM "{name}" ORDER BY ALL').fetchall() for name in tables
        }
        assert apply_migrations(c) == [14]
        assert apply_migrations(c) == []
        for name, values in before.items():
            assert c.execute(f'SELECT * FROM "{name}" ORDER BY ALL').fetchall() == values
        assert (
            c.execute(
                "SELECT availability FROM managed_stock WHERE security_id='SSE.600519'"
            ).fetchone()[0]
            == "ready"
        )
        assert (
            c.execute(
                "SELECT availability FROM managed_stock WHERE security_id='SSE.601318'"
            ).fetchone()[0]
            == "unavailable"
        )
    assert pools.resolve(pool["pool_id"], revision=1)["symbols_sha256"] == pool["symbols_sha256"]
    missing = [m for m in pools.get("pool-default-research")["members"] if m["status"] == "missing"]
    assert missing and all(m["admission_warning"] for m in missing)
    with pytest.raises(ValueError, match="missing from security_master"):
        pools.duplicate("pool-default-research", expected_revision=1, name="不可悄悄复制缺失身份")


def test_screen_save_cannot_bypass_admission_and_copilot_cannot_download(tmp_path):
    from test_factor_backtest import _database
    from test_screener import _spec

    from ai_quant_assistant.ai.tool_registry import require_allowed_tool
    from ai_quant_assistant.research.screener import PointInTimeScreener

    db = _database(tmp_path)
    screener = PointInTimeScreener(db)
    run = screener.run(_spec())
    with db.connect() as c:
        c.execute("UPDATE managed_stock SET status='archived' WHERE security_id='SSE.600519'")
        before = c.execute("SELECT COUNT(*) FROM saved_watchlist").fetchone()[0]
    with pytest.raises(ValueError, match="not ready"):
        screener.save_watchlist(run["run_id"], "拒绝而非静默过滤")
    with db.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM saved_watchlist").fetchone()[0] == before
    for name in ("add_managed_stock", "archive_managed_stock", "sync_stock", "run_shell"):
        with pytest.raises(PermissionError):
            require_allowed_tool(name)


def test_ops_cli_preserves_explicit_symbols_but_default_uses_managed_selection(
    tmp_path, monkeypatch, capsys
):
    import sys

    from ai_quant_assistant.cli import main

    captured = []

    def run(self, **kwargs):
        captured.append(kwargs)
        return {"status": "succeeded"}

    monkeypatch.setattr("ai_quant_assistant.storage.operations.IncrementalSyncService.run", run)
    monkeypatch.setenv("AI_QUANT_CACHE_DIR", str(tmp_path / "cache"))
    base = [
        "ai-quant",
        "ops",
        "sync",
        "--as-of",
        "2024-01-05",
        "--data-dir",
        str(tmp_path / "data"),
    ]
    monkeypatch.setattr(sys, "argv", [*base, "--symbols", "003816"])
    main()
    monkeypatch.setattr(sys, "argv", base)
    main()
    capsys.readouterr()
    assert captured[0]["symbols"] == ("003816",)
    assert captured[1]["symbols"] is None


def test_old_generation_cannot_publish_after_restore_and_new_submission(setup):
    db, provider, service = setup
    provider.block = True
    old, _ = submit(service)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.run, old["task_id"])
        assert provider.entered.wait(10)
        service.change("SZSE.003816", expected_revision=1, action="archive", confirm=True)
        service.change("SZSE.003816", expected_revision=2, action="restore", confirm=True)
        newer, created = submit(service, expected_revision=3, auto_update=False)
        assert created
        assert (
            service.run(newer["task_id"])["status"] == "queued"
        )  # old download still owns bounded slot
        provider.release.set()
        assert future.result(timeout=15)["status"] == "failed"
    with db.connect(read_only=True) as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM published_daily_bar WHERE security_id='SZSE.003816'"
            ).fetchone()[0]
            == 0
        )
    assert service.get("SZSE.003816")["last_task_id"] == newer["task_id"]
    assert service.run(newer["task_id"])["status"] == "succeeded"


def test_v14_migration_handler_failure_is_diagnostic_and_atomic(tmp_path, monkeypatch):
    from test_factor_backtest import _database

    from ai_quant_assistant.storage import managed_coverage

    db = _database(tmp_path)
    with db.connect() as c:
        before = c.execute("SELECT * FROM daily_bar ORDER BY ALL").fetchall()
        c.execute("DROP TABLE managed_stock")
        c.execute("DELETE FROM schema_migrations WHERE version=14")
    original = managed_coverage.seed_managed_stocks

    def fail(connection):
        original(connection)
        raise ValueError("controlled management migration conflict")

    monkeypatch.setattr(managed_coverage, "seed_managed_stocks", fail)
    with pytest.raises(ValueError, match="migration conflict"):
        db.initialize()
    with db.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=14").fetchone()[0] == 0
        assert c.execute("SELECT * FROM daily_bar ORDER BY ALL").fetchall() == before
        assert not c.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='managed_stock'"
        ).fetchall()
    monkeypatch.setattr(managed_coverage, "seed_managed_stocks", original)
    assert db.initialize()["applied_migrations"] == [14]
