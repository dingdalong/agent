# Agent 运行时

本文档详细说明应用主循环 `AgentApp`、Agent 状态机的全部状态、每轮的可变上下文 `RunContext`、逐个状态处理器（handler）的行为、边缘状态、`Agent.from_manifest` 工厂与 `PermissionModeController`。

相关文档：
- 四层架构与装配流程见 [architecture.md](./architecture.md)。
- 权限模式与 plan 模式细节见 [permissions.md](./permissions.md)。
- 上下文压缩 `CompactMgr` 见 [managers.md](./managers.md)。
- 事件与 UI 见 [events-and-ui.md](./events-and-ui.md)。

---

## 1. `AgentApp` 外层 REPL

`AgentApp`（`src/app/app.py:20-28`）是一个 dataclass，持有 `deps: AgentDeps`、`agent_view_store` 与 `output_router`，管理外层 REPL 与会话生命周期。

### `run()` — 主 REPL 循环（`app.py:30-76`）

1. `await self.deps.ui.start()` 启动 UI。
2. `asyncio.create_task(self._consume_events())` 创建事件消费任务，并 `await asyncio.sleep(0)` 让其先运行一步。
3. `await self.deps.event_bus.request_output(self._startup_banner())` 打印启动横幅（含 model、role、permission mode、workdir、快捷键提示，见 `app.py:277-304`）；role 使用 `RoleMgr` 实际激活的角色名与清单描述，无可用角色时显示 `unavailable`。
4. `agent = await self._reset_session(source="startup")` 初始化会话与主 agent。
5. `while True` 循环调用 `_run_agent_turn(agent)`，按返回的 `RunResult` 处理：
   - `result is None` → 被中断，`continue` 继续下一轮（`app.py:45-47`）。
   - `result.exit_requested` → `break` 退出循环（`app.py:48-49`）。
   - `result.command[0] == "clear"` → 调用 `_reset_session(source="clear")` 重建 agent，输出"上下文已清理"，`continue`（`app.py:50-55`）。
   - `result.command[0] == "agents"` → 从 Store 浏览子 agent 快照与只读转录（`app.py:56-59`）。
6. `finally` 收尾：运行 `SessionEnd` hook、关闭 `event_bus`、取消消费任务、`ui.stop()`（`app.py:60-76`）。

### `_consume_events()`（`app.py:120-132`）

订阅 `event_bus.subscribe()` 异步迭代事件。`InterruptRequested` 事件**内联处理**（调用 `_handle_interrupt_requested`），其余事件统一交 `output_router.dispatch(event)` 先写 Store 再分流。

### `_run_agent_turn()`（`app.py:134-155`）

把 `agent.run()` 包成 task 存入 `self._work_task`，在 `ui.watch_interrupt(self._request_interrupt)` 上下文中 `await` 它：

- 正常完成返回 `RunResult`。
- 捕获 `asyncio.CancelledError` / `KeyboardInterrupt` → 调用 `_handle_interrupted_turn()` 并返回 `None`（表示被中断）。
- `finally` 清空 `_work_task`。

`_handle_interrupted_turn()`（`app.py:234-249`）先消化当前 task 的 cancelling 状态（`uncancel`），取消工作任务与活跃输入，等待工作任务收束，输出"已中断当前任务"。

### `_reset_session()`（`app.py:182-220`）

会话重置流程：

1. 生成新 `session_id = str(uuid.uuid4())`（`app.py:198`），使新 agent 的 `TaskManager` 指向空目录（旧任务留在磁盘可 `/resume` 找回）。
2. 遍历有状态 Manager 逐个 `reload()`（列表见 [architecture.md](./architecture.md) 第 5 节，`app.py:200-205`）。
3. `AgentViewStore.reset()` 原子清空会话展示状态，再清空 `session_context`（`app.py:206-207`）。
4. `_install_permission_mode_controller()` 创建 `PermissionModeController` 并注入 `deps.permission_mode_controller`（`app.py:208-209`）。
5. `Agent.from_manifest(...)` 以激活角色 manifest 构造主 agent（`is_subagent=False`，`app.py:210-214`）。
6. `AgentViewStore.register_foreground(...)` 登记新主 agent，再安装权限快捷键并通知重绘（`app.py:215-218`）。
7. `_run_session_start_hooks(source)` 运行 `SessionStart` hook，其附加上下文追加到 `session_context`（`app.py:219`、`app.py:260-275`）。

