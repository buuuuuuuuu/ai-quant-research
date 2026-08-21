from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ai_quant_assistant import __version__
from ai_quant_assistant.ai.tool_registry import require_allowed_tool
from ai_quant_assistant.api.auth import SESSION_COOKIE, WebAuthenticator, safe_next_path
from ai_quant_assistant.api.backtest_tasks import BacktestTaskManager
from ai_quant_assistant.api.operations_tasks import OperationsTaskManager
from ai_quant_assistant.api.run_repository import BacktestRunRepository, FactorLabRunRepository
from ai_quant_assistant.api.schemas import (
    BackupPruneRequest,
    BackupRestoreRequest,
    BatchInterimReportEventStudyRequest,
    BatchResearchRunResponse,
    FactorBacktestRequest,
    FactorLabRequest,
    IncrementalSyncRequest,
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
from ai_quant_assistant.research.factor_lab import FactorLabService
from ai_quant_assistant.research.service import InterimReportResearchService
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.monitoring import (
    OperationAlertRepository,
    OperationAuditRepository,
    PlatformMonitor,
)
from ai_quant_assistant.storage.operations import (
    DatabaseBackupService,
    ExclusiveOperationLock,
    IncrementalSyncService,
    OperationsDashboard,
)
from ai_quant_assistant.storage.task_repository import OperationTaskRepository


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
    authenticator = WebAuthenticator(resolved_settings)
    task_repository = OperationTaskRepository(database)
    if database.path.is_file():
        task_repository.recover_interrupted()
    operation_lock = ExclusiveOperationLock(resolved_settings.data_dir / ".operations.lock")
    audit_repository = OperationAuditRepository(database)
    alert_repository = OperationAlertRepository(database)
    monitor = PlatformMonitor(database, resolved_settings, alert_repository, audit_repository)
    backup_service = DatabaseBackupService(
        database=database,
        backup_dir=resolved_settings.resolved_backup_dir,
        tasks=task_repository,
        operation_lock=operation_lock,
        audit=audit_repository,
        retention_days=resolved_settings.backup_retention_days,
        minimum_count=resolved_settings.backup_minimum_count,
    )
    sync_service = IncrementalSyncService(
        database=database,
        settings=resolved_settings,
        provider=_provider("akshare", resolved_settings),
        tasks=task_repository,
        operation_lock=operation_lock,
        audit=audit_repository,
    )
    operation_manager = OperationsTaskManager(
        repository=task_repository,
        sync_service=sync_service,
        backup_service=backup_service,
    )
    dashboard = OperationsDashboard(
        database,
        task_repository,
        backup_service,
        alert_repository,
        audit_repository,
        monitor,
    )
    run_repository = BacktestRunRepository(resolved_settings.artifact_dir)
    task_manager = BacktestTaskManager(
        FactorBacktestService(database=database, artifact_dir=resolved_settings.artifact_dir),
        task_repository,
        operation_lock,
    )
    factor_lab_repository = FactorLabRunRepository(resolved_settings.artifact_dir)
    factor_lab_manager = BacktestTaskManager(
        FactorLabService(database=database, artifact_dir=resolved_settings.artifact_dir),
        task_repository,
        operation_lock,
        max_workers=1,
        task_type="factor_lab",
    )
    web_root = Path(__file__).resolve().parents[1] / "web"
    templates = Jinja2Templates(directory=web_root / "templates")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        task_manager.shutdown()
        factor_lab_manager.shutdown()
        operation_manager.shutdown()

    app = FastAPI(
        title="AI 量化研究助手 API",
        version=__version__,
        description="A 股本地数据、事件研究与 Point-in-Time 财务因子回测。",
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.backtest_tasks = task_manager
    app.state.backtest_runs = run_repository
    app.state.factor_lab_tasks = factor_lab_manager
    app.state.factor_lab_runs = factor_lab_repository
    app.state.operation_tasks = operation_manager
    app.state.task_repository = task_repository
    app.state.backup_service = backup_service
    app.state.sync_service = sync_service
    app.state.audit_repository = audit_repository
    app.state.alert_repository = alert_repository
    app.state.platform_monitor = monitor
    app.mount("/assets", StaticFiles(directory=web_root / "static"), name="assets")

    @app.middleware("http")
    async def require_authenticated_session(request: Request, call_next):
        path = request.url.path
        if (
            not authenticator.enabled
            or path == "/health"
            or path == "/login"
            or path.startswith("/assets/")
            or authenticator.valid_session(request.cookies.get(SESSION_COOKIE))
        ):
            return await call_next(request)
        if path.startswith("/api") or path == "/openapi.json":
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(path, safe='/')}", status_code=303)

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        if not authenticator.enabled:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "version": __version__,
                "next_path": safe_next_path(request.query_params.get("next")),
                "error": None,
            },
        )

    @app.post("/login", include_in_schema=False)
    async def login(request: Request):
        if not authenticator.enabled:
            return RedirectResponse("/", status_code=303)
        body = await request.body()
        if len(body) > 4096:
            raise HTTPException(status_code=413, detail="login request is too large")
        values = parse_qs(body.decode("utf-8", errors="replace"))
        username = values.get("username", [""])[0]
        password = values.get("password", [""])[0]
        next_path = safe_next_path(values.get("next", ["/"])[0])
        remote_host = request.client.host if request.client else "unknown"
        if not authenticator.allow_attempt(remote_host):
            audit_repository.record(
                "login_rate_limited",
                actor=username or "anonymous",
                outcome="warning",
                remote_host=remote_host,
            )
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "version": __version__,
                    "next_path": next_path,
                    "error": "登录失败次数过多，请 5 分钟后重试。",
                },
                status_code=429,
            )
        if not authenticator.verify_credentials(username, password):
            audit_repository.record(
                "login_failed",
                actor=username or "anonymous",
                outcome="failure",
                remote_host=remote_host,
            )
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "version": __version__,
                    "next_path": next_path,
                    "error": "用户名或密码不正确。",
                },
                status_code=401,
            )
        authenticator.clear_attempts(remote_host)
        audit_repository.record("login_succeeded", actor=username, remote_host=remote_host)
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            authenticator.create_session(),
            max_age=resolved_settings.web_session_ttl_seconds,
            httponly=True,
            secure=resolved_settings.web_session_secure,
            samesite="lax",
        )
        return response

    @app.post("/logout", include_in_schema=False)
    def logout(request: Request):
        audit_repository.record(
            "logout",
            actor=resolved_settings.web_username,
            remote_host=request.client.host if request.client else None,
        )
        response = RedirectResponse("/login" if authenticator.enabled else "/", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/", include_in_schema=False)
    def workbench(request: Request):
        factors = [
            {"code": code, "label": definition["label"], "unit": definition["unit"]}
            for code, definition in FINANCIAL_METRICS.items()
        ]
        return templates.TemplateResponse(
            request=request,
            name="workbench.html",
            context={
                "version": __version__,
                "factors": factors,
                "auth_enabled": authenticator.enabled,
                "username": resolved_settings.web_username,
            },
        )

    @app.get("/api")
    def api_root() -> dict[str, Any]:
        return {
            "name": "AI 量化研究助手",
            "version": __version__,
            "docs": "/docs",
            "workbench": "/",
            "backtests": "/api/v1/backtests",
            "factor_lab": "/api/v1/factor-lab",
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/data/overview")
    def data_overview() -> dict[str, Any]:
        return database.research_overview()

    @app.get("/api/v1/ops/status")
    def operations_status() -> dict[str, Any]:
        return dashboard.status()

    @app.get("/api/v1/ops/tasks")
    def list_operations_tasks() -> list[dict[str, Any]]:
        return task_repository.list()

    @app.get("/api/v1/ops/tasks/{task_id}")
    def get_operations_task(task_id: str) -> dict[str, Any]:
        try:
            return task_repository.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="operation task not found") from exc

    @app.post("/api/v1/ops/sync", status_code=202)
    def create_incremental_sync(request: IncrementalSyncRequest) -> dict[str, Any]:
        task = operation_manager.submit_sync(as_of=request.as_of)
        audit_repository.record(
            "sync_requested",
            actor=resolved_settings.web_username,
            object_type="operation_task",
            object_id=task["task_id"],
        )
        return task

    @app.post("/api/v1/ops/backup", status_code=202)
    def create_database_backup() -> dict[str, Any]:
        task = operation_manager.submit_backup()
        audit_repository.record(
            "backup_requested",
            actor=resolved_settings.web_username,
            object_type="operation_task",
            object_id=task["task_id"],
        )
        return task

    @app.get("/api/v1/ops/backups")
    def list_database_backups() -> list[dict[str, Any]]:
        return backup_service.list()

    @app.get("/api/v1/ops/backups/{backup_id}/verify")
    def verify_database_backup(backup_id: str) -> dict[str, Any]:
        try:
            return backup_service.verify(backup_id, actor=resolved_settings.web_username)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="database backup not found") from exc
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/ops/backups/{backup_id}/restore")
    def restore_database_backup(backup_id: str, request: BackupRestoreRequest) -> dict[str, Any]:
        try:
            return backup_service.restore(
                backup_id,
                confirm_backup_id=request.confirm_backup_id,
                actor=resolved_settings.web_username,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="database backup not found") from exc
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/ops/backups/prune")
    def prune_database_backups(request: BackupPruneRequest) -> dict[str, Any]:
        return backup_service.prune(dry_run=not request.apply, actor=resolved_settings.web_username)

    @app.get("/api/v1/ops/alerts")
    def list_operation_alerts(active_only: bool = True) -> list[dict[str, Any]]:
        return alert_repository.list(active_only=active_only)

    @app.post("/api/v1/ops/alerts/check")
    def check_operation_alerts() -> dict[str, Any]:
        return monitor.check()

    @app.post("/api/v1/ops/alerts/{alert_id}/acknowledge")
    def acknowledge_operation_alert(alert_id: str) -> dict[str, Any]:
        try:
            alert = alert_repository.acknowledge(alert_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="active platform alert not found") from exc
        audit_repository.record(
            "alert_acknowledged",
            actor=resolved_settings.web_username,
            object_type="operation_alert",
            object_id=alert_id,
        )
        return alert

    @app.get("/api/v1/ops/audit")
    def list_operation_audit(limit: int = 50) -> list[dict[str, Any]]:
        return audit_repository.list(limit=limit)

    @app.get("/api/v1/ops/quality")
    def data_quality_status() -> dict[str, Any]:
        return monitor.quality()

    @app.post("/api/v1/backtests", status_code=202)
    def create_backtest(request: FactorBacktestRequest) -> dict[str, Any]:
        task = task_manager.submit(request.to_spec())
        audit_repository.record(
            "backtest_requested",
            actor=resolved_settings.web_username,
            object_type="operation_task",
            object_id=task["task_id"],
            details={"factor": request.factor, "symbol_count": len(request.symbols)},
        )
        return task

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

    @app.post("/api/v1/factor-lab", status_code=202)
    def create_factor_lab(request: FactorLabRequest) -> dict[str, Any]:
        task = factor_lab_manager.submit(request.to_spec())
        audit_repository.record(
            "factor_lab_requested",
            actor=resolved_settings.web_username,
            object_type="operation_task",
            object_id=task["task_id"],
            details={"factor_count": len(request.factors), "symbol_count": len(request.symbols)},
        )
        return task

    @app.get("/api/v1/factor-lab")
    def list_factor_lab_tasks() -> list[dict[str, Any]]:
        return factor_lab_manager.list()

    @app.get("/api/v1/factor-lab/{task_id}")
    def get_factor_lab_task(task_id: str) -> dict[str, Any]:
        try:
            return factor_lab_manager.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="factor lab task not found") from exc

    @app.get("/api/v1/factor-lab-runs")
    def list_factor_lab_runs() -> list[dict[str, Any]]:
        return factor_lab_repository.list()

    @app.get("/api/v1/factor-lab-runs/{run_id}")
    def get_factor_lab_run(run_id: str) -> dict[str, Any]:
        try:
            return factor_lab_repository.detail(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="factor lab run not found") from exc

    @app.get("/api/v1/factor-lab-runs/{run_id}/downloads/{filename}")
    def download_factor_lab_file(run_id: str, filename: str) -> FileResponse:
        try:
            path, media_type = factor_lab_repository.file(run_id, filename)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="factor lab artifact not found") from exc
        return FileResponse(path, media_type=media_type, filename=f"{run_id}-{filename}")

    @app.get("/reports/factor-lab/{run_id}", include_in_schema=False)
    def factor_lab_report(run_id: str) -> FileResponse:
        try:
            path, _ = factor_lab_repository.file(run_id, "report.html")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="factor lab report not found") from exc
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
