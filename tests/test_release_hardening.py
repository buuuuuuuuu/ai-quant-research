from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections import namedtuple
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_factor_backtest import _database
from test_platform_operations import _settings, _wait

from ai_quant_assistant import __version__
from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.cli import main
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.monitoring import (
    OperationAlertRepository,
    OperationAuditRepository,
    PlatformMonitor,
)
from ai_quant_assistant.storage.operations import DatabaseBackupService, ExclusiveOperationLock
from ai_quant_assistant.storage.schema import MIGRATIONS, SCHEMA_VERSION
from ai_quant_assistant.storage.task_repository import OperationTaskRepository


def _services(root: Path, **settings_changes):
    database = _database(root)
    settings = replace(_settings(root), **settings_changes)
    tasks = OperationTaskRepository(database)
    audit = OperationAuditRepository(database)
    alerts = OperationAlertRepository(database)
    monitor = PlatformMonitor(database, settings, alerts, audit)
    backups = DatabaseBackupService(
        database=database,
        backup_dir=settings.resolved_backup_dir,
        tasks=tasks,
        operation_lock=ExclusiveOperationLock(settings.data_dir / ".operations.lock"),
        audit=audit,
        retention_days=settings.backup_retention_days,
        minimum_count=settings.backup_minimum_count,
    )
    return database, settings, tasks, audit, alerts, monitor, backups


def test_v5_catalog_upgrades_to_v6_without_losing_tasks_or_backups(tmp_path) -> None:
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
        for migration in MIGRATIONS[:5]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                [migration.version, migration.name],
            )
        connection.execute(
            """
            INSERT INTO operation_task (task_id, task_type, status, progress, message, spec_json)
            VALUES ('existing-task', 'backup', 'succeeded', 100, 'finished', '{}')
            """
        )
        connection.execute(
            """
            INSERT INTO database_backup (backup_id, database_path, backup_path, size_bytes, sha256)
            VALUES ('existing-backup', 'catalog.duckdb', 'backup.duckdb', 12, 'digest')
            """
        )

    result = database.initialize()
    status = database.status()

    assert result["applied_migrations"] == [6]
    assert status["schema_version"] == SCHEMA_VERSION == 6
    assert status["relations"]["operation_task"] == 1
    assert status["relations"]["database_backup"] == 1
    assert status["relations"]["operation_audit"] == 0
    assert status["relations"]["operation_alert"] == 0


def test_audit_repository_removes_password_secret_and_token_fields(tmp_path) -> None:
    _, _, _, audit, _, _, _ = _services(tmp_path)

    audit.record(
        "login_succeeded",
        actor="admin",
        remote_host="203.0.113.8",
        details={"visible": "safe", "password": "secret", "session_token": "private"},
    )

    event = audit.list()[0]
    assert event["actor"] == "admin"
    assert event["remote_host"] == "203.0.113.8"
    assert event["details"] == {"visible": "safe"}


def test_backup_verification_checks_sha_size_and_database_schema(tmp_path) -> None:
    _, _, _, audit, _, _, backups = _services(tmp_path)
    created = backups.run()

    verified = backups.verify(created["backup_id"], actor="researcher")

    assert verified["status"] == "verified"
    assert verified["schema_version"] == SCHEMA_VERSION
    assert verified["sha256"] == hashlib.sha256(Path(created["backup_path"]).read_bytes()).hexdigest()
    assert audit.list()[0]["action"] == "backup_verified"


def test_backup_verification_rejects_tampered_content(tmp_path) -> None:
    _, _, _, _, _, _, backups = _services(tmp_path)
    created = backups.run()
    path = Path(created["backup_path"])
    with path.open("ab") as stream:
        stream.write(b"tampered")

    with pytest.raises(ValueError, match="integrity verification failed"):
        backups.verify(created["backup_id"])


def test_backup_verification_rejects_symbolic_links_and_external_paths(tmp_path) -> None:
    database, _, _, _, _, _, backups = _services(tmp_path)
    created = backups.run()
    source = Path(created["backup_path"])
    linked = source.with_name("linked-backup.duckdb")
    linked.symlink_to(source)
    with database.connect() as connection:
        connection.execute(
            "UPDATE database_backup SET backup_path = ? WHERE backup_id = ?",
            [str(linked), created["backup_id"]],
        )

    with pytest.raises(ValueError, match="regular file"):
        backups.verify(created["backup_id"])


def test_restore_requires_exact_confirmation_before_changing_database(tmp_path) -> None:
    database, _, _, _, _, _, backups = _services(tmp_path)
    created = backups.run()
    before = database.status()["relations"]["security_master"]

    with pytest.raises(ValueError, match="confirmation"):
        backups.restore(created["backup_id"], confirm_backup_id="incorrect")

    assert database.status()["relations"]["security_master"] == before
    assert len(backups.list()) == 1


