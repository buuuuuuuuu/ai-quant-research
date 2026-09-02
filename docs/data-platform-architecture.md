# A 股本地数据库架构（M0 + M1 + M2 + M3 + M4）

## M9-B 本地管理边界（Schema v14）

证券目录仅供身份检索；managed_stock 是明确维护的本地股票清单；stock_pool_member 只引用共享行情。
DataExplorer 新管理 API 原子提交管理记录、任务 claim 和审计，返回后由有界持久队列驱动原
MarketDataSyncService 年分区引擎；none/qfq 分别按股票和交易日历检查历史缺口，不再使用全表最新日期。
股票池所有新增/复制/筛选保存入口在同一个 DB 事务中检查管理状态，归档引用检查使用同一共享锁。
管理 revision/generation 与 operation_task owner/heartbeat 联合 fencing 每次仓储连接和完成事务。
网络不持有 DB access lock；单数据目录下载槽限制内存，不阻塞备份网络阶段。具体锁顺序、故障语义、
迁移及保守停牌诊断见 [M9-B 说明](local-stock-data-management.md)。既有研究引擎和 PIT 输入冻结不变。

## 目标与边界

本阶段把现有“每次研究临时抓取并保存快照”的能力扩展为可初始化、可迁移、可检查、
可增量同步和可断点续跑的本地数据底座。它覆盖 AkShare 沪深京证券主数据、交易日历、
股票日线、指数基准、四类定期公告、Point-in-Time 财务指标和显式股票池批量研究。
后续阶段已增加本地 Web 研究工作台、定时增量任务和数据库备份；仍不包含自动实盘交易。

现有事件研究继续通过 `MarketDataProvider` 运行，输出与计算口径保持不变。
`LocalFirstProvider` 只在 `data_coverage` 完整覆盖请求区间时读取本地 published 数据，
否则回退现有 AkShare Provider。

## 分层

| 层 | 目录/组件 | 约束 |
|---|---|---|
| Bronze | `data/raw/`、`daily_bar_raw` | 保存股票、指数、公告、财务分区原始 Parquet 与 SHA-256 |
| Silver | `data/silver/`、股票/指数/公告/财务表 | 标准代码、字段类型、主键、双时间轴和质量状态 |
| Gold | `data/gold/`、五个 `published_*` 视图 | 只暴露通过质量门禁的数据 |
| Snapshots | `data/snapshots/`、现有运行快照 | 绑定文件、行数、SHA-256 与代码版本 |
| Catalog | `data/catalog.duckdb` | Schema 版本、主数据、交易日历、批次和 DQ 结果 |

默认目录相对于项目根目录定位，不因命令从项目根目录还是子目录执行而变化。对于历史
遗留的 `data/catalog.duckdb` 与 `data/data/catalog.duckdb` 双库，启动时以只读方式比较
Schema 版本、已发布数据量和证券数量，自动选择更完整的数据库；评分相同时保留标准
`data/catalog.duckdb`。目录和 DuckDB 文件仍可以分别通过 `AI_QUANT_DATA_DIR` 与
`AI_QUANT_DUCKDB_PATH` 显式覆盖，且显式配置优先。运行产物写入项目根目录的
`artifacts/`，缓存写入选中数据目录的 `cache/`；清缓存不得删除正式数据与研究快照。

## Schema 迁移

`storage/schema.py` 按整数版本顺序执行迁移。每个版本在同一事务中完成，成功后写入
`schema_migrations`；失败时回滚，不留下半套表结构。`ai-quant data init` 可以重复执行，
已经应用的版本不会重复执行。

Schema v1 创建：

- `security_master`
- `trading_calendar`
- `daily_bar_raw`
- `daily_bar`
- `sync_batch`
- `dq_result`
- `published_daily_bar` 视图

Schema v2 增加：

- `sync_partition`：证券×年份×复权口径的断点状态；
- `adjustment_ratio`：复权收盘价与不复权收盘价的可审计比率；
- `data_coverage`：本地优先读取的显式覆盖区间。

