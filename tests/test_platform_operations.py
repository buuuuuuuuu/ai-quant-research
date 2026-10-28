from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from test_factor_backtest import _database
from test_financial_platform import FakeFinancialProvider

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.api.auth import SESSION_COOKIE, WebAuthenticator, safe_next_path
from ai_quant_assistant.cli import main
from ai_quant_assistant.config import Settings
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.operations import (
    DatabaseBackupService,
    ExclusiveOperationLock,
    IncrementalSyncService,
)
from ai_quant_assistant.storage.schema import MIGRATIONS, SCHEMA_VERSION
from ai_quant_assistant.storage.task_repository import OperationTaskRepository


def _settings(root: Path, **changes) -> Settings:
    settings = Settings(
        data_dir=root / "data",
        artifact_dir=root / "artifacts",
        cache_dir=root / "cache",
        duckdb_path=root / "data" / "catalog.duckdb",
        data_sync_rate_limit_seconds=0,
    )
    return replace(settings, **changes)


def _wait(client: TestClient, task_id: str) -> dict:
    for _ in range(250):
        task = client.get(f"/api/v1/ops/tasks/{task_id}").json()
        if task["status"] in {"succeeded", "failed"}:
            return task
        time.sleep(0.03)
    raise AssertionError("operation task did not finish")


def _backtest_payload() -> dict:
    return {
        "symbols": ["600519", "000001", "300750"],
        "start_date": "2024-01-02",
        "end_date": "2024-03-08",
        "factor": "roe",
        "top_n": 1,
        "rebalance": "monthly",
    }


def _enroll_price_fixture(database):
    from ai_quant_assistant.storage.managed_stocks import ManagedStockService

    with database.connect() as connection:
        connection.execute(
            """INSERT INTO security_master
               (security_id,symbol,exchange,name,list_date,source,source_batch_id)
               VALUES ('SSE.600519','600519','SSE','测试茅台','2001-08-27','fixture','fixture')"""
        )
    ManagedStockService(database).request(
        "600519", start=date(2024, 8, 13), end=date(2024, 8, 20), confirm=True, expected_revision=1,
    )


def test_v4_catalog_upgrades_without_losing_existing_market_data(tmp_path) -> None:
    directory = tmp_path / "data"
    directory.mkdir()
    database = LocalDatabase(directory / "catalog.duckdb", DataLayout.from_root(directory))
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
        for migration in MIGRATIONS[:4]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                [migration.version, migration.name],
            )
        connection.execute(
            """
            INSERT INTO security_master (security_id, symbol, exchange, name, source, source_batch_id)
            VALUES ('SSE.600519', '600519', 'SSE', '贵州茅台', 'fixture', 'fixture-batch')
            """
        )

    result = database.initialize()
    status = database.status()

    assert result["applied_migrations"] == list(range(5, SCHEMA_VERSION + 1))
    assert status["schema_version"] == SCHEMA_VERSION
    assert status["relations"]["security_master"] == 1
    assert status["relations"]["operation_task"] == 0
    assert status["relations"]["database_backup"] == 0


def test_web_login_protects_workbench_api_reports_and_openapi(tmp_path) -> None:
    _database(tmp_path)
    settings = _settings(
        tmp_path,
        web_username="researcher",
        web_password="a-long-test-password",
        web_session_secret="independent-session-signing-secret",
    )
    with TestClient(create_app(settings), follow_redirects=False) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/assets/workbench.css").status_code == 200
        assert client.get("/").status_code == 303
        assert client.get("/api/v1/data/overview").status_code == 401
        assert client.get("/api/v1/ops/status").status_code == 401
        assert client.get("/openapi.json").status_code == 401
        assert client.get("/reports/backtests/not-a-run").status_code == 303
        assert "进入澄明研究工作台" in client.get("/login").text

        failed = client.post("/login", data={"username": "researcher", "password": "wrong"})
        assert failed.status_code == 401
        assert "用户名或密码不正确" in failed.text

        logged_in = client.post(
            "/login",
            data={"username": "researcher", "password": "a-long-test-password", "next": "/"},
        )
        assert logged_in.status_code == 303
        assert "httponly" in logged_in.headers["set-cookie"].lower()
        assert "samesite=lax" in logged_in.headers["set-cookie"].lower()
        assert client.get("/").status_code == 200
        assert client.get("/api/v1/data/overview").status_code == 200
        assert client.get("/api/v1/ops/status").status_code == 200

        logged_out = client.post("/logout")
        assert logged_out.status_code == 303
        assert client.get("/api/v1/ops/status").status_code == 401


