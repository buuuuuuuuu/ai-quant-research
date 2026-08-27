from __future__ import annotations

from fastapi.testclient import TestClient
from test_factor_backtest import _database
from test_web_workbench import _settings

from ai_quant_assistant.api.app import create_app


def _explorer_database(tmp_path):
    database = _database(tmp_path)
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO announcement (
                announcement_id, security_id, company_name, title, report_type,
                report_year, report_period_end, announcement_date, published_at,
                url, source, source_batch_id, content_sha256
            ) VALUES (
                'announcement-600519-2023', 'SSE.600519', '测试公司600519',
                '2023 年年度报告', 'annual', 2023, '2023-12-31', '2024-03-20',
                '2024-03-20 18:00:00', 'https://example.test/annual.pdf',
                'fixture-announcement', 'fixture-announcement-batch', 'sha-announcement'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO research_sync_partition (
                batch_id, dataset, partition_key, object_id, variant,
                start_date, end_date, status, row_count, raw_path
            ) VALUES (
                'fixture-announcement-batch', 'announcement', 'announcement:600519:annual',
                'SSE.600519', 'annual', '2024-01-01', '2024-12-31',
                'published', 1, 'data/raw/announcement/600519/annual.parquet'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO financial_sync_partition (
                batch_id, partition_key, security_id, start_date, end_date,
                status, row_count, metric_count, raw_path
            ) VALUES (
                'fixture-batch', 'financial:600519', 'SSE.600519',
                '2023-01-01', '2024-12-31', 'published', 2, 4,
                'data/raw/financial_metric/600519/all.parquet'
            )
            """
        )
    return database


def test_data_explorer_lists_datasets_entities_and_security_detail(tmp_path) -> None:
    _explorer_database(tmp_path)
    with TestClient(create_app(_settings(tmp_path))) as client:
        datasets = client.get("/api/v1/data/datasets")
        entities = client.get(
            "/api/v1/data/securities",
            params={"dataset": "announcements", "query": "600519", "page_size": 1},
        )
        detail = client.get("/api/v1/data/securities/600519")

    assert datasets.status_code == 200
    by_name = {item["dataset"]: item for item in datasets.json()["datasets"]}
    assert by_name["prices"]["rows"] > 0
    assert by_name["benchmarks"]["rows"] > 0
    assert by_name["announcements"]["rows"] == 1
    assert by_name["financials"]["rows"] == 10
    assert entities.status_code == 200
    assert entities.json()["pagination"] == {"page": 1, "page_size": 1, "total": 1, "pages": 1}
    assert entities.json()["items"][0]["symbol"] == "600519"
    payload = detail.json()
    assert payload["announcement_count"] == 1
    assert payload["report_count"] == 2
    assert payload["metric_count"] == 4
    assert {item["adjustment"] for item in payload["price_coverage"]} == {"none", "qfq"}


def test_price_explorer_is_paginated_filterable_and_traces_lineage(tmp_path) -> None:
    _explorer_database(tmp_path)
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.get(
            "/api/v1/data/securities/600519/prices",
            params={
                "start_date": "2024-01-02",
                "end_date": "2024-01-10",
                "adjustment": "qfq",
                "page_size": 2,
            },
        )
        invalid = client.get("/api/v1/data/securities/600519/prices", params={"adjustment": "bad"})
        record_id = response.json()["items"][0]["record_id"]
        lineage = client.get(f"/api/v1/data/lineage/prices/{record_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["pagination"]["total"] == 7
    assert len(payload["items"]) == 2
    assert payload["items"][0]["trade_date"] > payload["items"][1]["trade_date"]
    assert payload["summary"]["adjustment"] == "qfq"
    assert invalid.status_code == 422
    assert lineage.status_code == 200
    assert lineage.json()["lineage"]["serving_source"] == "local:published_daily_bar"
    assert lineage.json()["lineage"]["source_batch_id"] == "fixture-batch"


def test_announcement_and_financial_explorer_exposes_point_in_time_and_csv(tmp_path) -> None:
    _explorer_database(tmp_path)
    with TestClient(create_app(_settings(tmp_path))) as client:
        announcements = client.get(
            "/api/v1/data/securities/600519/announcements",
            params={"report_type": "annual"},
        )
        announcement_id = announcements.json()["items"][0]["record_id"]
        announcement_lineage = client.get(f"/api/v1/data/lineage/announcements/{announcement_id}")
        metrics = client.get(
            "/api/v1/data/securities/600519/financial-metrics",
            params={"metric_code": "roe"},
        )
        csv_response = client.get(
            "/api/v1/data/securities/600519/financial-metrics",
            params={"metric_code": "roe", "output": "csv"},
        )

    assert announcements.status_code == 200
    announcement = announcements.json()["items"][0]
    assert announcement["report_period_end"] == "2023-12-31"
    assert announcement["published_at"].startswith("2024-03-20")
    assert "published_at" in announcements.json()["point_in_time_notice"]
    assert announcement_lineage.json()["lineage"]["raw_path"].endswith("annual.parquet")
    assert metrics.status_code == 200
    assert {item["metric_code"] for item in metrics.json()["items"]} == {"roe"}
    assert "published_at" in metrics.json()["point_in_time_notice"]
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert csv_response.content.startswith(b"\xef\xbb\xbf")
    assert "metric_code" in csv_response.text


def test_benchmark_explorer_and_workbench_drilldown_controls(tmp_path) -> None:
    _explorer_database(tmp_path)
    with TestClient(create_app(_settings(tmp_path))) as client:
        benchmarks = client.get("/api/v1/data/benchmarks")
        prices = client.get(
            "/api/v1/data/benchmarks/csi000300/prices",
            params={"start_date": "2024-01-02", "end_date": "2024-01-05", "page_size": 2},
        )
        home = client.get("/")

    assert benchmarks.status_code == 200
    assert benchmarks.json()["items"][0]["symbol"] == "csi000300"
    assert prices.status_code == 200
    assert prices.json()["pagination"]["total"] == 4
    assert 'data-explorer-dataset="prices"' in home.text
    assert "指数基准" in home.text
    assert "记录质量与数据血缘" in home.text
    assert "下载本页 CSV" in home.text
