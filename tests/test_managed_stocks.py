from __future__ import annotations

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
    assert result["status"] == "succeeded", result
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
    assert service.run(task["task_id"])["status"] == "failed"
    assert service.get("SZSE.003816")["availability"] == "partial"
    with pytest.raises(ValueError, match="not ready"):
        StockPoolService(db).create("no", symbols=["003816"])
    setattr(provider, problem, False)
    retry, created = submit(service)
    assert created and retry["task_id"] != task["task_id"]
    assert service.run(retry["task_id"])["status"] == "succeeded"
    with db.connect(read_only=True) as c:
        assert (
            c.execute("SELECT COUNT(*) FROM daily_bar WHERE security_id='SZSE.003816'").fetchone()[
                0
            ]
            == 8
        )


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
        assert apply_migrations(c) == [14]
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


def test_api_async_confirmation_limits_conflict_and_restore(setup, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ai_quant_assistant.api.managed_stock_routes import managed_stock_router
    from ai_quant_assistant.api.managed_stock_tasks import ManagedStockTaskManager

    _, provider, service = setup
    manager = ManagedStockTaskManager(service)
    monkeypatch.setattr(manager, "dispatch", lambda: None)
    app = FastAPI()
    app.include_router(managed_stock_router(manager, "fixture-api"))
    url = "/api/v1/data/managed-stocks"
    try:
        with TestClient(app) as client:
            body = dict(symbol="003816", start="2012-01-01", end="2024-01-05", confirm=True)
            assert client.post(url, json={**body, "confirm": False}).status_code == 422
            assert client.post(url, json={**body, "symbols": ["600022"]}).status_code == 422
            assert client.get(url + "?page_size=201").status_code == 422
            assert client.get(url + "/missing").status_code == 404
            result = client.post(url, json=body)
            assert result.status_code == 202 and result.json()["status"] == "queued"
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


def test_cli_enqueues_and_cannot_bypass_pool_admission(setup, capsys, monkeypatch):
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
            *common,
        ]
    )
    assert '"status": "queued"' in capsys.readouterr().out
    main(["data-stock", "show", "SZSE.003816", *common])
    assert '"eligible": false' in capsys.readouterr().out
    with pytest.raises(ValueError, match="not ready"):
        main(["stock-pool", "create", "--name", "blocked", "--symbols", "003816", *common])
    assert provider.calls == []


@pytest.mark.parametrize("value", ["20190826", "20190826.0", "invalid", "19800101"])
def test_listing_date_requires_explicit_valid_source(tmp_path, monkeypatch, value):
    from ai_quant_assistant.errors import ProviderError

    frame = pd.DataFrame({"item": ["上市时间"], "value": [value]})
    monkeypatch.setattr(
        "ai_quant_assistant.providers.akshare_provider.load_or_fetch", lambda **kwargs: frame
    )
    provider = AkShareProvider(cache_dir=tmp_path)
    if value.startswith("2019"):
        assert provider.get_listing_date("003816")[0] == date(2019, 8, 26)
    else:
        with pytest.raises(ProviderError):
            provider.get_listing_date("003816")


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
        assert c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 13
        assert c.execute("SELECT * FROM daily_bar ORDER BY ALL").fetchall() == before
        assert not c.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='managed_stock'"
        ).fetchall()
    monkeypatch.setattr(managed_coverage, "seed_managed_stocks", original)
    assert db.initialize()["applied_migrations"] == [14]
