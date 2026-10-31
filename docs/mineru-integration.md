# 可选私有 MinerU PDF 入库

本候选默认仍使用 EIC 现有本地 PDF 策略。仅知识库 PDF worker 可选择 MinerU；非 PDF、聊天附件及默认 chunk 行为不变。没有新增数据库字段、公共 API、前端或依赖。本文是独立候选的接入说明，不代表实际 OCR、真实服务或生产验收。

## 固定上游与接口

- 官方仓库：https://github.com/opendatalab/mineru
- 固定 commit：`31e4e3d0a54e5a82d169bdd81b2f3eddb652bb27`，`mineru/version.py` 为 `4.0.11`。
- 使用 `mineru/parser/api_server.py` 的 V1，而非已移除的 `/file_parse`。
- 周期：`POST /v1/uploads`、固定同源 PUT、SHA256 complete、`POST /v1/parse/jobs`、有界 GET 轮询、`GET /v1/files/{id}/content`。
- 只请求 `structured_content`，依据 `mineru/render/content_list_v2.py` 公共门面及 `mineru/render/_internal/content_list/v2.py` 实现的按页 V2 blocks/spans 转文本。任务必须全部 completed、源 file_id 一致、parser_version 精确一致、page_range 覆盖全部本地 PDF 页数，产物长度与声明相符。
- 不使用秒传、不执行 URL source、不下载图像/链接、不使用上游返回的任意上传地址/头、回调、ZIP 或公开解析服务。

## 配置

默认 `EIC_PDF_EXTRACTOR=local`，不联系 sidecar。私有部署完成并验收后，管理员可配置：

```dotenv
EIC_PDF_EXTRACTOR=mineru
EIC_MINERU_ENDPOINT=http://127.0.0.1:8000
EIC_MINERU_ALLOWED_IPS=127.0.0.1
EIC_MINERU_API_KEY=
EIC_MINERU_TIER=basic
EIC_MINERU_OCR_MODE=auto
EIC_MINERU_FALLBACK_LOCAL=true
EIC_MINERU_TIMEOUT_SECONDS=120
EIC_MINERU_POLL_INTERVAL_SECONDS=1
EIC_MINERU_MAX_POLLS=120
EIC_MINERU_MAX_PDF_BYTES=33554432
EIC_MINERU_MAX_RESPONSE_BYTES=16777216
EIC_MINERU_MAX_TEXT_CHARS=2000000
EIC_MINERU_MAX_PAGES=2000
```

`ENDPOINT` 只能是无路径、userinfo、query、fragment 的 HTTP(S) origin，host 为数字 IP，并逐个列入精确 allowlist。只接受 RFC1918、loopback、IPv6 ULA；拒绝域名、公网、metadata/link-local、IPv4-mapped IPv6。禁继承 HTTP proxy，禁重定向，TLS 证书校验保持启用。跨主机建议私有 HTTPS 或受控加密隧道，HTTP loopback 仅同机使用。该限制不会证明私有 IP 服务本身没有向外转发，部署网络必须阻断文档处理进程的公网出口。

Bearer key 使用 `SecretStr`；报告与错误仅保留固定错误码/commit/endpoint hash，不存密钥、原始响应、上传名称或远程路径。密钥从部署 secret 注入，不在命令或仓库中填真实值。

配置变更需要按既有配置缓存机制重启相关 EIC worker。开启 MinerU 时参数进入索引指纹；已有文件不会自动补 OCR，需显式重建/重试。关闭时恢复旧指纹，既有 MinerU 文本重切仍保留可信 page marker。不得把 fallback 成功当 MinerU 成功。

## 失败、权限与引用

