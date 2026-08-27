from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from test_factor_backtest import _database
from test_web_workbench import _settings

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.research.screener import (
    PointInTimeScreener,
    ScreenCondition,
    ScreenSpec,
)


def _spec(**changes) -> ScreenSpec:
    options = {
        "name": "高 ROE 低负债",
        "as_of": date(2024, 1, 2),
        "symbols": ("600519", "000001", "300750"),
        "conditions": (
            ScreenCondition(field="roe", operator="gte", value=15),
            ScreenCondition(field="debt_asset_ratio", operator="lt", value=150),
        ),
        "sort_by": "roe",
        "sort_direction": "desc",
    }
    options.update(changes)
    return ScreenSpec(**options)


@pytest.mark.parametrize(
    "changes",
    (
        {"symbols": ()},
        {"conditions": ()},
        {"conditions": (ScreenCondition("roe", "gte", 1),) * 2},
        {"universe_mode": "bad"},
        {"sort_by": "unknown"},
        {"sort_direction": "sideways"},
        {"missing_policy": "zero"},
        {"top_n": 0},
    ),
)
def test_screen_spec_rejects_unsafe_or_ambiguous_inputs(changes) -> None:
    with pytest.raises(ValueError):
        _spec(**changes)


def test_screen_uses_only_data_published_by_end_of_as_of_and_explains_rejections(
    tmp_path,
) -> None:
    database = _database(tmp_path)
    screener = PointInTimeScreener(database)

    preflight = screener.preflight(_spec())
    result = screener.run(_spec())

    assert preflight["ready"] is True
    assert preflight["candidate_count"] == 2
    assert preflight["field_coverage"] == {"roe": 2, "debt_asset_ratio": 2}
    assert result["status"] == "succeeded"
    assert result["candidate_count"] == 2
    assert result["selected_count"] == 1
    assert [item["symbol"] for item in result["items"] if item["selected"]] == ["600519"]
    maotai = result["items"][0]
    assert maotai["values"]["roe"] == 100.0
    assert maotai["evidence"]["financial"]["report_id"] == "maotai-revision"
    assert str(maotai["evidence"]["financial"]["published_at"]).startswith(
        "2024-01-02 18:00:00"
    )
    pingan = next(item for item in result["items"] if item["symbol"] == "000001")
    assert pingan["selected"] is False
    assert any(
        item["field"] == "debt_asset_ratio" and item["status"] == "failed"
        for item in pingan["evaluations"]
    )
    assert "pingan-annual" not in str(result)
    assert database.status()["relations"]["screen_run"] == 1
    assert database.status()["relations"]["screen_result"] == 2


def test_missing_values_never_become_zero_and_can_block_execution(tmp_path) -> None:
    screener = PointInTimeScreener(_database(tmp_path))
    spec = _spec(
        conditions=(ScreenCondition(field="revenue_yoy", operator="gte", value=0),),
        missing_policy="show",
    )

    preflight = screener.preflight(spec)

    assert preflight["ready"] is False
    assert preflight["field_coverage"] == {"revenue_yoy": 0}
    assert "no published values" in preflight["blockers"][0]
    with pytest.raises(ValueError, match="no published values"):
        screener.run(spec)


def test_screen_top_n_and_saved_watchlist_are_reproducible(tmp_path) -> None:
    screener = PointInTimeScreener(_database(tmp_path))
    result = screener.run(
        _spec(
            conditions=(ScreenCondition(field="roe", operator="gte", value=5),),
            top_n=1,
        )
    )
    watchlist = screener.save_watchlist(result["run_id"], "最高 ROE 候选")

    assert result["selected_count"] == 1
    assert watchlist["symbols"] == ["600519"]
    assert watchlist["screen_run_id"] == result["run_id"]
    assert watchlist["history_verified"] is False
    assert watchlist["warning"]
    assert screener.list_watchlists()[0]["watchlist_id"] == watchlist["watchlist_id"]


def test_screen_api_preflight_run_csv_and_watchlist(tmp_path) -> None:
    _database(tmp_path)
    request = {
        "name": "API 条件选股",
        "as_of": "2024-01-02",
        "symbols": ["600519", "000001", "300750"],
        "conditions": [
            {"field": "roe", "operator": "gte", "value": 15},
            {"field": "debt_asset_ratio", "operator": "lt", "value": 150},
        ],
        "sort_by": "roe",
    }
    with TestClient(create_app(_settings(tmp_path))) as client:
        fields = client.get("/api/v1/screens/fields")
        home = client.get("/")
        preflight = client.post("/api/v1/screens/preflight", json=request)
        run = client.post("/api/v1/screens/runs", json=request)
        run_id = run.json()["run_id"]
        csv_response = client.get(f"/api/v1/screens/runs/{run_id}/results.csv")
        watchlist = client.post(
            f"/api/v1/screens/runs/{run_id}/watchlists", json={"name": "API 股票池"}
        )
        invalid = client.post(
            "/api/v1/screens/preflight",
            json={
                **request,
                "conditions": [{"field": "raw_sql", "operator": "gte", "value": 1}],
            },
        )

    assert fields.status_code == 200
    assert home.status_code == 200
    assert "条件选股与候选池" in home.text
    assert 'id="screen-conditions"' in home.text
    assert {item["code"] for item in fields.json()["fields"]} >= {
        "roe",
        "close",
        "avg_amount_20d",
    }
    assert preflight.status_code == 200
    assert preflight.json()["ready"] is True
    assert run.status_code == 201
    assert run.json()["selected_count"] == 1
    assert csv_response.status_code == 200
    assert csv_response.content.startswith(b"\xef\xbb\xbf")
    assert "report_published_at" in csv_response.text
    assert watchlist.status_code == 201
    assert watchlist.json()["symbols"] == ["600519"]
    assert invalid.status_code == 422
