# Agent 运行时

本文档说明 `AgentApp` 外层 REPL、`Agent` 状态机、`RunContext` / `RunResult`、终态 LLM 错误处理、响应恢复、上下文压缩和子 agent 返回语义。LLM 分类与重试见 [llm.md](llm.md)，事件展示见 [events-and-ui.md](events-and-ui.md)。

## 1. `AgentApp` 外层 REPL

`AgentApp.run()` 先启动 UI 与事件消费者，再通过 `reset_session(source="startup")` 创建主 Agent 并输出启动 Banner，随后持续调用 `_run_agent_turn()`。Agent 内部可完成多轮输入；只有退出或上抛命令才返回给应用层。`/clear` 通过同一重建流程再次输出 Banner，`/resume` 切换会话但不输出 Banner，`/agents` 复用当前 Agent 浏览子 agent 记录。

`_consume_events()`（`app.py:121-133`）内联处理 `InterruptRequested`，其余事件统一交 `OutputRouter.dispatch()`，确保 Store 先记录再决定可见性。`_run_agent_turn()`（`app.py:135-156`）把 `agent.run()` 包成任务，取消或键盘中断时调用 `_handle_interrupted_turn()` 收束任务与输入。

`reset_session()` 是首次启动与 `/clear` 共用的新会话生命周期。处理 `/clear` 时先检查工作目录信任；确认结束后进入 UI reset gate，取消活跃、排队和只读请求并等待窗口 runner 清理。随后拒绝新的 `UiRequest`，用 `EventBus.join()` 收束已投递事件并保存源 `SessionState`，生成新 `session_id` 和空状态；`/clear` 再更新信任状态并重载配置、角色、Hook、模型与 MCP。最后重置 `AgentViewStore`，从激活角色 manifest 构造新主 Agent，替换 UI 会话状态，运行 `SessionStart` Hook，输出使用重载后模型、角色、Plan 状态和工作目录的启动 Banner，并在重新开放 gate 前完成最终事件 drain。重建失败时不输出 Banner。

`resume_session()` 使用同一组 UI/EventBus gate：保存源状态后绑定目标 `.state.json`，清空旧 metrics 和瞬态 UI，按目标 session 重建任务与 Plan，并从隐藏的 `subagent` view 投影恢复 `/agents` 只读记录，再从其他 `SessionRecord.view` 水合聊天。旧子 agent 不会继续运行。`/resume` 是 app 层命令，不能在 Agent 状态机内直接替换共享状态。

主会话只有一个状态权威写入点 `SessionState`（`src/mgr/session_state.py`）：

```text
SessionState.records
    ├─ context_ids → Agent.history / LLM 输入
    ├─ view         → TUI 历史
    └─ recallable raw_input → 输入回溯
```

用户记录同时保存原始输入和注入 hook/reminder 后的模型消息；assistant 以 LLM `call_id` 合并流与最终消息，工具以 `tool_call_id` 合并展示和 tool message。compact 只替换 `context_ids`，不会删除已有可见记录。子 Agent 仍使用独立的纯内存 history。

## 2. 状态枚举与流转

`AgentState` 定义在 `src/agent/states.py:42-56`：

| 状态 | 职责 |
|---|---|
| `REQUEST_INPUT` | 读取用户输入、命令和 UserPromptSubmit hook |
| `CHECK_COMPACT` | 构建提示词、估算完整输入、检查压缩进展 |
| `COMPACT` | 执行一次自动压缩并返回复检 |
| `LLM_CALL` | 调用统一 LLM 模板方法 |
| `PROCESS_RESPONSE` | 分流普通、工具、长度和协议续接终态 |
| `LENGTH_RETRY` | 保存可用文本、丢弃半截工具调用并请求恢复 |
| `PAUSE_TURN` | 回填 Anthropic 原始响应载体并按协议自动续接 |
| `EXECUTE_TOOLS` | 并行执行本轮工具调用 |
| `POST_ROUND` | 注入 reminder，处理手动压缩 |
| `CHECK_STOP` | 执行 Stop hook |
| `SUMMARIZE_EXIT` | 压缩无法继续时生成退出总结 |
| `CONTEXT_OVERFLOW` | 生成上下文限制的安全终态文本 |
| `LLM_FAILURE` | 生成其他 LLM 终态错误的安全终态文本 |
| `DONE` | 单轮状态机终态 |

