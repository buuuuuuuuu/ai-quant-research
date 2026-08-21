# AI 量化研究助手 V1

这是《AI 量化研究助手 V1 产品需求文档》的可运行项目骨架。当前版本实现第一条黄金路径：

> 研究 A 股公司历次半年报公告后 5/10/20 个交易日，相对沪深 300 的超额收益、胜率与分布。

V1 是研究与决策支持工具，不连接券商、不自动下单，也不提供收益承诺。

## 已实现

- `MarketDataProvider` 数据源抽象；内置离线样例与 AkShare 实现。
- 巨潮资讯半年报公告、A 股后复权行情和沪深 300 指数行情的标准化；指数主接口异常时自动切换 AKShare 备用接口。
- 严格使用公告日之后的第一个共同交易日收盘价作为入场价，避免公告时间不明确导致的前视偏差。
- 5/10/20 日事件收益、基准收益、净收益、超额收益、胜率及 95% 正态近似置信区间。
- 数据快照、SHA-256 摘要、运行清单、CSV、Excel 和 HTML 报告。
- CLI、FastAPI 接口、离线端到端测试和 Docker 入口。

## 项目结构

```text
ai-quant-research/
├── apps/
│   ├── api/                 # API 进程入口说明
│   ├── web/                 # Vue 3 前端预留
│   └── worker/              # RQ/Celery Worker 预留
├── src/ai_quant_assistant/
│   ├── ai/                  # 白名单研究工具注册表
│   ├── api/                 # FastAPI schemas/routes
│   ├── domain/              # 研究规格与结果模型
│   ├── providers/           # Fixture/AkShare/缓存与标准化
│   ├── reports/             # HTML/Excel 报告
│   ├── research/            # 事件研究引擎与服务
│   └── storage/             # Parquet 快照与 DuckDB 目录
├── docs/                    # 黄金路径口径与架构决策
├── scripts/                 # 可直接运行的演示脚本
└── tests/                   # 单元与端到端测试
```

## A 股本地数据库（M0 + M1）

数据平台提供 DuckDB Schema v2、可重复执行的迁移、Bronze/Silver/Gold/Snapshots
目录、离线 Fixture，以及 AkShare 沪深京证券主数据、交易日历和日线增量同步。日线按
证券、年份和复权口径分区，失败分区可以断点续跑，阻断级质量检查失败的数据不会发布。

初始化空数据库：

```bash
uv run ai-quant data init
```

初始化并写入离线样例：

```bash
uv run ai-quant data init --with-fixture
uv run ai-quant data status
```

建议先同步单只股票验证网络与数据口径：

```bash
uv run ai-quant data sync \
  --start-date 2024-01-01 \
  --symbols 600519 \
  --adjustments none qfq hfq
```

省略 `--symbols` 时同步当前沪深京 A 股列表；全市场任务耗时较长，可以先使用
`--limit 10` 冒烟验证。完全相同的请求会复用批次，只重试失败或中断的分区；需要强制
重新抓取时同时使用 `--no-resume --refresh`。

同步完成后，事件研究可以优先读取本地已发布数据，覆盖不足时自动回退 AkShare：

```bash
uv run ai-quant event-study \
  --source local \
  --symbol 600519 \
  --start-date 2024-01-01 \
  --adjustment qfq
```

事件研究的 `diagnostics.data_sources` 和 HTML 报告会显示股票、基准的服务来源、底层
Provider 来源、本地命中、本地覆盖回退、AkShare 备用接口，以及本地数据对应的同步批次。
本地股票快照还保留 `provider_source`、`source_batch_id` 和 `raw_path`，可从研究结果追溯
到同步批次与 Raw Parquet。

如果当前 `catalog.duckdb` 曾使用 `--with-fixture` 写入测试数据，正式同步前建议先归档：

```bash
mv data/catalog.duckdb data/catalog.fixture.duckdb
uv run ai-quant data init
```

默认创建 `data/raw`、`data/silver`、`data/gold`、`data/snapshots` 和
`data/catalog.duckdb`。可使用 `--data-dir`、`--duckdb-path` 或环境变量
`AI_QUANT_DATA_DIR`、`AI_QUANT_DUCKDB_PATH` 调整位置。

数据平台架构、同步语义和字段说明见：

- `docs/data-platform-architecture.md`
- `docs/data-dictionary.md`

## Ubuntu 快速启动

推荐在 Windows VS Code 中通过 Remote-SSH 连接 Ubuntu，然后在服务器执行：

```bash
uv venv
uv sync --extra dev
uv run pytest
```

先运行不依赖公网的离线黄金路径：

```bash
uv run ai-quant event-study \
  --source fixture \
  --symbol 600519 \
  --start-date 2012-01-01 \
  --end-date 2025-12-31 \
  --windows 5 10 20 \
  --cost-bps 20
```

结果保存在 `artifacts/runs/<run_id>/`。其中 `report.html` 是研究报告，`run.json` 是完整运行血缘。

确认离线结果后，再访问真实 AkShare 数据：

```bash
uv run ai-quant event-study \
  --source akshare \
  --symbol 600519 \
  --start-date 2012-01-01 \
  --end-date 2026-08-15
```

AkShare 当前官方接口口径：

- 公告：`stock_zh_a_disclosure_report_cninfo(..., category="半年报")`
- 股票行情：`stock_zh_a_hist(..., adjust="hfq")`
- 指数行情：主接口 `stock_zh_index_daily_em(symbol="csi000300")`，失败时回退 `stock_zh_index_daily(symbol="sh000300")`

仓库内置一份完全离线生成的示例产物，位于 `examples/golden-path-output/runs/`。

## 启动 API

```bash
uv run uvicorn ai_quant_assistant.api.app:create_app --factory --host 0.0.0.0 --port 8000
```

打开：

- API 文档：`http://<ubuntu-ip>:8000/docs`
- 健康检查：`http://<ubuntu-ip>:8000/health`

离线请求示例：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/research/interim-report-event-study \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "fixture",
    "symbol": "600519",
    "start_date": "2012-01-01",
    "end_date": "2025-12-31",
    "windows": [5, 10, 20],
    "round_trip_cost_bps": 20
  }'
```

## 测试与代码检查

```bash
uv run pytest
uv run ruff check .
```

## 当前边界

- API 当前同步执行；RQ/Celery 异步化放在 M3/M5。
- PostgreSQL 预留给项目、任务和审计元数据；黄金路径先以文件清单和 DuckDB 目录落地。
- Vue 3 前端尚未实现；当前通过 CLI、HTML 报告和 Swagger UI 验证闭环。
- 免费数据源只用于研究原型；商业化前必须完成数据授权与许可审查。
