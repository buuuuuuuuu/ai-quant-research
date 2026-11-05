from fastapi.testclient import TestClient

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.config import Settings


def app_for(tmp_path):
    return create_app(Settings(data_dir=tmp_path / 'data', artifact_dir=tmp_path / 'artifacts', cache_dir=tmp_path / 'cache', duckdb_path=tmp_path / 'data/catalog.duckdb'))


def test_evidence_is_explicit_immutable_and_never_claims_verification(tmp_path):
    app = app_for(tmp_path)
    claim = {'fund_id': 'SSE.510300', 'kind': 'listing', 'effective_from': '2012-05-28',
             'source_title': '基金2012年报', 'source_url': 'https://www.sse.com.cn/test.pdf',
             'published_date': '2013-03-27', 'confirm': True}
    with TestClient(app) as client:
        assert client.get('/api/v1/index-fund-evidence', params={'fund_id': 'SSE.510300'}).json() == {'records': [], 'truncated': False}
        with app.state.database.connect(read_only=True) as conn:
            assert conn.execute('SELECT COUNT(*) FROM index_fund_evidence').fetchone()[0] == 0
        assert client.post('/api/v1/index-fund-evidence', json={**claim, 'confirm': False}).status_code == 422
        first = client.post('/api/v1/index-fund-evidence', json=claim)
        assert first.status_code == 201
        assert first.json()['verification'] == 'unverified'
        assert client.post('/api/v1/index-fund-evidence', json=claim).json()['evidence_id'] == first.json()['evidence_id']
        different = client.post('/api/v1/index-fund-evidence', json={**claim, 'effective_from': '2012-05-29'})
        assert different.json()['evidence_id'] != first.json()['evidence_id']
        data = client.get('/api/v1/index-fund-evidence', params={'fund_id': 'SSE.510300'}).json()
        assert len(data['records']) == 2 and not data['truncated']
        assert all(r['claim']['verification'] == 'unverified' for r in data['records'])
        assert client.get('/api/v1/index-funds/000300').json()['snapshot'] is None


def test_tracking_requires_bounded_interval_and_source(tmp_path):
    app = app_for(tmp_path)
    claim = {'fund_id': 'SZSE.159919', 'kind': 'tracking', 'effective_from': '2025-06-30',
             'effective_to': '2025-06-30', 'index_code': '000300', 'source_title': '中期报告',
             'source_url': 'https://www.szse.cn/example.pdf', 'confirm': True}
    with TestClient(app) as client:
        assert client.post('/api/v1/index-fund-evidence', json=claim).status_code == 201
        for change in ({'effective_to': None}, {'effective_to': '2025-06-29'}, {'index_code': None},
                       {'source_url': 'http://example.com'}, {'source_url': 'https://user:secret@example.com/'},
                       {'source_sha256': 'invalid'}, {'verification': 'verified'}):
            assert client.post('/api/v1/index-fund-evidence', json={**claim, **change}).status_code == 422
        assert len(client.get('/api/v1/index-fund-evidence', params={'fund_id': 'SZSE.159919'}).json()['records']) == 1


def test_documented_evidence_examples_import_without_implying_verification(tmp_path):
    import json
    from pathlib import Path

    examples = json.loads((Path(__file__).resolve().parents[1] / 'examples/index-fund-evidence-claims.json').read_text())
    with TestClient(app_for(tmp_path)) as client:
        for claim in examples:
            response = client.post('/api/v1/index-fund-evidence', json=claim)
            assert response.status_code == 201
            assert response.json()['verification'] == 'unverified'
        records = client.get('/api/v1/index-fund-evidence', params={'fund_id': 'SZSE.159919'}).json()['records']
        tracking = next(row['claim'] for row in records if row['claim']['kind'] == 'tracking')
        assert tracking['effective_from'] == tracking['effective_to'] == '2025-06-30'
        assert tracking['published_date'] == '2025-08-29'


def test_upgrade_from_v20_retains_snapshots_and_checkpoints(tmp_path, monkeypatch):
    from ai_quant_assistant.storage import schema
    from ai_quant_assistant.storage.index_fund_snapshots import IndexFundSnapshotStore

    db = app_for(tmp_path).state.database
    migrations = schema.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(schema, 'MIGRATIONS', tuple(m for m in migrations if m.version <= 20))
        db.initialize()
        snapshot_id = IndexFundSnapshotStore(db).save('000300', {}, {}, {'points': []})
        with db.connect() as conn:
            conn.execute("INSERT INTO index_fund_sync_checkpoint(scope_id,partition_key,payload_json) VALUES ('a','b','[]')")
    db.initialize()
    assert db.status()['schema_version'] == schema.SCHEMA_VERSION
    assert IndexFundSnapshotStore(db).get(snapshot_id)['result'] == {'points': []}
    with db.connect(read_only=True) as conn:
        assert conn.execute('SELECT COUNT(*) FROM index_fund_sync_checkpoint').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM index_fund_evidence').fetchone()[0] == 0
