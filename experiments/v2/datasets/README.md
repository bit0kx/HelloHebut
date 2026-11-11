# 数据集索引

| 文件 | 用途与性质 |
| --- | --- |
| retrieval_reference.json、retrieval_holdout.json | 已参与诊断的开发检索集；holdout 文件名不代表独立盲测 |
| admission_routing.json | 必需工具路由案例 |
| policy_fixture.json | 合成政策资料 |
| human_review_template.jsonl | 30 条未批准的人工复核草案，不是参考答案 |
| frozen/（生成后） | 审核工具产出的 development / independent 冻结数据 |

完整字段、人工操作、来源快照、校验与冻结方法见 [评测说明](../../../docs/EVALUATION.md)。
