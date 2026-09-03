from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from test_factor_backtest import _database
from test_web_workbench import _settings

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.cli import main
from ai_quant_assistant.providers.akshare_provider import AkShareProvider
from ai_quant_assistant.storage.operations import DatabaseBackupService, ExclusiveOperationLock
from ai_quant_assistant.storage.security_master_refresh import SecurityMasterRefreshService
from ai_quant_assistant.storage.stock_pools import StockPoolService
from ai_quant_assistant.storage.sync_repository import SyncRepository
from ai_quant_assistant.storage.sync_service import MarketDataSyncService
from ai_quant_assistant.storage.task_repository import OperationTaskRepository


def directory(name="山东钢铁"):
    return pd.DataFrame(
        [
            {
                "security_id": "SSE.600022",
                "symbol": "600022",
                "exchange": "SSE",
                "name": name,
                "source": "fixture:directory",
            },
            {
                "security_id": "SSE.600519",
                "symbol": "600519",
                "exchange": "SSE",
                "name": "贵州茅台",
                "source": "fixture:directory",
            },
        ]
    )


class DirectoryOnlyProvider:
    def __init__(self, frame=None):
        self.frame = directory() if frame is None else frame
        self.calls = 0

    def get_security_master(self):
        self.calls += 1
        return self.frame

    def __getattr__(self, name):
        raise AssertionError(f"must not call market/calendar/document provider: {name}")


def service_for(database, provider):
    return SecurityMasterRefreshService(
        database,
        OperationTaskRepository(database),
        ExclusiveOperationLock(database.layout.root / ".operations.lock"),
        lambda _: provider,
    )


def master_rows(database):
    with database.connect(read_only=True) as connection:
        return connection.execute("SELECT * FROM security_master ORDER BY security_id").fetchall()


def test_directory_refresh_preserves_history_pools_and_listing_state(tmp_path):
    database = _database(tmp_path)
    provider = DirectoryOnlyProvider()
    service = service_for(database, provider)
    with database.connect() as connection:
        connection.execute(
            "UPDATE security_master SET status = 'delisted', delist_date = DATE '2025-01-01' WHERE symbol = '600519'"
        )
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
            for table in (
                "daily_bar",
                "announcement",
                "financial_report",
                "stock_pool",
                "stock_pool_member",
                "stock_pool_revision",
            )
        }
    task, created = service.prepare(actor="tester")
    assert created
    assert service.prepare(actor="other")[0]["task_id"] == task["task_id"]
    result = service.run(task["task_id"])
    assert result["status"] == "succeeded", result
    assert result["result"]["added_count"] == 1
    assert result["result"]["updated_count"] == 1
    assert result["progress"] == 100 and result["completed_at"] and not result["error"]
    assert result["claim_key"] is None and provider.calls == 1
    with database.connect(read_only=True) as connection:
        for table, rows in before.items():
            assert connection.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall() == rows
        assert connection.execute(
            "SELECT name, status, delist_date::VARCHAR FROM security_master WHERE symbol='600519'"
        ).fetchone() == ("贵州茅台", "delisted", "2025-01-01")
        assert connection.execute("SELECT COUNT(*) FROM security_master").fetchone()[0] == 4
        assert connection.execute(
            "SELECT dataset, status FROM sync_batch WHERE batch_id=?",
            [result["result"]["batch_id"]],
        ).fetchone() == ("security_master", "published")
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM operation_audit WHERE action='security_master_refreshed'"
            ).fetchone()[0]
            == 1
        )
    assert service.tasks.recover_interrupted(stale_after_seconds=0) == 0
    assert service.run(task["task_id"])["status"] == "succeeded"
    assert provider.calls == 1
    assert service.prepare(actor="tester")[0]["task_id"] != task["task_id"]