- `fallback_local=true`：失败后调用现有本地 extractor，日志、持久化 `extraction_report.extractor` 和 notes 记录 `requested=mineru, used=local, status=fallback, errorCode`。本地结果为空或失败仍失败。
- `fallback_local=false`：worker 保存 `parse_failed:mineru:<code>` 与失败 extraction report；不自动重发收费/外部请求，人工受控重试沿用既有 worker 机制。
- 取消保持原本 `uncertain/interrupted` 语义，同时记录 MinerU 清理状态；总网络周期最长配置 timeout + 最多 5 秒清理。输入 PDF 本地读取/页数校验另计。
- 入库前沿用文件绑定权限、租户与 upload_root 检查；解析后再次校验运行参数、source revision 和共享权限，失效结果不激活。撤销已入库文件共享后，检索继续按现有权限过滤。
- 每页父块/子块都携带 `[第N页]`，包括长页续片；`documentId`、`source_name`、`pageNumbers` 等现有 metadata 与引用结构保留。原文同形页码字符串转成普通文字，防伪造引用。
- 空页记录 `failedPages`，不推断 OCR 质量；全空、未知 block 或不匹配结构直接失败。检索/父块回填只取授权的本地 chunk。

## 私有部署与资源

本次没有安装 MinerU、下载模型、启动实际服务或部署。由运维在独立私有运行环境固定上述 commit 与所有依赖/模型 checksum。上游命令形态为 `mineru-kit api-server --host 127.0.0.1 --port 8000 --tier basic`，此处仅说明部署接口，不在当前机器执行。跨主机应增加受控鉴权/TLS 代理、请求字节限制和网络隔离。

上游 README 的最低要求不是本次性能实测：basic ONNX 可 CPU，模型约 0.8 GB、最低 RAM 2 GB；standard/advanced ONNX + llama.cpp 模型约 2 GB、最低 RAM 8 GB。GPU 高吞吐 `full` 需要至少 RAM 16 GB、Volta 或更新 NVIDIA GPU 至少 8 GB 可用 VRAM，并约多用 5 GB 安装磁盘。操作系统、上传/产物、并发队列和模型缓存需要额外容量。建议先 basic + concurrency=1，测量中文扫描件、表格、公式、多页复杂排版和失败/超时，再定实际容量。

禁止配置 remote parsing、公共 API 或公网对象存储上传；预下载模型后关闭公网出口并显式关闭上游 telemetry。管理员应证明 sidecar 配置没有公共服务 fallback，并做出口审计。本 adapter 的地址检查不替代该部署证明。

### 数据留存边界

adapter 会尽力取消未终态 job、取消未完成 upload，并删除已知源/structured_content 的 API file resource；每项失败继续尝试剩余项，状态为 `cleanup_pending`。进程强杀/网络断开或 API 返回丢失时可能有未知资源，须依靠私有存储清理与审计。

上游 `FileStore.delete_file` 仅移除资源索引，未删除 blob，因此无论 API 清理成功与否都标记 `blobDeletion=not_proven`。`expires_after=3600` 也不是字节删除证明。上游资源索引在内存，重启不保证上传/任务 ID 可用；保留 `upload-dir` 不等于可恢复任务。应隔离私有上传目录，定义容量、TTL、失败任务/孤儿清理、备份和不可恢复擦除策略，并验证并发读写安全。该项未通过前不应发布启用配置。

## 许可判断

EIC 基底为 MulanPSL-2.0；本 adapter 独立编写，不复制或 vendoring MinerU 实现，仅通过 HTTP 协议互操作。初步判断可以保持 EIC 自身许可证，但 MinerU sidecar/模型/依赖的分发与运行义务仍需分别审核，不等于法律意见或发布批准。

固定 commit 的 LICENSE 是 Apache 2.0 加 MinerU 附加条款，不是纯 Apache，也不是旧版本 AGPL。用户及关联方合并 MAU 超过 1 亿，或月总收入超过 2000 万美元，需要商业许可。向第三方提供基于 MinerU 的在线服务须在界面或公开文档显著标明使用 MinerU；未履行可自动终止许可。部署前需审核门槛、对外标识、模型许可证和第三方依赖许可证。此独立候选不修改 EIC LICENSE。

## 验证边界

仓库包含适配器源码和相关契约测试；这些不证明真实 OCR 识别率、MinerU 引擎可用、GPU/模型适配、生产容量或远程数据擦除。部署者应固定上游版本、依赖和模型校验和，并在隔离环境验证文档格式、页码引用、超时、清理和网络出口策略。

本功能默认关闭。启用之前应评估目标主机内存、磁盘、并发队列和上游许可证义务；在线提供基于 MinerU 的服务时，还应遵守上游的显著标注要求。
