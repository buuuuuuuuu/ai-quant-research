from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from ai_quant_assistant.domain.models import BatchEventStudySpec
from ai_quant_assistant.providers.base import MarketDataProvider
from ai_quant_assistant.reports.batch_writer import BatchReportWriter
from ai_quant_assistant.research.service import InterimReportResearchService
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.snapshots import write_json

logger = logging.getLogger(__name__)


class BatchInterimReportResearchService:
    def __init__(
        self,
        *,
        provider: MarketDataProvider,
        artifact_dir: Path,
        database: LocalDatabase | None = None,
    ) -> None:
        self.provider = provider
        self.artifact_dir = artifact_dir
        self.database = database
        self.research = InterimReportResearchService(
            provider=provider,
            artifact_dir=artifact_dir,
            duckdb_path=database.path if database else None,
        )
        self.writer = BatchReportWriter()

    @staticmethod
    def _batch_id() -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"batch-{timestamp}-{uuid4().hex[:8]}"

    @staticmethod
    def _pooled_summary(events: pd.DataFrame) -> pd.DataFrame:
        columns = [
            "window",
            "stock_count",
            "sample_size",
            "mean_stock_net_return",
            "mean_benchmark_return",
            "mean_excess_return",
            "median_excess_return",
            "win_rate",
        ]
        if events.empty:
            return pd.DataFrame(columns=columns)
        rows = []
        for window, group in events.groupby("window", sort=True):
            excess = pd.to_numeric(group["excess_return"], errors="coerce")
            rows.append(
                {
                    "window": int(window),
                    "stock_count": int(group["symbol"].nunique()),
                    "sample_size": int(len(group)),
                    "mean_stock_net_return": float(group["stock_net_return"].mean()),
                    "mean_benchmark_return": float(group["benchmark_return"].mean()),
                    "mean_excess_return": float(excess.mean()),
                    "median_excess_return": float(excess.median()),
                    "win_rate": float((excess > 0).mean()),
                }
            )
        return pd.DataFrame(rows, columns=columns)

    def _enrich_events(self, symbol: str, events: pd.DataFrame) -> pd.DataFrame:
        enriched = events.copy()
        if self.database is None:
            return enriched
        snapshots = [
            self.database.read_latest_financial_snapshot(symbol, row.announcement_date)
            for row in enriched.itertuples(index=False)
        ]
        if not any(snapshots):
            return enriched
        return pd.concat(
            [enriched.reset_index(drop=True), pd.DataFrame(snapshots).reset_index(drop=True)],
            axis=1,
        )

    def run(self, spec: BatchEventStudySpec) -> dict[str, Any]:
        batch_id = self._batch_id()
        batch_dir = self.artifact_dir / "batch-runs" / batch_id
        batch_dir.mkdir(parents=True, exist_ok=False)
        started_at = datetime.now(UTC)
        stock_summaries: list[pd.DataFrame] = []
        all_events: list[pd.DataFrame] = []
        failures: list[dict[str, Any]] = []
        runs: list[dict[str, Any]] = []

        for index, symbol in enumerate(spec.symbols, start=1):
            logger.info("batch event study %s/%s: %s", index, len(spec.symbols), symbol)
            try:
                output = self.research.run(spec.event_spec(symbol))
                events = self._enrich_events(symbol, output.result.events)
                summary = output.result.summary.copy()
                summary.insert(0, "symbol", symbol)
                summary["run_id"] = output.run_id
                latest = (
                    self.database.read_latest_financial_snapshot(symbol, spec.end_date)
                    if self.database
                    else {}
                )
                summary["financial_report_period_end"] = latest.get(
                    "financial_report_period_end", ""
                )
                stock_summaries.append(summary)
                all_events.append(events)
                diagnostics = output.result.diagnostics.get("data_sources", {})
                runs.append(
                    {
                        "symbol": symbol,
                        "run_id": output.run_id,
                        "status": "succeeded",
                        "local_hits": {
                            name: value.get("local_hit") for name, value in diagnostics.items()
                        },
                        "financial_local_hit": bool(latest),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - a failed symbol must not stop the universe
                logger.warning("batch event study failed for %s: %s", symbol, exc)
                failure = {
                    "symbol": symbol,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                failures.append(failure)
                runs.append({**failure, "status": "failed"})

        stock_summary = (
            pd.concat(stock_summaries, ignore_index=True)
            if stock_summaries
            else pd.DataFrame(columns=["symbol", "window", "sample_size", "run_id"])
        )
        events = (
            pd.concat(all_events, ignore_index=True)
            if all_events
            else pd.DataFrame(columns=["symbol", "window", "excess_return"])
        )
        failure_frame = pd.DataFrame(failures, columns=["symbol", "error_type", "error"])
        pooled_summary = self._pooled_summary(events)
        artifacts = self.writer.write(
            batch_dir,
            spec=spec.to_dict(),
            pooled_summary=pooled_summary,
            stock_summary=stock_summary,
            events=events,
            failures=failure_frame,
        )
        completed_at = datetime.now(UTC)
        status = "failed" if not stock_summaries else "partial" if failures else "succeeded"
        payload = {
            "batch_id": batch_id,
            "status": status,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_seconds": (completed_at - started_at).total_seconds(),
            "provider": self.provider.name,
            "spec": spec.to_dict(),
            "requested_symbols": len(spec.symbols),
            "successful_symbols": len(stock_summaries),
            "failed_symbols": len(failures),
            "pooled_summary": pooled_summary.to_dict(orient="records"),
            "runs": runs,
            "failures": failures,
            "artifacts": {name: str(path) for name, path in artifacts.items()},
        }
        manifest = batch_dir / "batch_manifest.json"
        payload["artifacts"]["batch_manifest"] = str(manifest)
        write_json(manifest, payload)
        return payload
