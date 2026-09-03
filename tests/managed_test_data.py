"""Explicit, validated local data fixtures for the M9-B admission contract."""

import json
from datetime import date

from ai_quant_assistant.storage.managed_coverage import coverage_report


def admit_fixture(connection, symbols):
    for symbol in symbols:
        security_id = connection.execute(
            "SELECT security_id FROM security_master WHERE symbol=?", [symbol]
        ).fetchone()[0]
        first, last = connection.execute(
            "SELECT MIN(trade_date),MAX(trade_date) FROM published_daily_bar WHERE security_id=?",
            [security_id],
        ).fetchone()
        if first is None:
            first, last = date(2024, 1, 2), date(2024, 1, 3)
            connection.execute(
                "UPDATE security_master SET list_date=COALESCE(list_date,?) WHERE security_id=?",
                [first, security_id],
            )
            connection.execute(
                """INSERT INTO daily_bar (security_id,trade_date,adjustment,open,high,low,close,
                       volume,amount,source_batch_id,quality_status)
                   SELECT ?,day,adjustment,10,11,9,10,100,1000,'managed-fixture','published'
                   FROM (VALUES (DATE '2024-01-02'),(DATE '2024-01-03')) days(day)
                   CROSS JOIN (VALUES ('none'),('qfq')) adjustments(adjustment)""",
                [security_id],
            )
        report = coverage_report(connection, security_id, first, last)
        assert report["availability"] == "ready", report
        connection.execute(
            """INSERT INTO managed_stock(security_id,target_start,target_end,availability,coverage_json)
               VALUES (?,?,?,'ready',?) ON CONFLICT(security_id) DO UPDATE SET
               target_start=EXCLUDED.target_start,target_end=EXCLUDED.target_end,
               availability=EXCLUDED.availability,coverage_json=EXCLUDED.coverage_json""",
            [security_id, first, last, json.dumps(report)],
        )
