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

`data status` 应显示 `schema_version: 10`，并列出：

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
