from __future__ import annotations

import json
import sys
from datetime import date

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from test_factor_backtest import _database
from test_web_workbench import _settings

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.cli import main
from ai_quant_assistant.domain.models import FactorBacktestSpec, FactorComponent, FactorLabSpec
from ai_quant_assistant.errors import ProviderError
from ai_quant_assistant.providers.akshare_provider import AkShareProvider
from ai_quant_assistant.research.factor_backtest import FactorBacktestService
from ai_quant_assistant.research.factor_lab import FactorLabService
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.schema import MIGRATIONS, SCHEMA_VERSION
from ai_quant_assistant.storage.universe import UniverseRepository


def _industry_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": "600519",
                "industry_name": "消费",
                "industry_code": "C",
                "effective_from": "2023-01-01",
                "published_at": "2023-01-02 09:00:00",
            },
            {
                "symbol": "000001",
                "industry_name": "消费",
                "industry_code": "C",
                "effective_from": "2023-01-01",
                "published_at": "2023-01-02 09:00:00",
            },
            {
                "symbol": "300750",
                "industry_name": "新能源",
                "industry_code": "E",
                "effective_from": "2024-03-01",
                "published_at": "2024-02-28 09:00:00",
            },
        ]
    )


def _constituent_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": "600519",
                "effective_from": "2024-01-01",
                "effective_until": "2024-02-29",
                "published_at": "2023-12-28 10:00:00",
                "weight": 0.4,
            },
            {
                "symbol": "000001",
                "effective_from": "2024-02-01",
                "published_at": "2024-01-30 10:00:00",
                "weight": 0.3,
            },
            {
                "symbol": "300750",
                "effective_from": "2024-03-01",
                "published_at": "2024-02-28 10:00:00",
                "weight": 0.3,
            },
        ]
    )


def _repository(tmp_path):
    database = _database(tmp_path)
    repository = UniverseRepository(database)
    repository.store_industries(
        _industry_rows(),
        classification_system="provider",
        history_mode="verified_history",
        as_of=date(2024, 3, 1),
        source="fixture:industry-history",
    )
    repository.store_constituents(
        _constituent_rows(),
        benchmark_symbol="csi000300",
        history_mode="verified_history",
        as_of=date(2024, 3, 1),
        source="fixture:index-history",
    )
    return database, repository


def test_v6_catalog_migrates_to_v7_preserving_operation_tasks(tmp_path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    database = LocalDatabase(root / "catalog.duckdb", DataLayout.from_root(root))
    with database.connect() as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name VARCHAR NOT NULL, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for migration in MIGRATIONS[:6]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                [migration.version, migration.name],
            )
        connection.execute(
            "INSERT INTO operation_task (task_id, task_type, status, progress, message, spec_json) VALUES ('old', 'backup', 'succeeded', 100, 'done', '{}')"
        )
    result = database.initialize()
    status = database.status()
    assert result["applied_migrations"] == [7]
    assert status["schema_version"] == SCHEMA_VERSION == 8
    assert status["relations"]["operation_task"] == 1
    assert status["relations"]["security_industry_history"] == 0
    assert status["relations"]["index_constituent_history"] == 0


def test_industry_history_obeys_effective_publication_and_knowledge_time(tmp_path) -> None:
    _, repository = _repository(tmp_path)
    before = repository.industries(("300750",), "2024-02-28 08:59:00")
    before_effective = repository.industries(("300750",), "2024-02-29 15:00:00")
    visible = repository.industries(("300750",), "2024-03-01 15:00:00")
    assert before.empty
    assert before_effective.empty
    assert visible["industry_name"].tolist() == ["新能源"]
    assert visible["history_mode"].tolist() == ["verified_history"]


