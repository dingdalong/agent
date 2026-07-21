# 事件系统与 UI

框架的所有输入与输出都通过 `EventBus` 以类型化事件流转。Agent 和 Manager 发布事件，`AgentApp` 消费事件并交给 `OutputRouter`；UI 不参与业务决策。事件定义见 `src/events/types.py` 与 `src/events/menu.py`，总线见 `src/events/bus.py`。

## EventBus

`EventBus` 由 `asyncio.Queue` 驱动，包含两层过滤：

1. `emit()` 按全局 `EventLevel` 丢弃高于当前级别的事件。
2. 每个订阅者可按事件类型集合过滤；未指定集合表示接收全部事件。

主要生产者 API：

| 方法 | 返回 |
|---|---|
| `emit(event)` | 无 |
| `request_output(content, markdown)` | 无 |
| `request_interrupt()` | 无 |
| `request_input(prompt, default, markdown)` | 用户文本 |
| `request_choice(prompt, options, default_index, markdown)` | 所选 value，空串表示取消 |
| `request_form(prompt, questions, markdown)` | `{"answers": [...], "discussion": "..."}` JSON |
| `request_choice_input(...)` | `{"choice": "...", "text": "..."}` JSON |
| `request_transcript_view(uuid)` | 恒为空串 |
| `request_permission(...)` | `yes/session/always/session_server/always_server/deny` |
| `notify_permission(...)` | 无 |

需要应答的请求统一继承 `MenuRequest`，内部 future 由 `complete/cancel/fail` 落定；无订阅者时抛 `NoEventSubscribers`。转录请求只携带 UUID（`bus.py:248`、`menu.py:171`），标题在渲染时从当前 agent 快照生成。

`request_permission`/`request_form`/`request_choice_input`/`notify_permission` 均接收可选 `caller_agent_type`/`caller_uuid`，由发起方（`tools_mgr.execute`→`permission_mgr.resolve_ask`/`notify_decision`、`ask_user`、`exit_plan_mode`）经 `caller_identity(agent)` 传入，落到事件的基类 caller 字段，供 UI 标注是哪个 agent 发起该弹窗/通知。

消费者使用 `subscribe(event_types)` 获取 async iterator，`join()` 等待队列处理完毕，`close()` 通过 sentinel 结束全部订阅。

## 事件目录

`EventLevel` 三级：`PROGRESS=1`、`DETAIL=2`、`TRACE=3`。交互、状态和 token 事件均为 PROGRESS；`ThinkingDelta` 与 `AgentStateChanged` 为 DETAIL。

`caller_agent_type`/`caller_uuid` 是 `Event` 基类的**一等属性**（`types.py`），标识发起该事件的 agent（主 Agent 为「main」，子智能体为各自类型；None 表示用户/应用发起）：所有事件——含下表菜单类与 `CompactDelta`/`PermissionNotice`——统一继承。取值口径唯一：`caller_identity(agent)`（`types.py`）。用途有二：`OutputRouter._is_background` 据 `caller_uuid` 做前台/后台分流；UI 经 `_agent_label` 统一标注是哪个 agent（工具行、回复/思考前缀、菜单 banner、`[compact]`/`[auto]`/`[deny]` 行）。下表「关键 payload」仅列各事件**特有**字段，不再重复 `caller_*`。

| 类名 | `type` | 关键 payload |
|---|---|---|
| `ResponseDelta` | `token_delta` | `content` |
| `ThinkingDelta` | `thinking_delta` | `content` |
| `CompactDelta` | `compact_delta` | `content` |
| `ToolCallStarted` | `tool_call_started` | 工具名、调用 ID、detail |
| `ToolCallCompleted` | `tool_call_completed` | 状态、耗时、结果预览 |
| `LLMCallStarted` | `llm_call_started` | 模型、`context_limit`、输入估算 |
| `LLMCallCompleted` | `llm_call_completed` | 输入/输出/cache token、速度 |
| `OutputRequested` | `output_requested` | `content`、`markdown` |
| `InterruptRequested` | `interrupt_requested` | 无 |
| `PermissionNotice` | `permission_notice` | 状态、工具名、detail |
| `AgentStateChanged` | `agent_state_changed` | agent、前后状态 |
| `SubagentLifecycle` | `subagent_lifecycle` | UUID、类型、start/end、结束 messages |
| `PermissionModeChanged` | `permission_mode_changed` | 无 payload，仅通知重读权限模式 |
| `InputMenu` | `input_menu` | prompt、default、markdown、future |
| `ChoiceMenu` | `choice_menu` | options、默认项、markdown、future |
| `ChoiceInputMenu` | `choice_input_menu` | options、descriptions、placeholder、future |
| `FormMenu` | `form_menu` | questions、markdown、future |
| `PermissionMenu` | `permission_menu` | 工具详情、建议规则、MCP server 规则、future |
| `TranscriptView` | `transcript_view` | UUID、future |

