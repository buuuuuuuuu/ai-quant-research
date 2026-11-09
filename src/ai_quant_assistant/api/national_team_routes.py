import json
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from ai_quant_assistant.api.managed_stock_tasks import ManagedStockTaskManager
from ai_quant_assistant.research.national_team import DEFAULT_FUNDS, GROUPS, calculate, slots
from ai_quant_assistant.storage.national_team import Scope, SyncScope, validate_rows


class NationalTeamTaskManager(ManagedStockTaskManager):
    def dispatch(self):
        if self.service.database.path.is_file() and not self.stop.is_set():
            self.service.tick()
        super().dispatch()


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Scope
    enabled: StrictBool
    confirm: StrictBool


def demo():
    funds = DEFAULT_FUNDS[:2]
    days = [(date(2025, 3, 3) + timedelta(days=i)).isoformat() for i in range(40)
            if (date(2025, 3, 3) + timedelta(days=i)).weekday() < 5]
    rows = []
    for i, day in enumerate(days):
        rows.append({"kind": "daily", "code": "INDEX.000001", "stamp": day, "close": str(3000 + i * 2)})
        for j, fund in enumerate(funds):
            rows.extend([
                {"kind": "shares", "code": fund["code"], "stamp": day, "shares": str(Decimal("1e10") + sum((Decimal("2e9") if n % 3 == 0 else Decimal("-2e7") for n in range(1, i + 1)), Decimal(0)) + j * Decimal("1e9"))},
                {"kind": "nav", "code": fund["code"], "stamp": day, "nav": "3"},
                {"kind": "daily", "code": fund["code"], "stamp": day, "amount": str(Decimal("5e9") + i * Decimal("1e8"))},
            ])
            for k, clock in enumerate(slots()):
                rows.append({"kind": "minute", "code": fund["code"], "stamp": day + " " + clock, "amount": str((100 + k % 15 + i) * 100000), "close": "3"})
        for k, clock in enumerate(slots()):
            rows.append({"kind": "minute", "code": "INDEX.000001", "stamp": day + " " + clock, "amount": "100", "close": str(3000 + i * 2 + k / 100)})
    result = calculate(funds, days, rows, days[1], days[-1])
    result.update(demo=True, batches=[], as_of="合成示例", calendar_days=len(days), input_sha256="synthetic-demo-v1")
    return result


def national_team_router(service, manager, templates, version, actor):
    router = APIRouter(tags=["national-team"])

    @router.get("/market/national-team", include_in_schema=False)
    def page(request: Request):
        return templates.TemplateResponse(request=request, name="national-team.html", context={"asset_version": version, "username": actor})

    @router.get("/api/v1/national-team/catalog")
    def catalog():
        return {"funds": DEFAULT_FUNDS, "groups": GROUPS, "scope": "示例固定名单；不代表历史排名或已确认的国家队持仓"}

    @router.get("/api/v1/national-team/demo")
    def synthetic():
        return demo()

    @router.post("/api/v1/national-team/query")
    def query(body: Scope):
        try:
            return service.read(body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/api/v1/national-team/sync", status_code=202)
    def sync(body: SyncScope):
        try:
            return manager.submit(**body.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/api/v1/national-team/tasks/{task_id}")
    def task(task_id: str):
        try:
            result = service.tasks.get(task_id)
            if result["spec"].get("dataset") != service.dataset:
                raise KeyError(task_id)
            return result
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

    @router.get("/api/v1/national-team/schedule")
    def schedule():
        return service.schedule()

    @router.post("/api/v1/national-team/schedule")
    def update_schedule(body: ScheduleRequest):
        if not body.confirm:
            raise HTTPException(422, "请确认变更自动采集计划")
        return service.set_schedule(body.scope, body.enabled)

    @router.post("/api/v1/national-team/import", status_code=201)
    async def import_records(request: Request):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 16 * 1024 * 1024:
                raise HTTPException(413, "文件不可超过16 MiB，请按日期分批")
        try:
            body = json.loads(content)
            if body.get("confirm") is not True:
                raise ValueError("请确认导入数据")
            scope = Scope(**body["scope"])
            rows = validate_rows(body["rows"])
            codes = {f.code for f in scope.funds} | {"INDEX.000001"}
            lower = (scope.start - timedelta(days=45)).isoformat()
            if any(r["code"] not in codes or not lower <= r["stamp"][:10] <= scope.end.isoformat() for r in rows):
                raise ValueError("导入记录超出声明的基金或日期范围")
            source = body.get("source", "")
            if not isinstance(source, str) or not source.strip() or len(source) > 200:
                raise ValueError("请填写不超过200字的来源说明")
            batch = service.save(scope.model_dump(mode="json"), rows, "user-import:" + source)
            return {"batch_id": batch, "records": len(rows), "verification": "unverified"}
        except (ValueError, KeyError, TypeError, AttributeError, ValidationError) as exc:
            raise HTTPException(422, "导入格式无效：" + str(exc)[:300]) from exc

    @router.post("/api/v1/national-team/export")
    def export(body: Scope):
        result = query(body)
        return Response(json.dumps(result, ensure_ascii=False), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="national-team.json"'})

    return router
