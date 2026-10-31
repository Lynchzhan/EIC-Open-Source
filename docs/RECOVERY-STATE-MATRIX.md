# 恢复状态与事务关系

本表是源码约束与后续故障验证清单，不是故障实测结果。模型调用、工具结果、业务完成和观测投递分别记账；任何单表状态都不能单独证明整轮可安全重放。

| 状态 | 主要入口 | 恢复者与事务要求 | 禁止行为 |
| --- | --- | --- | --- |
| 用户消息、GenerationLease、终态 ChatEvent | `core/generation.py` | allocator 后核验 owner token、有效期，再修改消息和唯一终态事件 | 失去租约的旧 producer 继续写入或调用模型 |
| Agent Run、checkpoint、worker lease | `main.recover_agent_request`、`core/agent_runtime.py` | 同图版本、安全节点和当前权限均成立才接管；不确定结果保留人工核对 | 仅因断线或超时重新执行未知外部调用 |
| 会话预算轮次及调用预留 | `ai/runtime_policy.py` | 恢复者已核验 generation lease 后，同事务关闭原 pending 轮次；新预留前重新 fence | 清零历史费用、释放 unknown 预留、旧档位迟到结果推进新档位 |
| 工具审批与执行意图 | `core/agent_approval.py`、工具执行服务 | 恢复先检查批准范围、执行意图及幂等身份；未知外部效果保持不自动重放 | 用新请求 ID 绕过未确认的原调用 |
| 入库 run、文档版本、向量投影及 outbox | `knowledge/indexing.py`、`core/outbox.py` | 当前输入、版本与 run 租约有效；投影完成后才激活版本 | 把投影写入当作数据库和文件已全部提交 |
| 头像、用户资料、上传预占 | `core/uploads.py`、`core/avatars.py` | 修改资料前取得租户锁及 allocator；外部事务上传复用同一事务；失败回滚后独立核对持久引用再补偿 | 上传服务重新 rollback 丢掉资料修改，或盲删可能已提交的文件 |
| 本地观测与 Langfuse 投递 | `core/observability.py`、`core/langfuse_delivery.py` | 原事件与待投递行同事务提交；按目标及导出身份去重；sending 使用 CAS 租约 | 用远端 trace 可见性替代业务账本，或自动重发未知 POST |
| 登录与流式订阅 | `core/authentication.py` | HTTP、SSE 使用同一密码指纹、sid 撤销及绝对到期时间；持续订阅重复核验 | 改密后沿用旧 JWT，或以刷新 stream cookie 无限延长登录 |

## 后续组合验证点

预算提交后但模型调用前、模型调用已经发出后、业务事务已提交但观测尚未落盘、远端已接收但回执丢失、生产者失联但旧进程稍后返回，均应分别验证。每项记录原 request/run/owner token、租约、消息、预算、执行意图、outbox 和终态事件的前后关系。

当前实现不会通过进程内观测队列证明业务完成。采集队列在本地提交前仍可能丢失；持久投递只覆盖已成功本地落盘且出口有效时登记的记录。头像等文件在进程硬崩溃后的孤儿核对仍需运维检查，不会因本次补偿逻辑就自动获得跨存储原子性。
