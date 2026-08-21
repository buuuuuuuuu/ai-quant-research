import json
import sys

from ai_quant_assistant.cli import main
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.fixture_data import load_fixture_data
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.schema import MIGRATIONS, SCHEMA_VERSION


def _database(tmp_path) -> LocalDatabase:
    data_dir = tmp_path / "data"
    return LocalDatabase(
        path=data_dir / "catalog.duckdb",
        layout=DataLayout.from_root(data_dir),
    )


def test_initialize_creates_layout_and_schema_idempotently(tmp_path) -> None:
    database = _database(tmp_path)

    first = database.initialize()
    second = database.initialize()
    status = database.status()

    assert first["applied_migrations"] == list(range(1, SCHEMA_VERSION + 1))
    assert second["applied_migrations"] == []
    assert status["initialized"] is True
    assert status["schema_version"] == SCHEMA_VERSION
    assert all(status["directories"].values())
    assert status["relations"]["security_master"] == 0
    assert status["relations"]["published_daily_bar"] == 0


def test_fixture_load_is_repeatable_and_only_publishes_validated_bars(tmp_path) -> None:
    database = _database(tmp_path)

    first = database.load_fixture()
    second = database.load_fixture()
    status = database.status()

    assert first == second
    assert status["relations"]["security_master"] == 3
    assert status["relations"]["trading_calendar"] == 15
    assert status["relations"]["daily_bar_raw"] == 5
    assert status["relations"]["daily_bar"] == 5
    assert status["relations"]["published_daily_bar"] == 5
    assert status["relations"]["dq_result"] == 1


def test_existing_schema_v1_database_upgrades_to_latest(tmp_path) -> None:
    database = _database(tmp_path)
    database.layout.ensure()
    database.path.parent.mkdir(parents=True, exist_ok=True)
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
        for statement in MIGRATIONS[0].statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (1, ?)",
            [MIGRATIONS[0].name],
        )

    initialized = database.initialize()

    assert initialized["applied_migrations"] == [2, 3, 4, 5]
    assert database.status()["schema_version"] == SCHEMA_VERSION
    assert database.status()["relations"]["sync_partition"] == 0
    assert database.status()["relations"]["research_sync_partition"] == 0


def test_existing_schema_v2_database_upgrades_without_losing_published_data(tmp_path) -> None:
    database = _database(tmp_path)
    database.layout.ensure()
    database.path.parent.mkdir(parents=True, exist_ok=True)
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
        for migration in MIGRATIONS[:2]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                [migration.version, migration.name],
            )
        load_fixture_data(connection)

    initialized = database.initialize()
    status = database.status()

    assert initialized["applied_migrations"] == [3, 4, 5]
    assert status["schema_version"] == SCHEMA_VERSION
    assert status["relations"]["daily_bar"] == 5
    assert status["relations"]["published_daily_bar"] == 5
    assert status["relations"]["benchmark_daily_bar"] == 0
    assert status["relations"]["announcement"] == 0
    assert status["relations"]["financial_report"] == 0
    assert status["relations"]["financial_metric"] == 0


def test_existing_schema_v3_keeps_announcements_and_accepts_quarterly_reports(tmp_path) -> None:
    database = _database(tmp_path)
    database.layout.ensure()
    database.path.parent.mkdir(parents=True, exist_ok=True)
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
        for migration in MIGRATIONS[:3]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                [migration.version, migration.name],
            )
        load_fixture_data(connection)
        connection.execute(
            """
            INSERT INTO announcement (
                announcement_id, security_id, company_name, title, report_type,
                report_year, report_period_end, announcement_date, published_at,
                url, source, source_batch_id, content_sha256
            ) VALUES (
                'existing-interim', 'SSE.600519', '贵州茅台', '2024年半年度报告',
                'interim', 2024, DATE '2024-06-30', DATE '2024-08-15',
                TIMESTAMP '2024-08-15 18:30:00', '', 'fixture', 'fixture-batch', 'abc'
            )
            """
        )

    initialized = database.initialize()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO announcement (
                announcement_id, security_id, company_name, title, report_type,
                report_year, report_period_end, announcement_date, published_at,
                url, source, source_batch_id, content_sha256
            ) VALUES (
                'new-q1', 'SSE.600519', '贵州茅台', '2024年第一季度报告',
                'q1', 2024, DATE '2024-03-31', DATE '2024-04-20',
                TIMESTAMP '2024-04-20 18:30:00', '', 'fixture', 'fixture-batch', 'def'
            )
            """
        )
        announcements = connection.execute(
            "SELECT report_type, published_at FROM published_announcement ORDER BY published_at"
        ).fetchall()

    assert initialized["applied_migrations"] == [4, 5]
    assert [item[0] for item in announcements] == ["q1", "interim"]
    assert str(announcements[1][1]) == "2024-08-15 18:30:00"


def test_cli_data_init_and_status_use_environment_paths(tmp_path, monkeypatch, capsys) -> None:
    data_dir = tmp_path / "local-data"
    database_path = data_dir / "market.duckdb"
    monkeypatch.setenv("AI_QUANT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AI_QUANT_DUCKDB_PATH", str(database_path))
    monkeypatch.setattr(sys, "argv", ["ai-quant", "data", "init", "--with-fixture"])

    main()
    initialized = json.loads(capsys.readouterr().out)

    assert initialized["status"] == "initialized"
    assert initialized["schema_version"] == SCHEMA_VERSION
    assert initialized["fixture"]["security_master"] == 3
    assert database_path.is_file()

    monkeypatch.setattr(sys, "argv", ["ai-quant", "data", "status"])
    main()
    status = json.loads(capsys.readouterr().out)

    assert status["initialized"] is True
    assert status["relations"]["published_daily_bar"] == 5
