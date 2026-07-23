# AGENTS.md

本文件为 Codex (Codex.ai/code) 在此仓库中工作时的指导文件。

## 开发命令

```bash
uv sync                              # 安装依赖
uv run python main.py                # 运行应用
uv run python main.py --workdir /path  # 指定工作目录运行
uv run python main.py --debug        # 启用 asyncio 慢回调告警（事件循环被占用 >0.1s 即告警），排查阻塞
uv run pytest                        # 运行全部测试
uv run pytest -k "test_name"         # 运行匹配名称的测试
uv run pytest tests/test_foo.py      # 运行单个测试文件
```

项目要求 Python >= 3.13，使用 `uv` 管理依赖。无 lint/format 工具配置。

## 深入参考

`docs/` 目录是本框架面向人的**完整技术参考**，涉及源码处均带 `文件:行` 引用；本文件只是精简工作指引，遇到需要细节时按主题查阅：`architecture.md`（装配顺序、`AgentDeps` 字段、`reload()` 协议、`paths.py`）、`agent-runtime.md`（`AgentApp` REPL、逐个 handler、`Agent.from_manifest`）、`managers.md`（逐个 Manager 职责/方法/消费配置）、`llm.md`、`tools.md`（`@tool`、执行流水线、内置工具清单）、`roles-subagents-skills.md`、`permissions.md`（6 步评估顺序、规则格式）、`mcp-and-hooks.md`、`events-and-ui.md`、`configuration-reference.md`（三份配置文件的完整 schema）。索引见 `docs/README.md`。

## 架构概述

本项目是一个自研的 AI Agent CLI 框架，采用 Python asyncio 构建，分为四层：

**入口与组装层** — `main.py` 解析 CLI 参数，`src/app/bootstrap.py` 中的 `create_app()` 是唯一的依赖组装点，手动构造所有 Manager 并注入 `AgentDeps` dataclass。

**应用主循环层** — `src/app/app.py` 中的 `AgentApp` 管理外层 REPL：启动 UI → 消费事件 → 驱动 Agent 轮次 → 处理中断 → 会话 Hook → 关闭。

**Agent 状态机层** — `src/agent/agent.py` 中的 `Agent` 是有限状态机，由 `_handlers: dict[AgentState, Callable]` 驱动（枚举见 `src/agent/states.py`）。主流程（happy path）：
```
REQUEST_INPUT → CHECK_COMPACT → [COMPACT →] LLM_CALL → PROCESS_RESPONSE
→ [EXECUTE_TOOLS → POST_ROUND → CHECK_COMPACT] → CHECK_STOP → DONE
```
另有边缘/退出状态：`LENGTH_RETRY`（响应因长度截断时续写，上限 `RunContext.max_length_recoveries`；截断的工具调用只保留正文并提示拆分）、`PAUSE_TURN`（按 Anthropic 协议把原始 assistant blocks 回填后自动续接）、`CONTEXT_OVERFLOW`（上下文溢出处理）、`LLM_FAILURE`（非上下文类终态 LLM 错误）、`SUMMARIZE_EXIT`（退出前总结）。length 与 pause 共用一条响应恢复链：首个待恢复响应建立 checkpoint，恢复期间累加正文，连续 pause 用最新载体替换旧载体；正常完成时提交，耗尽、终态错误或取消时统一回滚临时消息。普通工具轮仍只保留最后一次完整回答。`RunContext` 持有每轮的可变状态、恢复 checkpoint/计数、取消恢复快照与安全结构化的终态 LLM 错误，避免线程/异步冲突。

**Manager 服务层** — `src/mgr/` 下的各 Manager 类各司其职：`RoleMgr`（角色发现与激活）、`LLMMgr`（模型管理）、`ToolsMgr`（工具注册与执行）、`PermissionManager`（6 步权限检查）、`CompactMgr`（上下文压缩）、`PromptMgr`（系统提示词构建）、`SubAgentMgr`（子智能体调度）、`SkillMgr`（技能加载）等。部分 Manager 受 feature 门控（见下）：未启用对应 feature 时在 `bootstrap.create_app()` 注入 `None`（如 `MemoryMgr`/`PlanMgr`），其工具与提示词段随之从 schema 中排除。