def test_verified_history_is_hidden_until_knowledge_time(tmp_path) -> None:
    database = _database(tmp_path)
    repository = UniverseRepository(database)
    repository.store_industries(
        pd.DataFrame(
            [
                {
                    "symbol": "600519",
                    "industry_name": "消费",
                    "effective_from": "2024-01-01",
                    "published_at": "2024-01-02 09:00:00",
                    "knowledge_time": "2024-01-03 11:00:00",
                }
            ]
        ),
        classification_system="provider",
        history_mode="verified_history",
        as_of=date(2024, 1, 3),
        source="official:delayed-history",
    )
    assert repository.industries(("600519",), "2024-01-03 10:59:59").empty
    assert len(repository.industries(("600519",), "2024-01-03 11:00:01")) == 1


def test_failed_history_import_retains_batch_failure_reason(tmp_path) -> None:
    database = _database(tmp_path)
    repository = UniverseRepository(database)
    with pytest.raises(ValueError, match="effective_from and published_at"):
        repository.store_industries(
            pd.DataFrame([{"symbol": "600519", "industry_name": "消费"}]),
            classification_system="provider",
            history_mode="verified_history",
            as_of=date(2024, 1, 3),
            source="fixture:invalid-history",
        )
    with database.connect(read_only=True) as connection:
        status, error = connection.execute(
            "SELECT status, error FROM universe_sync_batch WHERE source='fixture:invalid-history'"
        ).fetchone()
    assert status == "failed"
    assert "effective_from and published_at" in error


def test_observed_snapshots_cannot_be_backdated_or_used_as_verified_history(tmp_path) -> None:
    _, repository = _repository(tmp_path)
    rows = pd.DataFrame(
        [{"symbol": "600519", "industry_name": "白酒", "effective_from": "2015-01-01"}]
    )
    with pytest.raises(ValueError, match="backdated"):
        repository.store_industries(
            rows,
            classification_system="snapshot",
            history_mode="observed_snapshot",
            as_of=date(2024, 3, 1),
            source="fixture:snapshot",
        )
    snapshot = pd.DataFrame([{"symbol": "600519", "industry_name": "白酒"}])
    repository.store_industries(
        snapshot,
        classification_system="snapshot",
        history_mode="observed_snapshot",
        as_of=date(2024, 3, 1),
        source="fixture:snapshot",
    )
    assert repository.industries(
        ("600519",), "2024-02-29 15:00:00", classification_system="snapshot"
    ).empty
    assert repository.industries(
        ("600519",), "2024-03-01 15:00:00", classification_system="snapshot", verified_only=True
    ).empty


def test_historical_index_membership_changes_without_future_or_survivor_leakage(tmp_path) -> None:
    _, repository = _repository(tmp_path)
    january = repository.constituents("csi000300", "2024-01-15 15:00:00")
    february = repository.constituents("csi000300", "2024-02-15 15:00:00")
    march = repository.constituents("csi000300", "2024-03-01 15:00:00")
    assert set(january["symbol"]) == {"600519"}
    assert set(february["symbol"]) == {"600519", "000001"}
    assert set(march["symbol"]) == {"000001", "300750"}


def test_dynamic_universe_honors_listing_and_delisting_dates(tmp_path) -> None:
    database, repository = _repository(tmp_path)
    before_listing = repository.build(as_of="2024-02-29 15:00:00", mode="all_available")
    after_listing = repository.build(as_of="2024-03-01 15:00:00", mode="all_available")
    assert "300750" not in before_listing["symbols"]
    assert "300750" in after_listing["symbols"]
    with database.connect() as connection:
        connection.execute(
            "UPDATE security_master SET delist_date='2024-02-15' WHERE symbol='000001'"
        )
    after_delisting = repository.build(as_of="2024-03-01 15:00:00", mode="all_available")
    assert "000001" not in after_delisting["symbols"]


