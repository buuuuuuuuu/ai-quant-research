# 许可与第三方组件 / Licensing and third-party components

审查日期：2026-10-22。基线：`f4e1b881bd7da014137db062941d398e90ee7feb`。

## 本项目许可

原创项目代码、文档、示例生成器及合成示例采用 **Apache-2.0**，完整条款见 [LICENSE](LICENSE)，归属见 [NOTICE](NOTICE)。选择宽松许可是为了允许个人研究、开源协作和商业二次开发，同时提供明确的贡献者专利授权条款。再分发需遵守原文中的许可、署名、修改标记等要求；不授予项目名称或标志的商标使用权。

本次没有改动策略计算或数据库 Schema，没有把上游库重新授权为 Apache-2.0。

## 审查范围与证据

检查了 `pyproject.toml`、`uv.lock`、仓库源文件中的第三方归属标记、浏览器静态资源及 Python 3.12/Linux 安装环境中的 distribution metadata/许可文件。仓库未包含 vendored Python 库或大型前端依赖；未发现源文件中声明的其他整项目许可证。本审查不证明所有历史贡献的权利来源，也不是覆盖所有平台二进制的法律认证。

[机器可读清单](docs/dependency-license-inventory.json) 列出 65 条锁定依赖记录（包括不同 Python 条件下的 NumPy 版本），记录版本、元数据许可声明及可用许可文件 SHA-256。安装环境以 `uv sync --extra dev --frozen` 为准。未安装的平台条件分支单独标注，不能当作已检查其二进制。

## 直接运行依赖

| 组件 | 锁定版本 | 上游许可 / 核对位置 |
|---|---|---|
| AkShare | 1.18.91 | MIT；distribution LICENSE / [上游](https://github.com/akfamily/akshare) |
| curl-cffi | 0.16.0 | MIT；含原生组件，打包时保留其组件通知 / [上游](https://github.com/lexiforest/curl_cffi) |
| DuckDB | 1.5.5 | MIT / [上游](https://github.com/duckdb/duckdb) |
| FastAPI | 0.141.1 | MIT / [上游](https://github.com/fastapi/fastapi) |
| Jinja2 | 3.1.6 | BSD-3-Clause / [上游](https://github.com/pallets/jinja) |
| NumPy | 2.5.2；2.4.6 条件分支 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0；以 wheel 内许可证为准 / [上游](https://github.com/numpy/numpy) |
| openpyxl | 3.1.5 | MIT / [项目](https://openpyxl.readthedocs.io/) |
| pandas | 2.3.3 | BSD-3-Clause / [上游](https://github.com/pandas-dev/pandas) |
| PyArrow | 24.0.0 | Apache-2.0；保留原生依赖通知 / [上游](https://github.com/apache/arrow) |
| Pydantic | 2.13.4 | MIT / [上游](https://github.com/pydantic/pydantic) |
| Uvicorn（含 standard extras） | 0.52.3 | BSD-3-Clause；extras 各自许可见清单 / [上游](https://github.com/encode/uvicorn) |

开发依赖 httpx、pytest、pytest-cov、Ruff 及传递依赖在机器清单中逐项列出。`pyproject.toml` 声明的是允许范围；表中版本为当前锁文件，不代表未来升级已通过审查。

## 特别注意的传递依赖与系统工具

- **certifi：MPL-2.0。** 证书来源及 MPL 通知保留在上游包中，不能将其改标 Apache。
- **tqdm：MPL-2.0 AND MIT。** 上游分别为源码及文档等内容声明许可，保留其 `LICENCE`。
- **py-mini-racer / mini-racer、akracer、curl-cffi、NumPy、PyArrow、lxml** 含原生组件或包装第三方引擎。顶层包许可不等于每个嵌入组件的许可；发布容器、独立可执行程序或重新打包 wheels 时，必须收集该具体构建的上游 LICENSE/NOTICE 和适用源码义务。
- **Poppler 工具**（`pdfinfo`、`pdftotext`、`pdftoppm`）是可选外部进程，通常按 GPL 许可分发；**Tesseract** 通常为 Apache-2.0，语言数据和其他系统依赖另有许可。这些不是本 wheel 内嵌内容，离线示例不需要它们。分发包含这些程序的镜像时应检查系统包的版权文件，不能仅随附本项目 LICENSE。
- 浏览器截图中的 CJK 字体来自 Noto Sans SC（SIL Open Font License）；截图流程使用，字体文件和浏览器二进制不纳入本项目分发。

## 数据权利与示例

开源软件许可不授予行情、公告、PDF、财务数据或第三方 API 的访问及再分发权。AkShare 自身的 MIT 许可不能替代各数据提供方条款。自行核对用途、授权、访问限制和来源署名后再使用或分发真实数据。

`examples/offline-demo/demo.py` 生成的价格、日历、名称和回测结果均是合成演示，使用现有引擎计算，不含个人或券商记录。真实证券代码仅作接口演示标识，不表示真实行情或公司背书。

## English summary

Original code, docs and synthetic examples are Apache-2.0. Dependencies retain their upstream licenses. The inventory records the locked Python versions, installed metadata and hashes of available license files; conditional packages and native/system components need platform-specific review before redistribution. In particular, preserve MPL notices for certifi/tqdm and all notices/source obligations of bundled native libraries or optional GPL Poppler tools. This review is not a complete binary SBOM or a guarantee of historical copyright provenance.

Market data and official documents are **not** relicensed by this repository. The offline demo and screenshots contain synthetic inputs only. No credentials, personal databases or production configuration are part of the example.

## 官方参考 / References

- [Apache-2.0 原文](https://www.apache.org/licenses/LICENSE-2.0.txt)
- [Apache 许可使用说明](https://www.apache.org/foundation/license-faq.html)
- [Mozilla MPL](https://www.mozilla.org/MPL/2.0/)
- [Poppler](https://poppler.freedesktop.org/)
- [Tesseract](https://github.com/tesseract-ocr/tesseract)

升级依赖或新增第三方代码时更新本清单；发行物应携带实际所包含组件的许可和通知，本文不能替代那些文件。
