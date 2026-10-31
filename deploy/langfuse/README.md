# 独立 Langfuse 自托管交付（源码，未部署）

唯一参考目标为 **Langfuse v3.100.0**，配套 A/B 已固定的 legacy `POST /api/public/ingestion` 契约。该标签路由源码含 POST、鉴权、batch 校验和 207 返回处理。v4 main 与当前 v4 在线文档不再作为配套依据；不推断 v4 支持该 API，不提供 v4→v3 降级方案。

本目录不被公共 Compose、EIC 启动入口或发布脚本引用。EIC `langfuse_enabled` 默认关闭，由主独占集成。没有执行 Docker、SSH、此目录脚本、AST、测试或业务请求。本材料不能作为部署许可或恢复演练通过证据。

## 镜像与安全前提

所有 `image` 均采用固定仓库加 `@sha256:${...:?}`，七个 digest 空项（含备份 helper）必须填写真实、独立核实的 64 位小写 SHA256。Compose 会拒绝空值；任意非空伪值仍需 `ops.py preflight` 的格式检查，格式通过也不证明来源可信。当前没有核实任何 digest，**未填写、未审核不得部署，未完成版本冻结**。

已读官方 `v3.100.0/docker-compose.yml` 使用 Docker Hub Web/Worker `:3`、Docker Hub MinIO，PG 默认 `latest`、CH 无 tag、Redis `:7`。**该 Compose 自身不能固定 v3.100.0 镜像与依赖组合**；模板不采用这些浮动值。Web/Worker 两个 digest 必须分别证明来自 v3.100.0 发行镜像且配套该 legacy API；PG/CH/Redis/MinIO/helper digest 也必须逐项记录与该发行版的兼容依据、平台、许可证和取证时间。仅填合法 hex 不能证明发行版绑定，脚本无法证明这一点。任何一项缺证均保持不可部署，具体逐项门槛见 SOURCES.md。

不再沿用 v4 main 的 PG17/CH25.12/Chainguard 结论。PG 卷路径保留该标签官方 `/var/lib/postgresql/data`，所选版本必须匹配此布局，不支持 PG18 默认布局。Docker Hub MinIO 需具备该标签 Compose 使用的 `sh`、`minio`、`mc`；helper 需具备支持数字 UID/GID 与权限恢复的 `tar`；均未验证。

已移除该版配置中未找到的 `LANGFUSE_IN_APP_AGENT_ENABLED` 与 `LANGFUSE_S3_MEDIA_UPLOAD_INTERNAL_ENDPOINT`。media 仅保留该标签单 endpoint 变量：Web 为浏览器 loopback，Worker 为内部 MinIO；event endpoint 均为内部 MinIO。本轮仅为不含正文/媒体的 legacy ingestion 提供参考，未证明多模态上传、Web 服务端读取或浏览器 media 流程可用，不能宣称具备 v4 双 endpoint 能力。

`env.example` 无默认凭据。将其复制到受控目录的 env 文件，Linux 权限 600，父目录 700。每项 secret 用密码学随机源独立生成 32 字节 hex，例如每次单独运行 `openssl rand -hex 32`；不得复用、手工编造或将输出提交仓库。脚本只检查格式、明显占位与复用，不能证明熵。`ENCRYPTION_KEY`、`SALT`、`NEXTAUTH_SECRET` 恢复时必须保持原值。

首次 `RUN_ID` 选择全新唯一值，`OBJECT_BUCKET=lf-<RUN_ID>`。Compose project `lf-<RUN_ID>`，独立网络 `lf-<RUN_ID>-private`、五个专用卷 `lf-<RUN_ID>-{pg,ch,chlogs,redis,objects}`；PG/CH 数据库名在专属实例内固定。不得 attach 到公共网络、公共数据库、公共 Redis 或现有业务卷。仅 Web 13000 和对象接口 19090 默认映射 `127.0.0.1`，worker/数据库/MinIO console 不发布宿主端口。端口可更换但不得冲突；Web/MinIO 浏览器地址自动对应端口。

网络 `internal: true` 禁止普通外网出口，遥测、实验功能和批量导出关闭。不配置模型凭据、评估器、MCP 或 webhook；不声称该版支持 v4 内置 Agent 开关。没有外部 TLS 入口、SSO、邮件或生产 HA。环境变量会出现在 Docker inspect 中，Docker 管理权限必须视为密钥权限；宿主磁盘与备份介质需加密。HTTP 仅限 loopback/独立内部网络。本机其他用户仍可能访问 loopback，初始化账号和项目后再由主决定导出配置。

## 供后续授权运维执行的接口

以下仅是操作说明，本轮没有执行。运维脚本仅支持 Linux 本机 Docker Engine、标准 Unix socket 和 default context，Python 3.11+ 标准库，不导入产品代码。命令工作目录为本目录，env 与备份使用绝对路径。

