# Manager 服务层完整参考

本文档面向开发者与运维者，逐一说明 `src/mgr/` 下的各 Manager 类：单一职责、消费的配置/文件、公共方法、是否受 feature 门控、是否实现 `reload()`、持有的关键状态。所有事实以源码为准，方法名/字段名/配置键均保留英文。

术语沿用四层架构中的约定（详见 [architecture.md](architecture.md)）：**四层架构**（入口组装层 / 应用主循环层 / Agent 状态机层 / Manager 服务层）、**feature 门控**（角色声明启用哪些可插拔 Manager）、**6 步权限检查**（`PermissionManager.check()` 的评估顺序，详见 [permissions.md](permissions.md)）。

## Manager 服务层是什么

Manager 服务层是框架的横切能力层：每个 Manager 类各司其职（角色发现、模型管理、工具执行、权限、上下文压缩、提示词构建、子智能体调度、技能、MCP、记忆、计划、文件、任务、会话、hooks、配置、插件、提醒），彼此低耦合，由上层按需组合。

### 两处装配点

Manager 分两批被构造：

1. **deps 层 Manager**（进程级、跨 agent 共享）——在 `src/app/bootstrap.py` 的 `create_app()` 中手动构造并注入 `AgentDeps` dataclass。包括：`ConfigManager`、`RoleMgr`、`ToolsMgr`、`MemoryMgr`、`PluginMgr`、`HooksMgr`、`PlanMgr`、`McpMgr`、`PermissionManager`、`SessionMgr`、`LLMMgr`。
2. **每 agent 层 Manager**（随 `Agent` 实例创建，主/子 agent 各自独立）——在 `Agent.__post_init__`（`src/agent/agent.py:113-160`）中构造。包括：`CompactMgr`、`FileMgr`、`SkillMgr`、`SubAgentMgr`、`PromptMgr`、`TaskManager`、`ReminderMgr`。子 agent 是共享同一份 `AgentDeps` 的完整 `Agent` 实例，因此复用 deps 层 Manager，但拥有自己的每 agent 层 Manager。

### feature 门控哪些 Manager

角色在 `role.md` frontmatter 声明 `features` 列表，`resolve_features()`（`src/mgr/features.py`）解析为有效启用集（合法名单：`task`、`skill`、`subagent`、`file`、`memory`、`plan`）。据此：

- deps 层：`MemoryMgr`（`memory`）、`PlanMgr`（`plan`）未启用时在 `bootstrap.create_app()` 注入 `None`。
- 每 agent 层：`FileMgr`（`file`）、`SkillMgr`（`skill`）、`SubAgentMgr`（`subagent`）、`TaskManager`（`task`）未启用时在 `Agent.__post_init__` 置 `None`。
- 未启用 feature 的工具由 `ToolsMgr.excluded_tool_names(enabled)` 从 schema 中排除。

