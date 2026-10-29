from fastapi.testclient import TestClient

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.config import Settings
from ai_quant_assistant.storage.index_fund_snapshots import IndexFundSnapshotStore


def test_index_page_reads_only_saved_snapshot(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", artifact_dir=tmp_path / "artifacts", cache_dir=tmp_path / "cache", duckdb_path=tmp_path / "data" / "catalog.duckdb")
    app = create_app(settings)
    with TestClient(app) as client:
        assert "指数基金规模观察" in client.get("/market/index-funds").text
        assert client.get("/api/v1/index-funds/000300").json()["snapshot"] is None
        assert client.get("/api/v1/index-funds/invalid").status_code == 404
        store = IndexFundSnapshotStore(app.state.database)
        snapshot_id = store.save("000300", {"group": ["SSE.510300"]}, {}, {"points": [{"trade_date": "2026-09-01", "ratio_fraction": "0.03"}]})
        result = client.get("/api/v1/index-funds/000300").json()
        assert result["snapshot"]["snapshot_id"] == snapshot_id
        assert result["snapshot"]["result"]["points"][0]["ratio_fraction"] == "0.03"
