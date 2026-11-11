---
name: policy-comparison
description: 比较不同年份、专业或考生范围的政策，逐项核对适用对象与证据缺口。
metadata:
  version: "1.0.0"
  agents: "policy,planning,general"
  keywords: "比较,对比,变化,区别,分别"
  references: "references/comparison-checklist.md"
  dependencies: "knowledge_search"
  tests: "test_skills_dynamic.py,test_agentic_rag.py"
---
# 政策比较流程

1. 拆分“年份×专业×适用对象”，用户未给年份时不得猜测。
2. 每个比较单元必须对应当前请求已注册的原文；相邻年份不能补齐缺失年份。
3. 按同一字段列出有依据的相同点和差异，引用来源编号。缺少原文时明确缺项。
4. 来源冲突进入复核，不用多数角色意见替代事实核验。
5. 需要比较表结构时按需读取 references/comparison-checklist.md；参考文件只提供方法，不提供政策真值。
