# Inline 转录独立覆盖层设计

## 问题

实时查看子 agent 转录时，当前实现把 `InteractionMode` 改为 `TRANSCRIPT`，并保存打开前的 mode、Buffer 文本和光标。若查看期间总控从处理态进入等待输入态，Esc 关闭面板仍会恢复旧的 `PROCESSING`，使已经结束的“回应中”活动行重新出现，步骤耗时和回合耗时继续增长。

根因是一个字段同时表达了两类可并存的状态：主流程交互阶段与转录面板显隐。

## 目标

- 主流程 mode 始终反映最新业务事件，不因查看转录而被覆盖。
- 转录面板作为独立只读覆盖层，单独控制显隐、滚动和快捷键抢占。
- 总控在查看期间完成后，关闭面板直接回到最新 `INPUT`，活动行隐藏且耗时冻结。
- 打开和关闭面板不修改 Buffer；原有草稿、默认输入和光标自然保留。
- 实时转录与 `/agents` 历史转录使用同一覆盖层状态。

## 非目标

- 不改变 agent 转录内容、历史保留、渲染缓存或滚动算法。
- 不改变主 agent、子 agent 的事件路由和 token 统计。
- 不改变权限菜单、表单和 ChoiceInput 的业务语义。

## 状态模型

`InteractionMode` 只保留互斥的主流程交互状态：

```text
PROCESSING | INPUT | SELECT | FORM | CHOICE_INPUT
```

转录覆盖层由 `AgentPanelController.viewing_uuid` 独立表示：

```text
None       = 覆盖层关闭
<agent id> = 覆盖层打开
```

两者正交组合。例如 `PROCESSING + viewing_uuid` 表示任务运行时查看转录，`INPUT + viewing_uuid` 表示总控已经等待输入但转录仍然可见。关闭覆盖层只清除 `viewing_uuid` 和滚动位置，不修改 `InteractionMode` 或 Buffer。

`InteractionMode.TRANSCRIPT`、`TranscriptRestore` 及其恢复快照随之删除，避免继续存在两套转录状态来源。

## 组件职责与数据流

### `runtime.py`

`InteractionMode` 删除 `TRANSCRIPT`，只描述主流程状态。

### `agent_panel.py`

`AgentPanelController` 继续拥有 `viewing_uuid`、`scroll`、`invoked` 和渲染缓存。实时打开只记录目标 UUID 并重置滚动；关闭只清除覆盖层状态。它不再接收、保存或返回 mode、Buffer 文本和光标。

### `keymap.py`

`resolve_key_scope` 仅根据 `transcript_visible` 判定转录快捷键域，不再检查 `InteractionMode.TRANSCRIPT`。实时列表 Enter 打开覆盖层但不修改 `_mode`；Esc 关闭实时覆盖层但不恢复任何旧状态。`/agents` 的 Esc 仍解析其只读查看 future，由既有 finally 收尾。

### `controller.py`

`_await_transcript_view` 打开模态历史转录时不再修改 `_mode`。当覆盖层可见时，普通输入的 `_accepting` 与 `_buffer_editable` 均为假，使 Buffer 保持只读；主流程事件仍可把 mode 从 `PROCESSING` 更新为 `INPUT`，并可写入新的默认输入。覆盖层关闭后，可编辑性立即由最新 mode 决定。

活动行继续由“主流程处于处理态、有 activity、且覆盖层关闭”共同控制。核心耗时继续由最新 mode 决定，因此总控进入 `INPUT` 后即使用 `_last_elapsed`，不会继续读取 monotonic 时钟。

## 边界与异常路径

- 查看期间子 agent 完成：面板继续显示已完成记录，关闭不影响主流程状态。
- 查看期间总控进入输入态：Buffer 暂时只读；关闭后直接可编辑，保留主流程写入的最新内容。
- `/agents` 查看：其 request future、Esc 返回列表和 finally 清理保持不变，只移除对 `_mode` 的占用。
- `/clear`、停止或取消：沿用现有清理逻辑，确保 `viewing_uuid`、滚动和待决 future 被清除。

## 测试

先增加失败回归测试，再修改实现：

1. `PROCESSING + 转录打开` 期间切换为 `INPUT`，Esc 后仍为 `INPUT`，Buffer 内容不回退。
2. 对同一状态连续渲染核心状态行，输入态显示固定 `_last_elapsed`，不随 monotonic 时间增长。
3. 打开实时转录不改变原有 `PROCESSING` 或 `INPUT` mode；关闭也不改变。
4. 覆盖层打开时 Buffer 不可编辑，关闭后按最新 mode 恢复可编辑性。
5. `/agents` 模态转录仍能用 Esc 解开 future 并返回列表。
6. 更新既有 mode 集合、快捷键优先级和实时恢复测试，随后运行 inline 组件测试及完整测试套件。

## 文档同步

实现时同步更新 `docs/events-and-ui.md`：转录属于独立覆盖层，`InteractionMode` 不再包含 `TRANSCRIPT`，关闭转录保留最新主流程状态和 Buffer。
