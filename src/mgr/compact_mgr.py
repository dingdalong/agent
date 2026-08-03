from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.llm.base import LLMProvider
from src.mgr.paths import project_data_dir
from src.mgr.secure_io import atomic_write_text


@dataclass
class CompactResult:
    messages: list[dict]
    transcript_path: Path | None = None
    summarized_message_count: int = 0
    summary: str = ""


@dataclass
class CompactionPartition:
    """源对话消息的无损分区。"""

    preserved_messages: list[dict]
    messages_to_summarize: list[dict]
    recent_messages: list[dict]


@dataclass(frozen=True)
class _SummaryRequest:
    """已完整渲染的摘要请求及其输入量估算。"""

    prompt: str
    estimated_tokens: int


def _serialize_json(value: object) -> str:
    """将完整数据序列化为保留 Unicode 原文的 JSON。

    Args:
        value: 为对话记录或摘要请求序列化的数据。

    Returns:
        不转义为 ASCII 且不截断字符的完整 JSON 文本。
    """
    return json.dumps(value, ensure_ascii=False, default=str)


def _atomic_message_spans(messages: list[dict]) -> list[tuple[int, int]]:
    """将助手消息与紧随其后的工具结果归为同一组。

    Args:
        messages: 源对话消息。

    Returns:
        不可拆分消息块在源消息中的左闭右开索引区间。
    """
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(messages):
        start = index
        index += 1
        if messages[start].get("role") == "assistant":
            while index < len(messages) and messages[index].get("role") == "tool":
                index += 1
        spans.append((start, index))
    return spans


def _validate_and_materialize_pages(
    pages: list[str],
    serialized_block: str,
) -> list[str] | None:
    """校验并复制无损且非空的序列化分页。

    Args:
        pages: 提供方生成的待校验、待具体化分页。
        serialized_block: 所有分页必须能够重建的完整源文本。

    Returns:
        校验通过后的普通分页列表；分页无效或有损时返回 None。
    """
    materialized_pages = list(pages)
    if (
        not materialized_pages
        or any(
            not isinstance(page, str) or not page
            for page in materialized_pages
        )
        or "".join(materialized_pages) != serialized_block
    ):
        return None
    return materialized_pages


def _bisect_fragment_at(fragments: list[str], fragment_index: int) -> bool:
    """将一个过大的片段原位替换为两个无损的字符串半片。

    Args:
        fragments: 包含过大文本的可变有序片段列表。
        fragment_index: 待原位替换片段的索引。

    Returns:
        成功拆分片段时返回 True；无法继续拆分时返回 False。
    """
    fragment = fragments[fragment_index]
    if len(fragment) <= 1:
        return False
    midpoint = len(fragment) // 2
    fragments[fragment_index:fragment_index + 1] = [
        fragment[:midpoint],
        fragment[midpoint:],
    ]
    return True


def _message_content_text(message: dict) -> str:
    """在不转义 Unicode 文本的前提下渲染一条消息的内容。

    Args:
        message: 待渲染内容的消息。

    Returns:
        字符串原文；结构化内容则返回完整 JSON。
    """
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return _serialize_json(content)


