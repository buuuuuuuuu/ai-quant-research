# M5-B 动态股票池与行业中性化

## 数据与可信边界

Schema v7 在现有目录上新增三张表：

- `security_industry_history`：证券、分类体系、行业名称/代码、生效区间、公开时间、系统可知时间和数据血缘。
- `index_constituent_history`：指数、证券、纳入/剔除区间、公开时间、可选权重和数据血缘。
- `universe_sync_batch`：行业或指数成分的同步/导入来源、可信级别、状态与失败原因。

可信级别只有两种：`verified_history` 表示外部历史文件实际提供了可证明的
`effective_from` 和 `published_at`；`observed_snapshot` 表示当前公开接口返回的观察快照。
当前行业或指数成分不能逆向回填到观察日之前，也不能进入 `historical_index` 或强制可信
行业研究。`knowledge_time` 不能早于 `published_at`，两者都必须严格早于研究时点。

## 升级与状态

```bash
uv run ai-quant data init
uv run ai-quant data status
uv run ai-quant universe status
uv run ai-quant universe build --as-of 2025-12-31 --mode all_available
```

现有 Schema v1–v6 数据库会原地升级，保留行情、公告、财务、持久化任务、告警、备份
和审计。股票池查询同时检查证券上市日期、退市日期以及时点之前是否已有已发布行情。

## 同步当前观察快照

```bash
uv run ai-quant data sync-industries \
  --symbols 600519 000001 601318 000858 600036 000333 600276 601888 002594 300750

uv run ai-quant data sync-constituents --benchmark csi000300
```

行业优先来自 AkShare `stock_individual_info_em`；失败时逐只股票回退到巨潮资讯
`stock_profile_cninfo`，再回退到雪球 `stock_individual_basic_info_xq`。同步结果会显示
实际成功接口、是否使用备用源及仍然失败的证券；逐行数据血缘保留真实来源，不会把备用
结果标记为东方财富。指数当前成员来自 `index_stock_cons_csindex`。
只写入本地证券主数据中已存在的股票；两者都会明确记录为
`observed_snapshot`，仅适用于观察日之后的前瞻研究，不能解释为历史指数成员。
实时同步命令拒绝人为指定过去或未来的观察日期，防止通过 `--as-of` 伪造历史。

## 导入可信历史行业

CSV 必须至少包含：

```csv
symbol,industry_code,industry_name,effective_from,effective_until,published_at,knowledge_time
600519,C,消费,2015-01-01,,2014-12-29 09:00:00,2014-12-29 09:00:00
300750,E,新能源,2018-06-11,,2018-06-08 09:00:00,2018-06-08 09:00:00
```

`symbol`、`industry_name`、`effective_from`、`published_at` 必填；
`effective_until`、`industry_code`、`knowledge_time` 可选，缺少 `knowledge_time`
时默认等于真实公开时间。来源必须指向实际可信历史提供方：

```bash
uv run ai-quant data import-universe-history \
  --dataset industries \
  --file /absolute/path/industry-history.csv \
  --classification-system provider \
  --source-name official:industry-history
```

## 导入可信历史指数成员

```csv
symbol,effective_from,effective_until,published_at,knowledge_time,weight
600519,2015-01-05,,2014-12-30 17:00:00,2014-12-30 17:00:00,0.04
300750,2018-12-17,,2018-12-03 18:00:00,2018-12-03 18:00:00,0.02
```

`symbol`、`effective_from`、`published_at` 必填；`effective_until`、`knowledge_time`
和 `weight` 可选。离开指数的历史成员必须填写实际生效结束日；不得从当前成员列表反推
过去成员。只有使用真正的历史变动文件时才可声明 `official:*` 来源。

```bash
uv run ai-quant data import-universe-history \
  --dataset constituents \
  --benchmark csi000300 \
  --file /absolute/path/index-history.csv \
  --source-name official:index-history

uv run ai-quant universe build \
  --as-of 2024-06-30 --mode historical_index --benchmark csi000300
```

## 回测与因子实验

```bash
uv run ai-quant backtest \
  --source local --symbols 600519 000001 601318 000858 600036 000333 600276 601888 002594 300750 \
  --start-date 2015-01-01 --end-date 2025-12-31 \
  --factor roe --top-n 3 --rebalance quarterly \
  --universe-mode historical_index \
  --industry-neutralization within_industry_rank \
  --max-industry-weight 0.34 --require-verified-industry

uv run ai-quant factor-lab \
  --source local --symbols 600519 000001 601318 000858 600036 000333 600276 601888 002594 300750 \
  --start-date 2015-01-01 --end-date 2025-12-31 \
  --factors roe:2:desc revenue_yoy:1:desc debt_asset_ratio:1:asc \
  --top-n-values 3 --rebalance-values quarterly --commission-bps-values 5 \
  --universe-mode historical_index \
  --industry-neutralization within_industry_rank \
  --max-industry-weight 0.34 --require-verified-industry
```

每个信号日都会重新读取当时生效且已公开的指数成员、上市/退市状态和行业分类；先在
行业内部进行方向调整后的百分位排名，再按行业持仓上限构建组合。真实指数历史缺失时
`historical_index` 直接失败；强制可信分类而没有可用行业历史时中性化直接失败。

`explicit` 表示用户指定股票；`all_available` 表示候选范围内有已发布行情的股票；
`historical_index` 表示可信历史成员；`observed_index` 表示首次观察后的成员，仍带有
幸存者偏差警告。持仓采用整数股票数量，因此单行业最多选择
`max(1, floor(top_n × max_industry_weight))` 只，过小股票池可能无法填满目标持仓。

## Web 与研究报告

Web 工作台新增“股票池与行业数据”面板，区分可信历史与当前观察快照，并显示行业覆盖
与可用证券数量。因子实验支持选择股票池模式、行业中性方式、最大行业权重及是否强制
可信历史。因子截面、选择记录、运行清单与 HTML 报告保留行业名称、历史可信级别、
中性化配置和幸存者偏差警告。

```text
GET /api/v1/universe/overview?as_of=2025-12-31
GET /api/v1/universe/build?as_of=2025-12-31&mode=historical_index&benchmark_symbol=csi000300
POST /api/v1/backtests
POST /api/v1/factor-lab
```

API 和 Web 登录认证、后台任务、下载白名单、审计、备份和生产运维沿用现有机制。