def test_restore_creates_pre_restore_backup_and_restores_selected_snapshot(tmp_path) -> None:
    database, _, _, audit, _, _, backups = _services(tmp_path)
    created = backups.run()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO security_master (security_id, symbol, exchange, name, source, source_batch_id)
            VALUES ('SSE.688001', '688001', 'SSE', '恢复前新增', 'fixture', 'restore-test')
            """
        )

    result = backups.restore(created["backup_id"], confirm_backup_id=created["backup_id"])

    assert result["status"] == "restored"
    assert result["schema_version"] == SCHEMA_VERSION
    assert Path(result["pre_restore_backup"]["backup_path"]).is_file()
    with database.connect(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM security_master WHERE symbol = '688001'"
            ).fetchone()[0]
            == 0
        )
    assert audit.list()[0]["action"] == "backup_restored"
    assert len(backups.list()) >= 2


def test_backup_retention_defaults_to_preview_and_preserves_minimum_count(tmp_path) -> None:
    database, _, _, _, _, _, backups = _services(
        tmp_path, backup_retention_days=10, backup_minimum_count=2
    )
    records = [backups.run() for _ in range(4)]
    with database.connect() as connection:
        for index, backup in enumerate(records):
            connection.execute(
                "UPDATE database_backup SET created_at = ? WHERE backup_id = ?",
                [datetime.now(UTC) - timedelta(days=60 - index), backup["backup_id"]],
            )

    preview = backups.prune()

    assert preview["dry_run"] is True
    assert preview["candidate_count"] == 2
    assert all(Path(item["backup_path"]).is_file() for item in records)

    applied = backups.prune(dry_run=False)

    assert applied["removed_count"] == 2
    assert len(backups.list()) == 2
    assert len(backups.list(include_deleted=True)) == 4
    assert sum(not Path(item["backup_path"]).exists() for item in records) == 2


def test_backup_retention_does_not_remove_recent_files(tmp_path) -> None:
    _, _, _, _, _, _, backups = _services(tmp_path, backup_minimum_count=1)
    for _ in range(3):
        backups.run()

    applied = backups.prune(dry_run=False)

    assert applied["candidate_count"] == 0
    assert len(backups.list()) == 3


def test_backup_relocation_preserves_existing_backup_history_after_production_move(tmp_path) -> None:
    database, _, _, audit, _, _, backups = _services(tmp_path)
    created = backups.run()
    old_root = Path("/root/code/ai-quant-research/data/data")
    old_path = old_root / "backups" / Path(created["backup_path"]).name
    with database.connect() as connection:
        connection.execute(
            "UPDATE database_backup SET database_path = ?, backup_path = ? WHERE backup_id = ?",
            [str(old_root / "catalog.duckdb"), str(old_path), created["backup_id"]],
        )

    result = backups.relocate(old_root)

    assert result["updated_count"] == 1
    assert backups.verify(created["backup_id"])["status"] == "verified"
    assert any(event["action"] == "backup_paths_relocated" for event in audit.list())


def test_monitor_persists_deduplicates_acknowledges_and_resolves_task_alerts(tmp_path) -> None:
    _, _, tasks, _, alerts, monitor, _ = _services(tmp_path)
    failed = tasks.create("sync")
    tasks.update(failed["task_id"], status="failed", error="market API timeout")

    first = monitor.check()
    second = monitor.check()

    item = next(alert for alert in first["alerts"] if alert["alert_key"] == "task_failed:sync")
    assert first["new_count"] >= 1
    assert second["new_count"] == 0
    assert item["severity"] == "critical"
    assert alerts.acknowledge(item["alert_id"])["status"] == "acknowledged"
    assert monitor.check()["new_count"] == 0

    recovered = tasks.create("sync")
    tasks.update(recovered["task_id"], status="succeeded", progress=100)
    final = monitor.check()

    assert final["resolved_count"] >= 1
    assert alerts.get(item["alert_id"])["status"] == "resolved"


def test_monitor_detects_repeated_login_failures_and_redacts_credentials(tmp_path) -> None:
    _, _, _, audit, _, monitor, _ = _services(tmp_path, alert_login_failure_threshold=3)
    for _ in range(3):
        audit.record("login_failed", actor="admin", outcome="failure")

    checked = monitor.check()

    alert = next(item for item in checked["alerts"] if item["alert_key"] == "login_failures")
    assert alert["details"]["failure_count"] == 3
    assert alert["severity"] == "warning"


def test_monitor_detects_low_disk_space(tmp_path, monkeypatch) -> None:
    _, _, _, _, _, monitor, _ = _services(tmp_path, alert_disk_free_percent=10)
    usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(
        "ai_quant_assistant.storage.monitoring.shutil.disk_usage",
        lambda _: usage(total=1000, used=950, free=50),
    )

    checked = monitor.check()

    alert = next(item for item in checked["alerts"] if item["alert_key"] == "disk_space_low")
    assert alert["details"]["free_percent"] == 5.0


def test_monitor_detects_stale_backup_and_blocking_quality_failure(tmp_path) -> None:
    database, _, _, _, _, monitor, backups = _services(tmp_path, alert_backup_stale_hours=12)
    record = backups.run()
    with database.connect() as connection:
        connection.execute(
            "UPDATE database_backup SET created_at = ? WHERE backup_id = ?",
            [datetime.now(UTC) - timedelta(hours=24), record["backup_id"]],
        )
        connection.execute(
            """
            INSERT INTO dq_result (batch_id, rule_id, object_key, severity, passed)
            VALUES ('bad-batch', 'positive_price', 'SSE.600519', 'blocking', false)
            """
        )

    checked = monitor.check()

    keys = {item["alert_key"] for item in checked["alerts"]}
    assert {"backup_stale", "blocking_quality_failures"} <= keys


def test_webhook_receives_new_alert_once(tmp_path, monkeypatch) -> None:
    _, _, tasks, _, _, monitor, _ = _services(
        tmp_path, alert_webhook_url="https://alerts.example.test/receive"
    )
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def deliver(request, timeout):
        requests.append((json.loads(request.data), timeout))
        return Response()

    monkeypatch.setattr("ai_quant_assistant.storage.monitoring.urllib.request.urlopen", deliver)
    task = tasks.create("backup")
    tasks.update(task["task_id"], status="failed", error="disk write failed")

    first = monitor.check()
    monitor.check()

    assert len(requests) == 1
    assert requests[0][0]["event"] == "ai_quant_alert"
    assert first["alerts"][0]["notified_at"] is not None


def test_webhook_rejects_unencrypted_external_destinations(tmp_path, monkeypatch) -> None:
    _, _, tasks, _, _, monitor, _ = _services(
        tmp_path, alert_webhook_url="http://alerts.example.test/insecure"
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("an insecure external webhook must not be contacted")

    monkeypatch.setattr("ai_quant_assistant.storage.monitoring.urllib.request.urlopen", unexpected)
    task = tasks.create("sync")
    tasks.update(task["task_id"], status="failed", error="network unavailable")

    checked = monitor.check()

    assert checked["active_count"] >= 1


def test_quality_reports_failed_partitions_and_security_coverage(tmp_path) -> None:
    database, _, _, _, _, monitor, _ = _services(tmp_path)
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO sync_partition (
                batch_id, partition_key, security_id, adjustment,
                start_date, end_date, status, error
            ) VALUES (
                'failed-batch', 'SSE.600519:qfq:2024', 'SSE.600519', 'qfq',
                '2024-01-01', '2024-12-31', 'failed', 'provider unavailable'
            )
            """
        )

    quality = monitor.quality()

    assert quality["failed_partitions"][0]["dataset"] == "daily_bar"
    assert quality["failed_partitions"][0]["error"] == "provider unavailable"
    assert any(item["symbol"] == "600519" for item in quality["securities"])


