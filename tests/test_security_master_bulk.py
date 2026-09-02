from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import duckdb
import pandas as pd
import pytest
from test_security_master_refresh import DirectoryOnlyProvider, master_rows, service_for

from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.sync_repository import SyncRepository


def full_directory():
    """Synthetic names/IDs, not a fetched market directory."""
    return pd.DataFrame(
        [
            {
                "security_id": f"SSE.{600000 + i}",
                "symbol": str(600000 + i),
                "exchange": "SSE",
                "name": f"离线测试公司{i}",
                "source": "fixture:bulk",
            }
            for i in range(6000)
        ]
    )


@pytest.fixture
def limited_database(tmp_path, monkeypatch):
    layout = DataLayout.from_root(tmp_path / "data")
    database = LocalDatabase(layout.root / "catalog.duckdb", layout)
    original_connect = database.connect

    @contextmanager
    def limited_connect(*, read_only=False):
        with original_connect(read_only=read_only) as connection:
            connection.execute("SET memory_limit = '128MB'")
            connection.execute("SET threads = 1")
            yield connection

    monkeypatch.setattr(database, "connect", limited_connect)
    database.initialize()
    return database


@pytest.mark.parametrize("threads", [1, 4])
def test_full_directory_refresh_128mb(limited_database, monkeypatch, threads):
    database = limited_database
    original_connect = database.connect

    @contextmanager
    def configured_connect(*, read_only=False):
        with original_connect(read_only=read_only) as connection:
            connection.execute(f"SET threads = {threads}")
            yield connection

    monkeypatch.setattr(database, "connect", configured_connect)
    repository = SyncRepository(database)
    seed = pd.DataFrame(
        [
            {
                "security_id": "SSE.600000",
                "symbol": "600000",
                "exchange": "SSE",
                "name": "原名称",
                "source": "fixture:seed",
                "status": "suspended",
                "list_date": date(2000, 1, 1),
                "delist_date": None,
                "industry": "原行业",
            },
            {
                "security_id": "SSE.600001",
                "symbol": "600001",
                "exchange": "SSE",
                "name": "已退市",
                "source": "fixture:seed",
                "status": "delisted",
                "list_date": date(2001, 1, 1),
                "delist_date": date(2020, 1, 1),
                "industry": None,
            },
            {
                "security_id": "SZSE.000001",
                "symbol": "000001",
                "exchange": "SZSE",
                "name": "本次未返回",
                "source": "fixture:seed",
                "status": "active",
                "list_date": None,
                "delist_date": None,
                "industry": None,
            },
        ]
    )
    repository.upsert_security_master(seed, "seed")
    with database.connect(read_only=True) as connection:
        before_pools = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
            for table in ("stock_pool", "stock_pool_member", "stock_pool_revision")
        }
    provider = DirectoryOnlyProvider(full_directory())
    service = service_for(database, provider)
    first, _ = service.prepare(actor="low-memory-test")
    result = service.run(first["task_id"])
    assert result["status"] == "succeeded", result.get("error")
    assert result["result"]["added_count"] == 5998
    assert result["result"]["updated_count"] == 2
    provider.frame = provider.frame.assign(name=lambda frame: "更新后的" + frame["name"])
    second, created = service.prepare(actor="low-memory-test")
    assert created and second["task_id"] != first["task_id"]
    repeated = service.run(second["task_id"])
    assert repeated["status"] == "succeeded", repeated.get("error")
    assert repeated["result"]["added_count"] == 0
    assert repeated["result"]["updated_count"] == 6000
    before_fallback = master_rows(database)
    placeholder = full_directory().assign(
        name=lambda frame: frame["symbol"], source="requested-symbol-fallback"
    )
    repository.upsert_security_master(placeholder, "later-price-sync")
    assert master_rows(database) == before_fallback
    with database.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM security_master").fetchone()[0] == 6001
        assert connection.execute(
            "SELECT name, status, list_date, industry FROM security_master WHERE symbol='600000'"
        ).fetchone() == ("更新后的离线测试公司0", "suspended", date(2000, 1, 1), "原行业")
        assert connection.execute(
            "SELECT status, delist_date FROM security_master WHERE symbol='600001'"
        ).fetchone() == ("delisted", date(2020, 1, 1))
        assert connection.execute(
            "SELECT name, source_batch_id FROM security_master WHERE symbol='000001'"
        ).fetchone() == ("本次未返回", "seed")
        for table, rows in before_pools.items():
            assert connection.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall() == rows
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM operation_audit WHERE action='security_master_refreshed'"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("problem", ["constraint", "date", "duplicate_id", "duplicate_key"])
def test_bulk_invalid_input_is_atomic_without_outer_transaction(limited_database, problem):
    database = limited_database
    repository = SyncRepository(database)
    frame = full_directory()
    repository.upsert_security_master(frame.iloc[:1], "seed")
    before = master_rows(database)
    frame["name"] = "这条更新也必须回滚"
    if problem == "constraint":
        frame["status"] = "active"
        frame.loc[5999, "status"] = "invalid-state"
        error = duckdb.ConstraintException
    elif problem == "date":
        frame["list_date"] = None
        frame.loc[5999, "list_date"] = "invalid-date"
        error = duckdb.ConversionException
    elif problem == "duplicate_id":
        frame.loc[5999, "security_id"] = frame.loc[0, "security_id"]
        error = ValueError
    else:
        frame.loc[5999, "symbol"] = frame.loc[0, "symbol"]
        error = ValueError
    with pytest.raises(error):
        repository.upsert_security_master(frame, "invalid")
    assert master_rows(database) == before


