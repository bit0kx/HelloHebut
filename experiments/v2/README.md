# v2 分阶段实施与验证

本目录集中保存评测、测试、报告和运行中间产物；业务实现复用根目录的 `core/`、`memory/`、`mcp/`、`api/`，仅新增 `rag/` 检索控制模块。方案见 [AGENT_EVOLUTION_PLAN.md](../../AGENT_EVOLUTION_PLAN.md)。

## 本轮范围与状态

按讨论后的依赖顺序实施前三个落地步骤：基础契约与基线、关键可靠性、Agentic RAG 首版。它们覆盖六周方案前两阶段的核心链路，并提前修复会话可靠性；不代表六周方案的全部验收项已完成。

| 步骤 | 已实现 | 主要代码 |
| --- | --- | --- |
| A：基础契约与基线 | TaskState、字段来源与条件失效、工具完整 Schema 校验、请求预算、脱敏元数据 Trace、必需 CSV 调用 | [contracts](../../core/contracts.py)、[task_state](../../core/task_state.py)、[runtime](../../core/runtime.py)、[ChatService](../../core/chat_service.py) |
| B：关键可靠性 | 匿名会话令牌、请求回执、同 ID 内容冲突、会话租约、整轮原子提交、摘要失败保留原文、并发压缩保护、有界维护队列 | [turn_store](../../memory/turn_store.py)、[conversation_memory](../../memory/conversation_memory.py)、[API](../../api/main.py) |
| C：Agentic RAG 首版 | 按年份/子问题规划、严格年份过滤、复用召回重排、证据 ID 校验、有限补查、回答核验与一次修复、资料不足回退 | [agentic](../../rag/agentic.py)、[tool_manager](../../mcp/tool_manager.py)、[knowledge_base](../../mcp/knowledge_base.py) |

关键行为：复合问题只要需要录取数字，就先查 CSV；查询失败或条件不足时直接说明/澄清。更换省份、科类、考试年度或考生后，相关成绩字段失效，不从旧历史文本重新补回。回答必须引用本次真实证据，数字先检查是否出现在依据中，再由模型检查对应关系；这仍不是形式化的语义正确性证明。

当前仍待实施：BM25/Embedding 的正式对照、资料活动版本的原子发布、带来源的长期事实库、PostgreSQL、LangGraph Checkpoint、标准 MCP、Composer 与动态 Skills。已有摘要/画像只修复了写入可靠性，不能称为“可验证长期记忆已完成”。模型驱动的通用工具循环也尚未接入角色 Agent。

## 文件与运行数据

```text
experiments/v2/
├── datasets/       固定路由案例、标明合成性质的政策资料
├── tests/          v2 单测、真实 Redis 故障/并发测试、HTTP 集成测试
├── legacy_tests/   原有 45 项回归测试的版本化副本
├── scripts/        可重复执行的评测入口
├── reports/        经检查的基线、回归、对照和小规模模型报告
│   └── live/       在线评测完整报告，Git 忽略
├── traces/         请求元数据轨迹，Git 忽略
└── tmp/            临时 Redis、Chroma、日志等，Git 忽略
```

Trace 只记录阶段、耗时、token 用量、工具名、参数字段名、证据 ID/版本和终止原因，不记录问题、回答、原始用户 ID 或凭据。后台记忆维护为有界的尽力执行队列，进程重启可能丢失待维护任务；已原子提交的对话不受影响。

Redis 使用 `v2:` 命名空间，Chroma 使用 `hello_hebut_v2`、`episodic_v2`、`user_profile_v2`；默认不迁移 v1 会话。请求回执和匿名令牌有效期为 24 小时，超过回执有效期不保证请求去重。令牌是匿名身份凭据，后续账号体系和跨设备长期记忆另行实现。

## 验证命令

从项目根目录执行，依赖安装使用现有 `requirements.txt`。真实 Redis 测试需要 PATH 中存在 `redis-server`；测试自行启动随机端口的专用 Redis 并清理，不连接或清空已有实例。

