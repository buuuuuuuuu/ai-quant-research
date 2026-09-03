from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from ai_quant_assistant.storage.database import LocalDatabase

MAX_POOL_MEMBERS = 500
MAX_BATCH_MEMBERS = 200
MANUAL_HISTORY_WARNING = (
    "手工维护的当前股票池不代表历史时点真实成分，历史研究可能存在幸存者偏差。"
)


class StockPoolConflictError(RuntimeError):
    """Raised when optimistic concurrency detects a stale revision."""


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _symbols_hash(symbols: list[str]) -> str:
    payload = json.dumps(symbols, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class StockPoolService:
    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    def list(self, *, include_archived: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        self.database.initialize()
        where = "" if include_archived else "WHERE pool.status = 'active'"
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT pool.pool_id, pool.name, pool.description, pool.source_type,
                       pool.source_screen_run_id, pool.as_of, pool.history_verified,
                       pool.warning, pool.status, pool.is_default, pool.current_revision,
                       pool.created_at, pool.updated_at, COUNT(member.security_id)
                FROM stock_pool AS pool
                LEFT JOIN stock_pool_member AS member USING (pool_id)
                {where}
                GROUP BY ALL
                ORDER BY pool.is_default DESC, pool.updated_at DESC, pool.name
                LIMIT ?
                """,
                [max(1, min(limit, 200))],
            ).fetchall()
        return [self._pool_row(row) for row in rows]

    def get(self, pool_id: str, *, revision: int | None = None) -> dict[str, Any]:
        self.database.initialize()
        with self.database.connect(read_only=True) as connection:
            return self._get(connection, pool_id, revision=revision)

    def resolve(self, pool_id: str | None = None, *, revision: int | None = None) -> dict[str, Any]:
        self.database.initialize()
        with self.database.connect(read_only=True) as connection:
            if pool_id is None:
                row = connection.execute(
                    "SELECT pool_id FROM stock_pool WHERE is_default = TRUE AND status = 'active'"
                ).fetchone()
                if row is None:
                    raise KeyError("default stock pool")
                pool_id = str(row[0])
            pool = self._get(connection, pool_id, revision=revision)
        if pool["status"] == "archived" and revision is None:
            raise ValueError("archived stock pool requires an explicit historical revision")
        resolved_revision = revision or int(pool["current_revision"])
        return {
            "pool_id": pool["pool_id"],
            "pool_revision": resolved_revision,
            "resolved_symbols": list(pool["symbols"]),
            "symbols_sha256": pool["symbols_sha256"],
            "resolved_at": datetime.now(UTC).isoformat(),
            "source_type": pool["source_type"],
            "as_of": pool["as_of"],
            "history_verified": pool["history_verified"],
            "warning": pool["warning"],
        }

    def search_securities(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        normalized = query.strip()
        if not normalized:
            return []
        self.database.initialize()
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                WITH matched AS (
                SELECT security.security_id, security.symbol, security.name,
                       security.exchange, security.status,
                       MIN(bar.trade_date) AS start_date, MAX(bar.trade_date) AS end_date,
                       COUNT(bar.trade_date) AS price_rows
                FROM security_master AS security
                LEFT JOIN published_daily_bar AS bar
                  ON bar.security_id = security.security_id AND bar.adjustment = 'qfq'
                WHERE security.symbol ILIKE ? OR security.name ILIKE ?
                GROUP BY security.security_id, security.symbol, security.name,
                         security.exchange, security.status
                )
                SELECT * FROM matched
                ORDER BY CASE WHEN symbol = ? THEN 0 ELSE 1 END, symbol
                LIMIT ?
                """,
                [f"%{normalized}%", f"%{normalized}%", normalized, max(1, min(limit, 50))],
            ).fetchall()
            managed = dict(connection.execute(
                "SELECT security_id, status || ':' || availability FROM managed_stock"
            ).fetchall())
        results = []
        for row in rows:
            item = self._security_row(row)
            item["management_state"] = managed.get(item["security_id"], "not_managed")
            item["eligible"] = item["status"] == "active" and item["management_state"] == "active:ready"
            item["admission_warning"] = None if item["eligible"] else "请先在数据浏览中添加并补齐行情"
            results.append(item)
        return results

    def create(
        self,
        name: str,
        *,
        description: str = "",
        symbols: list[str] | tuple[str, ...] = (),
        source_type: str = "manual",
        source_screen_run_id: str | None = None,
        as_of: date | datetime | str | None = None,
        history_verified: bool = False,
        warning: str | None = None,
        actor: str = "system",
        pool_id: str | None = None,
        created_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        normalized_name = self._name(name)
        normalized_description = self._description(description)
        if source_type not in {"system_default", "manual", "screen_snapshot", "imported"}:
            raise ValueError("unsupported stock pool source_type")
        normalized = self._normalize_symbols(symbols)
        limit = MAX_POOL_MEMBERS if source_type in {"system_default", "screen_snapshot"} else 200
        if len(normalized) > limit:
            raise ValueError(f"stock pool creation accepts at most {limit} symbols")
        if source_type == "screen_snapshot" and source_screen_run_id is None:
            raise ValueError("screen_snapshot stock pool requires source_screen_run_id")
        if not history_verified and not warning and source_type in {"manual", "imported"}:
            warning = MANUAL_HISTORY_WARNING
        self.database.initialize()
        pool_id = pool_id or f"pool-{uuid4().hex}"
        with self.database.connect() as connection:
            self._begin(connection)
            try:
                securities = self._validate_symbols(connection, normalized)
                timestamp = created_at or datetime.now(UTC)
                connection.execute(
                    """
                    INSERT INTO stock_pool (
                        pool_id, name, description, source_type, source_screen_run_id,
                        as_of, history_verified, warning, status, is_default,
                        current_revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', FALSE, 1, ?, ?)
                    """,
                    [
                        pool_id,
                        normalized_name,
                        normalized_description,
                        source_type,
                        source_screen_run_id,
                        as_of,
                        history_verified,
                        warning,
                        timestamp,
                        timestamp,
                    ],
                )
                self._insert_members(connection, pool_id, securities, timestamp=timestamp)
                self._write_revision(
                    connection,
                    pool_id,
                    1,
                    normalized,
                    "created",
                    f"创建股票池，包含 {len(normalized)} 只股票",
                    created_at=timestamp,
                )
                self._audit(
                    connection,
                    "stock_pool_created",
                    actor,
                    pool_id,
                    revision=1,
                    change_count=len(normalized),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get(pool_id)

    def update(
        self,
        pool_id: str,
        *,
        expected_revision: int,
        name: str | None = None,
        description: str | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        if name is None and description is None:
            return self.get(pool_id)
        self.database.initialize()
        with self.database.connect() as connection:
            self._begin(connection)
            try:
                pool = self._require_current(connection, pool_id, expected_revision)
                self._require_active(pool)
                next_name = self._name(name) if name is not None else str(pool[1])
                next_description = (
                    self._description(description) if description is not None else str(pool[2])
                )
                if next_name == pool[1] and next_description == pool[2]:
                    connection.execute("COMMIT")
                    return self._get(connection, pool_id, revision=None)
                revision = expected_revision + 1
                connection.execute(
                    """
                    UPDATE stock_pool SET name = ?, description = ?, current_revision = ?,
                           updated_at = CURRENT_TIMESTAMP
                    WHERE pool_id = ? AND current_revision = ?
                    """,
                    [next_name, next_description, revision, pool_id, expected_revision],
                )
                symbols = self._current_symbols(connection, pool_id)
                self._write_revision(
                    connection, pool_id, revision, symbols, "metadata_updated", "修改名称或描述"
                )
                self._audit(connection, "stock_pool_updated", actor, pool_id, revision=revision)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get(pool_id)

    def add_members(
        self,
        pool_id: str,
        symbols: list[str] | tuple[str, ...],
        *,
        expected_revision: int,
        actor: str = "system",
    ) -> dict[str, Any]:
        normalized = self._normalize_symbols(symbols)
        if len(normalized) > MAX_BATCH_MEMBERS:
            raise ValueError("a single batch accepts at most 200 symbols")
        self.database.initialize()
        with self.database.connect() as connection:
            self._begin(connection)
            try:
                pool = self._require_current(connection, pool_id, expected_revision)
                self._require_active(pool)
                existing = set(self._current_symbols(connection, pool_id))
                securities = self._validate_symbols(
                    connection, [symbol for symbol in normalized if symbol not in existing]
                )
                additions = [item for item in securities if item["symbol"] not in existing]
                if not additions:
                    connection.execute("COMMIT")
                    return self._get(connection, pool_id, revision=None)
                if len(existing) + len(additions) > MAX_POOL_MEMBERS:
                    raise ValueError("a stock pool may contain at most 500 members")
                start = len(existing) + 1
                self._insert_members(connection, pool_id, additions, start_order=start)
                revision = expected_revision + 1
                symbols_after = self._current_symbols(connection, pool_id)
                self._advance(
                    connection,
                    pool_id,
                    expected_revision,
                    revision,
                    symbols_after,
                    "members_added",
                    f"添加 {len(additions)} 只股票",
                )
                self._audit(
                    connection,
                    "stock_pool_member_added",
                    actor,
                    pool_id,
                    revision=revision,
                    change_count=len(additions),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get(pool_id)

    def update_member(
        self,
        pool_id: str,
        security_id: str,
        *,
        expected_revision: int,
        note: str | None = None,
        display_order: int | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        if note is not None and len(note) > 500:
            raise ValueError("note must contain at most 500 characters")
        if display_order is not None and display_order < 1:
            raise ValueError("display_order must be positive")
        self.database.initialize()
        with self.database.connect() as connection:
            self._begin(connection)
            try:
                pool = self._require_current(connection, pool_id, expected_revision)
                self._require_active(pool)
                current = connection.execute(
                    """
                    SELECT note, display_order FROM stock_pool_member
                    WHERE pool_id = ? AND security_id = ?
                    """,
                    [pool_id, security_id],
                ).fetchone()
                if current is None:
                    raise KeyError(security_id)
                next_note = str(current[0]) if note is None else note.strip()
                next_order = int(current[1]) if display_order is None else display_order
                if next_note == current[0] and next_order == current[1]:
                    connection.execute("COMMIT")
                    return self._get(connection, pool_id, revision=None)
                connection.execute(
                    """
                    UPDATE stock_pool_member SET note = ?, display_order = ?,
                           updated_at = CURRENT_TIMESTAMP
                    WHERE pool_id = ? AND security_id = ?
                    """,
                    [next_note, next_order, pool_id, security_id],
                )
                revision = expected_revision + 1
                self._advance(
                    connection,
                    pool_id,
                    expected_revision,
                    revision,
                    self._current_symbols(connection, pool_id),
                    "member_updated",
                    f"修改成员 {security_id} 的备注或顺序",
                )
                self._audit(
                    connection, "stock_pool_updated", actor, pool_id, revision=revision
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get(pool_id)

    def remove_member(
        self,
        pool_id: str,
        security_id: str,
        *,
        expected_revision: int,
        actor: str = "system",
    ) -> dict[str, Any]:
        return self.remove_members(
            pool_id, [security_id], expected_revision=expected_revision, actor=actor
        )

    def remove_members(
        self,
        pool_id: str,
        security_ids: list[str] | tuple[str, ...],
        *,
        expected_revision: int,
        actor: str = "system",
    ) -> dict[str, Any]:
        ids = list(dict.fromkeys(item.strip() for item in security_ids if item.strip()))
        if not ids:
            raise ValueError("security_ids must not be empty")
        if len(ids) > MAX_BATCH_MEMBERS:
            raise ValueError("a single batch accepts at most 200 members")
        self.database.initialize()
        with self.database.connect() as connection:
            self._begin(connection)
            try:
                pool = self._require_current(connection, pool_id, expected_revision)
                self._require_active(pool)
                existing = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT security_id FROM stock_pool_member WHERE pool_id = ?",
                        [pool_id],
                    ).fetchall()
                }
                missing = [item for item in ids if item not in existing]
                if missing:
                    raise KeyError(missing[0])
                if bool(pool[8]) and len(existing) == len(ids):
                    raise ValueError("the default stock pool must retain at least one member")
                connection.execute(
                    "DELETE FROM stock_pool_member WHERE pool_id = ? AND security_id IN (SELECT * FROM UNNEST(?))",
                    [pool_id, ids],
                )
                revision = expected_revision + 1
                self._advance(
                    connection,
                    pool_id,
                    expected_revision,
                    revision,
                    self._current_symbols(connection, pool_id),
                    "members_removed",
                    f"移除 {len(ids)} 只股票",
                )
                self._audit(
                    connection,
                    "stock_pool_member_removed",
                    actor,
                    pool_id,
                    revision=revision,
                    change_count=len(ids),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get(pool_id)

    def set_default(
        self, pool_id: str, *, expected_revision: int, actor: str = "system"
    ) -> dict[str, Any]:
        self.database.initialize()
        with self.database.connect() as connection:
            self._begin(connection)
            try:
                target = self._require_current(connection, pool_id, expected_revision)
                self._require_active(target)
                if not self._current_symbols(connection, pool_id):
                    raise ValueError("the default stock pool must contain at least one member")
                if bool(target[8]):
                    connection.execute("COMMIT")
                    return self._get(connection, pool_id, revision=None)
                previous = connection.execute(
                    "SELECT pool_id, current_revision FROM stock_pool WHERE is_default = TRUE"
                ).fetchone()
                if previous is not None:
                    previous_id, previous_revision = str(previous[0]), int(previous[1])
                    connection.execute(
                        """
                        UPDATE stock_pool SET is_default = FALSE, current_revision = ?,
                               updated_at = CURRENT_TIMESTAMP WHERE pool_id = ?
                        """,
                        [previous_revision + 1, previous_id],
                    )
                    self._write_revision(
                        connection,
                        previous_id,
                        previous_revision + 1,
                        self._current_symbols(connection, previous_id),
                        "default_changed",
                        f"默认股票池切换为 {pool_id}",
                    )
                revision = expected_revision + 1
                connection.execute(
                    """
                    UPDATE stock_pool SET is_default = TRUE, current_revision = ?,
                           updated_at = CURRENT_TIMESTAMP WHERE pool_id = ?
                    """,
                    [revision, pool_id],
                )
                self._write_revision(
                    connection,
                    pool_id,
                    revision,
                    self._current_symbols(connection, pool_id),
                    "default_changed",
                    "设为默认股票池",
                )
                self._audit(
                    connection,
                    "stock_pool_default_changed",
                    actor,
                    pool_id,
                    revision=revision,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get(pool_id)

    def archive(
        self, pool_id: str, *, expected_revision: int, actor: str = "system"
    ) -> dict[str, Any]:
        self.database.initialize()
        with self.database.connect() as connection:
            self._begin(connection)
            try:
                pool = self._require_current(connection, pool_id, expected_revision)
                if pool[7] == "archived":
                    connection.execute("COMMIT")
                    return self._get(connection, pool_id, revision=None)
                if bool(pool[8]):
                    raise ValueError("the default stock pool cannot be archived")
                revision = expected_revision + 1
                connection.execute(
                    """
                    UPDATE stock_pool SET status = 'archived', current_revision = ?,
                           updated_at = CURRENT_TIMESTAMP WHERE pool_id = ?
                    """,
                    [revision, pool_id],
                )
                self._write_revision(
                    connection,
                    pool_id,
                    revision,
                    self._current_symbols(connection, pool_id),
                    "archived",
                    "归档股票池（历史引用保留）",
                )
                self._audit(
                    connection, "stock_pool_archived", actor, pool_id, revision=revision
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get(pool_id)

    def duplicate(
        self,
        pool_id: str,
        *,
        expected_revision: int,
        name: str,
        actor: str = "system",
    ) -> dict[str, Any]:
        normalized_name = self._name(name)
        self.database.initialize()
        new_id = f"pool-{uuid4().hex}"
        with self.database.connect() as connection:
            self._begin(connection)
            try:
                source = self._require_current(connection, pool_id, expected_revision)
                self._validate_symbols(connection, self._current_symbols(connection, pool_id))
                members = connection.execute(
                    """
                    SELECT security_id, display_order, note FROM stock_pool_member
                    WHERE pool_id = ? ORDER BY display_order, security_id
                    """,
                    [pool_id],
                ).fetchall()
                connection.execute(
                    """
                    INSERT INTO stock_pool (
                        pool_id, name, description, source_type, as_of,
                        history_verified, warning, status, is_default, current_revision
                    ) VALUES (?, ?, ?, 'manual', ?, FALSE, ?, 'active', FALSE, 1)
                    """,
                    [new_id, normalized_name, source[2], source[5], MANUAL_HISTORY_WARNING],
                )
                if members:
                    connection.executemany(
                        """
                        INSERT INTO stock_pool_member (pool_id, security_id, display_order, note)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(new_id, row[0], row[1], row[2]) for row in members],
                    )
                symbols = self._current_symbols(connection, new_id)
                self._write_revision(
                    connection,
                    new_id,
                    1,
                    symbols,
                    "duplicated",
                    f"复制自 {pool_id} revision {expected_revision}",
                )
                self._audit(
                    connection,
                    "stock_pool_duplicated",
                    actor,
                    new_id,
                    revision=1,
                    change_count=len(symbols),
                    source_pool_id=pool_id,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get(new_id)

    def revisions(self, pool_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        self.database.initialize()
        with self.database.connect(read_only=True) as connection:
            if not connection.execute(
                "SELECT 1 FROM stock_pool WHERE pool_id = ?", [pool_id]
            ).fetchone():
                raise KeyError(pool_id)
            rows = connection.execute(
                """
                SELECT pool_id, revision_no, symbols_json, symbols_sha256,
                       change_type, change_summary, created_at
                FROM stock_pool_revision WHERE pool_id = ?
                ORDER BY revision_no DESC LIMIT ?
                """,
                [pool_id, max(1, min(limit, 100))],
            ).fetchall()
        return [
            {
                "pool_id": row[0],
                "revision_no": int(row[1]),
                "symbols": json.loads(row[2]),
                "symbols_sha256": row[3],
                "change_type": row[4],
                "change_summary": row[5],
                "created_at": _iso(row[6]),
            }
            for row in rows
        ]

    @staticmethod
    def _begin(connection: Any) -> None:
        connection.execute("BEGIN TRANSACTION")

    @staticmethod
    def _name(value: str | None) -> str:
        normalized = (value or "").strip()
        if not normalized:
            raise ValueError("name must not be empty")
        if len(normalized) > 120:
            raise ValueError("name must contain at most 120 characters")
        return normalized

    @staticmethod
    def _description(value: str | None) -> str:
        normalized = (value or "").strip()
        if len(normalized) > 1000:
            raise ValueError("description must contain at most 1000 characters")
        return normalized

    @staticmethod
    def _normalize_symbols(symbols: list[str] | tuple[str, ...]) -> list[str]:
        normalized = list(dict.fromkeys(str(item).strip() for item in symbols if str(item).strip()))
        invalid = [item for item in normalized if len(item) != 6 or not item.isdigit()]
        if invalid:
            raise ValueError(f"symbols must contain six-digit A-share codes: {invalid}")
        return normalized

    @staticmethod
    def _require_active(pool: tuple[Any, ...]) -> None:
        if pool[7] == "archived":
            raise ValueError("archived stock pool cannot be updated")

    @staticmethod
    def _require_current(connection: Any, pool_id: str, expected: int) -> tuple[Any, ...]:
        row = connection.execute(
            """
            SELECT pool_id, name, description, source_type, source_screen_run_id,
                   as_of, history_verified, status, is_default, current_revision, warning
            FROM stock_pool WHERE pool_id = ?
            """,
            [pool_id],
        ).fetchone()
        if row is None:
            raise KeyError(pool_id)
        if int(row[9]) != int(expected):
            raise StockPoolConflictError(
                f"stock pool revision conflict: expected {expected}, current {row[9]}"
            )
        return row

    @staticmethod
    def _validate_symbols(connection: Any, symbols: list[str]) -> list[dict[str, Any]]:
        if not symbols:
            return []
        rows = connection.execute(
            """
            SELECT security_id, symbol, exchange, name, status
            FROM security_master WHERE symbol IN (SELECT * FROM UNNEST(?))
            """,
            [symbols],
        ).fetchall()
        by_symbol = {str(row[1]): row for row in rows}
        missing = [symbol for symbol in symbols if symbol not in by_symbol]
        if missing:
            raise ValueError(f"symbols missing from security_master: {missing}")
        inactive = [symbol for symbol in symbols if str(by_symbol[symbol][4]) != "active"]
        if inactive:
            raise ValueError(f"symbols are not active in security_master: {inactive}")
        managed = dict(connection.execute(
            """SELECT s.symbol, m.status || ':' || m.availability FROM managed_stock m
               JOIN security_master s USING(security_id)
               WHERE s.symbol IN (SELECT * FROM UNNEST(?))""", [symbols],
        ).fetchall())
        unavailable = {symbol: managed.get(symbol, "not_managed") for symbol in symbols
                       if managed.get(symbol) != "active:ready"}
        if unavailable:
            raise ValueError(
                "stocks are not ready in local data management; 请先在数据浏览中添加并补齐行情: "
                + json.dumps(unavailable, ensure_ascii=False)
            )
        return [
            {
                "security_id": str(by_symbol[symbol][0]),
                "symbol": symbol,
                "exchange": str(by_symbol[symbol][2]),
                "name": str(by_symbol[symbol][3]),
            }
            for symbol in symbols
        ]

    @staticmethod
    def _insert_members(
        connection: Any,
        pool_id: str,
        securities: list[dict[str, Any]],
        *,
        start_order: int = 1,
        timestamp: Any | None = None,
    ) -> None:
        if not securities:
            return
        timestamp = timestamp or datetime.now(UTC)
        connection.executemany(
            """
            INSERT INTO stock_pool_member (
                pool_id, security_id, display_order, note, added_at, updated_at
            ) VALUES (?, ?, ?, '', ?, ?)
            """,
            [
                (pool_id, item["security_id"], start_order + index, timestamp, timestamp)
                for index, item in enumerate(securities)
            ],
        )

    @staticmethod
    def _current_symbols(connection: Any, pool_id: str) -> list[str]:
        return [
            str(row[0])
            for row in connection.execute(
                """
                SELECT COALESCE(security.symbol, split_part(member.security_id, '.', 2))
                FROM stock_pool_member AS member
                LEFT JOIN security_master AS security USING (security_id)
                WHERE member.pool_id = ?
                ORDER BY member.display_order, member.security_id
                """,
                [pool_id],
            ).fetchall()
        ]

    @staticmethod
    def _write_revision(
        connection: Any,
        pool_id: str,
        revision: int,
        symbols: list[str],
        change_type: str,
        summary: str,
        *,
        created_at: Any | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO stock_pool_revision (
                pool_id, revision_no, symbols_json, symbols_sha256,
                change_type, change_summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                pool_id,
                revision,
                json.dumps(symbols, ensure_ascii=False),
                _symbols_hash(symbols),
                change_type,
                summary,
                created_at,
            ],
        )

    def _advance(
        self,
        connection: Any,
        pool_id: str,
        expected: int,
        revision: int,
        symbols: list[str],
        change_type: str,
        summary: str,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE stock_pool SET current_revision = ?, updated_at = CURRENT_TIMESTAMP
            WHERE pool_id = ? AND current_revision = ? RETURNING pool_id
            """,
            [revision, pool_id, expected],
        ).fetchone()
        if updated is None:
            raise StockPoolConflictError("stock pool was updated concurrently")
        self._write_revision(connection, pool_id, revision, symbols, change_type, summary)

    @staticmethod
    def _audit(
        connection: Any,
        action: str,
        actor: str,
        pool_id: str,
        *,
        revision: int,
        change_count: int = 0,
        **details: Any,
    ) -> None:
        payload = {"pool_id": pool_id, "revision": revision, "change_count": change_count}
        payload.update(details)
        connection.execute(
            """
            INSERT INTO operation_audit (
                event_id, action, actor, outcome, object_type, object_id, details_json
            ) VALUES (?, ?, ?, 'success', 'stock_pool', ?, ?)
            """,
            [
                f"audit-{uuid4().hex}",
                action,
                actor,
                pool_id,
                json.dumps(payload, ensure_ascii=False, default=str),
            ],
        )

    def _get(self, connection: Any, pool_id: str, *, revision: int | None) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT pool_id, name, description, source_type, source_screen_run_id,
                   as_of, history_verified, warning, status, is_default,
                   current_revision, created_at, updated_at
            FROM stock_pool WHERE pool_id = ?
            """,
            [pool_id],
        ).fetchone()
        if row is None:
            raise KeyError(pool_id)
        if revision is not None:
            revision_row = connection.execute(
                """
                SELECT symbols_json, symbols_sha256 FROM stock_pool_revision
                WHERE pool_id = ? AND revision_no = ?
                """,
                [pool_id, revision],
            ).fetchone()
            if revision_row is None:
                raise KeyError(f"{pool_id}@{revision}")
            symbols = json.loads(revision_row[0])
            members = self._members_for_symbols(connection, symbols)
            symbols_sha256 = str(revision_row[1])
        else:
            members = self._members(connection, pool_id)
            symbols = [item["symbol"] for item in members]
            revision_row = connection.execute(
                """
                SELECT symbols_sha256 FROM stock_pool_revision
                WHERE pool_id = ? AND revision_no = ?
                """,
                [pool_id, row[10]],
            ).fetchone()
            symbols_sha256 = str(revision_row[0]) if revision_row else _symbols_hash(symbols)
        payload = self._pool_row((*row, len(symbols)))
        payload.update(
            {
                "members": members,
                "symbols": symbols,
                "symbols_sha256": symbols_sha256,
                "resolved_revision": revision or int(row[10]),
            }
        )
        return payload

    @staticmethod
    def _pool_row(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "pool_id": str(row[0]),
            "name": str(row[1]),
            "description": str(row[2]),
            "source_type": str(row[3]),
            "source_screen_run_id": row[4],
            "as_of": _iso(row[5]),
            "history_verified": bool(row[6]),
            "warning": row[7],
            "status": str(row[8]),
            "is_default": bool(row[9]),
            "current_revision": int(row[10]),
            "created_at": _iso(row[11]),
            "updated_at": _iso(row[12]),
            "member_count": int(row[13]),
        }

    @staticmethod
    def _security_row(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "security_id": str(row[0]),
            "symbol": str(row[1]),
            "name": str(row[2]),
            "exchange": str(row[3]),
            "status": str(row[4]),
            "coverage": {
                "start_date": _iso(row[5]),
                "end_date": _iso(row[6]),
                "rows": int(row[7]),
            },
        }

    def _members(self, connection: Any, pool_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT member.security_id,
                   COALESCE(security.symbol, split_part(member.security_id, '.', 2)),
                   COALESCE(security.name, '证券主数据缺失'),
                   COALESCE(security.exchange, split_part(member.security_id, '.', 1)),
                   COALESCE(security.status, 'missing'), member.display_order, member.note,
                   member.added_at, member.updated_at,
                   MIN(bar.trade_date), MAX(bar.trade_date), COUNT(bar.trade_date)
            FROM stock_pool_member AS member
            LEFT JOIN security_master AS security USING (security_id)
            LEFT JOIN published_daily_bar AS bar
              ON bar.security_id = member.security_id AND bar.adjustment = 'qfq'
            WHERE member.pool_id = ?
            GROUP BY ALL ORDER BY member.display_order, member.security_id
            """,
            [pool_id],
        ).fetchall()
        managed = dict(connection.execute(
            """SELECT m.security_id, m.status || ':' || m.availability FROM managed_stock m
               JOIN stock_pool_member p USING(security_id) WHERE p.pool_id=?""", [pool_id],
        ).fetchall())
        result = []
        for row in rows:
            member = self._member_row(row)
            member["management_state"] = managed.get(member["security_id"], "not_managed")
            member["admission_warning"] = None if member["management_state"] == "active:ready" else "本地行情待补齐；保留旧成员"
            result.append(member)
        return result

    def _members_for_symbols(self, connection: Any, symbols: list[str]) -> list[dict[str, Any]]:
        if not symbols:
            return []
        rows = connection.execute(
            """
            SELECT security_id, symbol, name, exchange, status,
                   1, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                   NULL, NULL, 0
            FROM security_master WHERE symbol IN (SELECT * FROM UNNEST(?))
            """,
            [symbols],
        ).fetchall()
        by_symbol = {str(row[1]): row for row in rows}
        return [self._member_row(by_symbol.get(symbol, (
            f"UNKNOWN.{symbol}", symbol, "证券主数据缺失", "unknown", "missing",
            index, "", None, None, None, None, 0,
        )), order=index) for index, symbol in enumerate(symbols, 1)]

    @staticmethod
    def _member_row(row: tuple[Any, ...], *, order: int | None = None) -> dict[str, Any]:
        return {
            "security_id": str(row[0]),
            "symbol": str(row[1]),
            "name": str(row[2]),
            "exchange": str(row[3]),
            "status": str(row[4]),
            "display_order": order or int(row[5]),
            "note": str(row[6]),
            "added_at": _iso(row[7]),
            "updated_at": _iso(row[8]),
            "coverage": {
                "start_date": _iso(row[9]),
                "end_date": _iso(row[10]),
                "rows": int(row[11]),
            },
        }
