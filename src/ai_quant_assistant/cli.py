from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

from ai_quant_assistant.config import Settings
from ai_quant_assistant.domain.models import (
    DEFAULT_RESEARCH_SYMBOLS,
    REPORT_TYPES,
    AnnouncementSyncSpec,
    BatchEventStudySpec,
    BenchmarkSyncSpec,
    DataSyncSpec,
    EventStudySpec,
    FactorBacktestSpec,
    FinancialSyncSpec,
    ResearchBackfillSpec,
)
from ai_quant_assistant.providers.akshare_provider import AkShareProvider
from ai_quant_assistant.providers.fixture_provider import FixtureProvider
from ai_quant_assistant.providers.local_provider import LocalFirstProvider
from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS
from ai_quant_assistant.research.batch_service import BatchInterimReportResearchService
from ai_quant_assistant.research.factor_backtest import FactorBacktestService
from ai_quant_assistant.research.service import InterimReportResearchService
from ai_quant_assistant.storage.backfill_service import ResearchBackfillService
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.financial_sync_service import FinancialDataSyncService
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.research_sync_service import ResearchDatasetSyncService
from ai_quant_assistant.storage.sync_service import MarketDataSyncService


def _date(value: str) -> date:
    return date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-quant", description="AI 量化研究助手 V1")
    subcommands = parser.add_subparsers(dest="command", required=True)
    study = subcommands.add_parser("event-study", help="运行半年报公告后超额收益研究")
    study.add_argument("--source", choices=["fixture", "akshare", "local"], default="fixture")
    study.add_argument("--symbol", default="600519")
    study.add_argument("--start-date", type=_date, default=date(2012, 1, 1))
    study.add_argument("--end-date", type=_date, default=date.today())
    study.add_argument("--benchmark", default="csi000300")
    study.add_argument("--windows", nargs="+", type=int, default=[5, 10, 20])
    study.add_argument("--adjustment", choices=["hfq", "qfq", ""], default="hfq")
    study.add_argument("--cost-bps", type=float, default=20.0)
    study.add_argument("--artifact-dir", type=Path)
    study.add_argument("--refresh", action="store_true")

    batch = subcommands.add_parser("batch-event-study", help="批量运行半年报公告事件研究")
    batch.add_argument("--source", choices=["fixture", "akshare", "local"], default="local")
    batch.add_argument("--symbols", nargs="+", default=list(DEFAULT_RESEARCH_SYMBOLS))
    batch.add_argument("--start-date", type=_date, default=date(2012, 1, 1))
    batch.add_argument("--end-date", type=_date, default=date.today())
    batch.add_argument("--benchmark", default="csi000300")
    batch.add_argument("--windows", nargs="+", type=int, default=[5, 10, 20])
    batch.add_argument("--adjustment", choices=["hfq", "qfq", ""], default="qfq")
    batch.add_argument("--cost-bps", type=float, default=20.0)
    batch.add_argument("--min-events", type=int, default=1)
    batch.add_argument("--artifact-dir", type=Path)
    batch.add_argument("--refresh", action="store_true")

    backtest = subcommands.add_parser("backtest", help="运行 Point-in-Time 财务因子组合回测")
    backtest.add_argument("--source", choices=["local"], default="local")
    backtest.add_argument("--symbols", nargs="+", default=list(DEFAULT_RESEARCH_SYMBOLS))
    backtest.add_argument("--start-date", type=_date, required=True)
    backtest.add_argument("--end-date", type=_date, default=date.today())
    backtest.add_argument("--factor", choices=list(FINANCIAL_METRICS), default="roe")
    backtest.add_argument("--factor-direction", choices=["asc", "desc"], default="desc")
    backtest.add_argument("--top-n", type=int, default=3)
    backtest.add_argument("--rebalance", choices=["monthly", "quarterly"], default="quarterly")
    backtest.add_argument("--benchmark", default="csi000300")
    backtest.add_argument("--adjustment", choices=["none", "qfq", "hfq"], default="qfq")
    backtest.add_argument("--initial-capital", type=float, default=1_000_000.0)
    backtest.add_argument("--commission-bps", type=float, default=5.0)
    backtest.add_argument("--slippage-bps", type=float, default=5.0)
    backtest.add_argument("--stamp-duty-bps", type=float, default=5.0)
    backtest.add_argument("--lot-size", type=int, default=100)
    backtest.add_argument("--artifact-dir", type=Path)

    data = subcommands.add_parser("data", help="初始化和检查 A 股本地数据库")
    data_commands = data.add_subparsers(dest="data_command", required=True)
    data_init = data_commands.add_parser("init", help="创建数据目录并执行数据库迁移")
    data_init.add_argument("--data-dir", type=Path)
    data_init.add_argument("--duckdb-path", type=Path)
    data_init.add_argument(
        "--with-fixture",
        action="store_true",
        help="写入可离线验证的沪深京样例主数据、交易日历和日线",
    )
    data_status = data_commands.add_parser("status", help="显示数据库版本和核心表行数")
    data_status.add_argument("--data-dir", type=Path)
    data_status.add_argument("--duckdb-path", type=Path)
    data_sync = data_commands.add_parser("sync", help="从 AkShare 增量同步并发布日线")
    data_sync.add_argument("--start-date", type=_date, required=True)
    data_sync.add_argument("--end-date", type=_date, default=date.today())
    data_sync.add_argument("--symbols", nargs="+", default=[])
    data_sync.add_argument(
        "--adjustments",
        nargs="+",
        choices=["none", "qfq", "hfq"],
        default=["none", "qfq", "hfq"],
    )
    data_sync.add_argument("--limit", type=int, help="仅同步证券列表前 N 个，用于冒烟验证")
    data_sync.add_argument("--rate-limit-seconds", type=float)
    data_sync.add_argument("--no-resume", action="store_true")
    data_sync.add_argument("--refresh", action="store_true")
    data_sync.add_argument("--data-dir", type=Path)
    data_sync.add_argument("--duckdb-path", type=Path)

    data_index = data_commands.add_parser("sync-index", help="同步并发布沪深指数历史日线")
    data_index.add_argument("--start-date", type=_date, required=True)
    data_index.add_argument("--end-date", type=_date, default=date.today())
    data_index.add_argument("--symbols", nargs="+", default=["csi000300"])
    data_index.add_argument("--rate-limit-seconds", type=float)
    data_index.add_argument("--no-resume", action="store_true")
    data_index.add_argument("--refresh", action="store_true")
    data_index.add_argument("--data-dir", type=Path)
    data_index.add_argument("--duckdb-path", type=Path)

    data_announcements = data_commands.add_parser(
        "sync-announcements", help="同步季报、半年报、年报公告及实际公开时间"
    )
    data_announcements.add_argument("--start-date", type=_date, required=True)
    data_announcements.add_argument("--end-date", type=_date, default=date.today())
    data_announcements.add_argument("--symbols", nargs="+", required=True)
    data_announcements.add_argument(
        "--types", nargs="+", choices=list(REPORT_TYPES), default=["interim"]
    )
    data_announcements.add_argument("--rate-limit-seconds", type=float)
    data_announcements.add_argument("--no-resume", action="store_true")
    data_announcements.add_argument("--refresh", action="store_true")
    data_announcements.add_argument("--data-dir", type=Path)
    data_announcements.add_argument("--duckdb-path", type=Path)

    financial = data_commands.add_parser("sync-financial", help="同步 Point-in-Time 财务指标")
    financial.add_argument("--start-date", type=_date, required=True)
    financial.add_argument("--end-date", type=_date, default=date.today())
    financial.add_argument("--symbols", nargs="+", required=True)
    financial.add_argument("--rate-limit-seconds", type=float)
    financial.add_argument("--no-resume", action="store_true")
    financial.add_argument("--refresh", action="store_true")
    financial.add_argument("--data-dir", type=Path)
    financial.add_argument("--duckdb-path", type=Path)

    financial_status = data_commands.add_parser(
        "financial-status", help="查看指定时点可见的已发布财务指标"
    )
    financial_status.add_argument("--symbol", required=True)
    financial_status.add_argument("--as-of", type=_date, default=date.today())
    financial_status.add_argument("--start-date", type=_date)
    financial_status.add_argument("--data-dir", type=Path)
    financial_status.add_argument("--duckdb-path", type=Path)

    backfill = data_commands.add_parser(
        "backfill-research", help="批量回填股票、指数、公告和 Point-in-Time 财务指标"
    )
    backfill.add_argument("--start-date", type=_date, default=date(2012, 1, 1))
    backfill.add_argument("--end-date", type=_date, default=date.today())
    backfill.add_argument("--symbols", nargs="+", default=list(DEFAULT_RESEARCH_SYMBOLS))
    backfill.add_argument("--benchmark", default="csi000300")
    backfill.add_argument(
        "--adjustments", nargs="+", choices=["none", "qfq", "hfq"], default=["qfq"]
    )
    backfill.add_argument(
        "--types", nargs="+", choices=list(REPORT_TYPES), default=list(REPORT_TYPES)
    )
    backfill.add_argument("--windows", nargs="+", type=int, default=[5, 10, 20])
    backfill.add_argument("--rate-limit-seconds", type=float)
    backfill.add_argument("--no-resume", action="store_true")
    backfill.add_argument("--refresh", action="store_true")
    backfill.add_argument("--data-dir", type=Path)
    backfill.add_argument("--duckdb-path", type=Path)
    return parser


