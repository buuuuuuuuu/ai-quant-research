from __future__ import annotations

import csv
import json

import pytest
from test_factor_backtest import _database
from test_web_workbench import _settings

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.api.run_repository import DailySignalRunRepository


def test_executed_orders_filters_entire_csv_and_pages_without_changing_source(tmp_path):
    run_id = "daily-signal-20240101T000000Z-1234abcd"
    directory = tmp_path / "daily-signals" / run_id
    directory.mkdir(parents=True)
    path = directory / "orders.csv"
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=[
            "symbol", "side", "status", "signal_date", "execution_date", "shares", "notional", "cost"
        ])
        writer.writeheader()
        for index in range(2200):
            writer.writerow({
                "symbol": "000001" if index % 2 else "600519",
                "side": "buy" if index % 3 else "sell",
                "status": "blocked" if index % 17 == 0 else "executed",
                "signal_date": "2024-01-01", "execution_date": f"2024-01-{index % 20 + 2:02d}",
                "shares": index + 100, "notional": float(index + 1), "cost": 0.0001,
            })
    original = path.read_bytes()
    repo = DailySignalRunRepository(tmp_path)
    first = repo.executed_orders(run_id, symbols=("000001",), side="buy", sort="notional", direction="asc", page_size=20)
    second = repo.executed_orders(run_id, symbols=("000001",), side="buy", sort="notional", direction="asc", page_size=20, page=2)
    assert first["total"] > 100 and len(first["rows"]) == 20
    assert all(row["symbol"] == "000001" and row["side"] == "buy" and row["status"] == "executed" for row in first["rows"])
    assert first["rows"][-1]["notional"] < second["rows"][0]["notional"]
    assert repo.executed_orders(run_id, date_from="2024-02-01")["total"] == 0
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="排序"):
        repo.executed_orders(run_id, sort="notional; DROP TABLE users")
    with pytest.raises(ValueError, match="金额"):
        repo.executed_orders(run_id, min_notional=float("nan"))


def test_old_run_current_catalog_name_is_labeled_not_historical(tmp_path):
    _database(tmp_path)
    settings = _settings(tmp_path)
    run_id = "daily-signal-20240101T000000Z-1234abcd"
    directory = settings.artifact_dir / "daily-signals" / run_id
    directory.mkdir(parents=True)
    (directory / "run.json").write_text(json.dumps({
        "run_id": run_id, "spec": {"symbols": ["600519", "999999"]},
    }), encoding="utf-8")
    app = create_app(settings)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/v1/daily-signal-runs/{run_id}")
    payload = endpoint(run_id)
    assert payload["display_metadata"] == {
        "symbol_names": {"600519": "测试公司600519"},
        "name_source": "current_catalog_not_historical",
    }
    saved = {"symbol_names": {"600519": "旧名称"}, "name_source": "security_master snapshot at run"}
    (directory / "run.json").write_text(json.dumps({
        "run_id": run_id, "spec": {"symbols": ["600519"]}, "display_metadata": saved,
    }), encoding="utf-8")
    assert endpoint(run_id)["display_metadata"] == saved