```powershell
python -X utf8 experiments/v2/scripts/run_tests.py --suite experiments/v2/legacy_tests --report experiments/v2/reports/legacy_after.json
python -X utf8 experiments/v2/scripts/run_tests.py --report experiments/v2/reports/regression.json
python -X utf8 experiments/v2/scripts/compare_routing.py
```

使用 `.env` 中已有模型配置执行小规模在线冒烟，产生模型调用费用：

```powershell
python -X utf8 experiments/v2/scripts/live_smoke.py
```

报告口径：

`commit` 记录执行时的 Git HEAD；提交前运行的报告以 `source_hashes` 和 `test_hashes` 标识实际受测文件。

- [baseline.json](reports/baseline.json)：修改业务代码前原有 45 项离线测试通过，保留当时源码哈希。
- [legacy_after.json](reports/legacy_after.json)、[regression.json](reports/regression.json)：最终回归结果，以报告中的测试数、失败数、源码/测试哈希为准。
- [routing_comparison.json](reports/routing_comparison.json)：8 个固定案例，原 ChatService 必需工具路由 4/8，v2 为 8/8。使用脚本化意图/回答和真实 CSV，只衡量工具路由，不能解释为整体回答准确率提升。
- [live_smoke.json](reports/live_smoke.json)：真实模型、合成政策资料的两例冒烟通过；验证跨年证据充分与缺失年份拒绝放行，不代表正式检索指标或全面注入防御评测。

本轮前端构建及 Compose 配置解析已通过。当前机器 Docker Engine 未运行，Anaconda 的 ONNX Runtime DLL 无法初始化，因此尚未完成完整 Docker 服务启动和真实 Chroma 向量召回的端到端验收。Redis 集成测试使用真实独立服务；LLM 冒烟使用真实模型服务，均未以模拟结果冒充完整部署验证。

## 启动 v2

Compose 项目名和容器名均使用 `hellohebut-v2`，宿主机端口与 v1 分开，网段由 Docker 分配；资料目录/CSV 只读共享，运行数据和评测产物使用本目录。

```powershell
docker compose config --quiet
docker compose up -d --build
docker compose ps -a
```

默认网页为 `http://localhost:8088`，后端为 `http://localhost:18000`，Chroma 为 `18001`、Redis 为 `16379`、Prometheus 为 `19090`。容器内仍使用 `chromadb:8000`、`redis:6379`；宿主机直跑 Python 时自行配置对应地址。

单独开发前端可在 `frontend/` 执行 `npm run dev`，Vite 默认代理到 `localhost:18000`；其他后端地址通过 `VITE_PYTHON_API_URL` 指定。

聊天 API 新增身份握手；前端自动完成。直接调用示例：

```powershell
$session = Invoke-RestMethod -Method Post http://localhost:18000/session
$headers = @{ Authorization = "Bearer $($session.session_token)" }
$requestId = [guid]::NewGuid().ToString()
$body = @{
  message = "河北物理类位次12000，计算机和电气怎么选？"
  client_request_id = $requestId
} | ConvertTo-Json
$answer = Invoke-RestMethod -Uri http://localhost:18000/chat -Method Post `
  -Headers $headers -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
$answer
```

追问复用令牌及 `conv_id`，生成新的请求 ID；同一消息超时重试沿用原 ID。首次消息没有 `conv_id` 时，服务端根据已验证身份和请求 ID 稳定生成会话 ID。同 ID 改消息返回 409，未提供有效令牌返回 401，伪造 `user_id` 返回 403。

可设置 `HELLOHEBUT_AGENTIC_RAG=false` 回退为原检索流程，身份、原子提交与 CSV 必需调用继续生效。`V2_CHAT_TIMEOUT_S` 默认 50 秒；补检索最多 3 轮/6 个查询，每请求最多 8 次工具调用和 16 次模型调用。模型核验不可用时保守返回原始证据/缺项，不标记核验通过。

下一轮先补真实 Chroma 部署验收、人工标注检索集和资料版本发布，再推进长期记忆与 Checkpoint；按照每轮实测结果调整路线。
