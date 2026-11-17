from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, StrictBool

from ai_quant_assistant.providers.gold_history import SOURCE, GoldHistory, GoldSourceError


class GoldRefresh(BaseModel):
    confirm: StrictBool


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
                "chart_start": (start.isoformat() if days == "7" else rows[0][0])
                if any(close is not None for _, close in rows) else None,
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
