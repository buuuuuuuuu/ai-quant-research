from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException

from ai_quant_assistant import __version__
from ai_quant_assistant.ai.tool_registry import require_allowed_tool
from ai_quant_assistant.api.schemas import (
    BatchInterimReportEventStudyRequest,
    BatchResearchRunResponse,
    InterimReportEventStudyRequest,
    ResearchRunResponse,
)
from ai_quant_assistant.config import Settings
from ai_quant_assistant.errors import QuantResearchError
from ai_quant_assistant.providers.akshare_provider import AkShareProvider
from ai_quant_assistant.providers.base import MarketDataProvider
from ai_quant_assistant.providers.fixture_provider import FixtureProvider
from ai_quant_assistant.providers.local_provider import LocalFirstProvider
from ai_quant_assistant.research.batch_service import BatchInterimReportResearchService
from ai_quant_assistant.research.service import InterimReportResearchService
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout


def _provider(source: str, settings: Settings) -> MarketDataProvider:
    if source == "fixture":
        return FixtureProvider()
    if source in {"akshare", "local"}:
        fallback = AkShareProvider(
            cache_dir=settings.cache_dir,
            retries=settings.akshare_retries,
            timeout_seconds=settings.akshare_timeout_seconds,
        )
        if source == "local":
            return LocalFirstProvider(
                database=LocalDatabase(
                    path=settings.duckdb_path,
                    layout=DataLayout.from_root(settings.data_dir),
                ),
                fallback=fallback,
            )
        return fallback
    raise ValueError(f"unsupported source: {source}")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    resolved_settings.ensure_directories()
    logging.basicConfig(level=resolved_settings.log_level)
    app = FastAPI(
        title="AI 量化研究助手 API",
        version=__version__,
        description="A 股量化研究工具；当前实现半年报公告后超额收益黄金路径。",
    )

    @app.get("/")
    def root() -> dict[str, Any]:
        return {
            "name": "AI 量化研究助手",
            "version": __version__,
            "docs": "/docs",
            "golden_path": "/api/v1/research/interim-report-event-study",
            "batch_research": "/api/v1/research/batch-interim-report-event-study",
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post(
        "/api/v1/research/interim-report-event-study",
        response_model=ResearchRunResponse,
    )
    def run_interim_report_event_study(
        request: InterimReportEventStudyRequest,
    ) -> dict[str, Any]:
        require_allowed_tool("run_interim_report_event_study")
        provider = _provider(request.source, resolved_settings)
        service = InterimReportResearchService(
            provider=provider,
            artifact_dir=resolved_settings.artifact_dir,
            duckdb_path=resolved_settings.duckdb_path,
        )
        try:
            return service.run(request.to_spec()).api_payload()
        except QuantResearchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logging.exception("research run failed")
            raise HTTPException(status_code=502, detail=f"research run failed: {exc}") from exc

    @app.post(
        "/api/v1/research/batch-interim-report-event-study",
        response_model=BatchResearchRunResponse,
    )
    def run_batch_interim_report_event_study(
        request: BatchInterimReportEventStudyRequest,
    ) -> dict[str, Any]:
        require_allowed_tool("run_batch_interim_report_event_study")
        database = (
            LocalDatabase(
                path=resolved_settings.duckdb_path,
                layout=DataLayout.from_root(resolved_settings.data_dir),
            )
            if request.source == "local" and resolved_settings.duckdb_path.is_file()
            else None
        )
        service = BatchInterimReportResearchService(
            provider=_provider(request.source, resolved_settings),
            artifact_dir=resolved_settings.artifact_dir,
            database=database,
        )
        try:
            return service.run(request.to_spec())
        except QuantResearchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logging.exception("batch research run failed")
            raise HTTPException(
                status_code=502, detail=f"batch research run failed: {exc}"
            ) from exc

    return app
