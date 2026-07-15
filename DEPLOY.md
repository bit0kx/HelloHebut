# 河北工业大学本科报考咨询 Docker 部署

本指南只保留 Docker Compose 部署、验证和维护所需内容。系统业务链路见 [main_flow.md](main_flow.md)。

## 1. 准备环境

需要 Docker Desktop 或 Docker Engine，并支持 Compose v2：

```powershell
docker version
docker compose version
```

复制环境变量示例：

```powershell
Copy-Item .env.example .env
```

至少检查 `.env` 中的以下配置：

```dotenv
ANTHROPIC_API_KEY=你的模型服务密钥

# 使用 Anthropic 兼容服务时按服务商要求填写
# ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
# ANTHROPIC_MODEL=deepseek-chat
DEEPSEEK_THINKING_MODE=disabled

REDIS_PASSWORD=请替换为安全密码
CHROMA_HOST=chromadb
CHROMA_PORT=8000
RAG_RERANK_MIN_SCORE=5.0

# 正式环境建议设置随机长字符串；留空表示不校验管理令牌
ADMIN_API_TOKEN=
```

注意：

- 代码读取的模型密钥名固定为 `ANTHROPIC_API_KEY`。
- Compose 会自动组装容器内的 `REDIS_URL`；ChromaDB 必须使用服务名 `chromadb:8000`，不能写宿主机的 `localhost:8001`。
- `.env` 已被 Git 忽略，不要提交密钥。
- 如需修改前端端口，可增加 `FRONTEND_PORT=8088`。

## 2. 构建并启动

先校验配置，再启动全部服务：

```powershell
docker compose config --quiet
docker compose up -d --build
docker compose ps -a
```

首次构建会下载镜像、Python/npm 依赖和 ChromaDB ONNX 模型，需要等待后端与前端变为 `healthy`。查看日志：

```powershell
docker compose logs -f hellohebut
docker compose logs -f frontend
```

Compose 启动五个服务：`frontend`、`hellohebut`、`redis`、`chromadb` 和 `prometheus`。

## 3. 访问地址

| 功能 | 默认地址 |
|---|---|
| 前端 | <http://localhost> |
| 前端代理健康检查 | <http://localhost/api/python/health> |
| 后端 Swagger | <http://localhost:8000/docs> |
| 后端监控摘要 | <http://localhost:8000/monitor> |
| Prometheus 指标 | <http://localhost:8000/metrics> |
| Prometheus 页面 | <http://localhost:9090> |
| ChromaDB 心跳 | <http://localhost:8001/api/v1/heartbeat> |

前端通过 `/api/python/*` 调用 FastAPI。`http://localhost/docs` 可以浏览 Swagger，但其中的 Execute 请求默认不带 `/api/python` 前缀，完整调试请使用 `http://localhost:8000/docs`。

## 4. 数据与配置更新

| 修改内容 | 生效方式 |
|---|---|
| `data/demo_docs` | `docker compose restart hellohebut`，启动时增量扫描 |
| `data/hebut_admission.csv` | `docker compose restart hellohebut`，启动时重新校验并加载 |
| `skills/` | 调用 `POST /skills/reload`，或重启后端 |
| `.env` | `docker compose up -d --force-recreate`，重建所有受环境变量影响的服务 |
| 代码、依赖、Dockerfile、前端 | `docker compose up -d --build` |

`data/demo_docs` 支持 `.txt`、`.md`、`.json`，按文件哈希增量导入。CSV 不进入 ChromaDB，当前用于查询河北、天津 2023—2025 年分专业最低分和最低位次。

通过 `POST /knowledge/add` 或 `POST /knowledge/upload` 导入知识时，来源 URL 必须属于 `hebut.edu.cn`。配置 `ADMIN_API_TOKEN` 后，以下接口必须携带同值请求头：

```text
X-Admin-Token: <ADMIN_API_TOKEN>
```

受保护接口为：

```text
POST /knowledge/add
POST /knowledge/upload
POST /skills/reload
POST /eval/run
```

## 5. 部署验证

健康与数据状态：

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/knowledge/stats
Invoke-RestMethod http://localhost:8000/admission/stats
```

测试聊天：

```powershell
$body = @{
  message = "2026年河北物理类考生，位次12000，报计算机专业风险如何？"
  user_id = "deploy_test_user"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "http://localhost:8000/chat" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

完整评测会多次调用模型且耗时较长，请通过后端 Swagger 的 `POST /eval/run` 执行，不要使用 Nginx 的 60 秒代理链路。结果写入 `data/eval/latest.json`，不会自动覆盖 `baseline.json`。

## 6. 常用维护

```powershell
# 状态与日志
docker compose ps -a
docker compose logs -f

# 重启或重新构建
docker compose restart
docker compose up -d --build

# 停止并删除容器、保留数据卷
docker compose down
```

`docker compose down -v` 会删除 Redis、ChromaDB 和 Prometheus 的命名数据卷，不要用于普通更新。

正式环境应配置 HTTPS、防火墙和强密码，并避免直接向公网暴露后端 `8000`、Redis `6379`、ChromaDB `8001` 与 Prometheus `9090`。
