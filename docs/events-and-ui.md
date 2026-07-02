# 事件系统与 UI

框架的**所有输出与输入都通过 `EventBus` 以类型化事件流转**，Agent/Manager 从不直接调用 UI。这实现了业务层与 UI 层的解耦：业务层 `emit` 事件，UI 层 `subscribe` 消费。事件类型见 `src/events/types.py`，总线见 `src/events/bus.py`，UI 见 `src/interfaces/`。

## EventBus

`EventBus`（`src/events/bus.py`）是 `asyncio.Queue` 驱动的发布订阅总线。

### 两层过滤

1. **全局级别门控**（`emit`，`bus.py:56-62`）：`event.level.value > bus.level.value` 的事件直接丢弃，不广播。级别由 `config.yaml` 的 `events.level` 设定（见下）。
2. **订阅者类型过滤**（`_Subscription.accepts`，`bus.py:38-41`）：每个订阅者可选只关注特定事件类型集合，`None` 表示全收。

### 生产者 API

| 方法 | 作用 | 返回 |
|---|---|---|
| `emit(event)` | 广播事件（非阻塞，`put_nowait`） | — |
| `request_output(content, markdown)` | 请求 UI 串行输出文本 | — |
| `request_interrupt()` | 请求中断当前交互/agent 工作 | — |
| `request_input(prompt, default, markdown)` | 请求用户输入 | 用户文本 |
| `request_choice(prompt, options, default_index, markdown)` | 请求菜单选择 | 所选 value（空串=取消） |
| `request_permission(tool_name, detail, suggested_rules, mcp_server_rule)` | 请求权限确认 | `yes`/`session`/`always`/`session_server`/`always_server`/`deny` |
| `notify_permission(status, tool_name, detail)` | 发布权限状态通知 | — |

需要 UI 应答的 `request_*`（input/choice/permission）在无订阅者时抛 `NoEventSubscribers`（`bus.py:23-28`）——它们经 `asyncio.Future` 等待 UI 回填结果（`UserInputRequest.complete/cancel/fail`，`types.py:22-38`）。

### 消费者 API

- `subscribe(event_types)`（`bus.py:199-216`）：返回 async iterator，`async for` 消费；退出时自动摘除订阅。
- `join()`（`bus.py:218-223`）：等待已投递事件全部处理完（`queue.join()`）。
- `close()`（`bus.py:233-236`）：向所有订阅者投 `_SENTINEL`，令其退出循环。
- `set_level(level)` / `level`：运行时动态调整级别。

## EventLevel 与事件目录

`EventLevel`（`src/events/levels.py`）三级：`PROGRESS=1`、`DETAIL=2`、`TRACE=3`。`from_str()` 对无效值回退 `PROGRESS`。`events.level` 越高看到的事件越多（高级别包含低级别）。

事件类型（`src/events/types.py`，均继承 `Event`：`timestamp`/`source`/`level`/`type`）：

