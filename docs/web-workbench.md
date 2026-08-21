# Web 研究工作台与平台运维（M3-B + M4）

## 运行结构

Web 页面、静态资源、研究 API、后台回测任务、历史报告和文件下载由同一个 FastAPI
进程提供，默认监听 8080。页面不依赖公网 CDN，也不需要独立 Node.js 服务。

```text
浏览器
  ├─ GET /                         工作台页面
  ├─ GET /assets/*                 本地 CSS / JavaScript
  ├─ GET /api/v1/data/overview     DuckDB 概览与覆盖区间
  ├─ POST /api/v1/backtests        提交后台回测
  ├─ GET /api/v1/backtests/{id}    状态与进度
  ├─ GET /api/v1/backtest-runs     持久化历史运行
  ├─ GET /api/v1/backtest-runs/{id} 曲线与明细
  ├─ GET /api/v1/ops/status         数据同步、任务和备份状态
  ├─ POST /api/v1/ops/sync          提交后台增量同步
  ├─ POST /api/v1/ops/backup        提交后台数据库备份
  ├─ GET /api/v1/ops/alerts         持久化运行告警
  ├─ GET /api/v1/ops/audit          管理员与任务安全审计
  ├─ GET /api/v1/ops/quality        质量规则、失败分区与股票覆盖
  └─ GET /reports/backtests/{id}   完整 HTML 报告
```

## 后台任务

`BacktestTaskManager` 和 `OperationsTaskManager` 使用受限线程池分别执行回测、增量同步
与数据库备份。状态机、0–100 进度、阶段说明、起止时间、错误、参数和最终运行 ID 持久化
到 Schema v6 的 `operation_task`，服务重启后仍可查询。重启时未完成任务会标记为失败，
不会被误认为仍在执行。成功回测产物继续写入 `artifacts/backtests/<run_id>/run.json`。

同步、备份与回测共用跨进程互斥锁；同一 DuckDB 文件的只读与读写连接在单个进程内串行
打开，避免后台执行与浏览器轮询同时触发连接配置冲突。

## 历史结果与下载边界

运行 ID 必须匹配程序生成的固定格式，所有路径解析后必须位于配置的
`artifacts/backtests/`。下载接口只允许净值、交易、持仓、因子选择、指标、Excel、HTML
和运行清单八类固定文件，不接受任意绝对路径或父目录跳转。图表接口最多返回 2,000 个
等距净值点，明细最多返回最近 2,000 条，避免多年回测一次性占用过多浏览器内存；原始
CSV 和 Excel 仍可完整下载。

## 部署边界

设置 `AI_QUANT_WEB_PASSWORD` 与 `AI_QUANT_WEB_SESSION_SECRET` 后启用单管理员登录；
工作台、研究接口、报告、下载和 API 文档都需要有效会话，健康检查保持公开。公网部署前
仍应配置 HTTPS、可信 IP 范围和反向代理限流。生产模式不要启用 Uvicorn `--reload`。

生产模式通过 Nginx 访问，Uvicorn 仅监听 `127.0.0.1:8080`，服务由专用 `ai-quant`
用户运行。Web 常驻服务、工作日增量同步、每日备份与运行检查的安装步骤见
`docs/platform-operations.md` 和 `docs/release-hardening.md`。当前版本不包含多用户、
权限分级或多节点任务队列。
