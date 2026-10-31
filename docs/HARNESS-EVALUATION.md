# Agent Harness、固定评测与发布门禁

## 本版范围

P15 的实现为单 Agent 的确定性工程回归控制面，读取 P10-P14 的工具、上下文、
记忆、Checkpoint 和依赖控制行为。它不是多 Agent 编排、LLM Judge，
也不把合成分数称为真实模型质量、生产性能或简历里的线上收益。

本目录描述如何使用项目自带 Harness 对 Agent 行为进行确定性回归。评测结果应保存在源码目录之外，并关联明确的源码版本、依赖和数据集版本。
合成评测不能替代真实模型质量、生产性能或安全验收；不得把运行日志、业务数据或真实访问凭据写入评测源码。

## 四类边界

| 类别 | 对象 | 实施方式 |
| --- | --- | --- |
| Locked | evaluator、gold/corpus、阈值、测试、发布脚本 | `evals/LOCK.json` + 候选之外固定批准 SHA-256；云端只读挂载 |
| Editable | 应用源码、检索参数、prompt、前端和示例配置 | 在独立候选副本修改；不得修改本轮 Locked 文件后自我批准 |
| Append-only | 外部 JSONL 实验日志、逐项结果、失败 ID | 互斥追加、fsync、单调序号与哈希链；每次结果新建，不覆盖 |
| Human-controlled | 密钥、生产切换、恢复、写工具审批、批准 pin | 不向聊天 Agent 暴露文件写入或发布工具；外部操作者控制 |

哈希链不等于物理不可删除存储。保留每次 CLI 返回的 `journalHead` 到独立位置；
下一次使用 `--expected-journal-head` 可发现截断和替换。禁止把待检查清单的
即时哈希当作批准值：批准 pin 必须来自本轮候选修改权限之外的冻结记录。
本版没有远程不可变日志服务或硬件签名，不声称抵抗拥有服务器管理员权限的篡改。

## 固定集与指标口径

`evals/agent-cases.json` 有 60 项，每层 15 项：

- simple：单知识检索、单工具、基础长期记忆。
- medium：RRF rank fixture、多工具、冲突事实。
- complex：重复 call 幂等、恢复拒绝边界、429/部分输出/未知结果/参数拒绝。
- long_context：完整轮次裁剪、过期/删除/其他用户记忆过滤、步数预算。

Corpus 单独冻结，不把 query/gold 写入知识入库路径。第二检索通道是可控的
排序 fixture，不冒充真实 embedding 或真实向量库。模型决策使用合成适配器，
但是 BM25、RRF、Tool Calling Agent、ToolCenter、记忆过滤、上下文预算、
持久 Checkpoint 与可靠调用运行的是应用实现。

RAG 指标包括 Hit@K、Recall@K、MRR、nDCG@K。重复结果占排名但不重复获得
gain；无相关项为 N/A。引用指标校验 claim/source ID 与检索证据的匹配，
不代表真实文本的语义蕴含。确定性答案正确/完整性按固定预期校验，
没有调用 LLM Judge 进行开放问答评分。

Agent 指标按实际工具条目统计分母，不按请求平均刷分。未获授权、尚未进行
Schema 校验的参数不进入参数有效率分母。工具、任务、拒绝、预算、重复执行、
兜底、未知结果分别统计，不能只看一个总分。

安全节点崩溃探针在 Checkpoint commit 后使用 `os._exit(17)` 突然终止写进程，
新进程读取持久库、恢复安全上下文节点并提交 completed。这个指标分母为一个
实际探针，其他“恢复决策正确”用例不混入崩溃恢复率。它不是断电磁盘故障、
全业务进程恢复、真实模型重放或写工具 exactly-once 的证明。

P50/P95 来自隔离执行实测耗时，节点耗时来自 `elapsedMs`。本版决策接口没有
真实流式首 token 与上游 usage，相关值为 N/A。UTF-8 字节保守估算、准入预留单位
和真实 token/费用保持分离；不将预留单位冒充上游账单。

## 可运行入口

下列命令在已准备应用依赖的隔离 Linux/云端环境执行，**不是要求本机安装**。
容器内 `/source` 与 `/baseline` 应为只读挂载；不得挂载真实数据库、上传目录、
Docker socket、密钥目录或生产 `.env`。网络应关闭。

批准哈希来自外部冻结记录，不从候选目录现场生成：

```bash
export HARNESS_APPROVED_SHA256='<外部冻结记录中的完整 SHA-256>'
mkdir -p /results

python -B /source/scripts/evaluate-agent.py evaluate \
  --expected-lock-sha256 "$HARNESS_APPROVED_SHA256" \
  --candidate-source /baseline \
  --output /results/baseline.json --journal /results/runs.jsonl --run-id baseline

python -B /source/scripts/evaluate-agent.py evaluate \
  --expected-lock-sha256 "$HARNESS_APPROVED_SHA256" \
  --candidate-source /source \
  --output /results/candidate.json --journal /results/runs.jsonl --run-id candidate

python -B /source/scripts/test-isolated.py --output /results/tests

python -B /source/scripts/evaluate-agent.py gate \
  --expected-lock-sha256 "$HARNESS_APPROVED_SHA256" \
  --candidate-source /source \
  --baseline /results/baseline.json --candidate /results/candidate.json \
  --test-evidence /results/tests \
  --output /results/gate.json --journal /results/runs.jsonl --run-id gate
```

所有结果必须放在源码之外。已有结果路径会被拒绝而非覆盖，失败会保留在日志；
断网后先读取已有输出、SHA-256 和后台任务退出码，不重跑已完成的部署或迁移。
评测入口清除继承的应用配置，使用独立临时 cwd 和合成数据库。

## 门禁与 Review

门禁由原始测量重算逐项评分，再重算总体及每层指标。检查以下情况：

1. 规则、固定集、corpus、源码版本、输入摘要匹配。
2. 空集、遗漏/重复 ID、NaN、缺失测量、分母变化、错误答案哈希不能通过。
3. 每个关键维度与每层均不得比基线退步，不能用总平均掩盖长上下文退步。
4. 全量测试模块必须完整，源代码哈希、pytest 命令、字面日志与退出码必须匹配。
5. OCR 缺字体的 4 个已知跳过单独记录，不计为通过，不开放真实发布门。
6. 不仅检查 `passed` 字段；错误答案 digest 与成功标记矛盾时直接拒绝。

隔离门通过只生成 `isolatedGate=true`，始终保持 `fullReleaseGate=false`。
真实发布还需要实际模型质量、真实依赖、从新锁构建、浏览器验收、数据库备份恢复、
容量/预算和人工审批的独立记录。`release_deploy.py --apply` 在网络操作之前要求
`--harness-gate` 和外部批准的 `--expected-harness-gate-sha256`；
合成 PASS 不能打开生产门。迁移 head 从归档迁移图推导，不硬编码旧 revision。

## 交付与回滚

每个阶段冻结源码、基线、补丁、清单和可运行源码回滚，实际在独立副本应用补丁、
逐文件验 SHA-256，并执行预检/应用/核验/重复回滚与防覆盖负对照。
最终中文报告、命令、字面输出和退出码保存在交付目录，不随公开源码携带。

源码回滚不降级 `0025_dependency_controls` 数据库。真实发布前必须保留经过恢复
演练的数据库/上传/配置备份；未知模型结果与写工具副作用不因回滚而自动重发。
