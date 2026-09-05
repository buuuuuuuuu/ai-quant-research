# M9-C：研究数据就绪与确认式补齐

M10-A 个股“准备研究”复用本章机制，参数变化清除旧预检/计划，迟到响应不串股票，已确认任务不取消。
单股回测 top_n=1；因子实验前往原表单选择多股范围。见 [个股工作台](stock-research-workbench.md)。

## 三种状态，不要混用

| 状态 | 含义 | 不代表什么 |
| --- | --- | --- |
| managed_stock active / archived | 是否维护该股票 | 目录存在或 active 不代表行情完整 |
| unavailable / partial / ready | M9-B 基础 none/qfq 行情可用性 | ready 不保证财务、基准或指定研究区间齐全 |
| 研究预检 ready / warning / blocked | 当前参数、股票、时点和规则的诊断 | 下载任务 succeeded 不自动触发研究 |

财务不足不撤销已验证行情，不改股票池准入，不删除已有成员或历史 revision。
定期公告元数据不是财务指标，也不是 PDF 正文。新增补齐不会下载 PDF、OCR、调用模型或交易。

## 操作

1. 在条件选股、财务因子回测或因子实验表单点击“研究数据预检 / 查看补齐计划”。
   使用表单的真实参数，包含指定 pool/revision，或显式股票；省略两者时使用数据库默认池。
   数据浏览的单股“财务预检 / 补齐计划”默认检查截至当天的 ROE；其他指标/历史时点从研究表单指定。
2. 查看逐股票、数据类型、指标及缺失时点摘要。预检只读本地数据，不联网或迁移。
3. 点击“查看补齐计划”，保存冻结输入、管理 generation、依赖和预计分区数，不下载。
4. 核对范围和公网访问提示，点击“确认补齐”。只接受服务端计划 ID、版本和摘要，不接受浏览器替换范围。
5. 页面显示任务、错误、补齐项；可手动刷新、明确确认重试、重新检查冻结参数或查看历史计划。
   原表单参数变更后，重新点击该表单的预检按钮；不会修改已确认计划。
6. 完成后查看新预检。研究仍需独立操作/确认，不能沿用补齐前的 ready 判断。

手动股票范围为空时，页面提示先选择非空股票池或填写代码，不发送请求，也不自动替换为上次股票。
每次切换研究参数或历史计划会清空旧报告与计划；预检等待中、参数错误或请求失败时不能创建/确认补齐。
成功返回的预检即使存在数据缺口，仍可查看补齐计划。历史计划始终按其保存的范围执行；迟到的旧请求
不能覆盖当前报告、错误或按钮状态。已明确确认并提交的后台任务不会因切换页面参数而自动取消。

首期每份计划最多 20 只股票，不自动拆成后续下载批次。预检可检查最多 500 只。
计划中的股票必须已明确纳入管理且 active；缺目录/未纳入/已移出的股票返回原因，不隐式纳入。
Copilot 预检使用同一服务；页面可从其服务端保存的研究参数生成数据计划。模型没有确认或下载工具。
数据补齐确认、研究确认互不替代。原 API 和研究引擎继续可用。

## 就绪规则与 Point-in-Time

- 行情按股票、复权和本地交易日历比较日期集合，不以全表 MAX 或有一行数据判定完整。
  回测检查指定复权及 none（执行价格），共享基准只规划一次。未知上市/日历边界、当天尚未形成的日线
  或没有停牌证据的缺口保留诊断，不提供“强制就绪”。
- 筛选以 as_of 当日结束时点包含式过滤，选择最新整个财报；不能用旧报告的 ROE 填补最新报告缺字段。
  市场条件按实际字段检查最新交易日或 20 个交易日窗口。
- 回测/因子实验沿用本地基准月/季首交易日，且该日之后还存在执行日；财务必须严格早于信号日 15:00。
  因子选择按报告期、真实公开时间和观测时间排序。实验检查所有所选调仓频率及单因子所需样本。
- 指标不机械要求每天/每季都有值，也不填零。沿用原引擎最新已公开报告策略，没有新设固定季度过期规则。
  允许的 exclude / available / show 策略保留，但列出受影响代码、缺失时点数与样例。
  足够的剩余样本可呈 warning/ready；complete 仍为 false。无合格样本或行情未知不能伪装为完整。
- 财务补齐默认从研究开始年份前两年的 1 月 1 日开始（最早 1990），按报告期分区；公告依赖按公开日期。
  这是有界初始尝试，不承诺两年一定足够。报告期落在区间内但后来才公开的报告不能进入早期研究。
- 新补齐要求财务版本自身具有 provider_notice 公开时间；当前数值没有公开时间时，仅找到旧公告仍
  无法证明数值版本，拒绝倒填历史时间。公告依赖提供可追溯核验元数据，但不保证可解除这一阻断。
  旧流程的 matched_announcement 历史记录保留；旧同步流程遇到多条匹配公告也不再猜测最早时间。
  无法匹配、字段缺失、空响应、过期缓存和网络失败分别保留诊断。
  新补齐遇到仅有日期/午夜时间的上游公开时间，保守推迟到当日结束，不能进入该日 15:00 的信号。
