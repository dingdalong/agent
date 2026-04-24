"""DeepSeek LLM Provider。"""

import asyncio
import logging
import time
from openai import AsyncOpenAI, APIConnectionError, RateLimitError, APIError
from src.events.types import ResponseDelta, ThinkingDelta
from src.llm.base import LLMProvider, LLMResponse
from src.tools import ToolDict
import transformers

logger = logging.getLogger(__name__)

def _normalize_content(content: object) -> str | list[dict]:
    """
    规范化消息的 content 字段。

    DeepSeek V4 支持：
    - 纯文本字符串: "Hello"
    - 多模态数组: [{"type": "text", "text": "..."}, {"type": "image_url", ...}]
    """
    # 已经是数组格式（多模态）
    if isinstance(content, list):
        normalized_parts = []
        for part in content:
            if isinstance(part, dict):
                normalized_parts.append(part)
            elif isinstance(part, str):
                normalized_parts.append({"type": "text", "text": part})
        return normalized_parts if normalized_parts else ""

    # 字符串内容
    if isinstance(content, str):
        return content.strip()

    # 其他类型 → 字符串化
    if content is not None:
        return str(content)

    return ""


def _normalize_tool_calls(tool_calls: object, msg_index: int) -> list[dict] | None:
    """
    规范化 tool_calls 字段。

    支持两种格式：
    1. OpenAI 标准格式（JSON）: [{"id": "...", "type": "function", "function": {...}}]
    2. DeepSeek V3 特殊分隔符格式（字符串形式）
    """
    if tool_calls is None:
        return None

    # 列表格式（标准 OpenAI 格式）
    if isinstance(tool_calls, list):
        valid_calls = []
        for call in tool_calls:
            if isinstance(call, dict) and "function" in call:
                valid_calls.append({
                    "id": call.get("id", ""),
                    "type": call.get("type", "function"),
                    "function": call["function"],
                })
        return valid_calls if valid_calls else None

    # 字符串格式（DeepSeek V3 特殊分隔符）
    if isinstance(tool_calls, str):
        # 最佳实践：转换为 OpenAI 标准格式
        # DeepSeek V3 使用 <｜tool calls begin｜> ... <｜tool calls end｜> 包裹
        import re
        import json

        tool_pattern = r"<｜tool call begin｜>(.*?)<｜tool call end｜>"
        matches = re.findall(tool_pattern, tool_calls, re.DOTALL)

        if matches:
            valid_calls = []
            for i, match in enumerate(matches):
                try:
                    parsed = json.loads(match.strip())
                    if isinstance(parsed, dict) and "name" in parsed:
                        valid_calls.append({
                            "id": f"call_{msg_index}_{i}",
                            "type": "function",
                            "function": {
                                "name": parsed["name"],
                                "arguments": json.dumps(
                                    parsed.get("arguments", {})
                                ),
                            },
                        })
                except json.JSONDecodeError:
                    # 解析失败，跳过
                    continue
            return valid_calls if valid_calls else None

    return None



