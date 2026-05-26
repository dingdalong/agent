"""DeepSeek LLM Provider。"""

import json
import logging
import re
import time
from functools import cached_property
from openai import AsyncOpenAI
from src.llm.base import LLMProvider, LLMResponse
from src.tools import ToolDict
import transformers

logger = logging.getLogger(__name__)

class DeepSeekProvider(LLMProvider):
    """基于 OpenAI SDK 的 LLM Provider。"""

    def __post_init__(self):
        super().__post_init__()
        self.supports_native_structured_output = False
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
        )

    def clear_reasoning_content(self, messages):
        """清理思考内容"""
        for message in messages:
            # 处理对象（有 reasoning_content 属性）
            if hasattr(message, 'reasoning_content'):
                message.reasoning_content = None
            # 处理字典（有 'reasoning_content' 键）
            elif isinstance(message, dict) and 'reasoning_content' in message:
                message['reasoning_content'] = None

    def estimate_tokens(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
    ) -> int:
        all_messages = (prompt or []) + messages
        messages_for_estimate = [{
            "messages": all_messages,
            "tools": tools,
        }] if tools else all_messages
        return len(self._tokenizer.encode(str(messages_for_estimate)))

    @cached_property
    def _tokenizer(self):
        return transformers.AutoTokenizer.from_pretrained(
            "src/llm/tokenizer/deepseek", trust_remote_code=True
        )

    def _extract_token_usage(
        self,
        usage: object | None,
    ) -> dict[str, int | None] | None:
        if usage is None:
            return None
        return {
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "cache_read_input_tokens": getattr(usage, "prompt_cache_hit_tokens", None),
            "cache_creation_input_tokens": getattr(usage, "prompt_cache_miss_tokens", None),
        }

    # ---- 文本工具调用的正则 ----
    _DSML_BLOCK_RE = re.compile(
        r'<｜DSML｜tool_calls>(.*?)</｜DSML｜tool_calls>', re.DOTALL
    )
    _DSML_INVOKE_RE = re.compile(
        r'<｜DSML｜invoke\s+name="([^"]+)">(.*?)</｜DSML｜invoke>', re.DOTALL
    )
    _DSML_PARAM_RE = re.compile(
        r'<｜DSML｜parameter\s+name="([^"]+)"[^>]*>(.*?)</｜DSML｜parameter>',
        re.DOTALL,
    )
    _V3_BLOCK_RE = re.compile(
        r'<｜tool call begin｜>(.*?)<｜tool call end｜>', re.DOTALL
    )

    def _try_parse_text_tool_calls(
        self, content: str
    ) -> tuple[dict[int, dict[str, str]], str]:
        # 格式 1: DSML 标签格式
        tool_calls, cleaned = self._parse_dsml_tool_calls(content)
        if tool_calls:
            return tool_calls, cleaned

        # 格式 2: V3 分隔符格式
        tool_calls, cleaned = self._parse_v3_tool_calls(content)
        if tool_calls:
            return tool_calls, cleaned

        return {}, content

    def _parse_dsml_tool_calls(
        self, content: str
    ) -> tuple[dict[int, dict[str, str]], str]:
        blocks = self._DSML_BLOCK_RE.findall(content)
        if not blocks:
            return {}, content

        tool_calls: dict[int, dict[str, str]] = {}
        idx = 0
        for block in blocks:
            for tool_name, invoke_body in self._DSML_INVOKE_RE.findall(block):
                params = {}
                for pname, pvalue in self._DSML_PARAM_RE.findall(invoke_body):
                    params[pname] = pvalue
                tool_calls[idx] = {
                    "id": f"text_call_{idx}",
                    "name": tool_name,
                    "arguments": json.dumps(params, ensure_ascii=False),
                }
                idx += 1

        cleaned = self._DSML_BLOCK_RE.sub("", content).strip()
        return tool_calls, cleaned

    def _parse_v3_tool_calls(
        self, content: str
    ) -> tuple[dict[int, dict[str, str]], str]:
        matches = self._V3_BLOCK_RE.findall(content)
        if not matches:
            return {}, content

        tool_calls: dict[int, dict[str, str]] = {}
        for idx, match in enumerate(matches):
            try:
                parsed = json.loads(match.strip())
                if isinstance(parsed, dict) and "name" in parsed:
                    tool_calls[idx] = {
                        "id": f"text_call_{idx}",
                        "name": parsed["name"],
                        "arguments": json.dumps(
                            parsed.get("arguments", {}), ensure_ascii=False
                        ),
                    }
            except json.JSONDecodeError:
                continue

        cleaned = self._V3_BLOCK_RE.sub("", content).strip()
        return tool_calls, cleaned

    def _normalize_content(self, content) -> str | list[dict]:
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(part)
                elif isinstance(part, str):
                    parts.append({"type": "text", "text": part})
            return parts if parts else ""
        if isinstance(content, str):
            return content.strip()
        if content is not None:
            return str(content)
        return ""

    def _normalize_tool_calls(self, tool_calls, msg_index: int) -> list[dict] | None:
        if tool_calls is None:
            return None
        if isinstance(tool_calls, list):
            valid_calls = []
            for call in tool_calls:
                if isinstance(call, dict) and "function" in call:
                    valid_calls.append({
                        "id": call.get("id", ""),
                        "type": call.get("type", "function"),
                        "function": call["function"],
                    })
            return valid_calls or None
        if isinstance(tool_calls, str):
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
                        continue
                return valid_calls or None
        return None

    def _normalize_assistant_extra(self, msg: dict, norm_msg: dict, role: str) -> None:
        if role != "assistant":
            return
        if msg.get("prefix") is True:
            norm_msg["prefix"] = True
        if msg.get("reasoning_content"):
            norm_msg["reasoning_content"] = msg["reasoning_content"]

    async def _do_chat(
        self,
        messages: list[dict],
        prompt: list[dict] | None = None,
        tools: list[ToolDict] | None = None,
        temperature: float = 0.6,
        tool_choice: str | dict | None = None,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> LLMResponse:
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=prompt + messages if prompt is not None else messages,
            tools=tools,
            stream=True,
            stream_options={"include_usage": True},
            temperature=temperature,
            tool_choice=tool_choice or ("auto" if tools else None),
            reasoning_effort=self.reasoning_effort,
            extra_body={"thinking": {"type": "enabled"}}
        )
        return await self._parse_stream(
            response,
            caller_agent_type,
            caller_uuid,
        )

    async def _parse_stream(
        self,
        stream,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> LLMResponse:
        """解析流式响应。"""
        tool_calls: dict[int, dict[str, str]] = {}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason = None
        usage = None

        async for chunk in stream:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage

            if not getattr(chunk, "choices", None):
                continue

            delta = chunk.choices[0].delta

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning is not None:
                reasoning_parts.append(reasoning)
                await self.emit_thinking_delta(reasoning, caller_agent_type, caller_uuid)

            if delta.content:
                if not (delta.tool_calls and delta.content.isspace()):
                    content_parts.append(delta.content)
                    await self.emit_response_delta(delta.content, caller_agent_type, caller_uuid)

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

        # 兜底：模型把 tool call 输出为文本而非结构化通道
        if not tool_calls and content:
            fallback_calls, content = self._try_parse_text_tool_calls(content)
            if fallback_calls:
                logger.info(
                    "从文本中解析出 %d 个工具调用（模型未走结构化通道）",
                    len(fallback_calls),
                )
                tool_calls = fallback_calls

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
            token_usage=self._extract_token_usage(usage),
        )
