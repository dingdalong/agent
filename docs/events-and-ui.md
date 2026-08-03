# 事件系统与 UI

所有输入输出通过 `EventBus` 流转。Agent、Manager 和 LLM provider 发布类型化事件，`AgentApp` 消费后交 `OutputRouter`；Router 先更新 `AgentViewStore`，再决定哪些事件进入前台 UI。事件定义见 `src/events/types.py` 与 `src/events/menu.py`。

## 1. EventBus 与安全遥测

`EventBus` 使用每订阅者一条 `asyncio.Queue`。`emit()` 先以 reset 拒绝 gate 取消不得跨会话入队的 `UiRequest`，再按 `EventLevel` 门控和订阅者事件类型集合广播（`src/events/bus.py:88-143`）。`PROGRESS=1`、`DETAIL=2`、`TRACE=3`；交互、LLM 边界和正文均为 PROGRESS，思考与状态转换为 DETAIL。

主要请求 API（`bus.py:145-417`）：

| 方法 | 返回 |
|---|---|
| `request_output(content, markdown)` | 无 |
| `request_interrupt()` | 无 |
| `request_input(prompt, default, markdown)` | 用户文本 |
| `request_choice(...)` | 选项 value；空串表示取消 |
| `request_form(...)` | answers/discussion JSON |
| `request_choice_input(...)` | choice/text JSON |
| `request_transcript_view(uuid)` | 空串 |
| `request_permission(...)` | 权限决策字符串 |

`UiRequest` 是所有窗口请求的共同基类，携 future 并由 `complete()` / `cancel()` / `fail()` 落定；`MenuRequest` 表示需要作答的窗口，`ViewRequest` 表示只读窗口（当前为 `TranscriptView`）。无订阅者时抛 `NoEventSubscribers`。

LLM 调用使用 `emit_telemetry_safely()`（`src/events/bus.py:36-65`）发布开始、重试、完成、失败及流增量。普通发布异常只写不含 payload 的告警，不改变调用成功/失败；`CancelledError`、`KeyboardInterrupt`、`SystemExit` 仍原样传播。

## 2. 调用方身份

`caller_agent_type` / `caller_uuid` 是 `Event` 基类的一等属性（`src/events/types.py:11-25`）。`caller_identity(agent)` 是业务 emit 点的统一取值函数：类型取 `agent.agent_type`，UUID 转字符串（`types.py:28-40`）。

身份用于：

- Router 判断前台/后台；
- Store 将 token、上下文、活动和转录归入正确 Agent；
- UI 在回复、思考、compact、权限和工具区域标注 Agent；
- `CompactMgr` 内部摘要调用仍归属创建它的 Agent。

## 3. 事件目录

| 类名 | `type` | 特有 payload |
|---|---|---|
| `ResponseDelta` | `token_delta` | `content` |
| `ThinkingDelta` | `thinking_delta` | `content` |
| `CompactDelta` | `compact_delta` | `content` |
| `ToolCallStarted` | `tool_call_started` | `tool_name`、`tool_call_id`、`detail` |
| `ToolCallCompleted` | `tool_call_completed` | `status`、耗时、结果预览 |
| `LLMCallStarted` | `llm_call_started` | 模型、窗口、输入估算、消息数、工具数、`attempt`、`max_attempts` |
| `LLMCallCompleted` | `llm_call_completed` | 输入/输出/缓存 token、耗时与吞吐率 |
| `LLMRetrying` | `llm_retrying` | `error_kind`、`safe_message`、`partial`、`tool_fragment_state`、`attempt`、`max_attempts`、`wait_seconds` |
| `LLMLengthRetrying` | `llm_length_retrying` | `truncation_kind`、`strategy`、`effort`、`attempt`、`max_attempts` |
| `LLMCallFailed` | `llm_call_failed` | `error_kind`、`safe_message`、`attempts`、`partial`、工具片段状态、状态码、provider code、request ID、diagnostic ID |
| `OutputRequested` | `output_requested` | `content`、`markdown` |
| `InterruptRequested` | `interrupt_requested` | 无 |
| `PermissionNotice` | `permission_notice` | 决策状态、工具名、detail |
| `AgentStateChanged` | `agent_state_changed` | Agent ID/类型、前后状态 |
| `SubagentLifecycle` | `subagent_lifecycle` | 子 Agent UUID/类型、start/end、结束 messages |
| `PlanStateChanged` | `plan_state_changed` | `active`，通知 UI 重读入口 Agent 的 Plan 状态 |
| `MenuRequest` / `ViewRequest` | 各自类型 | prompt、选项/问题或查看目标、future |

