from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from ai_quant_assistant.config import Settings
from ai_quant_assistant.domain.models import EventStudySpec
from ai_quant_assistant.providers.akshare_provider import AkShareProvider
from ai_quant_assistant.providers.fixture_provider import FixtureProvider
from ai_quant_assistant.research.service import InterimReportResearchService
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout


def _date(value: str) -> date:
    return date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-quant", description="AI 量化研究助手 V1")
    subcommands = parser.add_subparsers(dest="command", required=True)
    study = subcommands.add_parser("event-study", help="运行半年报公告后超额收益研究")
    study.add_argument("--source", choices=["fixture", "akshare"], default="fixture")
    study.add_argument("--symbol", default="600519")
    study.add_argument("--start-date", type=_date, default=date(2012, 1, 1))
    study.add_argument("--end-date", type=_date, default=date.today())
    study.add_argument("--benchmark", default="csi000300")
    study.add_argument("--windows", nargs="+", type=int, default=[5, 10, 20])
    study.add_argument("--adjustment", choices=["hfq", "qfq", ""], default="hfq")
    study.add_argument("--cost-bps", type=float, default=20.0)
    study.add_argument("--artifact-dir", type=Path)
    study.add_argument("--refresh", action="store_true")

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
    if args.command == "data":
        database = _local_database(args, settings)
        if args.data_command == "init":
            payload = database.initialize()
            if args.with_fixture:
                payload["fixture"] = database.load_fixture()
                payload["database_status"] = database.status()
        else:
            payload = database.status()
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return

    artifact_dir = args.artifact_dir or settings.artifact_dir
    if args.source == "fixture":
        provider = FixtureProvider()
    else:
        provider = AkShareProvider(
            cache_dir=settings.cache_dir,
            retries=settings.akshare_retries,
            timeout_seconds=settings.akshare_timeout_seconds,
            refresh=args.refresh,
        )
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
