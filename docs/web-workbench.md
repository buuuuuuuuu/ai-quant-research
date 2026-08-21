# Web 研究工作台（M3-B）

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
  └─ GET /reports/backtests/{id}   完整 HTML 报告
```

## 后台任务

`BacktestTaskManager` 使用受限线程池执行本地 DuckDB 只读回测。任务在内存中记录
`queued/running/succeeded/failed`、0–100 进度、阶段说明、起止时间、错误和最终运行 ID。
服务重启会清空任务队列，但成功运行写入 `artifacts/backtests/<run_id>/run.json`，历史
页面会从磁盘恢复，不依赖内存任务状态。

## 历史结果与下载边界

运行 ID 必须匹配程序生成的固定格式，所有路径解析后必须位于配置的
`artifacts/backtests/`。下载接口只允许净值、交易、持仓、因子选择、指标、Excel、HTML
和运行清单八类固定文件，不接受任意绝对路径或父目录跳转。图表接口最多返回 2,000 个
等距净值点，明细最多返回最近 2,000 条，避免多年回测一次性占用过多浏览器内存；原始
CSV 和 Excel 仍可完整下载。

## 部署边界

当前版本面向可信内网或个人服务器，不包含多用户认证、租户隔离和任务持久化队列。
公网使用前应通过 Nginx/Caddy 配置 HTTPS、访问认证、请求大小限制与可信 IP 范围。生产
模式不要启用 Uvicorn `--reload`，并确保 `data/` 与 `artifacts/` 有独立备份。
