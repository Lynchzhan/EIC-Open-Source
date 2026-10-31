# 部署与运维

源码包含 Alembic 数据库迁移。部署前应确认目标数据库版本、备份状态和迁移路径；不要在未核对 schema 兼容性的情况下启动新版本。

本文给出源码中已有部署入口的使用顺序。所有操作都应由部署者先在隔离测试环境验证。

## 生产前检查

1. 从 `deploy/env.example` 生成独立的 `deploy/.env`。
2. 替换所有 `replace-before-deployment` 和 `change-me`。
3. 使用随机且持久的 `EIC_JWT_SECRET` 和 `EIC_SECRET_ENCRYPTION_KEY`。
4. 设置正确的 `EIC_CORS_ORIGINS`，不要使用通配符。
5. 先备份 MySQL、PostgreSQL 和上传卷。
6. 单独执行迁移并记录 Alembic revision。
7. 通过 `/health` 和 `/api/Health/Ready` 后再切换流量。
8. 保存源码归档哈希、镜像 digest、数据库 revision 和备份恢复记录。

`python scripts/init-env.py deploy` 仅供全新部署，生成随机密码与密钥且不覆盖已有 `.env`。已有服务必须保留旧密钥，不应为升级重新初始化。

生产 `deploy/.env` 至少确认：

| 字段 | 要求 |
|---|---|
| `EIC_BACKEND_IMAGE` | 已构建或已获取并校验的后端镜像标签；正式发布宜记录 digest |
| `EIC_FRONTEND_IMAGE` | 已构建或已获取并校验的前端镜像标签；正式发布宜记录 digest |
| `EIC_ENVIRONMENT` | `production` |
| `EIC_BOOTSTRAP_ENABLED` | `false` |
| `EIC_SEED_DEMO_DATA` | `false` |
| `EIC_JWT_SECRET` | 持久的随机密钥 |
| `EIC_SECRET_ENCRYPTION_KEY` | 持久的独立随机密钥；恢复旧数据时使用旧值 |
| `EIC_CORS_ORIGINS` | 实际使用的浏览器入口 origin |
| `EIC_BOOTSTRAP_ADMIN_PASSWORD` | 仅新空库初始化需要；完成后清空 |

数据库密码被 Compose 拼进连接 URL；初始化脚本生成的十六进制密码可直接使用。若自行设置包含 URL 保留字符的密码，应检查连接 URL 编码与数据库密码是否一致。

## 开发编排

```bash
python scripts/init-env.py deploy
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up --build -d
docker compose --env-file deploy/.env -f deploy/docker-compose.yml ps
```

开发编排入口在 `8080`。它会创建数据库与队列容器，不需要再在主机安装这些数据库。

## 生产镜像准备

生产 Compose 必须提供镜像变量，直接执行 `up` 而未设置变量会报错。新发布者可以先本地构建自己的镜像，例如：

```bash
docker build -t eic-backend:0.1.0-source backend
docker build -t eic-frontend:0.1.0-source frontend
```

再在 `deploy/.env` 填写对应标签：

```dotenv
EIC_BACKEND_IMAGE=eic-backend:0.1.0-source
EIC_FRONTEND_IMAGE=eic-frontend:0.1.0-source
EIC_ENVIRONMENT=production
EIC_BOOTSTRAP_ENABLED=false
EIC_SEED_DEMO_DATA=false
```

这些标签仅为部署者自建镜像示例，不代表存在已发布的镜像或已经通过功能验收。构建会安装镜像内部依赖，由部署者自行决定何时执行。

## 空库部署与首次管理员初始化

先启动基础服务并迁移：

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.prod.yml up -d mysql postgres redis
docker compose --env-file deploy/.env -f deploy/docker-compose.prod.yml run --rm migrate
```

生产 backend/worker 在 Compose 的 `environment` 中强制设置 `EIC_BOOTSTRAP_ENABLED=false`。只修改 `.env` 为 `true` 不会覆盖它。仅在确认目标是全新空库且 schema 就绪后，用一次性容器显式覆盖开关，调用已有初始化函数：

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.prod.yml run --rm --no-deps \
  -e EIC_BOOTSTRAP_ENABLED=true -e EIC_SEED_DEMO_DATA=false \
  backend python -c "import asyncio; from eic.main import bootstrap_empty_environment; asyncio.run(bootstrap_empty_environment())"
```

初始用户名来自 `EIC_BOOTSTRAP_ADMIN`，默认 `admin`，租户代码为 `1000`。该函数不会用新的 bootstrap 密码覆盖已存在的管理员，也不是恢复旧库账号的工具。

启动应用与反向代理：

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.prod.yml up -d backend worker frontend nginx
docker compose --env-file deploy/.env -f deploy/docker-compose.prod.yml ps
```

生产默认入口在 `80`。当前 Nginx 配置不提供 TLS 证书；公开使用前，应自行配置 HTTPS 入口、访问策略和防火墙。

登录后修改初始密码，清空 `.env` 中的 bootstrap 密码，保持开关关闭，不改 JWT 或加密密钥。

## 已有服务升级

保留旧 `.env` 与数据卷，不执行空库初始化。先项目级备份与恢复验证，再单独迁移；确认 schema 和应用兼容后启动新镜像。数据库、上传文件和向量索引应有对应的一致性记录。

升级登录撤销机制后，旧登录可能需要重新认证。保留原 JWT 与数据加密密钥，不通过重建账号或重置密钥处理迁移。记忆索引回填和可选 PG 投影的启用顺序见[记忆索引说明](MEMORY-INDEXING.md)。对状态未知的外部投递，应先人工核对远端结果，不要盲目重发。

不要在迁移未验证时删除旧容器、旧镜像或旧数据卷。

## 观测

- 存活检查：`GET /health`
- 就绪检查：`GET /api/Health/Ready`
- 应用日志：API、worker、migration 容器日志
- 数据库：迁移 revision、连接池错误、锁等待、失败事务
- 队列：Redis/ARQ 队列长度、失败任务和重试
- 文件：上传卷容量、未完成 reservation 和索引状态

检查服务健康后，仍需测试登录、租户隔离、上传、知识召回、模型流式响应、任务恢复和消息已读状态。健康端点或构建通过都不代表这些业务已通过。

## 容量与备份

源码归档不包含数据卷。服务器清理时先区分旧镜像、构建缓存、日志、可重建依赖和必须保留的数据；数据库、上传文件、当前镜像及最近可验证回滚备份不能按“垃圾文件”处理。

备份至少覆盖业务数据库、向量数据库、上传卷和原配置密钥，密钥应与普通公开报告分开保存。恢复演练应检查数据库 revision、数据完整性、文件哈希和实际登录访问。保留策略由部署者按恢复目标和可用容量确定。

## 回滚

回滚必须同时考虑代码、数据库 schema、上传文件和向量索引。优先使用保留的旧镜像与数据库备份；不要仅回滚前端容器。若新版本已执行不可逆迁移，应先评估向后兼容性，再决定是应用回滚还是前向修复。

不能通过删除卷后重建来代替回滚。`scripts/recovery_snapshot.py`、`release_manifest.py` 和 `release_deploy.py` 提供已有发布辅助入口，使用前需查看参数和部署范围；源码交付不附带旧云端备份，也不替代恢复演练。
