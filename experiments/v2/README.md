# v2 分阶段实施与验证

本目录集中保存评测、测试、报告和运行中间产物；业务实现复用根目录的 `core/`、`memory/`、`mcp/`、`api/`，仅新增 `rag/` 检索控制模块。方案见 [AGENT_EVOLUTION_PLAN.md](../../AGENT_EVOLUTION_PLAN.md)。

每批完成后的简要更新与审查状态见 [STAGE_UPDATES.md](STAGE_UPDATES.md)，本页只维护当前运行和验证说明。

## 本轮范围与状态

按讨论后的依赖顺序实施前三个落地步骤：基础契约与基线、关键可靠性、Agentic RAG 首版。它们覆盖六周方案前两阶段的核心链路，并提前修复会话可靠性；不代表六周方案的全部验收项已完成。

| 步骤 | 已实现 | 主要代码 |
| --- | --- | --- |
| A：基础契约与基线 | TaskState、字段来源与条件失效、工具完整 Schema 校验、请求预算、脱敏元数据 Trace、必需 CSV 调用 | [contracts](../../core/contracts.py)、[task_state](../../core/task_state.py)、[runtime](../../core/runtime.py)、[ChatService](../../core/chat_service.py) |
| B：关键可靠性 | 匿名会话令牌、请求回执、同 ID 内容冲突、会话租约、整轮原子提交、摘要失败保留原文、并发压缩保护、有界维护队列 | [turn_store](../../memory/turn_store.py)、[conversation_memory](../../memory/conversation_memory.py)、[API](../../api/main.py) |
| C：Agentic RAG 与知识版本 | 按年份/子问题规划、证据核验与有限补查、确定性 CSV 短路径、回答核验/回退、原文展示、活动版本原子切换与回退 | [admission_answer](../../core/admission_answer.py)、[agentic](../../rag/agentic.py)、[knowledge_base](../../mcp/knowledge_base.py)、[knowledge_catalog](../../mcp/knowledge_catalog.py) |

关键行为：复合问题只要需要录取数字，就先查 CSV；查询失败或条件不足时直接说明/澄清。更换省份、科类、考试年度或考生后，相关成绩字段失效，不从旧历史文本重新补回。回答必须引用本次真实证据，数字先检查是否出现在依据中，再由模型检查对应关系；这仍不是形式化的语义正确性证明。

纯数字问题使用 CSV 固定模板生成，`verification_method=deterministic_csv` 表示程序确保数字与工具记录一致；混合问题继续执行证据核验。引用记录文档、修订和片段 ID、哈希与本次原文，前端可展开核对。

