from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ai_quant_assistant.research.dashboard_brief import ResearchDashboardBrief

BriefID = Annotated[str, Path(pattern=r"^brief-[0-9a-f]{64}$")]
Page = Annotated[int, Query(ge=1, le=10000)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class ResearchDashboardInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stock_pool_id: str | None = Field(default=None, min_length=1, max_length=80)
    stock_pool_revision: int | None = Field(default=None, ge=1)
    window_start: date | None = None
    as_of: date | None = None
    adjustment: Literal["none", "qfq", "hfq"] = "qfq"
    price_change_threshold: float = Field(default=5.0, ge=0, le=30)
    volume_ratio_threshold: float = Field(default=2.0, ge=1, le=20)
    benchmark_symbol: str = Field(default="csi000300", min_length=1, max_length=20)

    @field_validator("stock_pool_id", "benchmark_symbol")
    @classmethod
    def no_control_characters(cls, value):
        if value is not None and any(ord(char) < 32 for char in value):
            raise ValueError("字符串不能包含控制字符")
        return value

    @model_validator(mode="after")
    def valid_window(self):
        if self.stock_pool_revision is not None and self.stock_pool_id is None:
            raise ValueError("指定 revision 时必须同时指定 stock_pool_id")
        if self.window_start and self.as_of and self.window_start > self.as_of:
            raise ValueError("window_start 不能晚于 as_of")
        ResearchDashboardBrief.bounds(self.window_start, self.as_of)
        return self

    def service_options(self):
        payload = self.model_dump(mode="json")
        payload["pool_id"] = payload.pop("stock_pool_id")
        payload["pool_revision"] = payload.pop("stock_pool_revision")
        return payload


class ResearchBriefGenerateInput(ResearchDashboardInput):
    confirm: Literal[True]
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    def service_options(self):
        options = super().service_options()
        options.pop("confirm")
        return options


def research_dashboard_router(service, templates, asset_version, actor):
    router = APIRouter(tags=["research-dashboard"])

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, "股票池、revision 或研究简报不存在") from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/research-dashboard", include_in_schema=False)
    def page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="research-dashboard.html",
            context={"asset_version": asset_version, "username": actor},
        )

    @router.get("/api/v1/research-dashboard/pools")
    def pools():
        from ai_quant_assistant.research.stock_workbench import records

        with service.database.connect(read_only=True) as c:
            return records(
                c,
                "SELECT pool_id,name,current_revision,is_default,status FROM stock_pool WHERE status='active' ORDER BY is_default DESC,name,pool_id LIMIT 200",
            )

    @router.get("/api/v1/research-dashboard/deltas")
    @router.get("/api/v1/research-dashboard")
    def dashboard(
        stock_pool_id: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
        stock_pool_revision: Annotated[int | None, Query(ge=1)] = None,
        window_start: date | None = None,
        as_of: date | None = None,
        adjustment: Literal["none", "qfq", "hfq"] = "qfq",
        price_change_threshold: Annotated[float, Query(ge=0, le=30)] = 5.0,
        volume_ratio_threshold: Annotated[float, Query(ge=1, le=20)] = 2.0,
        benchmark_symbol: Annotated[str, Query(min_length=1, max_length=20)] = "csi000300",
        query: Annotated[str, Query(max_length=120)] = "",
        sort: Literal["pool_order", "symbol", "latest_announcement", "price_end"] = "pool_order",
        page: Page = 1,
        page_size: PageSize = 20,
    ):
        try:
            body = ResearchDashboardInput(
                stock_pool_id=stock_pool_id,
                stock_pool_revision=stock_pool_revision,
                window_start=window_start,
                as_of=as_of,
                adjustment=adjustment,
                price_change_threshold=price_change_threshold,
                volume_ratio_threshold=volume_ratio_threshold,
                benchmark_symbol=benchmark_symbol,
            )
        except ValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        return invoke(
            service.dashboard, query=query, sort=sort, page=page, page_size=page_size, **body.service_options()
        )

    @router.post("/api/v1/research-briefs/preview")
    def preview(body: ResearchDashboardInput):
        return invoke(service.preview, **body.service_options())

    @router.post("/api/v1/research-briefs", status_code=201)
    def generate(body: ResearchBriefGenerateInput):
        return invoke(service.generate, actor=actor, **body.service_options())

    @router.get("/api/v1/research-briefs")
    def list_briefs(
        stock_pool_id: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
        page: Page = 1,
        page_size: PageSize = 20,
    ):
        return invoke(service.list, pool_id=stock_pool_id, page=page, page_size=page_size)

    @router.get("/api/v1/research-briefs/{brief_id}")
    def get_brief(brief_id: BriefID):
        return invoke(service.get, brief_id)

    @router.get("/api/v1/research-briefs/{brief_id}/html")
    def download(brief_id: BriefID):
        return Response(
            invoke(service.get, brief_id, html=True),
            media_type="text/html",
            headers={
                "Content-Disposition": f'attachment; filename="{brief_id}.html"',
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router
