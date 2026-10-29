import csv
import io
import re
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ai_quant_assistant.storage.index_fund_snapshots import IndexFundSnapshotStore

INDEXES = {
    "000300": "沪深300", "000016": "上证50", "000905": "中证500",
    "000852": "中证1000", "932000": "中证2000", "000510": "中证A500",
    "399006": "创业板指", "000688": "科创50",
}


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index_code: str
    start: date
    end: date
    funds: list[str] = Field(min_length=1, max_length=10)
    confirm: StrictBool


def index_fund_router(database, templates, asset_version, actor, manager):
    router = APIRouter(tags=["index-fund-observation"])
    store = IndexFundSnapshotStore(database)

    @router.get("/market/index-funds", include_in_schema=False)
    def page(request: Request):
        return templates.TemplateResponse(request=request, name="index-funds.html", context={"asset_version": asset_version, "username": actor})

    @router.get("/api/v1/index-funds/{index_code}")
    def latest(index_code: str):
        if index_code not in INDEXES:
            raise HTTPException(404, "不支持的指数")
        if not database.path.is_file():
            return {"index_code": index_code, "index_name": INDEXES[index_code], "snapshot": None}
        return {"index_code": index_code, "index_name": INDEXES[index_code], "snapshot": store.latest(index_code)}

    @router.get("/api/v1/index-fund-snapshots/{snapshot_id}/csv")
    def export_csv(snapshot_id: str):
        if not re.fullmatch(r"index-fund-[0-9a-f]{64}", snapshot_id):
            raise HTTPException(404, "快照不存在")
        try:
            snapshot = store.get(snapshot_id)
        except KeyError as exc:
            raise HTTPException(404, "快照不存在") from exc
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["snapshot_id", "index_code", "fund_ids", "scope", "sources", "trade_date", "numerator_cny", "denominator_cny", "ratio_fraction", "ratio_percent", "availability", "missing"])
        spec, inputs = snapshot["spec"], snapshot["inputs"]
        def safe(value):
            text = str(value if value is not None else "")
            return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
        for point in snapshot["result"].get("points", []):
            ratio = point.get("ratio_fraction")
            from decimal import Decimal
            percent = str(Decimal(ratio) * 100) if ratio is not None else ""
            values = [snapshot_id, snapshot["index_code"], ";".join(spec.get("funds", [])), "fixed_group_unverified", ";".join(inputs.get("sources", [])), point.get("trade_date"), point.get("numerator_cny"), point.get("denominator_cny"), ratio, percent, point.get("availability"), ";".join(point.get("missing", []))]
            writer.writerow([safe(value) for value in values])
        return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{snapshot_id}.csv"', "X-Content-Type-Options": "nosniff"})

    @router.post("/api/v1/index-funds/sync", status_code=202)
    def sync(body: SyncRequest):
        try:
            return manager.submit(**body.model_dump())
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/api/v1/index-funds/tasks/{task_id}")
    def task(task_id: str):
        try:
            item = manager.service.tasks.get(task_id)
            if item["spec"].get("dataset") != manager.service.dataset:
                raise KeyError(task_id)
            return item
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

    return router
