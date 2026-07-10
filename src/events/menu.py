"""菜单/交互事件 — 阻塞等待用户经 TUI 作答、并通过 future 回传结果的事件。

这些事件都是 UI 的"控制面"：EventBus 发布后，UI 进入相应的模态 `_mode`
（select/form/input），渲染对应界面，待用户作答后经 future 回传，UI 退出模态。
每个事件的注释给出其在 TUI（src/interfaces/inline_ui.py）上的具体呈现形态。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from src.events.levels import EventLevel
from src.events.types import Event


@dataclass
class MenuRequest(Event):
    """需 UI 阻塞作答、经 future 回传结果的交互事件基类。

    UI 收到后进入模态（select/form/input），渲染对应界面；用户作答后由
    complete() 回填 future（空串约定为取消），失败/取消分别走 fail()/cancel()。
    """

    future: asyncio.Future[str] | None = None

    def _pending(self) -> bool:
        """future 已设置且尚未完成（可安全落定）时返回 True。"""
        return self.future is not None and not self.future.done()

    def complete(self, value: str) -> None:
        """以用户作答值完成 future（future 未设置或已完成时忽略）。"""
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
class PermissionMenu(MenuRequest):
    """请求 UI 读取工具权限确认，并通过 future 返回结果。

    TUI 呈现（_mode="select" → _render_select，上下文经 _permission_context_text
    打印、选项经 _permission_options 生成）。下图为示意，实际渲染以 inline_ui.py 的
    _render_select / _permission_context_text / _permission_options 为准，可能随其改动而滞后：

        工具请求权限
          工具: shell
          内容: rm -rf build/
        ❯ 1. 允许一次
          2. 本次会话始终允许
          3. 始终允许并保存
          4. 拒绝 (esc)

    选中行 ❯ 前缀且整行反显、数字为快捷键；Esc 返回 "deny"。MCP 工具会在「拒绝」前
    额外插入「会话信任整个 server(...)」「始终信任整个 server 并保存(...)」两项。
    返回值：yes/session/always/session_server/always_server/deny。
    """
    tool_name: str = ""
    detail: str = ""
    suggested_rules: list[str] = field(default_factory=list)
    mcp_server_rule: str | None = None  # MCP 工具的 server 级通配规则（mcp__<server>__*），供"信任整个 server"选项使用
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["permission_menu"] = field(default="permission_menu", init=False)


@dataclass
class InputMenu(MenuRequest):
    """请求 UI 串行读取用户输入，并通过 future 返回结果。

    TUI 呈现（_mode="input"）——上文提示打印在滚动区，可编辑输入行以 › 起头、
    行内为原生块光标，无占位符；Enter 提交。input 模式无独立渲染窗口，下图为示意，
    实际渲染以 inline_ui.py 的 _read_input / _render_input_context 为准，可能随其改动而滞后：

        你叫什么名字？
        ──────────────────────────────
        › Alice▮
        ──────────────────────────────

    prompt 的末行被 › 前缀取代、其余行打印在上方；default 作预填文本而非占位符。
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

    TUI 呈现（_mode="select" → _render_select，与 PermissionMenu 共用），上文为
    调用方 prompt。下图为示意，实际渲染以 inline_ui.py 的 _render_select 为准，可能随其改动而滞后
    （/mode 权限模式切换，仅示前 3 项）：

        权限模式（当前: default）
        ❯ 1. default - 只读自动放行；文件编辑和命令执行默认询问，可被 allow 规则放行
          2. acceptEdits - 只读和文件编辑自动放行；命令执行默认询问
          3. plan - 计划模式；只读自动放行，其余操作需确认

    选中行 ❯ 前缀且整行反显；Esc 返回 ""（取消）。用于 /mode 权限模式切换、resume 会话选择器。
    """
    prompt: str = ""  # 菜单上文（打印到 scrollback 的提示，如「权限模式（当前: default）」）
    options: list[tuple[str, str]] = field(default_factory=list)  # 选项列表，每项为 (value, label)
    default_index: int = 0  # 初始选中项下标
    markdown: bool = False  # 上文提示与选项标签是否按 Markdown 渲染（如 ask_user）
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["choice_menu"] = field(default="choice_menu", init=False)


@dataclass
class FormQuestion:
    """单屏表单中的单个问题（纯数据，非 Event）——作为 FormMenu.questions 的单题载荷。

    Attributes:
        question: 问题文本。
        options: (value, label) 选项列表；非空时该题为选项菜单，None 时仅自由文本输入。
        multi_select: 有 options 时是否允许勾选多项（True 为多选，False 为单选）。
        header: 顶部标签栏用的简短标签（概括该题主旨），为空时标签栏回退显示「问题N」。
        descriptions: 与 options 等长对齐的选项参考说明，逐项为该选项下方展示的浅色说明（空串表示无说明）；None 表示无任何说明。
    """
    question: str
    options: list[tuple[str, str]] | None = None
    multi_select: bool = False
    header: str = ""
    descriptions: list[str] | None = None


@dataclass
class FormMenu(MenuRequest):
    """请求 UI 以单屏表单读取多个问题的作答，通过 future 返回 JSON 编码的答案列表（空串表示取消）。

    TUI 呈现（_mode="form" → _render_form）——顶部标签栏（已答 ☑ 未答 ☐ + header
    简介 + 末尾「提交」）、题干、单选 ●/○ 或多选 [x]/[ ] 选项行（可带浅色参考说明副行）、
    末行自定义输入行、底部操作提示与讨论栏。下图为示意，实际渲染以 inline_ui.py 的
    _render_form 为准，可能随其改动而滞后：

         ☑ 语言   ☐ 经验   提交

        你主要用哪门语言？
         ○ 1. Python
             数据与脚本首选
         ○ 2. Rust
             系统级、内存安全
         ○ ⌨ 其他: 输入回答…
        ↑↓ 选择行 · Enter/空格 选中 · ←→ 切换标签 · Esc 取消
        ──────────────────────────────
        Tab 讨论这几个问题…

    ←→ 切标签、↑↓ 移行、空格/Enter 选中、Tab 切讨论区、Enter 提交、Esc 取消。用于 ask_user、plan。
    """
    prompt: str = ""  # 表单上文提示（打印到 scrollback，如「🤖 提问」）
    questions: list[FormQuestion] = field(default_factory=list)  # 问题列表，顺序即作答顺序
    markdown: bool = False  # 上文提示与问题/选项标签是否按 Markdown 渲染
    level: EventLevel = field(default=EventLevel.PROGRESS, init=False)
    type: Literal["form_menu"] = field(default="form_menu", init=False)


@dataclass
class ChoiceInputMenu(MenuRequest):
    """请求 UI 以「选项列表 + 一行可编辑输入」读取一次作答，通过 future 返回 JSON
    {"choice": "<value|''>", "text": "<typed|''>"} 串（空串表示取消）。

    TUI 呈现（_mode="choice_input" → _render_choice_input）——上文为调用方 prompt（打印到
    scrollback）；选项行（❯ 序号. 标签，可带浅色说明副行）与操作提示画在分割线上方的
    choice_input_window，而「输入行」即分割线下方那条常驻输入框（› 前缀），并非窗口内的内联行。
    下图为示意，实际渲染以 inline_ui.py 的 _render_choice_input 为准，可能随其改动而滞后：

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
