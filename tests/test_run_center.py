from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from test_data_explorer import _explorer_database
from test_web_workbench import _payload, _settings

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.api.run_center import DeletionConflict, RunCenter
from ai_quant_assistant.api.run_repository import BacktestRunRepository
from ai_quant_assistant.api.schemas import FactorBacktestRequest
from ai_quant_assistant.storage.research_run_index import register_run
from ai_quant_assistant.storage.stock_pools import StockPoolService
from ai_quant_assistant.storage.task_repository import OperationTaskRepository


@pytest.fixture
def center(tmp_path):
    return RunCenter(_explorer_database(tmp_path))


def index(center, run_id="indexed", spec=None, kind="factor_backtest"):
    register_run(
        center.database,
        kind,
        {
            "run_id": run_id,
            "spec": spec or FactorBacktestRequest(**_payload()).to_spec().to_dict(),
            "status": "succeeded",
            "metrics": {"total_return": 0.125},
            "completed_at": "2024-04-01T00:00:00+00:00",
        },
    )


def test_history_pagination_status_and_task_index_dedup(center, monkeypatch):
    monkeypatch.setattr(
        BacktestRunRepository, "list", lambda *_a, **_k: pytest.fail("directory scan")
    )
    tasks = OperationTaskRepository(center.database)
    first = tasks.create("backtest", spec=_payload())
    tasks.update(first["task_id"], status="succeeded", run_id="indexed")
    second = tasks.create("backtest", spec={**_payload(), "research_type": "factor_lab"})
    tasks.update(second["task_id"], status="failed", error="fixture failure")
    index(center)
    result = center.list(page_size=1)
    assert result["total"] == 2
    assert len(result["items"]) == 1
    assert (
        result["items"][0]["record_id"] != center.list(page=2, page_size=1)["items"][0]["record_id"]
    )
    assert center.list(kind="factor_lab", status="failed")["total"] == 1
    assert center.list(kind="factor_backtest", status="succeeded")["total"] == 1
    assert center.get("task", first["task_id"])["run_id"] == "indexed"
    for kwargs in ({"kind": "' OR 1=1"}, {"status": "draft"}, {"page_size": 101}, {"page": 0}):
        with pytest.raises(ValueError):
            center.list(**kwargs)


def test_delete_terminal_backtest_removes_aliases_artifacts_and_keeps_audit(center, tmp_path):
    run_id = "factor-backtest-20240101T000000Z-1234abcd"
    center.artifact_dir = tmp_path / "artifacts"
    directory = center.artifact_dir / "backtests" / run_id
    directory.mkdir(parents=True)
    (directory / "report.html").write_text("saved report")
    tasks = OperationTaskRepository(center.database)
    task = tasks.create("backtest", spec=_payload())
    tasks.update(task["task_id"], status="succeeded", run_id=run_id)
    index(center, run_id=run_id)
    with pytest.raises(DeletionConflict, match="身份"):
        center.delete_backtest("index", run_id, expected_run_id="wrong", actor="test")
    assert directory.is_dir()
    result = center.delete_backtest("index", run_id, expected_run_id=run_id, actor="test")
    assert result["deleted"] and not directory.exists()
    with pytest.raises(KeyError):
        BacktestRunRepository(center.artifact_dir).run_dir(run_id)
    assert center.list()["total"] == 0
    for source, record in (("index", run_id), ("task", task["task_id"])):
        with pytest.raises(KeyError):
            center.get(source, record)
    with center.database.connect(read_only=True) as c:
        audit = c.execute(
            "SELECT actor,action,object_id FROM operation_audit WHERE event_id=?",
            [result["audit_id"]],
        ).fetchone()
    assert audit == ("test", "backtest_deleted", run_id)


def test_delete_refuses_active_and_dependent_tasks(center, tmp_path):
    center.artifact_dir = tmp_path / "artifacts"
    tasks = OperationTaskRepository(center.database)
    queued = tasks.create("backtest", spec=_payload())
    with pytest.raises(DeletionConflict, match="排队"):
        center.delete_backtest(
            "task", queued["task_id"], expected_run_id=queued["task_id"], actor="test"
        )
    assert center.get("task", queued["task_id"])["status"] == "queued"
    run_id = "factor-backtest-20240101T000000Z-8765abcd"
    tasks.update(queued["task_id"], status="succeeded", run_id=run_id)
    index(center, run_id=run_id)
    with center.database.connect() as c:
        c.execute("UPDATE operation_task SET status='running' WHERE task_id=?", [queued["task_id"]])
    with pytest.raises(DeletionConflict, match="关联任务"):
        center.delete_backtest("index", run_id, expected_run_id=run_id, actor="test")
    assert center.get("index", run_id)["run_id"] == run_id
    with center.database.connect() as c:
        c.execute(
            "UPDATE operation_task SET status='succeeded' WHERE task_id=?", [queued["task_id"]]
        )
        c.execute(
            """INSERT INTO research_plan (
            plan_id,conversation_id,status,question,title,rationale,planner,task_id,run_id
            ) VALUES ('plan-test','conversation-test','succeeded','q','title','reason','test',?,?)""",
            [queued["task_id"], run_id],
        )
    with pytest.raises(DeletionConflict, match="研究计划"):
        center.delete_backtest("index", run_id, expected_run_id=run_id, actor="test")
    assert center.get("index", run_id)["run_id"] == run_id


