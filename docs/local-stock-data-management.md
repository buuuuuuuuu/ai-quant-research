# M9-B 本地股票数据管理

## 职责与使用流程

| 概念 | 负责 | 不意味着 |
| --- | --- | --- |
| 证券目录 `security_master` | 搜索身份、代码、名称、交易所、上市状态 | 本地已有行情或允许下载全市场 |
| 数据浏览 / `managed_stock` | 明确纳入、日线回填、维护、覆盖诊断、移出与恢复 | 股票池或实盘持仓 |
| 股票池 | 引用已就绪数据建立研究分组、冻结 revision | 拥有另一份行情或拥有历史指数成分真实性 |

以 `003816` 为例：先更新本地证券目录（如需要），在数据浏览搜索该代码，核对名称和交易所。
选定起点（默认 2012-01-01）与终点，确认将访问公网，然后点击“添加并同步”。HTTP 立即返回
持久化任务；单只股票后台下载不复权 `none` 与前复权 `qfq` 日线、执行原质量检查并按年分区发布。
不自动下载公告、财务、PDF，不做 OCR 或模型调用。浏览、搜索、移出、恢复和创建股票池均不隐式下载。

证券目录没有上市日期时，后台单独读取供应商明确的上市日期字段，保存来源；不从第一根 K 线猜测
上市时间。实际起点为目标起点与可靠上市日的较晚者。截止日按上海时间 16:30 的保守完整日边界裁剪，
再以交易日历中的开市日核验。不会填充上市前数据、周末数据或尚未形成的当日日线。

## 状态与准入

- `status=active/archived`：是否纳入本地管理，与任务运行状态不同。
- `availability=unavailable`：没有足够的已发布行情或尚无可验证范围。
- `availability=partial`：已有部分已发布分区，但 none/qfq、历史缺口、日历或上市证据未满足完整性要求。
- `availability=ready`：可靠起点、完整日历、两种复权的目标交易日逐一覆盖，且每种至少两行。
- `task.status=queued/running/succeeded/failed`：最近一次下载的执行状态，失败有可见原因及重试入口。

首次任务只有全部必要分区发布并通过覆盖核验才成功；空响应、一行行情、只完成 none 或最新日期
到达目标但中间缺行均不能标记就绪。部分成功保留已发布分区，重试继续检查缺口。
已经可用的股票在更新排队/执行期间仍可加入股票池；失败的增量更新保留之前已验证覆盖并附更新警告，
并不声称新目标已完成。新查询的日期范围仍由研究预检检查，不能用 `ready` 代替研究数据完整性判断。

**保守限制**：目前没有可信停牌区间适配器。无法解释的交易日缺行、上市证据缺失或日历边界不足
均保留诊断，不把空响应自动当停牌，也没有“强制就绪”开关。需要供应商修复数据或补充可靠证据后重试。
前复权会随公司行动变化，更新会重新获取已存 qfq 年分区；不把本地价量版本声称为历史时点可知的复权因子。
任务失败时请检查覆盖/质量诊断，研究继续沿用既有 Point-in-Time 与防未来规则。

所有新建、添加、复制、CLI、筛选保存候选池最终走同一 `StockPoolService` 准入检查：
目录状态 active、受管理 active、availability ready。不合格代码按原因返回，不静默过滤。
旧池成员和历史 revision 不强制删除；重复添加已存在的旧成员是无变化的幂等操作。
Copilot 沿用只读股票池/研究工具白名单，未新增可下载或移出股票的模型工具。

## 补齐算法与并发

不再从整张行情表的 `MAX(trade_date)` 推导新股票起点。每次使用该管理记录的目标范围，按
`security_id × adjustment × 年分区` 比较已发布交易日集合与日历；选择有缺口的历史分区及最近七日
涉及的分区重叠刷新。qfq 更新重取已存年分区。复用 `MarketDataSyncService`、原标准化、质量校验和
分区事务，不另建下载引擎。刷新响应若丢失已经发布的日期，拒绝覆盖旧有效行情。

管理记录、任务 claim 和请求审计在一个短数据库事务中提交。任务使用既有 `sync` 类型，
`spec.dataset=managed_stock`，固定 none/qfq；股票级唯一 claim 比配置级去重更严格，避免不同目标
同时覆盖同一股票。有活动任务时相同配置复用；更改范围要求 expected_revision 且不能与活动任务竞争。
成功任务重复请求会重新检查实际覆盖（包括跨收盘时点），覆盖不足则新建任务；失败/过期租约可重试。

owner、heartbeat（15 秒）、30 分钟租约和 management generation 共同 fencing。
每次原同步仓储获取连接时在共享 DB access lock 内检查任务 owner、lease、last_task_id、active 与 generation，
检查到发布之间没有跨进程竞争窗口。完成状态、就绪摘要和完成审计原子提交；提交失败不得显示成功。
请求携带管理 `expected_revision`；配置/移出/恢复递增 revision 和 generation，旧 worker 不能重新激活股票。

同一数据目录增加 `.managed-download.lock`，仅串行化本功能的下载槽位，跨线程和跨进程至多一个在下载。
抢不到槽位时任务保持 queued，由每 5 秒扫描的有界后台队列接手；数据库访问仍有 30 秒超时。
服务启动会恢复查询持久化队列，已成功任务不重新执行；过期 running 会失败释放 claim，允许显式重试。
默认请求间隔不低于 0.2 秒，按年处理而非全历史大事务，适配低内存服务器。

