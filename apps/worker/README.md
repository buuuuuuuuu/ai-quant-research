# Worker 预留

黄金路径目前同步执行。后续将 `InterimReportResearchService.run()` 放入 RQ/Celery Worker，并保留相同的 `run_id`、快照和运行清单语义。

