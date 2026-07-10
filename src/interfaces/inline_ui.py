"""InlineInterface — 基于单个常驻非全屏 prompt_toolkit Application + rich 的内联富文本 CLI。

底部状态块自上而下为：活动行（spinner，仅处理态可见）+ 分割线 + 输入框 + 分割线 + 核心状态行；
正文经 patch_stdout 的 StdoutProxy 在其上方的原生 scrollback 滚动输出；
非 TTY（管道 / CI）下不建 App，退化为扁平输出 + PromptSession 降级读取。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import unicodedata
from collections.abc import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
from prompt_toolkit.patch_stdout import StdoutProxy
from rich.console import Console
from rich.text import Text
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.interfaces.output_router import AgentRow

from src.events.types import (
    CompactDelta,
    LLMCallCompleted,
    LLMCallStarted,
    PermissionNotice,
    ResponseDelta,
    ThinkingDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from src.events.menu import FormQuestion
from src.interfaces.base import UserInterface
from src.interfaces.completer import SlashCommandCompleter
from src.interfaces.markdown_renderer import MarkdownStreamRenderer, render_markdown

# braille dots spinner 帧序列。
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# 子 agent 转录覆盖面板的最大可见行数（参照 agent 列表 Dimension(max=8)）。
_TRANSCRIPT_PANEL_ROWS = 12

# StdoutProxy 批量写入间隔（秒）。
_STDOUT_SLEEP_BETWEEN_WRITES = 0.0

# 彩色 › 前缀：可输入态加粗醒目，处理态压暗。
_PREFIX_ACTIVE = to_formatted_text(ANSI("\x1b[1;36m›\x1b[0m "))
_PREFIX_DIM = to_formatted_text(ANSI("\x1b[2;36m›\x1b[0m "))


def _fit_display_width(text: str, max_cols: int) -> str:
    """按终端列宽截断文本以适配定宽标签。

    Args:
        text: 待截断文本。
        max_cols: 最大列宽；东亚宽字符（W/F）计 2 列，其余计 1 列，累计超出即停止。
    Returns:
        列宽不超过 max_cols 的前缀子串。
    """
    cols = 0
    out: list[str] = []
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if cols + w > max_cols:
            break
        out.append(ch)
        cols += w
    return "".join(out)


class _MarkdownStream:
    """一条流式 markdown 输出流的状态：markdown 渲染器 + 是否已开流（已写过前缀、未收尾）。"""

    def __init__(self, *, base_style: str = "") -> None:
        """初始化一条流式 markdown 输出流。

        Args:
            base_style: 渲染时整体叠加到 markdown 正文上的 Rich 样式（如 "dim"）；空串表示不叠加。
        """
        self.renderer = MarkdownStreamRenderer(base_style=base_style)
        self.active = False

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


class InlineInterface(UserInterface):
    """带底部动态状态条的内联富文本 UI，是本框架唯一的具体 UserInterface 实现。"""

    def __init__(self, slash_commands: list[tuple[str, str]] | None = None) -> None:
        """初始化内联 UI：Rich Console、双流 markdown 渲染器、常驻 App 句柄与底部状态条运行时状态。

        Args:
            slash_commands: 斜杠命令列表，每项为 (命令名, 描述)，由组装层注入供补全器使用。
        """
        super().__init__()
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
        self._app: Application[None] | None = None  # 常驻 Application（非 TTY 下为 None）
        self._app_task: asyncio.Task | None = None  # 跑 run_async 的后台任务
        self._stdout_proxy: StdoutProxy | None = None  # 接管 stdout/stderr 的代理
        self._orig_stdout = None  # 装代理前的原始 sys.stdout
        self._orig_stderr = None  # 装代理前的原始 sys.stderr
        self._render_width: int = self._rich_console.width  # 渲染宽度快照（分割线/markdown）
        self._buffer: Buffer | None = None  # 常驻输入缓冲，_build_application 时创建
        self._input_future: asyncio.Future[str] | None = None  # 输入/权限应答的解析通道
        self._fallback_session: PromptSession[str] | None = None  # 非 TTY 降级读取用的 PromptSession（懒建）
        # 交互模式：processing（处理态/只读）/ input（可编辑文本输入）/ select（只读选择菜单）/ form（单屏多问题表单）。
        self._mode: str = "processing"
        # 可输入态（input）：缓冲可编辑、› 醒目、Enter 提交。
        self._cond_accepting = Condition(lambda: self._accepting)
        # 缓冲可编辑条件：input 态恒可编辑；form 态仅聚焦自由文本题时可编辑；其余只读。
        self._cond_buffer_editable = Condition(self._buffer_editable)

        # ---- 选择菜单（方向键导航，权限确认 / 任意 ChoiceMenu 共用）----
        self._select_options: list[tuple[str, str]] | None = None  # 当前菜单选项 (value, label)，None 表示无活跃菜单
        self._select_index: int = 0  # 当前选中项下标
        self._select_cancel_value: str = ""  # Esc 取消时返回的 value（权限为 "deny"，通用选择为空串）
        self._select_markdown: bool = False  # 当前菜单选项标签是否按 Markdown 渲染（仅 ask_user 等开启）

        # ---- 单屏标签页表单（form 态，ask_user 多问题共用）----
        self._form_questions: list[FormQuestion] | None = None  # 当前表单问题列表，None 表示无活跃表单
        self._form_focus: int = 0  # 当前聚焦标签下标；0..len(questions)，== len 为「提交」标签
        self._form_zone: str = "answer"  # 焦点区：answer（答题区）/ discuss（底部讨论栏），Tab 切换
        self._form_row: list[int] = []  # 各问题光标行下标（0..选项数-1 为选项行，==选项数 为自定义输入行）
        self._form_checked: list[set[int]] = []  # 各问题已选中选项下标集（多选可多个、单选至多一个，空集=未作答）
        self._form_text: list[str] = []  # 各问题自定义输入文本（与问题一一对应）
        self._form_discussion: str = ""  # 底部讨论栏文本（全局，随作答一并回传）
        self._form_markdown: bool = False  # 问题/选项标签是否按 Markdown 渲染

        # ---- 选项 + 输入行（choice_input 态，exit_plan_mode 等共用；输入行即常驻输入框）----
        self._choice_input_options: list[tuple[str, str]] | None = None  # 当前选项列表 (value, label)，None 表示无活跃菜单
        self._choice_input_descriptions: list[str] | None = None  # 与 options 对齐的选项浅色说明副行；None 表示无
        self._choice_input_placeholder: str = ""  # 输入行为空时的浅字占位文案
        self._choice_input_index: int = 0  # 当前光标行下标（0..选项数-1 为选项行，==选项数 为输入行）
        self._choice_input_markdown: bool = False  # 上文提示与选项标签是否按 Markdown 渲染

        # ---- 底部状态条运行时状态 ----
        self._activity: str = ""  # 当前活动文案（思考中/回应中/工具名/压缩上下文）
        self._current_agent_type: str | None = None  # 当前正在工作的 agent 类型（主 Agent 为其 agent_type；回合起始 reset 后短暂为 None）
        self._current_agent_uuid: str | None = None  # 当前正在工作的 agent 实例 uuid
        self._activity_started_monotonic: float | None = None  # 本步骤（当前活动）处理起点（monotonic 秒）
        self._session_in_tokens: int = 0  # 本会话累计提交给模型的输入 token（含缓存读取/写入）
        self._session_out_tokens: int = 0  # 本会话累计输出 token
        self._session_cache_read_tokens: int = 0  # 本会话累计缓存命中（读取）输入 token
        self._turn_started_monotonic: float | None = None  # 本回合处理起点（monotonic 秒）
        self._last_elapsed: float = 0.0  # 上一回合最终耗时（秒），供输入态状态行显示

        # ---- agent 列表（方向键导航，仅在有子 agent 时显示）----
        self._rows_provider: Callable[[], list[AgentRow]] | None = None  # 注入自 OutputRouter.agent_rows
        self._transcript_provider: Callable[[str], list[tuple[str, str]]] | None = None  # 注入自 OutputRouter.transcript_segments
        self._transcript_cache: tuple[tuple, list[str]] | None = None  # 转录渲染缓存：(签名, 已渲染行)，签名不变复用
        self._agent_selected_index: int = 0  # 列表聚焦时的选中行索引
        self._agent_list_window: ConditionalContainer | None = None  # 列表容器（_build_application 中创建）
        self._agent_list_inner: Window | None = None  # 列表内部 Window（供 has_focus / layout.focus 使用）
        self._input_window: Window | None = None  # 输入窗口引用（_build_application 中提升为实例属性）

        # ---- 子 agent 转录覆盖面板（半屏、实时跟随）----
        self._viewing_uuid: str | None = None  # 当前查看转录的子 agent uuid；None 表示未在查看（即面板可见性开关）
        self._view_scroll: int = 0  # 从底部向上滚动的可视行数；0 表示贴底实时跟随（tail -f）

    @property
    def _accepting(self) -> bool:
        """是否处于可编辑文本输入态（input）：缓冲可编辑、› 醒目、Enter 提交。

        注：权限/选择改用只读菜单（select 态），不属于可编辑态。
        """
        return self._mode == "input"

    @property
    def _app_running(self) -> bool:
        """常驻 App 是否已建且处于运行态。"""
        return self._app is not None and self._app.is_running

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
        self._app_task = asyncio.create_task(
            self._app.run_async(handle_sigint=True, set_exception_handler=False)
        )
        while not self._app.is_running:  # 等 App 进入运行态
            await asyncio.sleep(0)
        await asyncio.sleep(0)  # 再让一拍，确保首帧已绘出

    async def stop(self) -> None:
        """关闭常驻 App：请求退出、等任务收尾、还原 stdout/stderr 并关闭代理。

        非 TTY 下无 App/代理，仅走基类收尾。
        """
        if self._app_running:
            self._app.exit()
        if self._app_task is not None:
            try:
                await self._app_task
            except (asyncio.CancelledError, EOFError, KeyboardInterrupt):
                pass
            self._app_task = None
        if self._stdout_proxy is not None:
            sys.stdout, sys.stderr = self._orig_stdout, self._orig_stderr
            self._stdout_proxy.close()  # join 后台 flush 线程
            self._stdout_proxy = None
        await super().stop()

    # ---- 常驻 App 构建 ----

    def _build_application(self) -> Application[None]:
        """构建常驻非全屏 Application：转录覆盖面板（查看子 agent 时置顶覆盖主对话）+ 活动行（spinner，仅处理态可见）+ 分割线 + 输入框 + 分割线 + 核心状态行 + agent 列表（有子 agent 且未查看时）+ 权重填充窗口。

        Returns:
            可 await run_async() 的常驻 Application。
        """
        self._buffer = Buffer(
            multiline=True,
            read_only=~self._cond_buffer_editable,
            completer=SlashCommandCompleter(self._slash_commands),
            complete_while_typing=Condition(lambda: self._mode == "input"),  # 仅主输入态自动补全，form 文本题不弹斜杠补全
            document=Document("", 0),
        )
        # 转录面板可见性条件：查看子 agent 转录时为真，驱动面板显隐并隐藏 spinner / agent 列表。
        cond_viewing = Condition(lambda: self._viewing_uuid is not None)
        # 转录覆盖面板：header（1 行）+ 内容（max=_TRANSCRIPT_PANEL_ROWS），仅查看时可见，置于 root 顶部覆盖主对话。
        transcript_panel = ConditionalContainer(
            HSplit([
                Window(FormattedTextControl(self._render_transcript_header), dont_extend_height=True, height=Dimension.exact(1)),
                Window(FormattedTextControl(self._render_transcript_panel), dont_extend_height=True, height=Dimension(max=_TRANSCRIPT_PANEL_ROWS), wrap_lines=False),
            ]),
            filter=cond_viewing,
        )
        activity_window = ConditionalContainer(
            Window(FormattedTextControl(self._render_activity), dont_extend_height=True, height=Dimension(min=1)),
            filter=Condition(lambda: self._mode == "processing" and bool(self._activity) and self._viewing_uuid is None),
        )
        # 选择菜单窗口：select 态可见，只画可重绘的选项（上文已先打到 scrollback），置于输入框上方。
        select_window = ConditionalContainer(
            Window(FormattedTextControl(self._render_select), dont_extend_height=True, height=Dimension(min=1)),
            filter=Condition(lambda: self._mode == "select"),
        )
        # 表单窗口：form 态可见，纵向渲染全部问题与作答（上文已先打到 scrollback），置于输入框上方。
        form_window = ConditionalContainer(
            Window(FormattedTextControl(self._render_form), dont_extend_height=True, height=Dimension(min=1)),
            filter=Condition(lambda: self._mode == "form"),
        )
        # 选项+输入窗口：choice_input 态可见，画选项区与操作提示（输入行由下方常驻输入框承载），置于输入框上方。
        choice_input_window = ConditionalContainer(
            Window(FormattedTextControl(self._render_choice_input), dont_extend_height=True, height=Dimension(min=1)),
            filter=Condition(lambda: self._mode == "choice_input"),
        )
        # 斜杠命令补全下拉窗口：缓冲存在补全候选时可见，置于输入框上方。
        completion_window = ConditionalContainer(
            Window(FormattedTextControl(self._render_completions), dont_extend_height=True, height=Dimension(max=8)),
            filter=Condition(lambda: self._buffer is not None and self._buffer.complete_state is not None),
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
        # agent 列表窗口：仅在有子 agent 时显示，max=8 行封顶
        self._agent_list_inner = Window(
            FormattedTextControl(self._render_agent_list, focusable=True),
            dont_extend_height=True,
            height=Dimension(max=8),
        )
        self._agent_list_window = ConditionalContainer(
            self._agent_list_inner,
            filter=Condition(lambda: self._has_sub_agents() and self._viewing_uuid is None),
        )
        filler_window = Window(height=Dimension(weight=1))  # 吸收余高，使状态块紧贴输入框
        cond_not_form = Condition(lambda: self._mode != "form")  # 底分割线与核心状态行仅非表单态显示（表单态隐藏模式/token 等信息）
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
        return Application(
            layout=Layout(root, focused_element=self._input_window),
            key_bindings=self._build_key_bindings(),
            refresh_interval=0.1,
        )

    def _make_separator_window(self) -> Window:
        """构造一行占满终端宽度的暗色分割线窗口，用于框住输入框（其上、下各放一条）。

        Returns:
            渲染单行分割线的 Window。
        """
        return Window(
            FormattedTextControl(self._render_separator),
            dont_extend_height=True,
            height=Dimension.exact(1),
        )

    def _get_line_prefix(self, lineno: int, wrap_count: int):
        """输入框行前缀：仅首行用彩色 ›（缓冲可编辑时醒目，只读时压暗）；续行与自动折行均无前缀、顶格。

        缓冲可编辑涵盖可输入态与 form 态讨论栏/自定义输入行，故底栏可打字时前缀醒目。

        Args:
            lineno: 行号（0 为首行）。
            wrap_count: 当前行的折行计数（0 为该逻辑行首段）。
        Returns:
            prompt_toolkit 可接受的 formatted text 前缀。
        """
        if lineno == 0 and wrap_count == 0:
            return _PREFIX_ACTIVE if self._buffer_editable() else _PREFIX_DIM
        return []

    def _render_activity(self) -> ANSI:
        """构建活动行「spinner + 当前 agent · 活动 (本步耗时)」的 ANSI；仅处理态且有活动时由其窗口显示。

        首行留空，与上方滚动正文（messages 区）分隔。

        Returns:
            可作为 Window 内容的 ANSI（空行 + 单行活动文案）。
        """
        now = time.monotonic()
        frame = _SPINNER_FRAMES[int(now * 10) % len(_SPINNER_FRAMES)]
        step_elapsed = self._elapsed(self._activity_started_monotonic, now)
        status = Text("\n")
        status.append(f"{frame} ", style="cyan")
        status.append(f"{self._active_agent_name()} · {self._activity} ({step_elapsed:.1f}s)", style="cyan")
        with self._status_console.capture() as capture:
            self._status_console.print(status, end="")
        return ANSI(capture.get())

    def _render_select(self) -> ANSI:
        """渲染选择菜单选项列表（每选项一行），选中行以 ❯ + 反显标记；供 select_window 使用。

        每行格式：「<marker><序号>. <label>」，序号从 1 起，便于数字键直选。

        Returns:
            可作为 Window 内容的 ANSI（多行）；无活跃菜单时为空。
        """
        if not self._select_options:
            return ANSI("")
        text = Text()
        for i, (_value, label) in enumerate(self._select_options):
            selected = i == self._select_index
            line = Text()
            line.append("❯ " if selected else "  ", style="cyan" if selected else "")
            line.append(f"{i + 1}. ")
            line.append_text(self._markdown_label(label) if self._select_markdown else Text(label))
            if selected:
                line.stylize("reverse")  # 整行反显（叠加在标签自带的 Markdown 样式之上）
            text.append(line)
            if i < len(self._select_options) - 1:
                text.append("\n")
        with self._status_console.capture() as capture:
            self._status_console.print(text, end="")
        return ANSI(capture.get())

    def _render_choice_input(self) -> ANSI:
        """渲染 choice_input 菜单的选项区与操作提示（输入行由下方常驻输入框承载，不在此渲染）。

        逐选项一行「<marker><序号>. <label>」，光标落在某选项行时该行 ❯ 前缀 + 整行反显；可带浅色说明副行。
        光标在输入行时所有选项行均无 ❯（改由下方输入框醒目前缀提示）。末行操作提示随光标位置给出可用按键。

        Returns:
            可作为 Window 内容的 ANSI（多行）；无活跃菜单时为空。
        """
        if not self._choice_input_options:
            return ANSI("")
        descs = self._choice_input_descriptions or []
        on_input = self._choice_input_on_input_row()
        text = Text()
        for i, (_value, label) in enumerate(self._choice_input_options):
            selected = (not on_input) and i == self._choice_input_index
            line = Text()
            line.append("❯ " if selected else "  ", style="cyan" if selected else "")
            line.append(f"{i + 1}. ")
            line.append_text(self._markdown_label(label) if self._choice_input_markdown else Text(label))
            if selected:
                line.stylize("reverse")
            text.append_text(line)
            text.append("\n")
            desc = descs[i].strip() if i < len(descs) else ""
            if desc:  # 选项参考说明：浅色缩进副行，不参与光标反显
                sub = self._markdown_label(desc) if self._choice_input_markdown else Text(desc)
                sub.stylize("bright_black")
                text.append("     ")
                text.append_text(sub)
                text.append("\n")
        text.append_text(self._render_choice_input_hint(on_input))
        with self._status_console.capture() as capture:
            self._status_console.print(text, end="")
        return ANSI(capture.get())

    def _render_choice_input_hint(self, on_input_row: bool) -> Text:
        """渲染 choice_input 底部操作提示行，按光标是否在输入行给出可用按键。

        Args:
            on_input_row: 光标是否落在末行输入行。
        Returns:
            操作提示 Text（单行，浅色）。
        """
        if on_input_row:
            return Text("↑↓ 选择行 · Enter 提交输入 · Esc 取消", style="bright_black")
        return Text("↑↓ 选择行 · Enter/数字 选项 · Esc 取消", style="bright_black")

    def _render_form(self) -> ANSI:
        """渲染单屏标签页表单：顶部标签栏 + 聚焦标签内容 + 操作提示，供 form_window 使用。

        标签栏逐题「问题N」加末尾「提交」标签，聚焦项反显；问题标签展示题干与答案行（单选 ●/○ 单选圈+编号、
        多选 [x]/[ ] 复选框，末行恒有自定义输入行），光标行反显；「提交」标签展示逐题作答小结。
        底部讨论栏由常驻输入框承载，不在此渲染。

        Returns:
            可作为 Window 内容的 ANSI（多行）；无活跃表单时为空。
        """
        if not self._form_questions:
            return ANSI("")
        text = Text("\n")
        text.append_text(self._render_form_tabs())
        text.append("\n\n")
        if self._form_on_submit_tab():
            text.append_text(self._render_form_submit_page())
        else:
            text.append_text(self._render_form_question_page())
        text.append("\n")
        text.append_text(self._render_form_hint())
        with self._status_console.capture() as capture:
            self._status_console.print(text, end="")
        return ANSI(capture.get())

    def _render_form_tabs(self) -> Text:
        """渲染顶部标签栏：逐题「答题状态 + header 简介」（已答 ☑ 未答 ☐；header 截断至 12 列，空则回退「问题N」）+ 末尾「提交」，聚焦标签反显（问题青、提交绿），余压暗。

        Returns:
            标签栏 Text（单行）。
        """
        tabs = Text()
        for qi in range(len(self._form_questions)):
            header = self._form_questions[qi].header
            label_text = _fit_display_width(header, 12) if header else f"问题{qi + 1}"
            answered = bool(self._collect_form_answer(qi, self._form_questions[qi]).strip())
            marker = "☑" if answered else "☐"
            label = Text(f" {marker} {label_text} ")
            label.stylize("reverse cyan" if qi == self._form_focus else "bright_black")
            tabs.append_text(label)
            tabs.append(" ")
        submit = Text(" 提交 ")
        submit.stylize("reverse green" if self._form_on_submit_tab() else "bright_black")
        tabs.append_text(submit)
        return tabs

    def _render_form_question_page(self) -> Text:
        """渲染聚焦问题标签页：题干（加粗）+ 各答案行（选项行，含参考说明的紧随浅色缩进副行 + 自定义输入行），光标行反显。

        Returns:
            问题标签页内容 Text（多行）。
        """
        qi = self._form_focus
        question = self._form_questions[qi]
        multi = question.options is not None and question.multi_select
        cursor = self._form_row[qi]
        body = Text()
        title = self._markdown_label(question.question) if self._form_markdown else Text(question.question)
        title.stylize("bold")  # 题干加粗（Markdown 分支叠加在既有样式之上）
        body.append_text(title)
        body.append("\n")
        if question.options is not None:
            descs = question.descriptions or []
            for oi, (_value, label) in enumerate(question.options):
                body.append_text(self._render_form_option_line(qi, oi, label, multi, cursor == oi))
                body.append("\n")
                desc = descs[oi].strip() if oi < len(descs) else ""
                if desc:  # 选项参考说明：浅色缩进副行，不参与光标反显
                    sub = self._markdown_label(desc) if self._form_markdown else Text(desc)
                    sub.stylize("bright_black")
                    body.append("     ")
                    body.append_text(sub)
                    body.append("\n")
        body.append_text(self._render_form_custom_line(qi, multi, cursor == self._form_option_count(qi)))
        return body

    def _render_form_option_line(self, qi: int, oi: int, label: str, multi: bool, on_cursor: bool) -> Text:
        """渲染一个选项行；多选为 [x]/[ ] 复选框+标签，单选为 ●/○ 单选圈+编号+标签，选中项标记染绿，光标所在行整行反显。

        Args:
            qi: 问题下标。
            oi: 选项下标。
            label: 选项标签文本。
            multi: 该题是否多选。
            on_cursor: 光标是否落在本行。
        Returns:
            选项行 Text（单行）。
        """
        line = Text(" ")
        selected = oi in self._form_checked[qi]
        if multi:
            line.append("[x] " if selected else "[ ] ", style="green" if selected else "")
            line.append_text(self._markdown_label(label) if self._form_markdown else Text(label))
        else:
            line.append("● " if selected else "○ ", style="green" if selected else "")
            line.append(f"{oi + 1}. ")
            line.append_text(self._markdown_label(label) if self._form_markdown else Text(label))
        if on_cursor:
            line.stylize("reverse")
        return line

    def _render_form_custom_line(self, qi: int, multi: bool, on_cursor: bool) -> Text:
        """渲染自定义输入行；答题区光标在此且缓冲可编辑时取输入缓冲实时文本、在插入点内联绘制块状光标（不整行反显），否则取暂存文本、空则浅字占位并整行反显。
        前缀标记随「是否已填」染绿：多选 [x]/[ ] 复选框，单选 ●/○ 单选圈（自定义文本即单选的一项）。

        Args:
            qi: 问题下标。
            multi: 该题是否多选（多选前缀复选框，单选前缀单选圈）。
            on_cursor: 光标是否落在本行。
        Returns:
            自定义输入行 Text（单行）。
        """
        editing = on_cursor and self._form_zone == "answer" and self._buffer is not None and self._buffer_editable()
        value = self._buffer.text if editing else self._form_text[qi]
        line = Text(" ")
        filled = bool(value.strip())
        if multi:
            line.append("[x] " if filled else "[ ] ", style="green" if filled else "")
        else:
            line.append("● " if filled else "○ ", style="green" if filled else "")
        line.append("⌨ 其他: ", style="cyan" if on_cursor else "bright_black")
        if editing:  # 内联绘制块状光标：光标块为唯一反显，空缓冲则块光标 + 浅字占位；不整行反显
            self._append_caret_text(line, value, self._buffer.cursor_position)
            if not value:
                line.append("输入回答…", style="bright_black")
            return line
        if value:
            line.append(value)
        else:
            line.append("输入回答…", style="bright_black")
        if on_cursor:
            line.stylize("reverse")
        return line

    def _append_caret_text(self, line: Text, text: str, pos: int) -> None:
        """把文本连同插入点块状光标就地追加到行：插入点处字符反显作块光标；插入点在行尾或文本为空时以反显空格作块光标。

        Args:
            line: 目标 Text（就地追加，不返回）。
            text: 输入缓冲文本。
            pos: 插入点（光标）在文本中的下标。
        """
        pos = max(0, min(pos, len(text)))
        line.append(text[:pos])
        if pos < len(text):
            line.append(text[pos], style="reverse")
            line.append(text[pos + 1:])
        else:
            line.append(" ", style="reverse")

    def _render_form_submit_page(self) -> Text:
        """渲染「提交」标签页：逐题作答小结（已答显示答案，未答标注「未作答」）。

        Returns:
            提交标签页内容 Text（多行）。
        """
        body = Text()
        body.append("回答小结\n", style="bold green")
        for qi, question in enumerate(self._form_questions):
            answer = self._collect_form_answer(qi, question).strip()
            line = Text(" ")
            line.append(f"问题{qi + 1}：", style="cyan")
            if answer:
                line.append(answer)
            else:
                line.append("未作答", style="bright_black")
            body.append_text(line)
            body.append("\n")
        return body

    def _render_form_hint(self) -> Text:
        """渲染底部操作提示行，按焦点区与标签类型给出可用按键。

        Returns:
            操作提示 Text（单行）。
        """
        if self._form_zone == "discuss":
            return Text("Tab 返回答题 · Enter 提交 · Esc 取消", style="bright_black")
        if self._form_on_submit_tab():
            return Text("Enter 提交 · ←→ 返回修改 · Esc 取消", style="bright_black")
        on_custom = self._form_cursor_on_custom()
        keys = ["↑↓ 选择行"]
        if not on_custom and self._form_focused_multi():
            keys.append("空格 勾选")
        elif not on_custom and self._form_focused_single():
            keys.append("Enter/空格 选中")
        else:
            keys.append("Enter 确认")
        keys += ["←→ 切换标签", "Esc 取消"]
        return Text(" · ".join(keys), style="bright_black")

    def _render_form_footer(self) -> ANSI:
        """渲染表单答题区底部提示行（输入栏隐藏时占其位）：暗色提示按 Tab 唤出讨论输入栏，文案与输入栏占位一致。

        Returns:
            可作为 Window 内容的 ANSI（单行暗色提示）。
        """
        with self._status_console.capture() as capture:
            self._status_console.print(Text("Tab 讨论这几个问题…", style="bright_black"), end="")
        return ANSI(capture.get())

    def _render_completions(self) -> ANSI:
        """渲染斜杠命令补全下拉（每候选一行「<命令>  —  <描述>」），高亮当前选定项；供 completion_window 使用。

        Returns:
            可作为 Window 内容的 ANSI（多行）；无补全候选时为空。
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

    def _render_separator(self) -> ANSI:
        """构建一行占满终端宽度的暗色分割线 ANSI（框住输入框，其上、下各一条）。

        Returns:
            可作为 Window 内容的 ANSI（单行分割线）。
        """
        separator = Text("─" * self._render_width, style="bright_black")
        with self._status_console.capture() as capture:
            self._status_console.print(separator, end="")
        return ANSI(capture.get())

    def _render_core_status(self) -> ANSI:
        """构建底部核心状态行的 ANSI：「<权限模式> (Shift+Tab 切换) · ↑总输入 (缓存命中%) ↓输出 · 耗时s [· Ctrl+C 中断] [· ↓查看 agent]」。

        处理态（有活动）显示本回合实时累计耗时并追加「Ctrl+C 中断」提示；
        其余（可输入态、或提交后首个处理事件前的空闲）显示上一回合最终耗时、不带中断提示。

        Returns:
            可作为 Window 内容的 ANSI（单行核心状态）。
        """
        processing = self._mode == "processing" and bool(self._activity)
        elapsed = self._elapsed(self._turn_started_monotonic, time.monotonic()) if processing else self._last_elapsed
        status = Text()
        self._append_core_status(status, elapsed)
        if processing:
            status.append("  ·  ", style="bright_black")
            status.append("Ctrl+C 中断", style="bright_black")
        if self._has_sub_agents():
            status.append("  ·  ", style="bright_black")
            status.append("↓查看 agent", style="bright_black")
        with self._status_console.capture() as capture:
            self._status_console.print(status, end="")
        return ANSI(capture.get())

    def _render_agent_list(self) -> ANSI:
        """渲染 agent 列表（每 agent 一行），供 agent_list_window 使用。

        主 agent 行置顶，其余子 agent 按插入序。每行格式：
        <标记> <agent_type> <uuid8> <状态> <token> · <elapsed>s
        选中行反显（列表聚焦时）。行数 > 8 时按选中项裁出可视窗口段。

        Returns:
            可作为 Window 内容的 ANSI（多行）。
        """
        if self._rows_provider is None:
            return ANSI("")
        rows = self._rows_provider()
        if not rows:
            return ANSI("")

        # 滑动窗口：行数 > 8 时按选中项裁取可见段
        max_visible = 8
        visible_rows = list(rows)
        start = 0
        if len(visible_rows) > max_visible:
            start = max(0, min(self._agent_selected_index - 3, len(visible_rows) - max_visible))
            # 夹取：确保 select 在 [start, start+max_visible) 内
            if self._agent_selected_index >= start + max_visible:
                start = self._agent_selected_index - max_visible + 1
            elif self._agent_selected_index < start:
                start = self._agent_selected_index
            start = max(0, min(start, len(visible_rows) - max_visible))
            visible_rows = visible_rows[start:start + max_visible]

        # 列表是否已聚焦（选中行反显）
        focused = (
            self._app is not None
            and self._agent_list_inner is not None
            and self._app.layout.has_focus(self._agent_list_inner)
        )

        now = time.monotonic()
        text = Text()
        for i, row in enumerate(visible_rows):
            actual_idx = start + i
            is_selected = focused and actual_idx == self._agent_selected_index

            # elapsed 实时计算
            if row.ended_monotonic is not None and row.started_monotonic is not None:
                elapsed = row.ended_monotonic - row.started_monotonic
            elif row.running and row.started_monotonic is not None:
                elapsed = now - row.started_monotonic
            else:
                elapsed = 0.0

            # token 格式化
            total_in = row.in_tokens
            hit_pct = (row.cache_read / total_in * 100) if total_in else 0.0
            status = "运行中" if row.running else "已完成"
            uid8 = row.uuid.split("-")[0] if row.uuid else ""
            marker = "⏺" if row.is_main else "◯"

            # 整行样式：选中时反显
            style = "reverse" if is_selected else ""

            line = Text()
            line.append(f"{marker} ", style=style)
            if row.is_main:
                # 主 agent 行只显示标记 + 类型（其输出已在滚动区实时可见）
                line.append(row.agent_type, style=style)
            else:
                # 子 agent 行：完整信息
                line.append(row.agent_type, style=style)
                line.append(f"{' ' * 2}{uid8:<10}", style=style)
                line.append(f"{status:<6}", style=style)
                line.append(f" ↑{self._format_token_count(total_in)}", style=style)
                line.append(f" ({hit_pct:.0f}%)", style="bright_black")
                line.append(f" ↓{self._format_token_count(row.out_tokens)}", style=style)
                line.append(f"  ·  {elapsed:.1f}s", style=style)

            text.append(line)
            if i < len(visible_rows) - 1:
                text.append("\n")

        with self._status_console.capture() as capture:
            self._status_console.print(text, end="")
        return ANSI(capture.get())

    def _viewing_row(self) -> AgentRow | None:
        """返回当前查看的子 agent 行快照（按 _viewing_uuid 在 rows_provider 中查找）。

        Returns:
            匹配的 AgentRow；未查看、无数据源或已被裁剪（prune）时返回 None。
        """
        if self._viewing_uuid is None or self._rows_provider is None:
            return None
        for row in self._rows_provider():
            if row.uuid == self._viewing_uuid:
                return row
        return None

    def _render_transcript_header(self) -> ANSI:
        """渲染转录面板顶部标题行：「── <agent_type> <uuid8>（运行中/已完成）── <跟随态> · PgUp/PgDn 滚动 · Esc 关闭」。

        跟随态：_view_scroll==0 显示「实时」，否则显示「已上滚 N 行」。
        agent 已被裁剪（rows 中查不到）时退化显示 uuid8 + 「已结束」。

        Returns:
            可作为 Window 内容的 ANSI（单行标题）。
        """
        uid8 = self._viewing_uuid.split("-")[0] if self._viewing_uuid else ""
        row = self._viewing_row()
        if row is not None:
            label = self._agent_label(row.agent_type, row.uuid)
            status = "运行中" if row.running else "已完成"
        else:
            label = uid8
            status = "已结束"
        follow = "实时" if self._view_scroll == 0 else f"已上滚 {self._view_scroll} 行"
        header = Text()
        header.append("── ", style="bright_black")
        header.append(label, style="bold")
        header.append(f"（{status}）", style="bright_black")
        header.append(" ──  ", style="bright_black")
        header.append(follow, style="cyan")
        header.append("  ·  PgUp/PgDn 滚动 · Esc 关闭", style="bright_black")
        with self._status_console.capture() as capture:
            self._status_console.print(header, end="")
        return ANSI(capture.get())

    def _render_transcript_panel(self) -> ANSI:
        """渲染转录面板内容：把当前查看 agent 的转录按宽度折行后，切出贴底（或上滚后）的可视段。

        借常驻 App 的 100ms 重绘实现实时跟随（tail -f）：_view_scroll==0 时恒切末段。
        _view_scroll 在此就地夹取到合法上界，使越界的 PgUp 自然停在顶部。

        Returns:
            可作为 Window 内容的 ANSI（至多 _TRANSCRIPT_PANEL_ROWS 行）。
        """
        if self._transcript_provider is None or self._viewing_uuid is None:
            return ANSI("")
        segments = self._transcript_provider(self._viewing_uuid)
        lines = self._transcript_lines(self._viewing_uuid, segments)
        max_scroll = max(0, len(lines) - _TRANSCRIPT_PANEL_ROWS)
        self._view_scroll = min(self._view_scroll, max_scroll)  # 就地夹取上界
        start = max_scroll - self._view_scroll
        return ANSI("\n".join(lines[start:start + _TRANSCRIPT_PANEL_ROWS]))

    def _transcript_lines(self, uuid: str, segments: list[tuple[str, str]]) -> list[str]:
        """把转录分段渲染为可滚动的 ANSI 行列表：response/thinking 走 Markdown、tool 走纯文本。

        带缓存：签名 = (uuid, 段数, 各段文本总长, 渲染宽度)；签名不变直接复用已渲染行，
        仅在转录增长/切换 agent/改宽度时重渲，避免对增长缓冲每帧重复解析 Markdown。

        Args:
            uuid: 当前查看的 agent uuid（参与缓存签名，切换 agent 即失效）。
            segments: 转录分段列表，每项为 (kind, text)。
        Returns:
            渲染后的 ANSI 文本按 "\\n" 切分的行列表。
        """
        signature = (uuid, len(segments), sum(len(text) for _, text in segments), self._render_width)
        if self._transcript_cache is not None and self._transcript_cache[0] == signature:
            return self._transcript_cache[1]
        parts: list[str] = []
        for kind, text in segments:
            if kind == "response":
                parts.append(render_markdown(text, width=self._render_width))
            elif kind == "thinking":
                parts.append(render_markdown(text, width=self._render_width, base_style="dim"))
            else:  # tool：工具调用 chrome 行，纯文本渲染
                with self._status_console.capture() as capture:
                    self._status_console.print(Text(text), end="")
                parts.append(capture.get())
        lines = "".join(parts).split("\n")
        self._transcript_cache = (signature, lines)
        return lines

    def _append_core_status(self, line: Text, elapsed: float) -> None:
        """把核心状态段「<权限模式> (Shift+Tab 切换) · ↑总输入 (缓存命中%) ↓输出 · <耗时>s」原地追加。

        Args:
            line: 目标 Rich Text，原地追加内容。
            elapsed: 要显示的耗时秒数。处理态传本回合实时累计耗时，可输入态传上一回合的最终耗时。
        """
        line.append(self.get_system_state().permission_mode)
        if self._permission_mode_toggle_handler is not None:
            line.append(" (Shift+Tab 切换)", style="bright_black")
        line.append("  ·  ", style="bright_black")
        self._append_token_segment(line)
        line.append("  ·  ", style="bright_black")
        line.append(f"{elapsed:.1f}s")

    def _append_token_segment(self, line: Text) -> None:
        """把「↑总输入 (缓存命中百分比) ↓输出」token 段原地追加到给定 Text。

        总输入 = 本会话累计提交给模型的输入 token（已含缓存读取/写入）；
        括号内百分比 = 本会话累计缓存读取 / 总输入。

        Args:
            line: 目标 Rich Text，原地追加内容。
        """
        total_in = self._session_in_tokens
        hit_pct = (self._session_cache_read_tokens / total_in * 100) if total_in else 0.0
        line.append(f"↑{self._format_token_count(total_in)}")
        line.append(f" ({hit_pct:.0f}%)", style="bright_black")
        line.append(f" ↓{self._format_token_count(self._session_out_tokens)}")

    def _format_token_count(self, count: int) -> str:
        """把 token 数格式化为紧凑字符串（≥1000 显示为 k）。

        Args:
            count: token 数量。
        Returns:
            紧凑字符串，如 "0"、"640"、"1.2k"。
        """
        if count < 1000:
            return str(count)
        return f"{count / 1000:.1f}k"

    def set_agent_source(
        self,
        rows_provider: Callable[[], list[AgentRow]],
        transcript_provider: Callable[[str], list[tuple[str, str]]],
    ) -> None:
        """注入 agent 列表数据源与转录查询函数（来自 OutputRouter）。

        Args:
            rows_provider: 返回 agent 行快照列表的无参 callable。
            transcript_provider: 接受 uuid 返回转录分段 (kind, text) 列表的 callable。
        """
        self._rows_provider = rows_provider
        self._transcript_provider = transcript_provider

    def _has_sub_agents(self) -> bool:
        """检查当前是否有子 agent（列表是否应该可见）。

        Returns:
            True 当 rows_provider 已装配且存在非 main 的行。
        """
        if self._rows_provider is None:
            return False
        try:
            return any(not r.is_main and r.running for r in self._rows_provider())
        except Exception:
            return False

    @staticmethod
    def _elapsed(start: float | None, now: float) -> float:
        """计算从 start 到 now 的耗时秒数；start 为 None（未开始计时）时返回 0.0。

        Args:
            start: 起点 monotonic 秒，None 表示尚未开始。
            now: 当前 monotonic 秒。
        Returns:
            耗时秒数（start 为 None 时为 0.0）。
        """
        return now - start if start is not None else 0.0

    def _set_activity(self, activity: str) -> None:
        """进入处理态并设置当前活动文案，驱动底部状态条 spinner / 活动显示。

        首次记录本回合处理起点；活动切换时重置本步耗时起点。

        Args:
            activity: 当前活动文案（如"思考中"、"回应中"、工具名；空串为提交后的空闲态）。
        """
        activity_changed = activity != self._activity
        self._activity = activity
        self._mode = "processing"
        now = time.monotonic()
        if self._turn_started_monotonic is None:
            self._turn_started_monotonic = now
        if activity_changed:
            self._activity_started_monotonic = now  # 活动切换时重置本步耗时起点

    def _reset_turn_status(self) -> None:
        """清零单回合状态：整轮/本步耗时起点、活动文案与当前 agent。在每次进入输入阶段时调用。

        token 统计按会话累计、仅 /clear 时清零（见 reload），不属于单回合状态。
        """
        self._turn_started_monotonic = None
        self._activity_started_monotonic = None
        self._activity = ""
        self._current_agent_type = None
        self._current_agent_uuid = None

    def _active_agent_name(self) -> str:
        """返回活动行要显示的当前 agent 名：主 agent 与子 agent 均显示其 agent_type；
        agent_type 为空时（回合起始 reset 后的短暂空窗）回退「助手」。

        Returns:
            agent 显示名，如「main」「coder」「explore」或「助手」。
        """
        return self._agent_label(self._current_agent_type, self._current_agent_uuid) or "助手"

    def _set_current_agent(self, agent_type: str | None, agent_uuid: str | None) -> None:
        """记录当前正在工作的 agent，供活动行显示。

        Args:
            agent_type: 事件携带的 agent 类型（主 agent 为其 agent_type，如「main」）。
            agent_uuid: 事件携带的 agent 实例 uuid。
        """
        self._current_agent_type = agent_type
        self._current_agent_uuid = agent_uuid

    def reload(self) -> None:
        """/clear 重置会话时清零本会话累计的 token 统计、agent 列表选中态与转录面板查看态。"""
        self._session_in_tokens = 0
        self._session_out_tokens = 0
        self._session_cache_read_tokens = 0
        self._agent_selected_index = 0
        self._viewing_uuid = None
        self._view_scroll = 0
        # 列表隐藏自愈：若焦点在 agent 列表，切回输入框
        if (
            self._app_running
            and self._agent_list_inner is not None
            and self._app is not None
            and self._app.layout.has_focus(self._agent_list_inner)
        ):
            self._app.layout.focus(self._input_window)

    def on_system_state_changed(self) -> None:
        """系统状态（如权限模式）变化：重绘状态条以立即反映新模式。"""
        if self._app_running:
            self._app.invalidate()

    def set_permission_mode_toggle_handler(self, handler: Callable[[], None] | None) -> None:
        """登记输入态 Shift+Tab 的权限模式切换回调（None 表示不可用，状态栏据此决定是否提示）。"""
        self._permission_mode_toggle_handler = handler

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
        self._last_elapsed = self._elapsed(self._turn_started_monotonic, time.monotonic())
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
        loop = asyncio.get_running_loop()
        self._input_future = loop.create_future()
        self._mode = "input"
        # 实时夺焦：若当前焦点在 agent 列表，切回输入框确保按键到达应答 future
        if (
            self._app is not None
            and self._agent_list_inner is not None
            and self._app.layout.has_focus(self._agent_list_inner)
        ):
            self._app.layout.focus(self._input_window)
        self._buffer.set_document(Document(default, len(default)), bypass_readonly=True)
        self._app.invalidate()
        try:
            return await self._input_future
        finally:
            self._input_future = None
            self._enter_processing_idle()

    async def _await_selection(
        self, options: list[tuple[str, str]], default_index: int, cancel_value: str, markdown: bool = False
    ) -> str:
        """进入只读选择菜单（select 态），await 由方向键/数字/Enter/Esc 绑定解析的 future。

        若当前焦点在 agent 列表，先抢回输入框（否则按键落到列表而非应答 future）。

        Args:
            options: 选项列表，每项为 (value, label)。
            default_index: 初始选中项下标（夹取到合法范围）。
            cancel_value: Esc 取消时解析返回的 value。
            markdown: 选项标签是否按 Markdown 渲染。
        Returns:
            所选项的 value，或 Esc 取消时的 cancel_value。
        """
        loop = asyncio.get_running_loop()
        self._select_options = options
        self._select_index = max(0, min(default_index, len(options) - 1)) if options else 0
        self._select_cancel_value = cancel_value
        self._select_markdown = markdown
        self._mode = "select"
        self._input_future = loop.create_future()
        if (
            self._app is not None
            and self._agent_list_inner is not None
            and self._app.layout.has_focus(self._agent_list_inner)
        ):
            self._app.layout.focus(self._input_window)
        self._app.invalidate()
        try:
            return await self._input_future
        finally:
            self._input_future = None
            self._select_options = None
            self._enter_processing_idle()

    def _buffer_editable(self) -> bool:
        """输入缓冲当前是否可编辑：input 态恒可编辑；form 态讨论区或答题区光标落在自定义输入行时可编辑；
        choice_input 态光标落在输入行时可编辑；其余只读。

        Returns:
            缓冲是否可编辑。
        """
        if self._mode == "input":
            return True
        if self._mode == "form":
            if self._form_zone == "discuss":
                return True
            return self._form_cursor_on_custom()
        if self._mode == "choice_input":
            return self._choice_input_on_input_row()
        return False

    def _buffer_placeholder(self) -> str:
        """底部输入框缓冲为空时的浅字占位文案；不需占位的态返回空串。

        Returns:
            form 态答题区自定义行为「输入自定义回答…」、其余 form 态为「讨论这几个问题…」；
            choice_input 态为调用方给定的 input_placeholder；其它态为空串。
        """
        if self._mode == "form":
            if self._form_zone == "answer" and self._form_cursor_on_custom():
                return "输入自定义回答…"
            return "讨论这几个问题…"
        if self._mode == "choice_input":
            return self._choice_input_placeholder
        return ""

    def _current_form_question(self) -> FormQuestion | None:
        """返回当前聚焦的表单问题；无活跃表单、聚焦「提交」标签或下标越界时返回 None。

        Returns:
            当前聚焦的 FormQuestion，或 None。
        """
        if not self._form_questions or not (0 <= self._form_focus < len(self._form_questions)):
            return None
        return self._form_questions[self._form_focus]

    def _form_on_submit_tab(self) -> bool:
        """当前是否聚焦「提交」标签（焦点下标等于问题数）。

        Returns:
            聚焦「提交」标签时为 True。
        """
        return bool(self._form_questions) and self._form_focus == len(self._form_questions)

    def _form_option_count(self, qi: int) -> int:
        """第 qi 题的选项数（自由文本题为 0）。

        Args:
            qi: 问题下标。
        Returns:
            该题选项数量。
        """
        question = self._form_questions[qi]
        return len(question.options) if question.options else 0

    def _form_cursor_on_custom(self) -> bool:
        """答题区光标当前是否落在聚焦问题的自定义输入行（提交标签或无表单时恒 False）。

        Returns:
            光标在自定义输入行时为 True。
        """
        if self._current_form_question() is None:
            return False
        return self._form_row[self._form_focus] >= self._form_option_count(self._form_focus)

    def _form_focused_multi(self) -> bool:
        """聚焦问题是否为多选题（有 options 且 multi_select）。

        Returns:
            聚焦问题为多选题时为 True。
        """
        question = self._current_form_question()
        return question is not None and question.options is not None and question.multi_select

    def _form_focused_single(self) -> bool:
        """聚焦问题是否为单选题（有 options 且非 multi_select）。

        Returns:
            聚焦问题为单选题时为 True。
        """
        question = self._current_form_question()
        return question is not None and question.options is not None and not question.multi_select

    def _form_answering(self) -> bool:
        """是否处于表单答题区（form 态且焦点在 answer 区，即输入栏隐藏、底部提示显示之态）。

        Returns:
            form 态且 _form_zone == "answer" 时为 True。
        """
        return self._mode == "form" and self._form_zone == "answer"

    def _input_bar_visible(self) -> bool:
        """底部输入栏当前是否可见：除表单答题区外均可见（表单答题区隐藏输入栏，改由内联自定义行与底部提示承载）。

        choice_input 态输入栏恒可见——它即该菜单的输入行。

        Returns:
            输入栏应显示时为 True。
        """
        return not self._form_answering()

    def _choice_input_on_input_row(self) -> bool:
        """choice_input 态光标当前是否落在末行输入行（非 choice_input 态或无活跃菜单时恒 False）。

        Returns:
            光标在输入行（下标 == 选项数）时为 True。
        """
        if self._choice_input_options is None:
            return False
        return self._choice_input_index >= len(self._choice_input_options)

    def _hide_real_cursor(self) -> bool:
        """是否隐藏输入框真实光标：表单答题区（光标改由自定义行内联绘制）、
        或 choice_input 光标停在选项行（输入行未激活、输入框只读）时隐藏。

        Returns:
            应隐藏真实光标时为 True。
        """
        if self._form_answering():
            return True
        return self._mode == "choice_input" and not self._choice_input_on_input_row()

    async def _await_form(self, questions: list[FormQuestion], markdown: bool) -> str:
        """进入单屏表单态（form），await 由表单键位（Enter 提交 / Esc 取消）解析的 future。

        初始化各题光标/勾选/文本与讨论状态并聚焦首标签；若焦点在 agent 列表先抢回输入框（否则按键落到列表而非应答 future）。

        Args:
            questions: 问题列表。
            markdown: 问题/选项标签是否按 Markdown 渲染。
        Returns:
            JSON 编码的 {"answers": [...], "discussion": "..."} 对象串；Esc 取消时为空串。
        """
        loop = asyncio.get_running_loop()
        self._form_questions = questions
        self._form_row = [0] * len(questions)
        self._form_checked = [set() for _ in questions]
        self._form_text = [""] * len(questions)
        self._form_discussion = ""
        self._form_focus = 0
        self._form_zone = "answer"
        self._form_markdown = markdown
        self._mode = "form"
        self._input_future = loop.create_future()
        if (
            self._app is not None
            and self._agent_list_inner is not None
            and self._app.layout.has_focus(self._agent_list_inner)
        ):
            self._app.layout.focus(self._input_window)
        self._form_load_buffer()
        self._app.invalidate()
        try:
            return await self._input_future
        finally:
            self._input_future = None
            self._form_questions = None
            self._enter_processing_idle()

    def _form_commit_buffer(self) -> None:
        """把输入框当前文本提交回当前 sink：答题区光标在自定义行→该题自定义文本（单选题填入非空文本时
        清除其已选选项，保持单选互斥），否则→讨论栏。"""
        if self._buffer is None:
            return
        if self._form_zone == "answer" and self._form_cursor_on_custom():
            qi = self._form_focus
            self._form_text[qi] = self._buffer.text
            question = self._form_questions[qi]
            if question.options is not None and not question.multi_select and self._buffer.text.strip():
                self._form_checked[qi].clear()
        else:
            self._form_discussion = self._buffer.text

    def _form_load_buffer(self) -> None:
        """把当前 sink 文本载入输入框：答题区光标在自定义行→载该题自定义文本，否则→载讨论栏文本。"""
        if self._buffer is None:
            return
        if self._form_zone == "answer" and self._form_cursor_on_custom():
            value = self._form_text[self._form_focus]
        else:
            value = self._form_discussion
        self._buffer.set_document(Document(value, len(value)), bypass_readonly=True)

    def _move_form_focus(self, delta: int) -> None:
        """在标签间移动焦点（含末尾「提交」标签），到边界夹取、不循环。

        Args:
            delta: 移动步长（+1 下一标签、-1 上一标签）。
        """
        if not self._form_questions:
            return
        self._form_commit_buffer()
        self._form_focus = max(0, min(self._form_focus + delta, len(self._form_questions)))
        self._form_load_buffer()

    def _move_form_row(self, delta: int) -> None:
        """在聚焦问题的答案行间移动光标（选项行 + 自定义输入行），到边界夹取；提交标签无操作。

        Args:
            delta: 移动步长（+1 下一行、-1 上一行）。
        """
        if self._current_form_question() is None:
            return
        self._form_commit_buffer()
        last = self._form_option_count(self._form_focus)  # 自定义行下标 == 选项数
        self._form_row[self._form_focus] = max(0, min(self._form_row[self._form_focus] + delta, last))
        self._form_load_buffer()

    def _toggle_form_zone(self) -> None:
        """在答题区与底部讨论栏之间切换焦点（切换前后同步输入框 sink）。"""
        if not self._form_questions:
            return
        self._form_commit_buffer()
        self._form_zone = "discuss" if self._form_zone == "answer" else "answer"
        self._form_load_buffer()

    def _toggle_form_option(self) -> None:
        """翻转聚焦问题当前光标所在选项的选中态：多选题增删该项；单选题为单选切换——选中它并清除其余选项，
        再次切换同一已选项则取消（回到未作答），选中时同步清空该题自定义文本以保持单选互斥。
        无选项题或光标在自定义行时无操作。"""
        question = self._current_form_question()
        if question is None or question.options is None or self._form_cursor_on_custom():
            return
        qi = self._form_focus
        row = self._form_row[qi]
        selected = self._form_checked[qi]
        if question.multi_select:
            if row in selected:
                selected.discard(row)
            else:
                selected.add(row)
        elif row in selected:
            selected.clear()
        else:
            selected.clear()
            selected.add(row)
            self._form_text[qi] = ""

    def _form_number(self, index: int) -> None:
        """数字键操作聚焦问题的第 index 选项并把光标跳到该行：多选题翻转其勾选；单选题直接选中它
        （替换其余选项并清空该题自定义文本，保持单选互斥）；越界忽略。

        Args:
            index: 选项下标（0 基）。
        """
        question = self._current_form_question()
        if question is None or question.options is None or not (0 <= index < len(question.options)):
            return
        qi = self._form_focus
        selected = self._form_checked[qi]
        if question.multi_select:
            if index in selected:
                selected.discard(index)
            else:
                selected.add(index)
        else:
            selected.clear()
            selected.add(index)
            self._form_text[qi] = ""
        self._form_row[qi] = index

    def _confirm_question(self) -> None:
        """确认当前问题（暂存自定义文本）并自动切到下一标签；末题后落到「提交」标签。"""
        if not self._form_questions:
            return
        self._form_commit_buffer()
        self._form_focus = min(self._form_focus + 1, len(self._form_questions))
        self._form_load_buffer()

    def _collect_form_answer(self, qi: int, question: FormQuestion) -> str:
        """汇总第 qi 题的最终答案字符串。

        Args:
            qi: 问题下标。
            question: 对应的 FormQuestion。
        Returns:
            自由文本题为其自定义文本；单选题取选中项 value（未选中任何选项则取自定义文本，皆无为空串=未作答）；
            多选题为全部勾选项 value 与非空自定义文本以「、」连接。
        """
        if question.options is None:
            return self._form_text[qi]
        if question.multi_select:
            parts = [question.options[i][0] for i in sorted(self._form_checked[qi]) if i < len(question.options)]
            if self._form_text[qi].strip():
                parts.append(self._form_text[qi].strip())
            return "、".join(parts)
        if self._form_checked[qi]:  # 单选：已选选项优先
            oi = min(self._form_checked[qi])
            if oi < len(question.options):
                return question.options[oi][0]
        return self._form_text[qi]  # 未选中任何选项：取自定义文本（空串=未作答）

    def _submit_form(self) -> None:
        """收集全部作答与讨论文本（先提交当前输入框），以 JSON({answers,discussion}) 解析表单 future。"""
        if not self._form_questions:
            return
        self._form_commit_buffer()
        answers = [self._collect_form_answer(qi, question) for qi, question in enumerate(self._form_questions)]
        self._resolve_input(json.dumps({"answers": answers, "discussion": self._form_discussion}))

    def _cancel_form(self) -> None:
        """取消表单：以空串解析 future（上层 request_form 据此返回 ([], "") 表示取消）。"""
        self._resolve_input("")

    async def _await_choice_input(
        self,
        options: list[tuple[str, str]],
        descriptions: list[str] | None,
        input_placeholder: str,
        default_index: int,
        markdown: bool,
    ) -> str:
        """进入 choice_input 态，await 由 choice_input 键位（选项/输入行 Enter、数字、Esc）解析的 future。

        初始化选项/说明/占位/光标行与 Markdown 标志并清空输入缓冲；若焦点在 agent 列表先抢回输入框
        （否则按键落到列表而非应答 future）。

        Args:
            options: 选项列表，每项为 (value, label)。
            descriptions: 与 options 对齐的选项浅色说明副行；None 表示无。
            input_placeholder: 输入行为空时的浅字占位文案。
            default_index: 初始选中项下标（夹取到选项范围）。
            markdown: 上文提示与选项标签是否按 Markdown 渲染。
        Returns:
            JSON 编码的 {"choice": ..., "text": ...} 对象串；Esc 取消时为空串。
        """
        loop = asyncio.get_running_loop()
        self._choice_input_options = options
        self._choice_input_descriptions = descriptions
        self._choice_input_placeholder = input_placeholder
        self._choice_input_index = max(0, min(default_index, len(options) - 1)) if options else 0
        self._choice_input_markdown = markdown
        self._mode = "choice_input"
        self._input_future = loop.create_future()
        if (
            self._app is not None
            and self._agent_list_inner is not None
            and self._app.layout.has_focus(self._agent_list_inner)
        ):
            self._app.layout.focus(self._input_window)
        self._buffer.set_document(Document("", 0), bypass_readonly=True)
        self._app.invalidate()
        try:
            return await self._input_future
        finally:
            self._input_future = None
            self._choice_input_options = None
            self._enter_processing_idle()

    def _move_choice_input_row(self, delta: int) -> None:
        """在选项行与输入行之间移动光标（0..选项数），到边界夹取、不循环。

        跨越输入行会改变缓冲可编辑性与真实光标显隐，重绘由常驻 App 刷新完成。

        Args:
            delta: 移动步长（+1 下一行、-1 上一行）。
        """
        if not self._choice_input_options:
            return
        last = len(self._choice_input_options)  # 输入行下标 == 选项数
        self._choice_input_index = max(0, min(self._choice_input_index + delta, last))

    def _submit_choice_input_option(self) -> None:
        """以当前光标所在选项的 value 解析 choice_input future（text 为空）；光标不在选项行时无操作。"""
        if not self._choice_input_options or self._choice_input_on_input_row():
            return
        value = self._choice_input_options[self._choice_input_index][0]
        self._resolve_input(json.dumps({"choice": value, "text": ""}))

    def _submit_choice_input_text(self) -> None:
        """以输入行文本解析 choice_input future（choice 为空）；文本为空白则不提交（留在输入行）。"""
        if self._buffer is None:
            return
        text = self._buffer.text.strip()
        if not text:
            return
        self._resolve_input(json.dumps({"choice": "", "text": text}))

    def _choice_input_number(self, index: int) -> None:
        """数字键直选第 index 选项并立即提交（choice=该项 value、text 为空）；越界忽略。

        Args:
            index: 选项下标（0 基）。
        """
        if not self._choice_input_options or not (0 <= index < len(self._choice_input_options)):
            return
        value = self._choice_input_options[index][0]
        self._resolve_input(json.dumps({"choice": value, "text": ""}))

    def _cancel_choice_input(self) -> None:
        """取消 choice_input：以空串解析 future（上层 request_choice_input 据此返回 ("", "") 表示取消）。"""
        self._resolve_input("")

    def _enter_processing_idle(self) -> None:
        """回到处理态：清空输入缓冲、置处理模式并重绘（输入/权限读取结束后调用）。"""
        self._mode = "processing"
        if self._buffer is not None:
            self._buffer.set_document(Document("", 0), bypass_readonly=True)
        if self._app_running:
            self._app.invalidate()

    def _pending_input_future(self) -> asyncio.Future[str] | None:
        """返回待决的输入/权限应答 future（存在且未完成），否则 None。

        Returns:
            待决的 _input_future；无待决（为 None 或已完成）时返回 None。
        """
        future = self._input_future
        return future if (future is not None and not future.done()) else None

    def _resolve_input(self, text: str) -> None:
        """用提交文本解析待决的输入/权限应答 future（无待决则忽略）。

        Args:
            text: 用户提交的非空白文本。
        """
        future = self._pending_input_future()
        if future is not None:
            future.set_result(text)

    def _fail_input(self, exc: BaseException) -> None:
        """以异常解开待决的输入/权限读取（无待决则忽略）。

        Args:
            exc: 要置入 future 的异常（KeyboardInterrupt 或 EOFError）。
        """
        future = self._pending_input_future()
        if future is not None:
            future.set_exception(exc)

    def cancel_active_input(self) -> bool:
        """取消活跃输入：取消总线层请求 future（基类）并解开 UI 层读取 future。

        Returns:
            是否取消了总线层的活跃输入请求。
        """
        cancelled = super().cancel_active_input()
        future = self._pending_input_future()
        if future is not None:
            future.cancel()
        return cancelled

    def _render_input_context(self, prompt: str, markdown: bool = False) -> None:
        """把输入提示的「上文」打到 App 上方滚动区（输入框由常驻分割线框住，无需再打印分隔线）。

        提示末行（输入标签，如 "你: "）不打印，由输入框的彩色 › 前缀代替（见 _get_line_prefix）；
        末行之前的上文（如 ask_user 的多行问题）照常打印。

        Args:
            prompt: 上层请求的原始提示文本。
            markdown: 上文是否按 Markdown 渲染。
        """
        lines = prompt.splitlines()
        context = "\n".join(lines[:-1]).strip("\n") if len(lines) > 1 else ""
        if context.strip():
            if markdown:
                self._print_markdown(context)
            else:
                self._print_rich(context)

    async def _read_permission(
        self,
        tool_name: str,
        detail: str,
        suggested_rules: list[str] | None = None,
        mcp_server_rule: str | None = None,
    ) -> str:
        """工具权限确认：打印权限说明上文到 App 上方，再以方向键选择菜单读取决策。

        非 TTY 走扁平降级（打字 y/s/a/n，MCP 工具另加 ss/aa）。

        Args:
            tool_name: 工具名。
            detail: 权限请求详情。
            suggested_rules: 建议的 allow 规则列表，供展示。
            mcp_server_rule: MCP 工具的 server 级通配规则；非空时增加"信任整个 server"两项。
        Returns:
            "yes" / "session" / "always" / "session_server" / "always_server" / "deny"。
        """
        if not self._tty:
            return await self._read_permission_plain(tool_name, detail, suggested_rules, mcp_server_rule)
        self._print_rich(self._permission_context_text(tool_name, detail, suggested_rules), end="")
        return await self._await_selection(self._permission_options(suggested_rules, mcp_server_rule), 0, cancel_value="deny")

    async def _read_choice(
        self, prompt: str, options: list[tuple[str, str]], default_index: int, markdown: bool = False
    ) -> str:
        """以方向键选择菜单读取一次选择（通用 ChoiceMenu，如 /mode）。

        非 TTY 走扁平降级（打印编号菜单 + 读数字，纯文本）。

        Args:
            prompt: 菜单上文提示。
            options: 选项列表，每项为 (value, label)。
            default_index: 初始选中项下标。
            markdown: 上文提示与选项标签是否按 Markdown 渲染。
        Returns:
            所选项的 value；空串表示取消（Esc）。
        """
        if not self._tty:
            return await self._read_choice_plain(prompt, options, default_index)
        if prompt.strip():
            if markdown:
                self._print_markdown(prompt)
            else:
                self._print_rich(prompt)
        return await self._await_selection(options, default_index, cancel_value="", markdown=markdown)

    async def _read_form(self, prompt: str, questions: list[FormQuestion], markdown: bool = False) -> str:
        """以单屏表单读取多个问题的作答（ask_user 多问题）。

        非 TTY 走扁平降级（逐题打印后顺序读取）。

        Args:
            prompt: 表单上文提示。
            questions: 问题列表，每项带可选 (value, label) 选项（无则自由文本）。
            markdown: 上文提示与问题/选项标签是否按 Markdown 渲染。
        Returns:
            JSON 编码的 {"answers": [...], "discussion": "..."} 对象串（answers 与 questions 顺序对齐）；空串表示取消。
        """
        if not questions:
            return ""
        if not self._tty:
            return await self._read_form_plain(prompt, questions)
        if prompt.strip():
            if markdown:
                self._print_markdown(prompt)
            else:
                self._print_rich(prompt)
        return await self._await_form(questions, markdown)

    async def _read_choice_input(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        descriptions: list[str] | None,
        input_placeholder: str,
        default_index: int,
        markdown: bool = False,
    ) -> str:
        """以「选项列表 + 输入行」读取一次作答（choice_input，如 exit_plan_mode）。

        非 TTY 走扁平降级（编号菜单 + 读一行：合法编号返回该项，否则整行作输入文本）。

        Args:
            prompt: 菜单上文提示。
            options: 选项列表，每项为 (value, label)。
            descriptions: 与 options 对齐的选项浅色说明副行；None 表示无。
            input_placeholder: 输入行为空时的浅字占位文案。
            default_index: 初始选中项下标。
            markdown: 上文提示与选项标签是否按 Markdown 渲染。
        Returns:
            JSON 编码的 {"choice": ..., "text": ...} 对象串；空串表示取消。
        """
        if not self._tty:
            return await self._read_choice_input_plain(prompt, options, descriptions, input_placeholder, default_index)
        if prompt.strip():
            if markdown:
                self._print_markdown(prompt)
            else:
                self._print_rich(prompt)
        return await self._await_choice_input(options, descriptions, input_placeholder, default_index, markdown)

    def _permission_options(self, suggested_rules: list[str] | None, mcp_server_rule: str | None = None) -> list[tuple[str, str]]:
        """构建权限确认的菜单选项（value, label）。

        Args:
            suggested_rules: 建议的 allow 规则列表（非空时 session/always 标注「上述规则」）。
            mcp_server_rule: MCP 工具的 server 级通配规则；非空时追加"信任整个 server"两项。
        Returns:
            选项列表：允许一次 / 会话允许 / 始终允许并保存 /（MCP）会话信任整 server / 始终信任整 server / 拒绝。
        """
        session_label = "会话允许(上述规则)" if suggested_rules else "本次会话始终允许"
        always_label = "始终允许并保存(上述规则)" if suggested_rules else "始终允许并保存"
        options = [
            ("yes", "允许一次"),
            ("session", session_label),
            ("always", always_label),
        ]
        if mcp_server_rule:
            options.append(("session_server", f"会话信任整个 server({mcp_server_rule})"))
            options.append(("always_server", f"始终信任整个 server 并保存({mcp_server_rule})"))
        options.append(("deny", "拒绝 (esc)"))
        return options

    def _permission_context_text(self, tool_name: str, detail: str, suggested_rules: list[str] | None) -> Text:
        """构建权限确认上文（工具/内容/建议规则），供菜单上文与非 TTY 降级共用，不含操作提示。

        Args:
            tool_name: 工具名。
            detail: 权限请求详情。
            suggested_rules: 建议的 allow 规则列表，供展示。
        Returns:
            可经 _print_rich 输出的 Rich Text。
        """
        prompt_text = Text()
        prompt_text.append("\n")
        prompt_text.append("工具请求权限", style="yellow")
        prompt_text.append(f"\n  工具: {tool_name}\n")
        prompt_text.append(f"  内容: {detail}\n")
        if suggested_rules:
            if len(suggested_rules) == 1:
                prompt_text.append(f"  建议规则: {suggested_rules[0]}\n")
            else:
                prompt_text.append("  建议规则:\n")
                for rule_str in suggested_rules:
                    prompt_text.append(f"    - {rule_str}\n")
        return prompt_text

    def _permission_prompt_text(self, tool_name: str, detail: str, suggested_rules: list[str] | None, mcp_server_rule: str | None = None) -> Text:
        """构建权限确认说明块（上文 + 打字操作提示），供非 TTY 降级使用。

        Args:
            tool_name: 工具名。
            detail: 权限请求详情。
            suggested_rules: 建议的 allow 规则列表，供展示。
            mcp_server_rule: MCP 工具的 server 级通配规则；非空时追加 ss/aa 信任整 server 提示。
        Returns:
            可经 _print_rich 输出的 Rich Text。
        """
        session_label = "会话允许(上述规则)" if suggested_rules else "本次会话始终允许"
        always_label = "始终允许并保存(上述规则)" if suggested_rules else "始终允许并保存"
        prompt_text = self._permission_context_text(tool_name, detail, suggested_rules)
        if mcp_server_rule:
            prompt_text.append("  输入 y/s/a/ss/aa/n 后按 Enter 确认\n")
        else:
            prompt_text.append("  输入 y/s/a/n 后按 Enter 确认\n")
        prompt_text.append(f"  [y] 允许一次   [s] {session_label}   [a] {always_label}   [n] 拒绝\n")
        if mcp_server_rule:
            prompt_text.append(f"  [ss] 会话信任整个 server({mcp_server_rule})   [aa] 始终信任整个 server 并保存\n")
        return prompt_text

    def _normalize_permission_answer(self, answer: str, mcp_server_rule: str | None = None) -> str | None:
        """把用户输入归一化为权限决策；非法返回 None。

        Args:
            answer: 已 strip/lower 的用户输入。
            mcp_server_rule: MCP 工具的 server 级通配规则；非空时接受 ss/aa 信任整 server。
        Returns:
            "yes"/"session"/"always"/"session_server"/"always_server"/"deny"，非法时 None。
        """
        if answer in {"y", "yes"}:
            return "yes"
        if mcp_server_rule and answer in {"ss", "session_server"}:
            return "session_server"
        if mcp_server_rule and answer in {"aa", "always_server"}:
            return "always_server"
        if answer in {"s", "session"}:
            return "session"
        if answer in {"a", "always"}:
            return "always"
        if answer in {"n", "no", "deny"}:
            return "deny"
        return None

    # ---- 非 TTY 降级（管道 / CI）----

    async def _read_input_plain(self, prompt: str, default: str) -> str:
        """非 TTY 降级输入：用单个 PromptSession.prompt_async 读取（无状态条、无常驻 App）。

        Args:
            prompt: 提示文本。
            default: 预填默认值。
        Returns:
            用户输入文本。
        """
        if self._fallback_session is None:
            self._fallback_session = PromptSession()
        return await self._fallback_session.prompt_async(
            prompt, default=default, multiline=True, prompt_continuation="... ", handle_sigint=True
        )

    async def _read_permission_plain(self, tool_name: str, detail: str, suggested_rules: list[str] | None, mcp_server_rule: str | None = None) -> str:
        """非 TTY 降级权限确认：打印说明块后用 PromptSession 循环读取 y/s/a/n（MCP 工具另加 ss/aa）。

        Args:
            tool_name: 工具名。
            detail: 权限请求详情。
            suggested_rules: 建议的 allow 规则列表，供展示。
            mcp_server_rule: MCP 工具的 server 级通配规则；非空时接受 ss/aa 信任整 server。
        Returns:
            "yes" / "session" / "always" / "session_server" / "always_server" / "deny"。
        """
        self._print_rich(self._permission_prompt_text(tool_name, detail, suggested_rules, mcp_server_rule), end="")
        if self._fallback_session is None:
            self._fallback_session = PromptSession()
        hint = "请输入 y、s、a、ss、aa 或 n。" if mcp_server_rule else "请输入 y、s、a 或 n。"
        while True:
            answer = (await self._fallback_session.prompt_async("选择: ", handle_sigint=True)).strip().lower()
            decision = self._normalize_permission_answer(answer, mcp_server_rule)
            if decision is not None:
                return decision
            self._print_rich(hint, style="red")

    def _plain_choice_menu(self, options: list[tuple[str, str]], descriptions: list[str] | None) -> Text:
        """构建非 TTY 降级选择菜单文本：逐项「[编号] 标签」，有参考说明者紧随浅色缩进副行。

        Args:
            options: 选项列表，每项为 (value, label)。
            descriptions: 与 options 对齐的参考说明（空串/越界视为无）；None 表示无任何说明。
        Returns:
            菜单 Text（多行，含结尾换行）。
        """
        menu = Text()
        for i, (_value, label) in enumerate(options):
            menu.append(f"  [{i + 1}] {label}\n")
            desc = descriptions[i].strip() if descriptions and i < len(descriptions) else ""
            if desc:
                menu.append(f"      {desc}\n", style="bright_black")
        return menu

    async def _read_choice_plain(self, prompt: str, options: list[tuple[str, str]], default_index: int,
                                 descriptions: list[str] | None = None) -> str:
        """非 TTY 降级选择：打印编号菜单后用 PromptSession 读数字，映射回 value。

        Args:
            prompt: 菜单上文提示。
            options: 选项列表，每项为 (value, label)。
            default_index: 初始选中项下标（降级路径仅用于回车空输入时的默认值）。
            descriptions: 与 options 对齐的选项参考说明，逐项以浅色副行展示（None 或空串表示无）。
        Returns:
            所选项的 value；空输入返回默认项 value，无默认时返回空串（取消）。
        """
        if prompt.strip():
            self._print_rich(prompt)
        self._print_rich(self._plain_choice_menu(options, descriptions), end="")
        if self._fallback_session is None:
            self._fallback_session = PromptSession()
        while True:
            answer = (await self._fallback_session.prompt_async(f"选择 (1-{len(options)}): ", handle_sigint=True)).strip()
            if not answer:
                return options[default_index][0] if 0 <= default_index < len(options) else ""
            if answer.isdigit():
                idx = int(answer) - 1
                if 0 <= idx < len(options):
                    return options[idx][0]
            self._print_rich("请输入有效编号。", style="red")

    async def _read_choice_input_plain(
        self,
        prompt: str,
        options: list[tuple[str, str]],
        descriptions: list[str] | None,
        input_placeholder: str,
        default_index: int,
    ) -> str:
        """非 TTY 降级 choice_input：打印编号菜单后读一行；纯合法编号返回该项 choice，
        否则整行作输入 text；空输入返回默认项 choice、无默认项则取消。

        Args:
            prompt: 菜单上文提示。
            options: 选项列表，每项为 (value, label)。
            descriptions: 与 options 对齐的选项参考说明，逐项以浅色副行展示（None 或空串表示无）。
            input_placeholder: 输入行占位文案（并入读取提示）。
            default_index: 空输入时回退的默认项下标。
        Returns:
            JSON 编码的 {"choice": ..., "text": ...} 对象串；无有效输入且无默认项时为空串（取消）。
        """
        if prompt.strip():
            self._print_rich(prompt)
        self._print_rich(self._plain_choice_menu(options, descriptions), end="")
        if self._fallback_session is None:
            self._fallback_session = PromptSession()
        hint = input_placeholder.strip() or "输入回答"
        answer = (await self._fallback_session.prompt_async(
            f"选择编号 1-{len(options)} 或直接{hint}: ", handle_sigint=True
        )).strip()
        if not answer:
            if 0 <= default_index < len(options):
                return json.dumps({"choice": options[default_index][0], "text": ""})
            return ""
        if answer.isdigit():
            idx = int(answer) - 1
            if 0 <= idx < len(options):
                return json.dumps({"choice": options[idx][0], "text": ""})
        return json.dumps({"choice": "", "text": answer})

    async def _read_form_plain(self, prompt: str, questions: list[FormQuestion]) -> str:
        """非 TTY 降级表单：打印上文后逐题采集（多选读编号集、单选读编号、自由文本读整行），末尾收讨论，JSON 编码返回。

        Args:
            prompt: 表单上文提示。
            questions: 问题列表。
        Returns:
            JSON 编码的 {"answers": [...], "discussion": "..."} 对象串（answers 与 questions 顺序对齐）。
        """
        if prompt.strip():
            self._print_rich(prompt)
        answers: list[str] = []
        for qi, question in enumerate(questions):
            self._print_rich(f"\n问题{qi + 1}：{question.question}")
            if question.options is None:
                answers.append(await self._read_input_plain("你的回答: ", ""))
            elif question.multi_select:
                answers.append(await self._read_multi_choice_plain(question.options, question.descriptions))
            else:
                answers.append(await self._read_choice_plain("", question.options, 0, question.descriptions))
        discussion = await self._read_input_plain("讨论这几个问题（可回车跳过）: ", "")
        return json.dumps({"answers": answers, "discussion": discussion})

    async def _read_multi_choice_plain(self, options: list[tuple[str, str]],
                                       descriptions: list[str] | None = None) -> str:
        """非 TTY 降级多选：打印编号菜单后读一行；全为合法编号则按「、」连接对应 value，否则整行作自定义文本。

        Args:
            options: 选项列表，每项为 (value, label)。
            descriptions: 与 options 对齐的选项参考说明，逐项以浅色副行展示（None 或空串表示无）。
        Returns:
            选中项 value 的「、」连接串（去重保序）；输入含非编号内容时原样作自定义文本返回；空输入返回空串。
        """
        self._print_rich(self._plain_choice_menu(options, descriptions), end="")
        if self._fallback_session is None:
            self._fallback_session = PromptSession()
        answer = (await self._fallback_session.prompt_async(
            "多选（逗号分隔编号，或直接输入自定义回答，回车跳过）: ", handle_sigint=True
        )).strip()
        if not answer:
            return ""
        tokens = [t.strip() for t in answer.replace("，", ",").split(",") if t.strip()]
        if tokens and all(t.isdigit() and 1 <= int(t) <= len(options) for t in tokens):
            seen: set[int] = set()
            values: list[str] = []
            for token in tokens:
                idx = int(token) - 1
                if idx not in seen:
                    seen.add(idx)
                    values.append(options[idx][0])
            return "、".join(values)
        return answer  # 含非编号内容：整行作自定义文本

    # ---- 输出汇聚点 ----

    def _print_ansi(self, value: str) -> None:
        """输出预渲染 ANSI 到（被代理的）stdout，落入常驻 App 上方的原生 scrollback。

        非 TTY 下 sys.stdout 是真实流（无代理），显式 flush 确保管道/文件即时可见。

        Args:
            value: 预渲染好的 ANSI 字符串（可能多行；任何整体样式已烘焙在内）。
        """
        sys.stdout.write(value)
        if not self._tty:
            sys.stdout.flush()

    def _print_rich(self, content: str | Text, *, style: str = "", end: str = "\n") -> None:
        """通过 Rich 按终端宽度渲染文本/可渲染对象，再经 _print_ansi 输出（正确处理中文双宽字符）。

        Args:
            content: 文本字符串或 Rich Text。
            style: Rich 样式名，仅 content 为 str 时生效。
            end: 结尾字符，默认换行。
        """
        renderable = Text(content, style=style) if isinstance(content, str) else content
        with self._rich_console.capture() as capture:
            self._rich_console.print(renderable, end=end)
        self._print_ansi(capture.get())

    def _print_markdown(self, md: str) -> None:
        """把一段 Markdown 渲染为 ANSI 后输出到 scrollback（块级，支持多行/标题/列表）。

        仅 TTY 使用；非 TTY 由调用方走纯文本降级，避免 ANSI 落入管道。

        Args:
            md: 待渲染的 Markdown 源文本。
        """
        self._print_ansi(render_markdown(md, width=self._render_width))

    def _markdown_label(self, md: str) -> Text:
        """把一段 Markdown 渲染为单行 Rich Text（用于选择菜单的选项标签）。

        块级换行折叠为空格以保持单行，并去掉 Markdown 把段落填充到整宽产生的尾部空白
        （否则选中行 reverse 会铺满整宽）；内联样式（加粗/行内码等）经 Text.from_ansi 保留，
        便于与 ❯/序号前缀及选中行 reverse 高亮组合。

        Args:
            md: 待渲染的选项标签 Markdown 源文本。
        Returns:
            携带内联样式、去尾部填充的单行 Rich Text。
        """
        rendered = Text.from_ansi(render_markdown(md, width=self._render_width))
        rows: list[Text] = []
        for row in rendered.split("\n"):
            row.rstrip()  # 原地去尾部填充空白（Text.rstrip 就地修改）
            if row.plain:
                rows.append(row)
        return Text(" ").join(rows)

    async def _write(self, message: str, markdown: bool = False) -> None:
        """输出文本到 scrollback（不额外补换行）。

        Args:
            message: 要输出的文本。
            markdown: 为真且处于 TTY 时按 Markdown 渲染，否则按纯文本（经 _print_rich）。
        """
        if markdown and self._tty:
            self._print_markdown(message)
        else:
            self._print_rich(message, end="")

    # ---- 双流 markdown 渲染（回应 / 思考）----

    def _render_markdown(self, stream: _MarkdownStream, prefix_label: str, content: str) -> None:
        """把一段增量喂给指定流：未开流则先 reset 渲染器、单独成行写前缀标签、置位 active，再逐块输出。

        Args:
            stream: 目标输出流（回应或思考）。
            prefix_label: 该流的前缀标签（如「助手：」「思考」「coder a3f2b1c9」），仅在开流首段打印一次。
            content: 本次增量文本。
        """
        if not stream.active:
            stream.renderer.reset()
            self._write_stream_prefix(prefix_label)
            stream.active = True
        for chunk in stream.renderer.append(content):
            self._print_ansi(chunk)

    async def _end_markdown(self, stream: _MarkdownStream) -> None:
        """收尾指定流：flush 渲染器残留、补一个换行、复位 active。未开流则无操作。

        Args:
            stream: 目标输出流（回应或思考）。
        """
        if not stream.active:
            return
        for chunk in stream.renderer.flush():
            self._print_ansi(chunk)
        await self._write("\n")
        stream.active = False

    async def on_response_delta(self, event: ResponseDelta, content: str) -> None:
        """回应增量：记录当前 agent、状态条切到「回应中」，再做流式 markdown 渲染。

        Args:
            event: 回应增量事件，含 caller_agent_type / caller_uuid。
            content: 本次增量文本。
        """
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        self._set_activity("回应中")
        self._render_markdown(self._response_stream, self._response_prefix(event), content)

    async def on_thinking_delta(self, event: ThinkingDelta, content: str) -> None:
        """思考增量：记录当前 agent、状态条切到「思考中」，再做流式 markdown 渲染。

        仅在 events 级别为 DETAIL 时才会被调用（progress 级别下总线丢弃思考增量）。

        Args:
            event: 思考增量事件，含 caller_agent_type / caller_uuid。
            content: 本次增量文本。
        """
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        self._set_activity("思考中")
        self._render_markdown(self._thinking_stream, self._thinking_prefix(event), content)

    async def _end_response_if_needed(self) -> None:
        """收尾回应流（由基类 _end_streams_for 在切换到非回应事件时调用）。"""
        await self._end_markdown(self._response_stream)

    async def _end_thinking_if_needed(self) -> None:
        """收尾思考流（由基类 _end_streams_for 在切换到非思考事件时调用）。"""
        await self._end_markdown(self._thinking_stream)

    def _write_stream_prefix(self, label: str) -> None:
        """在常驻 App 上方滚动区单独成行打印「换行 + 彩色 › + 标签」。

        Args:
            label: 前缀标签（如「助手：」「coder：」「思考 coder」）。
        """
        prefix = Text("\n› ", style="bold cyan")
        prefix.append(label, style="bold")
        self._print_rich(prefix)  # 默认 end="\n"：前缀单独成行

    def _response_prefix(self, event: ResponseDelta) -> str:
        """回应前缀标签：子智能体显示「<agent>：」，主 Agent 显示「助手：」。"""
        agent = self._agent_label(event.caller_agent_type, event.caller_uuid)
        return f"回复({agent})：" if agent else "助手："

    def _thinking_prefix(self, event: ThinkingDelta) -> str:
        """思考前缀标签：子智能体显示「思考 <agent>」，主 Agent 显示「思考」。"""
        agent = self._agent_label(event.caller_agent_type, event.caller_uuid)
        return f"思考({agent})：" if agent else "思考"

    def _agent_label(self, agent_type: str | None, agent_uuid: str | None) -> str:
        """返回 agent 标签：「<agent_type> <uuid首段8位>」，便于区分同类型的多个并发实例。

        agent_type 为空时返回空串；agent_uuid 取第一个「-」前的 8 位，缺失时只返回 agent_type。

        Args:
            agent_type: agent 类型（主 agent 为其 agent_type，如「main」；子智能体为各自类型；None/空表示无身份）。
            agent_uuid: agent 实例 uuid 字符串（None 表示无）。

        Returns:
            形如「coder a3f2b1c9」的标签；无 agent_type 时为空串。
        """
        if not agent_type:
            return ""
        uid = agent_uuid.split("-")[0] if agent_uuid else ""
        return f"{agent_type} {uid}" if uid else agent_type

    # ---- 处理阶段钩子：驱动状态条 ----

    async def on_llm_call_started(self, event: LLMCallStarted) -> None:
        """LLM 调用开始：记录当前 agent，状态条进入「思考中」。

        Args:
            event: LLM 调用开始事件，含 caller_agent_type / caller_uuid。
        """
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        self._set_activity("思考中")

    async def on_llm_call_completed(self, event: LLMCallCompleted) -> None:
        """LLM 调用完成：把本次调用的 token 累加到本会话累计值（跨回合持续累积，仅 /clear 清零）。

        Args:
            event: LLM 调用完成事件，含 input_tokens / output_tokens / cache_read_input_tokens（均可能为 None）。
        """
        self._session_in_tokens += event.input_tokens or 0
        self._session_out_tokens += event.output_tokens or 0
        self._session_cache_read_tokens += event.cache_read_input_tokens or 0

    async def on_compact_delta(self, event: CompactDelta) -> None:
        """上下文压缩进度：状态条切到「压缩上下文」，并输出一行进度。

        Args:
            event: 压缩进度事件。
        """
        self._set_activity("压缩上下文")
        detail = event.content.strip() or "context"
        self._print_rich(f"[compact] {detail}", style="bright_black")

    async def on_permission_notice(self, event: PermissionNotice) -> None:
        """工具权限状态通知：auto_allow 打印绿色 [auto] 行，deny 打印 [deny] 行，allow 静默。

        Args:
            event: 权限状态通知事件。
        """
        if event.status == "auto_allow":
            self._print_rich(f"[auto] {event.detail or event.tool_name}", style="green")
            return
        if event.status == "allow":
            return
        await self._write(f"[deny] {event.detail or event.tool_name}\n")

    async def on_tool_call_started(self, event: ToolCallStarted) -> None:
        """工具开始：记录当前 agent，状态条切到该工具名，并打印 `● <工具> <详情>` 行。

        Args:
            event: 工具调用开始事件，含 caller_agent_type / caller_uuid。
        """
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        self._set_activity(event.tool_name)
        agent = self._agent_label(event.caller_agent_type, event.caller_uuid)
        detail = event.detail.strip()
        line = Text("● ", style="bold")
        if agent:
            line.append(f"{agent} ", style="cyan")
        line.append(event.tool_name, style="bold")
        if detail:
            line.append(f" {detail}")
        self._print_rich(line)

    async def on_tool_call_completed(self, event: ToolCallCompleted) -> None:
        """工具完成：打印 `  ⎿ <预览首行> (<耗时>s)` 子行；成功绿、失败红且保留完整预览。

        Args:
            event: 工具调用完成事件，含 status / result_preview / duration_seconds。
        """
        ok = event.status == "success"
        preview = (event.result_preview or "").strip()
        preview_lines = preview.splitlines()
        style = "green" if ok else "red"
        line = Text("  ⎿ ", style="bright_black")
        line.append(preview_lines[0] if preview_lines else ("完成" if ok else "失败"), style=style)
        line.append(f"  ({event.duration_seconds:.2f}s)", style="bright_black")
        self._print_rich(line)
        if not ok and len(preview_lines) > 1:
            # 失败时保留完整预览（首行之后的剩余内容），便于排查
            self._print_rich("\n".join(preview_lines[1:]), style="red")

    # ---- 按键绑定 ----

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
        - 查看面板时：PgUp/PgDn 上下滚动（暂停/恢复贴底实时跟随）；Esc：关闭面板还原主对话。

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
        # 选择菜单激活条件：select 态时为真。
        _cond_select = Condition(lambda: self._mode == "select")
        # 表单激活条件：form 态为真。派生条件按焦点区（答题/讨论）与标签类型细分，供各表单键位专用。
        _cond_form = Condition(lambda: self._mode == "form")
        _cond_form_answer = Condition(lambda: self._mode == "form" and self._form_zone == "answer")  # 答题区（←→↑↓ 导航）
        _cond_form_submit = Condition(lambda: self._mode == "form" and self._form_zone == "answer" and self._form_on_submit_tab())  # 答题区且聚焦「提交」标签
        _cond_form_opt = Condition(lambda: self._mode == "form" and self._form_zone == "answer" and self._current_form_question() is not None and self._current_form_question().options is not None and not self._form_cursor_on_custom())  # 答题区有选项题且光标在选项行（空格/数字选中）
        _cond_form_single_opt = Condition(lambda: self._mode == "form" and self._form_zone == "answer" and self._form_focused_single() and not self._form_cursor_on_custom())  # 答题区单选题且光标在选项行（Enter 选中，就地不推进）
        _cond_form_confirm = Condition(lambda: self._mode == "form" and self._form_zone == "answer" and not self._form_on_submit_tab() and not (self._form_focused_single() and not self._form_cursor_on_custom()))  # 答题区问题标签且非单选选项行（Enter 确认推进：多选题/自由文本题/单选自定义行）
        _cond_form_discuss = Condition(lambda: self._mode == "form" and self._form_zone == "discuss")  # 底部讨论栏
        # choice_input 激活条件：choice_input 态为真。派生条件按光标是否在输入行细分，供各键位专用。
        _cond_choice_input = Condition(lambda: self._mode == "choice_input")
        _cond_choice_input_opt = Condition(lambda: self._mode == "choice_input" and not self._choice_input_on_input_row())  # 光标在选项行（数字/Enter 直选）
        _cond_choice_input_row = Condition(lambda: self._mode == "choice_input" and self._choice_input_on_input_row())  # 光标在输入行（Enter 提交输入）
        # 斜杠命令补全激活条件：缓冲存在补全候选时为真。
        _cond_completing = Condition(lambda: self._buffer is not None and self._buffer.complete_state is not None)
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
            event.current_buffer.insert_text("\n")

        self._try_add_binding(bindings, "s-enter", lambda event: event.current_buffer.insert_text("\n"))
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
            if self._rows_provider is None:
                return
            rows = self._rows_provider()
            max_idx = len(rows) - 1
            if self._agent_selected_index < max_idx:
                self._agent_selected_index += 1

        # Enter：列表聚焦时在输入框上方打开选中子 agent 的转录覆盖面板（焦点回输入框，仍可输入）
        @bindings.add("enter", filter=_cond_list_focused)
        def _(event) -> None:
            if self._rows_provider is None:
                return
            rows = self._rows_provider()
            if self._agent_selected_index >= len(rows):
                return
            row = rows[self._agent_selected_index]
            if not row.is_main:
                self._viewing_uuid = row.uuid
                self._view_scroll = 0
            event.app.layout.focus(self._input_window)
            event.app.invalidate()

        # Esc：列表聚焦时返回输入框
        @bindings.add("escape", filter=_cond_list_focused)
        def _(event) -> None:
            event.app.layout.focus(self._input_window)

        # ---- 转录面板：滚动 / 关闭（输入框聚焦下仍全局响应；用 PgUp/PgDn 以让出 ↑↓ 给输入框）----

        # PgUp：面板上滚（暂停贴底跟随）；越界由 _render_transcript_panel 就地夹取
        @bindings.add("pageup", filter=_cond_viewing)
        def _(event) -> None:
            self._view_scroll += _TRANSCRIPT_PANEL_ROWS - 1
            event.app.invalidate()

        # PgDn：面板下滚；回到 0 即恢复贴底实时跟随
        @bindings.add("pagedown", filter=_cond_viewing)
        def _(event) -> None:
            self._view_scroll = max(0, self._view_scroll - (_TRANSCRIPT_PANEL_ROWS - 1))
            event.app.invalidate()

        # Esc：关闭转录面板，主对话原样恢复（select 态优先归选择菜单处理）
        @bindings.add("escape", filter=_cond_viewing & ~_cond_select)
        def _(event) -> None:
            self._viewing_uuid = None
            self._view_scroll = 0
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
        if self._permission_mode_toggle_handler is None:
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
