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
| `ToolCallStarted` | `tool_call_started` | `tool_name`、`tool_call_id`、`detail`、`display` |
| `ToolCallCompleted` | `tool_call_completed` | `status`、耗时、结果预览、`display` |
| `LLMCallStarted` | `llm_call_started` | 模型、窗口、输入估算、消息数、工具数、`attempt`、`max_attempts` |
| `LLMCallCompleted` | `llm_call_completed` | 输入/输出/缓存 token、耗时与吞吐率 |
| `LLMRetrying` | `llm_retrying` | `error_kind`、`safe_message`、`partial`、`tool_fragment_state`、`attempt`、`max_attempts`、`wait_seconds` |
| `LLMLengthRetrying` | `llm_length_retrying` | `truncation_kind`、`strategy`、`effort`、`attempt`、`max_attempts` |
| `LLMCallFailed` | `llm_call_failed` | `error_kind`、`safe_message`、`attempts`、`partial`、工具片段状态、状态码、provider code、request ID、diagnostic ID |
| `OutputRequested` | `output_requested` | `content`、`markdown` |
| `InterruptRequested` | `interrupt_requested` | 无 |
| `PermissionNotice` | `permission_notice` | 决策状态（allow/deny）、工具名、detail（裁决理由，完整不截断）、decision_source（`AuthorizationResult.source`，与基类 `Event.source` 含义不同）；UI 以 `{标记} {来源} · {中文工具名} · {结论}(理由)` 一行展示（组装见 `permission_line`） |
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

`AgentViewStore` 是状态栏与子 agent 读模型。它维护会话累计 token、前台 UUID、活动与历史 Agent、每 Agent 的窗口/活动/生命周期以及子 agent 分段转录；主聊天历史来自 `SessionState.records[].view`，Store 不保存第二份主聊天副本。子 agent 快照会以隐藏的 `subagent` view 投影写入当前 `SessionState`，恢复后重新水合为只读历史。

`record()`（`agent_view_store.py:139-165`）对 LLM 事件的处理：

- `LLMCallStarted`：记录窗口，活动改为“等待响应”；第二次及以后显示 `等待响应 attempt/max_attempts`（`:363-385`）。
- `ResponseDelta` / `ThinkingDelta`：分别追加 `response` / `thinking` 段；只有相邻同类段合并（`:480-502`）。
- `LLMRetrying`：活动改为“重试中”，追加独立 `retry` 段，写安全类别、摘要、尝试号和片段状态（`:387-405`）。该段会阻断失败尝试正文与下一尝试正文的合并。
- `LLMLengthRetrying`：活动改为“恢复中”，追加独立 `retry` 段（`_record_length_retry`），按 `strategy` 写「⚠ 输出截断（阶段）：<从中断处继续生成/降低推理力度至 X 后重生成/压缩思考后重生成> (attempt/max)」。重生成会丢弃被截断的思考/正文流，该段隔断被丢弃流与重生成流在转录中被误合并。
- `LLMCallFailed`：活动改为“失败”，追加独立 `error` 段；记录 attempts、partial、工具状态及可用的状态码、provider code、request ID、diagnostic ID（`:407-437`）。
- `LLMCallCompleted`：累计会话与 Agent token，并以实际输入 token 更新上下文用量（`:338-361`）。

Store 无 UUID 时只累计会话 token，不虚构 Agent。完成子 Agent 先保留在 active，前台下一次 `LLMCallStarted` 时由 Router 触发 `flush_completed()` 迁入有界历史。

## 5. OutputRouter 的前后台规则

`OutputRouter.dispatch()` 始终先 `store.record(event)`，完成前后台筛选后才把前台可见事件归并到当前 `SessionState` 并交给 UI。LLM 流按 `call_id` 合并，工具按 `tool_call_id` 合并。路由顺序如下：

1. `SubagentLifecycle` 先更新 Store 并同步当前子 agent 快照到 `SessionState`，不进 UI。
2. `LLMCallStarted`、`LLMRetrying`、`LLMLengthRetrying`、`LLMCallFailed` 是边界事件（`_LLM_BOUNDARY_EVENTS`）：只有 `caller_uuid` 精确等于已登记的前台 UUID 才转发；后台和缺前台身份的边界静默。前台 started 还先迁移已完成子 Agent。
3. 非 TTY 的 `passthrough=True` 对其余事件全部透传，因此正文、思考、工具、完成、菜单与 compact 都保持普通输出，同时 Store 仍记录。
4. TTY 下菜单、权限通知、显式输出和 `CompactDelta` 始终转发。
5. TTY 下带身份且 UUID 不等于前台的正文、思考、工具与完成事件只进 Store，避免后台事件切断前台 Markdown 流。
6. 其余前台事件转发。

