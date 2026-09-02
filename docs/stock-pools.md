# M9-A 自定义股票池

M9-A 将原来散落在 CLI、API、Web 和 Copilot 中的十只默认股票替换为数据库内的可版本化股票池。`DEFAULT_RESEARCH_SYMBOLS` 只用于 Schema v13 首次初始化和离线 fixture，不再参与运行时研究默认值解析。

## 数据模型

- `stock_pool` 保存名称、描述、来源、筛选运行、`as_of`、历史可信状态、默认标记、归档状态和当前 revision。
- `stock_pool_member` 使用 `security_id` 作为身份，`(pool_id, security_id)` 唯一；symbol 只作为输入和展示。
- `stock_pool_revision` 保存每次有效成员或关键元数据变化后的有序 symbol 快照及 SHA-256，`(pool_id, revision_no)` 唯一。

来源包括 `system_default`、`manual`、`screen_snapshot` 和 `imported`。手工与导入池默认 `history_verified=false`，历史研究会携带幸存者偏差警告。归档是逻辑删除，历史 revision 永久可解析。

## 研究冻结与 Point-in-Time

API 和 CLI 研究入口接受互斥的 `symbols` 或 `stock_pool_id` + 可选 revision；两者均省略时解析数据库默认池。提交时保存：

- `pool_id`、`pool_revision`；
- `resolved_symbols`、`symbols_sha256`、`resolved_at`；
- `source_type`、`as_of`、`history_verified` 和 `warning`。

该快照进入回测、因子实验、条件选股、批量事件研究和研究回填的 spec/result。后续修改当前股票池不会改变已提交研究。指定历史 revision 时不会自动替换为最新版本；当前手工池也不会被标记为历史指数成分。

## 并发与锁

股票池写入遵循：`DuckDB access lock → transaction → optimistic revision check → member/metadata change → immutable revision → audit → commit`。锁只覆盖短数据库事务，不覆盖证券搜索、网络下载或研究计算。客户端必须提交 `expected_revision`；旧页面得到 HTTP 409，不能覆盖新版本。数据库唯一约束阻止重复成员和重复 revision。

股票池缺失数据同步先冻结 revision，再按 pool/revision/as-of/数据类型生成 claim。并发请求复用同一活动任务；失败或租约过期后可重试。执行复用现有增量同步服务、owner/heartbeat/fencing 与全局 operation lock，不另建同步引擎。备份先持有 operation lock，再通过 DuckDB access lock 复制一致性文件；股票池短事务会等待备份完成，锁顺序与文档任务一致。

## API 与 CLI

API 前缀为 `/api/v1/stock-pools`，支持列表、详情、创建、元数据修改、归档、复制、默认切换、成员增删改、revision 列表、证券搜索及显式确认的缺失数据同步。名称最长 120、描述 1000、备注 500；单池最多 500 只，单批最多 200 只。

```bash
ai-quant stock-pool list
ai-quant stock-pool create --name "核心观察" --symbols 600519 000001
ai-quant stock-pool add pool-... --expected-revision 1 --symbols 300750
ai-quant stock-pool set-default pool-... --expected-revision 2
ai-quant backtest --stock-pool-id pool-... --stock-pool-revision 3 --start-date 2020-01-01
```

Web“股票池管理”支持新建、查看来源和覆盖、搜索与批量添加、备注/顺序修改、批量移除、复制、归档、默认切换和确认后同步。动态文本统一 HTML 转义。

## 证券目录与添加失败排查

证券搜索只查本地 `security_master`，不会因输入代码访问公网。早期仅同步十只股票的
数据库可能缺少其他证券，已有名称也可能是行情同步生成的代码占位符。

1. 搜索无结果、添加返回 `symbols missing from security_master`：点击本区域上方
   **更新证券主数据**，确认后等待任务完成，再重新搜索或添加；不要绕过主数据校验手工造记录。
2. 添加返回 422：查看添加按钮下方的具体原因，输入不会被清空；退市或异常状态仍禁止添加。
3. 返回 409：股票池 revision 已变化，先刷新核对再提交，不自动覆盖其他页面的修改。
4. 更新失败：错误和 task_id 显示在目录按钮旁，可在运维面板查看；网络恢复或其他长任务结束后重试。

