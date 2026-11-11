---
name: admission-risk-analysis
description: 根据已调用的录取数据工具解释分数、位次和风险；条件缺失时先澄清。
metadata:
  version: "1.0.0"
  agents: "risk,planning"
  keywords: "分数,位次,风险,冲稳保,最低分,最低位次"
  references: "references/risk-checklist.md"
  dependencies: "admission_data_query"
  tests: "test_roles_tools.py,test_chat_integration.py"
---
# 录取风险解释流程

1. 核对本轮考生、省份、科类、目标专业和成绩来源；换考生或考试范围时不复用旧条件。
2. 使用本轮工具返回的记录和风险等级；工具未执行、失败或无数据时只澄清/说明资料不足。
3. 位次优先，裸分仅作低置信参考；不得把历史对比写成未来录取概率。
4. 保留年份、专业、最低分/位次与来源的对应关系，不能为了省略上下文而剪掉必需数字。
5. 比较多个专业时按需读取 references/risk-checklist.md；不在本Skill写死年度数字。
