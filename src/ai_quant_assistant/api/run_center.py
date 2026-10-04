"""Bounded history projection and explicit deletion of terminal backtests."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from urllib.parse import quote, urlencode
from uuid import uuid4

from pydantic import ValidationError

from ai_quant_assistant.api.run_repository import RUN_ID_RE, SIGNAL_RUN_ID_RE
from ai_quant_assistant.api.schemas import (
    BatchInterimReportEventStudyRequest,
    DailySignalStrategyRequest,
    DocumentEventStudyRequest,
    FactorBacktestRequest,
    FactorLabRequest,
    InterimReportEventStudyRequest,
    PointInTimeScreenRequest,
)
from ai_quant_assistant.storage.research_run_index import frozen_symbols
from ai_quant_assistant.storage.stock_pools import StockPoolService

logger = logging.getLogger(__name__)


class DeletionConflict(Exception):
    """The record must be kept until its task or dependent research is resolved."""


KINDS = {
    "daily_signal": ("日线信号策略", DailySignalStrategyRequest, "/api/v1/daily-signal-strategies"),
    "factor_backtest": ("财务因子回测", FactorBacktestRequest, "/api/v1/backtests"),
    "factor_lab": ("因子实验", FactorLabRequest, "/api/v1/factor-lab"),
    "screen": ("条件选股", PointInTimeScreenRequest, "/api/v1/screens/runs"),
    "event_study": (
        "中报事件研究",
        InterimReportEventStudyRequest,
        "/api/v1/research/interim-report-event-study",
    ),
    "batch_event_study": (
        "批量中报事件研究",
        BatchInterimReportEventStudyRequest,
        "/api/v1/research/batch-interim-report-event-study",
    ),
    "document_event": ("公告事件研究", DocumentEventStudyRequest, None),
}
STATES = {"queued", "running", "succeeded", "failed", "cancelled", "partial", "unknown"}
SOURCES = {"index", "task", "screen", "event", "copilot"}
NOTICE = "仅展示数据库中的持久记录；无索引的旧文件产物仍通过原报告链接访问，不扫描目录。未知字段不按当前股票池补写。"

# A single relation supplies count, stable paging and exact-identity lookups.
# JSON is bounded before it leaves the database; list queries do not read results.
HISTORY = """
WITH raw AS (
 SELECT 'index' AS source, i.run_id AS record_id, research_type AS kind, i.run_id,
        i.spec_json, i.result_json, json_extract_string(TRY_CAST(i.result_json AS JSON),'$.status') AS status,
        t.created_at, t.started_at,
        i.completed_at,COALESCE(t.created_at,i.completed_at) AS sort_at, NULL::VARCHAR AS error,
        NULL::VARCHAR AS event_id, 1 AS priority
 FROM stock_research_run i LEFT JOIN LATERAL (
   SELECT created_at,started_at FROM operation_task
   WHERE run_id=i.run_id AND task_type='backtest'
   AND COALESCE(json_extract_string(TRY_CAST(spec_json AS JSON),'$.research_type'),'factor_backtest')=i.research_type
   ORDER BY created_at,task_id LIMIT 1
 ) t ON TRUE
 UNION ALL
 SELECT 'task',task_id,
        COALESCE(json_extract_string(TRY_CAST(spec_json AS JSON),'$.research_type'),'factor_backtest'),
        run_id,spec_json,COALESCE(result_json,'{}'),status,created_at,started_at,
        completed_at,created_at,error,NULL,4
 FROM operation_task WHERE task_type='backtest'
 UNION ALL
 SELECT 'screen',run_id,'screen',run_id,spec_json,
        json_object('candidate_count',candidate_count,'selected_count',selected_count,
                    'diagnostics',TRY_CAST(diagnostics_json AS JSON)),
        status,created_at,started_at,completed_at,created_at,error,NULL,2
 FROM screen_run
 UNION ALL
 SELECT 'event',run_id,'document_event',run_id,spec_json,result_json,
        json_extract_string(TRY_CAST(result_json AS JSON),'$.status'),created_at,NULL,NULL,created_at,NULL,event_id,2
 FROM document_event_research_run
 UNION ALL
 SELECT 'copilot',p.plan_id,
        CASE p.tool_name
          WHEN 'run_factor_backtest' THEN 'factor_backtest'
          WHEN 'run_factor_lab' THEN 'factor_lab'
          WHEN 'run_interim_report_event_study' THEN 'event_study'
          WHEN 'run_batch_interim_report_event_study' THEN 'batch_event_study'
          WHEN 'run_screen' THEN 'screen'
        END,p.run_id,p.spec_json,COALESCE(p.result_json,'{}'),
        t.status,t.created_at,t.started_at,t.completed_at,t.created_at,t.error,NULL,5
 FROM research_plan p JOIN operation_task t ON t.task_id=p.task_id
 WHERE p.tool_name IN ('run_factor_backtest','run_factor_lab',
     'run_interim_report_event_study','run_batch_interim_report_event_study','run_screen')
), ranked AS (
 SELECT *, ROW_NUMBER() OVER (
   PARTITION BY kind,COALESCE(run_id,source || ':' || record_id)
   ORDER BY priority,record_id) AS duplicate_rank
 FROM raw
), history AS (
 SELECT * EXCLUDE(status),COALESCE(status,'unknown') AS status
 FROM ranked WHERE duplicate_rank=1
)
"""


def decode(text):
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


class RunCenter:
    def __init__(self, database, artifact_dir: Path | None = None):
        self.database = database
        self.artifact_dir = artifact_dir

    def delete_backtest(self, source, record_id, *, expected_run_id, actor, remote_host=None):
        """Delete a terminal backtest identity, its task aliases and its own artifacts.

        A same-filesystem rename removes report/download URLs before the database
        transaction commits. On ordinary errors the directory is restored.
        """
        if source not in {"index", "task"} or not record_id or len(record_id) > 160:
            raise KeyError(record_id)
        if self.artifact_dir is None:
            raise RuntimeError("未配置回测产物目录")
        quarantined = None
        original = None
        with self.database.connect() as c:
            try:
                c.execute("BEGIN TRANSACTION")
                if source == "index":
                    identity = c.execute(
                        "SELECT run_id,research_type,result_json FROM stock_research_run WHERE run_id=?",
                        [record_id],
                    ).fetchone()
                    if not identity or identity[1] not in {"daily_signal", "factor_backtest"}:
                        raise KeyError(record_id)
                    run_id, kind = identity[:2]
                    if decode(identity[2]).get("status") not in {
                        "succeeded",
                        "failed",
                        "cancelled",
                    }:
                        raise DeletionConflict("运行尚未结束，不能删除")
                else:
                    identity = c.execute(
                        "SELECT run_id,status,spec_json FROM operation_task WHERE task_id=? AND task_type='backtest'",
                        [record_id],
                    ).fetchone()
                    if not identity:
                        raise KeyError(record_id)
                    run_id = identity[0]
                    kind = decode(identity[2]).get("research_type", "factor_backtest")
                    if kind not in {"daily_signal", "factor_backtest"}:
                        raise KeyError(record_id)
                    if identity[1] not in {"succeeded", "failed", "cancelled"}:
                        raise DeletionConflict("任务仍在排队或运行中，请先等待完成或取消排队任务")
                if expected_run_id != (run_id or record_id):
                    raise DeletionConflict("运行身份已变化，请刷新历史记录后重新核对")
                if run_id:
                    pattern = SIGNAL_RUN_ID_RE if kind == "daily_signal" else RUN_ID_RE
                    if not pattern.fullmatch(run_id):
                        raise DeletionConflict("运行 ID 格式不符合安全删除规则，请联系管理员核对")
                    tasks = c.execute(
                        "SELECT task_id,status,spec_json FROM operation_task WHERE task_type='backtest' AND run_id=?",
                        [run_id],
                    ).fetchall()
                    if any(
                        status not in {"succeeded", "failed", "cancelled"} for _, status, _ in tasks
                    ):
                        raise DeletionConflict("该运行仍有关联任务在执行，不能删除")
                    if any(
                        decode(spec).get("research_type", "factor_backtest") != kind
                        for _, _, spec in tasks
                    ):
                        raise DeletionConflict("该运行 ID 还关联其他类型任务，拒绝删除")
                    plan_count = c.execute(
                        """SELECT COUNT(*) FROM research_plan WHERE run_id=?
                        OR task_id IN (SELECT task_id FROM operation_task
                                       WHERE task_type='backtest' AND run_id=?)""",
                        [run_id, run_id],
                    ).fetchone()[0]
                    if plan_count:
                        raise DeletionConflict("该运行关联研究计划，请先在研究计划入口处理")
                    root = (
                        self.artifact_dir
                        / ("daily-signals" if kind == "daily_signal" else "backtests")
                    ).resolve()
                    original = root / run_id
                    if original.is_symlink():
                        raise DeletionConflict("运行目录是符号链接，拒绝删除")
                    if original.exists():
                        if not original.is_dir() or original.resolve().parent != root:
                            raise DeletionConflict("运行目录不安全，拒绝删除")
                        quarantined = root / f".deleted-{uuid4().hex}"
                        os.replace(original, quarantined)
                    c.execute(
                        "DELETE FROM stock_research_run WHERE run_id=? AND research_type=?",
                        [run_id, kind],
                    )
                    c.execute(
                        "DELETE FROM operation_task WHERE task_type='backtest' AND run_id=?",
                        [run_id],
                    )
                else:
                    plan_count = c.execute(
                        "SELECT COUNT(*) FROM research_plan WHERE task_id=?", [record_id]
                    ).fetchone()[0]
                    if plan_count:
                        raise DeletionConflict("该任务关联研究计划，请先在研究计划入口处理")
                    c.execute(
                        "DELETE FROM operation_task WHERE task_id=? AND task_type='backtest'",
                        [record_id],
                    )
                if source == "task":
                    c.execute(
                        "DELETE FROM operation_task WHERE task_id=? AND task_type='backtest'",
                        [record_id],
                    )
                audit_id = f"audit-{uuid4().hex}"
                c.execute(
                    """INSERT INTO operation_audit
                    (event_id,action,actor,outcome,remote_host,object_type,object_id,details_json)
                    VALUES (?, 'backtest_deleted', ?, 'success', ?, 'backtest', ?, ?)""",
                    [
                        audit_id,
                        actor,
                        remote_host,
                        run_id or record_id,
                        json.dumps(
                            {
                                "source": source,
                                "record_id": record_id,
                                "kind": kind,
                                "run_id": run_id,
                                "artifact_removed": quarantined is not None,
                            },
                            ensure_ascii=False,
                        ),
                    ],
                )
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                if quarantined is not None and original is not None:
                    os.replace(quarantined, original)
                raise
        if quarantined is not None:
            try:
                shutil.rmtree(quarantined)
            except OSError:
                logger.exception(
                    "Backtest deletion committed; quarantined files require cleanup: %s",
                    quarantined,
                )
        return {"deleted": True, "run_id": run_id or record_id, "audit_id": audit_id}

    def _rows(self, connection, where, params, page, size, detail=False):
        result = "CASE WHEN length(result_json)<=500000 THEN result_json END" if detail else "NULL"
        cursor = connection.execute(
            HISTORY
            + f"""SELECT source,record_id,kind,run_id,status,created_at,
            started_at,completed_at,error,event_id,
            CASE WHEN length(spec_json)<=100000 THEN spec_json END AS spec_json,
            {result} AS result_json FROM history {where}
            ORDER BY sort_at DESC NULLS LAST,kind,source,record_id LIMIT ? OFFSET ?""",
            [*params, size, (page - 1) * size],
        )
        columns = [x[0] for x in cursor.description]
        return [
            self._decode(dict(zip(columns, row, strict=True)), detail) for row in cursor.fetchall()
        ]

    def list(self, *, kind=None, status=None, page=1, page_size=20):
        if kind is not None and kind not in KINDS:
            raise ValueError("未知研究类型")
        if status is not None and status not in STATES:
            raise ValueError("未知运行状态")
        if not 1 <= page <= 10000 or not 1 <= page_size <= 100:
            raise ValueError("分页超出范围")
        conditions, params = [], []
        for key, value in (("kind", kind), ("status", status)):
            if value:
                conditions.append(f"{key}=?")
                params.append(value)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        with self.database.connect(read_only=True) as c:
            c.execute("BEGIN TRANSACTION")
            total = c.execute(HISTORY + f"SELECT COUNT(*) FROM history {where}", params).fetchone()[
                0
            ]
            items = self._rows(c, where, params, page, page_size)
            c.execute("COMMIT")
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "notice": NOTICE,
        }

    def get(self, source, record_id):
        if source not in SOURCES or not record_id or len(record_id) > 160:
            raise KeyError(record_id)
        with self.database.connect(read_only=True) as c:
            # Read raw identity as well: a task's old URL survives index publication.
            cursor = c.execute(
                HISTORY
                + """SELECT source,record_id,kind,run_id,COALESCE(status,'unknown') AS status,
                created_at,started_at,completed_at,error,event_id,
                CASE WHEN length(spec_json)<=100000 THEN spec_json END AS spec_json,
                CASE WHEN length(result_json)<=500000 THEN result_json END AS result_json
                FROM raw WHERE source=? AND record_id=?""",
                [source, record_id],
            )
            columns = [x[0] for x in cursor.description]
            row = cursor.fetchone()
        if row is None:
            raise KeyError(record_id)
        return self._decode(dict(zip(columns, row, strict=True)), True)

    @staticmethod
    def _decode(row, detail):
        spec_text = row.pop("spec_json")
        result_text = row.pop("result_json")
        spec = decode(spec_text)
        row["label"] = KINDS.get(row["kind"], ("未知类型",))[0]
        row["symbols"] = (
            frozen_symbols(spec) if spec.get("universe_mode", "explicit") == "explicit" else []
        )
        row["pool"] = spec.get("stock_pool") if isinstance(spec.get("stock_pool"), dict) else None
        row["start_date"] = spec.get("start_date") or spec.get("as_of")
        row["end_date"] = spec.get("end_date") or spec.get("as_of")
        row["duration_seconds"] = None
        if row["started_at"] and row["completed_at"]:
            duration = (row["completed_at"] - row["started_at"]).total_seconds()
            row["duration_seconds"] = duration if duration >= 0 else None
        for key in ("created_at", "started_at", "completed_at"):
            row[key] = row[key].isoformat() if row[key] else None
        query = urlencode({"source": row["source"], "record": row["record_id"]})
        row["detail_url"] = "/backtests/detail?" + query
        row["copy_url"] = "/backtests/configure?" + query
        row["report_url"] = None
        if row["run_id"] and row["kind"] in {"factor_backtest", "factor_lab", "daily_signal"}:
            segment = {
                "factor_backtest": "backtests",
                "factor_lab": "factor-lab",
                "daily_signal": "daily-signals",
            }[row["kind"]]
            row["report_url"] = f"/reports/{segment}/{quote(row['run_id'], safe='')}"
        if detail:
            row["spec"] = spec
            row["result"] = decode(result_text)
            row["payload_unavailable"] = spec_text is None or result_text is None
        return row

    def copy_parameters(self, source, record_id):
        row = self.get(source, record_id)
        kind = row["kind"]
        if kind not in KINDS:
            raise ValueError("该类型暂不支持复制")
        _, model, endpoint = KINDS[kind]
        spec = row["spec"]
        warnings = []
        if row["payload_unavailable"] or not spec:
            raise ValueError("原参数缺失或超过读取限制，不能用默认值替代")
        if spec.get("universe_mode", "explicit") != "explicit":
            raise ValueError(
                "该运行使用动态范围，无法恢复完整历史成员；请在原配置入口明确重新选择范围"
            )
        allowed = model.model_fields
        if kind == "document_event":
            spec = {**spec, "as_of": row["result"].get("as_of")}
            if not spec["as_of"]:
                raise ValueError("原事件研究的截至时点缺失，不能替换成现在")
        if (
            spec.get("entry_rule", "next_common_trading_day_close")
            != "next_common_trading_day_close"
        ):
            raise ValueError("原事件成交规则与当前接口不兼容")
        metadata = {"stock_pool", "research_type", "entry_rule", "preflight_snapshot"}
        if kind == "factor_backtest":
            if spec.get("factors") or spec.get("missing_policy", "exclude") != "exclude":
                raise ValueError("该回测使用组合因子或特殊缺失策略，请从原因子实验配置入口重建")
            metadata |= {"factors", "missing_policy"}
        if kind == "document_event":
            metadata |= {"symbol", "start_date", "end_date", "min_events"}
        unexpected = set(spec) - set(allowed) - metadata
        if unexpected:
            raise ValueError("存在无法映射的旧参数：" + ", ".join(sorted(unexpected)))
        params = {k: v for k, v in spec.items() if k in allowed}
        pool = row["pool"]
        if pool and pool.get("pool_id"):
            pool_id = pool.get("pool_id")
            revision = pool.get("pool_revision") or pool.get("revision") or pool.get("revision_no")
            if not pool_id or not revision or not row["symbols"]:
                raise ValueError("原股票池身份或冻结范围不完整")
            with self.database.connect(read_only=True) as c:
                original = StockPoolService(self.database)._get(c, pool_id, revision=revision)
            if original["symbols"] != row["symbols"]:
                raise ValueError("原 revision 与保存的冻结股票范围不一致")
            params.pop("symbols", None)
            params.update(stock_pool_id=pool_id, stock_pool_revision=revision)
        elif "symbols" in allowed:
            if not row["symbols"]:
                raise ValueError("原冻结股票范围缺失，不能使用当前默认股票池")
            params["symbols"] = row["symbols"]
        elif "symbol" in allowed and not spec.get("symbol"):
            raise ValueError("原股票代码未记录")
        if kind == "document_event":
            endpoint = f"/api/v1/document-events/{quote(row['event_id'], safe='')}/event-study"
        missing = []
        for key in allowed:
            if key in {"stock_pool_id", "stock_pool_revision", "symbols"}:
                continue
            if key not in params:
                missing.append(key)
        # A saved event spec does not record the provider. Require a deliberate
        # user selection instead of silently replaying against a default source.
        if missing:
            warnings.append("原记录未保存以下参数，必须检查并明确确认补充值：" + ", ".join(missing))
        try:
            validated = model.model_validate(params).model_dump(mode="json")
        except ValidationError as exc:
            raise ValueError("原参数与当前接口不兼容：" + str(exc)) from exc
        fields = model.model_json_schema()
        return {
            "kind": kind,
            "label": row["label"],
            "source_run_id": row["run_id"] or record_id,
            "params": validated,
            "schema": fields,
            "endpoint": endpoint,
            "warnings": warnings,
            "missing_fields": missing,
            "scope": row["symbols"],
            "pool": pool,
        }
