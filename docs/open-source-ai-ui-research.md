# 企业 AI 中台前端开源项目调研

> 调研目标：为 EIC（企智枢）寻找可借鉴的企业 AI 工作台、Agent、RAG 和知识库前端。
>
> 许可说明：本文件记录的是公开仓库在 2026-10-05 的初步核验结果。许可证会变更；在复制代码、发布二次开发版本或合并第三方资源前，应再次固定 commit、阅读完整许可证和 NOTICE，并把依赖写入 `docs/DEPENDENCIES.*`。

## 结论先行

EIC 不适合直接嵌入一个完整的 React/Next.js 前端。当前工程是 Vue 3 + Vite + `ant-design-vue`，并且已有权限、路由、SSE、Agent、RAG 和记忆相关 API。更稳妥的路线是：保留 EIC 的业务 API 和页面路由，只移植信息架构、状态表达和交互模式。

首批建议参考顺序：

1. **RAGFlow**：知识库、文档解析、切片、召回结果与引用预览。
2. **BISHENG**：企业 AI DevOps、工作流、Agent 和评测工作台的信息架构。
3. **LibreChat**：多模型对话、引用、工具调用、消息搜索和会话体验。
4. **DB-GPT**：数据 Agent、SQL 结果、图表和运营工作台。
5. **DocsGPT**：文档问答、Agent Builder、深度研究和来源展示。

这些项目适合“借鉴模式”；除非完成逐文件许可审查，不应直接复制其组件源码、图标、图片或 CSS。

## 候选对比

