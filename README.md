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
- 财务因子选股、Point-in-Time 组合回测、多因子实验、动态股票池与行业中性化。
- AI 自然语言研究对话、结构化计划预览、覆盖预检、显式确认和可追溯解释。
- 股票行情、指数基准、定期公告和财务指标的逐股票数据浏览、公告/财报详情、筛选、CSV 与记录血缘。
- Point-in-Time 条件选股、逐股票入选/淘汰证据、筛选快照和可复用候选股票池。
- 公告 PDF 本地校验归档、页码正文/OCR、全文检索、财报内容浏览、引用式问答与多版本比较。
- 单管理员登录、持久化后台任务、定时增量同步和 DuckDB 一致性备份。
- 非 root 服务、Nginx 反向代理、备份恢复、运行告警、运维审计与自动 CI。
- CLI、FastAPI 接口、离线端到端测试、Docker 与 systemd 部署入口。

## 项目结构

```text
ai-quant-research/
├── apps/
│   ├── api/                 # API 进程入口说明
│   ├── web/                 # Web 研究工作台使用说明
│   └── worker/              # RQ/Celery Worker 预留
├── src/ai_quant_assistant/
│   ├── ai/                  # 白名单研究工具注册表
│   ├── api/                 # FastAPI schemas/routes
│   ├── domain/              # 研究规格与结果模型
│   ├── documents/           # 公告 PDF 归档、解析、搜索与引用问答
│   ├── providers/           # Fixture/AkShare/缓存与标准化
│   ├── reports/             # HTML/Excel 报告
│   ├── research/            # 事件研究引擎与服务
│   └── storage/             # Parquet 快照与 DuckDB 目录
├── docs/                    # 黄金路径口径与架构决策
├── deploy/                  # systemd、Nginx、低权限安装和发布部署模板
├── scripts/                 # 可直接运行的演示脚本
└── tests/                   # 单元与端到端测试
```

## A 股本地数据库（M0–M9）

数据平台提供 DuckDB Schema v14、可重复执行的迁移、Bronze/Silver/Gold/Snapshots
目录、离线 Fixture，以及 AkShare 沪深京证券主数据、交易日历、股票日线、指数基准和
一季报、半年报、三季报、年报公告及十二项财务指标的增量同步。分区支持失败续跑，阻断
级质量检查失败的数据不会发布；公告和财务版本同时保存报告期与真实公开时间，防止把
未来信息带入历史研究。

初始化空数据库：

```bash
uv run ai-quant data init
```

已有 Schema v1–v13 数据库执行同一命令时会自动升级到 Schema v14，不删除已发布的
股票日线、指数、历史公告、财务报告、后台任务、备份或审计记录。

Schema v10 引入的公告文档能力从 Web“数据浏览 → 定期公告 → 查看公告 → 文档智能”进入。
首次点击“归档并解析”会启动后台任务；详见 [文档智能说明](docs/document-intelligence.md)。
Schema v11 增加跨进程 DuckDB 访问栅栏、文档归档原子版本分配、文档任务唯一 claim、
worker 租约/心跳和解析 fencing；重复请求会复用活动任务，过期或失败任务可以安全重试。
Schema v12 在已解析正文上提供业绩预告/快报、分红派息、股份回购、重大合同和股东
增减持五类离线规则提取。每个结构化字段绑定页码、正文块、逐字 quote 和 SHA-256，需
审核为 `accepted` 后才能复用现有事件研究引擎；研究按公告真实 `published_at` 做
Point-in-Time 过滤，并保存事件、公告、文档和原文证据。

Schema v13 将运行时固定十只股票替换为可自定义、可归档、带不可变 revision 的股票池。
研究提交时冻结 pool/revision、实际代码与 SHA-256；旧 `saved_watchlist` 保留兼容读取并迁移为
`screen_snapshot` 股票池。使用方式和升级诊断见 [docs/stock-pools.md](docs/stock-pools.md)。

Schema v14 增加本地股票数据管理：在“数据浏览”搜索证券并确认“添加并同步”，后台自动
回填日线（默认 2012-01-01 至提交日，按可靠上市日期裁剪，维护 none/qfq）。不下载公告、
财务或 PDF；股票池新成员只能来自 active 且行情已就绪的受管理股票。移出只归档管理记录，
保留行情、旧池成员与历史 revision；活动池当前引用会阻止移出。详见
[本地股票数据管理](docs/local-stock-data-management.md)。
沪市上市日期查询支持上交所单股备用证据；失败显示具体阶段，不再统一显示“99%”。
上市边界未知仍不能就绪，不把未知区间伪装成确定缺口；本修复沿用 Schema v14。

