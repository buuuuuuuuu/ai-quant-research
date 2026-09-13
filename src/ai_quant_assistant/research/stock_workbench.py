"""Bounded, local-only stock dossier and immutable offline summaries.

Lock order: existing DuckDB access lock only. Read snapshot, release lock,
render, then a short insert+audit transaction. No network or artifact writes.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ai_quant_assistant.storage.data_explorer import DataExplorer
from ai_quant_assistant.storage.research_run_index import canonical, frozen_symbols

SHANGHAI = ZoneInfo("Asia/Shanghai")
TEMPLATE_VERSION = "m10a-v1"
LIMITATIONS = [
    "当前身份、管理状态和股票池成员是当前上下文，不代表历史时点状态。",
    "行情按交易日过滤；本库没有完整历史行情/复权版本，尤其 qfq 可能使用后续复权信息，历史版本可信度 unknown。",
    "财务按真实公开时间筛选；报告期不等于公开时间。保留版本、单位和口径，不填零，不自动推导同比或因果关系。",
    "事件公开时间与人工审核时间不同；无法证明当时已接受的事件不进入历史摘要事实。",
    "摘要是有限本地证据摘录，不代表全部资料；没有数据不等于没有事件或风险。行情可用不等于研究就绪。",
    "旧研究若只有文件或缺少可靠冻结范围，无法自动关联；不扫描产物目录，也不按当前股票池反推。动态股票范围研究尚不自动关联。",
]


class BorrowedReadSnapshot:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self, *, read_only=False):
        if not read_only:
            raise RuntimeError("read-only snapshot")
        yield self.connection


def records(connection, sql, params=()):
    cursor = connection.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [
        {name: DataExplorer._value(value, name) for name, value in zip(names, row, strict=True)}
        for row in cursor.fetchall()
    ]


class StockResearchWorkbench:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def bounds(mode="current", as_of=None, start_date=None):
        today = datetime.now(SHANGHAI).date()
        if mode not in {"current", "historical"}:
            raise ValueError("未知浏览模式")
        if mode == "historical" and not as_of:
            raise ValueError("历史视图需要 as_of")
        if mode == "current" and as_of:
            raise ValueError("当前浏览不能指定历史 as_of")
        end = date.fromisoformat(str(as_of)) if as_of else today
        start = date.fromisoformat(str(start_date)) if start_date else date(2012, 1, 1)
        if not date(1990, 1, 1) <= start <= end <= today:
            raise ValueError("日期须在 1990 年至今天之间，且起点不能晚于终点")
        # Existing financial/announcement timestamps are naive local market time.
        # Event timestamps are TIMESTAMPTZ: bind the same boundary with +08:00.
        cutoff = datetime.combine(end + timedelta(days=1), time.min)
        return {
            "mode": mode,
            "as_of": end.isoformat(),
            "start_date": start.isoformat(),
            "timezone": "Asia/Shanghai",
            "boundary": "as_of 当日结束（次日零点之前）",
        }, cutoff

    @staticmethod
    def identity(connection, symbol):
        if not re.fullmatch(r"[0-9]{6}", symbol):
            raise ValueError("请输入六位股票代码")
        found = records(
            connection,
            "SELECT security_id,symbol,name,exchange,status AS listing_status FROM security_master WHERE symbol=? LIMIT 2",
            [symbol],
        )
        if not found:
            raise KeyError(symbol)
        if len(found) != 1:
            raise RuntimeError("证券目录身份冲突")
        return found[0]

    def overview_in(self, c, symbol, context, cutoff):
        stock = self.identity(c, symbol)
        sid = stock["security_id"]
        managed = records(
            c,
            "SELECT status,availability,target_start,target_end,revision,generation,last_success_at,last_task_id,updated_at,CASE WHEN length(coverage_json)<=100000 THEN coverage_json ELSE NULL END AS coverage_json FROM managed_stock WHERE security_id=?",
            [sid],
        )
        stock["management"] = (
            managed[0] if managed else {"status": "unmanaged", "availability": "unavailable"}
        )
        if managed:
            raw_coverage = stock["management"].pop("coverage_json")
            try:
                stock["management"]["diagnostics"] = (
                    json.loads(raw_coverage)
                    if raw_coverage
                    else {"warning": "管理覆盖记录超限或缺失，需在原管理页查看"}
                )
            except ValueError:
                stock["management"]["diagnostics"] = {"warning": "管理覆盖 JSON 无效，不代表就绪"}
        stock["pools"] = records(
            c,
            "SELECT p.pool_id,p.name,p.current_revision,p.status FROM stock_pool p JOIN stock_pool_member m USING(pool_id) WHERE m.security_id=? ORDER BY p.pool_id LIMIT 101",
            [sid],
        )
        stock["pools_truncated"] = len(stock["pools"]) > 100
        stock["pools"] = stock["pools"][:100]
        stock["task"] = None
        task_id = stock["management"].get("last_task_id")
        if task_id:
            tasks = records(
                c,
                "SELECT task_id,status,progress,substring(message,1,2000) AS message,substring(error,1,2000) AS error FROM operation_task WHERE task_id=?",
                [task_id],
            )
            stock["task"] = tasks[0] if tasks else None
        stock["coverage"] = {}
        stock["coverage"]["prices"] = records(
            c,
            "SELECT adjustment,COUNT(*) AS rows,MIN(trade_date) AS start_date,MAX(trade_date) AS end_date FROM published_daily_bar WHERE security_id=? AND trade_date BETWEEN ? AND ? GROUP BY adjustment ORDER BY adjustment",
            [sid, context["start_date"], context["as_of"]],
        )
        for key, table, field in [
            ("announcements", "published_announcement", "published_at"),
            ("financials", "published_financial_report", "published_at"),
        ]:
            stock["coverage"][key] = records(
                c,
                f"SELECT COUNT(*) AS rows,MIN({field}) AS first_publication,MAX({field}) AS last_publication FROM {table} WHERE security_id=? AND {field} < ?",
                [sid, cutoff],
            )[0]
        stock["coverage"]["documents"] = records(
            c,
            "SELECT d.status,COUNT(*) AS rows FROM announcement_document d JOIN published_announcement a USING(announcement_id) WHERE a.security_id=? AND a.published_at < ? GROUP BY d.status ORDER BY d.status",
            [sid, cutoff],
        )
        return {
            "stock": stock,
            "context": context,
            "limitations": LIMITATIONS,
            "research_ready": None,
        }

    def overview(self, symbol, **options):
        context, cutoff = self.bounds(**options)
        with self.database.connect(read_only=True) as c:
            return self.overview_in(c, symbol, context, cutoff)

    def section_in(
        self, c, symbol, section, context, cutoff, *, page=1, page_size=20, adjustment="qfq"
    ):
        if not 1 <= page <= 10000 or not 1 <= page_size <= 100:
            raise ValueError("分页超限")
        identity = self.identity(c, symbol)
        explorer = DataExplorer(BorrowedReadSnapshot(c))
        paging = {"page": page, "page_size": page_size}
        if section == "prices":
            return explorer.prices(
                symbol,
                start_date=context["start_date"],
                end_date=context["as_of"],
                adjustment=adjustment,
                **paging,
            )
        if section in {"announcements", "financial_reports", "financial_metrics"}:
            return getattr(explorer, section)(symbol, published_before=cutoff, **paging)
        params = [identity["security_id"], cutoff]
        if section == "documents":
            query = """SELECT d.document_id,d.announcement_id,d.version_no,d.sha256,d.status,d.page_count,d.extracted_at,a.title,a.published_at
                FROM announcement_document d JOIN published_announcement a USING(announcement_id)
                WHERE a.security_id=? AND a.published_at < ?"""
            order = "published_at DESC,document_id"
        elif section == "events":
            params = [
                identity["security_id"],
                cutoff.replace(tzinfo=SHANGHAI),
                cutoff.replace(tzinfo=SHANGHAI),
            ]
            query = """SELECT e.event_id,e.announcement_id,e.document_id,e.event_type,e.published_at,e.known_at,e.event_date,e.status,e.direction,e.amount,e.currency,e.ratio,e.extractor,e.extractor_version,e.reviewed_at,e.reviewed_by,e.review_note
                FROM document_event e JOIN published_announcement a USING(announcement_id)
                WHERE e.security_id=? AND e.published_at < ? AND e.known_at < ?
                  AND a.published_at < CAST(? AS TIMESTAMP)"""
            params.append(cutoff)
            order = "published_at DESC,event_id"
        elif section == "research":
            return self.research_in(c, symbol, context, cutoff, **paging)
        else:
            raise ValueError("不支持的数据区域")
        total = c.execute(f"SELECT COUNT(*) FROM ({query}) q", params).fetchone()[0]
        items = records(
            c,
            f"{query} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        )
        if section == "events":
            for item in items:
                item["evidence"], item["evidence_diagnostic"] = self.event_evidence(c, item)
                reviewed = (
                    datetime.fromisoformat(item["reviewed_at"]) if item["reviewed_at"] else None
                )
                item["historically_accepted"] = bool(
                    item["status"] == "accepted"
                    and reviewed
                    and reviewed < cutoff.replace(tzinfo=SHANGHAI)
                )
        return DataExplorer._result(items, total, page, page_size)

    @staticmethod
    def event_evidence(c, event):
        # Bounded evidence. Re-check persisted hashes, page AND chunk text; a
        # mutable accepted flag alone is not proof of a quote's validity.
        rows = records(
            c,
            """SELECT e.field_name,e.document_id,e.page_number,e.chunk_id,
            substring(e.quote,1,2001) AS quote,e.quote_sha256,
            e.document_id=? AND d.announcement_id=? AND d.status='ready'
              AND p.text IS NOT NULL AND contains(p.text,e.quote)
              AND p.text_sha256=sha256(p.text)
              AND k.document_id=e.document_id AND k.page_number=e.page_number
              AND contains(k.text,e.quote) AND k.text_sha256=sha256(k.text) AS valid
            FROM document_event_evidence e
            LEFT JOIN announcement_document d ON d.document_id=e.document_id
            LEFT JOIN document_page p ON p.document_id=e.document_id AND p.page_number=e.page_number
            LEFT JOIN document_chunk k ON k.chunk_id=e.chunk_id
            WHERE e.event_id=? ORDER BY e.evidence_order LIMIT 51""",
            [event["document_id"], event["announcement_id"], event["event_id"]],
        )
        if len(rows) > 50:
            return [], "证据数量超限，不能作为摘要事实"
        required = {"event_type"} | {
            key
            for key in ("event_date", "direction", "amount", "currency", "ratio")
            if event.get(key) not in (None, "", "unknown")
        }
        for row in rows:
            if (
                not row.pop("valid")
                or not row["quote"]
                or len(row["quote"]) > 2000
                or hashlib.sha256(row["quote"].encode()).hexdigest() != row["quote_sha256"]
            ):
                return [], "引用校验失败：页、分块、逐字 quote 或 SHA-256 不一致"
        if not required.issubset({r["field_name"] for r in rows}):
            return [], "字段证据不足"
        return rows, None

    @staticmethod
    def research_query(symbols):
        """Shared set query over frozen research inputs; never infer from live pools."""
        expression = "CASE WHEN json_valid(spec_json) THEN COALESCE(json_extract(spec_json,'$.stock_pool.resolved_symbols'),json_extract(spec_json,'$.symbols'),json_array(json_extract_string(spec_json,'$.symbol'))) ELSE '[]' END"
        eligibility = f"EXISTS (SELECT 1 FROM json_each(?) selected WHERE json_contains({expression},selected.value)) AND COALESCE(json_extract_string(CASE WHEN json_valid(spec_json) THEN spec_json ELSE '{{}}' END,'$.universe_mode'),'explicit')='explicit'"
        query = f"""SELECT run_id AS record_id,research_type,'indexed_run' AS source,run_id,spec_json,result_json,completed_at AS recorded_at,NULL::VARCHAR AS associated_symbol FROM stock_research_run WHERE EXISTS (SELECT 1 FROM json_each(?) selected WHERE json_contains(symbols_json,selected.value))
            UNION ALL SELECT task_id,COALESCE(json_extract_string(CASE WHEN json_valid(spec_json) THEN spec_json ELSE '{{}}' END,'$.research_type'),'factor_backtest'),'operation_task',run_id,spec_json,json_object('status',status),COALESCE(completed_at,created_at),NULL FROM operation_task WHERE task_type='backtest' AND {eligibility} AND (run_id IS NULL OR run_id NOT IN (SELECT run_id FROM stock_research_run))
            UNION ALL SELECT plan_id,tool_name,'copilot_plan',run_id,spec_json,json_object('status',status),COALESCE(completed_at,created_at),NULL FROM research_plan WHERE {eligibility}
            UNION ALL SELECT r.run_id,'event_study','document_event_research',r.run_id,r.spec_json,r.result_json,r.created_at,s.symbol FROM document_event_research_run r JOIN document_event e USING(event_id) JOIN security_master s ON s.security_id=e.security_id WHERE s.symbol IN (SELECT json_extract_string(value,'$') FROM json_each(?))
            UNION ALL SELECT run_id,'screen','screen_run',run_id,spec_json,json_object('status',status),COALESCE(completed_at,created_at),NULL FROM screen_run WHERE {eligibility}"""
        params = [canonical(symbols)] * 5
        return query, params

    @staticmethod
    def research_in(c, symbol, context, cutoff, *, page, page_size):
        query, params = StockResearchWorkbench.research_query([symbol])
        # Existing event spec lacks symbols, so obtain identity from its immutable event FK.
        wrapped = f"SELECT * FROM ({query}) q WHERE recorded_at < ?"
        boundary = (
            cutoff.replace(tzinfo=SHANGHAI)
            if context["mode"] == "historical"
            else datetime.max.replace(tzinfo=UTC)
        )
        total = c.execute(f"SELECT COUNT(*) FROM ({wrapped}) w", [*params, boundary]).fetchone()[0]
        bounded = f"SELECT * EXCLUDE(spec_json,result_json),CASE WHEN length(spec_json)<=100000 THEN spec_json ELSE NULL END AS spec_json,CASE WHEN length(result_json)<=500000 THEN result_json ELSE NULL END AS result_json FROM ({wrapped}) bounded"
        rows = records(
            c,
            f"{bounded} ORDER BY recorded_at DESC,record_id LIMIT ? OFFSET ?",
            [*params, boundary, page_size, (page - 1) * page_size],
        )
        for row in rows:
            # Reject unreasonable legacy payloads, do not load report directories.
            if row["spec_json"] is None or row["result_json"] is None:
                raise ValueError("旧研究记录超出有界读取限制，请通过原研究详情查看")
            row["spec"] = json.loads(row.pop("spec_json"))
            row["result"] = json.loads(row.pop("result_json"))
            row["resolved_symbols"] = frozen_symbols(row["spec"]) or (
                [symbol] if row["source"] == "document_event_research" else []
            )
            row["scope"] = (
                "该股票参与的组合研究；组合指标不是该股票自身表现"
                if len(row["resolved_symbols"]) > 1
                else "单股研究 / 计划（不代表投资建议）"
            )
            row["spec_sha256"] = hashlib.sha256(canonical(row["spec"]).encode()).hexdigest()
        return DataExplorer._result(rows, total, page, page_size)

    def section(self, symbol, section, *, page=1, page_size=20, adjustment="qfq", **options):
        context, cutoff = self.bounds(**options)
        with self.database.connect(read_only=True) as c:
            return self.section_in(
                c,
                symbol,
                section,
                context,
                cutoff,
                page=page,
                page_size=page_size,
                adjustment=adjustment,
            )

    def snapshot(self, symbol, *, adjustment="qfq", **options):
        context, cutoff = self.bounds(**options)
        with self.database.connect(read_only=True) as c:
            c.execute("BEGIN TRANSACTION")
            try:
                result = self.overview_in(c, symbol, context, cutoff)
                result["stock"].pop("task", None)
                result["stock"]["management"].pop("last_task_id", None)
                result["sections"] = {
                    key: self.section_in(
                        c, symbol, key, context, cutoff, page_size=20, adjustment=adjustment
                    )
                    for key in (
                        "prices",
                        "financial_metrics",
                        "announcements",
                        "events",
                        "research",
                    )
                }
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        events = result["sections"]["events"]
        excluded = []
        accepted = []
        for item in events["items"]:
            if (
                item["status"] != "accepted"
                or item["evidence_diagnostic"]
                or (context["mode"] == "historical" and not item["historically_accepted"])
            ):
                excluded.append(
                    {
                        "event_id": item["event_id"],
                        "status": item["status"],
                        "reason": item["evidence_diagnostic"] or "尚未接受或无法证明历史时点已接受",
                    }
                )
            else:
                accepted.append(item)
        events["items"] = accepted
        events["excluded"] = excluded
        result["template_version"] = TEMPLATE_VERSION
        result["adjustment"] = adjustment
        result["evidence_window"] = (
            "每区最多 20 条；pagination.total 为匹配总数，超出部分不在本摘要中。财务/公告保留截至 as_of 已公开的历史版本，不将研究起点当作财务历史起点。"
        )
        if len(canonical(result).encode()) > 2_000_000:
            raise ValueError("摘要证据超出 2MB 上限，请缩小范围")
        return result

    @staticmethod
    def render(snapshot, summary_id, created_at):
        environment = Environment(
            loader=FileSystemLoader(Path(__file__).parents[1] / "reports" / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )
        return environment.get_template("stock_summary.html").render(
            snapshot=snapshot, summary_id=summary_id, created_at=created_at
        )

    @staticmethod
    def audit(c, action, actor, sid, details, outcome="success"):
        c.execute(
            "INSERT INTO operation_audit(event_id,action,actor,outcome,object_type,object_id,details_json) VALUES(?,?,?,?,?,?,?)",
            [
                f"audit-{uuid4().hex}",
                action,
                actor,
                outcome,
                "stock_research_summary",
                sid,
                canonical(details),
            ],
        )

    def generate(self, symbol, *, actor="local", **options):
        try:
            snapshot = self.snapshot(symbol, **options)
            encoded = canonical(snapshot)
            fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
            summary_id = f"summary-{fingerprint}"
            created_at = datetime.now(UTC).isoformat()
            html = self.render(snapshot, summary_id, created_at)  # outside DB lock
            with self.database.connect() as c:
                c.execute("BEGIN TRANSACTION")
                try:
                    existing = c.execute(
                        "SELECT summary_id FROM stock_research_summary WHERE fingerprint=?",
                        [fingerprint],
                    ).fetchone()
                    if not existing:
                        c.execute(
                            "INSERT INTO stock_research_summary VALUES(?,?,?,?,?,?,?,?)",
                            [
                                summary_id,
                                snapshot["stock"]["security_id"],
                                fingerprint,
                                encoded,
                                html,
                                hashlib.sha256(html.encode()).hexdigest(),
                                actor,
                                created_at,
                            ],
                        )
                        self.audit(
                            c,
                            "stock_research_summary_created",
                            actor,
                            summary_id,
                            {
                                "symbol": symbol,
                                "fingerprint": fingerprint,
                                "context": snapshot["context"],
                            },
                        )
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
            return self.get_summary(symbol, summary_id)
        except Exception:
            with self.database.connect() as c:
                self.audit(
                    c,
                    "stock_research_summary_failed",
                    actor,
                    symbol,
                    {"symbol": symbol, "reason": "generation_or_persistence_failed"},
                    "failure",
                )
            raise

    def get_summary(self, symbol, summary_id, *, html=False):
        if not re.fullmatch(r"summary-[0-9a-f]{64}", summary_id):
            raise KeyError(summary_id)
        with self.database.connect(read_only=True) as c:
            stock = self.identity(c, symbol)
            rows = records(
                c,
                "SELECT * FROM stock_research_summary WHERE summary_id=? AND security_id=?",
                [summary_id, stock["security_id"]],
            )
        if not rows:
            raise KeyError(summary_id)
        row = rows[0]
        if (
            hashlib.sha256(row["snapshot_json"].encode()).hexdigest() != row["fingerprint"]
            or hashlib.sha256(row["html_content"].encode()).hexdigest() != row["html_sha256"]
        ):
            raise RuntimeError("摘要校验失败，不允许作为可信产物下载")
        if html:
            return row["html_content"]
        row["snapshot"] = json.loads(row.pop("snapshot_json"))
        row.pop("html_content")
        return row

    def summaries(self, symbol, *, page=1, page_size=20):
        if not 1 <= page <= 10000 or not 1 <= page_size <= 100:
            raise ValueError("分页超限")
        with self.database.connect(read_only=True) as c:
            stock = self.identity(c, symbol)
            params = [stock["security_id"]]
            total = c.execute(
                "SELECT COUNT(*) FROM stock_research_summary WHERE security_id=?", params
            ).fetchone()[0]
            items = records(
                c,
                "SELECT summary_id,fingerprint,created_at,actor,json_extract_string(snapshot_json,'$.context.as_of') AS as_of FROM stock_research_summary WHERE security_id=? ORDER BY created_at DESC,summary_id LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            )
        return DataExplorer._result(items, total, page, page_size)
