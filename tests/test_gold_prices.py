from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.api.gold_routes import chart_data
from ai_quant_assistant.config import Settings
from ai_quant_assistant.providers import gold_history


def test_gold_page_refresh_and_failed_retry(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path, cache_dir=tmp_path / "cache", artifact_dir=tmp_path / "artifacts",
        duckdb_path=tmp_path / "catalog.duckdb",
    )
    response = Mock()
    response.json.return_value = {"time": [
        ["2024-04-19", 1230, "1234.50", 1220, 1240],
        ["2024-04-18", 1200, "-", 1190, 1210],
        ["2024-04-17", 1200, 1201, 1190, 1210],
        ["2023-11-15", 900, 901, 880, 905],
    ]}
    post = Mock(return_value=response)
    monkeypatch.setattr(gold_history.requests, "post", post)
    client = TestClient(create_app(settings))
    assert "本地暂无黄金行情" in client.get("/market/gold").text
    url = "/api/v1/gold/refresh"
    assert client.post(url, json={"confirm": False}).status_code == 422
    post.assert_not_called()
    assert client.post(url, json={"confirm": True}).status_code == 200
    assert post.call_args.kwargs["data"] == {"instid": "Au99.99"}
    page = client.get("/market/gold?days=30").text
    assert "1,234.50" in page and "缺失" in page and "2023-11-15" not in page
    assert "2023-11-15" in client.get("/market/gold?days=all").text
    assert post.call_count == 1
    before = gold_history.GoldHistory(settings.cache_dir).read()
    response.json.return_value = {"time": []}
    assert client.post(url, json={"confirm": True}).status_code == 502
    assert gold_history.GoldHistory(settings.cache_dir).read() == before


def test_gold_chart_breaks_on_missing_close():
    chart = chart_data([("2024-04-17", 1201), ("2024-04-18", None), ("2024-04-19", 1234.5)])
    assert (chart["high"], chart["low"]) == (1234.5, 1201)
    assert chart["path"].count("M") == 2
    assert chart_data([("2024-04-17", None)]) is None
    assert "500.00,100.00" in chart_data([("2024-04-17", 1201)])["path"]


def test_gold_rejects_conflicting_dates(monkeypatch):
    response = Mock()
    response.json.return_value = {"time": [
        ["2024-04-19", 100, 101, 99, 102],
        ["2024-04-19", 100, 102, 99, 102],
    ]}
    monkeypatch.setattr(gold_history.requests, "post", Mock(return_value=response))
    with pytest.raises(gold_history.GoldSourceError, match="冲突"):
        gold_history.fetch_closes()
