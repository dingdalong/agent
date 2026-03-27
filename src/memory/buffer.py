"""对话缓冲管理。

ConversationBuffer 管理短期对话历史，支持 token 缓存和自动压缩。
"""

import logging
import uuid
from typing import TYPE_CHECKING, Any

import tiktoken

from src.llm.base import LLMProvider
from src.utils.performance import async_time_function

if TYPE_CHECKING:
    from .store import MemoryStore

logger = logging.getLogger(__name__)

_enc = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    return len(_enc.encode(text))


@async_time_function()
async def summarize_conversation(
    messages: list[dict[str, Any]],
    llm: LLMProvider,
) -> str:
    """调用模型生成对话摘要。"""
    prompt = (
        "请将以下对话内容总结为一段简洁的摘要，保留关键信息（如用户偏好、重要事实、已完成的步骤）。"
        "只输出摘要本身，不要多余的解释。\n\n"
        "对话：\n"
    )
    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")
        if role == "user":
            prompt += f"用户：{content}\n"
        elif role == "assistant":
            prompt += f"助手：{content}\n"

    response = await llm.chat(
        messages=[{"role": "user", "content": prompt}],
        silent=True,
    )
    return response.content


class ConversationBuffer:
    """短期对话缓冲，带 token 缓存。"""

    def __init__(
        self,
        max_rounds: int = 10,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        conversation_id: str | None = None,
    ):
        self.max_rounds = max_rounds
        self.max_tokens = max_tokens
        self.system_prompt = system_prompt
        self.conversation_id = conversation_id or str(uuid.uuid4())
        self._messages: list[dict[str, Any]] = []
        self._token_cache: list[int] = []  # 与 _messages 一一对应

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self._messages

    def _append(self, msg: dict[str, Any]) -> None:
        """添加消息并缓存其 token 数。"""
        self._messages.append(msg)
        content = msg.get("content", "") or ""
        self._token_cache.append(_count_tokens(content))

    def add_user_message(self, content: str) -> None:
        self._append({"role": "user", "content": content})

    def add_assistant_message(self, message: dict[str, Any]) -> None:
        self._append(message)

    def add_tool_message(self, tool_call_id: str, content: str) -> None:
        self._append({"role": "tool", "tool_call_id": tool_call_id, "content": content})

    def _total_tokens(self) -> int:
        return sum(self._token_cache)

    def should_compress(self) -> bool:
        return self._total_tokens() > self.max_tokens

    @async_time_function()
    async def compress(
        self,
        store: "MemoryStore",
        llm: LLMProvider,
    ) -> None:
        """压缩最早的对话为摘要并存入 MemoryStore。"""
        if len(self._messages) < 4:
            return

        compress_count = max(2, len(self._messages) // 2)

        old_msgs = self._messages[:compress_count]
        remaining_msgs = self._messages[compress_count:]
        remaining_tokens = self._token_cache[compress_count:]

        summary = await summarize_conversation(old_msgs, llm=llm)

        store.add_summary(
            summary_text=summary,
            conversation_id=self.conversation_id,
        )

        summary_msg = {"role": "system", "content": f"对话历史摘要：{summary}"}
        summary_tokens = _count_tokens(summary_msg["content"])

        self._messages = [summary_msg] + remaining_msgs
        self._token_cache = [summary_tokens] + remaining_tokens

        logger.info(f"[记忆系统] 对话已压缩，摘要长度：{len(summary)} 字符")

    def _split_prefix_and_rounds(self):
        prefix_messages = []
        prefix_tokens = []
        rounds = []
        round_tokens = []
        current_round = []
        current_tokens = []

        for i, msg in enumerate(self._messages):
            tok = self._token_cache[i]
            role = msg.get("role")
            if role == "system" and not prefix_messages and not rounds and not current_round:
                prefix_messages.append(msg)
                prefix_tokens.append(tok)
                continue
            if role == "user":
                if current_round:
                    rounds.append(current_round)
                    round_tokens.append(current_tokens)
                current_round = [msg]
                current_tokens = [tok]
                continue
            if not current_round:
                current_round = [msg]
                current_tokens = [tok]
            else:
                current_round.append(msg)
                current_tokens.append(tok)

        if current_round:
            rounds.append(current_round)
            round_tokens.append(current_tokens)

        return prefix_messages, prefix_tokens, rounds, round_tokens

    def get_messages_for_api(self) -> list[dict[str, Any]]:
        """返回适合 API 的消息列表，使用缓存的 token 数截断。"""
        prefix_messages, prefix_tokens, rounds, round_tokens = self._split_prefix_and_rounds()
        selected_rounds = rounds[-self.max_rounds:] if self.max_rounds > 0 else []
        selected_tokens = round_tokens[-self.max_rounds:] if self.max_rounds > 0 else []

        system_tokens = _count_tokens(self.system_prompt) if self.system_prompt else 0

        # 按轮次从最早开始丢弃，直到 token 数符合限制
        while selected_rounds:
            total = system_tokens + sum(prefix_tokens) + sum(sum(rt) for rt in selected_tokens)
            if total <= self.max_tokens:
                break
            if selected_rounds:
                selected_rounds.pop(0)
                selected_tokens.pop(0)
            elif prefix_messages:
                prefix_messages.pop(0)
                prefix_tokens.pop(0)
            else:
                break

        result = []
        if self.system_prompt:
            result.append({"role": "system", "content": self.system_prompt})
        result.extend(prefix_messages)
        for round_msgs in selected_rounds:
            result.extend(round_msgs)
        return result

    def clear(self) -> None:
        self._messages.clear()
        self._token_cache.clear()
