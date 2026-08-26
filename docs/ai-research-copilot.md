# M6-A AI 研究 Copilot

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

- `run_factor_backtest`：Point-in-Time 财务因子组合回测。
- `run_factor_lab`：IC、Rank IC、分组收益、参数扫描和样本外验证。
- `build_point_in_time_universe`：时点股票池与行业覆盖预览。
- 原有单股与批量定期报告事件研究仍保留在全局研究工具注册表；M6-A 对话执行器只开放
  上述三个完全本地化工具，避免覆盖不足时静默回退到外部 Provider。

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
