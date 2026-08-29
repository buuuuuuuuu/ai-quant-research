# 平台部署与自动化运维（M4-A + M4-B）

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

股票池默认取数据库中状态为 `active` 的证券；也可通过 `AI_QUANT_SYNC_SYMBOLS` 指定。
日线和指数根据最近已发布交易日回看 7 天，公告默认回看 120 天，财务报告默认回看
400 天；如果最近记录较旧，会自动补齐期间缺口。同步依次执行股票、指数、公告、财务
四个阶段，失败时保留任务、阶段进度和数据源错误。

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