### LLM 边界事件

`LLMCallStarted`（`src/events/types.py:97-113`）按每次尝试发出；首次通常为 `1/max_attempts`，重试会再次发出递增的 `attempt`。输入 token 估算在线程中执行，避免阻塞事件循环（`src/llm/base.py:953-990`）。

`LLMRetrying`（`types.py` `LLMRetrying`）在可重试失败完成分类和等待计算后发出。`wait_seconds` 保留抖动后的浮点值；显示层自行向上取整。`partial` 覆盖正文、思考或工具片段，`tool_fragment_state` 区分无片段、半截和完整工具片段。

`LLMLengthRetrying`（`types.py` `LLMLengthRetrying`）在响应因 `finish_reason == "length"` 被截断、进入自动恢复时发出，由 `agent.py` 的 `_emit_length_retrying` 发射（无 `event_bus` 的单测 agent 静默）。`truncation_kind` 取 `tool_call`/`content`/`thinking`/`unknown`，`strategy` 取 `continue`（正文/工具阶段续写）/`regenerate-lower-effort`（降档重生成）/`regenerate-compress`（触底压缩重生成），`effort` 为本次恢复调用将用的推理力度档位。级别为 PROGRESS，保证进入 Store 并参与前后台分流；它不含 `wait_seconds`，因长度恢复不进退避等待。

`LLMCallFailed`（`types.py` `LLMCallFailed`）只在终态失败时发出。它包含安全摘要和有限诊断字段，不包含请求、响应体、凭据或原始异常对象。长度恢复耗尽也发同一事件，并使用 `output_limit` 类别（`agent.py` `_fail_response_recovery`）。

`LLMCallCompleted` 只对应成功尝试；失败尝试不会产生完成事件。成功事件用于 token/context 统计，失败事件用于终态诊断，两者职责不混合。

## 4. `AgentViewStore`

`AgentViewStore` 是 UI 唯一读模型（`src/interfaces/agent_view_store.py:79-105`）。它维护会话累计 token、前台 UUID、活动与历史 Agent、每 Agent 的窗口/活动/生命周期以及分段转录。

`record()`（`agent_view_store.py:139-165`）对 LLM 事件的处理：

- `LLMCallStarted`：记录窗口，活动改为“等待响应”；第二次及以后显示 `等待响应 attempt/max_attempts`（`:363-385`）。
- `ResponseDelta` / `ThinkingDelta`：分别追加 `response` / `thinking` 段；只有相邻同类段合并（`:480-502`）。
- `LLMRetrying`：活动改为“重试中”，追加独立 `retry` 段，写安全类别、摘要、尝试号和片段状态（`:387-405`）。该段会阻断失败尝试正文与下一尝试正文的合并。
- `LLMLengthRetrying`：活动改为“恢复中”，追加独立 `retry` 段（`_record_length_retry`），按 `strategy` 写「⚠ 输出截断（阶段）：<从中断处继续生成/降低推理力度至 X 后重生成/压缩思考后重生成> (attempt/max)」。重生成会丢弃被截断的思考/正文流，该段隔断被丢弃流与重生成流在转录中被误合并。
- `LLMCallFailed`：活动改为“失败”，追加独立 `error` 段；记录 attempts、partial、工具状态及可用的状态码、provider code、request ID、diagnostic ID（`:407-437`）。
- `LLMCallCompleted`：累计会话与 Agent token，并以实际输入 token 更新上下文用量（`:338-361`）。

