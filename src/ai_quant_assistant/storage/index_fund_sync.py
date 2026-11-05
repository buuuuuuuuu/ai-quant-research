"""Explicit, bounded fixed-group ETF sync and immutable snapshot publication."""
import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from uuid import uuid4

from ai_quant_assistant.providers.index_float_cap_public import (
    MEMBER_COUNTS,
    PUBLIC_CAP_SOURCE,
    PublicMemberCap,
)
from ai_quant_assistant.providers.index_float_cap_sse import SQL_IDS, SseMonthlyFloatCapProvider
from ai_quant_assistant.providers.index_fund_close import IndexFundCloseProvider
from ai_quant_assistant.providers.index_fund_nav import IndexFundNavProvider
from ai_quant_assistant.providers.index_fund_shares import IndexFundShareProvider
from ai_quant_assistant.research.index_fund_series import (
    DailyFundInput,
    DailyIndexInput,
    build_series,
)
from ai_quant_assistant.storage.index_fund_snapshots import _canonical
from ai_quant_assistant.storage.managed_coverage import market_today
from ai_quant_assistant.storage.task_repository import OperationTaskRepository

INDEXES = {
    "000300": "沪深300", "000016": "上证50", "000905": "中证500",
    "000852": "中证1000", "932000": "中证2000", "000510": "中证A500",
    "399006": "创业板指", "000688": "科创50",
}


