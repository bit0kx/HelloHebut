# 部署与运行

本页维护启动、配置和运维入口。模块机制见 [架构与流程](ARCHITECTURE.md)，验证命令与证据见 [评测与验收](EVALUATION.md)。依赖版本以 [requirements.txt](../requirements.txt)、[前端锁文件](../frontend/package-lock.json) 和 [Dockerfile](../Dockerfile) 为准。

## 1. Docker 启动

需要 Docker Engine/Desktop 和 Compose v2。从项目根目录执行；已有 `.env` 不覆盖：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker version
docker compose version
```

编辑 `.env`：`ANTHROPIC_API_KEY` 必填，`ANTHROPIC_MODEL` 填写实际可用模型；兼容 Anthropic Messages 协议的服务可设置 `ANTHROPIC_BASE_URL`。未设置模型名时，代码仍回落到 `claude-3-5-sonnet-20241022`，这不保证该模型在当前账户可用。`DEEPSEEK_THINKING_MODE=disabled` 仅由兼容适配器对 DeepSeek 地址生效。

首次初始化前设置 `REDIS_PASSWORD`、`V2_MYSQL_PASSWORD` 和 `V2_MYSQL_ROOT_PASSWORD`，替换示例密码。已有 MySQL 用户密码不会随环境变量变化自动修改。普通聊天使用匿名 Bearer 身份；管理操作另需 `ADMIN_API_TOKEN`，未配置时管理接口返回 503。`.env` 已被 Git 忽略。

```powershell
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://localhost:18000/ready
```

| 服务 | 默认本机入口 | 容器内地址 / 用途 |
| --- | --- | --- |
| Vue / Nginx | http://localhost:8088 | `frontend:80`，页面与 `/api/python` 代理 |
| FastAPI | http://localhost:18000/docs | `hellohebut:8000`，API 与 Swagger |
| MySQL 8.4 | `127.0.0.1:15433` | `mysql:3306`，唯一关系数据库 |
| Redis | `localhost:16379` | `redis:6379`，身份与知识活动清单 |
| Chroma | `localhost:18001` | `chromadb:8000`，知识向量与版本片段 |
| Prometheus | http://localhost:19090 | `prometheus:9090`，抓取 API `/metrics` |

端口可由 `V2_FRONTEND_PORT/API_PORT/MYSQL_PORT/REDIS_PORT/CHROMA_PORT/PROMETHEUS_PORT` 覆盖。当前 Compose 只把 MySQL 宿主端口显式绑定到回环地址，其余端口沿用默认映射；本机演示配置不等于完成了公网部署。

Compose 自动覆盖容器内部 MySQL/Redis/Chroma 地址及数据路径。`V2_FACT_DATABASE_URL` 若非空，仍优先于 MySQL 分项配置，只接受 `mysql://` 或 `mysql+asyncmy://`；不要在容器环境保留指向宿主 `localhost` 的连接串。

## 2. 当前有效配置

下表区分直接运行 API 时的代码默认值与 Compose/示例配置，避免把实验开关当成线上已启用能力。

| 配置 | 默认行为与调整目的 |
| --- | --- |
| `HELLOHEBUT_CHAT_GRAPH` | `true`；启用 LangGraph，设 `false` 对照顺序流程，MySQL 账本仍生效 |
| `HELLOHEBUT_AGENTIC_RAG` | `true`；设 `false` 对照原检索路径，CSV 必需调用仍生效 |
| `HELLOHEBUT_VERIFIED_MEMORY` | Compose 和 `.env.example` 为 `true`；裸 API 未设置时为 `false`，仅关闭可验证偏好，聊天仍使用 MySQL |
| `HELLOHEBUT_ROLE_MODE` | `single`；可选 `composed` 一主一辅，`legacy_concat` 仅供旧拼接对照 |
| `HELLOHEBUT_TOOL_LOOP` | `false`；按需开启角色只读工具循环，不改变必需 CSV/检索流程 |
| `HELLOHEBUT_CONTEXT_MAX_INPUT_TOKENS` | `8000`；按 UTF-8 字节保守估算模型输入，必需事实超限会失败 |
| `HELLOHEBUT_SKILLS_DIR / HELLOHEBUT_SKILLS_MAX_PROMPT_CHARS` | 根目录 `skills/` / `5000` 字符；Compose 使用 `/app/skills` |
| `V2_CHAT_TIMEOUT_S / V2_WORKERS` | `50` 秒 / `2` 个 worker，worker 数限制为 1—4；与 API 同进程 |
| `RAG_LEXICAL_MODE / RAG_FUSION_MODE` | `filtered_legacy / balanced`；保留 `bm25`、`legacy` 对照 |
| `RAG_RERANK_MIN_SCORE` | `5.0`，LLM 重排的 0—10 分门槛 |
| `V2_KNOWLEDGE_COLLECTION` | `hello_hebut_v2`，知识集合；默认仍为 ONNX MiniLM 384 维，BGE 对照未替换生产索引 |
| `KNOWLEDGE_SEED_DIR / ADMISSION_DATA_PATH` | 启动导入资料目录 / CSV 路径；裸 API 可用空 seed 关闭自动导入，Compose 固定挂载现有资料 |
| `V2_TRACE_DIR / V2_EVAL_BASELINE_PATH` | `experiments/v2/traces/chat` / `experiments/v2/reports/live/baseline.json`，Compose 对应挂载目录 |
| `EVAL_JUDGE_MODEL` | 未配置时沿用回答模型；配置独立 Judge 不等于已完成人工校准 |
| `CORS_ALLOW_ORIGINS` | 按实际前端来源配置，绝对后端地址访问时尤其需要一致 |

