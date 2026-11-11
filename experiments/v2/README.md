# v2 实验与验收目录

| 目录 | 内容 |
| --- | --- |
| datasets/ | 开发案例、合成资料、人工复核草案与冻结数据 |
| tests/、legacy_tests/ | 当前回归与原有测试 |
| scripts/ | 可重复执行的测试、消融与验证入口 |
| reports/ | 已检查并纳入 Git 的证据；live/ 是忽略的复跑输出 |
| traces/、tmp/ | 忽略的运行轨迹、模型、环境、临时索引与日志 |

[测试与复现、报告口径及人工审核](../../docs/EVALUATION.md) · [当前架构](../../docs/ARCHITECTURE.md) · [部署](../../docs/DEPLOYMENT.md) · [阶段变更](../../docs/CHANGELOG.md)

历史 JSON 报告保留原始结果，不把开发集成绩解释为独立准确率。
