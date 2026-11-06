from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, StrictBool

from ai_quant_assistant.providers.gold_history import SOURCE, GoldHistory, GoldSourceError


class GoldRefresh(BaseModel):
    confirm: StrictBool


def chart_data(rows, *, window_start=None):
    prices = [close for _, close in rows if close is not None]
    if not prices:
        return None
    high, low = max(prices), min(prices)
    first, last = (date.fromisoformat(rows[i][0]) for i in (0, -1))
    first = window_start or first
    days = (last - first).days

    def x_for(offset):
        return 2 + 996 * offset / days if days else 500

    # Calendar ticks share the exact time scale used for prices, including weekends.
    intervals = min(days, 6)
    ticks = []
    for index in range(intervals + 1):
        offset = round(index * days / intervals) if intervals else 0
        day = first + timedelta(days=offset)
        ticks.append({
            "date": day.isoformat(), "label": day.strftime("%Y-%m" if days > 730 else "%m-%d"),
            "x": x_for(offset), "compact": index in (0, intervals // 2, intervals),
        })
    markers = []
    path = []
    start_segment = True
    for day, close in rows:
        if close is None:
            start_segment = True
            continue
        offset = (date.fromisoformat(day) - first).days
        x = x_for(offset)
        y = 4 + 192 * (high - close) / (high - low) if high > low else 100
        if window_start is not None:
            markers.append({"date": day, "close": close, "x": x, "y": y})
        # A zero-length round-capped segment also shows an isolated observation.
        if start_segment:
            path.append(f"M{x:.2f},{y:.2f}")
        path.append(f"L{x:.2f},{y:.2f}")
        start_segment = False
    return {
        "path": " ".join(path), "high": high, "low": low, "ticks": ticks, "markers": markers,
    }


def gold_router(cache_dir, templates, asset_version, actor):
    router = APIRouter(tags=["gold-prices"])
    history = GoldHistory(cache_dir)

    @router.get("/market/gold", include_in_schema=False)
    def page(request: Request, days: Literal["7", "30", "90", "365", "all"] = "90"):
        snapshot = history.read()
        rows = sorted(snapshot["closes"].items())
        start = None
        if rows and days != "all":
            start = date.fromisoformat(rows[-1][0]) - timedelta(days=int(days) - 1)
            rows = [(day, close) for day, close in rows if day >= start.isoformat()]
        updated = snapshot["updated_at"]
        if updated:
            updated = datetime.fromisoformat(updated).astimezone(ZoneInfo("Asia/Shanghai"))
        return templates.TemplateResponse(
            request=request,
            name="gold.html",
            context={
                "username": actor, "asset_version": asset_version, "days": days,
                "rows": rows, "source": SOURCE,
                "chart": chart_data(rows, window_start=start if days == "7" else None),
                "updated": updated, "recent": list(reversed(rows[-20:])),
            },
        )

    @router.post("/api/v1/gold/refresh")
    def refresh(body: GoldRefresh):
        if not body.confirm:
            raise HTTPException(422, "请先确认从上海黄金交易所更新行情。")
        try:
            history.refresh()
        except BlockingIOError as exc:
            raise HTTPException(409, str(exc)) from exc
        except GoldSourceError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"status": "ok"}

    return router
