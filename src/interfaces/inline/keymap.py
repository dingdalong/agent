"""Pure shortcut-scope decisions for Inline UI key bindings."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings

from src.interfaces.inline.runtime import InteractionMode


class KeyScope(StrEnum):
    """Priority-resolved scope that owns a key press."""

    TRANSCRIPT = "transcript"
    COMPLETION = "completion"
    MODAL = "modal"
    AGENT_LIST = "agent_list"
    INPUT = "input"


def resolve_key_scope(
    mode: InteractionMode,
    transcript_visible: bool,
    completion_visible: bool,
    agent_list_focused: bool,
) -> KeyScope:
    """Resolve the owner of a key using the fixed interaction priority.

    Args:
        mode: Current top-level interaction mode.
        transcript_visible: Whether a read-only transcript overlays the UI.
        completion_visible: Whether slash completion is visible.
        agent_list_focused: Whether the agent list owns focus.

    Returns:
        Highest-priority active key scope.
    """
    if transcript_visible:
        return KeyScope.TRANSCRIPT
    if completion_visible:
        return KeyScope.COMPLETION
    if mode in {
        InteractionMode.SELECT,
        InteractionMode.FORM,
        InteractionMode.CHOICE_INPUT,
    }:
        return KeyScope.MODAL
    if agent_list_focused:
        return KeyScope.AGENT_LIST
    return KeyScope.INPUT


def can_insert_newline(buffer_editable: bool) -> bool:
    """Return whether the newline shortcut may mutate the buffer.

    Args:
        buffer_editable: Current read-only condition resolved by the runtime.

    Returns:
        True only for an editable buffer.
    """
    return buffer_editable


def can_toggle_permission_mode(
    mode: InteractionMode,
    transcript_visible: bool,
    completion_visible: bool,
    agent_list_focused: bool,
) -> bool:
    """Return whether Shift+Tab may rotate the permission mode.

    Args:
        mode: Current interaction mode.
        transcript_visible: Whether transcript viewing is active.
        completion_visible: Whether completion is active.
        agent_list_focused: Whether the agent list owns focus.

    Returns:
        True only for unobscured normal input.
    """
    return (
        mode is InteractionMode.INPUT
        and not transcript_visible
        and not completion_visible
        and not agent_list_focused
    )


_TRANSCRIPT_PANEL_ROWS = 12


class KeymapActions:
    """Declare Inline shortcuts in their fixed priority order."""

    def _build_key_bindings(self) -> KeyBindings:
        """构建常驻 App 的按键绑定（按交互模式门控）。

        - Enter（仅可输入态且列表未聚焦）：补全菜单已选定项时应用补全不提交，否则纯空白忽略、有文本则提交。
        - Ctrl+J / Shift+Enter：插入换行。
        - Shift+Tab：切换权限模式并重绘。
        - Ctrl+C / 外部 SIGINT：处理态请求中断；可输入态有文本则清空、空则以 KeyboardInterrupt 解开读取。
        - Ctrl+D：可输入态空缓冲→以 EOFError 解开读取。
        - select 态（选择菜单）：↑↓ 移动选中、1-9 数字直选、Enter 确认、Esc 取消。
        - 补全态（斜杠命令下拉）：↓/Tab 下一项、↑ 上一项、Esc 关闭。
        - 方向键（↓↑）：从输入框进入 agent 列表 / 列表内导航 / 返回输入框（查看面板/选择菜单/补全时不进列表）。
        - 列表聚焦时 Enter：在输入框上方打开选中子 agent 的转录覆盖面板（焦点回输入框）；Esc：返回输入框。
        - 查看面板时：↑/↓ 整页上下滚动（暂停/恢复贴底实时跟随）；Esc：关闭只读覆盖层。

        Returns:
            供常驻 Application 使用的 KeyBindings。
        """
        bindings = KeyBindings()

        # 列表可见性 & 聚焦条件（lazy evaluate at key-press time）
        _cond_list_visible = Condition(lambda: self._has_sub_agents())
        _cond_list_focused = Condition(
            lambda: self._agent_list_inner is not None
            and self._app is not None
            and self._app.layout.has_focus(self._agent_list_inner)
        )
        # 转录面板可见性条件：查看子 agent 转录时为真。
        _cond_viewing = Condition(lambda: self._viewing_uuid is not None)
        # 主流程组件只在未被转录覆盖时接收快捷键。
        _cond_select = ~_cond_viewing & Condition(lambda: self._mode == "select")
        # 表单派生条件按焦点区（答题/讨论）与标签类型细分，供各表单键位专用。
        _cond_form = ~_cond_viewing & Condition(lambda: self._mode == "form")
        _cond_form_answer = _cond_form & Condition(lambda: self._form_zone == "answer")  # 答题区（←→↑↓ 导航）
        _cond_form_submit = _cond_form_answer & Condition(lambda: self._form_on_submit_tab())  # 答题区且聚焦「提交」标签
        _cond_form_opt = _cond_form_answer & Condition(lambda: self._current_form_question() is not None and self._current_form_question().options is not None and not self._form_cursor_on_custom())  # 答题区有选项题且光标在选项行（空格/数字选中）
        _cond_form_single_opt = _cond_form_answer & Condition(lambda: self._form_focused_single() and not self._form_cursor_on_custom())  # 答题区单选题且光标在选项行（Enter 选中，就地不推进）
        _cond_form_confirm = _cond_form_answer & Condition(lambda: not self._form_on_submit_tab() and not (self._form_focused_single() and not self._form_cursor_on_custom()))  # 答题区问题标签且非单选选项行（Enter 确认推进：多选题/自由文本题/单选自定义行）
        _cond_form_discuss = _cond_form & Condition(lambda: self._form_zone == "discuss")  # 底部讨论栏
        # choice_input 派生条件按光标是否在输入行细分，供各键位专用。
        _cond_choice_input = ~_cond_viewing & Condition(lambda: self._mode == "choice_input")
        _cond_choice_input_opt = _cond_choice_input & Condition(lambda: not self._choice_input_on_input_row())  # 光标在选项行（数字/Enter 直选）
        _cond_choice_input_row = _cond_choice_input & Condition(lambda: self._choice_input_on_input_row())  # 光标在输入行（Enter 提交输入）
        # 斜杠命令补全激活条件：缓冲存在补全候选时为真。
        _cond_completing = Condition(
            lambda: self._viewing_uuid is None
            and self._buffer is not None
            and self._buffer.complete_state is not None
        )
        # 输入态光标在末行：仅当缓冲区光标在末尾时 down 才能下移进列表
        _cond_cursor_at_end = Condition(
            lambda: self._buffer is not None
            and self._buffer.document.is_cursor_at_the_end
        )

        # Enter（可输入态且列表未聚焦）：补全已选定→应用补全不提交；否则关闭补全菜单并提交非空文本
        @bindings.add("enter", filter=self._cond_accepting & ~_cond_list_focused)
        def _(event) -> None:
            buf = event.current_buffer
            state = buf.complete_state
            if state is not None and state.complete_index is not None:
                buf.apply_completion(state.current_completion)
                return
            if state is not None:
                buf.cancel_completion()
            text = buf.text
            if not text.strip():
                return
            self._resolve_input(text)

        # Ctrl+J：插入换行
        @bindings.add("c-j")
        def _(event) -> None:
            if can_insert_newline(self._buffer_editable()):
                event.current_buffer.insert_text("\n")

        def insert_newline(event) -> None:
            """Insert a newline only while the shared Buffer is editable.

            Args:
                event: prompt-toolkit key event.

            Returns:
                None.
            """
            if can_insert_newline(self._buffer_editable()):
                event.current_buffer.insert_text("\n")

        self._try_add_binding(bindings, "s-enter", insert_newline)
        self._try_add_binding(bindings, "s-tab", self._handle_permission_mode_toggle)

        # Ctrl+C：中断当前
        @bindings.add("c-c")
        def _(event) -> None:
            self._interrupt_current()

        @bindings.add("c-d")
        def _(event) -> None:
            if self._accepting and not event.current_buffer.text:
                self._fail_input(EOFError())

        self._try_add_binding(bindings, "<sigint>", lambda event: self._interrupt_current())

        # ---- select 态：选择菜单导航（eager 抢占只读缓冲的默认光标/编辑绑定）----

        # ↑：上移选中项
        @bindings.add("up", filter=_cond_select, eager=True)
        def _(event) -> None:
            if self._select_index > 0:
                self._select_index -= 1

        # ↓：下移选中项
        @bindings.add("down", filter=_cond_select, eager=True)
        def _(event) -> None:
            if self._select_options and self._select_index < len(self._select_options) - 1:
                self._select_index += 1

        # Enter：以当前选中项的 value 解析应答
        @bindings.add("enter", filter=_cond_select, eager=True)
        def _(event) -> None:
            if self._select_options:
                self._resolve_input(self._select_options[self._select_index][0])

        # Esc：以取消 value 解析应答
        @bindings.add("escape", filter=_cond_select, eager=True)
        def _(event) -> None:
            self._resolve_input(self._select_cancel_value)

        # 1-9：数字直选（在范围内则选中并立即解析）
        for _digit in range(1, 10):
            @bindings.add(str(_digit), filter=_cond_select, eager=True)
            def _(event, idx: int = _digit - 1) -> None:
                if self._select_options and idx < len(self._select_options):
                    self._select_index = idx
                    self._resolve_input(self._select_options[idx][0])

        # ---- form 态：单屏标签页表单导航（eager 抢占只读缓冲的默认光标/编辑绑定）----
        # 答题区：←→ 切标签、↑↓ 移动答案行、空格/数字选中选项（多选增删、单选单选切换）、
        #         Enter 于单选选项行切换选中（就地不推进）、于其余问题标签确认推进、于「提交」标签或讨论栏整表提交、Tab 切讨论栏；
        # 讨论区：缓冲可编辑，方向键交默认光标移动，Enter 提交、Tab 返回答题区。

        # ↑：答题区上移一行（选项行/自定义输入行间）
        @bindings.add("up", filter=_cond_form_answer, eager=True)
        def _(event) -> None:
            self._move_form_row(-1)

        # ↓：答题区下移一行
        @bindings.add("down", filter=_cond_form_answer, eager=True)
        def _(event) -> None:
            self._move_form_row(1)

        # ←：答题区切到上一标签
        @bindings.add("left", filter=_cond_form_answer, eager=True)
        def _(event) -> None:
            self._move_form_focus(-1)

        # →：答题区切到下一标签
        @bindings.add("right", filter=_cond_form_answer, eager=True)
        def _(event) -> None:
            self._move_form_focus(1)

        # Tab：在答题区与底部讨论栏之间切换焦点
        @bindings.add("c-i", filter=_cond_form, eager=True)
        def _(event) -> None:
            self._toggle_form_zone()

        # 空格：答题区有选项题翻转当前光标所在选项的选中（多选增删、单选单选切换）
        @bindings.add("space", filter=_cond_form_opt, eager=True)
        def _(event) -> None:
            self._toggle_form_option()

        # 1-9：答题区聚焦有选项题时数字操作对应选项（单选直接选中、多选翻转勾选）
        for _digit in range(1, 10):
            @bindings.add(str(_digit), filter=_cond_form_opt, eager=True)
            def _(event, idx: int = _digit - 1) -> None:
                self._form_number(idx)

        # Enter：单选题选项行切换选中（就地不推进）
        @bindings.add("enter", filter=_cond_form_single_opt, eager=True)
        def _(event) -> None:
            self._toggle_form_option()

        # Enter：其余问题标签（多选题/自由文本题/单选自定义行）确认并推进到下一标签
        @bindings.add("enter", filter=_cond_form_confirm, eager=True)
        def _(event) -> None:
            self._confirm_question()

        @bindings.add("enter", filter=_cond_form_submit, eager=True)
        @bindings.add("enter", filter=_cond_form_discuss, eager=True)
        def _(event) -> None:
            self._submit_form()

        # Esc：取消整个表单
        @bindings.add("escape", filter=_cond_form, eager=True)
        def _(event) -> None:
            self._cancel_form()

        # ---- choice_input 态：选项 + 输入行导航（eager 抢占只读缓冲的默认绑定与 agent 列表 ↓）----
        # 选项行：↑↓ 在选项/输入行间移动、Enter/数字 直选并提交；
        # 输入行：缓冲可编辑，字符键交默认自插入，Enter 提交非空输入、↑ 回到选项行。

        # ↑：上移一行（选项行/输入行间）
        @bindings.add("up", filter=_cond_choice_input, eager=True)
        def _(event) -> None:
            self._move_choice_input_row(-1)

        # ↓：下移一行
        @bindings.add("down", filter=_cond_choice_input, eager=True)
        def _(event) -> None:
            self._move_choice_input_row(1)

        # Enter：光标在选项行→提交该项 value
        @bindings.add("enter", filter=_cond_choice_input_opt, eager=True)
        def _(event) -> None:
            self._submit_choice_input_option()

        # Enter：光标在输入行→提交非空输入文本
        @bindings.add("enter", filter=_cond_choice_input_row, eager=True)
        def _(event) -> None:
            self._submit_choice_input_text()

        # 1-9：光标在选项行时数字直选并提交（输入行时不拦截，交默认自插入）
        for _digit in range(1, 10):
            @bindings.add(str(_digit), filter=_cond_choice_input_opt, eager=True)
            def _(event, idx: int = _digit - 1) -> None:
                self._choice_input_number(idx)

        # Esc：取消
        @bindings.add("escape", filter=_cond_choice_input, eager=True)
        def _(event) -> None:
            self._cancel_choice_input()

        # ---- 补全态：斜杠命令下拉导航（eager 抢占，避免与 agent 列表 ↓ 冲突）----

        # ↓ / Tab：下一候选
        @bindings.add("down", filter=_cond_completing, eager=True)
        @bindings.add("c-i", filter=_cond_completing, eager=True)
        def _(event) -> None:
            event.current_buffer.complete_next()

        # ↑：上一候选
        @bindings.add("up", filter=_cond_completing, eager=True)
        def _(event) -> None:
            event.current_buffer.complete_previous()

        # Esc：关闭补全菜单
        @bindings.add("escape", filter=_cond_completing, eager=True)
        def _(event) -> None:
            event.current_buffer.cancel_completion()

        # ---- agent 列表方向键导航 ----

        # ↓（eager）：从输入框进入 agent 列表（输入态光标在末行 或 处理态只读；查看面板/选择菜单/表单/choice_input/补全时不进列表）
        @bindings.add("down", eager=True, filter=_cond_list_visible & ~_cond_list_focused & ~_cond_viewing & ~_cond_select & ~_cond_form & ~_cond_choice_input & ~_cond_completing & (self._cond_accepting & _cond_cursor_at_end | ~self._cond_accepting))
        def _(event) -> None:
            if self._agent_list_inner is not None:
                self._agent_selected_index = 0
                event.app.layout.focus(self._agent_list_inner)

        # ↑：列表聚焦时上移选中行；到头则返回输入框
        @bindings.add("up", filter=_cond_list_focused)
        def _(event) -> None:
            if self._agent_selected_index > 0:
                self._agent_selected_index -= 1
            else:
                event.app.layout.focus(self._input_window)

        # ↓：列表聚焦时下移选中行
        @bindings.add("down", filter=_cond_list_focused)
        def _(event) -> None:
            rows = self._agent_view_store.active_agent_snapshots()
            max_idx = len(rows) - 1
            if self._agent_selected_index < max_idx:
                self._agent_selected_index += 1

        # Enter：列表聚焦时在输入框上方打开选中子 agent 的只读转录覆盖层（焦点回输入框以接收覆盖层快捷键）
        @bindings.add("enter", filter=_cond_list_focused)
        def _(event) -> None:
            """Open the selected subagent transcript overlay.

            Args:
                event: prompt-toolkit key event.

            Returns:
                None.
            """
            rows = self._agent_view_store.active_agent_snapshots()
            if self._agent_selected_index >= len(rows):
                return
            row = rows[self._agent_selected_index]
            if not row.is_main:
                self._buffer.cancel_completion()
                self._agent_panel.open_live(row.uuid)
            event.app.layout.focus(self._input_window)
            event.app.invalidate()

        # Esc：列表聚焦时返回输入框
        @bindings.add("escape", filter=_cond_list_focused, eager=True)
        def _(event) -> None:
            event.app.layout.focus(self._input_window)

        # ---- 转录面板：滚动 / 关闭（输入框聚焦下仍全局响应；查看时 ↑/↓ 占用于整页滚动，Esc 关闭后恢复输入框方向键）----

        # ↑：面板上滚一整页（暂停贴底跟随）；越界由 _render_transcript_panel 就地夹取
        @bindings.add("up", filter=_cond_viewing, eager=True)
        def _(event) -> None:
            self._view_scroll += _TRANSCRIPT_PANEL_ROWS - 1
            event.app.invalidate()

        # ↓：面板下滚一整页；回到 0 即恢复贴底实时跟随
        @bindings.add("down", filter=_cond_viewing, eager=True)
        def _(event) -> None:
            self._view_scroll = max(0, self._view_scroll - (_TRANSCRIPT_PANEL_ROWS - 1))
            event.app.invalidate()

        # Esc：关闭转录覆盖层（始终优先于被遮挡的主流程组件）
        # - 调起态（/agents）：解开 request_transcript_view 的 future，令 app 循环回到列表（收尾由 _await_transcript_view finally 做）
        # - 实时态（列表 Enter）：只清理覆盖层，保留最新主流程 mode 与 Buffer
        @bindings.add("escape", filter=_cond_viewing, eager=True)
        def _(event) -> None:
            """Resolve a modal transcript or close a live overlay.

            Args:
                event: prompt-toolkit key event.

            Returns:
                None.
            """
            if self._viewing_invoked:
                self._resolve_input("")
            else:
                self._agent_panel.close_live()
                event.app.invalidate()

        return bindings

    def _interrupt_current(self) -> None:
        """中断当前交互（供 Ctrl+C 与外部 SIGINT 共用）。

        - 处理态：请求中断（_request_user_interrupt → 总线 InterruptRequested）。
        - 可输入态有文本：清空缓冲、留在原处。
        - 可输入态空缓冲：以 KeyboardInterrupt 解开读取。
        """
        if self._accepting:
            if self._buffer is not None and self._buffer.text:
                self._buffer.reset()
                return
            self._fail_input(KeyboardInterrupt())
            return
        self._request_user_interrupt()

    def _handle_permission_mode_toggle(self, event) -> None:
        """Shift+Tab：切换权限模式并 invalidate 触发状态条重绘，立即反映新模式。

        Args:
            event: prompt_toolkit 按键事件。
        """
        list_focused = (
            self._agent_list_inner is not None
            and event.app.layout.has_focus(self._agent_list_inner)
        )
        completion_visible = (
            self._buffer is not None
            and self._buffer.complete_state is not None
        )
        if (
            self._permission_mode_toggle_handler is None
            or not can_toggle_permission_mode(
                mode=self._mode,
                transcript_visible=self._viewing_uuid is not None,
                completion_visible=completion_visible,
                agent_list_focused=list_focused,
            )
        ):
            return
        self._permission_mode_toggle_handler()
        event.app.invalidate()

    def _try_add_binding(self, bindings: KeyBindings, key: str, handler: Callable) -> None:
        """尝试注册一个按键绑定；某些终端不支持该键序列（ValueError）时静默跳过。

        Args:
            bindings: 目标 KeyBindings。
            key: 键序列字符串（如 "s-tab"、"<sigint>"）。
            handler: 绑定的处理函数。
        """
        try:
            bindings.add(key)(handler)
        except ValueError:
            pass
