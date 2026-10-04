from __future__ import annotations

import hashlib
import json
import multiprocessing
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from pathlib import Path
from threading import Barrier, Event

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_factor_backtest import _database
from test_financial_platform import FakeFinancialProvider, _services
from test_web_workbench import _settings

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.api.research_data_routes import research_data_router
from ai_quant_assistant.cli import main
from ai_quant_assistant.domain.models import FinancialSyncSpec
from ai_quant_assistant.research.readiness import ResearchReadiness
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.locks import InterProcessFileLock
from ai_quant_assistant.storage.managed_stocks import ManagedStockService
from ai_quant_assistant.storage.operations import DatabaseBackupService, ExclusiveOperationLock
from ai_quant_assistant.storage.research_data_plans import DataPlanConflict, ResearchDataPlans
from ai_quant_assistant.storage.schema import MIGRATIONS, apply_migrations
from ai_quant_assistant.storage.stock_pools import StockPoolService

PARAMETERS = {
    "symbols": ["600519", "000001", "300750"],
    "start_date": "2024-01-02",
    "end_date": "2024-03-08",
    "factor": "roe",
    "top_n": 1,
    "rebalance": "monthly",
}
SCREEN = {
    "symbols": ["600519"],
    "as_of": "2024-08-30",
    "conditions": [{"field": "roe", "operator": "gte", "value": 1}],
}
DAILY_SIGNAL = {
    "symbols": ["600519", "000001"],
    "start_date": "2024-01-04",
    "end_date": "2024-01-12",
    "candidate_factors": [{"code": "return_1", "weight": 1, "direction": "desc"}],
    "candidate_count": 1,
    "candidate_refresh": "daily",
    "buy_rules": [{"kind": "close_vs_ma", "operator": "above", "window": 2}],
    "sell_rules": [{"kind": "close_vs_ma", "operator": "below", "window": 2}],
    "max_positions": 1,
}


@pytest.fixture
def db(tmp_path):
    return _database(tmp_path)


def finance_gap(db):
    with db.connect() as c:
        c.execute(
            "DELETE FROM financial_metric WHERE security_id='SSE.600519' AND metric_code='roe'"
        )


def plan_service(db, provider=None):
    return ResearchDataPlans(
        db, provider_factory=lambda _: provider or FakeFinancialProvider(provider_notice=True)
    )


def confirm(service, plan, **kwargs):
    return service.confirm(
        plan["plan_id"],
        version=plan["version"],
        input_sha256=plan["input_sha256"],
        confirm=True,
        **kwargs,
    )


def test_single_stock_without_candidate_factor_checks_prices_but_not_financial_metrics(db):
    params = {**DAILY_SIGNAL, "symbols": ["600519"], "candidate_factors": []}
    report = ResearchReadiness(db).run("daily_signal", params)
    assert report["input"]["resolved_symbols"] == ["600519"]
    assert not any(item["dataset"] == "financial_metric" for item in report["checks"])
    assert any(item["dataset"] == "daily_bar" and item["symbol"] == "600519" for item in report["checks"])


def test_per_stock_per_signal_not_global_counts_and_existing_availability_unchanged(db):
    finance_gap(db)
    readiness = ResearchReadiness(db)
    result = readiness.run("factor_backtest", PARAMETERS)
    missing = [c for c in result["checks"] if c.get("metric") == "roe" and c["symbol"] == "600519"]
    assert missing[0]["missing_count"] == 3
    assert missing[0]["status"] == "missing"
    assert result["ready"]  # Explicitly disclosed exclusion, another eligible stock remains.
    assert not result["complete"] and "600519" in result["affected_symbols"]
    assert "exclude" in missing[0]["reason"]
    with db.connect(read_only=True) as c:
        assert (
            c.execute(
                "SELECT availability FROM managed_stock WHERE security_id='SSE.600519'"
            ).fetchone()[0]
            == "ready"
        )


def test_readonly_default_pool_freeze_and_plan_generation_never_network(db, monkeypatch):
    pools = StockPoolService(db)
    pool = pools.create("test", symbols=["600519", "000001"])
    pools.set_default(pool["pool_id"], expected_revision=1)
    monkeypatch.setattr(
        db, "initialize", lambda: pytest.fail("read-only preflight must not initialize")
    )
    readiness = ResearchReadiness(db)
    params = {key: value for key, value in PARAMETERS.items() if key != "symbols"}
    frozen = readiness.freeze("factor_backtest", params)
    assert frozen["resolved_symbols"] == ["600519", "000001"]
    service = ResearchDataPlans(
        db, provider_factory=lambda _: pytest.fail("planning must not download")
    )
    plan = service.create("factor_backtest", params)
    assert plan["status"] == "draft" and plan["last_task_id"] is None


