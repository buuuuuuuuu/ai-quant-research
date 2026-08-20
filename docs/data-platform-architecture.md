# A 股本地数据库架构（M0 + M1-A）

## 目标与边界

本阶段把现有“每次研究临时抓取并保存快照”的能力扩展为可初始化、可迁移、可检查的
本地数据底座。它只冻结数据库、目录、主键、时间和发布状态口径，不在本阶段实现
AkShare 全市场增量同步、定时任务、Web 界面或自动实盘交易。

现有事件研究继续通过 `MarketDataProvider` 运行，输出与计算口径保持不变。M1-B 再引入
本地优先 Provider：先读取已发布的本地数据，缺失数据交给同步任务补齐。

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

## 时间和复权语义

- `trade_date` 使用交易所本地自然日，不使用抓取时间代替交易日。
- `trading_calendar` 是窗口推进、缺口判断和共同交易日对齐的唯一日历。
- Schema v1 显式保存 `adjustment=none/qfq/hfq`，调用方必须选择复权口径。
- M1-B 优先保存原始价与复权因子，再生成 qfq/hfq 数据；本阶段 Fixture 只写 `none`。
- 公告和财务的双时间轴将在 M1-B/M2 增加，现有事件研究仍严格使用公告日后的第一个
  股票与基准共同交易日收盘价入场。

## 离线验收

```bash
uv run ai-quant data init --with-fixture
uv run ai-quant data status
uv run pytest
uv run ruff check .
```

Fixture 覆盖上交所、深交所和北交所证券主数据，包含 5 个交易日、贵州茅台 5 条原始
和已发布日线，以及一条阻断级 DQ 通过记录。Fixture 仅用于测试，不作为真实行情。

## M1-B 接口预留

下一阶段在本基线上增加：

- 证券主数据与交易日历 Provider；
- AkShare 日线分片同步、限速、重试和断点续跑；
- 原始价、复权因子及 qfq/hfq 发布；
- 完整性、唯一性、OHLC 范围和交易日连续性 DQ；
- `LocalFirstProvider`，使现有黄金路径优先读取本地 published 数据。
