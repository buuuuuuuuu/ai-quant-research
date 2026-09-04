# M6-A AI 研究 Copilot

## M9-C 统一数据就绪检查

本入口可使用[研究数据预检与确认式补齐](research-data-readiness.md)：冻结实际股票池 revision 与参数，
按逐股票和真实研究时点列出行情/基准/指标缺失。行情 ready 不等于研究就绪；缺值不填零、不静默排除。
补齐计划需独立确认，最多 20 只 active 受管理股票；补齐后重新预检，绝不自动执行研究。
Copilot 没有下载或确认工具，仍受原白名单约束。原研究路径、缺失策略和 PIT 执行规则继续保留。

## 目标与边界

Copilot 把自然语言研究问题转换成可审阅、可重现的结构化计划。它不是自动交易代理，
不连接券商、不承诺收益，也不允许模型直接执行代码、Shell、SQL、文件操作或任意网络请求。
服务端只执行注册表中的研究工具，并且必须依次通过参数验证、数据覆盖预检、用户确认和
执行前二次预检。

## 工作流

1. 创建持久化对话并提交最多 4000 字的研究问题。
2. 规则规划器或 OpenAI Responses 规划器输出严格结构化计划；此时不会执行研究。
3. 服务端使用 Pydantic/domain model 再次验证参数，并检查证券、复权行情、指数、
   Point-in-Time 财务指标和股票池历史。
4. Web 展示工具名、完整参数、假设、风险和覆盖明细。阻断项存在时不能确认。
5. 用户按 `plan_id + revision` 显式确认，避免预览后计划被替换。
6. 执行接口只接受 `confirmed` 状态；后台任务再次预检后，通过互斥锁调用白名单工具。
7. 结果解释只引用实际 metrics、IC、股票池、运行 ID、产物路径和源批次，不把模型猜测
   写成研究结论。

## 白名单工具

- `list_stock_pools`：只读列出股票池、默认标记、成员数和 revision。
- `run_factor_backtest`：Point-in-Time 财务因子组合回测。
- `run_factor_lab`：IC、Rank IC、分组收益、参数扫描和样本外验证。
- `build_point_in_time_universe`：时点股票池与行业覆盖预览。

因子研究可以指定明确 `pool_id/revision`；未指定股票时，计划生成阶段解析并冻结数据库默认池。
冻结后的代码、摘要、历史可信状态和警告进入计划，确认后不会切换到更新版本。Copilot 不能创建、
修改、归档股票池，也不能在未经确认时发起大规模同步。
- 原有单股与批量定期报告事件研究仍保留在全局研究工具注册表；对话执行器只开放
  上述四个完全本地化只读/研究工具，避免覆盖不足时静默回退到外部 Provider。

## 持久化与审计

Schema v8 新增 `copilot_conversation`、`copilot_message`、`research_plan` 和
`research_plan_event`。计划保存原问题、工具、参数、假设、预检、规划器/模型元数据、
任务/运行 ID、证据化解释和状态事件。`operation_task` 新增 `copilot` 类型，服务重启时
未完成任务按既有规则标记失败，不伪造成功结果。创建、确认和执行请求同时写入
`operation_audit`。

## 配置

`AI_QUANT_COPILOT_PLANNER=rules` 是默认值，适合离线和可复现部署。设置为 `openai` 时，
必须通过运行环境提供 `OPENAI_API_KEY`；可用 `AI_QUANT_OPENAI_MODEL`、
`AI_QUANT_OPENAI_BASE_URL` 和 `AI_QUANT_OPENAI_TIMEOUT_SECONDS` 调整模型、端点和超时。
密钥字段禁止出现在配置对象 repr，并且不会落库。

## API

- `POST /api/v1/copilot/conversations`
- `GET /api/v1/copilot/conversations`
- `GET /api/v1/copilot/conversations/{conversation_id}`
- `POST /api/v1/copilot/conversations/{conversation_id}/messages`
- `POST /api/v1/copilot/plans/{plan_id}/preflight`
- `POST /api/v1/copilot/plans/{plan_id}/confirm`
- `POST /api/v1/copilot/plans/{plan_id}/execute`
- `GET /api/v1/copilot/plans/{plan_id}`

生产环境必须继续启用现有 Web 登录、HTTPS、非 root systemd、备份、告警和审计配置。
# M9-B 本地股票边界补充

证券目录、本地管理与研究股票池是三个独立概念。新增股票需要在数据浏览明确确认单只日线回填，
仅 active/ready 股票可经 StockPoolService 新增或复制到研究池；旧 revision 和 PIT 预检不变。
本次不扩展模型工具白名单：Copilot 仍可只读列池并使用冻结 pool/revision 开展已有研究，不能直接纳入、
下载、移出或恢复股票，也没有未经确认的自动数据补齐工具。缺行情时引导用户前往数据浏览操作。