### 角色系统（Roles）

**角色是框架的顶层组织单位**——一套角色决定了主 agent 的身份提示词、可用子 agent、技能、MCP server 与启用的 feature 集。`RoleMgr`（`src/mgr/role_mgr.py`）三层发现所有角色（低→高优先级）：内置 `src/roles/` → 全局 `~/.agent/roles/` → 项目 `.agent/roles/`，同名后者覆盖。激活角色由 `config.yaml` 的 `role:` 键指定（缺省回退 `coding`；当前仓库设为 `onboard`）。

每个角色目录 `src/roles/<role>/` 结构：
- `role.md` — 角色定义文件（YAML frontmatter + body，与子 agent 的 `*.md` 同格式）。body 成为**主 agent 的核心身份提示词**（`PromptMgr._build_core` 的“# 核心身份”段）；frontmatter 的 `features` 键声明启用的 feature 集，`permissionMode` 设定会话级默认权限模式（优先级高于 `settings.json` 的 `defaultMode`），`reasoning_effort` 设定推理力度档位（`low`/`medium`/`high`/`xhigh`/`max`），`agent_type` 固定视为 `main`。
- `AGENT.md` — 角色级行为准则，进入“# 行为准则”段。
- `agents/*.md` — 角色专属子 agent 定义。
- `skills/*/SKILL.md` — 角色专属技能。
- `plugins/`、`mcp_servers.json` — 角色专属插件与 MCP server 配置。

`src/roles/common/` 是**特殊的共享目录**（不是一个角色，`RoleMgr._discover` 显式跳过），其 `agents/`、`skills/`、`AGENT.md` 对所有角色生效，作为最低优先级层被叠加。

### feature 门控

角色在 `role.md` frontmatter 声明 `features` 列表，决定启用哪些**可插拔 Manager**及其工具、提示词段。合法名单见 `src/mgr/features.py` 的 `ALL_FEATURES`：`task`、`skill`、`subagent`、`file`、`memory`、`plan`。语义：
- 未声明（`None`）→ 全部启用（向后兼容）；声明 → 取与 `ALL_FEATURES` 的交集（未知名告警丢弃）。
- **依赖校验**：`plan` 依赖 `file`，缺 `file` 时丢弃 `plan` 并告警。
- `bootstrap.create_app()` 用 `resolve_features(role_mgr.manifest.features)` 计算有效集，据此决定 `MemoryMgr`/`PlanMgr` 等是否实例化（未启用注入 `None`）。
- 子 agent 的 feature 集：自身 manifest 声明则用其值，否则继承父 agent 已解析的集（见 `subagent_mgr.task_delegator`）。

### 事件驱动 I/O

所有输出和输入通过 `EventBus`（`src/events/bus.py`）以类型化事件流转，不直接调用 UI。事件类型定义在 `src/events/types.py`。

### 配置系统

3 层合并，后者覆盖前者：内置 `src/config.yaml` → 全局 `~/.agent/config.yaml` → 项目 `.agent/config.yaml`。环境变量通过 `.env` 文件加载（全局 `~/.agent/.env`、项目 `.agent/.env`）。`config.yaml` 的 `role:` 键选定激活角色（见「角色系统」），`llm.default` 指定默认模型（子 agent 通过别名引用），`llm.best`/`llm.fast` 可选；统一调用参数位于 `llm.timeout_seconds` 与 `llm.retry.{max_attempts,base_delay_seconds,max_delay_seconds}`，最大尝试次数包含首次调用。Anthropic 的 `llm_provider.anthropic.max_pause_turn_continuations` 控制单条恢复链的协议续接上限，默认 `5`，只接受非 bool 正整数；它与网络错误重试次数相互独立。

`settings.json`（全局 `~/.agent/` + 项目 `.agent/` 两层合并，`allow`/`deny` 列表去重并集，其余键项目覆盖全局）承载权限与 MCP 策略：

