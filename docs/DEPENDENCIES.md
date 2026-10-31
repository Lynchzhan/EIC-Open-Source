# EIC 企智枢依赖清单

本页和 `DEPENDENCIES.json` 是从依赖声明与锁文件整理的静态参考。精确版本以 `backend/uv.lock`、`frontend/package-lock.json` 为准。清单不会安装依赖或启动应用，也不构成完整的许可证或漏洞审查。

## 使用原则

- 后端依赖声明来自 `backend/pyproject.toml`，可复现版本来自 `backend/uv.lock`。
- 前端依赖声明和可复现版本来自 `frontend/package-lock.json`；根目录声明仍以 `frontend/package.json` 为准。
- `DEPENDENCIES.json` 同时记录直接依赖和锁文件中的解析包数量，供审计或自动化工具读取。
- 本文件不是许可证认证，也不代表本机已经具备运行环境。公开发布前应复核实际安装版本的许可证和安全公告。

## 运行前提

- Python：`>=3.11`。
- Node.js：20+，与项目 Vite 版本兼容。
- Docker：仅在采用容器化部署时需要。

## 后端直接运行依赖

| 包名 | 声明范围 | 锁定版本 |
|---|---|---|
| `aiomysql` | `aiomysql>=0.2.0` | `0.3.2` |
| `aiosqlite` | `aiosqlite>=0.20.0` | `0.22.1` |
| `alembic` | `alembic>=1.14.0` | `1.20.0` |
| `arq` | `arq>=0.26.0` | `0.28.0` |
| `asyncpg` | `asyncpg>=0.30.0` | `0.31.0` |
| `cryptography` | `cryptography>=43.0.0` | `50.0.2` |
| `fastapi` | `fastapi>=0.115.0` | `0.142.2` |
| `httpx` | `httpx>=0.28.0` | `0.28.1` |
| `langchain-core` | `langchain-core>=1.0.0,<2.0.0` | `1.6.6` |
| `langgraph` | `langgraph>=1.0.0,<2.0.0` | `1.2.12` |
| `pgvector` | `pgvector>=0.3.6` | `0.5.0` |
| `pymupdf` | `pymupdf>=1.24.0` | `1.28.2` |
| `pdfplumber` | `pdfplumber>=0.11.0` | `0.11.10` |
| `python-docx` | `python-docx>=1.1.0` | `1.2.0` |
| `openpyxl` | `openpyxl>=3.1.0` | `3.1.5` |
| `xlrd` | `xlrd>=2.0.1` | `2.0.2` |
| `Pillow` | `Pillow>=10.0.0` | `12.3.0` |
| `numpy` | `numpy>=1.26.0` | `2.5.3` |
| `rapidocr_onnxruntime` | `rapidocr_onnxruntime>=1.2.3,<1.3.0` | `1.2.13` |
| `jieba` | `jieba>=0.42.1` | `0.42.1` |
| `jsonschema` | `jsonschema>=4.23.0,<5.0.0` | `4.26.0` |
| `rank-bm25` | `rank-bm25>=0.2.2` | `0.2.2` |
| `PyJWT` | `PyJWT>=2.10.0` | `2.15.1` |
| `python-multipart` | `python-multipart>=0.0.20` | `0.0.32` |
| `pydantic-settings` | `pydantic-settings>=2.7.0` | `2.15.0` |
| `qdrant-client` | `qdrant-client>=1.12.0` | `1.19.1` |
| `PyYAML` | `PyYAML>=6.0.2` | `6.0.3` |
| `redis` | `redis>=5.2.0` | `5.3.1` |
| `SQLAlchemy` | `SQLAlchemy[asyncio]>=2.0.36,<3.0.0` | `2.1.3` |
| `uvicorn` | `uvicorn[standard]>=0.32.0` | `0.54.0` |

## 后端开发依赖

| 包名 | 声明范围 | 锁定版本 |
|---|---|---|
| `pytest` | `pytest>=8.3.0` | `9.1.1` |
| `pytest-asyncio` | `pytest-asyncio>=0.24.0` | `1.4.0` |
| `ruff` | `ruff>=0.8.0` | `0.16.10` |

锁文件 `backend/uv.lock` 共解析 111 个包；lock version `1`，revision `3`。完整机器清单见 `docs/DEPENDENCIES.json`。

发布前应使用锁文件在干净环境安装依赖，并单独复核平台标记、制品哈希与第三方许可证。依赖清单中的许可证元数据仅作审查入口。

## 前端直接运行依赖

| 包名 | 声明范围 | 锁定版本 |
|---|---|---|
| `@ant-design/icons-vue` | `^7.0.1` | `7.0.1` |
| `@codemirror/commands` | `^6.11.0` | `6.11.0` |
| `@codemirror/lang-javascript` | `^6.2.5` | `6.2.5` |
| `@codemirror/lang-markdown` | `^6.5.2` | `6.5.2` |
| `@codemirror/language` | `^6.12.4` | `6.12.4` |
| `@codemirror/state` | `^6.7.4` | `6.7.4` |
| `@codemirror/theme-one-dark` | `^6.1.3` | `6.1.3` |
| `@codemirror/view` | `^6.43.11` | `6.43.11` |
| `ant-design-vue` | `^4.2.6` | `4.2.6` |
| `axios` | `^1.7.9` | `1.20.0` |
| `codemirror` | `^6.0.2` | `6.0.2` |
| `dayjs` | `^1.11.13` | `1.11.23` |
| `file-saver` | `^2.0.5` | `2.0.5` |
| `jszip` | `^3.10.1` | `3.10.2` |
| `marked` | `^15.0.6` | `15.0.12` |
| `resize-observer-polyfill` | `^1.5.1` | `1.5.1` |
| `vue` | `^3.5.13` | `3.5.42` |
| `vue-router` | `^4.5.0` | `4.6.4` |

## 前端开发依赖

| 包名 | 声明范围 | 锁定版本 |
|---|---|---|
| `@vitejs/plugin-vue` | `^5.2.1` | `5.2.4` |
| `vite` | `^6.0.5` | `6.4.3` |

锁文件 `frontend/package-lock.json` 使用 lockfile version `3`，共记录 182 个 `node_modules` 解析包。完整机器清单见 `docs/DEPENDENCIES.json`。

## 许可证和发布提醒

- `NOTICE.md` 已注明 PyMuPDF 的 AGPL/商业许可提醒；最终发布方式仍需由项目权利人确认。
- 第三方依赖可能各自采用不同许可证；依赖清单不能替代逐包许可证复核。
- 依赖锁定版本来自当前源码副本，未来更新依赖后应重新生成本文件并记录变更。

