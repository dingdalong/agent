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

`create_app(workdir_override)` 按固定顺序构造组件。模型配置链的关键约束是先解析角色，再把同一个 `RoleMgr` 注入 `LLMMgr`；`LLMMgr` 由此读取实际激活角色的模型槽位。

| 步骤 | 构造对象 | 说明 |
|---|---|---|
| 1 | `global_dir` / `work_dir` | 解析并规范化全局目录与工作目录 |
| 2 | `ProjectTrustGate` | 在任何项目可执行配置加载前确认工作目录信任 |
| 3 | `ConfigManager` | 按信任结果加载三层配置与有效环境 |
| 4 | 首次 Provider 配置向导 | 无显式配置时运行 `SetupApp` 并持久化（见下）；失败抛 `LLMConfigurationError` 干净退出 |
| 5 | `DataGuard` / `RoleMgr` / `EventBus` / UI | 登记秘密；`RoleMgr` 发现角色并把缺省或不存在的配置角色解析为 `coding` |
| 6 | `ToolsMgr` / feature Managers / Hooks | 注册内置工具；项目 Hook 仅在受信任时加载 |
| 7 | `McpMgr.start()` | 按信任和连接开关启动 server；动态工具强制 `REVIEW + EXTERNAL` |
| 8 | `SessionMgr` / `LLMMgr` | 以 `LLMMgr(config_mgr, role_mgr, event_bus)` 构造，发现模型后调用 `ensure_slots_available()` 校验激活角色的两个槽位 |
| 9 | `PermissionManager` / `WebAccessMgr` | 注入 fast 槽位智能权限、一次性确认回调与 Web 路由依赖；Web LLM 安全审查客户端虽被构造，但当前授权路径未调用 |
| 10 | `AgentDeps` / `AgentApp` | 组装共享依赖并返回应用实例 |

### 首次 LLM Provider 配置向导

`bootstrap.create_app()` 在项目信任确认和 `ConfigManager` 构造后调用 `maybe_run_provider_setup(config_mgr)`。只有 `ConfigManager.has_explicit_provider_config()` 判定用户层尚无显式 Provider 配置时才进入向导；任一来源命中即跳过：

- 有效环境中存在任一内置 Provider 的 `{NAME}_API_KEY` 或 `{NAME}_API_URL` 键；有效环境遵循项目信任边界；
- 全局 `config.yaml` 存在非空 `llm_provider` mapping，或可信项目层存在同类配置；
- 用户层 `llm_provider` 非 mapping，或 YAML 无效时保守视为显式，避免覆盖无法解析的内容。

仅内置 `src/config.yaml` 的 provider 段、或用户层只有角色模型槽位，不算显式 Provider 配置。

TTY 向导依次完成 Provider、API 地址与凭据、严格 `list_models` 验证，然后在同一 Provider 返回的同一模型列表上显示两个模型选择屏：先选 `default`，再选 `fast`。进入 fast 屏时默认高亮刚选定的 default 模型，直接回车可让两个槽位使用同一模型；返回 default 屏时保留原选择。验证使用 10 秒超时且不采用静态模型列表回退，失败只显示安全化错误，不写配置。

确认后 `persist_setup()` 使用与 `RoleMgr` 相同的角色解析：`role.default` 指向的角色不存在时回退 `coding`。它先把完整 `{"default": ..., "fast": ...}` mapping 写到全局 `role.<有效角色>.model`，reload 并确认没有被更高优先级层覆盖，再把 `{PROVIDER}_API_URL` 与云 Provider 的 `{PROVIDER}_API_KEY` 写入全局 `.env`。两个文件分别原子更新，不提供跨文件事务；后置 reload 会同时核对两个槽位、URL 和凭据。

- 非 TTY：不读取 stdin，抛 `LLMConfigurationError`，提示手工配置全局 `.env` 与 `role.<有效角色>.model.default/fast`。
- 取消：不写任何配置并以配置错误退出。
- 已有显式 Provider 配置：跳过向导，直接进入模型发现与双槽位校验。
- 向导只在首次进程启动接线，`/clear` 不重跑；没有 `/setup` 命令。

### LLM 启动错误链

`LLMMgr` 先严格校验 `llm` 调用参数与 `llm_provider` 配置，再由 `load_models()` 并发发现模型。单个 Provider 发现失败时记录安全化的 `provider_errors`，仅在该 Provider 配置了非空静态 `models` 时注册静态列表；Provider 配置非法、模型列表非法配置或跨 Provider 模型归属冲突会抛 `LLMConfigurationError`。

`ensure_slots_available()` 随后读取实际激活角色的 `role.<角色>.model`。父键仍是旧字符串格式、缺少 `default`/`fast`、槽位不是非空字符串，均抛 `LLMConfigurationError`；任一槽位未精确命中已加载模型则抛 `ModelUnavailableError`，错误中附可用模型和安全化的 Provider 发现摘要。内置配置没有槽位兜底，也不会选择其他模型或 Provider。两类错误都在 UI 启动前由 `main.cli()` 收口为启动失败。

### 运行时模型数据流

```text
ConfigManager 三层合并
  └─ RoleMgr: role.default → 已发现角色（不存在则 coding）
       └─ LLMMgr: role.<实际角色>.model.{default,fast}
            ├─ 主 Agent: manifest.model=None → get(None) → default
            ├─ 子 Agent: manifest.model → 固定别名或完整模型 ID
            ├─ compact / 退出总结: 复用所属 Agent 的 provider
            ├─ 智能权限: get("fast") + 单次 effort=low
            └─ 原生 Web: 工具传入调用方 Agent 自己的 provider
```

主角色 `role.md` 出现 `model` 会在 `RoleMgr` 解析期报错；主 Agent 始终以 default 槽位构造。`SubAgentMgr` 在加载 manifest 时验证 model 只能是 `default`、`fast`、`opus`、`sonnet`、`haiku` 或已加载的完整模型 ID，并在错误中包含定义文件路径。`CompactMgr` 构造时接收 `self.llm`，退出总结也直接调用 `self.llm.chat()`，因此都保持所属 Agent 的模型和 Provider。智能权限固定解析 fast 槽位，结构化裁决对该次调用覆盖 `low`，不修改缓存 Provider 的默认 effort。`web_search`/`web_fetch` 则把发起工具调用的 `agent.llm` 传给 `WebAccessMgr`，原生路由不会改用主 Agent 或 fast 槽位。

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