主流程：

```text
REQUEST_INPUT
    ↓
CHECK_COMPACT ──需要压缩──→ COMPACT ──┐
    │                                 │
    └─────────────────────────────────┘
    ↓
LLM_CALL → PROCESS_RESPONSE ──length────→ LENGTH_RETRY ──可恢复──→ LLM_CALL
               │                              └─耗尽──→ LLM_FAILURE → DONE
               ├─pause_turn──→ PAUSE_TURN ───可恢复──→ LLM_CALL
               │                              └─耗尽──→ LLM_FAILURE → DONE
               ├─tool_calls──→ EXECUTE_TOOLS → POST_ROUND → CHECK_COMPACT
               └─普通结束──→ CHECK_STOP ──Stop hook 阻断──→ CHECK_COMPACT
                                      └─放行──→ DONE
```

`LENGTH_RETRY → LLM_CALL` 有两类回边：正文/工具截断走续写（保留残片 + 追加续写指令），思考/未知截断走 discard-regenerate（丢弃整条响应、不改历史，降档或加压缩指令后重生成）。详见 §6。

所有 handler 还有统一失败边：

```text
任一 handler 抛 LLMCallError
    ├─ context_limit → CONTEXT_OVERFLOW → DONE
    └─ 其他类别      → LLM_FAILURE      → DONE
```

因此自动/手动 compact、退出总结和普通对话调用都遵循同一错误收口，不在各 handler 内复制异常判断。

## 3. `RunContext` 与 `RunResult`

`RunContext`（`src/agent/states.py:59-120`）保存单轮全部可变状态：

| 字段组 | 字段 |
|---|---|
| 消息与输出 | `messages`、`prompt`、`final_text`、`response` |
| 轮次回滚 | `turn_start_messages`（追加本轮 user 前的浅快照）、`round_start_idx`（无快照上下文的兼容回退） |
| 轮次与工具 | `has_tool_calls`、`manual_compact`、`compact_focus` |
| 自动压缩 | `compact_streak`、`max_compact_streak=3`、压缩前 token、摘要消息数、摘要是否非空 |
| 响应恢复 | `length_recoveries`、`max_length_recoveries=3`、`response_recovery_start_idx`、`response_recovery_response_count`、`pause_turn_message_idx`、`pause_turn_continuations`、`length_effort_override`（思考截断重生成时的临时降档 effort）、`length_ephemeral_instruction`（触底时一次性压缩指令，永不落历史） |
| 交互终态 | `user_input`、`user_record_id`、`command`、`exit_requested`、`stop_hook_used` |
| LLM 终态 | `llm_error: LLMErrorInfo | None` |

`RunResult` 返回 `final_text`、`command`、`exit_requested`、`user_input` 和 `llm_error`。调用方无需从错误文本反向推断类别。`/plan`、`/models` 在 Agent 内处理；`/clear`、`/resume` 与 `/agents` 通过 `command` 交给应用层。`/models` 原地替换当前 Agent 的 provider、推理强度、压缩器和提示词模型信息，不更换 Agent UUID、会话或消息历史。

## 4. 单点 LLM 错误收口

`_run_single_turn()`（`src/agent/agent.py:381-420`）是唯一捕获 `LLMCallError` 的状态机边界。每次执行 handler 时：

1. 成功则使用 handler 返回的下一状态；
2. 捕获 `LLMCallError` 后把 `exc.info` 写入 `ctx.llm_error`；
3. `context_limit` 转 `CONTEXT_OVERFLOW`，其余类别转 `LLM_FAILURE`；
4. 仍发出本次状态变更事件；
5. 到 `DONE` 后把结构化错误放进 `RunResult.llm_error`。

