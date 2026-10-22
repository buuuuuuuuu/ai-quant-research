# 澄明 · AI 量化研究助手

**把本地数据、策略验证和结果核对放在同一个可追溯的研究工作台。**

[English](README.en.md) · [快速体验](#快速体验无需真实行情或-api-key) · [功能与边界](#功能与边界) · [详细文档](#文档导航)

[![CI](https://github.com/buuuuuuuuu/ai-quant-research/actions/workflows/ci.yml/badge.svg)](https://github.com/buuuuuuuuu/ai-quant-research/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

面向个人研究者和开发者的 A 股研究工具：管理本地行情、公告和财务数据，配置候选因子与日线技术信号，核对每笔模拟成交、费用、现金和持仓。适合研究、教学和复现实验；不连接券商，不提供实盘下单或收益承诺。

项目处于持续迭代阶段。策略表现、数据完整性和证据质量分别呈现；下载完成不等于研究已经就绪。

## 界面预览

以下为真实页面的浏览器截图，全部使用本仓库合成示例，不含私人账户或真实成交。样例数值不代表市场表现。

![统一研究工作台（合成示例）](docs/images/workspace-demo.png)

![日线策略运行详情与净值（合成示例）](docs/images/backtest-demo.png)

## 快速体验：无需真实行情或 API Key

推荐 Python 3.12；支持 Python 3.11–3.13。安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后，在本机执行：

```bash
git clone https://github.com/buuuuuuuuu/ai-quant-research.git
cd ai-quant-research
uv sync --extra dev --frozen
uv run python examples/offline-demo/demo.py --output .demo/first --serve
```

打开 <http://127.0.0.1:8080>，进入“回测中心 → 历史记录”查看已生成的日线示例；命令行也会输出详情地址和 HTML 报告路径。只生成结果、不启动网页时去掉 `--serve`。

- 首次安装依赖需要网络；**示例生成只使用确定性的合成数据，不调用行情或 AI 提供方**。
- 数据、缓存和结果全部写入新建的 `.demo/first`，不读取生产配置；目录已存在时拒绝覆盖。再次体验请换用 `.demo/second`。
- 示例使用虚构价格、工作日历和证券展示名称，不是实际历史行情；复权口径在示例中仅用于演示数据结构。
- Web 示例只监听本机。页面中的真实同步或 AI 操作属于另外的显式联网功能，请不要在离线体验中启用。
- 停止服务：终端按 `Ctrl+C`。完整说明见[离线示例](examples/offline-demo/README.md)。

## 功能与边界

| 能做什么 | 如何保持可核对 |
|---|---|
| 本地数据与股票池管理 | 保存来源、覆盖状态和不可变 revision；范围在运行前冻结 |
| 数据预检与确认式处理计划 | 区分下载失败、证据不足和研究就绪；未知缺口继续阻断 |
| 财务因子、技术信号与事件研究 | 财务指标按实际公开时间使用；日线信号与下一交易日执行分开 |
| 日线回测与逐日图表 | 展示信号、指令、成交、未成交和等待退出；选中日期查看记录 |
| 成交与账户审计 | 核对现金、持仓、费用、账户净值；缺失字段不伪装为零 |
| HTML / Excel / CSV / JSON 报告 | 保留机器字段、运行身份和可复现信息；原始单文件可下载 |
| 可选 AI 研究助手与文档智能 | 规则式流程可本地运行；模型、数据源和公告读取各有独立配置与边界 |

**研究限制：** 当前手工股票池可能产生幸存者偏差；数据来源可能有历史覆盖缺口；已有数据不一定等同运行时不可变快照。未知缺口不能通过“忽略并运行”绕过。`000333` 的特定历史停牌证据场景尚未完成验收，移除股票不代表问题已修复。

本项目不承诺稳定盈利，不输出券商真实交易流水，不自动填补停牌 K 线，也不会用当前财务指标回填历史。历史收益只描述相应样本和假设下的模拟结果。

## 使用真实数据

先阅读[本地数据管理](docs/local-stock-data-management.md)和[预检说明](docs/research-data-readiness.md)，明确选择股票与范围，再确认同步。浏览、预检和查看处理计划不启动下载；历史 CLI 中的 `--source local` 存在缺失时回退提供方的路径，不等同于绝对离线模式。

生产部署请使用[部署与运维文档](docs/platform-operations.md)：配置登录、会话密钥、HTTPS 和备份。不要将无鉴权的本地体验服务直接暴露到公网。当前 Schema 为 **18**；升级已有数据库前先备份。

## 文档导航

| 主题 | 文档 |
|---|---|
| 离线可运行示例 | [数据与结果说明](examples/offline-demo/README.md) |
| 日线策略 | [策略口径](docs/daily-signal-backtest-m12a.md) · [审计](docs/daily-signal-audit-m12d.md) · [可信度与范围](docs/backtest-trust-m12f.md) |
| 财务、实验与事件 | [财务因子](docs/factor-backtest.md) · [多因子实验](docs/factor-lab.md) · [事件研究](docs/golden-path.md) |
| 数据与范围 | [股票池](docs/stock-pools.md) · [数据字典](docs/data-dictionary.md) |
| 完整操作与架构 | [详细操作参考](docs/usage-reference.md) · [架构](docs/architecture.md) |
| 贡献与许可 | [贡献指南](CONTRIBUTING.md) · [依赖许可核对](THIRD_PARTY_NOTICES.md) |

## 开发与测试

```bash
uv run pytest
uv run ruff check .
node --test tests/js/*.test.cjs
uv build --wheel
uv run python deploy/scripts/check-wheel-contents.py
```

前端测试需要 Node.js（CI 使用 runner 提供的版本）。CI 还检查 JavaScript 语法、部署脚本与 systemd units。提交问题时请附可复现步骤和脱敏日志，不要上传密码、Token、私人数据库或服务器配置。

## 许可证

本项目原创代码、文档和合成示例采用 [Apache License 2.0](LICENSE)，允许商业使用、修改和再分发，须遵守保留许可与署名等条款。第三方依赖保持各自许可证；行情、公告及其他外部内容的访问和再分发权限不由本项目许可证授予。详见 [NOTICE](NOTICE) 和[第三方说明](THIRD_PARTY_NOTICES.md)。
