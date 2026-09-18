# 提示词与消息边界

本框架把提示词按来源、生命周期和 LLM 调用消费者分层，目标是保持同一工作 Agent 的 system 前缀固定，并让模型能够区分框架指令、用户输入和外部数据。上下文压缩和智能权限是独立 LLM 调用，不复用工作 Agent 的 prompt 或历史。

## 消息职责

| 载体 | 内容 | 生命周期 |
|---|---|---|
| 固定 `system` | `role.md` 身份、通用执行原则、工具协议、标签说明和安全边界 | Agent 创建时构建一次，模式切换和工具轮不改变 |
| 历史 `developer` | Manager 工作流、当前模式及模式流程、临时框架提醒 | 首轮在真实用户请求前追加；后续按模式变化或对应事件追加 |
| 外部 `user` | 能力目录、AGENTS.md、记忆简报、会话数据、环境、日期和 Hook 内容 | 首次 chat 或对应事件发生时追加 |
| 原始 `user` | 用户输入或框架排队的明确 user action | 按正常输入链路追加 |
| `tool` | 工具结果和按需加载的 Skill 正文 | 对应工具调用后追加 |
| tool schema | 工具参数、类型、枚举、默认值和单次调用契约 | 所有模式和 Agent 使用同一完整目录 |

`PromptMgr` 只负责组合这些来源。工具 schema 是参数契约的唯一权威；Manager 的 `system_guidance()` 只描述何时使用能力以及 ID、状态或产物如何跨调用流转，不复制字段手册。

## 独立 LLM 调用

| 调用 | 固定指令 | 动态输入 | 与工作 Agent 的关系 |
|---|---|---|---|
| 工作 Agent | `PromptMgr.build()` | 会话历史、模式 developer、工具 schema | 主/子 Agent 的正常推理调用 |
| 上下文压缩 | CompactMgr 的压缩专用 system | 待压缩历史、原文参照、重点和滚动摘要 | 复用 Provider，但不使用 PromptMgr 或 Agent 历史 |
| 智能权限 | `_JUDGE_SYSTEM_PROMPT` | 脱敏 JSON 和唯一 `record_verdict` schema | 独立结构化裁决，不接收 Agent 标签说明 |
| Web 安全审查 | `_WEB_SAFETY_SYSTEM_PROMPT` | 脱敏 JSON 和唯一 `record_verdict` schema | 与智能权限相同地保持隔离；当前授权路径未启用 |

压缩完成后，摘要会作为带 `<compacted_history_summary>` 的新 user 历史回灌工作 Agent；初始化外部 user、首个真实用户轮次和近期轮次保持原始消息角色，不再包进摘要消息。`<prior_compaction_summary>`、`<history_reference>` 和 `<history_to_summarize>` 是压缩调用私有标签。

## 外部信息与指令注入

首次外部 user 依次包含子智能体目录、技能目录、四层 `AGENTS.md`、项目记忆、会话上下文和运行环境。四层 `AGENTS.md` 按 common、role、global、project 顺序进入带 `source="AGENTS.md"` 和 `layer` 的 `<external_context>`，不进入 system/developer。它们可以提供任务范围内的项目约定和偏好，但不能覆盖 system/developer、用户授权、权限策略或工具边界。

环境、记忆、目录、共享摘要、Skill 和 Hook 内容同样属于外部信息。`src/prompt_tags.py` 的渲染器会转义属性，并中和外部正文中伪造的已注册标签边界；消息角色仍是权威级别的最终依据，标签本身不提升权限。

## 标签注册

`src/prompt_tags.py` 的 `PromptTag` 与 `PROMPT_TAGS` 是全部结构标签的唯一注册表。每项必须声明：

- 标签名和语义；
- canonical 消息角色；
- 信任类别；
- 实际读取该标签的独立 LLM 调用消费者；
- 必需属性。

工作 Agent system 和压缩专用 system 分别按消费者从注册表生成自己的标签说明，不在文档复制标签清单。智能权限使用 JSON 和工具 schema，不生成空的标签说明。所有标签生产点必须调用 `render_prompt_tag()` 或 `render_external_context()`，不得手写标签字符串。

新增或修改标签时，必须同步完成注册元数据、生产者改造和 `tests/test_prompt_tags.py` 覆盖；如果标签改变消息角色或 Provider 映射，还要更新对应 Provider 转换测试。删除标签时同时删除生产点、说明和失效测试，不保留兼容别名。

## Provider 适配

Agent 历史始终使用 canonical `system`/`developer`/`user`/`assistant`/`tool` 角色。Provider 只在请求副本中做语法映射：Anthropic 用 `<framework_instruction>` 包装历史 developer，缺少 developer 支持的 Chat API 映射为 system；这些转换不得把外部 user 内容提升为框架指令，也不得改变固定 system。