- `permissions.{allow,deny,ask}`：规则文本形如 `工具名` 或 `工具名(specifier)`，`specifier` 走 fnmatch；**工具名段也支持 `*`/`?` 通配**，故可写 `mcp__<server>__*` 一次性 allow/deny/ask 整个 MCP server，或 `mcp__github__get_*` 按前缀放行（`deny` 优先于 `allow`）。
- `permissions.defaultMode`：会话级默认权限模式。**解析优先级**：激活角色 `role.md` 的 `permissionMode` →（未声明）此处 `settings.json` `defaultMode` →（未声明）内置默认。该结果即 `PermissionManager.default_mode`，用作主 agent 初始模式、未声明 `permissionMode` 的子 agent 的回退值、`/clear` 重置目标与绑定前状态栏显示值。
- `mcp.enabledServers`（非空时作白名单）/ `mcp.disabledServers`（始终剔除）：在 `mcp_mgr.start()` 连接前过滤 server，被禁用的 server 不连接、其工具不注册、不进 LLM schema。与上面的 `deny` 规则正交——`deny` 仍连接并仅在调用时拒绝，`disabledServers` 是连接前的硬开关。

MCP server 连接配置在独立的 `mcp_servers.json`（角色 `src/roles/<role>/` → 全局 `~/.agent/` → 项目 `.agent/` 三层合并），格式见 `src/mgr/mcp_mgr.py`。

每个 server 配置可带一个**只读**的 `permissions` 块就近声明该 server 的权限（`{"allow": [...], "deny": [...], "ask": [...]}`）：

- 条目是相对该 server 的**上游工具名通配**（如 `get_*`、`create_issue`、`*` 通配全部），加载时 server 段经 `_safe_tool_name` 清洗后展开为 `mcp__<server>__<entry>` 规则；以 `mcp__` 开头的条目按完整工具模式原样使用（逃生口）。规则与 `settings.json` 共用同一 `PermissionRule` 表示与匹配引擎，不存在第二套格式。
- **分层覆盖**：`mcp_servers.json` 是最低优先级层。评估顺序 `settings(deny→ask→allow/session_allow) → 工具自检 → mcp(deny→ask→allow) → bypass → 模式默认`——某次调用上 `settings.json` 只要有规则命中即由它决定，否则才落到 `mcp_servers.json`，故 `settings.json` 的 `allow` 能覆盖 `mcp_servers.json` 的 `deny`（含"信任整个 server"写入 `session_allow` 的 `mcp__<server>__*`）。置于 bypass 前，BYPASS 模式下 mcp 的 `deny`/`ask` 仍生效。
- **只读**：框架永不写回 `mcp_servers.json`；`resolve_ask` 的 "always / 信任整个 server" 持久化只落 `settings.json`。
- 由 `McpMgr.start()` 在三层合并 + server 过滤后抽取，`PermissionManager` 在 `_load_config()` 拉取。**编辑该块需重启生效**（McpMgr 无 `reload`，`/clear` 不重连 server，故不刷新；`settings.json` 的权限编辑则随 `/clear` 重载）。

## 关键模式

**工具注册** — 使用 `@tool` 装饰器（`src/tools/decorator.py`）+ Pydantic 参数模型，自动注册到全局 `_registry`。工具实现在 `src/tools/builtin/`。新增工具时须确认其 `subagent` 标记：`ToolsMgr.resolve_subagent_tools()` 会自动注入所有 `subagent=True` 的工具、强制排除所有 `subagent=False` 的工具（如四个 plan 工具从子 agent 排除）。

