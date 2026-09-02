# 平台部署与自动化运维（M4-A + M4-B）

## 当前 M9-B 升级与维护（Schema v14）

先按现有停写/备份验证流程保留 v13 备份，再由服务用户在应用目录执行 `ai-quant data init`；
`data status` 应同时显示当前和预期版本 14，并包含 managed_stock。迁移不联网，不删除旧表、旧成员或旧 revision。
不要用旧程序直接打开 v14 作为回退；按已验证备份恢复，先保全升级后数据。开发分支不代表授权自动部署。

定时 `ops sync` 的**日线**范围现在是 active + auto_update 的本地受管理股票，按各自目标历史范围补缺。
公告/财务仍使用既有显式配置/默认池，纳入一只股票不会自动扩大这些数据类型。迁移已有日线股票开启维护，
仅旧池引用而无行情的股票关闭维护；升级后检查 `data-stock list`，按需确认开关。
网页管理队列在服务启动后扫描 queued 任务；成功任务不重跑，失去租约的任务可显式重试。
遇到父同步提示已有任务/超时，先查所列任务 ID，不重复创建下载。2GB 级主机每数据目录最多一个本地回填下载槽。

操作、API/CLI、恢复、缺口诊断和锁顺序详见 [本地股票数据管理](local-stock-data-management.md)。
本次不调整端口、监听地址、反向代理或安全配置。

## 数据库迁移

M4-A 将 DuckDB Schema 升级到 v5；M4-B 进一步升级到 v6，新增持久化运维告警、安全
审计与备份生命周期字段。已有 v1–v5 数据库通过下列命令原地迁移，历史行情、财务和
研究产物保持不变：

```bash
uv run ai-quant data init
uv run ai-quant data status
```

Web 服务启动时也会自动升级已存在的数据库。Schema v11 起，queued/running 任务使用
owner 和 heartbeat 租约；启动只回收超过租约期限的任务，不会把其他 Uvicorn Worker
仍在执行的任务误判为失败。已完成任务和回测结果继续可见。

Schema v12 增加 `document_event_extract` claim 和独立 extraction fencing。升级后先确认
`data status` 的 schema 版本至少为 12，再提交事件提取；备份、同步、文档解析和事件写入继续
使用 `.operations.lock` → DuckDB access lock 的既定顺序。

Schema v13 增加自定义股票池和 `stock_pool_sync` 任务。升级会校验所有旧
`saved_watchlist`；无效 JSON、重复代码或缺失证券主数据将回滚并给出具体记录 ID，需先修复
源数据，不能跳过或静默删除。升级前后创建并验证备份，升级后确认默认池、迁移池和 revision 数量。

股票池修改只持有短时 DuckDB access lock 和事务；缺失数据同步在网络阶段不持有该锁，
但沿用 operation lock 与 owner/heartbeat/fencing。统一锁顺序为 operation lock（长任务需要时）
再进入 DuckDB access lock，禁止反向获取。

## Web 登录

认证默认仅在 `AI_QUANT_WEB_PASSWORD` 已配置时启用，以兼容本机开发和既有调用。
启用后还必须配置 `AI_QUANT_WEB_SESSION_SECRET`；除健康检查和静态资源外，工作台、
API、报告与下载都需要有效的签名会话 Cookie。

```bash
# 认证变量由权限受限的生产环境文件注入；文档不提供密码或签名密钥示例值。
set -a
. /etc/ai-quant/ai-quant.env
set +a
uv run uvicorn ai_quant_assistant.api.app:create_app --factory --host 0.0.0.0 --port 8080
```

Cookie 启用 `HttpOnly`、`SameSite=Lax` 和 12 小时有效期；同一客户端连续登录失败
5 次后会被限制 5 分钟。通过 HTTPS 反向代理部署时设置
`AI_QUANT_WEB_SESSION_SECURE=true`。生产凭据只写 `/etc/ai-quant/ai-quant.env`，不要
提交到仓库。

## 增量同步、状态与备份

```bash
uv run ai-quant ops status
uv run ai-quant ops sync
uv run ai-quant ops sync --symbols 600519 000001
uv run ai-quant ops tasks --limit 20
uv run ai-quant ops backup
uv run ai-quant ops backup-list
uv run ai-quant ops backup-prune
uv run ai-quant ops check
uv run ai-quant ops alerts --active-only
uv run ai-quant ops quality
uv run ai-quant ops audit --limit 20
```

同步范围优先取请求显式 `symbols`，其次为 `AI_QUANT_SYNC_SYMBOLS`，否则解析数据库默认
股票池并冻结本次代码列表。不会自动把 `security_master` 的所有 active 证券当成同步范围。
日线和指数根据最近已发布交易日回看 7 天，公告默认回看 120 天，财务报告默认回看
400 天；如果最近记录较旧，会自动补齐期间缺口。同步依次执行股票、指数、公告、财务
四个阶段，失败时保留任务、阶段进度和数据源错误。

