from __future__ import annotations

import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event

import duckdb
import pytest
from fastapi.testclient import TestClient
from test_factor_backtest import _database
from test_web_workbench import _settings

from ai_quant_assistant.ai.copilot import CopilotService
from ai_quant_assistant.ai.planner import RuleBasedPlanner
from ai_quant_assistant.ai.preflight import CoveragePreflight
from ai_quant_assistant.ai.repository import CopilotRepository
from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.api.stock_pool_tasks import StockPoolSyncTaskManager
from ai_quant_assistant.cli import main
from ai_quant_assistant.domain.models import DEFAULT_RESEARCH_SYMBOLS, FactorBacktestSpec
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.monitoring import OperationAuditRepository
from ai_quant_assistant.storage.operations import DatabaseBackupService, ExclusiveOperationLock
from ai_quant_assistant.storage.schema import MIGRATIONS, SCHEMA_VERSION
from ai_quant_assistant.storage.stock_pools import StockPoolConflictError, StockPoolService
from ai_quant_assistant.storage.task_repository import OperationTaskRepository


def _add_security(database: LocalDatabase, symbol: str, *, status: str = "active") -> None:
    exchange = "SSE" if symbol.startswith("6") else "SZSE"
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO security_master (
                security_id, symbol, exchange, name, status, source, source_batch_id
            ) VALUES (?, ?, ?, ?, ?, 'test', 'test-batch')
            ON CONFLICT DO NOTHING
            """,
            [f"{exchange}.{symbol}", symbol, exchange, f"测试{symbol}", status],
        )
        if status == "active":
            from managed_test_data import admit_fixture

            admit_fixture(connection, [symbol])


def test_stock_pool_crud_revisions_freeze_and_audit(tmp_path) -> None:
    database = _database(tmp_path)
    _add_security(database, "600000")
    service = StockPoolService(database)
    pool = service.create("我的股票池", symbols=["600519"], actor="tester")
    frozen = service.resolve(pool["pool_id"], revision=1)

    unchanged = service.add_members(
        pool["pool_id"], ["600519"], expected_revision=1, actor="tester"
    )
    assert unchanged["current_revision"] == 1
    updated = service.add_members(
        pool["pool_id"], ["600000"], expected_revision=1, actor="tester"
    )
    assert updated["symbols"] == ["600519", "600000"]
    refrozen = service.resolve(pool["pool_id"], revision=1)
    assert refrozen["resolved_symbols"] == frozen["resolved_symbols"]
    assert refrozen["symbols_sha256"] == frozen["symbols_sha256"]

    member = next(item for item in updated["members"] if item["symbol"] == "600000")
    updated = service.update_member(
        pool["pool_id"],
        member["security_id"],
        expected_revision=2,
        note="银行样本",
        display_order=1,
        actor="tester",
    )
    assert updated["current_revision"] == 3
    assert updated["members"][0]["note"] == "银行样本"
    updated = service.remove_member(
        pool["pool_id"], member["security_id"], expected_revision=3, actor="tester"
    )
    duplicate = service.duplicate(
        pool["pool_id"], expected_revision=4, name="股票池副本", actor="tester"
    )
    default_pool = service.set_default(
        duplicate["pool_id"], expected_revision=1, actor="tester"
    )
    assert service.resolve()["pool_id"] == duplicate["pool_id"]
    with pytest.raises(ValueError, match="must retain"):
        service.remove_member(
            duplicate["pool_id"],
            default_pool["members"][0]["security_id"],
            expected_revision=default_pool["current_revision"],
        )
    archived = service.archive(pool["pool_id"], expected_revision=4, actor="tester")
    with pytest.raises(ValueError, match="archived"):
        service.add_members(
            pool["pool_id"], ["600000"], expected_revision=archived["current_revision"]
        )
    assert len(service.revisions(pool["pool_id"])) == 5
    with database.connect(read_only=True) as connection:
        actions = {
            str(row[0])
            for row in connection.execute(
                "SELECT action FROM operation_audit WHERE object_type = 'stock_pool'"
            ).fetchall()
        }
    assert {
        "stock_pool_created",
        "stock_pool_member_added",
        "stock_pool_member_removed",
        "stock_pool_duplicated",
        "stock_pool_archived",
        "stock_pool_default_changed",
    } <= actions


def test_validation_capacity_and_optimistic_concurrency(tmp_path) -> None:
    database = _database(tmp_path)
    _add_security(database, "600000", status="delisted")
    service = StockPoolService(database)
    pool = service.create("验证池")
    with pytest.raises(ValueError, match="missing from security_master"):
        service.add_members(pool["pool_id"], ["600001"], expected_revision=1)
    with pytest.raises(ValueError, match="not active"):
        service.add_members(pool["pool_id"], ["600000"], expected_revision=1)
    with pytest.raises(ValueError, match="six-digit"):
        service.add_members(pool["pool_id"], ["bad"], expected_revision=1)
    with pytest.raises(ValueError, match="at most 200"):
        service.add_members(
            pool["pool_id"], [f"{index:06d}" for index in range(201)], expected_revision=1
        )

    changed = service.update(pool["pool_id"], expected_revision=1, description="新描述")
    with pytest.raises(StockPoolConflictError):
        service.update(pool["pool_id"], expected_revision=1, name="旧页面")
    assert changed["current_revision"] == 2

    symbols = [f"{100000 + index:06d}" for index in range(500)]
    with database.connect() as connection:
        connection.executemany(
            """
            INSERT INTO security_master (
                security_id, symbol, exchange, name, status, source, source_batch_id
            ) VALUES (?, ?, 'SZSE', ?, 'active', 'test', 'capacity')
            """,
            [(f"SZSE.{symbol}", symbol, f"测试{symbol}") for symbol in symbols],
        )
        from managed_test_data import admit_fixture

        admit_fixture(connection, symbols)
    full = service.create("容量池", symbols=symbols[:200])
    full = service.add_members(full["pool_id"], symbols[200:400], expected_revision=1)
    full = service.add_members(full["pool_id"], symbols[400:], expected_revision=2)
    assert full["member_count"] == 500
    with pytest.raises(ValueError, match="at most 500"):
        service.add_members(full["pool_id"], ["600519"], expected_revision=3)


def test_concurrent_identical_add_has_one_member_and_revision(tmp_path) -> None:
    database = _database(tmp_path)
    service = StockPoolService(database)
    pool = service.create("并发池")

    def add() -> str:
        try:
            service.add_members(pool["pool_id"], ["600519"], expected_revision=1)
            return "added"
        except StockPoolConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: add(), range(2)))
    final = service.get(pool["pool_id"])
    assert sorted(outcomes) == ["added", "conflict"]
    assert final["symbols"] == ["600519"]
    assert final["current_revision"] == 2
    assert len(service.revisions(pool["pool_id"])) == 2


def test_api_default_resolution_validation_and_conflict(tmp_path) -> None:
    _database(tmp_path)
    with TestClient(create_app(_settings(tmp_path))) as client:
        created = client.post(
            "/api/v1/stock-pools", json={"name": "API 池", "symbols": ["600519"]}
        )
        pool = created.json()
        duplicate = client.post(
            f"/api/v1/stock-pools/{pool['pool_id']}/members",
            json={"expected_revision": 1, "symbols": ["600519"]},
        )
        changed = client.patch(
            f"/api/v1/stock-pools/{pool['pool_id']}",
            json={"expected_revision": 1, "description": "已更新"},
        )
        stale = client.patch(
            f"/api/v1/stock-pools/{pool['pool_id']}",
            json={"expected_revision": 1, "name": "旧页面"},
        )
        invalid = client.post(
            "/api/v1/screens/preflight",
            json={
                "name": "非法",
                "as_of": "2024-01-02",
                "stock_pool_id": pool["pool_id"],
                "symbols": ["600519"],
                "conditions": [{"field": "roe", "operator": "gte", "value": 1}],
            },
        )
        missing = client.post(
            "/api/v1/screens/preflight",
            json={
                "name": "缺失",
                "as_of": "2024-01-02",
                "stock_pool_id": "pool-missing",
                "conditions": [{"field": "roe", "operator": "gte", "value": 1}],
            },
        )
    assert created.status_code == 201
    assert duplicate.status_code == 200 and duplicate.json()["current_revision"] == 1
    assert changed.status_code == 200
    assert stale.status_code == 409
    assert invalid.status_code == 422
    assert missing.status_code == 404


def test_cli_and_frozen_research_scope(tmp_path, monkeypatch, capsys) -> None:
    database = _database(tmp_path)
    monkeypatch.setenv("AI_QUANT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AI_QUANT_DUCKDB_PATH", str(database.path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["ai-quant", "stock-pool", "create", "--name", "CLI 池", "--symbols", "600519"],
    )
    main()
    created = json.loads(capsys.readouterr().out)
    frozen = StockPoolService(database).resolve(created["pool_id"], revision=1)
    StockPoolService(database).add_members(
        created["pool_id"], ["000001"], expected_revision=1
    )
    spec = FactorBacktestSpec(
        symbols=tuple(frozen["resolved_symbols"]),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 3, 8),
        top_n=1,
        stock_pool=frozen,
    )
    assert spec.to_dict()["symbols"] == ["600519"]
    assert spec.to_dict()["stock_pool"]["pool_revision"] == 1


def test_copilot_lists_and_freezes_an_explicit_stock_pool(tmp_path) -> None:
    database = _database(tmp_path)
    pools = StockPoolService(database)
    pool = pools.create("Copilot 池", symbols=["600519", "000001"])
    service = CopilotService(
        repository=CopilotRepository(database),
        planner=RuleBasedPlanner(),
        preflight=CoveragePreflight(database),
        stock_pools=pools,
    )
    conversation = service.create_conversation()
    plan = service.propose(
        conversation["conversation_id"],
        f"用股票池 {pool['pool_id']} 版本 1 从 2024-01-02 到 2024-03-08 做 ROE 回测",
    )
    assert plan["spec"]["stock_pool"]["pool_id"] == pool["pool_id"]
    assert plan["spec"]["stock_pool"]["pool_revision"] == 1
    assert plan["spec"]["symbols"] == ["600519", "000001"]
    assert RuleBasedPlanner().plan("列出股票池")["tool_name"] == "list_stock_pools"


def test_sync_claim_is_deduplicated_and_failed_task_can_retry(tmp_path, monkeypatch) -> None:
    database = _database(tmp_path)
    pools = StockPoolService(database)
    pool = pools.create("同步池", symbols=["600519"])
    tasks = OperationTaskRepository(database)
    manager = StockPoolSyncTaskManager(
        pools=pools,
        repository=tasks,
        sync_service=object(),  # type: ignore[arg-type]
        audit=OperationAuditRepository(database),
    )
    submitted: list[tuple] = []
    monkeypatch.setattr(manager.executor, "submit", lambda *args: submitted.append(args))
    first = manager.submit(
        pool["pool_id"], expected_revision=1, as_of=date(2024, 1, 2), actor="tester"
    )
    second = manager.submit(
        pool["pool_id"], expected_revision=1, as_of=date(2024, 1, 2), actor="tester"
    )
    assert first["task_id"] == second["task_id"] and len(submitted) == 1
    assert tasks.start(first["task_id"], owner_id="old", message="start")
    assert tasks.fail(first["task_id"], owner_id="old", message="failed", error="controlled")
    retry = manager.submit(
        pool["pool_id"], expected_revision=1, as_of=date(2024, 1, 2), actor="tester"
    )
    assert retry["task_id"] != first["task_id"] and len(submitted) == 2
    assert tasks.start(retry["task_id"], owner_id="new", message="start")
    assert not tasks.succeed(first["task_id"], owner_id="old", message="stale", result={})
    manager.shutdown()


def test_pool_write_and_backup_are_serialized(tmp_path, monkeypatch) -> None:
    database = _database(tmp_path)
    settings = _settings(tmp_path)
    backups = DatabaseBackupService(
        database=database,
        backup_dir=settings.resolved_backup_dir,
        tasks=OperationTaskRepository(database),
        operation_lock=ExclusiveOperationLock(settings.data_dir / ".operations.lock"),
    )
    started, release = Event(), Event()
    original = shutil.copy2

    def slow_copy(source, target):
        started.set()
        assert release.wait(10)
        return original(source, target)

    monkeypatch.setattr("ai_quant_assistant.storage.operations.shutil.copy2", slow_copy)
    pools = StockPoolService(database)
    with ThreadPoolExecutor(max_workers=2) as executor:
        backup_future = executor.submit(backups.run)
        assert started.wait(10)
        write_future = executor.submit(pools.create, "备份期间", symbols=["600519"])
        time.sleep(0.1)
        assert not write_future.done()
        release.set()
        backup = backup_future.result(timeout=20)
        created = write_future.result(timeout=20)
    assert created["pool_id"]
    assert backups.verify(backup["backup_id"])["status"] == "verified"


def test_v12_to_v13_watchlist_migration_preserves_data_and_is_repeatable(tmp_path) -> None:
    path = tmp_path / "catalog.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY, name VARCHAR NOT NULL,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
        )
        for migration in MIGRATIONS:
            if migration.version > 12:
                break
            for statement in migration.statements:
                connection.execute(statement)
            if migration.handler:
                migration.handler(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                [migration.version, migration.name],
            )
        connection.execute(
            """
            INSERT INTO security_master (
                security_id, symbol, exchange, name, status, source, source_batch_id
            ) VALUES ('SSE.600519', '600519', 'SSE', '贵州茅台', 'active', 'test', 'test')
            """
        )
        connection.execute(
            """
            INSERT INTO saved_watchlist (
                watchlist_id, name, screen_run_id, as_of, symbols_json,
                history_verified, warning, created_at
            ) VALUES ('watchlist-history', '历史筛选', 'screen-history', '2024-01-02',
                      '["600519"]', TRUE, '可信快照', '2024-01-03')
            """
        )
    database = LocalDatabase(path, DataLayout.from_root(tmp_path / "data"))
    first, second = database.initialize(), database.initialize()
    migrated = StockPoolService(database).get("watchlist-history")
    assert first["schema_version"] == SCHEMA_VERSION == 16
    assert first["applied_migrations"] == [13, 14, 15, 16] and second["applied_migrations"] == []
    assert migrated["source_type"] == "screen_snapshot"
    assert migrated["source_screen_run_id"] == "screen-history"
    assert migrated["symbols"] == ["600519"] and migrated["history_verified"] is True
    assert StockPoolService(database).get("pool-default-research")["symbols_sha256"]
    with database.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM saved_watchlist").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0] == 2
        symbols = json.loads(
            connection.execute(
                "SELECT symbols_json FROM stock_pool_revision WHERE pool_id='pool-default-research'"
            ).fetchone()[0]
        )
    assert symbols == list(DEFAULT_RESEARCH_SYMBOLS)


def test_v13_migration_rejects_invalid_watchlist_without_data_loss(tmp_path) -> None:
    path = tmp_path / "invalid.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY, name VARCHAR NOT NULL,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
        )
        for migration in MIGRATIONS:
            if migration.version > 12:
                break
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                [migration.version, migration.name],
            )
        connection.execute(
            """
            INSERT INTO saved_watchlist (
                watchlist_id, name, screen_run_id, as_of, symbols_json
            ) VALUES ('broken', '损坏记录', 'screen', '2024-01-01', 'not-json')
            """
        )
    database = LocalDatabase(path, DataLayout.from_root(tmp_path / "data"))
    with pytest.raises(ValueError, match="invalid JSON"):
        database.initialize()
    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM saved_watchlist").fetchone()[0] == 1
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12