def test_price_gaps_and_adjustments_are_independent_and_unknown_calendar(db):
    with db.connect() as c:
        c.execute(
            "DELETE FROM daily_bar WHERE security_id='SSE.600519' AND adjustment='qfq' AND trade_date='2024-02-07'"
        )
    result = ResearchReadiness(db).run("factor_backtest", PARAMETERS)
    checks = [
        c for c in result["checks"] if c.get("symbol") == "600519" and c["dataset"] == "daily_bar"
    ]
    assert {c["adjustment"]: c["status"] for c in checks} == {"none": "ready", "qfq": "missing"}
    assert checks[1]["missing_dates"] == ["2024-02-07"]
    assert not result["ready"]
    with db.connect() as c:
        c.execute("DELETE FROM trading_calendar")
    assert any(
        c["status"] == "unknown"
        for c in ResearchReadiness(db).run("factor_backtest", PARAMETERS)["checks"]
    )


def test_pit_end_of_day_and_strict_signal_cutoffs(db):
    with db.connect() as c:
        c.execute(
            "DELETE FROM financial_metric WHERE security_id='SSE.600519' AND report_id<>(SELECT MIN(report_id) FROM financial_report WHERE security_id='SSE.600519')"
        )
        c.execute(
            "DELETE FROM financial_report WHERE security_id='SSE.600519' AND report_id<>(SELECT MIN(report_id) FROM financial_report WHERE security_id='SSE.600519')"
        )
        c.execute(
            "UPDATE financial_report SET published_at='2024-02-01 15:00:00' WHERE security_id='SSE.600519'"
        )
        c.execute(
            "UPDATE financial_metric SET published_at='2024-02-01 15:00:00' WHERE security_id='SSE.600519'"
        )
    service = ResearchReadiness(db)
    backtest = service.run("factor_backtest", PARAMETERS)
    metric = next(
        c for c in backtest["checks"] if c.get("symbol") == "600519" and c.get("metric") == "roe"
    )
    assert metric["missing_as_of"] == ["2024-01-02T15:00:00", "2024-02-01T15:00:00"]
    before = service.run("screen", {**SCREEN, "as_of": "2024-01-31"})
    after = service.run("screen", {**SCREEN, "as_of": "2024-02-01"})
    assert not before["complete"] and after["complete"]


def test_screener_latest_whole_report_and_twenty_day_warmup(db):
    with db.connect() as c:
        c.execute(
            "INSERT INTO financial_report(report_id,security_id,report_period_end,report_type,published_at,availability_source,source,source_batch_id,raw_sha256) VALUES ('later','SSE.600519','2023-12-31','annual','2024-02-01','provider_notice','fixture','fixture','hash')"
        )
        c.execute(
            "INSERT INTO financial_metric(report_id,security_id,report_period_end,published_at,metric_code,metric_value,unit,source_batch_id) VALUES ('later','SSE.600519','2023-12-31','2024-02-01','revenue',10,'CNY','fixture')"
        )
    check = ResearchReadiness(db).run("screen", {**SCREEN, "as_of": "2024-02-02"})
    assert not check["complete"]  # Old ROE must not fill a field missing in latest report.
    price = ResearchReadiness(db).run(
        "screen",
        {
            **SCREEN,
            "as_of": "2024-02-02",
            "conditions": [{"field": "avg_amount_20d", "operator": "gt", "value": 0}],
        },
    )
    assert price["checks"][0]["rows"] == 20
    assert price["checks"][0]["required_start"] < "2024-02-02"


def test_lab_unions_signal_frequencies_and_metric_requirements(db):
    params = {
        key: value
        for key, value in PARAMETERS.items()
        if key not in {"factor", "top_n", "rebalance"}
    }
    params.update(
        factors=[{"code": "roe"}, {"code": "debt_asset_ratio"}],
        top_n_values=[1],
        rebalance_values=["monthly", "quarterly"],
    )
    result = ResearchReadiness(db).run("factor_lab", params)
    assert result["signal_count"] == 3
    assert {c.get("metric") for c in result["checks"] if c["dataset"] == "financial_metric"} == {
        "roe",
        "debt_asset_ratio",
    }
    assert result["financial_warmup_start"] == "2022-01-01"