def test_authenticated_web_exposes_alerts_quality_audit_and_backup_verification(tmp_path) -> None:
    _database(tmp_path)
    settings = _settings(
        tmp_path, web_password="a-long-password", web_session_secret="an-independent-secret"
    )
    with TestClient(create_app(settings), follow_redirects=False) as client:
        assert client.get("/api/v1/ops/alerts").status_code == 401
        assert client.get("/api/v1/ops/audit").status_code == 401
        failed = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert failed.status_code == 401
        signed_in = client.post(
            "/login", data={"username": "admin", "password": "a-long-password"}
        )
        assert signed_in.status_code == 303
        assert client.post("/api/v1/ops/alerts/check").status_code == 200
        assert client.get("/api/v1/ops/quality").status_code == 200
        assert {item["action"] for item in client.get("/api/v1/ops/audit").json()} >= {
            "login_failed",
            "login_succeeded",
        }
        submitted = client.post("/api/v1/ops/backup")
        assert submitted.status_code == 202
        task = _wait(client, submitted.json()["task_id"])
        assert task["status"] == "succeeded"
        backup_id = task["result"]["backup_id"]
        assert client.get(f"/api/v1/ops/backups/{backup_id}/verify").json()["status"] == "verified"
        rejected = client.post(
            f"/api/v1/ops/backups/{backup_id}/restore",
            json={"confirm_backup_id": "incorrect"},
        )
        assert rejected.status_code == 409
        preview = client.post("/api/v1/ops/backups/prune", json={"apply": False})
        assert preview.status_code == 200
        assert preview.json()["dry_run"] is True


