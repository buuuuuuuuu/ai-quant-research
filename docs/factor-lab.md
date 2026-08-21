# M5-A / M5-B 多因子研究实验室

## 研究边界

- 只读取已经发布的本地股票、沪深 300 和财务数据，不在回测过程中请求远端行情。
- 在信号日 15:00 之前已经公开的报告才允许进入因子截面。
- 下一基准交易日开盘建立或调整持仓，沿用现有整手、T+1、停牌、一字涨跌停和交易成本约束。
- 默认股票池是用户明确给定的研究样本，不代表任何历史时点的指数成分，也不消除幸存者偏差。
- `historical_index` 必须使用具有真实生效和公开时间的历史指数成分；当前观察快照不能冒充历史。
- 行业中性化只使用信号时点已知且生效的行业分类，可强制仅接受可信历史记录。

## 因子合成

每个因子声明指标代码、正权重和排序方向。高值优先指标例如 `roe:2:desc`；
低值优先指标例如 `debt_asset_ratio:1:asc`。先在同一信号日进行截面百分位排名，
然后按权重计算综合得分。默认 `exclude` 要求所有选定因子存在；
`available` 对有效因子的权重重新归一。

样本上的 Pearson IC 衡量因子与未来收益的线性相关性；Rank IC 先分别计算
因子和未来收益的秩，再进行 Pearson 相关，因此不需要引入额外 SciPy 依赖。
对低值优先因子，IC 自动按方向修正，正值始终代表符合预期的选股方向。

未来收益起点为信号日后的下一交易日开盘，终点为下一调仓信号后的执行开盘。
按预期方向将股票划分为 2–10 个组，分别汇总各组平均远期收益。

## CLI

```bash
uv run ai-quant factor-lab \
  --source local \
  --symbols 600519 000001 601318 000858 600036 000333 600276 601888 002594 300750 \
  --start-date 2015-01-01 \
  --end-date 2025-12-31 \
  --factors roe:2:desc revenue_yoy:1:desc debt_asset_ratio:1:asc \
  --top-n-values 3 5 \
  --rebalance-values quarterly monthly \
  --commission-bps-values 5 10 \
  --quantiles 5 \
  --training-fraction 0.6 \
  --validation-fraction 0.2 \
  --walk-forward-folds 3 \
  --max-combinations 48
```

参数组合数为：`(单因子数量 + 多因子组合数量) × 持仓数量 × 调仓周期 × 佣金口径`。
示例中为 `(3 + 1) × 2 × 2 × 2 = 32`。超过显式限制时任务会在回测前拒绝执行。

添加 `--universe-mode historical_index --industry-neutralization within_industry_rank
--max-industry-weight 0.4 --require-verified-industry` 可将同一实验限制到每个信号日真实
指数成员，在行业内部排名，并约束等权组合单行业持仓数量。缺少可证明的历史分类或
指数成员时直接拒绝执行。当前观察数据仅可用于首次观察之后的 `observed_index` 研究，
报告仍会标记幸存者偏差风险。具体导入步骤见 `docs/universe-neutralization.md`。

## 样本内外纪律

按基准交易日先后分割为训练集、验证集和独立测试集。策略只按照验证集的夏普
比率排序；夏普一致时再比较验证集超额收益。测试集收益不参与模型选择。报告
同时提供不断扩展训练窗口、始终以未来区间测试的 walk-forward 结果。

参数扫描提供研究比较，不保证样本外有效性；在小样本股票池或短研究区间下，
IC 和夏普比率都可能不稳定，应结合覆盖率、年度稳定性及换手率解读。

## Web API 和产物

```text
POST /api/v1/factor-lab
GET  /api/v1/factor-lab
GET  /api/v1/factor-lab/{task_id}
GET  /api/v1/factor-lab-runs
GET  /api/v1/factor-lab-runs/{run_id}
GET  /api/v1/factor-lab-runs/{run_id}/downloads/{filename}
GET  /reports/factor-lab/{run_id}
```

因子实验通过已有持久化任务表记录；M5-B 将数据库升级到 Schema v7，增加行业历史、
指数成分历史和股票池同步批次表。任务内部带
`research_type=factor_lab` 标记，和传统单因子回测列表相互隔离。认证、
排他锁、失败记录、重启恢复和运维审计均沿用现有生产安全机制。

产物位于 `artifacts/factor-lab/<run_id>/`：

- `cross_sections.csv`：Point-in-Time 因子截面和可观察未来收益。
- `ic_series.csv`、`ic_summary.csv`：IC / Rank IC 时间序列与汇总。
- `group_returns.csv`、`coverage.csv`、`yearly.csv`：分组、覆盖和年度稳定性。
- `strategies.csv`、`segments.csv`、`walk_forward.csv`、`curves.csv`：参数扫描、样本分段、滚动验证和各策略净值。
- `factor_lab.xlsx`、`report.html`、`diagnostics.json`、`run.json`：汇总工作簿、HTML 报告、研究约束和运行清单。
