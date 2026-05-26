from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.llm.base import LLMProvider, LLMResponse

if TYPE_CHECKING:
    from src.agent import AgentDeps

@dataclass
class RecoveryMgr:
    deps: AgentDeps
    llm: LLMProvider = None
    max_length_recovery_attempts: int = 3
    continuation_message: str = (
        "Output limit hit. Continue directly from where you stopped -- "
        "no recap, no repetition. Pick up mid-sentence if needed."
    )
    context_limit_summary_message: str = (
        "由于对话上下文过长且多次压缩仍无法继续，请你基于当前已完成的工作做一个总结："
        "1) 已经完成了什么；2) 还有什么未完成；3) 给出后续建议。"
    )

    def _terminal_response(self, content: str) -> LLMResponse:
        return LLMResponse(
            content=content,
            finish_reason="recovery_exhausted",
            assistant_message={"role": "assistant", "content": content},
        )

    def context_limit_exhausted_response(self) -> LLMResponse:
        return self._terminal_response(
            "错误：上下文过长，已多次压缩仍无法继续。请缩小任务范围或重新开始较短的会话。"
        )

    async def summarize_context_limit_exhaustion(
        self,
        *,
        prompt: list[dict] | None,
        messages: list[dict],
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> LLMResponse:
        messages.append({"role": "user", "content": self.context_limit_summary_message})
        messages[:] = self.llm.normalize_messages(messages)
        try:
            return await self.llm.chat(
                prompt=prompt,
                messages=messages,
                tools=[],
                caller_agent_type=caller_agent_type,
                caller_uuid=caller_uuid,
            )
        except Exception as exc:
            if self.llm.is_context_too_long_error(exc):
                return self.context_limit_exhausted_response()
            raise

    async def chat_with_recovery(
        self,
        *,
        messages: list[dict],
        prompt: list[dict] | None,
        tools: list[dict] | None,
        caller_agent_type: str | None = None,
        caller_uuid: str | None = None,
    ) -> LLMResponse:
        length_recoveries = 0

        while True:
            try:
                response = await self.llm.chat(
                    prompt=prompt,
                    messages=messages,
                    tools=tools,
                    caller_agent_type=caller_agent_type,
                    caller_uuid=caller_uuid,
                )
            except Exception as exc:
                if self.llm.is_context_too_long_error(exc):
                    return self.context_limit_exhausted_response()
                raise

            if response.finish_reason != "length":
                return response

            messages.append(
                response.assistant_message
                or {"role": "assistant", "content": response.content or None}
            )

            if length_recoveries >= self.max_length_recovery_attempts:
                return self._terminal_response(
                    "错误：模型输出连续被截断，已达到自动续写恢复上限。请缩小输出范围后重试。"
                )

            length_recoveries += 1
            messages.append({"role": "user", "content": self.continuation_message})
            messages[:] = self.llm.normalize_messages(messages)

    async def execute_tool_with_recovery(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> str:
        try:
            return str(await self.deps.tools_mgr.execute(tool_name, arguments, context))
        except Exception as exc:
            return f"错误：工具 '{tool_name}' 执行失败: {type(exc).__name__}: {exc}"
