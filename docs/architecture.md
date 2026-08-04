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

**入口装配层** — `main.py` 解析 CLI 参数（`--workdir`、`--debug`），调用 `src/app/bootstrap.py` 的 `create_app()`。这是整个框架**唯一的具体实现实例化点**：手动构造所有 Manager 与 UI 状态服务，注入 `AgentDeps` dataclass，返回 `AgentApp`（`bootstrap.py:19-92`）。

**应用主循环层** — `src/app/app.py` 的 `AgentApp` 管理外层 REPL：启动 UI、创建事件消费任务、打印启动横幅、重置会话、循环驱动 Agent 轮次、处理中断、退出时收尾（`app.py:31-77`）。

**Agent 状态机层** — `src/agent/agent.py` 的 `Agent` 是由 `_handlers: dict[AgentState, Callable]` 驱动的有限状态机（`agent.py:236-249`），枚举定义在 `src/agent/states.py:42-55`。每轮的可变状态封装在 `RunContext`（`states.py:58-106`）中；终态 LLM 错误由 `LLM_FAILURE` 承接，上下文超限仍进入 `CONTEXT_OVERFLOW`。

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
│   任一 handler 的终态 LLM 错误 → CONTEXT_OVERFLOW/LLM_FAILURE  │
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

事件流是横切各层的：所有输出与输入都通过 `EventBus`（`src/events/bus.py`）以类型化事件流转，不直接调用 UI。每次 LLM 尝试由 provider 发出 `LLMCallStarted`，可重试失败发出 `LLMRetrying`，成功发出 `LLMCallCompleted`，终态失败发出 `LLMCallFailed`；发布使用安全遥测入口，随后由 `OutputRouter` 先写 `AgentViewStore`，再按前后台身份决定是否交给 UI（`events/bus.py:36-65`、`interfaces/output_router.py:50-87`）。详见 [events-and-ui.md](./events-and-ui.md)。

---

## 2. 启动装配 `create_app()`

`create_app(workdir_override)` 按固定顺序构造组件（`bootstrap.py:19-92`）。顺序不可随意调整——存在若干硬依赖，下表标注关键先后约束。

| 步骤 | 构造对象 | 说明 |
|---|---|---|
| 1 | `global_dir` / `work_dir` | 解析并规范化全局目录与工作目录 |
| 2 | `ProjectTrustGate` | 在任何项目可执行配置加载前确认工作区指纹 |
| 3 | `ConfigManager` / `DataGuard` | 按信任结果加载配置，并登记 Provider、环境和 MCP 精确秘密 |
| 4 | `RoleMgr` / `EventBus` / UI | 激活角色并构造事件与展示层 |
| 5 | `ToolsMgr` / feature Managers / Hooks | 注册内置工具；项目 Hook 仅在受信任时加载 |
| 6 | `McpMgr.start()` | 按信任和连接开关启动 server；动态工具强制 `REVIEW + EXTERNAL` |
| 7 | `SessionMgr` / `LLMMgr` | 构造持久化服务、发现模型并验证 default |
| 8 | `PermissionManager` / `WebAccessMgr` | 注入通用智能权限审查、Web 安全审查、一次性确认回调与 Web 路由依赖 |
| 9 | `AgentDeps` / `AgentApp` | 组装共享依赖并返回应用实例 |

启动信任必须先于 ConfigManager、Hook 和 MCP；这是防止未信任项目通过环境、模型端点或子进程在确认前执行的硬顺序。PermissionManager 不快照工具元数据，ToolsMgr 每次调用都把当前 ToolEntry 的 policy 与 origin 传给 `authorize()`，因此动态 MCP 工具可在运行时注册和重连。

### LLM 启动错误链

`LLMMgr` 构造时严格校验 `llm` 调用配置，非法值抛 `LLMConfigurationError`。`load_models()` 并发发现模型，网络或 SDK 异常以及非法模型列表响应都作为单个 provider 的发现失败：后者归类为 `response_protocol`，错误经安全分类后写入 `provider_errors`；只有配置了非空静态 `models` 才回退注册，否则该 provider 不提供模型。只有 provider 配置非法或跨 provider 模型归属冲突才抛 `LLMConfigurationError`（`llm_mgr.py:59-223,443-504`）。