`PermissionModeChanged` 定义于 `types.py:175`。UI 经明确的 `set_permission_mode_provider()` pull 当前入口主 agent 模式（`base.py:90`），权限状态不承载 token/context。

## AgentViewStore：唯一 UI 状态源

`AgentViewStore`（`src/interfaces/agent_view_store.py:75`）由 bootstrap 创建一次，并显式注入 `InlineInterface`、`OutputRouter` 和 `AgentApp`（`bootstrap.py:37-46`、`:88-92`）。

冻结快照类型：

- `TokenUsage(input_tokens, output_tokens, cache_read_tokens)`
- `ContextUsage(used_tokens, limit_tokens)`
- `AgentSnapshot(uuid, agent_type, is_main, running, usage, context, elapsed_seconds)`
- `SessionSnapshot(usage, foreground_context)`

Store 的职责：

- `register_foreground()` 登记入口主 agent（`agent_view_store.py:111`）。
- `record(event)` 处理 usage、context、lifecycle 和转录（`:135`）。
- `flush_completed()` 把结束的子 agent 移入最多 50 项的历史（`:159`）。
- `session_snapshot()` 返回全会话 token 总量和主 agent 当前上下文（`:181`）。
- `active_agent_snapshots()` / `subagent_snapshots()` 分别服务实时面板与 `/agents`（`:205`、`:217`）。
- 转录支持连续同类流合并、最多 400 段、原始 messages 查询。
- `reset()` 原子清空前台、usage、实时项、历史与转录（`:260`）。

缺 UUID 的 usage 仍计入会话总量，但不会虚构 agent；缺 input usage 时保留上一次准确上下文；未知窗口省略百分比；结束先于开始时不会被迟到的 start 复活。历史逐出只删除可浏览视图，不回退会话累计量。

## StatusPresenter：统一指标文本

`src/interfaces/status_presenter.py` 以纯函数把快照转成 Rich `Text`。TTY 使用 span 样式，历史摘要和非 TTY 使用同一 `Text.plain`。

统一指标格式：

```text
↑输入(缓存%) ↓输出 · 上下文 used(pct%) · elapsed
```

百分比取整数，括号前无空格；context 达 80% 显示黄、达 90% 显示红；窗口未知时只显示 used。未查看转录时，主状态栏使用“入口权限模式 + 全会话 token + 主 agent context + 全会话累计有效耗时”；打开实时或历史转录后，底部状态栏改用当前 `AgentSnapshot`，与子 agent 实时行、历史行共同调用 `present_agent()`，显示该 agent 的身份、运行状态、累计 token、当前 context 与生命周期耗时。转录标题调用 `present_agent_identity()`，只显示身份和运行状态，避免与底部指标重复。快照已被历史淘汰时，各详情表面共同调用 `present_ended_agent()` 显示短 UUID 与“已结束”，不会回退到主会话信息。

主会话状态的 `elapsed` 为**全会话累计有效耗时**（已完成回合累计 `_session_elapsed_accumulated` + 本回合实时段），与会话 token 累计语义一致：跨回合只增不减，`/clear`（`controller.reload`）随会话 token 一同归零。每个回合的有效耗时 = 自然墙钟剔除纯人工等待；回合边界（`_read_input`）把本回合有效耗时并入累计，随后 `_reset_turn_status` 清零本回合起点与时钟。输入态只显示累计值（冻结，本回合段为 0），处理/弹窗态叠加本回合实时段。查看子 agent 时不使用该值，而显示其生命周期开始至当前或结束时的耗时。

单回合的「剔除人工等待」由跨层共享的 `TurnClock`（`src/interfaces/turn_clock.py`）实现，维护 `work_depth`（工具执行层 `ToolsManager.execute` 围绕叶子工具本体成对增减，委派型 `task_delegator` 与纯等待型 `ask_user` 标 `counts_as_work=False` 不计）与 `human_wait_depth`（UI 交互层 `StatusBarActions._human_interaction` 包裹三处模态弹窗——权限/选择、计划确认、ask_user 表单——成对增减）。**当且仅当 `human_wait_depth > 0 且 work_depth == 0`（整轮只在等人工、无叶子工具在算）时暂停累计**：并发多工具同轮时按最长墙钟走、不累加；某工具弹窗等待时若另有工具在后台计算则时钟继续走，仅当无人在算时才暂停。暂停期间 `_turn_elapsed` 天然与 `now` 无关，状态栏冻结显示暂停起点值（非 `0.0s`），批准后从该值继续。

## OutputRouter

`OutputRouter.dispatch()`（`src/interfaces/output_router.py:48`）始终先调用 `store.record(event)`，再决定可见性：