def _build_summary_prompt(
    preserved_reference: str,
    history_text: str,
    recent_reference: str,
    focus: str | None,
    prior_summary: str,
    is_serialized_page: bool,
) -> str:
    """构建一条具体的滚动摘要提示词。

    Args:
        preserved_reference: 序列化后的完整保留原文消息。
        history_text: 序列化后的完整消息块或无损分页。
        recent_reference: 序列化后的完整权威近期消息。
        focus: 用户可选提供的压缩重点。
        prior_summary: 上一次请求生成的完整摘要。
        is_serialized_page: history_text 是否为原子消息块的一页。

    Returns:
        要求生成完整更新版滚动摘要的提示词。
    """
    focus_section = ""
    if focus:
        focus_section = (
            "重点保留提示：\n"
            f"<compaction_focus>\n{focus}\n</compaction_focus>\n\n"
        )
    prior_section = ""
    if prior_summary:
        prior_section = (
            "这是此前各批历史的完整滚动摘要。把本批新增信息合并进去，输出更新后的完整摘要，"
            "不要只输出增量：\n"
            f"<prior_rolling_summary>\n{prior_summary}\n</prior_rolling_summary>\n\n"
        )
    if is_serialized_page:
        history_instruction = (
            "下面是一个过大原子消息块的连续无损序列化分页。只总结本页新增信息；"
            "即使本页不是独立 JSON，也必须按原始文本理解："
        )
        history_tag = "serialized_history_page_to_summarize"
    else:
        history_instruction = "下面是本批待压缩历史："
        history_tag = "history_chunk_to_summarize"

    return (
        "请生成一份简洁、具体、可执行的压缩历史摘要，供后续工作继续。\n"
        "只总结待压缩历史。保留原文和近期原文仅作上下文参照，不得复述、改写或重复总结；"
        "近期原文是当前状态的权威依据，发生冲突时以近期原文为准。\n"
        "保留仍有效的用户要求与约束、设计决策、完成结果、未解决问题、风险、待办、"
        "重要文件与命令、测试与错误证据，以及已有摘要中仍有效的信息。删除已被取代的尝试、"
        "寒暄、重复确认和低价值过程。直接输出完整摘要正文，不要输出 XML 标签、JSON 原文或解释。\n\n"
        f"{focus_section}"
        f"{prior_section}"
        "以下原文保留消息只供参照，不要总结：\n"
        f"<preserved_messages_reference>\n{preserved_reference}\n"
        "</preserved_messages_reference>\n\n"
        f"{history_instruction}\n"
        f"<{history_tag}>\n{history_text}\n</{history_tag}>\n\n"
        "以下近期原文只供参照，不要总结；它对当前状态具有权威性：\n"
        f"<recent_raw_messages_reference>\n{recent_reference}\n"
        "</recent_raw_messages_reference>"
    )


