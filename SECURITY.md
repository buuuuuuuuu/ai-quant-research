# 安全政策 / Security policy

## 支持范围 / Supported versions

本项目仍处于早期迭代。安全修复以当前 `main` 为目标；已发布版本应升级到包含修复的提交。旧开发分支、历史预览版本没有独立的安全回补承诺。目前不提供 LTS、响应时限 SLA 或漏洞奖金承诺。版本号相同时仍请附准确提交 SHA。

Security fixes target current `main`. Older branches and preview snapshots have no separate backport commitment. There is no LTS, guaranteed response deadline or bug bounty. Include the exact commit SHA even when package versions match.

## 私密报告 / Private reporting

1. 打开仓库的 [Security 页面](https://github.com/buuuuuuuuu/ai-quant-research/security)。**只有页面提供 “Report a vulnerability” 时**，使用该入口私密提交；本文件的存在不表示仓库管理员已经启用此功能。
2. 若该按钮不可用，请暂不披露漏洞细节。可以提交仅含“请启用私密漏洞报告入口 / Please enable private vulnerability reporting”的普通 Issue，等待维护者提供并确认私密渠道；不要在该 Issue 中附目标路径、利用步骤、攻击载荷或敏感附件。此请求本身是公开的。
3. 不要把漏洞通过公开 PR、普通 Bug 模板或公开讨论披露。本项目没有公布专用安全邮箱，请勿猜测地址或发送给无关人员。

Use **Report a vulnerability** on the repository's Security page only if available. If unavailable, withhold details and optionally open a public issue containing only a request to enable private reporting. Wait for a confirmed private channel. Do not disclose exploits in public issues or PRs. No dedicated security email is currently published.

在私密渠道中，请提供 / In the private report, include:

- 受影响的提交 SHA、组件、环境和启用的相关功能 / Commit, component, environment and relevant enabled features.
- 影响及所需权限；使用合成数据的最小复现 / Impact, required access and a minimal synthetic reproduction.
- 脱敏错误和建议修复（可选） / Sanitized evidence and an optional fix suggestion.

请勿附真实凭据、Cookie、生产数据库或个人交易记录。仅在自己有权测试的本地环境复现，不要扫描或攻击他人部署。维护者确认问题后，将协调修复、验证和披露；不要把自动回复或接收报告视为已确认漏洞或已完成修复。

Never include live credentials, cookies, production databases or private trading records. Reproduce only in an authorized local environment. Remediation and disclosure will be coordinated after triage; receipt is not confirmation or resolution.

## 哪些问题使用安全渠道 / Scope

认证绕过、越权读取或删除、路径遍历、未授权任务执行、敏感信息泄漏、恶意内容执行等属于安全报告范围。一般界面问题、缺失行情、提供方覆盖不足和策略结果讨论通常使用普通 Issue；若同时涉及权限或数据泄漏，则使用私密渠道。

Authentication/authorization bypasses, path traversal, unauthorized task execution, sensitive-data exposure and code execution belong in private reports. Ordinary UI defects, market-data gaps and strategy questions normally belong in public issues with sanitized examples.

## 部署注意 / Deployment

本地合成示例不是公网服务配置。对外部署前按[运维文档](docs/platform-operations.md)配置认证、HTTPS、会话密钥、受限目录和备份；不要提交 `.env`。外部行情、公告及模型调用有独立的授权和数据边界。研究正确性测试和 CI 通过均不等于独立安全审计或安全保证。

The local demo is not a public deployment configuration. Follow the operations documentation for authentication, HTTPS, session secrets, restricted paths and backups. Passing CI is not an independent security audit or guarantee.
