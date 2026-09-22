"""脚本化多轮会话的落盘去重回归测试。

真实会话日志曾出现同一份数据被保存多份：每条记录在每轮被重复提交、同一段正文
各存规范化前后两份、工具展示正文与结果预览各占一段。本测试用真实 provider 归一
化、真实 ToolsMgr 展示构造与真实 SessionMgr 落盘跑一遍脚本化工具会话，再对日志
做内容审计，确保每份数据在日志里只出现一次。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import uuid

from src.agent.agent import Agent
from src.agent import SessionContext
from src.agent.spec import AgentSpec
from src.agent.states import AgentState, RunContext
from src.common.session_log import fold_log, iter_batch_lines
from src.common.session_state import SessionState
from src.llm.deepseek import DeepSeekProvider
from src.mgr.session_mgr import SessionMgr
from src.mgr.tools_mgr import ToolsMgr
from src.mode import RunMode


USER_INPUT = "  帮我看一下 session 的落盘逻辑  "
USER_TEXT = USER_INPUT.strip()
COMMANDS = (
    'rg -n "def _filter_payload" src/mgr/session_mgr.py',
    "sed -n 1,80p src/common/session_log.py",
    "git diff --stat",
)
# 结果正文以无首尾空白的形式入库：工具消息与展示投影都指向这一份。
OUTPUTS = tuple(
    (f"第 {index} 段工具输出\n" * 200).strip() for index in range(1, len(COMMANDS) + 1)
)


class _RecordingBus:
    """事件总线：把事件转交给会话状态（等价 OutputRouter 的记录分支）。"""

    def __init__(self, state: SessionState) -> None:
        """初始化事件总线。

        Args:
            state: 接收事件投影的会话状态。

        Returns:
            None。
        """
        self.state = state
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        """记录事件并写入会话状态。

        Args:
            event: 待处理事件。

        Returns:
            None。
        """
        self.events.append(event)
        self.state.record_event(event)


def _log_lines(path) -> list[dict]:
    """读取日志文件并解析为批记录列表。"""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _stored_strings(value: object) -> list[str]:
    """深度取出折叠载荷里的所有字符串，用于统计同一份内容的副本数。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _stored_strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _stored_strings(item)]
    return []


def _scripted_agent(tmp_path, provider: DeepSeekProvider) -> tuple[Agent, SessionMgr]:
    """构造只接线会话落盘所需字段的最小 agent。

    Args:
        tmp_path: 会话目录所在临时路径。
        provider: 提供写入状态前正文规范化规则的真实 provider。

    Returns:
        ``(agent, session_mgr)``。
    """
    manager = SessionMgr(tmp_path, tmp_path)
    state = SessionState()
    agent = object.__new__(Agent)
    agent.uuid = uuid.uuid4()
    agent.spec = AgentSpec(agent_type="main", description="")
    agent.history = []
    agent.mode = RunMode.EXECUTE
    agent.assembly = SimpleNamespace(llm=provider)
    agent.deps = SimpleNamespace(
        event_bus=_RecordingBus(state),
        data_guard=None,
        session_mgr=manager,
        session=SessionContext(id="session", state=state),
    )
    return agent, manager


