# Manager 服务层完整参考

本文档面向开发者与运维者，逐一说明 `src/mgr/` 下的各 Manager 类：单一职责、消费的配置/文件、公共方法、是否受 feature 门控、是否实现 `reload()`、持有的关键状态。所有事实以源码为准，方法名/字段名/配置键均保留英文。

术语沿用四层架构中的约定（详见 [architecture.md](architecture.md)）：四层架构、feature 门控和 `PermissionManager.authorize()` 单一授权入口。安全顺序见 [permissions.md](permissions.md)。

## Manager 服务层是什么

Manager 服务层是框架的横切能力层：每个 Manager 类各司其职（角色发现、模型管理、工具执行、权限、上下文压缩、提示词构建、子智能体调度、技能、MCP、记忆、计划、任务、会话、hooks、配置、插件、提醒），彼此低耦合，由上层按需组合。

### 两处装配点

Manager 分两批被构造：

1. **deps 层 Manager**（进程级、跨 agent 共享）——在 `src/app/bootstrap.py` 的 `create_app()` 中手动构造并注入 `AgentDeps` dataclass。包括：`ConfigManager`、`RoleMgr`、`ToolsMgr`、`MemoryMgr`、`ContextMgr`、`PluginMgr`、`HooksMgr`、`PlanMgr`、`McpMgr`、`PermissionManager`、`WebAccessMgr`、`SessionMgr`、`LLMMgr`。
2. **每 agent 层 Manager**（随 `Agent` 实例创建，主/子 agent 各自独立）——在 `Agent.__post_init__`（`src/agent/agent.py:113-160`）中构造。包括：`CompactMgr`、`SkillMgr`、`SubAgentMgr`、`PromptMgr`、`TaskManager`、`ReminderMgr`。子 agent 是共享同一份 `AgentDeps` 的完整 `Agent` 实例，因此复用 deps 层 Manager，但拥有自己的每 agent 层 Manager。

### feature 门控哪些 Manager

角色在 `role.md` frontmatter 声明 `features` 列表，`resolve_features()`（`src/mgr/features.py`）解析为有效启用集（合法名单：`task`、`skill`、`subagent`、`file`、`memory`、`plan`）。据此：

- deps 层：`MemoryMgr`（`memory`）、`PlanMgr`（`plan`）、`ContextMgr`（`subagent`）未启用时在 `bootstrap.create_app()` 注入 `None`。
- 每 agent 层：`SkillMgr`（`skill`）、`SubAgentMgr`（`subagent`）、`TaskManager`（`task`）未启用时在 `Agent.__post_init__` 置 `None`；`file` 直接门控 `apply_patch` 工具。
- 所有 agent 共享 `ToolsMgr.schemas()` 的完整 schema；未启用 feature 的工具由 `PermissionManager` 在执行期拒绝。