def test_historical_index_rejects_unverified_current_snapshot(tmp_path) -> None:
    database = _database(tmp_path)
    repository = UniverseRepository(database)
    repository.store_constituents(
        pd.DataFrame([{"symbol": "600519"}]),
        benchmark_symbol="csi000300",
        history_mode="observed_snapshot",
        as_of=date(2024, 3, 1),
        source="fixture:snapshot",
    )
    with pytest.raises(ValueError, match="verified"):
        repository.build(as_of="2024-03-01 15:00:00", mode="historical_index")
    observed = repository.build(as_of="2024-03-01 15:00:00", mode="observed_index")
    assert observed["symbols"] == ["600519"]
    assert observed["survivorship_bias_warning"]


def test_industry_neutral_ranking_and_exposure_cap_avoid_concentration(tmp_path) -> None:
    database, _ = _repository(tmp_path)
    service = FactorBacktestService(database=database, artifact_dir=tmp_path / "artifacts")
    spec = FactorBacktestSpec(
        symbols=("600519", "000001", "300750"),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 3, 8),
        factor="roe",
        top_n=2,
        industry_neutralization="within_industry_rank",
        max_industry_weight=0.5,
        require_verified_industry=True,
    )
    factors = service.eligible_snapshot(spec, "2024-03-01 15:00:00", {"600519", "000001", "300750"})
    selected = factors.loc[factors["selected"]]
    assert len(selected) == 2
    assert selected["industry_name"].nunique() == 2
    assert "raw_factor_value" in selected.columns


def test_neutralization_rejects_missing_trustworthy_industry_history(tmp_path) -> None:
    database = _database(tmp_path)
    service = FactorBacktestService(database=database, artifact_dir=tmp_path / "artifacts")
    spec = FactorBacktestSpec(
        symbols=("600519", "000001"),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 3, 8),
        top_n=1,
        industry_neutralization="within_industry_rank",
        require_verified_industry=True,
    )
    with pytest.raises(ValueError, match="industry classification"):
        service.eligible_snapshot(spec, "2024-01-02 15:00:00", {"600519", "000001"})


def test_historical_index_backtest_uses_signal_date_constituents(tmp_path) -> None:
    database, _ = _repository(tmp_path)
    service = FactorBacktestService(database=database, artifact_dir=tmp_path / "artifacts")
    result = service.run(
        FactorBacktestSpec(
            symbols=("600519", "000001", "300750"),
            start_date=date(2024, 1, 2),
            end_date=date(2024, 3, 8),
            factor="roe",
            top_n=1,
            rebalance="monthly",
            universe_mode="historical_index",
        )
    )
    rows = pd.read_csv(result["artifacts"]["factor_selections_csv"], dtype={"symbol": str})
    january = rows.loc[rows["signal_date"].str.startswith("2024-01")]
    march = rows.loc[rows["signal_date"].str.startswith("2024-03")]
    assert set(january["symbol"]) == {"600519"}
    assert "600519" not in set(march["symbol"])
    assert result["diagnostics"]["survivorship_bias_warning"] is None


def test_factor_lab_exports_dynamic_universe_and_industry_diagnostics(tmp_path) -> None:
    database, _ = _repository(tmp_path)
    result = FactorLabService(database=database, artifact_dir=tmp_path / "artifacts").run(
        FactorLabSpec(
            symbols=("600519", "000001", "300750"),
            start_date=date(2024, 1, 2),
            end_date=date(2024, 3, 8),
            factors=(FactorComponent("roe"),),
            top_n_values=(1,),
            rebalance_values=("monthly",),
            commission_bps_values=(5.0,),
            universe_mode="historical_index",
            industry_neutralization="within_industry_rank",
            require_verified_industry=True,
        )
    )
    rows = pd.read_csv(result["artifacts"]["cross_sections_csv"], dtype={"symbol": str})
    assert "industry_name" in rows.columns
    assert result["diagnostics"]["universe_mode"] == "historical_index"
    assert result["diagnostics"]["industry_neutralization"] == "within_industry_rank"
    assert result["diagnostics"]["survivorship_bias_warning"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("universe_mode", "future"),
        ("industry_neutralization", "fake"),
        ("max_industry_weight", 0.0),
        ("max_industry_weight", 1.1),
    ],
)
def test_backtest_rejects_invalid_universe_and_industry_controls(field, value) -> None:
    with pytest.raises(ValueError):
        FactorBacktestSpec(
            symbols=("600519", "000001"),
            start_date=date(2024, 1, 2),
            end_date=date(2024, 3, 8),
            top_n=1,
            **{field: value},
        )