async def _run_scripted_session(agent: Agent, provider: DeepSeekProvider, log_path) -> int:
    """跑一遍脚本化工具会话，返回 provider 归一化回写新增的批数。

    每轮依次执行：assistant 载体消息 → 工具开始/完成事件 → 工具结果消息 →
    provider 归一化回写。归一化回写不改变已落盘字节，因此不应新增批。

    Args:
        agent: 已接线会话的最小 agent。
        provider: 提供消息归一化的真实 provider。
        log_path: 会话日志路径，用于统计批数。

    Returns:
        归一化回写步骤新增的批数（期望 0）。
    """
    tools_mgr = ToolsMgr()
    tool = tools_mgr.get("exec_command")

    agent._append_message(agent.history, {"role": "user", "content": USER_INPUT}, kind="user")
    await agent._commit_session(USER_INPUT)

    extra_batches = 0
    for index, (command, output) in enumerate(zip(COMMANDS, OUTPUTS), start=1):
        call_id = f"call_{index}"
        agent._append_message(
            agent.history,
            {
                "role": "assistant",
                "_response_output": [{
                    "type": "function_call",
                    "call_id": call_id,
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": command}, ensure_ascii=False),
                }],
            },
            kind="assistant",
            correlation_id=call_id,
        )
        await agent._commit_session()

        await tools_mgr._emit_tool_started(
            agent.deps, agent, tool, command, call_id, arguments={"cmd": command},
        )
        await tools_mgr._emit_tool_completed(
            agent.deps, agent, tool, call_id, "success", 1.5, output,
        )
        await agent._commit_session()

        agent._append_message(
            agent.history,
            {"role": "tool", "tool_call_id": call_id, "content": f"\n{output}\n"},
            kind="tool",
            correlation_id=call_id,
        )
        await agent._commit_session()

        before = len(_log_lines(log_path))
        agent._replace_messages(
            agent.history,
            provider.normalize_messages(agent.history),
            preserve_positions=True,
        )
        await agent._commit_session()
        extra_batches += len(_log_lines(log_path)) - before
    return extra_batches


def test_scripted_tool_session_stores_each_payload_once(tmp_path) -> None:
    """脚本化多轮工具会话：每份数据只落盘一次，且折叠结果与内存状态一致。"""
    provider = DeepSeekProvider(
        api_key="test",
        base_url="https://api.example.test",
        model="deepseek-test",
        event_bus=None,
        max_attempts=1,
    )
    agent, manager = _scripted_agent(tmp_path, provider)
    log_path = tmp_path / "sessions" / "session.jsonl"

    assert asyncio.run(_run_scripted_session(agent, provider, log_path)) == 0

    state = agent.deps.session.state
    text = log_path.read_text(encoding="utf-8")
    lines = _log_lines(log_path)
    payload, _contents, _seq = fold_log(iter_batch_lines(text))

    # 同一段正文只落一份，且落盘形式就是 provider 之后会写入的形式。
    stored = _stored_strings(payload)
    assert [item for item in stored if USER_TEXT in item] == [USER_TEXT]
    assert [item for item in stored if OUTPUTS[0][:12] in item] == [OUTPUTS[0]]
    assert [item for item in stored if OUTPUTS[1][:12] in item] == [OUTPUTS[1]]
    assert [item for item in stored if OUTPUTS[2][:12] in item] == [OUTPUTS[2]]
    assert text.count(USER_TEXT) == 1
    assert USER_INPUT not in text

    # 记录不重复提交：同一条记录不会在两批里写入逐字节相同的版本。
    submitted: dict[str, str] = {}
    duplicates = 0
    for batch in lines:
        for record in batch.get("records") or []:
            key = json.dumps(record, ensure_ascii=False, sort_keys=True)
            if submitted.get(record.get("id")) == key:
                duplicates += 1
            submitted[record.get("id")] = key
    assert duplicates == 0

    # 上下文只提交增量：记录 ID 不会被整段重发。
    appended = [
        record_id
        for batch in lines
        for record_id in ((batch.get("context") or {}).get("append") or [])
    ]
    assert len(appended) == len(set(appended))
    assert all("context_ids" not in batch for batch in lines)

    # 展示正文与结果预览都是投影：只留配方，渲染时按引用重建。
    views = [record.get("view") or {} for record in payload["records"]]
    started = [view["data"]["started"] for view in views if "started" in (view.get("data") or {})]
    completed = [
        view["data"]["completed"] for view in views if "completed" in (view.get("data") or {})
    ]
    assert [item["detail"] for item in started] == list(COMMANDS)
    assert all(item["display"]["content"] == "" for item in started)
    assert all(item["display"]["render"] == {"kind": "params"} for item in started)
    assert all(item["result_preview"] == "" for item in completed)
    assert all(item["display"]["content"] == "" for item in completed)
    assert all(item["display"]["render"]["kind"] == "result" for item in completed)

    # 折叠结果与内存状态一致，回放能按配方重建参数与结果正文。
    restored = SessionState.from_dict(payload)
    assert restored is not None
    assert restored.context_messages() == state.context_messages()
    assert restored.to_dict() == state.to_dict()
    assert restored.tool_call_arguments("call_1") == {"cmd": COMMANDS[0]}
    assert restored.tool_result_text("call_1") == OUTPUTS[0]

    # 恢复会话只提交新变更：历史记录不因重启重写一遍。
    reloaded = manager.load_state("session")
    assert reloaded is not None
    reloaded.append_context({"role": "user", "content": "继续"})
    manager.append_state("session", reloaded)
    resumed = _log_lines(log_path)
    assert len(resumed) == len(lines) + 1
    assert [record["id"] for record in resumed[-1].get("records") or []] == [
        reloaded.context_ids[-1]
    ]
    assert resumed[-1]["context"]["append"] == [reloaded.context_ids[-1]]


