"""TTY output primitives for Inline UI streaming."""

from __future__ import annotations

import math
import sys

from src.events.types import (
    CompactDelta,
    LLMCallFailed,
    LLMCallStarted,
    LLMLengthRetrying,
    LLMRetrying,
    PermissionNotice,
    ResponseDelta,
    ThinkingDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from src.interfaces.markdown_renderer import MarkdownStreamRenderer, render_markdown

from rich.text import Text


class _MarkdownStream:
    """一条流式 markdown 输出流的状态：markdown 渲染器 + 是否已开流（已写过前缀、未收尾）。"""

    def __init__(self, *, base_style: str = "") -> None:
        """初始化一条流式 markdown 输出流。

        Args:
            base_style: 渲染时整体叠加到 markdown 正文上的 Rich 样式（如 "dim"）；空串表示不叠加。
        """
        self.renderer = MarkdownStreamRenderer(base_style=base_style)
        self.active = False


# 截断阶段分类到用户可见中文标签的映射，供长度恢复标记展示。
_TRUNCATION_KIND_LABELS = {
    "tool_call": "工具调用",
    "content": "正文",
    "thinking": "思考",
    "unknown": "未知",
}


class OutputActions:
    """Render TTY streams and visible progress events."""

    def _print_ansi(self, value: str) -> None:
        """输出预渲染 ANSI 到（被代理的）stdout，落入常驻 App 上方的原生 scrollback。

        非 TTY 下 sys.stdout 是真实流（无代理），显式 flush 确保管道/文件即时可见。

        Args:
            value: 预渲染好的 ANSI 字符串（可能多行；任何整体样式已烘焙在内）。
        """
        if not self._tty:
            self._plain.write(value)
            return
        sys.stdout.write(value)

    def _print_rich(self, content: str | Text, *, style: str = "", end: str = "\n") -> None:
        """通过 Rich 按终端宽度渲染文本/可渲染对象，再经 _print_ansi 输出（正确处理中文双宽字符）。

        Args:
            content: 文本字符串或 Rich Text。
            style: Rich 样式名，仅 content 为 str 时生效。
            end: 结尾字符，默认换行。
        """
        if not self._tty:
            self._plain.write(content, end=end)
            return
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

    async def _emit_caller_banner(self, caller_agent_type: str | None, caller_uuid: str | None) -> None:
        """交互菜单弹出前，若能识别发起 agent 则单独成行打印「彩色 › + agent 标签」。

        与 _write_stream_prefix 的 agent 归属前缀同源，让权限/表单/计划审核等弹窗一眼看出是哪个 agent 发起；
        标签为空（无 agent 身份，如用户/应用发起）时不打印。

        Args:
            caller_agent_type: 发起菜单的 agent 类型（主 agent 为「main」；空表示无身份，不打印）。
            caller_uuid: 发起菜单的 agent 实例 uuid。
        """
        label = self._agent_label(caller_agent_type, caller_uuid)
        if not label:
            return
        banner = Text("\n› ", style="bold cyan")
        banner.append(label, style="bold cyan")
        self._print_rich(banner)

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

    async def on_llm_call_started(self, event: LLMCallStarted) -> None:
        """LLM 调用开始：轮边界先 flush 上一轮工具缓冲成 scrollback 定稿块，再记录当前 agent、进入「等待响应」。

        路由已保证只转发前台 agent 的 LLMCallStarted，故此处即前台新一轮的起点（Trigger A）；
        缓冲空时 flush 为 no-op（含非 TTY 从不入缓冲的情形）。「等待响应」覆盖请求已发出、尚未收到
        首个增量的窗口；随后 detail 级别下由思考增量切「思考中」、回应增量切「回应中」。

        Args:
            event: LLM 调用开始事件，含 caller_agent_type / caller_uuid。
        """
        self._round_flush()
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        activity = (
            f"等待响应 {event.attempt}/{event.max_attempts}"
            if event.attempt > 1 and event.max_attempts > 0
            else "等待响应"
        )
        self._set_activity(activity)

    async def on_llm_retrying(self, event: LLMRetrying) -> None:
        """展示安全 LLM 重试边界并在 TTY 下驱动实时倒计时。

        TTY 已产生残片时先把尝试失败分隔永久写入 scrollback；无残片时只保留活动区倒计时。
        非 TTY 每次重试都打印静态行，剩余秒向上取整。

        路由已保证只转发前台 agent 的 LLMRetrying。

        Args:
            event: 携带错误类别、安全摘要、残片状态与等待秒数的重试事件。
        """
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        if self._tty:
            if event.partial:
                self._print_rich(
                    f"⚠ 尝试 {event.attempt}/{event.max_attempts} 失败，将重试 "
                    f"[{event.error_kind}] {event.safe_message} "
                    f"(partial=true, tool={event.tool_fragment_state})",
                    style="yellow",
                )
            self._begin_retry_countdown(
                event.error_kind,
                event.safe_message,
                event.attempt,
                event.max_attempts,
                event.wait_seconds,
            )
            return
        remaining = max(0, math.ceil(event.wait_seconds))
        self._print_rich(
            f"⚠ LLM 调用失败 [{event.error_kind}] {event.safe_message}；"
            f"{remaining}秒后重试 ({event.attempt}/{event.max_attempts})",
            style="yellow",
        )

    async def on_llm_length_retrying(self, event: LLMLengthRetrying) -> None:
        """展示一次输出长度截断的自动恢复标记（不驱动倒计时）。

        区别于 on_llm_retrying：长度恢复不进入退避等待，故只打印一行黄色标记说明
        截断阶段与所采取的恢复策略，不占用活动区倒计时。

        路由已保证只转发前台 agent 的 LLMLengthRetrying。

        Args:
            event: 携带截断阶段、恢复策略、推理力度与恢复计数的事件。

        Returns:
            None。
        """
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        kind_label = _TRUNCATION_KIND_LABELS.get(event.truncation_kind, event.truncation_kind)
        if event.strategy == "regenerate-lower-effort":
            action = f"降低推理力度至 {event.effort} 后重生成"
        elif event.strategy == "regenerate-compress":
            action = "压缩思考后重生成"
        else:
            action = "从中断处继续生成"
        self._print_rich(
            f"⚠ 输出截断（{kind_label}）：{action} ({event.attempt}/{event.max_attempts})",
            style="yellow",
        )

    async def on_llm_call_failed(self, event: LLMCallFailed) -> None:
        """永久展示安全 LLM 终态失败并清除可能残留的重试倒计时。

        Args:
            event: 携带安全错误摘要与关联 ID 的终态失败事件。

        Returns:
            None。
        """
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        self._set_activity("失败")
        identifiers: list[str] = []
        if event.request_id:
            identifiers.append(f"request_id={event.request_id}")
        if event.diagnostic_id:
            identifiers.append(f"diagnostic_id={event.diagnostic_id}")
        suffix = f" ({', '.join(identifiers)})" if identifiers else ""
        self._print_rich(
            f"✘ LLM 调用失败 [{event.error_kind}] {event.safe_message}{suffix}",
            style="red",
        )

    async def on_compact_delta(self, event: CompactDelta) -> None:
        """上下文压缩进度：状态条切到「压缩上下文」，并输出一行带发起 agent 标签的进度。

        Args:
            event: 压缩进度事件，含 caller_agent_type / caller_uuid。
        """
        self._set_activity("压缩上下文")
        detail = event.content.strip() or "context"
        label = self._agent_label(event.caller_agent_type, event.caller_uuid)
        prefix = f"{label} " if label else ""
        self._print_rich(f"[compact] {prefix}{detail}", style="bright_black")

    async def on_permission_notice(self, event: PermissionNotice) -> None:
        """工具权限状态通知：仅 deny 打印 [deny] 行；allow / auto_allow 静默（工具本身已由本轮面板/定稿块呈现）。

        Args:
            event: 权限状态通知事件，含 caller_agent_type / caller_uuid。
        """
        if event.status != "deny":
            return
        label = self._agent_label(event.caller_agent_type, event.caller_uuid)
        prefix = f"{label} " if label else ""
        await self._write(f"[deny] {prefix}{event.detail or event.tool_name}\n")

    async def on_tool_call_started(self, event: ToolCallStarted) -> None:
        """工具开始：记录当前 agent、状态条切到该工具名；TTY 下入本轮缓冲（由本轮面板/定稿块呈现），非 TTY 逐行打印 `● <工具> <详情>`。

        Args:
            event: 工具调用开始事件，含 caller_agent_type / caller_uuid。
        """
        self._set_current_agent(event.caller_agent_type, event.caller_uuid)
        self._set_activity(event.tool_name)
        if self._tty:
            self._round_append_start(event)
            return
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
        """工具完成：TTY 下把结果落定进本轮缓冲（轮边界统一 flush），非 TTY 逐行打印 `  ⎿ <预览首行> (<耗时>s)`。

        Args:
            event: 工具调用完成事件，含 status / result_preview / duration_seconds。
        """
        if self._tty:
            self._round_settle(event)
            return
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