### `shutdown()`（`app.py:251-258`）

断开 MCP server 连接（`mcp_mgr.stop()`）。与 `create_app()` 中的 `mcp_mgr.start()` 同处 `main` 任务，由 `main.py:41-42` 的 `finally` 保证调用。

---

## 2. `AgentState` 全枚举

状态枚举定义在 `src/agent/states.py:39-51`。

| 状态 | 值 | 职责 |
|---|---|---|
| `REQUEST_INPUT` | `request_input` | 收集用户输入、解析斜杠命令、运行 UserPromptSubmit hook |
| `CHECK_COMPACT` | `check_compact` | 构建系统提示词、估算完整 provider 输入、判断压缩进展 |
| `COMPACT` | `compact` | 执行一次自动压缩，记录结果并立即返回复检 |
| `LLM_CALL` | `llm_call` | 调用 LLM，捕获上下文超长错误 |
| `PROCESS_RESPONSE` | `process_response` | 处理响应：length 截断 / 工具调用 / 结束 |
| `LENGTH_RETRY` | `length_retry` | 文本截断时续写；工具调用截断时丢弃半截调用并要求重新生成 |
| `EXECUTE_TOOLS` | `execute_tools` | 并行执行本轮所有工具调用 |
| `CHECK_STOP` | `check_stop` | 运行 Stop hook，决定是否放行结束 |
| `POST_ROUND` | `post_round` | 注入 reminder、执行手动 compact |
| `SUMMARIZE_EXIT` | `summarize_exit` | 压缩连续失败后，退出前做一次总结 |
| `CONTEXT_OVERFLOW` | `context_overflow` | LLM 报上下文超长，产出错误提示后退出 |
| `DONE` | `done` | 单轮终态 |

### Happy path 流转图

```
REQUEST_INPUT
     │
     ▼
CHECK_COMPACT ──需压缩──▶ COMPACT ──┐
     │                              │
     │◀─────────────────────────────┘
     ▼
  LLM_CALL
     │
     ▼
PROCESS_RESPONSE ──length──▶ LENGTH_RETRY ──▶ LLM_CALL
     │                                   （上限后 → DONE）
     │ 有 tool_calls
     ▼
EXECUTE_TOOLS ──▶ POST_ROUND ──▶ CHECK_COMPACT （循环下一轮）
     │
     │ 无 tool_calls（PROCESS_RESPONSE 分支）
     ▼
 CHECK_STOP ──Stop hook 阻断──▶ CHECK_COMPACT
     │
     ▼
   DONE
```

即文字描述：`REQUEST_INPUT → CHECK_COMPACT → [COMPACT → CHECK_COMPACT → …] → LLM_CALL → PROCESS_RESPONSE → [EXECUTE_TOOLS → POST_ROUND → CHECK_COMPACT] → CHECK_STOP → DONE`。

---

## 3. `RunContext` 与 `RunResult`

### `RunContext`（`states.py:54-100`）

