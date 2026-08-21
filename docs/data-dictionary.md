# A 股本地数据库数据字典（Schema v4）

## 公共约定

- `security_id`：`交易所.六位代码`，如 `SSE.600519`、`SZSE.000001`、`BSE.430047`。
- `benchmark_id`：`INDEX.标准指数代码`，如 `INDEX.csi000300`。
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

`dataset` 取 `daily_bar`、`benchmark_daily_bar`、`announcement` 或 `financial_metric`；不同数据集复用相同
的批次状态机和请求参数规范化规则。

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
| dataset | VARCHAR | PK(1) | `daily_bar`、`benchmark_daily_bar`、`announcement`、`financial_metric` |
| security_id | VARCHAR | PK(2) | 股票标识或 `INDEX.<symbol>` |
| adjustment | VARCHAR | PK(3) | 股票复权口径、指数 `none`、公告类型、财务 `all` |
| start_date | DATE | PK(4) | 已处理区间起点 |
| end_date | DATE | PK(5) | 已处理区间终点 |
| source_batch_id | VARCHAR | NOT NULL | 发布批次 |
| updated_at | TIMESTAMP | NOT NULL | 更新时间 |

本地优先 Provider 会合并相邻覆盖区间；股票和指数只有完整覆盖请求范围且 published 视图
存在数据时才返回本地价格。公告允许“已完整同步但零记录”，此时返回本地空结果，不再
重复回退外部 Provider。

## benchmark_master

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| benchmark_id | VARCHAR | PK | 稳定指数标识，如 `INDEX.csi000300` |
| symbol | VARCHAR | UNIQUE, NOT NULL | 标准指数代码 |
| name | VARCHAR | NOT NULL | 指数名称 |
| source | VARCHAR | NOT NULL | 主数据来源 |
| source_batch_id | VARCHAR | NOT NULL | 最近更新批次 |
| updated_at | TIMESTAMP | NOT NULL | 更新时间 |

## benchmark_daily_bar

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| benchmark_id | VARCHAR | PK(1) | 指数标识 |
| trade_date | DATE | PK(2) | 交易日 |
| close | DOUBLE | 正数 | 指数收盘点位 |
| source | VARCHAR | NOT NULL | 主接口或备用接口实际来源 |
| source_batch_id | VARCHAR | NOT NULL | 发布批次 |
| quality_status | VARCHAR | pending/warning/published/rejected | 质量发布状态 |
| published_at | TIMESTAMP | NOT NULL | 本地发布时间 |

`published_benchmark_daily_bar` 只暴露 `quality_status='published'` 的稳定指数记录。

## announcement

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| announcement_id | VARCHAR | PK | 规范公告的稳定标识 |
| security_id | VARCHAR | NOT NULL | 证券标识 |
| company_name | VARCHAR | NOT NULL | 公告披露时的公司名称 |
| title | VARCHAR | NOT NULL | 公告标题 |
| report_type | VARCHAR | q1/interim/q3/annual | 一季报、半年报、三季报或年报 |
| report_year | INTEGER | NOT NULL | 报告所属年度 |
| report_period_end | DATE | NOT NULL | 报告所属期间结束日，即信息有效时间 |
| announcement_date | DATE | 不早于报告期结束 | 业务公告日期 |
| published_at | TIMESTAMP | NOT NULL | 信息实际公开时间，即可知时间 |
| url | VARCHAR | NOT NULL | 原始公告链接，无链接时为空字符串 |
| source | VARCHAR | NOT NULL | 实际公告 Provider 来源 |
| source_batch_id | VARCHAR | NOT NULL | 发布批次 |
| content_sha256 | VARCHAR | NOT NULL | 规范公告内容 SHA-256 |
| quality_status | VARCHAR | pending/warning/published/rejected | 质量发布状态 |
| ingested_at | TIMESTAMP | NOT NULL | 本地入库时间 |

一季报、半年报、三季报、年报 `report_period_end` 分别为报告年度的 3 月 31 日、6 月
30 日、9 月 30 日和 12 月 31 日。读取
`published_announcement` 时同时要求实际公开时间早于研究截止日次日零点，禁止
“报告已经形成但尚未公开”的记录参与研究。

## research_sync_partition

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| batch_id | VARCHAR | PK(1) | 所属同步批次 |
| dataset | VARCHAR | benchmark_daily_bar/announcement | 研究数据集 |
| partition_key | VARCHAR | PK(2) | 对象、类型和日期共同确定的稳定键 |
| object_id | VARCHAR | NOT NULL | 股票 `security_id` 或指数 `benchmark_id` |
| variant | VARCHAR | NOT NULL | 指数 `none` 或公告 `q1/interim/q3/annual` |
| start_date/end_date | DATE | start <= end | 分区请求区间 |
| status | VARCHAR | planned/fetching/published/failed | 断点续跑状态 |
| attempts | INTEGER | 非负 | 已尝试次数 |
| row_count | BIGINT | 非负 | 已发布行数 |
| error | VARCHAR | 可空 | 最近一次错误 |
| raw_path | VARCHAR | 可空 | 原始响应 Parquet 路径 |
| raw_sha256 | VARCHAR | 可空 | 原始文件 SHA-256 |
| created_at/updated_at | TIMESTAMP | NOT NULL | 生命周期时间 |

