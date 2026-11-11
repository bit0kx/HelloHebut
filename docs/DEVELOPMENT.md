# 开发与扩展

本页维护代码入口、接口权限、前端开发和 Skills 编写规则。整体模块执行顺序见 [ARCHITECTURE.md](ARCHITECTURE.md)，环境配置见 [DEPLOYMENT.md](DEPLOYMENT.md)，测试方法见 [EVALUATION.md](EVALUATION.md)。

## 1. 代码与资源入口

| 目录 | 开发职责 |
| --- | --- |
| `api/` | FastAPI 生命周期、会话/偏好/任务/SSE 与管理入口 |
| `core/`、`workflows/` | ChatService、条件与契约、预算、模型适配；LangGraph 节点与 MySQL Checkpointer |
| `agents/`、`rag/` | 角色执行、Composer、私有只读工具；证据驱动检索与回答核验 |
| `memory/` | MySQL 事实/账本/任务、会话协调、摘要和偏好维护、SQL 迁移 |
| `tooling/` | 本地工具、CSV、知识发布/检索，以及独立标准 MCP Server/Client |
| `skills/`、`data/` | 运行时规则与参考文件；官方资料、CSV 等事实来源 |
| `frontend/`、`config/` | Vue 页面、HTTP/SSE 客户端、Vite/Nginx/Prometheus 配置 |
| `evaluation/`、`monitor/` | 评分/断言/manifest、指标与元数据追踪 |
| `experiments/v2/`、`docs/` | 可复现测试/数据集/报告及隔离中间产物；集中维护项目说明 |

`skills/**/SKILL.md` 和 `references/*.md` 是程序加载的规则资源，`data/demo_docs/*.txt` 是检索资料，`requirements.txt` 是依赖输入；它们保留运行路径，不作为普通说明文档搬入 `docs/`。

## 2. 本地后端与前端

后端建议使用与 Dockerfile 相同的 Python 3.12，在项目根目录创建独立环境并安装 `requirements.txt`。`.env` 会由 API 加载；宿主直跑需把 Redis/MySQL/Chroma 配置为宿主可达地址，不能使用 Compose 服务名。可启动独立数据库服务后运行：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 18000
# 交互式 CLI 同样复用 ChatService；每次启动创建新身份
.venv/Scripts/python.exe -m api.main --cli
```

API 与 CLI 是两个独立启动方式。已有容器占用 18000 时先停止对应 API 或改用其他端口，并同步前端目标地址。Windows 宿主 ONNX 曾出现 DLL 加载问题；已验证的 Embedding 运行环境为 Linux 容器。

前端使用 Node.js 22，在 `frontend/` 执行：

```powershell
npm ci
npm run dev
# 生成 frontend/dist/ 供生产 Nginx 使用
npm run build
```

开发页默认 `http://localhost:5173`；浏览器请求 `/api/python`，Vite 去掉前缀后转发到 `http://localhost:18000`。设置 `VITE_PYTHON_API_URL` 可覆盖目标；它也影响浏览器默认地址，使用绝对地址需匹配 CORS。生产 VITE 变量在构建时注入，修改后需重新构建；页面已保存的端点设置优先。

[backends.js](../frontend/src/lib/backends.js) 保存匿名令牌、端点、会话 ID、请求 ID、run_id 与事件序号。令牌绑定后端地址；切换后端清理旧身份，未完成请求不会被不同消息覆盖。网络中断查询原任务，刷新后可“查询未完成任务”；令牌过期不能恢复旧匿名身份。“新对话”不删除服务端历史，管理令牌仅留在页面内存。评测和 Skills 管理使用 Swagger，页面暂未提供对应面板。

## 3. 接口与权限速查

完整参数、响应模型和当前路由以运行服务的 `/docs`、`/openapi.json` 为准。下表只维护用途和权限，不复制 Schema。

