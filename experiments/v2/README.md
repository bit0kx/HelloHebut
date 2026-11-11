# v2 分阶段实施与验证

本目录集中保存评测、测试、报告和运行中间产物；业务实现复用根目录的 `core/`、`memory/`、`mcp/`、`api/`，仅新增 `rag/` 检索控制模块。方案见 [AGENT_EVOLUTION_PLAN.md](../../AGENT_EVOLUTION_PLAN.md)。

每批完成后的简要更新与审查状态见 [STAGE_UPDATES.md](STAGE_UPDATES.md)，本页只维护当前运行和验证说明。

## 本轮范围与状态

已实现基础契约、关键可靠性、Agentic RAG 与可验证偏好。本批按用户要求将关系数据库改为 MySQL，并推进第 4 阶段的整轮账本与已生成答案恢复提交；统一功能测试已通过。报告中的 stage5 表示第五批交付，不代表第 5 周已完成；LangGraph 节点级恢复仍待实现。

| 步骤 | 已实现 | 主要代码 |
| --- | --- | --- |
| A：基础契约与基线 | TaskState、字段来源与条件失效、工具完整 Schema 校验、请求预算、脱敏元数据 Trace、必需 CSV 调用 | [contracts](../../core/contracts.py)、[task_state](../../core/task_state.py)、[runtime](../../core/runtime.py)、[ChatService](../../core/chat_service.py) |
| B：关键可靠性 | 匿名会话令牌、请求回执、同 ID 内容冲突、整轮原子提交、摘要失败保留原文、并发压缩保护、有界维护队列 | [turn_store](../../memory/turn_store.py)、[conversation_memory](../../memory/conversation_memory.py)、[API](../../api/main.py) |
| C：Agentic RAG 与知识版本 | 按年份/子问题规划、证据核验与有限补查、确定性 CSV 短路径、回答核验/回退、原文展示、活动版本切换/回退、年份去扰与等权融合、可选 BM25 | [admission_answer](../../core/admission_answer.py)、[agentic](../../rag/agentic.py)、[knowledge_base](../../mcp/knowledge_base.py)、[lexical_retrieval](../../mcp/lexical_retrieval.py)、[knowledge_catalog](../../mcp/knowledge_catalog.py) |
| D：可验证长期偏好 | MySQL 事实版本、原话来源、候选确认、乐观版本检查、操作幂等、更正/删除/过期、跨会话读取、管理面板 | [fact_store](../../memory/fact_store.py)、[preferences](../../memory/preferences.py)、[memory_routes](../../api/memory_routes.py)、[PreferencePanel](../../frontend/src/components/PreferencePanel.vue) |
| E：持久账本基础 | MySQL 对话/状态/回执同事务、数据库租约校验、答案快照恢复提交、Redis 存量懒导入与清空墓碑 | [mysql_turn_store](../../memory/mysql_turn_store.py)、[mysql_database](../../memory/mysql_database.py)、[ChatService](../../core/chat_service.py) |

关键行为：复合问题只要需要录取数字，就先查 CSV；查询失败或条件不足时直接说明/澄清。更换省份、科类、考试年度或考生后，相关成绩字段失效，不从旧历史文本重新补回。回答必须引用本次真实证据，数字先检查是否出现在依据中，再由模型检查对应关系；这仍不是形式化的语义正确性证明。

纯数字问题使用 CSV 固定模板生成，`verification_method=deterministic_csv` 表示程序确保数字与工具记录一致；混合问题继续执行证据核验。引用记录文档、修订和片段 ID、哈希与本次原文，前端可展开核对。

当前仍待实施：人工独立检索集、中文 Embedding 对照、LangGraph 节点 Checkpoint、标准 MCP、Composer 与动态 Skills。BM25 尚未证明优于修复后的原词法评分。长期记忆首版只覆盖明确偏好，不覆盖全部情景/程序性记忆或跨设备账号；模型驱动的通用工具循环尚未接入角色 Agent。

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

Redis 使用 `v2:` 命名空间，保留匿名身份、知识活动清单等职责；聊天持久化以 MySQL 为准。Chroma 知识库为 `hello_hebut_v2`，旧 `episodic_v2/user_profile_v2` 保留但当前 API 不再读写。MySQL 使用独立 `mysql-data` 数据卷、`hellohebut` 数据库和 `hellohebut_memory_v2_` 表前缀。匿名令牌有效期仍为 24 小时；新匿名身份不能找回旧偏好，账号恢复与跨设备使用尚未实现。

