from dataclasses import dataclass, field
from typing import Any, Type
from pydantic import BaseModel, ConfigDict, ValidationError
from src.config import config
from src.tools import ToolDict
from src.compact import CompactState
from src.events.types import CompactDelta
from src.todo import TodoManager
from src.compact import CompactMgr
import json, logging, time, uuid
from uuid import UUID

logger = logging.getLogger(__name__)

max_tool_rounds = config["llm"]["max_tool_rounds"]

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
    prompt: str
    deps: AgentDeps = field(repr=False)
    tools: set[str] | None = field(default=None)
    _prompt: list[dict]  = field(init=False, default_factory=list)
    _tools_schemas: list[ToolDict] = field(init=False)
    _todo: TodoManager = field(init=False, default_factory=TodoManager, repr=False)
    _compact: CompactMgr = field(init=False, repr=False)
    _compact_state: CompactState = field(init=False, repr=False)

    def __post_init__(self):
        self.uuid = uuid.uuid4()
        self._tools_schemas = self.deps.tools_mgr.get_schemas(self.tools)
        self._prompt.append({"role": "system", "content": self.prompt})
        self._compact = CompactMgr(self.deps)

    async def normalize_messages(self, messages: list) -> list:
        """在发送至 OpenAI API 前清理消息列表。
        主要完成三项工作：
        1. 剔除 API 无法识别的内部元数据字段（以下划线 '_' 开头的键）。
        2. 确保每条 assistant 的工具调用都有对应的 tool 消息；
        若缺失，则插入一条占位 tool 消息（内容为 "(cancelled)"）。
        3. 合并连续出现的、具有相同角色的消息（system、user、assistant），
        因为 OpenAI 要求严格交替（tool 角色消息因其 tool_call_id 各不相同，
        故允许连续存在，但不进行合并）。
        """
        # ---------- 辅助函数：递归清理对象 ----------
        def clean_dict(obj):
            """移除字典及列表中以下划线 '_' 开头的键。"""
            if isinstance(obj, dict):
                return {
                    k: clean_dict(v)
                    for k, v in obj.items()
                    if not k.startswith("_")
                }
            if isinstance(obj, list):
                return [clean_dict(item) for item in obj]
            return obj

        # ---------- 步骤 1：清理元数据字段 ----------
        cleaned_messages = []
        for msg in messages:
            clean_msg = clean_dict(msg)
            # 确保 role 字段存在
            if "role" not in clean_msg:
                continue
            cleaned_messages.append(clean_msg)

        # ---------- 步骤 2：插入缺失的工具调用结果 ----------
        # 收集所有已存在的工具调用 ID（来自 tool 消息）
        existing_tool_ids = set()
        for msg in cleaned_messages:
            if msg.get("role") == "tool" and "tool_call_id" in msg:
                existing_tool_ids.add(msg["tool_call_id"])

        # 构建新列表，将占位 tool 消息插入到包含孤立 tool_calls 的
        # assistant 消息之后。
        normalized = []
        for msg in cleaned_messages:
            normalized.append(msg)
            if msg.get("role") != "assistant":
                continue

            tool_calls = msg.get("tool_calls")
            if not tool_calls or not isinstance(tool_calls, list):
                continue

            for tc in tool_calls:
                tc_id = tc.get("id")
                if tc_id and tc_id not in existing_tool_ids:
                    # 插入占位 tool 消息
                    placeholder = {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": "(cancelled)"
                    }
                    normalized.append(placeholder)
                    existing_tool_ids.add(tc_id)  # 避免重复插入占位消息

        # ---------- 步骤 3：合并连续的同角色消息 ----------
        if not normalized:
            return []

        def _to_content_list(content):
            """将字符串内容转换为文本块列表，以便合并。"""
            if isinstance(content, list):
                return content
            if isinstance(content, str):
                return [{"type": "text", "text": content}]
            # 处理其他类型（如 None）—— 视作空文本
            return [{"type": "text", "text": str(content) if content else ""}]

        merged = [normalized[0]]
        for msg in normalized[1:]:
            prev = merged[-1]
            # 仅合并可安全拼接的角色：system、user、assistant
            # tool 消息保持独立，不进行合并
            if msg["role"] == prev["role"] and msg["role"] in ("system", "user", "assistant"):
                # 合并 content 字段
                prev_content_list = _to_content_list(prev.get("content", ""))
                curr_content_list = _to_content_list(msg.get("content", ""))
                prev["content"] = prev_content_list + curr_content_list

                # 对于 assistant 消息，保留首个非空的 tool_calls 数组。
                # （OpenAI 要求每条 assistant 消息最多包含一个 tool_calls 数组；
                #  在格式规范的对话中，不会出现连续且均含 tool_calls 的 assistant 消息。）
                if msg["role"] == "assistant":
                    if not prev.get("tool_calls") and msg.get("tool_calls"):
                        prev["tool_calls"] = msg["tool_calls"]
                # 若前后两条 assistant 消息均含有 tool_calls，则保留前者，
                # 后者会被忽略。实际生产环境可酌情增加告警。
                # 合并可能存在的其他字段（如 'name'）
                for key, value in msg.items():
                    if key not in ("role", "content", "tool_calls") and key not in prev:
                        prev[key] = value
            else:
                merged.append(msg)

        # ---------- 步骤 4：确保 assistant 消息合法 ----------
        # API 要求 assistant 消息至少包含 content 或 tool_calls 之一
        for msg in merged:
            if msg.get("role") == "assistant" and not msg.get("tool_calls"):
                if not msg.get("content"):
                    msg["content"] = ""

        return merged

    async def clear_reasoning_content(self, messages):
        for message in messages:
            # 处理对象（有 reasoning_content 属性）
            if hasattr(message, 'reasoning_content'):
                message.reasoning_content = None
            # 处理字典（有 'reasoning_content' 键）
            elif isinstance(message, dict) and 'reasoning_content' in message:
                message['reasoning_content'] = None

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
        for round_idx in range(max_tool_rounds):
            messages[:] = await self._compact.micro_compact(messages)
            if self._compact.is_need_compact(messages):
                await self.deps.event_bus.emit(CompactDelta(
                    timestamp=time.time(),
                    source=self.name,
                    content="auto manual",
                ))
                messages[:] = await self._compact.compact_history(messages, self._compact_state)
            messages[:] = await self.normalize_messages(messages)
            response = await self.deps.llm.chat(prompt=self._prompt, messages=messages, tools=self._tools_schemas, output_schema=schema_cls)
            content, tool_calls = response.content, response.tool_calls

            if content:
                final_text = content

            if not tool_calls:
                if content:
                    messages.append(response.assistant_message)
                break

            messages.append(response.assistant_message)

            used_todo = False
            for tc in tool_calls.values():
                tool_name = tc["name"]
                if self.tools is not None:
                    if tool_name not in self.tools:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
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

                result_text = await self.deps.tools_mgr.execute(tool_name, args, self.deps, self)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result_text),
                })

            rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
            if self._todo.has_open_items() and rounds_without_todo >= 3:
                messages.append({"role": "user", "content": [{"type": "text", "text": "<reminder>Update your todos.</reminder>"}]})

            if manual_compact:
                await self.deps.event_bus.emit(CompactDelta(
                    timestamp=time.time(),
                    source=self.name,
                    content="llm manual",
                ))
                messages[:] = await self._compact.compact_history(messages, self._compact_state, focus=compact_focus)
        else:
            # 超过 max_tool_rounds
            response = await self.deps.llm.chat(prompt=self._prompt, messages=messages)
            final_text = response.content

        #移除思考内容
        await self.clear_reasoning_content(messages)

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
                prompt=self._prompt,
                schema_name=struct_output.schema_name,
                schema_description=struct_output.schema_desc,
            )
            if result is not None:
                return result

        return final_text
