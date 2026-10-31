# 长短期记忆与上下文

P13 的运行实现位于 `backend/src/eic/ai/memory_context.py`、
`context_manifest.py` 和 `core/memory_writes.py`，已接入聊天 producer。
验证范围以独立验收报告为准，不把合成测试说成真实模型效果。

## 记忆分层与召回

- 工作记忆：当前问题、检索证据、模型工具决策、工具结果及预算。
- 会话记忆：完整 user/assistant 轮次；`short_term`、`session` 记忆仅用于绑定会话。
- 长期记忆：`long_term`、`fact`、`preference`、`entity`，可跨会话召回。
- 聊天召回始终按租户、用户、Agent、有效期和归档状态过滤。未配置 Agent 时仅召回 `agent_id=null`，不读取其他 Agent 的记忆。
- 冲突先按来源优先级、有效时间、置信度、重要性选赢家，再算关键词及向量 RRF。旧匹配值不能覆盖新事实。
- 每次模型/工具调用前重新核对已引用记忆的内容、期限和冲突赢家。删除、归档或替换后，不继续使用旧上下文。

AIApp `config`：

```json
{"memoryEnabled": true, "agentId": null, "memoryEmbeddingModelId": null}
```

未配置嵌入模型时使用关键词；显式配置租户内启用的 embedding 模型才调用向量能力。
后台索引将记忆向量持久保存在关系库；聊天召回只为当前查询嵌入一次，不逐条重新嵌入候选。
查询输入上限为 64KB，查询嵌入预算为 5 秒；失败时保留关键词路径，模型预算耗尽仍向上返回。
新增的持久词项索引与可选独立 PGVector 候选投影见 [记忆索引与批量观测](MEMORY-INDEXING.md)。
存量未回填时继续有界扫描，默认最多 4096 个候选、扫描预算 2 秒，可通过配置收紧。
这些是当前本地源码合同，实际数据库迁移、服务启用和召回质量须独立验证。
候选被截断会在 `memoryRecall.candidatesTruncated` 明确记录。

## 写入与删除

现有 `POST /api/AIAgentMemory/Save` 保留同步兼容。传入下列字段启用持久异步写入：

```json
{"content":"偏好中文回答","memoryType":"preference","asyncWrite":true,"idempotencyKey":"user-preference-1"}
```

返回 `id`、`sessionId`、`writeJobId`。`pending` 记忆不参与召回。
记忆和 Outbox 意图同事务保存；后台激活和消费收据也同事务保存。
同一用户的幂等键绑定规范化内容；不同内容或已删除对象返回中文 409，不重建删除对象。
后台重新检查所有权、权限、来源和期限。提交中断后保持 pending，可安全重试，不涉及重发模型。
需要启用现有 Outbox dispatcher；数据库存储失败时没有虚假的持久成功保证。

聊天请求只有显式提供 `memoryWrite` 对象且回答完成时才排队写入；不自动推断并保存所有聊天内容。
该路径发生记忆排队错误时，保留已完成回答并记录稳定错误分类，不泄露异常正文。

## 上下文与审计

系统约束和当前问题不可裁剪；可选知识证据及记忆按优先级整段纳入。
旧历史只按完整轮次删除。工具 Schema 计入预算，工具结果可裁为有效 JSON 摘要并保留原文哈希。
`Context Manifest` 保存来源、优先级、估算、内容哈希、保留标识及裁剪原因；
工具回合后的 `workingManifest` 重新计算实际协议占用，逐回合写入 trace。
答案引用列表只包含最终纳入上下文的知识证据。
当前计数为 `utf8_bytes_plus_framing` 保守估算，`exactTokenizer=false`，不是账单 token。

## 验证边界

固定记忆集位于 `backend/tests/fixtures/long_memory_cases.json`，覆盖跨会话、短期作用域、
用户/租户/Agent 隔离、过期、生效时间、归档、冲突、旧值匹配和空记忆。
另有混合召回、删除快照、异步收据、提交故障和上下文预算回归。
这些是合成 SQLite/Mock 证据，不是官方 LongMemEval 分数、真实嵌入召回率或生产容量证明。
