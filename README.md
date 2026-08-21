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

## A 股本地数据库（M0 + M1 + M2-A + M2-B）

数据平台提供 DuckDB Schema v4、可重复执行的迁移、Bronze/Silver/Gold/Snapshots
目录、离线 Fixture，以及 AkShare 沪深京证券主数据、交易日历、股票日线、指数基准和
一季报、半年报、三季报、年报公告及十二项财务指标的增量同步。分区支持失败续跑，阻断
级质量检查失败的数据不会发布；公告和财务版本同时保存报告期与真实公开时间，防止把
未来信息带入历史研究。

初始化空数据库：

```bash
uv run ai-quant data init
```

已有 Schema v1/v2/v3 数据库执行同一命令时会自动升级到 Schema v4，不删除已发布的
股票日线、指数或历史公告。

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

同步沪深 300 指数基准，以及指定公司的四类定期报告公告：

```bash
uv run ai-quant data sync-index \
  --start-date 2024-01-01 \
  --end-date 2025-03-16 \
  --symbols csi000300

uv run ai-quant data sync-announcements \
  --start-date 2024-01-01 \
  --end-date 2025-06-30 \
  --symbols 600519 \
  --types q1 interim q3 annual

uv run ai-quant data sync-financial \
  --start-date 2024-01-01 \
  --end-date 2024-12-31 \
  --symbols 600519

uv run ai-quant data financial-status \
  --symbol 600519 \
  --as-of 2024-08-15
```

财务指标优先使用 Provider 自带披露时间；缺失时按证券、报告期和报告类型匹配本地真实
公告。两者均不存在时会阻断发布，绝不会把报告期结束日伪装成公开时间。年报一般在次年
披露，因此公告同步区间需要覆盖研究期之后的实际年报发布日期。

一条命令批量回填股票日线、沪深 300、四类公告和 Point-in-Time 财务指标：

```bash
uv run ai-quant data backfill-research \
  --start-date 2012-01-01 \
  --end-date 2025-12-31 \
  --symbols 600519 000001 601318 000858 600036 000333 600276 601888 002594 300750 \
  --adjustments qfq
```

省略 `--symbols` 时使用上面十只显式研究样本；它们不代表历史沪深 300 成分股，以免误把
当前成分当作历史成分造成幸存者偏差。回填自动为研究窗口预留股票/指数后续交易日，并为
年报等延迟披露预留 180 天公告区间。相同请求复用各数据集批次，只重试失败分区。

研究会优先读取本地已发布的股票、基准和公告，任意数据集覆盖不足时仅对该数据集回退
AkShare：

```bash
uv run ai-quant event-study \
  --source local \
  --symbol 600519 \
  --start-date 2024-01-01 \
  --adjustment qfq
```

对多只股票运行同一研究并关联公告日当时已经公开的财务指标：

```bash
uv run ai-quant batch-event-study \
  --source local \
  --symbols 600519 000001 601318 \
  --start-date 2012-01-01 \
  --end-date 2025-12-31 \
  --windows 5 10 20 \
  --adjustment qfq
```

批量结果写入 `artifacts/batch-runs/<batch_id>/`，包括股票级汇总、全样本汇总、事件级
财务特征、失败股票列表、四工作表 Excel、HTML 报告和批次清单。单只股票失败不会中断
其他股票；返回状态为 `succeeded`、`partial` 或 `failed`。

事件研究的 `diagnostics.data_sources` 和 HTML 报告会分别显示股票、基准、公告的服务
来源、底层 Provider 来源、本地命中、本地覆盖回退、AkShare 备用接口，以及本地数据
对应的同步批次。三类研究快照均保留 `provider_source`、`source_batch_id` 和 `raw_path`，
可从研究结果追溯到同步批次与 Raw Parquet。

如果当前 `catalog.duckdb` 曾使用 `--with-fixture` 写入测试数据，正式同步前建议先归档：

```bash
mv data/catalog.duckdb data/catalog.fixture.duckdb
uv run ai-quant data init
```

默认创建项目根目录下的 `data/raw`、`data/silver`、`data/gold`、`data/snapshots` 和
`data/catalog.duckdb`；从项目子目录执行命令时，也会自动定位同一个项目根目录。
如果历史操作同时产生了 `data/catalog.duckdb` 与 `data/data/catalog.duckdb`，程序会
优先选择 Schema 更新、已发布数据更完整的数据库，因此无需每次手动导出环境变量。
可使用 `--data-dir`、`--duckdb-path` 或环境变量 `AI_QUANT_DATA_DIR`、
`AI_QUANT_DUCKDB_PATH` 显式指定位置；显式配置始终优先于自动识别。

升级后可以取消旧的临时环境变量并检查自动识别结果：

```bash
unset AI_QUANT_DATA_DIR AI_QUANT_DUCKDB_PATH
uv run ai-quant data status
```

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

- 公告：`stock_zh_a_disclosure_report_cninfo(..., category="一季报"/"半年报"/"三季报"/"年报")`
- 股票行情：`stock_zh_a_hist(..., adjust="hfq")`
- 指数行情：主接口 `stock_zh_index_daily_em(symbol="csi000300")`，失败时回退 `stock_zh_index_daily(symbol="sh000300")`
- 财务指标：主接口 `stock_financial_analysis_indicator_em(symbol="600519.SH", indicator="按报告期")`，失败时回退 `stock_financial_analysis_indicator(symbol="600519", start_year="2012")`

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

将请求中的 `"source": "fixture"` 改为 `"source": "local"`，即可使用与 CLI 一致的本地
优先股票、指数和公告数据源。

批量研究接口为 `POST /api/v1/research/batch-interim-report-event-study`，使用 `symbols`
数组代替单个 `symbol`，其余日期、窗口、基准和复权参数与单股票接口一致。

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