**统一 LLM 错误与重试** — 五个 provider 共用 `src/llm/base.py` 的调用模板与 `src/llm/errors.py` 的稳定分类；仅网络、超时、限流、服务和响应协议错误自动重试，provider 响应头优先决定等待时间，否则使用带抖动的指数退避。OpenAI refusal 与 Anthropic refusal/content filter 统一归为非重试 `content_policy`；`pause_turn` 是 Agent 发起的新协议续接调用，不占用同一次调用内部的网络重试次数。每次尝试、等待、成功与终态失败分别发出类型化事件，事件和日志只携安全摘要、有限 provider 元数据及诊断 ID。消息归一化先保留 provider 专属字段再删除真正空的 assistant，确保 `_response_output`、`_anthropic_content` 和 reasoning-only carrier 能跨轮往返。Agent 在单轮状态机边界统一捕获终态错误：上下文超限进入 `CONTEXT_OVERFLOW`，其余进入 `LLM_FAILURE`；保留用户消息与已输出分段，不伪造 assistant 历史，入口会话可继续，子 agent 任务则回滚为待处理。

**异步/阻塞契约（必须遵守）** — 整个框架跑在单线程 asyncio 事件循环上（UI 状态条按 100ms 重绘、事件分发、Agent 轮次共用同一循环）。事件循环只在 `await` 真异步原语时让出控制权；任何在事件循环上运行的 `async def` 一旦做*同步阻塞*工作（同步网络、文件 I/O、`socket.getaddrinfo`、CPU 密集循环）且不 `await`，就会冻结 UI 并停滞事件分发。因此每个工具 / Manager 方法只能是两类之一：
  - **真异步**：函数体只 `await` 真正的异步原语（如 `asyncio.create_subprocess_shell` + `await proc.communicate()`、`AsyncAnthropic`/`AsyncOpenAI`、事件总线等待）。保持 `async def`。正例：`shell`（`src/tools/builtin/shell.py`）、hooks（`src/mgr/hooks_mgr.py`）、LLM provider（`src/llm/`）。
  - **阻塞型**：函数体做同步 I/O / CPU 工作。叶子工具直接声明为普通 `def`——装饰器（`decorator.py:94-97`）会用 `asyncio.to_thread` 自动卸载到线程；若方法必须保留 `async def`（被异步调用方 `await` 的 Manager 方法），则把阻塞段包进 `await asyncio.to_thread(...)`。范例：`web_search`/`web_fetch` 用同步库（`ddgs`/`urllib`），声明为 `def`；`FileMgr`（`src/mgr/file_mgr.py`）各方法为普通 `def`，其工具包装（`file.py`/`plan.py`）也是普通 `def`，由装饰器统一经 `to_thread` 卸载（装饰器是唯一的线程卸载点，无需层层手写 `to_thread`）。
  - **禁止**：`async def` 里直接跑同步阻塞工作而不 `await`。排查此类问题可用 `python main.py --debug`（启用 asyncio 调试，事件循环被占用超过 0.1s 即打印 `Executing ... took N seconds` 告警）。

**子智能体** — 定义为 `*.md`（YAML frontmatter 声明 `agent_type`、`tools`、`model`、`memory`、`permissionMode`、`thinking`、`reasoning_effort`、`features` 等 + body 作提示词），由 `SubAgentMgr` **四层扫描**加载，同名后者覆盖（低→高）：共享 `src/roles/common/agents/` → 激活角色 `src/roles/<role>/agents/` → 全局 `~/.agent/agents/` → 项目 `.agent/agents/`。主 Agent 通过 `task_delegator` 工具调度子智能体，每个子智能体是共享 `AgentDeps` 的完整 `Agent` 实例（`Agent.from_manifest` 构造）。`model: inherit` 表示继承父 agent 已解析的真实模型 ID；`thinking`、`reasoning_effort` 未声明时同样继承父 agent 已解析值（`reasoning_effort` 最终未声明则回退 provider 配置）。