控制流取消不走 LLM 失败状态：`CancelledError` / `KeyboardInterrupt` 先回滚响应恢复链，再用 `turn_start_messages` 原地恢复本轮开始前的历史，把用户输入存入 `_pending_input` 后原样抛出；仅没有快照的兼容上下文才回退到 `round_start_idx`（`agent.py:413-420`）。因此即使 auto compact 已改写列表长度，取消也不会遗留当前 user 或协议载体。

### 分类到状态与操作建议

| 情况 | 状态 | 历史处理 |
|---|---|---|
| `context_limit` | `CONTEXT_OVERFLOW` | 保留本轮 user 消息，不追加错误 assistant |
| `finish_reason="length"`，正文/工具阶段截断 | `LENGTH_RETRY` | 续写路径：保存实际收到的 assistant 文本（半截工具调用不保存），追加续写指令 |
| `finish_reason="length"`，思考/未知阶段截断 | `LENGTH_RETRY` | 重生成路径：丢弃整条不完整响应，不追加任何消息，降低推理力度或加压缩指令后重试 |
| Anthropic 正常返回 `finish_reason="pause_turn"` | `PAUSE_TURN` | 回填 provider 原始 blocks；连续 pause 只保留最新载体 |
| 长度恢复耗尽 | `LLM_FAILURE`，`llm_error.kind=output_limit` | 回滚本轮截断正文和续写指令，只保留本轮原始 user 与此前合法历史 |
| pause 协议续接耗尽 | `LLM_FAILURE`，`llm_error.kind=output_limit` | 回滚整个混合恢复链，不切换模型 |
| `content_policy`、认证、权限、额度、请求、网络、服务、协议与未知错误 | `LLM_FAILURE` | 保留本轮 user 消息，不采用失败尝试的正文，不追加错误 assistant |

`_on_context_overflow()` 与 `_on_llm_failure()` 只设置 `ctx.final_text`，不改写 `ctx.messages`（`agent.py:1130-1166`）。`_format_llm_failure_text()` 按错误类别生成安全摘要和可操作建议：上下文/输出要求缩小范围，内容政策要求调整内容，认证/权限/额度要求检查凭据、权限或额度，瞬时错误建议稍后重试（`agent.py:40-97`）。

失败不会终止主 REPL。主 Agent 的当前单轮到 `DONE` 后，`Agent.run(input=None)` 先等待 EventBus 完成流式归并，再原子保存 `SessionState` 并继续下一次 `REQUEST_INPUT`；用户可在保留原请求的会话中继续输入。

## 5. 主要 handler

handler 映射在 `Agent.__post_init__` 建立（`src/agent/agent.py:236-250`）。

### 输入与命令

`_on_request_input()` 读取输入；处理 `exit` / `quit`；分派 `/plan`、`/clear`、`/agents`、`/resume`、`/models`；运行 `UserPromptSubmit` Hook；注入 turn-start reminder；最后保存 `turn_start_messages` 并追加 user 消息。

### 压缩检查

`_on_check_compact()`（`agent.py:705-792`）构建当前系统提示词，并在线程中按 provider 的真实请求形态估算 `messages + prompt + tools`。超阈值转 `COMPACT`；压缩后立即复检：摘要消息数为 0、摘要为空、token 未下降都视为无进展并转 `SUMMARIZE_EXIT`；token 下降但连续三次仍超阈值也转退出总结。

`_on_compact()`（`agent.py:794-817`）发 `CompactDelta`，调用当前 Agent 独占的 `CompactMgr`，替换历史并保存压缩进展信号。LLM 摘要调用携当前 Agent 的类型和 UUID，事件仍归属正确调用方。

### LLM 调用与正常响应

`_on_llm_call()`（`agent.py:819-840`）先 `normalize_messages()`，再调用 `llm.chat()`，传递 prompt、工具 schema、agent 身份与思考开关。它不捕获 LLM 异常。

