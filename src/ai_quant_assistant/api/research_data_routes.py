from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ai_quant_assistant.api.schemas import (
    DailySignalStrategyRequest,
    FactorBacktestRequest,
    FactorLabRequest,
    PointInTimeScreenRequest,
)
from ai_quant_assistant.storage.stock_pools import StockPoolConflictError


class ScreenParameters(PointInTimeScreenRequest):
    model_config = ConfigDict(extra="forbid")


class BacktestParameters(FactorBacktestRequest):
    model_config = ConfigDict(extra="forbid")


class LabParameters(FactorLabRequest):
    model_config = ConfigDict(extra="forbid")


class DailySignalParameters(DailySignalStrategyRequest):
    model_config = ConfigDict(extra="forbid")


class ScreenCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    research_type: Literal["screen"]
    parameters: ScreenParameters


class BacktestCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    research_type: Literal["factor_backtest"]
    parameters: BacktestParameters


class LabCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    research_type: Literal["factor_lab"]
    parameters: LabParameters


class DailySignalCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    research_type: Literal["daily_signal"]
    parameters: DailySignalParameters


ReadinessRequest = Annotated[
    ScreenCheck | BacktestCheck | LabCheck | DailySignalCheck,
    Field(discriminator="research_type"),
]
PlanID = Annotated[str, Path(min_length=1, max_length=100)]


class ConfirmDataPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm: StrictBool


def research_data_router(manager, actor, copilot_repository):
    router = APIRouter(prefix="/api/v1/research-data", tags=["research-data"])
    service = manager.service

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, "研究范围或计划不存在") from exc
        except (RuntimeError, StockPoolConflictError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/preflight")
    def preflight(request: ReadinessRequest):
        return invoke(
            service.readiness.run, request.research_type, request.parameters.model_dump(mode="json")
        )

    @router.post("/plans", status_code=201)
    def create(request: ReadinessRequest):
        return invoke(
            service.create,
            request.research_type,
            request.parameters.model_dump(mode="json"),
            actor=actor,
        )

    @router.get("/plans")
    def listing(limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return invoke(service.list, limit=limit)

    @router.get("/plans/{plan_id}")
    def detail(plan_id: PlanID):
        return invoke(service.get, plan_id)

    @router.post("/plans/{plan_id}/confirm", status_code=202)
    def confirm(plan_id: PlanID, request: ConfirmDataPlan):
        task = invoke(service.confirm, plan_id, **request.model_dump(), actor=actor)
        manager.dispatch()
        return task

    @router.post("/plans/{plan_id}/retry", status_code=202)
    def retry(plan_id: PlanID, request: ConfirmDataPlan):
        task = invoke(service.confirm, plan_id, **request.model_dump(), actor=actor, retry=True)
        manager.dispatch()
        return task

    @router.post("/plans/{plan_id}/preflight")
    def recheck(plan_id: PlanID):
        return invoke(service.recheck, plan_id)

    @router.post("/copilot/{plan_id}/plan", status_code=201)
    def from_copilot(plan_id: PlanID):
        from ai_quant_assistant.ai.preflight import CoveragePreflight

        def create():
            research_plan = copilot_repository.get_plan(plan_id)
            if research_plan["tool_name"] not in {"run_factor_backtest", "run_factor_lab"}:
                raise ValueError("该 Copilot 工具不支持首期财务补齐计划")
            report = CoveragePreflight(service.database).run(
                research_plan["tool_name"], research_plan["spec"]
            )
            return service.create_frozen(report["input"], actor=actor)

        return invoke(create)

    return router
