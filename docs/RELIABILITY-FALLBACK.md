# 依赖熔断、准入预算与降级

P14 通过云端固定镜像隔离验收：64 个后端模块，825 passed、4 skipped。
最终记录在源码之外的 `p14-final-v2/verification.json`。
该记录不代表新能力已上线或真实 MySQL/Redis/向量库容量通过。

## 配置和共享控制面

迁移 `0025_dependency_controls` 建立数据库共享的熔断及准入表。
Provider、工具、嵌入、向量查询与 Redis 使用不同 domain。
键绑定 domain、tenant、provider、model、endpoint 的 SHA-256，
状态表不保存 endpoint 或密钥原文。

默认保持开关关闭：

```dotenv
EIC_DEPENDENCY_CONTROLS_ENABLED=false
EIC_ADMISSION_ENABLED=false
EIC_CIRCUIT_FAILURE_THRESHOLD=5
EIC_CIRCUIT_WINDOW_SECONDS=60
EIC_CIRCUIT_OPEN_SECONDS=30
EIC_CIRCUIT_PROBE_SECONDS=210
EIC_PROVIDER_TOTAL_TIMEOUT_SECONDS=180
EIC_PROVIDER_RATE_LIMIT_RETRIES=1
```

启用准入时必须显式设置 global/tenant 的 concurrency、rate、units 六项预算，
缺项拒绝请求而不是静默无限运行。units 为每个 60 秒窗口的保守输入/输出预留，
不是实际上游账单，也不是每日费用上限。lease 到期归还并发容量，不退还未知
请求的速率和成本预留。真实金额预算需要供应商 usage 与价格表另行核算。

## 重试和恢复规则

- 确定未输出的 429 可重试一次，每次也经过准入与熔断。
- timeout、结果未知、部分输出和未知工具副作用不自动重放。
- 半开由数据库选出单个探测者，epoch/token fencing 拒绝迟到结果。
- 工具权限、审批、Schema 拒绝，以及用户取消和生成器关闭，不污染健康状态。
- 缓存命中不充当成功探测，旧模型与未绑定模型不被选为候选。
- Redis enqueue 失败保留 Outbox 意图和稳定任务 ID；向量失败可以使用关键词路径。

三独立进程的单 Half-Open 探测者已用合成 SQLite 验证，
不以此替代真实 MySQL、多机网络分区或生产吞吐测试。

## 多级兜底

既有备用模型只从应用绑定白名单选择。AIApp 可以显式开启：

```json
{"ragFallbackEnabled":true,"ruleFallbackEnabled":false}
```

只有模型确定未输出且无工具活动时，才可能进入本地兜底。RAG 摘录重新核验
租户、应用权限/配置、知识库、文档删除、共享文件、源 revision 和索引版本，
读取数据库当前正文并只引用实际使用的 chunk。字段为 `source_name`，
不能使用不存在的 `KnowledgeDocument.name`。

兜底内容明确显示“模型暂不可用”和“证据摘录”，不伪装成推理答案。
没有当前授权证据时不泄露缓存正文。规则提示只说明未生成答案。
`degraded`、`fallbackType`、`providerInvocations`、`retryCount`、
`providerOutcome` 与依赖尝试摘要进入运行记录，错误以稳定中文消息输出。