| 接口组 | 权限与使用方式 |
| --- | --- |
| `POST /session` | 创建匿名身份；服务端签发 Bearer 令牌，不接受自选 user_id 作为授权 |
| `POST /chat/tasks`；`GET /chat/tasks/{run_id}`、`/events` | 本人 Bearer；提交后返回 run_id，按 SSE 序号续读；相同请求 ID 内容冲突会拒绝 |
| `POST /chat` | 本人 Bearer，同步兼容入口；传入 user_id 必须与身份相符 |
| `GET /conversations/{conv_id}/state`；`DELETE /conversations/{conv_id}` | 本人 Bearer；读已提交条件或清空会话/取消关联任务 |
| `/memories` 及确认、忽略、删除子接口 | 本人 Bearer 且偏好服务已启用；写操作带 expected_revision 和 operation_id |
| `POST /knowledge/add`、`/upload`、`/rollback` | `X-Admin-Token`；文档来源与内容仍需管理员实际核对 |
| `POST /skills/reload` | `X-Admin-Token`；重载后用 `GET /skills` 检查版本、哈希与错误 |
| `POST /eval/jobs`、`GET /eval/jobs/{run_id}`、`POST /eval/run` | `X-Admin-Token`；持久任务提交可用 X-Request-ID 幂等 |
| `/live`、`/ready`、`/health`、`/monitor`、`/metrics` | API 当前公开读入口，部署边界另由代理/网络管理 |
| `GET /skills`、`/knowledge/stats`、`/admission/stats`、`/admission/query`；`POST /search` | API 当前公开读入口，检索调试不写入用户会话 |

在线工具直接调用本地 ToolManager；标准 MCP 是独立的 `python -m tooling.mcp_server` stdio 入口，供受控客户端复用三个只读工具。普通聊天不自动经 MCP 子进程调用。接口、工作流或存储变更需运行对应回归与故障检查，命令统一见评测文档。

## 4. Skills 格式与维护

Skills 保存咨询流程，年度政策与录取数字仍来自知识资料和 CSV。稳定角色底线常驻系统规则；Skill 不授予工具权限，也不执行脚本。当前七项规则为四类角色规则 `admission-general/policy/risk/planning`，以及 `policy-comparison`、`admission-risk-analysis`、`evidence-verification`。

新建 `skills/<小写连字符名称>/SKILL.md`，目录名与 name 一致；正文不得为空。项目格式示例：

```yaml
---
name: policy-comparison
description: 比较不同年份和对象的政策证据。
metadata:
  version: "1.0.0"
  agents: "policy,planning"
  keywords: "比较,对比,变化"
  references: "references/comparison-checklist.md"
  dependencies: "knowledge_search"
  tests: "test_skills_dynamic.py"
---
```

正文接在第二个 `---` 后。name 最多 64 字符，description 必填且最多 1024 字符；version 使用 `1.0` 或 `1.0.0` 形式。metadata 的值必须为字符串，多项使用逗号分隔：

- `agents` 仅允许 general、policy、risk、planning；`keywords` 按不区分大小写的包含匹配选正文，空关键词匹配该角色全部请求。
- `references` 声明本 Skill 的 `references/` 相对文件；`dependencies/tests` 只作追溯，不自动调用工具或执行测试。
- 允许的其他 metadata 键为 `always/author/label`，当前不驱动额外执行行为；不要将 `always` 理解为绕过关键词和预算。
- 旧顶层 `agents/keywords/enabled/version` 保留兼容映射；新增规则使用 metadata，停用仍由顶层 enabled 表达。顶层 `allowed-tools` 可解析，但不会授予权限。

[SkillManager](../core/skill_loader.py) 使用受限 PyYAML SafeLoader，拒绝未知字段、重复键、对象标签、别名/锚点和越界路径。单文件最多 64 KiB，front matter 最多 8192 字符。解析错误只跳过该文件；references 内的 SKILL.md 不会被单独发现为规则。根目录旧 md/txt/json 格式仅为兼容入口。

加载分三步：先列适用角色的名称/说明/版本，再按角色与关键词选择完整正文，最后通过 `skill_reference_read` 按需读取已匹配且已声明的参考文件。参考文件必须位于对应 Skill 的 references 目录内，最多 8 KiB；工具循环默认关闭，开启后也不能借引用文件扩权。

`HELLOHEBUT_SKILLS_MAX_PROMPT_CHARS` 控制目录和规则的总字符量；过大正文整块省略，不截断半条规则。最终请求另受 [上下文预算](../core/context_budget.py) 约束，保留完整工具交互和必需事实。修改 Skill 后检查 `/skills` 解析结果、相关回归和案例效果，再通过管理接口重载；自动生成的规则仍需人工审阅。正文与参考内容会参与工作流版本指纹，影响未完成图状态的复用。