**权限模式按 agent 独立** — 可变的权限模式 (`permission_mode`) 与 plan 模式状态 (`_pre_plan_mode`) 持有在每个 `Agent` 实例上，`PermissionManager` 只保留全局共享的规则、`session_allow` 和不可变的 `default_mode`（解析优先级：`role.md` `permissionMode` → `settings.json` `defaultMode` → 内置默认；由 `bootstrap` 将角色 manifest 的 `permission_mode` 注入 `PermissionManager`，`_load_config` 末尾套用使其胜出，`reload()` 自动重放）。`check()` / `is_tool_visible()` / `get_schemas()` 均接收 agent 的 `mode` 参数。语义：用户的模式设置（`/mode`、Shift+Tab、`/plan`、`/resume` 恢复）只作用于入口主 agent（总控）；每个子 agent 在构造时从自身 frontmatter 的 `permissionMode` 取一次值（缺省回退到 `default_mode`），整个生命周期固定不变——子 agent 无 plan 能力、四个 plan 工具标记 `subagent=False` 从子 agent 强制排除，故并发子 agent 互不干扰。MCP 工具触发 ask 弹窗时，除"本工具"会话/保存外，额外提供"信任整个 server"两项（写入 `mcp__<server>__*` 规则），server 名经 `ToolPermission.mcp_server` 透传（见 `permission_mgr.resolve_ask`）。

**技能系统** — `SkillMgr` 四层扫描 `SKILL.md`（共享 → 角色 → 全局 → 项目，插件技能穿插其间），同名后者覆盖；通过 `load_skill` 工具按需注入系统提示词。

**Hooks** — 8 种生命周期钩子事件（`PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`Stop`、`SessionStart`、`SessionEnd`、`SubagentStart`、`SubagentStop`），通过 shell 命令执行，支持 JSON stdin/stdout 协议。

**`reload()` 协议** — 有状态的 Manager 实现 `reload()` 方法，`/clear` 重置会话时通过 `hasattr` 发现并统一调用。

## 编码与协作规范

### 命名、类型与注释
- **见名知义**：函数名与实际用途强相关；相同含义、用途的函数与字段用同一命名。
- **参数标类型**：每个参数显式声明具体类型。
- **写文档注释**：每个新增或修改的函数都写精炼的文档注释，逐一说明各参数与返回值的含义，只描述"是什么/做什么"，不写缘由。

### 复用与抽象
- **消除真正的重复**：多处相同逻辑合并为一个通用函数，不复制粘贴；但不为复用而复用。
- **优先改造旧逻辑**：新需求与已有方法、函数、逻辑部分重合时，优先改造旧逻辑以适配；若改造后变复杂，则拆分并提取公共部分。
- **避免过度抽象**：不为"统一"强行拆分或封装；仅转发调用的包装函数（如 `on_enter/on_exit/on_fire` 只转调 `on_event`）应直接内联，保持扁平、减少间接层。

### 简洁与优化
- **代码简洁、清晰、健壮**，不留隐晦的背后约定；关键算法与设计模式优先用中级程序员能读懂的简单实现。
- **确需复杂算法或高级模式**，先与用户沟通并获批准。
- **不为优化而优化**：优化须实测有效（实现更简洁，或有性能数据支撑），否则先与用户沟通，不擅自改动。

### 修改与重构
- **改前查全链**：调整或重构函数前，检索并理解所有调用与引用点，保证全链一致、不遗漏。
- **删除死代码**：无任何引用的方法、字段、定义连同其注释文档一并移除。
- **同步提示词**：改动涉及工作流变化时，同步新增或更新提示词，指导 LLM 如何工作。
- **先复现再修 bug**：先稳定复现、定位根因，再修改，最后验证。
- **允许大范围重构**：当前项目未上线，无需考虑兼容性与过渡期；只要有益即可大范围重构，不留技术债。

### 判断依据
- **以实际代码逻辑为准**，而非注释、文档或记忆；不默认已有代码就是正确的。
- **标注提示词来源**：决策若受某条提示词影响，明确指出是哪一条。

### 提示词编写
- 只给确定、可落地的指导，杜绝模糊描述。

### 文档维护（强制）
- **废弃即删除，不留痕迹**：某设计、功能或方向被判断为"已取代"或"废弃"时，将其整句或整段从当前活跃文档（含 `AGENTS.md`）中彻底移除，禁止保留并加"历史参考/已废弃"等注释。
- **唯一存档点**：历史版本仅通过 Git 追溯；活跃文档不得保留任何失效计划、旧方向或旧设计。
- **执行标准**：宁可"消失"，不要"备注"。

当新增需要配置的内容时，需要在配置文件中体现出来