def test_concurrent_directory_submission_reuses_one_claim(tmp_path):
    database = _database(tmp_path)
    provider = DirectoryOnlyProvider()
    services = [service_for(database, provider) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda svc: svc.prepare(actor="tester"), services))
    assert len({task["task_id"] for task, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert provider.calls == 0
    assert services[0].run(results[0][0]["task_id"])["status"] == "succeeded"


@pytest.mark.parametrize(
    "problem", ["empty", "duplicate", "identity", "placeholder", "stale", "null", "columns"]
)
def test_directory_validation_failure_is_atomic_and_retryable(tmp_path, problem):
    database = _database(tmp_path)
    frame = directory()
    if problem == "empty":
        frame = frame.iloc[:0]
    elif problem == "duplicate":
        frame = pd.concat([frame, frame.iloc[:1]], ignore_index=True)
    elif problem == "identity":
        frame.loc[0, "security_id"] = "SZSE.600022"
    elif problem == "placeholder":
        frame.loc[0, "name"] = "600022"
    elif problem == "stale":
        frame.attrs["cache_status"] = "stale_fallback"
    elif problem == "null":
        frame.loc[0, "name"] = None
    else:
        frame = frame.drop(columns=["source"])
    provider = DirectoryOnlyProvider(frame)
    service = service_for(database, provider)
    before = master_rows(database)
    task, _ = service.prepare(actor="tester")
    failed = service.run(task["task_id"])
    assert failed["status"] == "failed", failed
    assert failed["completed_at"] and failed["error"] and failed["progress"] < 100
    assert failed["claim_key"] is None and "result" not in failed
    assert master_rows(database) == before
    provider.frame = directory()
    retry, created = service.prepare(actor="tester")
    assert created and retry["task_id"] != task["task_id"]
    assert service.run(retry["task_id"])["status"] == "succeeded"


def test_publication_failure_rolls_back_master_batch_and_audit(tmp_path, monkeypatch):
    database = _database(tmp_path)
    service = service_for(database, DirectoryOnlyProvider())
    original = SyncRepository.upsert_security_master

    def failing(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("controlled insert failure")

    monkeypatch.setattr(SyncRepository, "upsert_security_master", failing)
    before = master_rows(database)
    task, _ = service.prepare(actor="tester")
    assert service.run(task["task_id"])["status"] == "failed"
    assert master_rows(database) == before
    with database.connect(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sync_batch WHERE dataset='security_master'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM operation_audit WHERE action='security_master_refreshed'"
            ).fetchone()[0]
            == 0
        )


def test_expired_worker_cannot_publish_after_retry(tmp_path):
    database = _database(tmp_path)
    entered, release = Event(), Event()

    class SlowProvider(DirectoryOnlyProvider):
        def get_security_master(self):
            entered.set()
            assert release.wait(15)
            return directory("旧结果")

    old = service_for(database, SlowProvider())
    old.heartbeat_seconds = 3600
    task, _ = old.prepare(actor="old")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(old.run, task["task_id"])
        try:
            assert entered.wait(10)
            with database.connect() as connection:
                connection.execute(
                    "UPDATE operation_task SET heartbeat_at=? WHERE task_id=?",
                    [datetime.now(UTC) - timedelta(hours=1), task["task_id"]],
                )
            new = service_for(database, DirectoryOnlyProvider(directory("新结果")))
            replacement, created = new.prepare(actor="new")
            assert created
            assert new.run(replacement["task_id"])["status"] == "succeeded"
        finally:
            release.set()
        assert future.result(timeout=10)["status"] == "failed"
    assert StockPoolService(database).search_securities("600022")[0]["name"] == "新结果"


def test_network_stage_does_not_block_backup_or_pool_writes(tmp_path):
    database = _database(tmp_path)
    entered, release = Event(), Event()

    class SlowProvider(DirectoryOnlyProvider):
        def get_security_master(self):
            entered.set()
            assert release.wait(20)
            return directory()

    service = service_for(database, SlowProvider())
    task, _ = service.prepare(actor="tester")
    backup = DatabaseBackupService(
        database=database,
        backup_dir=tmp_path / "backups",
        tasks=service.tasks,
        operation_lock=service.operation_lock,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(service.run, task["task_id"])
        try:
            assert entered.wait(10)
            StockPoolService(database).create("网络期间可编辑", symbols=["600519"])
            saved = backup.run()
            assert backup.verify(saved["backup_id"])["status"] == "verified"
        finally:
            release.set()
        assert pending.result(timeout=10)["status"] == "succeeded"
    assert backup.verify(saved["backup_id"])["status"] == "verified"


def test_directory_does_not_expand_scheduled_sync_or_get_overwritten(tmp_path):
    database = _database(tmp_path)
    settings = _settings(tmp_path)
    app = create_app(settings)
    pools = StockPoolService(database)
    pool = pools.create("定时同步范围", symbols=["600519"])
    pools.set_default(pool["pool_id"], expected_revision=1)
    service = service_for(database, DirectoryOnlyProvider())
    task, _ = service.prepare(actor="tester")
    assert service.run(task["task_id"])["status"] == "succeeded"
    with TestClient(app):
        assert app.state.sync_service._symbols() == ("600519",)
        app.state.sync_service.settings = replace(settings, sync_symbols=("600022",))
        assert app.state.sync_service._symbols() == ("600022",)
    before = master_rows(database)
    fallback = MarketDataSyncService._missing_security_rows(("600519", "600022"), pd.DataFrame())
    SyncRepository(database).upsert_security_master(fallback, "later-price-sync")
    assert master_rows(database) == before


def test_api_requires_confirmation_and_directory_alone_cannot_admit_stock(tmp_path):
    _database(tmp_path)
    app = create_app(_settings(tmp_path))
    provider = DirectoryOnlyProvider()
    app.state.security_master_tasks.service.provider_factory = lambda _: provider
    with TestClient(app) as client:
        for body in (
            {},
            {"confirm": False},
            {"confirm": "true"},
            {"confirm": 1},
            {"confirm": True, "symbols": ["600022"]},
        ):
            assert (
                client.post("/api/v1/stock-pools/securities/refresh", json=body).status_code == 422
            )
        assert provider.calls == 0 and client.get("/api/v1/ops/tasks").json() == []
        created = client.post("/api/v1/stock-pools", json={"name": "验收池"}).json()
        url = f"/api/v1/stock-pools/{created['pool_id']}/members"
        assert (
            client.post(url, json={"expected_revision": 1, "symbols": ["600022"]}).status_code
            == 422
        )
        submitted = client.post("/api/v1/stock-pools/securities/refresh", json={"confirm": True})
        assert submitted.status_code == 202, submitted.text
        task_id = submitted.json()["task_id"]
        for _ in range(100):
            task = client.get(f"/api/v1/ops/tasks/{task_id}").json()
            if task["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.03)
        assert task["status"] == "succeeded", task
        assert (
            client.get("/api/v1/stock-pools/securities/search", params={"query": "600022"}).json()[
                0
            ]["name"]
            == "山东钢铁"
        )
        added = client.post(url, json={"expected_revision": 1, "symbols": ["600022"]})
        assert added.status_code == 422 and "not ready" in added.json()["detail"]
        assert client.get(f"/api/v1/stock-pools/{created['pool_id']}").json()["current_revision"] == 1
        home = client.get("/").text
        assert "更新证券主数据" in home
        assert '/assets/workbench.js?v=0.2.0"' not in home


def test_cli_confirmation_and_directory_only_refresh(tmp_path, monkeypatch, capsys):
    _database(tmp_path)
    calls = []
    monkeypatch.setattr(
        AkShareProvider, "get_security_master", lambda self: calls.append(True) or directory()
    )
    args = ["ai-quant", "data", "sync-security-master", "--data-dir", str(tmp_path / "data")]
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2 and calls == []
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", [*args, "--confirm"])
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "succeeded" and len(calls) == 1
