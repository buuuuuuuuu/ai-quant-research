# A 股本地数据库数据字典（Schema v2）

## 公共约定

- `security_id`：`交易所.六位代码`，如 `SSE.600519`、`SZSE.000001`、`BSE.430047`。
- `source`：实际数据来源或 Fixture 版本，任何降级接口都必须保留实际来源。
- `batch_id/source_batch_id`：关联一次同步批次，便于重试、质量检查和血缘追踪。
- 时间戳使用 UTC 存储；`trade_date/list_date/delist_date` 是不含时区的业务日期。
- `stock_zh_a_hist` 的成交量按 AkShare 原始单位“手”保存，成交额按“元”保存，禁止
  隐式换算；备用接口没有成交额时保存零并产生 `amount_available` 警告。

## schema_migrations

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| version | INTEGER | PK | 单调递增的 Schema 版本 |
| name | VARCHAR | NOT NULL | 迁移名称 |
| applied_at | TIMESTAMP | NOT NULL | 成功应用时间 |

## security_master

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| security_id | VARCHAR | PK | 稳定证券标识 |
| symbol | VARCHAR | NOT NULL | 六位证券代码 |
| exchange | VARCHAR | SSE/SZSE/BSE | 交易所 |
| name | VARCHAR | NOT NULL | 当前证券简称 |
| list_date | DATE | 可空 | 上市日期 |
| delist_date | DATE | 可空 | 退市日期 |
| status | VARCHAR | active/suspended/delisted | 证券状态 |
| industry | VARCHAR | 可空 | 当前基础行业映射 |
| source | VARCHAR | NOT NULL | 数据来源 |
| source_batch_id | VARCHAR | NOT NULL | 最近更新批次 |
| updated_at | TIMESTAMP | NOT NULL | 更新时间 |

`symbol + exchange` 唯一。历史名称和行业变更在后续版本使用历史表建模，不覆盖本表的
当前快照职责。

## trading_calendar

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| exchange | VARCHAR | PK(1) | 交易所 |
| trade_date | DATE | PK(2) | 自然日 |
| is_open | BOOLEAN | NOT NULL | 是否开市 |
| previous_open_date | DATE | 可空 | 前一开市日 |
| next_open_date | DATE | 可空 | 后一开市日 |
| session_no | BIGINT | 可空 | 开市日顺序号 |
| source | VARCHAR | NOT NULL | 数据来源 |
| source_batch_id | VARCHAR | NOT NULL | 更新批次 |

## daily_bar_raw

主键为 `security_id + trade_date + source + batch_id`，允许同一交易日保留不同来源或不同
抓取批次的原始版本。

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| security_id | VARCHAR | PK(1) | 证券标识 |
| trade_date | DATE | PK(2) | 交易日 |
| source | VARCHAR | PK(3) | 实际接口来源 |
| batch_id | VARCHAR | PK(4) | 抓取批次 |
| open/high/low/close | DOUBLE | 正数、OHLC 范围约束 | 原始不复权价格 |
| volume | DOUBLE | 非负 | 成交量，单位由 Provider 冻结 |
| amount | DOUBLE | 非负 | 成交额，单位由 Provider 冻结 |
| ingested_at | TIMESTAMP | NOT NULL | 入库时间 |

## daily_bar

主键为 `security_id + trade_date + adjustment`。规范层不允许用空字符串表达不复权，
统一使用 `none`。

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| security_id | VARCHAR | PK(1) | 证券标识 |
| trade_date | DATE | PK(2) | 交易日 |
| adjustment | VARCHAR | none/qfq/hfq | 复权口径 |
| open/high/low/close | DOUBLE | 正数、OHLC 范围约束 | 规范价格 |
| volume | DOUBLE | 非负 | 成交量 |
| amount | DOUBLE | 非负 | 成交额 |
| source_batch_id | VARCHAR | NOT NULL | 来源批次 |
| quality_status | VARCHAR | pending/warning/published/rejected | 质量发布状态 |
| published_at | TIMESTAMP | 可空 | 发布时间 |

`published_daily_bar` 是 `quality_status='published'` 的稳定视图。

## sync_batch

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| batch_id | VARCHAR | PK | 同步批次 ID |
| dataset | VARCHAR | NOT NULL | 数据集名称 |
| status | VARCHAR | 状态机枚举 | planned 到 published/failed |
| started_at/ended_at | TIMESTAMP | 可空 | 执行区间 |
| request_json | VARCHAR | NOT NULL | 冻结后的请求参数 JSON |
| row_count | BIGINT | 非负 | 本批写入行数 |
| error | VARCHAR | 可空 | 失败摘要 |
| created_at | TIMESTAMP | NOT NULL | 创建时间 |

## dq_result

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| batch_id | VARCHAR | PK(1) | 被检查批次 |
| rule_id | VARCHAR | PK(2) | 稳定规则标识 |
| object_key | VARCHAR | PK(3) | 数据集、分区或证券对象 |
| severity | VARCHAR | warning/blocking | 失败严重级别 |
| passed | BOOLEAN | NOT NULL | 是否通过 |
| observed | VARCHAR | 可空 | 实际值 |
| expected | VARCHAR | 可空 | 期望值 |
| details | VARCHAR | 可空 | 诊断信息 |
| checked_at | TIMESTAMP | NOT NULL | 检查时间 |

## sync_partition

主键为 `batch_id + partition_key`；`partition_key` 由证券、复权口径和日期区间确定。

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| batch_id | VARCHAR | PK(1) | 所属同步批次 |
| partition_key | VARCHAR | PK(2) | 稳定分区键 |
| security_id | VARCHAR | NOT NULL | 证券标识 |
| adjustment | VARCHAR | none/qfq/hfq | 复权口径 |
| start_date/end_date | DATE | start <= end | 分区请求区间 |
| status | VARCHAR | planned/fetching/published/failed | 断点状态 |
| attempts | INTEGER | 非负 | 已尝试次数 |
| row_count | BIGINT | 非负 | 已发布行数 |
| error | VARCHAR | 可空 | 最近一次错误 |
| raw_path | VARCHAR | 可空 | 规范化 Provider 响应的 Parquet 路径 |
| raw_sha256 | VARCHAR | 可空 | raw 文件 SHA-256 |
| created_at/updated_at | TIMESTAMP | NOT NULL | 生命周期时间 |

## adjustment_ratio

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| security_id | VARCHAR | PK(1) | 证券标识 |
| trade_date | DATE | PK(2) | 交易日 |
| adjustment | VARCHAR | PK(3), qfq/hfq | 复权口径 |
| close_ratio | DOUBLE | 正数 | 调整后收盘价 / 不复权收盘价 |
| method | VARCHAR | NOT NULL | 当前为 `derived_close_ratio` |
| source_batch_id | VARCHAR | NOT NULL | 发布批次 |

该表用于审计复权结果，不宣称是 AkShare 或交易所提供的原生复权因子。

## data_coverage

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| dataset | VARCHAR | PK(1) | 当前为 `daily_bar` |
| security_id | VARCHAR | PK(2) | 证券标识 |
| adjustment | VARCHAR | PK(3) | 复权口径 |
| start_date | DATE | PK(4) | 已处理区间起点 |
| end_date | DATE | PK(5) | 已处理区间终点 |
| source_batch_id | VARCHAR | NOT NULL | 发布批次 |
| updated_at | TIMESTAMP | NOT NULL | 更新时间 |

本地优先 Provider 会合并相邻覆盖区间；只有完整覆盖请求范围且 published 视图存在数据时
才返回本地价格，否则回退外部 Provider。
