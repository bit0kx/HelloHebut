# 测试、实验与人工验收

本页统一说明如何验证 HelloHebut，以及哪些结果能够用于展示。测试脚本、数据集和正式报告继续存放在 `experiments/v2/`；`docs/` 只保存说明。以下命令均从项目根目录执行。

## 1. 当前证据与适用范围

最近一次完整工程验收为 2026-11-11 的第六批，见 [统一报告](../experiments/v2/reports/stage6_validation.json)。该报告核对了 16 份结果、48 个运行源码文件及 3 个前端资源，不能解释为全部业务质量目标已达成。

| 验证层 | 已记录结果 | 能证明什么 |
| --- | --- | --- |
| 原有 / v2 离线回归 | [45/45](../experiments/v2/reports/stage6_legacy.json)、[119/119](../experiments/v2/reports/stage6_regression.json)，零跳过 | 固定断言下的路由、条件、工具、Skills、隔离和预算契约 |
| 真实 MySQL | [偏好 11/11](../experiments/v2/reports/stage6_memory.json)、[账本 12/12](../experiments/v2/reports/stage6_turns.json)、[图恢复 8/8](../experiments/v2/reports/stage6_graph.json)、[任务 8/8](../experiments/v2/reports/stage6_jobs.json) | 事务、租约、并发、outbox、故障恢复与取消防复活 |
| 存储 / 服务 / 前端 | [存储 4/4](../experiments/v2/reports/stage6_storage_retrieval.json)、[模型容器 7/7](../experiments/v2/reports/stage6_deployment.json)、[Nginx/SSE 2/2](../experiments/v2/reports/stage6_task_http.json)、[前端 11/11](../experiments/v2/reports/stage6_frontend.json) | 指定功能场景通过；前端请求夹具不替代浏览器视觉检查 |
| 实际重启 | [MySQL 与应用重启通过](../experiments/v2/reports/stage6_restart.json) | 偏好与已生成答案保留、回放不重复提交；图节点硬退出恢复另由 graph 报告验证 |
| 标准 MCP / 模型协议 | [MCP stdio](../experiments/v2/reports/stage6_mcp_protocol.json)、[模型能力探测](../experiments/v2/reports/stage6_model_capability.json) 通过 | 真实协议交互和严格输出校验，不代表长期稳定性或回答准确率 |

`commit/base_commit` 记录测试开始时的 Git HEAD；提交前测试以源码、测试与数据指纹标识受测内容。保留 `initial`、`failed` 历史报告，不能只保留成功样本。真实模型探测曾有一次结构化输出未通过，见 [失败记录](../experiments/v2/reports/stage6_model_capability_failed.json)；默认仍为 `single`、`tool_loop=false`。

## 2. 回归与服务验证命令

离线测试使用项目依赖，要求 PATH 中存在 `redis-server`；脚本启动随机端口专用 Redis，并自行清理。数据库检查需要本机 v2 Compose 服务运行，只创建、操作和清理独立测试表/身份。`--inside` 用于 CI 或已配置数据库环境的容器内部执行，不需要 Docker 嵌套。

```powershell
python -X utf8 experiments/v2/scripts/run_tests.py --suite experiments/v2/legacy_tests --report experiments/v2/reports/live/legacy.json
python -X utf8 experiments/v2/scripts/run_tests.py --report experiments/v2/reports/live/regression.json
node experiments/v2/scripts/frontend_checks.mjs
python -X utf8 experiments/v2/scripts/fact_store_checks.py
python -X utf8 experiments/v2/scripts/turn_store_checks.py
python -X utf8 experiments/v2/scripts/graph_checks.py
python -X utf8 experiments/v2/scripts/job_checks.py
python -X utf8 experiments/v2/scripts/container_checks.py --report experiments/v2/reports/live/storage_retrieval.json
```

下面调用已配置的真实模型，产生模型用量。容器冒烟覆盖已部署链路；`live_smoke.py` 只使用两条合成政策资料，不证明真实知识库召回质量。

```powershell
python -X utf8 experiments/v2/scripts/deployment_smoke.py --build-mode reused_local_base --report experiments/v2/reports/live/deployment.json
python -X utf8 experiments/v2/scripts/task_http_smoke.py
python -X utf8 experiments/v2/scripts/live_smoke.py
docker compose exec -T hellohebut python experiments/v2/scripts/role_ablation.py --live-probe
docker compose cp hellohebut:/app/experiments/v2/reports/stage6_model_capability.json experiments/v2/reports/live/model_capability.json
```

`--build-mode` 是报告标签，应按实际构建方式填写，不会改变构建行为。以下重启验证会短暂中断本机 v2 服务，必须单独运行，不与其他检查并发：

```powershell
python -X utf8 experiments/v2/scripts/memory_restart_smoke.py
```

