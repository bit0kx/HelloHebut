# HelloHebut v2 技术实现

本文说明当前实现的模块职责与边界；执行顺序见[主要流程](main_flow.md)，运行配置、实验报告和验收入口见 [v2 实施与验证指南](experiments/v2/README.md)。历史阶段记录不作为当前架构说明。

## 1. 技术与功能对应

| 技术 | 当前用途 | 主要入口 |
| --- | --- | --- |
| FastAPI、Pydantic | 会话鉴权、请求校验、聊天/评测任务、SSE、管理接口 | [api/main.py](api/main.py)、[api/task_routes.py](api/task_routes.py) |
| LangGraph | 六节点聊天流程、条件短路径、节点检查点恢复 | [workflows/chat_graph.py](workflows/chat_graph.py) |
| MySQL、asyncmy | 事实版本、整轮账本与图状态、持久任务和原子 outbox | [memory/](memory/) |
| Redis | Bearer 会话、知识发布清单和旧会话迁移读取 | [memory/turn_store.py](memory/turn_store.py)、[tooling/knowledge_catalog.py](tooling/knowledge_catalog.py) |
| Chroma、词法检索、RRF | 资料向量与关键词召回、版本隔离、融合与重排 | [tooling/knowledge_base.py](tooling/knowledge_base.py)、[tooling/lexical_retrieval.py](tooling/lexical_retrieval.py) |
| Agentic RAG | 子问题规划、按年份检索、证据充分性与答案核验 | [rag/agentic.py](rag/agentic.py) |
| CSV 确定性计算 | 历史录取记录、位次比较、风险等级与数字短路径 | [tooling/admission_data.py](tooling/admission_data.py)、[core/admission_answer.py](core/admission_answer.py) |
| Anthropic Messages 协议 | 统一模型调用、供应商兼容参数、用量统计 | [core/llm_client.py](core/llm_client.py) |
| 角色路由、Composer、Skills | 默认单角色、可选结构化协作、按需注入业务规则 | [agents/](agents/)、[core/skill_loader.py](core/skill_loader.py) |
| MCP SDK、stdio | 独立的标准只读工具 Server/Client | [tooling/mcp_server.py](tooling/mcp_server.py)、[tooling/mcp_client.py](tooling/mcp_client.py) |
| Prometheus、OTLP/HTTP JSON | 单请求指标、本地 Trace、可选 Langfuse 元数据导出 | [core/runtime.py](core/runtime.py)、[monitor/tracing.py](monitor/tracing.py) |
| Vue、Vite、Nginx、Docker | 聊天与偏好管理界面、请求代理和服务部署 | [frontend/](frontend/)、[docker-compose.yml](docker-compose.yml) |

主应用使用进程内 `tooling.tool_manager.MCPToolManager`；类名保留兼容，但标准 MCP 是单独的 stdio 模块。当前聊天请求不会因为存在 MCP Server 就自动改为跨进程调用。

## 2. 工作流、预算与恢复

API 默认启用 `HELLOHEBUT_CHAT_GRAPH=true`。节点依次为 `route → required_tools → retrieve → answer → verify → finalize`；偏好回应、条件澄清和纯数字回答可提前转入 `finalize`。关闭图的旧流程只作兼容和对照入口。

图状态保存业务条件、必需工具结果、证据及来源版本、上下文、回答与核验结果。`finalize` 不提交业务记录；ChatService 在其后保存答案快照，再执行整轮事务。

### 持久化契约

[MySQLCheckpointSaver](workflows/checkpoints.py) 实现 LangGraph 异步 checkpoint 与 pending-writes 契约。序列化只接受 JSON，不使用 pickle 或动态对象恢复；只支持本流程的根命名空间，不提供用户任意读取 thread 的接口。

检查点按用户/会话哈希与请求哈希隔离；恢复必须匹配消息内容、会话基础版本、工作流版本和一小时 TTL。每次写检查点或预算都在 MySQL 事务内校验租约 token。已提交回执优先于图恢复；清空会话删除图状态并保留旧 Redis 已迁移墓碑，防止旧数据重新导入。

工作流版本是受控源码、依赖清单、Skill 正文与参考文件、CSV 内容及已加载运行规则的内容指纹，不包含时间戳或绝对路径。修改条件合并、提示词、角色模式或数据后，不能继续使用旧规则下的中间图状态。