一次 `Agent.run()` 全部可变状态的载体。每轮新建一个实例，避免异步/多轮状态串扰。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `messages` | `list[dict]` | 必填 | 对话消息列表（指向 `self.history`） |
| `prompt` | `list[dict] \| None` | `None` | 系统提示词，`CHECK_COMPACT` 中由 `PromptMgr.build()` 填充 |
| `final_text` | `str` | `""` | LLM 最终输出文本 |
| `has_tool_calls` | `bool` | `False` | 本轮是否发生过工具调用 |
| `round_start_idx` | `int` | `0` | 本轮在 history 中的起始下标（中断时回滚用） |
| `compact_streak` | `int` | `0` | 连续“token 实际下降但仍超阈值”的自动压缩计数 |
| `max_compact_streak` | `int` | `3` | 连续有效压缩保护上限，第三次仍超阈值时转 `SUMMARIZE_EXIT` |
| `auto_compact_before_tokens` | `int \| None` | `None` | 待复检的自动压缩前完整输入 token 估算 |
| `auto_compact_summarized_message_count` | `int \| None` | `None` | 最近一次自动压缩返回的摘要消息数 |
| `auto_compact_has_summary` | `bool \| None` | `None` | 最近一次自动压缩是否返回非空摘要 |
| `stop_hook_used` | `bool` | `False` | Stop hook 是否已阻断过一次（防止无限阻断） |
| `length_recoveries` | `int` | `0` | 长度截断续写计数 |
| `max_length_recoveries` | `int` | `3` | 续写上限 |
| `response` | `LLMResponse \| None` | `None` | 最近一次 LLM 响应 |
| `manual_compact` | `bool` | `False` | 本轮是否调用了 `compact` 工具 |
| `compact_focus` | `str \| None` | `None` | 手动压缩的 focus 参数 |
| `user_input` | `str` | `""` | 用户本轮原始输入 |
| `command` | `tuple[str, list[str]] \| None` | `None` | 需 app 层处理的斜杠命令（仅 `/clear`） |
| `exit_requested` | `bool` | `False` | 是否请求退出 |

### `RunResult`（`states.py:103-119`）

`Agent.run()` 返回值。`/plan`、`/mode`、`/resume` 在 agent 内部处理，不进入 `command`；仅 `/clear` 通过 `command` 传给 app 层。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `final_text` | `str` | `""` | LLM 最终输出文本 |
| `command` | `tuple[str, list[str]] \| None` | `None` | 需 app 层处理的斜杠命令（仅 `/clear`） |
| `exit_requested` | `bool` | `False` | 用户请求退出（输入 exit/quit 或输入被取消） |
| `user_input` | `str` | `""` | 用户原始输入文本 |

### `SLASH_COMMANDS`（`states.py:8-14`）

斜杠命令元数据的唯一来源，供输入框自动补全，与 `agent.py` 的命令分发保持一致（仅列已实现命令）：

| 命令 | 描述 |
|---|---|
| `plan` | 进入计划模式 |
| `mode` | 切换权限模式 |
| `clear` | 清空会话 |
| `resume` | 恢复历史会话 |

`parse_command(user_input)`（`states.py:17-36`）将以 `/` 开头的输入解析为 `(命令名小写, 参数列表)`，否则返回 `None`。

---

## 4. 逐个 handler 行为

状态到方法的映射由 `_handlers` dict 建立（`agent.py:161-174`）。状态机循环在 `_run_single_turn`（`agent.py:300-326`）中：反复取 `_handlers[state](ctx)` 得到下一状态，直至 `DONE`；每次转移 `emit` 一个 `AgentStateChanged` 事件。捕获 `CancelledError`/`KeyboardInterrupt` 时回滚 `history[round_start_idx:]` 并保存 `_pending_input` 后重新抛出。

