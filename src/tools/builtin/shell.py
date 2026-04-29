from __future__ import annotations

import asyncio
from src.tools.decorator import tool
from pydantic import BaseModel, Field


class Shell(BaseModel):
    command: str = Field(..., description="要执行的 shell 命令")
    timeout: int = Field(default=300, description="超时时间（秒）")


@tool(model=Shell, description="执行 shell 命令并返回输出", sensitive=True)
async def shell(command: str, timeout: int) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"命令超时（{timeout}秒）"

    parts = []
    if stdout:
        parts.append(stdout.decode(errors="replace"))
    if stderr:
        parts.append(f"[stderr]\n{stderr.decode(errors='replace')}")
    if not parts:
        return f"（无输出，退出码：{proc.returncode}）"
    if proc.returncode != 0:
        parts.append(f"[退出码: {proc.returncode}]")
    return "\n".join(parts)