上述指纹只约束中间图恢复。已完成核验并存为 `computed` 的答案快照仍按会话基础版本与有效期恢复，不因部署版本改变而重新生成；已提交回执也始终优先。

默认每请求最多 8 次工具调用、16 次模型调用、50 秒执行预算；RAG 另限制最多 3 轮、6 个子查询。实际配置和调用过程还受工具超时、输出大小及上下文预算约束。模型/工具额度在外部调用之前持久化；重试以及失效检查点重新起图都不重置同请求已用额度。

恢复保证到已完成节点：`retrieve` 内部循环和 `verify` 内部修复不是更细的恢复点。外部调用中断后可能重复，无法承诺模型计费 exactly-once；未返回的供应商 token 用量也可能缺失。图状态包含实际对话与证据，应作为业务私有数据管理，不能等同于仅含元数据的 Trace。

**存储保留边界：**一小时 TTL 约束是否允许恢复，不自动物理删除历史图。清空会话会删除检查点、取消并擦除相关任务内容；尚无按时间自动清理全部历史会话/图/job 的后台策略，长期部署需单独制定保留与归档规则。

## 3. 数据职责与一致性

| MySQL 职责 | 数据与保证 |
| --- | --- |
| 可验证事实 | 偏好候选、来源回执、确认状态、更正版本、删除/过期；按用户隔离，只有已确认有效事实进入上下文 |
| 对话与图执行 | `TaskState`、问答、摘要、幂等回执、答案快照、图 checkpoints/pending writes/预算；会话租约与版本防止过期提交 |
| 后台任务 | 聊天、评测、维护 jobs；有界 worker、服务端时钟租约、最多三次尝试、阶段事件和最终结果 |

用户消息、助手最终回答、业务状态、回执和维护 outbox 同事务提交。图与业务提交不假装是一个事务，依靠“先回执、再答案快照、再图恢复”的顺序处理故障。

后台维护任务在会话租约内检查来源回执并捕获偏好，摘要再通过前缀/版本比较提交。清空会话与任务取消同事务完成，旧 worker 的 token 失效；新的任务入队与清空通过会话行锁排序。已确认偏好的删除通过 `/memories` 独立执行。

Redis 不再是线上对话写入的权威库。存量 Redis 会话只在首次读取时迁移；MySQL 出错不会回退到 Redis 写入。旧情景/画像代码保留兼容，但当前线上可验证记忆模式不依赖模型自动抽取的 Chroma 画像作为长期事实。

## 4. 条件、录取数据与证据

### 业务条件与 CSV

[TaskState](core/contracts.py) 记录实体、来源、修订号和失效字段；[merge_state](core/task_state.py) 将当前明确条件覆盖到历史状态。更换考生、省份、年份或考试条件时清除不适用的分数/位次，避免从摘要中复活旧条件。

录取需求检测独立于意图路由，先查 CSV 再回答。结果包括 `ok`、`needs_clarification`、`unsupported`、`not_found`、`unavailable`；缺记录不解释为学校不招生。数据覆盖及版本以 `/admission/stats` 为准，不在文档重复维护快照行数。

风险优先比较位次：设有效年份数为 `n`，达到历史最低位次的年份数为 `h`，历史最低位次中位数为 `m`，领先比例 `r=(m-考生位次)/m`。少于两年为数据不足；全部达到且 `r≥8%` 为相对稳妥；达到至少三分之二且 `r≥0` 为相对匹配；命中一年或 `r≥-5%` 为可冲；其余偏冲。只有分数时使用更低置信的分数比较规则。等级由程序计算，不是录取概率预测。

### 资料发布与检索

[knowledge_indexing](tooling/knowledge_indexing.py) 保留学院、专业、章节和来源信息；向量索引用文本与展示原文分别保存。文档修订先写入 Chroma，再切换 Redis 知识清单；查询固定清单 generation，缓存键包含发布范围，旧版本不会混入新请求。管理写入受管理员令牌与来源约束保护，但来源域名合法不能证明正文与官网一致。

召回组合向量与词法通道，通过 RRF、多查询合并和 LLM 重排形成候选。默认词法模式为 `filtered_legacy`；BM25 和其他融合方式保留为实验配置，不将“已实现”写成“已证明更优”。默认 Chroma embedding 仍可能使用内置 ONNX 模型；中文 embedding 对照独立运行，未自动替换线上模型。