锁顺序：既有增量同步 `operation lock → managed download slot → DB access lock`；
网页本地回填 `managed download slot → DB access lock`；备份 `operation lock → DB access lock`；
管理/股票池写入只取 DB access lock。不在 DB 事务里取得任务仓储的进程锁，也不在 DB 锁内等待网络。
增量父任务遇到别的 worker 已接手，最多等待 30 秒复用结果；超时显示已有任务 ID，子任务不会被重复启动。
原增量服务的全局 operation lock 生命周期保持不变；本功能网页下载不长期占用它，备份可在网络等待期间进行。

## 移出和恢复

移出需要确认，检查所有 active 股票池的**当前成员**；仍被引用返回 409，包含池 ID、名称和 revision。
先在这些池中移除成员，再移出管理。不自动级联改池，不物理删除任何行情、文档或研究数据。
归档原子关闭自动更新并使旧任务失效，历史池和研究结果仍可通过原明细路径读取。
恢复复用同一记录与数据、重新检查覆盖，默认关闭自动更新；补齐或启用后续自动下载必须另行确认。

## API 与 CLI

统一前缀 `/api/v1/data/managed-stocks`，使用既有登录保护：

| 方法/路径 | 用途 |
| --- | --- |
| GET 前缀 | query、include_archived、ready_only、page、page_size≤200 |
| GET `/directory?query=...` | 只读目录搜索，limit≤50，不纳入管理 |
| POST 前缀 | 单只添加/更新/重试，返回 202 和持久化任务 |
| GET `/{security_id}` | 管理记录、覆盖摘要与最近任务 |
| GET `/{security_id}/references` | active 池当前引用 |
| PATCH `/{security_id}` | action=archive/restore/configure，expected_revision、confirm；configure 可设 auto_update |

POST 字段：symbol（六位）、start、end、confirm=true、auto_update、可选 expected_revision、refresh。
日期限定 1990-01-01 至提交日。更新已有范围/自动维护配置需 revision；强制刷新用 refresh=true。
不支持批量股票，不接收公告/财务下载选项。404 为不存在，422 为参数/准入问题，409 为版本、引用、管理或锁冲突。
完整任务历史继续使用 `/api/v1/ops/tasks/{task_id}`。

```bash
ai-quant data-stock list
ai-quant data-stock add 003816 --start-date 2012-01-01 --confirm --enqueue-only
ai-quant data-stock show SZSE.003816
ai-quant data-stock sync 003816 --expected-revision 1 --confirm --refresh
ai-quant data-stock references SZSE.003816
ai-quant data-stock configure SZSE.003816 --expected-revision 1 --auto-update off --confirm
ai-quant data-stock archive SZSE.003816 --expected-revision 2 --confirm
ai-quant data-stock restore SZSE.003816 --expected-revision 3 --confirm
```

示例 revision 仅示意，必须以 show 的最新值为准。CLI 默认等待执行结果；`--enqueue-only` 仅持久化排队，
需要运行 Web 进程的队列扫描器。没有 Web 时可再次运行相同 add/sync（不带 enqueue-only）接手 queued 任务。
新 CLI 支持 `--data-dir` / `--duckdb-path`，不要在 root 不可遍历目录中降权执行。

## Schema v13 → v14 和验收

仅增加 managed_stock 与索引。旧实际日线、公告、财务对应证券和旧池当前成员进入迁移候选，
不是整个 security_master。已有行情按已存最小/最大日期建立目标；不声称自动拥有 2012 至今完整历史。
旧成员缺行情仍保留关系，标记待补齐；缺主数据/上市证据明确写入 coverage 诊断。
已存价量股票沿用自动维护（auto_update=true），仅旧引用/公告/财务而无日线的股票默认不开自动更新。
迁移不发起网络、不修改任何旧行情/事件/股票池 revision；同一 schema_migrations 事务保证可重复执行。

由管理员安排升级：停写 → 旧版本备份并 verify → 安装本分支 → 同一服务身份 `ai-quant data init`
→ `ai-quant data status` 确認 v14/managed_stock → 核对旧表和池 revision → 新版本备份验证 → 恢复服务。
升级前备份 ID 请使用命令真实返回值，不要原样输入 BACKUP_ID。不要在 v14 数据库上直接运行旧版程序回退；
需要回退时按备份恢复流程，保留故障现场并注意升级后新增数据需要另行保全。

部署脚本预期版本更新为 14，但本开发不执行部署、不修改监听地址或认证配置。
升级后先核对 managed-stock 列表和自动更新开关；未知上市日/缺口的旧数据可能显示 partial，这是诊断而非删除。
用一只股票确认回填、错误重试、池准入、引用阻止移出、恢复不自动下载，再扩大维护范围。

## 开发验证记录（2026-09-02）

本地完整 pytest：285 passed（262.30 秒）；仅有既有 Starlette TestClient 依赖弃用警告。
最终旧成员准入修正后另跑迁移/股票池专项 11 项通过。Node 离线行为测试 15 项通过。
Ruff、全部 JavaScript 语法、逐个部署 Shell 语法、systemd service/timer 验证、Wheel 构建及归档内容检查、
git diff --check 均通过。回归保留 128 MB / 6,000 条证券批量写入测试以及 M8/M9-A 测试。
并发测试使用线程屏障、两个独立 spawn 进程、受控供应商和本地临时数据库；没有使用真实公网、OpenAI、券商或系统 OCR。
提交后的 GitHub CI 状态以 Draft PR 检查为准；本次开发未连接或部署生产服务器。
