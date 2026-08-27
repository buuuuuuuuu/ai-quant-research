# 本地数据浏览器（M6-B）

Web 首页“数据库概览”的四张卡片可以下钻到股票行情、指数基准、定期公告和财务指标。
浏览器只读取 DuckDB 的 `published_*` 视图，不会为了填满页面而静默调用外部供应商。

## 使用层级

1. 数据集：查看记录数、日期覆盖、同步批次数和全局质量检查。
2. 股票或指数：按代码/名称检索，查看上市状态、行业、各复权覆盖、公告和财报数量。
3. 记录：按日期、复权、报告类型或指标筛选；服务端每页默认 50 条，最多 200 条。
4. 血缘：每条记录可查看服务层来源、供应商来源、同步批次、原始 Parquet 路径、发布时点和质量状态。

## 公告与财报内容详情（M7-B）

公告记录提供“查看公告”，展示公司、报告期、公告日期、真实公开时间、供应商、质量状态、
内容摘要和对应财务报告。本地库当前保存的是规范化元数据、原文 URL 与内容摘要，不保存
PDF 正文；页面只允许打开 `http/https` 原文地址，没有地址时明确显示“仅有本地元数据”。

财务指标记录提供“查看财报”，按不可变报告版本展示：

- 该版本的全部财务指标、中文名称、数值、单位和质量状态；
- 同报告期的旧版本与修订版本，旧版本不会被覆盖；
- 同证券、同报告期、同报告类型的定期公告；
- 报告 ID、供应商、同步批次、原始 Parquet 和 SHA-256 摘要。

详情页不会根据标题生成摘要，也不会把缺失指标填成零。报告期仍不等于公开时间，研究只能
从该公告或财报版本的 `published_at` 开始使用它。

定期公告明确区分 `report_period_end` 与 `published_at`。财务指标只从真实公开时间开始
可用，同一报告期的修订不会回填到修订公开之前。`observed_snapshot` 行业分类也会显式
提示不能用于推断首次观察之前的历史。

## API

- `GET /api/v1/data/datasets`
- `GET /api/v1/data/securities?dataset=prices&query=600519`
- `GET /api/v1/data/securities/{symbol}`
- `GET /api/v1/data/securities/{symbol}/prices`
- `GET /api/v1/data/securities/{symbol}/announcements`
- `GET /api/v1/data/announcements/{announcement_id}`
- `GET /api/v1/data/securities/{symbol}/financial-reports`
- `GET /api/v1/data/securities/{symbol}/financial-metrics`
- `GET /api/v1/data/financial-reports/{report_id}`
- `GET /api/v1/data/benchmarks`
- `GET /api/v1/data/benchmarks/{symbol}/prices`
- `GET /api/v1/data/lineage/{dataset}/{record_id}`

明细接口接受 `page`、`page_size` 和相应筛选参数。增加 `output=csv` 可下载当前筛选页；
导出会写入 UTF-8 BOM，并防止以公式控制字符开头的文本被表格软件执行。

所有动态值均使用 DuckDB 参数绑定；复权、报告类型、数据集和排序方向只接受白名单值。
Web 登录开启时，上述接口、CSV 和血缘查询沿用同一签名会话认证。
