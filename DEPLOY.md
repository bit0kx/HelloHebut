# HelloHebut Docker 部署与验证

本页保留 v1 部署基线；`v2` 的独立端口、运行目录和会话令牌调用方式见 [v2 实施与验证指南](experiments/v2/README.md)。

技术参数和接口定义见[技术索引](TECHNICAL_DESIGN.md)，本文只保留运行操作。

## 1. 准备环境

需要 Docker Desktop 或 Docker Engine，并支持 Compose v2。在项目根目录执行：

```powershell
docker version
docker compose version
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑 .env，至少填写：

```dotenv
ANTHROPIC_API_KEY=你的模型服务密钥
ANTHROPIC_MODEL=已获授权可用的模型标识
# 兼容服务按需填写 Anthropic Messages 协议地址
# ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
DEEPSEEK_THINKING_MODE=disabled
REDIS_PASSWORD=请替换为安全密码
CHROMA_HOST=chromadb
CHROMA_PORT=8000
# 设置后管理接口要求 X-Admin-Token
ADMIN_API_TOKEN=
```

Compose 会组装 Redis 地址，但不覆盖 Chroma 主机和端口；必须将示例的宿主机地址改为以上容器地址。其余配置及未生效的示例变量见[配置表](TECHNICAL_DESIGN.md#8-配置与默认值)。.env 不应提交密钥；前端改端口可增加 `FRONTEND_PORT=8088`。

## 2. 构建并启动

```powershell
docker compose config --quiet
docker compose up -d --build
docker compose ps -a
docker compose logs -f hellohebut
```

启动 frontend、hellohebut、redis、chromadb、prometheus 五个服务。首次构建下载依赖和 ONNX 模型；后端日志应显示连接 chromadb:8000、知识导入及 CSV 加载结果。容器 healthy 后继续执行第 5 节，验证真实查询与模型调用。

## 3. 访问地址

| 功能 | 默认地址 |
| --- | --- |
| 前端 | <http://localhost> |
| 前端代理健康检查 | <http://localhost/api/python/health> |
| 完整 Swagger 调试 | <http://localhost:8000/docs> |
| 监控摘要 / 指标 | <http://localhost:8000/monitor> / <http://localhost:8000/metrics> |
| Prometheus | <http://localhost:9090> |
| ChromaDB 心跳 | <http://localhost:8001/api/v1/heartbeat> |

前端通过 /api/python 代理。localhost/docs 可浏览定义，但 Swagger Execute 默认不带代理前缀，调试时直连上表后端地址。

## 4. 更新与维护

| 修改内容 | 生效方式 |
| --- | --- |
| data/demo_docs、data/hebut_admission.csv | `docker compose restart hellohebut`，重新扫描或加载 |
| skills/ | 按[Skills 说明](skills/README.md#热加载)热加载，或重启 |
| .env | `docker compose up -d --force-recreate`；restart 不重新载入容器环境 |
| 代码、依赖、Dockerfile、前端 | `docker compose up -d --build` |

管理接口的权限、资料来源与覆盖语义见[接口表](TECHNICAL_DESIGN.md#7-接口速查)及[索引说明](TECHNICAL_DESIGN.md#31-索引与来源)。管理令牌放在请求头 `X-Admin-Token`，不是模型密钥。

`docker compose down` 停止并删除容器，保留数据卷；加 `-v` 会删除 Redis、ChromaDB、Prometheus 命名卷，不用于普通更新。正式部署配置 HTTPS、强密码与网络访问限制，避免直接公网暴露后端和数据服务。

## 5. 部署验证

先检查状态与数据：

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/knowledge/stats
Invoke-RestMethod http://localhost:8000/admission/stats
```

再测试不调用模型的确定性查询，结果可对照[风险案例](main_flow.md#4-录取风险与多轮示例)：

```powershell
$query = [uri]::EscapeDataString("河北物理类位次12000，计算机专业风险如何")
Invoke-RestMethod -Uri "http://localhost:8000/admission/query?query=$query"
```

最后测试完整聊天：

```powershell
$body = @{
  message = "河北物理类位次12000，计算机专业风险如何？"
  user_id = "deploy_test_user"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "http://localhost:8000/chat" `
  -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

检查 intent、entities、admission_data_used、citations。追问“那电气呢”时复用返回的 conv_id 和同一 user_id。

在后端 Swagger 提交以下小规模评测；配置令牌时填写 X-Admin-Token：

```json
{
  "intent_cases": [],
  "dialog_cases": [
    {
      "question": "河北物理类位次12000，计算机专业风险如何？",
      "expected_intent": "score_risk",
      "expected_agent": "risk",
      "routing_assertions_hard": true,
      "require_admission_data": true,
      "require_citations": true,
      "reference": "当前CSV历史比较为相对匹配，不应承诺录取或给出录取概率。"
    }
  ]
}
```

空数组跳过该类用例，省略字段则使用对应默认用例。完整评测耗时较长，应直连后端，避免 Nginx 的 60 秒代理超时。

查看 data/eval/latest.json 的回答、引用、断言和 Judge 理由；判分与回归口径见[评测说明](TECHNICAL_DESIGN.md#6-评测方法与结果口径)。要更新冻结基线，核验报告后复制为同目录 baseline.json，再重启后端加载。

## 6. 排障

| 现象 | 检查方向 |
| --- | --- |
| 页面正常但聊天失败 | 后端模型错误、密钥权限、模型标识、协议地址 |
| Chroma 退回本地模式 | 第 1 节的容器地址、Chroma 心跳与连接日志 |
| CSV 单查正常，聊天却未使用 | 主意图是否为 score_risk，详见[聊天分支](main_flow.md#22-关键分支) |
| 检索无结果或资料更新未体现 | /search 的重排结果、资料导入报告，以及尚未过期的[工具缓存](TECHNICAL_DESIGN.md#5-记忆缓存与执行可靠性) |
| 管理接口 401 | X-Admin-Token 是否与后端配置一致 |
| 评测代理超时 | 按第 5 节直连后端；当前没有任务查询或断线续跑 |
