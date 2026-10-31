# P11 持久图和生产 Worker 恢复

## 实现边界

真实聊天生产路径使用 `CHAT_AGENT_GRAPH` 的 `DatabaseCheckpointer`，
`thread_id=agent_run_id`，节点持久化采用 `durability="sync"`。
同步的 `plan_chat_execution` 是无副作用规划接口，不冒充生产持久化执行。

`0026_graph_checkpoints` 新增完整 checkpoint 与 pending writes 两张表。
状态通过已有部署密钥加密，密文绑定 Run、namespace、checkpoint ID；
pending writes 还绑定 task ID、index 和 channel。不启用 pickle fallback。
审计表仅保存恢复策略和图快照引用，不写用户正文或密钥。

生产 Worker 同时持有生成租约和 Agent 租约。业务事件、图快照、工具 intent
及审计写入先检查 token、有效期和 Run 身份；失去租约的 Worker 停止写入。
固定锁顺序为 allocator、generation、worker，不在恢复过程中做启动 DDL。

## 崩溃处理

1. 原进程在模型调用前的安全图节点退出后，扫描器检查当前请求、用户状态、
   权限、图版本、快照完整性及最新恢复策略。
2. 仅一个 Worker 能领取同一 Run。它接管原生成租约并从已提交图状态续跑，
   重新加载当前模型配置和权限范围，重新召回可访问资料。
3. 在任何 provider 请求前，必须先提交 `provider_pending` 人工确认策略。
   请求已发出而结果未知时，聊天转失败，Run 转 `manual_review`，不自动重发。
4. 未知工具结果、审批、旧图版本、损坏快照、终态和输入身份变化都不作为
   自动重放凭证。该版本不承诺恢复模型流内部或外部写工具的未知副作用。

## 验证与部署

`test_p11_durable_worker.py` 验证真实编译图、恢复、加密状态、损坏/旧快照
拒绝和重复 Worker；`test_p11_process_recovery.py` 在独立子进程中强制退出
真实聊天 producer，然后由生产扫描器恢复。模型使用协议桩，真实模型质量
需要单独验收，不用这些测试替代。

先在现库副本升级并验证原表列值及上传文件，再做发布准备。最后上线必须
停写、重新快照、验证增量一致性；不要把之前的在线演练副本直接用于切换。
回退使用旧服务、旧镜像及配套的旧库，不对 `0026` 数据库执行盲目降级。

## 可复现构建

保留全部依赖版本、markers、依赖关系及制品 SHA-256，仅将 `uv.lock` 中的
制品下载地址改用清华 PyPI 镜像，解决云端原站大制品下载长时间停滞。
注册表标识仍为 PyPI，校验哈希不放宽；任何镜像内容不匹配都会安装失败。
安装使用固定 `uv==0.8.15` 和 `uv sync --locked --no-dev --no-editable`。
`PIP_INDEX_URL` 只影响 uv 的引导安装，可在构建时显式指定。
运行镜像显式安装 OpenCV/RapidOCR 必需的系统动态库；测试 target 额外安装
便携字体。OCR 原先因字体缺失而跳过的用例必须实际运行，不能把动态库缺失
造成的扫描 PDF/图片入库失败视作可接受的 skip。

完整生产门仍需要源码之外的独立证据，不能把固定集隔离门改名为生产通过。
