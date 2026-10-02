from __future__ import annotations

import hashlib
import json
import re
from contextlib import ExitStack, contextmanager
from datetime import UTC, date, datetime, timedelta
from threading import Event, Thread
from typing import Any
from uuid import uuid4

from ai_quant_assistant.config import Settings
from ai_quant_assistant.domain.models import AnnouncementSyncSpec, DataSyncSpec
from ai_quant_assistant.providers.akshare_provider import AkShareProvider
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.locks import InterProcessFileLock
from ai_quant_assistant.storage.managed_coverage import (
    ADJUSTMENTS,
    classified_daily_bar_gaps,
    completed_date,
    coverage_report,
    market_today,
)
from ai_quant_assistant.storage.research_sync_service import ResearchDatasetSyncService
from ai_quant_assistant.storage.sync_service import MarketDataSyncService
from ai_quant_assistant.storage.task_repository import TASK_COLUMNS, OperationTaskRepository

LEASE_SECONDS = 1800


class ManagedStockConflict(RuntimeError):
    pass


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _announcement_covers(
    coverage: dict[str, Any], start: date, end: date
) -> bool:
    """Return true only for a previously published result covering this request."""
    saved = coverage.get("announcement_sync") or {}
    try:
        return bool(
            saved.get("status") == "published"
            and date.fromisoformat(str(saved["start_date"])) <= start
            and date.fromisoformat(str(saved["end_date"])) >= end
        )
    except (KeyError, TypeError, ValueError):
        return False


def _stage(status: str, message: str, **details: Any) -> dict[str, Any]:
    return {"status": status, "message": message, **details}