| 情况 | 处理 |
|---|---|
| `SubagentLifecycle` | Store 消费后不转发 |
| 非 TTY passthrough | 正文事件保持透传，Store 同时记录真实摘要数据 |
| 菜单、权限通知、显式输出 | 始终转发 |
| `CompactDelta` | 始终转发 |
| 前台 `LLMCallStarted` | 迁移已完成子 agent 后转发 |
| 后台 `LLMCallStarted` | 静默 |
| TTY 后台正文、工具、`LLMCallCompleted` | Store 记录后静默，避免结束前台 Markdown 流 |
| 其余前台事件 | 转发 UI |

Router 不持有 agent 视图、不格式化摘要，也不提供 UI 数据 provider。`AgentApp._browse_subagents()` 直接读取 Store，并用共享 Presenter 生成非 TTY 摘要和 TTY 选择标签。

## Inline UI 组件

`src/interfaces/inline_ui.py` 是薄 `UserInterface` 门面；完整实现由 `src/interfaces/inline/` 下的组合根和职责组件完成：

| 模块 | 职责 |
|---|---|
| `controller.py` | 组装 Runtime、控制器和 prompt-toolkit 布局，协调普通输入 |
| `runtime.py` | 唯一持有 Application、Buffer、Layout、future、焦点引用和 stdout 代理；`interaction()` 排他管理 future 生命周期 |
| `status_bar.py` | 活动状态、会话/查看中 agent 状态栏切换和共享 Presenter |
| `agent_panel.py` | agent 列表、转录渲染、滚动、缓存、独立覆盖层状态 |
| `menus.py` | 选择菜单、权限菜单、ChoiceInput 状态与动作 |
| `form.py` | 表单状态、渲染、导航和 JSON wire payload |
| `output.py` | TTY Rich/流式 Markdown 输出与进度事件 |
| `plain.py` | 非 TTY 输入和保证无 ANSI 的输出 |
| `keymap.py` | 全部快捷键声明与优先级判断 |

`InteractionMode`（`runtime.py:10`）只描述互斥的主流程状态：`PROCESSING/INPUT/SELECT/FORM/CHOICE_INPUT`。转录由 `AgentPanelController.viewing_uuid` 表示为独立只读覆盖层，可与 `PROCESSING` 或 `INPUT` 并存。快捷键冲突优先级固定为：

```text
转录 → 补全 → 模态组件 → agent 列表 → 普通输入
```

关键约束：

- Enter 提交；Ctrl+J/Shift+Enter 仅在 Buffer 可编辑时插入换行。
- Shift+Tab 只在无遮罩的普通输入态切权限模式；转录、补全、列表和所有模态层禁用。
- select：↑↓、1–9、Enter、Esc。
- form：左右切题、上下移行、空格/数字选择、Tab 切讨论区、Enter 确认/提交、Esc 取消。
- ChoiceInput：上下切选项/输入行、Enter/数字提交、Esc 取消。
- agent 列表 Enter 打开只读实时转录覆盖层；打开和关闭均不修改主流程 mode 或 Buffer。
- 转录标题根据终端宽度自然换行并自适应增加高度，上下各有一条全宽暗色分隔线；小窗口不会因固定单行高度裁掉滚动或退出提示。
- 覆盖层可见时 Buffer 只读，↑/↓/Esc 由转录处理；关闭后按最新主流程 mode 恢复交互。因此查看期间总控进入 `INPUT` 时，关闭面板直接回到最新输入态，耗时保持冻结。
- `/agents` 历史转录复用同一覆盖层状态，事件只传 UUID，标题每帧从 Store 当前快照生成。
- 实时和历史转录覆盖层均以 `viewing_uuid` 驱动底部状态栏切换；详情态只显示当前子 agent 快照，并隐藏入口权限模式、Shift+Tab、`Ctrl+C 中断` 与 `↓查看 agent`，关闭后恢复最新主会话状态。
- 所有通过 future 等待结果的 TTY 交互都必须在 `InlineRuntime.interaction()` 上下文中完成；上下文从 future 创建、组件初始化和等待覆盖到组件清理，期间排他持有其所有权。退出时若 future 尚未完成则取消，随后释放所有权引用。
- 非 TTY 由 `PlainFrontend` 去除 ANSI，仍保留菜单/form/ChoiceInput 的既有返回 wire shape。

## Markdown 与补全

`MarkdownStreamRenderer`（`src/interfaces/markdown_renderer.py`）只渲染已完成的 Markdown 块：`append()` 输出完整块，`flush()` 收尾，`reset()` 清空。回应和思考各有独立流，思考流叠加 dim 样式。

`SlashCommandCompleter`（`src/interfaces/completer.py`）只在输入以 `/` 开头且尚未出现空格时返回候选；命令列表由 bootstrap 注入 UI，避免 UI 反向依赖业务层。