## 验证命令

从项目根目录执行，依赖安装使用现有 `requirements.txt`。真实 Redis 测试需要 PATH 中存在 `redis-server`；测试自行启动随机端口的专用 Redis 并清理，不连接或清空已有实例。

```powershell
python -X utf8 experiments/v2/scripts/run_tests.py --suite experiments/v2/legacy_tests --report experiments/v2/reports/stage5_legacy.json
python -X utf8 experiments/v2/scripts/run_tests.py --report experiments/v2/reports/stage5_regression.json
python -X utf8 experiments/v2/scripts/compare_routing.py
```

使用 `.env` 中已有模型配置执行小规模在线冒烟，产生模型调用费用：

```powershell
python -X utf8 experiments/v2/scripts/live_smoke.py
```

报告口径：

`commit` 记录执行时的 Git HEAD；提交前运行的报告以 `source_hashes` 和 `test_hashes` 标识实际受测文件。

- [baseline.json](reports/baseline.json)：修改业务代码前原有 45 项离线测试通过，保留当时源码哈希。
- [stage5_legacy.json](reports/stage5_legacy.json)、[stage5_regression.json](reports/stage5_regression.json)：本批原有 45/45、v2 回归 78/78 通过。历史 stage4 报告不覆盖。
- [routing_comparison.json](reports/routing_comparison.json)：8 个固定案例，原 ChatService 必需工具路由 4/8，v2 为 8/8。使用脚本化意图/回答和真实 CSV，只衡量工具路由，不能解释为整体回答准确率提升。
- [live_smoke.json](reports/live_smoke.json)：真实模型、合成政策资料的两例冒烟通过；验证跨年证据充分与缺失年份拒绝放行，不代表正式检索指标或全面注入防御评测。

**本批统一验证**：[MySQL 偏好与 HTTP](reports/stage5_memory.json) 11/11、[聊天账本](reports/stage5_turns.json) 12/12、[容器与真实模型冒烟](reports/stage5_deployment.json) 7/7；[数据库/应用真实重启](reports/stage5_restart.json) 后偏好保留，已生成答案不重复计算即可恢复提交，并能回放原回执。数据库专项测试采用独立表前缀和身份并清理，不修改用户偏好；[交付汇总](reports/stage5_validation.json) 核对源码与运行容器。首轮 SQL 插值和上下文快照变量冲突已修复，失败报告保留为 `stage5_initial_*.json`。

[第四批汇总](reports/stage4_validation.json) 保留当时的 45/45 原有回归、78/78 v2 回归、11/11 PG/Redis 检查及最终 7/7 容器冒烟。两次中间 6/7 失败也保留，单次模型冒烟通过不等于无波动的整体准确率。前端已在第四批构建；浏览器/Windows 自动化通道当时不可用，界面目测尚未验收。

[真实存储报告](reports/stage3_storage_retrieval.json) 中，4 项 Chroma/Redis 发布、故障、快照和回退检查通过，并覆盖可选 BM25 索引缓存。检索对照及适用边界见下节；测试集合单独创建并清理，不修改项目知识资料。

标准 `docker compose up -d --build` 的基础镜像元数据长度校验问题继续保留，不修改全局镜像源。本批已从 Docker 官方镜像的 ECR Public 入口下载 `public.ecr.aws/docker/library/mysql:8.4`；可设置 `V2_MYSQL_IMAGE` 使用该镜像。Python 数据库驱动改为 `asyncmy==0.2.17`；临时构建覆盖保存在 Git 忽略的 `tmp/`，正式全新构建仍待验收。

运行中的服务可用以下脚本复测（第一条调用真实模型并产生费用；第二条创建并清理隔离测试表和身份）：

```powershell
python -X utf8 experiments/v2/scripts/deployment_smoke.py --build-mode reused_local_base --report experiments/v2/reports/stage5_deployment.json
python -X utf8 experiments/v2/scripts/fact_store_checks.py
python -X utf8 experiments/v2/scripts/turn_store_checks.py
# 下一条会重启本机 v2 MySQL 与应用，单独运行，不与其他验收并发
python -X utf8 experiments/v2/scripts/memory_restart_smoke.py
```

