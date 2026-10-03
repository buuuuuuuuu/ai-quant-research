"""Confirmed, immutable research-data backfill plans; existing sync engines only."""

from __future__ import annotations

import hashlib
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
from ai_quant_assistant.storage.managed_coverage import (
    classified_daily_bar_gaps,
    completed_date,
)
from ai_quant_assistant.storage.managed_stocks import (
    ManagedStockService,
    _FencedDatabase,
    _FreshProvider,
)
from ai_quant_assistant.storage.research_sync_service import ResearchDatasetSyncService
from ai_quant_assistant.storage.sync_service import MarketDataSyncService
from ai_quant_assistant.storage.task_repository import TASK_COLUMNS, OperationTaskRepository


class DataPlanConflict(RuntimeError):
    pass


class ResearchDataPlans:
    dataset = "research_data_plan"
    retryable_item_statuses = frozenset({"pending", "planned", "fetching", "failed"})
    completed_item_statuses = frozenset({"published", "verified"})

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
            "确认补齐后访问公网同步行情/基准/必要公告元数据/财务；"
            "确认缺口核验后仅查询停牌或交易状态证据。两种操作都不下载 PDF，"
            "也不运行研究。"
        )
        decorated = self._decorate(plan)
        decorated["gap_verification"] = self._gap_verification_state(plan)
        if not decorated.get("items") and plan.get("input", {}).get(
            "research_type"
        ) == "daily_signal":
            gap = decorated["gap_verification"]
            current = gap.get("current_readiness") or {}
            if current.get("ready") is True:
                decorated["outcome"] = "ready"
            elif gap.get("unverified_gap"):
                decorated["outcome"] = "verification_required"
            elif gap.get("missing_bar") or gap.get("calendar_mismatch"):
                decorated["outcome"] = "blocked_unresolved"
        decorated["available_actions"] = {
            "sync_required": bool(
                plan.get("status") == "draft" and decorated.get("items")
            ),
            "verification_required": bool(
                decorated["gap_verification"].get("requires_confirmation")
            ),
            "calendar_attention": bool(
                decorated["gap_verification"].get("calendar_mismatch")
            ),
            "ready": decorated.get("outcome") == "ready",
        }
        return decorated

    @staticmethod
    def _published_price_range(
        connection, security_id: str, adjustment: str, start: date, end: date
    ) -> bool:
        """Prove that successful sync partitions cover a requested range.

        Missing rows alone are not proof of suspension or proof that a provider
        was queried.  This method only permits the evidence-verification path
        after persisted, published partition intervals cover the whole range.
        """
        rows = connection.execute(
            "SELECT start_date,end_date FROM sync_partition "
            "WHERE security_id=? AND adjustment=? AND status='published' "
            "AND end_date>=? AND start_date<=? ORDER BY start_date,end_date",
            [security_id, adjustment, start, end],
        ).fetchall()
        cursor = start
        for range_start, range_end in rows:
            if range_end < cursor:
                continue
            if range_start > cursor:
                return False
            cursor = max(cursor, range_end + timedelta(days=1))
            if cursor > end:
                return True
        return cursor > end

    def _linked_gap_tasks(self, plan_id: str) -> list[dict[str, object]]:
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                f"SELECT {', '.join(TASK_COLUMNS)} FROM operation_task "
                "WHERE json_extract_string(spec_json, '$.dataset')="
                "'managed_stock_gap_verification' "
                "AND json_extract_string(spec_json, '$.source_plan_id')=? "
                "ORDER BY created_at DESC LIMIT 100",
                [plan_id],
            ).fetchall()
        return [OperationTaskRepository._decode(row) for row in rows]

    def _gap_verification_state(self, plan: dict[str, object]) -> dict[str, object]:
        empty = {
            "status": "not_available",
            "total_dates": 0,
            "verified_suspension": 0,
            "missing_bar": 0,
            "calendar_mismatch": 0,
            "unverified_gap": 0,
            "requires_confirmation": False,
            "targets": [],
            "tasks": [],
        }
        if plan.get("input", {}).get("research_type") != "daily_signal":
            return empty
        sync_complete = plan.get("status") == "succeeded" or not plan.get("items")
        if not sync_complete:
            return empty
        report = self.readiness.check(plan["input"])
        price_checks = [
            check
            for check in report.get("checks", [])
            if check.get("dataset") == "daily_bar" and check.get("security_id")
        ]
        if not price_checks:
            return empty
        grouped: dict[str, dict[str, object]] = {}
        classified_cache: dict[tuple[str, str, str], dict[str, list[dict[str, object]]]] = {}
        with self.database.connect(read_only=True) as connection:
            for check in price_checks:
                security_id = check["security_id"]
                target = grouped.get(security_id)
                if target is None:
                    asset = connection.execute(
                        "SELECT revision,generation,status FROM managed_stock WHERE security_id=?",
                        [security_id],
                    ).fetchone()
                    if not asset or asset[2] != "active":
                        continue
                    target = {
                        "security_id": security_id,
                        "symbol": check.get("symbol"),
                        "revision": asset[0],
                        "generation": asset[1],
                        "required_start": check.get("required_start"),
                        "required_end": check.get("required_end"),
                        "adjustments": set(),
                        "details": {},
                    }
                    grouped[security_id] = target
                target["adjustments"].add(check.get("adjustment"))
                cache_key = (
                    security_id,
                    check["required_start"],
                    check["required_end"],
                )
                classified = classified_cache.get(cache_key)
                if classified is None:
                    classified = classified_daily_bar_gaps(
                        connection,
                        security_id,
                        date.fromisoformat(check["required_start"]),
                        date.fromisoformat(check["required_end"]),
                    )
                    classified_cache[cache_key] = classified
                for item in classified.get(check.get("adjustment"), []):
                    identity = (item["date"], item["classification"])
                    merged = target["details"].setdefault(identity, {**item, "adjustments": set()})
                    merged["adjustments"].add(check.get("adjustment"))
        targets: list[dict[str, object]] = []
        totals = {
            "verified_suspension": 0,
            "missing_bar": 0,
            "calendar_mismatch": 0,
            "unverified_gap": 0,
        }
        for target in sorted(grouped.values(), key=lambda item: str(item["symbol"])):
            details = []
            for item in sorted(
                target.pop("details").values(),
                key=lambda value: (value["date"], value["classification"]),
            ):
                normalized = {**item, "adjustments": sorted(item["adjustments"])}
                details.append(normalized)
                totals[normalized["classification"]] += 1
            if not details:
                continue
            classifications = {
                kind: sum(1 for item in details if item["classification"] == kind)
                for kind in (
                    "verified_suspension",
                    "missing_bar",
                    "calendar_mismatch",
                    "unverified_gap",
                )
            }
            checked = sorted(
                str(item["checked_at"])
                for item in details
                if item.get("checked_at")
            )
            unverified_dates = sorted(
                {item["date"] for item in details if item["classification"] == "unverified_gap"}
            )
            targets.append(
                {
                    **target,
                    "adjustments": sorted(value for value in target["adjustments"] if value),
                    "unverified_dates": unverified_dates[:100],
                    "unverified_count": len(unverified_dates),
                    "classification_counts": classifications,
                    "blocking_count": sum(
                        classifications[kind]
                        for kind in ("missing_bar", "calendar_mismatch", "unverified_gap")
                    ),
                    "evidence_sources": sorted(
                        {str(item["source"]) for item in details if item.get("source")}
                    ),
                    "last_checked_at": checked[-1] if checked else None,
                    "details": details[:20],
                    "details_truncated": max(0, len(details) - 20),
                }
            )
        linked_tasks = self._linked_gap_tasks(plan["plan_id"])
        latest_by_security: dict[str, dict[str, object]] = {}
        for task in linked_tasks:
            security_id = str((task.get("spec") or {}).get("security_id") or task["task_id"])
            latest_by_security.setdefault(security_id, task)
        tasks = list(latest_by_security.values())
        task_rows = [
            {
                key: task.get(key)
                for key in (
                    "task_id",
                    "status",
                    "progress",
                    "message",
                    "error",
                    "created_at",
                    "completed_at",
                    "result",
                )
            }
            | {
                "security_id": (task.get("spec") or {}).get("security_id"),
                "symbol": (task.get("spec") or {}).get("symbol"),
            }
            for task in tasks
        ]
        active = any(task.get("status") in {"queued", "running"} for task in tasks)
        failed = any(task.get("status") == "failed" for task in tasks)
        if active:
            status = "running"
        elif failed:
            status = "failed"
        elif totals["unverified_gap"]:
            status = "verification_incomplete" if tasks else "available"
        elif totals["missing_bar"] or totals["calendar_mismatch"]:
            status = "classified_blockers"
        elif any(totals.values()):
            status = "verified"
        else:
            status = "not_required"
        return {
            "status": status,
            "total_dates": sum(totals.values()),
            **totals,
            "requires_confirmation": bool(totals["unverified_gap"]) and not active,
            "current_readiness": {
                "checked_at": report.get("checked_at"),
                "ready": report.get("ready"),
                "complete": report.get("complete"),
                "blocker_count": len(report.get("blockers") or []),
                "warning_count": len(report.get("warnings") or []),
            },
            "targets": targets,
            "tasks": task_rows,
        }

    def prepare_gap_verification(
        self,
        plan_id: str,
        *,
        version: int,
        input_sha256: str,
        confirm: bool,
    ) -> list[dict[str, object]]:
        if confirm is not True:
            raise ValueError("核验缺失日期需要明确确认访问公网")
        plan = self.get(plan_id)
        if (
            version != plan["version"]
            or input_sha256 != plan["input_sha256"]
            or digest(plan["input"]) != input_sha256
        ):
            raise DataPlanConflict("计划版本或摘要不符，请重新查看计划")
        if plan["input"].get("research_type") != "daily_signal":
            raise DataPlanConflict("当前入口只支持日线信号策略缺口核验")
        if plan.get("status") != "succeeded" and plan.get("items"):
            raise DataPlanConflict("请先完成已确认的数据补齐，再核验剩余交易日缺口")
        gap_state = plan["gap_verification"]
        if any(task.get("status") in {"queued", "running"} for task in gap_state["tasks"]):
            raise DataPlanConflict("当前缺口核验任务正在执行，请刷新任务状态")
        requests = []
        for target in gap_state["targets"]:
            dates = list(target.get("unverified_dates") or [])
            if not dates:
                continue
            requests.append(
                {
                    "security_id": target["security_id"],
                    "expected_revision": target["revision"],
                    "dates": dates,
                    "source_context": {
                        "source_plan_id": plan_id,
                        "source_plan_version": version,
                        "source_input_sha256": input_sha256,
                        "source_symbols_sha256": plan["input"]["symbols_sha256"],
                        "source_stock_pool_id": (
                            plan["input"].get("stock_pool") or {}
                        ).get("pool_id"),
                        "source_stock_pool_revision": (
                            plan["input"].get("stock_pool") or {}
                        ).get("pool_revision"),
                        "source_gap_dates_sha256": hashlib.sha256(
                            "|".join(dates).encode()
                        ).hexdigest(),
                        "source": "daily_signal_research_data_plan",
                    },
                }
            )
        if not requests:
            raise DataPlanConflict(
                "当前计划没有可核验的未知交易日缺口；请重新检查本地数据"
            )
        return requests

    @classmethod
    def _execution_summary(cls, items):
        counts = {
            "total": len(items),
            "published": 0,
            "verified": 0,
            "failed": 0,
            "pending": 0,
            "unknown": 0,
            "retryable": 0,
        }
        for item in items:
            status = item.get("status") or "pending"
            if status in cls.completed_item_statuses | {"failed"}:
                counts[status] += 1
            elif status in cls.retryable_item_statuses:
                counts["pending"] += 1
            else:
                counts["unknown"] += 1
            if status in cls.retryable_item_statuses:
                counts["retryable"] += 1
        counts["processed"] = counts["published"] + counts["verified"] + counts["failed"]
        return counts

    @classmethod
    def _decorate(cls, plan):
        summary = cls._execution_summary(plan.get("items", []))
        report = plan.get("report") or {}
        readiness = {
            "ready": report.get("ready"),
            "complete": report.get("complete"),
            "blocker_count": len(report.get("blockers") or []),
            "warning_count": len(report.get("warnings") or []),
            "unresolved_check_count": sum(
                check.get("satisfied") is not True for check in report.get("checks") or []
            ),
        }
        task = plan.get("task") or {}
        task_status = task.get("status")
        if task_status in {"queued", "running"}:
            outcome = task_status
        elif task_status == "failed":
            if summary["failed"]:
                outcome = "download_failed"
            elif summary["retryable"]:
                outcome = "interrupted"
            elif (
                report.get("complete") is False
                and "补齐已执行，但研究数据仍有缺口" in (task.get("error") or "")
            ):
                # Historical plans used failed/95% for this completed execution.
                outcome = "completed_with_gaps"
            else:
                outcome = "interrupted"
        elif task_status == "succeeded" or plan.get("status") == "succeeded":
            outcome = "ready" if report.get("complete") is True else "completed_with_gaps"
        else:
            outcome = "planned"
        return {
            **plan,
            "outcome": outcome,
            "execution_summary": summary,
            "readiness": readiness,
        }

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
        gap_cache: dict[tuple[str, str, str], dict[str, list[dict[str, object]]]] = {}
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
            if (
                frozen["research_type"] == "daily_signal"
                and dataset == "daily_bar"
                and check.get("security_id")
                and check.get("list_date")
            ):
                cache_key = (
                    check["security_id"],
                    check["required_start"],
                    check["required_end"],
                )
                classified = gap_cache.get(cache_key)
                if classified is None:
                    with self.database.connect(read_only=True) as connection:
                        classified = classified_daily_bar_gaps(
                            connection,
                            check["security_id"],
                            date.fromisoformat(check["required_start"]),
                            date.fromisoformat(check["required_end"]),
                        )
                    gap_cache[cache_key] = classified
                adjustment = check.get("adjustment")
                classified_items = classified.get(adjustment, [])
                missing_bar_dates = [
                    date.fromisoformat(value["date"])
                    for value in classified_items
                    if value.get("classification") == "missing_bar"
                ]
                with self.database.connect(read_only=True) as connection:
                    published_range = self._published_price_range(
                        connection,
                        check["security_id"],
                        adjustment,
                        date.fromisoformat(check["required_start"]),
                        completed_date(date.fromisoformat(check["required_end"])),
                    )
                if classified_items and not missing_bar_dates and published_range:
                    # Published bars with only evidence/calendar questions are
                    # not a download job.  The existing explicit verification
                    # path handles unverified dates without inventing prices.
                    continue
                if missing_bar_dates:
                    item["start_date"] = str(min(missing_bar_dates))
                    item["end_date"] = str(max(missing_bar_dates))
                    item["reason"] = (
                        f"{len(missing_bar_dates)} 个日期已分类为真实行情缺失；"
                        "仅同步相关年份分区，未知停牌日期不重复下载"
                    )
            if dataset in {"daily_bar", "benchmark_daily_bar"}:
                item["end_date"] = str(completed_date(date.fromisoformat(item["end_date"])))
            if item["start_date"] > item["end_date"]:
                continue
            item_id = digest(item)[:24]
            item.update(
                item_id=item_id,
                reason=item.get("reason") or check["reason"],
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
        if any(
            check["dataset"] == "trading_calendar" and not check["satisfied"]
            for check in report["checks"]
        ):
            market_item = next(
                (item for item in items.values() if item["dataset"] == "daily_bar"), None
            )
            if market_item is not None:
                market_item["reason"] += "；沿用现有行情同步先刷新交易日历，不另建任务引擎"
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
            if retry:
                retryable = [
                    item
                    for item in plan["items"]
                    if item.get("status") in self.retryable_item_statuses
                ]
                if plan["status"] not in {"failed", "confirmed"} or not retryable:
                    raise DataPlanConflict(
                        "当前计划没有真实失败或未处理项；请重新检查本地数据，"
                        "如需刷新上游数据请基于当前参数创建新计划"
                    )
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
                    "retry_only": retry,
                },
                message="等待重试真实失败或未处理分区" if retry else "等待已确认研究数据补齐",
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
            run_items = (
                [
                    item
                    for item in items
                    if item.get("status") in self.retryable_item_statuses
                ]
                if spec.get("retry_only")
                else items
            )
            if not run_items:
                raise DataPlanConflict(
                    "当前计划没有真实失败或未处理项；不会重复下载已发布或已验证的数据"
                )
            errors = []
            for index, item in enumerate(run_items):
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
                            min(95, 5 + int(90 * (index + 1) / len(run_items))),
                            f"补齐项 {index + 1}/{len(run_items)}",
                            task_id,
                        ],
                    )
            report = self.readiness.check(plan["input"])
            summary = self._execution_summary(items)
            execution_succeeded = (
                not errors
                and summary["failed"] == 0
                and summary["pending"] == 0
                and summary["unknown"] == 0
            )
            outcome = (
                "ready"
                if execution_succeeded and report["complete"]
                else "completed_with_gaps"
                if execution_succeeded
                else "download_failed"
            )
            error = None if execution_succeeded else (
                "；".join(errors)
                or "补齐任务仍有真实失败或未处理项；只会重试这些分区"
            )
            message = (
                "补齐完成，数据检查通过；研究仍需单独操作"
                if outcome == "ready"
                else "同步完成，但最终预检仍有数据缺口；不会自动运行研究"
                if outcome == "completed_with_gaps"
                else "数据同步存在真实失败或未处理分区；可明确确认后重试"
            )
            with self.transaction() as connection:
                self.fence(connection, task_id, owner, spec)
                connection.execute(
                    "UPDATE research_data_plan SET status=?,report_json=?,updated_at=CURRENT_TIMESTAMP WHERE plan_id=?",
                    [
                        "succeeded" if execution_succeeded else "failed",
                        encoded(report),
                        plan["plan_id"],
                    ],
                )
                connection.execute(
                    "UPDATE operation_task SET status=?,progress=?,message=?,result_json=?,error=?,"
                    "completed_at=CURRENT_TIMESTAMP,heartbeat_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,"
                    "claim_key=CASE WHEN ? THEN claim_key ELSE NULL END WHERE task_id=?",
                    [
                        "succeeded" if execution_succeeded else "failed",
                        100,
                        message,
                        encoded(
                            {
                                "plan_id": plan["plan_id"],
                                "report": report,
                                "outcome": outcome,
                                "execution_summary": summary,
                            }
                        ),
                        error[:2000] if error else None,
                        execution_succeeded,
                        task_id,
                    ],
                )
                self.audit(
                    connection,
                    "completed" if execution_succeeded else "failed",
                    plan,
                    spec["actor"],
                    task_id=task_id,
                    outcome=outcome,
                    execution_summary=summary,
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
                            "progress=100,result_json=NULL,claim_key=NULL,"
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
