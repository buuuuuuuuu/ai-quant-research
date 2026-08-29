# M8-A 公告与财报文档智能

## 能力与入口

在 Web 工作台进入“数据浏览 → 定期公告 → 查看公告 → 文档智能”。系统支持：

- 公告 PDF 本地归档、SHA-256 校验、幂等处理和不可覆盖版本；
- 原生正文按页解析，文本不足的页面可用 OCR 兜底；
- 带页码全文搜索、页正文阅读和本地 PDF 查看；
- 单文档引用式问答；
- 同一股票最多六份财报的多期或修订版本比较；
- 持久化后台任务进度、失败原因和运维审计。

财务指标仍来自 Point-in-Time 发布表；PDF 正文是补充证据，不能覆盖结构化指标或真实
公开时间。只有 `published_announcement` 的公告可以归档。

## 服务器准备

原生解析优先使用可选的 `pypdf`，未安装时使用 Poppler。OCR 需要 Poppler 和 Tesseract：

```bash
sudo apt-get update
sudo apt-get install -y poppler-utils tesseract-ocr tesseract-ocr-chi-sim
```

应用的 `ai-quant` 系统用户必须能写 `/var/lib/ai-quant/data/documents`。发布后初始化迁移：

```bash
cd /opt/ai-quant/app
sudo -u ai-quant env \
  AI_QUANT_DATA_DIR=/var/lib/ai-quant/data \
  AI_QUANT_DUCKDB_PATH=/var/lib/ai-quant/data/catalog.duckdb \
  .venv/bin/ai-quant data init
```

`data status` 应显示 `schema_version: 11`，并列出：

```text
announcement_document
document_page
document_chunk
document_extraction
document_question
document_answer_citation
```

环境变量见 `deploy/systemd/ai-quant.env.example`。默认
`AI_QUANT_DOCUMENT_ANSWERER=rules`，完全离线并返回抽取式答案。配置 `openai` 时必须同时
提供 `OPENAI_API_KEY`；请求只包含检索到的正文块，使用严格 JSON Schema、禁用存储，并在
服务端逐字验证每个引用。

## 安全边界

- 只下载公告表保存的 HTTP(S) URL，拒绝凭据、非法端口和解析到内网、环回、链路本地或
  保留地址的主机；每次重定向都重新校验。
- 流式下载限制文件大小，并要求 `%PDF-` 文件魔数；归档采用临时文件和原子替换。
- 本地文件下载只允许数据库记录且位于文档根目录内的路径，并重新核对 SHA-256。
- PDF/OCR 命令使用固定参数数组，不经过 shell；页数、任务并发和运行时间有上限。
- 文档正文是不可信数据。正文里的“忽略规则”“执行命令”等内容永远不能成为系统指令。
- AI 不持有 SQL、Python、文件或网络工具；`answered` 必须至少有一条通过逐字校验的引用，
  否则降级为 `insufficient`。
- Web 所有正文、搜索结果、模型答案和引文均经过 HTML 转义；本地 PDF 响应带 `nosniff`
  和沙箱策略。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/documents/announcements/{id}/ingest` | 启动归档和解析后台任务 |
| GET | `/api/v1/documents/announcements/{id}` | 查看公告所有本地版本 |
| GET | `/api/v1/documents/securities/{symbol}` | 查看股票的可比较文档 |
| GET | `/api/v1/documents/{document_id}` | 查看文档元数据 |
| GET | `/api/v1/documents/{document_id}/pages/{page}` | 查看指定页正文 |
| GET | `/api/v1/documents/{document_id}/search?q=...` | 全文检索 |
| GET | `/api/v1/documents/{document_id}/file` | 校验后查看本地 PDF |
| POST | `/api/v1/documents/questions` | 单份或多份文档引用式问答 |
| POST | `/api/v1/documents/compare` | 多期/修订版本比较 |
| GET | `/api/v1/ops/tasks/{task_id}` | 查询后台进度 |

## 验收

1. 打开一条带原文 URL 的定期公告并点击“归档并解析”。
2. 后台进度完成后确认版本、页数、解析方式、摘要和本地 PDF 可见。
3. 搜索财务关键词，点击结果能跳转到正确页正文。
4. 提问后每项引用都能打开相同页，且引文逐字存在。
5. 选择两期或两个修订版本比较；证据不足时显示 `insufficient`，不生成无引用结论。
6. 重复归档相同 PDF 不增加版本；来源内容改变时增加版本且保留旧文件。

自动测试覆盖归档幂等、版本、URL/文件安全、解析、OCR、搜索、引用校验、提示注入文本、
OpenAI 伪造引文降级、后台进度、Web API 和文件响应安全头。

## 并发、任务租约与备份协调

- 所有 DuckDB 打开都经过同一数据目录下的跨进程访问锁；等待有 30 秒上限，超时会返回
  可诊断错误，不会无限阻塞。该边界适用于多线程、多个 Uvicorn Worker 和独立服务进程。
- 文档归档在短时数据库锁内重新查重、分配 `version_no`、插入记录并发布内容寻址文件；
  下载和 OCR 不持有此锁。临时文件总会清理，事务失败时不会保留未登记的新 PDF。
- 同一公告同时只能拥有一个 `document_ingest` claim。重复请求返回已有 `task_id`；成功后
  继续幂等返回该任务，失败或超过 30 分钟未续租后才允许创建新任务。
- 任务状态由 `owner_id` 条件更新，progress 会刷新 heartbeat；旧 worker 的迟到成功或失败
  不会覆盖终态。应用启动只回收已过期任务，不会无条件判死其他 Worker 的活动任务。
- 页面解析通过 `active_extraction_id` fencing。新租约接管后，旧解析不能删除或覆盖页面、
  分块和文档状态。
- 数据库备份在 checkpoint、文件复制、临时库校验、原子发布及备份登记的完整区间持有同一
  数据库访问锁；文档写入会等待复制完成，得到的是一致快照。

锁顺序固定为：长任务需要时先获取 `.operations.lock`，再短时获取 DuckDB access lock；
文档下载/OCR 不获取 `.operations.lock`，只在数据库读写和发布文件时短时获取 access lock。