class ManagedStockService:
    """Management state is distinct from task state and historical research inputs.

    All mutations use the shared short DB access lock/transaction. Network work
    is outside it. No repository-local lock is acquired inside a DB transaction.
    """

    def __init__(
        self,
        database: LocalDatabase,
        settings: Settings | None = None,
        provider_factory: Any = None,
    ) -> None:
        self.database = database
        self.settings = settings or Settings(data_dir=database.layout.root)
        self.tasks = OperationTaskRepository(database)
        self.provider_factory = provider_factory or (
            lambda attempt: AkShareProvider(
                cache_dir=database.layout.root / "cache" / "managed-stock" / attempt,
                retries=self.settings.akshare_retries,
                timeout_seconds=self.settings.akshare_timeout_seconds,
                refresh=True,
                reference_cache_dir=database.layout.root
                / "cache"
                / "managed-stock"
                / "reference",
            )
        )

    @contextmanager
    def transaction(self):
        self.database.initialize()
        with self.database.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                yield connection
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def audit(connection: Any, action: str, security_id: str, actor: str, **details) -> None:
        connection.execute(
            """INSERT INTO operation_audit
               (event_id, action, actor, outcome, object_type, object_id, details_json)
               VALUES (?, ?, ?, ?, 'managed_stock', ?, ?)""",
            [
                f"audit-{uuid4().hex}",
                action,
                actor,
                "failure" if action.endswith("_failed") else "success",
                security_id,
                encoded(details),
            ],
        )

    @staticmethod
    def _get(connection: Any, security_id: str) -> dict[str, Any]:
        cursor = connection.execute(
            """SELECT m.*, s.symbol, s.name, s.exchange, s.status AS listing_status
               FROM managed_stock m LEFT JOIN security_master s USING(security_id)
               WHERE m.security_id=?""",
            [security_id],
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(security_id)
        result = dict(zip([item[0] for item in cursor.description], row, strict=True))
        result["coverage"] = json.loads(result.pop("coverage_json"))
        financial = connection.execute(
            "SELECT COUNT(DISTINCT report_id),MAX(published_at),COUNT(DISTINCT metric_code) "
            "FROM published_financial_metric WHERE security_id=?",
            [security_id],
        ).fetchone()
        result["financial_summary"] = {
            "reports": financial[0],
            "latest_published_at": financial[1],
            "metrics": financial[2],
            "research_ready": None,
        }
        result["datasets"] = ["daily_bar"]
        result["adjustments"] = list(ADJUSTMENTS)
        result["eligible"] = (
            result["status"] == "active"
            and result["availability"] == "ready"
            and result["listing_status"] == "active"
        )
        if result["last_task_id"]:
            task = connection.execute(
                f"SELECT {', '.join(TASK_COLUMNS)} FROM operation_task WHERE task_id=?",
                [result["last_task_id"]],
            ).fetchone()
            result["task"] = OperationTaskRepository._decode(task) if task else None
        else:
            result["task"] = None
        return json.loads(encoded(result))

    def get(self, security_id: str) -> dict[str, Any]:
        self.database.initialize()
        with self.database.connect(read_only=True) as connection:
            return self._get(connection, security_id)

    def list(
        self,
        *,
        query: str = "",
        include_archived: bool = False,
        ready_only: bool = False,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        self.database.initialize()
        if len(query) > 120 or page < 1 or not 1 <= page_size <= 200:
            raise ValueError("invalid query or pagination")
        with self.database.connect(read_only=True) as connection:
            where = """WHERE (? OR m.status='active') AND
                       (m.security_id ILIKE ? OR s.name ILIKE ?)
                       AND (NOT ? OR (m.status='active' AND m.availability='ready' AND s.status='active'))"""
            params = [include_archived, f"%{query}%", f"%{query}%", ready_only]
            total = connection.execute(
                "SELECT COUNT(*) FROM managed_stock m LEFT JOIN security_master s USING(security_id) "
                + where,
                params,
            ).fetchone()[0]
            ids = connection.execute(
                "SELECT m.security_id FROM managed_stock m LEFT JOIN security_master s USING(security_id) "
                + where
                + " ORDER BY m.security_id LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return {
                "items": [self._get(connection, row[0]) for row in ids],
                "pagination": {"page": page, "page_size": page_size, "total": total},
            }

    def search_directory(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        if not query.strip() or len(query) > 120 or not 1 <= limit <= 50:
            raise ValueError("invalid directory query or limit")
        self.database.initialize()
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """SELECT s.security_id, s.symbol, s.name, s.exchange, s.status,
                          m.status, m.availability
                   FROM security_master s LEFT JOIN managed_stock m USING(security_id)
                   WHERE s.symbol ILIKE ? OR s.name ILIKE ? ORDER BY s.symbol LIMIT ?""",
                [f"%{query.strip()}%", f"%{query.strip()}%", limit],
            ).fetchall()
        keys = (
            "security_id",
            "symbol",
            "name",
            "exchange",
            "status",
            "management_status",
            "availability",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    @staticmethod
    def references_in(connection: Any, security_id: str) -> list[dict[str, Any]]:
        return [
            dict(zip(("pool_id", "name", "revision"), row, strict=True))
            for row in connection.execute(
                """SELECT p.pool_id, p.name, p.current_revision FROM stock_pool p
                       JOIN stock_pool_member m USING(pool_id)
                       WHERE m.security_id=? AND p.status='active' ORDER BY p.pool_id""",
                [security_id],
            ).fetchall()
        ]

    def references(self, security_id: str) -> list[dict[str, Any]]:
        self.get(security_id)
        with self.database.connect(read_only=True) as connection:
            return self.references_in(connection, security_id)

    def request_gap_verification(
        self,
        security_id: str,
        *,
        expected_revision: int,
        confirm: bool,
        actor: str = "system",
        dates: list[str] | tuple[str, ...] | None = None,
        source_context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if confirm is not True:
            raise ValueError("核验停牌状态需要明确确认访问公网")
        with self.transaction() as connection:
            asset = self._get(connection, security_id)
            if asset["revision"] != expected_revision:
                raise ManagedStockConflict(f"revision conflict: current {asset['revision']}")
            if asset["status"] != "active":
                raise ManagedStockConflict("已移出管理的股票不能启动缺口核验")
            raw_dates = list(dates or []) if dates is not None else None
            explicit = None
            parsed_dates: list[date] = []
            if raw_dates is not None:
                try:
                    parsed_dates = [date.fromisoformat(value) for value in raw_dates]
                except (TypeError, ValueError) as exc:
                    raise ValueError("缺失日期必须使用YYYY-MM-DD格式") from exc
                explicit = sorted({str(value) for value in parsed_dates})
            if explicit is not None and not explicit:
                raise ManagedStockConflict("当前没有可核验的未知交易日缺口；不会创建联网任务")
            if explicit is not None and len(explicit) > 100:
                raise ValueError("单次最多核验100个缺失日期，请分批确认")
            if any(day > market_today() for day in parsed_dates):
                raise ValueError("不能核验未来日期")
            target_start = (
                min(parsed_dates) if parsed_dates else date.fromisoformat(asset["target_start"])
            )
            target_end = (
                max(parsed_dates)
                if parsed_dates
                else completed_date(date.fromisoformat(asset["target_end"]))
            )
            if asset.get("listing_date"):
                target_start = max(target_start, date.fromisoformat(asset["listing_date"]))
            classified = classified_daily_bar_gaps(
                connection,
                security_id,
                target_start,
                target_end,
                evidence=asset["coverage"].get("gap_verification", {}),
            )
            available = sorted(
                {
                    item["date"]
                    for rows in classified.values()
                    for item in rows
                    if item.get("classification") == "unverified_gap" and item.get("date")
                }
            )
            requested = available[:100] if explicit is None else explicit
            if not requested:
                raise ManagedStockConflict("当前没有可核验的未知交易日缺口；不会创建联网任务")
            unavailable = sorted(set(requested) - set(available))
            if unavailable:
                raise ManagedStockConflict(
                    f"{len(unavailable)}个日期已不属于当前未知缺口，请重新检查后再确认"
                )
            source_context = dict(source_context or {})
            source_plan_id = source_context.get("source_plan_id")
            current_task = asset.get("task") or {}
            if current_task.get("status") in {"queued", "running"}:
                current_spec = current_task.get("spec") or {}
                if (
                    current_spec.get("dataset") == "managed_stock_gap_verification"
                    and current_spec.get("dates") == requested
                    and current_spec.get("source_plan_id") == source_plan_id
                ):
                    return current_task, False
                raise ManagedStockConflict("当前已有后台任务，请等待完成后再核验")
            saved_dates = asset["coverage"].get("gap_verification", {}).get("dates", {})
            evidence_identity = hashlib.sha256(
                json.dumps(
                    {value: saved_dates.get(value) for value in requested},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode()
            ).hexdigest()[:16]
            spec = {
                "dataset": "managed_stock_gap_verification",
                "security_id": security_id,
                "symbol": asset["symbol"],
                "generation": asset["generation"],
                "revision": asset["revision"],
                "dates": requested,
                "actor": actor,
                "confirmed": True,
                **source_context,
            }
            task, created = self.tasks.create_or_get_active(
                "sync",
                claim_key=(
                    f"managed-gap:{security_id}:{asset['generation']}:"
                    f"{hashlib.sha256('|'.join(requested).encode()).hexdigest()[:16]}:"
                    f"{evidence_identity}"
                ),
                spec=spec,
                message=f"等待核验 {len(requested)} 个无行情日期的停牌状态",
                connection=connection,
            )
            connection.execute(
                "UPDATE managed_stock SET last_task_id=?, updated_at=CURRENT_TIMESTAMP WHERE security_id=?",
                [task["task_id"], security_id],
            )
            if created:
                self.audit(
                    connection,
                    "managed_stock_gap_verification_requested",
                    security_id,
                    actor,
                    task_id=task["task_id"],
                    dates=requested,
                    source_plan_id=source_plan_id,
                )
            return task, created

    def request(
        self,
        symbol: str,
        *,
        start: date = date(2012, 1, 1),
        end: date | None = None,
        expected_revision: int | None = None,
        confirm: bool = False,
        actor: str = "system",
        refresh: bool = False,
        auto_update: bool = True,
        include_announcements: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        end = end or market_today()
        if confirm is not True:
            raise ValueError("请明确确认添加并下载行情")
        if not isinstance(include_announcements, bool):
            raise ValueError("include_announcements must be a boolean")
        if not re.fullmatch(r"[0-9]{6}", symbol):
            raise ValueError("symbol must be a six-digit code")
        if start < date(1990, 1, 1) or start > end or end > market_today():
            raise ValueError("invalid historical date range")
        with self.transaction() as connection:
            security = connection.execute(
                "SELECT security_id, status, list_date FROM security_master WHERE symbol=?",
                [symbol],
            ).fetchone()
            if security is None:
                raise ValueError(f"{symbol}: missing from security_master，请先更新证券目录")
            if security[1] != "active":
                raise ValueError(f"{symbol}: listing status is {security[1]}, cannot enroll")
            security_id = security[0]
            exists = connection.execute(
                "SELECT 1 FROM managed_stock WHERE security_id=?",
                [security_id],
            ).fetchone()
            if not exists:
                if expected_revision is not None:
                    raise KeyError(security_id)
                connection.execute(
                    """INSERT INTO managed_stock (security_id, target_start, target_end,
                       auto_update, listing_date, listing_source) VALUES (?, ?, ?, ?, ?, ?)""",
                    [
                        security_id,
                        start,
                        end,
                        auto_update,
                        security[2],
                        "security_master" if security[2] else None,
                    ],
                )
            asset = self._get(connection, security_id)
            if asset["status"] != "active":
                raise ManagedStockConflict("股票已移出管理，请先恢复管理")
            if expected_revision is not None and expected_revision != asset["revision"]:
                raise ManagedStockConflict(f"revision conflict: current {asset['revision']}")
            same_range = str(start) == asset["target_start"] and str(end) == asset["target_end"]
            same_datasets = (
                bool(asset["task"] and asset["task"]["spec"].get("include_announcements", False))
                == include_announcements
            )
            if (
                exists
                and (not same_range or auto_update != asset["auto_update"])
                and expected_revision is None
            ):
                raise ManagedStockConflict("已纳入管理；修改目标范围需携带 expected_revision")
            if asset["task"] and asset["task"]["status"] in {"queued", "running"}:
                live = connection.execute(
                    """SELECT 1 FROM operation_task WHERE task_id=?
                       AND COALESCE(heartbeat_at, started_at, updated_at) > ?""",
                    [asset["last_task_id"], datetime.now(UTC) - timedelta(seconds=LEASE_SECONDS)],
                ).fetchone()
                if (not same_range or not same_datasets) and live:
                    raise ManagedStockConflict(
                        "当前有同步任务且范围或数据类型不同，请等待完成后重试"
                    )
            elif (
                same_range
                and asset["availability"] == "ready"
                and not refresh
                and same_datasets
                and auto_update == asset["auto_update"]
                and asset["task"]
            ):
                if asset["task"]["status"] == "succeeded":
                    report = coverage_report(
                        connection,
                        security_id,
                        start,
                        completed_date(end),
                        date.fromisoformat(asset["listing_date"])
                        if asset["listing_date"]
                        else None,
                    )
                    if report["availability"] == "ready":
                        return asset["task"], False
            if exists and (not same_range or auto_update != asset["auto_update"]):
                connection.execute(
                    """UPDATE managed_stock SET target_start=?, target_end=?, auto_update=?,
                       revision=revision+1, updated_at=CURRENT_TIMESTAMP WHERE security_id=?""",
                    [start, end, auto_update, security_id],
                )
            retrying_failed = bool(
                exists
                and same_range
                and same_datasets
                and asset.get("task")
                and asset["task"].get("status") == "failed"
            )
            announcement_already_complete = bool(
                include_announcements
                and retrying_failed
                and _announcement_covers(asset["coverage"], start, completed_date(end))
            )
            previous_stages = (
                asset.get("task", {}).get("result", {}).get("stages", {})
                if retrying_failed
                else {}
            )
            previous_outcome = (
                asset.get("task", {}).get("result", {}).get("outcome")
                if retrying_failed
                else None
            )
            price_retry_needed = not retrying_failed or (
                previous_outcome == "coverage_incomplete"
                or previous_stages.get("listing_date", {}).get("status") == "failed"
                or previous_stages.get("daily_bar", {}).get("status")
                in {"failed", "not_started", "pending", "running"}
                or not previous_stages
            )
            requested_stages = (
                ["listing_date", "daily_bar"] if price_retry_needed else []
            )
            if include_announcements and not announcement_already_complete:
                requested_stages.append("announcement")
            if retrying_failed and not requested_stages:
                raise ManagedStockConflict(
                    "没有可重试的失败或未完成阶段；已完成的数据不会重复同步"
                )
            spec = {
                "dataset": "managed_stock",
                "security_id": security_id,
                "symbol": symbol,
                "generation": asset["generation"],
                "start_date": str(start),
                "end_date": str(end),
                "adjustments": list(ADJUSTMENTS),
                "actor": actor,
                "refresh": refresh,
                "confirmed": True,
                "include_announcements": include_announcements,
                "datasets": ["daily_bar", "announcement"]
                if include_announcements
                else ["daily_bar"],
                "requested_stages": requested_stages,
                "resume_completed_announcement": announcement_already_complete,
                "resume_failed_stages": retrying_failed,
                "preserve_classified_gaps": bool(
                    asset.get("task")
                    and asset["task"].get("status") == "succeeded"
                    and (
                        asset["task"].get("result", {}).get("outcome")
                        in {"needs_gap_verification", "verification_incomplete", "verified"}
                        or "仍有日期需核验" in (asset["task"].get("message") or "")
                    )
                ),
            }
            task, created = self.tasks.create_or_get_active(
                "sync",
                claim_key=f"managed-stock:{security_id}",
                spec=spec,
                message="等待单股行情与定期公告回填"
                if include_announcements
                else "等待单股历史行情回填",
                connection=connection,
            )
            connection.execute(
                "UPDATE managed_stock SET last_task_id=?, updated_at=CURRENT_TIMESTAMP WHERE security_id=?",
                [task["task_id"], security_id],
            )
            if created:
                self.audit(
                    connection,
                    "managed_stock_sync_requested",
                    security_id,
                    actor,
                    task_id=task["task_id"],
                    start=start,
                    end=end,
                    generation=asset["generation"],
                    created=not bool(exists),
                    datasets=spec["datasets"],
                )
            return task, created

    def change(
        self,
        security_id: str,
        *,
        expected_revision: int,
        action: str,
        confirm: bool,
        actor: str = "system",
        auto_update: bool | None = None,
    ) -> dict[str, Any]:
        if confirm is not True:
            raise ValueError("operation requires explicit confirmation")
        if action not in {"archive", "restore", "configure"}:
            raise ValueError("invalid management action")
        if action == "configure" and not isinstance(auto_update, bool):
            raise ValueError("configure requires an explicit auto_update boolean")
        with self.transaction() as connection:
            asset = self._get(connection, security_id)
            if asset["revision"] != expected_revision:
                raise ManagedStockConflict(f"revision conflict: current {asset['revision']}")
            if action == "archive":
                references = self.references_in(connection, security_id)
                if references:
                    raise ManagedStockConflict("仍被活动股票池引用：" + encoded(references))
            if action == "restore" and asset["status"] != "archived":
                raise ManagedStockConflict("stock is already active")
            if action == "configure" and asset["status"] != "active":
                raise ManagedStockConflict("archived stock cannot enable automatic updates")
            status = "archived" if action == "archive" else "active"
            report = coverage_report(
                connection,
                security_id,
                date.fromisoformat(asset["target_start"]),
                completed_date(date.fromisoformat(asset["target_end"])),
                date.fromisoformat(asset["listing_date"]) if asset["listing_date"] else None,
            )
            if asset["coverage"].get("announcement_sync"):
                report["announcement_sync"] = asset["coverage"]["announcement_sync"]
            # Invalidate before returning, in the same DB-locked transaction.
            connection.execute(
                """UPDATE operation_task SET status='failed', progress=LEAST(progress,99),
                   error='management generation changed', message='管理状态已变更，旧任务失效',
                   completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP,
                   result_json=NULL, claim_key=NULL
                   WHERE task_id=? AND status IN ('queued','running')""",
                [asset["last_task_id"]],
            )
            connection.execute(
                """UPDATE managed_stock SET status=?, auto_update=?, availability=?, coverage_json=?,
                   revision=revision+1, generation=generation+1, updated_at=CURRENT_TIMESTAMP
                   WHERE security_id=?""",
                [
                    status,
                    bool(auto_update) if action == "configure" else False,
                    report["availability"],
                    encoded(report),
                    security_id,
                ],
            )
            self.audit(
                connection,
                f"managed_stock_{action}",
                security_id,
                actor,
                revision=expected_revision + 1,
                generation=asset["generation"] + 1,
            )
        return self.get(security_id)

    def scheduled_symbols(self) -> tuple[str, ...]:
        self.database.initialize()
        with self.database.connect(read_only=True) as connection:
            return tuple(
                row[0]
                for row in connection.execute(
                    """SELECT s.symbol FROM managed_stock m JOIN security_master s USING(security_id)
                   WHERE m.status='active' AND m.auto_update AND s.status='active' ORDER BY s.symbol"""
                ).fetchall()
            )

    @staticmethod
    def fence(connection: Any, task_id: str, owner: str, spec: dict[str, Any]) -> None:
        owned = connection.execute(
            """SELECT 1 FROM operation_task t JOIN managed_stock m ON m.last_task_id=t.task_id
               WHERE t.task_id=? AND t.status='running' AND t.owner_id=?
                 AND t.heartbeat_at > ? AND m.security_id=? AND m.generation=? AND m.status='active'""",
            [
                task_id,
                owner,
                datetime.now(UTC) - timedelta(seconds=LEASE_SECONDS),
                spec["security_id"],
                spec["generation"],
            ],
        ).fetchone()
        if owned is None:
            raise ManagedStockConflict("worker lost task ownership or management generation")

    def run(self, task_id: str) -> dict[str, Any]:
        # A dedicated download slot bounds memory across Uvicorn workers, without
        # holding the global operation or DB lock over network calls.
        slot = InterProcessFileLock(
            self.database.layout.root / ".managed-download.lock",
            description="managed stock download slot",
        )
        with ExitStack() as stack:
            try:
                stack.enter_context(slot.hold(timeout_seconds=0))
            except RuntimeError as exc:
                task = self.tasks.get(task_id)
                task["waiting_reason"] = str(exc)
                return task
            return self._run_owned(task_id)

    def _run_owned(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        spec = task["spec"]
        if spec.get("dataset") == "managed_stock_gap_verification":
            return self._run_gap_verification(task_id, spec)
        if spec.get("dataset") != "managed_stock":
            raise ValueError("not a managed stock task")
        requested = set(
            spec.get("requested_stages")
            or [
                "listing_date",
                "daily_bar",
                *(["announcement"] if spec.get("include_announcements", False) else []),
            ]
        )
        stages = {
            "listing_date": _stage(
                "pending" if "listing_date" in requested else "skipped",
                "等待核验上市日期" if "listing_date" in requested else "本次未请求",
            ),
            "daily_bar": _stage(
                "pending" if "daily_bar" in requested else "skipped",
                "等待同步行情" if "daily_bar" in requested else "本次未请求",
            ),
            "announcement": _stage(
                "pending" if "announcement" in requested else "not_requested",
                "等待同步定期公告"
                if "announcement" in requested
                else "本次未请求定期公告",
            ),
        }
        if spec.get("resume_completed_announcement"):
            saved = self.get(spec["security_id"])["coverage"].get("announcement_sync", {})
            counts = saved.get("partitions") or {}
            stages["announcement"] = _stage(
                "skipped",
                "已有完整公告结果，本次重试跳过",
                completed_partitions=counts.get("published"),
                total_partitions=sum(counts.values()) or None,
            )
        owner = f"managed-worker-{uuid4().hex}"
        start_message = (
            "正在查询上市日期（行情下载尚未开始）"
            if "daily_bar" in requested
            else "正在恢复公告失败分区（已完成行情不会重复同步）"
        )
        if not self.tasks.start(
            task_id, owner_id=owner, message=start_message
        ):
            return self.tasks.get(task_id)
        stop = Event()

        def heartbeat():
            while not stop.wait(15):
                try:
                    self._heartbeat(task_id, owner, spec)
                except Exception:
                    return

        thread = Thread(target=heartbeat, daemon=True)
        thread.start()
        error = None
        announcement_result = None
        if "daily_bar" in requested:
            try:
                self._download(task_id, owner, spec, stages)
            except Exception as exc:
                error = str(exc)[:2000]
                if stages["listing_date"]["status"] not in {
                    "failed",
                    "pending",
                    "running",
                } and stages["daily_bar"]["status"] in {"pending", "running"}:
                    stages["daily_bar"] = _stage("failed", "行情同步未完成", error=error)
        else:
            stages["listing_date"] = _stage("skipped", "此前上市日期核验已完成")
            stages["daily_bar"] = _stage("skipped", "此前行情同步已完成")
        if "announcement" in requested:
            try:
                announcement_result = self._download_announcements(task_id, owner, spec)
            except Exception as exc:
                announcement_result = {"status": "failed", "error": str(exc)[:2000]}
            counts = announcement_result.get("partitions", {}) or {}
            if announcement_result["status"] == "published":
                stages["announcement"] = _stage(
                    "succeeded",
                    "定期公告同步完成",
                    completed_partitions=counts.get("published", 0),
                    failed_partitions=counts.get("failed", 0),
                    pending_partitions=counts.get("planned", 0),
                    total_partitions=sum(counts.values()),
                )
            else:
                issues = announcement_result.get("errors") or []
                detail = (issues[0].get("error") if issues else None) or announcement_result.get(
                    "error"
                )
                if announcement_result.get("paused_after_provider_failure"):
                    detail = announcement_result.get("error") or detail
                stages["announcement"] = _stage(
                    "failed",
                    "定期公告同步未完成",
                    completed_partitions=counts.get("published", 0),
                    failed_partitions=counts.get("failed", 0),
                    pending_partitions=counts.get("planned", 0),
                    total_partitions=sum(counts.values()),
                    error=(detail or "原因未记录")[:700],
                )
                announcement_error = (
                    "定期公告同步失败："
                    + (detail or "原因未记录")[:700]
                    + f"（完成 {counts.get('published', 0)}、失败 {counts.get('failed', 0)}、"
                    + f"待处理 {counts.get('planned', 0)} 个分区）。"
                    + "请排查后重新确认同步；公告失败不代表已有行情丢失。"
                )
                error = f"{error}；{announcement_error}" if error else announcement_error
        if error is None:
            try:
                self._progress(task_id, owner, 95, "正在校验目标覆盖并提交结果")
            except Exception as exc:
                error = str(exc)[:2000]
        try:
            with self.transaction() as connection:
                self.fence(connection, task_id, owner, spec)
                asset = self._get(connection, spec["security_id"])
                report = coverage_report(
                    connection,
                    spec["security_id"],
                    date.fromisoformat(spec["start_date"]),
                    completed_date(date.fromisoformat(spec["end_date"])),
                    date.fromisoformat(asset["listing_date"]) if asset["listing_date"] else None,
                )
                if announcement_result is not None:
                    report["announcement_sync"] = {
                        **announcement_result,
                        "task_id": task_id,
                        "start_date": spec["start_date"],
                        "end_date": spec["end_date"],
                    }
                elif asset["coverage"].get("announcement_sync"):
                    report["announcement_sync"] = asset["coverage"]["announcement_sync"]
                if asset["coverage"].get("listing_evidence"):
                    report["listing_evidence"] = asset["coverage"]["listing_evidence"]
                ready = error is None and report["availability"] == "ready"
                gap_only = bool(report.get("requires_gap_verification")) and all(
                    "交易日缺口仍需核验" in issue for issue in report.get("issues", [])
                )
                success = error is None and (ready or gap_only)
                error = error or ("；".join(report["issues"]) if not success else None)
                availability = report["availability"]
                # A failed refresh does not revoke previously validated historical coverage.
                if not success and asset["availability"] == "ready":
                    previous = asset["coverage"].get(
                        "previous_validated_coverage", asset["coverage"]
                    )
                    if previous.get("effective_start") and previous.get("effective_end"):
                        old_report = coverage_report(
                            connection,
                            spec["security_id"],
                            date.fromisoformat(previous["effective_start"]),
                            date.fromisoformat(previous["effective_end"]),
                            date.fromisoformat(asset["listing_date"])
                            if asset["listing_date"]
                            else None,
                        )
                        if old_report["availability"] == "ready":
                            availability = "ready"
                            report["previous_validated_coverage"] = old_report
                            report["update_warning"] = error
                connection.execute(
                    """UPDATE managed_stock SET availability=?, coverage_json=?,
                       last_success_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE last_success_at END,
                       updated_at=CURRENT_TIMESTAMP WHERE security_id=?""",
                    [availability, encoded(report), ready, spec["security_id"]],
                )
                stage = connection.execute(
                    "SELECT message FROM operation_task WHERE task_id=?", [task_id]
                ).fetchone()[0]
                if success:
                    outcome = "needs_gap_verification" if gap_only else "ready"
                    message = (
                        "行情同步完成，但仍有日期需核验；不会重复下载已发布行情"
                        if gap_only
                        else "行情与定期公告同步完成"
                        if spec.get("include_announcements")
                        else "历史行情已就绪"
                    )
                elif stages["listing_date"]["status"] == "failed":
                    outcome = "listing_date_failed"
                    suffix = (
                        "定期公告已经完成。可以重试上市日期核验，已完成的公告不会重复同步"
                        if stages["announcement"]["status"] in {"succeeded", "skipped"}
                        else "请重试上市日期核验；行情尚未开始"
                    )
                    message = f"上市日期核验失败，行情同步尚未开始；{suffix}"
                elif stages["daily_bar"]["status"] == "failed":
                    outcome = "daily_bar_failed"
                    message = "行情同步失败；只需重试行情未完成阶段"
                elif stages["announcement"]["status"] == "failed":
                    outcome = "announcement_failed"
                    message = "行情已完成，定期公告同步失败；只需重试公告失败分区"
                else:
                    outcome = "coverage_incomplete"
                    message = f"{stage}；同步阶段已结束，但覆盖检查未通过"
                # Keep the historical successful result fields at the top level;
                # stages/outcome/coverage are additive so older clients can ignore them.
                result_payload = {
                    **report,
                    "outcome": outcome,
                    "stages": stages,
                    "coverage": report,
                }
                connection.execute(
                    """UPDATE operation_task SET status=?,
                       progress=100,
                       message=?, result_json=?, error=?,
                       completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP,
                       heartbeat_at=CURRENT_TIMESTAMP, claim_key=NULL WHERE task_id=? AND owner_id=?""",
                    [
                        "succeeded" if success else "failed",
                        message,
                        encoded(result_payload),
                        error[:2000] if error else None,
                        task_id,
                        owner,
                    ],
                )
                self.audit(
                    connection,
                    "managed_stock_sync_completed" if success else "managed_stock_sync_failed",
                    spec["security_id"],
                    spec["actor"],
                    task_id=task_id,
                    generation=spec["generation"],
                    availability=availability,
                    datasets=spec.get("datasets", ["daily_bar"]),
                )
        except ManagedStockConflict:
            pass  # A newer management operation owns the terminal state.
        except Exception as exc:
            # The final transaction rolled back, including readiness and audit.
            # Report failure with an owner CAS; never leave a false success or
            # overwrite an archived/reclaimed task if finalization fails.
            self.tasks.fail(
                task_id,
                owner_id=owner,
                message="提交同步结果失败，可安全重试",
                error=str(exc)[:2000],
            )
        finally:
            stop.set()
            thread.join(timeout=1)
        return self.tasks.get(task_id)

    def _run_gap_verification(self, task_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        owner = f"managed-gap-worker-{uuid4().hex}"
        if not self.tasks.start(task_id, owner_id=owner, message="正在按缺失日期核验停牌证据"):
            return self.tasks.get(task_id)
        try:
            dates = tuple(date.fromisoformat(value) for value in spec["dates"])
            provider = self.provider_factory(f"{task_id}-{owner}")
            result = provider.get_suspension_evidence(spec["symbol"], dates)
            with self.transaction() as connection:
                self.fence(connection, task_id, owner, spec)
                asset = self._get(connection, spec["security_id"])
                old = asset["coverage"].get("gap_verification", {})
                saved = dict(old.get("dates", {})) if isinstance(old, dict) else {}
                checked_at = datetime.now(UTC).isoformat()
                verified = {
                    item["date"]: item
                    for item in result.get("verified", [])
                    if item.get("classification") == "verified_suspension"
                }
                errors = {item["date"]: item["error"] for item in result.get("errors", [])}
                for day in spec["dates"]:
                    saved[day] = verified.get(day) or {
                        "classification": "unverified_gap",
                        "source": result.get("source"),
                        "checked_at": checked_at,
                        "reason": errors.get(day)
                        or "停复牌来源未返回可明确覆盖该日期的记录；空结果不作为正常交易证明",
                    }
                evidence = {
                    "version": 1,
                    "checked_at": checked_at,
                    "source": result.get("source"),
                    "dates": saved,
                }
                staged = dict(asset["coverage"])
                staged["gap_verification"] = evidence
                connection.execute(
                    "UPDATE managed_stock SET coverage_json=? WHERE security_id=?",
                    [encoded(staged), spec["security_id"]],
                )
                report = coverage_report(
                    connection,
                    spec["security_id"],
                    date.fromisoformat(asset["target_start"]),
                    completed_date(date.fromisoformat(asset["target_end"])),
                    date.fromisoformat(asset["listing_date"]) if asset["listing_date"] else None,
                )
                if asset["coverage"].get("announcement_sync"):
                    report["announcement_sync"] = asset["coverage"]["announcement_sync"]
                summary = report.get("gap_summary", {})
                outcome = (
                    "verified"
                    if not report.get("requires_gap_verification")
                    else "verification_incomplete"
                )
                payload = {
                    "outcome": outcome,
                    "requested": len(spec["dates"]),
                    "verified_suspension": summary.get("verified_suspension", 0),
                    "unverified_gap": summary.get("unverified_gap", 0),
                    "provider_errors": result.get("errors", []),
                    "coverage": report,
                }
                connection.execute(
                    """UPDATE managed_stock SET availability=?, coverage_json=?,
                       updated_at=CURRENT_TIMESTAMP WHERE security_id=?""",
                    [report["availability"], encoded(report), spec["security_id"]],
                )
                connection.execute(
                    """UPDATE operation_task SET status='succeeded', progress=100, message=?,
                       result_json=?, error=NULL, completed_at=CURRENT_TIMESTAMP,
                       updated_at=CURRENT_TIMESTAMP, heartbeat_at=CURRENT_TIMESTAMP,
                       claim_key=NULL WHERE task_id=? AND owner_id=?""",
                    [
                        "缺失日期核验完成"
                        if outcome == "verified"
                        else "核验已完成，仍有日期暂时无法确认；不会重复下载已发布行情",
                        encoded(payload),
                        task_id,
                        owner,
                    ],
                )
                self.audit(
                    connection,
                    "managed_stock_gap_verification_completed",
                    spec["security_id"],
                    spec["actor"],
                    task_id=task_id,
                    outcome=outcome,
                    requested=len(spec["dates"]),
                )
        except ManagedStockConflict:
            pass
        except Exception as exc:
            self.tasks.fail(
                task_id,
                owner_id=owner,
                message="停牌证据核验任务失败；已发布行情未受影响",
                error=str(exc)[:2000],
            )
        return self.tasks.get(task_id)

    def _heartbeat(self, task_id: str, owner: str, spec: dict[str, Any]) -> None:
        # Renew the lease only. Heartbeats must not replace a precise stage with
        # a generic message, advance progress, or revive an obsolete generation.
        with self.database.connect() as connection:
            self.fence(connection, task_id, owner, spec)
            connection.execute(
                """UPDATE operation_task SET heartbeat_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP WHERE task_id=? AND owner_id=?""",
                [task_id, owner],
            )

    def _progress(self, task_id: str, owner: str, value: int, message: str) -> None:
        if not self.tasks.progress(task_id, owner_id=owner, progress=value, message=message):
            raise ManagedStockConflict("worker no longer owns the task")

    def _download(
        self,
        task_id: str,
        owner: str,
        spec: dict[str, Any],
        stages: dict[str, dict[str, Any]],
    ) -> None:
        provider = self.provider_factory(f"{task_id}-{owner}")
        start = date.fromisoformat(spec["start_date"])
        end = completed_date(date.fromisoformat(spec["end_date"]))
        asset = self.get(spec["security_id"])
        listed = date.fromisoformat(asset["listing_date"]) if asset["listing_date"] else None
        source = asset.get("listing_source")
        security_listed = None
        with self.database.connect(read_only=True) as connection:
            security_listed = connection.execute(
                "SELECT list_date FROM security_master WHERE security_id=?",
                [spec["security_id"]],
            ).fetchone()[0]
        if listed and security_listed and listed != security_listed:
            stages["listing_date"] = _stage(
                "failed",
                "上市日期来源冲突",
                managed_stock_date=str(listed),
                managed_stock_source=source or "未记录",
                security_master_date=str(security_listed),
                security_master_source="security_master",
            )
            stages["daily_bar"] = _stage("not_started", "上市日期冲突，行情尚未开始")
            raise RuntimeError(
                f"上市日期来源冲突：{source or 'managed_stock'}={listed}，"
                f"security_master={security_listed}；未自动覆盖"
            )
        evidence: dict[str, Any] | None = None
        if listed and source:
            evidence = asset["coverage"].get("listing_evidence") or {
                "source": source,
                "symbol": spec["symbol"],
                "listing_date": str(listed),
                "checked_at": None,
                "cache_status": "persisted",
                "fallback_used": False,
            }
            stages["listing_date"] = _stage(
                "skipped",
                "已有可靠上市日期，本次跳过联网查询",
                **evidence,
            )
        if listed is None:
            with self.transaction() as connection:
                self.fence(connection, task_id, owner, spec)
                listed = security_listed
                if listed:
                    evidence = {
                        "source": "security_master",
                        "symbol": spec["symbol"],
                        "listing_date": str(listed),
                        "checked_at": datetime.now(UTC).isoformat(),
                        "cache_status": "persisted",
                        "fallback_used": False,
                    }
                    coverage = asset["coverage"]
                    coverage["listing_evidence"] = evidence
                    connection.execute(
                        """UPDATE managed_stock SET listing_date=?, listing_source='security_master',
                           coverage_json=? WHERE security_id=?""",
                        [listed, encoded(coverage), spec["security_id"]],
                    )
                    stages["listing_date"] = _stage(
                        "skipped", "证券主数据已有可靠上市日期", **evidence
                    )
        if listed is None and hasattr(provider, "get_listing_date"):
            stages["listing_date"] = _stage("running", "正在查询可靠上市日期来源")
            try:
                listed, source = provider.get_listing_date(spec["symbol"])
                if not isinstance(listed, date) or listed > market_today() or not source:
                    raise ValueError("invalid provider listing-date evidence")
            except Exception as exc:
                stages["listing_date"] = _stage(
                    "failed",
                    "所有可靠上市日期来源均不可用或证据无效",
                    error=str(exc)[:1200],
                )
                stages["daily_bar"] = _stage(
                    "not_started", "上市日期尚未核验，行情下载尚未开始"
                )
                raise RuntimeError(f"上市日期查询失败，行情下载尚未开始：{exc}") from exc
            evidence = dict(getattr(provider, "last_listing_evidence", {}) or {})
            evidence.update(
                {
                    "source": source,
                    "symbol": spec["symbol"],
                    "listing_date": str(listed),
                    "checked_at": evidence.get("checked_at") or datetime.now(UTC).isoformat(),
                    "cache_status": evidence.get("cache_status", "unknown"),
                    "fallback_used": bool(evidence.get("fallback_used", False)),
                }
            )
            with self.transaction() as connection:
                self.fence(connection, task_id, owner, spec)
                coverage = self._get(connection, spec["security_id"])["coverage"]
                coverage["listing_evidence"] = evidence
                connection.execute(
                    """UPDATE managed_stock SET listing_date=?, listing_source=?, coverage_json=?
                       WHERE security_id=?""",
                    [listed, source, encoded(coverage), spec["security_id"]],
                )
                self.audit(
                    connection,
                    "managed_stock_listing_date_verified",
                    spec["security_id"],
                    spec["actor"],
                    task_id=task_id,
                    **evidence,
                )
            stages["listing_date"] = _stage(
                "succeeded", "上市日期核验完成", **evidence
            )
        elif listed is None:
            stages["listing_date"] = _stage(
                "failed", "当前数据提供方不支持可靠上市日期查询"
            )
            stages["daily_bar"] = _stage("not_started", "上市日期未知，行情尚未开始")
            raise RuntimeError("上市日期查询失败，行情下载尚未开始：提供方不支持可靠来源")
        if listed:
            start = max(start, listed)
        if start > end:
            stages["daily_bar"] = _stage("failed", "目标范围内尚无完整交易日")
            raise ValueError("尚无完整交易日可同步")
        stages["daily_bar"] = _stage("running", "正在同步不复权和前复权日线")
        self._progress(task_id, owner, 10, "正在准备交易日历与行情分区")
        guarded = _FencedDatabase(self, task_id, owner, spec)
        prior_gap_verification_completed = bool(spec.get("preserve_classified_gaps"))
        no_redownload = {
            (adjustment, date.fromisoformat(item["date"]))
            for adjustment, detail in asset["coverage"].get("adjustments", {}).items()
            for item in detail.get("gap_details", [])
            if (
                item.get("classification") in {"verified_suspension", "calendar_mismatch"}
                or (
                    item.get("classification") == "unverified_gap"
                    and prior_gap_verification_completed
                )
            )
            and item.get("date")
        }

        def needed(partition):
            a, b = partition["start_date"], partition["end_date"]
            with guarded.connect(read_only=True) as connection:
                expected = {
                    row[0]
                    for row in connection.execute(
                        """SELECT trade_date FROM trading_calendar WHERE exchange=? AND is_open
                       AND trade_date BETWEEN ? AND ?""",
                        [asset["exchange"], a, b],
                    ).fetchall()
                }
                observed = {
                    row[0]
                    for row in connection.execute(
                        """SELECT trade_date FROM published_daily_bar WHERE security_id=?
                       AND adjustment=? AND trade_date BETWEEN ? AND ?""",
                        [spec["security_id"], partition["adjustment"], a, b],
                    ).fetchall()
                }
            # qfq is mutable after corporate actions: refresh all stored qfq years
            # on an explicit refresh/new end date instead of mixing adjustment bases.
            refresh_qfq = partition["adjustment"] == "qfq" and asset["availability"] == "ready"
            unresolved_for_download = {
                day
                for day in expected - observed
                if (partition["adjustment"], day) not in no_redownload
            }
            known_non_download_gap = bool(expected - observed) and not unresolved_for_download
            return (
                bool(unresolved_for_download)
                or not expected
                or refresh_qfq
                or (b >= end - timedelta(days=7) and not known_non_download_gap)
            )

        def progress(index, total):
            self._progress(
                task_id,
                owner,
                10 + int((60 if spec.get("include_announcements") else 80) * index / total),
                f"历史行情分区 {index}/{total}",
            )

        market = MarketDataSyncService(
            database=guarded,
            layout=self.database.layout,
            provider=_FreshProvider(provider, guarded, spec["security_id"]),
            rate_limit_seconds=max(0.2, self.settings.data_sync_rate_limit_seconds),
            partition_selector=needed,
            raw_namespace=f"{task_id}-{owner}",
            progress_callback=progress,
        )
        try:
            result = market.run(
                DataSyncSpec(
                    start_date=start,
                    end_date=end,
                    symbols=(spec["symbol"],),
                    adjustments=ADJUSTMENTS,
                    resume=False,
                )
            )
        except Exception as exc:
            stages["daily_bar"] = _stage(
                "failed", "行情同步失败", error=str(exc)[:1200]
            )
            raise
        if result["status"] != "published":
            stages["daily_bar"] = _stage(
                "failed",
                "行情分区未全部发布",
                errors=result.get("errors", [])[:10],
            )
            raise RuntimeError(encoded(result.get("errors", [])))
        counts = result.get("partitions", {}) or {}
        stages["daily_bar"] = _stage(
            "succeeded",
            "不复权和前复权日线同步完成",
            completed_partitions=counts.get("published"),
            failed_partitions=counts.get("failed", 0),
            pending_partitions=counts.get("planned", 0),
            total_partitions=sum(counts.values()) or None,
        )

    def _download_announcements(
        self, task_id: str, owner: str, spec: dict[str, Any]
    ) -> dict[str, Any]:
        self._progress(task_id, owner, 75, "正在同步定期公告（不下载 PDF 或财务指标）")
        guarded = _FencedDatabase(self, task_id, owner, spec)
        with guarded.connect(read_only=True):
            pass  # Check generation before starting any independent network phase.
        provider = self.provider_factory(f"{task_id}-{owner}-announcements")
        service = ResearchDatasetSyncService(
            database=guarded,
            layout=self.database.layout,
            provider=_FreshProvider(provider, guarded, spec["security_id"]),
            rate_limit_seconds=max(0.2, self.settings.data_sync_rate_limit_seconds),
            raw_namespace=f"{task_id}-{owner}",
            progress_callback=lambda index, total: self._progress(
                task_id, owner, 75 + int(19 * index / total), f"定期公告分区 {index}/{total}"
            ),
        )
        result = service.sync_announcements(
            AnnouncementSyncSpec(
                start_date=date.fromisoformat(spec["start_date"]),
                end_date=date.fromisoformat(spec["end_date"]),
                symbols=(spec["symbol"],),
                report_types=("q1", "interim", "q3", "annual"),
                resume=bool(spec.get("resume_failed_stages")),
            )
        )
        return {
            key: result.get(key)
            for key in (
                "status",
                "batch_id",
                "row_count",
                "partitions",
                "errors",
                "error",
                "paused_after_provider_failure",
            )
        }


class _FencedDatabase:
    """Fence every existing repository write under its normal shared DB lock.

    No nested transactions: SyncRepository retains ownership of partition
    BEGIN/COMMIT; the shared connection lock closes the check-to-write race.
    """

    def __init__(self, service, task_id, owner, spec):
        self.service, self.task_id, self.owner, self.spec = service, task_id, owner, spec

    def __getattr__(self, name):
        return getattr(self.service.database, name)

    @contextmanager
    def connect(self, *, read_only=False):
        with self.service.database.connect(read_only=read_only) as connection:
            self.service.fence(connection, self.task_id, self.owner, self.spec)
            yield connection


class _FreshProvider:
    def __init__(self, provider, database, security_id):
        self.provider = provider
        self.name = provider.name
        self.database, self.security_id = database, security_id

    def get_trading_calendar(self):
        frame = self.provider.get_trading_calendar()
        if frame.attrs.get("cache_status") == "stale_fallback":
            raise ValueError("交易日历刷新失败，拒绝过期缓存")
        return frame

    def get_report_announcements(self, *args):
        frame = self.provider.get_report_announcements(*args)
        if frame.attrs.get("cache_status") == "stale_fallback":
            raise ValueError("公告刷新失败，拒绝以过期缓存标记更新成功")
        return frame

    def get_daily_bars(self, *args):
        import pandas as pd

        frame = self.provider.get_daily_bars(*args)
        if frame.attrs.get("cache_status") == "stale_fallback":
            raise ValueError("行情刷新失败，拒绝以过期缓存标记更新成功")
        _, start, end, adjustment = args
        with self.database.connect(read_only=True) as connection:
            previous = {
                row[0]
                for row in connection.execute(
                    """SELECT trade_date FROM published_daily_bar WHERE security_id=?
                   AND adjustment=? AND trade_date BETWEEN ? AND ?""",
                    [self.security_id, adjustment, start, end],
                ).fetchall()
            }
        received = set(pd.to_datetime(frame["trade_date"]).dt.date) if not frame.empty else set()
        if previous - received:
            raise ValueError("刷新结果遗漏已发布交易日；拒绝覆盖已有有效行情")
        return frame