feature 语义细节（未声明→全开、未知名告警、`plan` 依赖 `file`）见 [architecture.md](architecture.md#feature-门控) 与 [roles-subagents-skills.md](roles-subagents-skills.md)。

### reload 协议

有会话级可变状态、需在 `/clear` 时重置的 Manager 实现 `reload()` 方法。`/clear`（`src/app/app.py:135-142`）只对**固定的 deps 层 Manager 列表**用 `hasattr(mgr, "reload")` 发现并调用：`memory_mgr`、`tools_mgr`、`permission_mgr`、`config_mgr`、`plugin_mgr`、`hooks_mgr`、`plan_mgr`、`ui`（其中 `tools_mgr`、`ui` 无 `reload()`，被 `hasattr` 跳过）。每 agent 层 Manager（`CompactMgr`/`PromptMgr`/`SkillMgr`/`SubAgentMgr`/`TaskManager`/`ReminderMgr`/`FileMgr`）在 `/clear` 时随新 `Agent` 实例整体重建，不走 `reload()`。

> 注意：`McpMgr` **无** `reload()`——`/clear` 不重连 MCP server，故 `mcp_servers.json` 的编辑需重启进程才生效。详见 [mcp-and-hooks.md](mcp-and-hooks.md)。

## Manager 一览表

| Manager | 职责一句话 | feature 门控 | reload |
|---|---|---|---|
| `RoleMgr` (`role_mgr.py`) | 三层发现并激活角色，暴露角色资产路径 | 否 | 无 |
| `LLMMgr` (`llm_mgr.py`) | 按模型名/别名返回可用的 LLMProvider | 否 | 无 |
| `ToolsMgr` (`tools_mgr.py`) | 工具注册、执行、分页结果存储 | 否 | 无 |
| `PermissionManager` (`permission_mgr.py`) | 6 步权限检查、规则引擎 | 否 | 有 |
| `CompactMgr` (`compact_mgr.py`) | 上下文压缩与 transcript 落盘 | 否 | 无 |
| `PromptMgr` (`prompt_mgr.py`) | 分层拼装系统提示词 | 否 | 无（缓存可 invalidate） |
| `SubAgentMgr` (`subagent_mgr.py`) | 四层扫描子 agent，调度委派 | `subagent` | 无 |
| `SkillMgr` (`skill_mgr.py`) | 多层扫描技能，按需注入全文 | `skill` | 无 |
| `McpMgr` (`mcp_mgr.py`) | 连接 MCP server、注册其工具 | 否 | 无（编辑需重启） |
| `MemoryMgr` (`memory_mgr.py`) | 项目记忆的加载/构建/读写 | `memory` | 有 |
| `PlanMgr` (`plan_mgr.py`) | 计划模式切换与 plan 指令注入 | `plan`（依赖 `file`） | 有 |
| `FileMgr` (`file_mgr.py`) | 工作区文件读写/搜索（同步阻塞） | `file` | 无 |
| `TaskManager` (`task_mgr.py`) | 任务 CRUD、依赖、持久化、提醒 | `task` | 无（`/clear` 用 `clear_dir`） |
| `SessionMgr` (`session_mgr.py`) | 会话元数据/历史持久化与恢复 | 否 | 无 |
| `HooksMgr` (`hooks_mgr.py`) | 8 类生命周期钩子的加载与执行 | 否 | 有 |
| `ConfigManager` (`config_mgr.py`) | 三层配置/settings/.env 合并 | 否 | 有 |
| `PluginMgr` (`plugin_mgr.py`) | 三层扫描插件目录 | 否 | 有 |
| `ReminderMgr` (`reminder_mgr.py`) | 中介，统一收集各源的提醒注入 | 否 | 无 |
| `features.py` / `paths.py` | feature 名单解析 / 三层目录路径 | — | — |

---

## RoleMgr — 角色发现与激活

`src/mgr/role_mgr.py`

**单一职责**：三层发现所有已安装角色，激活 `config.yaml` 的 `role` 键指定的角色，并暴露该角色的资产路径。角色是框架的顶层组织单位。

**消费的配置或文件**：
- `config.yaml` 的 `role` 键（`_resolve()`，`role_mgr.py:228`）——缺省或角色不存在时回退 `_DEFAULT_ROLE`（`"coding"`，`role_mgr.py:28`）。
- 三层扫描目录（低→高优先级，同名后者覆盖，`_discover()` `role_mgr.py:195-216`）：内置 `builtin_root()/roles` → 全局 `~/.agent/roles` → 项目 `.agent/roles`。**跳过 `common/` 目录**（`role_mgr.py:211`，它是共享资源而非角色）。
- 角色目录内：`role.md`（frontmatter + body）、`AGENT.md`、`agents/`、`skills/`、`plugins/`、`mcp_servers.json`。

**模块级函数**（RoleMgr/SubAgentMgr 共用）：

| 函数 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `parse_frontmatter` | `text: str` | `tuple[dict, str]` | 从 `.md` 文本分离 YAML frontmatter 与 body |
| `extract_manifest` | `meta: dict`, `path: Path`, `prompt`, `id_field`, `default_id`, `default_description` | `AgentManifest` | 从 frontmatter+body 构造 `AgentManifest` |

**`AgentManifest` 数据类字段**（`role_mgr.py:150-168`）：`agent_type`（角色固定 `"main"`）、`description`、`path`、`prompt`、`tools`、`memory`、`model`、`permission_mode`、`enable_thinking`、`reasoning_effort`、`features`。

**公共方法/属性**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `active` (property) | — | `bool` | 是否有已激活角色 |
| `manifest` (property) | — | `AgentManifest \| None` | 当前角色的 manifest |
| `role_name` (property) | — | `str \| None` | 当前角色名（文件夹名） |
| `agents_dir` | — | `Path \| None` | 角色 `agents/` 目录（存在时） |
| `skills_dir` | — | `Path \| None` | 角色 `skills/` 目录 |
| `plugins_dir` | — | `Path \| None` | 角色 `plugins/` 目录 |
| `agent_md_path` | — | `Path \| None` | 角色 `AGENT.md` 文件 |
| `mcp_servers_path` | — | `Path \| None` | 角色 `mcp_servers.json` 文件 |
| `common_dir` | — | `Path \| None` | 共享资源目录 `roles/common/` |
| `common_agents_dir` | — | `Path \| None` | 共享 `agents/` 目录 |
| `common_skills_dir` | — | `Path \| None` | 共享 `skills/` 目录 |
| `common_agent_md_path` | — | `Path \| None` | 共享 `AGENT.md` 文件 |

**feature 门控**：否。 **reload**：无（角色在启动时一次性发现与解析）。

**持有的关键状态**：`_role_path`（激活角色目录）、`_manifest`（激活角色 manifest）、`_all_roles`（角色名 → 目录）。

角色系统整体见 [roles-subagents-skills.md](roles-subagents-skills.md)。

---

## LLMMgr — 模型管理

`src/mgr/llm_mgr.py`

**单一职责**：把模型名或别名解析为真实模型 ID，并返回对应的、按模型缓存的 `LLMProvider` 实例。

**消费的配置**：
- `llm`（`__post_init__`，`llm_mgr.py:59-119`）：顶层与 `retry` 必须是 mapping；`concurrency` 必须是非 bool 且 `>= 1` 的整数；`timeout_seconds`、`retry.base_delay_seconds`、`retry.max_delay_seconds` 必须是非 bool 的有限正数；`retry.max_attempts` 必须是非 bool 且 `>= 1` 的整数，最大延迟不得小于基础延迟。`default` 必填，`best`/`fast` 缺省回退 `default`。
- `tool.page_token_rate`（`llm_mgr.py:118`）——传给 provider 用于分页预算。
- `llm_provider.*`（`load_models`/`_create_provider`）：顶层与每个 provider 项必须是 mapping，provider 名和 `base_url` 必须是非空字符串；`models` 必须是仅含非空字符串的列表，并按首次出现顺序去重（`llm_mgr.py:407-478`）。其余字段包括 `api_key`、`reasoning_effort`、`preserve_thinking`、`context_limit`。
- **Claude Code 兼容别名**（`_CLAUDECODE_ALIASES` `llm_mgr.py:17-21`）：`opus`→`best`、`sonnet`→`default`、`haiku`→`fast`。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `load_models` (async) | — | `None` | 并发发现模型；单个 provider 失败时记录 `provider_errors`，仅在静态 `models` 非空时回退；跨 provider 的同名模型归属冲突会抛配置错误 |
| `resolve_model` | `model: str \| None` | `str` | 解析顺序：`None`→`"default"`→CC 别名→config 别名→精确匹配→子串模糊匹配（多个取最短）→回退 `default` |
| `ensure_default_available` | — | `None` | 启动前精确校验配置的默认模型；不可用时携安全化的 provider 发现错误抛 `ModelUnavailableError`，不切换到其他 provider |
| `get` | `model: str \| None` | `LLMProvider` | 解析并返回缓存的 provider 实例（未知模型抛 `ValueError`） |
| `list_models` | — | `list[str]` | 已加载可用模型名（排序） |

**feature 门控**：否。 **reload**：无。

**模型发现规则**（`llm_mgr.py:121-223`）：每个 provider 独立调用 `list_models()`；发现响应同样执行严格模型列表校验。失败信息经统一分类后写入 `provider_errors`，静态 `models` 为空时该 provider 不注册模型。模型 ID 只能归属一个 provider，冲突会终止启动。

**持有的关键状态**：`_model_to_provider`（模型→provider 名）、`_cache`（模型→provider 实例）、`provider_errors`（provider→安全结构化发现错误）、`_default_concurrency`、`_timeout_seconds`、`_retry_config`、`_page_token_rate`、`_user_agent`。

模型解析、Provider 抽象与流式细节见 [llm.md](llm.md)。配置键见 [configuration-reference.md](configuration-reference.md)。

---

## ToolsMgr — 工具注册与执行

`src/mgr/tools_mgr.py`

**单一职责**：工具注册表与执行引擎——注册工具、按 feature/权限过滤 schema、执行工具（串联 hook 与权限检查）、存储超长结果分页。

**消费的配置或文件**：构造时（`load_registered=True`）从 `src/tools/decorator.py` 的全局 `_registry` 载入所有 `@tool` 注册的工具（`tools_mgr.py:40-42`）；MCP 工具由 `McpMgr` 额外 `register()`。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `register` | `tool: ToolEntry` | `None` | 注册工具（重名跳过并告警） |
| `get` | `name: str` | `ToolEntry \| None` | 按名取工具 |
| `has` | `name: str` | `bool` | 是否已注册 |
| `list_entries` | — | `list[ToolEntry]` | 全部工具（只读优先排序） |
| `all_tool_names` | — | `set[str]` | 全部工具名 |
| `excluded_tool_names` | `enabled: set[str]` | `set[str]` | 因所属 feature 未启用而应排除的工具名 |
| `resolve_subagent_tools` | `tool_names: set[str] \| None` | `set[str]` | 在声明集上注入 `subagent=True`、排除 `subagent=False` |
| `get_schemas` | `tool_names`, `permission_mgr`, `mode` | `list[ToolDict]` | OpenAI function-calling schema；传 `permission_mgr`+`mode` 时按 `is_tool_visible` 过滤 |
| `get_page` | `tool_call_id: str`, `page: int` | `str` | 返回缓存分页结果的指定页 |
| `execute` (async) | `tool_name`, `arguments`, `current_tool_call_id`, `deps`, `agent` | `str` | 执行工具全流程（见下） |

**`execute()` 完整流程**（`tools_mgr.py:243-356`）：
1. PreToolUse hook（`blocked`/`deny` 决策则直接拒绝；`updated_input` 覆盖参数）；
2. 权限检查——取 `agent.permission_mode`（缺省 `permission_mgr.default_mode`），`check()` 返回 `deny` 直接拒并 `notify_decision`，`ask`（或 hook 要求 ask）走 `resolve_ask` 弹窗，否则 `notify_decision`；
3. `ToolCallStarted` 事件；
4. 调用工具本体；
5. PostToolUse hook（`blocked` 覆盖为拒绝、`additional_context` 追加到结果）；
6. `ToolCallCompleted` 事件（附 `status`、耗时、`result_preview`）；
7. `_truncate` 分页：结果 token 超 `llm.page_token_budget` 时切页存入 `_result_store`，返回首页提示（`tool.raw_output` 为真则跳过分页）。

**feature 门控**：否（但 `excluded_tool_names`/`resolve_subagent_tools` 是 feature 门控的执行点）。 **reload**：无。

**持有的关键状态**：`_tools`（工具名→`ToolEntry`）、`_result_store`（`tool_call_id`→分页列表）。

工具体系与内置工具见 [tools.md](tools.md)。

---

## PermissionManager — 权限模式与规则引擎

`src/mgr/permission_mgr.py`

**单一职责**：对每次工具调用做**6 步权限检查**，返回 `allow`/`deny`/`ask`/`auto_allow`；管理规则集与会话级放行、驱动 UI 确认弹窗。权限模式按 agent 独立（模式作为 `check()` 参数传入，此处只保留全局规则、`session_allow` 与不可变 `default_mode`）。

**消费的配置或文件**：
- `settings.json` 的 `permissions`（`_load_config` `permission_mgr.py:312-329`）：`defaultMode`、`allow`、`deny`、`ask` 列表。
- 构造参数 `role_default_mode`（`bootstrap.create_app()` 传入 `role_mgr.manifest.permission_mode`）：`_load_config` 末尾最后套用，使 `default_mode` 解析优先级为 role.md `permissionMode` → `settings.json` `defaultMode` → 内置 `DEFAULT_MODE`（详见 [permissions.md](permissions.md)）。
- `mcp_servers.json` 各 server 的 `permissions` 块（`_load_mcp_server_rules` `permission_mgr.py:331-359`，经 `McpMgr.server_permissions()` 拉取）——最低优先级层。
- 工具自身的权限元数据（`_load_tool_metadata`）：`kind`、`tips`、`check_permissions`、`specifier_arg`、`mcp_server`。

**公共方法**（签名概览，评估细节见 [permissions.md](permissions.md)）：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `check` | `tool_name`, `tool_input`, `mode` | `PermissionDecision` | 6 步评估：deny→ask→工具自检→allow(含 session_allow)→处理穿透 ask→mcp 层规则→bypass→模式默认 |
| `is_tool_visible` | `tool: ToolEntry`, `mode` | `bool` | 该工具在此模式下是否暴露给 LLM（plan_visible/readonly/普通规则） |
| `get_schemas` | — | — | *（无此方法；schema 过滤由 `ToolsMgr.get_schemas` 调用 `is_tool_visible` 完成）* |
| `resolve_ask` (async) | `tool_name`, `tool_input`, `deps` | `PermissionDecision` | 弹窗确认；支持 session/always 与 MCP“信任整个 server”（写 `session_allow`，always 落 `settings.json`） |
| `notify_decision` (async) | `tool_name`, `tool_input`, `deps`, `decision` | `None` | 向 UI 通知非 allow 决策 |
| `reload` | — | `None` | 清空规则、`session_allow`、重置 `default_mode`，重载配置 |

> 注：文档任务参考中提到的 `get_schemas()` 实际不在 `PermissionManager` 上——schema 可见性过滤是 `ToolsMgr.get_schemas` 传入 `permission_mgr`+`mode` 后回调 `is_tool_visible` 实现的。以源码为准。

**权限模式常量**（`permission_mgr.py:94-117`）：`DEFAULT_MODE`、`ACCEPT_EDITS_MODE`、`PLAN_MODE`、`BYPASS_MODE`、`AUTO_MODE`、`DONT_ASK_MODE`。Shift+Tab 轮转为 `CAROUSEL_MODES`（default/acceptEdits/plan），`/mode` 菜单为 `MENU_MODES`（全部 6 种）。

**feature 门控**：否。 **reload**：有（`/clear` 调用；注意会同时清空并重载 `settings.json` 权限，但不重连 MCP，故 mcp 层规则的编辑需重启）。

**持有的关键状态**：`deny_rules`/`ask_rules`/`allow_rules`/`session_allow`、`mcp_deny_rules`/`mcp_ask_rules`/`mcp_allow_rules`、`default_mode`、工具元数据映射（`_tool_kinds`/`_tool_tips`/`_check_permissions_fns`/`_specifier_args`/`_mcp_servers`）。

完整 6 步评估顺序、复合命令逐段匹配、规则通配语义见 [permissions.md](permissions.md)。

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
| `split_history_for_compaction` | `messages: list` | `CompactionPartition` | 切分为必须原文保留、待摘要、预算内近期原文；assistant 及紧随其后的 tool 结果不可拆散 |
| `summarize_history` (async) | `preserved_messages`, `messages_to_summarize`, `recent_messages`, `focus` | `str` | 完整输入不超过上下文 95% 时一次摘要；超限则按原子块滚动摘要，单块仍超限时无损分页 |
| `build_compacted_context_prefix` | `preserved_messages`, `summary`, `recent_files_hint` | `str` | 拼装“原始需求 + 摘要 + 近期文件提示”前缀 |
| `compact_history` (async) | `messages`, `focus` | `CompactResult` | 端到端压缩；返回消息、transcript、摘要消息数及摘要正文，无可摘要消息或空摘要时保留原历史 |

**feature 门控**：否。 **reload**：无（随新 Agent 重建）。

最近 N 个用户轮次只是优先保留范围：如果其完整原文超过硬预算，切分点会继续向后移动；被移出近期原文的首条用户消息和当前用户消息仍以原文进入压缩前缀，其余旧消息进入摘要。序列化、token 计算、分页与 transcript 文件 I/O 均卸载到线程，且不做字符截断。

**持有的关键状态**：`recent_files`（最近文件路径，上限 5）、`has_compacted`（是否已完成过有效压缩）。

压缩在状态机的 `CHECK_COMPACT`/`COMPACT`/`CONTEXT_OVERFLOW` 阶段驱动，见 [agent-runtime.md](agent-runtime.md)。

---

## PromptMgr — 系统提示词构建

`src/mgr/prompt_mgr.py`

**单一职责**：分层拼装系统提示词的静态前缀并缓存，构建时附上当前日期。PromptMgr 负责段顺序，各可插拔段内容由对应 Manager 提供（Manager 缺席则该段自动省略）。

**段顺序**（`_build_static_prefix` `prompt_mgr.py:105-151`）：
1. **核心身份**（primacy）——`role_prompt` 非空时用之，否则默认身份（`_build_core`）；
2. **行为准则**——`AGENT.md` 四层叠加：共享 `roles/common/AGENT.md` → 角色 `AGENT.md` → 全局 `~/.agent/AGENT.md` → 项目 `AGENT.md`（`_build_agent_md`）；
3. **运行环境**——平台/模型/工作目录（`_build_environment`）；
4. **任务管理指导**——`TaskManager.describe(is_subagent)`（仅 task feature）；
5. **项目记忆**——`MemoryMgr.build_prompt()`（仅主 agent 视角，`agent.memory == "project"` 时，`_build_memory_context`）；
6. **会话上下文**——`deps.session_context`（`_build_session_context`）；
7. **可用子智能体 / 可用技能**（recency，**仅主 agent**）——`SubAgentMgr.prompt_section()` / `SkillMgr.prompt_section()`。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `invalidate_cache` | — | `None` | 清除 `_static_prefix` 缓存，下次 `build()` 重建 |
| `build` | — | `list` | 返回 `[{"role":"system","content":静态前缀 + 当前日期}]` |

**feature 门控**：否（但各段内容按对应 feature Manager 是否存在动态出现）。 **reload**：无（提供 `invalidate_cache`；随新 Agent 重建）。

**持有的关键状态**：`_static_prefix`（缓存的静态前缀）。

---

## SubAgentMgr — 子智能体调度

`src/mgr/subagent_mgr.py`

**单一职责**：四层扫描子 agent 定义，暴露列表提示词段，并通过 `task_delegator` 委派任务给子智能体。

**消费的配置或文件**：四层扫描 `*.md`（低→高优先级，同名后者覆盖，`_load_all` `subagent_mgr.py:36-67`）：共享 `roles/common/agents/` → 角色 `agents/` → 全局 `~/.agent/agents/` → 项目 `.agent/agents/`。每个 `.md` 经 `parse_frontmatter`+`extract_manifest` 解析为 `AgentManifest`。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `describe` | — | `str \| None` | 子 agent 列表（`- type: description`，按 type 排序） |
| `prompt_section` | — | `str` | `# 可用子智能体` 段，无则空串 |
| `task_delegator` (async) | `agent_type`, `prompt`, `parent_agent`, `task_id` | `str` | 委派并返回子 agent 结果文本 |

**`task_delegator` 关键行为**（`subagent_mgr.py:84-211`）：
- 未知 `agent_type` 返回错误并列出已知；
- 有 `task_id` 时委派前将任务置 `in_progress` 并设 `owner`；异常退出或 `RunResult.llm_error` 非空时均回滚为无 owner 的 `pending`，其余正常返回**不**自动标 `completed`，留给主 agent 评估；
- 工具集经 `tools_mgr.resolve_subagent_tools(manifest.tools)` 解析；`model == "inherit"` 继承父 agent 已解析的真实模型 ID；`enable_thinking`/`reasoning_effort`/`features` 未声明时继承父 agent；
- 用 `Agent.from_manifest(is_subagent=True, ...)` 构造子 agent 实例；
- 触发 `SubagentStart`/`SubagentStop` hook 与 `SubagentLifecycle`（start/end）事件（异常/取消也发 end）；`SubagentStop` hook 的 `blocked`/`additional_context` 可覆盖或追加结果。

**feature 门控**：`subagent`（未启用时 `Agent` 中为 `None`）。 **reload**：无（随新 Agent 重建）。

**持有的关键状态**：`_documents`（`agent_type` → `AgentManifest`）。

子 agent 定义格式、feature 继承、权限模式固定见 [roles-subagents-skills.md](roles-subagents-skills.md)。

---

## SkillMgr — 技能加载

`src/mgr/skill_mgr.py`

**单一职责**：多层扫描 `SKILL.md` 并以 `namespace:name` 注册，暴露技能列表提示词段，按需返回技能全文（含 `<skill-file>` 引用）供 `load_skill` 工具注入。

**消费的配置或文件**：多层扫描（低→高优先级，同名后者覆盖，`_load_all` `skill_mgr.py:44-88`）：共享 `roles/common/skills/` → 角色 `skills/`（命名空间为角色名）→ 全局插件 `plugins/*`（命名空间为插件名）→ 全局 `~/.agent/skills`（`user`）→ 项目插件 → 项目 `.agent/skills`（`user`）。每目录递归 `rglob("SKILL.md")`。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `describe` | — | `str \| None` | 技能列表（`- [name]: description`，排序） |
| `prompt_section` | — | `str` | `# 可用技能` 段（含使用流程说明），无则空串 |
| `check_skill` | `name: str` | `bool` | 技能是否存在 |
| `load_full_text` | `name: str` | `str` | 技能全文（`<skill>` 包裹 body + 同目录文件的 `<skill-file>` 引用），不存在则错误信息 |

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
- 每个 server 的 `permissions` 块被抽取存入 `_server_permissions`，供 `PermissionManager` 拉取。

**transport 支持**（`_open_session` `mcp_mgr.py:256-297`）：`stdio`（默认，叠加默认环境含 PATH）、`sse`、`http`/`streamable-http`/`streamable_http`（httpx AsyncClient）。工具命名 `mcp__<server>__<tool>` 经 `_safe_tool_name` 清洗为 `[A-Za-z0-9_-]` 并截断到 64；`readOnlyHint` 为真→`kind=readonly`（全模式可见、自动放行），否则保守按非只读处理。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `start` (async) | — | `None` | 合并/过滤 server，抽取 permissions，为每个 server 起常驻任务并等待就绪 |
| `stop` (async) | — | `None` | 置停止事件，等待任务清退（超时强制取消） |
| `server_permissions` | — | `dict[str, dict]` | 各生效 server 声明的只读 permissions 块 |

**feature 门控**：否。 **reload**：**无**——`/clear` 不重连，故 `mcp_servers.json` 与其 per-server 权限块的编辑需重启进程生效。

**持有的关键状态**：`_conns`（server 名→`_ServerConn`）、`_tasks`（常驻连接任务）、`_stop_event`、`_server_permissions`。

MCP 连接配置格式、per-server 权限分层见 [mcp-and-hooks.md](mcp-and-hooks.md)。

---

## MemoryMgr — 项目记忆

`src/mgr/memory_mgr.py`

**单一职责**：加载、构建提示词、读取与保存项目记忆条目。

**消费的配置或文件**：`{workdir}/.agent/memory/*.md`（`__post_init__` `memory_mgr.py:33-37`）。每文件为 frontmatter（必填 `title`/`description`/`type`/`update_at`）+ body。`type` 合法值：`user`/`feedback`/`project`/`reference`（`MEMORY_TYPES`）。条目按 `(update_at, title)` 降序排序。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `reload` | — | `None` | 重新扫描 memory 目录 |
| `build_prompt` | — | `str` | `# 项目记忆` 段（使用指引 + 按 type 分组的简报，上限 `max_prompt_entries`=50），无记忆则空串 |
| `save` | `title`, `description`, `type`, `body` | `str` | 校验后写入 `{slug(title)}.md`，返回 title 或错误信息 |
| `read` | `title` | `str` | 返回指定标题记忆全文，不存在则错误信息 |

**feature 门控**：`memory`（未启用时 `bootstrap` 注入 `None`）。 **reload**：有。

**持有的关键状态**：`memory_dir`、`entries`（title → `MemoryEntry`）、`max_prompt_entries`（缺省 50）。

---

## PlanMgr — 计划模式与指令注入

`src/mgr/plan_mgr.py`

**单一职责**：管理计划模式的进入/退出（切换 agent 的 `permission_mode`）、生成计划文件路径、判断路径是否在计划目录内，并作为提醒源向 `ReminderMgr` 注入 plan 指令。

**消费的配置或文件**：计划文件目录 `{workdir}/.agent/plans/`（`PLANS_SUBDIR`）；文件名由 LLM 调用 `plan_write_file` 时命名。周期性提醒间隔 `_REMINDER_INTERVAL = 5` 轮。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `enter_mode` | `agent`, `reminder_mgr` | `bool` | 切 agent 到 PLAN_MODE，记录进入前模式，注册提醒源；已在 plan 返回 False |
| `exit_mode` | `agent`, `reminder_mgr` | `bool` | 恢复进入前模式，置退出提醒标志（保留一轮后注销）；不在 plan 返回 False |
| `resolve_plan_path` | `name: str` | `str` | 生成 `{plan_dir}/{name}.md` 绝对路径（首次写入调用） |
| `get_plan_dir` | — | `str` | 计划目录绝对路径（供权限检查） |
| `is_plan_file` | `file_path: str` | `bool` | 路径是否在计划目录下 |
| `get_turn_start_reminder` | `mode` | `str` | turn 开始时注入 plan 指令，或退出后一次性退出提醒 |
| `notify_tool_round` | `tool_names` | `None` | 累计距上次注入的轮数 |
| `pop_post_round_reminder` | `mode` | `str \| None` | POST_ROUND 注入：轮中进入 plan 或超阈值时返回指令 |
| `reload` | — | `None` | 重置会话级注入状态 |

**feature 门控**：`plan`（依赖 `file`；未启用时 `bootstrap` 注入 `None`）。 **reload**：有。

**持有的关键状态**：`_plan_dir`、`_full_instructions_sent`、`_pending_injection`、`_rounds_since_injection`、`_need_exit_reminder`、`_has_exited_plan`。

计划工作流与权限模式交互见 [permissions.md](permissions.md) 与 [agent-runtime.md](agent-runtime.md)。

---

## FileMgr — 工作区文件操作

`src/mgr/file_mgr.py`

**单一职责**：工作区文件/目录的读写、编辑、查找与搜索。**全部公共方法为普通 `def`（阻塞型）**——内部做同步文件 I/O，卸载到线程由 `@tool` 装饰器统一处理（见 CLAUDE.md 异步/阻塞契约）。仅做路径解析，不做访问控制（访问控制在权限层）。

**消费的配置或文件**：`workdir` 下的文件；`grep`/`glob` 通过 `rg`（ripgrep）子进程实现，二进制随 `ripgrep` 包（wheel）安装到环境 `bin` 目录、无需主机预装（缺失时回退 PATH 中的 `rg`），原生遵守 `.gitignore`、排除隐藏文件，`grep` 输出超过 200 行截断。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `safe_path` | `path_str` | `Path` | 解析为绝对路径 |
| `read_file` | `path`, `start_line`, `end_line` | `str` | 带行号读取（可指定行范围） |
| `write_file` | `path`, `content`, `append`, `chunk_index`, `total_chunks` | `str` | 写入/追加/分块写入 |
| `edit_file_lines` | `path`, `start_line`, `new_text`, `end_line` | `str` | 按行号替换/插入/删除 |
| `replace_all_in_file` | `path`, `old_text`, `new_text` | `str` | 全文替换所有匹配 |
| `get_file_info` | `path` | `str` | 文件/目录元信息 |
| `list_directory` | `path`, `max_depth` | `str` | 树状列目录 |
| `create_directory` | `path` | `str` | 创建目录（含父级） |
| `move_file` | `source`, `destination` | `str` | 移动/重命名 |
| `glob` | `pattern`, `path` | `str` | rg 按 glob 查找文件（遵守 .gitignore，不含目录） |
| `grep` | `pattern`, `path` | `str` | rg 正则搜索文件内容，返回文件、行号、匹配行 |

**feature 门控**：`file`（未启用时 `Agent` 中为 `None`）。 **reload**：无（无状态）。

**持有的关键状态**：`workdir`、`deps`（无可变会话状态）。

---

## TaskManager — 任务管理

`src/mgr/task_mgr.py`

**单一职责**：会话内任务的 CRUD、依赖关系（双向同步 + 环检测）、文件持久化，并作为提醒源向 `ReminderMgr` 注入任务状态。

**消费的配置或文件**：`tasks_dir`（主 agent 为 `{global_dir}/tasks/{session_id}/`，`agent.py:151-155`；子 agent 传 `None` 为**纯内存模式**）。每 task 一个 `{id}.json`（原子写），`.highwatermark` 记录最高分配 ID 防重用；所有任务 `completed` 后 `_auto_cleanup` 删除整个目录。`MAX_TASKS = 50`。

**`Task` 字段**（`task_mgr.py:14-36`）：`id`、`subject`、`description`、`active_form`、`status`（`pending`/`in_progress`/`completed`）、`owner`、`blocks`、`blocked_by`、`metadata`。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `create` | `subject`, `description`, `active_form`, `metadata` | `dict` | 创建 pending 任务并持久化（超 `MAX_TASKS` 抛 `ValueError`） |
| `update` | `task_id`, `subject`/`description`/`active_form`/`status`/`owner`/`add_blocks`/`add_blocked_by`/`metadata` | `dict` | 更新字段；`status="deleted"` 级联删除；`owner` 认领校验；依赖双向同步+环检测；`metadata` 合并（None 删键） |
| `list_tasks` | — | `dict` | 任务摘要列表（`blocked_by` 仅列未完成项，过滤 `_internal`） |
| `get_task` | `task_id` | `dict` | 单任务完整详情（不存在抛 `ValueError`） |
| `has_open_items` | — | `bool` | 是否有未完成任务 |
| `describe` | `is_subagent` | `str` | 任务管理提示词（主/子 agent 各返回独立文本） |
| `get_turn_start_reminder` | `mode` | `str` | 未完成且连续 ≥3 轮未用任务工具时注入任务列表 |
| `notify_tool_round` | `tool_names` | `None` | 含任意 `task_*` 工具则重置计数，否则 +1 |
| `pop_post_round_reminder` | `mode` | `str \| None` | 同条件下提示“更新你的任务列表” |
| `clear_dir` (static) | `tasks_dir` | `None` | 删除指定 tasks 目录（`/clear` 时调用） |

**feature 门控**：`task`（未启用时 `Agent` 中为 `None`）。 **reload**：无（`/clear` 由 `Agent` 侧新建实例 + 静态 `clear_dir` 处理，非实例 `reload()`）。

**持有的关键状态**：`_tasks`、`_next_id`、`_rounds_without_update`、`_tasks_dir`。

---

## SessionMgr — 会话持久化与恢复

`src/mgr/session_mgr.py`

**单一职责**：持久化会话元数据与对话历史，支持 `/resume` 恢复。

**消费的配置或文件**：`{global_dir}/sessions/` 下——`{id}.json`（元数据：`workdir`/时间戳/`topic`/`permission_mode`/`pre_plan_mode`）、`{id}.hist.json`（历史快照，覆写式原子写）。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `save_metadata` | `session_id`, `is_new`, `topic`, `permission_mode`, `pre_plan_mode` | `None` | 写/更新元数据（首次写 `created_at`，后续更 `updated_at`） |
| `save_history` | `session_id`, `messages` | `None` | 原子覆写历史快照（正确处理 compact 后缩短） |
| `list_sessions` | `limit` | `list[dict]` | 按 `updated_at` 降序列出会话元数据 |
| `list_resumable` | `current_session_id`, `limit` | `list[dict]` | 同上但排除当前会话 |
| `load_history` | `session_id` | `list[dict]` | 加载历史，不存在则空列表 |
| `resolve_resume` | `cmd_args`, `current_session_id`, `current_workdir` | `str \| ResumeResult` | 解析 `/resume` 目标（序号或 id 前缀）、加载校验；**拒绝跨 workdir 恢复** |
| `get_metadata` | `session_id` | `dict \| None` | 取指定会话元数据 |

**feature 门控**：否。 **reload**：无。

**持有的关键状态**：`_sessions_dir`、`_workdir`（无可变会话状态，历史/元数据落盘）。

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

**单一职责**：三层深度合并 `config.yaml`、双层合并 `settings.json`、加载三层 `.env`，并提供点路径取值与项目级 settings 原子写入。

**消费的配置或文件**：
- `config.yaml` 三层深合并（低→高，`load_config` `config_mgr.py:111-143`）：内置 `builtin_root()/config.yaml` → 全局 `~/.agent/config.yaml` → 项目 `.agent/config.yaml`。
- `.env` 三层（`override=True` 后者覆盖）：全局 `~/.agent/.env` → 仓库根 `{workdir}/.env` → 项目 `.agent/.env`；环境变量 `{PROVIDER}_API_KEY`/`{PROVIDER}_API_URL` 覆盖对应 provider 的 `api_key`/`base_url`。
- `settings.json` 双层（`load_user_settings` `config_mgr.py:162-198`）：`permissions.allow`/`permissions.deny` 列表合并去重，其余键项目覆盖全局。
- `mcp_servers.json` 双层（`load_mcp_servers`）：全局 → 项目（顶层 `mcpServers`，项目覆盖全局）。**写入位置固定为项目级 `.agent/settings.json`**。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `reload` | — | `None` | 重载 config 与 user settings |
| `load_config` | — | `dict` | 三层合并 config + 加载 .env + provider env 覆盖 |
| `load_mcp_servers` | — | `dict` | 双层合并 `mcpServers` |
| `load_user_settings` | — | `dict` | 双层合并 settings（allow/deny 并集去重） |
| `get_config` | `key`（点路径） | `Any` | 取配置值（缺失抛 `KeyError`） |
| `get_user_setting` | `key`（点路径） | `Any` | 取设置值（缺失返回空 dict） |
| `append_permission_list` | `list_name`, `rule_text` | `None` | 向项目 settings 的 allow/deny 追加规则（原子写、去重） |

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

**单一职责**：作为中介，在 agent 运行循环的三处时机统一向已注册的提醒源（`PlanMgr`、`TaskManager`）收集提醒，用 `<reminder>` 标签包装后交给状态机注入。提醒源通过 duck typing 识别（实现哪个接口方法就在对应时机被调用）。

**公共方法**：

| 方法 | 关键参数 | 返回 | 作用 |
|---|---|---|---|
| `register` | `provider` | `None` | 注册提醒源（重复注册忽略） |
| `unregister` | `provider` | `None` | 注销（不存在静默跳过） |
| `build_turn_start_instructions` | `mode` | `str` | turn 开始：收集各源 `get_turn_start_reminder(mode)`，`<reminder>` 包装拼接（prepend 用户输入） |
| `notify_tool_round` | `tool_names` | `None` | 工具轮后：调各源 `notify_tool_round(tool_names)` |
| `collect_post_round_messages` | `mode` | `list[dict]` | POST_ROUND：收集各源 `pop_post_round_reminder(mode)`，构造 `user` 消息追加到历史 |

**feature 门控**：否（但注册的提醒源受各自 feature 门控）。 **reload**：无（随新 Agent 重建）。

**持有的关键状态**：`_providers`（提醒源列表，按注册顺序迭代）。

三处注入时机在状态机中的位置见 [agent-runtime.md](agent-runtime.md)。

---

## features.py / paths.py — 支撑模块

`src/mgr/features.py` — feature 合法名单与解析。`ALL_FEATURES = {task, skill, subagent, file, memory, plan}`；`resolve_features(declared)`：`None`→全开，否则取与合法名单交集（未知名告警丢弃）并校验依赖（`plan` 依赖 `file`，缺则丢 `plan` 并告警）。被 `bootstrap` 与 `Agent` 共同引用。语义详见 [architecture.md](architecture.md#feature-门控)。

`src/mgr/paths.py` — 三层目录路径解析：`builtin_root()`（内置资源根，debug 指向 `src/`）、`common_role_dir()`（`src/roles/common/`）、`global_data_dir()`（`$AGENT_HOME` 或 `~/.agent/`）、`project_data_dir(workdir)`（`{workdir}/.agent/`）、`workdir(override)`（override → `$AGENT_WORKDIR` → cwd）。三层目录体系详见 [architecture.md](architecture.md)。
