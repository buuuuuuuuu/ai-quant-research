import json
import re

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.config import Settings
from ai_quant_assistant.reports.chart_assets import NAV_KEYS, chart_rows


def test_nav_keeps_dates_and_independent_missing_values():
    nav = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=3),
            "portfolio_nav": [0, None, 2],
            "benchmark_nav": [1, 1, float("inf")],
        }
    )
    assert chart_rows(nav, NAV_KEYS) == [
        {"date": "2025-01-01", "portfolio_nav": 0, "benchmark_nav": 1},
        {"date": "2025-01-02", "portfolio_nav": None, "benchmark_nav": 1},
        {"date": "2025-01-03", "portfolio_nav": 2, "benchmark_nav": None},
    ]


@pytest.mark.parametrize("daily", [True, False])
def test_existing_report_open_and_download_use_saved_evidence(tmp_path, daily):
    settings = Settings(
        data_dir=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        cache_dir=tmp_path,
        duckdb_path=tmp_path / "catalog.duckdb",
    )
    client = TestClient(create_app(settings))
    group = "daily-signals" if daily else "backtests"
    prefix = "daily-signal" if daily else "factor-backtest"
    run_id = prefix + "-20240101T000000Z-12345678"
    directory = settings.artifact_dir / group / run_id
    directory.mkdir()
    nav_label = "策略与基准累计净值，横轴日期，纵轴累计净值" if daily else "策略与基准净值曲线"
    original = f'<html><body>历史报告<svg aria-label="{nav_label}"></svg>'
    if daily:
        original += '<svg aria-label="股票价格、移动平均线和交易事件"></svg>'
    original += "</body></html>"
    (directory / "report.html").write_text(original)
    url = f"/reports/{group}/{run_id}"
    assert client.get(url).text == original
    (directory / "daily_nav.csv").write_text("date,portfolio_nav,benchmark_nav\n2024-01-02,1,\n")
    if daily:
        (directory / "indicator_series.csv").write_text(
            "date,symbol,price_adjustment,signal_close,ma_5\n2024-01-02,000001,qfq,12,\n"
        )
        (directory / "orders.csv").write_text(
            "symbol,planned_execution_date,execution_date,status\n000001,2024-01-03,2024-01-04,executed\n"
        )
    page = client.get(url)
    downloads = "daily-signal-runs" if daily else "backtest-runs"
    download = client.get(f"/api/v1/{downloads}/{run_id}/downloads/report.html")
    assert page.status_code == download.status_code == 200
    assert page.content == download.content
    assert "attachment" in download.headers["content-disposition"]
    assert (directory / "report.html").read_text() == original
    assert f'aria-label="{nav_label}"' not in page.text
    assert "<script src=" not in page.text
    data = json.loads(
        re.search(r'id="report-chart-data" type="application/json">(.*?)</script>', page.text)[1]
    )
    assert data["nav"][0]["benchmark_nav"] is None
    if daily:
        assert data["price"]["symbol"] == "000001"
        assert data["price"]["rows"][0]["ma_5"] is None
        assert [row["date"] for row in data["price"]["markers"]] == ["2024-01-03", "2024-01-04"]
