"""Explicit background collection, immutable input revisions and local-only reads."""

import hashlib
import json
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from threading import RLock
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from ai_quant_assistant.research.national_team import Fund, calculate
from ai_quant_assistant.storage.index_fund_snapshots import _canonical
from ai_quant_assistant.storage.managed_coverage import market_today
from ai_quant_assistant.storage.task_repository import OperationTaskRepository


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    funds: list[Fund] = Field(min_length=1, max_length=100)
    start: date
    end: date
    selected_day: date | None = None
    threshold: Decimal = Field(default=Decimal("10000000000"), ge=0, le=Decimal("1e15"), allow_inf_nan=False)
    excluded: list[date] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def valid(self):
        if self.start > self.end or (self.end - self.start).days > 1830 or self.end > market_today():
            raise ValueError("日期范围须在今天以前，且不超过1831天")
        if len({f.code for f in self.funds}) != len(self.funds):
            raise ValueError("基金代码不可重复")
        if self.selected_day and not self.start <= self.selected_day <= self.end:
            raise ValueError("分钟查看日期须在所选范围内")
        return self


class SyncScope(Scope):
    confirm: StrictBool
    mode: str = Field(default="daily", pattern="^(daily|minute)$")


def validate_rows(rows, max_rows=100000):
    if not isinstance(rows, list) or len(rows) > max_rows:
        raise ValueError(f"每次最多处理{max_rows}条记录")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in {"daily", "minute", "shares", "nav", "calendar"}:
            raise ValueError("无效的数据类型")
        if row.get("code") != "INDEX.000001":
            Fund(code=row.get("code"), name="导入", group="沪深300")
        stamp = row.get("stamp", "")
        fmt = "%Y-%m-%d %H:%M" if row["kind"] == "minute" else "%Y-%m-%d"
        if datetime.strptime(stamp, fmt).strftime(fmt) != stamp:
            raise ValueError("无效的时间格式")
        if "corporate_action" in row and type(row["corporate_action"]) is not bool:
            raise ValueError("corporate_action 必须为布尔值")
        keys = () if row["kind"] == "calendar" else ("amount", "close") if row["kind"] in {"daily", "minute"} else ("shares",) if row["kind"] == "shares" else ("nav",)
        for field in keys:
            try:
                value = Decimal(str(row[field]))
            except (KeyError, InvalidOperation) as exc:
                raise ValueError(f"缺少或无效字段 {field}") from exc
            if not value.is_finite() or value < 0 or (field != "amount" and value == 0):
                raise ValueError(f"无效字段 {field}")
            row[field] = str(value)
        key = row["kind"], row["code"], stamp
        if key in seen:
            raise ValueError("同批记录不可重复")
        seen.add(key)
    return rows


