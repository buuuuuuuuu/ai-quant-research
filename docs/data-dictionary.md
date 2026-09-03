# A 股本地数据库数据字典（Schema v14）

## managed_stock（v14）

| 字段 | 类型 / 约束 | 含义 |
| --- | --- | --- |
| security_id | VARCHAR PRIMARY KEY | 证券身份，名称/代码/交易所仍属于 security_master |
| status | active / archived | 逻辑纳入/移出管理；不删除行情 |
| target_start / target_end | DATE，start≤end | 用户希望维护的历史范围，实际按上市证据和完整交易日裁剪 |
| auto_update | BOOLEAN | 是否允许日常定时行情维护 |
| availability | unavailable / partial / ready | 基础行情可用性，独立于下载任务状态 |
| coverage_json | VARCHAR / JSON | none/qfq 覆盖、缺失样例、上市/日历诊断、迁移说明；更新失败可含先前有效范围 |
| listing_date / listing_source | DATE / VARCHAR，可空 | 可靠上市日期及证据来源，不以第一根 K 线推断 |
| last_task_id | VARCHAR，可空 | 最近 operation_task ID |
| last_success_at | TIMESTAMPTZ，可空 | 最近完整成功时间 |
| revision / generation | INTEGER≥1 | 管理 CAS 令牌 / 防止旧 worker 发布的代数 |
| created_at / updated_at | TIMESTAMPTZ | UTC 时间 |

日常维护类型固定 daily_bar、复权固定 none/qfq；显式单次任务可附带四类定期公告元数据，不重复建行情或公告表。
任务复用 `operation_task.task_type=sync`，`spec_json.dataset=managed_stock`，含 security_id、symbol、
目标范围、adjustments、generation、confirmed 和 actor。股票级 claim 保证一个有效下载任务；完成释放 claim，
是否复用成功任务由实际覆盖决定。管理写入、任务提交和请求审计同事务；完成摘要、任务终态、完成审计同事务。
新任务 spec 增加 include_announcements（旧任务缺省 false）与 datasets（daily_bar，或 daily_bar + announcement）；
活动任务范围/数据选项不同返回冲突，不能静默降低已确认范围。请求/完成审计同样记录 datasets。
coverage_json.announcement_sync 是最近一次联合任务摘要，包含 status、batch_id、row_count、partitions、errors、
task_id、start_date、end_date；异常提前终止时可仅含 failed/error 和任务/区间。row_count 为该批次发布数量。
摘要不参与行情准入计算，单独行情更新和管理状态变化保留该摘要。仍用 v14 JSON 字段，无新迁移。

`coverage_json.listing_date_verified` 表示是否已确认上市边界。未知时两种复权的 `expected_rows`、
`missing_count` 为 JSON null，`missing_sample=[]`，而不是把整个目标区间标为确定缺口或视为无缺口；
实际 `rows/start_date/end_date` 仍保留。`listing_source` 可为已有主数据、东方财富个股信息或上交所单股
`sse:COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L:LIST_DATE`。来源不可用仍不能 ready。
任务失败保留最近真实阶段的 progress/message；heartbeat 仅续租，终态仍与管理摘要、审计原子提交。

迁移只新增本表，不改写旧数据或池 revision；旧行情按实际覆盖接管，旧引用缺行情保留并诊断。
详细状态、API 和锁顺序见 [本地数据管理](local-stock-data-management.md)。

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

`data sync-security-master --confirm` / Web 目录更新只补充当前代码、名称和来源，
不会删除本次未返回证券，也不会覆盖已有上市日、退市日和停牌/退市状态。
显式行情同步生成的 `requested-symbol-fallback` 占位行只可插入缺失身份，不能覆盖已有
主数据。目录更新与成员修改独立，不改变 `stock_pool_revision` 或历史可信标记。

目录发布复用 `sync_batch.dataset=security_master`，`batch_id=security-master-{task_id}`。
`operation_task.task_type=sync`、`spec_json.dataset=security_master`；result_json 和成功
审计保存 row_count、added_count、updated_count、sha256、provider_sources、warning。
sha256 对按 security_id 排序的五字段规范化目录 JSON 计算，并非行情或历史成分摘要。
这些记录和主数据变更原子提交，无须 v14 迁移。

