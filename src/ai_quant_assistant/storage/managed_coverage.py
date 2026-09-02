"""Offline coverage policy shared by migration, admission and managed downloads."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

ADJUSTMENTS = ("none", "qfq")


def market_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def completed_date(as_of: date, now: datetime | None = None) -> date:
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    # Conservative publication cutoff; today's unfinished bars are never imported.
    cutoff = now.date() if now.time() >= time(16, 30) else now.date() - timedelta(days=1)
    return min(as_of, cutoff)


def coverage_report(
    connection: Any, security_id: str, start: date, end: date, listing_date: date | None = None
) -> dict[str, Any]:
    security = connection.execute(
        "SELECT exchange, list_date, delist_date FROM security_master WHERE security_id=?",
        [security_id],
    ).fetchone()
    issues: list[str] = []
    if security is None:
        return {
            "availability": "unavailable",
            "issues": ["证券主数据缺失，请更新目录"],
            "adjustments": {},
            "effective_start": str(start),
            "effective_end": str(end),
        }
    listed = listing_date or security[1]
    start = max(start, listed) if listed else start
    end = min(end, security[2] - timedelta(days=1)) if security[2] else end
    calendar = [
        row[0]
        for row in connection.execute(
            "SELECT trade_date FROM trading_calendar WHERE exchange=? AND is_open ORDER BY trade_date",
            [security[0]],
        ).fetchall()
    ]
    expected = {day for day in calendar if start <= day <= end}
    last_required = end - timedelta(days=max(0, end.weekday() - 4))
    if not calendar or calendar[-1] < last_required:
        issues.append("交易日历未覆盖目标结束日期，不能确认完整性")
    first_required = start + timedelta(days=(7 - start.weekday()) if start.weekday() >= 5 else 0)
    if calendar and calendar[0] > first_required:
        issues.append("交易日历未覆盖目标开始日期，不能豁免历史区间")
    if not expected:
        issues.append("目标期间没有可验证的交易日或尚未上市")
    if listed is None:
        issues.append("上市日期未知；上市前空区间不能自动豁免")
    details = {}
    for adjustment in ADJUSTMENTS:
        observed = {
            row[0]
            for row in connection.execute(
                """SELECT trade_date FROM published_daily_bar WHERE security_id=?
               AND adjustment=? AND trade_date BETWEEN ? AND ?""",
                [security_id, adjustment, start, end],
            ).fetchall()
        }
        missing = sorted(expected - observed)
        unexpected = sorted(observed - expected)
        if missing:
            issues.append(f"{adjustment}: {len(missing)} 个未解释的交易日缺口（不自动假定停牌）")
        if unexpected:
            issues.append(f"{adjustment}: {len(unexpected)} 条记录不在已验证交易日历中")
        if len(observed) < 2:
            issues.append(f"{adjustment}: 有效行情不足两行")
        details[adjustment] = {
            "rows": len(observed),
            "start_date": str(min(observed)) if observed else None,
            "end_date": str(max(observed)) if observed else None,
            "expected_rows": len(expected),
            "missing_count": len(missing),
            "missing_sample": [str(day) for day in missing[:10]],
        }
    return {
        "availability": "ready"
        if not issues
        else "partial"
        if any(d["rows"] for d in details.values())
        else "unavailable",
        "issues": issues,
        "adjustments": details,
        "effective_start": str(start),
        "effective_end": str(end),
    }


def seed_managed_stocks(connection: Any) -> None:
    """One-time migration/fixture adoption, never a runtime directory enrollment."""
    rows = connection.execute(
        """WITH identities AS (
             SELECT security_id FROM daily_bar UNION SELECT security_id FROM announcement
             UNION SELECT security_id FROM financial_report
             UNION SELECT security_id FROM stock_pool_member), spans AS (
             SELECT security_id, MIN(trade_date) first_day, MAX(trade_date) last_day
             FROM published_daily_bar GROUP BY security_id)
           SELECT i.security_id, p.first_day, p.last_day, s.list_date
           FROM identities i LEFT JOIN spans p USING(security_id)
           LEFT JOIN security_master s USING(security_id)
           WHERE NOT EXISTS (SELECT 1 FROM managed_stock m WHERE m.security_id=i.security_id)
           ORDER BY i.security_id"""
    ).fetchall()
    for security_id, first, last, listed in rows:
        start, end = first or date(2012, 1, 1), last or market_today()
        report = coverage_report(connection, security_id, start, end, listed)
        report["migration_note"] = "仅采用已存历史覆盖；不声称覆盖 2012 至今，不自动下载"
        connection.execute(
            """INSERT INTO managed_stock (security_id, target_start, target_end,
               availability, coverage_json, listing_date, listing_source, auto_update)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                security_id,
                start,
                end,
                report["availability"],
                json.dumps(report, ensure_ascii=False),
                listed,
                "security_master" if listed else None,
                first is not None,
            ],
        )
