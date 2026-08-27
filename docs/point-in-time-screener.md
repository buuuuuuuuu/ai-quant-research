# Point-in-Time 条件选股器（M7-A）

条件选股器把本地已发布数据转换为一个可复现的候选股票池。它不会调用任意 SQL，
不会为了补齐结果静默访问外部 Provider，也不会把缺失财务值当成零。

## 时点规则

- `as_of` 表示该自然日结束时点，只有 `published_at <= as_of 23:59:59.999999` 的财报可用。
- 每只证券只使用该时点最新的已发布财务报告；同一报告的后续修订只在真实公开后生效。
- `close`、`amount`、`volume` 和 `avg_amount_20d` 只读取日期不晚于 `as_of` 的前复权发布日线。
- `historical_index` 只读取 `verified_history`；`observed_index` 和其他模式保留幸存者偏差警告。
- 需要行业过滤时，行业记录还必须满足生效区间、公开时间和系统可知时间。

## 白名单条件

财务字段来自 `FINANCIAL_METRICS`，包括 ROE、营收同比、归母净利润同比、毛利率、
净利率和资产负债率等。行情字段只开放：

- `close`
- `amount`
- `volume`
- `avg_amount_20d`

操作符只开放 `gt`、`gte`、`lt`、`lte` 和 `between`。每次筛选最多 12 个字段，
同一字段只允许出现一次；区间条件必须同时提供上下界。

## CLI

```bash
uv run ai-quant screen \
  --name "高 ROE 低负债" \
  --as-of 2025-04-30 \
  --symbols 600519 000001 601318 000858 600036 000333 600276 601888 002594 300750 \
  --conditions roe:gte:12 revenue_yoy:gte:10 debt_asset_ratio:lt:60 \
  --sort-by roe --sort-direction desc --top-n 5 \
  --save-watchlist "2025-04-30 高质量候选"
```

使用全部本地可用股票时，可改为：

```bash
uv run ai-quant screen \
  --name "全库质量筛选" --as-of 2025-04-30 \
  --universe-mode all_available \
  --conditions roe:gte:12 debt_asset_ratio:lt:60
```

## API

- `GET /api/v1/screens/fields`：字段、单位、数据集和操作符白名单。
- `POST /api/v1/screens/preflight`：覆盖预检，不写入筛选结果。
- `POST /api/v1/screens/runs`：执行并持久化不可覆盖的筛选运行。
- `GET /api/v1/screens/runs`：筛选历史。
- `GET /api/v1/screens/runs/{run_id}`：逐股票条件值、评价和数据证据。
- `GET /api/v1/screens/runs/{run_id}/results.csv`：UTF-8 CSV，防表格公式注入。
- `POST /api/v1/screens/runs/{run_id}/watchlists`：保存最终入选股票池。
- `GET /api/v1/watchlists`：查看已保存候选池。

## 持久化与复现

Schema v9 新增：

- `screen_run`：名称、时点、股票池模式、完整参数、覆盖诊断和运行状态。
- `screen_result`：每只股票的条件值、通过/淘汰原因、排名、财报、行情和行业证据。
- `saved_watchlist`：入选代码、来源筛选运行、时点、历史可信度和风险警告。

旧筛选运行不会被新运行覆盖。保存的观察快照股票池仍会保留历史未验证警告，不能因
保存动作而升级为可信历史股票池。

## Web 验收

1. 打开“条件选股”，选择筛选时点和股票池。
2. 添加条件并点击“检查数据覆盖”。
3. 确认不存在阻断项后执行筛选。
4. 检查每只股票的报告期、实际公开时间、指标值和淘汰原因。
5. 下载 CSV，或保存候选股票池。
6. 将入选股票一键送入 AI 研究、组合回测或因子实验。

如果字段在目标时点的覆盖数为零，执行会被阻断；部分覆盖会给出明确警告。筛选结果
仅供研究，不构成投资建议。
