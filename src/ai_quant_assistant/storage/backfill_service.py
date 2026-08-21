from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from ai_quant_assistant.domain.models import (
    AnnouncementSyncSpec,
    BenchmarkSyncSpec,
    DataSyncSpec,
    FinancialSyncSpec,
    ResearchBackfillSpec,
)
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.financial_sync_service import FinancialDataSyncService
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.research_sync_service import ResearchDatasetSyncService
from ai_quant_assistant.storage.sync_service import MarketDataSyncService

logger = logging.getLogger(__name__)


class ResearchBackfillService:
    def __init__(
        self,
        *,
        database: LocalDatabase,
        layout: DataLayout,
        provider: Any,
        rate_limit_seconds: float = 0.2,
    ) -> None:
        self.database = database
        self.provider = provider
        self.market = MarketDataSyncService(
            database=database,
            layout=layout,
            provider=provider,
            rate_limit_seconds=rate_limit_seconds,
        )
        self.research = ResearchDatasetSyncService(
            database=database,
            layout=layout,
            provider=provider,
            rate_limit_seconds=rate_limit_seconds,
        )
        self.financial = FinancialDataSyncService(
            database=database,
            layout=layout,
            provider=provider,
            rate_limit_seconds=rate_limit_seconds,
        )

    def run(self, spec: ResearchBackfillSpec) -> dict[str, Any]:
        price_end = spec.end_date + timedelta(days=max(spec.windows) * 3 + 15)
        disclosure_end = spec.end_date + timedelta(days=180)
        stages: dict[str, dict[str, Any]] = {}

        logger.info("research backfill: syncing %s stock histories", len(spec.symbols))
        stages["daily_bar"] = self.market.run(
            DataSyncSpec(
                start_date=spec.start_date,
                end_date=price_end,
                symbols=spec.symbols,
                adjustments=spec.adjustments,
                resume=spec.resume,
            )
        )
        if stages["daily_bar"]["status"] == "failed":
            return self._result(spec, stages, price_end, disclosure_end)

        logger.info("research backfill: syncing benchmark %s", spec.benchmark_symbol)
        stages["benchmark_daily_bar"] = self.research.sync_benchmarks(
            BenchmarkSyncSpec(
                start_date=spec.start_date,
                end_date=price_end,
                symbols=(spec.benchmark_symbol,),
                resume=spec.resume,
            )
        )
        if stages["benchmark_daily_bar"]["status"] == "failed":
            return self._result(spec, stages, price_end, disclosure_end)

        logger.info("research backfill: syncing disclosures and publication timestamps")
        stages["announcement"] = self.research.sync_announcements(
            AnnouncementSyncSpec(
                start_date=spec.start_date,
                end_date=disclosure_end,
                symbols=spec.symbols,
                report_types=spec.report_types,
                resume=spec.resume,
            )
        )
        if stages["announcement"]["status"] == "failed":
            return self._result(spec, stages, price_end, disclosure_end)

        logger.info("research backfill: syncing Point-in-Time financial indicators")
        stages["financial_metric"] = self.financial.run(
            FinancialSyncSpec(
                start_date=spec.start_date,
                end_date=spec.end_date,
                symbols=spec.symbols,
                resume=spec.resume,
            )
        )
        return self._result(spec, stages, price_end, disclosure_end)

    def _result(
        self,
        spec: ResearchBackfillSpec,
        stages: dict[str, dict[str, Any]],
        price_end: Any,
        disclosure_end: Any,
    ) -> dict[str, Any]:
        return {
            "status": "failed"
            if any(item["status"] == "failed" for item in stages.values())
            else "published",
            "provider": self.provider.name,
            "symbols": list(spec.symbols),
            "benchmark_symbol": spec.benchmark_symbol,
            "report_types": list(spec.report_types),
            "start_date": spec.start_date.isoformat(),
            "end_date": spec.end_date.isoformat(),
            "price_end_date": price_end.isoformat(),
            "disclosure_end_date": disclosure_end.isoformat(),
            "stages": stages,
            "database_status": self.database.status(),
        }