class IndexFundSync:
    dataset = "index_fund_observation"

    def __init__(self, database, shares=None, nav=None, cap=None, close=None):
        self.database = database
        self.tasks = OperationTaskRepository(database)
        self.shares = shares or IndexFundShareProvider()
        self.nav = nav or IndexFundNavProvider()
        self.cap = cap or SseMonthlyFloatCapProvider()
        self.public_cap = PublicMemberCap()
        self.close = close or IndexFundCloseProvider()

    def request(self, index_code, start, end, funds, confirm, numerator_mode="nav"):
        if not confirm:
            raise ValueError("请确认数据同步")
        if index_code not in INDEXES:
            raise ValueError("不支持的指数")
        if numerator_mode not in {"nav", "close"}:
            raise ValueError("不支持的基金规模口径")
        if start > end or (end - start).days > 1830 or end > market_today():
            raise ValueError("请选择不超过1831个自然日且不晚于今天的日期范围")
        if not funds or len(funds) > 10 or len(set(funds)) != len(funds) or any(not re.fullmatch(r"(?:SSE|SZSE)\.[0-9]{6}", f) for f in funds):
            raise ValueError("请填写1至10个不重复基金代码，格式如 SSE.510300")
        self.database.initialize()
        with self.database.connect(read_only=True) as c:
            bounds = c.execute("SELECT MIN(trade_date), MAX(trade_date) FROM trading_calendar WHERE exchange='SSE' AND is_open").fetchone()
            rows = c.execute("SELECT trade_date, session_no FROM trading_calendar WHERE exchange='SSE' AND is_open AND trade_date BETWEEN ? AND ? ORDER BY trade_date", [start, end]).fetchall()
        if not rows or not bounds[0] or bounds[0] > start or bounds[1] < end:
            raise ValueError("本地交易日历未覆盖该区间，请先同步交易日历")
        sessions = [row[1] for row in rows]
        if any(x is None for x in sessions) or any(b != a + 1 for a, b in zip(sessions, sessions[1:], strict=False)):
            raise ValueError("交易日历存在缺口，请先同步并核验")
        spec = {"dataset": self.dataset, "index_code": index_code, "start": start.isoformat(), "end": end.isoformat(), "funds": sorted(funds), "days": [r[0].isoformat() for r in rows], "mode": "fixed_group", "scope_verified": False}
        if index_code in MEMBER_COUNTS:
            spec["cap_source"] = PUBLIC_CAP_SOURCE
        if numerator_mode != "nav":
            spec["numerator_mode"] = numerator_mode
        claim = "index-fund:" + hashlib.sha256(_canonical(spec).encode()).hexdigest()
        return self.tasks.create_or_get_active("sync", claim_key=claim, spec=spec, message="等待同步指数基金数据")

    def _partition(self, task_id, owner, scope, key, fetch, progress):
        """Read a completed partition or atomically record it under the active owner."""
        with self.database.connect(read_only=True) as c:
            row = c.execute("SELECT payload_json FROM index_fund_sync_checkpoint WHERE scope_id=? AND partition_key=?", [scope, key]).fetchone()
        if row is not None:
            if not self.tasks.progress(task_id, owner_id=owner, progress=progress, message=f"已复用分区 {key}"):
                raise RuntimeError("任务所有权已失效")
            return json.loads(row[0])
        payload = fetch()
        with self.database.connect() as c:
            c.execute("BEGIN TRANSACTION")
            try:
                owned = c.execute("UPDATE operation_task SET progress=GREATEST(progress, ?), message=?, heartbeat_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE task_id=? AND owner_id=? AND status='running' RETURNING task_id", [progress, f"已完成分区 {key}", task_id, owner]).fetchone()
                if not owned:
                    raise RuntimeError("任务所有权已失效")
                c.execute("INSERT INTO index_fund_sync_checkpoint(scope_id,partition_key,payload_json) VALUES (?,?,?) ON CONFLICT DO NOTHING", [scope, key, _canonical(payload)])
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return payload

    def run(self, task_id):
        owner = uuid4().hex
        if not self.tasks.start(task_id, owner_id=owner, message="正在读取基金和指数数据"):
            return
        try:
            task = self.tasks.get(task_id)
            spec = task["spec"]
            cap_source = spec.get("cap_source")
            if cap_source not in (None, PUBLIC_CAP_SOURCE):
                raise ValueError("任务使用已停用的数据源，请重新提交同步")
            days = tuple(date.fromisoformat(d) for d in spec["days"])
            funds = tuple(spec["funds"])
            numerator_mode = spec.get("numerator_mode", "nav")
            if numerator_mode not in {"nav", "close"}:
                raise ValueError("不支持的基金规模口径")
            # Bump this version when provider normalization or partition semantics change.
            scope = hashlib.sha256(_canonical(["index-fund-partitions-v1", spec]).encode()).hexdigest()
            months = sorted({(d.year, d.month) for d in days})
            markets = sorted({f.split('.')[0] for f in funds})
            cap_months = months if spec["index_code"] in SQL_IDS else []
            total = len(funds) * len(months) + len(days) * len(markets) + len(cap_months)
            if cap_source == PUBLIC_CAP_SOURCE:
                total += 1 + len(days)
            done = 0

            def partition(key, fetch):
                nonlocal done
                done += 1
                return self._partition(task_id, owner, scope, key, fetch, min(95, 1 + done * 94 // total))

            values = {}
            for fund in funds:
                for year, month in months:
                    month_days = [d for d in days if (d.year, d.month) == (year, month)]
                    provider = self.nav if numerator_mode == "nav" else self.close
                    rows = partition(f"{numerator_mode}:{fund}:{year:04d}-{month:02d}", lambda fund=fund, month_days=month_days, provider=provider: [{"date": row.trade_date.isoformat(), "value": str(row.unit_nav if numerator_mode == "nav" else row.close)} for row in provider.fetch(fund, month_days[0], month_days[-1])])
                    for row in rows:
                        values[(date.fromisoformat(row["date"]), fund)] = Decimal(row["value"])
            share_data = {}
            for day in days:
                for market in markets:
                    rows = partition(f"shares:{market}:{day}", lambda market=market, day=day: [{"fund_id": row.fund_id, "value": str(row.shares)} for row in self.shares.fetch_day(market, day) if row.fund_id in funds])
                    for row in rows:
                        share_data[(day, row["fund_id"])] = Decimal(row["value"])
            caps = {}
            for year, month in cap_months:
                rows = partition(f"cap:{year:04d}-{month:02d}", lambda year=year, month=month: [{"date": row.trade_date.isoformat(), "value": str(row.value_cny)} for row in self.cap.fetch_month(spec["index_code"], year, month)])
                for row in rows:
                    caps[date.fromisoformat(row["date"])] = Decimal(row["value"])
            reconstructed = []
            if cap_source == PUBLIC_CAP_SOURCE:
                index_code = spec["index_code"]
                intervals = partition("public:members", lambda: self.public_cap.membership(index_code))
                for day in days:
                    row = partition(f"public:cap:{day}", lambda day=day: self.public_cap.daily(index_code, day, intervals))
                    if row is not None:
                        caps[day] = Decimal(row["value"])
                        reconstructed.append(row)
            fund_rows = tuple(DailyFundInput(fund, day, share_data.get((day, fund)), values.get((day, fund))) for day in days for fund in funds)
            cap_rows = tuple(DailyIndexInput(day, caps.get(day)) for day in days)
            points = build_series(trading_days=days, fund_ids=funds, fund_rows=fund_rows, index_rows=cap_rows)
            input_key, field = ("nav", "unit_nav") if numerator_mode == "nav" else ("close", "close")
            source = "akshare:eastmoney" if numerator_mode == "nav" else "eastmoney:unadjusted_daily_close"
            sources = {"akshare:" + f.split(".")[0].lower() for f in funds} | {source}
            if cap_months:
                sources.add("sse:monthly_report")
            if cap_source:
                sources.add(cap_source)
            inputs = {"shares": [{"date": d.isoformat(), "fund_id": f, "shares": str(v)} for (d, f), v in sorted(share_data.items())], input_key: [{"date": d.isoformat(), "fund_id": f, field: str(v)} for (d, f), v in sorted(values.items())], "float_cap": [{"date": d.isoformat(), "value_cny": str(v)} for d, v in sorted(caps.items()) if d in days], "sources": sorted(sources), "publication_time": None}
            warning = "固定基金组由用户指定，尚未验证基金跟踪标的或历史排名；不用于回测。"
            if cap_source == PUBLIC_CAP_SOURCE:
                inputs["cap_evidence"] = reconstructed
                warning += "流通市值按新浪成分记录及东方财富个股流通市值汇总。成分、估值不全或剔除日期口径不明时，当日占比留空。"
            elif not cap_months:
                warning += "该指数历史流通市值尚未接入，仅展示固定基金组规模，占比缺失。"
            if numerator_mode == "close":
                warning += "收盘价来源及历史覆盖尚待完整核验。"
            result = {"points": points, "scope_verified": False, "warning": warning}
            texts = [_canonical(x) for x in (spec, inputs, result)]
            snapshot_id = "index-fund-" + hashlib.sha256(_canonical([spec["index_code"], spec, inputs, result]).encode()).hexdigest()
            with self.database.connect() as c:
                c.execute("BEGIN TRANSACTION")
                try:
                    owned = c.execute("UPDATE operation_task SET status='succeeded',progress=100,message='同步完成，请查看数据覆盖',result_json=?,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,claim_key=NULL WHERE task_id=? AND owner_id=? AND status='running' RETURNING task_id", [json.dumps({"snapshot_id": snapshot_id}, ensure_ascii=False), task_id, owner]).fetchone()
                    if not owned:
                        raise RuntimeError("任务所有权已失效")
                    c.execute("INSERT INTO index_fund_snapshot(snapshot_id,index_code,spec_json,input_json,result_json) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING", [snapshot_id, spec["index_code"], *texts])
                    c.execute("DELETE FROM index_fund_sync_checkpoint WHERE scope_id=?", [scope])
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
        except Exception as exc:
            self.tasks.fail(task_id, owner_id=owner, message="同步失败，可重试", error=str(exc))
            raise
