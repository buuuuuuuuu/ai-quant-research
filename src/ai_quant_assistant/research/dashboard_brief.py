"""Bounded local stock-pool dashboard and immutable research briefs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ai_quant_assistant.research.stock_workbench import SHANGHAI, StockResearchWorkbench
from ai_quant_assistant.research.stock_workbench import records as workbench_records
from ai_quant_assistant.storage.research_run_index import canonical
from ai_quant_assistant.storage.stock_pools import StockPoolService

TEMPLATE_VERSION = "m10b-v1"
MAX_POOL_SIZE = 500
MAX_SECTION_ITEMS = 100
MAX_EVENT_ITEMS = 50
MAX_SNAPSHOT_BYTES = 4_000_000
LIMITATIONS = [
    "研究看板是有界的本地资料汇总，不是研究就绪结论；行情 ready 不代表具体研究已就绪。",
    "没有本地记录不表示没有事件或风险；截断区块需进入个股页继续查看。",
    "历史行情缺少完整复权版本，尤其 qfq 的历史版本可信度保持 unknown。",
    "事件公开时间和当前审核状态分别表达；历史审核状态不能完整重建。",
    "管理状态、覆盖诊断和任务是当前本地状态，不伪装成历史状态。",
    "旧记录无可靠采集时区/首次批次时为 unknown；历史资料单列为采集时间不明，可能重复展示，不能声称是新公开。",
    "行情观察只做确定性阈值计算，不解释因果，也不构成投资建议。",
    "组合研究指标不是单股表现；缺少冻结范围的旧研究不会被推断关联。",
]


def records(c, sql, params=None):
    rows = workbench_records(c, sql, params)
    for row in rows:
        for key, value in row.items():
            if isinstance(value, str) and re.fullmatch(
                r"\d{4}-\d\d-\d\dT[0-9:.]+(?:[+-]\d\d:\d\d|Z)", value
            ):
                row[key] = datetime.fromisoformat(value).astimezone(UTC).isoformat()
    return rows


class ResearchDashboardBrief:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def _date(value, name):
        try:
            return value if isinstance(value, date) else date.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是 ISO 日期") from exc

    @classmethod
    def bounds(cls, window_start=None, as_of=None):
        today = datetime.now(SHANGHAI).date()
        end = cls._date(as_of, "as_of") if as_of else today
        start = cls._date(window_start, "window_start") if window_start else end - timedelta(days=6)
        if not date(1990, 1, 1) <= start <= end <= today:
            raise ValueError("增量窗口须在 1990 年至今天之间，且起点不能晚于 as_of")
        return (
            start,
            end,
            datetime.combine(start, time.min),
            datetime.combine(end + timedelta(days=1), time.min),
        )

    @staticmethod
    def _resolve_pool(c, pool_id, revision):
        if pool_id is None:
            row = c.execute(
                "SELECT pool_id FROM stock_pool WHERE is_default=TRUE AND status='active'"
            ).fetchone()
            if row is None:
                raise KeyError("default stock pool")
            pool_id = str(row[0])
        state = c.execute(
            "SELECT current_revision,status FROM stock_pool WHERE pool_id=?", [pool_id]
        ).fetchone()
        if state is None:
            raise KeyError(pool_id)
        if state[1] == "archived" and revision is None:
            raise RuntimeError("已归档股票池必须明确指定历史 revision")
        pool = StockPoolService(None)._get(c, pool_id, revision=revision or int(state[0]))
        if pool["status"] == "archived" and revision is None:
            raise RuntimeError("已归档股票池必须明确指定历史 revision")
        if not pool["symbols"]:
            raise ValueError("股票池没有可解析的冻结股票")
        if len(pool["symbols"]) > MAX_POOL_SIZE:
            raise ValueError(f"股票池超过 {MAX_POOL_SIZE} 只上限")
        return pool

    @staticmethod
    def _selected(symbols):
        sql = ",".join("(?,?)" for _ in symbols)
        params = [value for order, symbol in enumerate(symbols, 1) for value in (symbol, order)]
        return f"selected(symbol,display_order) AS (VALUES {sql})", params

    @staticmethod
    def _section(c, base, params, order, limit=MAX_SECTION_ITEMS):
        total, source_digest = c.execute(
            f"SELECT COUNT(*),sha256(COALESCE(string_agg(sha256(to_json(q)), '' ORDER BY sha256(to_json(q))),'')) FROM ({base}) q",
            params,
        ).fetchone()
        total = int(total)
        items = records(c, f"{base} ORDER BY {order} LIMIT ?", [*params, limit])
        return {
            "items": items,
            "total": total,
            "truncated": total > limit,
            "limit": limit,
            "source_digest": source_digest,
        }

    def _stocks(self, c, symbols, cutoff, adjustment):
        selected, params = self._selected(symbols)
        aware = cutoff.replace(tzinfo=SHANGHAI)
        return records(
            c,
            f"""WITH {selected}, identities AS (
          SELECT selected.symbol,selected.display_order,s.security_id,s.name,s.exchange,s.status listing_status
          FROM selected LEFT JOIN security_master s USING(symbol)
        ), prices AS (
          SELECT security_id,COUNT(*) price_rows,MIN(trade_date) price_start,MAX(trade_date) price_end
          FROM published_daily_bar WHERE security_id IN (SELECT security_id FROM identities) AND adjustment=? AND trade_date<? GROUP BY security_id
        ), announcements AS (
          SELECT security_id,MAX(published_at) latest_announcement FROM published_announcement
          WHERE security_id IN (SELECT security_id FROM identities) AND published_at<? GROUP BY security_id
        ), financials AS (
          SELECT security_id,MAX(published_at) latest_financial FROM published_financial_report
          WHERE security_id IN (SELECT security_id FROM identities) AND published_at<? GROUP BY security_id
        ), documents AS (
          SELECT a.security_id,MAX(d.extracted_at) latest_document FROM announcement_document d
          JOIN published_announcement a USING(announcement_id) WHERE a.security_id IN (SELECT security_id FROM identities) AND a.published_at<? GROUP BY a.security_id
        ), events AS (
          SELECT security_id,MAX(published_at) latest_event FROM document_event
          WHERE security_id IN (SELECT security_id FROM identities) AND published_at<? AND known_at<? GROUP BY security_id
        ), summaries AS (
          SELECT security_id,MAX(created_at) latest_summary FROM stock_research_summary
          WHERE security_id IN (SELECT security_id FROM identities) AND created_at<? GROUP BY security_id
        ) SELECT i.*,m.status management_status,m.availability,m.last_task_id,
          t.status task_status,t.progress task_progress,substring(COALESCE(t.error,t.message,''),1,1000) task_diagnostic,
          COALESCE(p.price_rows,0) price_rows,p.price_start,p.price_end,a.latest_announcement,
          f.latest_financial,d.latest_document,e.latest_event,s.latest_summary
        FROM identities i LEFT JOIN managed_stock m USING(security_id)
        LEFT JOIN operation_task t ON t.task_id=m.last_task_id LEFT JOIN prices p USING(security_id)
        LEFT JOIN announcements a USING(security_id) LEFT JOIN financials f USING(security_id)
        LEFT JOIN documents d USING(security_id) LEFT JOIN events e USING(security_id)
        LEFT JOIN summaries s USING(security_id) ORDER BY i.display_order""",
            [*params, adjustment, cutoff.date(), cutoff, cutoff, cutoff, aware, aware, aware],
        )

    def _deltas(self, c, symbols, start_at, cutoff, arrival_start=None):
        selected, p = self._selected(symbols)
        aware_start, aware_cutoff = (
            start_at.replace(tzinfo=SHANGHAI),
            cutoff.replace(tzinfo=SHANGHAI),
        )
        arrival_start = arrival_start or aware_start
        ann = f"""WITH {selected} SELECT a.announcement_id,s.symbol,s.name,a.title,a.report_type,
          a.report_period_end,a.published_at,a.first_visible_at ingested_at,a.first_visible_at IS NULL arrival_time_unknown,a.source_batch_id,a.first_source_batch_id,a.content_sha256,
          (a.first_visible_at>=? OR a.first_visible_at IS NULL) AND a.published_at<? late_arrival FROM published_announcement a
          JOIN security_master s USING(security_id) JOIN selected USING(symbol)
          WHERE a.published_at<? AND (a.first_visible_at<? OR a.first_visible_at IS NULL) AND ((a.published_at>=? AND a.published_at<?)
          OR (a.published_at<? AND (a.first_visible_at>=? OR a.first_visible_at IS NULL)))"""
        ap = [
            *p,
            arrival_start,
            start_at,
            cutoff,
            aware_cutoff,
            start_at,
            cutoff,
            start_at,
            arrival_start,
        ]
        announcements = self._section(c, ann, ap, "late_arrival,published_at DESC,announcement_id")
        fin = f"""WITH {selected} SELECT r.report_id,s.symbol,s.name,r.report_period_end,r.report_type,
          r.published_at,r.first_visible_at observed_at,r.first_visible_at IS NULL arrival_time_unknown,r.raw_sha256,r.availability_source,r.source_batch_id,m.metric_code,m.metric_value,m.unit,
          (r.first_visible_at>=? OR r.first_visible_at IS NULL) AND r.published_at<? late_arrival FROM published_financial_report r
          JOIN security_master s USING(security_id) JOIN selected USING(symbol)
          JOIN published_financial_metric m USING(report_id) WHERE r.published_at<? AND m.published_at<?
          AND (r.first_visible_at<? OR r.first_visible_at IS NULL) AND ((r.published_at>=? AND r.published_at<?) OR
          (r.published_at<? AND (r.first_visible_at>=? OR r.first_visible_at IS NULL)))"""
        fp = [
            *p,
            arrival_start,
            start_at,
            cutoff,
            cutoff,
            aware_cutoff,
            start_at,
            cutoff,
            start_at,
            arrival_start,
        ]
        financials = self._section(
            c, fin, fp, "late_arrival,published_at DESC,report_id,metric_code"
        )
        ev = f"""WITH {selected} SELECT e.event_id,e.announcement_id,e.document_id,s.symbol,s.name,
          e.event_type,e.event_date,e.published_at,e.known_at,e.status,e.direction,e.amount,e.currency,e.ratio,
          e.reviewed_at,e.created_at,e.created_at>=? AND e.published_at<? late_arrival
          FROM document_event e JOIN security_master s USING(security_id) JOIN selected USING(symbol)
          JOIN published_announcement a ON a.announcement_id=e.announcement_id
          WHERE e.published_at<? AND e.known_at<? AND e.created_at<? AND e.status!='rejected' AND
          ((e.published_at>=? AND e.published_at<?) OR (e.published_at<? AND e.created_at>=?) OR (e.reviewed_at>=? AND e.reviewed_at<?))"""
        ep = [
            *p,
            aware_start,
            aware_start,
            aware_cutoff,
            aware_cutoff,
            aware_cutoff,
            aware_start,
            aware_cutoff,
            aware_start,
            aware_start,
            aware_start,
            aware_cutoff,
        ]
        events = self._section(
            c, ev, ep, "late_arrival,published_at DESC,event_id", MAX_EVENT_ITEMS
        )
        accepted, review = [], []
        for item in events["items"]:
            item["evidence"], item["evidence_diagnostic"] = StockResearchWorkbench.event_evidence(
                c, item
            )
            reviewed = datetime.fromisoformat(item["reviewed_at"]) if item["reviewed_at"] else None
            valid = (
                item["status"] == "accepted"
                and reviewed
                and reviewed < aware_cutoff
                and not item["evidence_diagnostic"]
            )
            (accepted if valid else review).append(item)
        event_result = {
            "accepted": {
                "items": accepted,
                "total": len(accepted),
                "source_total": events["total"],
                "source_digest": events["source_digest"],
                "truncated": events["truncated"],
            },
            "review": {
                "items": review,
                "total": len(review),
                "source_total": events["total"],
                "source_digest": events["source_digest"],
                "truncated": events["truncated"],
            },
        }
        sums = f"""WITH {selected} SELECT x.summary_id,s.symbol,s.name,x.created_at,x.fingerprint
          FROM stock_research_summary x JOIN security_master s USING(security_id) JOIN selected USING(symbol)
          WHERE x.created_at>=? AND x.created_at<?"""
        summaries = self._section(
            c, sums, [*p, aware_start, aware_cutoff], "created_at DESC,summary_id"
        )
        query, params = StockResearchWorkbench.research_query(symbols)
        runs = f"""SELECT record_id,run_id,research_type,source,recorded_at,associated_symbol,
          sha256(spec_json) spec_sha256,sha256(result_json) result_sha256,
          CASE WHEN length(spec_json)<=100000 THEN spec_json ELSE NULL END spec_json
          FROM ({query}) r WHERE recorded_at>=? AND recorded_at<?
          AND json_extract_string(CASE WHEN json_valid(result_json) THEN result_json ELSE '{{}}' END,'$.status')='succeeded'"""
        research = self._section(
            c, runs, [*params, aware_start, aware_cutoff], "recorded_at DESC,record_id"
        )
        from ai_quant_assistant.storage.research_run_index import frozen_symbols

        for item in research["items"]:
            if item["spec_json"] is None:
                raise ValueError("旧研究输入超过有界读取限制，请通过原研究详情查看")
            item["resolved_symbols"] = frozen_symbols(json.loads(item.pop("spec_json"))) or (
                [item["associated_symbol"]] if item["associated_symbol"] else []
            )
            if item["source"] == "document_event_research" and not item["resolved_symbols"]:
                item["scope"] = "事件研究；通过不可变事件关联，完整参数见研究详情"
            else:
                item["scope"] = (
                    "该股票参与的组合研究；组合指标不是单股表现"
                    if len(item["resolved_symbols"]) > 1
                    else "单股研究"
                )
        return announcements, financials, event_result, summaries, research

    def _observations(
        self, c, symbols, cutoff, adjustment, change_threshold, volume_threshold, benchmark
    ):
        selected, p = self._selected(symbols)
        bars = records(
            c,
            f"""WITH {selected}, ranked AS (SELECT s.symbol,b.trade_date,b.close,b.volume,b.source_batch_id,b.security_id,
          ROW_NUMBER() OVER(PARTITION BY s.symbol ORDER BY b.trade_date DESC) rank
          FROM published_daily_bar b JOIN security_master s USING(security_id) JOIN selected USING(symbol)
          WHERE b.adjustment=? AND b.trade_date<?) SELECT * FROM ranked WHERE rank<=21 ORDER BY symbol,rank""",
            [*p, adjustment, cutoff.date()],
        )
        row = c.execute(
            """SELECT MAX(b.trade_date) FROM published_benchmark_daily_bar b JOIN benchmark_master m USING(benchmark_id)
          WHERE m.symbol=? AND b.trade_date<?""",
            [benchmark, cutoff.date()],
        ).fetchone()
        benchmark_date = str(row[0]) if row and row[0] else None
        return bars, benchmark_date

    @staticmethod
    def _calculate_observations(
        symbols, bars, benchmark_date, change_threshold, volume_threshold, benchmark
    ):
        grouped = defaultdict(list)
        for bar in bars:
            grouped[bar["symbol"]].append(bar)
        items, unknown = [], []
        for symbol in symbols:
            rows = grouped[symbol]
            if len(rows) < 2:
                unknown.append(
                    {"symbol": symbol, "reason": "有效行情不足两行，无法计算相邻交易记录涨跌幅"}
                )
                continue
            latest, previous = rows[0], rows[1]
            change = None
            if all(
                x["close"] is not None
                and math.isfinite(float(x["close"]))
                and float(x["close"]) > 0
                for x in (latest, previous)
            ):
                change = (float(latest["close"]) / float(previous["close"]) - 1) * 100
            volumes = [
                float(x["volume"])
                for x in rows[1:]
                if x["volume"] is not None
                and math.isfinite(float(x["volume"]))
                and float(x["volume"]) >= 0
            ]
            mean = sum(volumes) / len(volumes) if len(volumes) == 20 else None
            volume = latest["volume"]
            ratio = (
                float(volume) / mean
                if mean
                and mean > 0
                and volume is not None
                and math.isfinite(float(volume))
                and float(volume) >= 0
                else None
            )
            reasons = []
            if change is not None and abs(change) >= change_threshold:
                reasons.append(
                    f"相邻可用交易记录涨跌幅绝对值达到 {change_threshold:g}%（未豁免中间缺口）"
                )
            if ratio is not None and ratio >= volume_threshold:
                reasons.append(f"成交量达到前 20 个样本均值的 {volume_threshold:g} 倍")
            if benchmark_date and str(latest["trade_date"]) != benchmark_date:
                reasons.append("股票与基准最近可用交易日不一致")
            if change is None or ratio is None or benchmark_date is None:
                unknown.append(
                    {
                        "symbol": symbol,
                        "reason": "价格无效、前20条有效成交量不足/均值为零或基准缺失，相应统计量为 unknown",
                        "price_change_unknown": change is None,
                        "volume_ratio_unknown": ratio is None,
                        "benchmark_unknown": benchmark_date is None,
                    }
                )
            if reasons:
                items.append(
                    {
                        "symbol": symbol,
                        "trade_date": latest["trade_date"],
                        "previous_trade_date": previous["trade_date"],
                        "daily_change_pct": round(change, 6) if change is not None else None,
                        "volume_ratio": round(ratio, 6) if ratio is not None else None,
                        "benchmark_symbol": benchmark,
                        "benchmark_latest_date": benchmark_date,
                        "reasons": reasons,
                    }
                )
        return {
            "items": items,
            "unknown": unknown,
            "total": len(items),
            "evidence": bars,
            "benchmark_latest_date": benchmark_date,
        }

    def snapshot(
        self,
        *,
        pool_id=None,
        pool_revision=None,
        window_start=None,
        as_of=None,
        adjustment="qfq",
        price_change_threshold=5.0,
        volume_ratio_threshold=2.0,
        benchmark_symbol="csi000300",
    ):
        if adjustment not in {"none", "qfq", "hfq"}:
            raise ValueError("不支持的复权方式")
        if (
            not 0 <= float(price_change_threshold) <= 30
            or not 1 <= float(volume_ratio_threshold) <= 20
        ):
            raise ValueError("行情观察阈值超限")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", benchmark_symbol):
            raise ValueError("基准代码格式错误")
        with self.database.connect(read_only=True) as c:
            c.execute("SET TimeZone='UTC'")
            c.execute("BEGIN TRANSACTION")
            try:
                pool = self._resolve_pool(c, pool_id, pool_revision)
                requested = self._date(as_of, "as_of") if as_of else datetime.now(SHANGHAI).date()
                previous = None
                if window_start is None:
                    previous = c.execute(
                        "SELECT brief_id,as_of,created_at FROM research_brief WHERE pool_id=? AND pool_revision=? AND as_of<? AND json_extract_string(snapshot_json,'$.template_version')=? AND json_extract_string(snapshot_json,'$.parameters.adjustment')=? AND CAST(json_extract(snapshot_json,'$.parameters.price_change_threshold_pct') AS DOUBLE)=? AND CAST(json_extract(snapshot_json,'$.parameters.volume_ratio_threshold') AS DOUBLE)=? AND json_extract_string(snapshot_json,'$.parameters.benchmark_symbol')=? ORDER BY as_of DESC,created_at DESC LIMIT 1",
                        [
                            pool["pool_id"],
                            pool["resolved_revision"],
                            requested,
                            TEMPLATE_VERSION,
                            adjustment,
                            float(price_change_threshold),
                            float(volume_ratio_threshold),
                            benchmark_symbol,
                        ],
                    ).fetchone()
                    if previous:
                        window_start = previous[1] + timedelta(days=1)
                start, end, start_at, cutoff = self.bounds(window_start, requested)
                symbols = pool["symbols"]
                stocks = self._stocks(c, symbols, cutoff, adjustment)
                announcements, financials, events, summaries, research = self._deltas(
                    c, symbols, start_at, cutoff, previous[2] if previous else None
                )
                observation_inputs = self._observations(
                    c,
                    symbols,
                    cutoff,
                    adjustment,
                    float(price_change_threshold),
                    float(volume_ratio_threshold),
                    benchmark_symbol,
                )
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        observations = self._calculate_observations(
            symbols,
            *observation_inputs,
            float(price_change_threshold),
            float(volume_ratio_threshold),
            benchmark_symbol,
        )
        gaps = []
        for item in stocks:
            reasons = []
            if not item["security_id"]:
                reasons.append("股票池历史 revision 中的代码缺少证券主数据")
            if item["management_status"] != "active":
                reasons.append(f"本地管理状态为 {item['management_status'] or 'unmanaged'}")
            if item["availability"] != "ready":
                reasons.append(f"基础行情状态为 {item['availability'] or 'unavailable'}")
            if not item["price_rows"]:
                reasons.append("所选 as_of / 复权无本地行情")
            if not item["latest_financial"]:
                reasons.append("所选 as_of 缺少本地财务报告；具体研究需单独预检")
            if item["task_status"] == "failed":
                reasons.append(item["task_diagnostic"] or "最近同步任务失败")
            if reasons:
                gaps.append({"symbol": item["symbol"], "reasons": reasons})
        counts = {
            "active": sum(x["management_status"] == "active" for x in stocks),
            "archived": sum(x["management_status"] == "archived" for x in stocks),
            "partial": sum(x["availability"] == "partial" for x in stocks),
            "unavailable": sum(x["availability"] in {None, "unavailable"} for x in stocks),
            "sync_failed": sum(x["task_status"] == "failed" for x in stocks),
            "with_data_gaps": len(gaps),
        }
        snapshot = {
            "template_version": TEMPLATE_VERSION,
            "context": {
                "pool_id": pool["pool_id"],
                "pool_revision": pool["resolved_revision"],
                "window_start": start.isoformat(),
                "as_of": end.isoformat(),
                "timezone": "Asia/Shanghai",
                "window_semantics": "[window_start 当日 00:00, as_of 次日 00:00)，按真实公开时间；迟到资料另列",
                "previous_brief_id": str(previous[0]) if previous else None,
                "arrival_window_start": previous[2].astimezone(UTC).isoformat()
                if previous
                else start_at.replace(tzinfo=SHANGHAI).isoformat(),
                "local_state_basis": "current; not historical reconstruction",
                "mode": "current" if end == datetime.now(SHANGHAI).date() else "historical",
                "data_cutoff": cutoff.replace(tzinfo=SHANGHAI).isoformat(),
            },
            "pool": {
                key: pool[key]
                for key in (
                    "pool_id",
                    "name",
                    "source_type",
                    "status",
                    "history_verified",
                    "warning",
                    "symbols_sha256",
                    "resolved_revision",
                )
            }
            | {"status_counts": counts},
            "symbols": symbols,
            "parameters": {
                "adjustment": adjustment,
                "price_change_threshold_pct": float(price_change_threshold),
                "volume_ratio_threshold": float(volume_ratio_threshold),
                "volume_lookback_max": 20,
                "benchmark_symbol": benchmark_symbol,
                "price_formula": "(latest_close / previous_close - 1) * 100",
                "volume_formula": "latest_volume / mean(previous 20 valid volumes); fewer than 20 => unknown",
            },
            "stocks": {"items": stocks, "total": len(stocks)},
            "sections": {
                "announcements": announcements,
                "financial_metrics": financials,
                "late_arrivals": {
                    "items": [
                        x
                        for section in (announcements, financials)
                        for x in section["items"]
                        if x["late_arrival"]
                    ],
                    "total": sum(
                        x["late_arrival"]
                        for section in (announcements, financials)
                        for x in section["items"]
                    ),
                    "truncated": announcements["truncated"] or financials["truncated"],
                },
                "accepted_events": events["accepted"],
                "review_events": events["review"],
                "stock_summaries": summaries,
                "research_runs": research,
                "price_observations": observations,
                "data_gaps": {"items": gaps, "total": len(gaps)},
            },
            "limitations": LIMITATIONS,
            "evidence_window": f"每个增量区最多 {MAX_SECTION_ITEMS} 条；事件最多 {MAX_EVENT_ITEMS} 条并逐条复核引用。truncated=true 时不是完整清单。",
        }
        if len(canonical(snapshot).encode()) > MAX_SNAPSHOT_BYTES:
            raise ValueError("研究简报快照超过 4MB 上限，请缩短窗口或缩小股票池")
        return snapshot

    def dashboard(self, *, page=1, page_size=20, query="", sort="pool_order", **options):
        if not 1 <= int(page) <= 10000 or not 1 <= int(page_size) <= 100:
            raise ValueError("分页超限")
        if len(query) > 120:
            raise ValueError("搜索文字超过 120 字符")
        snapshot = self.snapshot(**options)
        normalized = query.strip().casefold()
        rows = snapshot["stocks"]["items"]
        if normalized:
            rows = [
                x
                for x in rows
                if normalized in str(x["symbol"]).casefold()
                or normalized in str(x["name"] or "").casefold()
            ]
        if sort not in {"pool_order", "symbol", "latest_announcement", "price_end"}:
            raise ValueError("不支持的排序")
        if sort != "pool_order":
            rows = sorted(
                rows, key=lambda x: (str(x.get(sort) or ""), x["symbol"]), reverse=sort != "symbol"
            )
        total = len(rows)
        offset = (int(page) - 1) * int(page_size)
        snapshot["stocks"] = {
            "items": rows[offset : offset + int(page_size)],
            "total": total,
            "pagination": {
                "page": int(page),
                "page_size": int(page_size),
                "total": total,
                "pages": max(1, math.ceil(total / int(page_size))),
            },
        }
        return snapshot

    def copilot_summary(self, **options):
        normalized = {
            {"stock_pool_id": "pool_id", "stock_pool_revision": "pool_revision"}.get(k, k): v
            for k, v in options.items()
        }
        snapshot = self.snapshot(**normalized)
        sections = snapshot["sections"]
        keys = (
            "announcements",
            "financial_metrics",
            "accepted_events",
            "review_events",
            "price_observations",
            "data_gaps",
        )
        return {
            "pool_id": snapshot["context"]["pool_id"],
            "pool_name": snapshot["pool"]["name"],
            "pool_revision": snapshot["context"]["pool_revision"],
            "window_start": snapshot["context"]["window_start"],
            "as_of": snapshot["context"]["as_of"],
            "timezone": snapshot["context"]["timezone"],
            "counts": {k: int(sections[k]["total"]) for k in keys},
            "affected_symbols": list(
                dict.fromkeys(
                    i["symbol"] for k in keys for i in sections[k]["items"] if i.get("symbol")
                )
            )[:20],
            "limitations": snapshot["limitations"],
            "action": "前往 /research-dashboard 查看逐项证据；本工具未生成简报、下载或研究任务。",
        }

    @staticmethod
    def render(snapshot, brief_id, created_at):
        environment = Environment(
            loader=FileSystemLoader(Path(__file__).parents[1] / "reports" / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )
        return environment.get_template("research_brief.html").render(
            snapshot=snapshot, brief_id=brief_id, created_at=created_at
        )

    @staticmethod
    def _audit(c, action, actor, object_id, details, outcome="success"):
        c.execute(
            "INSERT INTO operation_audit(event_id,action,actor,outcome,object_type,object_id,details_json) VALUES(?,?,?,?,?,?,?)",
            [
                f"audit-{uuid4().hex}",
                action,
                actor,
                outcome,
                "research_brief",
                object_id,
                canonical(details),
            ],
        )

    @staticmethod
    def fingerprint(snapshot):
        return hashlib.sha256(canonical(snapshot).encode()).hexdigest()

    def preview(self, **options):
        snapshot = self.snapshot(**options)
        return {"snapshot": snapshot, "fingerprint": self.fingerprint(snapshot)}

    def generate(self, *, actor="local", expected_fingerprint=None, **options):
        snapshot = None
        brief_id = None
        created_at = datetime.now(UTC).isoformat()
        try:
            snapshot = self.snapshot(**options)
            if (
                expected_fingerprint is not None
                and self.fingerprint(snapshot) != expected_fingerprint
            ):
                raise RuntimeError("本地证据或股票池已变化，请重新预览后确认")
            encoded = canonical(snapshot)
            fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
            brief_id = f"brief-{fingerprint}"
            html = self.render(snapshot, brief_id, created_at)
            html_sha = hashlib.sha256(html.encode()).hexdigest()
            context = snapshot["context"]
            with self.database.connect() as c:
                c.execute("BEGIN TRANSACTION")
                try:
                    existing = c.execute(
                        "SELECT brief_id FROM research_brief WHERE fingerprint=?", [fingerprint]
                    ).fetchone()
                    if not existing:
                        c.execute(
                            "INSERT INTO research_brief VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            [
                                brief_id,
                                fingerprint,
                                context["pool_id"],
                                context["pool_revision"],
                                context["window_start"],
                                context["as_of"],
                                context["timezone"],
                                canonical(snapshot["symbols"]),
                                snapshot["pool"]["symbols_sha256"],
                                encoded,
                                html,
                                html_sha,
                                actor,
                                created_at,
                            ],
                        )
                        self._audit(
                            c,
                            "research_brief_created",
                            actor,
                            brief_id,
                            {
                                "pool_id": context["pool_id"],
                                "pool_revision": context["pool_revision"],
                                "window_start": context["window_start"],
                                "as_of": context["as_of"],
                                "symbol_count": len(snapshot["symbols"]),
                                "fingerprint": fingerprint,
                                "symbols_sha256": snapshot["pool"]["symbols_sha256"],
                                "evidence_counts": {
                                    k: v["total"] for k, v in snapshot["sections"].items()
                                },
                                "task_id": None,
                            },
                        )
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
            return self.get(brief_id)
        except Exception as exc:
            details = {
                "reason": "preview_render_or_persistence_failed",
                "pool_id": options.get("pool_id"),
                "pool_revision": options.get("pool_revision"),
                "window_start": options.get("window_start"),
                "as_of": options.get("as_of"),
                "task_id": None,
            }
            if snapshot is not None:
                details.update(snapshot["context"])
                details.update(
                    symbol_count=len(snapshot["symbols"]),
                    symbols_sha256=snapshot["pool"]["symbols_sha256"],
                    fingerprint=self.fingerprint(snapshot),
                    evidence_counts={k: v["total"] for k, v in snapshot["sections"].items()},
                )
            try:
                with self.database.connect() as c:
                    self._audit(
                        c,
                        "research_brief_failed",
                        actor,
                        brief_id or str(options.get("pool_id") or "default"),
                        details,
                        "failure",
                    )
            except Exception:
                exc.add_note("research_brief_failed audit could not be persisted")
            raise

    @staticmethod
    def _brief_id(value):
        if not re.fullmatch(r"brief-[0-9a-f]{64}", value):
            raise KeyError(value)
        return value

    def get(self, brief_id, *, html=False):
        self._brief_id(brief_id)
        with self.database.connect(read_only=True) as c:
            rows = records(c, "SELECT * FROM research_brief WHERE brief_id=?", [brief_id])
        if not rows:
            raise KeyError(brief_id)
        row = rows[0]
        if (
            hashlib.sha256(row["snapshot_json"].encode()).hexdigest() != row["fingerprint"]
            or hashlib.sha256(row["html_content"].encode()).hexdigest() != row["html_sha256"]
        ):
            raise RuntimeError("简报内容校验失败，不允许作为可信产物下载")
        if html:
            return row["html_content"]
        row["snapshot"] = json.loads(row.pop("snapshot_json"))
        row.pop("html_content")
        row["symbols"] = json.loads(row.pop("symbols_json"))
        return row

    def list(self, *, pool_id=None, page=1, page_size=20):
        if not 1 <= int(page) <= 10000 or not 1 <= int(page_size) <= 100:
            raise ValueError("分页超限")
        where, params = ("WHERE pool_id=?", [pool_id]) if pool_id else ("", [])
        with self.database.connect(read_only=True) as c:
            total = int(
                c.execute(f"SELECT COUNT(*) FROM research_brief {where}", params).fetchone()[0]
            )
            items = records(
                c,
                f"SELECT brief_id,pool_id,pool_revision,window_start,as_of,timezone,symbols_sha256,actor,created_at FROM research_brief {where} ORDER BY created_at DESC,brief_id LIMIT ? OFFSET ?",
                [*params, int(page_size), (int(page) - 1) * int(page_size)],
            )
        return {
            "items": items,
            "pagination": {
                "page": int(page),
                "page_size": int(page_size),
                "total": total,
                "pages": max(1, math.ceil(total / int(page_size))),
            },
        }
