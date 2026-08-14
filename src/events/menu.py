"""菜单/交互事件 — 阻塞等待用户经 TUI 作答、并通过 future 回传结果的事件。

这些事件都是 UI 的"控制面"：EventBus 发布后，交互协调器选择对应输入或 Modal
并安排渲染；用户完成或取消后经 future 回传。
每个事件的注释给出其在组合式 TUI 上的具体呈现形态。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from src.events.levels import EventLevel
from src.events.types import Event


@dataclass
class UiRequest(Event):
    """UI 窗口请求的基类，持有由 EventBus 等待的结果 future。

    所有交互窗口都经 future 向调用方回传结束结果。作答窗口由
    ``MenuRequest`` 表示，只读窗口由 ``ViewRequest`` 表示。

    Attributes:
        future: 调用方等待的结果 future；由 EventBus 在发布前附加。
    """

    future: asyncio.Future[str] | None = None

    def _pending(self) -> bool:
        """future 已设置且尚未完成（可安全落定）时返回 True。"""
        return self.future is not None and not self.future.done()

    def complete(self, value: str) -> None:
        """以窗口结果完成 future（future 未设置或已完成时忽略）。"""
        if self._pending():
            self.future.set_result(value)

    def cancel(self) -> None:
        """取消 future（future 未设置或已完成时忽略）。"""
        if self._pending():
            self.future.cancel()

    def fail(self, exc: BaseException) -> None:
        """以异常终结 future（future 未设置或已完成时忽略）。"""
        if self._pending():
            self.future.set_exception(exc)


@dataclass
class MenuRequest(UiRequest):
    """需 UI 阻塞作答、经 future 回传结果的交互事件基类。

    UI 收到后进入相应交互模式并渲染对应界面；用户作答后由
    complete() 回填 future（空串约定为取消），失败/取消分别走 fail()/cancel()。
    """


@dataclass
class ViewRequest(UiRequest):
    """只读窗口请求基类，不占用输入交互 future。

    只读窗口可在作答窗口下方保持打开；用户关闭窗口后才完成其 future。
    """


@dataclass
class PermissionMenu(MenuRequest):
    """请求 UI 读取工具权限确认，并通过 future 返回结果。

    TUI 由 `tui/dialogs.py` 的 `SelectionDialog` 呈现：

        工具请求权限
          工具: shell
          内容: rm -rf build/
        ❯ 1. 允许
          2. 拒绝 (esc)

    选中行 ❯ 前缀且整行反显、数字为快捷键；Esc 返回 "deny"。返回值只有 yes/deny。
    """
    tool_name: str = ""
    detail: str = ""
    reason: str = ""
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["permission_menu"] = field(default="permission_menu", init=False)


@dataclass
class InputMenu(MenuRequest):
    """请求 UI 串行读取用户输入，并通过 future 返回结果。

    TUI 由 `tui/app.py` 和 `Composer` 呈现：上文提示打印在历史区，
    多行输入框中 Enter 提交、Shift+Enter/Ctrl+J 换行：

        你叫什么名字？
        ──────────────────────────────
        › Alice▮
        ──────────────────────────────

    prompt 的上下文打印在上方；default 作预填文本而非占位符。
    这是文本输入而非选项菜单，作为交互事件与其它菜单同属控制面。
    """
    prompt: str = ""
    default: str = ""
    markdown: bool = False  # 上文提示是否按 Markdown 渲染（如 ask_user 的问题）
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["input_menu"] = field(default="input_menu", init=False)


@dataclass
class ChoiceMenu(MenuRequest):
    """请求 UI 以菜单读取一次选择，通过 future 返回所选 value（空串表示取消）。

    TUI 由 `tui/dialogs.py` 的 `SelectionDialog` 呈现。选中行反显；
    Esc 返回 ""（取消）。用于 resume 等通用选择器。
    """
    prompt: str = ""  # 菜单上文（打印到 scrollback 的提示）
    options: list[tuple[str, str]] = field(default_factory=list)  # 选项列表，每项为 (value, label)
    default_index: int = 0  # 初始选中项下标
    markdown: bool = False  # 上文提示与选项标签是否按 Markdown 渲染（如 ask_user）
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["choice_menu"] = field(default="choice_menu", init=False)


@dataclass
class ModelMenu(MenuRequest):
    """模型与推理强度双轴选择器。"""

    prompt: str = ""
    models: list[tuple[str, str]] = field(default_factory=list)
    efforts: list[str] = field(default_factory=list)
    model_index: int = 0
    effort_index: int = 0
    markdown: bool = False
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["model_menu"] = field(default="model_menu", init=False)


@dataclass
class FormQuestion:
    """单屏表单中的单个问题（纯数据，非 Event）——作为 FormMenu.questions 的单题载荷。

    Attributes:
        question: 问题文本（纯文本渲染）。
        options: (value, label) 选项列表；非空时该题为选项菜单，None 时仅自由文本输入。
        multi_select: 有 options 时是否允许勾选多项（True 为多选，False 为单选）。
        header: 顶部标签栏用的简短标签（概括该题主旨），为空时标签栏回退显示「问题N」。
        descriptions: 与 options 等长对齐的选项参考说明，逐项为该选项下方展示的暗色说明（空串表示无说明）；None 表示无任何说明。
        previews: 与 options 等长对齐的选项预览内容（Markdown 格式），非空时该题在 UI 中切换为
                  左右分栏展示，右侧渲染当前光标所在选项的预览；None 或全空串表示无预览。
                  仅在单选模式下生效。
        recommended: 与 options 等长对齐的推荐标记；True 的选项由调用方排序在前，UI 在其标签后
                  紧跟 (推荐) 后缀。None 表示无推荐。
    """
    question: str
    options: list[tuple[str, str]] | None = None
    multi_select: bool = False
    header: str = ""
    descriptions: list[str] | None = None
    previews: list[str] | None = None
    recommended: list[bool] | None = None

    @property
    def has_previews(self) -> bool:
        """该题是否有至少一个非空 preview 且为单选模式。"""
        return (
            not self.multi_select
            and self.previews is not None
            and any(p.strip() for p in self.previews)
        )


@dataclass
class FormMenu(MenuRequest):
    """请求 UI 以单屏表单读取多个问题的作答，通过 future 返回 JSON 编码的答案列表（空串表示取消）。

    TUI 由 `tui/dialogs.py` 的 `InlineFormWidget` 以内嵌形式呈现：顶部标签栏（已答 ☑ 未答 ☐
    + header 简介 + 末尾「提交」）、题干、单选 ●/○ 或多选 [x]/[ ] 选项行（推荐项带 (推荐)
    后缀、可带暗色参考说明）、选项末尾常驻「其它」输入行与可切换的讨论输入行：

         ☑ 语言   ☐ 经验   提交

        你主要用哪门语言？
         ● 1. Python(推荐)
             数据与脚本首选
         ○ 2. Rust
             系统级、内存安全
         ○ 其它: 输入自定义回答…
        ↑↓ 选择行 · ←→ 切换问题 · Space/数字 选择 · Tab 讨论 · Esc 取消

    ←→ 切标签、↑↓ 移行、空格/数字 选中、Enter 确认或前进、Tab 切讨论区、Esc 取消。用于 ask_user。
    """
    prompt: str = ""  # 表单上文提示（打印到 scrollback，如「🤖 提问」）
    questions: list[FormQuestion] = field(default_factory=list)  # 问题列表，顺序即作答顺序
    markdown: bool = False  # 上文提示与选项说明/预览是否按 Markdown 渲染（题干与选项标签恒为纯文本）
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["form_menu"] = field(default="form_menu", init=False)


@dataclass
class TranscriptView(ViewRequest):
    """请求 UI 以只读分页面板查看某子 agent 的完整原始消息记录，通过 future 回传结果（恒 ""）。

    由 /agents 浏览器在用户选中某子 agent 后发起。TUI 由 `AgentTuiApp` 的
    转录面板呈现：面板按「是否已有完整原始记录」选源——已完成 agent 渲染其原始消息
    （user/assistant/thinking/工具调用完整参数/工具完整返回），↑/↓ 滚动，Esc 返回列表。下图为示意，
    标题在渲染时从共享 agent 快照现场生成，只显示身份、状态和操作提示；根据终端宽度自然换行，
    上下分隔线始终完整保留：

        ──────────────────────────────
        ── ◯ code  a1b2  已完成 ──  实时  ·  ↑/↓ 滚动 · Esc 返回列表
        ──────────────────────────────
        ▶ 用户
        <初始任务提示…>
        ● 助手
        <正文…>
          ⚙ read_file
          { "path": "..." }
          ⚙ 结果 (…)
        <工具返回原文…>

    这是只读查看而非作答菜单：无选项、无输入；Esc 经交互协调器关闭面板并完成 future，返回 ""。
    """
    uuid: str = ""  # 目标子 agent 的 uuid 字符串
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["transcript_view"] = field(default="transcript_view", init=False)


@dataclass
class ChoiceInputMenu(MenuRequest):
    """请求 UI 以「选项列表 + 一行可编辑输入」读取一次作答，通过 future 返回 JSON
    {"choice": "<value|''>", "text": "<typed|''>"} 串（空串表示取消）。

    TUI 由 `tui/dialogs.py` 的 `ChoiceInputDialog` 呈现：上文为调用方 prompt；
    选项行（❯ 序号. 标签，可带说明副行）与自由输入位于同一 Modal。
    布局如下：

        计划审核
        ❯ 1. 自动执行
             在当前上下文中自动实施计划
          2. 手动执行
             退出计划模式，自行实施
        ↑↓ 选择行 · Enter/数字 选项 · Esc 取消
        ──────────────────────────────
        › ▮
        ──────────────────────────────

    ↑↓ 在「各选项行 + 末尾输入行」间移动光标：停在选项行时该行 ❯ 反显，Enter/数字 → 提交该项
    value；光标下移到输入行时选项均无 ❯、输入框转为可编辑，Enter 且非空 → 提交输入文本，空则不提交；
    Esc → 取消。选项与输入互斥、以光标所在行为准。
    返回值：JSON {"choice": "所选 value 或空", "text": "输入文本或空"}，取消为空串。
    """
    prompt: str = ""  # 菜单上文（打印到 scrollback 的提示）
    options: list[tuple[str, str]] = field(default_factory=list)  # 选项列表，每项为 (value, label)
    descriptions: list[str] | None = None  # 与 options 等长对齐的选项浅色说明副行；None 表示无
    input_placeholder: str = ""  # 输入行为空时的浅字占位文案
    default_index: int = 0  # 初始选中项下标
    markdown: bool = False  # 上文提示与选项标签是否按 Markdown 渲染
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["choice_input_menu"] = field(default="choice_input_menu", init=False)
