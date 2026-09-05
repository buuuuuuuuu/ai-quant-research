from __future__ import annotations

import argparse
import hashlib
import json
import logging
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from ai_quant_assistant.config import Settings
from ai_quant_assistant.domain.models import (
    REPORT_TYPES,
    AnnouncementSyncSpec,
    BatchEventStudySpec,
    BenchmarkSyncSpec,
    DataSyncSpec,
    EventStudySpec,
    FactorBacktestSpec,
    FactorComponent,
    FactorLabSpec,
    FinancialSyncSpec,
    ResearchBackfillSpec,
)
from ai_quant_assistant.providers.akshare_provider import AkShareProvider
from ai_quant_assistant.providers.fixture_provider import FixtureProvider
from ai_quant_assistant.providers.local_provider import LocalFirstProvider
from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS
from ai_quant_assistant.research.batch_service import BatchInterimReportResearchService
from ai_quant_assistant.research.factor_backtest import FactorBacktestService
from ai_quant_assistant.research.factor_lab import FactorLabService
from ai_quant_assistant.research.screener import (
    SCREEN_FIELDS,
    PointInTimeScreener,
    ScreenCondition,
    ScreenSpec,
)
from ai_quant_assistant.research.service import InterimReportResearchService
from ai_quant_assistant.storage.backfill_service import ResearchBackfillService
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.financial_sync_service import FinancialDataSyncService
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.managed_coverage import market_today
from ai_quant_assistant.storage.managed_stocks import ManagedStockService
from ai_quant_assistant.storage.monitoring import (
    OperationAlertRepository,
    OperationAuditRepository,
    PlatformMonitor,
)
from ai_quant_assistant.storage.operations import (
    DatabaseBackupService,
    ExclusiveOperationLock,
    IncrementalSyncService,
    OperationsDashboard,
)
from ai_quant_assistant.storage.research_data_plans import ResearchDataPlans
from ai_quant_assistant.storage.research_sync_service import ResearchDatasetSyncService
from ai_quant_assistant.storage.security_master_refresh import SecurityMasterRefreshService
from ai_quant_assistant.storage.stock_pools import StockPoolService
from ai_quant_assistant.storage.sync_service import MarketDataSyncService
from ai_quant_assistant.storage.task_repository import OperationTaskRepository
from ai_quant_assistant.storage.universe import UniverseRepository


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
    batch_selection = batch.add_mutually_exclusive_group()
    batch_selection.add_argument("--symbols", nargs="+")
    batch_selection.add_argument("--stock-pool-id")
    batch.add_argument("--stock-pool-revision", type=int)
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
    backtest_selection = backtest.add_mutually_exclusive_group()
    backtest_selection.add_argument("--symbols", nargs="+")
    backtest_selection.add_argument("--stock-pool-id")
    backtest.add_argument("--stock-pool-revision", type=int)
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
    backtest.add_argument(
        "--universe-mode",
        choices=["explicit", "all_available", "historical_index", "observed_index"],
        default="explicit",
    )
    backtest.add_argument("--universe-benchmark", default="csi000300")
    backtest.add_argument("--classification-system", default="provider")
    backtest.add_argument(
        "--industry-neutralization", choices=["none", "within_industry_rank"], default="none"
    )
    backtest.add_argument("--max-industry-weight", type=float, default=1.0)
    backtest.add_argument("--require-verified-industry", action="store_true")

    lab = subcommands.add_parser("factor-lab", help="运行多因子研究、IC 分析与样本外参数扫描")
    lab.add_argument("--source", choices=["local"], default="local")
    lab_selection = lab.add_mutually_exclusive_group()
    lab_selection.add_argument("--symbols", nargs="+")
    lab_selection.add_argument("--stock-pool-id")
    lab.add_argument("--stock-pool-revision", type=int)
    lab.add_argument("--start-date", type=_date, required=True)
    lab.add_argument("--end-date", type=_date, default=date.today())
    lab.add_argument(
        "--factors", nargs="+", required=True, help="因子格式：roe:1:desc debt_asset_ratio:1:asc"
    )
    lab.add_argument("--top-n-values", nargs="+", type=int, default=[3])
    lab.add_argument(
        "--rebalance-values", nargs="+", choices=["monthly", "quarterly"], default=["quarterly"]
    )
    lab.add_argument("--commission-bps-values", nargs="+", type=float, default=[5.0])
    lab.add_argument("--quantiles", type=int, default=5)
    lab.add_argument("--benchmark", default="csi000300")
    lab.add_argument("--adjustment", choices=["none", "qfq", "hfq"], default="qfq")
    lab.add_argument("--initial-capital", type=float, default=1_000_000.0)
    lab.add_argument("--slippage-bps", type=float, default=5.0)
    lab.add_argument("--stamp-duty-bps", type=float, default=5.0)
    lab.add_argument("--lot-size", type=int, default=100)
    lab.add_argument("--missing-policy", choices=["exclude", "available"], default="exclude")
    lab.add_argument("--training-fraction", type=float, default=0.6)
    lab.add_argument("--validation-fraction", type=float, default=0.2)
    lab.add_argument("--walk-forward-folds", type=int, default=3)
    lab.add_argument("--max-combinations", type=int, default=48)
    lab.add_argument("--artifact-dir", type=Path)
    lab.add_argument(
        "--universe-mode",
        choices=["explicit", "all_available", "historical_index", "observed_index"],
        default="explicit",
    )
    lab.add_argument("--universe-benchmark", default="csi000300")
    lab.add_argument("--classification-system", default="provider")
    lab.add_argument(
        "--industry-neutralization", choices=["none", "within_industry_rank"], default="none"
    )
    lab.add_argument("--max-industry-weight", type=float, default=1.0)
    lab.add_argument("--require-verified-industry", action="store_true")

    screen = subcommands.add_parser("screen", help="运行 Point-in-Time 条件选股并保存候选池")
    screen.add_argument("--name", default="CLI 条件选股")
    screen.add_argument("--as-of", type=_date, required=True)
    screen.add_argument(
        "--conditions",
        nargs="+",
        required=True,
        help="条件格式：roe:gte:12 debt_asset_ratio:lt:60；区间使用 eps:between:1:3",
    )
    screen_selection = screen.add_mutually_exclusive_group()
    screen_selection.add_argument("--symbols", nargs="+")
    screen_selection.add_argument("--stock-pool-id")
    screen.add_argument("--stock-pool-revision", type=int)
    screen.add_argument(
        "--universe-mode",
        choices=["explicit", "all_available", "historical_index", "observed_index"],
        default="explicit",
    )
    screen.add_argument("--benchmark", default="csi000300")
    screen.add_argument("--classification-system", default="provider")
    screen.add_argument("--include-industries", nargs="*", default=[])
    screen.add_argument("--exclude-industries", nargs="*", default=[])
    screen.add_argument("--require-verified-industry", action="store_true")
    screen.add_argument("--missing-policy", choices=["exclude", "show"], default="show")
    screen.add_argument("--sort-by", choices=list(SCREEN_FIELDS))
    screen.add_argument("--sort-direction", choices=["asc", "desc"], default="desc")
    screen.add_argument("--top-n", type=int)
    screen.add_argument("--save-watchlist")
    screen.add_argument("--data-dir", type=Path)
    screen.add_argument("--duckdb-path", type=Path)

    universe = subcommands.add_parser("universe", help="构建 Point-in-Time 动态股票池")
    universe_commands = universe.add_subparsers(dest="universe_command", required=True)
    universe_build = universe_commands.add_parser("build", help="查看指定历史时点的股票池")
    universe_build.add_argument("--as-of", type=_date, required=True)
    universe_build.add_argument(
        "--mode",
        choices=["explicit", "all_available", "historical_index", "observed_index"],
        default="all_available",
    )
    universe_build.add_argument("--symbols", nargs="+", default=[])
    universe_build.add_argument("--benchmark", default="csi000300")
    universe_build.add_argument("--classification-system", default="provider")
    universe_build.add_argument("--limit", type=int)
    universe_build.add_argument("--require-verified-industry", action="store_true")
    universe_status = universe_commands.add_parser("status", help="检查行业和指数成分数据真实性")
    universe_status.add_argument("--as-of", type=_date, default=date.today())

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

    industry_sync = data_commands.add_parser(
        "sync-industries", help="同步当前行业观察快照，不冒充历史分类"
    )
    industry_sync.add_argument("--symbols", nargs="+", required=True)
    industry_sync.add_argument("--as-of", type=_date, default=date.today())
    industry_sync.add_argument("--classification-system", default="provider")
    industry_sync.add_argument("--refresh", action="store_true")
    industry_sync.add_argument("--rate-limit-seconds", type=float)
    industry_sync.add_argument("--data-dir", type=Path)
    industry_sync.add_argument("--duckdb-path", type=Path)

    constituent_sync = data_commands.add_parser(
        "sync-constituents", help="同步当前指数成分观察快照，不伪造历史成分"
    )
    constituent_sync.add_argument("--benchmark", default="csi000300")
    constituent_sync.add_argument("--as-of", type=_date, default=date.today())
    constituent_sync.add_argument("--refresh", action="store_true")
    constituent_sync.add_argument("--rate-limit-seconds", type=float)
    constituent_sync.add_argument("--data-dir", type=Path)
    constituent_sync.add_argument("--duckdb-path", type=Path)

    universe_import = data_commands.add_parser(
        "import-universe-history", help="导入有公开时间的可信历史行业或指数成分 CSV"
    )
    universe_import.add_argument("--dataset", choices=["industries", "constituents"], required=True)
    universe_import.add_argument("--file", type=Path, required=True)
    universe_import.add_argument("--as-of", type=_date, default=date.today())
    universe_import.add_argument("--benchmark", default="csi000300")
    universe_import.add_argument("--classification-system", default="provider")
    universe_import.add_argument("--source-name", required=True)
    universe_import.add_argument("--data-dir", type=Path)
    universe_import.add_argument("--duckdb-path", type=Path)

    backfill = data_commands.add_parser(
        "backfill-research", help="批量回填股票、指数、公告和 Point-in-Time 财务指标"
    )
    backfill.add_argument("--start-date", type=_date, default=date(2012, 1, 1))
    backfill.add_argument("--end-date", type=_date, default=date.today())
    backfill_selection = backfill.add_mutually_exclusive_group()
    backfill_selection.add_argument("--symbols", nargs="+")
    backfill_selection.add_argument("--stock-pool-id")
    backfill.add_argument("--stock-pool-revision", type=int)
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

    security_refresh = data_commands.add_parser(
        "sync-security-master", help="仅更新证券主数据；不下载历史行情、不改股票池"
    )
    security_refresh.add_argument(
        "--confirm", action="store_true", required=True,
        help="明确确认访问公网更新证券目录",
    )
    security_refresh.add_argument("--data-dir", type=Path)
    security_refresh.add_argument("--duckdb-path", type=Path)

    stock_pool = subcommands.add_parser("stock-pool", help="管理可版本化自定义股票池")
    stock_pool_commands = stock_pool.add_subparsers(dest="stock_pool_command", required=True)
    stock_pool_list = stock_pool_commands.add_parser("list", help="列出股票池")
    stock_pool_list.add_argument("--include-archived", action="store_true")
    stock_pool_show = stock_pool_commands.add_parser("show", help="显示股票池详情")
    stock_pool_show.add_argument("pool_id")
    stock_pool_show.add_argument("--revision", type=int)
    stock_pool_create = stock_pool_commands.add_parser("create", help="创建手工股票池")
    stock_pool_create.add_argument("--name", required=True)
    stock_pool_create.add_argument("--description", default="")
    stock_pool_create.add_argument("--symbols", nargs="*", default=[])
    stock_pool_update = stock_pool_commands.add_parser("update", help="修改股票池元数据")
    stock_pool_update.add_argument("pool_id")
    stock_pool_update.add_argument("--expected-revision", type=int, required=True)
    stock_pool_update.add_argument("--name")
    stock_pool_update.add_argument("--description")
    stock_pool_add = stock_pool_commands.add_parser("add", help="添加成员")
    stock_pool_add.add_argument("pool_id")
    stock_pool_add.add_argument("--expected-revision", type=int, required=True)
    stock_pool_add.add_argument("--symbols", nargs="+", required=True)
    stock_pool_remove = stock_pool_commands.add_parser("remove", help="移除成员")
    stock_pool_remove.add_argument("pool_id")
    stock_pool_remove.add_argument("--expected-revision", type=int, required=True)
    stock_pool_remove.add_argument("--security-ids", nargs="+", required=True)
    stock_pool_archive = stock_pool_commands.add_parser("archive", help="归档股票池")
    stock_pool_archive.add_argument("pool_id")
    stock_pool_archive.add_argument("--expected-revision", type=int, required=True)
    stock_pool_default = stock_pool_commands.add_parser("set-default", help="设置默认股票池")
    stock_pool_default.add_argument("pool_id")
    stock_pool_default.add_argument("--expected-revision", type=int, required=True)
    stock_pool_duplicate = stock_pool_commands.add_parser("duplicate", help="复制股票池")
    stock_pool_duplicate.add_argument("pool_id")
    stock_pool_duplicate.add_argument("--expected-revision", type=int, required=True)
    stock_pool_duplicate.add_argument("--name", required=True)
    stock_pool_revisions = stock_pool_commands.add_parser("revisions", help="查看历史版本")
    stock_pool_revisions.add_argument("pool_id")

    operations = subcommands.add_parser("ops", help="运行同步、备份与平台运维任务")
    operation_commands = operations.add_subparsers(dest="ops_command", required=True)
    operation_sync = operation_commands.add_parser("sync", help="增量更新行情、指数、公告与财务")
    operation_sync.add_argument("--as-of", type=_date, default=date.today())
    operation_sync.add_argument("--symbols", nargs="+")
    operation_backup = operation_commands.add_parser("backup", help="创建一致性 DuckDB 备份")
    operation_backup.add_argument("--backup-dir", type=Path)
    operation_backup.add_argument(
        "--apply-retention", action="store_true", help="备份成功后按保留策略清理旧备份"
    )
    operation_backup_list = operation_commands.add_parser("backup-list", help="查看数据库备份")
    operation_backup_list.add_argument("--include-deleted", action="store_true")
    operation_backup_list.add_argument("--limit", type=int, default=50)
    operation_backup_verify = operation_commands.add_parser(
        "backup-verify", help="验证备份 SHA-256、大小和数据库结构"
    )
    operation_backup_verify.add_argument("--backup-id", required=True)
    operation_backup_prune = operation_commands.add_parser(
        "backup-prune", help="预演或执行数据库备份保留策略"
    )
    operation_backup_prune.add_argument(
        "--apply", action="store_true", help="明确允许删除符合保留策略的过期备份"
    )
    operation_backup_relocate = operation_commands.add_parser(
        "backup-relocate", help="生产迁移后重新绑定已复制的备份文件路径"
    )
    operation_backup_relocate.add_argument("--old-data-dir", type=Path, required=True)
    operation_restore = operation_commands.add_parser(
        "restore", help="验证备份并在自动创建恢复前备份后安全恢复"
    )
    operation_restore.add_argument("--backup-id", required=True)
    operation_restore.add_argument(
        "--confirm-backup-id", required=True, help="必须再次输入完全相同的备份 ID"
    )
    operation_status = operation_commands.add_parser("status", help="查看同步、任务和备份状态")
    operation_tasks = operation_commands.add_parser("tasks", help="查看持久化后台任务")
    operation_tasks.add_argument("--limit", type=int, default=20)
    operation_alerts = operation_commands.add_parser("alerts", help="查看运行告警")
    operation_alerts.add_argument("--active-only", action="store_true")
    operation_alerts.add_argument("--limit", type=int, default=50)
    operation_audit = operation_commands.add_parser("audit", help="查看运维安全审计记录")
    operation_audit.add_argument("--limit", type=int, default=50)
    operation_check = operation_commands.add_parser("check", help="检查同步、备份、磁盘和登录异常")
    operation_check.add_argument("--fail-on-alert", action="store_true")
    operation_quality = operation_commands.add_parser("quality", help="查看数据质量与股票覆盖情况")
    for command in (
        operation_sync,
        operation_backup,
        operation_backup_list,
        operation_backup_verify,
        operation_backup_prune,
        operation_backup_relocate,
        operation_restore,
        operation_status,
        operation_tasks,
        operation_alerts,
        operation_audit,
        operation_check,
        operation_quality,
        stock_pool_list,
        stock_pool_show,
        stock_pool_create,
        stock_pool_update,
        stock_pool_add,
        stock_pool_remove,
        stock_pool_archive,
        stock_pool_default,
        stock_pool_duplicate,
        stock_pool_revisions,
    ):
        command.add_argument("--data-dir", type=Path)
        command.add_argument("--duckdb-path", type=Path)
    managed = subcommands.add_parser("data-stock", help="管理本地股票数据与历史回填")
    managed_commands = managed.add_subparsers(dest="managed_command", required=True)
    for name in ("list", "show", "add", "sync", "archive", "restore", "configure", "references"):
        command = managed_commands.add_parser(name)
        command.add_argument("--data-dir", type=Path)
        command.add_argument("--duckdb-path", type=Path)
        if name == "list":
            command.add_argument("--query", default="")
            command.add_argument("--include-archived", action="store_true")
            command.add_argument("--page", type=int, default=1)
        elif name in {"add", "sync"}:
            command.add_argument("symbol")
            command.add_argument("--start-date", type=_date, default=date(2012, 1, 1))
            command.add_argument("--end-date", type=_date, default=market_today())
            command.add_argument("--expected-revision", type=int, required=name == "sync")
            command.add_argument("--confirm", action="store_true", required=True)
            command.add_argument("--enqueue-only", action="store_true")
            command.add_argument("--refresh", action="store_true")
            command.add_argument("--no-auto-update", action="store_true")
            command.add_argument("--include-announcements", action="store_true",
                                 help="确认本次同时下载四类定期公告元数据，不下载 PDF")
        else:
            command.add_argument("security_id")
            if name not in {"show", "references"}:
                command.add_argument("--expected-revision", type=int, required=True)
                command.add_argument("--confirm", action="store_true", required=True)
            if name == "configure":
                command.add_argument("--auto-update", choices=["on", "off"], required=True)
    readiness = subcommands.add_parser("research-data", help="只读研究预检与确认式数据补齐")
    readiness_commands = readiness.add_subparsers(dest="readiness_command", required=True)
    for name in ("preflight", "plan", "list", "show", "confirm", "retry", "recheck", "work"):
        command = readiness_commands.add_parser(name)
        command.add_argument("--data-dir", type=Path)
        command.add_argument("--duckdb-path", type=Path)
        if name in {"preflight", "plan"}:
            command.add_argument("--research-type", required=True, choices=["screen", "factor_backtest", "factor_lab"])
            command.add_argument("--parameters", required=True, help="与研究 API 相同的 JSON 参数；只读解析")
        elif name != "list":
            command.add_argument("plan_id")
        if name in {"confirm", "retry"}:
            command.add_argument("--version", type=int, required=True)
            command.add_argument("--input-sha256", required=True)
            command.add_argument("--confirm", action="store_true", required=True)
    return parser


