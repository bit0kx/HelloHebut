# HelloHebut Skills

Skills 维护角色处理规则、澄清步骤和表达边界，匹配后注入 system prompt；不存储动态招生数据，不执行脚本或授予工具权限。实现见 [core/skill_loader.py](../core/skill_loader.py)。

## 内置规则与文件格式

| 目录 | Agent | 内容 |
| --- | --- | --- |
| `admission_general` | general | 学校、校园生活、澄清与兜底 |
| `admission_policy` | policy | 招生政策、选科体检、收费资助 |
| `admission_risk` | risk | CSV 数字使用、位次优先和风险解释 |
| `admission_planning` | planning | 专业比较、就业升学与规划 |

推荐在 `skills/<name>/SKILL.md` 中使用以下头部，然后编写正文：

```markdown
---
name: 录取数据与风险分析规范
description: 分数位次查询与风险解释
keywords:
agents: risk
enabled: true
---
```

`name`、`description` 用于展示；`agents`、`keywords` 支持英文逗号分隔。头部使用自定义轻量解析器，不支持完整 YAML；加载器还接受 TXT 和 JSON。

## 匹配与预算

加载目录由 `HELLOHEBUT_SKILLS_DIR` 指定。请求时依次检查启用状态、角色和关键词：关键词为空则通过，否则任一命中即可。四个内置 Skill 均仅限定角色，因此对应 Agent 每次调用都会注入规则。

单个 Skill 正文默认约 3200 字符，总内容预算由 `HELLOHEBUT_SKILLS_MAX_PROMPT_CHARS` 控制，默认 5000；包装说明另占上下文，这不是精确 token 限额。

编写时把关键规则放在前面，围绕职责、处理步骤、缺失信息和升级条件组织内容。年度政策与录取数字放到知识库或 CSV；不要要求用户提供身份证号、准考证号、验证码等敏感信息，也不要承诺录取结果。

## 热加载

Compose 已挂载规则目录，修改后无需重建镜像。以下为 Bash 示例，管理令牌变量需与后端配置一致：

```bash
curl -X POST -H "X-Admin-Token: $ADMIN_API_TOKEN" http://localhost:8000/skills/reload
curl http://localhost:8000/skills
```

检查加载结果中的角色、关键词、启用状态与错误列表，再发送相应问题验证。单文件解析失败不会阻止其他规则加载。角色路由见[技术文档](../TECHNICAL_DESIGN.md#22-角色路由与协作)。
