# HelloHebut 前端

Vue 3 + Vite 7 页面提供聊天、来源展示、知识检索/导入、健康检查和监控摘要。v2 会话接口与运行方式见[实施与验证指南](../experiments/v2/README.md)。

## 本地运行

在本目录使用 Node.js 22，安装锁定依赖并启动：

```bash
npm ci
npm run dev
```

访问 `http://localhost:5173`。默认浏览器请求 `/api/python`，Vite 去掉此前缀并转发到 `http://localhost:18000`。

需要覆盖地址时，在 PowerShell 中设置：

```powershell
$env:VITE_PYTHON_API_URL = "http://localhost:18000"
npm run dev
```

该变量同时影响代理目标与浏览器默认地址；绝对地址可能产生跨源请求，需匹配后端 CORS。页面已保存的接口设置优先于环境默认值。生产 `VITE_*` 值在构建时注入，修改后需重新构建或调整页面接口设置。

## 页面状态与管理操作

- 首次聊天自动获取服务端匿名令牌；令牌、服务端用户 ID、会话 ID 和接口地址保存在 localStorage，用户 ID 不可自行修改。清空页面不删除后端记忆。
- 网络失败后重试相同消息会复用请求 ID；令牌过期时清理身份，下次发送重新建立会话。
- 管理令牌仅保存在页面内存，用于知识写入；来源 URL 要求与权限见[接口表](../TECHNICAL_DESIGN.md#7-接口速查)。
- 页面没有评测和 Skills 管理入口，相关操作使用后端 Swagger。

v2 Docker 启动、访问地址及当前验收限制统一见[实施与验证指南](../experiments/v2/README.md)。
