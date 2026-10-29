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


def test_csv_export_uses_exact_snapshot_and_preserves_missing(tmp_path):
    import csv
    import io

    settings = Settings(data_dir=tmp_path / 'data', artifact_dir=tmp_path / 'artifacts', cache_dir=tmp_path / 'cache', duckdb_path=tmp_path / 'data' / 'catalog.duckdb')
    app = create_app(settings)
    store = IndexFundSnapshotStore(app.state.database)
    first = store.save('000300', {'funds': ['SSE.510300']}, {'sources': ['sse:monthly_report']}, {'points': [{'trade_date': '2026-09-01', 'numerator_cny': '200', 'denominator_cny': '10000', 'ratio_fraction': '0.02', 'availability': 'ready', 'missing': []}, {'trade_date': '2026-09-02', 'numerator_cny': None, 'denominator_cny': '10000', 'ratio_fraction': None, 'availability': 'partial', 'missing': ['SSE.510300:observation']}]})
    store.save('000300', {'funds': []}, {}, {'points': []})
    with TestClient(app) as client:
        response = client.get('/api/v1/index-fund-snapshots/' + first + '/csv')
        assert response.status_code == 200
        rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
        assert rows[0]['snapshot_id'] == first and rows[0]['ratio_percent'] == '2.00'
        assert rows[1]['ratio_fraction'] == '' and rows[1]['missing'] == 'SSE.510300:observation'
        assert client.get('/api/v1/index-fund-snapshots/invalid/csv').status_code == 404


def test_html_export_is_snapshot_bound_escaped_and_offline(tmp_path):
    settings = Settings(data_dir=tmp_path / 'data', artifact_dir=tmp_path / 'artifacts', cache_dir=tmp_path / 'cache', duckdb_path=tmp_path / 'data' / 'catalog.duckdb')
    app = create_app(settings)
    store = IndexFundSnapshotStore(app.state.database)
    first = store.save('000300', {'funds': ['<script>alert(1)</script>'], 'start': '2026-09-01', 'end': '2026-09-02'}, {'sources': ['test:<source>'], 'shares': [{'date': '2026-09-01', 'fund_id': '<script>alert(1)</script>', 'shares': '100'}], 'nav': [{'date': '2026-09-01', 'fund_id': '<script>alert(1)</script>', 'unit_nav': '2'}]}, {'warning': '<img onerror=bad>', 'points': [{'trade_date': '2026-09-01', 'numerator_cny': '200', 'denominator_cny': '10000', 'ratio_fraction': '0.02', 'availability': 'ready', 'missing': []}, {'trade_date': '2026-09-02', 'numerator_cny': None, 'denominator_cny': None, 'ratio_fraction': None, 'availability': 'partial', 'missing': ['missing:<bad>']}]})
    store.save('000300', {'funds': []}, {}, {'points': []})
    with TestClient(app) as client:
        response = client.get(f'/api/v1/index-fund-snapshots/{first}/html')
        assert response.status_code == 200 and 'attachment' in response.headers['content-disposition']
        assert "default-src 'none'" in response.headers['content-security-policy']
        assert first in response.text and '2.00%' in response.text and '2026-09-02' in response.text
        assert '基金每日明细' in response.text and '<td>100</td><td>2</td><td>200</td>' in response.text
        assert '&lt;script&gt;' in response.text and '&lt;img onerror=bad&gt;' in response.text
        assert '<script>' not in response.text and '<img ' not in response.text
        assert 'missing:&lt;bad&gt;' in response.text and 'http://' not in response.text and 'https://' not in response.text
        assert client.get('/api/v1/index-fund-snapshots/invalid/html').status_code == 404
