from __future__ import annotations
from typing import TYPE_CHECKING
import json
import time
from pathlib import Path
from dataclasses import dataclass, field
from src.config import config

if TYPE_CHECKING:
    from src.agent import AgentDeps

WORKDIR = Path.cwd()
TRANSCRIPT_DIR = WORKDIR / ".transcripts"

@dataclass
class CompactMgr:
    deps: AgentDeps = field(repr=False)
    recent_files: list[str] = field(init=False, default_factory=list)
    has_compacted: bool = False
    last_summary: str = ""

    def __post_init__(self):
        compact_cfg = config["compact"]
        default_llm_cfg = config["llm"]["default"]
        llm_provider_name = default_llm_cfg["provider"]
        llm_provider_cfg = config["llm_provider"][llm_provider_name]
        context_limit = llm_provider_cfg["context_limit"]
        self.auto_compact_size = context_limit * compact_cfg["auto_compact_rate"]
        self.keep_recent_tool_results = compact_cfg["keep_recent_tool_results"]

    def is_need_compact(self, messages: list, prompt: list) -> bool:
        message_token = self.deps.llm.estimate_tokens(messages)
        prompt_token = self.deps.llm.estimate_tokens(prompt)
        if message_token + prompt_token > self.auto_compact_size:
            return True
        else:
            return False

    async def micro_compact(self, messages: list) -> list:
        return self.deps.llm.micro_compact(messages, self.keep_recent_tool_results)

    async def track_recent_file(self, path: str) -> None:
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.append(path)
        if len(self.recent_files) > 5:
            self.recent_files[:] = self.recent_files[-5:]


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
        await self.deps.ui.output(f"[transcript saved: {transcript_path}]\n")
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
