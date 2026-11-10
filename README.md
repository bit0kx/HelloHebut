# HelloHebut：河北工业大学本科报考咨询助手

基于 Python、FastAPI 和 Anthropic Messages API 的多 Agent 咨询项目，结合官方资料 RAG、CSV 录取数据、Skills 与分层记忆，为本科报考问题提供带来源的回答。

## 主要功能

| 场景 | 能力 |
| --- | --- |
| 学校、校园生活、招生政策与费用 | 检索已收录的官方资料并展示来源 |
| 分数、位次与录取风险 | 查询河北、天津历史录取数据，计算并解释风险等级 |
| 专业比较、就业与升学 | 根据专业资料和用户条件给出建议 |
| 复合问题与多轮追问 | 四类 Agent 协作，利用会话历史和用户画像补充背景 |
| 资料维护与质量检查 | 文档导入、Skills 热加载、评测报告及运行监控 |

Agent 由应用规则编排，工具在回答前由业务服务调用。风险等级是历史数据参考，不代表录取概率。

## 文档导航

| 文档 | 主要内容 |
| --- | --- |
| [主要流程](main_flow.md) | 服务启动、聊天时序、知识导入、风险案例、记忆与评测流程 |
| [技术实现与接口索引](TECHNICAL_DESIGN.md) | 技术与代码入口、算法、参数、数据结构、接口及实现边界 |
| [Agent 精进方案](AGENT_EVOLUTION_PLAN.md) | 基于调研与 EchoMind 的升级设计、实施阶段、验收及消融实验（待实施） |
| [部署与验证](DEPLOY.md) | 环境配置、启动命令、验证示例、维护和排障 |
| [Skills 编写说明](skills/README.md) | 规则文件格式、匹配方式、内容预算和热加载 |
| [前端说明](frontend/README.md) | 页面功能、本地开发及代理配置 |

首次使用按“[部署](DEPLOY.md) → [主要流程](main_flow.md) → [技术实现](TECHNICAL_DESIGN.md)”阅读。后端完整调试入口为 `http://localhost:8000/docs`。

## 技术组成

- **服务与编排**：FastAPI、Pydantic、自定义 Agent、`asyncio`。
- **检索与数据**：ChromaDB、上下文切片、加权 RRF、查询改写、LLM 重排、确定性 CSV 查询。
- **记忆与运行**：Redis、Chroma 情景记忆/画像、LLM Judge、Prometheus。
- **界面与部署**：Vue 3、Vite 7、Docker Compose、Nginx。

依赖版本以 [requirements.txt](requirements.txt)、[frontend/package.json](frontend/package.json) 和 [Dockerfile](Dockerfile) 为准；代码阅读入口集中在[技术与功能对应表](TECHNICAL_DESIGN.md#1-技术与功能对应关系)。
