"""Deterministic ETF observation, never an attribution of investors' identities."""

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

GROUPS = ("上证50", "沪深300", "中证500", "中证1000", "中证2000", "中证A500",
          "科创50", "创业板指", "中证A50", "中证800", "深证100", "科创100")
VERSION = "etf-observation-v1"


class Fund(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^(SSE|SZSE)\.[0-9]{6}$")
    name: str = Field(min_length=1, max_length=80)
    group: str

    @field_validator("group")
    @classmethod
    def group_known(cls, value):
        if value not in GROUPS:
            raise ValueError("请选择支持的指数分类")
        return value


DEFAULT_FUNDS = [Fund(code=code, name=name, group=group).model_dump() for group, code, name in (
    ("沪深300", "SSE.510300", "华泰柏瑞沪深300ETF"),
    ("沪深300", "SSE.510310", "易方达沪深300ETF"),
    ("沪深300", "SSE.510330", "华夏沪深300ETF"),
    ("沪深300", "SZSE.159919", "嘉实沪深300ETF"),
    ("上证50", "SSE.510050", "华夏上证50ETF"),
    ("上证50", "SSE.510100", "易方达上证50ETF"),
    ("中证500", "SSE.510500", "南方中证500ETF"),
    ("中证500", "SSE.510510", "广发中证500ETF"),
    ("中证500", "SSE.512500", "华夏中证500ETF"),
    ("中证500", "SZSE.159922", "嘉实中证500ETF"),
    ("中证500", "SSE.510580", "易方达中证500ETF"),
    ("中证1000", "SSE.512100", "南方中证1000ETF"),
    ("中证1000", "SZSE.159845", "华夏中证1000ETF"),
    ("中证1000", "SSE.560010", "广发中证1000ETF"),
    ("中证A500", "SZSE.159351", "嘉实中证A500ETF"),
    ("科创50", "SSE.588000", "华夏科创50ETF"),
    ("科创50", "SSE.588080", "易方达科创50ETF"),
    ("科创50", "SSE.588090", "华泰柏瑞科创50ETF"),
    ("创业板指", "SZSE.159915", "易方达创业板ETF"),
    ("中证2000", "SSE.563300", "华泰柏瑞中证2000ETF"),
)]


def slots():
    # End-stamped continuous-auction bars; 09:30 auction is kept separately.
    return [(datetime(2000, 1, 1, h, m) + timedelta(minutes=i)).strftime("%H:%M")
            for h, m in ((9, 31), (13, 1)) for i in range(120)]


def total(values):
    return sum(values, Decimal(0)) if values and all(v is not None for v in values) else None


def number(value):
    return str(value) if value is not None else None


def calculate(funds, days, rows, start, end, selected_day=None, threshold="10000000000", excluded=()):
    """Missing inputs stay missing. Every historical comparison uses the same fixed group."""
    codes = [f["code"] for f in funds]
    lookup = {(r["kind"], r["code"], r["stamp"]): r for r in rows}
    threshold = Decimal(str(threshold))
    days = sorted(set(days))
    daily, by_day = [], {}
    for idx, day in enumerate(days):
        if not start <= day <= end:
            continue
        previous = days[idx - 1] if idx else None
        detail = []
        for fund in funds:
            code = fund["code"]
            current = lookup.get(("shares", code, day), {})
            prior = lookup.get(("shares", code, previous), {})
            nav = lookup.get(("nav", code, day), {})
            quote = lookup.get(("daily", code, day), {})
            change = None
            if current.get("shares") is not None and prior.get("shares") is not None:
                change = Decimal(current["shares"]) - Decimal(prior["shares"])
            flow = change * Decimal(nav["nav"]) if change is not None and nav.get("nav") else None
            # Explicit corporate actions invalidate the simple difference formula.
            if current.get("corporate_action") or prior.get("corporate_action"):
                flow = None
            missing = [name for name, value in (("当日份额", current.get("shares")),
                       ("前一交易日份额", prior.get("shares")), ("当日单位净值", nav.get("nav"))) if value is None]
            if current.get("corporate_action") or prior.get("corporate_action"):
                missing.append("份额折算待核验")
            detail.append({**fund, "shares": current.get("shares"), "nav": nav.get("nav"),
                           "change": number(change), "flow": number(flow), "amount": quote.get("amount"),
                           "missing": missing,
                           "corporate_action": bool(current.get("corporate_action") or prior.get("corporate_action"))})
        flow = total([Decimal(x["flow"]) if x["flow"] is not None else None for x in detail])
        amount = total([Decimal(x["amount"]) if x["amount"] is not None else None for x in detail])
        groups = {}
        for group in GROUPS:
            members = [x for x in detail if x["group"] == group]
            groups[group] = {"count": len(members), "covered": sum(x["flow"] is not None for x in members),
                             "flow": number(total([Decimal(x["flow"]) if x["flow"] is not None else None for x in members]))}
        point = {"date": day, "flow": number(flow), "amount": number(amount), "details": detail,
                 "covered": sum(x["flow"] is not None for x in detail), "groups": groups,
                 "index": lookup.get(("daily", "INDEX.000001", day), {}).get("close"),
                 "sample": flow is not None and flow > threshold and day not in excluded}
        daily.append(point)
        by_day[day] = point

    selected_day = selected_day or end
    time_slots = slots()
    minute_days = sorted({r["stamp"][:10] for r in rows if r["kind"] == "minute" and r["stamp"][:10] in days})
    curves = {}
    for day in minute_days:
        curve, cumulative = [], Decimal(0)
        for clock in time_slots:
            items = [lookup.get(("minute", code, f"{day} {clock}"), {}) for code in codes]
            amount = total([Decimal(x["amount"]) if x.get("amount") is not None else None for x in items])
            cumulative = cumulative + amount if cumulative is not None and amount is not None else None
            index = lookup.get(("minute", "INDEX.000001", f"{day} {clock}"), {}).get("close")
            curve.append({"time": clock, "amount": number(amount), "cumulative": number(cumulative), "index": index})
        curves[day] = curve
    previous_days = [d for d in days if d < selected_day][-20:]
    previous = previous_days[-1] if previous_days else None
    minute = []
    for i, clock in enumerate(time_slots):
        point = dict(curves.get(selected_day, [{}] * 240)[i])
        values = [curves.get(d, [{}] * 240)[i].get("cumulative") for d in previous_days]
        available = [Decimal(v) for v in values if v is not None]
        point.update(time=clock, mean20=number(sum(available) / 20) if len(available) == 20 else None,
                     history_count=len(available), previous=curves.get(previous, [{}] * 240)[i].get("cumulative"))
        minute.append(point)
    # Samples strictly precede the selected day. Retrospective comparison only.
    sample_days = [p["date"] for p in daily if p["sample"] and p["date"] < selected_day]
    for i, point in enumerate(minute):
        comparable = [Decimal(curves[d][i]["cumulative"]) for d in sample_days
                      if d in curves and curves[d][i]["cumulative"] is not None]
        point["sample_count"] = len(comparable)
        point["sample_percentile"] = number(Decimal(sum(v <= Decimal(point["cumulative"]) for v in comparable)) / len(comparable) * 100) if comparable and point.get("cumulative") is not None else None
    normal_days = [p["date"] for p in daily if p["flow"] is not None and not p["sample"]
                   and p["date"] not in excluded and p["date"] < selected_day]
    profiles = {}
    for name, candidates in (("sample", sample_days), ("normal", normal_days)):
        points = []
        for block in range(16):
            amounts, returns = [], []
            for day in candidates:
                curve = curves.get(day, [])
                segment = curve[block * 15:(block + 1) * 15]
                if len(segment) != 15:
                    continue
                value = total([Decimal(x["amount"]) if x["amount"] is not None else None for x in segment])
                if value is not None:
                    amounts.append(value)
                base = lookup.get(("daily", "INDEX.000001", days[days.index(day) - 1]), {}).get("close") if days.index(day) else None
                close = segment[-1].get("index")
                if base and close:
                    returns.append((Decimal(close) / Decimal(base) - 1) * 100)
            points.append({"time": time_slots[block * 15 + 14], "amount": number(sum(amounts) / len(amounts)) if amounts else None,
                           "index_return": number(sum(returns) / len(returns)) if returns else None,
                           "count": len(amounts), "index_count": len(returns)})
        profiles[name] = points
    return {"version": VERSION, "funds": funds, "start": start, "end": end,
            "selected_day": selected_day, "daily": daily, "minute": minute, "profiles": profiles,
            "sample_days": sample_days, "threshold": str(threshold), "excluded": list(excluded),
            "scope": "fixed_group", "warning": "固定观察名单，非国家队交易归因；公开时点及份额折算未全面核验，不用于时点回测。分钟统计不含09:30集合竞价。"}