Agentic RAG 将候选转为有 ID、正文哈希、年份和版本的证据，检查指定年份覆盖、缺失事实和冲突，再决定回答、补查或弃答。最终核验同时约束数字、有效引用编号和语义支持；最多一次回答修复。引用清理仅是其中一项检查，不能单独证明回答可信。

## 5. 角色、Skills 与工具边界

意图识别组合模型、Pattern 与可用的模板匹配，实体提取仅采纳当前明确条件。General、Policy、Risk、Planning 分别处理通用、政策、风险和规划问题；当前默认 `HELLOHEBUT_ROLE_MODE=single`。可选 composed 模式最多主辅两个角色，输出结构化主张与证据 ID，由 Composer 去重、区分建议和事实、排除冲突，再交最终核验；不靠多数投票判定事实。

Skills 使用受控 YAML 元数据，先提供目录，再注入匹配正文；参考文件仅在当前 Skill 已激活且路径已声明时读取。版本、依赖、文件 hash 与加载错误可追踪；动态加载不执行 Skill 内脚本。格式和目录维护见 [skills/README.md](skills/README.md)。

`HELLOHEBUT_TOOL_LOOP` 默认关闭。启用后使用 Messages `tool_use/tool_result` 循环，只开放本请求的 `request_evidence_read` 和当前 Skill 的 `skill_reference_read`。角色不能通过此入口重新检索或自行查询 CSV，防止新增证据绕过统一核验。

ToolManager 统一处理 JSON Schema、角色白名单、只读约束、调用预算、超时、结果大小、缓存和熔断。上下文组装保护系统规则、当前问题与必需事实，按预算裁剪可选 Skills 和完整历史轮次；超出无法裁剪的核心输入时明确失败，不悄悄截断当前问题。

独立 MCP Server 通过官方 SDK 提供公共只读数据工具，支持工具发现、调用和结构化结果；Client 使用固定模块启动受控子进程。stdio 的本地进程权限是信任边界，没有远程 HTTP MCP 鉴权或外部写工具。相关协议验证入口见 [v2 指南](experiments/v2/README.md)。

## 6. 接口、观测与验收

| 接口组 | 权限与职责 |
| --- | --- |
| `/session` | 创建匿名 Bearer 身份；不等同于跨设备账号系统 |
| `/chat`、`/chat/tasks`、任务状态与 `/events` | 会话鉴权；同步与持久队列共用 ChatService；SSE 只有提交后的 final 携带答案 |
| `/conversations/{conv_id}/state`、`DELETE /conversations/{conv_id}` | 仅操作当前身份的条件与会话 |
| `/memories` | 当前用户的候选确认、保存、更正、删除与历史查询 |
| 知识写入、Skills 重载、`/eval/run`、`/eval/jobs` | `X-Admin-Token` 管理权限 |
| `/live`、`/ready`、`/health` | 进程存活、依赖就绪、组件统计；三者语义不同 |
| `/metrics`、`/monitor` | Prometheus 指标与运行状态 |

SSE 事件使用递增序号，重连不会重复发送已消费 final，并在轮询时重新验证会话。任务结果来自业务提交后的返回值；若提交成功但 job 完成标记丢失，下一次消费优先读取回执。

即使第三次 worker 在业务提交后退出，也先按所属用户、会话、请求和消息内容核对已提交回执，再恢复任务为完成；没有对应回执才标记尝试次数耗尽，不为恢复结果增加模型调用。

本地 JSON Trace 与单请求指标记录阶段、耗时、用量和核验元数据。设置 `LANGFUSE_HOST`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 后可启用 OTLP/HTTP JSON connector；它不是完整 Langfuse SDK 集成，采用有界队列与尽力投递，不上传问题、答案、身份、偏好值或隐藏推理，导出故障不阻塞聊天。成本按配置单价估算，不能替代供应商账单。

评测复用业务服务，使用独立编排器、工具管理器和会话命名空间；`persist_memory=False` 走内存图。离线断言、真实存储故障检查、真实模型质量评测分别报告，不能用基础设施通过率替代答案质量。CI 执行 legacy/v2 零跳过回归、MySQL 隔离检查与前端构建；模型与外部服务效果见 [v2 实施与验证指南](experiments/v2/README.md)，本文不重复列历史测试数字。
