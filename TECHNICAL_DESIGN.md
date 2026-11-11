# HelloHebut 技术实现与接口索引

本页记录 v1 技术基线；`v2` 已实现的接口、状态、可靠性和检索变化见 [v2 实施与验证指南](experiments/v2/README.md)。

本文集中说明技术、参数与实现边界；执行顺序见[主要流程](main_flow.md)，操作命令见[部署文档](DEPLOY.md)。

## 1. 技术与功能对应关系

| 技术 | 作用 | 代码入口 |
| --- | --- | --- |
| FastAPI、Pydantic | 生命周期组装、请求模型与接口 | [api/main.py](api/main.py) |
| Anthropic Messages API | 统一模型调用；兼容参数、跳过 thinking 块、提取最终文本 | [core/llm_client.py](core/llm_client.py) |
| LLM + 模板向量 + Pattern | 意图投票、实体提取、紧急度判断 | [core/intent_recognizer.py](core/intent_recognizer.py) |
| 自定义 Agent、asyncio | 角色路由、并发协作、失败降级 | [agents/agent_orchestrator.py](agents/agent_orchestrator.py) |
| 动态 Skills | 业务规则匹配、注入和热加载 | [core/skill_loader.py](core/skill_loader.py) |
| 上下文切片 | 保留学院、专业、章节与正文关系 | [mcp/knowledge_indexing.py](mcp/knowledge_indexing.py) |
| ChromaDB、加权 RRF | 向量/关键词混合召回、资料版本管理 | [mcp/knowledge_base.py](mcp/knowledge_base.py) |
| Multi-query、LLM Rerank | 查询改写、候选合并、相关性重排 | [mcp/tool_manager.py](mcp/tool_manager.py) |
| CSV 与确定性规则 | 精确查询、中位数、位次差、风险等级 | [mcp/admission_data.py](mcp/admission_data.py) |
| Redis、ChromaDB | 最近消息、摘要、情景记忆和画像 | [memory/conversation_memory.py](memory/conversation_memory.py) |
| LLM Judge、业务断言 | 分类与回答质量评测、回归比较 | [evaluation/evaluator.py](evaluation/evaluator.py) |
| Prometheus、Z-score | 指标、异常告警、路由惩罚 | [monitor/performance_monitor.py](monitor/performance_monitor.py) |

[ChatService](core/chat_service.py) 控制业务流程：模型承担理解、改写、重排、回答、摘要、画像和评分；程序负责工具调用、数值计算、存储、引用编号与断言。`mcp/` 是本地工具层命名，当前未实现标准 MCP 协议。

## 2. 意图识别、Agent 与 Skills

### 2.1 意图投票

LLM 意图识别与启用的模板向量分支通过 `asyncio.gather` 并发，Pattern 本地匹配；随后实体提取另发一次模型请求。

```text
score(category) = Σ weight(source) × confidence(source)
取最高分类别；最高分 < 0.5 时归为 other
```

| 条件 | 通道与权重 |
| --- | --- |
| 未配置兼容 base_url | LLM 0.7、Embedding 0.2、Pattern 0.1 |
| 配置兼容 base_url | LLM 0.85、Pattern 0.15；模板向量分支关闭 |
| LLM 失败 | 优先可用的向量结果，再用关键词，否则 other |

当前 `AsyncAnthropic` 没有 Embeddings 资源时，模板匹配使用 256 维字符 n-gram 哈希向量；它不同于 Chroma 知识库的语义向量。`IntentResult.confidence` 保存 LLM 分支置信度，不是融合分数。`learn()` 只向进程内模板添加样本，无模型训练、公开接口或持久化。

实体提取只读取本轮明确提供的信息，字段为 `admission_year`、`province`、`exam_mode`、`subject_combination`、`batch`、`score`、`rank`、`major`、`college`、`campus`、`candidate_type`；缺失字段不应由模型推测。

### 2.2 角色路由与协作

