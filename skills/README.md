# HelloHebut Skills

Skills 保存版本化咨询流程，不保存年度政策或录取数字，不授予工具权限、不执行脚本。稳定角色底线在 Agent 的系统规则中常驻；本目录内容按任务加载。

| Skill | 用途 |
| --- | --- |
| `admission-general`、`admission-policy`、`admission-risk`、`admission-planning` | 保留四类角色业务规则，目录从原下划线名称迁为标准 slug |
| `policy-comparison` | 跨年份/专业政策比较、适用范围与缺项 |
| `admission-risk-analysis` | CSV 数字与风险解释、条件检查 |
| `evidence-verification` | 声明、引用、原文与冲突检查 |

## 格式与校验

采用 [Agent Skills 格式](https://agentskills.io/specification)，使用受限的 PyYAML SafeLoader；拒绝未知顶层字段、重复键、对象标签、别名、超大文件和越界路径。`name` 为与目录一致的小写连字符名称，`description` 必填。项目额外要求 `metadata.version`，并只接受声明过的项目元数据：

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

`agents`、`keywords`、`references`、`dependencies`、`tests` 使用逗号分隔字符串；这些是本项目的匹配/追溯元数据。旧顶层 `agents/keywords/enabled/version` 保留显式映射兼容，新增文件应使用 `metadata`。顶层 `allowed-tools` 可以解析但不授予权限，权限始终由程序配置的 ToolManager 决定。

## 逐步加载与预算

1. 请求只列出适用角色的名称、说明和版本。
2. 按角色、关键词选择正文；正文按完整块加载，超过预算时整块省略，不截断一半规则。
3. 模型确需参考资料时，通过 `skill_reference_read` 读取当前已匹配 Skill 声明的文件。资源必须位于该 Skill 的 `references/` 内且不超过 8 KiB。参考文件内容不能改变系统权限。

`HELLOHEBUT_SKILLS_MAX_PROMPT_CHARS` 限制目录和规则的字符总量；最终模型输入另受 `HELLOHEBUT_CONTEXT_MAX_INPUT_TOKENS` 限制。当前使用 UTF-8 字节数作为保守 token 上界，并记录估计方法；必需事实放不下时明确失败，不剪断 CSV 或工具交互。相关实现：[加载器](../core/skill_loader.py)、[上下文预算](../core/context_budget.py)、[只读工具](../agents/read_tools.py)。

`POST /skills/reload` 仍要求 `X-Admin-Token`，`GET /skills` 可检查版本、哈希和错误数量。一次解析失败只跳过该文件，不执行其任何内容。模型生成的新规则仍需人工审阅和回归验证后发布。
