from __future__ import annotations

import json
import sys
from datetime import date

import pandas as pd
from fastapi.testclient import TestClient

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.cli import main
from ai_quant_assistant.config import Settings
from ai_quant_assistant.domain.models import (
    AnnouncementSyncSpec,
    BenchmarkSyncSpec,
    DataSyncSpec,
    EventStudySpec,
)
from ai_quant_assistant.providers.local_provider import LocalFirstProvider
from ai_quant_assistant.research.service import InterimReportResearchService
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.research_sync_service import ResearchDatasetSyncService
from ai_quant_assistant.storage.sync_service import MarketDataSyncService


class FakeResearchProvider:
    name = "fake-research"

    def __init__(
        self,
        *,
        benchmark_fail_once: bool = False,
        invalid_announcement_period: bool = False,
    ) -> None:
        self.benchmark_fail_once = benchmark_fail_once
        self.invalid_announcement_period = invalid_announcement_period
        self.benchmark_calls = 0
        self.announcement_calls = 0
        self.announcement_fallback_calls = 0
        self.stock_fallback_calls = 0

    def get_benchmark_prices(self, symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
        self.benchmark_calls += 1
        if self.benchmark_fail_once:
            self.benchmark_fail_once = False
            raise ConnectionError("temporary benchmark outage")
        dates = pd.bdate_range(start_date, end_date)
        return pd.DataFrame(
            {
                "date": dates,
                "close": [100.0 + index for index in range(len(dates))],
                "symbol": symbol,
                "source": "akshare:stock_zh_index_daily_em",
            }
        )

    def get_report_announcements(
        self, symbol: str, start_date: date, end_date: date, report_type: str
    ) -> pd.DataFrame:
        self.announcement_calls += 1
        if report_type == "interim":
            announcement_date = pd.Timestamp(
                "2024-06-15" if self.invalid_announcement_period else "2024-08-15"
            )
            period_end = pd.Timestamp("2024-06-30")
            title = "贵州茅台2024年半年度报告"
        else:
            announcement_date = pd.Timestamp("2025-03-10")
            period_end = pd.Timestamp("2024-12-31")
            title = "贵州茅台2024年年度报告"
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
        if not start_date <= announcement_date.date() <= end_date:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "company_name": "贵州茅台",
                    "title": title,
                    "announcement_date": announcement_date,
                    "url": f"https://example.test/{report_type}",
                    "report_year": 2024,
                    "report_type": report_type,
                    "report_period_end": period_end,
                    "published_at": announcement_date,
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

    def get_stock_prices(
        self, symbol: str, start_date: date, end_date: date, adjustment: str
    ) -> pd.DataFrame:
        self.stock_fallback_calls += 1
        dates = pd.bdate_range(start_date, end_date)
        return pd.DataFrame(
            {
                "date": dates,
                "close": [1000.0 + index for index in range(len(dates))],
                "symbol": symbol,
                "source": f"akshare:stock_zh_a_hist:{adjustment}",
            }
        )

    def get_security_master(self) -> pd.DataFrame:
        return pd.DataFrame()

    def get_trading_calendar(self) -> pd.DataFrame:
        dates = pd.bdate_range("2024-08-01", "2024-09-30")
        rows = []
        for exchange in ("SSE", "SZSE", "BSE"):
            for index, trade_date in enumerate(dates):
                rows.append(
                    {
                        "exchange": exchange,
                        "trade_date": trade_date,
                        "is_open": True,
                        "previous_open_date": dates[index - 1] if index else None,
                        "next_open_date": dates[index + 1] if index + 1 < len(dates) else None,
                        "session_no": index + 1,
                        "source": "fixture-calendar",
                    }
                )
        return pd.DataFrame(rows)

    def get_daily_bars(
        self, symbol: str, start_date: date, end_date: date, adjustment: str
    ) -> pd.DataFrame:
        dates = pd.bdate_range(start_date, end_date)
        multiplier = 0.5 if adjustment == "qfq" else 1.0
        close = pd.Series([1000.0 + index for index in range(len(dates))]) * multiplier
        return pd.DataFrame(
            {
                "trade_date": dates,
                "open": close - 1,
                "high": close + 2,
                "low": close - 2,
                "close": close,
                "volume": 1000.0,
                "amount": 10000.0,
                "amount_missing": False,
                "symbol": symbol,
                "source": f"akshare:stock_zh_a_hist:{adjustment}",
            }
        )


def _services(tmp_path, provider: FakeResearchProvider):
    layout = DataLayout.from_root(tmp_path / "data")
    database = LocalDatabase(path=layout.root / "catalog.duckdb", layout=layout)
    research = ResearchDatasetSyncService(
        database=database,
        layout=layout,
        provider=provider,
        rate_limit_seconds=0,
    )
    market = MarketDataSyncService(
        database=database,
        layout=layout,
        provider=provider,
        rate_limit_seconds=0,
    )
    return research, market, database


def test_benchmark_sync_is_resumable_and_serves_local_lineage(tmp_path) -> None:
    provider = FakeResearchProvider()
    service, _, database = _services(tmp_path, provider)
    spec = BenchmarkSyncSpec(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 5),
        symbols=("000300",),
    )

    first = service.sync_benchmarks(spec)
    second = service.sync_benchmarks(spec)
    local = LocalFirstProvider(database=database, fallback=provider).get_benchmark_prices(
        "csi000300", date(2024, 1, 2), date(2024, 1, 5)
    )

    assert first["status"] == "published"
    assert first["row_count"] == 4
    assert first["partitions"] == {"published": 1}
    assert second["batch_id"] == first["batch_id"]
    assert second["resumed"] is True
    assert provider.benchmark_calls == 1
    assert local["source"].unique().tolist() == ["local:published_benchmark_daily_bar"]
    assert local["provider_source"].unique().tolist() == ["akshare:stock_zh_index_daily_em"]
    assert local["local_hit"].unique().tolist() == [True]
    assert local["source_batch_id"].unique().tolist() == [first["batch_id"]]


