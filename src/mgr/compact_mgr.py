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
from src.common.paths import project_data_dir
from src.common.secure_io import atomic_write_text
from src.prompt_tags import (
    PromptConsumer,
    PromptTag,
    render_prompt_tag,
    render_tag_guide,
)


@dataclass
class CompactResult:
    messages: list[dict]
    transcript_path: Path | None = None
    summarized_message_count: int = 0
    summary: str = ""


@dataclass
class CompactionPartition:
    """源对话消息的无损分区。"""

    prefix_messages: list[dict]
    messages_to_summarize: list[dict]
    recent_messages: list[dict]


@dataclass(frozen=True)
class _SummaryRequest:
    """已渲染的动态摘要输入及完整请求输入量估算。"""

    user_content: str
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


def _leading_developer_start(
    messages: list[dict],
    user_index: int,
    floor: int = 0,
) -> int:
    """返回与用户消息相邻的前置 developer 消息起点。"""
    start = user_index
    while start > floor and messages[start - 1].get("role") == "developer":
        start -= 1
    return start


def _contains_prompt_tag(message: dict, tag: PromptTag) -> bool:
    """判断框架 developer 消息是否包含指定注册标签。"""
    if message.get("role") != "developer":
        return False
    content = message.get("content", "")
    return isinstance(content, str) and f"<{tag.value}>" in content


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


_COMPACTOR_SYSTEM_PROMPT = "\n\n".join((
    "# 压缩职责\n"
    "生成简洁、具体、可执行的压缩历史摘要，供后续工作继续。\n"
    "只总结待压缩历史。保留原文和近期原文仅作上下文参照，不得复述、改写或重复总结；"
    "近期原文是当前状态的权威依据，发生冲突时以近期原文为准。\n"
    "保留仍有效的用户要求与约束、设计决策、完成结果、未解决问题、风险、待办、"
    "重要文件与命令、测试与错误证据，以及已有摘要中仍有效的信息。删除已被取代的尝试、"
    "寒暄、重复确认和低价值过程。直接输出完整摘要正文，不要输出 XML 标签、JSON 原文或解释。",
    render_tag_guide(PromptConsumer.COMPACTOR),
))