Store 无 UUID 时只累计会话 token，不虚构 Agent。完成子 Agent 先保留在 active，前台下一次 `LLMCallStarted` 时由 Router 触发 `flush_completed()` 迁入有界历史。

## 5. OutputRouter 的前后台规则

`OutputRouter.dispatch()` 始终先 `store.record(event)`（`src/interfaces/output_router.py:50-87`），然后按以下顺序路由：

1. `SubagentLifecycle` 只进 Store，不进 UI。
2. `LLMCallStarted`、`LLMRetrying`、`LLMLengthRetrying`、`LLMCallFailed` 是边界事件（`_LLM_BOUNDARY_EVENTS`）：只有 `caller_uuid` 精确等于已登记的前台 UUID 才转发；后台和缺前台身份的边界静默。前台 started 还先迁移已完成子 Agent。
3. 非 TTY 的 `passthrough=True` 对其余事件全部透传，因此正文、思考、工具、完成、菜单与 compact 都保持普通输出，同时 Store 仍记录。
4. TTY 下菜单、权限通知、显式输出和 `CompactDelta` 始终转发。
5. TTY 下带身份且 UUID 不等于前台的正文、思考、工具与完成事件只进 Store，避免后台事件切断前台 Markdown 流。
6. 其余前台事件转发。

边界事件先于 passthrough 判断，因此非 TTY 也不会直接打印后台 Agent 的重试倒计时或终态失败；这些信息仍可从 Store/转录查看。

## 6. 流式 Markdown 与尝试分段

`UserInterface.on_event()` 在分派新事件前调用 `_end_streams_for()`：非思考事件收尾思考流，非正文事件收尾正文流（`src/interfaces/base.py:288-311`）。因此 retry、failure、工具或完成边界到达时，当前 Markdown 渲染器先 flush 并换行，不会把诊断文本并入正文块。

TTY 的正文与思考各有独立 `MarkdownStreamRenderer`。首次增量打印一次 Agent 前缀，`append()` 只输出已完整的 Markdown 块，边界时 `flush()` 输出残留（`src/interfaces/inline/output.py:99-169`）。Store 同时按 `response` / `thinking` / `retry` / `error` 保留明确分段。

非 TTY 不输出 ANSI；增量仍经过同一事件分派，但落到纯文本前端。重试与失败诊断是单独静态行，不参与 Markdown 块合并。

## 7. TTY 与非 TTY 的重试/失败展示

`on_llm_call_started()` 将活动设为“等待响应”；后续思考、正文增量分别切到“思考中”“回应中”（`src/interfaces/inline/output.py:139-161,225-242`）。DETAIL 级能看到完整三段；PROGRESS 级思考事件被总线门控，等待状态持续到首个正文增量。

TTY 收到 `LLMRetrying` 时（`output.py:244-277`）：

- 有任何失败尝试残片时，先永久写一条尝试分隔，含安全类别、摘要和工具片段状态；
- 无残片时不额外污染 scrollback；
- 两种情况都进入黄色活动区倒计时；截止时间为 `monotonic + wait_seconds`，100ms 重绘，剩余秒 `ceil` 且下限为 0；
- 下一次 `LLMCallStarted` 调用 `_set_activity()` 时清除倒计时，显示新的 `等待响应 attempt/max_attempts`。

非 TTY 每次重试都打印静态黄色语义行，显示向上取整后的等待秒数，不做动态重绘。

`on_llm_length_retrying()`（`output.py` `on_llm_length_retrying`）区别于重试：长度恢复不进退避等待，故只按 `strategy` 打印一行黄色标记「⚠ 输出截断（阶段）：<从中断处继续生成/降低推理力度至 X 后重生成/压缩思考后重生成> (attempt/max)」，不启动活动区倒计时。路由已保证只转发前台 agent，TTY 与非 TTY 同样处理。

`on_llm_call_failed()` 在前台永久打印红色终态行，附可用的 request ID 与 diagnostic ID，并把活动设为“失败”（`output.py:279-299`）。Store 记录更完整的安全诊断元数据；后台失败只更新 Store。

## 8. 工具轮与状态栏

