# 河北工业大学本科报考咨询项目：当前设计与对话链路

本文档按当前代码实现说明系统，而不是描述理想方案。阅读顺序从服务启动、一次 `/chat` 请求，到知识检索、Agent 回答、引用返回和端到端测评。

## 1. 整体架构

```text
浏览器 / Vue 前端
    ↓  /api/python/chat
Nginx
    ↓  去掉 /api/python 前缀
FastAPI /chat
    ↓
ChatService（线上和端到端测评共用的应用服务）
    ├─ MemoryManager：读取会话、历史和用户画像
    ├─ IntentRecognizer：意图识别、实体提取、紧急度判断
    ├─ MCPToolManager
    │    ├─ knowledge_search → KnowledgeBase / ChromaDB
    │    └─ admission_data_query → AdmissionDataStore / CSV
    ├─ AgentOrchestrator：路由 General / Policy / Risk / Planning
    ├─ SkillManager：按 Agent 动态注入业务规则
    └─ MemoryManager：写入本轮问答并异步更新用户画像
    ↓
ChatResult
    ↓
response + intent + agent_type + citations + 使用状态 + 实体 + 延迟
```

主要运行组件：

| 组件 | 当前职责 |
|---|---|
| Vue + Nginx | 页面展示、请求转发、引用列表渲染 |
| FastAPI | HTTP 接口和组件初始化 |
| Redis | 当前会话工作记忆和会话摘要，TTL 为 24 小时 |
| ChromaDB | 官方知识库、情景记忆、用户画像的向量存储 |
| `data/hebut_admission.csv` | 河北、天津分专业录取数据的确定性数据源 |
| LLM API | 意图识别、实体提取、查询改写、重排、Agent 回答和 Judge |

## 2. 服务启动时做什么

FastAPI 的 `lifespan` 是整个应用的组装入口，位于 `api/main.py`。

启动顺序如下：

1. 从环境变量读取 `ANTHROPIC_API_KEY`、模型和兼容 API 地址。
2. 创建共享的 `IntentRecognizer`。
3. 从 `skills/` 加载四类招生咨询 Skill。
4. 创建线上 `AgentOrchestrator`。
5. 创建 Redis 客户端、连接 ChromaDB，创建 `MemoryManager`（Redis 在首次读写时实际建立连接）。
6. 创建知识库集合，默认集合名为 `hello_hebut`。
7. 检查并导入 `_load_default_docs()` 中的内置官方知识。
8. 扫描 `data/demo_docs`，增量导入学院资料。
9. 加载 `data/hebut_admission.csv`，校验字段、重复键和完整性。
10. 注册 `knowledge_search` 和 `admission_data_query` 两个工具。
11. 创建线上共用的 `ChatService`。
12. 启动性能监控。
13. 为测评创建独立的 Orchestrator 和 ToolManager，再创建测评用 `ChatService`。

知识库与 CSV 是两套不同的数据路径：招生政策、学院和专业正文进入 ChromaDB；最低分、最低位次和风险计算留在 CSV 确定性工具中。

## 3. 一次 `/chat` 请求的完整链路

### 3.1 前端与 API 入口

前端向以下地址发送请求：

```text
POST /api/python/chat
```

Nginx 将它改写为 FastAPI 的：

```text
POST /chat
```

请求体包含：

```json
{
  "message": "河北物理类考生，今年排名13000报考计算机稳吗",
  "user_id": "用户标识",
  "conv_id": "会话标识"
}
```

没有 `conv_id` 时后端生成新会话；没有 `user_id` 时使用与会话绑定的匿名用户 ID。

### 3.2 ChatService 读取记忆

`ChatService.handle()` 首先调用 `MemoryManager.get_context()`，读取：

- Redis 中的当前会话最近消息；
- ChromaDB 中同一用户语义相关的历史摘要；
- ChromaDB 中的用户画像；
- Redis 中已经压缩的会话摘要。

其中最近 5 条消息会作为本轮意图识别的上下文。完整记忆会格式化为 `[会话摘要]`、`[相关历史]`、`[用户画像]` 和 `[最近对话]`，稍后传给 Agent。

### 3.3 意图识别和实体提取

`IntentRecognizer` 同时负责三件事：