- 同一财务公开身份重复取得完全相同指标时不改原版本或原血缘；值/字段集合变化但没有新公开时间时
  拒绝发布该分区，保留旧事实。可靠的新公开时间产生新版本；原始文件按批次/尝试隔离，不覆盖旧文件。

## 任务、重试与锁

新增 `research_data_plan` 保存不可变 input_json、input_sha256、version，动态 items_json/report_json
及确认人、最近任务；复用 `operation_task` 的 sync 类型，spec.dataset=research_data_plan。
version 首期为 1，不提供原地改写接口；改参数需创建新计划并重新确认。

唯一 claim 是 data-plan:{plan_id}:{version}。重复或并发确认复用活动/成功任务；失败/过期释放 claim。
确认、任务提交、审计同事务。每次仓储连接及终态提交验证 owner、30 分钟租约、plan version/摘要、
last_task_id 和所有管理 generation。15 秒心跳，重启保留队列；过期任务明确重试，完成任务不重跑。
移出/恢复/改变管理代数使旧计划失效，需新计划；旧 worker 不能重新激活股票。

固定锁顺序：可选原 operation lock → managed-download 槽 → 数据集下载锁 → DuckDB access lock。
新计划不长期持有 operation lock；与 M9-B 共用单下载槽，按股票串行处理。
行情、指数、公告、财务的同步服务入口统一使用各自跨进程文件锁，包括 CLI 直接调用；争用立即给出
可理解冲突，不能绕过再下载。数据集锁不持有 DB 连接；备份沿用 operation → DB 顺序，可在网络等待期间执行。
原始写入/校验/发布沿用原引擎和短分区事务，不建立新下载引擎。

每项执行前重新预检，已满足项跳过。公告依赖成功后保留，财务仍缺时可能按已确认范围重新抓取；
原始尝试与已发布版本不覆盖。需要重新刷新已完成公告依赖时创建新计划并确认。
任务成功必须补齐项无执行错误且重新预检 complete；部分发布/仍缺字段记失败并保留成功项和报告。
诊断为 unknown 的历史成分/行业约束首期交由原引擎专项核验，不能自动补齐，可能使计划仍不完整。

## API

沿用会话认证；404 不存在，422 非法参数/超量，409 状态/版本/锁冲突。

| 方法与路径 | 用途 |
| --- | --- |
| POST /api/v1/research-data/preflight | `{research_type, parameters}`；type 为 screen / factor_backtest / factor_lab |
| POST /api/v1/research-data/plans | 同上，只保存计划 |
| GET /api/v1/research-data/plans?limit=50 | 有界历史列表（最大 100） |
| GET /api/v1/research-data/plans/{plan_id} | 冻结参数、项、任务与报告 |
| POST /api/v1/research-data/plans/{plan_id}/confirm | `{version, input_sha256, confirm:true}` |
| POST /api/v1/research-data/plans/{plan_id}/retry | 同上，明确重试 |
| POST /api/v1/research-data/plans/{plan_id}/preflight | 只读重查原冻结参数 |
| POST /api/v1/research-data/copilot/{research_plan_id}/plan | 从服务器 Copilot 研究参数创建待确认的数据计划 |

## CLI

```bash
ai-quant research-data preflight --research-type screen \
  --parameters '{"symbols":["600519"],"as_of":"2024-08-30","conditions":[{"field":"roe","operator":"gte","value":1}]}'
ai-quant research-data plan --research-type screen \
  --parameters '{"symbols":["600519"],"as_of":"2024-08-30","conditions":[{"field":"roe","operator":"gte","value":1}]}'
ai-quant research-data list
```

用返回的真实 plan_id、version、input_sha256 执行 `research-data show PLAN_ID`、
`research-data confirm PLAN_ID --version VERSION --input-sha256 SHA --confirm`。
confirm 仅入队，Web 后台会处理；没有 Web 时可显式运行 `research-data work PLAN_ID`。
`research-data retry` 使用相同确认参数；`research-data recheck PLAN_ID` 只重新检查，不下载。
不要原样使用占位符，也不要将密钥写入参数。

## 升级与边界

Schema v14→v15 仅增加空计划表，在既有逐版本事务内执行；重复执行无变化，失败回滚。
不联网、不修改既有行情/财报/文档/事件/股票池 revision/研究/任务/审计。备份包含新表。
升级前按现有停写、备份及验证流程；执行 `ai-quant data init` 后 `data status` 的当前/预期均应为 15。
回滚须先保全升级后数据再按已验证备份恢复，不能让旧程序直接管理新增计划。
部署脚本默认预期版本改为 15；没有改服务监听、认证、systemd 维护范围。
本阶段不扩大任何行情、公告或财务定时维护范围，不连接生产服务器，也不自动部署。