| 类名 | `type` | level | 关键 payload |
|---|---|---|---|
| `ResponseDelta` | `token_delta` | PROGRESS | `content`、`caller_agent_type`、`caller_uuid` |
| `ThinkingDelta` | `thinking_delta` | DETAIL | `content`、`caller_*`（progress 级别下被门控丢弃） |
| `CompactDelta` | `compact_delta` | PROGRESS | `content` |
| `ToolCallStarted` | `tool_call_started` | PROGRESS | `tool_name`、`tool_call_id`、`detail`、`caller_*` |
| `ToolCallCompleted` | `tool_call_completed` | PROGRESS | `status`(success/error)、`duration_seconds`、`result_preview` |
| `LLMCallStarted` | `llm_call_started` | PROGRESS | `model`、`estimated_input_tokens`、`message_count`、`tool_count`、`caller_*` |
| `LLMCallCompleted` | `llm_call_completed` | PROGRESS | `input/output/total_tokens`、`cache_read/creation_input_tokens`、速度、`caller_uuid` |
| `OutputRequested` | `output_requested` | PROGRESS | `content`、`markdown` |
| `InterruptRequested` | `interrupt_requested` | PROGRESS | — |
| `PermissionNotice` | `permission_notice` | PROGRESS | `status`(allow/deny/auto_allow)、`tool_name`、`detail` |
| `PermissionRequested` | `permission_requested` | PROGRESS | `tool_name`、`detail`、`suggested_rules`、`mcp_server_rule`、`future` |
| `InputRequested` | `input_requested` | PROGRESS | `prompt`、`default`、`markdown`、`future` |
| `ChoiceRequested` | `choice_requested` | PROGRESS | `prompt`、`options:list[(value,label)]`、`default_index`、`future` |
| `AgentStateChanged` | `agent_state_changed` | DETAIL | `agent_id`、`agent_type`、`from_state`、`to_state` |
| `SubagentLifecycle` | `subagent_lifecycle` | PROGRESS | `agent_uuid`、`agent_type`、`phase`(start/end) |
| `SystemStateChanged` | `system_state_changed` | PROGRESS | 无 payload（pull 模型，仅作重绘信号） |

> 交互与状态类事件（权限、输入、token 统计、状态刷新）刻意定为 PROGRESS，确保任何级别下都不被门控丢弃；仅 `ThinkingDelta`（思考正文）与 `AgentStateChanged` 为 DETAIL。

## UserInterface 抽象

`UserInterface`（`src/interfaces/base.py`）是 I/O 抽象基类，封装事件→handler 分发。核心是 `on_event(event)`（`base.py:178-243`）的 `match` 分派：

- **流收尾**：任何非 `ThinkingDelta`/`ResponseDelta` 事件到达前，先收尾未结束的思考流/回应流（`_end_streams_for`），保证输出不交叉。
- **交互事件**（`InputRequested`/`ChoiceRequested`/`PermissionRequested`）：记为 `_active_user_request`，经 `_complete_user_request` 读取并回填 `future`；中断时 `cancel_active_input()` 取消。
- **展示事件**：转发到可覆盖的 `on_*` 钩子（`on_response_delta`、`on_thinking_delta`、`on_tool_call_started/completed`、`on_llm_call_started/completed`、`on_compact_delta`、`on_permission_notice`），基类默认无操作，由具体 UI 实现渲染。
- **系统状态**：`SystemStateChanged` → `on_system_state_changed()`；UI 经 `get_system_state()`（pull 模型，返回 `SystemState(permission_mode)`）读取最新状态。

子类须实现四个抽象方法：`_write`、`_read_input`、`_read_permission`、`_read_choice`。

## InlineInterface — 终端 UI

`InlineInterface`（`src/interfaces/inline_ui.py`，基于 `prompt_toolkit`）是默认交互式 UI，维护一个常驻 `Application` 与底部状态栏。非 TTY（管道/CI）时降级为无状态条的 plain 读写（`_read_*_plain`）。

### 状态栏与布局

底部常驻区域按需渲染（`_render_*` 系列）：活动行（当前 agent + spinner + 计时）、核心状态（权限模式、token 累计）、子 agent 列表、转录覆盖面板、选择菜单、斜杠命令补全下拉、分隔线。

### 三种交互模式与按键

`_build_key_bindings()`（`inline_ui.py:1373+`）按交互模式门控按键：

| 场景 | 按键 |
|---|---|
| 可输入态 | Enter 提交（补全选中时应用补全）；Ctrl+J / Shift+Enter 换行；Shift+Tab 切换权限模式；Ctrl+C 清空/中断；Ctrl+D 空缓冲 EOF |
| select 态（选择菜单） | ↑↓ 移动、1–9 数字直选、Enter 确认、Esc 取消 |
| 补全态（斜杠命令下拉） | ↓/Tab 下一项、↑ 上一项、Esc 关闭 |
| agent 列表/转录面板 | ↓↑ 进入列表/导航/返回；列表内 Enter 打开子 agent 转录面板；面板内 PgUp/PgDn 滚动、Esc 关闭 |

