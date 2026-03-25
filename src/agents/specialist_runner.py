"""SpecialistRunner：运行专业 Agent 的轻量循环（非 FSM）。

专业 Agent 只接收精炼后的任务描述，独立于总控对话历史，
执行工具调用后返回 SpecialistResult（文本摘要 + 结构化数据）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from config import SPECIALIST_MAX_RESULT_LENGTH, SPECIALIST_MAX_TOOL_ROUNDS
from src.agents.registry import AgentDef
from src.core.async_api import call_model
from src.core.structured_output import build_output_schema, parse_output
from src.tools.tool_call import execute_tool_calls
from src.tools.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)


@dataclass
class SpecialistResult:
    """专业 Agent 执行结果。"""

    text: str        # 人类可读摘要（≤ max_result_length），追加到总控 tool 消息
    data: dict = field(default_factory=dict)  # 结构化数据，空 dict 表示无


def _build_task_with_context(task: str, shared_context: dict) -> str:
    """将 shared_context 中的结构化数据序列化，拼入任务描述。"""
    if not shared_context:
        return task
    context_lines = []
    for agent_name, data in shared_context.items():
        try:
            context_lines.append(f"[来自 {agent_name} 的数据]: {json.dumps(data, ensure_ascii=False)}")
        except (TypeError, ValueError):
            pass
    if not context_lines:
        return task
    return task + "\n\n参考数据：\n" + "\n".join(context_lines)


async def run_specialist(
    agent_def: AgentDef,
    task: str,
    all_tools: list,
    tool_executor: ToolExecutor,
    shared_context: dict,
    max_tool_rounds: int = SPECIALIST_MAX_TOOL_ROUNDS,
    max_result_length: int = SPECIALIST_MAX_RESULT_LENGTH,
) -> SpecialistResult:
    """运行专业 Agent，返回 SpecialistResult。

    上下文隔离：专业 Agent 只看到 [specialist_system, user_task]，
    不继承总控对话历史，通过精炼 task 描述传递必要信息。
    """
    try:
        # 1. 过滤工具（只允许该 agent 的工具集）
        filtered_tools = [
            t for t in all_tools
            if t["function"]["name"] in agent_def.tool_names
        ]

        # 2. 构建消息（注入 shared_context）
        enriched_task = _build_task_with_context(task, shared_context)
        messages: list[dict] = [
            {"role": "system", "content": agent_def.system_prompt},
            {"role": "user", "content": enriched_task},
        ]

        # 3. 工具调用循环（最多 max_tool_rounds 轮）
        final_text = ""
        for _ in range(max_tool_rounds):
            content, tool_calls, _ = await call_model(
                messages,
                tools=filtered_tools if filtered_tools else None,
                silent=True,
            )

            if not tool_calls:
                final_text = content
                break

            # 执行工具调用，追加消息
            new_msgs = await execute_tool_calls(content, tool_calls, tool_executor)
            messages.extend(new_msgs)
        else:
            # 超过 max_tool_rounds 后，取最后一次文本输出
            if not final_text:
                content, _, _ = await call_model(messages, silent=True)
                final_text = content

        # 4. 截断文本
        if len(final_text) > max_result_length:
            final_text = final_text[:max_result_length] + "…(已截断)"

        # 5. 若定义了 output_model，提取结构化数据
        structured_data: dict = {}
        if agent_def.output_model is not None:
            output_schema = build_output_schema(
                "specialist_output",
                f"将执行结果整理为 {agent_def.output_model.__name__} 结构",
                agent_def.output_model,
            )
            _, struct_tool_calls, _ = await call_model(
                messages + [{"role": "user", "content": "请将结果整理为结构化数据。"}],
                tools=[output_schema],
                silent=True,
            )
            parsed = parse_output(struct_tool_calls, "specialist_output", agent_def.output_model)
            if parsed is not None:
                structured_data = parsed.model_dump()

        return SpecialistResult(text=final_text, data=structured_data)

    except Exception as e:
        logger.error(f"专业 Agent [{agent_def.name}] 执行失败: {e}")
        return SpecialistResult(text=f"专业 Agent 执行失败: {type(e).__name__}: {e}", data={})
