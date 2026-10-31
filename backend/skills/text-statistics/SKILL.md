---
name: text-statistics
description: 统计文本字符、非空白字符、行、词和 UTF-8 字节数。
version: 1.0.0
enabled: true
allowed_tools: [text_statistics]
permissions: [skill.read, tool.execute]
---

# 文本统计

输入 JSON object 的 text 字段，执行受控的 text_statistics 工具。
只统计输入文本，不读取文件、不请求网络、不调用模型。