当前仍待实施：BM25/Embedding 的正式对照、带来源的长期事实库、PostgreSQL、LangGraph Checkpoint、标准 MCP、Composer 与动态 Skills。已有摘要/画像只修复了写入可靠性，不能称为“可验证长期记忆已完成”。模型驱动的通用工具循环也尚未接入角色 Agent。

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
python -X utf8 experiments/v2/scripts/run_tests.py --suite experiments/v2/legacy_tests --report experiments/v2/reports/stage2_legacy.json
python -X utf8 experiments/v2/scripts/run_tests.py --report experiments/v2/reports/stage2_regression.json
python -X utf8 experiments/v2/scripts/compare_routing.py
```

使用 `.env` 中已有模型配置执行小规模在线冒烟，产生模型调用费用：

```powershell
python -X utf8 experiments/v2/scripts/live_smoke.py
```

报告口径：

`commit` 记录执行时的 Git HEAD；提交前运行的报告以 `source_hashes` 和 `test_hashes` 标识实际受测文件。

- [baseline.json](reports/baseline.json)：修改业务代码前原有 45 项离线测试通过，保留当时源码哈希。
- [stage2_legacy.json](reports/stage2_legacy.json)、[stage2_regression.json](reports/stage2_regression.json)：当前批次 45＋50 项回归；不带 stage2 前缀的报告保留首批结果。
- [routing_comparison.json](reports/routing_comparison.json)：8 个固定案例，原 ChatService 必需工具路由 4/8，v2 为 8/8。使用脚本化意图/回答和真实 CSV，只衡量工具路由，不能解释为整体回答准确率提升。
- [live_smoke.json](reports/live_smoke.json)：真实模型、合成政策资料的两例冒烟通过；验证跨年证据充分与缺失年份拒绝放行，不代表正式检索指标或全面注入防御评测。

五个 v2 服务健康。最新 [容器验收](reports/stage2_deployment.json) 6/6：健康/代理/鉴权、向量检索、CSV 完整回答与幂等、条件变更、住宿费有证据回答、学费缺资料拒答。[首批 4/5 报告](reports/deployment_smoke.json) 保留；新版增加有具体原文金额的正向例，将原学费问题明确标注为缺资料，未把它冒充可回答问题。

[真实存储与检索报告](reports/stage2_storage_retrieval.json) 中，4 项真实 Chroma/Redis 发布、故障、快照和回退检查通过。6 条开发案例包含 4 条可回答问题（Recall@8=75%、nDCG@8=0.375）和 2 条缺年份问题（均为空）；前者仍有校区漏召回。该集合从已收录正文整理，待人工复核，未使用重排，不代表独立测试集或最终答案准确率。

标准 `docker compose up -d --build` 遇到基础镜像元数据长度校验错误。本次复用本机 `hellohebut-hellohebut:latest` 与 `hellohebut-frontend:latest`，补装 `jsonschema==4.19.2` 并覆盖 v2 源码/前端产物完成验证；临时 Dockerfile、忽略规则和 Compose 覆盖位于 Git 忽略的 `tmp/`。正式全新构建仍待验收，现有 v2 镜像可用 `docker compose up -d --no-build` 启动。

运行中的服务可用以下脚本复测（第一条调用真实模型并产生费用；第二条创建并清理独立测试集合，对项目资料只读）：

```powershell
python -X utf8 experiments/v2/scripts/deployment_smoke.py --build-mode reused_local_base
python -X utf8 experiments/v2/scripts/container_checks.py
```

## 知识发布与回退

导入统一为“写不可变修订 → 读回校验 → Redis CAS 切换活动清单”。并发发布冲突返回 409；失败和退休片段留在 Chroma，但检索只接受活动清单。每个聊天请求固定一份快照，缓存键包含发布代际，因此其他进程发布后新请求也不会误用旧缓存。Redis 清单是当前权威数据，需连同现有 AOF 数据卷持久化；后续再统一迁入 PostgreSQL。

`GET /knowledge/stats` 返回活动片段数、发布代际和可回退代际。`POST /knowledge/add`、`/knowledge/upload` 统一发布；`POST /knowledge/rollback` 接收 `{"generation": 目标代际}`，发布一个新代际指向历史内容。最近五次历史支持回退，旧修订暂不自动清理，以免破坏正在执行的快照；因此 Chroma 原始 count 可能大于活动 count。已有无修订标签的片段不会参与 v2 活动检索。

默认文档身份由来源、标题、年份生成；改标题且需替换同一逻辑文档时显式传稳定的 `document_id`。整份部署文件重导入会替换该文件的文档集合。`valid_from/valid_until` 目前只保存元数据，尚未实现自动过期调度；年份过滤仍按明确的 `effective_year`。

管理操作统一要求 `X-Admin-Token`；未设置 `ADMIN_API_TOKEN` 时返回 503，不再默认开放写入。发布回退同样需要令牌。普通 `/session` 与 `/chat` 不受此设置影响。

## ONNX 在哪里使用

`chromadb==0.5.23` 的默认 Embedding 函数使用 ONNX Runtime 运行 `all-MiniLM-L6-v2`，输出 384 维向量。项目没有显式传入向量时，Python 客户端负责计算，即使数据库使用 `HttpClient` 连接远端 Chroma 也是如此。

| 位置 | 触发向量化的操作 |
| --- | --- |
| `mcp/knowledge_base.py` | 文档 `upsert(documents=...)` 与语义检索 `query(query_texts=...)` |
| `memory/conversation_memory.py` | 历史摘要入库、相关历史检索、用户画像 `upsert(documents=...)`；画像读取本身按用户过滤，不做语义检索 |
| `Dockerfile` | 预下载模型并复制到应用用户缓存，避免运行时下载 |

CSV 查询、Redis 状态/幂等和远端大模型生成本身不使用 ONNX。此前错误来自 Windows Anaconda 的 DLL 加载；容器使用独立 Linux 环境，ONNX Runtime 1.27.0 已完成真实推理，因此 Docker 部署不需要先修复该 Windows 环境。

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

下一批先补标准构建、检索漏召回与开发集人工复核，再评估是否进入长期记忆；先等待本批审查。
