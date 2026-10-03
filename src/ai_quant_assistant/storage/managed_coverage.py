"""Offline coverage policy shared by migration, admission and managed downloads."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ai_quant_assistant.storage.trading_gaps import blocking_gaps, classify_gaps, gap_summary

ADJUSTMENTS = ("none", "qfq")


def classified_daily_bar_gaps(
    connection: Any,
    security_id: str,
    start: date,
    end: date,
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return the complete, evidence-backed gap classification for a bounded range.

    Coverage and research pages intentionally preview only a small number of
    dates. Confirmed verification tasks use this helper so their frozen input is
    not accidentally limited to that presentation preview.
    """
    security = connection.execute(
        "SELECT exchange FROM security_master WHERE security_id=?", [security_id]
    ).fetchone()
    if security is None:
        return {adjustment: [] for adjustment in ADJUSTMENTS}
    expected = {
        row[0]
        for row in connection.execute(
            "SELECT trade_date FROM trading_calendar WHERE exchange=? AND is_open "
            "AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
            [security[0], start, end],
        ).fetchall()
    }
    missing_by_adjustment: dict[str, set[date]] = {}
    for adjustment in ADJUSTMENTS:
        observed = {
            row[0]
            for row in connection.execute(
                "SELECT trade_date FROM published_daily_bar WHERE security_id=? "
                "AND adjustment=? AND trade_date BETWEEN ? AND ?",
                [security_id, adjustment, start, end],
            ).fetchall()
        }
        missing_by_adjustment[adjustment] = expected - observed
    if evidence is None:
        previous = connection.execute(
            "SELECT coverage_json FROM managed_stock WHERE security_id=?", [security_id]
        ).fetchone()
        try:
            saved = json.loads(previous[0]) if previous and previous[0] else {}
        except (TypeError, json.JSONDecodeError):
            saved = {}
        evidence = saved.get("gap_verification", {})
    return classify_gaps(missing_by_adjustment, evidence)


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
    if listed is not None and calendar and calendar[0] > first_required:
        issues.append("交易日历未覆盖目标开始日期，不能豁免历史区间")
    if listed is not None and not expected:
        issues.append("目标期间没有可验证的交易日或尚未上市")
    if listed is None:
        issues.append("上市日期未知；应有交易日范围和缺口尚不能判定，不自动豁免上市前空区间")
    previous = connection.execute(
        "SELECT coverage_json FROM managed_stock WHERE security_id=?", [security_id]
    ).fetchone()
    try:
        previous_coverage = json.loads(previous[0]) if previous and previous[0] else {}
    except (TypeError, json.JSONDecodeError):
        previous_coverage = {}
    evidence = previous_coverage.get("gap_verification", {})
    details = {}
    missing_by_adjustment: dict[str, set[date]] = {}
    observed_by_adjustment: dict[str, set[date]] = {}
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
        missing_by_adjustment[adjustment] = set(missing)
        observed_by_adjustment[adjustment] = observed
    classified = classify_gaps(missing_by_adjustment, evidence)
    for adjustment in ADJUSTMENTS:
        observed = observed_by_adjustment[adjustment]
        missing = sorted(missing_by_adjustment[adjustment])
        unresolved = blocking_gaps(classified[adjustment])
        unexpected = sorted(observed - expected)
        if listed is not None and unresolved:
            issues.append(
                f"{adjustment}: {len(unresolved)} 个交易日缺口仍需核验"
                "（不自动假定停牌；重复下载相同范围通常无法解决）"
            )
        if unexpected:
            issues.append(f"{adjustment}: {len(unexpected)} 条记录不在已验证交易日历中")
        if len(observed) < 2:
            issues.append(f"{adjustment}: 有效行情不足两行")
        details[adjustment] = {
            "rows": len(observed),
            "start_date": str(min(observed)) if observed else None,
            "end_date": str(max(observed)) if observed else None,
            "expected_rows": len(expected) if listed is not None else None,
            "missing_count": len(missing) if listed is not None else None,
            "missing_sample": [str(day) for day in missing[:10]] if listed is not None else [],
            "blocking_missing_count": len(unresolved) if listed is not None else None,
            "gap_details": classified[adjustment][:20] if listed is not None else [],
            "gap_details_truncated": max(0, len(classified[adjustment]) - 20),
        }
    report = {
        "availability": "ready"
        if not issues
        else "partial"
        if any(d["rows"] for d in details.values())
        else "unavailable",
        "issues": issues,
        "listing_date_verified": listed is not None,
        "adjustments": details,
        "effective_start": str(start),
        "effective_end": str(end),
        "gap_summary": gap_summary(classified),
        "requires_gap_verification": any(
            item.get("classification") == "unverified_gap"
            for items in classified.values()
            for item in items
        ),
    }
    if evidence:
        report["gap_verification"] = evidence
    if previous_coverage.get("listing_evidence"):
        report["listing_evidence"] = previous_coverage["listing_evidence"]
    return report


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
