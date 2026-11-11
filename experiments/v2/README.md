# v2 实施与验证

测试、数据集、报告和中间产物集中在本目录。业务代码复用根目录，新增 `workflows/`；原本地 `mcp/` 改名 `tooling/`，避免与标准 SDK 同名。目标见 [精进方案](../../AGENT_EVOLUTION_PLAN.md)，批次历史见 [阶段更新](STAGE_UPDATES.md)，流程见 [main_flow](../../main_flow.md)。

## 当前交付

关系数据库统一使用 **MySQL 8.4 + InnoDB + asyncmy**。第 1—6 阶段核心工程链路已接通；人工独立质量指标和外部服务验收不等同于功能测试通过。

| 能力 | 当前行为 | 入口 |
| --- | --- | --- |
| 条件与必需事实 | TaskState 保留来源，换省份/考生失效旧成绩，复合数字问题必查 CSV | [task_state](../../core/task_state.py)、[admission_answer](../../core/admission_answer.py) |
| Agentic RAG | 按年规划、混合召回、重排、证据覆盖、有限补查、回答核验，最多修复一次 | [agentic](../../rag/agentic.py)、[knowledge_base](../../tooling/knowledge_base.py) |
| 可验证偏好 | 原话生成待确认候选，确认后跨会话读取、更正、删除、过期 | [fact_store](../../memory/fact_store.py)、[偏好界面](../../frontend/src/components/PreferencePanel.vue) |
| 持久工作流 | route → required_tools → retrieve → answer → verify → finalize；MySQL JSON 快照、租约与恢复预算 | [chat_graph](../../workflows/chat_graph.py)、[checkpoints](../../workflows/checkpoints.py) |
| 提交与后台任务 | 对话/状态/回执/维护 outbox 同事务；chat/evaluation/maintenance 队列、租约重领、最多三次尝试 | [mysql_turn_store](../../memory/mysql_turn_store.py)、[job_store](../../memory/job_store.py) |
| 角色与 Skills | 默认单角色，可选一主一辅与结构化 Composer；冲突不输出争议事实；YAML 校验与引用渐进读取 | [orchestrator](../../agents/agent_orchestrator.py)、[Skills](../../skills/README.md) |
| 有界工具循环 | 每角色最多三次模型轮次，只读本轮证据和已匹配 Skill 引用；共享请求预算、Schema 与上下文限制 | [read_tools](../../agents/read_tools.py)、[context_budget](../../core/context_budget.py) |
| 标准 MCP | SDK stdio initialize/list/call；知识检索、CSV 查询、版本统计三个只读工具 | [server](../../tooling/mcp_server.py)、[client](../../tooling/mcp_client.py) |
| 交互与观察 | 前端进度、最终答案、断线查询、条件纠正；逐请求 Prometheus、可选 Langfuse OTLP | [task_routes](../../api/task_routes.py)、[tracing](../../monitor/tracing.py) |

CLI `python -m api.main --cli` 也复用 ChatService，每次启动创建新身份，不提供跨设备账号恢复。

## 启动与开关

部署见 [DEPLOY.md](../../DEPLOY.md)。本机页面 `http://localhost:8088`，API 文档 `http://localhost:18000/docs`。

| 配置 | 默认与用途 |
| --- | --- |
| HELLOHEBUT_CHAT_GRAPH | true；false 对照保留的顺序路径，MySQL 账本仍生效 |
| HELLOHEBUT_AGENTIC_RAG | true；false 对照原检索流程，CSV 必需调用仍生效 |
| HELLOHEBUT_ROLE_MODE | single；composed 一主一辅，legacy_concat 仅供旧拼接对照 |
| HELLOHEBUT_TOOL_LOOP | false；真实协议探测通过，尚无独立收益证据，按需启用 |
| HELLOHEBUT_CONTEXT_MAX_INPUT_TOKENS | 8000；UTF-8 字节作保守 token 上界，必需内容超限则失败 |
| V2_WORKERS | 2，限制 1—4；与 API 同进程，状态在 MySQL |
| V2_CHAT_TIMEOUT_S | 50；最多 8 次工具、16 次模型调用，恢复不重置额度 |
| RAG_LEXICAL_MODE / RAG_FUSION_MODE | filtered_legacy / balanced，保留 BM25 与旧融合对照 |
| LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY | 默认空，三项齐全才启用 |
| V2_INPUT_PRICE_PER_MILLION / V2_OUTPUT_PRICE_PER_MILLION | 默认空，费用为 null；填合同单价后估算，币种由 V2_PRICE_CURRENCY 指定 |