| 状态 | 方法 | 关键行为与转移 |
|---|---|---|
| `REQUEST_INPUT` | `_on_request_input`（`agent.py:330-407`） | 见下方详解 |
| `CHECK_COMPACT` | `_on_check_compact`（`agent.py:544-631`） | 见下方详解 |
| `COMPACT` | `_on_compact`（`agent.py:633-656`） | emit `CompactDelta("auto compact")`，调用 `compact_history` 替换 `messages`，保存结果信号后转回 `CHECK_COMPACT` |
| `LLM_CALL` | `_on_llm_call`（`agent.py:658-673`） | `normalize_messages` 后 `llm.chat(...)`；捕获 `is_context_too_long_error` 转 `CONTEXT_OVERFLOW`，否则转 `PROCESS_RESPONSE` |
| `PROCESS_RESPONSE` | `_on_process_response`（`agent.py:675-687`） | `finish_reason == "length"` → `LENGTH_RETRY`；有 `tool_calls` → `EXECUTE_TOOLS`；否则 → `CHECK_STOP` |
| `LENGTH_RETRY` | `_on_length_retry`（`agent.py:689-736`） | 见下方"边缘状态" |
| `EXECUTE_TOOLS` | `_on_execute_tools`（`agent.py:738-798`） | 见下方详解 |
| `CHECK_STOP` | `_on_check_stop`（`agent.py:800-818`） | 见下方详解 |
| `POST_ROUND` | `_on_post_round`（`agent.py:820-840`） | 见下方详解 |
| `SUMMARIZE_EXIT` | `_on_summarize_exit`（`agent.py:842-864`） | 见下方"边缘状态" |
| `CONTEXT_OVERFLOW` | `_on_context_overflow`（`agent.py:866-869`） | 见下方"边缘状态" |

### `_on_request_input`（`agent.py:330-407`）

1. `event_bus.request_input("\n\n你: ", default=self._pending_input)` 收集输入；被取消/无订阅者时置 `exit_requested=True` 转 `DONE`（`agent.py:338-349`）。
2. 输入 `exit`/`quit`（小写）→ `exit_requested=True`，转 `DONE`（`agent.py:354-356`）。
3. `parse_command` 解析斜杠命令并分派（`agent.py:358-378`）：
   - `/plan` → `_handle_plan_command()` 进入计划模式，回到 `REQUEST_INPUT`。
   - `/mode` → `_handle_mode_command()`（委托 `PermissionModeController.prompt_selection`），回到 `REQUEST_INPUT`。
   - `/clear` → 写入 `ctx.command`，转 `DONE`（交 app 层处理）。
   - `/resume` → `_handle_resume_command(args)`，回到 `REQUEST_INPUT`。
   - 未知命令 → 输出提示，回到 `REQUEST_INPUT`。
4. 运行 `UserPromptSubmit` hook（`agent.py:380-396`）：`blocked` 则输出原因回到 `REQUEST_INPUT`；`additional_context` 追加到 `user_input`。
5. `build_turn_start_instructions(permission_mode)` 前缀注入（`agent.py:398-400`），记录 `round_start_idx`，追加 user 消息，转 `CHECK_COMPACT`。

### `_on_check_compact`（`agent.py:544-631`）

`prompt = PromptMgr.build()`，再在线程中按实际 provider 请求形态估算 `messages + prompt + tools`。首次超阈值时把估算写入 `auto_compact_before_tokens` 并转 `COMPACT`；`COMPACT` 完成后立即回到本状态重新估算。

复检时，摘要消息数为 0、摘要为空或 `after_tokens >= before_tokens` 都表示没有有效进展，记录 agent 类型、前后 token 和原因后立即转 `SUMMARIZE_EXIT`。只有 token 实际下降但仍超阈值才增加 `compact_streak`；第三次连续有效压缩后仍超阈值时记录“连续 3 次有效 compact 后仍需压缩”并退出总结。降到阈值内则清零 streak 后转 `LLM_CALL`。`context_limit <= 0` 换算出的阈值非正，自动压缩禁用。

### `_on_execute_tools`（`agent.py:738-798`）

- 置 `has_tool_calls=True`，重置 `manual_compact`/`compact_focus`。
- 内嵌 `_run_one(tc)` 对每个工具调用做校验与执行：被排除工具返回错误文本（`tool_name=None`）；未知工具返回错误；解析 `arguments`，若工具名为 `compact` 则特判置 `manual_compact=True` 并记录 `focus`（`agent.py:769-775`）；调用 `tools_mgr.execute(...)`，异常包成错误文本。
- **`asyncio.gather` 并行执行**同一轮所有工具调用（`agent.py:787`），结果按原始顺序追加为 `role: tool` 消息。
- `reminder_mgr.notify_tool_round(called_tools)`，转 `POST_ROUND`。