证券主数据整批写入使用单条 `INSERT … SELECT … ON CONFLICT`，输入中的重复
`security_id` 或 `(symbol, exchange)` 在写入前明确拒绝。数据库唯一约束继续生效，
字段转换或约束失败不会提交部分主数据；该实现不新增或重建任何持久化表。

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
存在数据时才返回本地价格。股票上市前、退市后等合法 0 行分区仍写入完整覆盖；为兼容旧
数据库，`sync_partition` 中状态为 `published` 且 `row_count=0` 的分区读取时也视为完整
覆盖。公告允许“已完整同步但零记录”，此时返回本地空结果，不再重复回退外部 Provider。

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

## operation_task

Schema v5 统一持久化 Web 回测、定时增量同步和数据库备份。后台线程只负责执行，任务
历史和最终状态以 DuckDB 为准；服务重启后未完成任务会明确标记为失败。

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| task_id | VARCHAR | PK | 稳定任务标识 |
| task_type | VARCHAR | backtest/sync/backup/copilot/document_ingest/document_event_extract/stock_pool_sync | 任务类型 |
| status | VARCHAR | queued/running/succeeded/failed | 持久化生命周期状态 |
| progress | INTEGER | 0–100 | 当前执行进度 |
| message | VARCHAR | NOT NULL | 人类可读的阶段说明 |
| spec_json | VARCHAR | NOT NULL | 原始研究或同步参数 |
| result_json | VARCHAR | 可空 | 完成后的结构化执行结果 |
| run_id | VARCHAR | 可空 | 对应财务因子回测运行标识 |
| error | VARCHAR | 可空 | 失败原因 |
| created_at/started_at/completed_at/updated_at | TIMESTAMPTZ | 创建和更新不可空 | 带时区的任务生命周期时间 |
| claim_key | VARCHAR | UNIQUE，可空 | 跨进程幂等业务 claim；成功任务保留，失败或过期释放 |
| owner_id/heartbeat_at | VARCHAR/TIMESTAMPTZ | 可空 | worker 租约、心跳和终态 fencing |

## database_backup

记录已通过 DuckDB 打开校验、SHA-256 校验和原子发布的完整数据库文件。只有明确执行
保留策略才会删除超出保留天数且超过最少保留数量的旧文件，并保留删除审计信息。

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| backup_id | VARCHAR | PK | 稳定备份标识 |
| database_path | VARCHAR | NOT NULL | 被备份的数据库路径 |
| backup_path | VARCHAR | UNIQUE, NOT NULL | 一致性备份文件路径 |
| size_bytes | BIGINT | 非负 | 备份文件大小 |
| sha256 | VARCHAR | NOT NULL | 备份文件 SHA-256 |
| created_at | TIMESTAMPTZ | NOT NULL | 带时区的备份创建时间 |
| deleted_at | TIMESTAMPTZ | 可空 | 经明确授权执行保留策略的删除时间 |
| deletion_reason | VARCHAR | 可空 | 删除原因，当前固定为 `retention` |

## operation_audit

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| event_id | VARCHAR | PK | 审计事件标识 |
| action | VARCHAR | NOT NULL | 登录、同步、备份、恢复或告警处理动作 |
| actor | VARCHAR | NOT NULL | 管理员用户名或 `system` |
| outcome | VARCHAR | success/failure/warning | 操作结果 |
| remote_host | VARCHAR | 可空 | 经可信反向代理解析的客户端地址 |
| object_type/object_id | VARCHAR | 可空 | 关联任务、告警或备份标识 |
| details_json | VARCHAR | NOT NULL | 不包含密码、密钥和令牌的结构化详情 |
| created_at | TIMESTAMPTZ | NOT NULL | 审计时间 |

## operation_alert

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| alert_id | VARCHAR | PK | 告警实例标识 |
| alert_key | VARCHAR | UNIQUE | 同类异常去重键 |
| severity | VARCHAR | warning/critical | 告警级别 |
| category | VARCHAR | NOT NULL | 同步、备份、磁盘、安全或数据质量 |
| title/message | VARCHAR | NOT NULL | 告警标题与说明 |
| status | VARCHAR | active/acknowledged/resolved | 告警生命周期 |
| details_json | VARCHAR | NOT NULL | 阈值、时间和失败原因等详情 |
| first_seen_at/last_seen_at | TIMESTAMPTZ | NOT NULL | 首次与最近检测时间 |
| acknowledged_at/resolved_at/notified_at | TIMESTAMPTZ | 可空 | 确认、恢复和 Webhook 通知时间 |

