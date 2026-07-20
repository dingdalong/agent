# 架构总览

本文档面向需要理解框架整体结构的开发者与运维者，介绍四层架构、启动装配流程、依赖容器 `AgentDeps`、feature 门控机制、`reload()` 协议与目录路径解析。

相关文档：
- Agent 状态机与运行时细节见 [agent-runtime.md](./agent-runtime.md)。
- 各 Manager 的职责见 [managers.md](./managers.md)。
- 工具注册与执行见 [tools.md](./tools.md)。
- 角色、子 agent、技能见 [roles-subagents-skills.md](./roles-subagents-skills.md)。
- 权限系统见 [permissions.md](./permissions.md)。
- 目录布局速查见 [configuration-reference.md](./configuration-reference.md)。

---

## 1. 四层架构

框架是一个基于 Python `asyncio` 单事件循环运行的 AI Agent CLI，自顶向下分为四层。上层依赖下层，装配方向自下而上（先构造底层 Manager，再注入上层）。

**入口装配层** — `main.py` 解析 CLI 参数（`--workdir`、`--debug`），调用 `src/app/bootstrap.py` 的 `create_app()`。这是整个框架**唯一的具体实现实例化点**：手动构造所有 Manager，注入 `AgentDeps` dataclass，返回 `AgentApp`（`bootstrap.py:19-78`）。

**应用主循环层** — `src/app/app.py` 的 `AgentApp` 管理外层 REPL：启动 UI、创建事件消费任务、打印启动横幅、重置会话、循环驱动 Agent 轮次、处理中断、退出时收尾（`app.py:26-63`）。

**Agent 状态机层** — `src/agent/agent.py` 的 `Agent` 是由 `_handlers: dict[AgentState, Callable]` 驱动的有限状态机（`agent.py:161-174`），枚举定义在 `src/agent/states.py:39-51`。每轮的可变状态封装在 `RunContext`（`states.py:54-100`）中，避免异步冲突。

**Manager 服务层** — `src/mgr/` 下各 Manager 各司其职（`RoleMgr`、`LLMMgr`、`ToolsMgr`、`PermissionManager`、`CompactMgr`、`PromptMgr`、`SubAgentMgr`、`SkillMgr` 等）。部分 Manager 受 feature 门控，未启用时在 `create_app()` 注入 `None`。

```
┌─────────────────────────────────────────────────────────────┐
│ 入口装配层                                                     │
│   main.py  →  bootstrap.create_app()                          │
│   解析 CLI、构造全部 Manager、组装 AgentDeps、返回 AgentApp     │
└───────────────────────────────┬─────────────────────────────┘
                                 │ 注入 AgentDeps
                                 ▼
┌─────────────────────────────────────────────────────────────┐
│ 应用主循环层                                                   │
│   AgentApp (src/app/app.py)                                   │
│   ui.start → _consume_events → _reset_session                 │
│            → while: _run_agent_turn → 处理 interrupt/exit/clear│
└───────────────────────────────┬─────────────────────────────┘
                                 │ Agent.from_manifest / agent.run()
                                 ▼
┌─────────────────────────────────────────────────────────────┐
│ Agent 状态机层                                                 │
│   Agent (src/agent/agent.py) + _handlers[AgentState]          │
│   REQUEST_INPUT → CHECK_COMPACT → [COMPACT → CHECK_COMPACT]    │
│   → LLM_CALL                                                   │
│   → PROCESS_RESPONSE → [EXECUTE_TOOLS → POST_ROUND] → …→ DONE  │
└───────────────────────────────┬─────────────────────────────┘
                                 │ 调用 Manager 方法
                                 ▼
┌─────────────────────────────────────────────────────────────┐
│ Manager 服务层  (src/mgr/)                                     │
│   RoleMgr · LLMMgr · ToolsMgr · PermissionManager · McpMgr    │
│   CompactMgr · PromptMgr · SubAgentMgr · SkillMgr · HooksMgr   │
│   MemoryMgr* · PlanMgr* · PluginMgr · SessionMgr · TaskManager │
│                              (* = feature 门控，未启用注入 None) │
└─────────────────────────────────────────────────────────────┘
```

事件流是横切各层的：所有输出与输入都通过 `EventBus`（`src/events/bus.py`）以类型化事件流转，不直接调用 UI。详见 [events-and-ui.md](./events-and-ui.md)。