def test_daily_signal_preflight_requires_warmup_and_paired_price_series(db, tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post(
            "/api/v1/research-data/preflight",
            json={"research_type": "daily_signal", "parameters": DAILY_SIGNAL},
        )
        assert response.status_code == 200, response.text
        assert response.json()["input"]["research_type"] == "daily_signal"
        assert response.json()["status_label"] in {
            "预检阻断",
            "可以运行（有提示）",
            "可以运行",
        }
        assert isinstance(response.json()["recommendations"], list)

    result = ResearchReadiness(db).run("daily_signal", DAILY_SIGNAL)
    assert result["ready"] is True
    calendar = next(item for item in result["checks"] if item["dataset"] == "trading_calendar")
    assert calendar["satisfied"] is True
    assert result["technical_warmup_start"] == "2024-01-02"
    assert result["technical_warmup_trading_days"] == 3
    assert "不会在研究开始日前产生交易" in result["technical_warmup_explanation"]
    price_checks = [item for item in result["checks"] if item["dataset"] == "daily_bar"]
    assert {item["adjustment"] for item in price_checks} == {"none", "qfq"}
    assert all(item["required_start"] == "2024-01-02" for item in price_checks)
    with db.connect() as connection:
        connection.execute(
            "DELETE FROM daily_bar WHERE security_id='SSE.600519' "
            "AND trade_date=DATE '2024-01-03' AND adjustment='none'"
        )
    missing = ResearchReadiness(db).run("daily_signal", DAILY_SIGNAL)
    assert missing["ready"] is False
    raw = next(
        item
        for item in missing["checks"]
        if item.get("symbol") == "600519" and item.get("adjustment") == "none"
    )
    assert raw["missing_dates"] == ["2024-01-03"]


def test_daily_signal_calendar_and_benchmark_gaps_are_explained_separately(db):
    with db.connect() as connection:
        connection.execute("DELETE FROM trading_calendar WHERE trade_date >= DATE '2024-01-10'")
    result = ResearchReadiness(db).run("daily_signal", DAILY_SIGNAL)
    recommendations = {item["code"]: item for item in result["recommendations"]}
    assert recommendations["calendar_coverage"]["title"] == "交易日历覆盖不足"
    assert recommendations["benchmark_coverage"]["title"] == "基准日线覆盖不足"
    assert recommendations["calendar_coverage"]["action_kind"] == "data_plan"
    assert recommendations["calendar_coverage"]["network_required_for_resolution"] is True
    assert recommendations["benchmark_coverage"]["affected_symbols"] == ["csi000300"]
    calendar = next(item for item in result["checks"] if item["dataset"] == "trading_calendar")
    assert calendar["status"] == "missing"
    assert calendar["actual_end"] == "2024-01-09"
    plan = plan_service(db).create("daily_signal", DAILY_SIGNAL)
    assert any(
        item["dataset"] == "daily_bar" and "刷新交易日历" in item["reason"]
        for item in plan["items"]
    ), plan["items"]
    assert plan["task"] is None  # Viewing a frozen plan never starts network work.


def test_daily_signal_preflight_explains_listing_boundary_and_next_action(db):
    result = ResearchReadiness(db).run(
        "daily_signal",
        {**DAILY_SIGNAL, "symbols": ["300750"]},
    )
    recommendation = next(
        item for item in result["recommendations"] if item["code"] == "listing_after_start"
    )
    assert result["status_label"] == "预检阻断"
    assert result["blocking_check_count"] == len(result["blockers"])
    assert "300750" in recommendation["explanation"]
    assert "2024-03-01" in recommendation["explanation"]
    assert "研究区间" in recommendation["action"]
    assert recommendation["field"] == "start_date"
    assert recommendation["suggested_value"] == "2024-03-01"
    assert recommendation["affected_symbols"] == ["300750"]
    assert recommendation["action_kind"] == "update_field"
    price = next(item for item in result["checks"] if item["dataset"] == "daily_bar")
    assert price["list_date"] == "2024-03-01"
    assert price["latest_complete_date"]


def test_plan_freezes_revision_and_rejects_tampering_or_overlimit(db):
    finance_gap(db)
    pools = StockPoolService(db)
    pool = pools.create("frozen", symbols=["600519", "000001"])
    service = plan_service(db)
    plan = service.create(
        "screen",
        {**SCREEN, "symbols": None, "stock_pool_id": pool["pool_id"], "stock_pool_revision": 1},
    )
    pools.remove_member(pool["pool_id"], "SZSE.000001", expected_revision=1)
    assert service.get(plan["plan_id"])["input"]["resolved_symbols"] == ["600519", "000001"]
    assert plan["input"]["stock_pool"]["pool_revision"] == 1
    with pytest.raises(DataPlanConflict):
        service.confirm(plan["plan_id"], version=2, input_sha256=plan["input_sha256"], confirm=True)
    with pytest.raises(DataPlanConflict):
        service.confirm(plan["plan_id"], version=1, input_sha256="0" * 64, confirm=True)
    with pytest.raises(ValueError, match="20"):
        service.create("screen", {**SCREEN, "symbols": [f"{n:06d}" for n in range(21)]})


def test_confirm_threads_one_task_and_atomic_audit_rollback(db, monkeypatch):
    finance_gap(db)
    service = plan_service(db)
    plan = service.create("screen", SCREEN)
    barrier = Barrier(2)

    def submit():
        barrier.wait(timeout=10)
        return confirm(plan_service(db), plan)["task_id"]

    with ThreadPoolExecutor(2) as executor:
        ids = list(executor.map(lambda _: submit(), range(2)))
    assert len(set(ids)) == 1
    another = service.create("screen", SCREEN)
    monkeypatch.setattr(
        service, "audit", lambda *a, **k: (_ for _ in ()).throw(ValueError("audit rollback"))
    )
    with pytest.raises(ValueError, match="audit rollback"):
        confirm(service, another)
    assert service.get(another["plan_id"])["status"] == "draft"
    with db.connect(read_only=True) as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM operation_task WHERE claim_key LIKE 'data-plan:%'"
            ).fetchone()[0]
            == 1
        )


def _process_confirm(root, plan, barrier, queue):
    try:
        layout = DataLayout.from_root(Path(root))
        service = ResearchDataPlans(LocalDatabase(layout.root / "catalog.duckdb", layout))
        barrier.wait(timeout=30)
        queue.put(confirm(service, plan)["task_id"])
    except Exception as exc:
        queue.put(f"ERROR:{exc}")


