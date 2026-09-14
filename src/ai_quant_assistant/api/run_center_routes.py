from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from ai_quant_assistant.api.run_center import KINDS, RunCenter


def run_center_router(database, templates, asset_version, actor):
    router = APIRouter(tags=["run-center"])
    service = RunCenter(database)

    def invoke(method, **kwargs):
        try:
            return method(**kwargs)
        except KeyError as exc:
            raise HTTPException(404, "运行记录或原股票池 revision 不存在") from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/api/v1/run-center")
    def history(
        kind: str | None = None,
        status: str | None = None,
        page: Annotated[int, Query(ge=1, le=10000)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ):
        return invoke(service.list, kind=kind, status=status, page=page, page_size=page_size)

    @router.get("/api/v1/run-center/record")
    def detail(source: str, record: Annotated[str, Query(min_length=1, max_length=160)]):
        return invoke(service.get, source=source, record_id=record)

    @router.get("/api/v1/run-center/copy")
    def copy(source: str, record: Annotated[str, Query(min_length=1, max_length=160)]):
        return invoke(service.copy_parameters, source=source, record_id=record)

    @router.get("/backtests", include_in_schema=False)
    @router.get("/backtests/detail", include_in_schema=False)
    @router.get("/backtests/configure", include_in_schema=False)
    def center(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="run-center.html",
            context={
                "asset_version": asset_version,
                "username": actor,
                "page_id": "backtests",
                "page_title": "回测中心",
                "kinds": KINDS,
            },
        )

    return router