### 仅补充证券目录（Schema 仍为 v13）

当股票池搜索缺少新证券或名称仍显示代码时，通过 Web“更新证券主数据”明确确认，或在
应用部署目录、服务用户和既有生产环境配置下执行 `ai-quant data sync-security-master --confirm`。
不要在 `/root` 源码目录下切换成服务用户执行，避免工作目录权限错误；不要用全市场
`data sync` 代替目录更新。

任务仅获取代码/名称目录，不拉历史行情，不自动新增成员；完成后重新搜索、添加，再按需
确认“同步缺失数据”。任务采用 `sync` 类型并在 spec 中标明 `dataset=security_master`，
可通过 `ops tasks` 和 Web 运维面板检查。并发更新复用活动任务，失败或租约过期可重试；
同步/备份占用 operation lock 时发布快速失败，待其结束后重试。

审计动作包括 `security_master_refresh_requested` 和 `security_master_refreshed`。
成功记录包含 task_id、批次、来源、规范化目录 SHA-256、新增/更新数量；主数据写入、
批次、成功审计及任务终态在同一事务提交。已知停牌/退市信息和未返回证券保留。
备份仍沿用 operation → access 锁顺序；下载目录期间不阻塞备份和股票池编辑。

若旧版本目录更新在 `SyncRepository.upsert_security_master` 的 `executemany` 处出现
DuckDB `OutOfMemoryException`，先更新包含集合式写入修复的代码，再重试一次目录更新。
修复以单条 SQL 发布整批目录，不分批提交，也不提高 DuckDB 内存额度，Schema 仍为 v13。
失败任务保留诊断，旧 worker fencing、审计和任务终态的事务一致性保持不变。

`free -h` 是执行命令时的系统内存快照，不能代表失败任务的峰值；已有 Swap 也不能绕过
DuckDB 自身的内存额度。不要仅根据该错误提高额度、关闭 Swap 或反复触发全市场同步。
确认更新后的提交号，结合任务 ID 和 `journalctl -u ai-quant-web.service` 的最新堆栈排查；
升级前的旧搜索错误不应与新进程日志混为一谈。

备份默认保存到自动识别的数据目录下的 `backups/`。流程先持有长任务互斥锁，再持有
DuckDB 跨进程 access lock，并执行 `CHECKPOINT`、复制数据库、重新打开验证 Schema、
计算 SHA-256，最后原子发布
备份文件。清理默认只预演；systemd 备份服务通过明确的 `--apply-retention` 开启保留
策略，默认至少保留最近 7 份且只清理超过 30 天的旧备份。同步、备份、恢复与 Web
回测共用长任务锁；文档下载/OCR 不占用长任务锁，只在短时数据库读写阶段使用 access
lock，因此备份复制期间文档写入会有界等待，而不会产生不一致快照。

## 安装 systemd 服务

M4-B 不再让服务访问 `/root`。专用 `ai-quant` 用户执行 `/opt/ai-quant/app` 中的应用，
行情、研究产物与备份分别位于 `/var/lib/ai-quant/data`、`artifacts` 和 `backups`。
从已验收的 M4-A 环境安全迁移：

```bash
cd /root/code/ai-quant-research
git switch main
git pull --ff-only

# 安装前先停止旧服务，避免复制活跃 DuckDB。
systemctl stop ai-quant-web.service ai-quant-sync.timer ai-quant-backup.timer
systemctl stop ai-quant-sync.service ai-quant-backup.service
bash deploy/scripts/install-production.sh /root/code/ai-quant-research

# 旧管理员密码和会话密钥不会被覆盖；确认新增 /var/lib 路径。
nano /etc/ai-quant/ai-quant.env

systemctl enable --now ai-quant-web.service
systemctl enable --now ai-quant-sync.timer ai-quant-backup.timer ai-quant-monitor.timer
```

Web 单元在启动前会拒绝空密码和空会话签名密钥，并执行一次数据库迁移。同步定时器
在上海时区工作日 18:30 后运行，备份定时器每日 02:30 后运行，运行风险检查每 15 分钟
执行；错过的任务会在机器恢复后补执行。生产 Web 仅绑定 `127.0.0.1:8080`，外部访问
需要安装 `deploy/nginx/` 中的反向代理模板。

```bash
systemctl status ai-quant-web.service
systemctl list-timers ai-quant-*.timer
journalctl -u ai-quant-web.service -f
journalctl -u ai-quant-sync.service -n 100 --no-pager
systemctl start ai-quant-sync.service
systemctl start ai-quant-backup.service
systemctl start ai-quant-monitor.service
```

Web 运维面板显示最近同步批次、任务进度、失败信息、已验证备份、运行告警、数据质量
与管理员审计；点击“立即增量同步”“创建数据库备份”或“检查运行风险”可立即执行。
完整反向代理、备份恢复和回滚说明见 `docs/release-hardening.md`。