1. 判断主要意图；
2. 提取报考实体；
3. 判断紧急度。

意图识别由以下信号融合：

- LLM 语义判断；
- 模板向量近似匹配；
- 关键词模式匹配。

使用第三方兼容 `base_url` 时，模板向量通道默认关闭，投票权重为 LLM `0.85`、关键词 `0.15`；没有兼容地址时权重为 LLM `0.7`、模板向量 `0.2`、关键词 `0.1`。当前 SDK 没有 Embeddings 资源时，模板向量通道实际使用本地字符 n-gram 哈希向量。LLM 失败时由可用的向量或关键词结果兜底，融合置信度低于 `0.5` 时归为 `other`。

实体字段包括：

```text
admission_year、province、exam_mode、subject_combination、batch、
score、rank、major、college、campus、candidate_type
```

实体提取只记录用户明确提供的信息，不应推测缺失字段。意图缓存键包含最近三轮上下文，避免“这个专业呢”在不同会话中误用旧结果。

### 3.4 根据意图构建两类事实上下文

完成意图识别后，ChatService 分别构建：

```text
官方知识上下文     knowledge_search
结构化录取上下文   admission_data_query
```

两者都由 ToolManager 调用，但用途不同。

#### 官方知识上下文

除“你好、谢谢、再见”等纯寒暄外，系统会尝试知识库检索。意图会映射到优先知识类别：

| 意图 | 优先知识类别 |
|---|---|
| `school_info` | `school_info` |
| `major_info` | `major_info` |
| `admission_policy` | `admission_policy` |
| `score_risk` | `score_risk` |
| `tuition` | `tuition` |
| `campus_life` | `campus_life` |
| `career` | `major_info` |
| `comparison` | `major_info`、`school_info` |
| `escalation` | `escalation` |
| `greeting`、`other` | 不限定类别；非寒暄时可全库检索 |

检索成功后，ChatService 把最多 3 条重排结果整理成带 `[来源N]` 的背景资料。来自同一 URL 的多个片段会共用一个来源编号；没有 URL 时按“标题 + 有效年份”去重。

#### 结构化录取上下文

只有主意图为 `score_risk` 时才调用 CSV 工具。工具综合使用：

- 本轮用户原话；
- 意图识别器提取的实体；
- 当前会话中的历史用户消息。

因此多轮对话可以继承省份、科类、分数、位次或目标专业。工具解析出的确定值会补回实体集合，再一起交给 RiskAgent。

### 3.5 组装 Agent 请求

Agent 收到的背景顺序为：

```text
记忆上下文

结构化录取数据查询结果（仅 score_risk）

官方知识检索结果

结构化实体 JSON
```

背景信息与用户当前问题分开发送给模型，避免模型把背景当成新的用户问题。

### 3.6 Agent 路由与多 Agent 协作

主路由如下：

| 意图 | Agent |
|---|---|
| `school_info` | GeneralAgent |
| `campus_life` | GeneralAgent |
| `greeting` | GeneralAgent |
| `escalation` | GeneralAgent，并标记升级 |
| `other` | GeneralAgent |
| `admission_policy` | PolicyAgent |
| `tuition` | PolicyAgent |
| `score_risk` | RiskAgent |
| `major_info` | PlanningAgent |
| `career` | PlanningAgent |
| `comparison` | PlanningAgent |

复合问题还会根据领域关键词补充协作目标。例如同一句同时出现位次、调剂和就业时，可能并行调用 Risk、Policy、Planning。合并时只拼接各 Agent 正文，不显示 `[policy]`、`[risk]` 等内部标签；返回的 `agent_type` 使用第一个协作 Agent。

专属 Agent 执行失败时降级到 GeneralAgent。严重紧急请求、明确人工请求，或者回答中出现需要招生办确认的表达时，将 `escalated` 标为 `true`。当前只标记升级，不会自动创建真实工单。

### 3.7 动态注入 Skills

Agent 调用模型前，`SkillManager` 根据 Agent 类型和关键词匹配 Skill，将内容拼入 system prompt。

当前四个 Skill 都限定了 Agent 类型，但没有配置关键词，因此对应 Agent 每次执行时都会注入自己的 Skill：

