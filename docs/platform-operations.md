# 平台部署与自动化运维（M4-A）

## 数据库迁移

M4-A 将 DuckDB Schema 升级到 v5，新增 `operation_task` 和 `database_backup`。已有
v1–v4 数据库通过下列命令原地迁移，历史行情、财务和研究产物保持不变：

```bash
uv run ai-quant data init
uv run ai-quant data status
```

Web 服务启动时也会自动升级已存在的数据库。正在执行的任务如果因服务重启中断，会
被标记为失败并保留原因；已完成任务和回测结果继续可见。

## Web 登录

认证默认仅在 `AI_QUANT_WEB_PASSWORD` 已配置时启用，以兼容本机开发和既有调用。
启用后还必须配置 `AI_QUANT_WEB_SESSION_SECRET`；除健康检查和静态资源外，工作台、
API、报告与下载都需要有效的签名会话 Cookie。

```bash
export AI_QUANT_WEB_USERNAME=admin
export AI_QUANT_WEB_PASSWORD='<设置足够长的随机密码>'
export AI_QUANT_WEB_SESSION_SECRET='<设置独立的随机签名密钥>'
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
```

股票池默认取数据库中状态为 `active` 的证券；也可通过 `AI_QUANT_SYNC_SYMBOLS` 指定。
日线和指数根据最近已发布交易日回看 7 天，公告默认回看 120 天，财务报告默认回看
400 天；如果最近记录较旧，会自动补齐期间缺口。同步依次执行股票、指数、公告、财务
四个阶段，失败时保留任务、阶段进度和数据源错误。

备份默认保存到自动识别的数据目录下的 `backups/`。流程先持有跨进程互斥锁，再执行
DuckDB `CHECKPOINT`、复制数据库、重新打开验证 Schema、计算 SHA-256，最后原子发布
备份文件。系统不会自动删除已有备份。同步、备份与 Web 回测共用同一把锁。

## 安装 systemd 服务

仓库提供的单元默认匹配 `/root/code/ai-quant-research`。如果项目路径或运行用户不同，
请先修改 `deploy/systemd/*.service` 中的 `WorkingDirectory`、`User` 和可执行文件路径。

```bash
cd /root/code/ai-quant-research
uv sync --extra dev

install -d -m 700 /etc/ai-quant
install -m 600 deploy/systemd/ai-quant.env.example /etc/ai-quant/ai-quant.env

# 必须填入 AI_QUANT_WEB_PASSWORD 和 AI_QUANT_WEB_SESSION_SECRET。
# 可使用 openssl rand -base64 32 生成随机值。
nano /etc/ai-quant/ai-quant.env

install -m 644 deploy/systemd/ai-quant-web.service /etc/systemd/system/
install -m 644 deploy/systemd/ai-quant-sync.service /etc/systemd/system/
install -m 644 deploy/systemd/ai-quant-sync.timer /etc/systemd/system/
install -m 644 deploy/systemd/ai-quant-backup.service /etc/systemd/system/
install -m 644 deploy/systemd/ai-quant-backup.timer /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now ai-quant-web.service
systemctl enable --now ai-quant-sync.timer ai-quant-backup.timer
```

Web 单元在启动前会拒绝空密码和空会话签名密钥，并执行一次数据库迁移。同步定时器
在上海时区工作日 18:30 后运行，备份定时器每日 02:30 后运行；错过的任务会在机器恢复
后补执行。

```bash
systemctl status ai-quant-web.service
systemctl list-timers ai-quant-*.timer
journalctl -u ai-quant-web.service -f
journalctl -u ai-quant-sync.service -n 100 --no-pager
systemctl start ai-quant-sync.service
systemctl start ai-quant-backup.service
```

Web 运维面板显示最近同步批次、任务进度、失败信息和已验证备份；点击“立即增量同步”
或“创建数据库备份”即可提交后台任务。
