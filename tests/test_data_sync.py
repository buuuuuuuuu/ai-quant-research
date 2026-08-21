from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from ai_quant_assistant.cli import main
from ai_quant_assistant.domain.models import DataSyncSpec
from ai_quant_assistant.providers.local_provider import LocalFirstProvider
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.sync_service import MarketDataSyncService


class FakeSyncProvider:
    name = "fake-sync"

    def __init__(
        self,
        *,
        fail_qfq_once: bool = False,
        invalid_hfq: bool = False,
        fail_master: bool = False,
    ) -> None:
        self.fail_qfq_once = fail_qfq_once
        self.invalid_hfq = invalid_hfq
        self.fail_master = fail_master
        self.master_calls = 0
        self.calls: list[tuple[str, str]] = []

    def get_security_master(self) -> pd.DataFrame:
        self.master_calls += 1
        if self.fail_master:
            raise ConnectionError("security master endpoint unavailable")
        return pd.DataFrame(
            [
                {
                    "security_id": "SSE.600519",
                    "symbol": "600519",
                    "exchange": "SSE",
                    "name": "贵州茅台",
                    "source": "fixture-sync",
                },
                {
                    "security_id": "SZSE.000001",
                    "symbol": "000001",
                    "exchange": "SZSE",
                    "name": "平安银行",
                    "source": "fixture-sync",
                },
            ]
        )

    def get_trading_calendar(self) -> pd.DataFrame:
        dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
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
        self.calls.append((symbol, adjustment))
        if adjustment == "qfq" and self.fail_qfq_once:
            self.fail_qfq_once = False
            raise ConnectionError("temporary provider failure")
        dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
        multiplier = {"none": 1.0, "qfq": 0.5, "hfq": 2.0}[adjustment]
        close = pd.Series([10.0, 10.5, 10.2, 10.8]) * multiplier
        if adjustment == "hfq" and self.invalid_hfq:
            close.iloc[0] = -1.0
        return pd.DataFrame(
            {
                "trade_date": dates,
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": [1000.0, 1200.0, 900.0, 1500.0],
                "amount": [10000.0, 12600.0, 9180.0, 16200.0],
                "amount_missing": False,
                "symbol": symbol,
                "source": f"fixture-daily:{adjustment}",
            }
        )


class FakeRecentlyListedProvider(FakeSyncProvider):
    def get_daily_bars(
        self, symbol: str, start_date: date, end_date: date, adjustment: str
    ) -> pd.DataFrame:
        if end_date < date(2024, 1, 1):
            return pd.DataFrame(
                columns=[
                    "trade_date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount",
                    "amount_missing",
                    "symbol",
                    "source",
                ]
            )
        return super().get_daily_bars(symbol, start_date, end_date, adjustment)


class TrackingFallback:
    name = "tracking-fallback"

    def __init__(self) -> None:
        self.stock_calls = 0

    def get_interim_announcements(self, *args, **kwargs) -> pd.DataFrame:
        return pd.DataFrame()

    def get_stock_prices(self, symbol, start_date, end_date, adjustment) -> pd.DataFrame:
        self.stock_calls += 1
        return pd.DataFrame(
            {
                "date": pd.to_datetime([start_date]),
                "close": [99.0],
                "symbol": [symbol],
                "source": ["fallback"],
            }
        )

    def get_benchmark_prices(self, *args, **kwargs) -> pd.DataFrame:
        return pd.DataFrame()


def _service(tmp_path, provider: FakeSyncProvider) -> tuple[MarketDataSyncService, LocalDatabase]:
    layout = DataLayout.from_root(tmp_path / "data")
    database = LocalDatabase(path=layout.root / "catalog.duckdb", layout=layout)
    return (
        MarketDataSyncService(
            database=database,
            layout=layout,
            provider=provider,
            rate_limit_seconds=0,
        ),
        database,
    )


def _spec(*adjustments: str) -> DataSyncSpec:
    return DataSyncSpec(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 5),
        symbols=("600519",),
        adjustments=adjustments,
    )