def test_bulk_keeps_caller_transaction_and_cleans_relation(limited_database):
    database = limited_database
    repository = SyncRepository(database)
    with database.connect() as connection:
        connection.register("caller_owned_frame", pd.DataFrame({"value": [7]}))
        before_views = connection.execute(
            "SELECT view_name FROM duckdb_views() ORDER BY 1"
        ).fetchall()
        connection.execute("BEGIN TRANSACTION")
        repository.upsert_security_master(full_directory(), "not-committed", connection=connection)
        assert connection.execute("SELECT COUNT(*) FROM security_master").fetchone()[0] == 6000
        assert (
            connection.execute("SELECT view_name FROM duckdb_views() ORDER BY 1").fetchall()
            == before_views
        )
        connection.execute("ROLLBACK")
        assert connection.execute("SELECT COUNT(*) FROM security_master").fetchone()[0] == 0
        invalid = full_directory().assign(status="invalid-state")
        connection.execute("BEGIN TRANSACTION")
        with pytest.raises(duckdb.ConstraintException):
            repository.upsert_security_master(invalid, "failed", connection=connection)
        connection.execute("ROLLBACK")
        assert (
            connection.execute("SELECT view_name FROM duckdb_views() ORDER BY 1").fetchall()
            == before_views
        )
        assert connection.execute("SELECT * FROM caller_owned_frame").fetchone() == (7,)
        connection.unregister("caller_owned_frame")


def test_bulk_publication_failure_rolls_back_and_retry_succeeds(limited_database, monkeypatch):
    database = limited_database
    service = service_for(database, DirectoryOnlyProvider(full_directory()))
    original = SyncRepository.upsert_security_master

    def fail_after_write(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("controlled failure after 6000-row write")

    monkeypatch.setattr(SyncRepository, "upsert_security_master", fail_after_write)
    first, _ = service.prepare(actor="low-memory-test")
    failed = service.run(first["task_id"])
    assert failed["status"] == "failed" and "6000-row" in failed["error"]
    assert failed["claim_key"] is None and failed["progress"] < 100
    assert failed["completed_at"] and "result" not in failed
    assert master_rows(database) == []
    with database.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sync_batch").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM operation_audit WHERE action='security_master_refreshed'"
            ).fetchone()[0]
            == 0
        )
    monkeypatch.setattr(SyncRepository, "upsert_security_master", original)
    second, created = service.prepare(actor="low-memory-test")
    assert created and second["task_id"] != first["task_id"]
    assert service.run(second["task_id"])["status"] == "succeeded"
    assert len(master_rows(database)) == 6000