def test_failed_benchmark_partition_resumes_after_provider_recovery(tmp_path) -> None:
    provider = FakeResearchProvider(benchmark_fail_once=True)
    service, _, database = _services(tmp_path, provider)
    spec = BenchmarkSyncSpec(start_date=date(2024, 1, 2), end_date=date(2024, 1, 5))

    failed = service.sync_benchmarks(spec)
    recovered = service.sync_benchmarks(spec)

    assert failed["status"] == "failed"
    assert failed["partitions"] == {"failed": 1}
    assert recovered["status"] == "published"
    assert recovered["batch_id"] == failed["batch_id"]
    assert provider.benchmark_calls == 2
    assert database.status()["relations"]["published_benchmark_daily_bar"] == 4


def test_announcement_sync_preserves_point_in_time_and_annual_periods(tmp_path) -> None:
    provider = FakeResearchProvider()
    service, _, database = _services(tmp_path, provider)

    interim = service.sync_announcements(
        AnnouncementSyncSpec(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            symbols=("600519",),
        )
    )
    annual = service.sync_announcements(
        AnnouncementSyncSpec(
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
            symbols=("600519",),
            report_types=("annual",),
        )
    )
    local = LocalFirstProvider(database=database, fallback=provider).get_interim_announcements(
        "600519", date(2024, 1, 1), date(2024, 12, 31)
    )
    before_publication = database.read_published_announcements(
        "600519", date(2024, 1, 1), date(2024, 8, 14)
    )

    assert interim["status"] == "published"
    assert annual["status"] == "published"
    assert database.status()["relations"]["published_announcement"] == 2
    assert local["source"].tolist() == ["local:published_announcement"]
    assert local["provider_source"].tolist() == [
        "akshare:stock_zh_a_disclosure_report_cninfo:interim"
    ]
    assert local["report_period_end"].tolist() == [pd.Timestamp("2024-06-30")]
    assert local["published_at"].tolist() == [pd.Timestamp("2024-08-15")]
    assert before_publication.empty
    assert provider.announcement_fallback_calls == 0


def test_future_report_period_is_blocked_before_announcement_publication(tmp_path) -> None:
    provider = FakeResearchProvider(invalid_announcement_period=True)
    service, _, database = _services(tmp_path, provider)

    result = service.sync_announcements(
        AnnouncementSyncSpec(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            symbols=("600519",),
        )
    )

    assert result["status"] == "failed"
    assert result["partitions"] == {"failed": 1}
    assert "report_period_available" in result["errors"][0]["error"]
    assert database.status()["relations"]["published_announcement"] == 0


def test_announcement_query_uses_actual_publication_timestamp(tmp_path) -> None:
    provider = FakeResearchProvider()
    service, _, database = _services(tmp_path, provider)
    result = service.sync_announcements(
        AnnouncementSyncSpec(
            start_date=date(2024, 8, 1),
            end_date=date(2024, 8, 31),
            symbols=("600519",),
        )
    )
    assert result["status"] == "published"

    with database.connect() as connection:
        connection.execute("UPDATE announcement SET published_at = TIMESTAMP '2024-08-16 09:30:00'")

    not_yet_public = database.read_published_announcements(
        "600519", date(2024, 8, 1), date(2024, 8, 15)
    )
    public_next_day = database.read_published_announcements(
        "600519", date(2024, 8, 1), date(2024, 8, 16)
    )

    assert not_yet_public.empty
    assert public_next_day["published_at"].tolist() == [pd.Timestamp("2024-08-16 09:30:00")]


