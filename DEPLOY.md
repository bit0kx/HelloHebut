# 河北工业大学本科报考咨询 Docker 部署指南

本文档说明如何使用 Docker Compose 部署本科报考咨询后端、前端、Redis、ChromaDB 和 Prometheus，以及如何访问前端、API 文档和监控页面。

## 1. 部署架构

执行根目录的 `docker-compose.yml` 会启动以下服务：

| 服务 | 容器名 | 宿主机端口 | 用途 |
|---|---|---:|---|
| Frontend + Nginx | `echomind-frontend` | `80`，可通过 `FRONTEND_PORT` 修改 | Vue 前端及 API 反向代理 |
| 报考咨询 API | `echomind-app` | `8000` | FastAPI 后端 |
| ChromaDB | `echomind-chromadb` | `8001` | RAG、情景记忆和用户画像的向量存储 |
| Redis | `echomind-redis` | `6379` | 工作记忆和会话缓存 |
| Prometheus | `echomind-prometheus` | `9090` | 指标采集和监控 |

容器之间通过 Docker 内部网络通信。后端访问 ChromaDB 时使用 `chromadb:8000`，不是宿主机的 `localhost:8001`。

## 2. 环境要求

- Docker Desktop 或 Docker Engine
- Docker Compose v2，即支持 `docker compose` 命令
- 可正常访问 Docker Hub 和 Python/npm 软件源
- 首次构建建议预留至少 5 GB 磁盘空间

检查安装：

```powershell
docker version
docker compose version
```

## 3. 配置环境变量

项目根目录必须存在 `.env`。如果还没有，可从示例创建：

```powershell
Copy-Item .env.example .env
```

使用 DeepSeek Anthropic 兼容接口时，至少确认以下配置：

```dotenv
ANTHROPIC_API_KEY=你的_DeepSeek_API_Key
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_MODEL=deepseek-v4-pro
DEEPSEEK_THINKING_MODE=disabled
RAG_RERANK_MIN_SCORE=5.0

REDIS_PASSWORD=请设置一个安全密码
CHROMA_HOST=chromadb
CHROMA_PORT=8000
```

管理接口可选使用共享令牌保护：

```dotenv
# 本地开发可以留空；正式部署应设置随机长字符串
ADMIN_API_TOKEN=
```

配置非空值后，调用 `POST /knowledge/add`、`POST /knowledge/upload`、
`POST /skills/reload` 和 `POST /eval/run` 时，必须在请求头中提供完全相同的
`X-Admin-Token`。未配置或配置为空时，后端不会校验管理令牌。

`ADMIN_API_TOKEN` 不是 DeepSeek API Key，也不是用户登录密码。前端的“管理令牌”输入框和
Swagger 的 `x-admin-token` 都只是把这个值作为 `X-Admin-Token` 请求头发送给后端。
修改 `.env` 中的令牌后，需要重新创建后端容器才能加载新环境变量：

```powershell
docker compose up -d --force-recreate echomind
```

注意：

- 代码读取的密钥变量名是 `ANTHROPIC_API_KEY`，即使实际使用的是 DeepSeek 密钥，也不要改成 `DEEPSEEK_API_KEY`。
- 咨询回答、意图识别和 JSON 提取默认关闭 DeepSeek 思考模式；即使 `.env` 未配置，代码也会按 `disabled` 处理。
- Docker 模式下 `CHROMA_HOST` 必须是 Compose 服务名 `chromadb`。
- `.env` 已被 Git 忽略，不应上传到 GitHub。
- 如需修改前端宿主机端口，可在 `.env` 中增加 `FRONTEND_PORT=8088`。

## 4. 部署前检查

在项目根目录执行：

```powershell
docker compose config --quiet
```

命令没有输出且退出码为 `0`，表示 Compose 配置可解析。

检查将要启动的服务：

```powershell
docker compose config --services
```

正常应包含：

```text
chromadb
redis
echomind
frontend
prometheus
```

## 5. 首次部署

在项目根目录执行：

```powershell
docker compose up -d --build
```

首次构建会下载基础镜像、Python/npm 依赖和 ChromaDB ONNX embedding 模型，耗时可能较长。模型会在镜像构建阶段写入后端镜像，容器运行时不需要再次下载。

查看启动状态：

```powershell
docker compose ps -a
```

查看所有服务日志：

```powershell
docker compose logs -f
```

只查看后端或前端日志：

