import json
import sys

from ai_quant_assistant.cli import main
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.schema import SCHEMA_VERSION


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

    assert first["applied_migrations"] == [SCHEMA_VERSION]
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