| Skill | Agent | 主要规则 |
|---|---|---|
| `admission_general` | General | 学校、校园生活、澄清、兜底和官方渠道 |
| `admission_policy` | Policy | 章程、录取、调剂、体检、收费和资助 |
| `admission_risk` | Risk | CSV 数字使用、位次优先和风险等级 |
| `admission_planning` | Planning | 专业匹配、比较、就业升学和条件化建议 |

Skills 只补充业务规则，不存储事实数据。`POST /skills/reload` 可以热加载修改后的 Skill，无需重启服务。

### 3.8 回答、引用校验与保守降级

Agent 正文可以使用 `[来源1]` 这样的引用标记，但来源对象由 ChatService 管理：

1. RAG 和 CSV 返回来源元数据；
2. ChatService 对 RAG 来源按 URL 或“标题 + 有效年份”去重，再顺序追加 CSV 来源并统一编号；
3. Agent 只在正文中引用这些编号；
4. `_sanitize_citations()` 删除超出真实来源数量的伪引用标记；
5. FastAPI 将真实 `citations` 数组单独返回；
6. 前端根据该数组在回答下方渲染“来源1：标题”。

因此正文中的引用编号和前端来源列表应使用同一份元数据，不应该依靠模型自行构造来源对象。

对学校概况、招生政策、学费、校园生活和官方联系方式等事实型意图，如果 RAG 重排后没有有效来源，ChatService 会覆盖 Agent 回答，返回“当前知识库资料不足”的固定说明，防止模型继续编造具体数字或学校现状。

### 3.9 写回记忆并返回结果

线上请求会把用户问题和最终回答写入 Redis。随后异步调用 LLM 提炼非敏感偏好并更新 ChromaDB 用户画像。

返回字段包括：

```text
conv_id
response
intent
agent_type
escalated
latency_ms
knowledge_used
admission_data_used
citations
entities
```

`admission_data_used=true` 表示 CSV 返回了可用分析或候选专业；`knowledge_used` 表示本轮使用了有效 RAG 结果或有效结构化录取数据。前端据此展示 `RAG` 或“结构化录取数据”标签。

## 4. 意图边界

当前最容易混淆的边界如下：

| 用户问题 | 意图 | 原因 |
|---|---|---|
| 学校性质、办学层次、历史 | `school_info` | 学校基本事实 |
| 校区数量、校区名称、校区地址 | `school_info` | 校区基本事实 |
| 宿舍配置、食堂、交通、社团 | `campus_life` | 校园生活条件和体验 |
| 住宿费多少钱 | `tuition` | 收费规则，不是宿舍体验 |
| 专业学什么、适合谁 | `major_info` | 专业培养与匹配 |
| 就业、岗位、考研、保研 | `career` | 发展方向 |
| 两个专业或校区怎么选 | `comparison` | 多选项决策 |
| 招生计划如何发布、以谁为准 | `admission_policy` | 官方规则和流程 |
| 某省某专业招多少人 | `score_risk` | 具体计划数字归 Risk 路由，但当前 CSV 不含计划人数 |
| 调剂、退档、级差、体检限制 | `admission_policy` | 明确制度规则 |
| 最低分、最低位次、能否录取 | `score_risk` | 确定性数据与风险判断 |

## 5. 知识库如何搭建

### 5.1 数据来源

知识集合默认名为 `hello_hebut`，包含两类资料：

1. `_load_default_docs()` 内置的 8 条官方招生摘要；
2. `data/demo_docs` 下部署时自动导入的学院资料。

当前仓库内置与部署种子资料的快照为：

```text
8 条内置知识 + 15 份学院文档生成的 197 个片段 = 205 个 Chroma 片段
```

这个数字是片段数，不是源文件数。通过管理接口另外导入的资料会使实际集合总数高于 205；CSV 录取记录不计入知识库片段数。

### 5.2 `data/demo_docs` 的增量导入

后端递归扫描 `.txt`、`.md` 和 `.json`：

- TXT/MD：整个文件先作为一个源文档；
- JSON：支持单个对象、对象数组或 `documents` 数组；
- 默认类别为 `major_info`；
- 从文件名提取年份；
- 文件 SHA256 作为 `document_version`；
- 保存 `source_file` 和 `source_type=deployment_seed`。