`_on_process_response()`（`agent.py:842-870`）在普通调用中用当前正文替换 `final_text`（包括空串，避免旧工具前言残留），在已建立恢复 checkpoint 时只累加非空正文；`length` 与 `pause_turn` 分别转专用恢复状态。普通终态或完整工具调用会清空恢复状态、追加真实 `assistant_message`，再转 `EXECUTE_TOOLS` 或 `CHECK_STOP`。因此恢复链能给 `RunResult` 与 Stop hook 完整拼接正文，后续普通工具轮仍只返回最后一份完整回答。

### 工具与 Stop hook

`_on_execute_tools()`（`agent.py:991-1051`）用 `asyncio.gather` 并行执行同一回复的所有工具调用，结果按原顺序追加为 tool 消息。禁用/未知工具与执行异常都转换为对应工具结果文本。`POST_ROUND` 注入提醒；如本轮调用 `compact` 工具，则执行带 focus 的手动压缩，再回 `CHECK_COMPACT`（`agent.py:1073-1093`）。

`_on_check_stop()`（`agent.py:1053-1071`）只允许 Stop hook 阻断一次；阻断时追加 reminder user 消息并回 `CHECK_COMPACT`，否则结束本轮。

## 6. 响应恢复链

`length` 与 Anthropic `pause_turn` 共用 `response_recovery_start_idx`，checkpoint 在第一份待恢复响应写入历史前建立，不依赖可能被 compact 改写的 `round_start_idx`。`response_recovery_response_count` 只统计当前链成功返回的恢复响应，供合成的 `LLMCallFailed.attempts` 使用；provider 请求内部的网络重试仍由 `LLMProvider.chat()` 独立计数（`src/agent/states.py:79-113`、`src/agent/agent.py:872-989`）。

### `length`

`_on_length_retry()`（`src/agent/agent.py` 的 `_on_length_retry`）处理 provider 已合法返回的 `finish_reason="length"`。每趟按 `response.truncation_kind`（`LLMProvider.chat()` 在 length 终态下用 `classify_truncation` 计算，Agent 侧再用 `classify_truncation(response)` 兜底）分四类，优先级 **工具 → 正文 → 思考 → 未知**：

- **正文截断（CONTENT）**：保存真实 assistant 消息，追加“从中断处继续”的 user 指令，经归一化后再调 LLM。
- **工具调用截断（TOOL_CALL）**：不执行、不保存半截调用 ID、名称、参数或 provider 原始工具载体；仅保存非空正文为纯文本 assistant，再要求模型生成完整且更小的工具调用。
- **思考/未知截断（THINKING / UNKNOWN）**：模型在推理阶段就耗尽输出预算（正文为空、无工具调用，仅半截 reasoning，或全空）。**丢弃整条不完整响应、不向 `ctx.messages` 追加任何内容**，改为按调用临时降低推理力度重生成：`self.llm.next_lower_effort()` 有更低档位时写入 `ctx.length_effort_override`（strategy `regenerate-lower-effort`）；无更低档位时改用一次性压缩指令 `ctx.length_ephemeral_instruction`（strategy `regenerate-compress`）。因不追加消息，checkpoint 使回滚成为 no-op，历史全程干净。降档/压缩瞬态跨恢复腿持续，只在干净终态由 `_on_process_response` 复位为 None。
- 从 pause 转入 length 时清除 `pause_turn_message_idx`，停止替换旧 pause 载体，但保留整条恢复链 checkpoint。

四类都在分支末尾发出 `LLMLengthRetrying` 进度事件（携 `truncation_kind`、`strategy`、`effort`、`attempt`/`max_attempts`），供 UI 标记与 Store 转录隔断。`length_recoveries` 以 `max_length_recoveries`（=3）封顶，耗尽时经 `_fail_response_recovery` 产出 `output_limit`；思考/未知阶段用思考专属失败文案（`_length_failure_message`），提示降低推理力度后仍无法完成、建议缩小任务或改用输出上限更高的模型。

### `pause_turn`

`_on_pause_turn()`（`src/agent/agent.py:937-989`）从 provider 查询 `protocol_continuation_limit("pause_turn")`。未达上限时不添加合成 user，而是把 Anthropic `assistant_message` 中的原始 blocks 回填历史，以完全相同的 prompt、messages 和 tools 发起下一次独立调用。连续 pause 用最新载体替换上一份；若中间经过 length，则新 pause 追加新载体。归一化后会重新校验实际载体位置，避免空载体被删除后留下越界索引。

