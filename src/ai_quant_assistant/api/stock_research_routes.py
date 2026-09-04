from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, model_validator

from ai_quant_assistant.research.stock_workbench import StockResearchWorkbench

Symbol = Annotated[str, Path(pattern=r"^[0-9]{6}$")]
SummaryID = Annotated[str, Path(pattern=r"^summary-[0-9a-f]{64}$")]
Page = Annotated[int, Query(ge=1, le=10000)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Section = Literal[
    "prices",
    "financial_reports",
    "financial_metrics",
    "announcements",
    "documents",
    "events",
    "research",
]


class StockResearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["current", "historical"] = "current"
    as_of: date | None = None
    start_date: date = date(2012, 1, 1)
    adjustment: Literal["none", "qfq", "hfq"] = "qfq"

    @model_validator(mode="after")
    def valid_range(self):
        StockResearchWorkbench.bounds(self.mode, self.as_of, self.start_date)
        return self


def stock_research_router(service, templates, asset_version, actor):
    router = APIRouter(tags=["stock-research"])

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, "股票或摘要不存在") from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/stocks/{symbol}", include_in_schema=False)
    def page(request: Request, symbol: Symbol):
        invoke(service.overview, symbol)
        return templates.TemplateResponse(
            request=request,
            name="stock-research.html",
            context={"symbol": symbol, "asset_version": asset_version, "username": actor},
        )

    @router.get("/api/v1/stocks/{symbol}/overview")
    def overview(
        symbol: Symbol,
        mode: Literal["current", "historical"] = "current",
        as_of: date | None = None,
        start_date: date = date(2012, 1, 1),
    ):
        return invoke(service.overview, symbol, mode=mode, as_of=as_of, start_date=start_date)

    @router.post("/api/v1/stocks/{symbol}/summaries", status_code=201)
    def generate(symbol: Symbol, body: StockResearchInput):
        return invoke(service.generate, symbol, actor=actor, **body.model_dump(mode="json"))

    @router.get("/api/v1/stocks/{symbol}/summaries")
    def summaries(symbol: Symbol, page: Page = 1, page_size: PageSize = 20):
        return invoke(service.summaries, symbol, page=page, page_size=page_size)

    @router.get("/api/v1/stocks/{symbol}/summaries/{summary_id}")
    def summary(symbol: Symbol, summary_id: SummaryID):
        return invoke(service.get_summary, symbol, summary_id)

    @router.get("/api/v1/stocks/{symbol}/summaries/{summary_id}/html")
    def download(symbol: Symbol, summary_id: SummaryID):
        content = invoke(service.get_summary, symbol, summary_id, html=True)
        return Response(
            content,
            media_type="text/html",
            headers={
                "Content-Disposition": f'attachment; filename="{summary_id}.html"',
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/api/v1/stocks/{symbol}/{section}")
    def section(
        symbol: Symbol,
        section: Section,
        mode: Literal["current", "historical"] = "current",
        as_of: date | None = None,
        start_date: date = date(2012, 1, 1),
        adjustment: Literal["none", "qfq", "hfq"] = "qfq",
        page: Page = 1,
        page_size: PageSize = 20,
    ):
        return invoke(
            service.section,
            symbol,
            section,
            mode=mode,
            as_of=as_of,
            start_date=start_date,
            adjustment=adjustment,
            page=page,
            page_size=page_size,
        )

    return router
