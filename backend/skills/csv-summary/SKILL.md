---
name: csv-summary
description: 汇总 CSV 指定数值列的行数、合计、均值、最小值和最大值。
version: 1.0.0
enabled: true
allowed_tools: [csv_summary]
permissions: [skill.read, tool.execute]
---

# CSV 数值汇总

输入 text 和 column 字段，执行受控的 csv_summary 工具。
使用 Decimal 汇总，拒绝空数据、异常行、非数值和超限数据。
