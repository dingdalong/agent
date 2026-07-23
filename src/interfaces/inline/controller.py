"""InlineController — 组合单个常驻 prompt_toolkit Application 与职责控制器。

底部状态块自上而下为：活动行（spinner，仅处理态可见）+ 分割线 + 输入框 + 分割线 + 核心状态行；
正文经 patch_stdout 的 StdoutProxy 在其上方的原生 scrollback 滚动输出；
非 TTY（管道 / CI）下不建 App，退化为扁平输出 + PromptSession 降级读取。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from collections.abc import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.input.base import Input
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
from prompt_toolkit.patch_stdout import StdoutProxy
from prompt_toolkit.output.base import Output
from rich.console import Console
from rich.text import Text

from src.events.menu import MenuRequest, UiRequest
from src.interfaces.base import UserInterface
from src.interfaces.agent_view_store import AgentViewStore
from src.interfaces.turn_clock import TurnClock
from src.interfaces.completer import SlashCommandCompleter
from src.interfaces.inline.agent_panel import AgentPanelActions, AgentPanelController
from src.interfaces.inline.form import FormActions, FormState
from src.interfaces.inline.keymap import (
    KeymapActions,
)
from src.interfaces.inline.menus import (
    ChoiceInputState,
    MenuActions,
    SelectionState,
)
from src.interfaces.inline.output import (
    _MarkdownStream,
    OutputActions,
)
from src.interfaces.inline.plain import PlainActions, PlainFrontend
from src.interfaces.inline.runtime import InlineRuntime
from src.interfaces.inline.status_bar import _RoundEntry, StatusBarActions, StatusBarController
from src.interfaces.inline.window_manager import WindowManager

# 状态栏 agent 列表最多同时显示的 agent 行数（不含上下滚动指示行）。
_AGENT_LIST_MAX_ROWS = 8

# 子 agent 转录覆盖面板的最大可见行数（参照 agent 列表 _AGENT_LIST_MAX_ROWS）。
_TRANSCRIPT_PANEL_ROWS = 12

# StdoutProxy 批量写入间隔（秒）。
_STDOUT_SLEEP_BETWEEN_WRITES = 0.0

# 彩色 › 前缀：可输入态加粗醒目，处理态压暗。
_PREFIX_ACTIVE = to_formatted_text(ANSI("\x1b[1;36m›\x1b[0m "))
_PREFIX_DIM = to_formatted_text(ANSI("\x1b[2;36m›\x1b[0m "))


class _PlaceholderProcessor(Processor):
    """输入框缓冲为空时，在首行渲染浅字占位提示（供 form 态讨论栏/自定义输入行提示）。"""

    def __init__(self, get_placeholder: Callable[[], str]) -> None:
        """初始化占位 Processor。

        Args:
            get_placeholder: 返回当前占位文案的回调；返回空串表示不显示占位。
        """
        self._get_placeholder = get_placeholder

    def apply_transformation(self, transformation_input: TransformationInput) -> Transformation:
        """缓冲为空且处于首行时，用浅字占位替换空行片段。

        Args:
            transformation_input: prompt_toolkit 提供的单行变换输入。
        Returns:
            渲染用的 Transformation（占位或原片段）。
        """
        if transformation_input.lineno == 0 and not transformation_input.document.text:
            hint = self._get_placeholder()
            if hint:
                return Transformation([("class:placeholder fg:ansibrightblack", hint)])
        return Transformation(transformation_input.fragments)


class _NestedField:
    """Descriptor routing legacy implementation attributes into one controller."""

    def __init__(
        self,
        controller_name: str,
        field_name: str,
        converter: Callable[[object], object] | None = None,
    ) -> None:
        """Initialize a routed controller field.

        Args:
            controller_name: Attribute holding the target controller.
            field_name: Attribute read and written on that controller.
            converter: Optional value normalization applied on writes.

        Returns:
            None.
        """
        self._controller_name = controller_name
        self._field_name = field_name
        self._converter = converter

    def __get__(self, instance: object | None, owner: type | None = None) -> object:
        """Read the routed controller value.

        Args:
            instance: Owning Inline implementation instance.
            owner: Owning class when accessed through the type.

        Returns:
            Descriptor itself for class access, otherwise routed field value.
        """
        if instance is None:
            return self
        controller = getattr(instance, self._controller_name)
        return getattr(controller, self._field_name)

    def __set__(self, instance: object, value: object) -> None:
        """Write the routed controller value.

        Args:
            instance: Owning Inline implementation instance.
            value: Value stored after optional normalization.

        Returns:
            None.
        """
        controller = getattr(instance, self._controller_name)
        normalized = self._converter(value) if self._converter is not None else value
        setattr(controller, self._field_name, normalized)


class InlineController(
    StatusBarActions,
    AgentPanelActions,
    FormActions,
    MenuActions,
    OutputActions,
    PlainActions,
    KeymapActions,
    UserInterface,
):
    """组合各 Inline 组件并实现完整终端交互。"""

    _app = _NestedField("_runtime", "app")
    _app_task = _NestedField("_runtime", "app_task")
    _stdout_proxy = _NestedField("_runtime", "stdout_proxy")
    _orig_stdout = _NestedField("_runtime", "original_stdout")
    _orig_stderr = _NestedField("_runtime", "original_stderr")
    _buffer = _NestedField("_runtime", "buffer")
    _input_future = _NestedField("_runtime", "_input_future")
    _agent_list_window = _NestedField("_runtime", "agent_list_window")
    _agent_list_inner = _NestedField("_runtime", "agent_list_inner")
    _input_window = _NestedField("_runtime", "input_window")

    _transcript_cache = _NestedField("_agent_panel", "transcript_cache")
    _message_cache = _NestedField("_agent_panel", "message_cache")
    _agent_selected_index = _NestedField("_agent_panel", "selected_index")

    _select_options = _NestedField("_selection", "options")
    _select_index = _NestedField("_selection", "index")
    _select_cancel_value = _NestedField("_selection", "cancel_value")
    _select_markdown = _NestedField("_selection", "markdown")

    _form_questions = _NestedField("_form", "questions")
    _form_focus = _NestedField("_form", "focus")
    _form_zone = _NestedField("_form", "zone")
    _form_row = _NestedField("_form", "rows")
    _form_checked = _NestedField("_form", "checked")
    _form_text = _NestedField("_form", "text")
    _form_discussion = _NestedField("_form", "discussion")
    _form_markdown = _NestedField("_form", "markdown")

    _choice_input_options = _NestedField("_choice_input", "options")
    _choice_input_descriptions = _NestedField("_choice_input", "descriptions")
    _choice_input_placeholder = _NestedField("_choice_input", "placeholder")
    _choice_input_index = _NestedField("_choice_input", "index")
    _choice_input_markdown = _NestedField("_choice_input", "markdown")

    def __init__(
        self,
        agent_view_store: AgentViewStore,
        slash_commands: list[tuple[str, str]] | None = None,
        turn_clock: TurnClock | None = None,
    ) -> None:
        """初始化内联 UI：Rich Console、双流 markdown 渲染器、常驻 App 句柄与底部状态条运行时状态。

        Args:
            agent_view_store: 全部状态栏、agent 行和转录共用的唯一读模型。
            slash_commands: 斜杠命令列表，每项为 (命令名, 描述)，由组装层注入供补全器使用。
            turn_clock: 与工具执行层共享的回合时钟，用于耗时剔除纯人工等待；缺省自建独立实例。
        """
        super().__init__()
        self._runtime = InlineRuntime()
        self._turn_clock = turn_clock or TurnClock()
        self._agent_view_store = agent_view_store
        self._agent_panel = AgentPanelController()
        self._window_manager = WindowManager(self._run_window_dialog, self._invalidate_window_state)
        self._selection = SelectionState()
        self._form = FormState()
        self._choice_input = ChoiceInputState()
        self._plain = PlainFrontend()
        self._status_bar = StatusBarController(
            agent_view_store,
            self.get_permission_mode,
        )
        self._slash_commands: list[tuple[str, str]] = slash_commands or []
        self._permission_mode_toggle_handler: Callable[[], None] | None = None
        # 输出渲染用 Console。
        self._rich_console = self._make_console(None)
        self._status_console = self._rich_console  # 状态块捕获用 Console。
        # 两条流式 markdown 输出流：回应与思考。
        self._response_stream = _MarkdownStream()
        self._thinking_stream = _MarkdownStream(base_style="dim")

        # ---- 常驻 App 与终端代理 ----
        self._tty: bool = sys.stdout.isatty()
        self.is_tty: bool = self._tty  # 公共只读属性，供外部（bootstrap）判断是否可建富 UI
        self._render_width: int = self._rich_console.width  # 渲染宽度快照（分割线/markdown）
        self._fallback_session: PromptSession[str] | None = None  # 非 TTY 降级读取用的 PromptSession（懒建）
        # 可输入态（input）：缓冲可编辑、› 醒目、Enter 提交。
        self._cond_accepting = Condition(lambda: self._accepting)
        # 缓冲可编辑条件由 WindowManager 栈顶决定：input 恒可编辑；form 仅自由文本题可编辑；其余只读。
        self._cond_buffer_editable = Condition(self._buffer_editable)

        # ---- 选择菜单（方向键导航，权限确认 / 任意 ChoiceMenu 共用）----
        self._select_options = None
        self._select_index = 0
        self._select_cancel_value = ""
        self._select_markdown = False

        # ---- 单屏标签页表单（form 态，ask_user 多问题共用）----
        self._form_questions = None
        self._form_focus = 0
        self._form_zone = "answer"
        self._form_row = []
        self._form_checked = []
        self._form_text = []
        self._form_discussion = ""
        self._form_markdown = False

        # ---- 选项 + 输入行（choice_input 态，exit_plan_mode 等共用；输入行即常驻输入框）----
        self._choice_input_options = None
        self._choice_input_descriptions = None
        self._choice_input_placeholder = ""
        self._choice_input_index = 0
        self._choice_input_markdown = False

        # ---- 底部状态条运行时状态 ----
        self._activity: str = ""  # 当前活动文案（思考中/回应中/工具名/压缩上下文）
        self._current_agent_type: str | None = None  # 当前正在工作的 agent 类型（主 Agent 为其 agent_type；回合起始 reset 后短暂为 None）
        self._current_agent_uuid: str | None = None  # 当前正在工作的 agent 实例 uuid
        self._activity_started_monotonic: float | None = None  # 本步骤（当前活动）处理起点（monotonic 秒）
        self._turn_started_monotonic: float | None = None  # 本回合处理起点（monotonic 秒）
        self._activity_paused_baseline: float = 0.0  # 本步起始时的累计暂停基线（本步耗时只剔除其后的暂停）
        self._session_elapsed_accumulated: float = 0.0  # 全会话已完成回合的累计有效耗时（秒），跨回合累加、剔除人工等待；/clear 归零

        # ---- API 重试倒计时（前台 agent 指数退避等待，驱动活动区黄色倒计时行）----
        self._retry_deadline: float | None = None  # 本次重试等待的截止 monotonic 秒（None 表示不在重试等待）
        self._retry_error_kind: str = ""  # 稳定的 LLM 错误类别
        self._retry_safe_message: str = ""  # 不含请求响应正文或凭据的安全错误摘要
        self._retry_attempt: int = 0  # 已失败的尝试序号（1 基）
        self._retry_max: int = 0  # 允许的最大尝试次数

        # ---- 本轮工具缓冲（前台 agent 当前这一轮的工具调用，驱动顶部「本轮面板」并在轮边界 flush 成 scrollback 定稿块）----
        self._round_entries: list[_RoundEntry] = []
        self._round_agent_type: str | None = None  # 本轮工具所属 agent 类型（缓冲空转非空时记录）
        self._round_agent_uuid: str | None = None  # 本轮工具所属 agent 实例 uuid

        # ---- agent 列表（方向键导航，仅在有子 agent 时显示）----
        self._transcript_cache = None
        self._message_cache = None
        self._agent_selected_index = 0
        self._agent_list_window = None
        self._agent_list_inner = None
        self._input_window = None

    @property
    def _accepting(self) -> bool:
        """Return whether unobscured normal text input is editable.

        Returns:
            True only while INPUT is active and no transcript overlays it.
        """
        return self._top_window_kind == "input"

    @property
    def _top_window_kind(self) -> str | None:
        """Return the visually topmost window kind.

        Returns:
            Top window kind, or None when only the base interface is visible.
        """
        top = self._window_manager.top_window
        return top.kind if top is not None else None

    @property
    def _viewing_uuid(self) -> str | None:
        """Return the UUID visible in the retained transcript window.

        Returns:
            Viewed subagent UUID, or None when no transcript is retained.
        """
        return self._window_manager.transcript_uuid

    @property
    def _view_scroll(self) -> int:
        """Return the transcript offset from its live tail.

        Returns:
            Non-negative transcript scroll offset.
        """
        return self._window_manager.transcript_scroll

    @_view_scroll.setter
    def _view_scroll(self, scroll: int) -> None:
        """Store a transcript scroll offset in WindowManager.

        Args:
            scroll: Desired non-negative offset from the live tail.

        Returns:
            None.
        """
        self._window_manager.set_transcript_scroll(scroll)

    @property
    def _viewing_invoked(self) -> bool:
        """Return whether the transcript belongs to a pending ViewRequest.

        Returns:
            True when Esc should settle a history-view request.
        """
        return self._window_manager.transcript_is_requested

    @property
    def _transcript_visible(self) -> bool:
        """Return whether the transcript is the topmost keyboard owner.

        Returns:
            True only while no dialog overlays the transcript.
        """
        return self._top_window_kind == "transcript"

    def _invalidate_window_state(self) -> None:
        """Request a redraw after WindowManager changes derived UI state.

        Returns:
            None.
        """
        if self._app_running and self._app is not None:
            self._app.invalidate()

    @property
    def _app_running(self) -> bool:
        """常驻 App 是否已建且处于运行态。"""
        return self._runtime.app_running

    def _make_console(self, width: int | None) -> Console:
        """构造强制着色的 Rich Console，用于把输出/状态条捕获为 ANSI。

        Args:
            width: 固定渲染宽度；None 表示自动探测（非 TTY 降级时使用）。
        Returns:
            配置好的 Rich Console。
        """
        return Console(force_terminal=True, color_system="standard", width=width, legacy_windows=False)

    # ---- 生命周期：常驻 App 启停 ----

    async def start(self) -> None:
        """启动常驻 prompt_toolkit Application：装 stdout 代理、以后台任务跑 run_async，等首帧绘出。

        非 TTY（管道/CI）下不建 App、不装代理，走扁平降级路径（见 _read_input_plain）。

        Returns:
            None.
        """
        if not self._tty:
            return
        self._render_width = shutil.get_terminal_size(fallback=(88, 24)).columns
        self._rich_console = self._make_console(self._render_width)
        self._status_console = self._make_console(self._render_width)
        self._response_stream.renderer.width = self._render_width
        self._thinking_stream.renderer.width = self._render_width

        self._app = self._build_application()
        self._stdout_proxy = StdoutProxy(raw=True, sleep_between_writes=_STDOUT_SLEEP_BETWEEN_WRITES)
        self._orig_stdout, self._orig_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self._stdout_proxy, self._stdout_proxy
        try:
            self._app_task = asyncio.create_task(
                self._app.run_async(handle_sigint=True, set_exception_handler=False)
            )
            while not self._app.is_running:  # 等 App 进入运行态
                if self._app_task.done():
                    await self._app_task
                await asyncio.sleep(0)
            await asyncio.sleep(0)  # 再让一拍，确保首帧已绘出
        except BaseException:
            task = self._app_task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._app_task = None
            self._restore_terminal_streams()
            raise

    async def stop(self) -> None:
        """Close WindowManager, then stop the prompt-toolkit application.

        非 TTY 下无 App/代理，仅走基类收尾。

        Returns:
            None.
        """
        try:
            await self._window_manager.close()
            if self._app_running:
                self._app.exit()
            if self._app_task is not None:
                try:
                    await self._app_task
                except (asyncio.CancelledError, EOFError, KeyboardInterrupt):
                    pass
        finally:
            self._app_task = None
            self._runtime.cancel_input()
            self._restore_terminal_streams()
            await super().stop()

    def _restore_terminal_streams(self) -> None:
        """Restore process streams and close the active stdout proxy.

        Returns:
            None.
        """
        proxy = self._stdout_proxy
        if proxy is None:
            return
        sys.stdout, sys.stderr = self._orig_stdout, self._orig_stderr
        proxy.close()
        self._stdout_proxy = None

    # ---- 常驻 App 构建 ----

    def _build_application(
        self,
        input: Input | None = None,
        output: Output | None = None,
    ) -> Application[None]:
        """构建常驻非全屏 Application：带分隔线和折行标题的转录覆盖面板（查看子 agent 时置顶覆盖主对话）+ 活动行（spinner，仅处理态可见）+ 分割线 + 输入框 + 分割线 + 核心状态行 + agent 列表（有子 agent 且未查看时）+ 权重填充窗口。

        Args:
            input: 可选 prompt-toolkit 输入；None 使用终端默认输入。
            output: 可选 prompt-toolkit 输出；None 使用终端默认输出。

        Returns:
            可 await run_async() 的常驻 Application。
        """
        self._buffer = Buffer(
            multiline=True,
            read_only=~self._cond_buffer_editable,
            completer=SlashCommandCompleter(self._slash_commands),
            complete_while_typing=Condition(lambda: self._top_window_kind == "input"),  # 仅主输入态自动补全，form 文本题不弹斜杠补全
            document=Document("", 0),
        )
        # 转录面板可见性条件：查看子 agent 转录时为真，驱动面板显隐并隐藏 spinner / agent 列表。
        cond_viewing = Condition(lambda: self._transcript_visible)
        # 转录覆盖面板：分隔线框住自适应折行标题，下接内容（max=_TRANSCRIPT_PANEL_ROWS）。
        transcript_panel = ConditionalContainer(
            HSplit([
                self._make_separator_window(),
                Window(
                    FormattedTextControl(self._render_transcript_header),
                    dont_extend_height=True,
                    height=Dimension(min=1),
                    wrap_lines=True,
                ),
                self._make_separator_window(),
                Window(FormattedTextControl(self._render_transcript_panel), dont_extend_height=True, height=Dimension(max=_TRANSCRIPT_PANEL_ROWS), wrap_lines=False),
            ]),
            filter=cond_viewing,
        )
        activity_window = ConditionalContainer(
            Window(FormattedTextControl(self._render_activity), dont_extend_height=True, height=Dimension(min=1)),
            filter=Condition(lambda: self._top_window_kind is None and (bool(self._activity) or bool(self._round_entries) or self._retry_deadline is not None)),
        )
        # 选择菜单窗口：permission/choice 栈顶时可见，只画可重绘的选项（上文已先打到 scrollback）。
        select_window = ConditionalContainer(
            Window(FormattedTextControl(self._render_select), dont_extend_height=True, height=Dimension(min=1)),
            filter=Condition(lambda: self._top_window_kind in {"permission", "choice"}),
        )
        # 表单窗口：form 栈顶时可见，纵向渲染全部问题与作答（上文已先打到 scrollback）。
        form_window = ConditionalContainer(
            Window(FormattedTextControl(self._render_form), dont_extend_height=True, height=Dimension(min=1)),
            filter=Condition(lambda: self._top_window_kind == "form"),
        )
        # 选项+输入窗口：choice_input 栈顶时可见，画选项区与操作提示（输入行由下方常驻输入框承载）。
        choice_input_window = ConditionalContainer(
            Window(FormattedTextControl(self._render_choice_input), dont_extend_height=True, height=Dimension(min=1)),
            filter=Condition(lambda: self._top_window_kind == "choice_input"),
        )
        # 斜杠命令补全下拉窗口：缓冲存在补全候选时可见，置于输入框上方。
        completion_window = ConditionalContainer(
            Window(FormattedTextControl(self._render_completions), dont_extend_height=True, height=Dimension(max=8)),
            filter=Condition(
                lambda: self._top_window_kind == "input"
                and self._buffer is not None
                and self._buffer.complete_state is not None,
            ),
        )
        self._input_window = Window(
            BufferControl(
                buffer=self._buffer,
                input_processors=[_PlaceholderProcessor(self._buffer_placeholder)],
            ),
            get_line_prefix=self._get_line_prefix,
            dont_extend_height=True,
            height=Dimension(min=1),
            wrap_lines=True,
            always_hide_cursor=Condition(self._hide_real_cursor),  # 表单答题区光标内联绘制、choice_input 光标在选项行时，兜底隐藏输入框真实光标
        )
        # 表单答题区底部提示：输入栏隐藏时占其位，暗色提示按 Tab 唤出讨论输入栏。
        form_footer_window = ConditionalContainer(
            Window(FormattedTextControl(self._render_form_footer), dont_extend_height=True, height=Dimension.exact(1)),
            filter=Condition(self._form_answering),
        )
        core_status_window = Window(
            FormattedTextControl(self._render_core_status),
            dont_extend_height=True,
            height=Dimension(min=1),
        )
        # agent 列表窗口：仅在有子 agent 时显示，封顶 = agent 行 + 上/下各一行滚动指示
        self._agent_list_inner = Window(
            FormattedTextControl(self._render_agent_list, focusable=True),
            dont_extend_height=True,
            height=Dimension(max=_AGENT_LIST_MAX_ROWS + 2),
        )
        self._agent_list_window = ConditionalContainer(
            self._agent_list_inner,
            filter=Condition(lambda: self._has_sub_agents() and self._top_window_kind in {None, "input"}),
        )
        filler_window = Window(height=Dimension(weight=1))  # 吸收余高，使状态块紧贴输入框
        cond_not_form = Condition(lambda: self._top_window_kind != "form")  # 底分割线与核心状态行仅非表单态显示（表单态隐藏模式/token 等信息）
        root = HSplit([
            transcript_panel,
            activity_window,
            select_window,
            form_window,
            choice_input_window,
            completion_window,
            self._make_separator_window(),  # 顶分割线：恒显
            ConditionalContainer(self._input_window, filter=Condition(self._input_bar_visible)),  # 输入栏：表单答题区隐藏
            form_footer_window,  # 底部提示：表单答题区显示，占输入栏位
            ConditionalContainer(self._make_separator_window(), filter=cond_not_form),  # 底分割线：非表单态显示
            ConditionalContainer(core_status_window, filter=cond_not_form),  # 核心状态行：非表单态显示
            self._agent_list_window,
            filler_window,
        ])
        layout = Layout(root, focused_element=self._input_window)
        self._runtime.layout = layout
        app = Application(
            layout=layout,
            key_bindings=self._build_key_bindings(),
            refresh_interval=0.1,
            input=input,
            output=output,
        )
        app.ttimeoutlen = 0.05  # 孤立 Esc 的转义冲刷等待：默认 0.5s→50ms，消除按 Esc 的体感延迟
        return app

    def _make_separator_window(self) -> Window:
        """构造一行占满终端宽度的暗色分割线窗口。

        Returns:
            渲染单行分割线的 Window。
        """
        return Window(
            FormattedTextControl(self._render_separator),
            dont_extend_height=True,
            height=Dimension.exact(1),
        )

    def _get_line_prefix(self, lineno: int, wrap_count: int):
        """输入框行前缀：仅首行用彩色 ›，续行与自动折行均无前缀。

        Args:
            lineno: 行号（0 为首行）。
            wrap_count: 当前行的折行计数（0 为该逻辑行首段）。

        Returns:
            prompt_toolkit 可接受的 formatted text 前缀。
        """
        if lineno == 0 and wrap_count == 0:
            return _PREFIX_ACTIVE if self._buffer_editable() else _PREFIX_DIM
        return []

    def _render_completions(self) -> ANSI:
        """渲染斜杠命令补全下拉。

        Returns:
            可作为 Window 内容的 ANSI；无补全候选时为空。
        """
        if self._buffer is None or self._buffer.complete_state is None:
            return ANSI("")
        state = self._buffer.complete_state
        text = Text()
        completions = state.completions
        for i, comp in enumerate(completions):
            selected = i == state.complete_index
            line = Text()
            line.append("❯ " if selected else "  ", style="cyan" if selected else "")
            line.append(comp.display_text, style="reverse" if selected else "")
            meta = comp.display_meta_text
            if meta:
                line.append(f"  —  {meta}", style="reverse" if selected else "bright_black")
            text.append(line)
            if i < len(completions) - 1:
                text.append("\n")
        with self._status_console.capture() as capture:
            self._status_console.print(text, end="")
        return ANSI(capture.get())

    def reload(self) -> None:
        """/clear 重置 UI 的 agent 列表选中态、转录面板查看态、全会话累计耗时与本轮工具缓冲。

        与 Store 会话 token 归零同步：新会话耗时从 0 重新累计。

        Returns:
            None.
        """
        self._session_elapsed_accumulated = 0.0
        self._clear_retry_status()
        self._round_entries = []
        self._round_agent_type = None
        self._round_agent_uuid = None
        self._agent_selected_index = 0
        self._window_manager.reload()
        if (
            self._app_running
            and self._agent_list_inner is not None
            and self._app is not None
            and self._app.layout.has_focus(self._agent_list_inner)
        ):
            self._app.layout.focus(self._input_window)

    # ---- 输入 / 权限：解析常驻 App 的内部 future ----

    async def _read_input(self, prompt: str, default: str = "", markdown: bool = False) -> str:
        """读取用户输入：进入输入态、预填默认值，await 由 Enter 键绑定解析的 future。

        Args:
            prompt: 上层请求的提示文本（主循环 "你: "、ask_user 的多行问题等）。
            default: 预填入的默认输入。
            markdown: 上文提示是否按 Markdown 渲染（如 ask_user 的问题）。
        Returns:
            用户提交的非空白文本。
        """
        if not self._tty:
            return await self._read_input_plain(prompt, default)
        # 回合边界（Trigger B）：最后一轮工具后无后续 LLMCallStarted 触发 flush，故在回到输入态最前
        # 先把本轮缓冲定稿成 scrollback 分组块（中断残留项标「已中断」）；缓冲空则 no-op。
        # 仅 REPL「你:」提示经此路径；ask_user 走 request_form/FormMenu，不会在工具在飞时误 flush。
        self._round_flush()
        # 把刚结束回合的有效耗时并入全会话累计，随后 finally 里 _reset_turn_status
        # 清零本回合起点与时钟；输入态显示该累计值（冻结），下一回合在此基础上继续增长。
        self._session_elapsed_accumulated += self._turn_elapsed(time.monotonic())
        self._render_input_context(prompt, markdown)
        try:
            text = await self._await_submission(default)
            self._echo_submitted_input(text)
            return text
        finally:
            self._reset_turn_status()

    def _echo_submitted_input(self, text: str) -> None:
        """把刚提交的用户输入回显到滚动区，使其在输入框清空后仍留痕。

        首行加 cyan `›` 前缀（呼应输入框前缀，视觉上像输入"上移"成记录），多行续行以两空格对齐。
        仅常驻 App（TTY）路径需要——非 TTY 由终端自身回显。

        Args:
            text: 用户刚提交的非空白文本（可能多行）。
        """
        echo = Text()
        lines = text.split("\n")
        for i, line in enumerate(lines):
            echo.append("› " if i == 0 else "  ", style="cyan")
            echo.append(line)
            if i < len(lines) - 1:
                echo.append("\n")
        self._print_rich(echo)

    async def _await_submission(self, default: str = "") -> str:
        """进入可编辑文本输入态（input），预填 default，await 由 Enter 键绑定解析的 future。

        若当前焦点在 agent 列表，先抢回输入框（否则按键落到列表而非应答 future → 交互卡死）。

        Args:
            default: 预填入缓冲的默认文本。
        Returns:
            用户提交的一行/多行文本（未经校验）。
        """
        with self._runtime.interaction() as future:
            try:
                if (
                    self._app is not None
                    and self._agent_list_inner is not None
                    and self._app.layout.has_focus(self._agent_list_inner)
                ):
                    self._app.layout.focus(self._input_window)
                self._buffer.set_document(Document(default, len(default)), bypass_readonly=True)
                self._app.invalidate()
                return await future
            finally:
                self._enter_processing_idle()

    def _buffer_editable(self) -> bool:
        """返回输入缓冲是否可编辑。

        WindowManager 栈顶为转录或选择窗口时恒为只读；栈顶为 input 时可编辑，form
        的讨论区或自定义输入行可编辑，choice_input 的输入行可编辑。

        Returns:
            缓冲是否可编辑。
        """
        if self._top_window_kind == "input":
            return True
        if self._top_window_kind == "form":
            if self._form_zone == "discuss":
                return True
            return self._form_cursor_on_custom()
        if self._top_window_kind == "choice_input":
            return self._choice_input_on_input_row()
        return False

    def _buffer_placeholder(self) -> str:
        """底部输入框缓冲为空时的浅字占位文案；不需占位的态返回空串。

        Returns:
            form 态答题区自定义行为「输入自定义回答…」、其余 form 态为「讨论这几个问题…」；
            choice_input 态为调用方给定的 input_placeholder；其它态为空串。
        """
        if self._top_window_kind == "form":
            if self._form_zone == "answer" and self._form_cursor_on_custom():
                return "输入自定义回答…"
            return "讨论这几个问题…"
        if self._top_window_kind == "choice_input":
            return self._choice_input_placeholder
        return ""








    def _input_bar_visible(self) -> bool:
        """底部输入栏当前是否可见：除表单答题区外均可见（表单答题区隐藏输入栏，改由内联自定义行与底部提示承载）。

        choice_input 态输入栏恒可见——它即该菜单的输入行。

        Returns:
            输入栏应显示时为 True。
        """
        return not self._form_answering()


    def _hide_real_cursor(self) -> bool:
        """是否隐藏输入框真实光标：表单答题区（光标改由自定义行内联绘制）、
        或 choice_input 光标停在选项行（输入行未激活、输入框只读）时隐藏。

        Returns:
            应隐藏真实光标时为 True。
        """
        if self._form_answering():
            return True
        return self._top_window_kind == "choice_input" and not self._choice_input_on_input_row()



















    def _enter_processing_idle(self) -> None:
        """Clear the shared input buffer after an answer-window reader exits.

        Returns:
            None.
        """
        if self._buffer is not None:
            self._buffer.set_document(Document("", 0), bypass_readonly=True)
        if self._app_running:
            self._app.invalidate()

    def _pending_input_future(self) -> asyncio.Future[str] | None:
        """返回待决的输入/权限应答 future（存在且未完成），否则 None。

        Returns:
            待决的 _input_future；无待决（为 None 或已完成）时返回 None。
        """
        return self._runtime.pending_input_future()

    def _resolve_input(self, text: str) -> None:
        """用提交文本解析待决的输入/权限应答 future（无待决则忽略）。

        Args:
            text: 用户提交的非空白文本。

        Returns:
            None.
        """
        self._runtime.resolve_input(text)

    def _fail_input(self, exc: BaseException) -> None:
        """以异常解开待决的输入/权限读取（无待决则忽略）。

        Args:
            exc: 要置入 future 的异常（KeyboardInterrupt 或 EOFError）。

        Returns:
            None.
        """
        self._runtime.fail_input(exc)

    def cancel_active_input(self) -> bool:
        """Cancel every retained TTY window or the serial fallback request.

        Returns:
            Whether any request or input future was cancelled.
        """
        cancelled = self._window_manager.cancel_all() if self._tty else super().cancel_active_input()
        self._runtime.cancel_input()
        return cancelled

    async def wait_interactions_idle(self) -> None:
        """Wait until WindowManager runners have released component state.

        Returns:
            None.
        """
        if self._tty:
            await self._window_manager.wait_idle()

    async def _accept_ui_request(self, request: UiRequest) -> bool:
        """Hand every TTY request to WindowManager without blocking EventBus consumption.

        Args:
            request: Pending EventBus UI request.

        Returns:
            True when the TTY manager accepted or discarded the request during shutdown.
        """
        if not self._tty:
            return False
        self._window_manager.submit(request)
        return True

    async def _run_window_dialog(self, request: MenuRequest) -> str:
        """Render and read one active dialog after WindowManager promotes it.

        Args:
            request: Active answer request promoted from the FIFO queue.

        Returns:
            Answer value to settle after component cleanup completes.
        """
        try:
            return await self._read_menu_request(request)
        except (EOFError, KeyboardInterrupt):
            self._request_user_interrupt()
            raise

    def _render_input_context(self, prompt: str, markdown: bool = False) -> None:
        """把输入提示的「上文」打到 App 上方滚动区（输入框由常驻分割线框住，无需再打印分隔线）。

        提示末行（输入标签，如 "你: "）不打印，由输入框的彩色 › 前缀代替（见 _get_line_prefix）；
        末行之前的上文（如 ask_user 的多行问题）照常打印。

        Args:
            prompt: 上层请求的原始提示文本。
            markdown: 上文是否按 Markdown 渲染。

        Returns:
            None.
        """
        lines = prompt.splitlines()
        context = "\n".join(lines[:-1]).strip("\n") if len(lines) > 1 else ""
        if context.strip():
            if markdown:
                self._print_markdown(context)
            else:
                self._print_rich(context)



    async def _read_transcript_view(self, uuid: str) -> str:
        """Provide the serial fallback result for a transcript view request.

        TTY requests are always accepted by WindowManager before this method can
        run. Non-TTY callers use the existing plain `/agents` summary path.

        Args:
            uuid: 目标子 agent 的 uuid 字符串。
        Returns:
            恒为空串（只读查看）。
        """
        return ""