def test_confirm_independent_processes(db):
    finance_gap(db)
    plan = plan_service(db).create("screen", SCREEN)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [
        context.Process(target=_process_confirm, args=(str(db.layout.root), plan, barrier, queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=60) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert results[0].startswith("task-") and len(set(results)) == 1, results


def test_successful_finance_backfill_rechecks_and_preserves_market_data(db):
    finance_gap(db)
    provider = FakeFinancialProvider(provider_notice=True)
    service = plan_service(db, provider)
    plan = service.create("screen", SCREEN)
    assert {i["dataset"] for i in plan["items"]} == {"announcement", "financial_metric"}
    assert all(i["start_date"] == "2022-01-01" for i in plan["items"])
    task = confirm(service, plan)
    result = service.run(task["task_id"])
    assert result["status"] == "succeeded", result
    after = service.get(plan["plan_id"])
    assert after["report"]["complete"]
    assert confirm(service, plan)["task_id"] == task["task_id"]
    assert service.tasks.recover_interrupted(stale_after_seconds=0) == 0
    calls = provider.financial_calls
    service.run(task["task_id"])
    assert provider.financial_calls == calls
    with db.connect(read_only=True) as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM operation_task WHERE task_type IN ('backtest','factor_lab','copilot')"
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT availability FROM managed_stock WHERE security_id='SSE.600519'"
            ).fetchone()[0]
            == "ready"
        )


def test_published_partitions_with_remaining_gaps_complete_without_retry(db, monkeypatch):
    finance_gap(db)
    service = plan_service(db)
    plan = service.create("screen", SCREEN)
    task = confirm(service, plan)
    monkeypatch.setattr(service, "_download", lambda *args: {"status": "published", "row_count": 0})
    result = service.run(task["task_id"])
    assert result["status"] == "succeeded" and result["progress"] == 100
    after = service.get(plan["plan_id"])
    assert after["status"] == "succeeded"
    assert after["outcome"] == "completed_with_gaps"
    assert after["execution_summary"] == {
        "total": 2,
        "published": 2,
        "verified": 0,
        "failed": 0,
        "pending": 0,
        "unknown": 0,
        "retryable": 0,
        "processed": 2,
    }
    assert not after["report"]["complete"]
    with pytest.raises(DataPlanConflict, match="没有真实失败或未处理项"):
        confirm(service, plan, retry=True)


def test_daily_plan_freezes_and_links_explicit_gap_verification(db):
    missing_day = date(2024, 1, 5)
    with db.connect() as connection:
        connection.execute(
            "DELETE FROM daily_bar WHERE security_id='SSE.600519' AND trade_date=?",
            [missing_day],
        )
        for adjustment in ("none", "qfq"):
            connection.execute(
                "INSERT INTO sync_partition(batch_id,partition_key,security_id,adjustment,"
                "start_date,end_date,status,row_count) VALUES (?,?,?,?,?,?,'published',1)",
                [
                    "daily-bar-e3-fixture",
                    f"SSE.600519:{adjustment}:2024",
                    "SSE.600519",
                    adjustment,
                    date(2024, 1, 1),
                    date(2024, 12, 31),
                ],
            )
    plan_service_instance = plan_service(db)
    plan = plan_service_instance.create("daily_signal", DAILY_SIGNAL)
    assert plan["items"] == []
    assert plan["outcome"] == "verification_required"
    assert plan["available_actions"] == {
        "sync_required": False,
        "verification_required": True,
        "calendar_attention": False,
        "ready": False,
    }
    after = plan_service_instance.get(plan["plan_id"])
    gap = after["gap_verification"]
    assert gap["status"] == "available"
    assert gap["unverified_gap"] == 1
    assert gap["targets"][0]["unverified_dates"] == [str(missing_day)]
    with pytest.raises(ValueError, match="明确确认"):
        plan_service_instance.prepare_gap_verification(
            plan["plan_id"],
            version=plan["version"],
            input_sha256=plan["input_sha256"],
            confirm=False,
        )
    prepared = plan_service_instance.prepare_gap_verification(
        plan["plan_id"],
        version=plan["version"],
        input_sha256=plan["input_sha256"],
        confirm=True,
    )
    assert prepared[0]["dates"] == [str(missing_day)]
    assert prepared[0]["source_context"] == {
        "source_plan_id": plan["plan_id"],
        "source_plan_version": plan["version"],
        "source_input_sha256": plan["input_sha256"],
        "source_symbols_sha256": plan["input"]["symbols_sha256"],
        "source_stock_pool_id": None,
        "source_stock_pool_revision": None,
        "source_gap_dates_sha256": hashlib.sha256(str(missing_day).encode()).hexdigest(),
        "source": "daily_signal_research_data_plan",
    }

    class SuspensionProvider:
        @staticmethod
        def get_suspension_evidence(symbol, dates):
            assert symbol == "600519" and dates == (missing_day,)
            return {
                "verified": [
                    {
                        "date": str(missing_day),
                        "classification": "verified_suspension",
                        "source": "fixture:official",
                        "reason": "fixture suspension",
                    }
                ],
                "errors": [],
                "source": "fixture:official",
            }

    managed = ManagedStockService(db, provider_factory=lambda _: SuspensionProvider())
    request = prepared[0]
    verification, created = managed.request_gap_verification(
        request["security_id"],
        expected_revision=request["expected_revision"],
        confirm=True,
        dates=request["dates"],
        source_context=request["source_context"],
        actor="fixture",
    )
    assert created
    duplicate, duplicate_created = managed.request_gap_verification(
        request["security_id"],
        expected_revision=request["expected_revision"],
        confirm=True,
        dates=request["dates"],
        source_context=request["source_context"],
        actor="fixture",
    )
    assert duplicate["task_id"] == verification["task_id"]
    assert duplicate_created is False
    linked = plan_service_instance.get(plan["plan_id"])["gap_verification"]
    assert linked["status"] == "running"
    assert linked["tasks"][0]["task_id"] == verification["task_id"]
    assert managed.run(verification["task_id"])["status"] == "succeeded"
    completed = plan_service_instance.get(plan["plan_id"])["gap_verification"]
    assert completed["status"] == "verified"
    assert completed["verified_suspension"] == 1
    assert completed["unverified_gap"] == 0
    assert all(
        item["status"] == "published"
        for item in plan_service_instance.get(plan["plan_id"])["items"]
    )
    assert plan_service_instance.recheck(plan["plan_id"])["ready"] is True