Schema v3 增加：

- `benchmark_master`、`benchmark_daily_bar`、`published_benchmark_daily_bar`：指数
  标识与通过质量门禁的日线；
- `announcement`、`published_announcement`：半年报/年报公告、报告所属期间、实际
  公告时间与内容摘要；
- `research_sync_partition`：指数与公告按对象、年份和类型拆分的可恢复分区；
- 复用已有 `sync_batch`、`data_coverage` 和 `dq_result`，保证历史股票数据无需重建。

Schema v4 增加：

- 在一个事务内重建 `announcement`，保留历史公告并将报告类型扩展为
  `q1/interim/q3/annual`；
- `financial_report`、`published_financial_report`：证券、报告期、真实公开时间确定的
  可追溯财报版本；
- `financial_metric`、`published_financial_metric`：按版本发布的十二项标准化财务指标；
- `financial_sync_partition`：证券×自然年可恢复财务分区、报告数、指标数和 Raw 血缘；
- 继续复用 `sync_batch`、`data_coverage`、`dq_result`，不覆盖 v1/v2/v3 已发布数据。

Schema v5 增加：

- `operation_task`：回测、增量同步和备份的持久化任务、进度、参数与失败原因；
- `database_backup`：一致性备份文件位置、字节数、SHA-256 与创建时间；
- 任务和备份生命周期采用 `TIMESTAMPTZ`，现有 v1–v4 行情、公告和财务数据保持不变。

Schema v6 增加：

- `operation_audit`：管理员登录、回测、同步、备份、恢复和告警处理的安全审计；
- `operation_alert`：同步、备份、磁盘、登录及阻断级数据质量异常的持久化告警；
- `database_backup.deleted_at/deletion_reason`：明确执行备份保留策略后的生命周期记录；
- 继续保留 v1–v5 的历史行情、Point-in-Time 财务、后台任务和既有备份。

Schema v7 增加：

- `security_industry_history`：行业分类生效区间、真实公开时间、系统可知时间和可信级别；
- `index_constituent_history`：指数成分纳入/剔除区间、公开时间、权重及可信级别；
- `universe_sync_batch`：行业与指数成分同步/可信历史导入的状态、行数、来源和失败原因；
- `verified_history` 与 `observed_snapshot` 明确分离，禁止用当前快照伪造历史数据；
- 继续保留 v1–v6 的行情、公告、财务、后台任务、备份、告警和审计。

Schema v8 增加：

- `copilot_conversation` / `copilot_message`：AI 研究对话与消息历史；
- `research_plan` / `research_plan_event`：结构化计划、覆盖预检、确认、执行和证据解释；
- `operation_task` 增加 `copilot` 后台任务类型，服务重启后仍保留任务状态；
- 模型输出先通过严格结构验证，只有服务端白名单工具、覆盖预检和显式确认共同通过才执行；
- 继续保留 v1–v7 的行情、财务、股票池、备份、告警和审计。

Schema v9 增加：

- `screen_run`：Point-in-Time 筛选参数、覆盖诊断和不可覆盖的运行状态；
- `screen_result`：逐股票条件值、入选/淘汰原因、排名和数据证据；
- `saved_watchlist`：筛选产生的候选股票池、来源运行、时点和历史可信警告；
- 筛选只接受字段与操作符白名单，不开放任意 SQL，缺失值不会自动填零；
- 继续保留 v1–v8 的行情、财务、研究、Copilot、股票池和运维记录。

Schema v10 增加：

- `announcement_document`：公告 PDF 的校验归档、摘要和不可覆盖版本；
- `document_page` / `document_chunk`：带页码的原生/OCR 正文和检索证据块；
- `document_extraction`：解析运行、原生/OCR/空白页统计和失败诊断；
- `document_question` / `document_answer_citation`：问题、答案器版本和服务端逐字验证引文；
- `operation_task` 增加 `document_ingest`，沿用持久化进度和服务重启恢复语义。