证券目录不等于已有行情。如果搜索提示 `missing from security_master`，先在
Web“股票池管理”点击“更新证券主数据”并确认，再前往数据浏览添加并回填。也可显式运行：

```bash
uv run ai-quant data sync-security-master --confirm
```

此操作仅补充证券代码、名称和来源，不下载历史行情、不自动添加成员、不扩大定时同步范围。
`ops sync` 和定时同步的日线范围改为 active 且启用自动更新的受管理股票；公告/财务仍
沿用既有显式配置或默认股票池范围，不因纳入管理而扩大。目录刷新不创建管理记录。

证券目录发布使用单条集合式 upsert，避免全目录逐条写入耗尽 DuckDB 内存；在 128 MB
DuckDB 内存额度下覆盖 6,000 条证券的新增、重复更新和失败回滚测试。保留原事务、
任务 fencing 和已知上市状态，无须为本次修复提高服务器内存上限。

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

省略 `--symbols` 和 `--stock-pool-id` 时使用数据库默认股票池；它不代表历史沪深 300 成分股，以免误把
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

## 财务因子选股与组合回测（M3-A）

已有本地股票行情、沪深 300 和 Point-in-Time 财务指标后，可以直接运行组合回测：

```bash
uv run ai-quant backtest \
  --source local \
  --symbols 600519 000001 601318 000858 600036 000333 600276 601888 002594 300750 \
  --start-date 2015-01-01 \
  --end-date 2025-12-31 \
  --factor roe \
  --top-n 3 \
  --rebalance quarterly \
  --benchmark csi000300 \
  --adjustment qfq
```

因子可选 `roe`、`revenue_yoy`、`parent_net_profit_yoy`、`gross_margin`、`net_margin`、
`debt_asset_ratio` 等已发布财务指标。默认按因子从高到低排序；负债率等低值优先指标可
增加 `--factor-direction asc`。支持 `--rebalance monthly`、自定义初始资金，以及
`--commission-bps`、`--slippage-bps`、`--stamp-duty-bps` 和 `--lot-size`。

每次调仓只允许读取信号日 15:00 之前真正公开的财报，下一基准交易日开盘执行，使用
等权组合、100 股整数买入、T+1、停牌和一字涨跌停约束。研究输出累计/年化收益、最大
回撤、夏普比率、相对沪深 300 超额收益，以及净值、订单、逐日持仓、财务因子选择的
CSV、五工作表 Excel、HTML 净值报告和运行清单，统一写入
`artifacts/backtests/<run_id>/`。

数据库默认池和手工池仍然只代表当前研究样本。导入带有真实生效与发布时间的指数历史成分后，可以
使用 `--universe-mode historical_index` 在每个信号日动态过滤真实历史成员；未提供可信
历史时该模式直接拒绝执行，不会把当前指数快照冒充过去的成分。

## 多因子实验室与样本外验证（M5-A）

对多个已发布财务因子执行方向调整、横截面百分位标准化和权重合成，同时计算
IC、Rank IC、分组收益、覆盖率、年度稳定性及参数扫描：

```bash
uv run ai-quant factor-lab \
  --source local \
  --symbols 600519 000001 601318 000858 600036 000333 600276 601888 002594 300750 \
  --start-date 2015-01-01 \
  --end-date 2025-12-31 \
  --factors roe:2:desc revenue_yoy:1:desc debt_asset_ratio:1:asc \
  --top-n-values 3 5 \
  --rebalance-values monthly quarterly \
  --commission-bps-values 5 10 \
  --quantiles 5
```

每个因子的格式为 `指标代码:权重:方向`，其中负债率等低值优先指标使用 `asc`。
单因子和多因子组合会在同一个参数网格下分别回测。所有因子截面只使用信号日
15:00 之前实际公开的财报；下一交易日开盘执行，并保留整手、T+1、涨跌停、
停牌和佣金/滑点/印花税约束。

默认按交易日顺序分为 60% 训练集、20% 验证集和 20% 独立测试集；最优参数只按
验证集夏普比率和超额收益排序，测试集不参与选参，并附加滚动样本外验证。
使用 `--training-fraction`、`--validation-fraction` 和 `--walk-forward-folds` 调整验证。
为避免意外提交大型扫描任务，默认最多允许 48 组参数，可通过 `--max-combinations`
明确调整。缺失因子默认剔除，也可通过 `--missing-policy available` 按可用权重归一。

