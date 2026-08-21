from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.cli import main
from ai_quant_assistant.config import Settings
from ai_quant_assistant.domain.models import (
    REPORT_TYPES,
    AnnouncementSyncSpec,
    BatchEventStudySpec,
    FinancialSyncSpec,
    ResearchBackfillSpec,
)
from ai_quant_assistant.providers.local_provider import LocalFirstProvider
from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS
from ai_quant_assistant.research.batch_service import BatchInterimReportResearchService
from ai_quant_assistant.storage.backfill_service import ResearchBackfillService
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.financial_sync_service import FinancialDataSyncService
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.research_sync_service import ResearchDatasetSyncService


class FakeFinancialProvider:
    name = "fake-financial"

    publications = {
        "q1": ("2024-03-31", "2024-04-20 18:30:00"),
        "interim": ("2024-06-30", "2024-08-15 18:45:00"),
        "q3": ("2024-09-30", "2024-10-20 19:15:00"),
        "annual": ("2024-12-31", "2025-03-10 20:30:00"),
    }

    def __init__(
        self,
        *,
        provider_notice: bool = False,
        financial_fail_once: bool = False,
        missing_interim_symbols: tuple[str, ...] = (),
    ) -> None:
        self.provider_notice = provider_notice
        self.financial_fail_once = financial_fail_once
        self.missing_interim_symbols = missing_interim_symbols
        self.financial_calls = 0
        self.stock_fallback_calls = 0
        self.announcement_fallback_calls = 0

    def get_security_master(self) -> pd.DataFrame:
        return pd.DataFrame()

    def get_trading_calendar(self) -> pd.DataFrame:
        days = pd.bdate_range("2024-01-01", "2025-06-30")
        rows = []
        for exchange in ("SSE", "SZSE", "BSE"):
            for index, current in enumerate(days):
                rows.append(
                    {
                        "exchange": exchange,
                        "trade_date": current,
                        "is_open": True,
                        "previous_open_date": days[index - 1] if index else None,
                        "next_open_date": days[index + 1] if index + 1 < len(days) else None,
                        "session_no": index + 1,
                        "source": "fake-financial:calendar",
                    }
                )
        return pd.DataFrame(rows)

    def get_daily_bars(
        self, symbol: str, start_date: date, end_date: date, adjustment: str
    ) -> pd.DataFrame:
        days = pd.bdate_range(start_date, end_date)
        base = 1000.0 if symbol == "600519" else 100.0
        multiplier = 0.8 if adjustment == "qfq" else 1.0
        prices = pd.Series([base + index for index in range(len(days))]) * multiplier
        return pd.DataFrame(
            {
                "trade_date": days,
                "open": prices - 0.4,
                "high": prices + 1.0,
                "low": prices - 1.0,
                "close": prices,
                "volume": 1000.0,
                "amount": 10000.0,
                "amount_missing": False,
                "symbol": symbol,
                "source": f"akshare:stock_zh_a_hist:{adjustment}",
            }
        )

    def get_stock_prices(
        self, symbol: str, start_date: date, end_date: date, adjustment: str
    ) -> pd.DataFrame:
        self.stock_fallback_calls += 1
        bars = self.get_daily_bars(symbol, start_date, end_date, adjustment or "none")
        return bars.rename(columns={"trade_date": "date"})[["date", "close", "symbol", "source"]]

    def get_benchmark_prices(self, symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
        days = pd.bdate_range(start_date, end_date)
        return pd.DataFrame(
            {
                "date": days,
                "close": [100.0 + index * 0.3 for index in range(len(days))],
                "symbol": symbol,
                "source": "akshare:stock_zh_index_daily_em",
            }
        )

    def get_report_announcements(
        self, symbol: str, start_date: date, end_date: date, report_type: str
    ) -> pd.DataFrame:
        columns = [
            "symbol",
            "company_name",
            "title",
            "announcement_date",
            "url",
            "report_year",
            "report_type",
            "report_period_end",
            "published_at",
            "source",
        ]
        if report_type == "interim" and symbol in self.missing_interim_symbols:
            return pd.DataFrame(columns=columns)
        period_text, publication_text = self.publications[report_type]
        published_at = pd.Timestamp(publication_text)
        if not start_date <= published_at.date() <= end_date:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "company_name": f"测试公司{symbol}",
                    "title": f"测试公司{symbol}2024年{report_type}报告",
                    "announcement_date": published_at.normalize(),
                    "url": f"https://example.test/{symbol}/{report_type}",
                    "report_year": 2024,
                    "report_type": report_type,
                    "report_period_end": pd.Timestamp(period_text),
                    "published_at": published_at,
                    "source": f"akshare:stock_zh_a_disclosure_report_cninfo:{report_type}",
                }
            ],
            columns=columns,
        )

    def get_interim_announcements(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        self.announcement_fallback_calls += 1
        return self.get_report_announcements(symbol, start_date, end_date, "interim")

    def get_financial_indicators(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        self.financial_calls += 1
        if self.financial_fail_once:
            self.financial_fail_once = False
            raise ConnectionError("temporary financial indicator outage")
        rows = []
        for index, (report_type, (period_text, publication_text)) in enumerate(
            self.publications.items(), start=1
        ):
            period = pd.Timestamp(period_text)
            if not start_date <= period.date() <= end_date:
                continue
            row = {
                "symbol": symbol,
                "company_name": f"测试公司{symbol}",
                "report_period_end": period,
                "report_type": report_type,
                "published_at": pd.Timestamp(publication_text) if self.provider_notice else pd.NaT,
                "source": "akshare:stock_financial_analysis_indicator_em",
            }
            row.update(
                {
                    metric: float(index * 100 + metric_index)
                    for metric_index, metric in enumerate(FINANCIAL_METRICS, start=1)
                }
            )
            rows.append(row)
        return pd.DataFrame(
            rows,
            columns=[
                "symbol",
                "company_name",
                "report_period_end",
                "report_type",
                "published_at",
                "source",
                *FINANCIAL_METRICS,
            ],
        )


def _services(tmp_path: Path, provider: FakeFinancialProvider):
    layout = DataLayout.from_root(tmp_path / "data")
    database = LocalDatabase(path=layout.root / "catalog.duckdb", layout=layout)
    research = ResearchDatasetSyncService(
        database=database, layout=layout, provider=provider, rate_limit_seconds=0
    )
    financial = FinancialDataSyncService(
        database=database, layout=layout, provider=provider, rate_limit_seconds=0
    )
    return research, financial, database


def _sync_announcements(service: ResearchDatasetSyncService, symbols: tuple[str, ...]):
    return service.sync_announcements(
        AnnouncementSyncSpec(
            start_date=date(2024, 1, 1),
            end_date=date(2025, 3, 31),
            symbols=symbols,
            report_types=REPORT_TYPES,
        )
    )


def test_financial_sync_matches_disclosure_timestamps_and_enforces_point_in_time(tmp_path) -> None:
    provider = FakeFinancialProvider()
    research, financial, database = _services(tmp_path, provider)
    announcements = _sync_announcements(research, ("600519",))

    result = financial.run(
        FinancialSyncSpec(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            symbols=("600519",),
        )
    )
    before_disclosure = database.read_financial_metrics("600519", "2024-08-15 18:44:00")
    after_disclosure = database.read_financial_metrics("600519", "2024-08-15 18:46:00")
    before_annual = database.read_financial_metrics("600519", date(2025, 3, 9))
    after_annual = database.read_financial_metrics("600519", date(2025, 3, 10))
    latest = database.read_latest_financial_snapshot("600519", "2024-08-15 18:46:00")

    assert announcements["status"] == "published"
    assert result["status"] == "published", result
    assert result["row_count"] == 4
    assert result["metric_count"] == 48
    assert result["partitions"] == {"published": 1}
    assert provider.financial_calls == 1
    assert before_disclosure["report_type"].unique().tolist() == ["q1"]
    assert after_disclosure["report_type"].unique().tolist() == ["q1", "interim"]
    assert "annual" not in before_annual["report_type"].tolist()
    assert "annual" in after_annual["report_type"].tolist()
    assert after_disclosure["availability_source"].unique().tolist() == ["matched_announcement"]
    assert after_disclosure["source"].unique().tolist() == ["local:published_financial_metric"]
    assert latest["financial_report_period_end"] == "2024-06-30"
    assert latest["revenue"] == 201.0
    assert database.has_financial_coverage("600519", date(2024, 1, 1), date(2024, 12, 31))


def test_financial_sync_blocks_reports_without_a_known_publication_timestamp(tmp_path) -> None:
    provider = FakeFinancialProvider()
    _, financial, database = _services(tmp_path, provider)

    result = financial.run(
        FinancialSyncSpec(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            symbols=("600519",),
        )
    )

    assert result["status"] == "failed"
    assert "financial_publication_known" in result["errors"][0]["error"]
    assert database.status()["relations"]["published_financial_report"] == 0
    assert database.status()["relations"]["published_financial_metric"] == 0


def test_financial_provider_notice_is_sufficient_without_local_announcements(tmp_path) -> None:
    provider = FakeFinancialProvider(provider_notice=True)
    _, financial, database = _services(tmp_path, provider)

    result = financial.run(
        FinancialSyncSpec(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            symbols=("600519",),
        )
    )
    visible = database.read_financial_metrics("600519", date(2024, 8, 15))

    assert result["status"] == "published", result
    assert visible["availability_source"].unique().tolist() == ["provider_notice"]
    assert len(visible) == 24


def test_failed_financial_partition_resumes_without_duplicating_versions(tmp_path) -> None:
    provider = FakeFinancialProvider(provider_notice=True, financial_fail_once=True)
    _, financial, database = _services(tmp_path, provider)
    spec = FinancialSyncSpec(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        symbols=("600519",),
    )

    failed = financial.run(spec)
    recovered = financial.run(spec)
    repeated = financial.run(spec)

    assert failed["status"] == "failed"
    assert recovered["status"] == "published", recovered
    assert recovered["batch_id"] == failed["batch_id"]
    assert repeated["batch_id"] == failed["batch_id"]
    assert provider.financial_calls == 2
    assert database.status()["relations"]["financial_report"] == 4
    assert database.status()["relations"]["financial_metric"] == 48


def test_financial_revision_only_becomes_visible_after_its_own_publication(tmp_path) -> None:
    provider = FakeFinancialProvider(provider_notice=True)
    _, financial, database = _services(tmp_path, provider)
    first_spec = FinancialSyncSpec(
        start_date=date(2024, 4, 1),
        end_date=date(2024, 6, 30),
        symbols=("600519",),
    )
    first = financial.run(first_spec)
    provider.publications = dict(provider.publications)
    provider.publications["interim"] = ("2024-06-30", "2024-09-01 09:30:00")
    original_fetch = provider.get_financial_indicators

    def revised_financials(symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
        frame = original_fetch(symbol, start_date, end_date)
        frame.loc[frame["report_type"] == "interim", "revenue"] = 999.0
        return frame

    provider.get_financial_indicators = revised_financials
    revised = financial.run(
        FinancialSyncSpec(
            start_date=first_spec.start_date,
            end_date=first_spec.end_date,
            symbols=first_spec.symbols,
            resume=False,
        )
    )

    before_revision = database.read_latest_financial_snapshot("600519", "2024-09-01 09:29:00")
    after_revision = database.read_latest_financial_snapshot("600519", "2024-09-01 09:31:00")

    assert first["status"] == "published"
    assert revised["status"] == "published", revised
    assert before_revision["revenue"] == 201.0
    assert after_revision["revenue"] == 999.0
    assert database.status()["relations"]["financial_report"] == 2
    assert database.status()["relations"]["financial_metric"] == 24


def test_empty_financial_year_is_published_as_complete_coverage(tmp_path) -> None:
    provider = FakeFinancialProvider(provider_notice=True)
    _, financial, database = _services(tmp_path, provider)

    result = financial.run(
        FinancialSyncSpec(
            start_date=date(2023, 1, 1),
            end_date=date(2023, 12, 31),
            symbols=("600519",),
        )
    )

    assert result["status"] == "published", result
    assert result["row_count"] == 0
    assert result["metric_count"] == 0
    assert database.has_financial_coverage("600519", date(2023, 1, 1), date(2023, 12, 31))


def test_backfill_orchestrates_all_datasets_and_batch_research_uses_local_financials(
    tmp_path,
) -> None:
    provider = FakeFinancialProvider()
    _, _, database = _services(tmp_path, provider)
    service = ResearchBackfillService(
        database=database,
        layout=database.layout,
        provider=provider,
        rate_limit_seconds=0,
    )

    backfill = service.run(
        ResearchBackfillSpec(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            symbols=("600519", "000001"),
            adjustments=("qfq",),
            windows=(5, 10),
        )
    )
    local = LocalFirstProvider(database=database, fallback=provider)
    result = BatchInterimReportResearchService(
        provider=local,
        artifact_dir=tmp_path / "artifacts",
        database=database,
    ).run(
        BatchEventStudySpec(
            symbols=("600519", "000001"),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            windows=(5, 10),
        )
    )
    events = pd.read_csv(result["artifacts"]["events_csv"])

    assert backfill["status"] == "published", backfill
    assert set(backfill["stages"]) == {
        "daily_bar",
        "benchmark_daily_bar",
        "announcement",
        "financial_metric",
    }
    assert backfill["stages"]["financial_metric"]["row_count"] == 8
    assert result["status"] == "succeeded", result
    assert result["successful_symbols"] == 2
    assert [row["stock_count"] for row in result["pooled_summary"]] == [2, 2]
    assert {str(item).zfill(6) for item in events["symbol"]} == {"600519", "000001"}
    assert events["financial_report_period_end"].unique().tolist() == ["2024-06-30"]
    assert events["revenue"].unique().tolist() == [201.0]
    assert all(item["financial_local_hit"] for item in result["runs"])
    assert all(Path(path).is_file() for path in result["artifacts"].values())
    assert provider.stock_fallback_calls == 0
    assert provider.announcement_fallback_calls == 0


def test_batch_research_isolates_failed_symbols_and_exports_failure_list(tmp_path) -> None:
    provider = FakeFinancialProvider(missing_interim_symbols=("000001",))
    result = BatchInterimReportResearchService(
        provider=provider,
        artifact_dir=tmp_path / "artifacts",
    ).run(
        BatchEventStudySpec(
            symbols=("600519", "000001"),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            windows=(5,),
        )
    )

    assert result["status"] == "partial"
    assert result["successful_symbols"] == 1
    assert result["failed_symbols"] == 1
    assert result["failures"][0]["symbol"] == "000001"
    assert len(pd.read_csv(result["artifacts"]["failures_csv"])) == 1


def test_cli_financial_sync_and_point_in_time_status(tmp_path, monkeypatch, capsys) -> None:
    provider = FakeFinancialProvider(provider_notice=True)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("AI_QUANT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AI_QUANT_DATA_SYNC_RATE_LIMIT_SECONDS", "0")
    monkeypatch.setattr("ai_quant_assistant.cli.AkShareProvider", lambda **_: provider)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ai-quant",
            "data",
            "sync-financial",
            "--start-date",
            "2024-01-01",
            "--end-date",
            "2024-12-31",
            "--symbols",
            "600519",
        ],
    )

    main()
    synchronized = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ai-quant",
            "data",
            "financial-status",
            "--symbol",
            "600519",
            "--as-of",
            "2024-08-15",
        ],
    )
    main()
    status = json.loads(capsys.readouterr().out)

    assert synchronized["status"] == "published", synchronized
    assert status["report_count"] == 2
    assert status["metric_count"] == 24
    assert status["latest"]["financial_report_period_end"] == "2024-06-30"


