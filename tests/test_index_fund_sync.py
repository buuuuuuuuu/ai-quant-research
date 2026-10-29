import time
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.config import Settings


class Shares:
    def fetch_day(self, market, day):
        return [SimpleNamespace(fund_id="SSE.510300", shares=Decimal("100"))]


class Nav:
    def fetch(self, fund, start, end):
        return [SimpleNamespace(trade_date=start, unit_nav=Decimal("2"))]


class Cap:
    def fetch_month(self, index_code, year, month):
        return [SimpleNamespace(trade_date=date(2026, 9, day), value_cny=Decimal("10000")) for day in (1, 2)]


def test_confirmed_background_sync_publishes_snapshot_with_gaps(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", artifact_dir=tmp_path / "artifacts", cache_dir=tmp_path / "cache", duckdb_path=tmp_path / "data" / "catalog.duckdb")
    app = create_app(settings)
    db = app.state.database
    db.initialize()
    with db.connect() as c:
        for number, day in enumerate((date(2026, 9, 1), date(2026, 9, 2)), 1):
            c.execute("INSERT INTO trading_calendar(exchange,trade_date,is_open,source,source_batch_id,session_no) VALUES ('SSE',?,TRUE,'test','test',?)", [day, number])
    service = app.state.index_fund_tasks.service
    service.shares, service.nav, service.cap = Shares(), Nav(), Cap()
    payload = {"index_code": "000300", "start": "2026-09-01", "end": "2026-09-02", "funds": ["SSE.510300"], "confirm": False}
    with TestClient(app) as client:
        assert client.post('/api/v1/index-funds/sync', json=payload).status_code == 422
        payload['confirm'] = True
        submitted = client.post('/api/v1/index-funds/sync', json=payload)
        assert submitted.status_code == 202, submitted.text
        task_id = submitted.json()['task_id']
        for _ in range(200):
            task = client.get('/api/v1/index-funds/tasks/' + task_id).json()
            if task['status'] in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        assert task['status'] == 'succeeded', task
        data = client.get('/api/v1/index-funds/000300').json()['snapshot']
        assert data['result']['points'][0]['ratio_fraction'] == '0.02'
        assert data['result']['points'][1]['ratio_fraction'] is None
        assert data['result']['scope_verified'] is False


def test_failed_sync_preserves_old_snapshot_and_can_retry(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", artifact_dir=tmp_path / "artifacts", cache_dir=tmp_path / "cache", duckdb_path=tmp_path / "data" / "catalog.duckdb")
    app = create_app(settings)
    db = app.state.database
    db.initialize()
    with db.connect() as c:
        c.execute("INSERT INTO trading_calendar(exchange,trade_date,is_open,source,source_batch_id,session_no) VALUES ('SSE','2026-09-01',TRUE,'test','test',1)")
    from ai_quant_assistant.storage.index_fund_snapshots import IndexFundSnapshotStore

    store = IndexFundSnapshotStore(db)
    prior = store.save('000300', {}, {}, {'points': []})
    service = app.state.index_fund_tasks.service
    class BadNav:
        def fetch(self, *args):
            raise RuntimeError('provider failed')
    service.nav = BadNav()
    task, created = service.request('000300', date(2026, 9, 1), date(2026, 9, 1), ['SSE.510300'], True)
    assert created
    duplicate, created = service.request('000300', date(2026, 9, 1), date(2026, 9, 1), ['SSE.510300'], True)
    assert not created and duplicate['task_id'] == task['task_id']
    import pytest
    with pytest.raises(RuntimeError, match='provider failed'):
        service.run(task['task_id'])
    assert service.tasks.get(task['task_id'])['status'] == 'failed'
    assert store.latest('000300')['snapshot_id'] == prior
    retry, created = service.request('000300', date(2026, 9, 1), date(2026, 9, 1), ['SSE.510300'], True)
    assert created and retry['task_id'] != task['task_id']
