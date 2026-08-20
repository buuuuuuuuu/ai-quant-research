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

## A 股本地数据库（M0 + M1-A）

第一阶段已经提供 DuckDB Schema v1、可重复执行的迁移、Bronze/Silver/Gold/Snapshots
目录初始化、数据库状态查询，以及完全离线的沪深京 Fixture 数据。该阶段先冻结主键、
交易日、复权和发布状态口径；AkShare 全市场增量同步放在 M1-B。

初始化空数据库：

```bash
uv run ai-quant data init
```

初始化并写入离线样例：

```bash
uv run ai-quant data init --with-fixture
uv run ai-quant data status
```

默认创建 `data/raw`、`data/silver`、`data/gold`、`data/snapshots` 和
`data/catalog.duckdb`。可使用 `--data-dir`、`--duckdb-path` 或环境变量
`AI_QUANT_DATA_DIR`、`AI_QUANT_DUCKDB_PATH` 调整位置。

数据平台架构和字段说明见：

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
