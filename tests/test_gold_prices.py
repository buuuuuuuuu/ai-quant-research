import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from ai_quant_assistant.api.app import create_app
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
        ["2024-04-12", 910, 911, 900, 915],
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
    week = client.get("/market/gold?days=7")
    assert week.status_code == 200
    assert 'href="?days=7" aria-current="page"' in week.text
    assert "日期范围：2024-04-13 至 2024-04-19" in week.text
    assert "2024-04-12" not in week.text
    assert post.call_count == 1
    before = gold_history.GoldHistory(settings.cache_dir).read()
    response.json.return_value = {"time": []}
    assert client.post(url, json={"confirm": True}).status_code == 502
    assert gold_history.GoldHistory(settings.cache_dir).read() == before


def test_gold_rejects_conflicting_dates(monkeypatch):
    response = Mock()
    response.json.return_value = {"time": [
        ["2024-04-19", 100, 101, 99, 102],
        ["2024-04-19", 100, 102, 99, 102],
    ]}
    monkeypatch.setattr(gold_history.requests, "post", Mock(return_value=response))
    with pytest.raises(gold_history.GoldSourceError, match="冲突"):
        gold_history.fetch_closes()


@pytest.mark.parametrize(("closes", "expected_start"), [
    ({"2025-12-31": 900, "2026-01-01": None, "2026-01-02": 910}, "2025-12-27"),
    ({"2026-01-02": 910}, "2025-12-27"),
    ({"2026-01-02": None}, None),
])
def test_week_chart_uses_calendar_dates_and_local_quotes(tmp_path, closes, expected_start):
    history = gold_history.GoldHistory(tmp_path)
    history.path.write_text(json.dumps({"closes": closes, "updated_at": None}))
    client = TestClient(create_app(Settings(data_dir=tmp_path, cache_dir=tmp_path,
                                           duckdb_path=tmp_path / "catalog.duckdb")))
    page = client.get("/market/gold?days=7")
    assert page.context["chart_start"] == expected_start
    assert page.context["rows"] == sorted(closes.items())
    if expected_start:
        assert 'id="gold-plot"' in page.text and "/assets/vendor/lightweight-charts-" in page.text
    else:
        assert "所选区间没有有效收盘价" in page.text and 'id="gold-plot"' not in page.text
