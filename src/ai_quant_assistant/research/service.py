from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_quant_assistant import __version__
from ai_quant_assistant.domain.models import EventStudySpec, RunOutput
from ai_quant_assistant.providers.base import MarketDataProvider
from ai_quant_assistant.reports.writer import ReportWriter
from ai_quant_assistant.research.event_study import EventStudyEngine
from ai_quant_assistant.storage.catalog import DuckDBCatalog
from ai_quant_assistant.storage.snapshots import SnapshotStore, write_json


class InterimReportResearchService:
    def __init__(
        self,
        provider: MarketDataProvider,
        artifact_dir: Path,
        duckdb_path: Path | None = None,
        database=None,
    ) -> None:
        self.provider = provider
        self.database = database
        self.artifact_dir = artifact_dir
        self.engine = EventStudyEngine()
        self.report_writer = ReportWriter()
        self.catalog = DuckDBCatalog(duckdb_path) if duckdb_path else None

    @staticmethod
    def _run_id() -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{timestamp}-{uuid4().hex[:8]}"

    def run(self, spec: EventStudySpec) -> RunOutput:
        run_id = self._run_id()
        run_dir = self.artifact_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        manifest_path = run_dir / "run.json"
        started_at = datetime.now(UTC)
        base_manifest: dict[str, Any] = {
            "run_id": run_id,
            "status": "running",
            "started_at": started_at.isoformat(),
            "provider": self.provider.name,
            "code_version": __version__,
            "spec": spec.to_dict(),
        }
        write_json(manifest_path, base_manifest)

        try:
            price_end = spec.end_date + timedelta(days=max(spec.windows) * 3 + 15)
            announcements = self.provider.get_interim_announcements(
                spec.symbol, spec.start_date, spec.end_date
            )
            stock_prices = self.provider.get_stock_prices(
                spec.symbol, spec.start_date, price_end, spec.adjustment
            )
            benchmark_prices = self.provider.get_benchmark_prices(
                spec.benchmark_symbol, spec.start_date, price_end
            )

            snapshot_store = SnapshotStore(run_dir)
            refs = {
                "announcements": snapshot_store.save("announcements", announcements),
                "stock_prices": snapshot_store.save("stock_prices", stock_prices),
                "benchmark_prices": snapshot_store.save("benchmark_prices", benchmark_prices),
            }
            snapshot_refs = {name: ref.to_dict() for name, ref in refs.items()}
            if self.catalog:
                for snapshot in snapshot_refs.values():
                    self.catalog.register(run_id, snapshot)

            result = self.engine.run(spec, announcements, stock_prices, benchmark_prices)
            artifacts = self.report_writer.write(run_dir, result)
            artifacts["run_manifest"] = manifest_path
            completed_at = datetime.now(UTC)
            manifest = {
                **base_manifest,
                "status": "succeeded",
                "completed_at": completed_at.isoformat(),
                "duration_seconds": (completed_at - started_at).total_seconds(),
                "snapshots": snapshot_refs,
                "diagnostics": result.diagnostics,
                "artifacts": {name: str(path) for name, path in artifacts.items()},
            }
            write_json(manifest_path, manifest)
            if self.database is not None:
                from ai_quant_assistant.storage.research_run_index import register_run
                register_run(self.database, "event_study", {**manifest, "summary": result.summary.to_dict(orient="records")})
            return RunOutput(
                run_id=run_id,
                run_dir=run_dir,
                result=result,
                artifacts=artifacts,
                snapshot_refs=snapshot_refs,
            )
        except Exception as exc:
            failed_at = datetime.now(UTC)
            write_json(
                manifest_path,
                {
                    **base_manifest,
                    "status": "failed",
                    "failed_at": failed_at.isoformat(),
                    "duration_seconds": (failed_at - started_at).total_seconds(),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise
