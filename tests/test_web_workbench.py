from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient
from test_factor_backtest import _database

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.api.run_repository import BacktestRunRepository
from ai_quant_assistant.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        artifact_dir=tmp_path / "artifacts",
        cache_dir=tmp_path / "cache",
        duckdb_path=tmp_path / "data" / "catalog.duckdb",
    )


def _payload() -> dict:
    return {
        "symbols": ["600519", "000001", "300750"],
        "start_date": "2024-01-02",
        "end_date": "2024-03-08",
        "factor": "roe",
        "factor_direction": "desc",
        "top_n": 1,
        "rebalance": "monthly",
        "benchmark_symbol": "csi000300",
        "adjustment": "qfq",
        "initial_capital": 100000,
        "commission_bps": 5,
        "slippage_bps": 5,
        "stamp_duty_bps": 5,
        "lot_size": 100,
    }


def _wait_for_task(client: TestClient, task_id: str) -> dict:
    for _ in range(200):
        task = client.get(f"/api/v1/backtests/{task_id}").json()
        if task["status"] in {"succeeded", "failed"}:
            return task
        time.sleep(0.025)
    raise AssertionError("backtest task did not finish")


def test_workbench_home_and_static_assets_are_served(tmp_path) -> None:
    _database(tmp_path)
    with TestClient(create_app(_settings(tmp_path))) as client:
        home = client.get("/")
        stylesheet = client.get("/assets/workbench.css")
        javascript = client.get("/assets/workbench.js")
        metadata = client.get("/api")

    assert home.status_code == 200
    assert "澄明 · A 股研究工作台" in home.text
    assert "配置财务因子回测" in home.text
    assert home.headers["content-type"].startswith("text/html")
    assert stylesheet.status_code == 200
    assert "--forest" in stylesheet.text
    assert javascript.status_code == 200
    assert "pollTask" in javascript.text
    assert metadata.json()["workbench"] == "/"


def test_database_overview_exposes_counts_coverage_and_research_universe(tmp_path) -> None:
    _database(tmp_path)
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.get("/api/v1/data/overview")

    assert response.status_code == 200
    overview = response.json()
    assert overview["database"]["schema_version"] == 4
    assert overview["database"]["relations"]["security_master"] == 3
    assert {row["symbol"] for row in overview["securities"]} == {"600519", "000001", "300750"}
    assert overview["coverage"]["stock"]["start_date"] == "2024-01-02"
    assert overview["coverage"]["benchmark"]["rows"] > 0
    assert overview["coverage"]["financial"]["rows"] == 5


def test_async_backtest_progress_result_history_and_downloads(tmp_path) -> None:
    _database(tmp_path)
    with TestClient(create_app(_settings(tmp_path))) as client:
        submitted = client.post("/api/v1/backtests", json=_payload())
        assert submitted.status_code == 202, submitted.text
        initial = submitted.json()
        assert initial["status"] in {"queued", "running", "succeeded"}
        task = _wait_for_task(client, initial["task_id"])
        assert task["status"] == "succeeded", task
        assert task["progress"] == 100
        assert task["run_id"].startswith("factor-backtest-")

        detail = client.get(f"/api/v1/backtest-runs/{task['run_id']}")
        history = client.get("/api/v1/backtest-runs")
        tasks = client.get("/api/v1/backtests")
        excel = client.get(
            f"/api/v1/backtest-runs/{task['run_id']}/downloads/backtest_result.xlsx"
        )
        report = client.get(f"/reports/backtests/{task['run_id']}")

    assert detail.status_code == 200, detail.text
    run = detail.json()
    assert run["metrics"]["trade_count"] >= 1
    assert run["series"][0]["portfolio_nav"] == 1.0
    assert run["trades"]
    assert run["positions"]
    assert run["selections"]
    assert history.json()[0]["run_id"] == task["run_id"]
    assert tasks.json()[0]["task_id"] == task["task_id"]
    assert excel.status_code == 200
    assert excel.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument"
    )
    assert "财务因子组合回测" in report.text


def test_download_api_rejects_unknown_files_and_invalid_run_ids(tmp_path) -> None:
    _database(tmp_path)
    with TestClient(create_app(_settings(tmp_path))) as client:
        unknown_file = client.get(
            "/api/v1/backtest-runs/factor-backtest-20240101T000000Z-aaaaaaaa/"
            "downloads/secrets.txt"
        )
        invalid_run = client.get("/api/v1/backtest-runs/not-a-run")
        missing_task = client.get("/api/v1/backtests/task-does-not-exist")

    assert unknown_file.status_code == 404
    assert invalid_run.status_code == 404
    assert missing_task.status_code == 404


def test_backtest_request_validation_happens_before_task_creation(tmp_path) -> None:
    _database(tmp_path)
    payload = _payload()
    payload["symbols"] = ["600519"]
    payload["top_n"] = 2
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post("/api/v1/backtests", json=payload)
        tasks = client.get("/api/v1/backtests")

    assert response.status_code == 422
    assert tasks.json() == []


def test_failed_background_task_preserves_error_for_web_client(tmp_path) -> None:
    database = _database(tmp_path)
    with database.connect() as connection:
        connection.execute("DELETE FROM benchmark_daily_bar")
    with TestClient(create_app(_settings(tmp_path))) as client:
        submitted = client.post("/api/v1/backtests", json=_payload()).json()
        task = _wait_for_task(client, submitted["task_id"])

    assert task["status"] == "failed"
    assert task["progress"] < 100
    assert "benchmark" in task["error"]


def test_history_reader_converts_csv_missing_values_to_json_null(tmp_path) -> None:
    run_id = "factor-backtest-20240101T000000Z-aaaaaaaa"
    directory = tmp_path / "artifacts" / "backtests" / run_id
    directory.mkdir(parents=True)
    (directory / "run.json").write_text(
        '{"run_id":"' + run_id + '","status":"succeeded","spec":{},"metrics":{}}',
        encoding="utf-8",
    )
    (directory / "trades.csv").write_text(
        "date,symbol,status,price,reason\n2024-01-03,600519,blocked,,suspended\n",
        encoding="utf-8",
    )

    detail = BacktestRunRepository(tmp_path / "artifacts").detail(run_id)

    assert detail["trades"][0]["price"] is None
