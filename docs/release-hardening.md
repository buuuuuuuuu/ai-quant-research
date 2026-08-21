# 生产发布加固与恢复手册（M4-B）

## 发布边界

版本 `0.2.0` 使用 DuckDB Schema v6。迁移增加运维审计、活动告警和备份保留状态，不
删除既有行情、财务数据、后台任务或备份。应用仍是研究系统，不连接券商、不自动下单。

生产路径固定为：

| 组件 | 位置 | 所属用户 |
|---|---|---|
| 源码发布副本 | `/opt/ai-quant/app` | root 管理，`ai-quant` 只读执行 |
| DuckDB 与原始行情 | `/var/lib/ai-quant/data` | `ai-quant` |
| 研究报告 | `/var/lib/ai-quant/artifacts` | `ai-quant` |
| 一致性数据库备份 | `/var/lib/ai-quant/backups` | `ai-quant` |
| 管理员凭据 | `/etc/ai-quant/ai-quant.env` | root，权限 `0600` |

专用用户无法读取 `/root`；应用目录和数据目录必须迁移，而不是仅修改 systemd 的
`User=`。安装脚本只在目标数据库不存在时复制旧数据，重复运行不会覆盖生产目录。
旧数据库备份会复制到新备份目录，并更新数据库内登记的绝对路径，避免迁移后显示
“备份文件缺失”。

## 从 M4-A 升级

```bash
cd /root/code/ai-quant-research
git switch main
git pull --ff-only

# 停止写入，保证旧 DuckDB 处于可复制状态。
systemctl stop ai-quant-web.service
systemctl stop ai-quant-sync.timer ai-quant-backup.timer
systemctl stop ai-quant-sync.service ai-quant-backup.service

bash deploy/scripts/install-production.sh /root/code/ai-quant-research
nano /etc/ai-quant/ai-quant.env

systemctl enable --now ai-quant-web.service
systemctl enable --now ai-quant-sync.timer ai-quant-backup.timer ai-quant-monitor.timer
systemctl status ai-quant-web.service --no-pager
systemctl list-timers ai-quant-*.timer
```

已有管理员密码和签名密钥不会被安装脚本覆盖。确认环境文件包含：

```ini
AI_QUANT_DATA_DIR=/var/lib/ai-quant/data
AI_QUANT_DUCKDB_PATH=/var/lib/ai-quant/data/catalog.duckdb
AI_QUANT_ARTIFACT_DIR=/var/lib/ai-quant/artifacts
AI_QUANT_BACKUP_DIR=/var/lib/ai-quant/backups
AI_QUANT_BACKUP_RETENTION_DAYS=30
AI_QUANT_BACKUP_MINIMUM_COUNT=7
```

## Nginx HTTP 与 HTTPS

只有公网 IP 而没有域名时，先使用 HTTP 模板：

```bash
apt-get update
apt-get install nginx

install -m 644 /opt/ai-quant/app/deploy/nginx/ai-quant-http.conf \
  /etc/nginx/sites-available/ai-quant
ln -s /etc/nginx/sites-available/ai-quant /etc/nginx/sites-enabled/ai-quant

# 如果默认站点拦截了访问，可检查并禁用该具体站点。
# unlink /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx
```

此时浏览器访问 `http://服务器IP/`，Uvicorn 的 8080 仅本机监听。HTTP 不加密管理员
密码，因此公网使用前应尽快配置域名与 HTTPS，或通过 VPN/防火墙限制来源。

如果已有域名和有效证书：

1. 将 `deploy/nginx/ai-quant-https.conf` 中的 `research.example.com` 替换为真实域名。
2. 使用 Certbot 或现有证书管理系统签发该域名证书。
3. 安装 HTTPS 配置，执行 `nginx -t` 并重新加载 Nginx。
4. 将 `AI_QUANT_WEB_SESSION_SECURE=true` 写入 `/etc/ai-quant/ai-quant.env`。
5. 执行 `systemctl restart ai-quant-web.service`，确认 HTTP 自动跳转到 HTTPS。

不要在只有公网 IP、没有可信域名证书时把示例证书路径当作可直接使用的真实证书。

## 备份校验、保留与恢复

生产命令通过服务用户和持久化环境执行：

```bash
systemctl start ai-quant-backup.service
systemctl start ai-quant-monitor.service

cd /opt/ai-quant/app
set -a
source /etc/ai-quant/ai-quant.env
set +a

.venv/bin/ai-quant ops backup-list
.venv/bin/ai-quant ops backup-verify --backup-id BACKUP_ID
.venv/bin/ai-quant ops backup-prune
```

`backup-prune` 默认只返回待清理候选；只有指定 `--apply` 才真正删除。备份定时任务
显式使用 `--apply-retention`，同时满足“超过 30 天”和“保留至少最新 7 份”才清理。

恢复生产数据库前必须停止 Web、同步和备份服务，核对备份 SHA-256，并输入两次完全
一致的备份 ID：

```bash
systemctl stop ai-quant-web.service ai-quant-sync.timer ai-quant-backup.timer ai-quant-monitor.timer
systemctl stop ai-quant-sync.service ai-quant-backup.service ai-quant-monitor.service

.venv/bin/ai-quant ops restore \
  --backup-id BACKUP_ID \
  --confirm-backup-id BACKUP_ID

systemctl start ai-quant-web.service
systemctl start ai-quant-sync.timer ai-quant-backup.timer ai-quant-monitor.timer
```

恢复前自动创建一份当前数据库的一致性备份。恢复只接受已登记、SHA-256 和大小一致、
且实际位于配置备份目录内的常规文件，不允许符号链接和路径越界。

## 告警、审计与质量

```bash
.venv/bin/ai-quant ops check
.venv/bin/ai-quant ops alerts --active-only
.venv/bin/ai-quant ops quality
.venv/bin/ai-quant ops audit --limit 50
```

默认规则：同步超过 48 小时未更新、备份超过 36 小时未更新、磁盘剩余不足 10%、过去
一小时登录失败达到 5 次、最近同步/备份失败，以及阻断级数据质量规则未通过。重复异常
会按键去重；恢复后自动关闭；管理员可在受登录保护的 API 中确认告警。

如需企业微信、钉钉或自定义接收器，在环境文件设置 HTTPS Webhook：

```ini
AI_QUANT_ALERT_WEBHOOK_URL=https://example.com/your-private-webhook
```

Webhook 不写入 Git，也不会返回给浏览器；除本机测试地址外，拒绝未加密 HTTP。

## 回滚

代码回滚和数据库恢复是不同操作。先使用已验证备份进行恢复演练，再切换部署副本到
已知版本；不要把旧版本程序直接指向已升级数据库并假定它一定兼容。M4-B 安装过程中
保留 `/root/code/ai-quant-research` 原始源码与数据，不会删除既有 M4-A 环境。

## 自动验证

`.github/workflows/ci.yml` 对 `main` 提交和 PR 自动执行依赖安装、Ruff、pytest、
JavaScript 检查、部署脚本语法、systemd 单元验证与 Python wheel 构建。
