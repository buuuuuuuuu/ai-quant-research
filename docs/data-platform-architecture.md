# A 股本地数据库架构（M0 + M1 + M2-A + M2-B）

## 目标与边界

本阶段把现有“每次研究临时抓取并保存快照”的能力扩展为可初始化、可迁移、可检查、
可增量同步和可断点续跑的本地数据底座。它覆盖 AkShare 沪深京证券主数据、交易日历、
股票日线、指数基准、四类定期公告、Point-in-Time 财务指标和显式股票池批量研究，不在
本阶段实现定时任务、Web 界面或自动实盘交易。

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

目录和 DuckDB 文件可以分别通过 `AI_QUANT_DATA_DIR` 与
`AI_QUANT_DUCKDB_PATH` 配置。运行产物仍写入 `artifacts/`，缓存仍写入
`data/cache/`，清缓存不得删除正式数据与研究快照。

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

默认研究股票池是十个明确指定的代码，并非历史沪深 300 成分股。不得把当前股票池解释
为历史指数成分，以免引入幸存者偏差；未来若要做指数成员研究，必须单独建设成员变动与
生效日期数据集。

## 研究数据源诊断与血缘

`event-study` 在 `diagnostics.data_sources` 和 HTML 报告中分别记录股票、基准与公告的：

- `serving_sources`：研究实际读取的服务层来源；
- `provider_sources`：本地数据对应的原始 AkShare/Fixture 接口来源；
- `local_hit/local_fallback_used`：是否命中本地覆盖、是否回退外部 Provider；
- `provider_fallback_used`：AkShare 是否使用备用接口；
- `source_batch_ids/raw_paths`：本地发布数据对应的同步批次和 Raw Parquet。

股票血缘通过 `daily_bar.source_batch_id` 与 `sync_partition.raw_path` 重建；指数和公告
通过各自记录的实际 Provider 来源、同步批次与 `research_sync_partition.raw_path` 重建。
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