部署资料没有 URL 也可以正常导入，其可追溯依据是源文件名和内容版本。通过 `/knowledge/add` 或 `/knowledge/upload` 写入时必须提供 `hebut.edu.cn` 官方 URL；配置了 `ADMIN_API_TOKEN` 时还必须携带管理令牌。

文件内容和索引版本都未变化时跳过导入。发生变化时先写入新片段，成功后再清理同一文件的旧版本，避免导入中断造成知识丢失。

### 5.3 学院文档切片

TXT 学院资料使用有状态的启发式解析：

```text
从标题“之XXXX学院”识别学院
    ↓
逐行读取，忽略空行
    ↓
识别专业名称和章节标题，更新当前上下文
    ↓
标题行通常不生成片段
    ↓
每个非空正文行成为候选片段
    ↓
正文行超过 500 字时，尽量按中文句号继续切分
    ↓
每个正文片段继承当前学院、专业和章节
```

可识别章节包括学院介绍、专业介绍、培养目标、核心课程、就业方向和专业特色。未出现明确章节标题时，解析器不会根据正文语义自动推断章节；例如某专业后面的课程和就业文字可能继续归入“专业介绍”。当前切片没有重叠窗口。

### 5.4 实际存入 Chroma 的内容

向量化的不是孤立正文，而是 `index_text`：

```text
文档：河北工业大学2026年本科招生简章之外国语学院
类别：major_info
学院：外国语学院
专业：英语
章节：专业介绍
正文：英语专业是国家一流本科专业……
```

如果文档提供了 `keywords`，也会拼入索引文本。原始正文另存为 `raw_content`，用于重排、展示和传给 Agent。

每个 Chroma 片段的 metadata 还包含：

```text
title、source_url、source_file、source_type、category、keywords、
published_at、effective_year、document_version、college、major、section、
index_version、chunk_index、total_chunks
```

片段 ID 由来源、文档版本、标题、片段序号和正文前 80 字生成，后续多路召回和多查询合并都优先使用这个 ID 去重。

## 6. 知识库查询逻辑

### 6.1 查询改写

ToolManager 要求 LLM 生成 3 个不同角度的改写，并保留原始问题，因此正常情况下最多有 4 条去重后的子查询。改写失败时只使用原始问题。

各子查询并行执行知识库召回。

### 6.2 每条子查询的类别池和全库池

Chat 线上最终需要 Top 3，因此每条子查询的召回规模为：

```text
recall_k = max(TopK × 6, 30) = 30
```

每条子查询执行：

```text
类别范围：向量通道 + 关键词通道 → 类别候选池
全库范围：向量通道 + 关键词通道 → 全库候选池
        ↓
约 80% 类别候选 + 至少 20% 全库候选
        ↓
按 4:1 交错；任一侧不足时由另一侧补齐
```

没有类别限制时只执行全库混合召回。

### 6.3 向量与关键词融合

向量通道通过 Chroma 的 `query_texts` 取得相似片段。关键词通道不是只比较人工 `keywords`，而是同时检查：

- 标题、章节和 keywords；
- 用于索引的上下文文本；
- 原始正文 `raw_content`。

中文查询会生成连续中文块和 2、3 字符 n-gram。标题、章节和 keywords 命中的权重高于正文命中，完整查询连续命中还有额外加分。

两路按照基于排名的融合分数合并：

```text
retrieval_score = 0.6 / (60 + 向量排名)
                + 0.4 / (60 + 关键词排名)
```

同一片段两路都命中时会同时获得两部分分数。每个通道还保留约 20% 的候选位置，避免其中一路被融合排名完全挤出。

### 6.4 多查询轮询、去重和 LLM 重排

所有子查询结果按排名轮询合并，而不是先把第一条查询全部放入候选池。这样每个改写查询都有机会进入重排候选。

合并规则：

1. 优先按检索结果 `id` 去重；
2. 没有 ID 时使用来源、标题、版本、片段号和正文哈希；
3. 去重键不包含 score；
4. 候选上限为 `max(TopK × 4, 20)`；线上 Chat 的 TopK 为 3，因此最多选 20 条交给 LLM 重排。