目录更新入口为 `POST /api/v1/stock-pools/securities/refresh`，请求体必须为
`{"confirm": true}`，返回 202 和持久化任务；缺失确认、false 或非布尔值返回 422，且不会调用数据源。
任务状态使用现有 `GET /api/v1/ops/tasks/{task_id}` 查询。CLI 等价操作：

```bash
ai-quant data sync-security-master --confirm
```

只调用现有 Provider 的证券目录接口，可能使用其既有备用接口；不调用历史行情、公告、
财务或交易日历同步。成员和 revision 保持不变，用户随后自行添加并确认“同步缺失数据”。
定时同步不会按补齐后的全市场目录展开：请求中的显式 symbols 优先，其次
`AI_QUANT_SYNC_SYMBOLS`，最后为数据库默认股票池。低层 `data sync` 省略 symbols 的
原有全市场行为不变，**不要把它当成仅更新证券目录的命令**。

目录任务使用既有 `sync` 类型与 `spec.dataset=security_master`；跨进程唯一 claim
复用 queued/running 任务，owner、15 秒 heartbeat 和 30 分钟租约保护发布。成功后释放 claim，
下一次明确操作可获取新目录；失败/过期允许重试，旧 worker 不能发布。
网络请求不持有 operation lock 或 DuckDB access lock；发布遵循
`operation lock → DuckDB access lock → transaction`，在一个事务中校验 fencing 并写入
主数据、同步批次、成功审计及任务终态。其他长任务占用 operation lock 时快速失败，
提示待其结束后重试；不无限等待。校验或事务失败时不留下半更新目录。

证券主数据采用 `INSERT … SELECT … ON CONFLICT` 集合式写入，不再对完整目录在同一
事务内逐条 `executemany` upsert。输入注册为连接私有、唯一名称的临时关系，日期和可空
行业字段显式转换；成功/失败均注销关系，不写中间目录文件、不提交外层事务。
输入重复 security_id 或 symbol/exchange 明确报错，不能依赖批量 SQL 任意挑选重复行。
这不改变证券目录校验、任务去重或未返回证券的保留规则。

低内存回归使用 6,000 条合成证券、128 MB DuckDB 内存额度，分别在 1/4 个线程下测试
完整任务的新增、重复刷新及占位行情主数据防覆盖，并验证类型/约束错误、调用方回滚、
临时关系清理和失败后重试。128 MB 指 DuckDB 管理的内存额度，不是整个 Python 进程的
总内存上限；不对生产服务器总内存作容量承诺。本次不修改全局内存或线程配置，Schema 仍为 v13。

已有未返回证券不删除，已知上市日、停牌/退市状态和退市日保留。拒绝空目录、重复身份、
身份冲突、代码占位名称和过期缓存。后续显式行情同步不会覆盖真实目录名称及来源。
当前目录不能证明历史成分或历史可知状态，不会改变股票池 `history_verified` 或历史 revision。

前端为保存操作提供忙碌状态、防重复点击和就近错误消息；搜索响应有过期保护。
静态资源 URL 使用内容哈希，部署后重新加载页面即可获取修复，不依赖包版本号变化。

## v12 → v13

升级事务先创建规范化表并扩展 `operation_task` 的 `stock_pool_sync` 类型，再执行受校验的数据迁移：

1. 用原十只兼容常量创建唯一稳定 ID `pool-default-research`；
2. 每条 `saved_watchlist` 以原 `watchlist_id` 创建 `screen_snapshot` 池，保留筛选运行、时点、可信状态、警告、股票顺序和创建时间；
3. 原 `saved_watchlist` 表继续保留，`GET /api/v1/watchlists` 兼容读取；新保存筛选同时创建 stock pool；
4. 无效 JSON、重复代码、非法代码或主数据缺失会明确报错并回滚整个 v13 事务，不静默删除。

部署前后均应创建并验证备份。升级后执行 `ai-quant data status`，确认 `schema_version=13`，再检查默认池和由 watchlist 迁移的池。重复执行迁移不会重复创建池或 revision。