def test_web_acknowledges_existing_alert_and_records_audit(tmp_path) -> None:
    _database(tmp_path)
    app = create_app(_settings(tmp_path))
    task = app.state.task_repository.create("sync")
    app.state.task_repository.update(task["task_id"], status="failed", error="provider failed")

    with TestClient(app) as client:
        checked = client.post("/api/v1/ops/alerts/check").json()
        alert = next(item for item in checked["alerts"] if item["alert_key"] == "task_failed:sync")
        acknowledged = client.post(
            f"/api/v1/ops/alerts/{alert['alert_id']}/acknowledge"
        )

        assert acknowledged.status_code == 200
        assert acknowledged.json()["status"] == "acknowledged"
        assert client.get("/api/v1/ops/audit").json()[0]["action"] == "alert_acknowledged"


def test_cli_lists_verifies_previews_and_reports_operational_health(
    tmp_path, monkeypatch, capsys
) -> None:
    _database(tmp_path)
    monkeypatch.setenv("AI_QUANT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AI_QUANT_DUCKDB_PATH", str(tmp_path / "data" / "catalog.duckdb"))

    monkeypatch.setattr(sys, "argv", ["ai-quant", "ops", "backup"])
    main()
    backup = json.loads(capsys.readouterr().out)

    commands = [
        (["backup-list"], "backups"),
        (["backup-verify", "--backup-id", backup["backup_id"]], "verified_at"),
        (["backup-prune"], "dry_run"),
        (["check"], "active_count"),
        (["alerts"], "alerts"),
        (["quality"], "securities"),
        (["audit"], "events"),
    ]
    for arguments, expected_key in commands:
        monkeypatch.setattr(sys, "argv", ["ai-quant", "ops", *arguments])
        main()
        payload = json.loads(capsys.readouterr().out)
        assert expected_key in payload


def test_low_privilege_systemd_units_use_localhost_and_protected_state_directory() -> None:
    root = Path(__file__).resolve().parents[1]
    names = ["web", "sync", "backup", "monitor"]
    for name in names:
        unit = (root / f"deploy/systemd/ai-quant-{name}.service").read_text(encoding="utf-8")
        assert "User=ai-quant" in unit
        assert "Group=ai-quant" in unit
        assert "WorkingDirectory=/opt/ai-quant/app" in unit
        assert "ProtectSystem=strict" in unit
        assert "ProtectHome=true" in unit
        assert "ReadWritePaths=/var/lib/ai-quant" in unit
        assert "User=root" not in unit
    web = (root / "deploy/systemd/ai-quant-web.service").read_text(encoding="utf-8")
    monitor = (root / "deploy/systemd/ai-quant-monitor.timer").read_text(encoding="utf-8")
    assert "--host 127.0.0.1" in web
    assert "--forwarded-allow-ips=127.0.0.1" in web
    assert "OnUnitActiveSec=15min" in monitor


def test_nginx_profiles_proxy_to_localhost_and_https_requires_real_certificate() -> None:
    root = Path(__file__).resolve().parents[1]
    http = (root / "deploy/nginx/ai-quant-http.conf").read_text(encoding="utf-8")
    https = (root / "deploy/nginx/ai-quant-https.conf").read_text(encoding="utf-8")
    assert "proxy_pass http://127.0.0.1:8080" in http
    assert "proxy_set_header X-Forwarded-For" in http
    assert "ssl_certificate /etc/letsencrypt/live/research.example.com/fullchain.pem" in https
    assert "Strict-Transport-Security" in https
    assert "return 301 https://$host$request_uri" in https


def test_ci_workflow_and_release_version_cover_all_required_checks() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    for command in (
        "uv sync --extra dev --frozen",
        "uv run ruff check .",
        "uv run pytest",
        "node --check",
        "bash -n deploy/scripts/install-production.sh",
        "systemd-analyze verify",
        "uv build --wheel",
    ):
        assert command in workflow
    assert __version__ == "0.2.0"
    assert 'version = "0.2.0"' in project


def test_production_installer_is_syntactically_valid_and_preserves_existing_catalog() -> None:
    root = Path(__file__).resolve().parents[1]
    installer = root / "deploy/scripts/install-production.sh"
    content = installer.read_text(encoding="utf-8")
    checked = subprocess.run(["bash", "-n", str(installer)], check=False, capture_output=True)

    assert checked.returncode == 0, checked.stderr.decode()
    assert 'if [[ ! -f "$state_dir/data/catalog.duckdb" ]]' in content
    assert "useradd --system" in content
    assert "/opt/ai-quant/app" in content
    assert "git clone --no-hardlinks" in content
    assert "runuser -u ai-quant" in content
    assert '"$resolved_python" == /root/*' in content