`LANGFUSE_HOST`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 三项齐全才启用 OTLP/HTTP 元数据导出；默认未启用。使用 HTTPS，程序仅为限定的本地地址允许 HTTP；连接失败不阻断聊天。当前只有连接器契约测试，真实项目连接待验收。

`V2_INPUT_PRICE_PER_MILLION` 和 `V2_OUTPUT_PRICE_PER_MILLION` 留空时费用为 null；填写合同单价后按已返回 token 估算，币种由 `V2_PRICE_CURRENCY` 指定。估算不含缓存折扣、服务商调整和中断调用未返回用量。

`.env.example` 中旧的 `SECRET_KEY/JWT_SECRET_KEY`、`ENABLE_*`、`GRAFANA_*` 等条目未驱动当前对应功能；不能用它们声称已启用 JWT、关闭接口或部署 Grafana。有效配置以 [API 组装代码](../api/main.py)、具体模块读取和 [Compose](../docker-compose.yml) 为准。

## 3. 运维、恢复与数据保留

`/live` 检查进程，`/ready` 在有限时间内检查 MySQL、Redis、Chroma，容器健康检查采用后者。`/health` 保留初始化状态兼容接口，不能替代依赖检查。

```powershell
docker compose logs --tail=80 hellohebut
docker compose restart hellohebut
docker compose down
```

普通停止不附加 `-v`；MySQL、Redis、Chroma 各使用独立数据卷。旧 PostgreSQL 服务已停用，仅保留历史卷供核对，当前无需启动 PostgreSQL。运行日志、模型缓存、实验环境与索引放在 `experiments/v2/` 的忽略目录中。

应用退出后，worker 在旧任务租约过期后接管。已完成节点可恢复，中断的外部模型/工具调用可能重做；MySQL 整轮事务和回执避免重复提交。工作流规则变化使旧中间图失效，但不重算已经核验完成的有效答案快照或已提交回执。SSE 断线只查询原任务，不代表后台取消。

匿名令牌 24 小时失效后不能找回原身份；尚无跨设备账号恢复。偏好候选 7 天、确认默认 180 天，逻辑过期不等于物理删除。图快照的一小时 TTL 仅限制恢复；聊天、回执、图和任务没有周期归档清理策略。清空会话会取消关联任务、擦除会话与任务内容；已确认偏好使用独立删除接口。

## 4. 已记录的构建边界

本机标准全新构建曾持续遇到镜像源元数据长度校验错误，按用户要求没有修改 Docker 全局镜像源。第六批复用本地基础镜像、安装锁定依赖、覆盖源码并构建前端完成运行验证；临时覆盖位于忽略的 `experiments/v2/tmp/`，不作为可移植部署材料。

MySQL 可选 Docker 官方 ECR 分发镜像 `V2_MYSQL_IMAGE=public.ecr.aws/docker/library/mysql:8.4`。标准 Dockerfile 全新构建仍需在可用镜像源环境复验；本机功能测试通过不能替代该项。测试结果和未完成验收统一维护在 [EVALUATION.md](EVALUATION.md)。