def test_universe_cli_builds_historical_stock_pool(tmp_path, monkeypatch, capsys) -> None:
    _repository(tmp_path)
    monkeypatch.setenv("AI_QUANT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AI_QUANT_DUCKDB_PATH", str(tmp_path / "data" / "catalog.duckdb"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ai-quant",
            "universe",
            "build",
            "--as-of",
            "2024-02-15",
            "--mode",
            "historical_index",
            "--benchmark",
            "csi000300",
        ],
    )
    main()
    result = json.loads(capsys.readouterr().out)
    assert set(result["symbols"]) == {"600519", "000001"}
    assert result["history_verified"] is True


def test_verified_history_csv_import_requires_effective_and_publication_dates(
    tmp_path, monkeypatch, capsys
) -> None:
    _database(tmp_path)
    path = tmp_path / "industries.csv"
    _industry_rows().to_csv(path, index=False)
    monkeypatch.setenv("AI_QUANT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AI_QUANT_DUCKDB_PATH", str(tmp_path / "data" / "catalog.duckdb"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ai-quant",
            "data",
            "import-universe-history",
            "--dataset",
            "industries",
            "--file",
            str(path),
            "--source-name",
            "official:test",
            "--as-of",
            "2024-03-01",
        ],
    )
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["history_mode"] == "verified_history"
    assert result["row_count"] == 3


def test_live_snapshot_cli_publishes_observation_and_rejects_backdating(
    tmp_path, monkeypatch, capsys
) -> None:
    _database(tmp_path)
    monkeypatch.setenv("AI_QUANT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AI_QUANT_DUCKDB_PATH", str(tmp_path / "data" / "catalog.duckdb"))
    monkeypatch.setattr(
        AkShareProvider,
        "get_industry_snapshot",
        lambda self, symbols: pd.DataFrame(
            [{"symbol": symbol, "industry_name": "消费"} for symbol in symbols]
        ),
    )
    monkeypatch.setattr(sys, "argv", ["ai-quant", "data", "sync-industries", "--symbols", "600519"])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["history_mode"] == "observed_snapshot"
    assert result["as_of"] == date.today().isoformat()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ai-quant",
            "data",
            "sync-industries",
            "--symbols",
            "600519",
            "--as-of",
            "2010-01-01",
        ],
    )
    with pytest.raises(ValueError, match="today's observation date"):
        main()


def test_universe_web_api_reports_trust_and_rejects_fake_historical_index(tmp_path) -> None:
    _repository(tmp_path)
    with TestClient(create_app(_settings(tmp_path))) as client:
        overview = client.get("/api/v1/universe/overview", params={"as_of": "2024-03-01"})
        universe = client.get(
            "/api/v1/universe/build", params={"as_of": "2024-02-15", "mode": "historical_index"}
        )
        missing = client.get(
            "/api/v1/universe/build", params={"as_of": "2020-01-01", "mode": "historical_index"}
        )
    assert overview.status_code == 200
    assert overview.json()["indexes"][0]["history_mode"] == "verified_history"
    assert set(universe.json()["symbols"]) == {"600519", "000001"}
    assert missing.status_code == 422


