from __future__ import annotations

import json
import re
from contextlib import ExitStack, contextmanager
from datetime import UTC, date, datetime, timedelta
from threading import Event, Thread
from typing import Any
from uuid import uuid4

from ai_quant_assistant.config import Settings
from ai_quant_assistant.domain.models import DataSyncSpec
from ai_quant_assistant.providers.akshare_provider import AkShareProvider
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.locks import InterProcessFileLock
from ai_quant_assistant.storage.managed_coverage import (
    ADJUSTMENTS,
    completed_date,
    coverage_report,
    market_today,
)
from ai_quant_assistant.storage.sync_service import MarketDataSyncService
from ai_quant_assistant.storage.task_repository import TASK_COLUMNS, OperationTaskRepository

LEASE_SECONDS = 1800


class ManagedStockConflict(RuntimeError):
    pass


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


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
    ) -> tuple[dict[str, Any], bool]:
        end = end or market_today()
        if confirm is not True:
            raise ValueError("请明确确认添加并下载行情")
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
            if (
                exists
                and (not same_range or auto_update != asset["auto_update"])
                and expected_revision is None
            ):
                raise ManagedStockConflict("已纳入管理；修改目标范围需携带 expected_revision")
            if asset["task"] and asset["task"]["status"] in {"queued", "running"}:
                if not same_range:
                    raise ManagedStockConflict("当前有同步任务，先等待完成或移出管理")
            elif (
                same_range
                and asset["availability"] == "ready"
                and not refresh
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
            }
            task, created = self.tasks.create_or_get_active(
                "sync",
                claim_key=f"managed-stock:{security_id}",
                spec=spec,
                message="等待单股历史行情回填",
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
        if spec.get("dataset") != "managed_stock":
            raise ValueError("not a managed stock task")
        owner = f"managed-worker-{uuid4().hex}"
        if not self.tasks.start(task_id, owner_id=owner, message="正在回填单股历史行情"):
            return self.tasks.get(task_id)
        stop = Event()

        def heartbeat():
            while not stop.wait(15):
                try:
                    if not self.tasks.progress(
                        task_id, owner_id=owner, progress=5, message="历史行情同步中"
                    ):
                        return
                except Exception:
                    return

        thread = Thread(target=heartbeat, daemon=True)
        thread.start()
        error = None
        try:
            self._download(task_id, owner, spec)
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
                success = error is None and report["availability"] == "ready"
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
                    [availability, encoded(report), success, spec["security_id"]],
                )
                connection.execute(
                    """UPDATE operation_task SET status=?, progress=?, message=?, result_json=?, error=?,
                       completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP,
                       heartbeat_at=CURRENT_TIMESTAMP, claim_key=NULL WHERE task_id=? AND owner_id=?""",
                    [
                        "succeeded" if success else "failed",
                        100 if success else 99,
                        "历史行情已就绪" if success else "同步未完整完成，可安全重试",
                        encoded(report) if success else None,
                        error,
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

    def _download(self, task_id: str, owner: str, spec: dict[str, Any]) -> None:
        provider = self.provider_factory(f"{task_id}-{owner}")
        start = date.fromisoformat(spec["start_date"])
        end = completed_date(date.fromisoformat(spec["end_date"]))
        asset = self.get(spec["security_id"])
        listed = date.fromisoformat(asset["listing_date"]) if asset["listing_date"] else None
        if listed is None:
            with self.transaction() as connection:
                self.fence(connection, task_id, owner, spec)
                listed = connection.execute(
                    "SELECT list_date FROM security_master WHERE security_id=?",
                    [spec["security_id"]],
                ).fetchone()[0]
                if listed:
                    connection.execute(
                        "UPDATE managed_stock SET listing_date=?, listing_source='security_master' WHERE security_id=?",
                        [listed, spec["security_id"]],
                    )
        if listed is None and hasattr(provider, "get_listing_date"):
            listed, source = provider.get_listing_date(spec["symbol"])
            if not isinstance(listed, date) or listed > market_today() or not source:
                raise ValueError("invalid provider listing-date evidence")
            with self.transaction() as connection:
                self.fence(connection, task_id, owner, spec)
                connection.execute(
                    "UPDATE managed_stock SET listing_date=?, listing_source=? WHERE security_id=?",
                    [listed, source, spec["security_id"]],
                )
        if listed:
            start = max(start, listed)
        if start > end:
            raise ValueError("尚无完整交易日可同步")
        guarded = _FencedDatabase(self, task_id, owner, spec)

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
            return (
                bool(expected - observed)
                or not expected
                or refresh_qfq
                or b >= end - timedelta(days=7)
            )

        def progress(index, total):
            if not self.tasks.progress(
                task_id,
                owner_id=owner,
                progress=10 + int(80 * index / total),
                message=f"历史行情分区 {index}/{total}",
            ):
                raise ManagedStockConflict("worker no longer owns the task")

        market = MarketDataSyncService(
            database=guarded,
            layout=self.database.layout,
            provider=_FreshProvider(provider, guarded, spec["security_id"]),
            rate_limit_seconds=max(0.2, self.settings.data_sync_rate_limit_seconds),
            partition_selector=needed,
            raw_namespace=f"{task_id}-{owner}",
            progress_callback=progress,
        )
        result = market.run(
            DataSyncSpec(
                start_date=start,
                end_date=end,
                symbols=(spec["symbol"],),
                adjustments=ADJUSTMENTS,
                resume=False,
            )
        )
        if result["status"] != "published":
            raise RuntimeError(encoded(result.get("errors", [])))


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
