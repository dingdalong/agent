import json, logging, time, uuid
from uuid import UUID
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Type
from pydantic import BaseModel, ConfigDict, ValidationError
from src.config import config
from src.tools import ToolDict
from src.events.types import CompactDelta
from src.mgr import FileMgr, TodoManager, CompactMgr, PromptMgr, SkillMgr

logger = logging.getLogger(__name__)

@dataclass
class StructOutputConfig:
    """Agent.run 的结构化输出配置。"""
    model_cls: Type[BaseModel]
    schema_name: str = "structured_output"
    schema_desc: str = "结构化输出"

class AgentDeps(BaseModel):
    """外部依赖
    所有字段声明为 Any 以避免循环导入
    组装时通过 isinstance 断言保证。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    llm: Any = None  # LLMProvider
    ui: Any = None  # UserInterface
    event_bus: Any = None  # EventBus
    tools_mgr: Any = None  # ToolsMgr

@dataclass
class Agent:
    """Agent 定义。

    Attributes:
        name: 唯一标识。
        description: 一句话描述
        prompt: 系统提示
    """

    uuid: UUID = field(init=False)
    name: str
    description: str
    deps: AgentDeps = field(repr=False)
    tools: set[str] | None = field(default=None)
    _tools_schemas: list[ToolDict] = field(init=False)
    _todo_mgr: TodoManager = field(init=False, default_factory=TodoManager, repr=False)
    _compact_mgr: CompactMgr = field(init=False, repr=False)
    _file_mgr: FileMgr = field(init=False, repr=False)
    _prompt_mgr: PromptMgr  = field(init=False, repr=False)
    _skill_mgr: SkillMgr = field(init=False, repr=False)

    def __post_init__(self):
        self.uuid = uuid.uuid4()
        self._tools_schemas = self.deps.tools_mgr.get_schemas(self.tools)
        self._compact_mgr = CompactMgr(self.deps)
        workspace = Path.cwd() / "workspace"
        self._file_mgr = FileMgr(workspace, self.deps)
        self._skill_mgr = SkillMgr(workspace / ".skills")

        self._prompt_mgr = PromptMgr(agent = self, model = self.deps.llm.model, workdir = workspace)

    async def run(
        self,
        input: str,
        messages: list[dict],
        struct_output: StructOutputConfig | None = None,
    ) -> str | BaseModel:
        messages.append({"role": "user", "content": input})
        schema_cls = struct_output.model_cls if struct_output else None

        final_text = ""
        rounds_without_todo = 0
        manual_compact = False
        compact_focus = None
        round_start_idx = len(messages)
        has_tool_calls = False
        compact_streak = 0
        max_compact_streak = 3
        while True:
            messages[:] = await self._compact_mgr.micro_compact(messages)
            if self._compact_mgr.is_need_compact(messages):
                compact_streak += 1
                if compact_streak > max_compact_streak:
                    logger.warning("连续 %d 次 compact 后仍需压缩，终止循环防止空转", compact_streak - 1)
                    messages.append({"role": "user", "content": "由于对话上下文过长且多次压缩仍无法继续，请你基于当前已完成的工作做一个总结：1) 已经完成了什么；2) 还有什么未完成；3) 给出后续建议。"})
                    messages[:] = self.deps.llm.normalize_messages(messages)
                    response = await self.deps.llm.chat(prompt=self._prompt_mgr.build(), messages=messages, tools=[], output_schema=schema_cls)
                    if response.content:
                        final_text = response.content
                    messages.append(response.assistant_message)
                    break
                await self.deps.event_bus.emit(CompactDelta(
                    timestamp=time.time(),
                    source=self.name,
                    content="auto manual",
                ))
                messages[:] = await self._compact_mgr.compact_history(messages)
            else:
                compact_streak = 0

            messages[:] = self.deps.llm.normalize_messages(messages)
            response = await self.deps.llm.chat(prompt=self._prompt_mgr.build(), messages=messages, tools=self._tools_schemas, output_schema=schema_cls)
            content, tool_calls = response.content, response.tool_calls

            if content:
                final_text = content

            messages.append(response.assistant_message)
            if not tool_calls:
                break

            has_tool_calls = True
            used_todo = False
            for tc in tool_calls.values():
                tool_name = tc["name"]
                tool_call_id = tc["id"]
                if self.tools is not None:
                    if tool_name not in self.tools:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": f"错误：未知工具 '{tool_name}'",
                        })
                        continue

                if tool_name == "todo_write":
                    used_todo = True
                try:
                    args = json.loads(tc["arguments"])
                    if tool_name == "compact":
                        manual_compact = True
                        compact_focus = args.get("focus")
                except json.JSONDecodeError:
                    args = {}

                result_text = await self.deps.tools_mgr.execute(
                        tool_name, args,
                        {"tool_use_id": tool_call_id, "deps": self.deps, "agent": self},
                    )

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": str(result_text),
                })

            rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
            if self._todo_mgr.has_open_items() and rounds_without_todo >= 3:
                messages.append({"role": "user", "content": [{"type": "text", "text": "<reminder>更新你的待办事项。</reminder>"}]})

            if manual_compact:
                await self.deps.event_bus.emit(CompactDelta(
                    timestamp=time.time(),
                    source=self.name,
                    content="llm manual",
                ))
                messages[:] = await self._compact_mgr.compact_history(messages, focus=compact_focus)

        if not has_tool_calls:
            self.deps.llm.clear_reasoning_content(messages[round_start_idx:])

        # 结构化输出解析
        if struct_output is not None:
            # 原生 Provider：final_text 已是受约束的 JSON，直接解析
            if self.deps.llm.supports_native_structured_output and final_text:
                try:
                    return struct_output.model_cls.model_validate_json(final_text)
                except (ValidationError, Exception) as e:
                    logger.debug(f"原生结构化输出解析失败，回退到 structured_chat: {e}")
            # 非原生 或 解析失败：Two-Pass 兜底
            result = await self.deps.llm.structured_chat(
                output_schema=struct_output.model_cls,
                messages=messages,
                prompt=self._prompt_mgr.build(),
                schema_name=struct_output.schema_name,
                schema_description=struct_output.schema_desc,
            )
            if result is not None:
                return result

        return final_text