def test_delete_failed_unindexed_task_and_reject_other_types(center, tmp_path):
    center.artifact_dir = tmp_path / "artifacts"
    tasks = OperationTaskRepository(center.database)
    task = tasks.create("backtest", spec=_payload())
    tasks.update(task["task_id"], status="failed", error="fixture")
    center.delete_backtest("task", task["task_id"], expected_run_id=task["task_id"], actor="test")
    assert center.list()["total"] == 0
    with pytest.raises(KeyError):
        center.delete_backtest("screen", "x", expected_run_id="x", actor="test")


def test_delete_daily_signal_run_only_uses_its_artifact_root(center, tmp_path):
    center.artifact_dir = tmp_path / "artifacts"
    run_id = "daily-signal-20240101T000000Z-1234abcd"
    directory = center.artifact_dir / "daily-signals" / run_id
    directory.mkdir(parents=True)
    (directory / "orders.csv").write_text("symbol,shares\n")
    other = center.artifact_dir / "backtests" / run_id
    other.mkdir(parents=True)
    index(center, run_id=run_id, kind="daily_signal")
    center.delete_backtest("index", run_id, expected_run_id=run_id, actor="test")
    assert not directory.exists()
    assert other.is_dir()
    assert center.list()["total"] == 0


def test_copy_exact_costs_and_original_scope_read_only(center):
    original = FactorBacktestRequest(**_payload()).to_spec().to_dict()
    original.update(commission_bps=12.3, slippage_bps=17, stamp_duty_bps=0, lot_size=10)
    index(center, spec=original)
    copied = center.copy_parameters("index", "indexed")
    assert copied["params"]["commission_bps"] == 12.3
    assert copied["params"]["stamp_duty_bps"] == 0
    assert copied["params"]["symbols"] == original["symbols"]
    assert copied["missing_fields"] == []
    assert "run_id" not in copied["params"]
    with center.database.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM operation_task").fetchone()[0] == 0

    # API/Copilot manual inputs carry an explicit context with no pool identity.
    original["stock_pool"] = {
        "pool_id": None,
        "pool_revision": None,
        "resolved_symbols": original["symbols"],
        "source_type": "explicit",
    }
    index(center, "manual-context", spec=original)
    manual = center.copy_parameters("index", "manual-context")
    assert manual["params"]["symbols"] == original["symbols"]
    assert manual["params"]["stock_pool_id"] is None


def test_copy_pool_revision_not_current_members(center):
    pools = StockPoolService(center.database)
    pool = pools.create(name="original", symbols=["600519", "000001"], actor="test")
    frozen = pools.resolve(pool["pool_id"], revision=1)
    spec = (
        FactorBacktestRequest(**{**_payload(), "symbols": ["600519", "000001"]})
        .to_spec(stock_pool=frozen)
        .to_dict()
    )
    index(center, spec=spec)
    copied = center.copy_parameters("index", "indexed")
    assert copied["params"]["stock_pool_revision"] == 1
    assert copied["params"]["symbols"] is None
    assert copied["scope"] == ["600519", "000001"]
    with center.database.connect() as c:
        c.execute(
            "UPDATE stock_pool_revision SET symbols_json='[\"300750\"]' WHERE pool_id=?",
            [pool["pool_id"]],
        )
    with pytest.raises(ValueError, match="不一致"):
        center.copy_parameters("index", "indexed")


def test_missing_dynamic_and_oversized_records_are_not_invented(center):
    tasks = OperationTaskRepository(center.database)
    empty = tasks.create("backtest", spec={})
    row = center.get("task", empty["task_id"])
    assert row["symbols"] == [] and row["duration_seconds"] is None
    with pytest.raises(ValueError, match="缺失"):
        center.copy_parameters("task", empty["task_id"])
    dynamic = tasks.create("backtest", spec={**_payload(), "universe_mode": "all_available"})
    with pytest.raises(ValueError, match="动态范围"):
        center.copy_parameters("task", dynamic["task_id"])
    with center.database.connect() as c:
        c.execute(
            "UPDATE operation_task SET spec_json=? WHERE task_id=?",
            [json.dumps({"x": "x" * 100001}), empty["task_id"]],
        )
    assert center.get("task", empty["task_id"])["payload_unavailable"]
    assert center.list()["total"] == 2
    with pytest.raises(KeyError):
        center.get("index", "../../private")


