from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from src.tools import AccessKind, DataFlow, ToolPolicy
from src.tools.decorator import tool


_OUTPUT_BYTES = 1024 * 1024
_OUTPUT_LINES = 20_000


class Shell(BaseModel):
    command: str = Field(..., description="要执行的 shell 命令")
    timeout: int = Field(default=300, ge=1, le=600, description="超时时间（秒）")


@dataclass
class _OutputBudget:
    remaining_bytes: int = _OUTPUT_BYTES
    remaining_lines: int = _OUTPUT_LINES
    chunks: dict[str, list[bytes]] = field(
        default_factory=lambda: {"stdout": [], "stderr": []}
    )
    truncated: bool = False

    def add(self, stream: str, chunk: bytes) -> None:
        if not chunk or self.remaining_bytes <= 0 or self.remaining_lines <= 0:
            self.truncated = self.truncated or bool(chunk)
            return
        accepted = chunk[:self.remaining_bytes]
        newline_count = accepted.count(b"\n")
        if newline_count > self.remaining_lines:
            cutoff = 0
            for _ in range(self.remaining_lines):
                cutoff = accepted.index(b"\n", cutoff) + 1
            accepted = accepted[:cutoff]
            newline_count = self.remaining_lines
            self.truncated = True
        if len(accepted) < len(chunk):
            self.truncated = True
        self.chunks[stream].append(accepted)
        self.remaining_bytes -= len(accepted)
        self.remaining_lines -= newline_count

    def render(self) -> str:
        parts: list[str] = []
        stdout = b"".join(self.chunks["stdout"])
        stderr = b"".join(self.chunks["stderr"])
        if stdout:
            parts.append(stdout.decode(errors="replace"))
        if stderr:
            parts.append("[stderr]\n" + stderr.decode(errors="replace"))
        result = "\n".join(parts)
        if self.truncated:
            result += "\n[输出已截断]"
        return result


async def _drain_stream(
    stream: asyncio.StreamReader | None,
    name: str,
    budget: _OutputBudget,
) -> None:
    if stream is None:
        return
    while chunk := await stream.read(64 * 1024):
        budget.add(name, chunk)


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        elif proc.returncode is None:
            proc.terminate()
    except ProcessLookupError:
        pass
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass
    else:
        await asyncio.sleep(0.1)
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        elif proc.returncode is None:
            proc.kill()
    except ProcessLookupError:
        pass
    if proc.returncode is None:
        await proc.wait()


@tool(
    model=Shell,
    description="执行 shell 命令并返回输出",
    policy=ToolPolicy(
        AccessKind.REVIEW,
        DataFlow.DYNAMIC,
        detail_template="{command}",
    ),
)
async def shell(command: str, timeout: int, deps=None) -> str:
    """并发读取输出；超时或取消时终止并回收整个进程组。"""
    cwd = str(deps.workdir) if deps and deps.workdir else None
    data_guard = getattr(deps, "data_guard", None) if deps is not None else None
    config_mgr = getattr(deps, "config_mgr", None) if deps is not None else None
    base_environment = getattr(config_mgr, "environment", os.environ)
    env = (
        data_guard.safe_environment(base_environment)
        if data_guard is not None
        else {str(key): str(value) for key, value in base_environment.items()}
    )
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    budget = _OutputBudget()
    readers = [
        asyncio.create_task(_drain_stream(proc.stdout, "stdout", budget)),
        asyncio.create_task(_drain_stream(proc.stderr, "stderr", budget)),
    ]
    try:
        await asyncio.wait_for(
            asyncio.gather(proc.wait(), *readers),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        await _terminate_process_group(proc)
        await asyncio.gather(*readers, return_exceptions=True)
        return f"命令超时（{timeout}秒）"
    except asyncio.CancelledError:
        await _terminate_process_group(proc)
        await asyncio.gather(*readers, return_exceptions=True)
        raise

    output = budget.render()
    if not output:
        return f"（无输出，退出码：{proc.returncode}）"
    if proc.returncode:
        output += f"\n[退出码: {proc.returncode}]"
    return output