默认上限由 Anthropic provider 配置决定。达到上限时不切换模型，统一生成非重试 `output_limit`；网络、超时等错误仍只消耗 provider 自己的重试次数。

### 收口与回滚

普通终态或完整工具调用会清 checkpoint、响应计数和 pause 状态并提交历史。恢复耗尽由 `_fail_response_recovery()` 统一构造一次安全 `LLMCallFailed`；续接调用抛出的终态 `LLMCallError` 则已由 provider 发出失败事件，Agent 只调用 `_rollback_response_recovery()`，不会重复遥测（`src/agent/agent.py:381-486`）。两条路径都会删除 checkpoint 之后的全部 length/pause 临时消息，保留原始 user 与此前合法历史。

恢复期间已显示的正文是真实模型输出，不是错误占位消息；若最终失败，这些残片只保留在 UI/Store 转录中。取消或键盘中断还会用本轮开始快照恢复 compact 前历史，防止重复 user 与孤立载体（`src/agent/agent.py:413-437`）。

## 7. 退出总结与 compact 错误

`_on_summarize_exit()`（`src/agent/agent.py:1095-1128`）在自动压缩无进展或连续压缩仍超阈值时构造一份临时 `summary_messages`，以 `tools=[]`、`enable_thinking=False` 调用同一 `llm.chat()`。成功时才把临时消息和真实 assistant 总结写回历史。

如果 compact 的内部摘要调用或退出总结调用失败，`LLMCallError` 都由 `_run_single_turn()` 捕获。失败的临时总结 user 指令不会写回 `ctx.messages`；原用户历史保持可继续使用。上下文类别进入 `CONTEXT_OVERFLOW`，其他类别进入 `LLM_FAILURE`。

`CompactMgr._call_summary_request()` 透传所属 Agent 的 `caller_agent_type` / `caller_uuid`（`src/mgr/compact_mgr.py:454-469`），因此压缩调用的开始、重试、失败和流增量都进入正确 agent 的 Store 视图。

## 8. 主 Agent 与子 Agent

`Agent.from_manifest()` 映射 manifest 的身份、提示词、工具、记忆、模型、初始 Plan、思考与 feature。主 Agent 未声明 memory 时默认 `project`，子 Agent 默认不加载；`**overrides` 供委派时注入父 Agent 当前 Plan 等已解析设置。

`Agent.__post_init__()` 解析模型和 feature、过滤工具，创建带调用方身份的 `CompactMgr`，再按 feature 创建 `FileMgr`、`SkillMgr`、`SubAgentMgr`、`TaskManager`，并构造 `PromptMgr`、`ReminderMgr` 与 handler 表。

`Agent.run()` 有两种模式（`agent.py:342-379`）：

- 主 Agent `input=None`：持续多轮 REPL；终态 LLM 错误结束当前轮但不退出会话。
- 子 Agent `input` 非空：从 `CHECK_COMPACT` 执行一个任务，立即返回含 `llm_error` 的 `RunResult`。

`SubAgentMgr.task_delegator()` 检查 `run_result.llm_error`；子 agent 以 LLM 终态失败返回时，关联 task 回滚为无 owner 的 `pending`，与异常/取消路径一致，最终错误文本仍返回父 Agent（`src/mgr/subagent_mgr.py:197-205`）。生命周期 end 事件照常携当时的真实 history，便于 `/agents` 诊断。

## 9. Plan 状态协调

`PlanModeController` 只作用于入口主 Agent。Shift+Tab 调用 `toggle()`，直接翻转 `agent.plan_active`、刷新 UI 并发布 `PlanStateChanged`。切换不重建工具 schema，退出 Plan 也不清除活动计划路径。子 Agent 构造时继承父 Agent 当前 Plan 状态；调用时安全边界由 `PermissionManager.authorize()` 独立执行。