def test_pages_api_auth_and_original_urls(tmp_path):
    _explorer_database(tmp_path)
    with TestClient(create_app(_settings(tmp_path))) as client:
        for path in (
            "/",
            "/data",
            "/data/pools",
            "/research",
            "/research/copilot",
            "/system",
            "/backtests",
            "/backtests/history",
            "/backtests/new",
            "/backtests/factors",
            "/backtests/daily-signals",
            "/backtests/screen",
            "/backtests/detail",
            "/backtests/configure",
            "/research-dashboard",
            "/stocks/600519",
        ):
            response = client.get(path)
            assert response.status_code == 200, path
            assert 'id="app-navigation"' in response.text
        assert client.get("/?stock=600519&factor=roe#backtest").status_code == 200
        assert client.get("/api/v1/run-center?page_size=101").status_code == 422
        assert client.get("/api/v1/run-center?kind=bad").status_code == 422
        assert client.get("/api/v1/run-center/record?source=task&record=unknown").status_code == 404

    settings = replace(
        _settings(tmp_path),
        web_password="fixture-password",
        web_session_secret="fixture-independent-secret",
    )
    with TestClient(create_app(settings), follow_redirects=False) as client:
        for path in (
            "/api/v1/run-center",
            "/api/v1/run-center/configuration?kind=factor_lab",
            "/api/v1/run-center/configuration?kind=daily_signal",
            "/api/v1/daily-signal-strategies",
            "/api/v1/run-center/copy?source=index&record=x",
            "/api/v1/run-center/record?source=task&record=x",
        ):
            assert client.get(path).status_code == 401
        assert (
            client.request(
                "DELETE",
                "/api/v1/run-center/record?source=task&record=x",
                json={"expected_run_id": "x"},
            ).status_code
            == 401
        )
        redirect = client.get("/backtests/detail?source=index&record=original")
        assert redirect.status_code == 303
        assert "source%3Dindex%26record%3Doriginal" in redirect.headers["location"]
        client.post("/login", data={"username": "admin", "password": "fixture-password"})
        assert client.get("/api/v1/run-center").status_code == 200


def test_copy_special_strategy_or_unknown_parameters_is_blocked(center):
    spec = FactorBacktestRequest(**_payload()).to_spec().to_dict()
    index(center, spec={**spec, "factors": [{"code": "roe", "weight": 1, "direction": "desc"}]})
    with pytest.raises(ValueError, match="组合因子"):
        center.copy_parameters("index", "indexed")
    index(center, "future-spec", spec={**spec, "unknown_future_option": 1})
    with pytest.raises(ValueError, match="无法映射"):
        center.copy_parameters("index", "future-spec")


def test_event_copy_restores_recorded_as_of_and_warns_missing_provider(center):
    spec = {
        "symbol": "600519",
        "start_date": "2024-01-01",
        "end_date": "2024-03-01",
        "benchmark_symbol": "csi000300",
        "windows": [5, 10],
        "adjustment": "qfq",
        "round_trip_cost_bps": 18,
        "min_events": 1,
        "entry_rule": "next_common_trading_day_close",
    }
    with center.database.connect() as c:
        c.execute(
            """INSERT INTO document_event_research_run VALUES (
            'event-fixture','event-original','announcement-original','document-original',
            '2024-03-01T10:00:00Z',?,?, '{}', '2024-03-01T10:00:00Z')""",
            [
                json.dumps(spec),
                json.dumps({"status": "succeeded", "as_of": "2024-03-01T10:00:00+00:00"}),
            ],
        )
    result = center.copy_parameters("event", "event-fixture")
    assert result["params"]["as_of"].startswith("2024-03-01T10:00:00")
    assert result["params"]["windows"] == [5, 10]
    assert result["missing_fields"] == ["source"]
    assert result["endpoint"] == "/api/v1/document-events/event-original/event-study"


def test_read_only_history_returns_screen_counts_and_unknown_dates(center):
    with center.database.connect() as c:
        c.execute(
            """INSERT INTO screen_run(run_id,name,status,as_of,universe_mode,spec_json,started_at)
            VALUES ('screen-fixture','sample','running','2024-03-01','explicit',?,CURRENT_TIMESTAMP)""",
            [json.dumps({"as_of": "2024-03-01", "symbols": ["600519"]})],
        )
    row = center.get("screen", "screen-fixture")
    assert row["status"] == "running"
    assert row["duration_seconds"] is None
    assert row["start_date"] == row["end_date"] == "2024-03-01"
    assert row["result"]["selected_count"] == 0
