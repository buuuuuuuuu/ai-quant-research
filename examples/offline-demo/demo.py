"""Isolated synthetic demo. No environment settings, API keys or market providers."""

from __future__ import annotations

import argparse
import json
import math
from datetime import date
from pathlib import Path

import pandas as pd

from ai_quant_assistant.config import Settings
from ai_quant_assistant.domain.models import DailySignalRule, DailySignalStrategySpec
from ai_quant_assistant.research.daily_signal import DailySignalStrategyService
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.managed_coverage import coverage_report


def build_demo(output: Path) -> tuple[Settings, dict]:
    """Create a new directory exclusively; never reuse an existing database."""
    output = output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    settings = Settings(
        data_dir=output / "data",
        duckdb_path=output / "data/catalog.duckdb",
        artifact_dir=output / "artifacts",
        cache_dir=output / "cache",
        copilot_planner="rules",
        document_answerer="rules",
    )
    database = LocalDatabase(
        path=settings.duckdb_path, layout=DataLayout.from_root(settings.data_dir)
    )
    database.initialize()
    days = pd.bdate_range("2024-01-02", "2024-04-30")
    # Weekdays are deliberately synthetic, not an exchange holiday calendar.
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO security_master
               (security_id,symbol,exchange,name,list_date,source,source_batch_id)
               VALUES ('SSE.600519','600519','SSE','合成演示股票（非真实行情）',
                       '2001-08-27','synthetic-demo-v1','synthetic-demo-v1')"""
        )
        connection.executemany(
            """INSERT INTO trading_calendar
               (exchange,trade_date,is_open,source,source_batch_id)
               VALUES ('SSE',?,TRUE,'synthetic-demo-v1','synthetic-demo-v1')""",
            [[day.date()] for day in days],
        )
        bars = []
        for i, day in enumerate(days):
            close = round(10 + i * 0.015 + math.sin(i / 4) * 0.9, 4)
            opening = round(close + math.cos(i / 3) * 0.03, 4)
            for adjustment in ("none", "qfq"):
                bars.append(
                    [
                        day.date(),
                        adjustment,
                        opening,
                        max(opening, close) + 0.1,
                        min(opening, close) - 0.1,
                        close,
                        1000.0,
                        close * 1000,
                    ]
                )
        connection.executemany(
            """INSERT INTO daily_bar
               (security_id,trade_date,adjustment,open,high,low,close,volume,amount,
                source_batch_id,quality_status)
               VALUES ('SSE.600519',?,?,?,?,?,?,?,?, 'synthetic-demo-v1','published')""",
            bars,
        )
        connection.execute(
            """INSERT INTO benchmark_master
               (benchmark_id,symbol,name,source,source_batch_id)
               VALUES ('INDEX.csi000300','csi000300','合成演示基准（非真实指数）',
                       'synthetic-demo-v1','synthetic-demo-v1')"""
        )
        connection.executemany(
            """INSERT INTO benchmark_daily_bar
               (benchmark_id,trade_date,close,source,source_batch_id)
               VALUES ('INDEX.csi000300',?,?,'synthetic-demo-v1','synthetic-demo-v1')""",
            [[day.date(), 1000 + i * 0.5] for i, day in enumerate(days)],
        )
        coverage = coverage_report(connection, "SSE.600519", days[0].date(), days[-1].date())
        if coverage["availability"] != "ready":
            raise RuntimeError("合成数据覆盖检查未通过，请勿手工放行")
        connection.execute(
            """INSERT INTO managed_stock
               (security_id,target_start,target_end,availability,coverage_json)
               VALUES ('SSE.600519',?,?,'ready',?)
               ON CONFLICT(security_id) DO UPDATE SET target_start=EXCLUDED.target_start,
               target_end=EXCLUDED.target_end,availability=EXCLUDED.availability,
               coverage_json=EXCLUDED.coverage_json""",
            [days[0].date(), days[-1].date(), json.dumps(coverage)],
        )
    spec = DailySignalStrategySpec(
        symbols=("600519",),
        start_date=date(2024, 2, 1),
        end_date=date(2024, 4, 30),
        candidate_factors=(),
        candidate_count=1,
        candidate_refresh="monthly",
        buy_rules=(DailySignalRule("ma_cross", "cross_above", short_window=5, long_window=20),),
        sell_rules=(DailySignalRule("ma_cross", "cross_below", short_window=5, long_window=20),),
        max_positions=1,
        initial_capital=100_000,
    )
    result = DailySignalStrategyService(database=database, artifact_dir=settings.artifact_dir).run(
        spec
    )
    manifest = {
        "data_kind": "synthetic_demo_not_market_data",
        "generator_version": "offline-demo-v1",
        "calendar": "weekdays only; not an exchange calendar",
        "adjustment": "none and qfq are deliberately equal synthetic series",
        "scope": "one demonstration symbol; no financial factors or personal data",
        "spec": spec.to_dict(),
        "run_id": result["run_id"],
        "detail_path": f"/backtests/detail?source=index&record={result['run_id']}",
        "report": str(result["artifacts"]["html_report"]),
    }
    (output / "DEMO.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return settings, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="生成独立合成示例，不调用外部数据源")
    parser.add_argument("--output", type=Path, required=True, help="必须是尚不存在的新目录")
    parser.add_argument("--serve", action="store_true", help="生成后在本机启动 Web")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口须在 1024–65535 之间")
    try:
        settings, manifest = build_demo(args.output)
    except FileExistsError:
        parser.error("目录已经存在，拒绝覆盖。请换用新的 --output 路径。")
    print("合成演示 / SYNTHETIC DATA — 非真实行情、非投资表现", flush=True)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), flush=True)
    if args.serve:
        import uvicorn

        from ai_quant_assistant.api.app import create_app

        print(f"详情：http://127.0.0.1:{args.port}{manifest['detail_path']}", flush=True)
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
