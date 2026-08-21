from __future__ import annotations

import json
import logging
import shutil
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from ai_quant_assistant.config import Settings
from ai_quant_assistant.storage.database import LocalDatabase

logger = logging.getLogger(__name__)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC).isoformat() if value.tzinfo is None else value.isoformat()
    return str(value)


class OperationAuditRepository:
    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    def record(
        self,
        action: str,
        *,
        actor: str = "system",
        outcome: str = "success",
        remote_host: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.database.initialize()
        event_id = f"audit-{uuid4().hex}"
        safe_details = {
            key: value
            for key, value in (details or {}).items()
            if not any(secret in key.lower() for secret in ("password", "secret", "token"))
        }
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO operation_audit (
                    event_id, action, actor, outcome, remote_host,
                    object_type, object_id, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    event_id,
                    action,
                    actor,
                    outcome,
                    remote_host,
                    object_type,
                    object_id,
                    json.dumps(safe_details, ensure_ascii=False, default=str),
                ],
            )
        return {"event_id": event_id, "action": action, "outcome": outcome}

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not self.database.path.is_file():
            return []
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT event_id, action, actor, outcome, remote_host,
                       object_type, object_id, details_json, created_at
                FROM operation_audit
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [max(1, min(limit, 500))],
            ).fetchall()
        return [
            {
                "event_id": row[0],
                "action": row[1],
                "actor": row[2],
                "outcome": row[3],
                "remote_host": row[4],
                "object_type": row[5],
                "object_id": row[6],
                "details": json.loads(row[7]),
                "created_at": _iso(row[8]),
            }
            for row in rows
        ]


