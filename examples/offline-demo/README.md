# 独立离线示例 / Isolated offline demo

从仓库根目录运行（先按首页安装依赖）：

```bash
uv run python examples/offline-demo/demo.py --output .demo/first --serve
```

去掉 `--serve` 只生成产物。已有目录会报错，不会覆盖；再次运行换成 `.demo/second`。服务固定监听 `127.0.0.1`，默认端口 8080，可用 `--port 8081` 修改。按 `Ctrl+C` 停止。无需密码、API Key、行情账户或生产数据库；只适合本机体验。

## 示例是什么

- 一个展示代码 `600519`，名称明确标为“合成演示股票”，不代表贵州茅台真实行情。
- 2024-01-02 至 2024-04-30 的合成工作日历、正弦加趋势价格和合成基准；工作日不等于真实交易所开市日。
- none/qfq 使用相同的合成数值，仅演示双口径存储，不演示真实复权算法。
- 研究从 2024-02-01 开始；此前数据用于 MA5/MA20 预热。
- 单股不做因子排名；MA5 上穿 MA20 生成买入信号，下穿生成卖出信号。实际成交继续由现有引擎处理，未新增交易规则。
- 10 万元初始模拟资金；具体费用、规则及运行参数以生成的 `DEMO.json` 和 `run.json` 为准。

## 去哪里看

打开 `http://127.0.0.1:8080` → 回测中心 → 历史记录 → 示例详情。终端同时输出直接详情链接。

| 输出 | 内容 |
|---|---|
| `DEMO.json` | 合成标识、生成器版本、参数、运行 ID、详情路径 |
| `data/catalog.duckdb` | 只属于本示例的新数据库，含来源标记 |
| `artifacts/daily-signals/<run_id>/` | 具体目录以 `DEMO.json.report` 为准 |
| 运行目录中的 `report.html` | 可独立打开的 HTML 报告 |
| `orders.csv`、`signals.csv`、`daily_nav.csv` 等 | 引擎实际生成的模拟结果及审计文件 |

报告内的通用证券标签可能显示代码对应名称；所有输入和结果仍属于合成示例。不要作为实际股票业绩对外展示。

## 边界

示例生成不联网，不读取 `Settings.from_env()`、`.env` 或已有数据目录。导入的应用仍是完整产品；用户主动使用真实同步、公告核验或 AI 功能可能联网，这些操作不属于离线演示。不要导入私人资料或把本示例服务暴露到公网。

输入序列和策略参数固定；运行 ID、时间戳、路径、代码版本等运行元数据会随执行变化，因此不宣称整个 ZIP 或报告逐字节相同。

## English

Run the command above from the repository root after installing dependencies. Omit `--serve` to generate results only. The output directory must not exist; reuse is rejected rather than overwritten. The server binds to loopback only.

All prices, names and benchmark values are synthetic. The weekday calendar is not an exchange calendar. The demo uses one display symbol, equal synthetic none/qfq series, MA5/MA20 cross signals and the unchanged daily engine. There are no personal records or financial-factor inputs. Inspect `DEMO.json`, the generated HTML report and original CSV/JSON artifacts. Runtime IDs and timestamps vary; input values and parameters are deterministic.

Generation never requests market data or uses deployment settings. The full UI still exposes explicitly confirmed online functions; do not activate them during offline evaluation.