```powershell
docker compose logs -f echomind
docker compose logs -f frontend
```

后端首次初始化 ChromaDB 和 ONNX 模型可能需要一些时间，建议等待所有服务显示为 `healthy`。

### 自动导入学院知识

后端每次启动都会扫描 `data/demo_docs`，自动导入 `.txt`、`.md` 和 `.json` 文件。Compose 会把该目录只读挂载到容器的 `/app/data/demo_docs`；`run-image.sh` 挂载整个 `data` 目录，因此也会自动生效。

导入按文件内容哈希增量执行：未变化的文件会跳过，文件更新后会先写入新片段，再删除该文件的旧版本片段。导入状态可在后端日志或 `GET /knowledge/stats` 的 `seed_import` 字段查看。新增或修改资料后的部署命令为：

```powershell
docker compose up -d --build
```

知识片段的索引会包含“文档标题、业务类别、学院、专业、关键词、章节、正文”，返回给前端和 Agent 的仍是原始正文。
当前索引版本变化时，即使资料文件内容未变，后端启动也会自动升级内置默认知识并重建 `data/demo_docs` 对应片段。
每条查询会同时建立类别候选池和全库候选池，两池均执行向量 0.6、关键词 0.4 的混合召回，并为向量、关键词各保底约 20% 的独有候选。最终按约 80% 类别、至少 20% 全库的软配额交错输出，任一侧不足时由另一侧补齐。每个改写查询最多返回 30 条，随后按排名轮询合并并优先使用片段 `id` 去重，线上取前 20 条进入 LLM 相关性重排。重排采用 0—10 分；低于 `RAG_RERANK_MIN_SCORE`
（默认 `5.0`）的结果会被丢弃，允许在没有可靠知识时返回空结果。

如果只是修改已挂载的 `data/demo_docs` 文件，不必重建镜像，重启后端即可触发扫描：

```powershell
docker compose restart echomind
docker compose logs -f echomind
```

纯文本资料默认归类为 `major_info`，标题取文件名，文件名中的四位年份会写入 `effective_year`。由于本地文本没有来源网址，系统只记录相对文件路径和内容哈希，不会虚构官方链接。JSON 文件仍可为每篇文档显式提供 `title`、`content`、`category` 和 `source_url`。

### 结构化录取数据

`data/hebut_admission.csv` 不导入 ChromaDB，而是在启动时加载为确定性查询数据，供 RiskAgent 查询河北、天津2023—2025年分专业最低分和最低位次。Compose 将该文件只读挂载到 `/app/data/hebut_admission.csv`。

CSV 采用长表结构，每行对应一个省份、年份、科类和专业，必需字段为：

```text
province,year,subject_type,major,min_score,min_rank,batch,source_file,source_url
```

服务兼容 UTF-8、UTF-8 BOM 和 GB18030 编码。启动时会校验字段、数值、重复键以及最低分/最低位次是否同时缺失；校验失败会停止启动，防止错误数据进入风险判断。可以通过以下接口验证：

```text
GET /admission/stats
GET /admission/query?query=河北2025年计算机科学与技术最低位次是多少
```

更新 CSV 后无需重建镜像，重启后端即可重新加载：

```powershell
docker compose restart echomind
```

## 6. 访问地址

默认端口下，可使用以下地址：

| 功能 | 地址 |
|---|---|
| 前端界面 | <http://localhost> |
| 前端代理的后端健康检查 | <http://localhost/api/python/health> |
| Nginx 健康检查 | <http://localhost/health> |
| Nginx 入口的 Swagger（主要用于浏览） | <http://localhost/docs> |
| ReDoc API 文档 | <http://localhost/redoc> |
| OpenAPI JSON | <http://localhost/openapi.json> |
| 后端直连地址 | <http://localhost:8000> |
| 后端直连 Swagger（可完整执行接口） | <http://localhost:8000/docs> |
| 后端直连健康检查 | <http://localhost:8000/health> |
| 后端监控摘要 | <http://localhost:8000/monitor> |
| Prometheus 指标 | <http://localhost:8000/metrics> |
| Prometheus 页面 | <http://localhost:9090> |
| ChromaDB 心跳 | <http://localhost:8001/api/v1/heartbeat> |

### 端口 80 与端口 8000 的区别

```text
浏览器 → localhost:80   → Nginx → Vue 前端
                              └→ /api/python/* → FastAPI

浏览器 → localhost:8000 → FastAPI 后端直连
```

