from __future__ import annotations

from datetime import date, datetime
from typing import Any

FIXTURE_BATCH_ID = "fixture-data-platform-v1"
FIXTURE_TIMESTAMP = datetime(2024, 1, 8, 18, 30)


def load_fixture_data(connection: Any) -> dict[str, int]:
    connection.execute(
        """
        INSERT OR REPLACE INTO sync_batch (
            batch_id, dataset, status, started_at, ended_at, request_json, row_count, created_at
        ) VALUES (?, 'data_platform_fixture', 'published', ?, ?, ?, 29, ?)
        """,
        [
            FIXTURE_BATCH_ID,
            FIXTURE_TIMESTAMP,
            datetime(2024, 1, 8, 18, 30, 1),
            '{"source":"fixture-v1"}',
            FIXTURE_TIMESTAMP,
        ],
    )
    connection.executemany(
        """
        INSERT INTO security_master (
            security_id, symbol, exchange, name, list_date, status, industry,
            source, source_batch_id, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'active', ?, 'fixture-v1', ?, ?)
        ON CONFLICT (security_id) DO UPDATE SET
            symbol = EXCLUDED.symbol,
            exchange = EXCLUDED.exchange,
            name = EXCLUDED.name,
            list_date = EXCLUDED.list_date,
            status = EXCLUDED.status,
            industry = EXCLUDED.industry,
            source = EXCLUDED.source,
            source_batch_id = EXCLUDED.source_batch_id,
            updated_at = EXCLUDED.updated_at
        """,
        [
            ["SSE.600519", "600519", "SSE", "贵州茅台", date(2001, 8, 27), "食品饮料", FIXTURE_BATCH_ID, FIXTURE_TIMESTAMP],
            ["SZSE.000001", "000001", "SZSE", "平安银行", date(1991, 4, 3), "银行", FIXTURE_BATCH_ID, FIXTURE_TIMESTAMP],
            ["BSE.430047", "430047", "BSE", "诺思兰德", date(2020, 11, 24), "医药生物", FIXTURE_BATCH_ID, FIXTURE_TIMESTAMP],
        ],
    )
    trade_dates = [
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 5),
        date(2024, 1, 8),
    ]
    calendar_rows = []
    for exchange in ("SSE", "SZSE", "BSE"):
        for index, trade_date in enumerate(trade_dates):
            calendar_rows.append(
                [
                    exchange,
                    trade_date,
                    True,
                    trade_dates[index - 1] if index > 0 else None,
                    trade_dates[index + 1] if index + 1 < len(trade_dates) else None,
                    index + 1,
                    "fixture-v1",
                    FIXTURE_BATCH_ID,
                ]
            )
    connection.executemany(
        """
        INSERT OR REPLACE INTO trading_calendar
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        calendar_rows,
    )
    prices = [
        [date(2024, 1, 2), 1700.0, 1725.0, 1690.0, 1715.0, 25000.0, 42875000.0],
        [date(2024, 1, 3), 1715.0, 1730.0, 1702.0, 1720.0, 24000.0, 41280000.0],
        [date(2024, 1, 4), 1720.0, 1742.0, 1710.0, 1735.0, 26000.0, 45110000.0],
        [date(2024, 1, 5), 1735.0, 1740.0, 1718.0, 1728.0, 22000.0, 38016000.0],
        [date(2024, 1, 8), 1728.0, 1750.0, 1720.0, 1745.0, 28000.0, 48860000.0],
    ]
    raw_rows = [
        ["SSE.600519", trade_date, "fixture-v1", FIXTURE_BATCH_ID, *values, FIXTURE_TIMESTAMP]
        for trade_date, *values in prices
    ]
    connection.executemany(
        """
        INSERT OR REPLACE INTO daily_bar_raw (
            security_id, trade_date, source, batch_id, open, high, low, close, volume, amount,
            ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        raw_rows,
    )
    published_rows = [
        ["SSE.600519", trade_date, "none", *values, FIXTURE_BATCH_ID, FIXTURE_TIMESTAMP]
        for trade_date, *values in prices
    ]
    connection.executemany(
        """
        INSERT OR REPLACE INTO daily_bar (
            security_id, trade_date, adjustment, open, high, low, close, volume, amount,
            source_batch_id, quality_status, published_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'published', ?)
        """,
        published_rows,
    )
    connection.execute(
        """
        INSERT OR REPLACE INTO dq_result (
            batch_id, rule_id, object_key, severity, passed, observed, expected, details,
            checked_at
        ) VALUES (?, 'primary_key_unique', 'daily_bar:SSE.600519', 'blocking', TRUE,
                  '5 unique rows', '5 unique rows', 'offline fixture validation', ?)
        """,
        [FIXTURE_BATCH_ID, FIXTURE_TIMESTAMP],
    )
    return {
        "security_master": 3,
        "trading_calendar": len(calendar_rows),
        "daily_bar_raw": len(raw_rows),
        "daily_bar": len(published_rows),
        "dq_result": 1,
    }
