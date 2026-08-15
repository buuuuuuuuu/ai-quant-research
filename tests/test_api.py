from fastapi.testclient import TestClient

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.config import Settings


def test_api_runs_offline_golden_path(tmp_path) -> None:
    settings = Settings(
        artifact_dir=tmp_path / "artifacts",
        cache_dir=tmp_path / "cache",
        duckdb_path=tmp_path / "catalog.duckdb",
    )
    client = TestClient(create_app(settings))

    response = client.post(
        "/api/v1/research/interim-report-event-study",
        json={
            "source": "fixture",
            "symbol": "600519",
            "start_date": "2012-01-01",
            "end_date": "2025-12-31",
            "windows": [5, 10, 20],
            "round_trip_cost_bps": 20,
            "min_events": 10,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "succeeded"
    assert [row["window"] for row in payload["summary"]] == [5, 10, 20]
    assert payload["diagnostics"]["usable_event_years"] == 14


def test_health_endpoint(tmp_path) -> None:
    settings = Settings(
        artifact_dir=tmp_path / "artifacts",
        cache_dir=tmp_path / "cache",
        duckdb_path=tmp_path / "catalog.duckdb",
    )
    client = TestClient(create_app(settings))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