`http://localhost` 默认访问端口 `80` 的 Nginx。Nginx 会提供前端页面，并将带
`/api/python/` 前缀的请求去掉前缀后转发给 FastAPI。因此前端实际调用的是：

```text
POST http://localhost/api/python/chat
POST http://localhost/api/python/knowledge/add
```

Nginx 还单独转发了 `/docs` 和 `/openapi.json`，所以 <http://localhost/docs> 能正常显示
FastAPI 的 Swagger 页面。但是该页面点击 **Execute** 时，Swagger 会按同源地址请求
`POST http://localhost/eval/run`、`POST http://localhost/chat` 等不带 `/api/python` 的路径。
这些路径会落到前端静态站点，通常返回 Nginx 的 `405 Not Allowed`，而不是 FastAPI 响应。

因此：

- <http://localhost/docs> 适合浏览接口定义；只有 Nginx 明确转发的少数路径能够直接执行。
- <http://localhost:8000/docs> 直接连接 FastAPI，适合在本机完整调试所有后端接口。
- 前端页面不受上述限制，因为前端代码使用 `/api/python/*` 路径。
- `405` HTML 表示请求没有到达 FastAPI，与 `x-admin-token` 无关；令牌错误会返回 FastAPI 的 `401` JSON。
- 正式部署不建议向公网开放 `8000`，应通过 HTTPS 和受控反向代理访问管理接口。

如果部署在远程服务器，将 `localhost` 替换为服务器 IP 或域名，例如：

```text
http://192.168.1.100
http://192.168.1.100/docs
```

还需要确认服务器防火墙允许对应端口访问。生产环境不建议直接暴露 Redis、ChromaDB、后端 `8000` 和 Prometheus 端口。

## 7. 测试聊天接口

PowerShell 示例：

```powershell
$body = @{
  message = "2026年河北物理类考生，位次12000，报电气专业风险如何？"
  user_id = "deploy_test_user"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "http://localhost:8000/chat" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

也可以通过前端代理调用：

```powershell
Invoke-RestMethod `
  -Uri "http://localhost/api/python/chat" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

调用聊天接口会产生 DeepSeek API 用量。

## 8. 运行评测

完整评测包含意图用例和端到端对话用例，会多次调用模型并可能运行数分钟。Nginx 的
`/api/python/` 代理当前读取超时为 60 秒，因此评测应直连端口 `8000`，不要从
<http://localhost/docs> 的 Swagger 执行。

可以使用 <http://localhost:8000/docs>，在 `POST /eval/run` 中填写可选的
`x-admin-token`，Request Body 填写 `{}`；也可以使用 PowerShell：

```powershell
$adminToken = ""  # .env 未配置 ADMIN_API_TOKEN 时保持为空
$headers = @{}
if ($adminToken) {
  $headers["X-Admin-Token"] = $adminToken
}

$report = Invoke-RestMethod `
  -Uri "http://localhost:8000/eval/run" `
  -Method Post `
  -Headers $headers `
  -ContentType "application/json" `
  -Body "{}" `
  -TimeoutSec 1800

$report | Select-Object suite_version,total,passed,pass_rate,avg_scores,judge_stats,regressions | Format-List
```

测评完成后，完整报告会自动写入 `data/eval/latest.json`。查看失败用例：

```powershell
$report.results |
  Where-Object { -not $_.passed } |
  Select-Object test_id,detail,scores |
  Format-List
```

`judge_stats.coverage` 表示成功完成 Judge 评分的对话用例比例；Judge 失败项不会进入四维质量均分。
只有在确认覆盖率正常、没有 Judge 调用异常、确定性断言错误和事实数据问题后，才应将
`data/eval/latest.json` 人工复制为 `data/eval/baseline.json`，然后重启后端加载新基线。

## 9. 常用维护命令

查看状态：

```powershell
docker compose ps -a
```

启动已创建的服务：

```powershell
docker compose start
```

停止服务但保留容器：

```powershell
docker compose stop
```

重启服务：

```powershell
docker compose restart
```

停止并删除容器及网络，保留数据卷：

```powershell
docker compose down
```

重新构建并启动：

```powershell
docker compose up -d --build
```

拉取代码后更新部署：

```powershell
git pull
docker compose up -d --build
```

删除容器和所有命名数据卷：

```powershell
docker compose down -v
```

警告：`down -v` 会删除 Redis、ChromaDB 和 Prometheus 的持久化数据，不能用于普通更新。