@dataclass
class CompactMgr:
    """上下文压缩管理器。

    Args:
        llm: LLM 提供方实例，用于估算 token 和生成摘要。
        workdir: 用户工作目录，对话记录存放在 workdir/.agent/transcripts/。
        caller_agent_type: 所属 agent 类型，透传给内部摘要调用事件。
        caller_uuid: 所属 agent UUID，透传给内部摘要调用事件。
        auto_compact_size: 自动压缩的绝对输入 token 阈值；非正数表示禁用。
        keep_recent_user_turns: 优先保留原文的近期用户轮数。
        recent_messages_token_limit: 保留近期原文的绝对 token 上限。
    """

    llm: LLMProvider = field(repr=False)
    workdir: Path = field(repr=False)
    caller_agent_type: str | None = None
    caller_uuid: str | None = None
    auto_compact_size: int = 0
    keep_recent_user_turns: int = 3
    recent_messages_token_limit: int = 0
    data_guard: Any = field(default=None, repr=False)
    recent_files: list[str] = field(init=False, default_factory=list)
    has_compacted: bool = False

    def is_need_compact(
        self,
        messages: list[dict],
        prompt: list[dict] | None,
        tools: list[dict] | None = None,
        estimated_tokens: int | None = None,
    ) -> bool:
        """判断提供方的完整输入是否超过自动压缩阈值。

        Args:
            messages: 将提交给提供方的会话消息。
            prompt: 将提交给提供方的系统提示词。
            tools: 将提交给提供方的工具结构定义。
            estimated_tokens: 可复用的完整输入 token 估算；None 时现场估算。

        Returns:
            阈值为正且输入 token 估算严格超过阈值时返回 True。
        """
        if self.auto_compact_size <= 0:
            return False
        input_tokens = estimated_tokens
        if input_tokens is None:
            input_tokens = self.llm.estimate_tokens(messages, prompt, tools)
        return input_tokens > self.auto_compact_size

    async def track_recent_file(self, path: str) -> None:
        """记录一条近期对话记录文件路径。

        Args:
            path: 相对于工作目录的对话记录文件路径。

        Returns:
            无返回值。
        """
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.append(path)
        if len(self.recent_files) > 5:
            self.recent_files[:] = self.recent_files[-5:]

    def _write_transcript_sync(self, messages: list[dict]) -> Path:
        """以独占且可避免名称冲突的方式写入对话记录文件。

        Args:
            messages: 以 JSON Lines 格式写入的源对话消息。

        Returns:
            已创建对话记录文件的绝对路径。
        """
        transcript_dir = project_data_dir(self.workdir) / "transcripts"
        path = transcript_dir / f"transcript_{time.time_ns()}_{uuid.uuid4().hex}.jsonl"
        safe_messages = self.data_guard.redact(messages) if self.data_guard is not None else messages
        content = "".join(_serialize_json(message) + "\n" for message in safe_messages)
        atomic_write_text(path, content)
        return path

    async def write_transcript(self, messages: list[dict]) -> Path:
        """将对话历史写入对话记录文件。

        Args:
            messages: 待写入的消息列表。

        Returns:
            对话记录文件路径。
        """
        path = await asyncio.to_thread(self._write_transcript_sync, messages)
        await self.track_recent_file(path.relative_to(self.workdir).as_posix())
        return path

    def split_history_for_compaction(
        self,
        messages: list[dict],
    ) -> CompactionPartition:
        """在不拆分助手消息与工具结果原子块的前提下划分历史。

        最后 ``keep_recent_user_turns`` 个用户轮次定义优先保留原文的后缀。
        后缀未超出限制时从该位置准确开始；后缀过大时按原子块向后裁剪。
        该配置为零时不保留用户轮次后缀。没有用户消息时，保留符合限制的
        最大原子后缀。

        Args:
            messages: 按原始顺序排列的源对话消息。

        Returns:
            必须保留的消息、可摘要历史和受 token 限制的近期后缀。
        """
        user_indices = [
            idx for idx, message in enumerate(messages)
            if message.get("role") == "user"
        ]
        if user_indices and self.keep_recent_user_turns <= 0:
            recent_start = len(messages)
        else:
            if user_indices:
                preferred_turn_count = min(
                    self.keep_recent_user_turns,
                    len(user_indices),
                )
                preferred_start = user_indices[-preferred_turn_count]
            else:
                preferred_start = 0

            preferred_messages = messages[preferred_start:]
            if (
                not preferred_messages
                or self.llm.estimate_tokens(preferred_messages)
                <= self.recent_messages_token_limit
            ):
                recent_start = preferred_start
            else:
                recent_start = len(messages)
                for start, _ in reversed(_atomic_message_spans(messages)):
                    if start < preferred_start:
                        break
                    candidate = messages[start:]
                    if (
                        self.llm.estimate_tokens(candidate)
                        > self.recent_messages_token_limit
                    ):
                        break
                    recent_start = start

        required_user_indices: list[int] = []
        if user_indices:
            required_user_indices.append(user_indices[0])
            if user_indices[-1] != user_indices[0]:
                required_user_indices.append(user_indices[-1])
        preserved_indices = {
            index for index in required_user_indices if index < recent_start
        }
        preserved_messages = [
            message
            for index, message in enumerate(messages[:recent_start])
            if index in preserved_indices
        ]
        messages_to_summarize = [
            message
            for index, message in enumerate(messages[:recent_start])
            if index not in preserved_indices
        ]
        return CompactionPartition(
            preserved_messages=preserved_messages,
            messages_to_summarize=messages_to_summarize,
            recent_messages=messages[recent_start:],
        )

    def _create_summary_request(
        self,
        preserved_reference: str,
        history_text: str,
        recent_reference: str,
        focus: str | None,
        prior_summary: str,
        is_serialized_page: bool,
    ) -> _SummaryRequest:
        """渲染并估算一条完整摘要请求。

        Args:
            preserved_reference: 序列化后的保留原文消息。
            history_text: 序列化后的原子消息块或无损分页文本。
            recent_reference: 序列化后的权威近期消息。
            focus: 用户可选提供的压缩重点。
            prior_summary: 上一次调用生成的完整滚动摘要。
            is_serialized_page: history_text 是否为一页不完整的 JSON。

        Returns:
            渲染后的提示词和提供方估算的输入 token 数量。
        """
        prompt = _build_summary_prompt(
            preserved_reference=preserved_reference,
            history_text=history_text,
            recent_reference=recent_reference,
            focus=focus,
            prior_summary=prior_summary,
            is_serialized_page=is_serialized_page,
        )
        estimated_tokens = self.llm.estimate_tokens([{
            "role": "user",
            "content": prompt,
        }])
        return _SummaryRequest(prompt=prompt, estimated_tokens=estimated_tokens)

    def _largest_fitting_atomic_chunk(
        self,
        messages: list[dict],
        spans: list[tuple[int, int]],
        start_block: int,
        preserved_reference: str,
        recent_reference: str,
        focus: str | None,
        prior_summary: str,
        request_budget: int,
    ) -> tuple[int, _SummaryRequest] | None:
        """查找接下来符合限制的最大完整原子块序列。

        Args:
            messages: 待摘要的完整消息。
            spans: 消息列表中的原子消息块区间。
            start_block: 第一个未摘要原子块的索引。
            preserved_reference: 序列化后的保留原文消息。
            recent_reference: 序列化后的权威近期消息。
            focus: 用户可选提供的压缩重点。
            prior_summary: 上一次调用生成的完整滚动摘要。
            request_budget: 每次请求允许的最大输入 token 估算值。

        Returns:
            下一块的右开索引及其请求；单个块也无法满足限制时返回 None。
        """
        source_start = spans[start_block][0]
        best: tuple[int, _SummaryRequest] | None = None
        for block_index in range(start_block, len(spans)):
            source_end = spans[block_index][1]
            history_text = _serialize_json(messages[source_start:source_end])
            request = self._create_summary_request(
                preserved_reference=preserved_reference,
                history_text=history_text,
                recent_reference=recent_reference,
                focus=focus,
                prior_summary=prior_summary,
                is_serialized_page=False,
            )
            if request.estimated_tokens > request_budget:
                break
            best = block_index + 1, request
        return best

    async def _call_summary_request(self, request: _SummaryRequest) -> str:
        """调用 LLM 处理一条已渲染的摘要请求。

        Args:
            request: 已渲染且预先估算过的摘要请求。

        Returns:
            去除首尾空白的摘要正文；响应为空时返回空字符串。
        """
        response = await self.llm.chat(
            messages=[{"role": "user", "content": request.prompt}],
            caller_agent_type=self.caller_agent_type,
            caller_uuid=self.caller_uuid,
            enable_thinking=False,
        )
        return (response.content or "").strip()

    async def _summarize_serialized_pages(
        self,
        serialized_block: str,
        preserved_reference: str,
        recent_reference: str,
        focus: str | None,
        prior_summary: str,
        request_budget: int,
    ) -> str | None:
        """无损地分批摘要一个过大原子块的提供方分页。

        Args:
            serialized_block: 一个原子块序列化后的完整文本。
            preserved_reference: 序列化后的保留原文消息。
            recent_reference: 序列化后的权威近期消息。
            focus: 用户可选提供的压缩重点。
            prior_summary: 上一次调用生成的完整滚动摘要。
            request_budget: 每次请求允许的最大输入 token 估算值。

        Returns:
            更新后的滚动摘要；不存在符合限制的无损请求时返回 None。
        """
        raw_pages = await asyncio.to_thread(self.llm.split_page, serialized_block)
        fragments = await asyncio.to_thread(
            _validate_and_materialize_pages,
            raw_pages,
            serialized_block,
        )
        if fragments is None:
            return None

        rolling_summary = prior_summary
        fragment_index = 0
        while fragment_index < len(fragments):
            fragment = fragments[fragment_index]
            request = await asyncio.to_thread(
                self._create_summary_request,
                preserved_reference,
                fragment,
                recent_reference,
                focus,
                rolling_summary,
                True,
            )
            if request.estimated_tokens > request_budget:
                made_progress = await asyncio.to_thread(
                    _bisect_fragment_at,
                    fragments,
                    fragment_index,
                )
                if not made_progress:
                    return None
                continue

            rolling_summary = await self._call_summary_request(request)
            if not rolling_summary:
                return None
            fragment_index += 1
        return rolling_summary

    async def summarize_history(
        self,
        preserved_messages: list[dict] | None = None,
        messages_to_summarize: list[dict] | None = None,
        recent_messages: list[dict] | None = None,
        focus: str | None = None,
    ) -> str:
        """在提供方输入预算内完整摘要历史。

        Args:
            preserved_messages: 仅用作参照且必须保留的原文消息。
            messages_to_summarize: 按原始顺序摘要的源消息。
            recent_messages: 用作参照的权威近期原文消息。
            focus: 用户可选提供的压缩重点。

        Returns:
            最终滚动摘要；摘要失败时返回空字符串。
        """
        preserved_messages = preserved_messages or []
        messages_to_summarize = messages_to_summarize or []
        recent_messages = recent_messages or []
        if self.data_guard is not None:
            preserved_messages = self.data_guard.redact(preserved_messages)
            messages_to_summarize = self.data_guard.redact(messages_to_summarize)
            recent_messages = self.data_guard.redact(recent_messages)
            focus = str(self.data_guard.redact(focus)) if focus is not None else None
        if not messages_to_summarize:
            return ""

        preserved_reference, recent_reference, full_history_text = await asyncio.gather(
            asyncio.to_thread(_serialize_json, preserved_messages),
            asyncio.to_thread(_serialize_json, recent_messages),
            asyncio.to_thread(_serialize_json, messages_to_summarize),
        )
        full_request = await asyncio.to_thread(
            self._create_summary_request,
            preserved_reference,
            full_history_text,
            recent_reference,
            focus,
            "",
            False,
        )
        if self.llm.context_limit <= 0:
            return await self._call_summary_request(full_request)

        request_budget = math.floor(self.llm.context_limit * 0.95)
        if full_request.estimated_tokens <= request_budget:
            return await self._call_summary_request(full_request)

        spans = await asyncio.to_thread(
            _atomic_message_spans,
            messages_to_summarize,
        )
        rolling_summary = ""
        block_index = 0
        while block_index < len(spans):
            fitting_chunk = await asyncio.to_thread(
                self._largest_fitting_atomic_chunk,
                messages_to_summarize,
                spans,
                block_index,
                preserved_reference,
                recent_reference,
                focus,
                rolling_summary,
                request_budget,
            )
            if fitting_chunk is not None:
                block_index, request = fitting_chunk
                rolling_summary = await self._call_summary_request(request)
                if not rolling_summary:
                    return ""
                continue

            start, end = spans[block_index]
            serialized_block = await asyncio.to_thread(
                _serialize_json,
                messages_to_summarize[start:end],
            )
            paged_summary = await self._summarize_serialized_pages(
                serialized_block=serialized_block,
                preserved_reference=preserved_reference,
                recent_reference=recent_reference,
                focus=focus,
                prior_summary=rolling_summary,
                request_budget=request_budget,
            )
            if paged_summary is None:
                return ""
            rolling_summary = paged_summary
            block_index += 1
        return rolling_summary

    def build_compacted_context_prefix(
        self,
        preserved_messages: list[dict],
        summary: str,
        recent_files_hint: str = "",
    ) -> str:
        """根据原文参照和摘要构建压缩后的用户消息前缀。

        Args:
            preserved_messages: 近期历史之外必须保留的用户原文消息。
            summary: 压缩历史的非空摘要。
            recent_files_hint: 后续可能重新打开的可选文件路径提示。

        Returns:
            放置在权威近期原文历史之前的用户消息内容。
        """
        preserved_users = [
            message for message in preserved_messages
            if message.get("role") == "user"
        ]
        preserved_sections: list[str] = []
        for index, message in enumerate(preserved_users):
            if len(preserved_users) == 1:
                tag = "preserved_user_message"
            elif index == 0:
                tag = "first_user_message"
            elif index == len(preserved_users) - 1:
                tag = "current_user_message"
            else:
                tag = "preserved_user_message"
            preserved_sections.append(
                f"<{tag}>\n{_message_content_text(message)}\n</{tag}>"
            )

        preserved_reference = ""
        if preserved_sections:
            preserved_reference = (
                "以下是压缩时必须保留的用户原文；各条用途由标签区分。\n"
                f"{'\n\n'.join(preserved_sections)}\n\n"
            )

        return (
            f"{preserved_reference}"
            "以下是已压缩历史摘要，用于衔接原始用户需求和后续未压缩近期原文。\n"
            "这不是完整对话；摘要之后的未压缩近期原文应优先作为当前状态依据。\n"
            "如果摘要与后续原文冲突，以后续原文为准。\n\n"
            f"<compacted_history_summary>\n{summary}\n</compacted_history_summary>"
            f"{recent_files_hint}"
        )

    async def compact_history(
        self,
        messages: list[dict],
        focus: str | None = None,
    ) -> CompactResult:
        """持久化、划分、摘要并安全压缩对话历史。

        Args:
            messages: 源对话消息。
            focus: 用户可选提供的摘要重点。

        Returns:
            包含压缩后消息、对话记录路径、尝试摘要的消息数和摘要结果的对象。
        """
        if self.data_guard is not None:
            messages = self.data_guard.redact(messages)
            focus = str(self.data_guard.redact(focus)) if focus is not None else None
        transcript_path = await self.write_transcript(messages)
        partition = await asyncio.to_thread(
            self.split_history_for_compaction,
            messages,
        )
        attempted_count = len(partition.messages_to_summarize)
        if attempted_count == 0:
            return CompactResult(
                messages=messages,
                transcript_path=transcript_path,
            )

        summary = await self.summarize_history(
            preserved_messages=partition.preserved_messages,
            messages_to_summarize=partition.messages_to_summarize,
            recent_messages=partition.recent_messages,
            focus=focus,
        )
        summary = summary.strip()
        if not summary:
            return CompactResult(
                messages=messages,
                transcript_path=transcript_path,
                summarized_message_count=attempted_count,
            )

        recent_files_hint = ""
        if self.recent_files:
            recent_lines = "\n".join(f"- {path}" for path in self.recent_files)
            recent_files_hint = f"\n\n如有需要，可重新打开这些近期文件：\n{recent_lines}"
        self.has_compacted = True
        context_prefix = await asyncio.to_thread(
            self.build_compacted_context_prefix,
            partition.preserved_messages,
            summary,
            recent_files_hint,
        )
        if (
            partition.recent_messages
            and partition.recent_messages[0].get("role") == "user"
        ):
            first_recent = dict(partition.recent_messages[0])
            first_recent["content"] = (
                f"{context_prefix}\n\n"
                "以下是未压缩近期原文中的第一条用户消息，之后的历史消息保持原始顺序。\n"
                "<uncompressed_recent_user_message>\n"
                f"{first_recent.get('content', '')}\n"
                "</uncompressed_recent_user_message>"
            )
            return CompactResult(
                messages=[first_recent] + partition.recent_messages[1:],
                transcript_path=transcript_path,
                summarized_message_count=attempted_count,
                summary=summary,
            )

        return CompactResult(
            messages=[{
                "role": "user",
                "content": context_prefix,
            }] + partition.recent_messages,
            transcript_path=transcript_path,
            summarized_message_count=attempted_count,
            summary=summary,
        )