## 可验证偏好：使用、数据职责与边界

前端“我的偏好”可手动保存专业、兴趣、费用和地域偏好，或在聊天中表达“我偏好的专业是计算机科学与技术”，再确认候选。点“新对话”后问“我的专业偏好是什么？”可核对跨会话读取；更正新增版本并使旧版 `superseded`，待确认内容不会进入回答。分数、位次和身份凭证不进入偏好字段。

候选提取采用保守的显式第一人称规则，不调用模型猜测；只接受已原子提交且哈希匹配的本人消息，保存消息/运行/会话 ID 与原话。候选 7 天失效；确认默认 180 天，手动保存可选 1—365 天。到期记录在下一次访问时标为 `expired` 并排除，历史查看仍可见；没有定期物理清理任务。

MySQL 是偏好唯一权威来源，不复制到 Chroma 或 Redis 缓存。按用户行锁串行修改，`expected_revision` 拒绝陈旧页面覆盖，`operation_id` 回执保证重试不重复更新；删除清除该键的所有版本、候选和原话。只保留不含原文的版本/操作摘要，旧请求重放或正在执行的旧轮次不能重新写回偏好。数据库事务和行锁分别参考 [MySQL 事务说明](https://dev.mysql.com/doc/refman/8.4/en/commit.html) 与 [InnoDB 锁定读取](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html)。

API 全部用 `/session` 的 Bearer 身份，不能指定他人的 `user_id` 或伪造来源：

| 接口 | 行为 |
| --- | --- |
| `GET /memories`，可加 `?history=true` | 返回当前用户记录、字段标签和全局 revision |
| `POST /memories` | 本人手动保存并确认；字段为 key/value/valid_days/expected_revision/operation_id |
| `POST /memories/{memory_id}/confirm` 或 `/reject` | 确认或忽略候选；提交 expected_revision/operation_id |
| `DELETE /memories/{key}` 或 `DELETE /memories` | 删除某类全部版本或全部偏好；同样提交版本与操作编号 |

MySQL 初始化执行 [001_facts.sql](../../memory/migrations/001_facts.sql)，不迁移缺少来源的旧模型画像。当前 API 停用旧画像提炼和跨会话情景召回；`HELLOHEBUT_VERIFIED_MEMORY=false` 只关闭偏好功能，不关闭 MySQL 聊天账本或恢复旧画像。数据库故障时不回退到旧画像，也不回退写 Redis。旧 TaskState v1 保守迁移为“不应用本人偏好”，新对话或明确“回到我自己”后恢复本人范围，避免给旧的代问任务套用偏好。

**仍有边界**：偏好与聊天虽已同库，候选提取仍在聊天提交后单独执行，最多等待两秒；失败不撤销已提交聊天，同一请求重试可补写，尚无持久后台任务队列。删除偏好不删除聊天原文、摘要或历史回执，已在执行的回答可能已读取旧偏好；新会话只读取当前有效记录。本版适用于同一有效匿名身份，不是跨设备账号记忆；自动提取覆盖率和 Write Precision 尚无人工独立评测。

## MySQL 聊天账本、恢复与存量切换

用户消息、最终回答、TaskState 和请求回执在一个 MySQL 事务中提交；数据库会话租约、到期时间和状态版本在同一事务内校验，失去租约的旧执行不能提交。已提交回执持久保留，同身份、会话、请求 ID 及内容返回原结果；同 ID 改内容返回冲突。目前没有定时清理聊天与回执的任务，匿名令牌的 24 小时有效期与数据库保留时间不同。

答案计算完成后先保存可恢复快照，再提交整轮。恢复先查已提交回执；未提交且快照未超过 1 小时、会话 revision 未变化时，直接恢复提交，不重复调用模型。快照过期、状态变更或在保存快照前中断时需重新计算。这只覆盖已生成答案的恢复，尚未接入 LangGraph，也不能从检索或模型调用中途继续。

恢复的是旧请求已经生成的答案，不会重新检索最新知识或重新应用偏好更正；需要当前资料/偏好时发起新请求。已提交请求始终回放原结果。MySQL 聊天账本接入当前 FastAPI/Web 路径，历史 `--cli` 演示入口仍保留 v1 记忆行为，不用于 v2 持久化验收。

首次切换前停止旧应用，避免 Redis 与 MySQL 同时接收聊天写入。旧 Redis 会话在首次读取时，将仍存在的历史、摘要与回执导入 MySQL；已过期的 Redis 数据无法恢复。导入后 MySQL 是唯一权威来源，运行故障不回退写 Redis。清空会话删除 MySQL 正文、请求和回执，保留不含正文的迁移墓碑，避免旧 Redis 数据复活；清空后同请求 ID 可重新执行。

旧 PG 主库已检查：偏好记录、操作回执、候选捕获回执均为 0，只有 9 条 owner 记录，没有偏好内容待迁移。本机切换时停止旧 PG 容器并保留 `postgres-data` 卷；不执行数据库卷删除。本批 MySQL 测试使用同一数据库中的独立表前缀，完成后只清理对应测试表。

## 检索配置与对照

当前默认 `RAG_LEXICAL_MODE=filtered_legacy`、`RAG_FUSION_MODE=balanced`。年份仍是元数据硬过滤，只在词法查询中去除已用于过滤的同一年份，保留位次/金额等数字语义。融合使用等权 RRF，同分优先词法候选，避免两路结果不重叠时旧的 0.6/0.4 权重挤掉词法结果；类别优先与全库补充、后续 LLM 重排继续保留。

`RAG_LEXICAL_MODE=bm25` 可切换到 BM25 候选；中文使用连续 2/3 字符片段，英文/数字按词，正文只计一次，k1=1.2、b=0.75 取自 [Lucene BM25 公式与默认值](https://lucene.apache.org/core/9_12_3/core/org/apache/lucene/search/similarities/BM25Similarity.html)，没有用案例调参。这是面向小语料的内存评分器，不是 Lucene/Elasticsearch 索引。最多缓存 8 份按发布代际、类别、年份、分析器版本隔离的索引，请求内固定快照；工具缓存额外包含词法和融合策略。

修改 `.env` 后执行 `docker compose up -d --no-build hellohebut` 重建应用容器以加载配置。回退原检索同时设置 `RAG_LEXICAL_MODE=legacy` 和 `RAG_FUSION_MODE=legacy`；只读接口 `GET /knowledge/stats` 的 publication 字段可检查当前模式。模式切换不更换向量或重新导入资料。

[最终八组消融](reports/stage3_retrieval_ablation.json) 固定 generation=16、205 个活动片段；所有混合方案复用每条问题同一组真实 ONNX 向量候选，不使用重排。下表均为 Recall@8 / nDCG@8：

| 方案 | 原开发集：4 条正向 | 按来源分开的验证集：8 条正向 |
| --- | --- | --- |
| 原混合检索 | 75% / 0.375 | 87.5% / 0.438 |
| 仅去除过滤年份，保留旧融合 | 100% / 0.500 | 87.5% / 0.438 |
| 去除过滤年份＋等权融合（当前默认） | 100% / 0.908 | 100% / 0.923 |
| BM25＋旧融合 | 100% / 0.500 | 87.5% / 0.438 |
| BM25＋等权融合 | 100% / 0.875 | 100% / 0.873 |
| 仅向量 | 0% / 0 | 0% / 0 |

四条缺年份问题在所有方案下均为空。原校区文档被年份词法加分挤到第 33 位；另一个专业案例被旧融合丢弃。提升主要来自这两处修复，不能归功于 BM25 替换。原词法单路和 BM25 单路的细项也保留在报告中；默认继续保留语义通道，为后续中文 Embedding 对照留下同一结构。

边界：标签由已收录正文整理，未经人工复核。`retrieval_holdout.json` 首轮虽按来源分开且提前固定，但首轮失败随后用于融合诊断，**现在属于开发验证集，不是未触碰的独立测试集**；标签内容未改变，标注说明已更新。[首轮消融](reports/stage3_initial_ablation.json) 保留失败记录。当前样例小且词法线索明显，结果不代表全量问答准确率或泛化能力；现有模型 `all-MiniLM-L6-v2` 在这些中文样例上单路召回弱，BGE-M3 尚未实际对照。报告时间只记录检索分量，不是完整链路 p95。

```powershell
python -X utf8 experiments/v2/scripts/retrieval_ablation.py
```

脚本只读项目资料，在同一发布快照上对照候选代码，记录源码/数据集/语料哈希和逐条片段结果。完整语料快照放在 Git 忽略的 `tmp/retrieval_corpus_*.json`；正式 JSON 报告位于 `reports/`，两者均不含聊天记录。

## 知识发布与回退

导入统一为“写不可变修订 → 读回校验 → Redis CAS 切换活动清单”。并发发布冲突返回 409；失败和退休片段留在 Chroma，但检索只接受活动清单。每个聊天请求固定一份快照，缓存键包含发布代际，因此其他进程发布后新请求也不会误用旧缓存。Redis 知识清单仍是当前权威数据，需连同现有 AOF 数据卷持久化；它没有随本批聊天账本一起迁移，后续单独迁入 MySQL。

`GET /knowledge/stats` 返回活动片段数、发布代际和可回退代际。`POST /knowledge/add`、`/knowledge/upload` 统一发布；`POST /knowledge/rollback` 接收 `{"generation": 目标代际}`，发布一个新代际指向历史内容。最近五次历史支持回退，旧修订暂不自动清理，以免破坏正在执行的快照；因此 Chroma 原始 count 可能大于活动 count。已有无修订标签的片段不会参与 v2 活动检索。

默认文档身份由来源、标题、年份生成；改标题且需替换同一逻辑文档时显式传稳定的 `document_id`。整份部署文件重导入会替换该文件的文档集合。`valid_from/valid_until` 目前只保存元数据，尚未实现自动过期调度；年份过滤仍按明确的 `effective_year`。

管理操作统一要求 `X-Admin-Token`；未设置 `ADMIN_API_TOKEN` 时返回 503，不再默认开放写入。发布回退同样需要令牌。普通 `/session` 与 `/chat` 不受此设置影响。

## ONNX 在哪里使用

`chromadb==0.5.23` 的默认 Embedding 函数使用 ONNX Runtime 运行 `all-MiniLM-L6-v2`，输出 384 维向量。项目没有显式传入向量时，Python 客户端负责计算，即使数据库使用 `HttpClient` 连接远端 Chroma 也是如此。

| 位置 | 触发向量化的操作 |
| --- | --- |
| `mcp/knowledge_base.py` | 文档 `upsert(documents=...)` 与语义检索 `query(query_texts=...)` |
| `memory/conversation_memory.py` 的旧实现 | 历史摘要入库、相关历史检索、用户画像 `upsert(documents=...)`；当前 v2 API 已停用这些跨会话路径，MySQL 偏好与账本不使用 ONNX |
| `Dockerfile` | 预下载模型并复制到应用用户缓存，避免运行时下载 |

CSV 查询、Redis 状态/幂等和远端大模型生成本身不使用 ONNX。此前错误来自 Windows Anaconda 的 DLL 加载；容器使用独立 Linux 环境，ONNX Runtime 1.27.0 已完成真实推理，因此 Docker 部署不需要先修复该 Windows 环境。

## 启动 v2

Compose 项目名和容器名均使用 `hellohebut-v2`，宿主机端口与 v1 分开，网段由 Docker 分配；资料目录/CSV 只读共享，运行数据和评测产物使用本目录。

```powershell
docker compose config --quiet
docker compose up -d --build
docker compose ps -a
```

默认网页为 `http://localhost:8088`，后端为 `http://localhost:18000`，Chroma 为 `18001`、Redis 为 `16379`、Prometheus 为 `19090`、MySQL 为 `127.0.0.1:15433`。Compose 共六个服务，容器内使用 `chromadb:8000`、`redis:6379`、`mysql:3306`。宿主机直跑 Python 使用 `.env.example` 中的 `MYSQL_HOST/PORT/DATABASE/USER/PASSWORD`；可选 `V2_FACT_DATABASE_URL=mysql://...` 优先于这些字段。Compose 的镜像、宿主机端口和初始密码由 `V2_MYSQL_IMAGE/PORT/PASSWORD` 配置。

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

本批完成后暂停审查。下一阶段在 MySQL 账本基础上拆分工作流节点并接入 LangGraph Checkpointer，验证更细粒度的恢复；中文 Embedding 对照、人工真值与标准构建继续保留为未完成验收项。