Schema v11 增加：

- `operation_task.claim_key`：同一公告只允许一个活动归档任务，终态或租约过期后释放；
- `operation_task.owner_id/heartbeat_at`：worker 租约和条件终态更新，阻止迟到线程覆盖结果；
- `announcement_document.active_extraction_id` 与 `document_extraction.heartbeat_at`：解析
  fencing、过期接管和页面/分块事务发布；
- 所有 DuckDB 连接通过带超时的跨进程 access lock，备份在同一锁内完成 checkpoint、
  复制、校验和原子发布；下载、PDF 解析和 OCR 不长期持锁。

Schema v12 增加：

- `document_event_extraction` 与 `active_event_extraction_id`：事件提取租约、心跳和发布
  fencing；`operation_task` 增加按文档和提取器版本唯一的 `document_event_extract` claim；
- `document_event`：五类结构化事件、业务时间、真实公开时间、版本可知时间、提取器版本与
  审核状态；同公告后续 PDF 版本不会回灌到归档前的研究时点；
- `document_event_evidence`：逐字段绑定文档、页码、正文块、逐字 quote 与 SHA-256；
- `document_event_research_run`：冻结事件、公告、文档、as_of、研究参数、结果和证据；
- 提取计算和行情获取在 DuckDB 锁外完成，最终 fencing 校验/事务写入和备份复制复用现有
  access lock；锁顺序仍为 `.operations.lock` → DuckDB access lock。

Schema v13 增加：

- `stock_pool`、`stock_pool_member`、`stock_pool_revision`：自定义股票池、规范化成员和不可变版本；
- `operation_task.task_type=stock_pool_sync`：按冻结 revision 去重的数据补齐任务；
- 原 `saved_watchlist` 保留并迁移为同 ID 的 `screen_snapshot` 池；
- 默认股票池成为数据库数据，固定十只常量只负责首次初始化和测试 fixture。

证券目录与研究股票池是两个不同集合。目录更新复用既有 AkShare Provider、`sync` 任务、
`sync_batch` 和审计表，不增表；只允许明确确认后访问公网获取目录，不自动修改成员。
`ops sync` 的默认范围改为数据库默认股票池（显式请求和 `AI_QUANT_SYNC_SYMBOLS` 优先），
避免补齐全市场目录后定时任务意外扩大范围。显式行情同步的占位主数据不再覆盖真实名称。
目录更新在网络阶段不持有锁，在发布阶段按 operation → access → transaction 的顺序校验
claim/owner/heartbeat 并原子写入数据、成功审计及任务结果。旧 worker 不能覆盖新结果；
保存当前目录不等于建立可信历史证券全集，不升级股票池历史可信状态。

目录写入通过连接内注册的 DataFrame 执行单条集合式 upsert，替代同事务逐条
`executemany`，降低完整证券目录的写入内存开销。注册关系使用内部生成的唯一名称并在
成功/异常路径注销，业务值不拼入 SQL；可空日期/行业显式转换，输入重复键先行拒绝。
外层事务、fencing、operation → access 锁顺序和统一成功提交点不变，不以分批提交换取低内存。

现有 `snapshot_catalog` 由研究服务继续维护，后续迁移会在兼容现有数据的前提下统一。

## 数据生命周期

1. `planned`：同步计划已创建，尚未请求外部接口。
2. `fetching`：按分片抓取并写入 raw。
3. `normalizing`：执行代码、日期、数值和字段标准化。
4. `validating`：执行唯一性、完整性、范围和连续性规则。
5. `published`：合格记录进入稳定查询视图。
6. `failed`：保留错误和失败分片，修复后可以重试。

`daily_bar.quality_status` 使用 `pending/warning/published/rejected`。研究和回测默认只能
读取 `published_daily_bar`，不能读取同步中的半成品。

