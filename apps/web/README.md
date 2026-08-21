# Web 研究工作台

M3-B 工作台由现有 FastAPI 服务直接提供，不需要独立 Node.js 进程。启动 API 后访问
`http://<server-ip>:8080/`，即可查看数据库概览、提交 Point-in-Time 财务因子回测、
跟踪后台进度、浏览净值/持仓/交易/因子选择，以及打开或下载历史研究产物。

页面模板和静态资源位于 `src/ai_quant_assistant/web/`；业务数据全部通过
`/api/v1/data/overview`、`/api/v1/backtests` 和 `/api/v1/backtest-runs` 获取。