---

## 2. 启动装配 `create_app()`

`create_app(workdir_override)` 按固定顺序构造组件（`bootstrap.py:19-78`）。顺序不可随意调整——存在若干硬依赖，下表标注关键先后约束。

| 步骤 | 构造对象 | 源码 | 说明 |
|---|---|---|---|
| 1 | `global_dir` | `bootstrap.py:27-28` | `global_data_dir()`（`$AGENT_HOME` 或 `~/.agent/`），并 `mkdir` 确保存在 |
| 2 | `work_dir` | `bootstrap.py:29` | `workdir(workdir_override)` 解析工作目录 |
| 3 | `ConfigManager` | `bootstrap.py:31` | 三层配置合并（内置 → 全局 → 项目） |
| 4 | `RoleMgr` | `bootstrap.py:32` | 发现并激活角色，提供 `manifest` |
| 5 | `EventBus` | `bootstrap.py:33` | 事件级别取自 `config_mgr.get_config("events")["level"]`（缺省 `"progress"`），经 `EventLevel.from_str` 解析 |
| 6 | `InlineInterface` | `bootstrap.py:34` | 终端 UI，注入 `SLASH_COMMANDS` 供自动补全 |
| 7 | `OutputRouter` | `bootstrap.py:35-36` | 消费端事件路由器；非 TTY 时 `passthrough=True` |
| 8 | `ToolsMgr` | `bootstrap.py:37` | 工具注册表 |
| 9 | `resolve_features` | `bootstrap.py:39` | 依据激活角色 manifest 的 `features` 计算有效 feature 集 |
| 10 | `MemoryMgr`（门控） | `bootstrap.py:40` | 仅当 `"memory" in feats` 才实例化，否则 `None` |
| 11 | `PluginMgr` | `bootstrap.py:41` | 插件发现 |
| 12 | `HooksMgr` | `bootstrap.py:42` | 生命周期钩子，依赖 `plugin_mgr` |
| 13 | `PlanMgr`（门控） | `bootstrap.py:43` | 仅当 `"plan" in feats` 才实例化，否则 `None` |
| 14 | `McpMgr` + `await start()` | `bootstrap.py:45-46` | 连接 MCP server 并将其工具注册进 `tools_mgr` |
| 15 | `PermissionManager` | `bootstrap.py:47-53` | 从 `tools_mgr.list_entries()` 收集工具权限元数据 |
| 16 | `SessionMgr` | `bootstrap.py:54` | 会话历史持久化与恢复 |
| 17 | `LLMMgr` + `await load_models()` | `bootstrap.py:55-56` | 加载模型清单 |
| 18 | `ensure_default_available()` | `bootstrap.py:59` | 启动前置校验，默认模型不可用时抛错 |
| 19 | `AgentDeps` | `bootstrap.py:60-77` | 组装依赖容器 |
| 20 | `AgentApp` | `bootstrap.py:78` | 返回应用实例 |

### 为何 `McpMgr.start()` 必须在 `PermissionManager` 之前

`McpMgr.start()`（`bootstrap.py:46`）在连接 MCP server 后会把每个 server 的上游工具注册进 `tools_mgr`。`PermissionManager` 构造时（`bootstrap.py:47-53`）通过 `tools=tools_mgr.list_entries()` 一次性收集**所有已注册工具**的权限元数据。若权限层先于 MCP 启动构造，MCP 工具尚未进入 `tools_mgr`，其权限元数据就不会被收录——权限检查将无法识别 MCP 工具。因此二者顺序是硬约束。注释见 `bootstrap.py:44`。

### 为何 `ensure_default_available()` 在 UI 启动前

`llm_mgr.ensure_default_available()`（`bootstrap.py:59`）在 `load_models()` 之后、返回 `AgentApp` 之前执行前置校验：默认模型不可用时抛 `ModelUnavailableError`。此时 UI（`AgentApp.run()` 里的 `ui.start()`）尚未启动，异常直接冒泡到 `main.cli()`，被捕获后打印可操作提示并以非零码干净退出，而非在 UI 已接管终端后抛出深层堆栈（`main.py:53-56`，注释见 `bootstrap.py:57-58`）。

---

## 3. `AgentDeps` 依赖容器

