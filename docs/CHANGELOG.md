# 简要变更记录

后续每批只追加“改动、验证、遗留项”，当前架构和操作说明分别维护，避免重复。历史报告保留在 [experiments/v2/reports](../experiments/v2/reports)，失败记录不覆盖。

## 2026-11-11｜文档集中与去重

- 项目说明统一到 docs，README 只保留概览或目录导航；运行时 Skills、招生资料和实验数据维持原位置。
- 主要流程与技术设计合并为 [ARCHITECTURE.md](ARCHITECTURE.md)，分模块讲解状态、CSV、证据、角色、事务、任务、记忆、协议与评测，突出实际优化与启用状态。
- 部署/前端/Skills/评测重复内容归入各自专文；旧完整精进方案和原始研究交接不再作为当前说明，未完成目标与技术取舍归入 [ROADMAP.md](ROADMAP.md)。旧原文仍可从 Git 历史恢复。
- 11 份说明/README 从 1316 行合并为 661 行，98 个本地链接检查通过；已核对代码事实与变更范围。无业务代码变更，未重跑上一批功能测试；本批无新增环境阻塞。

## 2026-11-11｜第六批：持久工作流与剩余工程（cd5fdaa）

- LangGraph 六节点、MySQL Checkpointer/任务/outbox、提交后回执恢复、清空防复活；前端 SSE/断线恢复/后端身份绑定。
- 可选主辅协作、Composer、只读工具循环、渐进 Skills、标准 MCP、隔离评测、manifest、逐请求指标与可选 Langfuse；中文 BGE 完成隔离开发实验，生产索引未替换。
- 原有 45 与 v2 119 项离线回归通过，MySQL、真实进程退出/数据库重启、前端、MCP 和模型冒烟通过；详见 [统一汇总](../experiments/v2/reports/stage6_validation.json)。独立人工质量、真实 Langfuse、页面目测和标准构建未据此宣称完成。
- 初次旧 mock 路径与一次模型结构化探测失败已记录；修正并复测，保留 [探测未通过报告](../experiments/v2/reports/stage6_model_capability_failed.json)。默认仍为单角色、工具循环关闭。

## 2026-11-11｜第一至五批历史摘要

| 批次 / 提交 | 主要变化 | 当时验证与限制 |
| --- | --- | --- |
| 第一批 b1e5db7 | 类型契约、条件纠正、必需 CSV、预算/Trace、身份幂等、Agentic RAG 首版 | 原有 45 + 新增 36 项通过；早期模型完整回答有未通过项，后续批次修复；[当时容器报告](../experiments/v2/reports/deployment_smoke.json) |
| 第二批 0a1b991 | CSV 确定性短路径、资料暂存/发布/回退、原文引用与管理权限 | 容器 6/6、存储 4/4；开发检索集仍漏召回；[报告](../experiments/v2/reports/stage2_deployment.json) |
| 第三批 77e5690 | 年份去扰、等权 RRF、BM25 对照、按发布范围隔离缓存 | 开发集改善，BM25 未证明优于修复后词法；非独立质量成绩；[消融](../experiments/v2/reports/stage3_retrieval_ablation.json) |
| 第四批 3a292c4 | 偏好来源、确认/更正/删除/过期与管理面板，当时使用 PG | 记录的是历史 PG 验收，不能作为当前 MySQL 证明；[历史汇总](../experiments/v2/reports/stage4_validation.json) |
| 第五批 bcf800b | 按用户要求切 MySQL，持久整轮账本、答案快照、Redis 懒导入/清空墓碑 | 偏好 11/11、账本 12/12、重启与模型检查通过；当时尚无节点级恢复；[历史汇总](../experiments/v2/reports/stage5_validation.json) |

旧 PG 容器已停用并保留卷，没有恢复为当前运行依赖。各批次沿用的 Docker 镜像源元数据阻塞、人工标签不足等仍由 [部署说明](DEPLOYMENT.md) 和 [评测说明](EVALUATION.md) 说明当前状态，不重复抄写历史待办。
