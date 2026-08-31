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

## v12 → v13

升级事务先创建规范化表并扩展 `operation_task` 的 `stock_pool_sync` 类型，再执行受校验的数据迁移：

1. 用原十只兼容常量创建唯一稳定 ID `pool-default-research`；
2. 每条 `saved_watchlist` 以原 `watchlist_id` 创建 `screen_snapshot` 池，保留筛选运行、时点、可信状态、警告、股票顺序和创建时间；
3. 原 `saved_watchlist` 表继续保留，`GET /api/v1/watchlists` 兼容读取；新保存筛选同时创建 stock pool；
4. 无效 JSON、重复代码、非法代码或主数据缺失会明确报错并回滚整个 v13 事务，不静默删除。

部署前后均应创建并验证备份。升级后执行 `ai-quant data status`，确认 `schema_version=13`，再检查默认池和由 watchlist 迁移的池。重复执行迁移不会重复创建池或 revision。