所有子进程有界等待：普通命令 60 秒；每批停止的 CLI 等待 600 秒（容器停机宽限仍为 120 秒）；每个卷的冷 tar 备份/恢复最长等待 3600 秒。这些是单次子进程上限，不是整个流程或本地哈希/文件复制的总时限。捕获 `TimeoutExpired` 后只报告固定安全信息，不展示命令、异常原文、stdout/stderr 或部分输出。

**Docker CLI 超时不保证 daemon 操作或 helper 已停止**，也不保证服务均已停机。立即中止后续步骤，保留部分归档、卷和容器，不自动清理、重启或重试。helper 在启动前输出不含 secret 的唯一容器名，取消 `--rm`；成功和失败都保留 helper 容器以便人工核查。运维须核实该名字对应的容器 ID、镜像、挂载和状态，再决定如何处置，不能直接换 RUN_ID 重试或据 CLI 退出判断后台已停止。

```bash
python3 ops.py preflight --env /secure/langfuse/source.env
docker compose --project-name lf-<RUN_ID> --env-file /secure/langfuse/source.env -f compose.yaml pull
docker pull docker.io/library/alpine@sha256:<HELPER_SHA256>
docker compose --project-name lf-<RUN_ID> --env-file /secure/langfuse/source.env -f compose.yaml up -d
```

必须先解除版本冻结缺口并取得部署授权，再运行上述命令。不要使用环境中的同名变量覆盖 env，清除 `COMPOSE_*` 及本模板同名变量；脚本会过滤它们，直接 Compose 不会。`preflight` 只运行 Compose 配置预检，不证明镜像/健康。首次启动后人工确认六服务健康、存储迁移完成、账号和项目创建、API key 保存进外部 secret 管理；没有预置弱密码或默认管理员。创建 API key 不属于此脚本。应用 API keys 不写入此 env，必须在同一备份窗口按外部密钥清单保全。

## 冷备份与完整一致性边界

1. 主先将 EIC 导出关闭，暂停所有向本 Langfuse 写入的客户端、UI 操作、定时任务和直接 S3 写入，阻止外部自动化重启容器。确认请求已结束。`--writers-paused` 是运维确认，脚本无法证明外部生产者已暂停。
2. 预先拉取核实 digest 的 helper，确认备份盘空间、加密、权限和父目录存在。指定尚不存在的备份目录，脚本拒绝覆盖。
3. 执行以下命令。脚本先核对六服务、镜像配置和卷所有权，依次停止 Web、Worker、四个存储，再检查 ExitCode=0、无 OOM、卷无其他运行挂载。任何失败立即退出，不重启。超过 120 秒被强杀不算干净备份；人工排查后重做。

```bash
python3 ops.py backup --env /secure/langfuse/source.env --directory /encrypted-backups/lf-window-001 --writers-paused
sha256sum /encrypted-backups/lf-window-001/manifest.json
```

最后一条结果需保存在异地可信记录或签名系统；目录内 SHA256 仅检测损坏，不防攻击者同时篡改文件与 manifest。归档复制到异地后逐文件核对 SHA256，再核对外存的 manifest digest。不要在工单、日志或 delivery 中展示 secrets.env。

| 归档 | 一致性与保留内容 |
| --- | --- |
| pg.tar | PG 整个数据目录，含元数据、账号、项目、API key 哈希和 WAL；干净停库物理副本，非逻辑 pg_dump/PITR |
| ch.tar / chlogs.tar | ClickHouse 数据、parts、元数据、内部日志；单节点完整卷，不是集群在线快照 |
| objects.tar | MinIO 整个数据卷，包含隐藏系统元数据、事件与 media；不能只复制 events/ 前缀 |
| redis.tar | 完整 AOF/RDB 与队列状态，`appendonly yes`、`noeviction`；不能当可随意清空的缓存 |
| secrets.env | PG/CH/Redis/S3 凭据、SALT、加密密钥、会话密钥、桶名和 digest；明文仅放加密盘 |
| compose.yaml / ops.py / manifest.json | 精确工具配置、镜像引用、实际容器 image ID、平台、时间与逐文件 SHA256 |

停 Worker 后允许未处理队列保存在 Redis，随后全存储停止才串行归档；不会把运行中的独立 PG dump、CH snapshot 与 S3 sync 拼成“同一时点”。已经向外发送、但尚未本地 commit/flush 的请求不在保证内；上游 EIC 内存队列与业务数据库不在此备份内。本方案不证明零丢失、exactly-once 或可重放完整性。不能丢弃 Redis、替换空 Redis 或仅恢复 PG；否则待处理事件/任务可能丢失或重复。

备份成功后源服务也保持停止。失败留下无有效 manifest 的部分目录，人工检查后使用新目录重做；脚本不删除部分证据。重新启动源需另经运维确认，不能把自动 resume 放进备份 finally。操作期间禁止并发运维、Docker autoheal/外部调度重启或手工写卷；脚本不能对 Docker 管理员建立强制锁。

## 恢复到全新隔离 run_id

