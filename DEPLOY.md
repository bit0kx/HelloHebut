# HelloHebut v2 部署与验证

需要 Docker Compose v2。首次创建配置，已有 .env 不覆盖：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker version
docker compose version
```

填写 ANTHROPIC_API_KEY、可用的 ANTHROPIC_MODEL，兼容服务按需设置 ANTHROPIC_BASE_URL。替换 Redis/MySQL 示例密码；已有数据库用户密码不会随环境变量自动改变。管理功能需 ADMIN_API_TOKEN，普通聊天不需要。不要提交 .env。

```powershell
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://localhost:18000/ready
```

| 服务 | 默认本机入口 |
| --- | --- |
| Vue/Nginx | http://localhost:8088 |
| API 文档 | http://localhost:18000/docs |
| Chroma | localhost:18001 |
| Redis | localhost:16379 |
| MySQL | 127.0.0.1:15433 |
| Prometheus | http://localhost:19090 |

端口由 V2_*_PORT 覆盖。Compose 自动覆盖容器内部地址；宿主机直跑填写 MYSQL_*、REDIS_URL、CHROMA_HOST/PORT。V2_FACT_DATABASE_URL 若设置则优先于 MySQL 分项配置。

本机标准构建曾持续遇到镜像源元数据长度校验错误，按用户要求未改全局源。本批复用本地基础镜像、安装锁定依赖、覆盖源码并构建前端验收。临时覆盖在忽略的 experiments/v2/tmp/，不作为可移植部署材料。MySQL 可选官方 ECR 镜像 V2_MYSQL_IMAGE=public.ecr.aws/docker/library/mysql:8.4。标准构建仍需在可用镜像源环境复验。

## 日常检查

/live 为进程存活，/ready 为 MySQL/Redis/Chroma 就绪；容器健康检查使用后者，/health 保留兼容。日志用 docker compose logs --tail=80 hellohebut；不要分享含凭据的配置输出。

MySQL/Redis/Chroma 各有独立数据卷。旧 PG 已停用并保留历史卷，当前无需 PostgreSQL。普通停止用 docker compose down，不附加 -v。

任务在 MySQL，应用重启后 worker 在租约过期时接管；中断调用可能重做，事务防止整轮重复提交。规则更新可使图快照失效。开关、接口、恢复边界和测试命令集中在 [v2 指南](experiments/v2/README.md)。

前端开发：在 frontend/ 执行 npm ci、npm run dev，默认代理 API 18000；npm run build 生成 frontend/dist/。