## security_industry_history

Schema v7 按证券和分类体系保存行业分类变更；只允许同时满足生效区间、真实公开时间
和系统可知时间的记录进入 Point-in-Time 因子截面。

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| record_id | VARCHAR | PK | 证券、分类、生效与公开时间共同确定的稳定标识 |
| security_id | VARCHAR | NOT NULL | 证券标识 |
| classification_system | VARCHAR | NOT NULL | 行业分类体系，默认为 `provider` |
| industry_code/industry_name | VARCHAR | 名称不可空 | 行业编码及名称 |
| effective_from/effective_until | DATE | 起始不可空 | 分类生效区间，结束日包含当天 |
| published_at | TIMESTAMP | NOT NULL | 该分类变动实际公开时间 |
| knowledge_time | TIMESTAMP | NOT NULL | 系统实际可知时间，不早于 `published_at` |
| history_mode | VARCHAR | verified_history/observed_snapshot | 可信历史或首次观察快照 |
| source/source_batch_id | VARCHAR | NOT NULL | 来源与对应股票池同步批次 |
| observed_at | TIMESTAMP | NOT NULL | 实际写入时间 |

## index_constituent_history

只在 `effective_from <= as_of <= effective_until`、真实公开时间和可知时间均严格早于
研究时点，并且证券已经上市且尚未退市时，才认为证券属于对应时点的指数成分。

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| record_id | VARCHAR | PK | 指数、证券、生效与公开时间共同确定的稳定标识 |
| benchmark_symbol | VARCHAR | NOT NULL | 标准指数代码，如 `csi000300` |
| security_id | VARCHAR | NOT NULL | 证券标识 |
| effective_from/effective_until | DATE | 起始不可空 | 历史成员纳入和剔除区间 |
| published_at/knowledge_time | TIMESTAMP | NOT NULL | 真实公开时间与系统可知时间 |
| history_mode | VARCHAR | verified_history/observed_snapshot | 可信历史或观察快照 |
| weight | DOUBLE | 可空 | 实际提供的指数权重，不推算缺失值 |
| source/source_batch_id | VARCHAR | NOT NULL | 真实来源与股票池同步批次 |
| observed_at | TIMESTAMP | NOT NULL | 实际写入时间 |

`historical_index` 只读取 `verified_history`；`observed_index` 可读取观察快照，但必须
保留幸存者偏差警告，且不能使用首次观察之前的记录。

## universe_sync_batch

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| batch_id | VARCHAR | PK | 行业/指数成分同步或导入批次 |
| dataset | VARCHAR | industry_classification/index_constituents | 股票池数据集 |
| target | VARCHAR | NOT NULL | 分类体系或标准指数代码 |
| history_mode | VARCHAR | verified_history/observed_snapshot | 输入数据可信级别 |
| as_of | DATE | NOT NULL | 本次导入或实际观察日期 |
| status | VARCHAR | running/published/failed | 批次生命周期 |
| row_count | BIGINT | 非负 | 成功写入的本地证券行数 |
| source | VARCHAR | NOT NULL | AkShare 接口或显式声明的可信历史来源 |
| error | VARCHAR | 可空 | 失败原因 |
| created_at/completed_at | TIMESTAMP | 创建时间不可空 | 开始与完成时间 |

## AI 研究对话与计划（Schema v8）

`copilot_conversation` 和 `copilot_message` 保存对话标题、角色、正文、关联计划与创建时间；
消息正文不可作为代码执行。`research_plan` 保存计划版本、状态、白名单工具名、严格验证后的
参数、假设、风险、覆盖预检、规划器/模型标识、后台任务、研究运行、结果、解释和错误。
`research_plan_event` 追加记录 created、preflighted、confirmed、started、succeeded/failed
等状态事件及操作者。`operation_task.task_type` 在保留原数据的迁移中增加 `copilot`。

任何 API 密钥均不属于上述表结构，也不允许进入 `metadata_json`、`spec_json` 或审计详情。

## 条件选股运行与候选池（Schema v9）