TTY 只缓冲前台 Agent 当前一轮的工具。实时区优先级为“重试倒计时 > 本轮工具面板 > 单行活动”；轮边界把缓冲定稿为 scrollback 工具块（`src/interfaces/inline/status_bar.py:103-166,191-273`）。边界是前台新一次 `LLMCallStarted` 或返回输入态，保证正文、工具块与下一轮正文顺序稳定。非 TTY 不缓冲工具，按开始/完成事件逐行打印。

`StatusPresenter` 从不可变快照统一生成 token、上下文和 elapsed 文本。主会话 elapsed 是跨回合累计的有效耗时；纯人工等待仅在没有叶子工具继续计算时暂停。Agent 转录覆盖层显示当前 Agent 自身的生命周期、token 与上下文。

## 9. Inline UI 组件

`src/interfaces/inline_ui.py` 是薄门面，实际组件位于 `src/interfaces/inline/`：

| 模块 | 职责 |
|---|---|
| `controller.py` | 组装 prompt-toolkit 布局和普通输入 |
| `runtime.py` | 持有 Application、Buffer 与当前作答窗口的内部 future |
| `window_manager.py` | 窗口栈、唯一键盘焦点、FIFO 作答队列和 runner 生命周期 |
| `status_bar.py` | 活动、重试倒计时、工具轮与底部状态 |
| `agent_panel.py` | Agent 列表和转录渲染缓存 |
| `menus.py` / `form.py` | 选择、权限、组合输入和表单 |
| `output.py` | Rich、流式 Markdown、LLM/工具进度展示 |
| `plain.py` | 非 TTY 输入输出，保证无 ANSI |
| `keymap.py` | 快捷键与覆盖层优先级 |

TTY 的 `WindowManager` 是窗口状态与键盘焦点的唯一来源。它最多保留一个转录窗口和一个作答窗口：普通输入、权限、选择、表单和组合输入不会抢占已有作答窗口，而是按 FIFO 排队；状态栏显示“等待 N：来源”，来源优先使用发起 agent 类型、缺失时回退事件 source。只有真正开始运行的作答窗口才打印调用方标记和菜单上文。

转录是只读窗口，不占用 `InlineRuntime.interaction()` 的内部 future。`/agents` 创建带 future 的 `TranscriptView`，Esc 移除窗口后才完成该 future；实时查看则创建无 future 的同类窗口。作答窗口位于转录之上时拥有键盘，结束后转录的 UUID 和滚动位置原样恢复。普通输入可以被实时转录临时覆盖，关闭后复用同一个 Buffer、文本和光标；权限、表单和选择期间不能进入 Agent 列表。

`UserInterface.on_event()` 先让 TTY 前端接受 `UiRequest`，成功后立即返回给事件消费者；非 TTY 仍串行读取。这样正在查看转录时，后续权限请求可马上进入 WindowManager，而不会被 `TranscriptView` 阻塞。`InlineRuntime.interaction()` 只排他服务当前作答窗口；下一个作答窗口必须等前一个 runner 清理完共享 UI 状态后才会启动，且对外 future 在清理后才落定。`EventBus.join()` 等待订阅队列处理完成，并通过 delivery revision 覆盖稳定检查前已经开始的投递，但不等待 WindowManager 自有的 dialog runner，因此中断收束会在 join 后额外等待 `ui.wait_interactions_idle()`；UI 停止时先关闭 WindowManager，取消活动、排队和只读请求，再退出 prompt-toolkit。`/clear` 的项目信任菜单在 reset gate 前完成；随后重载 Managers、清空 Store 和创建新 Agent 前，会同步取消旧 UI 请求并等待所有窗口 runner 清理完成。重置期间到达的 UI 请求同样会被取消，不会跨越 session 边界。

键盘由栈顶窗口决定：栈顶为作答窗口时，其快捷键优先；栈顶为转录时才由转录处理滚动和 Esc；普通输入内部再按补全、Agent 列表和输入行处理。LLM 与工具活动只更新状态栏遥测，不得改变窗口栈或隐藏活动作答窗口。
