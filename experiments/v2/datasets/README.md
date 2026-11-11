# 数据集与人工复核

`retrieval_reference.json`、`retrieval_holdout.json` 的标签来自项目已收录资料，且已参与诊断与调参；两者都属于 **development**。文件名中的 holdout 不代表当前仍是独立盲测集。`admission_routing.json` 验证必需工具路由；`policy_fixture.json` 为合成资料，均不提供真实业务准确率结论。

`human_review_template.jsonl` 提供 **30 条 draft 审阅任务**，覆盖 10 类咨询/失败/安全场景。问题、审阅目标和候选文件由程序开发过程整理，`expected` 与人工签名保持空白。它不是 30 条已核验答案，也不是独立人工真值。无需为了通过校验而填入虚构审核人、答案或引用。

## 人工实际要做的事

1. **开发抽检**：复制模板，逐条查看年度官方材料、CSV 来源或对应产品要求，补充 `expected.response_kind`、可执行的 `assertions`/必含禁含项、必要工具；不能直接照抄当前模型答案。
2. 每条记录补 `review.reviewer/reviewed_at/human_verified`，时间使用带时区的 ISO 8601；审阅通过才将 `review_status` 改为 `approved`。未核验来源的题保留 `draft` 或 `rejected`。
3. 在 `review.verified_references` 中记录已实际核验的依据：`reference_id/kind/path/sha256/locator/verified/verified_by/verified_at`。官方政策/数据另填 `source_url`。`locator` 写章节、页码或 CSV 行/查询条件，不能只写整个网站。网页/PDF 快照放 `experiments/v2/review_sources/`；不要放账号、聊天记录或敏感考生信息。哈希可用 `Get-FileHash -Algorithm SHA256` 获取并转为小写。
4. **独立验收**：另建未在开发或调参中使用的问题，由不依赖当前实现输出的人工独立标注；设置 `provenance.case_origin=independent_human`、`review.used_for_tuning=false`、`review.independent_from_implementation=true`。模板题已经曝光，不能只改状态就宣称独立。一旦独立集用于定位问题或调参，应降级为开发集并另建验收集。

引用示例（占位值须替换为真正审阅信息；此段不是已完成核验）：

```json
{
  "reference_id": "source-1",
  "kind": "official_policy",
  "path": "experiments/v2/review_sources/年度政策快照.txt",
  "sha256": "本地快照的64位小写SHA256",
  "locator": "对应章节、原文段落及适用年份",
  "source_url": "https://官方原始页面",
  "verified": true,
  "verified_by": "实际审阅人",
  "verified_at": "2026-11-11T18:00:00+08:00"
}
```

`kind` 允许 `official_policy/official_data/product_requirement/synthetic_fixture`。产品安全/故障场景可核对产品要求或合成故障夹具，不必伪造一个官方政策来源。哈希只证明本地内容未变，不证明来源真实或人工判断正确。

## 校验与冻结

在项目根目录运行（可使用测试虚拟环境的 Python）：

```powershell
python experiments/v2/scripts/review_dataset.py validate --tier development
python experiments/v2/scripts/review_dataset.py validate --input experiments/v2/datasets/人工复核结果.jsonl --tier independent
python experiments/v2/scripts/review_dataset.py freeze --input experiments/v2/datasets/人工复核结果.jsonl --tier independent
```

默认校验报告位于 `experiments/v2/reports/stage6_review_validation.json`。无审阅人、未核验引用、哈希不符、缺少评分标签或已有调参污染的记录，均不能冻结为 independent。development 允许保存未审阅的任务骨架，但冻结文件会明确 `eligible_for_independent_acceptance=false`。

冻结产物为 `datasets/frozen/<tier>-<hash>.json`，`benchmark_id` 绑定题目、评分要求、审阅信息及引用快照哈希。相同输入重复冻结不变；同名文件被修改时拒绝覆盖。实现/model/prompt 的变化应放运行 manifest，不能偷偷改同一 benchmark 的答案。冻结工具仅完成格式、声明与完整性门禁；当前在线 evaluator 仍使用既有开发场景，冻结的人审集应在实际人工完成后接入后续独立验收，不能据空模板计算准确率。