def test_cli_research_backfill_orchestrates_all_four_dataset_stages(
    tmp_path, monkeypatch, capsys
) -> None:
    provider = FakeFinancialProvider(provider_notice=True)
    monkeypatch.setenv("AI_QUANT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AI_QUANT_DATA_SYNC_RATE_LIMIT_SECONDS", "0")
    monkeypatch.setattr("ai_quant_assistant.cli.AkShareProvider", lambda **_: provider)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ai-quant",
            "data",
            "backfill-research",
            "--start-date",
            "2024-01-01",
            "--end-date",
            "2024-12-31",
            "--symbols",
            "600519",
            "--types",
            "interim",
            "--windows",
            "5",
        ],
    )

    main()
    result = json.loads(capsys.readouterr().out)

    assert result["status"] == "published", result
    assert result["symbols"] == ["600519"]
    assert list(result["stages"]) == [
        "daily_bar",
        "benchmark_daily_bar",
        "announcement",
        "financial_metric",
    ]
    assert result["stages"]["financial_metric"]["row_count"] == 4


def test_batch_cli_and_api_run_offline_with_fixture_provider(tmp_path, monkeypatch, capsys) -> None:
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("AI_QUANT_ARTIFACT_DIR", str(artifacts))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ai-quant",
            "batch-event-study",
            "--source",
            "fixture",
            "--symbols",
            "600519",
            "--start-date",
            "2024-01-01",
            "--end-date",
            "2024-12-31",
            "--windows",
            "5",
        ],
    )
    main()
    cli_payload = json.loads(capsys.readouterr().out)

    settings = Settings(
        data_dir=tmp_path / "api-data",
        artifact_dir=tmp_path / "api-artifacts",
        cache_dir=tmp_path / "api-cache",
        duckdb_path=tmp_path / "api-data" / "catalog.duckdb",
    )
    response = TestClient(create_app(settings)).post(
        "/api/v1/research/batch-interim-report-event-study",
        json={
            "source": "fixture",
            "symbols": ["600519"],
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "windows": [5],
        },
    )

    assert cli_payload["status"] == "succeeded", cli_payload
    assert cli_payload["successful_symbols"] == 1
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded"
    assert response.json()["pooled_summary"][0]["stock_count"] == 1