def _build_summary_input(
    preserved_reference: str,
    history_text: str,
    recent_reference: str,
    prior_summary: str,
    is_serialized_page: bool,
) -> str:
    """构建一条包含本批动态数据的滚动摘要 user 消息。

    Args:
        preserved_reference: 序列化后的完整保留原文消息。
        history_text: 序列化后的完整消息块或无损分页。
        recent_reference: 序列化后的完整权威近期消息。
        prior_summary: 上一次请求生成的完整摘要。
        is_serialized_page: history_text 是否为原子消息块的一页。

    Returns:
        本次压缩调用的动态 user 消息正文。
    """
    sections = []
    if prior_summary:
        sections.append(
            "这是此前各批历史的完整滚动摘要。把本批新增信息合并进去，输出更新后的完整摘要，"
            "不要只输出增量：\n"
            + render_prompt_tag(
                PromptTag.PRIOR_COMPACTION_SUMMARY, prior_summary,
            )
        )
    if is_serialized_page:
        history_instruction = (
            "下面是一个过大原子消息块的连续无损序列化分页。只总结本页新增信息；"
            "即使本页不是独立 JSON，也必须按原始文本理解："
        )
        history_format = "serialized_page"
    else:
        history_instruction = "下面是本批待压缩历史："
        history_format = "message_batch"

    sections.extend((
        "以下原文保留消息只供参照，不要总结：\n"
        + render_prompt_tag(
            PromptTag.HISTORY_REFERENCE,
            preserved_reference,
            kind="preserved",
        ),
        history_instruction
        + "\n"
        + render_prompt_tag(
            PromptTag.HISTORY_TO_SUMMARIZE,
            history_text,
            format=history_format,
        ),
        "以下近期原文只供参照，不要总结；它对当前状态具有权威性：\n"
        + render_prompt_tag(
            PromptTag.HISTORY_REFERENCE,
            recent_reference,
            kind="recent",
        ),
    ))
    return "\n\n".join(sections)


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
        bootstrap_message_count: int = 0,
    ) -> CompactionPartition:
        """划分不可压缩前缀、待摘要中段和近期原文后缀。

        初始化上下文和首个真实用户轮次始终原文保留。developer 与其后紧邻的
        user 视为同一轮；近期后缀至少保留当前用户轮次，并受配置扩展。中段旧
        developer 属于过期框架控制信息，不进入摘要；最新模式 developer 若落在
        中段则移动到近期后缀之前。

        Args:
            messages: 按原始顺序排列的源对话消息。
            bootstrap_message_count: 开头不可压缩的初始化外部消息数量。

        Returns:
            不可压缩前缀、可摘要历史和受 token 限制的近期后缀。
        """
        bootstrap_count = max(0, min(bootstrap_message_count, len(messages)))
        bootstrap_messages = messages[:bootstrap_count]
        conversation = messages[bootstrap_count:]
        user_indices = [
            idx for idx, message in enumerate(conversation)
            if message.get("role") == "user"
        ]
        if not user_indices:
            return CompactionPartition(
                prefix_messages=bootstrap_messages,
                messages_to_summarize=[
                    message for message in conversation
                    if message.get("role") != "developer"
                ],
                recent_messages=[],
            )

        first_user_index = user_indices[0]
        first_turn_end = first_user_index + 1
        preferred_turn_count = min(
            max(self.keep_recent_user_turns, 1),
            len(user_indices),
        )
        preferred_user_position = len(user_indices) - preferred_turn_count
        recent_start = _leading_developer_start(
            conversation,
            user_indices[preferred_user_position],
            first_turn_end,
        )

        if (
            self.llm.estimate_tokens(conversation[recent_start:])
            > self.recent_messages_token_limit
        ):
            recent_start = _leading_developer_start(
                conversation,
                user_indices[-1],
                first_turn_end,
            )
            for position in range(preferred_user_position + 1, len(user_indices)):
                candidate_start = _leading_developer_start(
                    conversation,
                    user_indices[position],
                    first_turn_end,
                )
                if (
                    self.llm.estimate_tokens(conversation[candidate_start:])
                    <= self.recent_messages_token_limit
                ):
                    recent_start = candidate_start
                    break

        if recent_start <= first_user_index:
            return CompactionPartition(
                prefix_messages=bootstrap_messages,
                messages_to_summarize=[],
                recent_messages=conversation,
            )

        prefix_messages = bootstrap_messages + conversation[:first_turn_end]
        middle = conversation[first_turn_end:recent_start]
        recent_messages = list(conversation[recent_start:])

        latest_mode_index = next(
            (
                index
                for index in range(len(conversation) - 1, -1, -1)
                if _contains_prompt_tag(
                    conversation[index], PromptTag.COLLABORATION_MODE,
                )
            ),
            None,
        )
        if (
            latest_mode_index is not None
            and first_turn_end <= latest_mode_index < recent_start
        ):
            recent_messages.insert(0, conversation[latest_mode_index])

        messages_to_summarize = [
            message for message in middle
            if message.get("role") != "developer"
        ]
        return CompactionPartition(
            prefix_messages=prefix_messages,
            messages_to_summarize=messages_to_summarize,
            recent_messages=recent_messages,
        )

    def _create_summary_request(
        self,
        preserved_reference: str,
        history_text: str,
        recent_reference: str,
        prior_summary: str,
        is_serialized_page: bool,
    ) -> _SummaryRequest:
        """渲染并估算一条完整摘要请求。

        Args:
            preserved_reference: 序列化后的保留原文消息。
            history_text: 序列化后的原子消息块或无损分页文本。
            recent_reference: 序列化后的权威近期消息。
            prior_summary: 上一次调用生成的完整滚动摘要。
            is_serialized_page: history_text 是否为一页不完整的 JSON。

        Returns:
            动态 user 正文和包含压缩 system 的输入 token 估算。
        """
        user_content = _build_summary_input(
            preserved_reference=preserved_reference,
            history_text=history_text,
            recent_reference=recent_reference,
            prior_summary=prior_summary,
            is_serialized_page=is_serialized_page,
        )
        messages = [{
            "role": "user",
            "content": user_content,
        }]
        system_prompt = [{"role": "system", "content": _COMPACTOR_SYSTEM_PROMPT}]
        estimated_tokens = self.llm.estimate_tokens(messages, system_prompt)
        return _SummaryRequest(
            user_content=user_content,
            estimated_tokens=estimated_tokens,
        )

    def _largest_fitting_atomic_chunk(
        self,
        messages: list[dict],
        spans: list[tuple[int, int]],
        start_block: int,
        preserved_reference: str,
        recent_reference: str,
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
            prompt=[{"role": "system", "content": _COMPACTOR_SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": request.user_content}],
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
        prior_summary: str,
        request_budget: int,
    ) -> str | None:
        """无损地分批摘要一个过大原子块的提供方分页。

        Args:
            serialized_block: 一个原子块序列化后的完整文本。
            preserved_reference: 序列化后的保留原文消息。
            recent_reference: 序列化后的权威近期消息。
            prior_summary: 上一次调用生成的完整滚动摘要。
            request_budget: 每次请求允许的最大输入 token 估算值。

        Returns:
            更新后的滚动摘要；不存在符合限制的无损请求时返回 None。
        """
        # 压缩分片依据本次摘要预算；不再借用工具分页或模型窗口比例。
        fragment_chars = max(1, request_budget // 4)
        raw_pages = [serialized_block[i:i + fragment_chars] for i in range(0, len(serialized_block), fragment_chars)] or [""]
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
    ) -> str:
        """在提供方输入预算内完整摘要历史。

        Args:
            preserved_messages: 仅用作参照且必须保留的原文消息。
            messages_to_summarize: 按原始顺序摘要的源消息。
            recent_messages: 用作参照的权威近期原文消息。

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
        summary: str,
        recent_files_hint: str = "",
    ) -> str:
        """构建放在原文前缀与近期原文之间的摘要消息。

        Args:
            summary: 压缩历史的非空摘要。
            recent_files_hint: 后续可能重新打开的可选文件路径提示。

        Returns:
            放置在权威近期原文历史之前的用户消息内容。
        """
        return (
            "以下是已压缩历史摘要，用于衔接原始用户需求和后续未压缩近期原文。\n"
            "这不是完整对话；摘要之后的未压缩近期原文应优先作为当前状态依据。\n"
            "如果摘要与后续原文冲突，以后续原文为准。\n\n"
            + render_prompt_tag(PromptTag.COMPACTED_HISTORY_SUMMARY, summary)
            + recent_files_hint
        )

    async def compact_history(
        self,
        messages: list[dict],
        bootstrap_message_count: int = 0,
    ) -> CompactResult:
        """持久化、划分、摘要并安全压缩对话历史。

        Args:
            messages: 源对话消息。
            bootstrap_message_count: 开头不可压缩的初始化外部消息数量。

        Returns:
            包含压缩后消息、对话记录路径、尝试摘要的消息数和摘要结果的对象。
        """
        if self.data_guard is not None:
            messages = self.data_guard.redact(messages)
        transcript_path = await self.write_transcript(messages)
        partition = await asyncio.to_thread(
            self.split_history_for_compaction,
            messages,
            bootstrap_message_count,
        )
        attempted_count = len(partition.messages_to_summarize)
        if attempted_count == 0:
            return CompactResult(
                messages=messages,
                transcript_path=transcript_path,
            )

        summary = await self.summarize_history(
            preserved_messages=partition.prefix_messages,
            messages_to_summarize=partition.messages_to_summarize,
            recent_messages=partition.recent_messages,
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
            summary,
            recent_files_hint,
        )
        return CompactResult(
            messages=partition.prefix_messages + [{
                "role": "user",
                "content": context_prefix,
            }] + partition.recent_messages,
            transcript_path=transcript_path,
            summarized_message_count=attempted_count,
            summary=summary,
        )
