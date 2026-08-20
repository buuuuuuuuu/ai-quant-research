# A 股本地数据库架构（M0 + M1）

## 目标与边界

本阶段把现有“每次研究临时抓取并保存快照”的能力扩展为可初始化、可迁移、可检查、
可增量同步和可断点续跑的本地数据底座。它覆盖 AkShare 沪深京证券主数据、交易日历和
日线，不在本阶段实现定时任务、Web 界面或自动实盘交易。

现有事件研究继续通过 `MarketDataProvider` 运行，输出与计算口径保持不变。
`LocalFirstProvider` 只在 `data_coverage` 完整覆盖请求区间时读取本地 published 数据，
否则回退现有 AkShare Provider。

## 分层

| 层 | 目录/组件 | 约束 |
|---|---|---|
| Bronze | `data/raw/`、`daily_bar_raw` | 保留来源和批次，不静默改写原始值 |
| Silver | `data/silver/`、`daily_bar` | 标准代码、字段类型、主键和质量状态 |
| Gold | `data/gold/`、`published_daily_bar` | 只暴露通过质量门禁的数据 |
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

## 数据质量门禁

阻断级规则包括日期可解析、主键唯一、数值完整、分区范围、正价格、非负成交活动与
OHLC 一致性。任何阻断级失败都会保留 raw 文件和 DQ 结果，但不会进入 published 视图。

交易日连续性和备用接口缺少成交额属于警告级：停牌可能产生合法交易日缺口；新浪备用
接口没有成交额时写入零并显式记录警告。空分区也记录警告，兼容上市前和退市后区间。

## 时间和复权语义

- `trade_date` 使用交易所本地自然日，不使用抓取时间代替交易日。
- `trading_calendar` 是窗口推进、缺口判断和共同交易日对齐的唯一日历。
- Schema v1 显式保存 `adjustment=none/qfq/hfq`，调用方必须选择复权口径。
- M1-B 保存 Provider 返回的 none/qfq/hfq 日线；`adjustment_ratio` 保存调整后收盘价与
  不复权收盘价的逐日比率，`method=derived_close_ratio` 明确它不是供应商原生因子。
- 公告和财务的双时间轴将在 M1-B/M2 增加，现有事件研究仍严格使用公告日后的第一个
  股票与基准共同交易日收盘价入场。

## 离线验收

```bash
uv run ai-quant data init --with-fixture
uv run ai-quant data status
uv run ai-quant data sync --start-date 2024-01-01 --symbols 600519
uv run pytest
uv run ruff check .
```

Fixture 覆盖上交所、深交所和北交所证券主数据，包含 5 个交易日、贵州茅台 5 条原始
和已发布日线，以及一条阻断级 DQ 通过记录。Fixture 仅用于测试，不作为真实行情。

## 后续入口

M2 增加公告与财务双时间轴、AI 研究规划和更多模板分析。V1 继续不接券商、不自动下单、
不提供投资建议，也不扩展分钟级高频数据。