def test_daily_plan_only_resyncs_a_real_one_sided_gap(db):
    missing_day = date(2024, 1, 5)
    with db.connect() as connection:
        connection.execute(
            "DELETE FROM daily_bar WHERE security_id='SSE.600519' "
            "AND adjustment='qfq' AND trade_date=?",
            [missing_day],
        )
        for adjustment in ("none", "qfq"):
            connection.execute(
                "INSERT INTO sync_partition(batch_id,partition_key,security_id,adjustment,"
                "start_date,end_date,status,row_count) VALUES (?,?,?,?,?,?,'published',1)",
                [
                    "daily-bar-e3-one-sided",
                    f"SSE.600519:{adjustment}:2024",
                    "SSE.600519",
                    adjustment,
                    date(2024, 1, 1),
                    date(2024, 12, 31),
                ],
            )
    service = plan_service(db)
    first = service.create("daily_signal", DAILY_SIGNAL)
    assert len(first["items"]) == 1
    assert first["items"][0]["dataset"] == "daily_bar"
    assert first["items"][0]["adjustment"] == "qfq"
    assert first["items"][0]["start_date"] == str(missing_day)
    assert first["items"][0]["end_date"] == str(missing_day)
    assert "真实行情缺失" in first["items"][0]["reason"]


def test_gap_verification_api_requires_confirmation_and_submits_exact_targets():
    calls = []

    class PlanService:
        @staticmethod
        def prepare_gap_verification(plan_id, *, version, input_sha256, confirm):
            if confirm is not True:
                raise ValueError("核验缺失日期需要明确确认访问公网")
            assert (plan_id, version, input_sha256) == ("data-plan-fixture", 1, "a" * 64)
            return [
                {
                    "security_id": "SZSE.000333",
                    "expected_revision": 7,
                    "dates": ["2024-02-01"],
                    "source_context": {"source_plan_id": plan_id},
                }
            ]

        @staticmethod
        def get(plan_id):
            return {"plan_id": plan_id, "gap_verification": {"status": "running"}}

    class PlanManager:
        service = PlanService()

    class GapManager:
        @staticmethod
        def submit_gap_verification(security_id, **kwargs):
            calls.append((security_id, kwargs))
            return {"task_id": "task-gap"}

    app = FastAPI()
    app.include_router(
        research_data_router(PlanManager(), "fixture", object(), GapManager())
    )
    body = {"version": 1, "input_sha256": "a" * 64, "confirm": False}
    with TestClient(app) as client:
        denied = client.post(
            "/api/v1/research-data/plans/data-plan-fixture/verify-gaps", json=body
        )
        assert denied.status_code == 422
        accepted = client.post(
            "/api/v1/research-data/plans/data-plan-fixture/verify-gaps",
            json={**body, "confirm": True},
        )
    assert accepted.status_code == 202
    assert accepted.json()["gap_verification"]["status"] == "running"
    assert calls == [
        (
            "SZSE.000333",
            {
                "expected_revision": 7,
                "dates": ["2024-02-01"],
                "source_context": {"source_plan_id": "data-plan-fixture"},
                "confirm": True,
                "actor": "fixture",
            },
        )
    ]


