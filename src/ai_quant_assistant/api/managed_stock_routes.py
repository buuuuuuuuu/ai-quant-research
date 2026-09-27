from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from ai_quant_assistant.api.managed_stock_tasks import ManagedStockTaskManager
from ai_quant_assistant.storage.managed_coverage import market_today
from ai_quant_assistant.storage.managed_stocks import ManagedStockConflict


class ManagedStockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[0-9]{6}$")
    start: date = date(2012, 1, 1)
    end: date = Field(default_factory=market_today)
    expected_revision: int | None = Field(default=None, ge=1)
    confirm: StrictBool
    refresh: StrictBool = False
    auto_update: StrictBool = True
    include_announcements: StrictBool = False

    @model_validator(mode="after")
    def validate_range(self):
        if not self.confirm or not date(1990, 1, 1) <= self.start <= self.end <= market_today():
            raise ValueError("确认并提供有效日期范围，不允许未来日期")
        return self


class ManagedStockChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    action: Literal["archive", "restore", "configure"]
    confirm: StrictBool
    auto_update: StrictBool | None = None

    @model_validator(mode="after")
    def validate_action(self):
        if not self.confirm:
            raise ValueError("explicit confirmation required")
        if (self.action == "configure") != (self.auto_update is not None):
            raise ValueError("auto_update is required only for configure")
        return self


class GapVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    confirm: StrictBool

    @model_validator(mode="after")
    def validate_confirmation(self):
        if not self.confirm:
            raise ValueError("核验停牌状态需要明确确认访问公网")
        return self


def managed_stock_router(manager: ManagedStockTaskManager, actor: str) -> APIRouter:
    router = APIRouter(prefix="/api/v1/data/managed-stocks", tags=["managed-stocks"])
    service = manager.service

    def invoke(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="managed stock not found") from exc
        except ManagedStockConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("")
    def listing(
        query: Annotated[str, Query(max_length=120)] = "",
        include_archived: bool = False,
        ready_only: bool = False,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    ):
        return invoke(
            service.list,
            query=query,
            include_archived=include_archived,
            ready_only=ready_only,
            page=page,
            page_size=page_size,
        )

    @router.get("/directory")
    def directory(
        query: Annotated[str, Query(min_length=1, max_length=120)],
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ):
        return invoke(service.search_directory, query, limit=limit)

    @router.post("", status_code=202)
    def add(request: ManagedStockRequest):
        return invoke(manager.submit, **request.model_dump(), actor=actor)

    @router.get("/{security_id}")
    def detail(security_id: Annotated[str, Path(max_length=32)]):
        return invoke(service.get, security_id)

    @router.get("/{security_id}/references")
    def references(security_id: Annotated[str, Path(max_length=32)]):
        return invoke(service.references, security_id)

    @router.post("/{security_id}/verify-gaps", status_code=202)
    def verify_gaps(
        security_id: Annotated[str, Path(max_length=32)], request: GapVerificationRequest
    ):
        return invoke(
            manager.submit_gap_verification,
            security_id,
            **request.model_dump(),
            actor=actor,
        )

    @router.patch("/{security_id}")
    def change(security_id: Annotated[str, Path(max_length=32)], request: ManagedStockChange):
        return invoke(service.change, security_id, **request.model_dump(), actor=actor)

    return router