`AgentDeps` 是进程级依赖容器，定义在 `src/agent/agent.py:49-73`（**并非单独文件**）。它由 `create_app()` 一次性组装，注入所有 Agent 实例共享。门控 Manager 未启用时注入 `None`。

| 字段 | 类型 | feature 门控 | 用途 |
|---|---|---|---|
| `llm_mgr` | `LLMMgr` | 否 | 模型管理与解析 |
| `ui` | `UserInterface` | 否 | 终端交互接口 |
| `event_bus` | `EventBus` | 否 | 类型化事件总线 |
| `tools_mgr` | `ToolsMgr` | 否 | 工具注册与执行 |
| `permission_mgr` | `PermissionManager \| None` | 否 | 权限检查（无 permission 时可为 None） |
| `config_mgr` | `ConfigManager` | 否 | 三层配置合并 |
| `memory_mgr` | `MemoryMgr \| None` | **memory** | 记忆读写，未启用注入 None |
| `hooks_mgr` | `HooksMgr \| None` | 否 | 生命周期钩子 |
| `plan_mgr` | `PlanMgr \| None` | **plan** | 计划模式，未启用注入 None |
| `plugin_mgr` | `PluginMgr \| None` | 否 | 插件发现 |
| `session_mgr` | `SessionMgr \| None` | 否 | 会话持久化与恢复 |
| `mcp_mgr` | `McpMgr \| None` | 否 | MCP server 连接管理 |
| `role_mgr` | `RoleMgr \| None` | 否 | 角色发现与激活，提供 manifest |
| `permission_mode_controller` | `Any` | 否 | 权限模式 UI 协调器，`_reset_session` 时注入 |
| `session_context` | `list[str]` | 否 | 会话级附加上下文（SessionStart hook、resume 摘要注入） |
| `session_id` | `str` | 否 | 当前会话 ID，`_reset_session` 时生成 |
| `workdir` | `Path \| None` | 否 | 用户工作目录 |
| `global_dir` | `Path \| None` | 否 | 全局配置目录 |

> 说明：`memory_mgr` 与 `plan_mgr` 在 `bootstrap.create_app()` 处即按 feature 门控决定是否实例化（`bootstrap.py:40,43`）。而 `FileMgr`、`SkillMgr`、`SubAgentMgr`、`TaskManager` 则是在每个 `Agent.__post_init__` 内按该 agent 自身的 feature 集创建（见 [agent-runtime.md](./agent-runtime.md) 第 6 节），不进入 `AgentDeps`。

---

## 4. feature 门控机制

feature 门控是角色控制"启用哪些可插拔能力"的开关系统，实现在 `src/mgr/features.py`。

**合法名单**（`features.py:15`）：

```python
ALL_FEATURES = frozenset({"task", "skill", "subagent", "file", "memory", "plan"})
```

**`resolve_features(declared)` 语义**（`features.py:18-41`）：

| 输入 | 行为 |
|---|---|
| `declared is None`（未声明） | 返回 `ALL_FEATURES` 全集（向后兼容，全部启用） |
| `declared` 为集合 | 取 `declared & ALL_FEATURES` 交集 |
| `declared` 含未知名 | 差集告警后丢弃（`features.py:31-37`） |
| 结果含 `plan` 但缺 `file` | 丢弃 `plan` 并告警（`features.py:38-40`）——**plan 依赖 file** |

**每个 feature 门控的对象：**

| feature | 门控的 Manager / 工具 / 提示词段 |
|---|---|
| `task` | `TaskManager`（`agent.py:151-155`），任务工具，任务提醒段 |
| `skill` | `SkillMgr`（`agent.py:141-144`），`load_skill` 等技能工具，技能提示词段 |
| `subagent` | `SubAgentMgr`（`agent.py:145-148`），`task_delegator` 工具 |
| `file` | `FileMgr`（`agent.py:140`），文件读写工具 |
| `memory` | `MemoryMgr`（`bootstrap.py:40`），记忆工具与提示词段 |
| `plan` | `PlanMgr`（`bootstrap.py:43`），4 个 plan 工具，计划模式 |