## 增量同步与断点续跑

`ai-quant data sync` 先同步证券主数据和交易日历，再按证券、自然年与
`none/qfq/hfq` 拆分日线分区。规范化后的 Provider 响应先写入
`data/raw/daily_bar/<symbol>/<adjustment>/`，文件路径和 SHA-256 写入分区记录，随后执行
DQ 并事务化发布。

请求参数会规范化后写入 `sync_batch.request_json`。完全相同的请求默认复用最近批次：

- `published` 分区不会重复抓取；
- `failed/fetching/planned` 分区会继续执行；
- `--no-resume --refresh` 创建新批次并绕过缓存；
- 单个分区失败不会丢失其他已发布分区，命令以失败状态退出，修复网络后可原命令重试。

AkShare 访问仍使用现有缓存、指数退避重试与新浪备用接口。CLI 通过标准错误输出分区
进度；`AI_QUANT_DATA_SYNC_RATE_LIMIT_SECONDS` 控制请求间隔。

`ai-quant data sync-index` 按指数和自然年同步基准日线。`ai-quant data
sync-announcements` 按证券、自然年、`q1/interim/q3/annual` 报告类型同步公告。二者同样复用
参数一致的批次，保留 Raw Parquet 路径与摘要，并只重试未发布分区。成功处理但没有公告
的区间也写入完整覆盖，避免本地查询因合法的零记录重复访问外部接口。

`ai-quant data sync-financial` 按证券、自然年同步十二项财务指标；同一证券的多年历史
只抓取一次 Provider 响应，再拆成年度分区逐个校验和发布。市场可知时间优先取 Provider
真实公告字段；没有公开时间时只能匹配本地同证券、同报告期、同报告类型的已发布公告。
无法证明公开时间时分区保持失败，不允许使用报告期结束日或抓取日伪造历史可知时间。

`ai-quant data backfill-research` 按顺序运行股票、指数、四类公告、财务四个阶段。股票和
指数自动向后预留 `max(window) * 3 + 15` 个自然日；公告自动向后预留 180 天，覆盖次年
披露的年报。每个阶段复用自己的批次和分区；上游失败时后续阶段停止，原命令可恢复执行。

默认研究股票池仍然是明确指定的代码，并非历史沪深 300 成分股。M5-B 的
`historical_index` 模式仅接受具备真实成员变动、生效日期和公开时间的可信历史记录；
从免费接口获得的当前成分只能作为首次观察日起的 `observed_snapshot`，不能回填过去。

## 研究数据源诊断与血缘

`event-study` 在 `diagnostics.data_sources` 和 HTML 报告中分别记录股票、基准与公告的：

- `serving_sources`：研究实际读取的服务层来源；
- `provider_sources`：本地数据对应的原始 AkShare/Fixture 接口来源；
- `local_hit/local_fallback_used`：是否命中本地覆盖、是否回退外部 Provider；
- `provider_fallback_used`：AkShare 是否使用备用接口；
- `source_batch_ids/raw_paths`：本地发布数据对应的同步批次和 Raw Parquet。

股票血缘通过 `daily_bar.source_batch_id` 与 `sync_partition.raw_path` 重建；指数和公告
通过各自记录的实际 Provider 来源、同步批次与 `research_sync_partition.raw_path` 重建。
公告 PDF 在通过魔数、大小、公开地址和 SHA-256 校验后写入 `data/documents/<摘要前缀>/`；
Schema v10–v12 只保存相对该归档的页码正文、检索块、解析运行、结构化事件和已验证引文，不把外部网页或
模型生成内容混入已发布公告事实表。
研究快照保留上述逐行字段，`source=local` 的 CLI 和 API 均可实现三个研究输入全部本地命中。

`batch-event-study` 对每只股票独立运行现有研究，失败股票不会中断其他股票。事件记录
附加事件公告日已知的最新财务版本、十二项指标、实际 Provider 来源和同步批次。批次
产物包含全样本汇总、股票汇总、事件明细、失败列表、四工作表 Excel、HTML 和批次清单。