### `_on_check_stop`（`agent.py:800-818`）

若有 `hooks_mgr` 且本轮尚未用过 Stop hook：运行 `Stop` hook。若 `blocked` → 置 `stop_hook_used=True`，追加 `<reminder>{reason}</reminder>` user 消息，转回 `CHECK_COMPACT`（让 LLM 继续）。否则转 `DONE`。

### `_on_post_round`（`agent.py:820-840`）

- 收集 `reminder_mgr.collect_post_round_messages(permission_mode)` 追加到 `messages`。
- 若 `ctx.manual_compact`（本轮调用了 `compact` 工具）→ emit `CompactDelta("llm manual")`，用 `focus` 执行与自动压缩相同的切分/摘要流水线并替换 `messages`（`agent.py:824-838`）；无待摘要消息时原历史不变，且不写入自动压缩进展标记。
- 转 `CHECK_COMPACT`（进入下一轮）。

---

## 5. 边缘状态

### `LENGTH_RETRY`（`_on_length_retry`，`agent.py:689-736`）

响应因 `finish_reason == "length"` 被截断时进入，并按响应是否携带工具调用分流：

- 普通文本截断保留完整的 provider assistant 消息，追加“从中断处直接继续，不要回顾、不要重复”的 user 指令后重试，原有续写语义不变（`agent.py:708-712,733-735`）。
- 工具调用截断绝不执行或保存半截调用，只把非空 `content` 重建为纯文本 assistant；`tool_calls`、调用 ID、参数、推理字段和 provider 原始调用载体均不进入历史。随后明确告知模型该调用已丢弃且未执行，要求重新生成完整调用；长参数拆成较小调用，写大文件使用分块能力（`agent.py:699-707,726-735`）。

若 `length_recoveries >= max_length_recoveries`（达到 3 次恢复上限），追加不含工具调用的错误 assistant 并转 `DONE`；否则计数加一，经 `normalize_messages` 后转回 `LLM_CALL`。上限来自 `RunContext.max_length_recoveries = 3`（`states.py:94`），达到上限时历史仍满足消息协议（`agent.py:714-736`）。

### `CONTEXT_OVERFLOW`（`_on_context_overflow`，`agent.py:866-869`）

`LLM_CALL` 捕获到 `llm.is_context_too_long_error(exc)` 时进入。写入固定错误文本"上下文过长，已多次压缩仍无法继续……"追加为 assistant 消息，转 `DONE`。

### `SUMMARIZE_EXIT`（`_on_summarize_exit`，`agent.py:842-864`）

自动压缩无有效进展，或连续 3 次有效压缩后仍需压缩时进入。追加一条 user 消息要求 LLM 总结（1 已完成 / 2 未完成 / 3 后续建议），以 `tools=[]`、`enable_thinking=False` 调用 `llm.chat`。若此次调用又报上下文超长 → 写入错误文本转 `DONE`；否则记录总结文本、追加 assistant 消息，转 `DONE`。上限来自 `RunContext.max_compact_streak = 3`（`states.py:88`）。

---

## 6. `Agent.from_manifest` 工厂与 `__post_init__`

### `from_manifest(manifest, deps, *, is_subagent=False, **overrides)`（`agent.py:176-226`）

将 `AgentManifest` 各字段映射为 `Agent` 构造参数：`agent_type`、`description`、`role_prompt`（= `manifest.prompt`）、`tools`、`memory`（经 `_resolve_memory_scope` 解析）、`model`、`permission_mode`、`enable_thinking`（默认 `True`）、`features`。`**overrides` 允许覆盖任意字段。`manifest is None` 时创建最小回退 Agent（`agent.py:199-207`）。

