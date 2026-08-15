from __future__ import annotations

from datetime import date
from pathlib import Path

from ai_quant_assistant.domain.models import EventStudySpec
from ai_quant_assistant.providers.fixture_provider import FixtureProvider
from ai_quant_assistant.research.service import InterimReportResearchService


def main() -> None:
    service = InterimReportResearchService(
        provider=FixtureProvider(),
        artifact_dir=Path("artifacts"),
        duckdb_path=Path("data/catalog.duckdb"),
    )
    output = service.run(
        EventStudySpec(
            symbol="600519",
            start_date=date(2012, 1, 1),
            end_date=date(2025, 12, 31),
            windows=(5, 10, 20),
            round_trip_cost_bps=20,
        )
    )
    print(f"run_id: {output.run_id}")
    print(f"report: {output.artifacts['html_report']}")
    print(output.result.summary.to_string(index=False))


if __name__ == "__main__":
    main()