重排 Prompt 只拼接必要信息：标题、类别、学院、专业、章节、有效年份和最多 600 字正文，不再截断整段 JSON。

LLM 为每条结果输出 `0—10` 的 `rerank_score`：

- 低于 `RAG_RERANK_MIN_SCORE`，默认 5 分，直接丢弃；
- 允许所有结果都被丢弃；
- 输出解析失败时安全返回空结果；
- ChatService 最终最多取 Top 3。

### 6.5 前端“知识库检索”和线上 Chat 的区别

两者使用同一个 `hello_hebut` 集合和同一套混合检索、查询改写与重排逻辑，但调用条件不同：

| 功能 | 是否识别意图 | 类别优先 | CSV 工具 | Agent 回答 |
|---|---:|---:|---:|---:|
| 前端 `/search` | 否 | 否，全库检索 | 否 | 否 |
| 线上 `/chat` | 是 | 是 | `score_risk` 时使用 | 是 |

因此 `/search` 是检索排障入口，不是完整对话链路。如果修改了已挂载的 `data/demo_docs`，需要重启后端触发扫描；不需要重新构建镜像。

## 7. CSV 录取数据与 RiskAgent

`AdmissionDataStore` 启动时要求 CSV 至少包含：

```text
province、year、subject_type、major、min_score、min_rank、
batch、source_file、source_url
```

当前 CSV 覆盖河北、天津 2023—2025 年的分专业最低分和最低位次，不包含各专业招生计划人数。查询流程为：

```text
用户问题 + 意图实体 + 历史用户消息
    ↓
解析省份、科类、分数、位次、专业和年份
    ↓
专业别名归一化和歧义检查
    ↓
精确过滤同省、同科类、同专业、同年份记录
    ↓
计算逐年位次差、历史中位分和历史中位位次
    ↓
按确定性规则输出风险等级
```

工具可能返回：

| 状态 | 含义 | Agent 行为 |
|---|---|---|
| `ok` | 找到分析或候选专业 | 严格使用工具数字和风险等级 |
| `needs_clarification` | 缺少省份、科类、位次或专业 | 只补问缺失字段 |
| `unsupported` | 不在河北、天津覆盖范围 | 说明数据边界 |
| `not_found` | 当前口径没有匹配记录 | 不解释为“该专业没有招生” |
| `unavailable` | 工具异常或熔断降级 | 引导稍后重试或官方查询 |

有位次时优先按位次判断。风险等级规则为：

- 全部有效年份均达到历史最低位次，且领先历史中位位次至少 8%：`相对稳妥`；
- 至少三分之二年份达到，且不差于历史中位位次：`相对匹配`；
- 至少命中一年，或距离历史中位位次不超过约 5%：`可冲`；
- 其余：`偏冲`；
- 有效年份少于 2 年：`数据不足`。

只有裸分时可以低置信比较，但必须提醒补充位次。系统不生成录取概率，也不承诺录取。

## 8. 三级记忆设计

“三级”指短期会话、跨会话情景记忆和长期用户画像；Redis 会话摘要是短期会话层的压缩结果，不是独立的第四级。

| 层级 | 存储 | 用途 |
|---|---|---|
| 短期会话 | Redis | 当前会话最近消息（最多读取 20 条）及压缩摘要，TTL 24 小时 |
| 情景记忆 | ChromaDB `episodic` | 跨会话检索相关历史摘要 |
| 用户画像 | ChromaDB `user_profile` | 省份、选科、目标专业和关注方向等非敏感偏好 |

工作记忆达到 15 条时：

1. 旧消息由 LLM 压缩成 2—3 句话；
2. 摘要写入 Redis；
3. 摘要写入情景记忆；
4. 工作记忆只保留最近 5 条。

用户画像提炼 Prompt 会排除姓名、手机号、身份证号、准考证号、考生号和精确地址等敏感信息。但当前代码仍会把用户原始消息写入 Redis，尚未实现输入脱敏；前端和调用方不应提交身份证号、验证码等敏感信息。

## 9. `/eval/run` 端到端测评链路

