"""CLIInterface — prompt_toolkit based command line interface."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import termios
from collections.abc import Callable
from contextlib import contextmanager

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

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
from src.interfaces.base import UserInterface
from src.interfaces.markdown_renderer import MarkdownStreamRenderer


class CLIInterface(UserInterface):
    """基于 prompt_toolkit 的 CLI 交互实现。"""

    def __init__(self) -> None:
        super().__init__()
        self._resume_interrupt_reader: Callable[[], None] | None = None
        self._interrupt_watch_depth = 0
        self._permission_mode_toggle_handler: Callable[[], None] | None = None
        self._style = Style.from_dict({
            "agent.response": "",
            "agent.thinking": "ansibrightblack",
            "agent.error": "ansired",
            "agent.muted": "ansibrightblack",
            "agent.permission": "ansiyellow",
            "agent.success": "ansigreen",
            "agent.tool": "ansibrightblack",
        })
        self._session: PromptSession[str] = PromptSession(
            key_bindings=self._build_input_key_bindings(),
            style=self._style,
        )
        self._markdown_renderers = {
            "response": MarkdownStreamRenderer(),
            "thinking": MarkdownStreamRenderer(),
        }

    @contextmanager
    def watch_interrupt(self, request_interrupt: Callable[[], None]):
        with super().watch_interrupt(request_interrupt):
            if self._interrupt_watch_depth > 0:
                self._interrupt_watch_depth += 1
                try:
                    yield
                finally:
                    self._interrupt_watch_depth -= 1
                return

            self._interrupt_watch_depth = 1
            loop = asyncio.get_running_loop()
            fd = None
            old_terminal_attrs = None
            reader_installed = False

            def schedule_interrupt() -> None:
                loop.call_soon_threadsafe(request_interrupt)

            def on_sigint(signum, frame) -> None:
                schedule_interrupt()

            def on_stdin_ready() -> None:
                if fd is None:
                    return
                try:
                    if b"\x03" in os.read(fd, 1024):
                        schedule_interrupt()
                except OSError:
                    pass

            def resume_interrupt_reader() -> None:
                nonlocal reader_installed
                if fd is None:
                    return
                if reader_installed:
                    try:
                        loop.remove_reader(fd)
                    except (AttributeError, NotImplementedError, OSError):
                        pass
                    reader_installed = False
                try:
                    loop.add_reader(fd, on_stdin_ready)
                except (AttributeError, NotImplementedError, OSError):
                    return
                reader_installed = True

            old_handler = signal.signal(signal.SIGINT, on_sigint)
            try:
                fd = sys.stdin.fileno()
                old_terminal_attrs = termios.tcgetattr(fd)
                new_terminal_attrs = list(old_terminal_attrs)
                new_terminal_attrs[3] &= ~(termios.ICANON | termios.ISIG | termios.ECHO)
                new_terminal_attrs[6] = list(new_terminal_attrs[6])
                new_terminal_attrs[6][termios.VMIN] = 1
                new_terminal_attrs[6][termios.VTIME] = 0
                termios.tcsetattr(fd, termios.TCSANOW, new_terminal_attrs)
            except (AttributeError, OSError, termios.error):
                fd = None
                old_terminal_attrs = None
            resume_interrupt_reader()
            self._resume_interrupt_reader = resume_interrupt_reader if fd is not None else None
            try:
                yield
            finally:
                self._interrupt_watch_depth = 0
                self._resume_interrupt_reader = None
                if fd is not None and reader_installed:
                    try:
                        loop.remove_reader(fd)
                    except (AttributeError, NotImplementedError, OSError):
                        pass
                if fd is not None and old_terminal_attrs is not None:
                    try:
                        termios.tcflush(fd, termios.TCIFLUSH)
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_terminal_attrs)
                    except (OSError, termios.error):
                        pass
                signal.signal(signal.SIGINT, old_handler)

    async def _read_input(self, prompt: str, default: str = "") -> str:
        try:
            with patch_stdout():
                return await self._session.prompt_async(
                    prompt,
                    default=default,
                    multiline=True,
                    prompt_continuation="... ",
                    bottom_toolbar=self._input_bottom_toolbar,
                    handle_sigint=True,
                )
        finally:
            if self._resume_interrupt_reader is not None:
                self._resume_interrupt_reader()

    def set_permission_mode_toggle_handler(
        self,
        handler: Callable[[], None] | None,
    ) -> None:
        self._permission_mode_toggle_handler = handler

    def _input_bottom_toolbar(self) -> str:
        permission_mode = self.get_system_state().permission_mode
        if self._permission_mode_toggle_handler is None:
            return (
                "Enter 提交 · "
                f"权限模式: {permission_mode} · "
                "Shift+Enter/Ctrl+J 换行 · Ctrl+C 清空/退出"
            )
        return (
            "Enter 提交 · "
            f"Shift+Tab 权限模式: {permission_mode} · "
            "Shift+Enter/Ctrl+J 换行 · Ctrl+C 清空/退出"
        )

    async def _read_permission(self, tool_name: str, detail: str) -> str:
        return await self._prompt_permission(tool_name, detail)

    async def _write(self, message: str) -> None:
        self._print(message, end="")

    async def _write_response_prefix(self, event: ResponseDelta) -> None:
        await self._write(f"\n{self._response_prefix(event)}")

    async def _write_thinking_prefix(self, event: ThinkingDelta) -> None:
        await self._write(f"\n{self._thinking_prefix(event)}")

    async def _prompt_permission(self, tool_name: str, detail: str) -> str:
        self._print(HTML(
            "\n<agent.permission>工具请求权限</agent.permission>\n"
            f"  工具: {self._escape_html(tool_name)}\n"
            f"  内容: {self._escape_html(detail)}\n"
            "  输入 y/s/a/n 后按 Enter 确认\n"
            "  [y] 允许一次   [s] 本次会话始终允许   [a] 始终允许并保存   [n] 拒绝\n"
        ))
        session: PromptSession[str] = PromptSession(
            key_bindings=self._build_permission_key_bindings(),
            style=self._style,
        )
        with patch_stdout():
            while True:
                try:
                    answer = (await session.prompt_async("选择: ", handle_sigint=True)).strip().lower()
                finally:
                    if self._resume_interrupt_reader is not None:
                        self._resume_interrupt_reader()
                if answer in {"y", "yes"}:
                    return "yes"
                if answer in {"s", "session"}:
                    return "session"
                if answer in {"a", "always"}:
                    return "always"
                if answer in {"n", "no", "deny"}:
                    return "deny"
                self._print(HTML("<agent.error>请输入 y、s、a 或 n。</agent.error>"))

    async def on_response_delta(self, event: ResponseDelta, content: str) -> None:
        await self._write_markdown_delta(
            "response",
            self._write_response_prefix,
            event,
            content,
        )

    async def _end_response_if_needed(self) -> None:
        await self._end_markdown_stream_if_needed("response")

    async def on_thinking_delta(self, event: ThinkingDelta, content: str) -> None:
        await self._write_markdown_delta(
            "thinking",
            self._write_thinking_prefix,
            event,
            content,
        )

    async def _end_thinking_if_needed(self) -> None:
        await self._end_markdown_stream_if_needed("thinking")

    async def _write_markdown_delta(
        self,
        stream_name: str,
        prefix_writer: Callable,
        event,
        content: str,
    ) -> None:
        if not self._is_markdown_stream_active(stream_name):
            self._markdown_renderers[stream_name].reset()
            await prefix_writer(event)
            self._set_markdown_stream_active(stream_name, True)
        for chunk in self._markdown_renderers[stream_name].append(content):
            self._print_ansi(chunk)

    async def _end_markdown_stream_if_needed(self, stream_name: str) -> None:
        if self._is_markdown_stream_active(stream_name):
            for chunk in self._markdown_renderers[stream_name].flush():
                self._print_ansi(chunk)
            await self._write("\n")
            self._set_markdown_stream_active(stream_name, False)

    def _is_markdown_stream_active(self, stream_name: str) -> bool:
        if stream_name == "response":
            return self._in_response
        if stream_name == "thinking":
            return self._in_thinking
        raise ValueError(f"unknown markdown stream: {stream_name}")

    def _set_markdown_stream_active(self, stream_name: str, active: bool) -> None:
        if stream_name == "response":
            self._in_response = active
            return
        if stream_name == "thinking":
            self._in_thinking = active
            return
        raise ValueError(f"unknown markdown stream: {stream_name}")

    async def on_permission_notice(self, event: PermissionNotice) -> None:
        if event.status == "auto_allow":
            label = f"[auto] {event.detail or event.tool_name}"
            self._print(HTML(f"<agent.success>{self._escape_html(label)}</agent.success>"))
            return
        if event.status == "allow":
            return
        if event.detail:
            await self._write(f"[deny] {event.detail}\n")
        else:
            await self._write(f"[deny] {event.tool_name}\n")

    async def on_compact_delta(self, event: CompactDelta) -> None:
        detail = event.content.strip() or "context"
        self._print(HTML(
            f"<agent.muted>[compact] {self._escape_html(detail)}</agent.muted>"
        ))

    async def on_tool_call_started(self, event: ToolCallStarted) -> None:
        agent = self._agent_label(event.caller_agent_type, event.caller_uuid)
        detail = event.detail.strip()
        pieces = ["[tool]"]
        if agent:
            pieces.append(agent)
        pieces.append(event.tool_name)
        if detail:
            pieces.append(detail)
        self._print(HTML(
            f"<agent.tool>{self._escape_html(' '.join(pieces))}</agent.tool>"
        ))

    async def on_tool_call_completed(self, event: ToolCallCompleted) -> None:
        label = "done" if event.status == "success" else "error"
        style = "agent.success" if event.status == "success" else "agent.error"
        line = f"[{label}] {event.tool_name} {event.duration_seconds:.2f}s"
        if event.status != "success" and event.result_preview:
            line += f" {event.result_preview}"
        self._print(HTML(
            f"<{style}>{self._escape_html(line)}</{style}>"
        ))

    async def on_llm_call_started(self, event: LLMCallStarted) -> None:
        self._print(
            "LLM call start: "
            f"model={event.model} "
            f"estimated_input_tokens={event.estimated_input_tokens} "
            f"messages={event.message_count} "
            f"tools={event.tool_count}",
        )

    async def on_llm_call_completed(self, event: LLMCallCompleted) -> None:
        self._print(
            "LLM usage: "
            f"model={event.model} "
            f"input={self._format_optional_int(event.input_tokens)} "
            f"output={self._format_optional_int(event.output_tokens)} "
            f"total={self._format_optional_int(event.total_tokens)} "
            f"cache_read={self._format_optional_int(event.cache_read_input_tokens)} "
            f"cache_created={self._format_optional_int(event.cache_creation_input_tokens)} "
            f"duration={self._format_optional_float(event.duration_seconds)}s "
            f"output_tps={self._format_optional_float(event.output_tokens_per_second)} "
            f"total_tps={self._format_optional_float(event.total_tokens_per_second)}",
        )

    def _response_prefix(self, event: ResponseDelta) -> str:
        agent = self._agent_label(event.caller_agent_type, event.caller_uuid)
        if agent:
            return f"{agent}："
        return "助手："

    def _thinking_prefix(self, event: ThinkingDelta) -> str:
        agent = self._agent_label(event.caller_agent_type, event.caller_uuid)
        if agent:
            return f"思考 {agent} "
        return "思考 "

    def _agent_label(self, agent_type: str | None, agent_uuid: str | None) -> str:
        if not agent_type:
            return ""
        return agent_type

    def _print(self, *values, **kwargs) -> None:
        print_formatted_text(*values, style=self._style, **kwargs)

    def _print_ansi(self, value: str) -> None:
        print_formatted_text(ANSI(value), end="")

    def _build_input_key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("enter")
        def _(event) -> None:
            event.app.exit(result=event.current_buffer.text)

        @bindings.add("c-j")
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        self._try_add_binding(bindings, "s-enter", lambda event: event.current_buffer.insert_text("\n"))
        self._try_add_binding(bindings, "s-tab", self._handle_permission_mode_toggle)

        @bindings.add("c-c")
        def _(event) -> None:
            if event.current_buffer.text:
                event.current_buffer.reset()
                return
            event.app.exit(exception=KeyboardInterrupt)

        return bindings

    def _handle_permission_mode_toggle(self, event) -> None:
        if self._permission_mode_toggle_handler is None:
            return
        self._permission_mode_toggle_handler()
        event.app.invalidate()

    def _build_permission_key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()
        bindings.add("c-c")(lambda event: event.app.exit(exception=KeyboardInterrupt))
        return bindings

    def _try_add_binding(self, bindings: KeyBindings, key: str, handler: Callable) -> None:
        try:
            bindings.add(key)(handler)
        except ValueError:
            pass

    def _escape_html(self, value: str) -> str:
        return (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def _format_optional_int(self, value: int | None) -> str:
        if value is None:
            return "n/a"
        return str(value)

    def _format_optional_float(self, value: float | None) -> str:
        if value is None:
            return "n/a"
        return f"{value:.2f}"
