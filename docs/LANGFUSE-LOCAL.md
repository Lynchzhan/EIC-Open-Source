# 自建 Langfuse 接入

EIC 可将已提交的运行观测发送到部署者自己的 Langfuse 实例。Exporter 默认关闭；启用后由 EIC 后端/worker 发起 HTTP 请求，Langfuse 不会自动随 EIC 一起启动。本文描述连接配置，不证明任何 Langfuse 服务已部署或验收。

## 准备 Langfuse

先单独部署并初始化自建 Langfuse，在项目设置中创建 API keys。EIC 当前默认 exporter 使用 Langfuse legacy ingestion：`POST /api/public/ingestion`。`deploy/langfuse/` 中的 Compose 模板只针对其说明中列出的版本契约，镜像摘要尚未绑定和验证，不能直接当作已验证的部署方案。

## 配置 EIC

在受控的本地环境变量文件中设置以下值。不要提交真实 key：

```dotenv
EIC_LANGFUSE_ENABLED=true
EIC_LANGFUSE_BASE_URL=https://langfuse.example.test
EIC_LANGFUSE_PUBLIC_KEY=pk-lf-replace-with-project-key
EIC_LANGFUSE_SECRET_KEY=replace-with-project-secret
EIC_LANGFUSE_UI_BASE_URL=https://langfuse.example.test
EIC_LANGFUSE_PROJECT_ID=replace-with-project-id
```

`EIC_LANGFUSE_BASE_URL` 应填写 origin，不附加 `/api/public/ingestion`、查询参数或片段；EIC 会自行追加 ingestion 路径。非 loopback 地址必须使用 HTTPS，TLS 证书校验保持启用。开发者在同一台机器运行 EIC 与 Langfuse 时，可使用 loopback HTTP；容器里的 `127.0.0.1` 指向当前容器自身，不能用它访问另一个容器。

如果 EIC 在容器内运行，应使用 EIC 容器可以解析并通过 TLS 校验的 HTTPS 地址，或经受控 HTTPS 代理连接自建服务。不要为了连通而把 Langfuse 暴露到公网，也不要关闭 TLS 校验。网络策略应只允许 EIC 到 Langfuse 的必要出口。

将变量注入实际运行 EIC API 和 worker 的环境，并按部署方式重启相关进程。不要把 API keys 放入前端变量、浏览器代码、命令行参数或镜像层。

## 检查与限制

先以合成输入生成少量观测，检查 EIC exporter 状态、Langfuse 项目中的 trace 可见性和敏感字段清理，再决定是否用于真实数据。健康端点成功不能证明 exporter 正常；传输回执也不等于 Langfuse 查询端已可见。

Exporter 只传递 EIC 已整理的观测内容，不保证所有 Agent 阶段、所有失败路径或上游模型内部信息都可见。超时、部分回执和结果未知时遵循持久投递状态处理，不应手工清除 `unconfirmed` 后盲目重发。体验模式是否导出需依照体验实现及部署策略另行确认。

自托管 Langfuse 的数据库、ClickHouse、Redis、对象存储、备份、升级和密钥轮换由 Langfuse 部署者负责。EIC 源码不提供这些服务的数据恢复保证。