def test_empty_announcement_partition_is_a_complete_local_interval(tmp_path) -> None:
    provider = FakeResearchProvider()
    service, _, database = _services(tmp_path, provider)

    result = service.sync_announcements(
        AnnouncementSyncSpec(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 5, 31),
            symbols=("600519",),
        )
    )
    local = LocalFirstProvider(database=database, fallback=provider).get_interim_announcements(
        "600519", date(2024, 1, 1), date(2024, 5, 31)
    )

    assert result["status"] == "published"
    assert result["row_count"] == 0
    assert database.has_announcement_coverage("600519", date(2024, 1, 1), date(2024, 5, 31))
    assert local.empty
    assert provider.announcement_fallback_calls == 0


def test_cli_syncs_benchmarks_and_announcements_with_injected_provider(
    tmp_path, monkeypatch, capsys
) -> None:
    provider = FakeResearchProvider()
    data_dir = tmp_path / "cli-data"
    monkeypatch.setattr("ai_quant_assistant.cli.AkShareProvider", lambda **kwargs: provider)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ai-quant",
            "data",
            "sync-index",
            "--start-date",
            "2024-01-02",
            "--end-date",
            "2024-01-05",
            "--symbols",
            "csi000300",
            "--rate-limit-seconds",
            "0",
            "--data-dir",
            str(data_dir),
        ],
    )
    main()
    benchmark = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ai-quant",
            "data",
            "sync-announcements",
            "--start-date",
            "2024-01-01",
            "--end-date",
            "2024-12-31",
            "--symbols",
            "600519",
            "--types",
            "interim",
            "--rate-limit-seconds",
            "0",
            "--data-dir",
            str(data_dir),
        ],
    )
    main()
    announcements = json.loads(capsys.readouterr().out)

    assert benchmark["dataset"] == "benchmark_daily_bar"
    assert benchmark["status"] == "published"
    assert announcements["dataset"] == "announcement"
    assert announcements["row_count"] == 1


def test_event_study_and_api_use_local_stock_benchmark_and_announcements(
    tmp_path, monkeypatch
) -> None:
    provider = FakeResearchProvider()
    service, market, database = _services(tmp_path, provider)
    price_start = date(2024, 8, 1)
    price_end = date(2024, 9, 21)

    market_result = market.run(
        DataSyncSpec(
            start_date=price_start,
            end_date=price_end,
            symbols=("600519",),
            adjustments=("qfq",),
        )
    )
    benchmark_result = service.sync_benchmarks(
        BenchmarkSyncSpec(start_date=price_start, end_date=price_end)
    )
    announcement_result = service.sync_announcements(
        AnnouncementSyncSpec(
            start_date=date(2024, 8, 1),
            end_date=date(2024, 8, 31),
            symbols=("600519",),
        )
    )
    assert market_result["status"] == "published"
    assert benchmark_result["status"] == "published"
    assert announcement_result["status"] == "published"

    local = LocalFirstProvider(database=database, fallback=provider)
    research = InterimReportResearchService(
        provider=local,
        artifact_dir=tmp_path / "artifacts",
        duckdb_path=database.path,
    )
    output = research.run(
        EventStudySpec(
            symbol="600519",
            start_date=date(2024, 8, 1),
            end_date=date(2024, 8, 31),
            benchmark_symbol="csi000300",
            windows=(2,),
            adjustment="qfq",
        )
    )
    diagnostics = output.result.diagnostics["data_sources"]

    assert diagnostics["announcements"]["serving_sources"] == ["local:published_announcement"]
    assert diagnostics["stock"]["serving_sources"] == ["local:published_daily_bar"]
    assert diagnostics["benchmark"]["serving_sources"] == ["local:published_benchmark_daily_bar"]
    assert all(diagnostics[item]["local_hit"] for item in diagnostics)
    assert provider.announcement_fallback_calls == 0
    assert provider.stock_fallback_calls == 0
    assert provider.benchmark_calls == 1

    monkeypatch.setattr("ai_quant_assistant.api.app.AkShareProvider", lambda **kwargs: provider)
    settings = Settings(
        data_dir=database.layout.root,
        artifact_dir=tmp_path / "api-artifacts",
        cache_dir=tmp_path / "cache",
        duckdb_path=database.path,
    )
    response = TestClient(create_app(settings)).post(
        "/api/v1/research/interim-report-event-study",
        json={
            "source": "local",
            "symbol": "600519",
            "start_date": "2024-08-01",
            "end_date": "2024-08-31",
            "benchmark_symbol": "csi000300",
            "windows": [2],
            "adjustment": "qfq",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert all(item["local_hit"] for item in payload["diagnostics"]["data_sources"].values())