`_resolve_memory_scope`（`agent.py:32-45`）：manifest 显式声明则用其值；未声明时主 agent 默认 `"project"`（加载项目记忆），子 agent 默认 `None`（不加载）。

### `__post_init__` 关键副作用（`agent.py:113-174`）

1. 生成 `uuid`。
2. 解析权限模式：`permission_mode is None` 时回退 `permission_mgr.default_mode`（缺失则全局 `DEFAULT_MODE`）（`agent.py:119-122`）。
3. `self.llm = deps.llm_mgr.get(self.model)`（`agent.py:123`）；`model="inherit"` 由 LLMMgr 解析为父 agent 真实模型 ID。
4. `resolve_features(self.features)` 解析 feature 集，`excluded_tool_names(features)` 算出 `_excluded_tools`，`refresh_tools_schemas()` 构建工具 schema（`agent.py:125-128`）。
5. 创建 `CompactMgr`：`auto_compact_size = context_limit * compact_cfg["auto_compact_rate"]`（`agent.py:129-137`）。
6. feature 门控创建 agent 级 Manager：`FileMgr`（`file`）、`SkillMgr`（`skill`）、`SubAgentMgr`（`subagent`）（`agent.py:138-148`）。
7. `PromptMgr`（`agent.py:149`）。
8. `TaskManager`（`task` feature）：主 agent 持久化到磁盘（`global_dir / "tasks" / session_id`），子 agent 为纯内存独立实例（`agent.py:151-157`）。
9. `ReminderMgr`，若有 `_task_mgr` 则注册它（`agent.py:158-160`）。
10. 构建 `_handlers` dict（`agent.py:161-174`）。

### `run(input)` 双模式（`agent.py:265-298`）

- **子 agent 模式**（`input is not None`）：从 `CHECK_COMPACT` 开始执行**单轮**，追加 user 输入（含 turn-start 指令前缀），返回后清理 reasoning 内容（`agent.py:278-288`）。
- **主 agent 模式**（`input is None`）：`while True` 交互循环，每轮从 `REQUEST_INPUT` 开始，轮末 `_persist_session(user_input)` 持久化会话历史与元数据（`agent.py:290-298`）；`exit_requested` 或有 `command` 时返回。

权限模式切换（`set_permission_mode`）与 plan 模式进入/退出逻辑见 [permissions.md](./permissions.md)（`agent.py:238-263`）。

---

## 7. `PermissionModeController`

`src/app/permission_mode_controller.py` 协调权限模式交互、UI 状态条与 agent 工具 schema 刷新。**仅作用于入口主 agent**——子 agent 权限模式构造后固定不变。

| 成员 | 源码 | 行为 |
|---|---|---|
| `install_mode_provider()` | `permission_mode_controller.py:44-58` | 向 UI 注册明确的权限模式 provider；主 agent 未绑定时回退 `default_mode` |
| `prompt_selection()` | `permission_mode_controller.py:60-86` | `/mode` 命令：以 `MENU_MODES` 构建方向键选择菜单，选中后 `agent.set_permission_mode(mode)`，变化则 `_refresh_agent()` |
| `install_shortcut(agent)` | `permission_mode_controller.py:88-98` | 绑定主 agent 并注册 Shift+Tab 轮转回调（`cycle_mode`） |
| `cycle_mode()` | `permission_mode_controller.py:108-124` | Shift+Tab：在 `CAROUSEL_MODES` 中循环切换主 agent 权限模式，变化则 `_refresh_agent()` |
| `_refresh_agent()` | `permission_mode_controller.py:126-132` | 刷新 agent 工具 schema（`refresh_tools_schemas`）并发出权限模式重绘通知 |

`CAROUSEL_MODES`（Shift+Tab 轮换集）与 `MENU_MODES`（`/mode` 菜单集）定义在 `src/mgr/permission_mgr.py`，详见 [permissions.md](./permissions.md)。控制器由 `AgentApp._install_permission_mode_controller()` 在每次 `_reset_session` 时创建并注入 `deps.permission_mode_controller`。