def test_web_authentication_rejects_tampering_and_rate_limits_logins(tmp_path) -> None:
    _database(tmp_path)
    settings = _settings(tmp_path, web_password="secret-password", web_session_secret="signing-key")
    with TestClient(create_app(settings), follow_redirects=False) as client:
        client.cookies.set(SESSION_COOKIE, "tampered.invalid")
        assert client.get("/api/v1/data/overview").status_code == 401
        for _ in range(5):
            response = client.post("/login", data={"username": "admin", "password": "incorrect"})
            assert response.status_code == 401
        limited = client.post("/login", data={"username": "admin", "password": "secret-password"})
        assert limited.status_code == 429


def test_login_requires_independent_signing_secret_and_rejects_external_redirects(tmp_path) -> None:
    settings = _settings(tmp_path, web_password="configured-password")
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        create_app(settings)

    assert safe_next_path("https://example.test/steal") == "/"
    assert safe_next_path("//example.test/steal") == "/"
    assert safe_next_path("/reports/backtests/example") == "/reports/backtests/example"


def test_signed_sessions_expire_after_configured_lifetime(tmp_path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        web_password="configured-password",
        web_session_secret="configured-secret",
        web_session_ttl_seconds=120,
    )
    authenticator = WebAuthenticator(settings)
    monkeypatch.setattr("ai_quant_assistant.api.auth.time.time", lambda: 1_000)
    token = authenticator.create_session()
    assert authenticator.valid_session(token)
    monkeypatch.setattr("ai_quant_assistant.api.auth.time.time", lambda: 1_121)
    assert not authenticator.valid_session(token)


def test_persistent_backtest_tasks_remain_available_after_web_restart(tmp_path) -> None:
    _database(tmp_path)
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        submitted = client.post("/api/v1/backtests", json=_backtest_payload())
        assert submitted.status_code == 202
        task = _wait(client, submitted.json()["task_id"])
        assert task["status"] == "succeeded"

    with TestClient(create_app(settings)) as restarted:
        restored = restarted.get(f"/api/v1/backtests/{task['task_id']}")
        history = restarted.get("/api/v1/backtests")

    assert restored.status_code == 200
    assert restored.json()["run_id"] == task["run_id"]
    assert history.json()[0]["task_id"] == task["task_id"]


