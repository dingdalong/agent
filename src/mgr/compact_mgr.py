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
    keep_recent_user_turns: int = field(init=False)
    recent_messages_token_limit: int = field(init=False)

    def __post_init__(self):
        compact_cfg = config["compact"]
        default_llm_cfg = config["llm"]["default"]
        llm_provider_name = default_llm_cfg["provider"]
        llm_provider_cfg = config["llm_provider"][llm_provider_name]
        context_limit = llm_provider_cfg["context_limit"]
        self.auto_compact_size = context_limit * compact_cfg["auto_compact_rate"]
        self.keep_recent_tool_results = compact_cfg["keep_recent_tool_results"]
        self.keep_recent_user_turns = compact_cfg.get("keep_recent_user_turns", 3)
        self.recent_messages_token_limit = int(
            context_limit * compact_cfg.get("keep_recent_messages_token_rate", 0.25)
        )

    def is_need_compact(self, messages: list, prompt: list, tools: list | None = None) -> bool:
        input_tokens = self.deps.llm.estimate_tokens(messages, prompt, tools)
        if input_tokens > self.auto_compact_size:
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

    def split_history_for_compaction(self, messages: list) -> tuple[list, list]:
        user_indices = [
            idx for idx, message in enumerate(messages)
            if message.get("role") == "user"
        ]
        if len(user_indices) <= self.keep_recent_user_turns:
            split_idx = 0
            return messages[:split_idx], messages[split_idx:]

        spans: list[tuple[int, int]] = []
        for pos, start in enumerate(user_indices):
            end = user_indices[pos + 1] if pos + 1 < len(user_indices) else len(messages)
            spans.append((start, end))

        kept_spans: list[tuple[int, int]] = []
        kept_tokens = 0
        for span in reversed(spans):
            start, end = span
            span_tokens = self.deps.llm.estimate_tokens(messages[start:end])
            if (
                len(kept_spans) >= self.keep_recent_user_turns
                and kept_tokens + span_tokens > self.recent_messages_token_limit
            ):
                break
            kept_spans.append(span)
            kept_tokens += span_tokens

        split_idx = min(start for start, _ in kept_spans) if kept_spans else len(messages)
        return messages[:split_idx], messages[split_idx:]

    async def summarize_history(self, early_messages: list, recent_messages: list) -> str:
        if not early_messages:
            return "没有早期对话需要压缩。"
        early_conversation = json.dumps(early_messages, default=str)[:80000]
        recent_conversation = json.dumps(recent_messages, default=str)[:40000]
        prompt = (
            "请总结这段对话中的早期部分，以便后续工作可以继续。\n"
            "不要把它描述成完整对话：未压缩的近期原文会保留在这条摘要后面。\n"
            "近期原文只作为参照，用来判断早期信息中哪些仍然需要保留。\n"
            "不要总结、复述或改写近期原文。\n\n"
            "如果早期历史与近期原文冲突，以近期原文为准。\n"
            "直接输出摘要正文，不要输出 XML 标签、JSON 原文或解释说明。\n\n"
            "请保留：\n"
            "1. 用户明确提出过且后续仍应遵守的需求、约束、偏好\n"
            "2. 已确认的设计决策、实现策略、边界条件和取舍\n"
            "3. 已完成的关键操作，以及它们产生的结果或结论\n"
            "4. 已发现但尚未解决的问题、风险、失败原因和待办事项\n"
            "5. 重要文件、命令、测试结果、错误信息或外部状态的引用\n"
            "6. 如果早期对话中有被压缩前的摘要，请合并其中仍然有效的信息\n\n"
            "请删除：\n"
            "1. 已被后续结论取代的尝试过程\n"
            "2. 无结论的寒暄、重复确认和低价值中间推理\n"
            "3. 近期原文中会完整保留的内容\n\n"
            "摘要要简洁、具体、可执行，并适合作为近期原文之前的上下文前缀。\n\n"
            "下面是需要压缩的早期历史：\n"
            f"<early_history_to_summarize>\n{early_conversation}\n</early_history_to_summarize>\n\n"
            "下面是已经保留为原文的近期对话，仅用于判断哪些早期信息仍需保留：\n"
            f"<recent_raw_messages_reference>\n{recent_conversation}\n</recent_raw_messages_reference>"
        )
        response = await self.deps.llm.chat(messages=[{"role": "user", "content": prompt}])
        return response.content

    async def compact_history(self, messages: list, focus: str | None = None) -> list:
        transcript_path = await self.write_transcript(messages)
        await self.deps.event_bus.request_output(f"[transcript saved: {transcript_path}]\n")
        early_messages, recent_messages = self.split_history_for_compaction(messages)
        summary = await self.summarize_history(early_messages, recent_messages)
        if focus:
            summary += f"\n\n接下来需要重点保留：{focus}"
        if self.recent_files:
            recent_lines = "\n".join(f"- {path}" for path in self.recent_files)
            summary += f"\n\n如有需要，可重新打开这些近期文件：\n{recent_lines}"
        self.has_compacted = True
        self.last_summary = summary
        return [{
            "role": "user",
            "content": (
                "以下是早期对话的压缩摘要，用于衔接后续未压缩的近期原文。\n"
                "这不是完整对话；本消息之后的历史消息仍然是未压缩原文，应优先作为当前状态依据。\n"
                "如果摘要与后续原文冲突，以后续原文为准。\n\n"
                f"<early_history_summary>\n{summary}\n</early_history_summary>"
            ),
        }] + recent_messages
