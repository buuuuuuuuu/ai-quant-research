from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Request
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
from ai_quant_assistant.api.operations_tasks import OperationsTaskManager
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
    DocumentQuestionRequest,
    FactorBacktestRequest,
    FactorLabRequest,
    IncrementalSyncRequest,
    InterimReportEventStudyRequest,
    PointInTimeScreenRequest,
    ResearchRunResponse,
    SaveWatchlistRequest,
)
from ai_quant_assistant.config import Settings
from ai_quant_assistant.documents import DocumentIntelligenceService, DocumentTaskManager
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
    copilot_service = CopilotService(
        repository=copilot_repository,
        planner=create_planner(resolved_settings),
        preflight=CoveragePreflight(database),
    )
    copilot_tasks = CopilotTaskManager(
        plans=copilot_repository,
        tasks=task_repository,
        factor_backtests=FactorBacktestService(
            database=database, artifact_dir=resolved_settings.artifact_dir
        ),
        factor_lab=FactorLabService(database=database, artifact_dir=resolved_settings.artifact_dir),
        universe=UniverseRepository(database),
        operation_lock=operation_lock,
    )
    web_root = Path(__file__).resolve().parents[1] / "web"
    templates = Jinja2Templates(directory=web_root / "templates")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        task_manager.shutdown()
        factor_lab_manager.shutdown()
        copilot_tasks.shutdown()
        operation_manager.shutdown()
        document_tasks.shutdown()

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
        query: str = "", dataset: str = "all", page: int = 1, page_size: int = 50
    ) -> dict[str, Any]:
        try:
            return data_explorer.securities(
                query=query, dataset=dataset, page=page, page_size=page_size
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

    @app.post(
        "/api/v1/documents/announcements/{announcement_id}/ingest", status_code=202
    )
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
            return screener.preflight(request.to_spec())
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/screens/runs", status_code=201)
    def create_screen_run(request: PointInTimeScreenRequest) -> dict[str, Any]:
        try:
            run = screener.run(request.to_spec())
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
                    if isinstance(value, str)
                    and value.startswith(("=", "+", "-", "@", "\t", "\r"))
                    else value
                    for key, value in row.items()
                }
            )
        return Response(
            "\ufeff" + stream.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{run_id}-screen-results.csv"'
            },
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
