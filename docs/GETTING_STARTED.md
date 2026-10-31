# 安装与首次运行

## 1. 环境要求

这是给后续部署者的操作说明。仅整理、阅读或开源源码时，不需要安装任何依赖或启动服务。选择一种运行方式，不必全部搭建。

| 组件 | 本地模拟 | 完整部署 |
|---|---|---|
| Python | 3.11+ | 3.11+ |
| Node.js | 20+ | 20+ |
| SQLite | 内置 | 可选 |
| MySQL | 不需要 | 8.4+ |
| PostgreSQL/PGVector | 不需要 | PostgreSQL 16 + pgvector |
| Redis | 不需要 | Redis 7+ |
| Docker | 不需要 | 推荐 |

## 2. 本地模拟环境

模拟环境适合首次体验，数据写入项目内的 `data/`，上传文件写入 `data/uploads/`。

```powershell
python -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -e ".\backend[dev]"
cd frontend
npm ci
npm run build
cd ..
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-sim.ps1
```

以上命令均从仓库根目录开始，`pip` 只指向项目内虚拟环境。前端必须先构建；`start-sim.ps1` 不构建前端，也不会默认安装后端依赖。

访问 `http://127.0.0.1:8080/login`，登录租户 `1000`、账号 `admin`。初始随机密码在 `data/admin-password.txt`。模拟示例不是生产数据，也不能用来证明真实模型调用成功。

停止服务时按 `Ctrl+C`。清空模拟数据前先备份整个 `data/`，因为其中包含数据库、`simulation.env` 密钥、上传文件和管理员密码。已有数据库时不要重新生成或替换加密密钥。

Linux/macOS 可以先运行 `cd frontend && npm ci && npm run build && cd ..`，再显式执行 `EIC_INSTALL_DEPENDENCIES=true bash scripts/start-sim.sh`；后续已具备依赖时执行 `bash scripts/start-sim.sh`。

## 3. 开发环境

准备好项目虚拟环境、后端依赖和 `frontend/node_modules` 后，从根目录执行：

```powershell
python scripts\init-env.py backend
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-windows.ps1
```

另开终端：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-frontend.ps1
```

前端代理由 `frontend/vite.config.js` 指向 `http://127.0.0.1:8080`。

脚本会先切换到 `backend/` 并执行迁移。若需要单独迁移，在根目录运行：

```powershell
cd backend
.\.venv\Scripts\python.exe -m eic.core.schema upgrade
.\.venv\Scripts\python.exe -m eic.core.schema check
cd ..
```

`SettingsConfigDict(env_file=".env")` 按当前工作目录加载配置，SQLite URL 也包含相对路径；不要从根目录直接运行 schema 或 uvicorn。开发模式初始密码在 `backend/.env` 的 `EIC_BOOTSTRAP_ADMIN_PASSWORD` 中，初始化脚本不覆盖已有配置。初始化后关闭 `EIC_BOOTSTRAP_ENABLED`。

## 4. Docker 开发环境

```powershell
python scripts\init-env.py deploy
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up --build -d
docker compose --env-file deploy/.env -f deploy/docker-compose.yml ps
```

模型密钥通过登录后的模型管理配置或受控环境配置提供，不应放入公开源码。开发 Compose 包含数据库服务，默认前端入口为 `http://127.0.0.1:8080`。生产配置、镜像变量与空库初始化见[运维说明](OPERATIONS.md)。

## 5. 首次使用流程

1. 在模拟或开发环境登录，修改初始密码。
2. 检查租户、组织、角色和用户权限。
3. 在模型管理中配置自己的模型供应商、模型名称、地址和凭据。
4. 上传允许用于测试的文档，检查知识库处理状态。
5. 创建或配置 AI 应用，绑定模型和知识库，运行固定测试问题。
6. 检查聊天流式输出、权限隔离、引用与任务记录。外部模型请求可能产生费用。

SQLite 体验版不包含完整生产队列和向量基础设施。任务执行与附件开关默认关闭，按业务需要审核后开启。

## 6. 常见检查

```powershell
backend\.venv\Scripts\python.exe scripts\validate_deploy.py
cd backend
.\.venv\Scripts\python.exe -m pytest -q
cd ..\frontend
npm run build
```

`validate_deploy.py` 使用后端依赖中的 `PyYAML`。它检查配置结构，不能证明镜像构建、真实数据库或服务可用。

`scripts/healthcheck.py` 固定检查本机 `8000` 端口；模拟和开发后端默认运行在 `8080`，可使用：

```powershell
python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8080/health').read().decode())"
```

存活检查不等于端到端业务通过。还应查看 `/api/Health/Ready`，并用自己配置的模型和固定输入验证实际请求。

## 7. 常见问题

| 提示或现象 | 排查方式 |
|---|---|
| `缺少 frontend/dist` | 自行进入 `frontend/` 执行 `npm ci` 与 `npm run build`，再启动模拟脚本 |
| `缺少后端虚拟环境` | 确认项目专用 `backend/.venv` 已准备，不要把安装操作指向系统 Python |
| `Database revision is not ready` | 先进入 `backend/`，使用同一配置执行 `python -m eic.core.schema upgrade` |
| `greenlet` 相关错误 | 本源码使用 `SQLAlchemy[asyncio]` 声明依赖，检查部署环境是否按当前声明安装 |
| 管理员不存在 | 空库需要显式 bootstrap；恢复旧库则保留原账号，不能靠新密码覆盖旧用户 |
| 已有数据库但密钥缺失 | 恢复原 `simulation.env` 或 `.env`，不要重新生成密钥 |
| 模型返回错误 | 检查供应商地址、模型 ID、凭据、额度和实际响应；`/health` 成功不能代替模型调用 |

实际模型、外部数据库和第三方服务的可用性取决于部署者自己的配置与验证。