`screen_run` 保存不可覆盖的筛选名称、自然日结束时点、股票池模式、完整白名单条件、覆盖
诊断、候选数和入选数。`screen_result` 按运行和证券保存入选状态、排名、条件值、每项
通过/淘汰/缺失评价，以及财报、行情、行业可信级别和同步批次证据。

`saved_watchlist` 保存由成功筛选产生的入选代码、来源运行、筛选时点与历史可信度。保存
动作不会把 `observed_snapshot` 升级为 `verified_history`，风险警告必须随股票池保留。

## 公告文档智能（Schema v10）

`announcement_document` 按公告保存不可覆盖的 PDF 版本、来源 URL、本地路径、SHA-256、
文件大小、处理状态、页数和解析方式。相同公告、相同摘要幂等复用；摘要变化才增加
`version_no`。读取本地文件前再次校验摘要。

`document_page` 保存页码、正文、正文摘要、字符数和 `native/ocr/empty` 解析方式；
`document_chunk` 将正文切为带页码的检索证据块。`document_extraction` 追加保存每次解析的
原生、OCR、空白页统计和失败原因。

`document_question` 保存问题范围、回答状态、答案器/模型、提示版本和限制；
`document_answer_citation` 仅保存经过服务端验证、确实出现在对应证据块中的逐字引文。
模型输出的文档 ID、页码或引文不能绕过该校验。`operation_task` 新增
`document_ingest`，沿用持久化后台进度和重启失败恢复语义。

## 文档任务并发控制（Schema v11）

`operation_task.claim_key` 是活动任务的唯一业务 claim。公告文档任务使用
`document_ingest:<announcement_id>`；queued/running 期间保持唯一，任务成功、失败或租约
过期时释放，因此历史任务仍完整保留。`owner_id` 和 `heartbeat_at` 用于条件状态转换及
过期恢复，旧 worker 不能把已经终止或被接管的任务重新写成成功。

`announcement_document.active_extraction_id` 对解析结果写入进行 fencing；
`document_extraction.heartbeat_at` 标记解析租约。页面和分块只在当前 extraction 仍持有
文档租约时以单一事务替换。v10 已有的 `(announcement_id, sha256)` 与
`(announcement_id, version_no)` 唯一约束继续作为数据库级最终完整性边界。

## 公告结构化事件（Schema v12）

`document_event_extraction` 记录 `extraction_id/document_id/extractor/extractor_version`、
running/succeeded/failed 状态、heartbeat 和错误。`announcement_document` 新增
`active_event_extraction_id`，作为写入 fencing；只有当前 extraction 可发布结果。

`document_event` 保存 `event_id`、公告/文档/证券、五类 `event_type`、真实
`published_at`、研究版本可知时间 `known_at`、可选 `event_date/period_start/period_end`、明确披露的
`direction/amount/currency/ratio`、完整 `structured_payload_json`、提取器版本和
`insufficient/needs_review/accepted/rejected` 状态。业务唯一键为
`document_id + event_type + extractor_version`。审核人、审核时间和备注与更新时间一并
保留，修订版本不覆盖旧行。

`document_event_evidence` 以 `event_id + evidence_order` 为主键。每项包含字段/claim 名、
文档、页码、正文块、逐字 quote 和 `quote_sha256`；quote 必须在该正文块中逐字存在。

`document_event_research_run` 冻结 `run_id/event_id/announcement_id/document_id/as_of`、
事件研究参数、结果和证据 JSON。只有 `published_at <= as_of` 且 `known_at <= as_of` 的
accepted 事件可以创建研究记录。`operation_task` 增加 `document_event_extract`，claim key
包含文档和提取器版本。
## 自定义股票池（Schema v13）

`stock_pool` 保存 `pool_id`、名称、描述、`source_type`、来源筛选运行、`as_of`、
`history_verified`、warning、active/archived、默认标记、`current_revision` 和时间戳。

`stock_pool_member` 保存 `pool_id`、`security_id`、显示顺序、备注和增改时间；
`(pool_id, security_id)` 唯一。证券代码、名称、交易所和上市状态来自 `security_master`，
用户不能通过股票池修改证券主数据。

`stock_pool_revision` 保存 `pool_id`、`revision_no`、有序 `symbols_json`、
`symbols_sha256`、变更类型、摘要和创建时间；`(pool_id, revision_no)` 唯一且快照不可覆盖。
研究运行在 spec/result 中保存冻结的股票池上下文，不需要为各研究引擎重复建表。