class NationalTeamService:
    dataset = "national_team"

    def __init__(self, database, provider=None):
        self.database = database
        self.tasks = OperationTaskRepository(database)
        self.provider = provider
        self.cache = {}
        self.cache_lock = RLock()

    def days(self, start, end):
        if not self.database.path.is_file():
            return []
        with self.database.connect(read_only=True) as c:
            rows = c.execute("SELECT trade_date,session_no FROM trading_calendar WHERE exchange='SSE' AND is_open AND trade_date BETWEEN ? AND ? ORDER BY trade_date", [start, end]).fetchall()
        if len(rows) > 1 and any(a[1] is None or b[1] is None or b[1] != a[1] + 1 for a, b in zip(rows, rows[1:], strict=False)):
            raise ValueError("交易日历缺少连续序号，请在数据管理同步完整日历")
        return [str(r[0]) for r in rows]

    def save(self, spec, rows, source, errors=(), task_id=None, owner=None):
        rows = validate_rows(rows, max_rows=600000)
        batch = "national-team-" + uuid4().hex
        self.database.initialize()
        with self.database.connect() as c:
            c.execute("BEGIN TRANSACTION")
            try:
                if task_id:
                    owned = c.execute("SELECT 1 FROM operation_task WHERE task_id=? AND owner_id=? AND status='running'", [task_id, owner]).fetchone()
                    if not owned:
                        raise ValueError("任务所有权已失效")
                if rows:
                    lower, upper = min(r["stamp"] for r in rows), max(r["stamp"] for r in rows)
                    old = c.execute("""SELECT kind,code,stamp,payload_json FROM (
                        SELECT o.*,row_number() OVER (PARTITION BY kind,code,stamp ORDER BY b.created_at DESC,b.batch_id DESC) AS rank
                        FROM national_team_observation o JOIN national_team_batch b USING(batch_id)
                        WHERE stamp BETWEEN ? AND ?
                    ) WHERE rank=1""", [lower, upper]).fetchall()
                    existing = {(r[0], r[1], r[2]): r[3] for r in old}
                    rows = [r for r in rows if existing.get((r["kind"], r["code"], r["stamp"])) != _canonical(r)]
                c.execute("INSERT INTO national_team_batch(batch_id,source,spec_json,errors_json) VALUES (?,?,?,?)", [batch, source, _canonical(spec), _canonical(errors)])
                if rows:
                    c.executemany("INSERT INTO national_team_observation VALUES (?,?,?,?,?)", [(batch, r["kind"], r["code"], r["stamp"], _canonical(r)) for r in rows])
                if task_id:
                    result = _canonical({"batch_id": batch, "records": len(rows), "errors": errors})
                    c.execute("UPDATE operation_task SET status='succeeded',progress=100,message=?,result_json=?,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,claim_key=NULL WHERE task_id=? AND owner_id=?", ["已保存；部分来源失败" if errors else "采集完成；覆盖情况以看板为准", result, task_id, owner])
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        with self.cache_lock:
            self.cache.clear()
        return batch

    def read(self, scope):
        key = _canonical(scope.model_dump(mode="json"))
        with self.cache_lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 20:
                return cached[1]
            result = self._read(scope)
            if len(self.cache) >= 4:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = time.monotonic(), result
            return result

    def _read(self, scope):
        spec = scope.model_dump(mode="json")
        start = (scope.start - timedelta(days=45)).isoformat()
        days = self.days(start, spec["end"])
        rows, batches = [], []
        if self.database.path.is_file():
            with self.database.connect(read_only=True) as c:
                codes = [f.code for f in scope.funds] + ["INDEX.000001"]
                placeholders = ",".join("?" for _ in codes)
                raw = c.execute(f"""SELECT payload_json, batch_id FROM (
                    SELECT o.*, row_number() OVER (PARTITION BY kind,code,stamp ORDER BY b.created_at DESC,b.batch_id DESC) AS rank
                    FROM national_team_observation o JOIN national_team_batch b USING(batch_id)
                    WHERE stamp>=? AND stamp<? AND code IN ({placeholders})
                ) WHERE rank=1""", [start, spec["end"] + "z", *codes]).fetchall()
                rows = [json.loads(r[0]) for r in raw]
                days = sorted(set(days) | {r["stamp"] for r in rows if r["kind"] == "calendar"})
                ids = sorted({r[1] for r in raw})
                if ids:
                    batches = [{"batch_id": r[0], "source": r[1], "fetched_at": r[2].isoformat(), "errors": json.loads(r[3])} for r in c.execute("SELECT batch_id,source,created_at,errors_json FROM national_team_batch WHERE batch_id IN (" + ",".join("?" for _ in ids) + ") ORDER BY created_at DESC", ids).fetchall()]
        result = calculate(spec["funds"], days, rows, spec["start"], spec["end"], spec["selected_day"], spec["threshold"], spec["excluded"])
        result.update(batches=batches, calendar_days=len(days), as_of=datetime.now(UTC).isoformat(),
                      input_sha256=hashlib.sha256(_canonical(sorted(rows, key=lambda r: (r["kind"], r["code"], r["stamp"]))).encode()).hexdigest())
        return result

    def schedule(self):
        if not self.database.path.is_file():
            return {"enabled": False}
        with self.database.connect(read_only=True) as c:
            row = c.execute("SELECT enabled,spec_json,next_at,error FROM national_team_schedule WHERE schedule_id=1").fetchone()
        return {"enabled": row[0], "scope": json.loads(row[1]), "next_at": str(row[2]), "error": row[3]} if row else {"enabled": False}

    def set_schedule(self, scope, enabled):
        self.database.initialize()
        with self.database.connect() as c:
            c.execute("INSERT INTO national_team_schedule VALUES (1,?,?,NULL,NULL) ON CONFLICT(schedule_id) DO UPDATE SET enabled=EXCLUDED.enabled,spec_json=EXCLUDED.spec_json,next_at=NULL,error=NULL", [enabled, _canonical(scope.model_dump(mode="json"))])
        return self.schedule()

    def tick(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        clock = now.strftime("%H:%M")
        if not ("09:31" <= clock <= "11:31" or "13:01" <= clock <= "15:05"):
            return
        if now.date().isoformat() not in self.days(now.date().isoformat(), now.date().isoformat()):
            return
        with self.database.connect() as c:
            row = c.execute("UPDATE national_team_schedule SET next_at=CURRENT_TIMESTAMP + INTERVAL 60 SECOND WHERE schedule_id=1 AND enabled AND (next_at IS NULL OR next_at<=CURRENT_TIMESTAMP) RETURNING spec_json").fetchone()
        if row:
            spec = json.loads(row[0])
            today = now.date()
            spec.update(start=(today - timedelta(days=7)).isoformat(), end=today.isoformat(), selected_day=None, confirm=True, mode="minute")
            try:
                self.request(**spec)
                error = None
            except ValueError:
                error = "分钟采集未提交，请检查本地交易日历"
            with self.database.connect() as c:
                c.execute("UPDATE national_team_schedule SET error=? WHERE schedule_id=1", [error])

    def request(self, **kwargs):
        scope = SyncScope(**kwargs)
        if not scope.confirm:
            raise ValueError("请明确确认联网采集")
        start = scope.start - timedelta(days=45)
        days = self.days(start.isoformat(), scope.end.isoformat())
        if not any(d < scope.start.isoformat() for d in days) or not any(d >= scope.start.isoformat() for d in days):
            raise ValueError("请先在数据管理同步交易日历，覆盖查询区间及之前45天")
        spec = scope.model_dump(mode="json")
        spec.update(dataset=self.dataset, days=days)
        claim = self.dataset + ":" + hashlib.sha256(_canonical(spec).encode()).hexdigest()
        task, _ = self.tasks.create_or_get_active("sync", claim_key=claim, spec=spec, message="等待采集重点ETF")
        return task

    def run(self, task_id):
        owner = uuid4().hex
        if not self.tasks.start(task_id, owner_id=owner, message="正在采集ETF数据"):
            return
        try:
            from ai_quant_assistant.providers.national_team import NationalTeamProvider

            provider = self.provider or NationalTeamProvider()
            spec = self.tasks.get(task_id)["spec"]
            codes = [f["code"] for f in spec["funds"]]
            days = spec["days"]
            calls = []
            if spec["mode"] == "minute":
                for code in [*codes, "INDEX.000001"]:
                    calls.append((f"minute:{code}", lambda code=code: provider.bars(code, days[0], spec["end"], True)))
            else:
                for code in [*codes, "INDEX.000001"]:
                    calls.append((f"daily:{code}", lambda code=code: provider.bars(code, days[0], spec["end"])))
                months = sorted({d[:7] for d in days}, reverse=True)
                for month in months:
                    month_days = [d for d in days if d.startswith(month)]
                    for code in codes:
                        calls.append((f"nav:{code}:{month}", lambda code=code, month_days=month_days: provider.fetch_nav(code, month_days[0], month_days[-1])))
                for day in reversed(days):
                    for market in sorted({c.split(".")[0] for c in codes}):
                        calls.append((f"shares:{market}:{day}", lambda market=market, day=day: provider.fetch_shares(market, day, codes)))
            rows, errors, failures, skipped = [], [], {}, {}
            for i, (key, fetch) in enumerate(calls):
                family = ":".join(key.split(":")[:2]) if key.startswith("shares:") else key.split(":")[0]
                if failures.get(family, 0) >= 5:
                    skipped[family] = skipped.get(family, 0) + 1
                    continue
                if not self.tasks.progress(task_id, owner_id=owner, progress=int(95 * i / len(calls)), message=f"采集 {key}"):
                    return
                try:
                    rows.extend(validate_rows(fetch()))
                    failures[family] = 0
                except Exception as exc:
                    # Never include provider URLs, headers or credential values.
                    detail = {"partition": key, "error": type(exc).__name__}
                    response = getattr(exc, "response", None)
                    if response is not None:
                        detail["http_status"] = response.status_code
                    if type(exc) is ValueError and str(exc) in {
                        "净值分页不完整，未发布此基金净值", "净值分页总数发生变化，请重试",
                        "净值来源返回范围外数据", "上交所返回了不同统计日期", "上交所份额响应被截断",
                    }:
                        detail["reason"] = str(exc)
                    errors.append(detail)
                    failures[family] = failures.get(family, 0) + 1
            errors.extend({"partition": family, "error": "StoppedAfterFiveFailures", "count": count} for family, count in skipped.items())
            if not rows:
                self.tasks.fail(task_id, owner_id=owner, message="所有来源均未返回有效记录，已有数据保留", error="请查看各来源错误", result={"errors": errors})
                return
            self.save(spec, rows, "public:exchange/eastmoney", errors, task_id, owner)
        except Exception as exc:
            self.tasks.fail(task_id, owner_id=owner, message="采集失败，已有数据保留", error=type(exc).__name__ + ": 请检查来源或交易日历后重试")
