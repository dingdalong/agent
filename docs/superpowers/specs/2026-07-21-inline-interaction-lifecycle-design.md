# Inline UI 交互生命周期修复设计

## 背景

组合式 Inline UI 将原单文件实现拆分到 `controller.py`、`menus.py`、`form.py` 与
`runtime.py` 后，`menus.py` 和 `form.py` 使用 `prompt_toolkit.document.Document`
却没有导入它。`exit_plan_mode` 进入计划审核界面时，`_await_choice_input()` 已创建
交互 future，随后在初始化输入缓冲时抛出 `NameError`。该异常发生在组件现有的
`try/finally` 之前，future 因而留在 `InlineRuntime` 中；下一轮普通输入再申请 future
时才出现 `RuntimeError: an Inline UI interaction is already pending`。

第二个异常不是实际并发，而是前一次初始化失败后的资源泄漏。相同的导入遗漏也会影响
表单交互。

## 目标

- 恢复计划审核与表单交互的正常初始化。
- 由 `InlineRuntime` 强制保证一个交互 future 从创建到释放的完整生命周期。
- 初始化、等待或取消阶段抛出异常时，不得留下待决 future 影响下一次交互。
- 保留真正并发交互时的明确 `RuntimeError`，不自动覆盖或静默取消已有交互。
- 让新增交互通过唯一 API 获得上述保证，不依赖调用方手写相同的 `try/finally` 约定。

## 非目标

- 不集中管理各组件的菜单选项、表单答案、焦点或缓冲内容。
- 不改变 `EventBus`、`MenuRequest` 或 `exit_plan_mode` 的返回协议。
- 不允许并发显示多个顶层 Inline UI 交互。
- 不借此重构无关的 Inline UI 渲染和按键逻辑。

## 设计

### 唯一的 future 所有权 API

`InlineRuntime` 新增同步上下文管理器 `interaction()`。它是创建交互 future 的唯一入口：

1. 进入时检查是否已有活跃交互上下文；存在时抛出当前的 `RuntimeError`。
2. 创建 future，记录到 runtime，并将它交给调用方等待。
3. 离开时仅清理本次创建的 future，不干扰可能存在的更新实例。
4. 若 future 仍未完成，先取消再释放引用，覆盖初始化异常、任务取消和等待异常。

现有 `create_input_future()` 与 `clear_settled_future()` 删除，避免新代码绕过统一生命周期。
`resolve_input()`、`fail_input()` 和 `cancel_input()` 继续负责从按键、中断或停机路径落定当前
future，但不再提前释放 runtime 的所有权引用。future 已落定、组件尚未完成清理的短暂阶段仍
属于当前交互；只有 `interaction()` 退出才能释放所有权。`pending_input_future()` 仍只返回尚未
落定的 future，供提交和中断路径判断是否还能安全落定。

### 组件职责

普通输入、选择菜单、计划审核输入、表单和转录查看的 `_await_*` 方法全部改用
`interaction()`。组件在成功取得所有权后才设置自身 mode 和交互状态，防止真正的并发申请
失败时覆盖当前界面。

每个组件仍在自己的 `finally` 中清理选项、表单、查看状态和 processing mode；runtime 的
外层上下文独立兜底 future。即使组件初始化或组件清理抛出异常，runtime 仍会释放本次 future。
这保持了职责边界：runtime 管所有权，组件管显示状态。

### 缺失依赖

在实际使用 `Document` 的 `menus.py` 和 `form.py` 中分别显式导入它。依赖跟随使用点，避免
依赖混入类的多重继承装配过程后形成隐式可见性假设。

## 数据与错误流

正常路径：

`MenuRequest` → UI 进入 `interaction()` → 初始化组件 → 等待 future → 按键落定 future
→ 组件清理 → runtime 释放所有权 → 完成 `MenuRequest`。

初始化失败路径：

`MenuRequest` → UI 进入 `interaction()` → 初始化抛异常 → 组件清理 → runtime 取消并释放
future → `MenuRequest.fail()` 收到原始异常。后续交互可以正常开始，不产生级联异常。

真正并发路径：

第二个交互进入 `interaction()` → runtime 发现已有活跃交互上下文 → 抛出明确的
`RuntimeError`；第一个交互及其界面状态不被修改。

## 测试

测试按失败优先顺序补充：

1. 计划审核 `choice_input` 从真实交互入口初始化并可提交选项，覆盖 `Document` 导入遗漏。
2. 表单从真实交互入口初始化并可取消或提交，覆盖同类遗漏。
3. 在取得交互 future 后注入初始化异常，断言异常保留且 runtime 不再持有待决 future。
4. 在一个交互待决时申请第二个交互，断言仍抛 `RuntimeError`，且原 future 保持可用；原
   future 已落定但上下文尚未退出时也必须保持相同排他性。
5. 任务取消和显式取消后，断言 runtime 所有权被释放。

先运行新增的定向测试确认它们能在当前实现上以预期原因失败；实现后运行 Inline UI 定向
测试，再运行全部测试。

## 文档影响

若 `docs/events-and-ui.md` 已描述 Inline UI 的交互 future 所有权，则将其更新为
`InlineRuntime.interaction()` 的唯一入口和排他语义；不保留旧的手工创建/清理流程说明。