def test_sync_publishes_raw_adjusted_ratios_and_reuses_completed_batch(tmp_path) -> None:
    provider = FakeSyncProvider()
    service, database = _service(tmp_path, provider)

    first = service.run(_spec("qfq"))
    calls_after_first = list(provider.calls)
    second = service.run(_spec("qfq"))

    assert first["status"] == "published"
    assert first["partitions"] == {"published": 2}
    assert first["row_count"] == 8
    assert second["batch_id"] == first["batch_id"]
    assert second["resumed"] is True
    assert provider.calls == calls_after_first
    status = database.status()
    assert status["relations"]["daily_bar_raw"] == 4
    assert status["relations"]["daily_bar"] == 8
    assert status["relations"]["adjustment_ratio"] == 4
    assert status["relations"]["data_coverage"] == 2
    with database.connect(read_only=True) as connection:
        raw_artifacts = connection.execute(
            "SELECT raw_path, raw_sha256 FROM sync_partition ORDER BY partition_key"
        ).fetchall()
    assert all(len(item[1]) == 64 for item in raw_artifacts)
    assert all(Path(item[0]).is_file() for item in raw_artifacts)


def test_explicit_symbol_sync_does_not_require_full_security_master(tmp_path) -> None:
    provider = FakeSyncProvider(fail_master=True)
    service, database = _service(tmp_path, provider)

    result = service.run(_spec("none"))

    assert result["status"] == "published"
    assert result["securities"] == 1
    assert provider.master_calls == 0
    with database.connect(read_only=True) as connection:
        security = connection.execute(
            "SELECT security_id, symbol, exchange, source FROM security_master"
        ).fetchone()
    assert security == ("SSE.600519", "600519", "SSE", "requested-symbol-fallback")


def test_failed_partition_resumes_without_refetching_published_partition(tmp_path) -> None:
    provider = FakeSyncProvider(fail_qfq_once=True)
    service, _ = _service(tmp_path, provider)

    first = service.run(_spec("qfq"))
    second = service.run(_spec("qfq"))

    assert first["status"] == "failed"
    assert first["partitions"] == {"failed": 1, "published": 1}
    assert second["status"] == "published"
    assert second["batch_id"] == first["batch_id"]
    assert provider.calls.count(("600519", "none")) == 1
    assert provider.calls.count(("600519", "qfq")) == 2


def test_blocking_quality_failure_does_not_publish_invalid_adjusted_rows(tmp_path) -> None:
    provider = FakeSyncProvider(invalid_hfq=True)
    service, database = _service(tmp_path, provider)

    result = service.run(_spec("hfq"))

    assert result["status"] == "failed"
    assert result["partitions"] == {"failed": 1, "published": 1}
    with database.connect(read_only=True) as connection:
        adjustments = connection.execute(
            "SELECT DISTINCT adjustment FROM published_daily_bar ORDER BY adjustment"
        ).fetchall()
        failed_checks = connection.execute(
            "SELECT COUNT(*) FROM dq_result WHERE severity = 'blocking' AND NOT passed"
        ).fetchone()[0]
    assert adjustments == [("none",)]
    assert failed_checks >= 1


def test_local_first_provider_uses_coverage_then_falls_back(tmp_path) -> None:
    sync_provider = FakeSyncProvider()
    service, database = _service(tmp_path, sync_provider)
    service.run(_spec("qfq"))
    fallback = TrackingFallback()
    provider = LocalFirstProvider(database=database, fallback=fallback)

    local = provider.get_stock_prices(
        "600519", date(2024, 1, 1), date(2024, 1, 5), "qfq"
    )
    remote = provider.get_stock_prices(
        "600519", date(2024, 1, 1), date(2024, 1, 8), "qfq"
    )

    assert local["source"].unique().tolist() == ["local:published_daily_bar"]
    assert local["provider_source"].unique().tolist() == ["fixture-daily:qfq"]
    assert local["source_batch_id"].nunique() == 1
    assert all(Path(path).is_file() for path in local["raw_path"].unique())
    assert local["local_hit"].unique().tolist() == [True]
    assert local["local_fallback_used"].unique().tolist() == [False]
    assert local["local_fallback_reason"].unique().tolist() == [""]
    assert fallback.stock_calls == 1
    assert remote["source"].tolist() == ["fallback"]
    assert remote["local_hit"].tolist() == [False]
    assert remote["local_fallback_used"].tolist() == [True]
    assert remote["local_fallback_reason"].tolist() == ["coverage_incomplete"]


