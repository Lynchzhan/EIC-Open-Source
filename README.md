<div align="center">

<img src="frontend/src/assets/eic-logo.svg" alt="EIC 企智枢" width="112">

# EIC 企智枢

**简体中文** | [English](README.en.md)

**面向企业知识工作的 AI 工作空间**

把知识检索、AI 对话、Agent 执行、组织权限与运行记录放进同一工作空间。

![许可证：木兰宽松许可证第 2 版](https://img.shields.io/badge/license-Mulan%20PSL--2.0-1769AA)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![FastAPI](https://img.shields.io/badge/backend-FastAPI-009688)
![Vue 3](https://img.shields.io/badge/frontend-Vue%203-42B883)

[快速体验](#quick-start) · [产品导览](#product-screens) · [能力概览](#capabilities) · [系统架构](#architecture) · [路线图](#roadmap) · [文档](#documentation)

<img src="docs/assets/readme/云端智能体管理.png" alt="EIC 智能体管理主界面：以卡片集中浏览助手、模型与知识库配置，动态数据已脱敏" width="100%">

**从一个助手开始，连接知识、工具与团队。**

[查看登录与工作空间](#screen-login) · [探索知识库](#screen-knowledge) · [了解 Agent 配置](#screen-agents) · [浏览完整画廊](docs/screenshots/界面导览.md)

</div>

## 项目简介

EIC 面向需要管理内部知识、构建 AI 助手并控制组织访问边界的团队。平台把知识库与检索、模型接入、Agent 工作流、任务执行和管理后台整合在一起，支持本地模拟与自托管部署。

**工作流：** 文档与知识库 → 检索与引用 → Agent 对话或任务 → 运行记录与审计

| 面向使用者 | 面向构建者 | 面向管理员 |
| --- | --- | --- |
| 从对话进入工作，选择助手、查找知识、接收任务消息。 | 配置模型、知识库、技能与上下文策略，组织不同场景的助手。 | 管理组织、访问权限、体验资格与变更记录。 |

<a id="capabilities"></a>

## 能力概览

| 能力 | 说明 |
| --- | --- |
| 知识与检索 | 文档解析、分块、向量化及权限过滤检索；回答可关联来源。 |
| AI 助手 | 多轮与流式对话，支持模型、知识库、技能和工具配置。 |
| Agent 与任务 | 使用 LangChain 与 LangGraph 组织 Agent 流程，并提供后台任务能力。 |
| 企业管理 | 管理租户、组织、用户、角色和权限。 |
| 运行记录 | 提供本地运行记录与审计；可选 Langfuse 导出默认关闭。 |

<a id="product-screens"></a>

## 产品导览

从登录入口到团队管理，沿着八个场景了解 EIC。首页共选用 **21 张截图**，核心画面直接展示，配置细节点击展开；更多页面见[完整界面画廊](docs/screenshots/界面导览.md)。

| 开始使用 | 构建助手 | 管理团队 |
| --- | --- | --- |
| [01 登录入口](#screen-login)<br>[02 对话工作空间](#screen-chat)<br>[03 知识库与文档](#screen-knowledge) | [04 Agent 与记忆](#screen-agents)<br>[05 模型、提示词与工具](#screen-models)<br>[06 自动任务与消息](#screen-tasks) | [07 组织与权限](#screen-access)<br>[08 审计与体验管理](#screen-audit) |

> **画面来源与边界：** 登录页使用维护者提供的原图；其余画面来自已登录的云端 EIC，账号、组织、配置及业务数据已在浏览器呈现层做脱敏处理。云端截图过程未发送对话、调用模型或提交修改；配置入口和界面状态不代表相关外部服务已经验收。详见[截图索引与覆盖说明](docs/screenshots/截图索引.md)。

<a id="screen-login"></a>

### 01 · 从登录开始

统一的登录入口把租户、账号与工作空间连接起来。画面展示密码登录、验证码登录，以及体验演示、限时体验和注册入口；各入口是否开放取决于部署配置。

![EIC 登录页：紫色渐变背景、租户输入、密码与验证码登录页签，以及体验入口](docs/assets/readme/登录界面.png)

**想先看看界面？** 可从[本地模拟](#quick-start)开始；对外开放演示或限时真实体验前，请阅读[体验说明](docs/public-experience.md)。

<a id="screen-chat"></a>

### 02 · 把日常工作放进对话

左侧管理会话，中央保留对话空间，底部集中输入与附件入口。先选择适合任务的助手，再进入对应的会话；会话内容与动态配置已遮罩，图中重点展示工作区布局。

![EIC AI 对话工作空间：会话列表、对话区域与消息输入，内容已脱敏](docs/assets/readme/云端AI对话.png)

<details>
<summary>展开：新建对话时选择助手</summary>

按用途浏览可用助手，把不同场景的对话分开管理。助手的可见范围取决于当前用户及其授权。

![新建对话的智能体选择窗口，动态助手信息已脱敏](docs/screenshots/云端实机截图-1920x1080/45-chat-agent-picker.png)

</details>

<a id="screen-knowledge"></a>

### 03 · 从文档到知识库

知识库卡片集中展示文档、处理状态和管理入口，便于按业务主题组织资料。创建与详情界面将分段设置、向量模型、Rerank 模型和文档处理状态放在同一处。

![EIC 知识库管理：卡片、文档状态与管理入口，动态数据已脱敏](docs/assets/readme/云端知识库管理.png)

<details>
<summary>展开：创建知识库与查看文档状态</summary>

**创建知识库：** 为资料集合命名，选择文件或添加文本，并配置分段参数与模型。界面明确区分文件保存、后台解析、向量化与索引状态。

![添加知识库窗口：分段参数、文件或文本入口、向量与 Rerank 模型选择](docs/screenshots/云端实机截图-1920x1080/30-kb-add.png)

**跟踪文档：** 在知识库详情中查看文件类型、处理状态、切片数与更新时间；上传成功仅代表文件已保存，最终处理结果应以后端记录为准。

![知识库详情与文档窗口：模型设置、文档列表和处理状态，文件信息已脱敏](docs/screenshots/云端实机截图-1920x1080/32-kb-documents-drawer.png)

</details>

<a id="screen-agents"></a>

### 04 · 给每个助手配置工作方式

首屏的智能体管理以卡片组织不同用途的助手；进入配置后，可以围绕基础信息、模型与知识库、技能工具、绑定范围、压缩策略、长期记忆和运行限制分别设置。

![智能体基础配置：名称、描述、提示词、模型与知识库绑定，动态值已脱敏](docs/screenshots/云端实机截图-1920x1080/24-agent-editor-tab-1.png)

<details>
<summary>展开：技能绑定、长期记忆与预算限制</summary>

**技能与工具：** 在助手内选择工具、Skill 与 MCP 配置，并按需启用对应类别。这里展示配置入口，具体工具是否可执行仍取决于接入和授权。

![智能体技能工具页签：AI 工具、Skill、MCP 与记忆开关，条目内容已脱敏](docs/screenshots/云端实机截图-1920x1080/24-agent-editor-tab-2.png)

**长期记忆：** 记忆模式有独立配置入口。界面说明默认关闭，自动提取仅生成待确认候选，确认后才参与后续对话；完整作用域与写入规则见[记忆与上下文](docs/MEMORY-CONTEXT.md)。

![智能体长期记忆页签：记忆模式与默认关闭、候选确认说明](docs/screenshots/云端实机截图-1920x1080/24-agent-editor-tab-5.png)

**运行限制：** 配置会话累计调用、Token、调用时长、单次超时和模型步骤上限，并选择达到阈值或模型故障时的策略。运行时的准入与降级规则见[可靠性说明](docs/RELIABILITY-FALLBACK.md)。

![智能体运行限制与降级策略页签：会话预算、超时与模型步骤字段，配置值已脱敏](docs/screenshots/云端实机截图-1920x1080/24-agent-editor-tab-6.png)

</details>

<a id="screen-models"></a>

### 05 · 管理模型、提示词与工具

模型管理把租户默认向量模型、Rerank 模型、会话标题与摘要模型，以及各模型卡片集中展示。知识库可以继承默认设置；已有向量索引的模型变更与重建要求需按界面提示处理。

![模型配置管理：租户知识库默认模型、会话标题与摘要模型及模型卡片，地址与配置已脱敏](docs/screenshots/云端实机截图-1920x1080/09-model-settings.png)

<details>
<summary>展开：模型接入、提示词与技能目录</summary>

**模型接入：** 通过添加模型表单维护接入信息与参数。截图展示表单结构，不包含可复用的密钥，也不表示测试调用已通过。

![添加模型表单，模型接入字段与动态配置已脱敏](docs/screenshots/云端实机截图-1920x1080/28-model-add.png)

**提示词管理：** 集中浏览和维护不同用途的提示词，为助手配置提供可查找的内容入口。

![提示词管理列表与操作入口，动态内容已脱敏](docs/screenshots/云端实机截图-1920x1080/06-prompt-management.png)

**技能与工具：** 在同一目录浏览技能工具卡片与确定性非模型工具入口。当前画面列出文本统计、JSON 校验与格式化、CSV 数值汇总；截图未执行这些工具。

![技能工具管理：确定性工具与技能卡片，动态配置已脱敏](docs/screenshots/云端实机截图-1920x1080/08-skills-tools.png)

</details>

<a id="screen-tasks"></a>

### 06 · 让计划与消息有处可查

任务页提供任务搜索、计划时间与时区、最近执行和结果查看入口。**截图中的自动任务尚未启用，队列显示暂不可用**；它展示的是管理界面及关闭状态，实际执行需完成部署配置与独立验收。

![自动任务管理：计划与结果入口，同时明确显示自动任务未启用和队列暂不可用](docs/screenshots/云端实机截图-1920x1080/02-ai-tasks.png)

<details>
<summary>展开：我的消息</summary>

消息中心按系统私信、私人私信、公司公告、系统消息与 AI 消息分类，方便在工作空间中切换查看不同来源的信息。

![我的消息：多种消息分类与列表布局，消息内容已脱敏](docs/screenshots/云端实机截图-1920x1080/03-my-messages.png)

</details>

<a id="screen-access"></a>

### 07 · 按团队组织访问范围

部门、岗位、用户、角色与权限共同构成管理入口。部门页提供层级结构与列表视图；角色权限配置将接口权限与菜单权限分开展示，便于管理员检查授权范围。

![部门管理：组织树、部门列表与管理入口，组织信息已脱敏](docs/screenshots/云端实机截图-1920x1080/10-departments.png)

<details>
<summary>展开：角色权限树</summary>

使用树形选择器查看角色的接口与菜单权限。截图仅展示授权配置结构，具体资源隔离与权限校验仍需在实际部署中验证。

![角色权限配置：接口与菜单权限树，背景角色信息已脱敏](docs/screenshots/云端实机截图-1920x1080/39-role-permissions.png)

</details>

<a id="screen-audit"></a>

### 08 · 查看变更，控制体验范围

变更审计提供操作类型、对象等筛选入口，帮助管理员浏览记录。操作日志与变更审计分别保留在管理导航中；画面中的记录字段已脱敏。

![EIC 变更审计：筛选条件、记录列表与管理布局，审计数据已脱敏](docs/assets/readme/云端变更审计.png)

<details>
<summary>展开：公开演示与限时体验管理</summary>

管理员在这里设置公开演示入口、体验时长、并发与调用额度，并限定真实体验可访问的页面和获批资源。公开演示使用合成数据，真实体验受授权及预算限制；配置说明见[公开体验文档](docs/public-experience.md)。

![演示与限时体验管理：入口、总额度、可用页面与体验资格设置，动态配置已脱敏](docs/screenshots/云端实机截图-1920x1080/18-experience-admin.png)

</details>

[继续浏览全部界面](docs/screenshots/界面导览.md) · [查看截图覆盖与捕获边界](docs/screenshots/截图索引.md) · [在本地启动 EIC](#quick-start)

<a id="quick-start"></a>

## 快速体验

本地模拟需要 Python 3.11+、Node.js 20+；Windows 使用 PowerShell 7，Linux 使用 Bash。它不需要 Docker、MySQL、PostgreSQL、Redis 或模型 API Key。

**Windows / PowerShell**（从仓库根目录执行）：

```powershell
python -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -e .\backend
Push-Location frontend
npm ci
npm run build
Pop-Location
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-sim.ps1 -OpenBrowser
```

**Linux / Bash**（从仓库根目录执行）：

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -e ./backend
cd frontend
npm ci
npm run build
cd ..
bash scripts/start-sim.sh
```

打开 `http://127.0.0.1:8080/login`。模拟环境使用租户 `1000`、账号 `admin`；临时密码由脚本在本机生成并写入 `data/admin-password.txt`。`data/` 同时保存模拟数据库和密钥，**不要提交或分享该目录**。完整部署与开发步骤见[安装与首次运行](docs/GETTING_STARTED.md)。

<a id="architecture"></a>

## 系统架构

![EIC 系统架构](docs/architecture.svg)

| 层级 | 技术 |
| --- | --- |
| 前端 | Vue 3、Vite、Naive UI |
| API 与数据层 | FastAPI、SQLAlchemy Async、Alembic |
| Agent 流程 | LangChain、LangGraph |
| 本地模拟 | SQLite；无需 Docker、Redis、向量数据库或模型 API Key |
| 自托管部署 | MySQL 8.4+ 或 PostgreSQL 16 + PGVector，可组合 Redis 7+ 与 ARQ |

完整组件边界与数据流见[架构说明](docs/ARCHITECTURE.md)。本地模拟不代表完整生产队列、向量基础设施或真实模型调用。

## 演示边界

- 公开演示需要管理员显式启用；所有可浏览页面使用合成数据，回复不会调用真实模型。
- 限时真实体验只能访问管理员明确授权的模型、Agent 和知识库，并受时长与预算限制。
- 公开演示截图不含真实用户资料、业务文档、生产凭据或真实观测记录。
- 本地模拟适合探索 UI 和基础流程，不能证明外部模型、生产数据库、后台队列或部署容量已经验证。

<a id="roadmap"></a>

## 路线图

路线按三个方向并行推进，不承诺具体日期；优先级会结合社区反馈和验证结果调整。

| 方向 | 下一步 |
| --- | --- |
| 开源上手 | 持续打磨本地模拟、首次运行说明和贡献者验证路径。 |
| AI 能力 | 围绕固定场景持续完善检索与 Agent 流程评测。 |
| 企业部署与运维 | 补齐部署验收、恢复流程和可选集成的验证说明。 |

<a id="documentation"></a>

## 文档

| 主题 | 文档 |
| --- | --- |
| 产品界面 | [完整界面画廊](docs/screenshots/界面导览.md) · [截图索引与覆盖说明](docs/screenshots/截图索引.md) |
| 上手 | [安装与首次运行](docs/GETTING_STARTED.md) · [公开演示与限时体验](docs/public-experience.md) |
| 架构与部署 | [系统架构](docs/ARCHITECTURE.md) · [部署与运维](docs/OPERATIONS.md) · [迁移](docs/MIGRATION.md) |
| AI 与运行时 | [记忆与上下文](docs/MEMORY-CONTEXT.md) · [检索索引与观测投递](docs/MEMORY-INDEXING.md) · [可靠性与降级](docs/RELIABILITY-FALLBACK.md) · [Harness 与评测](docs/HARNESS-EVALUATION.md) |
| 接入与依赖 | [MinerU 接入](docs/mineru-integration.md) · [本地 Langfuse 接入](docs/LANGFUSE-LOCAL.md) · [依赖清单](docs/DEPENDENCIES.md) |
| 参与项目 | [贡献指南](CONTRIBUTING.md) · [安全问题报告](SECURITY.md) · [第三方声明](NOTICE.md) |

## 集成与许可

MinerU 是可选的私有 PDF 解析适配器，默认关闭；本源码包不安装或运行 MinerU 引擎。Langfuse 可连接自托管实例，EIC 导出器默认关闭。启用这些能力前请阅读对应文档中的网络、数据保留、资源和许可边界。

EIC 源码使用[木兰宽松许可证第 2 版](LICENSE)。部分依赖有独立许可，尤其 PyMuPDF/MuPDF 提供 AGPL 或商业许可；请根据分发和使用方式核对[第三方声明](NOTICE.md)及上游条款。项目许可证不会取代依赖自身的许可义务。

欢迎通过仓库的 Issue 和 Pull Request 参与；安全问题请先阅读[安全报告说明](SECURITY.md)。