研究结果保存到 `artifacts/factor-lab/<run_id>/`，包括因子截面、IC 时间序列、
分组收益、因子覆盖、年度稳定性、策略扫描、训练/验证/测试分段、滚动验证、
策略净值 CSV，以及多工作表 Excel、HTML 报告和运行清单。Web 工作台支持可视化
选择因子权重、方向、扫描参数、查看 Rank IC 图、策略对比和历史报告下载。

因子实验也支持 `--universe-mode historical_index`、
`--industry-neutralization within_industry_rank`、`--max-industry-weight 0.4` 和
`--require-verified-industry`；这些选项依赖真实 Point-in-Time 历史记录，不会用当前快照
伪造历史分类或指数成分。

## 动态股票池与行业中性化（M5-B）

先将现有目录升级到最新 Schema，再查看当前已发布行情能够支持的时点股票池：

```bash
uv run ai-quant data init
uv run ai-quant universe status
uv run ai-quant universe build --as-of 2025-12-31 --mode all_available
```

同步今日行业分类和沪深 300 成分，只会产生 `observed_snapshot` 当前观察记录：

```bash
uv run ai-quant data sync-industries --symbols 600519 000001 300750
uv run ai-quant data sync-constituents --benchmark csi000300
```

要对过去进行可信历史指数或行业中性研究，必须导入能独立证明 `effective_from` 与
`published_at` 的历史文件：

```bash
uv run ai-quant data import-universe-history \
  --dataset industries --file industry-history.csv --source-name official:industry-history

uv run ai-quant data import-universe-history \
  --dataset constituents --benchmark csi000300 \
  --file index-history.csv --source-name official:index-history

uv run ai-quant universe build \
  --as-of 2024-06-30 --mode historical_index --benchmark csi000300

uv run ai-quant factor-lab \
  --source local --symbols 600519 000001 300750 \
  --start-date 2020-01-01 --end-date 2025-12-31 \
  --factors roe:2:desc revenue_yoy:1:desc \
  --top-n-values 2 --rebalance-values quarterly \
  --commission-bps-values 5 --universe-mode historical_index \
  --industry-neutralization within_industry_rank \
  --max-industry-weight 0.5 --require-verified-industry
```

## AI 研究 Copilot（M6-A）

启动 Web 工作台后进入“AI 研究”，用自然语言描述股票、日期、因子、调仓与股票池约束。
系统先生成持久化的结构化计划并展示数据覆盖；只有覆盖通过、计划版本未变化且管理员点击
“确认此计划”后，服务端才允许后台调用白名单研究工具。确认和执行是两个独立动作，模型
不能直接运行 SQL、Python、Shell、联网抓取或修改数据库。

默认使用完全离线、可复现的规则规划器：

```bash
AI_QUANT_COPILOT_PLANNER=rules uv run uvicorn \
  ai_quant_assistant.api.app:create_app --factory --host 0.0.0.0 --port 8080
```

如需使用 OpenAI Responses API 解释更自由的自然语言，仅从环境变量读取密钥：

```bash
export AI_QUANT_COPILOT_PLANNER=openai
export OPENAI_API_KEY='...'
export AI_QUANT_OPENAI_MODEL=gpt-5-mini
uv run uvicorn ai_quant_assistant.api.app:create_app --factory --host 0.0.0.0 --port 8080
```

密钥不会写入 DuckDB、对话、计划、任务、日志或研究产物。AI 输出使用严格 JSON Schema，
之后仍会经过本地参数验证、工具白名单、覆盖预检和执行前二次检查。详见
[`docs/ai-research-copilot.md`](docs/ai-research-copilot.md)。

## Point-in-Time 条件选股器（M7-A）

Web 工作台“条件选股”支持在指定自然日结束时点，按已发布财务指标、最近价格、成交量、
成交额、20 日平均成交额和行业条件筛选候选股票。财报只能在真实公开后使用，缺失值不会
被填成零；每只股票都会显示通过、淘汰或缺失原因及对应报告期、公开时间和数据批次。

CLI 示例：

