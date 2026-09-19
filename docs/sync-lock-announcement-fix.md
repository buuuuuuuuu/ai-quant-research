# 日历写入与公告同步故障修复

适用分支：`feature/daily-signal-backtest-m12a`，Schema 仍为 17。

## 证据与修复

服务器日志中的 `timed out after 30s waiting for DuckDB access lock` 是应用数据库访问锁等待超时。只读请求与任务队列也使用该锁，因此可能出现列表 409、等待加载、队列调度报错。日志没有记录持锁者，不能仅凭它断言是哪项操作；首个行情分区启动前的日历逐条写入是已识别的长持锁风险。

- 日历从锁内逐条 `executemany` 改为一次 `INSERT OR REPLACE … SELECT`。数据准备在锁外，写入仍经过原访问锁及任务 generation 检查。
- 保留交易所、日期、相邻开市日、交易日序号、来源批次，以及重复输入最后一条生效的行为；整条写入失败不会部分发布。
- 日志新增 `trading calendar upsert: … rows in …s`，用于更新后确认写入耗时。没有延长锁超时，也没有绕过锁。

`Expecting value: line 1 column 1` 表示收到的内容不能作为 JSON 解析，并不表示公告数为零。现有 AkShare 公告调用使用 HTTP、未设置请求超时、直接调用 `.json()`；仅凭原错误不能判断究竟是空响应、拦截页面还是其他上游错误。

- 仅将公告传输替换为独立 HTTPS 客户端，保持原查询分类、规范化函数、时区、来源标签及公告链接身份。
- 请求有连接/读取超时、响应体大小和分页上限；关闭自动跳转，不回退 HTTP，不关闭 TLS 验证，不记录响应正文或凭据。
- 区分 HTTP 状态、空响应、非 JSON、字段缺失、分页变化等错误。只有结构合法的零条结果才可发布空覆盖；后续页失败也不能发布前面页的部分结果。
- 401/403/429 立即暂停本批次；其他相同公告接口错误连续出现三个分区时暂停。未尝试分区保持 `planned`，批次为 `failed`，不声称完成；不同类型的数据质量失败仍按原分区处理。
- 原 `resume` 可恢复失败/待处理分区，跳过已发布分区。管理页面重新确认同步沿用原任务机制，不自动发起重试或补齐。
- 页面显示简短中文原因、完成/失败/待处理分区数量和下一步；结构化错误仍保存在批次、分区及股票覆盖信息中。公告失败不撤销已就绪行情。

`requests` 原本已由 AkShare 安装，本次补为直接依赖；锁定版本不变。没有 Schema、研究公式、交易假设或历史报告改动。

## 自动化验证

新增离线用例覆盖：27,000 行日历在 128 MB 限制下写入及逐字段核对、重复日期替换、空输入、失败原子性；HTTPS/超时/重定向、空响应和 HTML、分页不完整/重复/总数变化、响应上限、429 不重复请求、公告历史链接与实际发布时间兼容；连续失败暂停、已发布分区不重复处理、断点恢复、已有行情保留及中文错误摘要。

验证命令：

```bash
uv sync --extra dev --frozen
uv run ruff check .
uv run pytest
node --test tests/js/*.test.cjs
uv build --wheel
uv run python deploy/scripts/check-wheel-contents.py
```

上述测试不连接生产或真实公告源。真实服务器到巨潮的可达性、限流及页面展示需人工确认；本修复不能保证上游始终可用。

## 服务器更新和验收

先确认无正在执行的同步/研究任务，并记录 `/opt/ai-quant/app` 当前提交用于回滚。以下步骤由管理员在服务器人工执行，更新会短暂停止服务。

```bash
cd /root/code/ai-quant-research
git status --short
git -C /opt/ai-quant/app rev-parse HEAD
```

有本地修改时先处理，不要强制覆盖。工作区干净后：

```bash
git fetch origin
git ls-remote --exit-code origin refs/heads/feature/daily-signal-backtest-m12a
git switch feature/daily-signal-backtest-m12a
git pull --ff-only origin feature/daily-signal-backtest-m12a
git log -1 --oneline
bash deploy/scripts/update-production-from-git.sh \
  /root/code/ai-quant-research \
  feature/daily-signal-backtest-m12a \
  17
```

确认输出 `Deployment succeeded` 后再验收；失败则保留日志和备份编号，按既有部署文档恢复，不要删锁或数据库文件。

```bash
git -C /opt/ai-quant/app log -1 --oneline
systemctl --no-pager --full status ai-quant-web.service
curl --fail http://127.0.0.1:8080/health
journalctl -u ai-quant-web.service --since "10 minutes ago" --no-pager
```

1. 已安装提交应与源码新提交相同；页面刷新后，股票列表和最近任务应能加载。
2. 先选一只已管理股票、一个已结束且预计有公告的年度，明确确认同步一次。不要同时重复点击或直接重跑全部历史。
3. 检查日历写入耗时日志；同步期间查看股票列表，确认不再出现持续 30 秒锁等待。若仍出现，记录相同时间段完整日志，以排查其他持锁者。
4. 公告源正常时应发布实际返回的公告；公告源失败时，应显示具体 HTTP/响应原因、批次失败与待处理数量，不能显示完整覆盖。
5. 检查已有行情仍可查看。排除上游故障后，再明确确认重试。获取到公告不等于已经具备财务因子或回测预热所需全部数据。

## 合并风险与回滚

- 公告传输改动需要真实服务器小范围验收；严格分页检查可能将上游动态变化判为失败，需要稍后重试，而非接受不完整数据。
- 保留原重试和缓存规则；同步入口仍拒绝过期缓存作为新发布数据。
- 批量写入仍会短暂持锁；不能据此承诺所有数据库操作永不争用。
- 无 Schema 迁移，可按既有部署/备份流程回滚代码。已成功发布的数据和历史报告保留；回滚旧代码会重新出现逐条日历写入及公告诊断不足的风险。