def test_no_tool_round_prunes_thinking_before_single_commit(tmp_path) -> None:
    """无工具轮次：assistant 记录只以裁剪思考后的最终形态落盘一次。

    回归场景：过去该记录先在 `_on_process_response` 带思考落一次盘，轮末裁剪
    思考后又标脏写第二份，日志里留下同一记录的两个版本，磁盘版本也与内存及
    下一次提交的上下文不一致。
    """
    provider = DeepSeekProvider(
        api_key="test",
        base_url="https://api.example.test",
        model="deepseek-test",
        event_bus=None,
        max_attempts=1,
    )
    agent, manager = _scripted_agent(tmp_path, provider)
    log_path = tmp_path / "sessions" / "session.jsonl"

    agent._append_message(agent.history, {"role": "user", "content": USER_INPUT}, kind="user")
    asyncio.run(agent._commit_session(USER_INPUT))
    batches_before = len(_log_lines(log_path))

    thinking = "推演同一记录的两个落盘版本。" * 40
    answer = "结论：每个逻辑输出只存一份。"
    assistant_message = {
        "role": "assistant",
        "content": answer,
        "reasoning_content": thinking,
        "_response_output": [
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": thinking}]},
            {"type": "message", "content": [{"type": "output_text", "text": answer}]},
        ],
    }
    ctx = RunContext(
        messages=agent.history,
        round_start_idx=0,
        response=SimpleNamespace(
            content=answer,
            finish_reason="stop",
            assistant_message=assistant_message,
            tool_calls={},
            call_id="call_no_tool",
        ),
    )

    assert asyncio.run(agent._on_process_response(ctx)) is AgentState.CHECK_STOP
    state = agent.deps.session.state
    assert len(_log_lines(log_path)) == batches_before + 1

    # 轮末兜底再次裁剪并提交：内容未变，零写入。
    agent._finish_round_reasoning(ctx)
    asyncio.run(agent._commit_session(USER_INPUT))
    lines = _log_lines(log_path)
    assert len(lines) == batches_before + 1

    assistant_id = next(
        record.id
        for record in state.records
        if record.model_message is assistant_message
    )
    submissions = [
        record
        for batch in lines
        for record in batch.get("records") or []
        if record.get("id") == assistant_id
    ]
    assert len(submissions) == 1
    stored = submissions[0]["model_message"]
    assert "reasoning_content" not in stored
    assert [item["type"] for item in stored["_response_output"]] == ["message"]
    assert thinking not in log_path.read_text(encoding="utf-8")

    # 折叠恢复后的上下文与内存历史逐字段一致，且不含思考载体。
    restored = manager.load_state("session")
    assert restored is not None
    assert restored.context_messages() == agent.history
    assert agent.history[-1] is assistant_message