def _local_database(args: argparse.Namespace, settings: Settings) -> LocalDatabase:
    data_dir = args.data_dir or settings.data_dir
    duckdb_path = args.duckdb_path or (
        settings.duckdb_path if args.data_dir is None else data_dir / "catalog.duckdb"
    )
    return LocalDatabase(path=duckdb_path, layout=DataLayout.from_root(data_dir))


def _resolve_cli_stock_selection(
    args: argparse.Namespace, database: LocalDatabase
) -> tuple[tuple[str, ...], dict[str, object]]:
    if args.stock_pool_revision is not None and args.stock_pool_id is None:
        raise ValueError("--stock-pool-revision requires --stock-pool-id")
    if args.symbols is not None:
        symbols = tuple(dict.fromkeys(item.strip() for item in args.symbols))
        canonical = json.dumps(list(symbols), ensure_ascii=False, separators=(",", ":"))
        return symbols, {
            "pool_id": None,
            "pool_revision": None,
            "resolved_symbols": list(symbols),
            "symbols_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "resolved_at": datetime.now(UTC).isoformat(),
            "source_type": "explicit",
            "as_of": None,
            "history_verified": False,
            "warning": "显式股票列表可能存在幸存者偏差。",
        }
    context = StockPoolService(database).resolve(
        args.stock_pool_id, revision=args.stock_pool_revision
    )
    return tuple(context["resolved_symbols"]), context


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "research-data":
        service = ResearchDataPlans(_local_database(args, settings), settings)
        command = args.readiness_command
        if command in {"preflight", "plan"}:
            if len(args.parameters) > 65536:
                raise ValueError("research parameters exceed 64 KiB")
            parameters = json.loads(args.parameters)
            payload = service.readiness.run(args.research_type, parameters) if command == "preflight" else service.create(args.research_type, parameters, actor="cli")
        elif command in {"confirm", "retry"}:
            payload = service.confirm(args.plan_id, version=args.version, input_sha256=args.input_sha256,
                                      confirm=args.confirm, actor="cli", retry=command == "retry")
        elif command == "work":
            plan = service.get(args.plan_id)
            if not plan["last_task_id"]:
                raise ValueError("plan has not been confirmed")
            payload = service.run(plan["last_task_id"])
        elif command == "list":
            payload = service.list()
        else:
            payload = service.recheck(args.plan_id) if command == "recheck" else service.get(args.plan_id)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        if isinstance(payload, dict) and payload.get("status") == "failed":
            raise SystemExit(1)
        return
    if args.command == "data-stock":
        managed = ManagedStockService(_local_database(args, settings), settings)
        command = args.managed_command
        if command == "list":
            payload = managed.list(query=args.query, include_archived=args.include_archived, page=args.page)
        elif command in {"show", "references"}:
            payload = getattr(managed, "get" if command == "show" else "references")(args.security_id)
        elif command in {"add", "sync"}:
            payload, created = managed.request(
                args.symbol, start=args.start_date, end=args.end_date,
                expected_revision=args.expected_revision, confirm=args.confirm,
                refresh=args.refresh, auto_update=not args.no_auto_update, actor="cli",
                include_announcements=args.include_announcements,
            )
            if payload["status"] == "queued" and not args.enqueue_only:
                payload = managed.run(payload["task_id"])
        else:
            payload = managed.change(
                args.security_id, expected_revision=args.expected_revision, action=command,
                confirm=args.confirm, actor="cli",
                auto_update=args.auto_update == "on" if command == "configure" else None,
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        if isinstance(payload, dict) and payload.get("status") == "failed":
            raise SystemExit(1)
        return

    if args.command == "stock-pool":
        service = StockPoolService(_local_database(args, settings))
        command = args.stock_pool_command
        if command == "list":
            payload = {
                "stock_pools": service.list(include_archived=args.include_archived)
            }
        elif command == "show":
            payload = service.get(args.pool_id, revision=args.revision)
        elif command == "create":
            payload = service.create(
                args.name, description=args.description, symbols=args.symbols, actor="cli"
            )
        elif command == "update":
            payload = service.update(
                args.pool_id,
                expected_revision=args.expected_revision,
                name=args.name,
                description=args.description,
                actor="cli",
            )
        elif command == "add":
            payload = service.add_members(
                args.pool_id,
                args.symbols,
                expected_revision=args.expected_revision,
                actor="cli",
            )
        elif command == "remove":
            payload = service.remove_members(
                args.pool_id,
                args.security_ids,
                expected_revision=args.expected_revision,
                actor="cli",
            )
        elif command == "archive":
            payload = service.archive(
                args.pool_id, expected_revision=args.expected_revision, actor="cli"
            )
        elif command == "set-default":
            payload = service.set_default(
                args.pool_id, expected_revision=args.expected_revision, actor="cli"
            )
        elif command == "duplicate":
            payload = service.duplicate(
                args.pool_id,
                expected_revision=args.expected_revision,
                name=args.name,
                actor="cli",
            )
        else:
            payload = {"revisions": service.revisions(args.pool_id)}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return

    if args.command == "data":
        database = _local_database(args, settings)
        if args.data_command == "sync-security-master":
            service = SecurityMasterRefreshService.from_settings(database, settings)
            task, created = service.prepare(actor="cli")
            payload = service.run(task["task_id"]) if created else task
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            if payload["status"] == "failed":
                raise SystemExit(1)
            return
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
        elif args.data_command == "import-universe-history":
            import pandas as pd

            frame = pd.read_csv(args.file, dtype={"symbol": str})
            repository = UniverseRepository(database)
            if args.dataset == "industries":
                payload = repository.store_industries(
                    frame,
                    classification_system=args.classification_system,
                    history_mode="verified_history",
                    as_of=args.as_of,
                    source=args.source_name,
                )
            else:
                payload = repository.store_constituents(
                    frame,
                    benchmark_symbol=args.benchmark,
                    history_mode="verified_history",
                    as_of=args.as_of,
                    source=args.source_name,
                )
        else:
            if (
                args.data_command in {"sync-industries", "sync-constituents"}
                and args.as_of != date.today()
            ):
                raise ValueError(
                    "live industry and constituent snapshots can only use today's observation date; "
                    "import independently verified historical data instead"
                )
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
            if args.data_command == "sync-industries":
                repository = UniverseRepository(database)
                frame = provider.get_industry_snapshot(tuple(args.symbols))
                diagnostics = frame.attrs.get("diagnostics", {})
                sources = diagnostics.get("provider_sources", [])
                source = (
                    sources[0]
                    if len(sources) == 1
                    else "akshare:mixed_industry_sources"
                    if len(sources) > 1
                    else "akshare:stock_individual_info_em"
                )
                payload = repository.store_industries(
                    frame,
                    classification_system=args.classification_system,
                    history_mode="observed_snapshot",
                    as_of=args.as_of,
                    source=source,
                )
                payload["diagnostics"] = diagnostics
            elif args.data_command == "sync-constituents":
                repository = UniverseRepository(database)
                frame = provider.get_index_constituent_snapshot(args.benchmark)
                payload = repository.store_constituents(
                    frame,
                    benchmark_symbol=args.benchmark,
                    history_mode="observed_snapshot",
                    as_of=args.as_of,
                    source="akshare:index_stock_cons_csindex",
                )
            elif args.data_command == "sync":
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
                symbols, stock_pool_context = _resolve_cli_stock_selection(args, database)
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
                        symbols=symbols,
                        benchmark_symbol=args.benchmark,
                        adjustments=tuple(args.adjustments),
                        report_types=tuple(args.types),
                        windows=tuple(args.windows),
                        resume=not args.no_resume,
                        stock_pool=stock_pool_context,
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

    if args.command == "screen":
        conditions = []
        for definition in args.conditions:
            parts = definition.split(":")
            if len(parts) not in {3, 4}:
                raise SystemExit(
                    f"invalid screen condition: {definition}; expected field:operator:value[:upper]"
                )
            conditions.append(
                ScreenCondition(
                    field=parts[0],
                    operator=parts[1],
                    value=float(parts[2]),
                    upper_value=float(parts[3]) if len(parts) == 4 else None,
                )
            )
        database = _local_database(args, settings)
        symbols, stock_pool_context = _resolve_cli_stock_selection(args, database)
        service = PointInTimeScreener(database)
        payload = service.run(
            ScreenSpec(
                name=args.name,
                as_of=args.as_of,
                conditions=tuple(conditions),
                symbols=symbols,
                universe_mode=args.universe_mode,
                benchmark_symbol=args.benchmark,
                classification_system=args.classification_system,
                include_industries=tuple(args.include_industries),
                exclude_industries=tuple(args.exclude_industries),
                require_verified_industry=args.require_verified_industry,
                missing_policy=args.missing_policy,
                sort_by=args.sort_by,
                sort_direction=args.sort_direction,
                top_n=args.top_n,
                stock_pool=stock_pool_context,
            )
        )
        if args.save_watchlist:
            payload["saved_watchlist"] = service.save_watchlist(
                payload["run_id"], args.save_watchlist
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return

    if args.command == "universe":
        database = LocalDatabase(
            path=settings.duckdb_path, layout=DataLayout.from_root(settings.data_dir)
        )
        repository = UniverseRepository(database)
        if args.universe_command == "status":
            payload = repository.overview(args.as_of)
        else:
            payload = repository.build(
                as_of=args.as_of,
                mode=args.mode,
                symbols=tuple(args.symbols),
                benchmark_symbol=args.benchmark,
                limit=args.limit,
                classification_system=args.classification_system,
                require_history=args.require_verified_industry,
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return

    if args.command == "ops":
        if args.ops_command == "sync" and args.symbols:
            settings = replace(settings, sync_symbols=tuple(args.symbols))
        if args.ops_command == "backup" and args.backup_dir:
            settings = replace(settings, backup_dir=args.backup_dir)
        database = _local_database(args, settings)
        tasks = OperationTaskRepository(database)
        audit = OperationAuditRepository(database)
        alerts = OperationAlertRepository(database)
        monitor = PlatformMonitor(database, settings, alerts, audit)
        operation_lock = ExclusiveOperationLock(database.layout.root / ".operations.lock")
        backup_service = DatabaseBackupService(
            database=database,
            backup_dir=settings.resolved_backup_dir,
            tasks=tasks,
            operation_lock=operation_lock,
            audit=audit,
            retention_days=settings.backup_retention_days,
            minimum_count=settings.backup_minimum_count,
        )
        if args.ops_command == "status":
            payload = OperationsDashboard(
                database, tasks, backup_service, alerts, audit, monitor
            ).status()
        elif args.ops_command == "tasks":
            payload = {"tasks": tasks.list(limit=args.limit)}
        elif args.ops_command == "backup":
            payload = backup_service.run(apply_retention=args.apply_retention)
        elif args.ops_command == "backup-list":
            payload = {
                "backups": backup_service.list(
                    limit=args.limit, include_deleted=args.include_deleted
                )
            }
        elif args.ops_command == "backup-verify":
            payload = backup_service.verify(args.backup_id)
        elif args.ops_command == "backup-prune":
            payload = backup_service.prune(dry_run=not args.apply)
        elif args.ops_command == "backup-relocate":
            payload = backup_service.relocate(args.old_data_dir)
        elif args.ops_command == "restore":
            payload = backup_service.restore(
                args.backup_id, confirm_backup_id=args.confirm_backup_id
            )
        elif args.ops_command == "alerts":
            payload = {"alerts": alerts.list(active_only=args.active_only, limit=args.limit)}
        elif args.ops_command == "audit":
            payload = {"events": audit.list(limit=args.limit)}
        elif args.ops_command == "check":
            payload = monitor.check()
        elif args.ops_command == "quality":
            payload = monitor.quality()
        else:
            provider = AkShareProvider(
                cache_dir=settings.cache_dir,
                retries=settings.akshare_retries,
                timeout_seconds=settings.akshare_timeout_seconds,
            )
            service = IncrementalSyncService(
                database=database,
                settings=settings,
                provider=provider,
                tasks=tasks,
                operation_lock=operation_lock,
                audit=audit,
            )
            payload = service.run(as_of=args.as_of, symbols=tuple(args.symbols) if args.symbols else None)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        if args.ops_command == "check" and args.fail_on_alert and payload["active_count"]:
            raise SystemExit(1)
        return

    if args.command == "factor-lab":
        components = []
        for definition in args.factors:
            parts = definition.split(":")
            if len(parts) > 3 or not parts[0]:
                raise SystemExit(
                    f"invalid factor definition: {definition}; expected code[:weight[:direction]]"
                )
            components.append(
                FactorComponent(
                    code=parts[0],
                    weight=float(parts[1]) if len(parts) > 1 else 1.0,
                    direction=parts[2] if len(parts) > 2 else "desc",
                )
            )
        database = LocalDatabase(
            path=settings.duckdb_path, layout=DataLayout.from_root(settings.data_dir)
        )
        symbols, stock_pool_context = _resolve_cli_stock_selection(args, database)
        service = FactorLabService(
            database=database, artifact_dir=args.artifact_dir or settings.artifact_dir
        )
        spec = FactorLabSpec(
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            factors=tuple(components),
            top_n_values=tuple(args.top_n_values),
            rebalance_values=tuple(args.rebalance_values),
            commission_bps_values=tuple(args.commission_bps_values),
            quantiles=args.quantiles,
            benchmark_symbol=args.benchmark,
            adjustment=args.adjustment,
            initial_capital=args.initial_capital,
            slippage_bps=args.slippage_bps,
            stamp_duty_bps=args.stamp_duty_bps,
            lot_size=args.lot_size,
            missing_policy=args.missing_policy,
            training_fraction=args.training_fraction,
            validation_fraction=args.validation_fraction,
            walk_forward_folds=args.walk_forward_folds,
            max_combinations=args.max_combinations,
            universe_mode=args.universe_mode,
            universe_benchmark=args.universe_benchmark,
            classification_system=args.classification_system,
            industry_neutralization=args.industry_neutralization,
            max_industry_weight=args.max_industry_weight,
            require_verified_industry=args.require_verified_industry,
            stock_pool=stock_pool_context,
        )
        print(json.dumps(service.run(spec), ensure_ascii=False, indent=2, default=str))
        return

    if args.command == "backtest":
        database = LocalDatabase(
            path=settings.duckdb_path,
            layout=DataLayout.from_root(settings.data_dir),
        )
        symbols, stock_pool_context = _resolve_cli_stock_selection(args, database)
        service = FactorBacktestService(
            database=database,
            artifact_dir=args.artifact_dir or settings.artifact_dir,
        )
        payload = service.run(
            FactorBacktestSpec(
                symbols=symbols,
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
                universe_mode=args.universe_mode,
                universe_benchmark=args.universe_benchmark,
                classification_system=args.classification_system,
                industry_neutralization=args.industry_neutralization,
                max_industry_weight=args.max_industry_weight,
                require_verified_industry=args.require_verified_industry,
                stock_pool=stock_pool_context,
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
    database = (
        LocalDatabase(path=settings.duckdb_path, layout=DataLayout.from_root(settings.data_dir))
        if args.source == "local" and settings.duckdb_path.is_file()
        else None
    )
    if args.command == "batch-event-study":
        batch_service = BatchInterimReportResearchService(
            provider=provider,
            artifact_dir=artifact_dir,
            database=database,
        )
        selection_database = database or LocalDatabase(
            path=settings.duckdb_path,
            layout=DataLayout.from_root(settings.data_dir),
        )
        symbols, stock_pool_context = _resolve_cli_stock_selection(
            args, selection_database
        )
        payload = batch_service.run(
            BatchEventStudySpec(
                symbols=symbols,
                start_date=args.start_date,
                end_date=args.end_date,
                benchmark_symbol=args.benchmark,
                windows=tuple(args.windows),
                adjustment=args.adjustment,
                round_trip_cost_bps=args.cost_bps,
                min_events=args.min_events,
                stock_pool=stock_pool_context,
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
        database=database,
    )
    output = service.run(spec)
    print(json.dumps(output.api_payload(), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
