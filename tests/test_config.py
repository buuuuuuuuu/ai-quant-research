from __future__ import annotations

import json
import sys
from pathlib import Path

from ai_quant_assistant.cli import main
from ai_quant_assistant.config import Settings
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.schema import MIGRATIONS, SCHEMA_VERSION


def _project(tmp_path: Path) -> Path:
    project_root = tmp_path / "ai-quant-research"
    (project_root / "src" / "ai_quant_assistant").mkdir(parents=True)
    (project_root / "pyproject.toml").write_text(
        '[project]\nname = "ai-quant-research"\n', encoding="utf-8"
    )
    return project_root


def _database(data_dir: Path, *, version: int = SCHEMA_VERSION, fixture: bool = False) -> Path:
    database = LocalDatabase(
        path=data_dir / "catalog.duckdb", layout=DataLayout.from_root(data_dir)
    )
    if version == SCHEMA_VERSION:
        if fixture:
            database.load_fixture()
        else:
            database.initialize()
        return database.path

    database.layout.ensure()
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
        for migration in MIGRATIONS[:version]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                [migration.version, migration.name],
            )
    return database.path


def _clear_data_environment(monkeypatch) -> None:
    for name in (
        "AI_QUANT_DATA_DIR",
        "AI_QUANT_DUCKDB_PATH",
        "AI_QUANT_ARTIFACT_DIR",
        "AI_QUANT_CACHE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_anchor_to_project_root_from_nested_working_directory(
    tmp_path, monkeypatch
) -> None:
    project_root = _project(tmp_path)
    working_directory = project_root / "data" / "nested"
    working_directory.mkdir(parents=True)
    _clear_data_environment(monkeypatch)
    monkeypatch.chdir(working_directory)

    settings = Settings.from_env()

    assert settings.data_dir == project_root / "data"
    assert settings.duckdb_path == project_root / "data" / "catalog.duckdb"
    assert settings.cache_dir == project_root / "data" / "cache"
    assert settings.artifact_dir == project_root / "artifacts"


def test_auto_detects_newer_nested_catalog_over_stale_default(tmp_path, monkeypatch) -> None:
    project_root = _project(tmp_path)
    _database(project_root / "data", version=3)
    current_catalog = _database(project_root / "data" / "data", fixture=True)
    _clear_data_environment(monkeypatch)

    for working_directory in (project_root, project_root / "data"):
        monkeypatch.chdir(working_directory)
        settings = Settings.from_env()
        assert settings.data_dir == project_root / "data" / "data"
        assert settings.duckdb_path == current_catalog


def test_auto_detects_more_complete_catalog_with_matching_schema(tmp_path, monkeypatch) -> None:
    project_root = _project(tmp_path)
    _database(project_root / "data")
    complete_catalog = _database(project_root / "data" / "data", fixture=True)
    _clear_data_environment(monkeypatch)
    monkeypatch.chdir(project_root)

    assert Settings.from_env().duckdb_path == complete_catalog


def test_prefers_canonical_catalog_when_both_are_equally_complete(tmp_path, monkeypatch) -> None:
    project_root = _project(tmp_path)
    canonical_catalog = _database(project_root / "data", fixture=True)
    _database(project_root / "data" / "data", fixture=True)
    _clear_data_environment(monkeypatch)
    monkeypatch.chdir(project_root)

    assert Settings.from_env().duckdb_path == canonical_catalog


def test_explicit_environment_paths_override_automatic_catalog_selection(
    tmp_path, monkeypatch
) -> None:
    project_root = _project(tmp_path)
    old_catalog = _database(project_root / "data", version=3)
    _database(project_root / "data" / "data", fixture=True)
    _clear_data_environment(monkeypatch)
    monkeypatch.setenv("AI_QUANT_DATA_DIR", str(project_root / "data"))
    monkeypatch.setenv("AI_QUANT_DUCKDB_PATH", str(old_catalog))
    monkeypatch.chdir(project_root)

    settings = Settings.from_env()

    assert settings.data_dir == project_root / "data"
    assert settings.duckdb_path == old_catalog


def test_explicit_database_path_selects_matching_data_directory(tmp_path, monkeypatch) -> None:
    project_root = _project(tmp_path)
    selected_catalog = _database(project_root / "data" / "data", fixture=True)
    _clear_data_environment(monkeypatch)
    monkeypatch.setenv("AI_QUANT_DUCKDB_PATH", str(selected_catalog))
    monkeypatch.chdir(project_root / "data")

    settings = Settings.from_env()

    assert settings.data_dir == selected_catalog.parent
    assert settings.duckdb_path == selected_catalog


def test_cli_status_auto_detects_nested_catalog_without_environment_exports(
    tmp_path, monkeypatch, capsys
) -> None:
    project_root = _project(tmp_path)
    _database(project_root / "data", version=3)
    selected_catalog = _database(project_root / "data" / "data", fixture=True)
    _clear_data_environment(monkeypatch)
    monkeypatch.chdir(project_root)
    monkeypatch.setattr(sys, "argv", ["ai-quant", "data", "status"])

    main()
    status = json.loads(capsys.readouterr().out)

    assert status["database_path"] == str(selected_catalog)
    assert status["schema_version"] == SCHEMA_VERSION
    assert status["relations"]["published_daily_bar"] == 5