### 权限对话选项

`_permission_options()`（`inline_ui.py:964-984`）根据 `mcp_server_rule` 是否存在构建菜单：

| value | label（有建议规则时） |
|---|---|
| `yes` | 允许一次 |
| `session` | 会话允许(上述规则) |
| `always` | 始终允许并保存(上述规则) |
| `session_server` | 会话信任整个 server(...)（仅 MCP 工具） |
| `always_server` | 始终信任整个 server 并保存(...)（仅 MCP 工具） |
| `deny` | 拒绝 (esc) |

非 TTY 降级时接受打字缩写 `y/s/a/n`（MCP 追加 `ss/aa`），由 `_normalize_permission_answer()`（`inline_ui.py:1033-1054`）归一化。

## OutputRouter — 多 agent 输出路由

`OutputRouter`（`src/interfaces/output_router.py`）插在事件消费与 `ui.on_event` 之间，解决并发子 agent 输出交叉的问题。

`dispatch(event)`（`output_router.py:104-150`）的决策表：

| 事件 | 处理 |
|---|---|
| 透传模式（非 TTY） | 全部实时转发（`SubagentLifecycle` 丢弃） |
| 控制面（`InputRequested`/`PermissionRequested`/`PermissionNotice`/`OutputRequested`） | 始终实时转发 |
| `LLMCallCompleted` | 先按 `caller_uuid` 累计 per-agent token，再转发 |
| `SubagentLifecycle` | 自消费，维护 `_agents` 视图（不转发 UI） |
| `CompactDelta` | 始终实时 |
| 流/工具事件（携带 `caller_uuid` 且 ≠ 前台 uuid） | **进后台缓冲**转录，不实时渲染 |
| 其余（前台 agent） | 实时转发 |

- **前台 agent**（`set_foreground` 登记的根 agent uuid）的输出实时渲染在滚动区；**后台子 agent** 的流/工具事件按 agent 追加到 `_AgentView.transcript`（`deque`，`_MAX_TRANSCRIPT_SEGMENTS=400`），供用户在列表中打开转录面板回看。
- 连续同类流文本合并进同一段（`_append_transcript`），避免跨 delta 把 Markdown 代码围栏/列表拆碎。
- 已完成 agent 视图至多保留 `_MAX_COMPLETED_VIEWS=20` 个，按结束时间逐出最旧（运行中的恒保留）。
- `agent_rows()` / `transcript_segments(uuid)` 供 UI 每帧拉取快照渲染。`reload()` 在 `/clear` 时清空所有视图。

## MarkdownStreamRenderer — 流式 Markdown

`MarkdownStreamRenderer`（`src/interfaces/markdown_renderer.py`）在流式输出中**只渲染已完成的 Markdown 块**，不重解析已输出内容：

- `append(content)` 累积缓冲并吐出已完成块；`flush()` 收尾剩余；`reset()` 清空。
- `_completed_block_end()` 识别块边界：代码围栏配对、空行、原子行（标题/列表项/引用/分隔线）。
- `base_style`（如 `"dim"`）在渲染时整体叠加到 Markdown 上并烘焙进 ANSI（思考流用来整体压暗），避免污染 `Live` 底部状态栏。
- `render_markdown()` 是一次性渲染函数，基于 `rich`（`code_theme` 默认 `monokai`）。

## SlashCommandCompleter — 斜杠命令补全

`SlashCommandCompleter`（`src/interfaces/completer.py`）为输入框提供斜杠命令补全：仅在命令名阶段（以 `/` 开头且不含空格）产出候选，一旦输入空格（带参数）即停止。命令列表 `(name, description)` 由组装层注入，避免 UI 反向依赖业务层。
