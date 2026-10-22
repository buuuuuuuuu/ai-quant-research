# 参与贡献 / Contributing

欢迎通过 Issue 提交可复现问题，通过 Pull Request 提交改进。普通问题可使用中英文 Issue 表单；安全漏洞请先阅读 [SECURITY.md](SECURITY.md)，不要公开披露。请先阅读 [AGENTS.md](AGENTS.md) 和[项目工作流](.agents/skills/ai-quant-project-workflow/SKILL.md)。

1. 从最新 main 创建分支，说明问题、预期行为和实际行为。
2. 用合成固定数据编写必要测试；不得用真实账户、服务器配置或私人数据库作 fixture。
3. 数据口径、财务公开时点、信号和成交时序的变化须明确说明，不得通过放宽预检隐藏缺口。
4. 运行 README 中的检查；UI 变更附手机、平板、桌面截图，说明未验证项。
5. 在 PR 中说明数据来源、兼容性及许可。引用第三方代码时保留原作者署名与许可；不要把第三方内容改称本项目原创。

提交者应有权贡献相应内容，主动提交用于纳入项目的贡献按 Apache-2.0 第 5 节处理（明确另有约定的内容除外）。不要求付费、Star 或使用某一种 AI 工具。

Please open reproducible issues and focused pull requests. Use synthetic fixtures, preserve data/financial-time/trading semantics, run the documented checks, and disclose compatibility and licensing changes. Never publish credentials, private databases, or account records. Contributions intentionally submitted for inclusion are handled under Section 5 of Apache-2.0 unless explicitly stated otherwise. Preserve upstream notices when using third-party material.

Security vulnerabilities must follow [SECURITY.md](SECURITY.md), not public bug reports. Release/application preparation is tracked in the [open-source handoff](docs/open-source/README.md).