`llm_mgr.ensure_default_available()`（`bootstrap.py:71`）随后按精确模型 ID 校验默认模型；不可用时将安全化的发现失败摘要加入 `ModelUnavailableError`，不会选择其他 provider（`llm_mgr.py:308-335`）。两类错误都在 UI 启动前冒泡到 `main.cli()`，由其打印“启动失败”并以状态码 1 退出，不输出深层堆栈（`main.py:46-61`）。

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
| `web_access_mgr` | `WebAccessMgr` | 否 | 本地/provider 原生 Web 统一路由 |
| `config_mgr` | `ConfigManager` | 否 | 三层配置合并 |
| `memory_mgr` | `MemoryMgr \| None` | **memory** | 记忆读写，未启用注入 None |
| `hooks_mgr` | `HooksMgr \| None` | 否 | 生命周期钩子 |
| `plan_mgr` | `PlanMgr \| None` | **plan** | 计划模式，未启用注入 None |
| `plugin_mgr` | `PluginMgr \| None` | 否 | 插件发现 |
| `session_mgr` | `SessionMgr \| None` | 否 | 会话持久化与恢复 |
| `mcp_mgr` | `McpMgr \| None` | 否 | MCP server 连接管理 |
| `role_mgr` | `RoleMgr \| None` | 否 | 角色发现与激活，提供 manifest |
| `plan_mode_controller` | `Any` | 否 | 入口 Agent 的 Plan 状态与快捷键协调器，`_reset_session` 时注入 |
| `session_context` | `list[str]` | 否 | 会话级附加上下文（SessionStart hook、resume 摘要注入） |
| `session_id` | `str` | 否 | 当前会话 ID，`_reset_session` 时生成 |
| `workdir` | `Path \| None` | 否 | 用户工作目录 |
| `global_dir` | `Path \| None` | 否 | 全局配置目录 |

> 说明：`memory_mgr` 与 `plan_mgr` 在 `bootstrap.create_app()` 处即按 feature 门控决定是否实例化（`bootstrap.py:50,53`）。而 `FileMgr`、`SkillMgr`、`SubAgentMgr`、`TaskManager` 则是在每个 `Agent.__post_init__` 内按该 agent 自身的 feature 集创建（见 [agent-runtime.md](./agent-runtime.md) 第 6 节），不进入 `AgentDeps`。

`AgentViewStore` 与 `OutputRouter` 属于 app/UI 层，不进入业务依赖容器 `AgentDeps`。同一个 Store 实例由 `TextualInterface`、`OutputRouter` 和 `AgentApp` 共享，避免业务 Agent 持有展示状态。

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
| `memory` | `MemoryMgr`（`bootstrap.py:50`），记忆工具与提示词段 |
| `plan` | `PlanMgr`（`bootstrap.py:53`），4 个 plan 工具，计划模式 |

`create_app()` 用 `resolve_features(role_mgr.manifest.features)` 计算有效集（`bootstrap.py:49`），决定 `MemoryMgr`/`PlanMgr` 是否实例化。每个 `Agent` 在 `__post_init__` 中再次调用 `resolve_features(self.features)`（`agent.py:125-126`）解析自身 feature 集，据此过滤工具 schema、按需创建 agent 级 Manager。子 agent 的 feature 集：自身 manifest 声明则用其值，否则继承父 agent。详见 [roles-subagents-skills.md](./roles-subagents-skills.md) 与 [managers.md](./managers.md)。

未启用的 feature 对应工具通过 `tools_mgr.excluded_tool_names(features)` 计算出 `_excluded_tools`（`agent.py:127`），在 `refresh_tools_schemas()` 中从 schema 中减去（`agent.py:228-236`），LLM 不再看到这些工具。

---

## 5. `reload()` 协议

有状态的 Manager 可以实现 `reload()`，但 `/clear` 不做无序发现。`AgentApp._reset_session()` 按依赖顺序显式执行：停止 MCP 并注销 MCP 工具 → 更新项目信任与 ConfigManager → 重建 DataGuard 秘密集 → 重载 RoleMgr、PluginMgr、HooksMgr → `LLMMgr.reconfigure()` → 重启 MCP → 清理会话级 Memory/Tools/Plan/UI 状态 → 重建 Agent。该顺序保证旧 provider、端点、项目环境和外部连接不会跨会话存活。

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

`src/roles/common/` 是特殊的共享目录（不是角色），其 `agents/`、`skills/`、`AGENTS.md` 对所有角色生效，作为最低优先级层被叠加。

完整目录布局速查见 [configuration-reference.md](./configuration-reference.md)。
