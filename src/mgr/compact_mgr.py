from __future__ import annotations

import json
import time
from pathlib import Path
from dataclasses import dataclass, field

from src.llm.base import LLMProvider

WORKDIR = Path.cwd()
TRANSCRIPT_DIR = WORKDIR / ".transcripts"


@dataclass
class CompactResult:
    messages: list[dict]
    transcript_path: Path | None = None


@dataclass
class CompactMgr:
    llm: LLMProvider = field(repr=False)
    auto_compact_size: int = 0.8
    keep_recent_user_turns: int = 3
    recent_messages_token_limit: int = 0.25
    recent_files: list[str] = field(init=False, default_factory=list)
    has_compacted: bool = False

    def is_need_compact(self, messages: list, prompt: list, tools: list | None = None) -> bool:
        input_tokens = self.llm.estimate_tokens(messages, prompt, tools)
        if input_tokens > self.auto_compact_size:
            return True
        else:
            return False

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

    def split_history_for_compaction(self, messages: list) -> tuple[list, list, list]:
        user_indices = [
            idx for idx, message in enumerate(messages)
            if message.get("role") == "user"
        ]
        if len(user_indices) <= self.keep_recent_user_turns:
            split_idx = 0
            return [], messages[:split_idx], messages[split_idx:]

        spans: list[tuple[int, int]] = []
        for pos, start in enumerate(user_indices):
            end = user_indices[pos + 1] if pos + 1 < len(user_indices) else len(messages)
            spans.append((start, end))

        kept_spans: list[tuple[int, int]] = []
        kept_tokens = 0
        for span in reversed(spans):
            start, end = span
            span_tokens = self.llm.estimate_tokens(messages[start:end])
            if (
                len(kept_spans) >= self.keep_recent_user_turns
                and kept_tokens + span_tokens > self.recent_messages_token_limit
            ):
                break
            kept_spans.append(span)
            kept_tokens += span_tokens

        split_idx = min(start for start, _ in kept_spans) if kept_spans else len(messages)
        messages_to_summarize = messages[:split_idx]
        recent_messages = messages[split_idx:]

        preserved_messages = []
        first_user_idx = user_indices[0] if user_indices else None
        if first_user_idx is not None and first_user_idx < split_idx:
            first_user = messages[first_user_idx]
            preserved_messages.append(first_user)
            messages_to_summarize = [
                message
                for index, message in enumerate(messages_to_summarize)
                if index != first_user_idx
            ]
        return preserved_messages, messages_to_summarize, recent_messages

    async def summarize_history(
        self,
        preserved_messages: list | None = None,
        messages_to_summarize: list | None = None,
        recent_messages: list | None = None,
        focus: str | None = None,
    ) -> str:
        preserved_messages = preserved_messages or []
        messages_to_summarize = messages_to_summarize or []
        recent_messages = recent_messages or []

        if not messages_to_summarize:
            return "没有待压缩历史需要压缩。"
        conversation_to_summarize = json.dumps(messages_to_summarize, default=str)[:80000]
        recent_conversation = json.dumps(recent_messages, default=str)[:40000]
        preserved_conversation = json.dumps(preserved_messages, default=str)[:40000]
        preserved_reference = ""
        if preserved_messages:
            preserved_reference = (
                "下面是原文保留消息，仅用于理解待压缩历史：\n"
                f"<preserved_messages_reference>\n{preserved_conversation}\n</preserved_messages_reference>\n\n"
            )
        focus_instruction = ""
        if focus:
            focus_instruction = (
                "本次压缩的重点保留提示如下。总结时请按此提示保留相关内容：\n"
                f"<compaction_focus>\n{focus}\n</compaction_focus>\n\n"
            )
        prompt = (
            "请总结待压缩历史，以便后续工作可以继续。\n"
            "不要把它描述成完整对话：原文保留消息和未压缩近期原文会出现在这条摘要前后。\n"
            "原文保留消息只作为参照，用来判断待压缩历史中哪些信息仍然需要保留。\n"
            "不要总结、复述或改写已经保留为原文的内容。\n"
            "如果待压缩历史与原文保留消息冲突，以原文保留消息为准。\n"
            "如果待压缩历史依赖已保留原文，只总结待压缩历史中的后续变化、结论、状态和仍需保留的信息。\n"
            "直接输出摘要正文，不要输出 XML 标签、JSON 原文或解释说明。\n\n"
            "请保留：\n"
            "1. 用户明确提出过且后续仍应遵守的需求、约束、偏好\n"
            "2. 已确认的设计决策、实现策略、边界条件和取舍\n"
            "3. 已完成的关键操作，以及它们产生的结果或结论\n"
            "4. 已发现但尚未解决的问题、风险、失败原因和待办事项\n"
            "5. 重要文件、命令、测试结果、错误信息或外部状态的引用\n"
            "6. 如果待压缩历史中有被压缩前的摘要，请合并其中仍然有效的信息\n\n"
            "请删除：\n"
            "1. 已被后续结论取代的尝试过程\n"
            "2. 无结论的寒暄、重复确认和低价值中间推理\n"
            "3. 未压缩近期原文中会完整保留的内容\n\n"
            "摘要要简洁、具体、可执行，并适合作为未压缩近期原文之前的上下文前缀。\n\n"
            f"{focus_instruction}"
            f"{preserved_reference}"
            "下面是待压缩历史：\n"
            f"<history_to_summarize>\n{conversation_to_summarize}\n</history_to_summarize>\n\n"
            "下面是未压缩近期原文，仅用于判断哪些待压缩历史信息仍需保留：\n"
            f"<recent_raw_messages_reference>\n{recent_conversation}\n</recent_raw_messages_reference>"
        )
        response = await self.llm.chat(messages=[{"role": "user", "content": prompt}])
        return response.content

    def build_compacted_context_prefix(
        self,
        preserved_messages: list,
        summary: str,
        recent_files_hint: str = "",
    ) -> str:
        preserved_parts = []
        for message in preserved_messages:
            if message.get("role") != "user":
                continue
            preserved_parts.append(str(message.get("content", "")))

        original_request_section = ""
        if preserved_parts:
            original_request_section = (
                "以下是本轮任务最初的原始用户需求，作为后续上下文的最高优先级来源之一。\n"
                "<original_user_request>\n"
                f"{'\n\n'.join(preserved_parts)}\n"
                "</original_user_request>\n\n"
            )

        return (
            f"{original_request_section}"
            "以下是已压缩历史摘要，用于衔接原始用户需求和后续未压缩近期原文。\n"
            "这不是完整对话；摘要之后的未压缩近期原文应优先作为当前状态依据。\n"
            "如果摘要与后续原文冲突，以后续原文为准。\n\n"
            f"<compacted_history_summary>\n{summary}\n</compacted_history_summary>"
            f"{recent_files_hint}"
        )

    async def compact_history(self, messages: list, focus: str | None = None) -> CompactResult:
        transcript_path = await self.write_transcript(messages)
        preserved_messages, messages_to_summarize, recent_messages = self.split_history_for_compaction(messages)
        summary = await self.summarize_history(
            preserved_messages=preserved_messages,
            messages_to_summarize=messages_to_summarize,
            recent_messages=recent_messages,
            focus=focus,
        )
        recent_files_hint = ""
        if self.recent_files:
            recent_lines = "\n".join(f"- {path}" for path in self.recent_files)
            recent_files_hint = f"\n\n如有需要，可重新打开这些近期文件：\n{recent_lines}"
        self.has_compacted = True
        if not messages_to_summarize:
            return CompactResult(
                messages=preserved_messages + recent_messages,
                transcript_path=transcript_path,
            )

        context_prefix = self.build_compacted_context_prefix(
            preserved_messages,
            summary,
            recent_files_hint,
        )
        if recent_messages and recent_messages[0].get("role") == "user":
            first_recent = dict(recent_messages[0])
            first_recent["content"] = (
                f"{context_prefix}\n\n"
                "以下是未压缩近期原文中的第一条用户消息，之后的历史消息保持原始顺序。\n"
                "<uncompressed_recent_user_message>\n"
                f"{first_recent.get('content', '')}\n"
                "</uncompressed_recent_user_message>"
            )
            return CompactResult(
                messages=[first_recent] + recent_messages[1:],
                transcript_path=transcript_path,
            )

        return CompactResult(
            messages=[{
                "role": "user",
                "content": context_prefix,
            }] + recent_messages,
            transcript_path=transcript_path,
        )
