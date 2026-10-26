"""Terminal diagnostics, CLI automation, and financial comparison boundaries."""
from __future__ import annotations

import json
import sys

import pytest
from test_financial_platform import FakeFinancialProvider
from test_platform_operations import _enroll_price_fixture, _settings

from ai_quant_assistant.cli import main
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.financial_sync_service import FinancialDataSyncService
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.task_repository import OperationTaskRepository


def test_failure_result_and_progress_are_fenced_and_defaults_unchanged(tmp_path):
    settings = _settings(tmp_path)
    database = LocalDatabase(settings.duckdb_path, DataLayout.from_root(settings.data_dir))
    tasks = OperationTaskRepository(database)
    task = tasks.create("sync")
    task_id = task["task_id"]
    assert tasks.start(task_id, owner_id="worker", message="started")
    assert tasks.progress(task_id, owner_id="worker", progress=80, message="finance")
    result = {"status": "partial", "stages": {"daily_bar": {"status": "published"}}}
    before = tasks.get(task_id)
    assert not tasks.fail(
        task_id, owner_id="stale", message="wrong", error="wrong", result=result, progress=100
    )
    assert tasks.get(task_id) == before
    assert tasks.fail(
        task_id, owner_id="worker", message="finished with errors", error="fixture",
        result=result, progress=100,
    )
    stored = OperationTaskRepository(database).get(task_id)
    assert stored["status"] == "failed" and stored["progress"] == 100
    assert stored["result"] == result and stored["completed_at"]
    assert not tasks.progress(task_id, owner_id="worker", progress=80, message="late heartbeat")
    assert not tasks.fail(task_id, owner_id="worker", message="late", error="overwrite")
    assert not tasks.succeed(task_id, owner_id="worker", message="late success", result={})
    assert tasks.get(task_id) == stored

    # Other task types keep the original fail() behavior when no new options are used.
    other = tasks.create("sync")["task_id"]
    assert tasks.start(other, owner_id="worker", message="started")
    assert tasks.progress(other, owner_id="worker", progress=42, message="working")
    assert tasks.fail(other, owner_id="worker", message="interrupted", error="fixture")
    stored = tasks.get(other)
    assert stored["progress"] == 42 and "result" not in stored


@pytest.mark.parametrize("benchmark_fails", [False, True])
def test_cli_sync_emits_result_and_returns_nonzero_for_partial(
    tmp_path, monkeypatch, capsys, benchmark_fails
):
    class Provider(FakeFinancialProvider):
        def get_benchmark_prices(self, symbol, start_date, end_date):
            if benchmark_fails:
                raise ConnectionError("controlled benchmark failure")
            return super().get_benchmark_prices(symbol, start_date, end_date)

    settings = _settings(tmp_path, sync_symbols=("600519",))
    database = LocalDatabase(settings.duckdb_path, DataLayout.from_root(settings.data_dir))
    database.initialize()
    _enroll_price_fixture(database)
    monkeypatch.setattr("ai_quant_assistant.cli.Settings.from_env", lambda: settings)
    monkeypatch.setattr(
        "ai_quant_assistant.cli.AkShareProvider", lambda **kwargs: Provider(provider_notice=True)
    )
    monkeypatch.setattr(sys, "argv", ["ai-quant", "ops", "sync", "--as-of", "2024-08-20"])
    if benchmark_fails:
        with pytest.raises(SystemExit) as exited:
            main()
        assert exited.value.code == 1
    else:
        main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == ("partial" if benchmark_fails else "succeeded")
    stored = OperationTaskRepository(database).get(result["task_id"])
    assert stored["result"] == result and stored["progress"] == 100
    assert stored["status"] == ("failed" if benchmark_fails else "succeeded")
    # A benchmark failure does not short-circuit either subsequent stage.
    assert result["stages"]["announcement"]["status"] == "published"
    assert result["stages"]["financial_metric"]["status"] == "published"


@pytest.mark.parametrize("metric,old,new,unchanged", [
    ("book_value_per_share", 83.6092746779, 83.609269004268, True),
    ("book_value_per_share", 100.0, 100.000009, True),
    ("book_value_per_share", 100.0, 100.000011, False),
    ("book_value_per_share", 1.0, 1.00000009, True),
    ("book_value_per_share", 1.0, 1.00000011, False),
    ("book_value_per_share", 0.0, 1e-8, True),
    ("book_value_per_share", 0.0, 1.01e-8, False),
    ("book_value_per_share", 1000.0, 1000.00005, False),
    ("revenue", 100_000_000_000.0, 100_000_050_000.0, False),
    ("parent_net_profit", 100_000_000_000.0, 100_000_000_001.0, False),
    ("roe", 0.0, 9e-7, False),
    ("eps", 1.0, 1.00000001, False),
    ("roe", -1.0, -1.0, True),
    ("book_value_per_share", float("inf"), float("inf"), False),
    ("book_value_per_share", float("nan"), 1.0, False),
])
def test_financial_comparison_is_field_specific_and_bounded(metric, old, new, unchanged):
    assert FinancialDataSyncService._metrics_unchanged({metric: old}, {metric: new}) is unchanged


def test_financial_comparison_does_not_ignore_added_or_missing_metrics():
    compare = FinancialDataSyncService._metrics_unchanged
    assert not compare({"roe": 1}, {})
    assert not compare({"roe": 1}, {"roe": 1, "revenue": 10})