manifest 增加 `reference_release=v3.100.0`，仅为参考目标声明，不是实际镜像版本证明。恢复拒绝缺失或不同标记，仍要求 digest、工具和 Compose 完全相符。旧模板备份应保留原工具处理，禁止补标记冒充兼容，禁止把 v4 数据直接交 v3 启动。

1. 从可信外存记录验证 manifest digest，再校验归档。保持目标与源实例外部写入均关闭。恢复环境选择全新 RUN_ID 和未占用的 loopback 端口；保留备份 OBJECT_BUCKET、全部 secret、七项 digest。桶名虽沿用原标识，但存在目标新卷及独立网络内，不连接源存储。
2. 使用备份对应的工具/Compose（脚本会比对自身及 Compose hash），相同 Linux 架构、数据库及镜像 digest。不要在恢复中顺带升级或迁移。目标六服务容器和五个卷必须不存在；脚本拒绝覆盖。归档必须来自可信备份，不能对不可信 tar/manifest 执行恢复。
3. 执行以下命令。脚本验证全部归档 SHA256、平台、密钥、镜像与目标空状态后创建新卷并恢复数字 UID/GID/权限；不启动应用、不改源卷。失败保留目标部分卷，人工调查后换新 RUN_ID，不自动清理或重试覆盖。

tar 信任边界：脚本只校验固定文件名、普通文件/非符号链接和 SHA256；没有逐条验证 tar 成员路径、链接目标或设备文件。可信外存的 manifest digest/签名验证必须先由运维完成，备份目录须防篡改且恢复期间不可被并发修改。helper 以 root 解包以保留存储 UID/GID，不能将此实现当成处理不可信归档的沙箱；不接受外来 tar，即使其与同目录 manifest 自洽。`--network=none`、只读 rootfs 和只读备份挂载不替代归档来源可信性。

新卷约束：先要求目标无容器且五个目标卷全不存在，每卷创建前再次检查；指定默认 local driver，以本次唯一 restore-token 加项目/卷标签在创建后核对，拒绝 Docker `volume create` 隐式复用已有卷，拒绝自定义 driver/options，解包前拒绝运行中的占用。该措施不能锁住拥有 Docker 管理权限的并发操作者；禁止并发运维仍是前提。超时先人工确认 daemon/helper 状态，再考虑新 RUN_ID；不要仅因脚本退出就启动恢复出的服务。

```bash
python3 ops.py restore --env /secure/langfuse/restore.env --directory /encrypted-backups/lf-window-001
```

恢复后的启动与验收需要单独授权。按相同 `--project-name/--env-file/-f` 先 `up -d postgres clickhouse redis minio`，确认数据库可读、无损坏/迁移错误、Redis AOF/RDB 载入成功与对象桶存在；再 `up -d langfuse-web`，最后 `up -d langfuse-worker`。恢复 Worker 会消费旧队列，必须在验收方案中明确预期，不能在比较备份计数前放开消费。

人工比对项目/用户/API key 认证、PG 关键表计数、ClickHouse 已完成时段 trace/observation 计数和抽样父子关联、S3 对象数量/大小/抽样 hash、事件与对象引用一致性、Redis 队列状态及恢复后消费结果。验证加密凭据可解密、原 SALT 下 API key 可认证、会话密钥恢复行为。不得调用真实模型或外部业务系统；必要的合成验收须主另行授权。本轮没有任何这些验收结果。

物理冷备份仅覆盖本机本模板五个 local volumes。外部 S3、KMS、集群 CH、多节点 Redis、远程 Docker、宿主崩溃恢复、跨版本/跨架构恢复不受支持，需各自一致性方案。恢复出的应用数据可能含秘密，隔离环境必须按同级敏感数据保护。

## 关闭与回退

主先关闭 EIC `langfuse_enabled` 并按既有发布流程使 Web/worker 配置生效，再暂停所有外部写入。此目录不修改主配置，也不负责排空 EIC 内存导出队列。

```bash
python3 ops.py stop --env /secure/langfuse/source.env --writers-paused
```

该命令只停止独立项目，保留网络/容器/卷；不调用 `down -v`、不删卷、不清 Redis、不撤销业务主库数据。原卷保持是第一回退点；若候选迁移失败，停止候选，用升级前完整冷备份恢复至新 RUN_ID 与旧镜像，不将迁移后的 PG/CH 直接交旧版本启动。回退后人工核对健康与凭据，主决定是否重新启用导出。

`stop` 与 `backup` 共用 `require_target`：要求恰好六个预期服务，逐项核对项目标签、镜像引用、显式环境、完整卷挂载及五卷项目/卷标签，拒绝额外非模板挂载、卷被其他运行容器使用或任意身份不符。独立 `stop` 也必须满足完整目标，缺失服务不降级为按同名项目盲停。每批停止前重新核对同一组容器 ID，实际向已核对的精确 ID 发出停止，而非交给 Compose 按项目名重新选择。仍不声称能对抗并发 Docker 管理员；后续批次发现变化会停止继续操作，之前已停服务不自动重启。
