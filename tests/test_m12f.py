from __future__ import annotations

import time
from datetime import date
from threading import Event

import duckdb
import pandas as pd
import pytest

from ai_quant_assistant.api.backtest_tasks import BacktestTaskManager
from ai_quant_assistant.api.schemas import DailySignalStrategyRequest
from ai_quant_assistant.domain.models import (
    DailySignalRule,
    DailySignalStrategySpec,
    FactorComponent,
)
from ai_quant_assistant.reports.daily_signal_writer import daily_signal_trust_summary
from ai_quant_assistant.research.readiness import ResearchReadiness
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.operations import ExclusiveOperationLock
from ai_quant_assistant.storage.schema import MIGRATIONS, apply_migrations
from ai_quant_assistant.storage.task_repository import OperationTaskRepository


def database(tmp_path):
    layout = DataLayout.from_root(tmp_path / "data")
    db = LocalDatabase(layout.root / "catalog.duckdb", layout)
    db.initialize()
    return db


def spec():
    return DailySignalStrategySpec(
        symbols=("600519",), start_date=date(2024, 1, 2), end_date=date(2024, 1, 10),
        candidate_factors=(FactorComponent("return_1"),), candidate_count=1,
        candidate_refresh="daily", buy_rules=(DailySignalRule("close_vs_ma", "above", window=2),),
        sell_rules=(DailySignalRule("close_vs_ma", "below", window=2),), max_positions=1,
    )


class FixtureService:
    def __init__(self):
        self.called = Event()
        self.calls = 0

    def run(self, value, progress_callback):
        self.calls += 1
        self.called.set()
        progress_callback(50, "正在计算")
        return {"run_id": "daily-signal-20240110T000000Z-12345678"}


def test_schema_18_preserves_v17_tasks_and_allows_cancel(tmp_path):
    path = tmp_path / "v17.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name VARCHAR,applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        for migration in MIGRATIONS[:17]:
            for statement in migration.statements:
                connection.execute(statement)
            if migration.handler:
                migration.handler(connection)
            connection.execute("INSERT INTO schema_migrations(version,name) VALUES (?,?)", [migration.version, migration.name])
        connection.execute("INSERT INTO operation_task(task_id,task_type,status,spec_json) VALUES ('old-queued','backtest','queued','{\"research_type\":\"daily_signal\"}')")
        connection.execute("INSERT INTO operation_task(task_id,task_type,status) VALUES ('pool-queued','stock_pool_sync','queued')")
        assert apply_migrations(connection) == [18]
        assert connection.execute("SELECT status FROM operation_task WHERE task_id='old-queued'").fetchone()[0] == "queued"
        assert connection.execute("SELECT task_type FROM operation_task WHERE task_id='pool-queued'").fetchone()[0] == "stock_pool_sync"
        connection.execute("UPDATE operation_task SET status='cancelled' WHERE task_id='old-queued'")
        assert apply_migrations(connection) == []


def test_cancel_cas_and_restart_dispatch_reuse_original_task(tmp_path):
    db = database(tmp_path)
    tasks = OperationTaskRepository(db)
    queued, created = tasks.create_or_get_active("backtest", claim_key="daily-signal:fixture", spec={
        **spec().to_dict(), "research_type": "daily_signal"
    }, message="等待执行")
    assert created
    tasks.recover_interrupted(stale_after_seconds=0)
    assert tasks.get(queued["task_id"])["status"] == "queued"
    service = FixtureService()
    manager = BacktestTaskManager(service, tasks, ExclusiveOperationLock(tmp_path / "operations.lock"), task_type="daily_signal")
    try:
        manager.start()
        assert service.called.wait(5)
        deadline = time.monotonic() + 5
        while tasks.get(queued["task_id"])["status"] != "succeeded" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert tasks.get(queued["task_id"])["status"] == "succeeded"
        assert service.calls == 1
        with pytest.raises(ValueError, match="只有等待执行"):
            manager.cancel(queued["task_id"])
    finally:
        manager.shutdown()

    another = tasks.create("backtest", spec={"research_type": "daily_signal"})
    cancelled = tasks.cancel_queued(another["task_id"], research_type="daily_signal")
    assert cancelled["status"] == "cancelled"
    assert not tasks.start(another["task_id"], owner_id="late-worker", message="运行")
    other_type = tasks.create("backtest", spec={"research_type": "factor_lab"})
    with pytest.raises(KeyError):
        tasks.cancel_queued(other_type["task_id"], research_type="daily_signal")


def test_attribution_uses_only_executed_orders_and_saved_nav():
    nav = pd.DataFrame([
        {"date": "2024-01-02", "portfolio_nav": 1.0, "cash": 1000, "position_value": 0, "portfolio_value": 1000},
        {"date": "2024-01-03", "portfolio_nav": 1.1, "cash": 500, "position_value": 600, "portfolio_value": 1100},
        {"date": "2024-01-04", "portfolio_nav": 0.9, "cash": 400, "position_value": 500, "portfolio_value": 900},
    ])
    orders = pd.DataFrame([
        {"symbol": "600519", "side": "buy", "status": "executed", "notional": 500, "cost": 2},
        {"symbol": "600519", "side": "sell", "status": "blocked", "notional": 900, "cost": 0},
    ])
    summary = daily_signal_trust_summary(
        spec={"symbols": ["600519"], "initial_capital": 1000,
              "scope_adjustment": {"excluded": [{"symbol": "000333", "reason": "未知缺口"}]}},
        metrics={"final_value": 900, "signal_count": 2, "total_transaction_cost": 2},
        nav=nav, orders=orders, diagnostics={"open_exit_intents": []}, reproducibility={},
    )
    assert summary["executed_count"] == 1 and summary["unfilled_count"] == 1
    assert summary["per_symbol"] == [{
        "symbol": "600519", "buy_count": 1, "sell_count": 0,
        "buy_notional": 500.0, "sell_notional": 0.0, "total_cost": 2.0,
    }]
    assert summary["drawdown_period"]["peak_date"] == "2024-01-03"
    assert summary["drawdown_period"]["trough_date"] == "2024-01-04"
    assert summary["drawdown_period"]["magnitude"] == pytest.approx(1 - 0.9 / 1.1)
    assert summary["profit_attribution"] is None
    assert summary["scope"]["excluded"][0]["symbol"] == "000333"


def test_excluded_scope_requires_new_frozen_identity_and_does_not_fix_source(tmp_path):
    readiness = ResearchReadiness(database(tmp_path))
    params = DailySignalStrategyRequest.model_validate(spec().to_dict()).model_dump(mode="json")
    params["scope_adjustment"] = {
        "pool_id": None, "pool_revision": None,
        "source_symbols": ["600519", "000333"],
        "excluded": [{"symbol": "000333", "reason": "原范围仍有未核验缺口"}],
    }
    frozen = readiness.freeze("daily_signal", params)
    assert frozen["resolved_symbols"] == ["600519"]
    assert frozen["parameters"]["scope_adjustment"]["excluded"][0]["symbol"] == "000333"
    params["symbols"] = ["000333"]
    with pytest.raises(ValueError, match="调整后的冻结范围"):
        readiness.freeze("daily_signal", params)
    params["symbols"] = ["600519"]
    params["candidate_count"] = 2
    with pytest.raises(ValueError, match="冻结股票范围只有 1 只"):
        readiness.freeze("daily_signal", params)