下表同时列出角色和 RAG 类别，避免将两套路由混为一谈：

| 意图 | 单角色路由 | 优先知识类别 |
| --- | --- | --- |
| school_info | General | school_info |
| campus_life | General | campus_life |
| greeting / other | General | 不限定类别；纯寒暄跳过检索 |
| escalation | General，标记升级 | escalation |
| admission_policy | Policy | admission_policy |
| tuition | Policy | tuition |
| score_risk | Risk | score_risk |
| major_info | Planning | major_info |
| career | Planning | major_info |
| comparison | Planning | major_info、school_info |

常见边界：校区数量/地址属于学校信息，宿舍配置属于校园生活，住宿费属于费用；招生计划发布规则属于政策，具体计划人数属于 `score_risk`，但当前 CSV 不含计划人数字段。

复合问题检查先于单角色路由：按主意图和领域关键词选择 Policy、Risk、Planning，多个目标并行，成功回答按该顺序直接拼接，未设置总结 Agent。`agent_type` 取第一个协作目标，不是独立评分选出的主角色。

每个角色使用相同事实背景和各自的 system prompt / Skill，一次调用生成文本，没有 `tools` 参数或模型工具往返循环。专业角色失败后由 General 再尝试。单角色严重紧急请求交给 General；严重紧急度、人工意图或特定回答措辞产生 `escalated=true`，只作标记，不创建工单。

### 2.3 Skills

规则通过 system prompt 约束角色处理方式。内置文件、匹配条件、字符预算和热加载统一见 [Skills 说明](skills/README.md)。

## 3. RAG 的技术要点

### 3.1 索引与来源

学院文档逐行解析明确的学院、专业与章节标题，正文继承当前上下文；不做正文语义章节分类。目标切片长度 500 字符、无重叠窗口，按中文句号拆句，超长单句可能超过目标长度。

向量化的是 `index_text`：标题、类别、学院、专业、关键词、章节和正文；原文另存为 `raw_content`，用于重排与展示。片段 metadata 保留：

```text
title、category、keywords、college、major、section、
source_url、source_file、source_type、published_at、fetched_at、
effective_year、document_version、index_version、chunk_index、total_chunks
```

片段 ID 由来源、版本、标题、序号和正文前 80 字生成。部署目录按文件哈希与索引版本增量更新，先写新片段再清理旧版本；管理接口主要做片段级 upsert，不提供相同的整文件替换语义。

部署资料可没有 URL，依靠文件名与内容版本溯源；管理接口要求 HTTP(S) 的 `hebut.edu.cn` 或其子域 URL。域名校验不验证提交正文与网页内容是否一致。资料写入不主动失效检索缓存。

### 3.2 召回与重排

| 环节 | 当前策略 |
| --- | --- |
| 查询改写 | 请求生成 3 个改写，保留原查询并去重；失败只用原查询 |
| 范围选择 | 类别候选约 80%、全库候选至少 20%，按 4:1 交错且不足互补；无类别时仅全库 |
| 向量通道 | Chroma `query_texts` 语义检索 |
| 关键词通道 | 中文词块与 2/3 字符 n-gram；标题/章节/关键词命中权重高于正文，连续完整命中加分 |
| 通道融合 | 加权 RRF；每路另保留约 20% 候选，不是 BM25 |
| 多查询合并 | 按排名轮询，优先按 ID 去重；无 ID 时用来源、标题、版本、序号与正文哈希，去重键不含分数 |
| 重排 | 输入必要元数据与每项最多 600 字正文；模型打 0—10 分，低于阈值丢弃，允许空结果；解析失败返回空 |

```text
retrieval_score = 0.6 / (60 + vector_rank)
                + 0.4 / (60 + keyword_rank)
recall_k        = max(TopK × 6, 30)
candidate_limit = max(TopK × 4, 20)
```