## 数据质量门禁

阻断级规则包括日期可解析、主键唯一、数值完整、分区范围、正价格、非负成交活动与
OHLC 一致性。任何阻断级失败都会保留 raw 文件和 DQ 结果，但不会进入 published 视图。

指数额外检查交易日可解析、日期范围、日期唯一性和收盘价为正；公告检查标识唯一、报告
期有效、公告日期/实际公开时间有效，以及实际披露不得早于报告期结束。违反报告期顺序的
记录将被阻断，防止未来信息污染历史研究。

财务阻断规则包括报告期有效且落在请求分区、版本唯一、至少存在一项标准化指标、实际
公开时间可证明且不早于报告期结束。空年度分区仅产生警告并标记为已完整覆盖，兼容上市
前和退市后区间。

交易日连续性和备用接口缺少成交额属于警告级：停牌可能产生合法交易日缺口；新浪备用
接口没有成交额时写入零并显式记录警告。空分区也记录警告并发布完整覆盖，兼容上市前和
退市后区间；读取旧数据库时，已发布的 0 行日线分区也等价于完整覆盖，避免研究区间早于
上市日期时错误回退外部 Provider。

## 时间和复权语义

- `trade_date` 使用交易所本地自然日，不使用抓取时间代替交易日。
- `trading_calendar` 是窗口推进、缺口判断和共同交易日对齐的唯一日历。
- Schema v1 显式保存 `adjustment=none/qfq/hfq`，调用方必须选择复权口径。
- M1-B 保存 Provider 返回的 none/qfq/hfq 日线；`adjustment_ratio` 保存调整后收盘价与
  不复权收盘价的逐日比率，`method=derived_close_ratio` 明确它不是供应商原生因子。
- 公告以 `report_period_end` 保存信息所属期间，以 `published_at` 保存市场可知时间；
  本地查询只读取研究截止日结束前已实际公开的记录。
- 一季报、半年报、三季报、年报报告期结束日分别为 3 月 31 日、6 月 30 日、9 月 30 日、
  12 月 31 日；数据库约束和 DQ 均拒绝 `announcement_date < report_period_end` 的公告。
- 财务读取以 `published_at < as_of` 为准；同一报告期发生后续修订时，仅选择查询时点
  已知的最新版本，不允许用之后的修订覆盖历史快照。
- 营业收入、归母净利润、扣非净利润使用元；每股收益和每股净资产使用元/股；增长率、
  ROE、毛利率、净利率、资产负债率使用百分点；经营净现金流/营业收入统一为无量纲比率。
- 事件研究继续严格使用公告日后的第一个股票与基准共同交易日收盘价入场。

## 离线验收

```bash
uv run ai-quant data init --with-fixture
uv run ai-quant data status
uv run ai-quant data sync --start-date 2024-01-01 --symbols 600519
uv run ai-quant data sync-index --start-date 2024-01-01 --symbols csi000300
uv run ai-quant data sync-announcements --start-date 2024-01-01 --symbols 600519 --types q1 interim q3 annual
uv run ai-quant data sync-financial --start-date 2024-01-01 --symbols 600519
uv run ai-quant data financial-status --symbol 600519 --as-of 2024-08-15
uv run ai-quant batch-event-study --source fixture --symbols 600519 --start-date 2024-01-01
uv run pytest
uv run ruff check .
```

Fixture 覆盖上交所、深交所和北交所证券主数据，包含 5 个交易日、贵州茅台 5 条原始
和已发布日线，以及一条阻断级 DQ 通过记录。Fixture 仅用于测试，不作为真实行情。

## 后续入口

下一阶段可继续增加历史指数成分、因子截面与中性化、更多研究模板、AI 研究规划和调度
自动化。V1 继续不接券商、不自动下单、不提供投资建议，也不扩展分钟级高频数据。
