# HelloHebut：河北工业大学本科报考咨询 Agent

基于 Python、FastAPI、Vue 和 Anthropic Messages 协议，结合官方资料检索、CSV 录取数据与可验证记忆，提供带来源的本科报考咨询。当前 `v2` 使用 MySQL 保存偏好、会话、工作流快照和后台任务，Redis 保存身份与知识清单，Chroma 保存资料索引。

## 重点优化

- **可恢复执行**：LangGraph 六节点 + MySQL JSON Checkpointer、租约、版本与持久预算；幂等回执防止整轮重复提交。
- **有依据的回答**：录取问题必查 CSV；Agentic RAG 按年份与缺项补查，回答核验后再提交，证据不足明确说明。
- **可验证记忆**：结构化条件及来源、换考生/省份失效旧成绩；长期偏好由用户确认，可更正、删除与过期。
- **受控协作与规则**：默认单角色，可选主辅角色 + Composer；渐进 Skills、完整上下文预算、有界只读工具循环。
- **持久任务与交互**：事务 outbox、后台 worker、SSE 阶段进度、断线恢复及最终结果查询。
- **可复现工程**：隔离评测、Trace/Prometheus、标准只读 MCP、可选 Langfuse、实验指纹与故障测试。

风险等级是历史数据参考，不代表录取概率。BGE 中文向量已做隔离对照，生产索引仍使用 MiniLM；可选能力和已启用能力在文档中分别说明。

## 文档导航

**先读 [整体架构、模块流程与技术优化](docs/ARCHITECTURE.md)**，再按需要查看：

| 文档 | 唯一职责 |
| --- | --- |
| [架构与工作流程](docs/ARCHITECTURE.md) | 模块分工、六节点主链路、知识/记忆维护、故障恢复与技术优化 |
| [部署与配置](docs/DEPLOYMENT.md) | 环境、启动、端口、有效开关、日常运维与构建限制 |
| [开发与扩展](docs/DEVELOPMENT.md) | 代码目录、前后端开发、接口权限与 Skills 编写 |
| [评测与复现](docs/EVALUATION.md) | 测试命令、实验报告、人工参考审核、冻结与结果边界 |
| [后续路线与取舍](docs/ROADMAP.md) | 尚未完成的验收、候选技术上线条件与设计来源 |
| [简要变更记录](docs/CHANGELOG.md) | 每批改动、验证、遗留项与历史提交 |

当前业务版本 `cd5fdaa` 的 45 + 119 项离线回归通过；存储、恢复、前端与模型专项证据见 [统一报告](experiments/v2/reports/stage6_validation.json)。这些结果不等于独立人工准确率或全部生产验收。

## 快速定位

- 部署后页面：http://localhost:8088；API 文档：http://localhost:18000/docs。
- 依赖：[requirements.txt](requirements.txt)、[frontend/package.json](frontend/package.json)；配置：[.env.example](.env.example)。
- 项目说明集中在 `docs/`；各目录 README 仅作导航。`skills/` 的 Markdown 是运行规则，`data/` 是知识/录取数据，`experiments/v2/` 保存测试与证据。
