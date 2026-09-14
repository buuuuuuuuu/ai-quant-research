from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ai_quant_assistant import __version__
from ai_quant_assistant.ai.copilot import CopilotService, CopilotTaskManager
from ai_quant_assistant.ai.planner import create_planner
from ai_quant_assistant.ai.preflight import CoveragePreflight
from ai_quant_assistant.ai.repository import CopilotRepository
from ai_quant_assistant.ai.tool_registry import copilot_tool_catalog, require_allowed_tool
from ai_quant_assistant.api.auth import SESSION_COOKIE, WebAuthenticator, safe_next_path
from ai_quant_assistant.api.backtest_tasks import BacktestTaskManager
from ai_quant_assistant.api.managed_stock_routes import managed_stock_router
from ai_quant_assistant.api.managed_stock_tasks import ManagedStockTaskManager
from ai_quant_assistant.api.operations_tasks import OperationsTaskManager
from ai_quant_assistant.api.research_data_routes import research_data_router
from ai_quant_assistant.api.run_center_routes import run_center_router
from ai_quant_assistant.api.run_repository import BacktestRunRepository, FactorLabRunRepository
from ai_quant_assistant.api.schemas import (
    BackupPruneRequest,
    BackupRestoreRequest,
    BatchInterimReportEventStudyRequest,
    BatchResearchRunResponse,
    CopilotConfirmRequest,
    CopilotConversationRequest,
    CopilotMessageRequest,
    DocumentCompareRequest,
    DocumentEventReviewRequest,
    DocumentEventStudyRequest,
    DocumentQuestionRequest,
    FactorBacktestRequest,
    FactorLabRequest,
    IncrementalSyncRequest,
    InterimReportEventStudyRequest,
    PointInTimeScreenRequest,
    ResearchRunResponse,
    SaveWatchlistRequest,
    SecurityMasterRefreshRequest,
    StockPoolCreateRequest,
    StockPoolDuplicateRequest,
    StockPoolMembersAddRequest,
    StockPoolMembersRemoveRequest,
    StockPoolMemberUpdateRequest,
    StockPoolRevisionRequest,
    StockPoolSyncRequest,
    StockPoolUpdateRequest,
)
from ai_quant_assistant.api.security_master_tasks import SecurityMasterTaskManager
from ai_quant_assistant.api.stock_pool_tasks import StockPoolSyncTaskManager
from ai_quant_assistant.config import Settings
from ai_quant_assistant.documents import (
    DocumentEventService,
    DocumentEventTaskManager,
    DocumentIntelligenceService,
    DocumentTaskManager,
)
from ai_quant_assistant.errors import QuantResearchError
from ai_quant_assistant.providers.akshare_provider import AkShareProvider
from ai_quant_assistant.providers.base import MarketDataProvider
from ai_quant_assistant.providers.fixture_provider import FixtureProvider
from ai_quant_assistant.providers.local_provider import LocalFirstProvider
from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS
from ai_quant_assistant.research.batch_service import BatchInterimReportResearchService
from ai_quant_assistant.research.factor_backtest import FactorBacktestService
from ai_quant_assistant.research.factor_lab import FactorLabService
from ai_quant_assistant.research.screener import PointInTimeScreener
from ai_quant_assistant.research.service import InterimReportResearchService
from ai_quant_assistant.storage.data_explorer import DataExplorer
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.managed_stocks import ManagedStockService
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
from ai_quant_assistant.storage.research_data_plans import ResearchDataPlans
from ai_quant_assistant.storage.security_master_refresh import SecurityMasterRefreshService
from ai_quant_assistant.storage.stock_pools import StockPoolConflictError, StockPoolService
from ai_quant_assistant.storage.task_repository import OperationTaskRepository
from ai_quant_assistant.storage.universe import UniverseRepository


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
    stock_pools = StockPoolService(database)
    from ai_quant_assistant.research.dashboard_brief import ResearchDashboardBrief

    research_dashboard = ResearchDashboardBrief(database)
    managed_stock_tasks = ManagedStockTaskManager(ManagedStockService(database, resolved_settings))
    research_data_tasks = ManagedStockTaskManager(ResearchDataPlans(database, resolved_settings))
    security_master_tasks = SecurityMasterTaskManager(
        SecurityMasterRefreshService.from_settings(
            database, resolved_settings, task_repository, operation_lock,
        )
    )
    stock_pool_sync_tasks = StockPoolSyncTaskManager(
        pools=stock_pools,
        repository=task_repository,
        sync_service=sync_service,
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
    copilot_repository = CopilotRepository(database)
    data_explorer = DataExplorer(database)
    screener = PointInTimeScreener(database)
    document_service = DocumentIntelligenceService(database, resolved_settings)
    document_tasks = DocumentTaskManager(document_service, task_repository)
    document_event_service = DocumentEventService(database)
    document_event_tasks = DocumentEventTaskManager(document_event_service, task_repository)
    copilot_service = CopilotService(
        repository=copilot_repository,
        planner=create_planner(resolved_settings),
        preflight=CoveragePreflight(database),
        stock_pools=stock_pools,
    )
    copilot_tasks = CopilotTaskManager(
        plans=copilot_repository,
        tasks=task_repository,
        factor_backtests=FactorBacktestService(
            database=database, artifact_dir=resolved_settings.artifact_dir
        ),
        factor_lab=FactorLabService(database=database, artifact_dir=resolved_settings.artifact_dir),
        universe=UniverseRepository(database),
        stock_pools=stock_pools,
        operation_lock=operation_lock,
        research_dashboard=research_dashboard,
    )
    web_root = Path(__file__).resolve().parents[1] / "web"
    templates = Jinja2Templates(directory=web_root / "templates")
    asset_version = hashlib.sha256(
        b"".join(
            (web_root / "static" / name).read_bytes()
            for name in (
                "workbench.js", "managed-stocks.js", "research-data.js", "stock-research.js",
                "stock-research.css", "research-dashboard.js", "research-dashboard.css",
                "workbench.css", "stock-pools.css", "app-shell.css", "app-shell.js", "run-center.js",
            )
        )
    ).hexdigest()[:16]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        managed_stock_tasks.start()
        research_data_tasks.start()
        yield
        managed_stock_tasks.shutdown()
        research_data_tasks.shutdown()
        task_manager.shutdown()
        factor_lab_manager.shutdown()
        copilot_tasks.shutdown()
        operation_manager.shutdown()
        document_tasks.shutdown()
        document_event_tasks.shutdown()
        stock_pool_sync_tasks.shutdown()
        security_master_tasks.shutdown()

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
    app.state.universe_repository = UniverseRepository(database)
    app.state.copilot_repository = copilot_repository
    app.state.copilot_service = copilot_service
    app.state.copilot_tasks = copilot_tasks
    app.state.data_explorer = data_explorer
    app.state.screener = screener
    app.state.document_service = document_service
    app.state.document_tasks = document_tasks
    app.state.document_event_service = document_event_service
    app.state.document_event_tasks = document_event_tasks
    app.state.stock_pools = stock_pools
    app.state.managed_stock_tasks = managed_stock_tasks
    app.state.research_data_tasks = research_data_tasks
    app.include_router(research_data_router(research_data_tasks, resolved_settings.web_username, copilot_repository))
    app.include_router(managed_stock_router(managed_stock_tasks, resolved_settings.web_username))
    from ai_quant_assistant.api.research_dashboard_routes import research_dashboard_router
    from ai_quant_assistant.api.stock_research_routes import stock_research_router
    from ai_quant_assistant.research.stock_workbench import StockResearchWorkbench
    app.include_router(stock_research_router(StockResearchWorkbench(database), templates, asset_version, resolved_settings.web_username))
    app.include_router(research_dashboard_router(research_dashboard, templates, asset_version, resolved_settings.web_username))
    app.include_router(run_center_router(database, templates, asset_version, resolved_settings.web_username))
    app.state.stock_pool_sync_tasks = stock_pool_sync_tasks
    app.state.security_master_tasks = security_master_tasks
    app.mount("/assets", StaticFiles(directory=web_root / "static"), name="assets")

    def resolve_stock_selection(request: Any) -> tuple[list[str], dict[str, Any]]:
        requested = getattr(request, "symbols", None)
        pool_id = getattr(request, "stock_pool_id", None)
        revision = getattr(request, "stock_pool_revision", None)
        if requested is not None:
            symbols = list(dict.fromkeys(str(item).strip() for item in requested))
            canonical = json.dumps(symbols, ensure_ascii=False, separators=(",", ":"))
            return symbols, {
                "pool_id": None,
                "pool_revision": None,
                "resolved_symbols": symbols,
                "symbols_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "resolved_at": datetime.now(UTC).isoformat(),
                "source_type": "explicit",
                "as_of": None,
                "history_verified": False,
                "warning": (
                    "显式或手工股票列表不代表历史时点真实成分，历史研究可能存在幸存者偏差。"
                ),
            }
        context = stock_pools.resolve(pool_id, revision=revision)
        return list(context["resolved_symbols"]), context

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
        next_url = path + ("?" + request.url.query if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(next_url, safe='/')}", status_code=303)

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
    @app.get("/data", include_in_schema=False)
    @app.get("/data/pools", include_in_schema=False)
    @app.get("/research", include_in_schema=False)
    @app.get("/research/copilot", include_in_schema=False)
    @app.get("/system", include_in_schema=False)
    @app.get("/backtests/new", include_in_schema=False)
    @app.get("/backtests/factors", include_in_schema=False)
    @app.get("/backtests/screen", include_in_schema=False)
    def workbench(request: Request):
        page_id, page_title = {
            "/": ("overview", "总览"), "/data": ("data", "数据与股票池"),
            "/data/pools": ("pools", "股票池管理"), "/research": ("research", "研究中心"),
            "/research/copilot": ("copilot", "AI 研究助手"), "/system": ("system", "系统管理"),
            "/backtests/new": ("backtest", "财务因子回测"),
            "/backtests/factors": ("lab", "因子实验"),
            "/backtests/screen": ("screen", "条件选股"),
        }[request.url.path]
        factors = [
            {"code": code, "label": definition["label"], "unit": definition["unit"]}
            for code, definition in FINANCIAL_METRICS.items()
        ]
        return templates.TemplateResponse(
            request=request,
            name="workbench.html",
            context={
                "version": __version__,
                "page_id": page_id,
                "page_title": page_title,
                "factors": factors,
                "asset_version": asset_version,
                "screen_fields": screener.fields()["fields"],
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
            "universe": "/api/v1/universe/overview",
            "screens": "/api/v1/screens/runs",
            "copilot": "/api/v1/copilot/conversations",
            "data_explorer": "/api/v1/data/datasets",
            "documents": "/api/v1/documents/announcements/{announcement_id}",
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/data/overview")
    def data_overview() -> dict[str, Any]:
        return database.research_overview()

    def explorer_response(payload: dict[str, Any], output: str, filename: str) -> Any:
        if output != "csv":
            return payload
        rows = payload.get("items", [])
        stream = io.StringIO()
        columns = list(rows[0]) if rows else []
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        if columns:
            writer.writeheader()
            safe_rows = []
            for row in rows:
                safe_rows.append(
                    {
                        key: f"'{value}"
                        if isinstance(value, str)
                        and value.startswith(("=", "+", "-", "@", "\t", "\r"))
                        else value
                        for key, value in row.items()
                    }
                )
            writer.writerows(safe_rows)
        safe_filename = "".join(char for char in filename if char.isalnum() or char in "-_")
        return Response(
            "\ufeff" + stream.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{safe_filename}.csv"'},
        )

    @app.get("/api/v1/data/datasets")
    def data_datasets() -> dict[str, Any]:
        return data_explorer.datasets()

    @app.get("/api/v1/data/securities")
    def data_securities(
        query: str = "", dataset: str = "all", page: int = 1, page_size: int = 50,
        managed_only: bool = False,
    ) -> dict[str, Any]:
        try:
            return data_explorer.securities(
                query=query, dataset=dataset, page=page, page_size=page_size, managed_only=managed_only
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/data/securities/{symbol}")
    def data_security(symbol: str) -> dict[str, Any]:
        try:
            return data_explorer.security(symbol)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="security not found") from exc

    @app.get("/api/v1/data/securities/{symbol}/prices")
    def data_security_prices(
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjustment: str = "qfq",
        page: int = 1,
        page_size: int = 50,
        order: str = "desc",
        output: str = "json",
    ) -> Any:
        try:
            payload = data_explorer.prices(
                symbol,
                start_date=start_date,
                end_date=end_date,
                adjustment=adjustment,
                page=page,
                page_size=page_size,
                order=order,
            )
            return explorer_response(payload, output, f"{symbol}-{adjustment}-prices")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/data/securities/{symbol}/announcements")
    def data_security_announcements(
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        report_type: str | None = None,
        page: int = 1,
        page_size: int = 50,
        output: str = "json",
    ) -> Any:
        try:
            payload = data_explorer.announcements(
                symbol,
                start_date=start_date,
                end_date=end_date,
                report_type=report_type,
                page=page,
                page_size=page_size,
            )
            return explorer_response(payload, output, f"{symbol}-announcements")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/data/announcements/{announcement_id}")
    def data_announcement_detail(announcement_id: str) -> dict[str, Any]:
        try:
            return data_explorer.announcement_detail(announcement_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="announcement not found") from exc

    @app.get("/api/v1/data/securities/{symbol}/financial-reports")
    def data_security_financial_reports(
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        report_type: str | None = None,
        page: int = 1,
        page_size: int = 50,
        output: str = "json",
    ) -> Any:
        try:
            payload = data_explorer.financial_reports(
                symbol,
                start_date=start_date,
                end_date=end_date,
                report_type=report_type,
                page=page,
                page_size=page_size,
            )
            return explorer_response(payload, output, f"{symbol}-financial-reports")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/data/securities/{symbol}/financial-metrics")
    def data_security_financial_metrics(
        symbol: str,
        report_id: str | None = None,
        metric_code: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        page: int = 1,
        page_size: int = 50,
        output: str = "json",
    ) -> Any:
        payload = data_explorer.financial_metrics(
            symbol,
            report_id=report_id,
            metric_code=metric_code,
            start_date=start_date,
            end_date=end_date,
            page=page,
            page_size=page_size,
        )
        return explorer_response(payload, output, f"{symbol}-financial-metrics")

    @app.get("/api/v1/data/financial-reports/{report_id}")
    def data_financial_report_detail(report_id: str) -> dict[str, Any]:
        try:
            return data_explorer.financial_report_detail(report_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="financial report not found") from exc

    @app.get("/api/v1/data/benchmarks")
    def data_benchmarks(page: int = 1, page_size: int = 50) -> dict[str, Any]:
        return data_explorer.benchmarks(page=page, page_size=page_size)

    @app.get("/api/v1/data/benchmarks/{symbol}/prices")
    def data_benchmark_prices(
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        page: int = 1,
        page_size: int = 50,
        order: str = "desc",
        output: str = "json",
    ) -> Any:
        try:
            payload = data_explorer.benchmark_prices(
                symbol,
                start_date=start_date,
                end_date=end_date,
                page=page,
                page_size=page_size,
                order=order,
            )
            return explorer_response(payload, output, f"{symbol}-benchmark-prices")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/data/lineage/{dataset}/{record_id}")
    def data_lineage(dataset: str, record_id: str) -> dict[str, Any]:
        try:
            return data_explorer.lineage(dataset, record_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="data record not found") from exc

    @app.post("/api/v1/documents/announcements/{announcement_id}/ingest", status_code=202)
    def ingest_announcement_document(announcement_id: str) -> dict[str, Any]:
        try:
            document_service.list_announcement_documents(announcement_id)
            task = document_tasks.submit(announcement_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="announcement not found") from exc
        audit_repository.record(
            "announcement_document_ingest_requested",
            actor=resolved_settings.web_username,
            object_type="announcement",
            object_id=announcement_id,
            details={"task_id": task["task_id"]},
        )
        return task

    @app.get("/api/v1/documents/announcements/{announcement_id}")
    def list_announcement_documents(announcement_id: str) -> dict[str, Any]:
        try:
            return document_service.list_announcement_documents(announcement_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="announcement not found") from exc

    @app.get("/api/v1/documents/securities/{symbol}")
    def list_security_documents(symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        if len(symbol) != 6 or not symbol.isdigit():
            raise HTTPException(status_code=422, detail="symbol must be a six-digit code")
        return document_service.list_security_documents(symbol, limit=limit)

    @app.get("/api/v1/documents/{document_id}")
    def get_document(document_id: str) -> dict[str, Any]:
        try:
            return document_service.get_document(document_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="document not found") from exc

    @app.get("/api/v1/documents/{document_id}/pages/{page_number}")
    def get_document_page(document_id: str, page_number: int) -> dict[str, Any]:
        try:
            return document_service.page(document_id, page_number)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="document page not found") from exc

    @app.get("/api/v1/documents/{document_id}/search")
    def search_document(document_id: str, q: str, limit: int = 20) -> dict[str, Any]:
        try:
            return document_service.search([document_id], q, limit=limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="document not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/documents/{document_id}/file", response_class=FileResponse)
    def download_document(document_id: str) -> FileResponse:
        try:
            path = document_service.safe_path(document_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="document file not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{document_id}.pdf"',
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
            },
        )

    @app.post("/api/v1/documents/questions", status_code=201)
    def ask_documents(request: DocumentQuestionRequest) -> dict[str, Any]:
        try:
            answer = document_service.ask(request.document_ids, request.question)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="document not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        audit_repository.record(
            "document_question_answered",
            actor=resolved_settings.web_username,
            object_type="document_question",
            object_id=answer["question_id"],
            outcome="warning" if answer["status"] != "answered" else "success",
            details={"document_ids": request.document_ids, "status": answer["status"]},
        )
        return answer

    @app.get("/api/v1/documents/questions/{question_id}")
    def get_document_question(question_id: str) -> dict[str, Any]:
        try:
            return document_service.get_question(question_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="document question not found") from exc

    @app.post("/api/v1/documents/compare", status_code=201)
    def compare_documents(request: DocumentCompareRequest) -> dict[str, Any]:
        try:
            return document_service.compare(request.document_ids, request.focus)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="document not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/documents/{document_id}/events/extract", status_code=202)
    def extract_document_events(document_id: str) -> dict[str, Any]:
        try:
            document_event_service.list_document_events(document_id)
            task = document_event_tasks.submit(document_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="document not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        audit_repository.record(
            "document_event_extraction_requested",
            actor=resolved_settings.web_username,
            object_type="announcement_document",
            object_id=document_id,
            details={"task_id": task["task_id"]},
        )
        return task

    @app.get("/api/v1/documents/{document_id}/events")
    def list_document_events(document_id: str) -> dict[str, Any]:
        try:
            return document_event_service.list_document_events(document_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="document not found") from exc

    @app.get("/api/v1/document-events")
    def list_point_in_time_document_events(
        as_of: Annotated[datetime | None, Query()] = None,
        symbol: Annotated[str | None, Query(pattern=r"^\d{6}$")] = None,
        event_type: Annotated[str | None, Query(max_length=40)] = None,
        status: Annotated[
            str, Query(pattern="^(accepted|rejected|needs_review|insufficient)$")
        ] = "accepted",
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> dict[str, Any]:
        as_of = as_of or datetime.now(UTC)
        if as_of.tzinfo is None:
            raise HTTPException(status_code=422, detail="as_of must include an explicit timezone")
        allowed_types = {
            "performance_forecast",
            "dividend",
            "share_repurchase",
            "major_contract",
            "shareholder_change",
        }
        if event_type is not None and event_type not in allowed_types:
            raise HTTPException(status_code=422, detail="unsupported document event type")
        return document_event_service.list_events(
            as_of=as_of,
            symbol=symbol,
            event_type=event_type,
            status=status,
            limit=limit,
        )

    @app.get("/api/v1/document-events/{event_id}")
    def get_document_event(event_id: str) -> dict[str, Any]:
        try:
            return document_event_service.get_event(event_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="document event not found") from exc

    @app.post("/api/v1/document-events/{event_id}/review")
    def review_document_event(event_id: str, request: DocumentEventReviewRequest) -> dict[str, Any]:
        try:
            event = document_event_service.review(
                event_id,
                status=request.status,
                actor=resolved_settings.web_username,
                note=request.note,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="document event not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit_repository.record(
            "document_event_reviewed",
            actor=resolved_settings.web_username,
            object_type="document_event",
            object_id=event_id,
            details={"status": request.status, "note": request.note},
        )
        return event

    @app.post("/api/v1/document-events/{event_id}/event-study", status_code=201)
    def run_document_event_study(
        event_id: str, request: DocumentEventStudyRequest
    ) -> dict[str, Any]:
        try:
            result = document_event_service.run_event_study(
                event_id,
                provider=_provider(request.source, resolved_settings),
                as_of=request.as_of,
                benchmark_symbol=request.benchmark_symbol,
                windows=tuple(request.windows),
                adjustment=request.adjustment,
                round_trip_cost_bps=request.round_trip_cost_bps,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="document event not found") from exc
        except (QuantResearchError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        audit_repository.record(
            "document_event_study_completed",
            actor=resolved_settings.web_username,
            object_type="document_event_research_run",
            object_id=result["run_id"],
            details={"event_id": event_id, "as_of": request.as_of.isoformat()},
        )
        return result

    @app.get("/api/v1/copilot/tools")
    def copilot_tools() -> list[dict[str, object]]:
        return copilot_tool_catalog()

    @app.post("/api/v1/copilot/conversations", status_code=201)
    def create_copilot_conversation(request: CopilotConversationRequest) -> dict[str, Any]:
        conversation = copilot_service.create_conversation(request.title)
        audit_repository.record(
            "copilot_conversation_created",
            actor=resolved_settings.web_username,
            object_type="copilot_conversation",
            object_id=conversation["conversation_id"],
        )
        return conversation

    @app.get("/api/v1/copilot/conversations")
    def list_copilot_conversations() -> list[dict[str, Any]]:
        return copilot_repository.list_conversations()

    @app.get("/api/v1/copilot/conversations/{conversation_id}")
    def get_copilot_conversation(conversation_id: str) -> dict[str, Any]:
        try:
            return copilot_service.conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="copilot conversation not found") from exc

    @app.post("/api/v1/copilot/conversations/{conversation_id}/messages", status_code=201)
    def create_copilot_message(
        conversation_id: str, request: CopilotMessageRequest
    ) -> dict[str, Any]:
        try:
            plan = copilot_service.propose(conversation_id, request.content)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="copilot conversation not found") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        audit_repository.record(
            "copilot_plan_created",
            actor=resolved_settings.web_username,
            object_type="research_plan",
            object_id=plan["plan_id"],
            outcome="warning" if plan["status"] == "blocked" else "success",
            details={"tool_name": plan["tool_name"], "status": plan["status"]},
        )
        return plan

    @app.get("/api/v1/copilot/plans/{plan_id}")
    def get_copilot_plan(plan_id: str) -> dict[str, Any]:
        try:
            plan = copilot_repository.get_plan(plan_id)
            if plan.get("task_id"):
                try:
                    plan["task"] = task_repository.get(plan["task_id"])
                except KeyError:
                    plan["task"] = None
            return plan
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="research plan not found") from exc

    @app.post("/api/v1/copilot/plans/{plan_id}/preflight")
    def refresh_copilot_preflight(plan_id: str) -> dict[str, Any]:
        try:
            return copilot_service.refresh_preflight(plan_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="research plan not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/copilot/plans/{plan_id}/confirm")
    def confirm_copilot_plan(plan_id: str, request: CopilotConfirmRequest) -> dict[str, Any]:
        try:
            plan = copilot_service.confirm(
                plan_id,
                revision=request.revision,
                actor=resolved_settings.web_username,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="research plan not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit_repository.record(
            "copilot_plan_confirmed",
            actor=resolved_settings.web_username,
            object_type="research_plan",
            object_id=plan_id,
            details={"revision": request.revision},
        )
        return plan

    @app.post("/api/v1/copilot/plans/{plan_id}/execute", status_code=202)
    def execute_copilot_plan(plan_id: str) -> dict[str, Any]:
        try:
            task = copilot_tasks.submit(plan_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="research plan not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit_repository.record(
            "copilot_plan_execution_requested",
            actor=resolved_settings.web_username,
            object_type="research_plan",
            object_id=plan_id,
            details={"task_id": task["task_id"]},
        )
        return task

    @app.get("/api/v1/universe/overview")
    def universe_overview(as_of: str | None = None) -> dict[str, Any]:
        try:
            return app.state.universe_repository.overview(as_of or datetime.now().isoformat())
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/universe/build")
    def build_universe(
        as_of: str,
        mode: str = "all_available",
        benchmark_symbol: str = "csi000300",
        symbols: str = "",
        limit: int | None = None,
        classification_system: str = "provider",
        require_verified_industry: bool = False,
    ) -> dict[str, Any]:
        try:
            parsed = tuple(
                item.strip() for item in symbols.replace(",", " ").split() if item.strip()
            )
            return app.state.universe_repository.build(
                as_of=as_of,
                mode=mode,
                symbols=parsed,
                benchmark_symbol=benchmark_symbol,
                limit=limit,
                classification_system=classification_system,
                require_history=require_verified_industry,
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/screens/fields")
    def screen_fields() -> dict[str, Any]:
        return screener.fields()

    @app.post("/api/v1/screens/preflight")
    def screen_preflight(request: PointInTimeScreenRequest) -> dict[str, Any]:
        try:
            symbols, context = resolve_stock_selection(request)
            result = screener.preflight(
                request.to_spec(resolved_symbols=symbols, stock_pool=context)
            )
            result["research_data"] = research_data_tasks.service.readiness.run(
                "screen", request.model_dump(mode="json")
            )
            return result
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool not found") from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/screens/runs", status_code=201)
    def create_screen_run(request: PointInTimeScreenRequest) -> dict[str, Any]:
        try:
            symbols, context = resolve_stock_selection(request)
            run = screener.run(request.to_spec(resolved_symbols=symbols, stock_pool=context))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool not found") from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        audit_repository.record(
            "point_in_time_screen_succeeded",
            actor=resolved_settings.web_username,
            object_type="screen_run",
            object_id=run["run_id"],
            details={
                "as_of": request.as_of.isoformat(),
                "candidate_count": run["candidate_count"],
                "selected_count": run["selected_count"],
            },
        )
        return run

    @app.get("/api/v1/screens/runs")
    def list_screen_runs(limit: int = 20) -> list[dict[str, Any]]:
        return screener.list_runs(limit=limit)

    @app.get("/api/v1/screens/runs/{run_id}")
    def get_screen_run(run_id: str) -> dict[str, Any]:
        try:
            return screener.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="screen run not found") from exc

    @app.get("/api/v1/screens/runs/{run_id}/results.csv")
    def download_screen_results(run_id: str, selected_only: bool = False) -> Response:
        try:
            run = screener.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="screen run not found") from exc
        conditions = [item["field"] for item in run["spec"]["conditions"]]
        stream = io.StringIO()
        columns = [
            "symbol",
            "name",
            "industry",
            "selected",
            "rank",
            *conditions,
            "report_period_end",
            "report_published_at",
            "trade_date",
            "evaluations",
        ]
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for item in run["items"]:
            if selected_only and not item["selected"]:
                continue
            financial = item["evidence"].get("financial", {})
            market = item["evidence"].get("market", {})
            row = {
                "symbol": item["symbol"],
                "name": item["name"],
                "industry": item["industry"],
                "selected": item["selected"],
                "rank": item["rank"],
                "report_period_end": financial.get("report_period_end"),
                "report_published_at": financial.get("published_at"),
                "trade_date": market.get("trade_date"),
                "evaluations": "; ".join(
                    f"{evaluation['label']}:{evaluation['status']}"
                    for evaluation in item["evaluations"]
                ),
            }
            row.update({field: item["values"].get(field) for field in conditions})
            writer.writerow(
                {
                    key: f"'{value}"
                    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r"))
                    else value
                    for key, value in row.items()
                }
            )
        return Response(
            "\ufeff" + stream.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{run_id}-screen-results.csv"'},
        )

    @app.post("/api/v1/screens/runs/{run_id}/watchlists", status_code=201)
    def save_screen_watchlist(run_id: str, request: SaveWatchlistRequest) -> dict[str, Any]:
        try:
            watchlist = screener.save_watchlist(run_id, request.name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="screen run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit_repository.record(
            "screen_watchlist_saved",
            actor=resolved_settings.web_username,
            object_type="saved_watchlist",
            object_id=watchlist["watchlist_id"],
            details={"screen_run_id": run_id, "symbol_count": len(watchlist["symbols"])},
        )
        return watchlist

    @app.get("/api/v1/watchlists")
    def list_saved_watchlists(limit: int = 50) -> list[dict[str, Any]]:
        return screener.list_watchlists(limit=limit)

    @app.get("/api/v1/stock-pools")
    def list_stock_pools(
        include_archived: bool = False,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> list[dict[str, Any]]:
        return stock_pools.list(include_archived=include_archived, limit=limit)

    @app.post("/api/v1/stock-pools", status_code=201)
    def create_stock_pool(request: StockPoolCreateRequest) -> dict[str, Any]:
        try:
            return stock_pools.create(
                request.name,
                description=request.description,
                source_type=request.source_type,
                symbols=request.symbols,
                actor=resolved_settings.web_username,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/stock-pools/securities/search")
    def search_stock_pool_securities(
        query: Annotated[str, Query(min_length=1, max_length=120)],
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ) -> list[dict[str, Any]]:
        return stock_pools.search_securities(query, limit=limit)

    @app.post("/api/v1/stock-pools/securities/refresh", status_code=202)
    def refresh_security_master(request: SecurityMasterRefreshRequest) -> dict[str, Any]:
        return security_master_tasks.submit(actor=resolved_settings.web_username)

    @app.get("/api/v1/stock-pools/{pool_id}")
    def get_stock_pool(
        pool_id: str, revision: Annotated[int | None, Query(ge=1)] = None
    ) -> dict[str, Any]:
        try:
            return stock_pools.get(pool_id, revision=revision)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool not found") from exc

    @app.patch("/api/v1/stock-pools/{pool_id}")
    def update_stock_pool(pool_id: str, request: StockPoolUpdateRequest) -> dict[str, Any]:
        try:
            return stock_pools.update(
                pool_id,
                expected_revision=request.expected_revision,
                name=request.name,
                description=request.description,
                actor=resolved_settings.web_username,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool not found") from exc
        except StockPoolConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/v1/stock-pools/{pool_id}")
    def archive_stock_pool(
        pool_id: str, expected_revision: Annotated[int, Query(ge=1)]
    ) -> dict[str, Any]:
        try:
            return stock_pools.archive(
                pool_id,
                expected_revision=expected_revision,
                actor=resolved_settings.web_username,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool not found") from exc
        except StockPoolConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/stock-pools/{pool_id}/members")
    def add_stock_pool_members(
        pool_id: str, request: StockPoolMembersAddRequest
    ) -> dict[str, Any]:
        try:
            return stock_pools.add_members(
                pool_id,
                request.symbols,
                expected_revision=request.expected_revision,
                actor=resolved_settings.web_username,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool not found") from exc
        except StockPoolConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.patch("/api/v1/stock-pools/{pool_id}/members/{security_id}")
    def update_stock_pool_member(
        pool_id: str, security_id: str, request: StockPoolMemberUpdateRequest
    ) -> dict[str, Any]:
        try:
            return stock_pools.update_member(
                pool_id,
                security_id,
                expected_revision=request.expected_revision,
                note=request.note,
                display_order=request.display_order,
                actor=resolved_settings.web_username,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool member not found") from exc
        except StockPoolConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/stock-pools/{pool_id}/members/remove")
    def remove_stock_pool_members(
        pool_id: str, request: StockPoolMembersRemoveRequest
    ) -> dict[str, Any]:
        try:
            return stock_pools.remove_members(
                pool_id,
                request.security_ids,
                expected_revision=request.expected_revision,
                actor=resolved_settings.web_username,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool member not found") from exc
        except StockPoolConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/v1/stock-pools/{pool_id}/members/{security_id}")
    def remove_stock_pool_member(
        pool_id: str,
        security_id: str,
        expected_revision: Annotated[int, Query(ge=1)],
    ) -> dict[str, Any]:
        try:
            return stock_pools.remove_member(
                pool_id,
                security_id,
                expected_revision=expected_revision,
                actor=resolved_settings.web_username,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool member not found") from exc
        except StockPoolConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/stock-pools/{pool_id}/revisions")
    def list_stock_pool_revisions(
        pool_id: str, limit: Annotated[int, Query(ge=1, le=100)] = 100
    ) -> list[dict[str, Any]]:
        try:
            return stock_pools.revisions(pool_id, limit=limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool not found") from exc

    @app.post("/api/v1/stock-pools/{pool_id}/set-default")
    def set_default_stock_pool(
        pool_id: str, request: StockPoolRevisionRequest
    ) -> dict[str, Any]:
        try:
            return stock_pools.set_default(
                pool_id,
                expected_revision=request.expected_revision,
                actor=resolved_settings.web_username,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool not found") from exc
        except StockPoolConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/stock-pools/{pool_id}/duplicate", status_code=201)
    def duplicate_stock_pool(
        pool_id: str, request: StockPoolDuplicateRequest
    ) -> dict[str, Any]:
        try:
            return stock_pools.duplicate(
                pool_id,
                expected_revision=request.expected_revision,
                name=request.name,
                actor=resolved_settings.web_username,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool not found") from exc
        except StockPoolConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/stock-pools/{pool_id}/sync-missing", status_code=202)
    def synchronize_stock_pool(
        pool_id: str, request: StockPoolSyncRequest
    ) -> dict[str, Any]:
        try:
            return stock_pool_sync_tasks.submit(
                pool_id,
                expected_revision=request.expected_revision,
                as_of=request.as_of,
                actor=resolved_settings.web_username,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool not found") from exc
        except StockPoolConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
        try:
            symbols, context = resolve_stock_selection(request)
            task = task_manager.submit(
                request.to_spec(resolved_symbols=symbols, stock_pool=context)
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        audit_repository.record(
            "backtest_requested",
            actor=resolved_settings.web_username,
            object_type="operation_task",
            object_id=task["task_id"],
            details={"factor": request.factor, "symbol_count": len(symbols)},
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
        try:
            symbols, context = resolve_stock_selection(request)
            task = factor_lab_manager.submit(
                request.to_spec(resolved_symbols=symbols, stock_pool=context)
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        audit_repository.record(
            "factor_lab_requested",
            actor=resolved_settings.web_username,
            object_type="operation_task",
            object_id=task["task_id"],
            details={"factor_count": len(request.factors), "symbol_count": len(symbols)},
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
            database=database,
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
            symbols, context = resolve_stock_selection(request)
            return service.run(
                request.to_spec(resolved_symbols=symbols, stock_pool=context)
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stock pool not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except QuantResearchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logging.exception("batch research run failed")
            raise HTTPException(
                status_code=502, detail=f"batch research run failed: {exc}"
            ) from exc

    return app
