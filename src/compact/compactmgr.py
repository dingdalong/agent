from __future__ import annotations
from typing import TYPE_CHECKING
import json
import time
from pathlib import Path
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from src.agent import AgentDeps

WORKDIR = Path.cwd()
CONTEXT_LIMIT = 50000
KEEP_RECENT_TOOL_RESULTS = 3
TRANSCRIPT_DIR = WORKDIR / ".transcripts"

@dataclass
class CompactMgr:
    deps: AgentDeps = field(repr=False)
    recent_files: list[str] = field(init=False, default_factory=list)
    has_compacted: bool = False
    last_summary: str = ""

    def is_need_compact(self, messages: list) -> bool:
        if self.estimate_context_size(messages) > CONTEXT_LIMIT:
            return True
        else:
            return False

    def estimate_context_size(self, messages: list) -> int:
        return len(str(messages))

    async def track_recent_file(self, path: str) -> None:
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.append(path)
        if len(self.recent_files) > 5:
            self.recent_files[:] = self.recent_files[-5:]

    async def collect_tool_result_blocks(self, messages: list) -> list[tuple[int, int, dict]]:
        blocks = []
        for message_index, message in enumerate(messages):
            content = message.get("content")
            if message.get("role") != "user" or not isinstance(content, list):
                continue
            for block_index, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    blocks.append((message_index, block_index, block))
        return blocks

    async def micro_compact(self, messages: list) -> list:
        tool_results = await self.collect_tool_result_blocks(messages)
        if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
            return messages
        for _, _, block in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
            content = block.get("content", "")
            if not isinstance(content, str) or len(content) <= 120:
                continue
            block["content"] = "[Earlier tool result compacted. Re-run the tool if you need full detail.]"
        return messages

    async def write_transcript(self, messages: list) -> Path:
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
        with path.open("w") as handle:
            for message in messages:
                handle.write(json.dumps(message, default=str) + "\n")
        await self.track_recent_file(path.as_posix())
        return path

    async def summarize_history(self, messages: list) -> str:
        conversation = json.dumps(messages, default=str)[:80000]
        prompt = (
            "Summarize this coding-agent conversation so work can continue.\n"
            "Preserve:\n"
            "1. The current goal\n"
            "2. Important findings and decisions\n"
            "3. Files read or changed\n"
            "4. Remaining work\n"
            "5. User constraints and preferences\n"
            "Be compact but concrete.\n\n"
            f"{conversation}"
        )
        response = await self.deps.llm.chat(messages=[{"role": "user", "content": prompt}])
        return response.content

    async def compact_history(self, messages: list, focus: str | None = None) -> list:
        transcript_path = await self.write_transcript(messages)
        print(f"[transcript saved: {transcript_path}]")
        summary = await self.summarize_history(messages)
        if focus:
            summary += f"\n\nFocus to preserve next: {focus}"
        if self.recent_files:
            recent_lines = "\n".join(f"- {path}" for path in self.recent_files)
            summary += f"\n\nRecent files to reopen if needed:\n{recent_lines}"
        self.has_compacted = True
        self.last_summary = summary
        return [{
            "role": "user",
            "content": (
                "This conversation was compacted so the agent can continue working.\n\n"
                f"{summary}"
            ),
        }]