边界事件先于 passthrough 判断，因此非 TTY 也不会直接打印后台 Agent 的重试倒计时或终态失败；这些信息仍可从 Store/转录查看。

## 6. 流式 Markdown 与尝试分段

`UserInterface.on_event()` 在分派新事件前调用 `_end_streams_for()`：非思考事件收尾思考流，非正文事件收尾正文流（`src/interfaces/base.py:288-311`）。因此 retry、failure、工具或完成边界到达时，当前 Markdown 渲染器先 flush 并换行，不会把诊断文本并入正文块。

TTY 的正文与思考各使用一个 Textual `Markdown` 流。首次增量挂载 Agent 前缀和 Markdown Widget，后续增量写入该 Widget 的 stream；边界时停止 stream。Store 同时按 `response` / `thinking` / `retry` / `error` 保留明确分段。

非 TTY 不输出 ANSI；增量仍经过同一事件分派，但落到纯文本前端。重试与失败诊断是单独静态行，不参与 Markdown 块合并。

## 7. TTY 与非 TTY 的重试/失败展示

`AgentTuiApp.on_llm_call_started()` 将活动设为“等待响应”；后续思考、正文增量分别切到“思考中”“回应中”。DETAIL 级能看到完整三段；PROGRESS 级思考事件被总线门控，等待状态持续到首个正文增量。

TTY 收到 `LLMRetrying` 时：

- 有任何失败尝试残片时，先永久写一条尝试分隔，含安全类别、摘要和工具片段状态；
- 无残片时不额外污染 scrollback；
- 两种情况都进入黄色活动区倒计时；截止时间为 `monotonic + wait_seconds`，100ms 重绘，剩余秒 `ceil` 且下限为 0；
- 下一次 `LLMCallStarted` 调用 `_set_activity()` 时清除倒计时，显示新的 `等待响应 attempt/max_attempts`。

非 TTY 每次重试都打印静态黄色语义行，显示向上取整后的等待秒数，不做动态重绘。

`on_llm_length_retrying()` 区别于重试：长度恢复不进退避等待，故只按 `strategy` 打印一行黄色标记「⚠ 输出截断（阶段）：<从中断处继续生成/降低推理力度至 X 后重生成/压缩思考后重生成> (attempt/max)」，不启动活动区倒计时。路由已保证只转发前台 agent，TTY 与非 TTY 同样处理。

`on_llm_call_failed()` 在前台永久打印红色终态行，附可用的 request ID 与 diagnostic ID，并把活动设为“失败”。Store 记录更完整的安全诊断元数据；后台失败只更新 Store。

## 8. 工具轮与状态栏

TTY 只缓冲前台 Agent 当前一轮的工具。实时区优先级为”重试倒计时 > 本轮工具面板 > 单行活动”；轮边界把缓冲定稿为历史区工具块。边界是前台新一次 `LLMCallStarted` 或返回输入态，保证正文、工具块与下一轮正文顺序稳定。非 TTY 不缓冲工具，按开始/完成事件逐行打印。

### ToolDisplay 展示数据

工具事件的 `display` 字段携带 `ToolDisplay` 数据（`src/tools/display.py`），为 UI 提供结构化的展示信息，不影响 LLM 侧结果。

`ToolDisplay` 字段：
- `title`（`str`）：中文动作标题，如”执行命令”、”• 已编辑 path (+3 -1)”。
- `content`（`str`）：格式化的参数或结果文本。
- `content_type`（`str`）：`”text”` | `”diff”` | `”json”`。
- `truncated`（`bool`）：是否被截断。

**隐私边界**：`display` 只供 UI 和 Store 消费，不进入 LLM 历史。`ToolCallStarted.display` 的参数内容经 `DataGuard.redact()` 脱敏；`EXTERNAL_READ` 工具（web_search/web_fetch）不生成参数展示。`ToolCallCompleted.display` 中来自 `ToolResult` 的文件差异内容同样经 `DataGuard` 脱敏；`EXTERNAL_READ` 工具不生成结果展示（`display=None`）。

**历史区逐工具渲染**：`flush_round()` 将每个工具调用渲染为独立 Rich Text 块，包含标题行（状态标记 + 中文标题 + 耗时）、参数摘要（bright_black）和结果内容（diff 行 `|+` 绿色、`|-` 红色，普通文本 bright_black）。截断时附截断提示。

**非 TTY 与 Store 转录**：非 TTY 输出使用 `display.title` 和内容的前 20 行（含截断提示）。`AgentViewStore` 转录使用 `display.title` 和前 10 行内容，回退到 `result_preview`。