| 项目 | 官方仓库 | 许可初步核验 | 前端技术线索 | 适合借鉴的页面/模式 | EIC 映射 | 集成难度 | 使用边界 |
|---|---|---|---|---|---|---|---|
| BISHENG | [github.com/dataelement/bisheng](https://github.com/dataelement/bisheng) | Apache-2.0（仓库 `LICENSE` 首行） | 前后端分离的企业 AI 工作台，前端包含流程、应用和评测界面 | Flow/应用编排、运行状态、评测与观测 | `AgentManagement.vue`、`MyAITasks.vue`、Harness 页面 | 中 | 可作为 Apache 参考，仍需固定版本和检查第三方依赖 |
| RAGFlow | [github.com/infiniflow/ragflow](https://github.com/infiniflow/ragflow) | Apache-2.0（仓库 `LICENSE` 首行） | `web/package.json` 显示 React、Ant Design、Radix、Lexical、Monaco 等 | 知识库列表、文档处理状态、切片/引用预览、检索反馈 | `KnowledgeBaseManagement.vue`、`RecallTest.vue` | 中 | 不直接把 React 页面嵌进 Vue；借鉴 RAG 信息架构 |
| LibreChat | [github.com/danny-avila/LibreChat](https://github.com/danny-avila/LibreChat) | MIT（仓库 `LICENSE` 首行） | `client/package.json` 显示 React、Headless UI、Radix、Monaco、Sandpack | 多模型会话、消息操作、引用、工具/MCP 状态和搜索 | `ChatView.vue`、`MyAIChat.vue` | 中 | MIT 允许较宽松复用，但保留版权声明并审查依赖 |
| Wanwu | [github.com/wenge-research/wanwu](https://github.com/wenge-research/wanwu) | 主分支 LICENSE 文件未在本次原始文件探测中确认 | 多租户 Agent、Workflow、RAG、模型和发布管理 | 企业租户、Agent 发布、应用生命周期 | `AgentManagement.vue`、租户/权限页面 | 中 | 先人工打开仓库确认当前许可证，当前只作产品/视觉参考 |
| DB-GPT | [github.com/eosphoros-ai/DB-GPT](https://github.com/eosphoros-ai/DB-GPT) | MIT（仓库 `LICENSE` 首行） | `web/package.json` 显示 Next.js、React、MUI、Ant Design Plots、Monaco | 数据问答、SQL 执行过程、表格/图表结果、数据源管理 | Agent 工具结果、审计和运营看板 | 中 | MIT 代码可研究，图标/字体/第三方包需单独核验 |
| AnythingLLM | [github.com/Mintplex-Labs/anything-llm](https://github.com/Mintplex-Labs/anything-llm) | 本次主分支 `LICENSE` 路径未确认，需人工核验 | Workspace、文档上传、RAG 对话和本地模型体验 | Workspace 切换、知识范围提示、上传状态 | 首页工作区、知识库详情、对话上下文条 | 中 | 许可证未确认前只作交互参考 |
| Langflow | [github.com/langflow-ai/langflow](https://github.com/langflow-ai/langflow) | MIT（仓库 `LICENSE` 首行） | 仓库根 `package.json` 未直接反映完整前端依赖，需按前端目录复核 | 可视化节点编排、组件配置、运行调试 | Agent Workflow 编辑器、工具链调试 | 高 | 借鉴节点语义和调试反馈，不直接迁移整套画布 |
| DocsGPT | [github.com/arc53/DocsGPT](https://github.com/arc53/DocsGPT) | MIT（仓库 `LICENSE` 首行） | `frontend/package.json` 显示 React、Tailwind、React Flow、Markdown、图表 | 文档问答、Agent Builder、深度研究、来源和图谱 | PDF 入库、引用面板、Agent 任务页 | 中 | MIT 许可较清晰，仍需记录复制文件的来源 |
| NextChat | [github.com/ChatGPTNextWeb/NextChat](https://github.com/ChatGPTNextWeb/NextChat) | MIT（仓库 `LICENSE` 首行） | 根 `package.json` 显示 Next.js、React、Markdown、会话相关组件 | 轻量聊天、模型切换、会话管理、响应式布局 | `ChatView.vue` 的空态、侧栏、模型状态 | 低 | 适合借鉴聊天交互，不适合承载完整企业后台 |
| MaxKB | [github.com/1Panel-dev/MaxKB](https://github.com/1Panel-dev/MaxKB) | GPL-3.0-or-later 方向（仓库 `LICENSE` 首行） | `ui/package.json` 显示 Vue 3、Element Plus、Vite、VueUse、Vue Router | Vue 生态的知识库、应用编排、发布配置 | EIC 知识库和 Agent 发布页 | 低到中 | 主要做视觉/交互参考；复制 GPL 代码前必须评估整体授权影响 |
| Dify | [github.com/langgenius/dify](https://github.com/langgenius/dify) | 自定义 Open Source License（`LICENSE` 首行不是标准 MIT/Apache） | 多应用、Workflow、RAG、Agent、模型供应商工作台 | 应用创建向导、Workflow、发布和运行观测 | EIC 的 Agent 生命周期和 Harness | 高 | 按自定义许可证逐条核对，不能默认按 MIT 使用 |
| FastGPT | [github.com/labring/FastGPT](https://github.com/labring/FastGPT) | 自定义 FastGPT Open Source License | 知识库、可视化流程、Agent 和聊天工作区 | 知识库测试、流程节点、引用和发布配置 | RAG 流程、召回测试、Agent 发布 | 高 | 只在确认许可条款后考虑代码复用 |
| Flowise | [github.com/FlowiseAI/Flowise](https://github.com/FlowiseAI/Flowise) | 当前主分支 LICENSE 路径未在本次探测中确认 | 可视化节点和工具调试产品 | 节点画布、工具输入输出、运行日志 | Workflow 编辑和 Tool Calling 调试 | 高 | 先核验许可证和版本；优先借鉴交互模型 |
| LobeHub | [github.com/lobehub/lobehub](https://github.com/lobehub/lobehub) | LobeHub Community License（仓库 `LICENSE` 首行） | Agent 工作区、模型/插件、任务和协作体验 | Agent 工作区、任务卡片、模型状态和团队感 | 首页工作台、Agent 列表、任务中心 | 中到高 | 社区许可证不是 MIT/Apache，直接复用前需法律审查 |
| Onyx | [github.com/onyx-dot-app/onyx](https://github.com/onyx-dot-app/onyx) | 仓库 `LICENSE` 首行显示版权声明，本次未判定完整许可 | 企业搜索、知识助手、连接器、权限空间 | 搜索结果、来源、连接器和访问范围 | RAG 检索解释、权限过滤、知识源管理 | 高 | 许可证和依赖需独立确认，当前只作产品参考 |

## EIC 当前界面与参考模式的差距

### 首页 `ConsolePage.vue`

当前是固定侧栏、顶部操作区和管理页面入口，信息密度偏向传统后台。建议改成“企业 AI 工作台”首页：

- 顶部保留全局搜索、租户/用户、通知和运行状态。
- 首屏放置“继续对话”“最近 Agent Run”“知识库索引状态”“待处理任务”四类工作区，而不是只放菜单入口。
- 使用 RAGFlow/AnythingLLM 的 workspace 思路，把当前租户、默认模型和可用知识库作为明确的上下文条。
- 使用 BISHENG/Dify 的应用生命周期语义，区分草稿、测试、已发布和异常状态。

### 对话 `ChatView.vue` / `MyAIChat.vue`

当前 `ChatView.vue` 会把 SSE 事件直接平铺成时间线，已经能展示 `retrieval`、`aIToolsContentMsg` 和 reasoning 类事件，但用户不容易理解它们的关系。建议采用三层消息结构：

1. **回答层**：最终回答、Markdown、复制/重新生成/反馈。
2. **证据层**：引用文档、页码、chunk、相似度/排序分数和权限过滤提示。
3. **运行层**：检索、重排、工具调用、模型 fallback、耗时和 token 的可折叠 trace。

LibreChat/NextChat 适合借鉴对话输入、会话侧栏和模型状态；RAGFlow/DocsGPT 适合借鉴来源面板；DB-GPT 适合借鉴结构化工具结果。

### 知识库 `KnowledgeBaseManagement.vue` / `RecallTest.vue`

当前已有文档数、切片数、索引状态、TopK、阈值和召回测试入口。下一步应增加：

- PDF 上传后的阶段进度：上传、解析、版面识别、切片、embedding、索引、可检索。
- 文档级失败原因和重试按钮，避免只显示一个笼统状态。
- chunk 预览：原文、页码、标题路径、metadata、embedding/稀疏分数、最终融合分数。
- 召回测试的 query、候选列表、过滤原因、rerank 前后排序和命中引用。
- 索引版本、构建时间、当前生效版本和回滚入口。

## 推荐的 Vue 改造顺序

### 第一阶段：工作台骨架

- 复用现有 `ant-design-vue`，不新增 UI 框架。
- 在 `ConsolePage.vue` 增加工作台首屏数据模型和状态卡片。
- 抽出统一的 `StatusBadge`、`RunTimeline`、`SourceCitation`、`MetricCard` 组件。
- 保留原路由和权限检查，确保旧页面仍可访问。

### 第二阶段：AI 对话工作区

- 将 SSE 事件按 `runId` 聚合为回答、证据、工具和 trace 四类。
- 将 `retrieval` 事件转换为来源卡片，并支持展开查看 chunk metadata。
- 将工具调用显示为输入、执行中、成功、失败、重试和 fallback 状态。
- 对长回答和慢模型保留停止、断线重连、已完成片段和中文错误提示。

### 第三阶段：知识库工作台

- 以文档处理 pipeline 替代单一状态标签。
- 提供 PDF 页码预览、解析文本、chunk、召回和引用之间的关联。
- 把 `RecallTest.vue` 变成可解释的检索实验面板，保留 TopK、阈值版本和混合检索分数空间。

### 第四阶段：运行与评测

- 首页增加 Agent Run、checkpoint、恢复、fallback 和预算状态。
- Harness 页面展示测试集、通过率、Recall@K、MRR、nDCG、失败样本和 trace 链接。
- 所有指标注明数据来源和证据范围，避免把本地 Mock 或 SQLite 结果写成生产指标。

## 许可与第三方合规清单

在真正复制任何第三方代码之前：

1. 固定仓库 commit、仓库 URL、文件路径和许可证文本哈希。
2. 将实际复制的文件和改动记录到 `docs/DEPENDENCIES.md`、`NOTICE.md` 或单独的 attribution 文件。
3. 对 GPL、Community License、Custom Open Source License 和未确认许可证项目，默认只借鉴界面行为与信息架构。
4. 检查图标、字体、图片、代码高亮主题和 npm 依赖的独立许可证。
5. 重新运行前端构建、权限回归、SSE 对话、知识库上传和 recall 测试。

## 最终建议

EIC 最值得形成自己差异化的地方不是“换成某个开源项目的皮肤”，而是把现有的 Agent Run、LangGraph checkpoint、RAG 引用、混合检索、记忆、fallback 和 Harness 评测变成可见、可解释、可恢复的产品界面。视觉上可以采用 RAGFlow 的知识库信息架构、LibreChat 的对话密度、BISHENG/Dify 的工作流语义和 DB-GPT 的结构化结果，但实现继续使用 Vue 3 与现有 API，避免引入整套异构前端。