feature 语义细节（未声明→全开、未知名告警、`plan` 依赖 `file`）见 [architecture.md](architecture.md#feature-门控) 与 [roles-subagents-skills.md](roles-subagents-skills.md)。

### reload 协议

有会话级可变状态、需在 `/clear` 时重置的 Manager 实现 `reload()`。`/clear` 先检查工作目录信任，未记录时通过 EventBus 菜单确认；进入 reset gate 后按安全依赖顺序显式停止 MCP、更新信任与配置、重建秘密集、重载角色/插件/Hook、重配 LLM 并重启 MCP。每 Agent 层 Manager 随新 Agent 实例整体重建。

## Manager 一览表

| Manager | 职责一句话 | feature 门控 | reload |
|---|---|---|---|
| `RoleMgr` (`role_mgr.py`) | 按信任状态发现并激活角色，暴露角色资产路径 | 否 | 有 |
| `LLMMgr` (`llm_mgr.py`) | 按模型名/别名返回可用的 LLMProvider | 否 | 无 |
| `ToolsMgr` (`tools_mgr.py`) | 工具注册、执行、有界临时日志 | 否 | 无 |
| `PermissionManager` (`permission_mgr.py`) | 路径解析、代码硬拒绝、Plan 约束、智能权限和一次性确认 | 否 | 无 |
| `WebAccessMgr` (`web_access_mgr.py`) | 按当前模型和统一配置路由本地或 provider 原生 Web 能力 | 否 | 无 |
| `CompactMgr` (`compact_mgr.py`) | 上下文压缩与 transcript 落盘 | 否 | 无 |
| `PromptMgr` (`prompt_mgr.py`) | 构建固定 system、模式指令与初始外部上下文 | 否 | 无 |
| `SubAgentMgr` (`subagent_mgr.py`) | 四层扫描子 agent，调度委派 | `subagent` | 无 |
| `SkillMgr` (`skill_mgr.py`) | 多层扫描技能，按需注入全文 | `skill` | 无 |
| `McpMgr` (`mcp_mgr.py`) | 连接 MCP server、注册其工具 | 否 | 无（编辑需重启） |
| `MemoryMgr` (`memory_mgr.py`) | 项目记忆的加载/构建/读写 | `memory` | 有 |
| `ContextMgr` (`context_mgr.py`) | 跨 agent 共享上下文账本的记账、渲染与追加落盘 | `subagent` | 有 |
| `PlanMgr` (`plan_mgr.py`) | 计划模式切换与当前指令组装 | `plan`（依赖 `file`） | 无 |
| `TaskManager` (`task_mgr.py`) | 任务 CRUD、依赖、持久化、提醒 | `task` | 无 |
| `SessionMgr` (`session_mgr.py`) | 会话元数据/SessionState 持久化与恢复 | 否 | 无 |
| `HooksMgr` (`hooks_mgr.py`) | 8 类生命周期钩子的加载与执行 | 否 | 有 |
| `ConfigManager` (`config_mgr.py`) | 三层配置/settings/.env 合并 | 否 | 有 |
| `PluginMgr` (`plugin_mgr.py`) | 三层扫描插件目录 | 否 | 有 |
| `ReminderMgr` (`reminder_mgr.py`) | 中介，统一收集各源的提醒注入 | 否 | 无 |
| `features.py` / `paths.py` | feature 名单解析 / 三层目录路径 | — | — |
| `env_baseline.py` | 静态环境基线采集（纯函数，无状态） | — | — |

---

## RoleMgr — 角色发现与激活

`src/mgr/role_mgr.py`

**单一职责**：按信任状态发现角色，解析实际激活角色及其 `role.md`，暴露角色资产路径，并把角色级 `reasoning_effort` 覆盖应用到主角色 manifest。模型槽位由 `LLMMgr` 读取，`RoleMgr` 不从 `role.md` 取模型。

**发现与激活**：
- `discover_roles()` 按内置 `src/roles/` → 全局 `~/.agent/roles/` → 可信项目 `.agent/roles/` 扫描，同名后者覆盖；目录须含 `role.md`。
- 角色目录名作为配置 mapping key 原样使用，允许 Unicode、点号和长名称；`common`、`default` 是保留名，冲突目录告警并忽略。
- `active_role_name()` 读取 `role.default`；缺键、非字符串或空白回退 `coding`。`resolve_role_name()` 在配置角色未发现时同样回退 `coding`。
- 项目未信任时不发现项目角色；`ConfigManager` 仍可保留项目层 `role.default`，但实际只能激活已发现的内置或全局角色。

**manifest 契约**：`role.md` body 是主 agent 的角色提示词；frontmatter 可声明 features、thinking、reasoning_effort、memory、tools、startInPlanMode 等。`model` 键已禁止，哪怕值为空或 `null` 也会由 `_reject_manifest_model()` 抛 `LLMConfigurationError`，错误包含文件路径与应使用的 `role.<角色>.model.default/fast` 配置键；随后主角色 manifest 的 `model` 固定为 `None`，使主 agent 经 `LLMMgr.get(None)` 使用 default 槽位。

`role.<实际角色>.reasoning_effort` 是角色级单值覆盖：合法字符串经去空白、转小写后写入 manifest；缺键或 `null` 保留 `role.md` 值，非法值告警并忽略。模型 mapping 不写入 manifest，而由 `LLMMgr` 按实际角色名现读。

**模块级函数**：

| 函数 | 作用 |
|---|---|
| `discover_roles` | 按信任状态发现合法角色目录 |
| `active_role_name` | 读取并规范化 `role.default` |
| `resolve_role_name` | 在发现结果中解析角色，未命中回退 `coding` |
| `parse_frontmatter` | 从 `.md` 文本分离 YAML frontmatter 与 body |
| `extract_manifest` | 从 frontmatter 与 body 构造 `AgentManifest`，由角色和子 agent 共用 |

**公共属性与资产方法**：`active`、`manifest`、`role_name`；`agents_dir()`、`skills_dir()`、`plugins_dir()`、`agent_md_path()`、`mcp_servers_path()`；以及 `common_dir()`、`common_agents_dir()`、`common_skills_dir()`、`common_agent_md_path()`。

**feature 门控**：否。**reload**：有；按当前信任与配置重新发现、激活和解析角色。**关键状态**：`_role_path`、`_manifest`、`_all_roles`。

角色结构与配置示例见 [roles-subagents-skills.md](roles-subagents-skills.md) 和 [configuration-reference.md](configuration-reference.md)。

---

## LLMMgr — 模型管理

`src/mgr/llm_mgr.py`

**单一职责**：管理模型候选，并把角色槽位或显式的 `供应商/模型ID` 引用解析为 Provider。候选列表仅供选择，请求路由直接来自模型引用。

`ConfigManager` 保存角色选择，`RoleMgr` 确定激活角色。`LLMMgr` 管理配置快照、候选快照和以完整引用为键的客户端缓存；`src/llm/models.py` 提供向导与管理器共用的解析、规范化和发现逻辑。

| 方法 | 作用 |
|---|---|
| `refresh_models()` | 显式并发发现模型，与配置候选合并；错误记录在 `provider_errors`，不改变客户端缓存 |
| `reconfigure()` | 同步重读本地配置并清空旧端点的客户端和发现快照；用于 `/clear` |
| `resolve_model(model)` | 空值使用 default，兼容别名映射槽位，显式引用按第一个斜杠拆分；验证格式与供应商配置 |
| `get(model)` | 根据完整引用创建或复用 Provider，向 SDK 传递原始模型 ID |
| `web_mode_for_provider()` | 按客户端的明确供应商身份查询 Web 路由模式 |
| `list_models()` / `models_by_provider()` | 返回配置、在线发现和当前所选模型的排序并集；分组接口返回原始模型 ID |

两个角色槽位都必填且不提供模型兜底。槽位在每次解析时现读，`/models` 保存并 reload 后，新建子 agent 与智能权限使用新值。模型不在候选列表、发现失败或返回空列表均不影响请求发送；实际调用错误由供应商返回并进入统一 LLM 错误处理。启动与 `/clear` 不执行在线发现。

模型别名、effort 和 Provider 调用细节见 [llm.md](llm.md)。

---

## ToolsMgr — 工具注册与执行

`src/mgr/tools_mgr.py`

**单一职责**：工具注册表与执行引擎——维护稳定的完整 schema 目录、执行工具（串联 hook 与权限检查）、保存有界脱敏临时日志。

**消费的配置或文件**：构造时（`load_registered=True`）从 `src/tools/decorator.py` 的全局 `_registry` 载入所有 `@tool` 注册的工具（`tools_mgr.py:40-42`）；MCP 工具由 `McpMgr` 额外 `register()`。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `register` | `tool: ToolEntry` | `None` | 注册工具（重名跳过并告警） |
| `get` | `name: str` | `ToolEntry \| None` | 按名取工具 |
| `has` | `name: str` | `bool` | 是否已注册 |
| `list_entries` | — | `list[ToolEntry]` | 全部工具（只读优先排序） |
| `all_tool_names` | — | `set[str]` | 全部工具名 |
| `schemas` | — | `list[ToolDict]` | 所有已注册工具的稳定 OpenAI function-calling schema 目录 |
| `unavailable_in_mode` | `mode: RunMode` | `tuple[str, ...]` | 当前模式不能执行的完整工具名列表 |
| `execute` (async) | `tool_name`, `arguments`, `current_tool_call_id`, `deps`, `agent`, `run_context` | `ToolResult` | 执行工具全流程；`run_context` 供工具访问当前用户轮次状态（见下） |

**`execute()` 完整流程**：Pydantic 校验 → PreToolUse Hook → 修改后重校验 → `authorize()` → 脱敏的 `ToolCallStarted`（含 `ToolDisplay`） → 调用工具 → 提取 `ToolResult` → 立即脱敏和限长 → PostToolUse → 再次脱敏 → 一次输出整理 → `ToolCallCompleted`（含 `ToolDisplay`） → 历史。临时日志、Hook payload 和事件预览都只接收脱敏数据。

**展示数据生成逻辑**：
- `_emit_tool_started()`：接收 `arguments`，使用 `tool_title()` 生成中文标题（`src/tools/display.py` 的 `TOOL_TITLES` 映射），使用 `format_params()` 按工具类型格式化参数摘要（如 exec_command 提取命令）。`EXTERNAL_READ` 工具不生成参数展示。参数经 `DataGuard.redact()` 脱敏后传入 `ToolDisplay`。
- `_emit_tool_completed()`：接收 `tool_display`（来自 `ToolResult`，如文件差异）。若存在则直接使用并对 `content` 脱敏；否则使用 `format_result()` 截断结果内容生成通用 `ToolDisplay`。`EXTERNAL_READ` 工具不生成结果展示（`display=None`）。

**feature 门控**：否；feature、模式与 agent 范围统一在授权请求中判定。 **reload**：有，回收工具临时日志。

**持有的关键状态**：`_tools`（工具名→`ToolEntry`）、`_schemas`（注册表变化时失效的完整 schema 缓存）、`output`（ToolOutput 预算和临时日志）。

工具体系与内置工具见 [tools.md](tools.md)。

---

## PermissionManager — 单一授权服务

`src/mgr/permission_mgr.py`

**单一职责**：对一次已经校验的工具调用先执行 availability 判定，再执行路径解析、Hard Deny、Plan 约束、确定性策略、LLM 智能权限审查和一次性人工确认，返回冻结的 `AuthorizationResult`。

**构造依赖**：规范化 workdir、`JudgeClient`、一次性 yes/no 确认回调和共享 `DataGuard`。工具策略由调用方显式传入；授权服务不读取用户授权配置，也不依赖 EventBus、MCP Manager、ToolEntry 或 Agent。

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `authorize` (async) | `request: ToolAuthorizationRequest` | `AuthorizationResult` | 按 `ToolCallerContext` 检查 mode、feature、主/子身份和 manifest，再独立裁决风险；不缓存、不创建后续放行 |

**关键协作者**：`PathResolver` 统一规范化和分类路径，`HardDenyDetector` 处理不可覆盖的高危动作，`LLMJudgeClient` 每次通过 `llm_mgr.get("fast")` 现读激活角色的 fast 槽位，`StructuredVerdictRunner` 对该次结构化裁决覆盖 `reasoning_effort="low"`（并关闭 thinking、最多尝试三次），不修改缓存 Provider。fast 缺失或格式非法是配置错误；候选列表不限制调用，实际调用错误不触发 default 回退。`WebPrivacyGuard` 负责 Web 外部读取的本地隐私预检；`LLMWebSafetyClient` 虽在装配时注入，但当前 `_review_web()` 路径未调用，不应视为已启用的 LLM Web 审查。`DataGuard` 保证裁决请求、原因和展示详情不含原始秘密。**feature 门控**：否。**reload**：无。

---

## CompactMgr — 上下文压缩

`src/mgr/compact_mgr.py`

**单一职责**：判断是否需要压缩，按原子消息块无损切分历史，用 LLM 滚动生成摘要，拼装压缩后的上下文前缀，并把完整原始历史写入 transcript。

**构造参数**（每 agent 层，在 `Agent.__post_init__` `agent.py:202-212` 从 `compact` 配置换算）：
- `llm`：本 agent 的 provider（估算 token、生成摘要）。
- `workdir`：transcript 落盘目录 `workdir/.agent/transcripts/`。
- `caller_agent_type`、`caller_uuid`：摘要 LLM 调用沿用的 agent 类型与实例标识。
- `auto_compact_size` = `context_limit * compact.auto_compact_rate`；非正数禁用自动压缩。
- `keep_recent_user_turns` = `compact.keep_recent_user_turns`（缺省 3），定义优先保留近期原文的用户轮次范围。
- `recent_messages_token_limit` = `context_limit * compact.keep_recent_messages_token_rate`（缺省率 0.25），是近期原文的硬预算。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `is_need_compact` | `messages`, `prompt`, `tools`, `estimated_tokens` | `bool` | 判断完整 provider 输入估算是否超 `auto_compact_size`；可复用调用方估算，非正阈值直接返回 `False` |
| `track_recent_file` (async) | `path: str` | `None` | 维护最近文件列表（上限 5，去重后置尾） |
| `write_transcript` (async) | `messages: list` | `Path` | 在线程中以 UTF-8/Unicode JSONL 写入 `.agent/transcripts/transcript_{time_ns}.jsonl`，排他创建避免并发覆盖 |
| `split_history_for_compaction` | `messages`, `bootstrap_message_count` | `CompactionPartition` | 切分为初始化及首个真实轮次前缀、待摘要中段和预算内近期原文 |
| `summarize_history` (async) | `preserved_messages`, `messages_to_summarize`, `recent_messages` | `str` | 完整输入不超过上下文 95% 时一次摘要；超限则按原子块滚动摘要，单块仍超限时无损分页 |
| `build_compacted_context_prefix` | `summary`, `recent_files_hint` | `str` | 拼装摘要和近期文件提示消息 |
| `compact_history` (async) | `messages`, `bootstrap_message_count` | `CompactResult` | 端到端自动压缩；返回消息、transcript、摘要消息数及摘要正文，无可摘要消息或空摘要时保留原历史 |

**feature 门控**：否。 **reload**：无（随新 Agent 重建）。

Agent 显式传入初始化消息数量，CompactMgr 原文保留该 bootstrap、首个真实 user 及其前置 developer，并至少保留当前用户轮次。最近 N 个用户轮次超过硬预算时向当前轮收缩，但不会摘要这些强制前缀。中段旧 developer 不进入摘要；最新 `<collaboration_mode>` 若落在中段，会原样移动到近期后缀之前，保证压缩后的当前模式仍明确。序列化、token 计算、分页与 transcript 文件 I/O 均卸载到线程，且不做字符截断。

**持有的关键状态**：`recent_files`（最近文件路径，上限 5）、`has_compacted`（是否已完成过有效压缩）。

摘要调用使用 CompactMgr 自己的固定 system，包含压缩职责和仅供压缩模型读取的标签说明；待压缩历史、原文参照、重点和滚动摘要作为动态 user 消息。token 预算按同一份 system + user 请求估算。它只复用所属 Agent 的 Provider 和事件归属标识，不读取 `PromptMgr` 的固定 system 或工作历史。压缩结果中的摘要边界标签随后作为 user 历史回灌工作 Agent。

压缩在状态机的 `CHECK_COMPACT`/`COMPACT`/`CONTEXT_OVERFLOW` 阶段驱动，见 [agent-runtime.md](agent-runtime.md)。

---

## PromptMgr — 系统提示词构建

`src/mgr/prompt_mgr.py`

**职责**：构建 Agent 生命周期内固定不变的一条 system；另行生成 Manager/模式 developer 指令和首次 chat 前的外部 user 上下文。模式切换、模型切换和工具轮都不重建 system。

**固定 system 顺序**（`_build_static_prompt`）：
1. **身份与通用原则**——`role_prompt` 非空时用之，否则使用默认身份；按主/子 agent 注入固定责任、证据、推进和验收原则；
2. **工具协议**——schema 是参数契约的唯一权威；
3. **消息来源与标签**——由 `src/prompt_tags.py` 注册表生成当前 Agent 会看到的标签说明和信任边界；
4. **固定 Web 安全规则**。system 不包含 Manager 工作流、模式流程、当前日期、AGENTS.md、能力目录或其他运行期数据。

**初始外部上下文**（`build_initial_context_messages`）：子智能体目录、技能目录、四层 AGENTS.md、项目记忆、结构化会话上下文、运行环境依次渲染为带 `source` 的 `<external_context>`，再合并为一条 user 消息。AGENTS.md 保持共享 → 角色 → 全局 → 项目顺序，运行环境固定为最后一段；所有外部正文经集中渲染器中和保留标签边界。环境基线由 `collect_env_baseline()` 在 `AgentApp._reset_session` 中经 `asyncio.to_thread` 采集一次。

**Manager 与模式指令**：`build_manager_instructions()` 返回当前 Agent 实际具备的 Task/SubAgent/Skill/Memory 工作流，只在首个真实用户请求前注入一次；随后是当前模式及 turn-start 提醒，同一条 developer 中保持 Manager → 模式 → 提醒顺序。模式变化在下一条真实 user 前注入，post-round 提醒仍在下一次 chat 前追加。

**模式指令**（`build_mode_instructions`）：普通模式只包含简短的当前执行状态，不得提及计划流程、计划模式或 `submit_plan`；Plan 模式由 `PlanMgr.instructions()` 明确当前模式、只读边界、测试例外、禁止事项以及完整规划和提交流程。Plan 流程不是 Skill，不经过 `load_skill` 或工具结果注入。

`Agent._append_pending_framework_message()` 是唯一落点：真实用户轮次在追加 user 前先收集 Manager、最终模式和 turn-start 提醒；工具轮之间只在下一次 `llm.chat()` 前追加 post-round 或恢复指令。工具执行中途只排队，不修改消息。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `build` | — | `list` | 返回一条固定 system 消息 |
| `build_manager_instructions` | — | `str` | 返回首个真实用户请求前追加的 Manager 工作流 |
| `build_mode_instructions` | — | `str` | 返回当前最终模式的框架指令 |
| `build_initial_context_messages` | — | `list[dict]` | 返回首次 chat 前追加的外部 user 上下文 |

**feature 门控**：否（初始上下文和模式段按对应 Manager 是否存在动态出现）。 **reload**：无（随新 Agent 重建）。

**持有的关键状态**：`_system_content`（Agent 生命周期内固定的 system 正文）。

---

## SubAgentMgr — 子智能体调度

`src/mgr/subagent_mgr.py`

**单一职责**：四层扫描子 agent 定义，在加载期验证模型字段，暴露列表提示词段，并通过 `task_delegator` 构造和运行完整子 Agent。

**扫描与模型校验**：共享 `roles/common/agents/` → 激活角色 `agents/` → 全局 `~/.agent/agents/` → 项目 `.agent/agents/`，同名 `agent_type` 后者覆盖。每份文件在解析 frontmatter 时校验 model 的格式：

- `None` 合法，委派时由 `LLMMgr.get(None)` 使用角色 default 槽位；
- 固定别名只允许 `default`、`fast`、`opus`、`sonnet`、`haiku`；
- 其他字符串必须采用 `供应商/模型ID`，不查询模型候选列表；
- 非法值抛 `LLMConfigurationError`，消息包含 manifest 路径和合法格式。没有 `best`、`inherit`、子串匹配或静默回退。

**公共方法**：`describe()` 只返回按 type 排序的外部能力目录，`system_guidance()` 返回不含目录数据的固定协作工作流，`task_delegator(agent_type, prompt, parent_agent, task_id, description, shared_context)` 执行委派。

**`task_delegator` 关键行为**：
- 未知 `agent_type` 返回错误并列出已知；带 `task_id` 时先置 `in_progress` 并设 owner，异常或 `RunResult.llm_error` 时回滚为无 owner 的 `pending`，正常返回不自动 completed；
- manifest 工具声明原样传给 Agent，作为执行期授权边界；所有子 agent 仍接收统一 schema。模型原样传 manifest：`None`、槽位别名、兼容别名或完整 ID 最终都由 `LLMMgr.get()` 解析；
- `thinking` 自身未声明时继承父 agent；`reasoning_effort` 自身合法声明优先，否则继承 `parent_agent.reasoning_effort`，父值仍为空时继承父 Provider 的 effort；该 effort 继承与子 agent 选择哪个模型槽位相互独立；
- `features` 未声明时继承父 agent 已解析集，同时继承父 agent 当前 `mode`；
- 用 `Agent.from_manifest(is_subagent=True, ...)` 构造实例，触发 `SubagentStart`/`SubagentStop` hook 与 start/end 生命周期事件，异常和取消路径也发 end；
- **跨 agent 上下文交接的唯一枢纽**（见 [ContextMgr](#contextmgr--跨-agent-共享上下文)）：
  - *注入*——`run()` 之前把 `ContextMgr.digest()` 拼到 `prompt` 前面（摘要在前、任务正文在最后，recency）。`shared_context="none"` 可完全隔离，供独立复核用。账本为空时 prompt 逐字节不变。
  - *记账*——`SubagentStop` hook 处理**之后**把 `result` 记入账本，因此记的正是父 agent 实际收到的文本。仅当 `llm_error is None`、`agent_type` 在 `record_types` 白名单内、正文够长时才记；异常与取消路径走不到记账点，天然不记。

**feature 门控**：`subagent`。**reload**：无，随新 Agent 重建。**关键状态**：`_documents`（`agent_type` → `AgentManifest`）。

子 Agent 定义格式见 [roles-subagents-skills.md](roles-subagents-skills.md)。

---

## SkillMgr — 技能加载

`src/mgr/skill_mgr.py`

**单一职责**：多层扫描 `SKILL.md` 并以 `namespace:name` 注册，暴露外部技能目录和固定加载工作流，按需返回 `<skill>` 包裹的技能全文与附属文件 Markdown 索引。

**消费的配置或文件**：多层扫描（低→高优先级，同名后者覆盖，`_load_all` `skill_mgr.py:44-88`）：共享 `roles/common/skills/` → 角色 `skills/`（命名空间为角色名）→ 全局插件 `plugins/*`（命名空间为插件名）→ 全局 `~/.agent/skills`（`user`）→ 项目插件 → 项目 `.agent/skills`（`user`）。每目录递归 `rglob("SKILL.md")`。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `describe` | — | `str \| None` | 技能列表（`- [name]: description`，排序） |
| `system_guidance` | — | `str` | 目录匹配、`load_skill` 加载与权限边界工作流 |
| `check_skill` | `name: str` | `bool` | 技能是否存在 |
| `load_full_text` | `name: str` | `str` | 技能全文（`<skill>` 包裹 body 和附属文件索引），不存在则错误信息 |

**feature 门控**：`skill`。 **reload**：无（随新 Agent 重建）。

**持有的关键状态**：`_documents`（`namespace:name` → `SkillDocument`，含 `manifest`/`body`/`full_text`）。

技能系统详见 [roles-subagents-skills.md](roles-subagents-skills.md)。

---

## McpMgr — MCP 客户端

`src/mgr/mcp_mgr.py`

**单一职责**：三层合并 MCP server 配置、按开关过滤，为每个 server 启动常驻连接任务、发现其工具并注册进 `ToolsMgr`，关闭时统一断开。

**消费的配置或文件**：
- 三层合并 server（低→高，`start()` `mcp_mgr.py:131-153`）：角色 `mcp_servers.json` → 全局+项目（`config_mgr.load_mcp_servers()`，项目覆盖全局，二者覆盖角色层）。
- `settings.json` 的 `mcp.enabledServers`（非空作白名单）/ `mcp.disabledServers`（始终剔除）——`_apply_server_policy`。

**transport 支持**：`stdio`、`sse`、`http`/`streamable-http`/`streamable_http`。stdio 使用 DataGuard 安全环境；工具名 `mcp__<server>__<tool>` 清洗限长。所有工具无条件注册为 `REVIEW + EXTERNAL`，annotation 不能提升权限。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `start` (async) | — | `None` | 合并/过滤 server，为每个 server 起常驻任务并等待就绪 |
| `stop` (async) | — | `None` | 置停止事件，等待任务清退（超时强制取消） |

**feature 门控**：否。 **reload**：无；`/clear` 由 AgentApp 显式 stop/start 重连。

**持有的关键状态**：`_conns`（server 名→`_ServerConn`）、`_tasks`（常驻连接任务）、`_stop_event`。

MCP 连接配置和授权边界见 [mcp-and-hooks.md](mcp-and-hooks.md)。

---

## MemoryMgr — 项目记忆

`src/mgr/memory_mgr.py`

**单一职责**：加载、构建外部记忆简报、读取与保存项目记忆条目，并提供固定的读写工作流。

**消费的配置或文件**：`{workdir}/.agent/memory/*.md`（`__post_init__` `memory_mgr.py:33-37`）。每文件为 frontmatter（必填 `title`/`description`/`type`/`update_at`）+ body。`type` 合法值：`user`/`feedback`/`project`/`reference`（`MEMORY_TYPES`）。条目按 `(update_at, title)` 降序排序。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `reload` | — | `None` | 重新扫描 memory 目录 |
| `system_guidance` | — | `str` | 简报定位、读取正文和同标题合并覆盖工作流 |
| `build_context` | — | `str` | 按 type 分组的外部记忆简报（上限 `max_prompt_entries`=50），无记忆则空串 |
| `save` | `title`, `description`, `type`, `body` | `str` | 校验后写入 `{slug(title)}.md`，返回 title 或错误信息 |
| `read` | `title` | `str` | 返回指定标题记忆全文，不存在则错误信息 |

**feature 门控**：`memory`（未启用时 `bootstrap` 注入 `None`）。 **reload**：有。

**持有的关键状态**：`memory_dir`、`entries`（title → `MemoryEntry`）、`max_prompt_entries`（缺省 50）。

---

## ContextMgr — 跨 agent 共享上下文

`src/mgr/context_mgr.py`

**单一职责**：维护会话级的「已核实事实」账本，供 `SubAgentMgr.task_delegator` 在委派前注入、委派后记账，让子 agent 不必重新探索别人已经查清的东西。

**为什么需要**：子 agent 的 `history` 从空开始，唯一输入是委派 prompt。此前 3 个并行 `explore` 的发现必须由主 agent 手抄摘要进下一个委派 prompt，抄漏了下游就重新探索一遍。而框架本来就白拿着 `run_result.final_text`——`explore` 的输出格式（结论/证据/可复用资产/影响面/不确定点）已经是一张结构良好的发现卡，自动记账即可，写入侧零 LLM 纪律。

**消费的配置或文件**：`config.yaml` 的 `context` 段（`enabled`/`max_entries`/`max_entry_chars`/`inject_char_budget`/`inject_entry_chars`/`record_types`）；落盘 `{workdir}/.agent/context/{session_id}.md`（append-only，目录 0700 / 文件 0600）。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `bind_session` | `session_id` | `None` | 绑定落盘文件名 |
| `reload` | — | `None` | 清空内存条目与序号，**不删磁盘文件** |
| `note_path` | — | `Path` | 当前会话的落盘路径 |
| `add` | `kind`, `topic`, `content`, `author`, `refs` | `ContextEntry \| None` | 同步纯内存登记（脱敏、截断、超限淘汰） |
| `record` | 同 `add` | `ContextEntry \| None` | `add()` + 追加落盘（`to_thread` + `asyncio.Lock`） |
| `digest` | `char_budget`, `include_ids`, `now` | `str` | 渲染 `<shared_context>` 注入块，倒序、按预算裁剪 |
| `mark_stale` | `paths` | `int` | 把提及这些文件的条目标为可能过时 |
| `entry_ids` | — | `list[str]` | 当前全部条目 id |

**条目类型**（`ContextEntry.kind`）：`delegation`（子 agent 返回报告，框架自动记）、`note`（agent 经 `note_context` 工具显式记）、`decision`（预留给用户决策）。

**三条设计约束**（改这块前必读）：

1. **注入载体只能是子 agent 的首条 user 消息，绝不能进固定 system 或框架 developer。** 账本是动态外部数据；放消息尾部既保持信任边界，也不改变已缓存的固定前缀。
2. **注入点是 `SubAgentMgr.task_delegator` 而非 `ReminderMgr`。** ReminderMgr 的 provider 只收 `(mode, is_subagent)`，拿不到本次委派信息；按委派过滤就得在进程级单例上存槽位，而计划工作流允许同一轮并行委派多个 `explore`，`asyncio.gather` 会互相覆盖——共享消费槽位会产生同样的竞态。
3. **落盘必须由本 Manager 直接写，不能改成 `apply_patch` 工具。** `.agent` 被 `PathResolver` 归为 protected，`.agent/context/**` 因此是 `PathClass.PROTECTED`；而 plan 模式下 `PermissionManager._authorize_plan()` 拒绝通用文件写入，走 `apply_patch` 必被拒——plan 模式恰是本机制最痛的场景。触发它的工具（`task_delegator`、`note_context`）声明 `INTERNAL + plan_safe=True`，与 `save_memory` 同构。

**生命周期语义**：`/clear` 走 `reload()` 清内存、磁盘旧文件保留供排查，新会话按新 `session_id` 另开文件。**resume 不恢复账本**——恢复的历史里主 agent 已带着全部工具结果，账本只服务后续新委派，这是刻意设计不是遗漏。

**与 onboard 角色的关系**：`record_types` 默认白名单与 onboard 的子 agent 类型零交集，因此 onboard 的账本恒空、不产生注入；它继续用自己的 `.agent/onboard/**` 文件约定。

**feature 门控**：`subagent`（未启用时 `bootstrap` 注入 `None`）。 **reload**：有。

---

## PlanMgr 与工具运行时

`PlanMgr` 管理模式切换、当前指令正文与受控计划保存。正文由 PromptMgr 生成，并在下一次 chat 前作为 developer 消息追加；模式切换本身不改历史。`save(content, previous)` 原子写入 `.agent/plans/`，审核状态和路径保存在 `SessionState.plan`。自动批准后 `submit_plan` 通过 `Agent.queue_user_action()` 排队固定执行请求并结束当前 turn，下一轮由 `_on_request_input()` 复用正常 Hook、持久化和 user 消息链路。`agent.mode` 是模式状态权威，授权由 PermissionManager 执行。

文件发现、搜索和读取由 `exec_command` 在真实受限 Shell 中执行；随包 ripgrep 的定位由 `ripgrep.resolve_rg()` 负责。补丁文本计算与提交在 `patch.py`；进程生命周期与工作区读写租约在 `ProcessMgr`；一次输出整理及临时日志归 `ToolOutput`。流程与接口见 [tools.md](tools.md)。

---

## TaskManager — 任务管理

`src/mgr/task_mgr.py`

**单一职责**：会话内任务的 CRUD、依赖关系（双向同步 + 环检测）、文件持久化，并作为提醒源向 `ReminderMgr` 注入任务状态。

**消费的配置或文件**：`tasks_dir`（主 agent 为 `{global_dir}/tasks/{session_id}/`，`agent.py:151-155`；子 agent 传 `None` 为**纯内存模式**）。每 task 一个 `{id}.json`（原子写），`.highwatermark` 记录最高分配 ID 防重用；全部任务 `completed` 时由轮末 `cleanup_if_all_completed()` 清空内存并删除整个目录。`MAX_TASKS = 50`。

**`Task` 字段**（`task_mgr.py:14-36`）：`id`、`subject`、`description`、`active_form`、`status`（`pending`/`in_progress`/`completed`）、`owner`、`blocks`、`blocked_by`、`metadata`。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `create` | `subject`, `description`, `active_form`, `metadata` | `dict` | 创建 pending 任务并持久化（超 `MAX_TASKS` 抛 `ValueError`） |
| `update` | `task_id`, `subject`/`description`/`active_form`/`status`/`owner`/`add_blocks`/`add_blocked_by`/`metadata` | `dict` | 更新字段；`status="deleted"` 级联删除；`owner` 认领校验；依赖双向同步+环检测；`metadata` 合并（None 删键） |
| `list_tasks` | — | `dict` | 任务摘要列表（`blocked_by` 仅列未完成项，过滤 `_internal`） |
| `get_task` | `task_id` | `dict` | 单任务完整详情（不存在抛 `ValueError`） |
| `has_open_items` | — | `bool` | 是否有未完成任务 |
| `system_guidance` | — | `str` | 创建返回 ID、更新/委派传递 ID 及验收状态流转工作流 |
| `get_turn_start_reminder` | `mode, is_subagent` | `str` | 未完成且连续 ≥3 轮未用任务工具时注入任务列表；Plan 模式静默 |
| `notify_tool_round` | `tool_names` | `None` | 含任意 `task_*` 工具则重置计数，否则 +1 |
| `pop_post_round_reminder` | `mode, is_subagent` | `str \| None` | 同条件下提示“更新你的任务列表”；Plan 模式静默 |
| `cleanup_if_all_completed` | — | `bool` | 轮末收尾：全部任务 `completed` 时清空内存列表、删除 tasks 目录并发布空快照（隐藏 UI 面板）；否则返回 `False` 不清理 |

**feature 门控**：`task`（未启用时 `Agent` 中为 `None`）。 **reload**：无（`/clear` 由 `Agent` 侧新建实例处理，非实例 `reload()`）。
**持有的关键状态**：`_tasks`、`_next_id`、`_rounds_without_update`、`_tasks_dir`。

轮末清理时机：`Agent` 每轮结束调用 `cleanup_if_all_completed()`（`agent.py:460-470`），仅当本轮正常结束（turn DONE，无 LLM 错误/退出/斜杠命令）且全部任务 `completed` 时执行；存在未完成任务时不清理，磁盘目录保留，`/resume` 恢复行为不变。清理后 `_next_id` 不重置，新任务 ID 会话内继续单调递增。

---

## SessionMgr — 会话持久化与恢复

`src/mgr/session_mgr.py`

**单一职责**：持久化会话元数据与单一 `SessionState` 快照，支持 `/resume` 恢复。

**消费的配置或文件**：`{global_dir}/sessions/` 下——`{id}.json`（元数据：`workdir`/时间戳/`topic`/`mode`）、`{id}.state.json`（version 2：`records`、`context_ids` 与 attempt 级 `llm_calls`，经 DataGuard 脱敏并原子写）。旧格式不读取、不迁移。

`SessionRecord` 可同时包含模型消息、可见 `ViewPayload`、原始输入和关联 ID。`SessionState` 分别投影 LLM 上下文、TUI 历史与输入回溯，并以 `LLMCallRecord` 持久化每次 provider attempt 的模型、调用者、阶段、结果和原始 usage；compact 只更新上下文投影。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `save_metadata` | `session_id`, `is_new`, `topic`, `mode` | `None` | 原子写/更新元数据（首次写 `created_at`，后续更 `updated_at`） |
| `save_state` | `session_id`, `state` | `None` | DataGuard 脱敏后原子覆写 `.state.json` |
| `load_state` | `session_id` | `SessionState \| None` | 加载并完整校验 version、record 与 context 引用 |
| `list_sessions` | `limit` | `list[dict]` | 按 `updated_at` 降序列出会话元数据 |
| `list_resumable` | `current_session_id`, `limit` | `list[dict]` | 只列有有效 `.state.json` 的非当前会话 |
| `resolve_resume` | `cmd_args`, `current_session_id`, `current_workdir` | `str \| ResumeResult` | 解析 `/resume` 目标（序号或 id 前缀）、加载校验；**拒绝跨 workdir 恢复** |
| `get_metadata` | `session_id` | `dict \| None` | 取指定会话元数据 |

**feature 门控**：否。 **reload**：无。

**持有的关键状态**：`_sessions_dir`、`_workdir`（无可变会话状态，`SessionState` 的生命周期所有者是 app 层）。

会话与 `/resume` 流程见 [agent-runtime.md](agent-runtime.md)。

---

## HooksMgr — 生命周期钩子

`src/mgr/hooks_mgr.py`

**单一职责**：加载并执行 8 类生命周期钩子事件（`PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`Stop`、`SessionStart`、`SessionEnd`、`SubagentStart`、`SubagentStop`），通过 shell 命令 + JSON stdin/stdout 协议交互。

**消费的配置或文件**：两层加载（全部**追加**不覆盖，`_load_hooks` `hooks_mgr.py:88-117`）：全局插件 `hooks/hooks.json` → 全局 `settings.json` → 项目插件 `hooks/hooks.json` → 项目 `.agent/settings.json`。每条 `hooks` 项含 `matcher`、`command`、`timeout`（缺省 60s）、`async`。

**matcher 规则**（`_matches` `hooks_mgr.py:204-214`）：`None`/`*`→匹配全部；`^[\w|]+$`→管道分隔的精确名匹配；其余按正则 `fullmatch`。

**退出码语义**（`_run_hook` `hooks_mgr.py:264-276`）：`0`→解析 stdout（JSON 或纯文本 additional_context）；`2`→`blocked`（stderr 为原因）；其他非零→记录但不阻止。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `run_event` (async) | `event`, `match_value`, `extra`, `session_id`/`agent_id`/`agent_type`, `pre_tool` | `HookRunResult` | 运行匹配的 hook，合并结果（同步串行，`async` 项后台调度） |
| `reload` | — | `None` | 重新加载所有 hooks |

**`HookRunResult` 字段**（`hooks_mgr.py:42-48`）：`additional_context`、`permission_decisions`、`updated_input`、`blocked`、`block_reason`、`errors`。

**feature 门控**：否。 **reload**：有。

**持有的关键状态**：`_hooks`（`HookEntry` 列表）。

hook 协议、JSON 字段与插件 `CLAUDE_PLUGIN_ROOT` 环境变量见 [mcp-and-hooks.md](mcp-and-hooks.md)。

---

## ConfigManager — 配置合并

`src/mgr/config_mgr.py`

**单一职责**：按项目启动信任结果合并 `config.yaml`、`settings.json`、`.env` 和 MCP server 配置，并提供点路径取值。

**消费的配置或文件**：
- `config.yaml` 三层深合并（低→高，`load_config` `config_mgr.py:111-143`）：内置 `builtin_root()/config.yaml` → 全局 `~/.agent/config.yaml` → 项目 `.agent/config.yaml`。
- `.env` 三层：全局 `~/.agent/.env` → 仓库根 `{workdir}/.env` → 项目 `.agent/.env`；`dotenv_values()` 构造私有有效环境且后者覆盖前者，不修改 `os.environ`。`{PROVIDER}_API_KEY`/`{PROVIDER}_API_URL` 覆盖对应 provider 字段。
- `settings.json` 双层深合并；受限模式忽略项目层。
- `mcp_servers.json` 双层（`load_mcp_servers`）：全局 → 项目；受限模式忽略项目层。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `reload` | — | `None` | 重载 config 与 user settings |
| `load_config` | — | `dict` | 三层合并 config + 构造私有环境 + provider env 覆盖 |
| `load_mcp_servers` | — | `dict` | 双层合并 `mcpServers` |
| `load_user_settings` | — | `dict` | 按信任状态深合并 settings |
| `get_config` | `key`（点路径） | `Any` | 取配置值（缺失抛 `KeyError`） |
| `get_config_parts` | `parts`（原样路径段 tuple） | `Any` | 取含点号等动态 mapping key 下的配置值（缺失抛 `KeyError`） |
| `get_user_setting` | `key`（点路径） | `Any` | 取设置值（缺失返回空 dict） |
| `set_project_trusted` | `trusted` | `None` | 更新信任状态并重载配置 |
| `set_config` | `key`, `value`, `scope`（`"global"`/`"project"`） | `None` | 原子写单个点路径到指定配置层（YAML 规范化输出，不保留原注释格式）；写后需 `reload()` 或重启才生效 |
| `set_configs` | `values`, `scope` | `None` | 原子批量写多个点路径到同一配置层 |
| `set_config_parts` / `set_configs_parts` | 原样路径段、值、scope | `None` | 原子写入动态 mapping key；路径段内部的点不会被拆分 |
| `set_global_env` | `values` | `None` | 批量原子写全局 `.env`（`global_dir/.env`）：只改目标变量、保留注释与无关原文，目录 0700/文件 0600，不修改 `os.environ`；写后需 `reload()` 或重启才生效 |
| `has_explicit_provider_config` | — | `bool` | 用户层是否已有显式 Provider 配置（有效环境含内置 `{NAME}_API_KEY`/`{NAME}_API_URL` 键、全局或 trusted 项目非空 `llm_provider`）；首次 Provider 向导据此决定是否跳过 |

**feature 门控**：否。 **reload**：有。

**持有的关键状态**：`_config`、`_user_settings`、`settings_path`、`_lock`（`RLock`，写入线程安全）。

配置键完整清单见 [configuration-reference.md](configuration-reference.md)。

---

## PluginMgr — 插件发现

`src/mgr/plugin_mgr.py`

**单一职责**：三层扫描 `plugins/` 目录发现所有插件（仅发现目录，不解析内部内容），供 `SkillMgr`/`HooksMgr` 按层自取。

**消费的配置或文件**：三层扫描（`_scan` `plugin_mgr.py:55-66`）：角色 `plugins/` → 全局 `~/.agent/plugins/` → 项目 `.agent/plugins/`。**不去重、全部收集**，结果按 角色→全局→项目 排列。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `plugins` | `layer: PluginLayer \| None` | `list[PluginInfo]` | 返回已发现插件，可按层过滤 |
| `reload` | — | `None` | 重新扫描 |

**`PluginInfo` 字段**：`name`（目录名，兼作技能命名空间）、`root`、`layer`（`ROLE`/`GLOBAL`/`PROJECT`）。

**feature 门控**：否。 **reload**：有。

**持有的关键状态**：`_plugins`（`PluginInfo` 列表）。

---

## ReminderMgr — 提醒注入中介

`src/mgr/reminder_mgr.py`

**单一职责**：在 turn start 和 post round 向已注册的提醒源（如 `TaskManager`）收集固定框架提醒并排队。它不构造消息；Agent 在下一次 chat 前与模式指令合并为一条 developer。提醒源不得返回任务标题、工具结果或其他外部内容。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `register` | `provider` | `None` | 注册提醒源（重复注册忽略） |
| `unregister` | `provider` | `None` | 注销（不存在静默跳过） |
| `queue_turn_start` | `mode, is_subagent` | `None` | turn 开始：收集各源 `get_turn_start_reminder(mode, is_subagent)` 并排队 |
| `notify_tool_round` | `tool_names` | `None` | 工具轮后：调各源 `notify_tool_round(tool_names)` |
| `queue_post_round` | `mode, is_subagent` | `None` | POST_ROUND：收集各源 `pop_post_round_reminder(mode, is_subagent)` 并排队 |
| `pop_pending` | — | `list[str]` | chat 前一次取出并清空去重后的待发送提醒 |

**feature 门控**：否（但注册的提醒源受各自 feature 门控）。 **reload**：无（随新 Agent 重建）。

**持有的关键状态**：`_providers`（提醒源列表，按注册顺序迭代）、`_pending`（下一次 chat 的固定框架提醒）。

三处注入时机在状态机中的位置见 [agent-runtime.md](agent-runtime.md)。

---

## features.py / paths.py — 支撑模块

`src/mgr/features.py` — feature 合法名单与解析。`ALL_FEATURES = {task, skill, subagent, file, memory, plan}`；`resolve_features(declared)`：`None`→全开，否则取与合法名单交集（未知名告警丢弃）并校验依赖（`plan` 依赖 `file`，缺则丢 `plan` 并告警）。被 `bootstrap` 与 `Agent` 共同引用。语义详见 [architecture.md](architecture.md#feature-门控)。

`src/mgr/paths.py` — 三层目录路径解析：`builtin_root()`（内置资源根，debug 指向 `src/`）、`common_role_dir()`（`src/roles/common/`）、`global_data_dir()`（`$AGENT_HOME` 或 `~/.agent/`）、`project_data_dir(workdir)`（`{workdir}/.agent/`）、`workdir(override)`（override → `$AGENT_WORKDIR` → cwd）。三层目录体系详见 [architecture.md](architecture.md)。