Langfuse 使用当前支持的 [OTLP/HTTP JSON](https://langfuse.com/integrations/native/opentelemetry) 与 v4 ingestion header，只导出阶段、状态、计数、耗时，不上传问题、答案、身份、偏好或隐藏思考。队列有界，失败不阻断聊天；未配置真实项目，仅通过模拟 HTTP 契约测试。

## 会话、任务与恢复

先 POST /session 获取匿名 Bearer 令牌，权限绑定服务端身份。首次没有 conv_id 时根据身份和请求 ID 稳定生成；不同内容复用请求 ID 返回冲突。

| 接口 | 用途 |
| --- | --- |
| POST /chat/tasks | message、client_request_id、可选 conv_id；202 返回 run_id |
| GET /chat/tasks/{run_id} | 本人任务状态和已完成结果 |
| GET /chat/tasks/{run_id}/events | Bearer SSE，after_sequence 或 Last-Event-ID 恢复，仅最终事件含答案 |
| POST /chat | 同步兼容入口，不经过任务队列 |
| GET /conversations/{conv_id}/state | 已提交结构化条件 |
| DELETE /conversations/{conv_id} | 清空会话，取消关联任务并擦除载荷/答案，保留防复活墓碑 |
| GET/POST /memories | 本人偏好，确认/更正/删除详见 /docs |
| POST /eval/jobs、GET /eval/jobs/{run_id} | 管理令牌提交/查询评测，X-Request-ID 可用于幂等 |
| /live、/ready | 进程存活；MySQL/Redis/Chroma 依赖就绪 |

前端保留 run_id 与事件序号，刷新后可“查询未完成任务”；断线查询原任务。点击条件栏可发送更正。“新对话”仅切换会话，不删除服务器内容。escalated 表示建议咨询招生办，没有自动联系真人。

LangGraph 在**已完成节点边界**恢复，retrieve 内多次查询仍属一个节点；中断的外部调用可能重做。Checkpoint 不保证外部调用 exactly-once，租约与事务保证整轮提交不重复。图快照要求同请求、有效版本、未变 revision 且 1 小时内；过期/版本变化会丢弃节点状态，但已用额度保留。已经计算并核验完成的答案快照按 revision/TTL 恢复原结果，不因图版本变化重新生成；已提交回执始终回放原答案，需要新资料/偏好时新建请求。

MySQL 是事实与聊天权威来源；Redis 保存身份与知识活动清单，Chroma 保存知识索引。旧 Redis 仅首次懒导入尚存内容，MySQL 故障不回退写 Redis。旧 PG 已停用，历史卷保留，当前无需 PostgreSQL 依赖或服务。

维护由与聊天同事务的 outbox 产生，在会话租约内核对回执并捕获偏好。清空与任务取消同事务，旧 worker 不得重建内容。评测使用 hellohebut_evaluation_v2 独立 SQL 命名空间并关闭长期偏好。

## 统一验证

汇总见 [stage6_validation.json](reports/stage6_validation.json)。报告保存源码/数据指纹，commit 是测试启动时 HEAD。历史失败报告保留；小样本真实模型通过仅说明这些案例通过。

```powershell
python -X utf8 experiments/v2/scripts/run_tests.py --suite experiments/v2/legacy_tests --report experiments/v2/reports/stage6_legacy.json
python -X utf8 experiments/v2/scripts/run_tests.py --report experiments/v2/reports/stage6_regression.json
node experiments/v2/scripts/frontend_checks.mjs
python -X utf8 experiments/v2/scripts/fact_store_checks.py
python -X utf8 experiments/v2/scripts/turn_store_checks.py
python -X utf8 experiments/v2/scripts/graph_checks.py
python -X utf8 experiments/v2/scripts/job_checks.py
python -X utf8 experiments/v2/scripts/container_checks.py --report experiments/v2/reports/stage6_storage_retrieval.json
# 以下调用真实模型，产生用量
python -X utf8 experiments/v2/scripts/deployment_smoke.py --build-mode reused_local_base --report experiments/v2/reports/stage6_deployment.json
python -X utf8 experiments/v2/scripts/task_http_smoke.py
# 下条重启 MySQL/应用，单独执行
python -X utf8 experiments/v2/scripts/memory_restart_smoke.py
```

离线回归需 PATH 有 redis-server，自行创建随机端口实例。数据库脚本只创建/清理隔离表和身份。CI 使用临时 MySQL/Redis，离线任何 skip 均失败；远程状态以 GitHub 结果为准。

## 对照与人工复核

- [角色消融](reports/stage6_roles.json)：同一脚本化输出比较 single/legacy_concat/composed、冲突/去重与裁剪，验证程序契约，不是模型质量提升。[协议探测](reports/stage6_model_capability.json) 证实当前模型支持配对工具交互。
- [MCP](reports/stage6_mcp_protocol.json)：真实 stdio、三个工具、Schema/权限/大小限制、资料版本不变。运行 `docker compose exec -T hellohebut python experiments/v2/scripts/mcp_checks.py`；在线问答仍直接调用本地工具。
- [Embedding](reports/stage6_embedding_ablation.json)：冻结 generation16 的 205 片，MiniLM 384 维与 BGE-small-zh-v1.5 512 维；官方 revision/SHA256、CPU 环境及每查询三次重复均记录。已有开发集不是独立人工集，BGE-M3 未运行。
- 默认混合检索保持 filtered_legacy + balanced。BGE 单路改善开发例，但旧 384 维集合不能混入 512 维；候选通过独立集后需新集合重建、验证、切换并保留旧集合回退。
- [人工材料与冻结门槛](datasets/README.md) 提供 30 条审阅草案。开发题不能改标签冒充独立集，人工需另建未曝光题，提供来源与 reviewer。benchmark_id 固定用例/评分/Judge，run_manifest 单独记录实现。

Embedding 模型/环境/索引均在 tmp/，不进入生产依赖。embedding_ablation.py --prepare 下载、--freeze 冻结；正式运行需 torch2.6.0+cpu、transformers4.46.3、tokenizers0.20.3、safetensors0.7.0 隔离环境，指定 --corpus <冻结JSON> --repeats 3。

## 产物与边界

datasets/、tests/、legacy_tests/、scripts/、reports/ 纳入版本控制；tmp/、traces/、reports/live/ 忽略。模型下载、环境、运行日志和完整在线评测不进入仓库。

仍需人工/环境验收：独立参考真值、至少 30 例 Judge 人工校准、真实 Langfuse 连接、页面视觉检查、标准全新 Docker 构建。本机镜像源元数据异常按用户要求保留，复用本地基础镜像完成运行验证，不代表标准构建通过。

匿名令牌 24 小时后不能恢复身份；无账号/跨设备同步。偏好首版限专业、兴趣、费用、地域四类，候选 7 天、确认默认 180 天，逻辑过期不等于删除。聊天/回执/图/任务尚无周期归档清理，1 小时 TTL 仅限制图恢复。费用估算不含缓存折扣、服务商调整和中断调用未返回用量。上述边界应保留在展示材料中。