同片段命中两路时分数相加，只命中一路时仅计该路。Chat 的 Top-K 为 3，对应每条子查询召回 30 条、合并后最多 20 条参与重排、最终最多 3 条；默认重排阈值为 5。

子查询通过 `asyncio.gather` 并发调度，但 Chroma 查询和关键词扫描仍是同步操作，两个通道在 `search()` 内顺序执行，不能视为底层线程并行。

### 3.3 引用与无资料兜底

RAG 来源按规范化 URL 去重；无 URL 时按标题和有效年份去重。同源片段共用编号，再追加 CSV 来源；真实来源对象由后端返回，模型只引用编号。

`_sanitize_citations()` 删除越界编号，不逐句核验事实。主意图为 `school_info`、`admission_policy`、`tuition`、`campus_life`、`escalation` 且无有效 RAG 来源时，业务服务覆盖 Agent 回答，返回固定的资料不足说明。

## 4. CSV 风险计算与多轮字段

CSV 必需字段为 `province`、`year`、`subject_type`、`major`、`min_score`、`min_rank`、`batch`、`source_file`、`source_url`。数据范围和逐年计算示例见[风险案例](main_flow.md#4-录取风险与多轮示例)。

解析优先使用本轮实体，再从本轮原话和逆序近期用户消息补全，不读取历史 assistant 文本。天津按综合改革处理；专业别名、年份和省份按规则归一化。当前没有独立持久化的业务状态对象。

| 状态 | 含义与处理 |
| --- | --- |
| ok | 返回有效分析或候选专业，按工具数据解释 |
| needs_clarification | 缺字段或专业歧义，补问所缺信息 |
| unsupported | 超出覆盖省份，说明范围 |
| not_found | 无匹配记录，不等同于没有招生 |
| unavailable | 工具异常或熔断降级，提示重试或官方查询 |

位次优先，按以下顺序计算：

```text
n = 分数和位次均完整的有效年份数
h = 考生位次 <= 当年最低位次的年份数
m = 历史最低位次中位数
r = (m - 考生位次) / m
```

| 条件 | 风险等级 |
| --- | --- |
| n < 2 | 数据不足 |
| h == n 且 r >= 0.08 | 相对稳妥 |
| h >= ceil(2n/3) 且 r >= 0 | 相对匹配 |
| h > 0 或 r >= -0.05 | 可冲 |
| 其余 | 偏冲 |

只有裸分时采用低置信规则：全部有效年份达到且领先中位分至少 10 分为相对稳妥；至少三分之二年份达到为相对匹配；命中一年或落后中位分不超过 5 分为可冲；其余偏冲，少于两年仍为数据不足。等级由代码计算，Agent 负责解释，不生成录取概率。

## 5. 记忆、缓存与执行可靠性

| 数据/状态 | 存储 | 行为 |
| --- | --- | --- |
| 最近问答与摘要 | Redis，用户/会话拼接键 | TTL 24 小时；最多读 20 条；达到 15 条时压缩旧消息、保留最近 5 条 |
| 情景记忆 | Chroma episodic | 按用户过滤，检索 Top 5，给 Agent 最多 3 条 |
| 用户画像 | Chroma user_profile | 用户哈希稳定 ID；列表字段合并去重；进程内用户级更新锁 |
| 意图缓存 | 进程内字典 | 当前问题 + 最近 3 条历史；无 TTL，达到 1000 项时移除最先插入的 500 项，不是严格 LRU |
| 工具缓存 | 各 ToolManager 进程内字典 | 知识查询 300 秒，CSV 查询 600 秒，不存入 Redis |
| Agent 统计 | 进程内实例 | 累计成功率、平均耗时和 Monitor 惩罚 |

### 记忆边界

用户和助手消息分两次写入，阈值按消息条数计算，压缩在请求内执行。摘要失败仍写占位摘要并裁剪；没有快照比较或事务保护并发压缩。画像更新另行异步调度，旧版多条画像读取时合并，下一次成功更新后迁移为稳定 ID。

画像 Prompt 排除敏感信息，但原始消息未在入库前脱敏。Redis/Chroma 同步操作可能阻塞异步请求。

### 工具与监控

工具依次检查缓存、熔断、必填字段与基础 Python 类型，再超时执行、更新缓存统计；参数检查不是完整 JSON Schema 校验。默认超时 30 秒，连续失败 5 次打开熔断，60 秒后进入半开，半开未限制为单一探测。

降级 handler 执行完成可返回 `success=true` 并携带错误，调用方仍需检查 `fallback` 或业务 `status`。

Monitor 默认每 10 秒采样，同类实例选择分数为：

```text
(0.7 × 累计成功率 + 0.3 / (1 + 累计平均耗时秒数)) × (1 - monitor_penalty)
```

当前每类只有一个实例，不能通过评分产生替代选择，也没有领域冷却/恢复机制。Agent 延迟直方图记录采样时的累计平均值，不是单请求耗时分布。业务 Prometheus 指标仅在非零 `PROMETHEUS_PORT` 触发初始化后注册。

## 6. 评测方法与结果口径

默认套件 `hebut-undergrad-v2` 包含 22 条意图用例、12 个对话场景（14 轮），完整执行预期产生 36 条结果；这是规模，不是通过数。

| 项目 | 规则 |
| --- | --- |
| 意图指标 | Accuracy、各类 Precision / Recall / F1、Macro-F1 |
| Judge 输入 | 本轮问题与回答、此前对话、参考答案、citations；不包含 CSV 原始查询结果或确定性断言结果 |
| Judge 输出 | relevance、accuracy、completeness、helpfulness 各含 0—1 有限分数和非空理由 |
| Judge 异常 | 缺字段、格式/分数/理由非法等失败后重试一次；保留原始输出、原因和尝试次数 |
| 硬断言 | 引用、结构化数据、升级要求、required_terms、forbidden_terms；文本按字面子串匹配 |
| 软断言 | 意图、角色、soft_required_terms、soft_forbidden_terms；routing_assertions_hard 可将路由提升为硬断言 |
| 对话通过 | Judge 有效、四维均分 >= 0.75、全部硬断言通过 |
| 总通过率 | 意图结果 + 所有对话轮次均计入分母；Judge 失败记未通过，但从质量均分中排除，另报告 coverage |

评测使用独立编排器与工具管理器，共享知识库、CSV、记忆、意图识别器和 Skills。长对话压缩可能写入评测用户的 Chroma 情景记忆；结束时只清理 Redis 会话。因此自定义用例应使用专用测试身份。

`/eval/run` 同步返回，不提供任务队列或轮询。报告写入 `latest.json`，不自动覆盖冻结 `baseline.json`；比较优先使用本进程上一份报告，否则使用启动时加载的同套件版本基线。平均指标相对下降超过 5% 标记回归。未对用例、模型、提示词、资料生成配置指纹，比较前需保持条件一致；基线更新步骤见[部署验证](DEPLOY.md#5-部署验证)。

## 7. 接口速查

请求/响应定义以 [api/main.py](api/main.py) 和后端 `/docs` 为准。配置非空 `ADMIN_API_TOKEN` 后，标“是”的接口要求 `X-Admin-Token`。

| 方法与路径 | 输入位置 | 功能 | 管理令牌 |
| --- | --- | --- | --- |
| `POST /chat` | JSON：message，可选 user_id、conv_id | 完整对话 | 否 |
| `POST /search` | URL：query、top_k（默认 5） | 全库改写检索 | 否 |
| `GET /admission/query` | URL：query | 确定性查询，不调用 LLM | 否 |
| `GET /admission/stats` | 无 | CSV 覆盖、完整记录数与版本 | 否 |
| `GET /knowledge/stats` | 无 | 知识片段数和导入状态 | 否 |
| `POST /knowledge/add` | JSON：documents，每项含 title、content、source_url | 文档导入 | 是 |
| `POST /knowledge/upload` | multipart：file、source_url，可选 category | TXT/MD/JSON 导入，应用层上限 10 MB | 是 |
| `GET /skills` | 无 | 规则与加载错误 | 否 |
| `POST /skills/reload` | 无 | 热加载 | 是 |
| `POST /eval/run` | 可选 JSON：intent_cases、dialog_cases | 同步评测 | 是 |
| `GET /health` | 无 | 编排器初始化状态及统计 | 否 |
| `GET /monitor` | 无 | 指标、告警、建议 | 否 |
| `GET /metrics` | 无 | Prometheus 文本指标 | 否 |

聊天没有用户登录绑定、整轮幂等回执、分布式会话锁或整轮超时预算，用户标识由调用方提供。`/health` 不逐项实时探测依赖。`ChatResponse` 不含 verified、grounded、request_id 或工具轨迹字段，前端兼容读取不代表后端已实现。

## 8. 配置与默认值

| 配置 | 含义 |
| --- | --- |
| ANTHROPIC_API_KEY | 模型密钥名，兼容服务也使用此项 |
| ANTHROPIC_BASE_URL、ANTHROPIC_MODEL | Anthropic 兼容协议地址与可用模型标识；历史默认值不保证当前可用 |
| DEEPSEEK_THINKING_MODE | 识别到 api.deepseek.com 时注入，默认 disabled |
| REDIS_URL | 宿主机自行配置；Compose 用密码和服务名覆盖 |
| CHROMA_HOST、CHROMA_PORT | 宿主机示例 localhost:8001；Compose 内 chromadb:8000，不自动覆盖 |
| CHROMA_PERSIST_DIRECTORY | HTTP Chroma 连接失败后的本地库目录 |
| KNOWLEDGE_COLLECTION、KNOWLEDGE_SEED_DIR | 知识集合与种子目录；Compose 固定目录到挂载位置 |
| ADMISSION_DATA_PATH | CSV 路径；Compose 固定到挂载文件 |
| RAG_RERANK_MIN_SCORE | 0—10 评分中的保留阈值 |
| HELLOHEBUT_SKILLS_DIR、HELLOHEBUT_SKILLS_MAX_PROMPT_CHARS | Skills 目录与内容字符预算，详见[规则说明](skills/README.md) |
| EVAL_BASELINE_PATH、EVAL_JUDGE_MODEL | 冻结基线路径、Judge 模型；同目录另写 latest.json |
| MONITOR_INTERVAL、ALERT_WEBHOOK_URL | 采样间隔与可选告警 webhook |
| PROMETHEUS_PORT | 注册业务指标并启动附加服务，示例 9091；Compose 抓取共享注册表的后端 8000/metrics |
| ADMIN_API_TOKEN、CORS_ALLOW_ORIGINS | 管理接口令牌、浏览器跨源允许列表 |

示例中的 `SECRET_KEY`、`JWT_SECRET_KEY`、`GRAFANA_*`、`ENABLE_SWAGGER_UI`、`ENABLE_MONITORING`、`ENABLE_EVALUATION`、`ANOMALY_DETECTION_THRESHOLD`、`APP_NAME` 当前未被业务代码消费。`API_HOST`、`API_PORT` 和 `APP_ENV` 重载逻辑仅供直接运行 `api/main.py`；Compose 固定监听 8000。

## 9. 验证入口

服务与模型集成验证见[部署步骤](DEPLOY.md#5-部署验证)。本地另有 7 个测试文件、45 个测试方法，覆盖 CSV、切片召回、引用、Judge、断言、LLM 文本兼容和协作标签。`tests/` 被当前 .gitignore 忽略，新克隆不保证包含。

已具备测试文件与项目依赖时执行：

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

本地单元测试与真实模型、Redis、ChromaDB、代理链路的集成验证分别进行；不要将用例数量视为通过结果。
