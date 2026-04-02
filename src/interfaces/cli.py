"""CLIInterface — 命令行交互实现。"""

import asyncio

from src.events.types import (
    Event,
    GraphStarted,
    GraphEnded,
    NodeStarted,
    NodeEnded,
    ErrorOccurred,
    AgentStarted,
    AgentEnded,
    ToolCalled,
    ToolResult,
    Handoff,
    TokenDelta,
    ThinkingDelta,
)


class CLIInterface:
    """基于标准输入/输出的 CLI 交互实现。"""

    async def prompt(self, message: str) -> str:
        return await asyncio.to_thread(input, message)

    async def display(self, message: str) -> None:
        print(message, end="", flush=True)

    async def confirm(self, message: str) -> bool:
        response = await self.prompt(f"{message} (y/n): ")
        return response.strip().lower() in ("y", "yes", "确认")

    async def on_event(self, event: Event) -> None:
        """按事件类型格式化输出到终端。"""
        match event:
            case GraphStarted():
                print("\n[开始执行]", flush=True)
            case GraphEnded():
                print("\n[执行完成]", flush=True)
            case NodeStarted(source=name, node_type=ntype):
                print(f"\n  ▶ {name} ({ntype})", flush=True)
            case NodeEnded(source=name, output_summary=summary):
                label = f": {summary}" if summary else ""
                print(f"  ✓ {name} 完成{label}", flush=True)
            case ErrorOccurred(source=name, error=err):
                print(f"  ✗ {name} 错误: {err}", flush=True)
            case AgentStarted(agent_name=name):
                print(f"    [agent] {name} 开始", flush=True)
            case AgentEnded(agent_name=name):
                print(f"    [agent] {name} 结束", flush=True)
            case ToolCalled(tool_name=name, args=args):
                print(f"    ⚙ {name}({args})", flush=True)
            case ToolResult(tool_name=name, result=result):
                truncated = result[:200] + "..." if len(result) > 200 else result
                print(f"    ← {name}: {truncated}", flush=True)
            case Handoff(from_agent=src, to_agent=dst, task=task):
                print(f"    → {src} → {dst}: {task}", flush=True)
            case TokenDelta(delta=d):
                print(d, end="", flush=True)
            case ThinkingDelta(content=c):
                print(f"    💭 {c}", flush=True)
