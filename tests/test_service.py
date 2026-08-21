import json
from datetime import date

from ai_quant_assistant.domain.models import EventStudySpec
from ai_quant_assistant.providers.fixture_provider import FixtureProvider
from ai_quant_assistant.research.service import InterimReportResearchService


def test_fixture_golden_path_writes_reproducible_artifacts(tmp_path) -> None:
    service = InterimReportResearchService(
        provider=FixtureProvider(),
        artifact_dir=tmp_path / "artifacts",
        duckdb_path=tmp_path / "catalog.duckdb",
    )
    spec = EventStudySpec(
        symbol="600519",
        start_date=date(2012, 1, 1),
        end_date=date(2025, 12, 31),
        windows=(5, 10, 20),
        round_trip_cost_bps=20,
        min_events=10,
    )

    output = service.run(spec)

    assert output.result.summary["sample_size"].tolist() == [14, 14, 14]
    assert output.result.events["report_year"].nunique() == 14
    for name in ["summary_csv", "events_csv", "diagnostics_json", "excel", "html_report"]:
        assert output.artifacts[name].exists(), name
    for snapshot in output.snapshot_refs.values():
        assert len(snapshot["sha256"]) == 64
        assert snapshot["rows"] > 0
    manifest = json.loads(output.artifacts["run_manifest"].read_text("utf-8"))
    assert manifest["status"] == "succeeded"
    assert manifest["provider"] == "fixture-v1"
    assert "snapshots" in manifest
    assert output.result.diagnostics["data_sources"]["stock"]["serving_sources"] == [
        "fixture-v1:hfq"
    ]
    report = output.artifacts["html_report"].read_text("utf-8")
    assert "半年报公告后超额收益" in report
    assert "数据来源与血缘" in report
    assert "fixture-v1:hfq" in report
    assert "不构成投资建议" in report