`StatusPresenter` 从不可变快照统一生成 token、上下文和 elapsed 文本。主会话 elapsed 是跨回合累计的有效耗时；纯人工等待仅在没有叶子工具继续计算时暂停。Agent 转录覆盖层显示当前 Agent 自身的生命周期、token 与上下文。

## 9. Textual UI 组件

`src/interfaces/textual_ui.py` 实现 `UserInterface` 门面，TTY 组件位于 `src/interfaces/tui/`：

| 模块 | 职责 |
|---|---|
| `app.py` | Textual App、历史/流输出、活动区、状态栏、Agent 与转录切换 |
| `widgets.py` | 多行输入、历史锚定、跨视口选择补偿、系统剪贴板后端 |
| `dialogs.py` | 权限、选择、组合输入、表单 Modal 与 FIFO 协调器 |
| `plain.py` | 非 TTY 输入输出，保证无 ANSI |
| `diagnostics.py` | TUI 生命周期、降级和转录渲染的后台滚动诊断日志 |
| `history_journal.py` | 当前前台的纯文本降级缓存；会话切换时从 `SessionRecord.view` 重建 |
| `agent.tcss` | 宽窄和高矮窗口的响应式布局 |

TTY 的 `InteractionCoordinator` 是交互请求状态权威写入者。它最多保留一个转录视图和一个活动作答窗口：普通输入、权限、选择、模型双轴选择、表单和组合输入不会抢占已有作答窗口，而是按 FIFO 排队；状态栏显示“等待 N：来源”，来源优先使用发起 agent 类型、缺失时回退事件 source。`ModelMenu` 用上下键选择模型、左右键选择 `low/medium/high/xhigh/max` 强度，Enter 提交、Esc 取消。只有真正激活的请求才打印调用方标记和菜单上文。

内嵌交互结束后默认静默：`InteractionCoordinator` 只移除控件并写入 Widget 返回的非空摘要，是否保留成功或取消历史由各 Widget 显式声明。`ask_user` 专用的 `FormMenu` 取消时保留 `[用户取消了作答]`，普通选择和组合输入取消不留摘要；权限 Esc 作为拒绝结果保留权限确认摘要。`ModelMenu` 成功和取消都不留摘要，`/models` 与其他命令一样保留用户输入，但菜单上文不进入历史；取消后只留下命令，成功后再显示命令最终输出。

转录是只读面板，不占用作答队列。`/agents` 创建带 future 的 `TranscriptView`，Esc 关闭后才完成该 future；Agent 列表实时查看不创建请求 future。Modal 覆盖转录时拥有键盘，结束后转录的 UUID 和每 Agent 独立滚动位置原样恢复。权限、表单和选择期间不能进入 Agent 列表。

转录数据来自 Agent 运行期间保存的原始消息，渲染层按不可信数据防御式处理：缺失或格式异常的消息、tool call 和参数会降级为可读文本，不应导致 Textual App 退出。已结束的子 Agent 在 Store 的有界历史中保留期间仍可浏览。初次打开、实时刷新和切换视图都进入同一个常驻渲染 worker；worker 串行完成当前 `Markdown.update()`，并只保留一个最新待处理目标。左右键立即更新目标索引和版本，连续输入会跳过中间展示；旧版本完成后不得恢复焦点、滚动位置或标题，关闭视图会使当前版本失效。渲染等待布局稳定期间不得采样滚动位置，避免把过渡几何覆盖到目标 Agent 的已保存位置。其他展示 worker 接收异步函数工厂，只在 worker 真正启动后创建 coroutine。

`UserInterface.on_event()` 先让 TTY 前端接受 `UiRequest`，成功后立即返回给事件消费者；非 TTY 仍串行读取。这样正在查看转录时，后续权限请求可马上进入 Modal，而不会被 `TranscriptView` 阻塞。协调器在清理活动 Modal、人工等待计时和焦点后才落定对外 future。`EventBus.join()` 不等待协调器自有的 Modal 生命周期，因此中断收束会在 join 后额外等待 `ui.wait_interactions_idle()`；UI 停止时取消活动、排队和只读请求，再退出 Textual。`/clear` 在重载 Managers、清空 Store 和创建新 Agent 前同步取消旧请求并等待交互清理，重置期间到达的新请求不会跨越 session 边界。

Textual 进程意外结束后，门面会停止向旧 App 投递消息，等待 Textual 退出并恢复终端，然后永久切换到 `PlainFrontend`。切换提示之后，`PlainHistoryJournal` 把已展示的 Rich 文本转为无样式文字、按原文输出 Markdown，并把尚未结束的流式增量作为普通文字一次性回放。日志不保存控件树或视口状态，因此输入草稿、Modal 选择/编辑状态、主历史滚动位置和转录滚动位置不会恢复。

