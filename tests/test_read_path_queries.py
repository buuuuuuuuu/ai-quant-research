"""Read models preserve distinct counts and missing-data semantics."""
from datetime import date

from test_data_platform import _database

from ai_quant_assistant.config import Settings
from ai_quant_assistant.storage.monitoring import (
    OperationAlertRepository,
    OperationAuditRepository,
    PlatformMonitor,
)


def test_read_models_count_independently_and_keep_missing_dates(tmp_path):
    db = _database(tmp_path)
    db.initialize()
    with db.connect() as con:
        for i, state in [(1, 'active'), (2, 'active'), (3, 'active'), (4, 'active'), (5, 'suspended')]:
            con.execute("INSERT INTO security_master (security_id,symbol,exchange,name,status,source,source_batch_id) VALUES (?,?,'SZSE',?,?,'test','test')",
                        [f'SZSE.{i:06}', f'{i:06}', f'Security {i}', state])
        # Isolate read models from publication mechanics; duplicate rows must not inflate counts.
        con.execute('DROP VIEW published_daily_bar')
        con.execute("CREATE VIEW published_daily_bar AS SELECT * FROM (VALUES ('SZSE.000001',DATE '2026-01-01','qfq'), ('SZSE.000001',DATE '2026-01-01','qfq'), ('SZSE.000001',DATE '2026-01-02','qfq'), ('SZSE.000001',DATE '2026-01-03','none'), ('SZSE.000002',DATE '2026-02-01','qfq'), ('SZSE.000005',DATE '2026-03-01','qfq')) t(security_id,trade_date,adjustment)")
        con.execute('DROP VIEW published_financial_report')
        con.execute("CREATE VIEW published_financial_report AS SELECT * FROM (VALUES ('SZSE.000001','r1',DATE '2025-09-30'), ('SZSE.000001','r1',DATE '2025-09-30'), ('SZSE.000001','r2',DATE '2025-12-31'), ('SZSE.000003','r3',DATE '2025-12-31')) t(security_id,report_id,report_period_end)")
    overview = {row['symbol']: row for row in db.research_overview()['securities']}
    assert set(overview) == {'000001', '000002', '000003', '000005'}
    assert overview['000001']['price_rows'] == 2
    assert overview['000001']['report_count'] == 2
    assert overview['000001']['end_date'].date() == date(2026, 1, 2)
    assert overview['000002']['report_count'] == 0
    assert overview['000003']['price_rows'] == 0
    assert overview['000003']['start_date'] is None
    monitor = PlatformMonitor(db, Settings(), OperationAlertRepository(db), OperationAuditRepository(db))
    quality = {row['symbol']: row for row in monitor.quality()['securities']}
    assert set(quality) == {'000001', '000002', '000003', '000004'}
    assert quality['000001']['price_rows'] == 2
    assert quality['000001']['financial_reports'] == 2
    assert quality['000001']['latest_trade_date'] == '2026-01-02'
    assert quality['000004']['latest_trade_date'] is None
    assert quality['000004']['latest_financial_period'] is None
    assert quality['000004']['price_rows'] == quality['000004']['financial_reports'] == 0
    counts = db.status()['relations']
    with db.connect(read_only=True) as con:
        for name, count in counts.items():
            assert count == con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
