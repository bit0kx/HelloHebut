# 河北工业大学本科报考咨询前端

本科报考咨询的 Vue 前端；保留 Python/Java 后端适配层，便于同一接口协议的实现复用。

## 功能

- 在页面中切换 Java / Python 后端。
- 统一适配 `/chat` 响应字段：
  - Python：`conv_id`、`agent_type`、`latency_ms`、`admission_data_used`
  - Java：`conversation_id`、`agent_type`、`latency_ms`
- 支持聊天调试、健康检查、监控摘要、知识库检索、知识库文档导入、文件上传。

## 默认后端地址

| 后端 | 默认地址 |
|------|----------|
| Python | `http://localhost:8000` |
| Java | `http://localhost:8080` |

开发模式下，Vite 会代理：

| 前端路径 | 代理到 |
|----------|--------|
| `/api/python` | `http://localhost:8000` |
| `/api/java` | `http://localhost:8080` |

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
VITE_PYTHON_API_URL=http://localhost:8000 \
VITE_JAVA_API_URL=http://localhost:8080 \
npm run dev
```

## Docker 部署

Docker 配置统一位于仓库根目录。回到根目录执行：

```bash
docker compose up -d --build
```

部署完成后访问 `http://localhost`。前端会通过同源路径 `/api/python` 访问 Python 后端。

## 后端启动参考

Python 版默认：

```text
http://localhost:8000
```

Java 版默认：

```text
http://localhost:8080
```

两个后端不需要同时启动。前端页面里选择当前要调试的后端即可。
