"""Confirmed, immutable research-data backfill plans; existing sync engines only."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from threading import Event, Thread
from uuid import uuid4

from ai_quant_assistant.config import Settings
from ai_quant_assistant.domain.models import (
    AnnouncementSyncSpec,
    BenchmarkSyncSpec,
    DataSyncSpec,
    FinancialSyncSpec,
)
from ai_quant_assistant.research.readiness import ResearchReadiness, digest, encoded
from ai_quant_assistant.storage.financial_sync_service import FinancialDataSyncService
from ai_quant_assistant.storage.locks import InterProcessFileLock
from ai_quant_assistant.storage.managed_coverage import completed_date
from ai_quant_assistant.storage.managed_stocks import (
    ManagedStockService,
    _FencedDatabase,
    _FreshProvider,
)
from ai_quant_assistant.storage.research_sync_service import ResearchDatasetSyncService
from ai_quant_assistant.storage.sync_service import MarketDataSyncService
from ai_quant_assistant.storage.task_repository import OperationTaskRepository


class DataPlanConflict(RuntimeError):
    pass


class ResearchDataPlans:
    dataset = "research_data_plan"

    def __init__(self, database, settings: Settings | None = None, provider_factory=None):
        self.database = database
        self.settings = settings or Settings(data_dir=database.layout.root)
        self.tasks = OperationTaskRepository(database)
        self.readiness = ResearchReadiness(database)
        self.provider_factory = (
            provider_factory or ManagedStockService(database, self.settings).provider_factory
        )

    @contextmanager
    def transaction(self):
        with self.database.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                yield connection
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def audit(connection, action, plan, actor, **details):
        connection.execute(
            "INSERT INTO operation_audit(event_id,action,actor,outcome,object_type,object_id,details_json) "
            "VALUES (?,?,?,?,'research_data_plan',?,?)",
            [
                f"audit-{uuid4().hex}",
                f"research_data_plan_{action}",
                actor,
                "failure" if action == "failed" else "success",
                plan["plan_id"],
                encoded(
                    {
                        "version": plan["version"],
                        "symbols_sha256": plan["input"]["symbols_sha256"],
                        "datasets": sorted({item["dataset"] for item in plan["input"]["items"]}),
                        **details,
                    }
                ),
            ],
        )

    @staticmethod
    def _get(connection, plan_id):
        cursor = connection.execute("SELECT * FROM research_data_plan WHERE plan_id=?", [plan_id])
        row = cursor.fetchone()
        if row is None:
            raise KeyError(plan_id)
        plan = dict(zip([column[0] for column in cursor.description], row, strict=True))
        for field in ("input", "items", "report"):
            plan[field] = json.loads(plan.pop(f"{field}_json"))
        for field in ("created_at", "updated_at", "confirmed_at"):
            plan[field] = str(plan[field]) if plan[field] else None
        return plan

    def get(self, plan_id):
        with self.database.connect(read_only=True) as connection:
            plan = self._get(connection, plan_id)
        plan["task"] = self.tasks.get(plan["last_task_id"]) if plan["last_task_id"] else None
        plan["network_notice"] = (
            "确认后访问公网，仅行情/基准/必要公告元数据/财务；不下载 PDF，不运行研究。"
        )
        return plan

    def list(self, *, limit=50):
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT plan_id,status,version,created_at,last_task_id FROM research_data_plan "
                "ORDER BY created_at DESC LIMIT ?",
                [max(1, min(limit, 100))],
            ).fetchall()
        return [
            dict(
                zip(
                    ("plan_id", "status", "version", "created_at", "last_task_id"), row, strict=True
                )
            )
            for row in rows
        ]

    def create(self, research_type, parameters, *, actor="system"):
        frozen = self.readiness.freeze(research_type, parameters)
        return self.create_frozen(frozen, actor=actor)

    def create_frozen(self, frozen, *, actor="system"):
        if len(frozen["resolved_symbols"]) > 20:
            raise ValueError("单次补齐最多 20 只股票，请明确分批；不会自动提交后续批次")
        report = self.readiness.check(frozen)
        items = {}
        for check in report["checks"]:
            if check["satisfied"] or not check.get("can_backfill"):
                continue
            dataset = check["dataset"]
            if dataset not in {"daily_bar", "benchmark_daily_bar", "financial_metric"}:
                continue
            item = {
                key: check.get(key) for key in ("symbol", "security_id", "dataset", "adjustment")
            }
            item.update(start_date=check["required_start"], end_date=check["required_end"])
            if dataset in {"daily_bar", "benchmark_daily_bar"}:
                item["end_date"] = str(completed_date(date.fromisoformat(item["end_date"])))
            if item["start_date"] > item["end_date"]:
                continue
            item_id = digest(item)[:24]
            item.update(
                item_id=item_id,
                reason=check["reason"],
                estimated_partitions=date.fromisoformat(item["end_date"]).year
                - date.fromisoformat(item["start_date"]).year
                + 1,
            )
            if dataset == "daily_bar" and item["adjustment"] != "none":
                item["estimated_partitions"] *= 2
                item["reason"] += "；沿用复权引擎同时下载 none，用于复权一致性校验"
            items[item_id] = item
            if dataset == "financial_metric":
                dependency = {
                    **item,
                    "dataset": "announcement",
                    "adjustment": None,
                    "reason": "核验财务公开时间所需的四类定期公告元数据；不含 PDF",
                }
                dependency["estimated_partitions"] *= 4
                dependency["item_id"] = digest(
                    {
                        key: dependency[key]
                        for key in ("symbol", "dataset", "start_date", "end_date")
                    }
                )[:24]
                items[dependency["item_id"]] = dependency
        # DataSyncSpec automatically includes none for adjusted prices. Represent
        # that dependency once, with its work estimate and explicit notice.
        for item_id, item in list(items.items()):
            if item["dataset"] != "daily_bar" or item["adjustment"] != "none":
                continue
            if any(
                other["dataset"] == "daily_bar"
                and other["adjustment"] != "none"
                and all(other[key] == item[key] for key in ("symbol", "start_date", "end_date"))
                for other in items.values()
            ):
                del items[item_id]
        ordered = sorted(
            items.values(),
            key=lambda item: (
                {
                    "daily_bar": 0,
                    "benchmark_daily_bar": 1,
                    "announcement": 2,
                    "financial_metric": 3,
                }[item["dataset"]],
                item["symbol"],
            ),
        )
        plan_id = f"data-plan-{uuid4().hex}"
        with self.transaction() as connection:
            managed = {}
            for symbol in frozen["resolved_symbols"]:
                row = connection.execute(
                    "SELECT m.security_id,m.status,m.generation FROM managed_stock m JOIN security_master s USING(security_id) WHERE s.symbol=?",
                    [symbol],
                ).fetchone()
                if row:
                    managed[symbol] = {
                        "security_id": row[0],
                        "status": row[1],
                        "generation": row[2],
                    }
            frozen = {**frozen, "items": ordered, "management": managed}
            connection.execute(
                "INSERT INTO research_data_plan(plan_id,input_sha256,input_json,items_json,report_json,actor) VALUES (?,?,?,?,?,?)",
                [
                    plan_id,
                    digest(frozen),
                    encoded(frozen),
                    encoded([{**item, "status": "pending", "attempts": []} for item in ordered]),
                    encoded(report),
                    actor,
                ],
            )
            self.audit(connection, "created", self._get(connection, plan_id), actor)
        return self.get(plan_id)

    @staticmethod
    def _management(connection, frozen):
        for symbol in frozen.get("resolved_symbols", []):
            if symbol not in frozen["management"]:
                raise DataPlanConflict(
                    f"{symbol}: not an active managed stock; enroll in data explorer first"
                )
        for symbol, item in frozen["management"].items():
            row = connection.execute(
                "SELECT status,generation FROM managed_stock WHERE security_id=?",
                [item["security_id"]],
            ).fetchone()
            if not row or row[0] != "active" or row[1] != item["generation"]:
                raise DataPlanConflict(
                    f"{symbol}: management state/generation changed; create and confirm a new plan"
                )
        for item in frozen["items"]:
            if (
                item["dataset"] != "benchmark_daily_bar"
                and item["symbol"] not in frozen["management"]
            ):
                raise DataPlanConflict(f"{item['symbol']}: not an active managed stock")

    def confirm(self, plan_id, *, version, input_sha256, confirm, actor="system", retry=False):
        if confirm is not True:
            raise ValueError("explicit confirmation required")
        with self.transaction() as connection:
            plan = self._get(connection, plan_id)
            if (
                version != plan["version"]
                or input_sha256 != plan["input_sha256"]
                or digest(plan["input"]) != input_sha256
            ):
                raise DataPlanConflict("计划版本或摘要不符，请重新查看计划")
            if not plan["input"]["items"]:
                raise DataPlanConflict("没有可自动补齐项目，请查看预检诊断")
            if len(plan["input"]["resolved_symbols"]) > 20:
                raise ValueError("backfill limit is 20 stocks")
            self._management(connection, plan["input"])
            if plan["status"] == "failed" and not retry:
                raise DataPlanConflict("失败计划请明确确认重试")
            task, created = self.tasks.create_or_get_active(
                "sync",
                claim_key=f"data-plan:{plan_id}:{version}",
                spec={
                    "dataset": self.dataset,
                    "plan_id": plan_id,
                    "version": version,
                    "input_sha256": input_sha256,
                    "management": plan["input"]["management"],
                    "actor": actor,
                },
                message="等待已确认研究数据补齐",
                connection=connection,
            )
            if created:
                connection.execute(
                    "UPDATE research_data_plan SET status='confirmed',confirmed_by=?,confirmed_at=CURRENT_TIMESTAMP,"
                    "last_task_id=?,updated_at=CURRENT_TIMESTAMP WHERE plan_id=?",
                    [actor, task["task_id"], plan_id],
                )
                self.audit(
                    connection,
                    "retried" if retry else "confirmed",
                    plan,
                    actor,
                    task_id=task["task_id"],
                )
        return task

    def recheck(self, plan_id):
        plan = self.get(plan_id)
        report = self.readiness.check(plan["input"])
        # Viewing/checking is read-only, and never changes an execution's result.
        return report

    @staticmethod
    def fence(connection, task_id, owner, spec):
        row = connection.execute(
            "SELECT 1 FROM operation_task t JOIN research_data_plan p ON p.last_task_id=t.task_id "
            "WHERE t.task_id=? AND t.status='running' AND t.owner_id=? AND t.heartbeat_at>? "
            "AND p.plan_id=? AND p.version=? AND p.input_sha256=?",
            [
                task_id,
                owner,
                datetime.now(UTC) - timedelta(minutes=30),
                spec["plan_id"],
                spec["version"],
                spec["input_sha256"],
            ],
        ).fetchone()
        if not row:
            raise DataPlanConflict("worker lost task owner/lease/plan version")
        ResearchDataPlans._management(connection, {"management": spec["management"], "items": []})

    def run(self, task_id):
        # Shared with managed_stock: conservative one-stock network/memory slot.
        # No operation lock: backups remain possible between partition writes.
        slot = InterProcessFileLock(
            self.database.layout.root / ".managed-download.lock",
            description="managed/backfill download slot",
        )
        with slot.hold(timeout_seconds=0):
            return self._run_owned(task_id)

    def _run_owned(self, task_id):
        task = self.tasks.get(task_id)
        spec = task["spec"]
        if spec.get("dataset") != self.dataset:
            raise ValueError("not a research data plan task")
        owner = f"data-worker-{uuid4().hex}"
        if not self.tasks.start(task_id, owner_id=owner, message="正在重新检查已确认范围"):
            return self.tasks.get(task_id)
        guarded = _FencedDatabase(self, task_id, owner, spec)
        stop = Event()

        def heartbeat():
            while not stop.wait(15):
                try:
                    with guarded.connect() as connection:
                        connection.execute(
                            "UPDATE operation_task SET heartbeat_at=CURRENT_TIMESTAMP WHERE task_id=?",
                            [task_id],
                        )
                except Exception:
                    return

        thread = Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            plan = self.get(spec["plan_id"])
            with self.transaction() as connection:
                self.fence(connection, task_id, owner, spec)
                self.audit(connection, "started", plan, spec["actor"], task_id=task_id)
            items = plan["items"]
            errors = []
            for index, item in enumerate(items):
                with guarded.connect(read_only=True):
                    pass
                current = self.readiness.check(
                    {
                        **plan["input"],
                        "resolved_symbols": []
                        if item["dataset"] == "benchmark_daily_bar"
                        else [item["symbol"]],
                    }
                )
                relevant = [
                    check
                    for check in current["checks"]
                    if check.get("symbol") == item["symbol"]
                    and check["dataset"]
                    == (
                        "financial_metric" if item["dataset"] == "announcement" else item["dataset"]
                    )
                    and (
                        item["dataset"] != "daily_bar"
                        or check.get("adjustment") in {"none", item["adjustment"]}
                    )
                ]
                if relevant and all(check["satisfied"] for check in relevant):
                    item["status"] = "verified"
                    item["note"] = "重新预检已满足，无需重复下载"
                elif item["status"] == "published" and item["dataset"] == "announcement":
                    item["note"] = "保留已完成公告依赖；如上游修订需创建新计划明确刷新"
                else:
                    if item["attempts"]:
                        item["note"] = (
                            "预检仍有缺失或上次失败，按已确认范围重试；保留历史发布版本与原始证据"
                        )
                    try:
                        result = self._download(guarded, task_id, owner, item)
                        item["attempts"] = [
                            *item["attempts"],
                            {"task_id": task_id, "result": result},
                        ][-10:]
                        item["status"] = (
                            "published" if result["status"] == "published" else "failed"
                        )
                        if item["status"] == "failed":
                            errors.append(
                                f"{item['symbol']} {item['dataset']}: {encoded(result.get('errors', []))}"
                            )
                    except Exception as exc:
                        item["status"] = "failed"
                        item["attempts"] = [
                            *item["attempts"],
                            {"task_id": task_id, "error": str(exc)[:2000]},
                        ][-10:]
                        errors.append(str(exc)[:2000])
                with self.transaction() as connection:
                    self.fence(connection, task_id, owner, spec)
                    connection.execute(
                        "UPDATE research_data_plan SET items_json=?,updated_at=CURRENT_TIMESTAMP WHERE plan_id=?",
                        [encoded(items), plan["plan_id"]],
                    )
                    connection.execute(
                        "UPDATE operation_task SET progress=?,message=?,heartbeat_at=CURRENT_TIMESTAMP WHERE task_id=?",
                        [
                            min(95, 5 + int(90 * (index + 1) / len(items))),
                            f"补齐项 {index + 1}/{len(items)}",
                            task_id,
                        ],
                    )
            report = self.readiness.check(plan["input"])
            success = not errors and report["complete"]
            error = (
                None
                if success
                else (
                    "；".join(errors)
                    or "补齐已执行，但研究数据仍有缺口；请查看逐股票诊断，不自动运行研究"
                )
            )
            with self.transaction() as connection:
                self.fence(connection, task_id, owner, spec)
                connection.execute(
                    "UPDATE research_data_plan SET status=?,report_json=?,updated_at=CURRENT_TIMESTAMP WHERE plan_id=?",
                    ["succeeded" if success else "failed", encoded(report), plan["plan_id"]],
                )
                connection.execute(
                    "UPDATE operation_task SET status=?,progress=?,message=?,result_json=?,error=?,"
                    "completed_at=CURRENT_TIMESTAMP,heartbeat_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,"
                    "claim_key=CASE WHEN ? THEN claim_key ELSE NULL END WHERE task_id=?",
                    [
                        "succeeded" if success else "failed",
                        100 if success else 95,
                        "补齐后预检完成；研究仍需单独操作"
                        if success
                        else "部分补齐或失败，请查看计划诊断",
                        encoded({"plan_id": plan["plan_id"], "report": report})
                        if success
                        else None,
                        error[:2000] if error else None,
                        success,
                        task_id,
                    ],
                )
                self.audit(
                    connection,
                    "completed" if success else "failed",
                    plan,
                    spec["actor"],
                    task_id=task_id,
                )
        except Exception as exc:
            # CAS cannot overwrite a reclaimed owner. Do not write plan results
            # after generation invalidation; prior completed items remain intact.
            try:
                with self.transaction() as connection:
                    owned = connection.execute(
                        "SELECT 1 FROM operation_task t JOIN research_data_plan p ON p.last_task_id=t.task_id "
                        "WHERE t.task_id=? AND t.owner_id=? AND t.status='running' AND p.version=?",
                        [task_id, owner, spec["version"]],
                    ).fetchone()
                    if owned:
                        plan = self._get(connection, spec["plan_id"])
                        connection.execute(
                            "UPDATE operation_task SET status='failed',error=?,message='补齐中断，可安全重试',"
                            "progress=LEAST(progress,99),result_json=NULL,claim_key=NULL,"
                            "completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                            [str(exc)[:2000], task_id],
                        )
                        connection.execute(
                            "UPDATE research_data_plan SET status='failed',updated_at=CURRENT_TIMESTAMP WHERE plan_id=?",
                            [plan["plan_id"]],
                        )
                        self.audit(connection, "failed", plan, spec["actor"], task_id=task_id)
            except Exception:
                self.tasks.fail(
                    task_id,
                    owner_id=owner,
                    message="提交补齐结果失败，事务已回滚",
                    error=str(exc)[:2000],
                )
        finally:
            stop.set()
            thread.join(timeout=1)
        return self.tasks.get(task_id)

    def _download(self, guarded, task_id, owner, item):
        provider = _PlanProvider(
            self.provider_factory(f"{task_id}-{owner}-{item['item_id']}"), guarded
        )
        common = dict(
            database=guarded,
            layout=self.database.layout,
            provider=provider,
            rate_limit_seconds=max(0.2, self.settings.data_sync_rate_limit_seconds),
            raw_namespace=f"{task_id}-{owner}-{item['item_id']}",
        )
        dates = dict(
            start_date=date.fromisoformat(item["start_date"]),
            end_date=date.fromisoformat(item["end_date"]),
            symbols=(item["symbol"],),
            resume=False,
        )
        if item["dataset"] == "financial_metric":
            result = FinancialDataSyncService(**common, require_provider_publication=True).run(
                FinancialSyncSpec(**dates)
            )
        elif item["dataset"] == "announcement":
            result = ResearchDatasetSyncService(**common).sync_announcements(
                AnnouncementSyncSpec(**dates, report_types=("q1", "interim", "q3", "annual"))
            )
        elif item["dataset"] == "benchmark_daily_bar":
            result = ResearchDatasetSyncService(**common).sync_benchmarks(
                BenchmarkSyncSpec(**dates)
            )
        else:
            common["provider"] = _FreshProvider(provider, guarded, item["security_id"])
            result = MarketDataSyncService(**common).run(
                DataSyncSpec(**dates, adjustments=(item["adjustment"],))
            )
        return {
            key: value
            for key, value in result.items()
            if key in {"status", "batch_id", "row_count", "metric_count", "partitions", "errors"}
        }


class _PlanProvider:
    def __init__(self, provider, database):
        self.provider, self.database, self.name = provider, database, provider.name

    def __getattr__(self, name):
        function = getattr(self.provider, name)
        if not callable(function):
            return function

        def call(*args, **kwargs):
            with self.database.connect(read_only=True):
                pass
            result = function(*args, **kwargs)
            if getattr(result, "attrs", {}).get("cache_status") == "stale_fallback":
                raise ValueError(f"{name}: stale cache after refresh failure")
            return result

        return call