`create_app()` 用 `resolve_features(role_mgr.manifest.features)` 计算有效集（`bootstrap.py:39`），决定 `MemoryMgr`/`PlanMgr` 是否实例化。每个 `Agent` 在 `__post_init__` 中再次调用 `resolve_features(self.features)`（`agent.py:125-126`）解析自身 feature 集，据此过滤工具 schema、按需创建 agent 级 Manager。子 agent 的 feature 集：自身 manifest 声明则用其值，否则继承父 agent。详见 [roles-subagents-skills.md](./roles-subagents-skills.md) 与 [managers.md](./managers.md)。

未启用的 feature 对应工具通过 `tools_mgr.excluded_tool_names(features)` 计算出 `_excluded_tools`（`agent.py:127`），在 `refresh_tools_schemas()` 中从 schema 中减去（`agent.py:228-236`），LLM 不再看到这些工具。

---

## 5. `reload()` 协议

有状态的 Manager 实现 `reload()` 方法。`/clear` 重置会话时（`AgentApp._reset_session`），框架通过 `hasattr(mgr, "reload")` 发现并统一调用（`app.py:135-142`）。

`_reset_session` 中实际遍历并 reload 的对象列表（`app.py:135-137`）：

```python
for attr in ("memory_mgr", "tools_mgr", "permission_mgr",
             "config_mgr", "plugin_mgr", "hooks_mgr", "plan_mgr",
             "ui"):
```

即：`memory_mgr`、`tools_mgr`、`permission_mgr`、`config_mgr`、`plugin_mgr`、`hooks_mgr`、`plan_mgr`、`ui`（其中 `memory_mgr`、`plan_mgr` 可能为 `None`，被 `mgr is not None` 跳过）。`ui` 一并纳入以清零会话级 token 统计（注释见 `app.py:134`）。

`output_router` 由 app 层单独持有（不在 `AgentDeps` 中），在遍历之后单独调用 `output_router.reload()` 清空 agent 视图（`app.py:141-142`）。

> 每个 Manager 是否实现 `reload()` 请以其源码为准，见 [managers.md](./managers.md)。文档所述"实现 reload"的对象须与 `_reset_session` 实际调用列表一致，即上述 8 个属性 + `output_router`。

---

## 6. 目录与路径解析

所有路径解析集中在 `src/mgr/paths.py`，统一管理三层目录体系（内置 → 全局 → 项目）。

| 函数 | 返回 | 来源/规则 | 源码 |
|---|---|---|---|
| `builtin_root()` | 内置资源根目录 | debug 时为 `src/`，安装后为 site-packages 中的包目录（`Path(__file__).resolve().parent.parent`） | `paths.py:9-18` |
| `common_role_dir()` | 共享角色资源目录 | `builtin_root() / "roles" / "common"`；非可激活角色（无 role.md），加载优先级最低层 | `paths.py:21-30` |
| `global_data_dir()` | 全局配置目录 | 环境变量 `$AGENT_HOME`，否则 `~/.agent/` | `paths.py:33-42` |
| `project_data_dir(workdir)` | 项目配置目录 | `{workdir}/.agent/` | `paths.py:45-56` |
| `workdir(override)` | 用户工作目录 | `override` → 环境变量 `$AGENT_WORKDIR` → `Path.cwd()` | `paths.py:59-75` |

### 三层目录如何贯穿各子系统

框架的配置、角色、agents、skills、plugins 都遵循"内置 → 全局 → 项目"三层叠加，后者覆盖前者：

| 子系统 | 内置（最低优先级） | 全局 `~/.agent/` | 项目 `.agent/`（最高优先级） |
|---|---|---|---|
| 配置 `config.yaml` | `src/config.yaml` | `~/.agent/config.yaml` | `.agent/config.yaml` |
| 角色 | `src/roles/<role>/` | `~/.agent/roles/` | `.agent/roles/` |
| 子 agent | `src/roles/common/agents/` + 角色 `agents/` | `~/.agent/agents/` | `.agent/agents/` |
| 技能 | `src/roles/common/skills/` + 角色 `skills/` | `~/.agent/skills/` | `.agent/skills/` |
| MCP server | 角色 `mcp_servers.json` | `~/.agent/mcp_servers.json` | `.agent/mcp_servers.json` |
| 权限/settings | — | `~/.agent/settings.json` | `.agent/settings.json` |

`src/roles/common/` 是特殊的共享目录（不是角色），其 `agents/`、`skills/`、`AGENT.md` 对所有角色生效，作为最低优先级层被叠加。

完整目录布局速查见 [configuration-reference.md](./configuration-reference.md)。