## financial_report

主键 `report_id` 是证券、报告期和真实公开时间的稳定 SHA-256 摘要；同一报告期后续
公开修订形成独立版本，绝不覆盖修订前的历史可知状态。

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| report_id | VARCHAR | PK | 证券、报告期、公开时间对应的稳定版本标识 |
| security_id | VARCHAR | NOT NULL | 证券标识 |
| report_period_end | DATE | NOT NULL | 财务所属报告期结束日 |
| report_type | VARCHAR | q1/interim/q3/annual | 财务报告类型 |
| published_at | TIMESTAMP | 不早于报告期 | 实际市场可知时间 |
| availability_source | VARCHAR | provider_notice/matched_announcement | 公开时间来自 Provider 公告字段或匹配本地公告 |
| source | VARCHAR | NOT NULL | 实际财务指标 Provider 来源 |
| source_batch_id | VARCHAR | NOT NULL | 本地同步发布批次 |
| raw_sha256 | VARCHAR | NOT NULL | 财务 Raw Parquet SHA-256 |
| quality_status | VARCHAR | pending/warning/published/rejected | 发布状态 |
| observed_at | TIMESTAMP | NOT NULL | 本地最后观察时间 |

`security_id + report_period_end + published_at` 唯一。查询先限制 `published_at < as_of`，
再按报告期选择截至查询时点最近的已公开版本。

## financial_metric

主键为 `report_id + metric_code`；每个版本拆分为标准化指标，以支持不同 Provider 字段
映射、指标单位约束和历史版本追溯。

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| report_id | VARCHAR | PK(1) | 所属 Point-in-Time 财报版本 |
| security_id | VARCHAR | NOT NULL | 证券标识 |
| report_period_end | DATE | NOT NULL | 所属报告期结束日 |
| published_at | TIMESTAMP | NOT NULL | 对应版本真实公开时间 |
| metric_code | VARCHAR | PK(2) | 稳定标准指标代码 |
| metric_value | DOUBLE | NOT NULL | 统一单位后的指标数值 |
| unit | VARCHAR | NOT NULL | CNY/CNY_PER_SHARE/PERCENT/RATIO |
| source_batch_id | VARCHAR | NOT NULL | 发布批次 |
| quality_status | VARCHAR | pending/warning/published/rejected | 发布状态 |

### 标准财务指标

| metric_code | 中文含义 | unit | 主接口字段 |
|---|---|---|---|
| revenue | 营业总收入 | CNY | TOTALOPERATEREVE |
| parent_net_profit | 归母净利润 | CNY | PARENTNETPROFIT |
| deducted_net_profit | 扣非净利润 | CNY | KCFJCXSYJLR |
| eps | 基本每股收益 | CNY_PER_SHARE | EPSJB |
| book_value_per_share | 每股净资产 | CNY_PER_SHARE | BPS |
| revenue_yoy | 营业收入同比增长 | PERCENT | TOTALOPERATEREVETZ |
| parent_net_profit_yoy | 归母净利润同比增长 | PERCENT | PARENTNETPROFITTZ |
| roe | 加权净资产收益率 | PERCENT | ROEJQ |
| gross_margin | 销售毛利率 | PERCENT | XSMLL |
| net_margin | 销售净利率 | PERCENT | XSJLL |
| debt_asset_ratio | 资产负债率 | PERCENT | ZCFZL |
| operating_cash_flow_to_revenue | 经营净现金流与营业收入比 | RATIO | JYXJLYYSR |

备用新浪接口若以百分数给出经营净现金流与营业收入比，入库前除以 100 转为无量纲比率；
其他 `PERCENT` 指标继续保存原始百分点，不混用比率。

## financial_sync_partition

主键为 `batch_id + partition_key`；分区由证券和自然年确定，允许独立失败与恢复。

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| batch_id | VARCHAR | PK(1) | 所属财务同步批次 |
| partition_key | VARCHAR | PK(2) | 证券和报告期区间共同确定的稳定键 |
| security_id | VARCHAR | NOT NULL | 证券标识 |
| start_date/end_date | DATE | start <= end | 财务报告期请求范围 |
| status | VARCHAR | planned/fetching/published/failed | 分区断点状态 |
| attempts | INTEGER | 非负 | 外部抓取尝试次数 |
| row_count | BIGINT | 非负 | 已发布财报版本数量 |
| metric_count | BIGINT | 非负 | 已发布标准化指标数量 |
| error | VARCHAR | 可空 | 最近一次失败原因 |
| raw_path | VARCHAR | 可空 | 财务 Raw Parquet 文件路径 |
| raw_sha256 | VARCHAR | 可空 | 财务 Raw 文件 SHA-256 |
| created_at/updated_at | TIMESTAMP | NOT NULL | 分区生命周期时间 |
