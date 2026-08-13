# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在此仓库中工作时的指导文件。

使用用户输入的语言交流、撰写注释与文档等。

## 开发命令

```bash
uv sync                              # 安装依赖
uv run python main.py                # 运行应用
uv run python main.py --workdir /path  # 指定工作目录运行
uv run python main.py --debug        # 启用 asyncio 慢回调告警（事件循环被占用 >0.1s 即告警），排查阻塞
uv run python main.py --self-check   # 核对随包资源与内置工具/命令注册（验证打包产物）
uv run pytest                        # 运行全部测试
uv run pytest -k "test_name"         # 运行匹配名称的测试
uv run pytest tests/test_foo.py      # 运行单个测试文件
make build                           # 构建当前平台的可执行分发包
make check                           # 对构建产物跑冻结态冒烟测试
make install                         # 构建并把产物装到 ~/.local/bin（等价于用户跑包内 install.sh）
```

项目要求 Python >= 3.13，使用 `uv` 管理依赖。无 lint/format 工具配置。

## 深入参考

`docs/` 目录是本框架面向人的完整技术参考；本文件只保留精简工作指引。权限入口、工具策略、路径与数据安全见 `permissions.md`，装配与状态机见 `architecture.md` 和 `agent-runtime.md`，其余主题索引见 `docs/README.md`。

## 架构概述

本项目是一个自研的 AI Agent CLI 框架，采用 Python asyncio 构建，分为四层：

**入口与组装层** — `main.py` 解析 CLI 参数，`src/app/bootstrap.py` 中的 `create_app()` 是唯一的依赖组装点，手动构造所有 Manager 并注入 `AgentDeps` dataclass。

**应用主循环层** — `src/app/app.py` 中的 `AgentApp` 管理外层 REPL：启动 UI → 消费事件 → 驱动 Agent 轮次 → 处理中断 → 会话 Hook → 关闭。

**Agent 状态机层** — `src/agent/agent.py` 中的 `Agent` 是有限状态机，由 `_handlers: dict[AgentState, Callable]` 驱动（枚举见 `src/agent/states.py`）。主流程（happy path）：
```
REQUEST_INPUT → CHECK_COMPACT → [COMPACT →] LLM_CALL → PROCESS_RESPONSE
→ [EXECUTE_TOOLS → POST_ROUND → CHECK_COMPACT] → CHECK_STOP → DONE
```
另有边缘/退出状态：`LENGTH_RETRY`（响应因长度截断时重试，上限 `RunContext.max_length_recoveries`）、`CONTEXT_OVERFLOW`（上下文溢出处理）、`SUMMARIZE_EXIT`（退出前总结）。`RunContext` 持有每轮的可变状态，避免线程/异步冲突。

**Manager 服务层** — `src/mgr/` 下的各 Manager 类各司其职：`RoleMgr`（角色发现与激活）、`LLMMgr`（模型管理）、`ToolsMgr`（工具注册与执行）、`PermissionManager`（单入口授权）、`CompactMgr`（上下文压缩）、`PromptMgr`（系统提示词构建）、`SubAgentMgr`（子智能体调度）、`SkillMgr`（技能加载）等。部分 Manager 受 feature 门控（见下）：未启用对应 feature 时在 `bootstrap.create_app()` 注入 `None`（如 `MemoryMgr`/`PlanMgr`），其工具与提示词段随之从 schema 中排除。

### 角色系统（Roles）

**角色是框架的顶层组织单位**——一套角色决定了主 agent 的身份提示词、可用子 agent、技能、MCP server 与启用的 feature 集。`RoleMgr`（`src/mgr/role_mgr.py`）三层发现所有角色（低→高优先级）：内置 `src/roles/` → 全局 `~/.agent/roles/` → 项目 `.agent/roles/`，同名后者覆盖。激活角色由 `config.yaml` 的 `role.default` 指定（缺省回退 `coding`）；`role.<角色名>.model` 与 `role.<角色名>.reasoning_effort` 可覆盖激活主角色 `role.md` 的同名字段。

每个角色目录 `src/roles/<role>/` 结构：
- `role.md` — 角色定义文件（YAML frontmatter + body，与子 agent 的 `*.md` 同格式）。body 成为主 agent 的核心身份与主控职责提示词；frontmatter 的 `features` 声明启用能力，`startInPlanMode` 声明初始 Plan 状态，`reasoning_effort` 声明推理力度，`agent_type` 固定视为 `main`。
- `AGENTS.md` — 激活角色内主 agent 与所有子 agent 共用的行为准则，进入“# 行为准则”段；不得放入仅属于主 agent 的身份或总控职责。
- `agents/*.md` — 角色专属子 agent 定义。
- `skills/*/SKILL.md` — 角色专属技能。
- `plugins/`、`mcp_servers.json` — 角色专属插件与 MCP server 配置。