降级时，活动和排队的作答请求按原 FIFO 顺序由文字前端从头询问，旧草稿和 Modal 内部状态不参与；活动 `TranscriptView` 直接以空结果完成。降级处理这些旧请求期间，新的 UI 事件等待文字前端就绪，避免并发读取 stdin。已经投递但尚未完成的 TUI 调用会同时监听 App Task，App 先结束时取消调用并等待降级完成，避免 future 永久悬挂。`stop()` 仍保持幂等，并恢复 VS Code 终端键盘协议。VS Code 的 debugpy 配置使用 `onTerminate: KeyboardInterrupt`，使停止调试进入应用清理流程；`AgentApp` 只把 `_work_task` 的取消视为轮次中断，外层任务取消必须向上传播到 `run()` 的 `finally`，否则 debugpy 硬杀会绕过终端恢复。

Textual 8.2.8 可能在消息循环内部捕获 fatal exception 后以 `run_async()` 正常返回，因此 UI 生命周期同时检查 asyncio Task 异常、App 捕获的 fatal、内部 `_exception`、return code、退出标志和 UI 状态。无异常但非停止流程中的返回也作为 `unexpected_return` 记录，不构造替代真实原因的异常。Textual App 只创建一次；终止记录与降级最多执行一次，不会在同一终端原位重建 App。

生产装配默认把结构化诊断写入 `$AGENT_HOME/logs/tui.jsonl`，降级提示包含本次进程的诊断 ID 和路径。日志由后台线程写入，单文件达到 2 MiB 时轮转，保留当前文件和两个备份；目录权限为 `0700`，文件权限为 `0600`。事件只包含生命周期、切换版本、渲染耗时、合并数、worker/流状态及异常类型；不写对话正文、Markdown、用户输入或工具参数。异常文本与 traceback 经共享 `DataGuard` 脱敏并限制字段长度。写入、轮转和关闭失败不能影响 TUI。

键盘由当前 Textual Screen 和逻辑焦点决定：Modal 快捷键优先；转录有焦点时处理分页、切换和 Esc；普通输入内部再按补全、Agent 列表和输入行处理。逻辑焦点只随键盘导航变化，终端窗口重新激活、Modal 切换或转录关闭时恢复到 Modal 当前选项/输入区、转录面板或主界面先前的输入框/Agent 列表。处理态的主输入框保持只读但继续承接键盘焦点，并显示 Textual 原生常亮光标；字符与提交键无效，`↓` 仍可进入运行中 Agent 列表。鼠标只用于历史与转录的文本选择、复制和滚动；点击输入、选项、Agent 行或窗口空白不会切换焦点、移动输入光标、选择或提交。普通输入默认一行、按显式换行增高到八行，支持 `Shift+Enter`/`Ctrl+J` 换行；查看转录时隐藏主输入栏及其分隔线，转录或 Modal 覆盖期间隐藏整个 Agent 列表。`Ctrl+C` 在 Windows 有选区时复制，普通输入非空时清空、空时退出，处理态或 Modal 中请求中断；macOS 默认鼠标选中即复制，并保留 `Cmd+C` 显式复制。

历史区只在位于底部时锚定新输出；用户上滚后保持当前视口，回到底部恢复跟随。鼠标选择开始时把选择起点归一到历史容器，并按滚动增量补偿 Textual 8.2.8 的选择状态，使起始行滚出视口后选区仍连续。`SelectionScreen` 把选区自动滚动限制为 20 Hz，同时保留每秒 60 行的最大速度；每个滚动步只重算一次选区，并在松开鼠标或到达边界时停止定时器。动态 Markdown 重建后，布局缓存可能短暂返回已卸载的段落，选择事件边界会丢弃这类陈旧命中。`HistoryPanel` 以 `Screen.set_reactive()` 静默补偿自动滚动中的起点，手动滚动仍显式刷新一次。该逻辑依赖固定版本的 Textual 私有 `_select_state`、`_selecting`、`_auto_select_scroll_timer` 和 `_update_select()`，升级 Textual 时必须重新运行双向跨视口选择、边界停止和连续拖选测试。

macOS/Windows 原生剪贴板由单个异步 worker 串行写入；原生写入期间发生的多次复制只保留最新待处理文本，任意时刻最多运行一个 `pbcopy` 或 `clip.exe`。内部剪贴板状态仍立即更新；原生写入成功时不重复发送 OSC 52，后端不可用、禁用或最新一次原生写入失败时才回退一次 OSC 52。Linux 保持 OSC 52 行为。
