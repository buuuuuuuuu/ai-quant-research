# 开源发布准备 / Open-source release preparation

这是可审阅的发布与申请材料，不是发布或申请完成证明。基线核对于 **2026-10-22**：PR #41 已合并，`main` 为 `68f5b555b78653b151a7b0a4db28a3175f8a927d`；当时没有其他打开的 PR，GitHub 没有标签或 Release。Schema 18、包版本 0.2.0。本轮不修改运行代码、依赖、策略、回测公式或历史产物。

These are reviewable preparation materials, not proof of publication or application. The baseline is pinned above. No runtime, dependency, schema or research semantics change is included.

## 交付入口 / Materials

| 内容 / Item | 文件 / File | 状态 / Status |
| --- | --- | --- |
| 简介、Topics建议值 | [repository-metadata.json](repository-metadata.json) | 可直接用于仓库 About；文件本身不会更新 GitHub |
| Release属性 | [release-draft.json](release-draft.json) | draft=true、prerelease=true；文件不会创建标签或 Release |
| 中英文Release说明 | [预览稿](../releases/v0.2.0-oss-preview.1.md) | 等待维护者审阅；未正式发布 |
| 中文申请材料 | [application.zh-CN.md](application.zh-CN.md) | 未提交；身份及组织字段由本人填写 |
| English application | [application.en.md](application.en.md) | Not submitted; personal details omitted |
| 漏洞处理 | [SECURITY.md](../../SECURITY.md) | 未假定私密报告功能已启用 |
| Issue / PR模板 | [Issue表单](../../.github/ISSUE_TEMPLATE) · [PR模板](../../.github/pull_request_template.md) | 合并至默认分支后再验证 GitHub 原生表单 |

## 可核查的事实 / Evidence

| 声明 / Claim | 证据 / Evidence | 限制 / Limit |
| --- | --- | --- |
| 公开源码及明确许可 | [PR #41](https://github.com/buuuuuuuuu/ai-quant-research/pull/41)、[Apache-2.0](../../LICENSE)、[依赖说明](../../THIRD_PARTY_NOTICES.md) | 代码许可不授权再分发上游行情或公告 |
| 当前基线有持续集成 | [main CI 39718583660](https://github.com/buuuuuuuuu/ai-quant-research/actions/runs/39718583660)、[工作流](../../.github/workflows/ci.yml) | Ubuntu/Python3.12；不代表生产、所有平台或实时提供方验证 |
| 499项Python、136项JS测试通过 | 同一CI的test job日志；Python有1条warning | 测试数不是用户量、维护者数或安全认证 |
| 可复现的无私人数据体验 | [离线示例](../../examples/offline-demo/README.md)、[测试](../../tests/test_offline_demo.py)、[截图](../images) | 全部合成数据；不是实际市场表现 |
| 可追溯维护活动 | [PR #41变更与评论](https://github.com/buuuuuuuuu/ai-quant-research/pull/41) | 自动审查不等于独立人工审计 |
| 外部采用仍处早期 | 截至2026-10-22公开仓库0 Stars、0 Forks | 没有独立核实的月下载量、活跃用户或第三方部署数；不推算 |
| 已知问题没有隐瞒 | [README限制](../../README.md) | 000333特定历史证据场景未验收；排除股票不是修复 |

## GitHub 管理设置 / Settings handoff

仓库代码连接器能提交文件与创建 PR，但本次没有修改 About/Topics 或创建 Release 的接口。GitHub 浏览器当时未登录，因此这些管理动作不能从文件存在推定已完成。保存后应重新读取仓库 About 和 Release 状态，记录真实结果。

- **About**：粘贴 metadata JSON 中的 description 和 topics，不添加私人服务器地址，也不把自己标为官方OpenAI项目。
- **Release**：使用 release-draft.json 属性及Release文案，核对精确目标SHA，只点 **Save draft**；不要点 Publish release。不创建“已发布”徽章。不上传数据库、环境文件或私人产物。
- **私密漏洞报告**：管理员检查 Security / Settings 的 private vulnerability reporting 是否启用；若尚未启用，先启用并检查入口，才可以宣称可以私密报告。本轮没有自动修改此设置。
- **模板**：合并后打开新Issue选择页，检查两个表单；通过普通PR检查模板。无需提交测试垃圾Issue。

Repository files do not apply metadata or create a GitHub release. Verify the actual remote state after saving. Private reporting must be enabled separately by an administrator before advertising it as available.

## 本轮验证与范围 / Validation and scope

文档检查应覆盖Issue表单YAML结构、ID唯一性、相对链接、JSON有效性、Topics格式、申请字段500字符限制、diff whitespace以及仅文档/协作文件变更。PR CI沿用现有完整工作流；此文档中的主线CI不替代新PR的实际CI结果。没有应用UI修改，故不新增应用截图或声称完成新的390/768/1440px视觉验收。

## 发布或申请前仍由维护者决定 / Maintainer decisions

1. 审阅并决定是否合并本轮Draft PR；本次不合并。
2. 确认Release目标、标签及预发布属性；正式发布需另行决定。
3. 亲自确认维护职责、真实使用数据、姓名邮箱和组织ID，阅读官方条款后决定是否提交申请。
4. 无真实用户指标时保留“未核实”；不补造下载量或推广成绩。仅有许可证和模板不能保证入选。

## 回滚 / Rollback

回退本轮文档提交即可撤回模板和政策文字，不涉及Schema或运行数据。GitHub About/Topics及Release草稿是独立管理状态，代码回退不会恢复它们；保存前记录原值（本次观察为简介空、Topics空、无Release）。撤回草稿不应误删其他Release或标签。不要为了撤回文档降级生产数据库。
