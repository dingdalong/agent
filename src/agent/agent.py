import json, logging, time, uuid
from uuid import UUID
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Type
from pydantic import BaseModel, ConfigDict
from src.tools import ToolDict
from src.events.types import CompactDelta
from src.mgr import FileMgr, TodoManager, CompactMgr, PromptMgr, SkillMgr, SubAgentMgr

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
    permission_mgr: Any = None  # PermissionManager
    config_mgr: Any = None  # ConfigManager

@dataclass
class Agent:
    """Agent 定义。

    Attributes:
        uuid: 唯一类型标识。
        agent_type: agent类型
        description: 一句话描述
    """

    uuid: UUID = field(init=False)
    agent_type: str
    description: str
    deps: AgentDeps = field(repr=False)
    role_prompt: str | None = field(default=None)
    tools: set[str] | None = field(default=None)
    is_subagent: bool = field(default=False)
    _tools_schemas: list[ToolDict] = field(init=False)
    _todo_mgr: TodoManager = field(init=False, default_factory=TodoManager, repr=False)
    _compact_mgr: CompactMgr = field(init=False, repr=False)
    _file_mgr: FileMgr = field(init=False, repr=False)
    _skill_mgr: SkillMgr = field(init=False, repr=False)
    _subagent_mgr: SubAgentMgr = field(init=False, repr=False)

    _prompt_mgr: PromptMgr  = field(init=False, repr=False)

    def __post_init__(self):
        self.uuid = uuid.uuid4()
        self._tools_schemas = self.deps.tools_mgr.get_schemas(self.tools)
        self._compact_mgr = CompactMgr(self.deps)
        workspace = Path.cwd() / "workspace"
        self._file_mgr = FileMgr(workspace, self.deps)
        self._skill_mgr = SkillMgr(workspace)
        self._subagent_mgr = SubAgentMgr(workspace, self.deps)

        self._prompt_mgr = PromptMgr(agent = self, model = self.deps.llm.model, workdir = workspace, role_prompt = self.role_prompt)

    async def run(
        self,
        input: str,
        messages: list[dict],
    ) -> str:
        messages.append({"role": "user", "content": input})

        final_text = ""
        rounds_without_todo = 0
        manual_compact = False
        compact_focus = None
        round_start_idx = len(messages)
        has_tool_calls = False
        compact_streak = 0
        max_compact_streak = 3
        while True:
            prompt = self._prompt_mgr.build()
            messages[:] = await self._compact_mgr.micro_compact(messages)
            if self._compact_mgr.is_need_compact(messages, prompt, self._tools_schemas):
                compact_streak += 1
                if compact_streak > max_compact_streak:
                    logger.warning("连续 %d 次 compact 后仍需压缩，终止循环防止空转", compact_streak - 1)
                    messages.append({"role": "user", "content": "由于对话上下文过长且多次压缩仍无法继续，请你基于当前已完成的工作做一个总结：1) 已经完成了什么；2) 还有什么未完成；3) 给出后续建议。"})
                    messages[:] = self.deps.llm.normalize_messages(messages)
                    response = await self.deps.llm.chat(
                        prompt=prompt,
                        messages=messages,
                        tools=[],
                        caller_agent_type=self.agent_type,
                        caller_uuid=str(self.uuid),
                    )
                    if response.content:
                        final_text = response.content
                    messages.append(response.assistant_message)
                    break
                await self.deps.event_bus.emit(CompactDelta(
                    timestamp=time.time(),
                    source=self.agent_type,
                    content="auto manual",
                ))
                messages[:] = await self._compact_mgr.compact_history(messages)
            else:
                compact_streak = 0

            messages[:] = self.deps.llm.normalize_messages(messages)
            response = await self.deps.llm.chat(
                prompt=prompt,
                messages=messages,
                tools=self._tools_schemas,
                caller_agent_type=self.agent_type,
                caller_uuid=str(self.uuid),
            )
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
                        {"current_tool_call_id": tool_call_id, "deps": self.deps, "agent": self},
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
                    source=self.agent_type,
                    content="llm manual",
                ))
                messages[:] = await self._compact_mgr.compact_history(messages, focus=compact_focus)

        if not has_tool_calls:
            self.deps.llm.clear_reasoning_content(messages[round_start_idx:])

        return final_text