def test_local_prices_resolve_relative_raw_lineage_from_data_root(tmp_path, monkeypatch) -> None:
    service, database = _service(tmp_path, FakeSyncProvider())
    service.run(_spec("qfq"))
    with database.connect() as connection:
        partitions = connection.execute("SELECT partition_key, raw_path FROM sync_partition").fetchall()
        for partition_key, raw_path in partitions:
            relative_path = Path(raw_path).relative_to(database.layout.root.parent)
            connection.execute(
                "UPDATE sync_partition SET raw_path = ? WHERE partition_key = ?",
                [str(relative_path), partition_key],
            )

    other_directory = tmp_path / "other-working-directory"
    other_directory.mkdir()
    monkeypatch.chdir(other_directory)
    prices = database.read_published_prices(
        "600519", date(2024, 1, 1), date(2024, 1, 5), "qfq"
    )

    assert prices["provider_source"].unique().tolist() == ["fixture-daily:qfq"]
    assert all(Path(path).is_absolute() and Path(path).is_file() for path in prices["raw_path"])


def test_local_first_treats_published_empty_prelisting_partitions_as_coverage(tmp_path) -> None:
    provider = FakeRecentlyListedProvider()
    service, database = _service(tmp_path, provider)
    result = service.run(
        DataSyncSpec(
            start_date=date(2023, 1, 1),
            end_date=date(2024, 1, 5),
            symbols=("600519",),
            adjustments=("qfq",),
        )
    )
    fallback = TrackingFallback()
    local_first = LocalFirstProvider(database=database, fallback=fallback)

    local = local_first.get_stock_prices(
        "600519", date(2023, 1, 1), date(2024, 1, 5), "qfq"
    )

    assert result["status"] == "published"
    assert result["partitions"] == {"published": 4}
    assert local["source"].unique().tolist() == ["local:published_daily_bar"]
    assert local["local_hit"].unique().tolist() == [True]
    assert fallback.stock_calls == 0
    with database.connect() as connection:
        empty_coverage = connection.execute(
            """
            SELECT COUNT(*) FROM data_coverage
            WHERE dataset = 'daily_bar' AND security_id = 'SSE.600519'
              AND adjustment = 'qfq' AND start_date = DATE '2023-01-01'
              AND end_date = DATE '2023-12-31'
            """
        ).fetchone()[0]
    assert empty_coverage == 1


def test_legacy_empty_published_partition_repairs_coverage_at_read_time(tmp_path) -> None:
    provider = FakeRecentlyListedProvider()
    service, database = _service(tmp_path, provider)
    service.run(
        DataSyncSpec(
            start_date=date(2023, 1, 1),
            end_date=date(2024, 1, 5),
            symbols=("600519",),
            adjustments=("qfq",),
        )
    )
    with database.connect() as connection:
        connection.execute(
            """
            DELETE FROM data_coverage
            WHERE dataset = 'daily_bar' AND security_id = 'SSE.600519'
              AND start_date = DATE '2023-01-01'
            """
        )

    assert database.has_coverage(
        "600519", date(2023, 1, 1), date(2024, 1, 5), "qfq"
    )


def test_cli_data_sync_runs_offline_with_injected_provider(
    tmp_path, monkeypatch, capsys
) -> None:
    provider = FakeSyncProvider()
    data_dir = tmp_path / "cli-data"
    monkeypatch.setattr(
        "ai_quant_assistant.cli.AkShareProvider", lambda **kwargs: provider
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ai-quant",
            "data",
            "sync",
            "--start-date",
            "2024-01-01",
            "--end-date",
            "2024-01-05",
            "--symbols",
            "600519",
            "--adjustments",
            "none",
            "--rate-limit-seconds",
            "0",
            "--data-dir",
            str(data_dir),
        ],
    )

    main()
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "published"
    assert payload["securities"] == 1
    assert payload["partitions"] == {"published": 1}