def test_restart_marks_incomplete_tasks_failed_without_deleting_history(tmp_path) -> None:
    database = _database(tmp_path)
    repository = OperationTaskRepository(database)
    queued = repository.create("sync", spec={"as_of": "2024-08-20"})
    running = repository.create("backup")
    repository.update(running["task_id"], status="running", progress=35)
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE operation_task
            SET updated_at = CURRENT_TIMESTAMP - INTERVAL '2 hours'
            WHERE task_id IN (?, ?)
            """,
            [queued["task_id"], running["task_id"]],
        )

    with TestClient(create_app(_settings(tmp_path))) as client:
        tasks = client.get("/api/v1/ops/tasks").json()

    restored = {task["task_id"]: task for task in tasks}
    assert restored[queued["task_id"]]["status"] == "failed"
    assert restored[running["task_id"]]["status"] == "failed"
    assert "restarted" in restored[running["task_id"]]["error"]


def test_incremental_sync_updates_all_four_datasets_and_records_progress(tmp_path) -> None:
    settings = _settings(tmp_path, sync_symbols=("600519",))
    database = LocalDatabase(settings.duckdb_path, DataLayout.from_root(settings.data_dir))
    database.initialize()
    _enroll_price_fixture(database)
    repository = OperationTaskRepository(database)
    service = IncrementalSyncService(
        database=database,
        settings=settings,
        provider=FakeFinancialProvider(provider_notice=True),
        tasks=repository,
        operation_lock=ExclusiveOperationLock(settings.data_dir / ".operations.lock"),
    )

    result = service.run(as_of=date(2024, 8, 20))
    stored = repository.get(result["task_id"])
    status = database.status()

    assert result["status"] == "succeeded"
    assert set(result["stages"]) == {
        "daily_bar",
        "benchmark_daily_bar",
        "announcement",
        "financial_metric",
    }
    assert result["windows"]["daily_bar"] == {"600519": "2024-08-13"}
    assert stored["status"] == "succeeded"
    assert stored["progress"] == 100
    assert status["relations"]["published_daily_bar"] > 0
    assert status["relations"]["published_benchmark_daily_bar"] > 0
    assert status["relations"]["published_announcement"] > 0
    assert status["relations"]["published_financial_metric"] > 0


def test_web_incremental_sync_exposes_background_progress_and_dataset_status(tmp_path) -> None:
    settings = _settings(tmp_path, sync_symbols=("600519",))
    database = LocalDatabase(settings.duckdb_path, DataLayout.from_root(settings.data_dir))
    database.initialize()
    _enroll_price_fixture(database)
    app = create_app(settings)
    app.state.sync_service.provider = FakeFinancialProvider(provider_notice=True)
    app.state.managed_stock_tasks.service.provider_factory = lambda _: app.state.sync_service.provider

    with TestClient(app) as client:
        submitted = client.post("/api/v1/ops/sync", json={"as_of": "2024-08-20"})
        assert submitted.status_code == 202, submitted.text
        task = _wait(client, submitted.json()["task_id"])
        dashboard = client.get("/api/v1/ops/status").json()

    assert task["status"] == "succeeded", task
    assert task["progress"] == 100
    assert {item["dataset"] for item in dashboard["datasets"]} >= {
        "daily_bar",
        "benchmark_daily_bar",
        "announcement",
        "financial_metric",
    }


@pytest.mark.parametrize("owner_id", [None, "sync-test-worker"])
def test_partial_incremental_sync_records_provider_error_in_task_history(tmp_path, owner_id) -> None:
    class BrokenProvider(FakeFinancialProvider):
        def get_trading_calendar(self):
            raise ConnectionError("upstream trading calendar unavailable")

    settings = _settings(tmp_path, sync_symbols=("600519",))
    database = LocalDatabase(settings.duckdb_path, DataLayout.from_root(settings.data_dir))
    database.initialize()
    _enroll_price_fixture(database)
    repository = OperationTaskRepository(database)
    service = IncrementalSyncService(
        database=database,
        settings=settings,
        provider=BrokenProvider(provider_notice=True),
        tasks=repository,
        operation_lock=ExclusiveOperationLock(settings.data_dir / ".operations.lock"),
    )

    task = repository.create("sync", spec={"as_of": "2024-08-20"})
    result = service.run(as_of=date(2024, 8, 20), task_id=task["task_id"], owner_id=owner_id)

    failed = repository.list(task_type="sync")[0]
    assert result["status"] == "partial"
    assert result["failed_datasets"] == ["daily_bar"]
    assert failed["status"] == "failed"
    assert failed["progress"] == 100
    reopened = OperationTaskRepository(database).get(task["task_id"])
    assert reopened["result"] == json.loads(json.dumps(result, default=str))
    assert reopened["result"]["stages"]["financial_metric"]["batch_id"]
    assert reopened["completed_at"]
    assert "calendar unavailable" in failed["error"]
    assert database.status()["relations"]["published_financial_metric"] > 0


def test_failed_benchmark_stage_does_not_skip_remaining_datasets(tmp_path) -> None:
    class BrokenBenchmarkProvider(FakeFinancialProvider):
        def get_benchmark_prices(self, symbol, start_date, end_date):
            raise ConnectionError("benchmark upstream unavailable")

    settings = _settings(tmp_path, sync_symbols=("600519",))
    database = LocalDatabase(settings.duckdb_path, DataLayout.from_root(settings.data_dir))
    database.initialize()
    _enroll_price_fixture(database)
    repository = OperationTaskRepository(database)
    service = IncrementalSyncService(
        database=database,
        settings=settings,
        provider=BrokenBenchmarkProvider(provider_notice=True),
        tasks=repository,
        operation_lock=ExclusiveOperationLock(settings.data_dir / ".operations.lock"),
    )

    result = service.run(as_of=date(2024, 8, 20))

    stored = repository.get(result["task_id"])
    relations = database.status()["relations"]
    assert result["status"] == "partial"
    assert result["failed_datasets"] == ["benchmark_daily_bar"]
    assert stored["status"] == "failed"
    assert "benchmark_daily_bar" in stored["error"]
    assert relations["published_daily_bar"] > 0
    assert relations["published_announcement"] > 0
    assert relations["published_financial_metric"] > 0


@pytest.mark.parametrize("owner_id", [None, "sync-test-worker"])
def test_incremental_sync_preserves_all_failed_stage_results(tmp_path, owner_id) -> None:
    class DeadProvider(FakeFinancialProvider):
        def get_trading_calendar(self):
            raise ConnectionError("calendar upstream unavailable")

        def get_benchmark_prices(self, symbol, start_date, end_date):
            raise ConnectionError("benchmark upstream unavailable")

        def get_report_announcements(self, symbol, start_date, end_date, report_type):
            raise ConnectionError("announcement upstream unavailable")

        def get_interim_announcements(self, symbol, start_date, end_date):
            raise ConnectionError("announcement upstream unavailable")

        def get_financial_indicators(self, symbol, start_date, end_date):
            raise ConnectionError("financial upstream unavailable")

    settings = _settings(tmp_path, sync_symbols=("600519",))
    database = LocalDatabase(settings.duckdb_path, DataLayout.from_root(settings.data_dir))
    database.initialize()
    _enroll_price_fixture(database)
    repository = OperationTaskRepository(database)
    service = IncrementalSyncService(
        database=database,
        settings=settings,
        provider=DeadProvider(provider_notice=True),
        tasks=repository,
        operation_lock=ExclusiveOperationLock(settings.data_dir / ".operations.lock"),
    )

    task = repository.create("sync", spec={"as_of": "2024-08-20"})
    with pytest.raises(RuntimeError, match="全部同步阶段均有失败"):
        service.run(as_of=date(2024, 8, 20), task_id=task["task_id"], owner_id=owner_id)

    failed = repository.list(task_type="sync")[0]
    assert failed["status"] == "failed"
    assert "全部同步阶段均有失败" in failed["error"]
    assert failed["progress"] == 100
    assert failed["result"]["status"] == "failed"
    assert len(failed["result"]["failed_datasets"]) == 4
    assert all(stage["errors"] for stage in failed["result"]["stages"].values())


def test_backup_is_consistent_verified_and_visible_in_web_dashboard(tmp_path) -> None:
    database = _database(tmp_path)
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        submitted = client.post("/api/v1/ops/backup")
        assert submitted.status_code == 202
        task = _wait(client, submitted.json()["task_id"])
        assert task["status"] == "succeeded", task
        backups = client.get("/api/v1/ops/backups").json()
        dashboard = client.get("/api/v1/ops/status").json()

    assert len(backups) == 1
    backup = backups[0]
    path = Path(backup["backup_path"])
    assert path.parent == settings.data_dir / "backups"
    assert path.is_file()
    assert backup["exists"] is True
    assert backup["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert dashboard["backups"][0]["backup_id"] == backup["backup_id"]
    with database._duckdb().connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM security_master").fetchone()[0] == 3


def test_exclusive_operation_lock_blocks_parallel_database_mutations(tmp_path) -> None:
    first = ExclusiveOperationLock(tmp_path / "operations.lock")
    second = ExclusiveOperationLock(tmp_path / "operations.lock")
    with first.hold():
        with pytest.raises(RuntimeError, match="already running"), second.hold():
            raise AssertionError("a second operation must not acquire the lock")


def test_backup_fences_document_style_database_writes_until_copy_finishes(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    database = _database(tmp_path)
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

    def write_after_backup_starts():
        with database.connect() as connection:
            connection.execute(
                """
                INSERT INTO security_master (
                    security_id, symbol, exchange, name, source, source_batch_id
                ) VALUES ('SSE.688999', '688999', 'SSE', '并发写入', 'fixture', 'race')
                """
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        backup_future = executor.submit(backups.run)
        assert copy_started.wait(10)
        writer_future = executor.submit(write_after_backup_starts)
        time.sleep(0.1)
        assert not writer_future.done()
        release_copy.set()
        backup = backup_future.result(timeout=20)
        writer_future.result(timeout=20)

    assert backups.verify(backup["backup_id"])["status"] == "verified"
    with database._duckdb().connect(backup["backup_path"], read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM security_master WHERE symbol = '688999'"
            ).fetchone()[0]
            == 0
        )


def test_cli_backup_status_and_task_history_use_automatic_catalog(
    tmp_path, monkeypatch, capsys
) -> None:
    _database(tmp_path)
    monkeypatch.setenv("AI_QUANT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AI_QUANT_DUCKDB_PATH", str(tmp_path / "data" / "catalog.duckdb"))
    monkeypatch.setenv("AI_QUANT_BACKUP_DIR", str(tmp_path / "safe-backups"))

    monkeypatch.setattr(sys, "argv", ["ai-quant", "ops", "backup"])
    main()
    created = json.loads(capsys.readouterr().out)
    assert created["status"] == "succeeded"
    assert Path(created["backup_path"]).parent == tmp_path / "safe-backups"

    monkeypatch.setattr(sys, "argv", ["ai-quant", "ops", "status"])
    main()
    status = json.loads(capsys.readouterr().out)
    assert status["database"]["schema_version"] == SCHEMA_VERSION
    assert status["backups"][0]["backup_id"] == created["backup_id"]

    monkeypatch.setattr(sys, "argv", ["ai-quant", "ops", "tasks"])
    main()
    tasks = json.loads(capsys.readouterr().out)["tasks"]
    assert tasks[0]["status"] == "succeeded"


def test_backup_service_never_deletes_existing_backups(tmp_path) -> None:
    database = _database(tmp_path)
    repository = OperationTaskRepository(database)
    service = DatabaseBackupService(
        database=database,
        backup_dir=tmp_path / "backup-files",
        tasks=repository,
        operation_lock=ExclusiveOperationLock(tmp_path / "operations.lock"),
    )

    first = service.run()
    second = service.run()

    assert Path(first["backup_path"]).is_file()
    assert Path(second["backup_path"]).is_file()
    assert len(service.list()) == 2


def test_systemd_units_enable_authenticated_web_and_persistent_timers() -> None:
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy/systemd/ai-quant-web.service").read_text(encoding="utf-8")
    sync_timer = (root / "deploy/systemd/ai-quant-sync.timer").read_text(encoding="utf-8")
    backup_timer = (root / "deploy/systemd/ai-quant-backup.timer").read_text(encoding="utf-8")

    assert "AI_QUANT_WEB_PASSWORD" in service
    assert "AI_QUANT_WEB_SESSION_SECRET" in service
    assert "ai-quant data init" in service
    assert "18:30:00 Asia/Shanghai" in sync_timer
    assert "Persistent=true" in sync_timer
    assert "02:30:00 Asia/Shanghai" in backup_timer
