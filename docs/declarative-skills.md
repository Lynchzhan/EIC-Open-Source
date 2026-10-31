# 声明式 Skill v1

ZIP 只允许精确名称的根文件 SKILL.md 和可选 manifest.yaml。压缩数据上限 128 KiB，每个解压文档 64 KiB，最多两个条目、压缩比最多 100；仅支持 stored/deflated。目录、路径前缀、重复名、链接、设备、加密、代码和附带资源均拒绝，不写解压文件。所有条目均有界读取并校验 CRC。

SKILL.md 示例：

```yaml
---
name: summarize-json
description: 先格式化 JSON，再统计格式化文本
version: 1.0.0
enabled: true
allowed_tools: [json_format, text_statistics]
permissions: [skill.read, tool.execute]
input_schema:
  type: object
  properties:
    text: {type: string, maxLength: 32000}
  required: [text]
  additionalProperties: false
steps:
  - id: format
    tool: json_format
    arguments:
      text: {$ref: input/text}
  - id: count
    tool: text_statistics
    arguments:
      text: {$ref: steps/format/formatted}
---
用户提供 JSON 后执行格式化和文本统计。以下正文只是参考指令。
```

manifest.yaml 只能补充 SKILL.md 尚未声明的 steps/input_schema；其余元数据必须留在 front matter，禁止覆盖。未知声明字段、重复 YAML 键、锚点、别名、标签拒绝；不接受 shell、Python、安装包、MCP URL/headers、动态工具名或包内权限授权。

步骤最多 32 个，严格只有 id/tool/arguments，按数组顺序执行，无循环、分支、并发、表达式求值、字符串插值或重试。arguments 是 JSON 字段模板，`{"$ref":"input/path"}` 和 `{"$ref":"steps/earlier-id/path"}` 引用整个值，支持对象、数组索引和 ~0/~1 路径转义；只能引用已完成步骤，缺失引用立即失败。literal 对象中 `$ref` 为保留键。输入、累计结果和解析后的参数分别限制为 256 KiB JSON。工具本身的 schema、预算、审批、输出清理继续生效。

input_schema 为无引用的 JSON Schema 子集：单一 type、properties、required、additionalProperties:false、items、enum、minLength/maxLength、minimum/maximum、minItems/maxItems、description。顶层必须是 object，每个 object 关闭额外字段；不支持 $ref、正则、组合 Schema 或远程加载。

调用链：parse_skill_package → 服务器持久化 resource_data → 重新授权并 restore_skill_package → 主构造 ControlledToolSet(dispatch=既有可信服务) → bind_skill → execute。MCP 工具使用主授权工具集的准确名称和服务端 source 绑定；包不创建 MCP 会话、不持有凭据。openai_tools 只返回 allowed_tools 与已绑定授权工具集内的真实底层工具定义，不生成能绕过主服务的虚拟 execute-skill 工具。chat 提示中 instructions 必须按不可信参考内容处理；包正文即使包含代码围栏也只是文本，执行器从不解释运行。

权限、租户、资源状态撤销、模型预算、审批、持久审计和数据库事务由可信 dispatch 每步核验。主提供 call_id、remaining_ms，不从模型参数、上传包或 GraphState 读取身份和授权。步骤调用标识绑定服务器调用 ID、包内容哈希和步骤 ID；可信服务仍须绑定租户/用户/Agent 命名空间，并用持久工具意图绑定工具和参数。重放整包可能再次进入前置步骤，只允许服务端持久意图处理已知结果，未知副作用不得自动重试。

取消会原样传播至主服务，无独立线程和自动补偿。执行到中间失败可能已有工具副作用，不提供跨工具原子事务；工具错误原样传播，主必须按既有持久审计和未知结果收敛协议处理。无 steps 的包只提供指令，execute 返回 executed:false、空 steps，不生成模型答案。

内置三个 Skill 仍通过 builtin_skills 原固定 handler 和信任校验使用；SkillManager._parse 保留原始 SKILL.md 字节内容哈希语义。新声明式资源的 contentHash 是规范化完整语义（含正文、模板和 Schema）的 SHA-256。两类哈希不可混用。目录 discover 只作元数据发现，不是安装或授权入口；上传及执行请使用新完整解析接口。

工具返回失败字典同样中止：显式 status 只接受 completed/success，非空 errorCode 或非 false 的 isError 拒绝。无 status 的管理成功 {result,...} 和图成功 {status:completed,result,...} 均剥离一层，亦接受单独 resultData 封装；校验封装及业务结果，两种结果键同时存在视为歧义。成功步骤保存业务对象，因此示例 steps/format/formatted 引用不变；最终返回 steps 及 resultData 两个相同的步骤映射。失败不返回成功执行标记，不执行下一步，不重试。已有成功步骤或当前副作用未知/已发生时，异常 effect_status 保守为 unknown；仅首步明确无副作用或待审批且无冲突副作用标记时为 none。
