from contextlib import contextmanager
from datetime import date

import duckdb
import pandas as pd
import pytest

from ai_quant_assistant.providers.normalization import normalize_trading_calendar
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.sync_repository import SyncRepository


def calendar(periods=9000):
    dates = pd.bdate_range("1990-01-01", periods=periods)
    return normalize_trading_calendar(pd.DataFrame({"trade_date": dates}), "fixture:calendar")


def test_bulk_calendar_preserves_all_rows_edges_and_replace_semantics(tmp_path, monkeypatch):
    layout = DataLayout.from_root(tmp_path)
    database = LocalDatabase(layout.root / "catalog.duckdb", layout)
    database.initialize()
    original = database.connect

    class NoRowWrites:
        def __init__(self, connection):
            self.connection = connection

        def executemany(self, *args, **kwargs):
            pytest.fail("calendar must not write one row at a time under the database lock")

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def bounded_connection(*, read_only=False):
        with original(read_only=read_only) as connection:
            connection.execute("SET memory_limit='128MB'")
            connection.execute("SET threads=1")
            yield NoRowWrites(connection)

    monkeypatch.setattr(database, "connect", bounded_connection)
    repository = SyncRepository(database)
    frame = calendar()
    repository.upsert_trading_calendar(frame, "original")
    with original(read_only=True) as connection:
        actual = connection.execute("SELECT * FROM trading_calendar ORDER BY exchange, trade_date").fetchdf()
    expected = frame.copy()
    expected["source_batch_id"] = "original"
    for column in ("trade_date", "previous_open_date", "next_open_date"):
        actual[column] = actual[column].astype("datetime64[ns]")
        expected[column] = expected[column].astype("datetime64[ns]")
    pd.testing.assert_frame_equal(
        actual[expected.columns].reset_index(drop=True),
        expected.sort_values(["exchange", "trade_date"]).reset_index(drop=True),
        check_dtype=False,
    )
    replacement = pd.concat([frame.iloc[[0]], frame.iloc[[0]]], ignore_index=True)
    replacement.loc[0, "source"] = "first"
    replacement.loc[1, "source"] = "last"
    repository.upsert_trading_calendar(replacement, "replacement")
    with original(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0] == 27000
        assert connection.execute("SELECT source, source_batch_id FROM trading_calendar WHERE exchange='SSE' AND trade_date=?", [date(1990, 1, 1)]).fetchone() == ("last", "replacement")
        before = connection.execute("SELECT * FROM trading_calendar ORDER BY ALL").fetchall()
    broken = frame.iloc[:2].copy()
    broken.loc[1, "exchange"] = None
    with pytest.raises(duckdb.ConstraintException):
        repository.upsert_trading_calendar(broken, "must-not-publish")
    repository.upsert_trading_calendar(pd.DataFrame(), "empty")
    with original(read_only=True) as connection:
        assert connection.execute("SELECT * FROM trading_calendar ORDER BY ALL").fetchall() == before
