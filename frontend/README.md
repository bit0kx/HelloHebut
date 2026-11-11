# HelloHebut 前端

Vue 3 + Vite 7 页面提供任务进度、断线恢复、条件纠正、来源原文、偏好管理、知识检索/导入与监控。v2 会话接口与运行方式见[实施与验证指南](../experiments/v2/README.md)。

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
- 聊天通过持久任务与 Bearer SSE 返回，核验和提交后才显示最终答案；断线查询相同 run_id，刷新后可点击“查询未完成任务”。发送重试复用请求 ID；令牌过期不能恢复旧匿名身份。
- 条件栏显示已提交条件，点击可填入更正消息；“新对话”不删除服务端历史。
- 管理令牌仅保存在页面内存，用于知识写入；来源 URL 要求与权限见[接口表](../TECHNICAL_DESIGN.md#7-接口速查)。
- 页面没有评测和 Skills 管理入口，相关操作使用后端 Swagger。

v2 Docker 启动、访问地址及当前验收限制统一见[实施与验证指南](../experiments/v2/README.md)。