部分专项脚本固定写入 `reports/stage6_*.json`；复跑前保留已有历史报告，不能把覆盖旧文件当作新增批次证据。`final_validation.py` 会读取指定的第六批报告并检查当前源码、运行容器、前端资源与依赖是否一致；它只适用于完整重跑对应报告后的核对，不是文档修改必须执行的检查。

## 3. MCP、路由和角色消融

```powershell
python -X utf8 experiments/v2/scripts/compare_routing.py
python -X utf8 experiments/v2/scripts/role_ablation.py
python -X utf8 experiments/v2/scripts/retrieval_ablation.py
docker compose exec -T hellohebut python experiments/v2/scripts/mcp_checks.py
docker compose cp hellohebut:/app/experiments/v2/reports/stage6_mcp_protocol.json experiments/v2/reports/live/mcp_protocol.json
```

| 实验 | 固定条件与解释边界 |
| --- | --- |
| 必需工具路由 | 脚本化意图/回答配合真实 CSV，衡量复合问题是否漏查数字 |
| 角色与上下文 | 相同角色夹具比较 single、legacy_concat、composed，验证去重、冲突隐藏、来源范围和整轮裁剪；没有真实模型质量增益结论 |
| 词法与融合 | 固定已有资料和开发案例，比较 legacy、filtered_legacy、BM25 及融合；默认保留 filtered_legacy + balanced |
| MCP | 真实 initialize/list/call，知识检索、CSV、资料版本统计三个公共工具；检查参数拒绝、私有工具不暴露和资料版本未变化 |

MCP 本机 CSV 专项可运行 `python experiments/v2/scripts/mcp_checks.py --admission-only`，无需 Chroma/Redis，但仍是实际 stdio 子进程；完整检查需这些服务。在线聊天仍直接调用本地 ToolManager，MCP 测试通过不表示线上默认改为跨进程调用。

## 4. 中文 Embedding 对照

[第六批对照](../experiments/v2/reports/stage6_embedding_ablation.json) 固定 generation16 的 205 个片段、相同年份过滤和 Top-8，每查询重复三次。MiniLM 为 384 维，BGE-small-zh-v1.5 为 512 维，模型 revision、文件 SHA256、索引哈希和各案例命中均记录。

在两套已有开发集上，BGE 向量单路 Recall@8 为 75% / 100%，MiniLM 为 0% / 0%。这是**纯向量组件对照**，没有混合召回或重排，不能解释为最终问答准确率。开发集已参与诊断，文件名 `holdout` 不代表独立盲测；CPU 组件时延不能外推生产端到端延迟。BGE-M3 未运行，线上集合未替换。

实验环境独立放在 `experiments/v2/tmp/`，使用项目运行依赖及以下已测 CPU 组合；不要把实验依赖加入生产 requirements：

| 包 | 已测版本 |
| --- | --- |
| torch | 2.6.0+cpu，官方 CPU 构建 |
| transformers / safetensors | 4.46.3 / 0.7.0 |
| tokenizers | 0.20.3，与 Chroma 0.5.23 基线兼容 |

在该实验环境执行以下步骤。冻结时终端需提供实际 Chroma/Redis 连接配置；宿主机 v2 端口为 18001/16379，不能直接沿用容器主机名。`--prepare` 下载固定官方模型；`--freeze` 只读活动集合并写入本地冻结清单。

```powershell
python -X utf8 experiments/v2/scripts/embedding_ablation.py --prepare
python -X utf8 experiments/v2/scripts/embedding_ablation.py --freeze
python -X utf8 experiments/v2/scripts/embedding_ablation.py --repeats 3
```

最后一条自动读取 `tmp/embedding_corpus_manifest.json` 并校验语料哈希；也可显式传 `--corpus experiments/v2/tmp/embedding_corpus_<hash>.json`。运行需预备 MiniLM 的 ONNX 模型缓存；BGE 推理使用本地 safetensors，不执行远程模型代码。产物为 `tmp/embedding_models/`、`tmp/embedding_indexes/` 和 `reports/stage6_embedding_ablation.json`。

切换生产模型需先补独立验收，再新建集合、重建索引、验证和切换，并保留旧集合回退；不能把 512 维向量写入现有 384 维集合。

## 5. 人工参考与冻结门槛

[30 条审阅草案](../experiments/v2/datasets/human_review_template.jsonl) 覆盖十类场景，当前全部为 draft，人工批准数为 0。它是任务骨架，不能据此计算独立准确率。已有 `retrieval_reference.json`、`retrieval_holdout.json` 属于 development；`admission_routing.json` 验证工具路由，`policy_fixture.json` 是合成资料。

人工操作按以下顺序进行：