class DeepSeekProvider(LLMProvider):
    """基于 OpenAI SDK 的 LLM Provider。"""

    def __post_init__(self):
        super().__post_init__()
        self.supports_native_structured_output = False
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

    def estimate_tokens(self, message: list[dict]) -> int:
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            "src/tokenizer/deepseek", trust_remote_code=True
            )
        return len(tokenizer.encode(str(message)))

    def normalize_messages(
        self,
        messages: list[dict],
        allow_developer_role: bool = False,
        allow_tool_calls: bool = True,
        strict: bool = False,
    ):
        """
        将用户传入的 messages 规范化为 DeepSeek API 兼容的格式。

        其余参数与返回值请参见原文档。
        """

        VALID_ROLES = {"system", "user", "assistant", "tool"}
        if allow_developer_role:
            VALID_ROLES.add("developer")

        # 1. 输入格式统一化
        if isinstance(messages, dict):
            raw_messages = [messages]
        elif isinstance(messages, list):
            raw_messages = list(messages)
        else:
            raise TypeError(
                f"messages 必须是 dict 或 list[dict]，当前类型: {type(messages).__name__}"
            )

        # 2. 逐条规范化
        normalized: list[dict] = []

        for idx, msg in enumerate(raw_messages):
            if not isinstance(msg, dict):
                if strict:
                    raise TypeError(f"messages[{idx}] 必须是 dict，当前类型: {type(msg).__name__}")
                continue

            # 2.2 Role 校验与降级
            role = msg.get("role", "").strip().lower()
            if not role:
                role = "user" if not strict else (_ for _ in ()).throw(
                    ValueError(f"messages[{idx}] 缺少必填字段 'role'")
                )
            if role not in VALID_ROLES:
                if strict:
                    raise ValueError(
                        f"messages[{idx}] role='{role}' 不被支持。"
                        f"支持的 role: {sorted(VALID_ROLES)}"
                    )
                role = "user"

            # 2.3 Content 规范化
            content = _normalize_content(msg.get("content", ""))

            has_tool_calls = bool(msg.get("tool_calls"))

            # 跳过空内容且无 tool_calls 的消息
            if not content and not has_tool_calls:
                continue

            # 2.4 构建基础消息体
            norm_msg: dict[str, object] = {"role": role, "content": content}

            # 2.5 Tool Calls 处理
            if role == "assistant" and has_tool_calls and allow_tool_calls:
                tool_calls = _normalize_tool_calls(msg.get("tool_calls"), idx)
                if tool_calls:
                    norm_msg["tool_calls"] = tool_calls
            # 2.6 Tool 角色处理
            if role == "tool":
                tool_call_id = msg.get("tool_call_id", "")
                if not tool_call_id and strict:
                    raise ValueError(f"messages[{idx}] role='tool' 但缺少 tool_call_id")
                if tool_call_id:
                    norm_msg["tool_call_id"] = tool_call_id
                if not allow_tool_calls:
                    norm_msg["role"] = "user"
                    norm_msg.pop("tool_call_id", None)

            # 2.7 Prefix 字段保留
            if role == "assistant" and msg.get("prefix") is True:
                norm_msg["prefix"] = True

            # 2.8 Name 字段透传
            if "name" in msg and isinstance(msg["name"], str):
                norm_msg["name"] = msg["name"]

            # 2.9 推理内容保留规则：始终保留 reasoning_content
            # 工具调用轮次中所有 assistant 的 reasoning_content 必须回传，
            # 非工具调用轮次传入也只会被忽略，不会报错
            if role == "assistant" and msg.get("reasoning_content"):
                norm_msg["reasoning_content"] = msg["reasoning_content"]

            normalized.append(norm_msg)

        return normalized

    async def chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 0,
        tool_choice: str | dict | None = None,
        output_schema=None,
    ) -> LLMResponse:
        """流式调用 LLM，返回完整响应。"""
        async with self._semaphore:
            for attempt in range(self.max_retries):
                try:
                    response = await self._client.chat.completions.create(
                        model=self.model,
                        messages=prompt + messages if prompt is not None else messages,
                        tools=tools,
                        stream=True,
                        temperature=temperature,
                        tool_choice=tool_choice or ("auto" if tools else None),
                        reasoning_effort="max",
                        extra_body={"thinking": {"type": "enabled"}}
                    )
                    return await self._parse_stream(response)

                except (APIConnectionError, RateLimitError, asyncio.TimeoutError) as e:
                    if attempt == self.max_retries - 1:
                        raise
                    wait_time = 2 ** attempt
                    logger.warning(f"API错误 ({type(e).__name__})，{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)

                except APIError:
                    raise
            raise RuntimeError("LLM chat: 所有重试均失败")

    async def _parse_stream(
        self, stream
    ) -> LLMResponse:
        """解析流式响应。"""
        tool_calls: dict[int, dict[str, str]] = {}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason = None

        async for chunk in stream:
            delta = chunk.choices[0].delta

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning is not None:
                reasoning_parts.append(reasoning)
                await self.event_bus.emit(ThinkingDelta(
                    timestamp=time.time(),
                    source=self.model,
                    content=reasoning,
                ))

            if delta.content:
                if not (delta.tool_calls and delta.content.isspace()):
                    content_parts.append(delta.content)
                    await self.event_bus.emit(ResponseDelta(
                        timestamp=time.time(),
                        source=self.model,
                        content=delta.content,
                    ))

            if delta.tool_calls:
                for tool_chunk in delta.tool_calls:
                    idx = tool_chunk.index
                    if idx not in tool_calls:
                        tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                    if tool_chunk.id:
                        tool_calls[idx]["id"] = tool_chunk.id
                    if tool_chunk.function.name:
                        tool_calls[idx]["name"] += tool_chunk.function.name
                    if tool_chunk.function.arguments:
                        tool_calls[idx]["arguments"] += tool_chunk.function.arguments

            if chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason

        content = "".join(content_parts) or ""
        reasoning_content = "".join(reasoning_parts) or ""

        # 构造完整的 assistant 消息，包含 Provider 特有字段
        assistant_message: dict = {
            "role": "assistant",
            "content": content,
        }
        if reasoning_content:
            assistant_message["reasoning_content"] = reasoning_content
        if tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in tool_calls.values()
            ]

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            assistant_message=assistant_message,
        )
