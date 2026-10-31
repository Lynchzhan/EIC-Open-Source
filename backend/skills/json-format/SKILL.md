---
name: json-format
description: 校验 JSON 并按字段排序格式化，拒绝重复字段和非有限数值。
version: 1.0.0
enabled: true
allowed_tools: [json_format]
permissions: [skill.read, tool.execute]
---

# JSON 校验与格式化

输入 JSON object 的 text 字段，执行受控的 json_format 工具。
输出 valid、rootType 和 formatted，不执行输入内容。