def _local_database(args: argparse.Namespace, settings: Settings) -> LocalDatabase:
    data_dir = args.data_dir or settings.data_dir
    duckdb_path = args.duckdb_path or (
        settings.duckdb_path if args.data_dir is None else data_dir / "catalog.duckdb"
    )
    return LocalDatabase(path=duckdb_path, layout=DataLayout.from_root(data_dir))


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "data":
        database = _local_database(args, settings)
        if args.data_command == "init":
            payload = database.initialize()
            if args.with_fixture:
                payload["fixture"] = database.load_fixture()
                payload["database_status"] = database.status()
        elif args.data_command == "status":
            payload = database.status()
        elif args.data_command == "financial-status":
            financial_frame = database.read_financial_metrics(
                args.symbol, args.as_of, start_date=args.start_date
            )
            payload = {
                "symbol": args.symbol,
                "as_of": args.as_of.isoformat(),
                "report_count": int(financial_frame["report_period_end"].nunique()),
                "metric_count": len(financial_frame),
                "latest": database.read_latest_financial_snapshot(args.symbol, args.as_of),
                "reports": financial_frame.to_dict(orient="records"),
            }
        else:
            provider = AkShareProvider(
                cache_dir=(
                    settings.cache_dir if args.data_dir is None else database.layout.root / "cache"
                ),
                retries=settings.akshare_retries,
                timeout_seconds=settings.akshare_timeout_seconds,
                refresh=args.refresh,
            )
            rate_limit = (
                args.rate_limit_seconds
                if args.rate_limit_seconds is not None
                else settings.data_sync_rate_limit_seconds
            )
            if args.data_command == "sync":
                service = MarketDataSyncService(
                    database=database,
                    layout=database.layout,
                    provider=provider,
                    rate_limit_seconds=rate_limit,
                )
                payload = service.run(
                    DataSyncSpec(
                        start_date=args.start_date,
                        end_date=args.end_date,
                        symbols=tuple(args.symbols),
                        adjustments=tuple(args.adjustments),
                        symbol_limit=args.limit,
                        resume=not args.no_resume,
                    )
                )
            elif args.data_command in {"sync-index", "sync-announcements"}:
                service = ResearchDatasetSyncService(
                    database=database,
                    layout=database.layout,
                    provider=provider,
                    rate_limit_seconds=rate_limit,
                )
                if args.data_command == "sync-index":
                    payload = service.sync_benchmarks(
                        BenchmarkSyncSpec(
                            start_date=args.start_date,
                            end_date=args.end_date,
                            symbols=tuple(args.symbols),
                            resume=not args.no_resume,
                        )
                    )
                else:
                    payload = service.sync_announcements(
                        AnnouncementSyncSpec(
                            start_date=args.start_date,
                            end_date=args.end_date,
                            symbols=tuple(args.symbols),
                            report_types=tuple(args.types),
                            resume=not args.no_resume,
                        )
                    )
            elif args.data_command == "sync-financial":
                service = FinancialDataSyncService(
                    database=database,
                    layout=database.layout,
                    provider=provider,
                    rate_limit_seconds=rate_limit,
                )
                payload = service.run(
                    FinancialSyncSpec(
                        start_date=args.start_date,
                        end_date=args.end_date,
                        symbols=tuple(args.symbols),
                        resume=not args.no_resume,
                    )
                )
            else:
                service = ResearchBackfillService(
                    database=database,
                    layout=database.layout,
                    provider=provider,
                    rate_limit_seconds=rate_limit,
                )
                payload = service.run(
                    ResearchBackfillSpec(
                        start_date=args.start_date,
                        end_date=args.end_date,
                        symbols=tuple(args.symbols),
                        benchmark_symbol=args.benchmark,
                        adjustments=tuple(args.adjustments),
                        report_types=tuple(args.types),
                        windows=tuple(args.windows),
                        resume=not args.no_resume,
                    )
                )
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        if (
            args.data_command
            in {
                "sync",
                "sync-index",
                "sync-announcements",
                "sync-financial",
                "backfill-research",
            }
            and payload["status"] == "failed"
        ):
            raise SystemExit(1)
        return

    if args.command == "backtest":
        database = LocalDatabase(
            path=settings.duckdb_path,
            layout=DataLayout.from_root(settings.data_dir),
        )
        service = FactorBacktestService(
            database=database,
            artifact_dir=args.artifact_dir or settings.artifact_dir,
        )
        payload = service.run(
            FactorBacktestSpec(
                symbols=tuple(args.symbols),
                start_date=args.start_date,
                end_date=args.end_date,
                factor=args.factor,
                factor_direction=args.factor_direction,
                top_n=args.top_n,
                rebalance=args.rebalance,
                benchmark_symbol=args.benchmark,
                adjustment=args.adjustment,
                initial_capital=args.initial_capital,
                commission_bps=args.commission_bps,
                slippage_bps=args.slippage_bps,
                stamp_duty_bps=args.stamp_duty_bps,
                lot_size=args.lot_size,
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return

    artifact_dir = args.artifact_dir or settings.artifact_dir
    if args.source == "fixture":
        provider = FixtureProvider()
    else:
        akshare_provider = AkShareProvider(
            cache_dir=settings.cache_dir,
            retries=settings.akshare_retries,
            timeout_seconds=settings.akshare_timeout_seconds,
            refresh=args.refresh,
        )
        provider = (
            LocalFirstProvider(
                database=LocalDatabase(
                    path=settings.duckdb_path,
                    layout=DataLayout.from_root(settings.data_dir),
                ),
                fallback=akshare_provider,
            )
            if args.source == "local"
            else akshare_provider
        )
    if args.command == "batch-event-study":
        database = (
            LocalDatabase(
                path=settings.duckdb_path,
                layout=DataLayout.from_root(settings.data_dir),
            )
            if args.source == "local" and settings.duckdb_path.is_file()
            else None
        )
        batch_service = BatchInterimReportResearchService(
            provider=provider,
            artifact_dir=artifact_dir,
            database=database,
        )
        payload = batch_service.run(
            BatchEventStudySpec(
                symbols=tuple(args.symbols),
                start_date=args.start_date,
                end_date=args.end_date,
                benchmark_symbol=args.benchmark,
                windows=tuple(args.windows),
                adjustment=args.adjustment,
                round_trip_cost_bps=args.cost_bps,
                min_events=args.min_events,
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        if payload["status"] == "failed":
            raise SystemExit(1)
        return

    spec = EventStudySpec(
        symbol=args.symbol,
        start_date=args.start_date,
        end_date=args.end_date,
        benchmark_symbol=args.benchmark,
        windows=tuple(args.windows),
        adjustment=args.adjustment,
        round_trip_cost_bps=args.cost_bps,
    )
    service = InterimReportResearchService(
        provider=provider,
        artifact_dir=artifact_dir,
        duckdb_path=settings.duckdb_path,
    )
    output = service.run(spec)
    print(json.dumps(output.api_payload(), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
