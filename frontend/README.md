# 河北工业大学本科报考咨询前端

本科报考咨询的 Vue 前端，通过统一的同源代理访问 Python 后端。

## 功能

- 适配 Python `/chat` 响应字段：`conv_id`、`agent_type`、`latency_ms`、`admission_data_used`。
- 支持聊天调试、健康检查、监控摘要、知识库检索、知识库文档导入、文件上传。

## 默认后端地址

| 后端 | 默认地址 |
|------|----------|
| Python | `http://localhost:8000` |

开发模式下，Vite 会代理：

| 前端路径 | 代理到 |
|----------|--------|
| `/api/python` | `http://localhost:8000` |

## 本地运行

安装依赖：

```bash
npm install
```

启动：

```bash
npm run dev
```

访问：

```text
http://localhost:5173
```

如果后端端口不是默认值，可以启动时覆盖：

```bash
VITE_PYTHON_API_URL=http://localhost:8000 npm run dev
```

## Docker 部署

Docker 配置统一位于仓库根目录。回到根目录执行：

```bash
docker compose up -d --build
```

部署完成后访问 `http://localhost`。前端会通过同源路径 `/api/python` 访问 Python 后端。

## 后端地址

Python 后端默认地址：

```text
http://localhost:8000
```
