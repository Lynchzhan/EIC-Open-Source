# v3.100.0 官方依据与未冻结事项

增量读取日期：2026-10-16。唯一参考目标为 **v3.100.0**，配套 A/B 固定的 legacy ingestion API。此前 main Compose/当前 v4 文档只属历史调查，不再作为版本或变量兼容依据。以下均为本轮实际读取的官方标签源码/仓库文档；标签仍需绑定 commit/发行镜像，不能代替 digest 证据。

| 已读取官方来源 | 实际确认的内容 / 本交付映射 |
| --- | --- |
| https://raw.githubusercontent.com/langfuse/langfuse/v3.100.0/docker-compose.yml | Docker Hub Web/Worker :3、Docker Hub MinIO；PG 默认 latest、CH 无 tag、Redis :7；拓扑、变量、卷路径、CH UID/GID、MinIO sh 建桶/mc ready local |
| https://raw.githubusercontent.com/langfuse/langfuse/v3.100.0/README.md | 该版仓库文档：self-host、创建项目/API 凭据、public API/OpenAPI 入口、TELEMETRY_ENABLED=false；站外链接可漂移，未把当前落地页视为历史文档 |
| https://raw.githubusercontent.com/langfuse/langfuse/v3.100.0/web/src/env.mjs | Web NEXTAUTH/SALT/ENCRYPTION_KEY/CH；media 单 endpoint schema；未找到 Agent/internal media 两项 |
| https://raw.githubusercontent.com/langfuse/langfuse/v3.100.0/worker/src/env.ts | Worker CH DB/单节点与 media 单 endpoint schema；未找到上述两项 |
| https://raw.githubusercontent.com/langfuse/langfuse/v3.100.0/packages/shared/src/env.ts | 共享 Redis/CH/加密/event/media schema；CLICKHOUSE_DB=default；ENCRYPTION_KEY 64 hex 长度说明；无 media INTERNAL_ENDPOINT |
| https://raw.githubusercontent.com/langfuse/langfuse/v3.100.0/web/src/pages/api/public/ingestion.ts | POST、ApiAuthService 项目鉴权、batch 数组及可空 metadata 校验、processEventBatch、207 返回；仅证实路由实现存在，不替代 A/B OpenAPI 字段核对或运行验证 |

保留变量由 **v3.100.0** Compose 的实际键确认：`NEXTAUTH_URL`、`NEXTAUTH_SECRET`、`DATABASE_URL`、`SALT`、`ENCRYPTION_KEY`、`TELEMETRY_ENABLED`、`LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES`、`CLICKHOUSE_MIGRATION_URL`、`CLICKHOUSE_URL`、`CLICKHOUSE_USER`、`CLICKHOUSE_PASSWORD`、`CLICKHOUSE_CLUSTER_ENABLED`、`REDIS_HOST`、`REDIS_PORT`、`REDIS_AUTH`、`REDIS_TLS_ENABLED`、`LANGFUSE_S3_EVENT_UPLOAD_{BUCKET,REGION,ACCESS_KEY_ID,SECRET_ACCESS_KEY,ENDPOINT,FORCE_PATH_STYLE,PREFIX}`、`LANGFUSE_S3_MEDIA_UPLOAD_{BUCKET,REGION,ACCESS_KEY_ID,SECRET_ACCESS_KEY,ENDPOINT,FORCE_PATH_STYLE,PREFIX}`、`LANGFUSE_S3_BATCH_EXPORT_ENABLED`。`CLICKHOUSE_DB` 另由该标签 Web/Worker/shared schema 确认。

修正：Web/Worker 改为 Docker Hub `langfuse/langfuse`、`langfuse/langfuse-worker`，MinIO 改为 `minio/minio`；仍强制 @sha256，不复制浮动值或弱密码。移除该标签 Compose/Web/Worker/shared 配置未找到的 `LANGFUSE_IN_APP_AGENT_ENABLED` 与 `LANGFUSE_S3_MEDIA_UPLOAD_INTERNAL_ENDPOINT`。Web media 为浏览器 loopback，Worker 为内部 MinIO，未证明媒体完整流程可用；仅提供 metadata-only legacy ingestion 参考，不能宣称 v4 双 endpoint 能力。

