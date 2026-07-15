# 河北工业大学本科报考咨询 Skills

服务启动时会从 `ECHOMIND_SKILLS_DIR` 读取 Skills，并按 Agent 类型注入 system prompt。招生简章、学院介绍等非结构化事实进入带来源和年份的知识库；分数、位次等表格数据进入结构化查询工具。Skills 只维护处理规则、澄清流程、风险边界和表达规范。

当前内置四类 Skills：

```text
skills/admission_general/SKILL.md   # 学校事实、校园生活、澄清与兜底
skills/admission_policy/SKILL.md    # 招生政策、体检选科、收费与资助
skills/admission_risk/SKILL.md      # 分数位次、计划和冲稳保风险
skills/admission_planning/SKILL.md  # 专业选择、就业升学与志愿规划
```

## Skill 文件格式

推荐每个 Skill 使用独立目录，并将主文件命名为 `SKILL.md`：

```text
skills/<skill_name>/SKILL.md
```

文件顶部使用简单 front matter：

```markdown
---
name: 录取数据与风险分析规范
description: 适用于 RiskAgent 的分数位次、计划和冲稳保分析
keywords:
agents: risk
enabled: true
---
```

字段说明：

- `name`：Skill 展示名称，会出现在注入给模型的 prompt 中。
- `description`：简短说明，方便 `/skills` 接口排查。
- `keywords`：触发关键词，留空表示该 Agent 每次调用都注入；多个关键词用英文逗号分隔。
- `agents`：适用 Agent，可填 `general`、`policy`、`risk`、`planning`。
- `enabled`：是否启用，支持 `true/false`。

## 编写要求

- 重要规则放在文档前半部分，因为过长内容会按 prompt 预算截断。
- 一类 Skill 只描述一类职责，不要把动态分数、计划和年度政策硬编码进 Skill。
- 优先包含处理范围、处理流程、信息边界和禁止事项等稳定章节。
- 对身份证号、准考证号、考生号、验证码等敏感信息必须写明禁止收集或公开。
- 对无法保证的事项使用保守措辞，例如“通常”“预计”“需要核验后确认”。
- 对政策冲突、个案资格和录取异常等场景要明确写出招生办人工确认条件。

## 热加载

修改 Skill 文件后，不需要重启服务，调用：

```bash
curl -X POST -H "X-Admin-Token: $ADMIN_API_TOKEN" http://localhost:8000/skills/reload
```

查看加载结果和解析错误：

```bash
curl http://localhost:8000/skills
```