```text
POST /eval/run
    ↓
EndToEndEvaluator
    ├─ 意图分类用例 → 直接测试共享 IntentRecognizer
    └─ 对话用例 → 测评专用 ChatService
                      ↓
                 与线上相同的记忆、意图、RAG、CSV、Agent、Skill 流程
                      ↓
                 LLM Judge + 确定性断言
                      ↓
                 latest.json + 与冻结 baseline.json 比较
```

### 9.1 与线上一致的部分

端到端对话用例调用同一个 `ChatService.handle()`，因此以下逻辑一致：

- 记忆读取和多轮上下文；
- 意图和实体识别；
- 类别优先 RAG；
- CSV 录取数据查询；
- Agent 路由和 Skills；
- 引用后处理；
- 最终响应字段。

### 9.2 合理隔离的部分

测评使用独立的 AgentOrchestrator 和 ToolManager，避免评测请求污染线上 Agent 成功率、延迟、熔断状态和工具缓存。两者仍共享同一知识库、CSV、MemoryManager、IntentRecognizer、Skills 和模型配置。

默认情况下，每个测评对话生成独立的 `user_id`、`conv_id`（自定义用例也可以显式覆盖）：

- 开始前清理短期记忆；
- 多轮用例内部正常写入记忆；
- 禁止更新长期用户画像；
- 用例结束后再次清理短期记忆。

### 9.3 LLM Judge

Judge 评价：

```text
relevance、accuracy、completeness、helpfulness
```

Judge 必须为四个维度同时返回 `score` 和 `reason`，结构如下：

```json
{
  "relevance": {"score": 0.0, "reason": "维度理由"},
  "accuracy": {"score": 0.0, "reason": "维度理由"},
  "completeness": {"score": 0.0, "reason": "维度理由"},
  "helpfulness": {"score": 0.0, "reason": "维度理由"}
}
```

缺字段、缺理由、非数字、越界、空输出或非法 JSON 都视为失败，不再静默补 0。第一次失败后会带错误原因重试一次。

报告保留：

- 最终原始输出；
- 两次尝试的全部原始输出；
- 每个维度的理由；
- 错误类型、错误信息和尝试次数。

Judge 失败的对话用例判为未通过，但不进入四项质量均分，同时在 `judge_stats.coverage` 中反映覆盖率。

### 9.4 确定性断言

当前支持：

- 意图和 Agent 路由；
- 是否升级；
- 是否存在真实引用；
- 是否使用结构化录取数据；
- 回答必须包含的文本；
- 回答禁止包含的文本。

断言分为：

- `hard`：失败会直接导致用例不通过；
- `soft`：只统计通过率，不否决用例。

意图和 Agent 默认是 soft，只有 `routing_assertions_hard=true` 才变成 hard。`required_terms`、`forbidden_terms`、引用、结构化数据和升级要求当前默认是 hard；`soft_required_terms` 和 `soft_forbidden_terms` 只记录。

当前 `required_terms` 仍然使用字面子串匹配，不是正则或同义表达匹配。Judge 先评分，确定性断言随后独立计算；Judge 能看到参考答案和 `citations`，但当前不会收到确定性断言结果或 CSV 原始查询结果，也没有“accuracy=0 时自动二次事实复核”的逻辑。

### 9.5 通过条件和基线

单个对话结果同时满足才通过：

```text
Judge 成功
四维平均分 >= 0.75
全部 hard 断言通过
```

每次评测结果写入：

```text
data/eval/latest.json
```

不会自动覆盖：

```text
data/eval/baseline.json
```

确认 `latest.json` 的回答、引用、硬断言和 Judge 理由可靠后，才能人工复制为新的冻结基线。当前套件版本为 `hebut-undergrad-v2`，不同版本的旧基线会被忽略。平均指标相对基线或同一进程上次报告下降超过 5% 时记录为回归。

## 10. 监控与工具可靠性

ToolManager 为工具统一提供：

- 参数校验；
- TTL 缓存；
- 超时；
- 连续失败熔断；
- 降级结果；
- 成功率、延迟和连续失败统计。

熔断器连续失败 5 次后打开，默认 60 秒后进入半开探测。