1. 复制草案，核对对应年度政策、CSV 来源或产品要求，填写 `expected.response_kind`、可核对的 `assertions`、必含/禁含项和必要工具，不能直接抄当前模型输出。
2. 实际审核后填写 `review.reviewer/reviewed_at/human_verified`，时间用带时区 ISO 8601；通过才设 `review_status=approved`，否则保留 draft 或 rejected。
3. 为每条题目提供 `review.verified_references`：reference_id、kind、path、sha256、locator、verified、verified_by、verified_at；官方政策/数据另填 source_url。locator 指向页码、章节或 CSV 行/查询条件，不能只填网站首页。
4. 独立验收另建未曝光题，设置 `provenance.case_origin=independent_human`、`review.used_for_tuning=false`、`review.independent_from_implementation=true`。开发题不能只改状态成为独立题；一旦用于调参，降级为开发集。

`response_kind` 取 answer、clarify、partial、refuse 或 handoff。must_include、must_not_include、required_tools、assertions 均为字符串数组；must_include / must_not_include / assertions 至少一组非空。每个数组最多 30 项，单项最多 1000 字符。此处填写评分要求，不执行用户提交的代码。

来源快照放 `experiments/v2/review_sources/`；这是评测数据，不是项目说明文档。不要放个人聊天、账号或敏感考生信息。可用 `Get-FileHash -Algorithm SHA256` 获取文件哈希并转为小写。引用结构如下，所有占位值均需实际核验后填写：

```json
{
  "reference_id": "source-1",
  "kind": "official_policy",
  "path": "experiments/v2/review_sources/年度政策快照.txt",
  "sha256": "本地快照的64位小写SHA256",
  "locator": "适用年份、章节、原文段落或查询条件",
  "source_url": "https://实际官方原始页面",
  "verified": true,
  "verified_by": "实际审阅人",
  "verified_at": "2026-11-11T18:00:00+08:00"
}
```

kind 可为 official_policy、official_data、product_requirement、synthetic_fixture。故障/权限题可引用产品要求或合成夹具，不必虚构政策出处。快照哈希证明内容未变化，不证明来源可信或人工判断正确。

```powershell
python experiments/v2/scripts/review_dataset.py validate --tier development --report experiments/v2/reports/live/review_development.json
python experiments/v2/scripts/review_dataset.py validate --input experiments/v2/datasets/人工复核结果.jsonl --tier independent --report experiments/v2/reports/live/review_independent.json
python experiments/v2/scripts/review_dataset.py freeze --input experiments/v2/datasets/人工复核结果.jsonl --tier independent --report experiments/v2/reports/live/review_freeze.json
```

无审核人、引用未核验、哈希不符、评分标签缺失或调参污染，均不能冻结为 independent。development 允许未审核骨架，但标记 `eligible_for_independent_acceptance=false`。当前草案被独立门禁拒绝是预期行为，见 [门禁记录](../experiments/v2/reports/stage6_review_independent_gate.json)。

冻结结果保存到 `experiments/v2/datasets/frozen/<tier>-<hash>.json`；benchmark_id 绑定题目、评分要求、审核声明与快照哈希，相同输入身份稳定，同名被篡改则拒绝覆盖。实现/model/prompt 变化记录到 run_manifest。校验器不认证真实审核人或事实真伪；当前在线 evaluator 仍使用原开发场景，人工完成后的冻结集还需接入独立验收，不能宣称已经自动运行。

## 6. CI、运行评测与剩余验收

[CI 工作流](../.github/workflows/v2.yml) 使用临时 MySQL/Redis、Python 3.12 和 Node 22，执行两套零跳过回归、四套真实 MySQL 检查、前端构建与断线夹具，不使用真实模型凭据。远程运行是否成功以 GitHub 结果为准；提交了配置不等于 CI 已运行。

管理员可通过 `POST /eval/run` 同步评测，或 `POST /eval/jobs` 提交、`GET /eval/jobs/{run_id}` 查询持久任务；使用 X-Admin-Token，任务幂等键为 X-Request-ID。评测复用 ChatService，但使用独立命名空间、逐次场景身份并关闭长期偏好，避免污染用户数据。响应区分固定 benchmark_id 与运行 manifest，并记录知识发布前后是否变化。

正式报告和可复现数据纳入 Git；`tmp/`、`traces/`、`reports/live/` 忽略。完整在线输出留在运行目录，公开报告不要包含凭据、真实身份或私人对话。

仍待验收：独立人工真值、至少 30 例 Judge 校准、真实 Langfuse 项目连接、页面目测及标准全新 Docker 构建。本机镜像源阻塞按既定决定保留；本地基础镜像验证不代表标准构建成功。匿名身份 24 小时限制、缺少周期归档及外部中断调用可能重做，属于当前产品边界，不能用上述通过率消除。