def test_real_failures_retry_and_expired_owner_fencing(db, monkeypatch):
    finance_gap(db)
    service = plan_service(db)
    plan = service.create("screen", SCREEN)
    task = confirm(service, plan)
    monkeypatch.setattr(
        service,
        "_download",
        lambda *args: {"status": "failed", "errors": [{"error": "provider timeout"}]},
    )
    result = service.run(task["task_id"])
    assert result["status"] == "failed" and result["progress"] == 100
    assert service.get(plan["plan_id"])["outcome"] == "download_failed"
    retry = confirm(service, plan, retry=True)
    assert retry["task_id"] != task["task_id"]
    assert service.tasks.start(retry["task_id"], owner_id="expired", message="fixture")
    with db.connect() as c:
        c.execute(
            "UPDATE operation_task SET heartbeat_at=CURRENT_TIMESTAMP-INTERVAL 1 HOUR WHERE task_id=?",
            [retry["task_id"]],
        )
    replacement = confirm(service, plan, retry=True)
    with db.connect(read_only=True) as c, pytest.raises(DataPlanConflict):
        service.fence(c, retry["task_id"], "expired", retry["spec"])
    assert replacement["task_id"] != retry["task_id"]


def test_retry_only_runs_failed_or_unprocessed_items(db, monkeypatch):
    finance_gap(db)
    service = plan_service(db)
    plan = service.create("screen", SCREEN)
    item_ids = [item["item_id"] for item in plan["items"]]
    calls = []

    def first_download(_guarded, _task_id, _owner, item):
        calls.append(item["item_id"])
        if item["item_id"] == item_ids[0]:
            return {"status": "published", "row_count": 1}
        return {"status": "failed", "errors": [{"error": "fixture failure"}]}

    monkeypatch.setattr(service, "_download", first_download)
    first = confirm(service, plan)
    assert service.run(first["task_id"])["status"] == "failed"
    assert calls == item_ids
    calls.clear()
    monkeypatch.setattr(
        service,
        "_download",
        lambda _guarded, _task_id, _owner, item: (
            calls.append(item["item_id"]) or {"status": "published", "row_count": 0}
        ),
    )
    retry = confirm(service, plan, retry=True)
    assert service.run(retry["task_id"])["status"] == "succeeded"
    assert calls == [item_ids[1]]
    after = service.get(plan["plan_id"])
    assert after["outcome"] == "completed_with_gaps"
    assert after["execution_summary"]["retryable"] == 0


def test_legacy_failed_95_percent_gap_plan_is_derived_without_rewriting(db, monkeypatch):
    finance_gap(db)
    service = plan_service(db)
    plan = service.create("screen", SCREEN)
    task = confirm(service, plan)
    monkeypatch.setattr(service, "_download", lambda *args: {"status": "published", "row_count": 0})
    service.run(task["task_id"])
    with db.connect() as connection:
        connection.execute(
            "UPDATE research_data_plan SET status='failed' WHERE plan_id=?",
            [plan["plan_id"]],
        )
        connection.execute(
            "UPDATE operation_task SET status='failed',progress=95,message='部分补齐或失败，请查看计划诊断',"
            "error='补齐已执行，但研究数据仍有缺口；请查看逐股票诊断，不自动运行研究' WHERE task_id=?",
            [task["task_id"]],
        )
    legacy = service.get(plan["plan_id"])
    assert legacy["task"]["progress"] == 95  # Stored history remains untouched.
    assert legacy["outcome"] == "completed_with_gaps"
    assert legacy["execution_summary"]["retryable"] == 0
    with pytest.raises(DataPlanConflict, match="没有真实失败或未处理项"):
        confirm(service, legacy, retry=True)


def test_generation_change_during_network_and_backup_remains_valid(db, tmp_path, monkeypatch):
    finance_gap(db)
    service = plan_service(db)
    plan = service.create("screen", SCREEN)
    task = confirm(service, plan)
    entered, release = Event(), Event()

    def download(guarded, *args):
        entered.set()
        assert release.wait(30)
        with guarded.connect() as c:
            c.execute("UPDATE managed_stock SET status='active' WHERE security_id='SSE.600519'")
        return {"status": "published"}

    monkeypatch.setattr(service, "_download", download)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(service.run, task["task_id"])
        assert entered.wait(30)
        backup = DatabaseBackupService(
            database=db,
            backup_dir=tmp_path / "backups",
            tasks=service.tasks,
            operation_lock=ExclusiveOperationLock(db.layout.root / ".operations.lock"),
        )
        result = backup.run()
        assert backup.verify(result["backup_id"])["status"] == "verified"
        with db.connect() as c:
            c.execute(
                "UPDATE managed_stock SET status='archived',generation=generation+1 WHERE security_id='SSE.600519'"
            )
        release.set()
        assert future.result(timeout=30)["status"] == "failed"
    with db.connect(read_only=True) as c:
        assert (
            c.execute("SELECT status FROM managed_stock WHERE security_id='SSE.600519'").fetchone()[
                0
            ]
            == "archived"
        )
    with pytest.raises(DataPlanConflict, match="generation"):
        confirm(service, plan, retry=True)


def test_dataset_download_contention_is_explicit_not_duplicate(tmp_path):
    provider = FakeFinancialProvider(provider_notice=True)
    _, financial, db = _services(tmp_path, provider)
    slot = InterProcessFileLock(
        db.layout.root / ".financial_metric-download.lock", description="fixture"
    )
    with slot.hold(), pytest.raises(RuntimeError, match="download already active"):
        financial.run(FinancialSyncSpec(date(2024, 1, 1), date(2024, 12, 31), ("600519",)))
    assert provider.financial_calls == 0