`PerformanceMonitor` 默认每 10 秒读取线上 Orchestrator 和 ToolManager 统计，生成阈值告警、Z-score 异常和优化建议，并把成功率和延迟转换成 Agent 路由惩罚。当前每种 Agent 只有一个实例，因此路由评分主要用于监控；增加同类多个实例后才会在同类型内部选择表现更好的实例。

可通过以下接口查看：

```text
GET /health
GET /monitor
GET /metrics
GET /knowledge/stats
GET /admission/stats
```

## 11. 管理接口和 Nginx 边界

配置 `ADMIN_API_TOKEN` 后，下列写入或高权限接口需要请求头：

```text
X-Admin-Token: 与 .env 中 ADMIN_API_TOKEN 相同的值
```

受保护接口包括：

```text
POST /skills/reload
POST /knowledge/add
POST /knowledge/upload
POST /eval/run
```

前端“管理令牌”只保存在当前页面内存，用于给知识写入请求增加该请求头，不是模型 API Key，也不是用户登录密码。

当前 Nginx 只完整代理 `/api/python/*`，但 `/docs` 页面中的 Swagger 请求默认指向根路径。因此：

- `http://localhost/docs` 适合浏览接口定义；
- `http://localhost:8000/docs` 直连 FastAPI，适合执行全部调试和管理接口；
- 前端业务请求通过 `/api/python/*` 正常代理；
- 生产环境不应直接向公网开放后端 8000、Redis 或 ChromaDB。

## 12. 模块职责速查

| 文件 | 主要职责 |
|---|---|
| `api/main.py` | 初始化组件、注册工具、提供 FastAPI 接口 |
| `core/chat_service.py` | 统一线上与测评的完整对话链路 |
| `core/intent_recognizer.py` | 意图融合、实体提取、紧急度和缓存 |
| `core/llm_client.py` | 统一 LLM 调用和兼容 API 参数，安全提取最终文本 |
| `core/skill_loader.py` | 加载、匹配、热更新和注入 Skills |
| `agents/agent_orchestrator.py` | Agent 路由、协作、降级和升级判断 |
| `mcp/knowledge_indexing.py` | 学院、专业、章节识别，正文切片和关键词评分 |
| `mcp/knowledge_base.py` | 知识导入、版本管理、向量/关键词混合召回 |
| `mcp/tool_manager.py` | 查询改写、多查询合并、去重、重排、缓存和熔断 |
| `mcp/admission_data.py` | CSV 校验、精确查询、位次差和风险规则 |
| `memory/conversation_memory.py` | Redis 工作记忆、情景记忆、画像和摘要压缩 |
| `evaluation/evaluator.py` | 意图测评、端到端对话测评、Judge、断言和基线 |
| `monitor/performance_monitor.py` | 在线统计、异常、告警、建议和路由反馈 |
| `frontend/src/App.vue` | 对话、知识检索、知识导入和来源列表展示 |
| `frontend/src/lib/backends.js` | 前端 Python API 请求封装和响应字段归一化 |
| `config/nginx/nginx.conf` | 静态页面、反向代理、安全头和限流 |

## 13. 当前保守边界

1. 结构化录取数据只覆盖河北、天津以及 CSV 已提供的年份和专业。
2. 历史分数、位次不能预测下一年度录取线，风险等级不是录取概率。
3. RAG 重排允许返回空结果；`school_info`、`admission_policy`、`tuition`、`campus_life`、`escalation` 无有效来源时会使用固定的资料不足回复。
4. `data/demo_docs` 没有 URL 时可以导入，但只能追溯到部署文件，不等同于已验证的网页来源。
5. 学院 TXT 的章节识别依赖明确标题，不做正文语义章节分类。
6. 多 Agent 协作当前是并行生成后直接拼接，尚未增加专门的总结 Agent。
7. 引用对象由后端返回、前端展示；回答正文中的 `[来源N]` 只是对真实来源数组的编号引用。
8. 当前 CSV 不含专业招生计划人数字段；这类问题虽路由到 `score_risk`，只能依赖知识库中的官方查询入口或提示用户到官方渠道核对。
9. 原始会话消息尚未做输入脱敏；用户画像 Prompt 的敏感信息排除不能替代存储前过滤。
10. 测评有模型随机性、查询改写随机性和 Judge 波动；冻结基线必须人工核验，不应以单次总分自动晋升。