def test_akshare_snapshots_are_normalized_without_claiming_historical_membership(
    tmp_path, monkeypatch
) -> None:
    class Fake:
        @staticmethod
        def stock_individual_info_em(symbol, timeout=None):
            return pd.DataFrame({"item": ["行业"], "value": ["白酒"]})

        @staticmethod
        def index_stock_cons_csindex(symbol):
            assert symbol == "000300"
            return pd.DataFrame({"成分券代码": ["600519", "000001"], "权重": [5.0, 2.0]})

    monkeypatch.setattr(AkShareProvider, "_akshare", staticmethod(lambda: Fake()))
    provider = AkShareProvider(tmp_path / "cache")
    industry = provider.get_industry_snapshot(("600519",))
    assert industry["industry_name"].tolist() == ["白酒"]
    assert industry["source"].tolist() == ["akshare:stock_individual_info_em"]
    assert industry.attrs["diagnostics"]["provider_fallback_used"] is False
    assert set(provider.get_index_constituent_snapshot("csi000300")["symbol"]) == {
        "600519",
        "000001",
    }


def test_industry_snapshot_falls_back_to_cninfo_and_preserves_actual_source(
    tmp_path, monkeypatch
) -> None:
    class Fake:
        @staticmethod
        def stock_individual_info_em(symbol, timeout=None):
            raise ConnectionError("eastmoney blocked")

        @staticmethod
        def stock_profile_cninfo(symbol):
            return pd.DataFrame({"所属行业": ["酒、饮料和精制茶制造业"]})

    monkeypatch.setattr(AkShareProvider, "_akshare", staticmethod(lambda: Fake()))
    frame = AkShareProvider(tmp_path / "cache").get_industry_snapshot(("600519",))
    assert frame["industry_name"].tolist() == ["酒、饮料和精制茶制造业"]
    assert frame["source"].tolist() == ["akshare:stock_profile_cninfo"]
    assert frame.attrs["diagnostics"]["provider_sources"] == ["akshare:stock_profile_cninfo"]
    assert frame.attrs["diagnostics"]["provider_fallback_count"] == 1


def test_industry_snapshot_falls_back_to_xueqiu_with_exchange_symbol(tmp_path, monkeypatch) -> None:
    class Fake:
        @staticmethod
        def stock_individual_info_em(symbol, timeout=None):
            return pd.DataFrame({"item": ["股票简称"], "value": ["贵州茅台"]})

        @staticmethod
        def stock_profile_cninfo(symbol):
            raise TimeoutError("cninfo timeout")

        @staticmethod
        def stock_individual_basic_info_xq(symbol, timeout=None):
            assert symbol == "SH600519"
            return pd.DataFrame({"item": ["affiliate_industry"], "value": ["白酒"]})

    monkeypatch.setattr(AkShareProvider, "_akshare", staticmethod(lambda: Fake()))
    frame = AkShareProvider(tmp_path / "cache").get_industry_snapshot(("600519",))
    assert frame["industry_name"].tolist() == ["白酒"]
    assert frame["source"].tolist() == ["akshare:stock_individual_basic_info_xq"]


def test_industry_snapshot_reports_partial_failures_without_losing_successes(
    tmp_path, monkeypatch, caplog
) -> None:
    class Fake:
        @staticmethod
        def stock_individual_info_em(symbol, timeout=None):
            if symbol == "600519":
                return pd.DataFrame({"item": ["行业"], "value": ["白酒"]})
            raise ConnectionError("eastmoney unreachable")

        @staticmethod
        def stock_profile_cninfo(symbol):
            raise TimeoutError("cninfo unavailable")

        @staticmethod
        def stock_individual_basic_info_xq(symbol, timeout=None):
            raise PermissionError("xueqiu requires authentication")

    monkeypatch.setattr(AkShareProvider, "_akshare", staticmethod(lambda: Fake()))
    frame = AkShareProvider(tmp_path / "cache").get_industry_snapshot(("600519", "000001"))
    diagnostics = frame.attrs["diagnostics"]
    assert frame["symbol"].tolist() == ["600519"]
    assert diagnostics["requested_symbols"] == 2
    assert diagnostics["successful_symbols"] == 1
    assert diagnostics["failed_symbols"][0]["symbol"] == "000001"
    assert len(diagnostics["failed_symbols"][0]["attempts"]) == 3
    assert "eastmoney unreachable" in caplog.text
    assert "cninfo unavailable" in caplog.text