`RUN_ID`、`OBJECT_BUCKET`、`*_SHA256`、`PG_PASSWORD`、`CH_PASSWORD`、`REDIS_PASSWORD`、`S3_ACCESS_KEY`、`S3_SECRET_KEY`、`WEB_PORT`、`OBJECT_PORT` 是本目录模板接口，不宣称是 Langfuse 原生变量。Compose 映射后才传入各服务。

本方案沿用该标签 MinIO 目录建桶/root 凭据，仅用于全隔离实例，不声称 S3 最小权限。专用 Redis 卷、AOF、noeviction、完整停机冷备份/恢复为本交付运维设计，不是该发行版官方已演练保证。

## 逐项发行版绑定门槛

| 项 | 必须补足的证据 |
| --- | --- |
| WEB_SHA256 | Docker Hub Web digest 对应 v3.100.0 的发行标签/构建来源和平台，支持该 legacy API；只属于 :3 不足 |
| WORKER_SHA256 | Docker Hub Worker digest 对应同一 v3.100.0 发行版/平台，并与 Web 配套 |
| PG_SHA256 | 与 v3.100.0 兼容的确定版本/digest、迁移及数据路径证据；该版 Compose 默认 latest 不构成锁定 |
| CH_SHA256 | 与 v3.100.0 兼容的确定版本/digest、schema/权限/迁移证据；不沿用 v4 CH25.12 |
| REDIS_SHA256 | Redis 7 系列确定版本/digest，队列/AOF/noeviction 与 v3.100.0 配套证据 |
| MINIO_SHA256 | Docker Hub MinIO 确定版本/digest、sh/minio/mc、建桶/S3 行为配套证据；不采用 Chainguard 依据 |
| HELPER_SHA256 | Alpine 确定版本/digest/平台及 tar 数字 UID/GID/权限保留、配套存储恢复证据；helper 非 Langfuse 官方依赖 |

没有取得任何 digest 或该标签对应 commit 的额外证据。PG/CH/MinIO 在该标签 Compose 未固定版本，不能推断 PG17/CH25.12 属于其官方配套。格式校验不证明发行版绑定，manifest reference_release 仅为参考声明，不能补齐供应链证据。七项仍为空，任何一项缺证保持不可部署，未完成版本冻结。

本轮仅官方 GET 和本地人工文本复审，未执行 Docker、脚本、AST、测试、产品 import 或业务请求。未验证镜像/发行版绑定、平台、Compose 解析、存储迁移、网络、健康、备份/恢复及 legacy API 端到端行为。历史 v4 资料不得用来补这些缺口。

## 运维源码风险增量（2026-10-16，仅人工静读）

本次仅修改既有 `ops.py`、`README.md` 和本文件，未新增文件，未修改公共入口或目录外报告。没有新文档请求，没有 Docker、脚本、AST 或测试执行。

`subprocess.run` 统一设置普通命令 60 秒、精确 ID 分批停止 600 秒、单卷冷 tar 3600 秒超时；`TimeoutExpired` 转换成固定安全提示且不透传命令/输出。超时后 daemon/helper 状态未知，停止后续流程并保留部分产物；helper 唯一名字在调用前输出并取消 --rm，成功/失败均留容器供人工检查，不自动清理、重启或重试。这是等待上限设计，不是已运行超时验证或整个流程硬时限。

抽取 `require_target`，backup 与独立 stop 同用：六服务完整性、项目标签、精确镜像引用、显式环境、完整卷挂载、五卷归属标签及非目标运行占用。stop 每批重新核对同一组 ID，仅操作已核对的精确 ID，避免仅同名项目就停止其他对象；任何身份不符拒绝继续，不自动回启此前已停止的服务。

额外静读 restore：固定文件集合/哈希验证不认证归档来源，未验证 tar 成员路径/链接/设备；必须由运维先用外部可信记录核验 manifest，保护备份目录免于并发改写，不接受不可信 tar。全新 RUN_ID/全目标卷不存在约束保留；补充每卷创建前复查、默认 local driver、本次唯一 restore-token/项目/卷标签核对与解包前运行占用检查，拒绝幂等 create 复用既有卷。仍依赖无并发 Docker 管理员，不声称原子资源锁或恶意 tar 沙箱。

本次最终字节需由主静读并精确集成；所有部署与恢复未验证边界、v3.100.0 参考目标和 digest 缺证不可部署门槛继续有效。
