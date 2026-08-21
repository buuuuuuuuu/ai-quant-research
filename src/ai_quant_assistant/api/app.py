from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ai_quant_assistant import __version__
from ai_quant_assistant.ai.tool_registry import require_allowed_tool
from ai_quant_assistant.api.backtest_tasks import BacktestTaskManager
from ai_quant_assistant.api.run_repository import BacktestRunRepository
from ai_quant_assistant.api.schemas import (
    BatchInterimReportEventStudyRequest,
    BatchResearchRunResponse,
    FactorBacktestRequest,
    InterimReportEventStudyRequest,
    ResearchRunResponse,
)
from ai_quant_assistant.config import Settings
from ai_quant_assistant.errors import QuantResearchError
from ai_quant_assistant.providers.akshare_provider import AkShareProvider
from ai_quant_assistant.providers.base import MarketDataProvider
from ai_quant_assistant.providers.fixture_provider import FixtureProvider
from ai_quant_assistant.providers.local_provider import LocalFirstProvider
from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS
from ai_quant_assistant.research.batch_service import BatchInterimReportResearchService
from ai_quant_assistant.research.factor_backtest import FactorBacktestService
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
    database = LocalDatabase(
        path=resolved_settings.duckdb_path,
        layout=DataLayout.from_root(resolved_settings.data_dir),
    )
    run_repository = BacktestRunRepository(resolved_settings.artifact_dir)
    task_manager = BacktestTaskManager(
        FactorBacktestService(database=database, artifact_dir=resolved_settings.artifact_dir)
    )
    web_root = Path(__file__).resolve().parents[1] / "web"
    templates = Jinja2Templates(directory=web_root / "templates")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        task_manager.shutdown()

    app = FastAPI(
        title="AI 量化研究助手 API",
        version=__version__,
        description="A 股本地数据、事件研究与 Point-in-Time 财务因子回测。",
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.backtest_tasks = task_manager
    app.state.backtest_runs = run_repository
    app.mount("/assets", StaticFiles(directory=web_root / "static"), name="assets")

    @app.get("/", include_in_schema=False)
    def workbench(request: Request):
        factors = [
            {"code": code, "label": definition["label"], "unit": definition["unit"]}
            for code, definition in FINANCIAL_METRICS.items()
        ]
        return templates.TemplateResponse(
            request=request,
            name="workbench.html",
            context={"version": __version__, "factors": factors},
        )

    @app.get("/api")
    def api_root() -> dict[str, Any]:
        return {
            "name": "AI 量化研究助手",
            "version": __version__,
            "docs": "/docs",
            "workbench": "/",
            "backtests": "/api/v1/backtests",
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/data/overview")
    def data_overview() -> dict[str, Any]:
        return database.research_overview()

    @app.post("/api/v1/backtests", status_code=202)
    def create_backtest(request: FactorBacktestRequest) -> dict[str, Any]:
        return task_manager.submit(request.to_spec())

    @app.get("/api/v1/backtests")
    def list_backtest_tasks() -> list[dict[str, Any]]:
        return task_manager.list()

    @app.get("/api/v1/backtests/{task_id}")
    def get_backtest_task(task_id: str) -> dict[str, Any]:
        try:
            return task_manager.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="backtest task not found") from exc

    @app.get("/api/v1/backtest-runs")
    def list_backtest_runs() -> list[dict[str, Any]]:
        return run_repository.list()

    @app.get("/api/v1/backtest-runs/{run_id}")
    def get_backtest_run(run_id: str) -> dict[str, Any]:
        try:
            return run_repository.detail(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="backtest run not found") from exc

    @app.get("/api/v1/backtest-runs/{run_id}/downloads/{filename}")
    def download_backtest_file(run_id: str, filename: str) -> FileResponse:
        try:
            path, media_type = run_repository.file(run_id, filename)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="backtest artifact not found") from exc
        return FileResponse(path, media_type=media_type, filename=f"{run_id}-{filename}")

    @app.get("/reports/backtests/{run_id}", include_in_schema=False)
    def backtest_report(run_id: str) -> FileResponse:
        try:
            path, _ = run_repository.file(run_id, "report.html")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="backtest report not found") from exc
        return FileResponse(path, media_type="text/html")

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
        local_database = database if request.source == "local" and database.path.is_file() else None
        service = BatchInterimReportResearchService(
            provider=_provider(request.source, resolved_settings),
            artifact_dir=resolved_settings.artifact_dir,
            database=local_database,
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