def test_mixed_industry_sources_keep_per_security_lineage(tmp_path) -> None:
    database = _database(tmp_path)
    repository = UniverseRepository(database)
    result = repository.store_industries(
        pd.DataFrame(
            [
                {
                    "symbol": "600519",
                    "industry_name": "白酒",
                    "source": "akshare:stock_profile_cninfo",
                },
                {
                    "symbol": "000001",
                    "industry_name": "银行",
                    "source": "akshare:stock_individual_info_em",
                },
            ]
        ),
        classification_system="provider",
        history_mode="observed_snapshot",
        as_of=date(2024, 3, 1),
        source="akshare:mixed_industry_sources",
    )
    industry = repository.industries(("600519", "000001"), "2024-03-01 15:00:00")
    assert result["source"] == "akshare:mixed_industry_sources"
    assert dict(zip(industry["symbol"], industry["source"], strict=True)) == {
        "000001": "akshare:stock_individual_info_em",
        "600519": "akshare:stock_profile_cninfo",
    }


def test_live_snapshot_cli_displays_fallback_source_and_diagnostics(
    tmp_path, monkeypatch, capsys
) -> None:
    database = _database(tmp_path)
    monkeypatch.setenv("AI_QUANT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AI_QUANT_DUCKDB_PATH", str(tmp_path / "data" / "catalog.duckdb"))

    class Fake:
        @staticmethod
        def stock_individual_info_em(symbol, timeout=None):
            raise ConnectionError("eastmoney blocked")

        @staticmethod
        def stock_profile_cninfo(symbol):
            return pd.DataFrame({"所属行业": ["白酒"]})

    monkeypatch.setattr(AkShareProvider, "_akshare", staticmethod(lambda: Fake()))
    monkeypatch.setattr(sys, "argv", ["ai-quant", "data", "sync-industries", "--symbols", "600519"])
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "akshare:stock_profile_cninfo"
    assert payload["diagnostics"]["provider_fallback_used"] is True
    assert payload["diagnostics"]["provider_sources"] == ["akshare:stock_profile_cninfo"]
    with database.connect(read_only=True) as connection:
        source = connection.execute("SELECT source FROM security_industry_history").fetchone()[0]
    assert source == "akshare:stock_profile_cninfo"


def test_akshare_empty_industry_snapshot_is_not_published_as_success(tmp_path, monkeypatch) -> None:
    class Fake:
        @staticmethod
        def stock_individual_info_em(symbol, timeout=None):
            raise RuntimeError("provider offline")

    monkeypatch.setattr(AkShareProvider, "_akshare", staticmethod(lambda: Fake()))
    provider = AkShareProvider(tmp_path / "cache")
    with pytest.raises(ProviderError, match="provider offline") as error:
        provider.get_industry_snapshot(("600519",))
    assert "stock_individual_info_em" in str(error.value)
    assert "stock_profile_cninfo" in str(error.value)
    assert "stock_individual_basic_info_xq" in str(error.value)


def test_factor_lab_domain_preserves_universe_and_industry_controls() -> None:
    spec = FactorLabSpec(
        symbols=("600519", "000001"),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 3, 8),
        factors=(FactorComponent("roe"),),
        top_n_values=(1,),
        universe_mode="historical_index",
        industry_neutralization="within_industry_rank",
        max_industry_weight=0.5,
        require_verified_industry=True,
    )
    payload = spec.to_dict()
    assert payload["universe_mode"] == "historical_index"
    assert payload["industry_neutralization"] == "within_industry_rank"
    assert payload["require_verified_industry"] is True