```bash
uv run ai-quant screen \
  --name "高 ROE 低负债" --as-of 2025-04-30 \
  --symbols 600519 000001 601318 000858 600036 000333 600276 601888 002594 300750 \
  --conditions roe:gte:12 revenue_yoy:gte:10 debt_asset_ratio:lt:60 \
  --sort-by roe --sort-direction desc --top-n 5 \
  --save-watchlist "2025-04-30 高质量候选"
```

筛选执行前可通过 `POST /api/v1/screens/preflight` 查看字段、行业和股票池覆盖；运行参数、
逐股票评价和数据证据保存在 Schema v9 的 `screen_run`、`screen_result` 中。最终入选代码
可保存为 `saved_watchlist`，或从页面一键送入 AI 研究、回测和因子实验。完整口径见
[`docs/point-in-time-screener.md`](docs/point-in-time-screener.md)。

历史文件字段、发布时间约束、快照可信边界、Web 接口及验收步骤见
[`docs/universe-neutralization.md`](docs/universe-neutralization.md)。

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
- `docs/factor-backtest.md`
- `docs/web-workbench.md`
- `docs/platform-operations.md`
- `docs/release-hardening.md`

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

## 启动 Web 研究工作台与 API

```bash
uv run uvicorn ai_quant_assistant.api.app:create_app --factory --host 0.0.0.0 --port 8080
```

打开：

- Web 研究工作台：`http://<ubuntu-ip>:8080/`
- API 文档：`http://<ubuntu-ip>:8080/docs`
- 健康检查：`http://<ubuntu-ip>:8080/health`

工作台首页显示当前自动识别的 DuckDB 路径、Schema 版本、股票/行情/公告/财务记录数
和数据覆盖区间。浏览器可以配置股票池、日期、财务因子、Top-N、调仓频率、复权和
交易成本，提交后由后台线程执行，页面持续显示排队、运行进度、成功或失败原因。

回测完成后，页面直接展示策略/沪深 300 净值曲线、收益风险指标、交易、逐日持仓和
因子排名；历史研究在服务重启后仍从 `artifacts/backtests/` 恢复。完整 HTML 报告可在
浏览器打开，Excel、CSV 和 JSON 通过白名单下载接口获取，不允许读取其他服务器文件。

配置 `AI_QUANT_WEB_PASSWORD` 和 `AI_QUANT_WEB_SESSION_SECRET` 后，工作台、API、
研究报告与下载会要求登录；`/health` 保持公开以便健康检查。公网部署仍建议在反向代理
层配置 HTTPS，并在启用 HTTPS 后设置 `AI_QUANT_WEB_SESSION_SECURE=true`。

## 自动同步、任务持久化与备份

```bash
uv run ai-quant data init
uv run ai-quant ops status
uv run ai-quant ops sync
uv run ai-quant ops backup
uv run ai-quant ops tasks --limit 20
```

Schema v14 会持久化回测、Copilot、条件选股、版本化股票池、本地股票管理、公告文档与事件提取任务、
逐字段证据、事件研究输入、增量同步、数据库备份任务、运行告警、管理员审计，以及行业与
指数成分同步批次。同步按现有覆盖区间依次更新股票、
指数、公告与 Point-in-Time 财务数据；备份执行 DuckDB checkpoint、完整性验证和 SHA-256
校验。Web 首页“数据同步与备份”可直接查看状态并启动任务。

`deploy/systemd/` 提供低权限 Web 常驻服务、交易日收盘后增量同步、每日备份及每
15 分钟运行检查；`deploy/nginx/` 提供 HTTP 和 HTTPS 反向代理模板。完整升级、
备份恢复和发布操作见 `docs/platform-operations.md` 与 `docs/release-hardening.md`。

生产服务器从已通过 CI 的 `main` 安全更新时，使用带备份验证、提交锁定、Schema 校验、
健康检查和失败停机保护的脚本：

```bash
cd /root/code/ai-quant-research
bash deploy/scripts/update-production-from-git.sh
```

离线请求示例：

```bash
curl -X POST http://127.0.0.1:8080/api/v1/research/interim-report-event-study \
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

- 后台任务使用单机线程池与 DuckDB 持久化，不适用于多节点分布式执行。
- 登录是单管理员会话认证，不包含多租户、权限分级或外部身份提供商。
- 生产 systemd 单元使用 `/opt/ai-quant/app` 与 `/var/lib/ai-quant`，避免服务访问 `/root`。
- 免费数据源只用于研究原型；商业化前必须完成数据授权与许可审查。