class OperationAlertRepository:
    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    def upsert(self, finding: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        self.database.initialize()
        alert_id = f"alert-{uuid4().hex}"
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT alert_id, status FROM operation_alert WHERE alert_key = ?",
                [finding["key"]],
            ).fetchone()
            is_new = existing is None or existing[1] == "resolved"
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO operation_alert (
                        alert_id, alert_key, severity, category, title,
                        message, status, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
                    """,
                    [
                        alert_id,
                        finding["key"],
                        finding["severity"],
                        finding["category"],
                        finding["title"],
                        finding["message"],
                        json.dumps(finding.get("details", {}), ensure_ascii=False, default=str),
                    ],
                )
            else:
                alert_id = str(existing[0])
                connection.execute(
                    """
                    UPDATE operation_alert
                    SET severity = ?, title = ?, message = ?, details_json = ?,
                        last_seen_at = CURRENT_TIMESTAMP,
                        status = CASE WHEN status = 'resolved' THEN 'active' ELSE status END,
                        resolved_at = CASE WHEN status = 'resolved' THEN NULL ELSE resolved_at END,
                        notified_at = CASE WHEN status = 'resolved' THEN NULL ELSE notified_at END
                    WHERE alert_id = ?
                    """,
                    [
                        finding["severity"],
                        finding["title"],
                        finding["message"],
                        json.dumps(finding.get("details", {}), ensure_ascii=False, default=str),
                        alert_id,
                    ],
                )
        return self.get(alert_id), is_new

    def get(self, alert_id: str) -> dict[str, Any]:
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT alert_id, alert_key, severity, category, title, message, status,
                       details_json, first_seen_at, last_seen_at,
                       acknowledged_at, resolved_at, notified_at
                FROM operation_alert WHERE alert_id = ?
                """,
                [alert_id],
            ).fetchone()
        if row is None:
            raise KeyError(alert_id)
        return self._decode(row)

    def list(self, *, active_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        if not self.database.path.is_file():
            return []
        condition = "WHERE status IN ('active', 'acknowledged')" if active_only else ""
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT alert_id, alert_key, severity, category, title, message, status,
                       details_json, first_seen_at, last_seen_at,
                       acknowledged_at, resolved_at, notified_at
                FROM operation_alert {condition}
                ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END,
                         last_seen_at DESC
                LIMIT ?
                """,
                [max(1, min(limit, 500))],
            ).fetchall()
        return [self._decode(row) for row in rows]

    def acknowledge(self, alert_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            result = connection.execute(
                """
                UPDATE operation_alert
                SET status = 'acknowledged', acknowledged_at = CURRENT_TIMESTAMP
                WHERE alert_id = ? AND status = 'active'
                RETURNING alert_id
                """,
                [alert_id],
            ).fetchone()
        if result is None:
            raise KeyError(alert_id)
        return self.get(alert_id)

    def resolve_missing(self, active_keys: set[str]) -> int:
        current = self.list(active_only=True, limit=500)
        missing = [item["alert_id"] for item in current if item["alert_key"] not in active_keys]
        if not missing:
            return 0
        with self.database.connect() as connection:
            for alert_id in missing:
                connection.execute(
                    """
                    UPDATE operation_alert
                    SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP
                    WHERE alert_id = ?
                    """,
                    [alert_id],
                )
        return len(missing)

    def mark_notified(self, alert_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE operation_alert SET notified_at = CURRENT_TIMESTAMP WHERE alert_id = ?",
                [alert_id],
            )

    @staticmethod
    def _decode(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "alert_id": row[0],
            "alert_key": row[1],
            "severity": row[2],
            "category": row[3],
            "title": row[4],
            "message": row[5],
            "status": row[6],
            "details": json.loads(row[7]),
            "first_seen_at": _iso(row[8]),
            "last_seen_at": _iso(row[9]),
            "acknowledged_at": _iso(row[10]),
            "resolved_at": _iso(row[11]),
            "notified_at": _iso(row[12]),
        }


class PlatformMonitor:
    def __init__(
        self,
        database: LocalDatabase,
        settings: Settings,
        alerts: OperationAlertRepository,
        audit: OperationAuditRepository,
    ) -> None:
        self.database = database
        self.settings = settings
        self.alerts = alerts
        self.audit = audit

    def check(self) -> dict[str, Any]:
        self.database.initialize()
        findings = self._findings()
        created: list[dict[str, Any]] = []
        for finding in findings:
            alert, is_new = self.alerts.upsert(finding)
            if is_new:
                created.append(alert)
                logger.warning("platform alert [%s] %s", alert["severity"], alert["message"])
                self._notify(alert)
        resolved = self.alerts.resolve_missing({item["key"] for item in findings})
        return {
            "status": "critical"
            if any(item["severity"] == "critical" for item in findings)
            else "warning"
            if findings
            else "healthy",
            "checked_at": datetime.now(UTC).isoformat(),
            "active_count": len(findings),
            "new_count": len(created),
            "resolved_count": resolved,
            "alerts": self.alerts.list(active_only=True),
        }

    def quality(self) -> dict[str, Any]:
        if not self.database.path.is_file():
            return {"checks": [], "failed_partitions": [], "securities": []}
        with self.database.connect(read_only=True) as connection:
            checks = connection.execute(
                """
                SELECT severity, COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE NOT passed) AS failed
                FROM dq_result GROUP BY severity ORDER BY severity
                """
            ).fetchall()
            failures = connection.execute(
                """
                SELECT 'daily_bar', partition_key, security_id, error, updated_at
                FROM sync_partition WHERE status = 'failed'
                UNION ALL
                SELECT dataset, partition_key, object_id, error, updated_at
                FROM research_sync_partition WHERE status = 'failed'
                UNION ALL
                SELECT 'financial_metric', partition_key, security_id, error, updated_at
                FROM financial_sync_partition WHERE status = 'failed'
                ORDER BY 5 DESC LIMIT 50
                """
            ).fetchall()
            securities = connection.execute(
                """
                SELECT security.symbol, security.name,
                       MAX(bar.trade_date) AS latest_trade_date,
                       COUNT(DISTINCT bar.trade_date) AS price_rows,
                       MAX(report.report_period_end) AS latest_financial_period,
                       COUNT(DISTINCT report.report_id) AS financial_reports
                FROM security_master AS security
                LEFT JOIN published_daily_bar AS bar
                  ON bar.security_id = security.security_id AND bar.adjustment = 'qfq'
                LEFT JOIN published_financial_report AS report
                  ON report.security_id = security.security_id
                WHERE security.status = 'active'
                GROUP BY security.symbol, security.name
                ORDER BY security.symbol LIMIT 500
                """
            ).fetchall()
        return {
            "checks": [
                {"severity": row[0], "total": int(row[1]), "failed": int(row[2])}
                for row in checks
            ],
            "failed_partitions": [
                {
                    "dataset": row[0],
                    "partition_key": row[1],
                    "object_id": row[2],
                    "error": row[3],
                    "updated_at": _iso(row[4]),
                }
                for row in failures
            ],
            "securities": [
                {
                    "symbol": row[0],
                    "name": row[1],
                    "latest_trade_date": _iso(row[2]),
                    "price_rows": int(row[3]),
                    "latest_financial_period": _iso(row[4]),
                    "financial_reports": int(row[5]),
                }
                for row in securities
            ],
        }

    def _findings(self) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        findings: list[dict[str, Any]] = []
        with self.database.connect(read_only=True) as connection:
            failed_tasks = connection.execute(
                """
                SELECT task_type, error, completed_at
                FROM operation_task
                WHERE task_type IN ('sync', 'backup')
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY task_type ORDER BY created_at DESC
                ) = 1 AND status = 'failed'
                """
            ).fetchall()
            latest_sync = connection.execute(
                "SELECT MAX(ended_at) FROM sync_batch WHERE status = 'published'"
            ).fetchone()[0]
            latest_backup = connection.execute(
                "SELECT MAX(created_at) FROM database_backup WHERE deleted_at IS NULL"
            ).fetchone()[0]
            login_failures = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM operation_audit
                    WHERE action IN ('login_failed', 'login_rate_limited')
                      AND created_at >= ?
                    """,
                    [now - timedelta(hours=1)],
                ).fetchone()[0]
            )
            blocking_failures = int(
                connection.execute(
                    "SELECT COUNT(*) FROM dq_result WHERE severity = 'blocking' AND NOT passed"
                ).fetchone()[0]
            )
        for task_type, error, _ in failed_tasks:
            label = "增量同步" if task_type == "sync" else "数据库备份"
            findings.append(
                self._finding(
                    f"task_failed:{task_type}",
                    "critical",
                    task_type,
                    f"{label}执行失败",
                    str(error or f"最近一次{label}任务失败"),
                )
            )
        self._check_stale(
            findings,
            now,
            latest_sync,
            self.settings.alert_sync_stale_hours,
            "sync_stale",
            "数据同步长时间未更新",
        )
        self._check_stale(
            findings,
            now,
            latest_backup,
            self.settings.alert_backup_stale_hours,
            "backup_stale",
            "数据库备份长时间未更新",
        )
        usage = shutil.disk_usage(self.database.path.parent)
        free_percent = usage.free * 100 / usage.total if usage.total else 0
        if free_percent < self.settings.alert_disk_free_percent:
            findings.append(
                self._finding(
                    "disk_space_low",
                    "critical",
                    "disk",
                    "数据库磁盘空间不足",
                    f"剩余磁盘空间 {free_percent:.1f}%，低于设定阈值",
                    {"free_percent": round(free_percent, 2), "free_bytes": usage.free},
                )
            )
        if login_failures >= self.settings.alert_login_failure_threshold:
            findings.append(
                self._finding(
                    "login_failures",
                    "warning",
                    "security",
                    "管理员登录失败次数过多",
                    f"过去一小时共有 {login_failures} 次失败登录",
                    {"failure_count": login_failures},
                )
            )
        if blocking_failures:
            findings.append(
                self._finding(
                    "blocking_quality_failures",
                    "critical",
                    "data_quality",
                    "存在阻断级数据质量异常",
                    f"共有 {blocking_failures} 条阻断级质量规则未通过",
                    {"failure_count": blocking_failures},
                )
            )
        return findings

    @staticmethod
    def _check_stale(
        findings: list[dict[str, Any]],
        now: datetime,
        latest: datetime | None,
        hours: int,
        key: str,
        title: str,
    ) -> None:
        if latest is None:
            return
        normalized = latest.replace(tzinfo=UTC) if latest.tzinfo is None else latest
        age_hours = (now - normalized).total_seconds() / 3600
        if age_hours > max(1, hours):
            findings.append(
                PlatformMonitor._finding(
                    key,
                    "warning",
                    "sync" if key.startswith("sync") else "backup",
                    title,
                    f"最近成功记录距今 {age_hours:.1f} 小时，超过 {hours} 小时阈值",
                    {"age_hours": round(age_hours, 2), "latest_at": _iso(normalized)},
                )
            )

    @staticmethod
    def _finding(
        key: str,
        severity: str,
        category: str,
        title: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "key": key,
            "severity": severity,
            "category": category,
            "title": title,
            "message": message,
            "details": details or {},
        }

    def _notify(self, alert: dict[str, Any]) -> None:
        webhook = self.settings.alert_webhook_url.strip()
        if not webhook:
            return
        parsed = urlparse(webhook)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            logger.error("alert webhook must use HTTPS unless it targets localhost")
            return
        body = json.dumps(
            {"event": "ai_quant_alert", "alert": alert}, ensure_ascii=False, default=str
        ).encode()
        request = urllib.request.Request(
            webhook,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.alert_webhook_timeout_seconds
            ):
                self.alerts.mark_notified(alert["alert_id"])
        except (OSError, ValueError, urllib.error.URLError):
            logger.exception("failed to deliver platform alert webhook")