`src/roles/common/` 是**特殊的共享目录**（不是一个角色，`RoleMgr._discover` 显式跳过），其 `agents/`、`skills/`、`AGENTS.md` 对所有角色生效，作为最低优先级层被叠加。

### feature 门控

角色在 `role.md` frontmatter 声明 `features` 列表，决定启用哪些**可插拔 Manager**及其工具、提示词段。合法名单见 `src/mgr/features.py` 的 `ALL_FEATURES`：`task`、`skill`、`subagent`、`file`、`memory`、`plan`。语义：
- 未声明（`None`）→ 全部启用（向后兼容）；声明 → 取与 `ALL_FEATURES` 的交集（未知名告警丢弃）。
- **依赖校验**：`plan` 依赖 `file`，缺 `file` 时丢弃 `plan` 并告警。
- `bootstrap.create_app()` 用 `resolve_features(role_mgr.manifest.features)` 计算有效集，据此决定 `MemoryMgr`/`PlanMgr` 等是否实例化（未启用注入 `None`）。
- 子 agent 的 feature 集：自身 manifest 声明则用其值，否则继承父 agent 已解析的集（见 `subagent_mgr.task_delegator`）。

### 事件驱动 I/O

所有输出和输入通过 `EventBus`（`src/events/bus.py`）以类型化事件流转，不直接调用 UI。事件类型定义在 `src/events/types.py`。

### 配置系统

3 层合并，后者覆盖前者：内置 `src/config.yaml` → 全局 `~/.agent/config.yaml` → 项目 `.agent/config.yaml`。环境变量通过 `.env` 文件加载（全局 `~/.agent/.env`、项目 `.agent/.env`）。`config.yaml` 的 `role.default` 选定激活角色；`role.<角色名>.model` 与 `role.<角色名>.reasoning_effort` 覆盖该主角色 `role.md`，未配置时保留 manifest 值，再由 `llm.default` 与 provider 推理强度兜底。子 agent 不使用这些覆盖项。`llm.default` 指定默认模型（子 agent 通过别名引用），`llm.best`/`llm.fast` 可选。

`settings.json`（全局 `~/.agent/` + 项目 `.agent/` 两层深度合并）承载 Hook 与 MCP 连接开关。`mcp.enabledServers` 非空时作白名单，`mcp.disabledServers` 始终剔除；被禁用的 server 不连接、其工具不注册、不进 LLM schema。授权策略不从配置加载。

MCP server 连接配置在独立的 `mcp_servers.json`（角色 `src/roles/<role>/` → 全局 `~/.agent/` → 项目 `.agent/` 三层合并）。MCP 工具无条件注册为 `REVIEW + EXTERNAL`；server annotation 只能作为描述信息，不能改变授权策略。

## 关键模式

**工具注册** — 使用 `@tool` 装饰器（`src/tools/decorator.py`）+ Pydantic 参数模型，自动注册到全局 `_registry`。工具实现在 `src/tools/builtin/`。新增工具时须确认其 `subagent` 标记：`ToolsMgr.resolve_subagent_tools()` 会自动注入所有 `subagent=True` 的工具、强制排除所有 `subagent=False` 的工具（如四个 plan 工具从子 agent 排除）。新增工具后同步更新 `src/app/self_check.py` 的 `EXPECTED_TOOL_COUNT`。

**可冻结性（必须遵守）** — 项目以 PyInstaller 打包成可执行分发包（`agent.spec` + `scripts/build_exe.py`）。冻结后**文件系统里不存在 `.py` 源文件**，因此：
  - **禁止**用目录 glob 扫 `*.py` 来发现模块。内置工具（`src/tools/__init__.py`）与内置 slash 命令（`src/commands/mgr.py`）一律用 `pkgutil.iter_modules(<包>.__path__)`；用户层的外部 `.py` 才用 `spec_from_file_location` 按路径加载。新增这类插件式子模块时，须在 `agent.spec` 的 `collect_submodules` 里覆盖到。
  - 内置命令的注册发生在**模块执行期**，已在 `sys.modules` 里时必须 `importlib.reload`，否则 `CommandMgr` 重建或 `/clear` 走 `reload()` 后命令全空。
  - **禁止**用 cwd 相对路径读随包资源，一律走 `builtin_root()`（`src/mgr/paths.py`）。新增随包资源须加进 `agent.spec` 的 `datas`。
  - 要落在产物**顶层**（与 `agent` 同级、而非 `_internal/` 内）的文件走 `scripts/build_exe.py` 的 `stage_installer()`：`agent.spec` 的 `datas` 一律进 `_internal/`。安装脚本与 `VERSION` 即属此类。
  - 惰性 import 的模块（如按 transport 分支的 `mcp.client.*`）静态分析看不到，须列入 `agent.spec` 的 `hiddenimports`。
  - 运行时读自身版本（`importlib.metadata`）的包须列入 `copy_metadata`。
  - **所有 spawn 子进程的地方**都要用 `clean_env()`（`src/mgr/frozen.py`）构造环境：冻结产物的动态库搜索路径被引导器改写过，直接继承会让子进程加载错动态库（MCP server 常常本身就是另一个 Python 程序，后果最严重）。
  - 以上失效**都是静默的**——应用照常启动，只是工具、命令或编码悄悄不见。改动相关机制后必须跑 `make build && make check`，仅跑源码测试发现不了。