def test_v15_migration_preserves_all_tables_repeatable_and_rollback(db, monkeypatch):
    with db.connect() as c:
        c.execute("DROP TABLE research_data_plan")
        c.execute("DELETE FROM schema_migrations WHERE version=15")
        tables = [
            r[0]
            for r in c.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_type='BASE TABLE' AND table_name<>'schema_migrations'"
            ).fetchall()
        ]
        before = {
            name: c.execute(f'SELECT * FROM "{name}" ORDER BY ALL').fetchall() for name in tables
        }
        import ai_quant_assistant.storage.schema as schema

        monkeypatch.setattr(
            schema,
            "MIGRATIONS",
            (
                *MIGRATIONS[:14],
                replace(
                    MIGRATIONS[14],
                    statements=(*MIGRATIONS[14].statements, "SELECT unknown_migration_failure"),
                ),
            ),
        )
        with pytest.raises(Exception, match="unknown_migration_failure"):
            apply_migrations(c)
        assert (
            c.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=15").fetchone()[0] == 0
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='research_data_plan'"
            ).fetchone()[0]
            == 0
        )
        monkeypatch.setattr(schema, "MIGRATIONS", MIGRATIONS)
        assert apply_migrations(c) == [15]
        assert apply_migrations(c) == []
        assert before == {
            name: c.execute(f'SELECT * FROM "{name}" ORDER BY ALL').fetchall() for name in tables
        }


def test_financial_same_publication_conflict_retains_values_and_raw_bytes(tmp_path, monkeypatch):
    provider = FakeFinancialProvider(provider_notice=True)
    _, financial, db = _services(tmp_path, provider)
    spec = FinancialSyncSpec(date(2024, 1, 1), date(2024, 12, 31), ("600519",), resume=False)
    assert financial.run(spec)["status"] == "published"
    with db.connect(read_only=True) as c:
        before = c.execute("SELECT * FROM financial_metric ORDER BY ALL").fetchall()
        raw = c.execute("SELECT raw_path,raw_sha256 FROM financial_sync_partition").fetchall()
    original = provider.get_financial_indicators

    def changed(*args):
        frame = original(*args)
        frame["roe"] += 100
        return frame

    monkeypatch.setattr(provider, "get_financial_indicators", changed)
    result = financial.run(spec)
    assert result["status"] == "failed" and "version conflict" in result["errors"][0]["error"]
    with db.connect(read_only=True) as c:
        assert c.execute("SELECT * FROM financial_metric ORDER BY ALL").fetchall() == before
    for path, sha in raw:
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha


def test_stale_finance_cache_and_ambiguous_publication_are_not_published(tmp_path, monkeypatch):
    provider = FakeFinancialProvider()
    research, financial, db = _services(tmp_path, provider)
    from test_financial_platform import _sync_announcements

    _sync_announcements(research, ("600519",))
    with db.connect() as c:
        c.execute(
            "INSERT INTO announcement SELECT * REPLACE ('revision' AS announcement_id, TIMESTAMP '2024-09-01' AS published_at) FROM announcement WHERE report_type='interim'"
        )
    partition = {
        "security_id": "SSE.600519",
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 12, 31),
    }
    frame = financial._bind_availability(
        partition, provider.get_financial_indicators("600519", date(2024, 1, 1), date(2024, 12, 31))
    )
    assert pd.isna(frame.loc[frame.report_type == "interim", "published_at"].iloc[0])
    frame.attrs["cache_status"] = "stale_fallback"
    monkeypatch.setattr(provider, "get_financial_indicators", lambda *args: frame)
    result = financial.run(FinancialSyncSpec(date(2023, 1, 1), date(2024, 12, 31), ("600519",)))
    assert result["status"] == "failed" and "stale cache" in result["errors"][0]["error"]
    assert result["partitions"] == {"failed": 2}