**异步/阻塞契约（必须遵守）** — 整个框架跑在单线程 asyncio 事件循环上（UI 状态条按 100ms 重绘、事件分发、Agent 轮次共用同一循环）。事件循环只在 `await` 真异步原语时让出控制权；任何在事件循环上运行的 `async def` 一旦做*同步阻塞*工作（同步网络、文件 I/O、`socket.getaddrinfo`、CPU 密集循环）且不 `await`，就会冻结 UI 并停滞事件分发。因此每个工具 / Manager 方法只能是两类之一：
  - **真异步**：函数体只 `await` 真正的异步原语（如 `asyncio.create_subprocess_shell` + `await proc.communicate()`、`AsyncAnthropic`/`AsyncOpenAI`、事件总线等待）。保持 `async def`。正例：`shell`（`src/tools/builtin/shell.py`）、hooks（`src/mgr/hooks_mgr.py`）、LLM provider（`src/llm/`）。
  - **阻塞型**：函数体做同步 I/O / CPU 工作。叶子工具直接声明为普通 `def`——装饰器（`decorator.py:94-97`）会用 `asyncio.to_thread` 自动卸载到线程；若方法必须保留 `async def`（被异步调用方 `await` 的 Manager 方法），则把阻塞段包进 `await asyncio.to_thread(...)`。范例：`web_search`/`web_fetch` 用同步库（`ddgs`/`urllib`），声明为 `def`；`FileMgr`（`src/mgr/file_mgr.py`）各方法为普通 `def`，其工具包装（`file.py`/`plan.py`）也是普通 `def`，由装饰器统一经 `to_thread` 卸载（装饰器是唯一的线程卸载点，无需层层手写 `to_thread`）。
  - **禁止**：`async def` 里直接跑同步阻塞工作而不 `await`。排查此类问题可用 `python main.py --debug`（启用 asyncio 调试，事件循环被占用超过 0.1s 即打印 `Executing ... took N seconds` 告警）。

**子智能体** — 定义为 `*.md`（YAML frontmatter 声明 `agent_type`、`tools`、`model`、`memory`、`startInPlanMode`、`thinking`、`reasoning_effort`、`features` 等 + body 作提示词），由 `SubAgentMgr` 四层扫描加载。主 Agent 通过 `task_delegator` 调度子智能体；子智能体继承父 Agent 当前的 `plan_active`，并共享 `AgentDeps`。

**统一授权与 Plan** — `PermissionManager.authorize()` 是唯一授权入口；工具声明冻结的 `ToolPolicy`，不从用户配置提升权限。`Agent.plan_active` 是独立状态，Shift+Tab 可双向切换；Plan 激活时只允许本地读取、明确安全的内部工具和 `.agent/plans/**` 写入，其余操作直接拒绝且不调用智能权限。

**技能系统** — `SkillMgr` 四层扫描 `SKILL.md`（共享 → 角色 → 全局 → 项目，插件技能穿插其间），同名后者覆盖；通过 `load_skill` 工具按需注入系统提示词。

**Hooks** — 8 种生命周期钩子事件（`PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`Stop`、`SessionStart`、`SessionEnd`、`SubagentStart`、`SubagentStop`），通过 shell 命令执行，支持 JSON stdin/stdout 协议。

**`reload()` 协议** — 有状态的 Manager 实现 `reload()` 方法。`/clear` 必须在 `AgentApp._reset_session()` 中按安全依赖顺序显式重载：停止 MCP、更新信任与配置、重建秘密集、重载角色/插件/Hook、重配 LLM、重启 MCP，最后重建 Agent；不得改回无序的通用 reload 循环。

## 编码与协作规范
读取并遵循[`编码与协作规范.md`](编码与协作规范.md)