def test_api_cli_confirmation_and_no_browser_scope_override(db, tmp_path, monkeypatch, capsys):
    finance_gap(db)
    with TestClient(create_app(_settings(tmp_path))) as client:
        manager = client.app.state.research_data_tasks
        monkeypatch.setattr(manager, "dispatch", lambda: None)
        with db.connect(read_only=True) as connection:
            before = connection.execute("SELECT COUNT(*) FROM research_data_plan").fetchone()[0]
        for endpoint in ("preflight", "plans"):
            rejected = client.post(
                f"/api/v1/research-data/{endpoint}",
                json={
                    "research_type": "factor_backtest",
                    "parameters": {**PARAMETERS, "symbols": []},
                },
            )
            assert rejected.status_code == 422
            assert rejected.json()["detail"][0]["loc"][-1] == "symbols"
        with db.connect(read_only=True) as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM research_data_plan").fetchone()[0]
                == before
            )
        preflight = client.post(
            "/api/v1/research-data/preflight",
            json={"research_type": "screen", "parameters": SCREEN},
        )
        assert preflight.status_code == 200 and not preflight.json()["complete"]
        response = client.post(
            "/api/v1/research-data/plans", json={"research_type": "screen", "parameters": SCREEN}
        )
        assert response.status_code == 201, response.text
        plan = response.json()
        body = {"version": 1, "input_sha256": plan["input_sha256"], "confirm": True}
        root = f"/api/v1/research-data/plans/{plan['plan_id']}"
        assert (
            client.post(root + "/confirm", json={**body, "symbols": ["000001"]}).status_code == 422
        )
        assert client.post(root + "/confirm", json={**body, "version": 2}).status_code == 409
        assert client.post(root + "/confirm", json=body).status_code == 202
        assert client.get("/api/v1/research-data/plans/missing").status_code == 404
        assert client.get("/").status_code == 200
        assert "research-data.js" in client.get("/").text
        html = client.get("/").text
        for action in ("create", "confirm", "retry", "refresh", "recheck"):
            assert f'id="research-data-{action}" type="button" disabled' in html
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ai-quant",
            "research-data",
            "preflight",
            "--data-dir",
            str(db.layout.root),
            "--research-type",
            "screen",
            "--parameters",
            json.dumps(SCREEN),
        ],
    )
    main()
    assert not json.loads(capsys.readouterr().out)["complete"]


def test_twenty_stock_plan_limit_and_shared_benchmark_single_item(db):
    service = plan_service(db)
    symbols = [f"600{number:03d}" for number in range(20)]
    with db.connect() as c:
        c.executemany(
            "INSERT INTO security_master(security_id,symbol,exchange,name,list_date,source,source_batch_id) "
            "VALUES (?,?,'SSE','fixture','2000-01-01','fixture','fixture')",
            [[f"SSE.{symbol}", symbol] for symbol in symbols],
        )
        c.executemany(
            "INSERT INTO managed_stock(security_id,target_start,target_end) VALUES (?,'2012-01-01','2024-08-30')",
            [[f"SSE.{symbol}"] for symbol in symbols],
        )
    plan = service.create("screen", {**SCREEN, "symbols": symbols})
    assert len(plan["input"]["resolved_symbols"]) == 20 and len(plan["items"]) == 40
    assert confirm(service, plan)["status"] == "queued"
    with db.connect() as c:
        c.execute("DELETE FROM benchmark_daily_bar")
    shared = service.create("factor_backtest", PARAMETERS)
    assert len([item for item in shared["items"] if item["dataset"] == "benchmark_daily_bar"]) == 1


def test_current_values_without_version_publication_cannot_be_backdated(db):
    finance_gap(db)
    service = plan_service(db, FakeFinancialProvider(provider_notice=False))
    plan = service.create("screen", SCREEN)
    task = confirm(service, plan)
    result = service.run(task["task_id"])
    assert result["status"] == "failed"
    with db.connect(read_only=True) as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM published_announcement WHERE security_id='SSE.600519'"
            ).fetchone()[0]
            > 0
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM published_financial_metric WHERE security_id='SSE.600519' AND metric_code='roe'"
            ).fetchone()[0]
            == 0
        )
    assert not service.recheck(plan["plan_id"])["complete"]


def test_date_only_financial_notice_is_not_available_at_same_day_signal(tmp_path):
    provider = FakeFinancialProvider(provider_notice=True)
    _, financial, _ = _services(tmp_path, provider)
    financial.require_provider_publication = True
    frame = provider.get_financial_indicators("600519", date(2024, 1, 1), date(2024, 12, 31))
    frame["published_at"] = frame["published_at"].dt.normalize()
    checked = financial._bind_availability({}, frame)
    assert all(stamp.hour == 23 and stamp.minute == 59 for stamp in checked["published_at"])


def test_creation_and_final_audit_failure_roll_back_plan_task_state(db, monkeypatch):
    finance_gap(db)
    service = plan_service(db)
    original = service.audit

    def fail_created(connection, action, *args, **kwargs):
        if action == "created":
            raise ValueError("creation audit rollback")
        return original(connection, action, *args, **kwargs)

    monkeypatch.setattr(service, "audit", fail_created)
    with pytest.raises(ValueError, match="creation audit"):
        service.create("screen", SCREEN)
    assert service.list() == []
    monkeypatch.setattr(service, "audit", original)
    plan = service.create("screen", SCREEN)
    task = confirm(service, plan)

    def fail_completed(connection, action, *args, **kwargs):
        if action == "completed":
            raise ValueError("terminal audit rollback")
        return original(connection, action, *args, **kwargs)

    monkeypatch.setattr(service, "audit", fail_completed)
    result = service.run(task["task_id"])
    assert result["status"] == "failed" and "terminal audit" in result["error"]
    assert not service.get(plan["plan_id"])["report"]["complete"]
    assert service.recheck(plan["plan_id"])[
        "complete"
    ]  # Published partitions survive final rollback.
    with db.connect(read_only=True) as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM operation_audit WHERE action='research_data_plan_completed'"
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM operation_audit WHERE action='research_data_plan_failed'"
            ).fetchone()[0]
            == 1
        )
