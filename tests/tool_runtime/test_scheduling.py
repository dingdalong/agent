import asyncio
import json
from types import SimpleNamespace

from pydantic import BaseModel

from src.agent import Agent
from src.agent.states import RunContext
from src.llm.base import LLMResponse
from src.mgr.workspace_access import WorkspaceAccess
from src.mgr.reminder_mgr import ReminderMgr
from src.mode import RunMode
from src.tools.decorator import ToolEntry
from src.tools.policy import AccessKind, DataFlow, ToolOrigin, ToolPolicy


def test_workspace_readers_parallel_writer_waits():
    async def scenario():
        access = WorkspaceAccess()
        release = asyncio.Event()
        readers = 0
        ready = asyncio.Event()
        async def read():
            nonlocal readers
            async with access.read():
                readers += 1
                if readers == 2:
                    ready.set()
                await release.wait()
        tasks = [asyncio.create_task(read()) for _ in range(2)]
        await asyncio.wait_for(ready.wait(), 1)
        writer = asyncio.create_task(access.acquire())
        await asyncio.sleep(0)
        assert not writer.done()
        release.set()
        await asyncio.gather(*tasks, writer)
        assert access.writer
        await access.release()
    asyncio.run(scenario())


def test_cancel_keeps_completed_tool_and_invalid_json_does_not_execute(runtime):
    deps, fake = runtime
    agent = object.__new__(Agent)
    agent.deps = deps
    agent.uuid = fake.uuid
    agent.agent_type = 'main'
    agent.mode = RunMode.EXECUTE
    agent.llm = fake.llm
    agent.tools = None
    agent.history = []
    agent._reminder_mgr = ReminderMgr()
    ready = asyncio.Event()
    class Args(BaseModel):
        value: str = 'default'
    calls = []
    async def fast(value):
        calls.append(value)
        return 'completed-evidence'
    async def slow(value):
        ready.set()
        await asyncio.Event().wait()
    for name, func in [('fast', fast), ('slow', slow)]:
        deps.tools_mgr.register(ToolEntry(name, func, Args, '', Args.model_json_schema(), policy=ToolPolicy(AccessKind.INTERNAL,DataFlow.LOCAL,plan_safe=True), origin=ToolOrigin('builtin'), parallel=True))
    completed_reads = asyncio.Event()
    finished = 0
    execute = deps.tools_mgr.execute
    async def observed_execute(name, arguments, **kwargs):
        nonlocal finished
        result = await execute(name, arguments, **kwargs)
        finished += 1
        if finished == 2:
            completed_reads.set()
        return result
    deps.tools_mgr.execute = observed_execute
    tool_calls = {i: {'id': str(i), 'name':name, 'arguments':args} for i,(name,args) in enumerate([('fast','{}'),('fast','broken-json'),('slow','{}')])}
    ctx = RunContext(messages=agent.history, response=LLMResponse(content='', tool_calls=tool_calls))
    async def scenario():
        task = asyncio.create_task(agent._on_execute_tools(ctx))
        await ready.wait()
        await asyncio.wait_for(completed_reads.wait(), 1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert calls == ['default']
        assert 'completed-evidence' in agent.history[0]['content']
        assert 'invalid_arguments' in agent.history[1]['content']
        assert 'cancelled' in agent.history[2]['content']
    asyncio.run(scenario())


def test_cancel_sync_write_holds_lease_until_thread_finishes(runtime):
    import threading
    deps, agent = runtime
    agent.mode = RunMode.EXECUTE
    started = threading.Event()
    finish = threading.Event()
    from src.tools.policy import PathArgument, PathRole
    class Args(BaseModel):
        path: str = "target.txt"
    def write(path):
        started.set()
        assert finish.wait(2)
        return 'written'
    deps.tools_mgr.register(ToolEntry('blocking_write', write, Args, '', Args.model_json_schema(), policy=ToolPolicy(AccessKind.WORKSPACE_WRITE, DataFlow.LOCAL, (PathArgument("path", PathRole.WRITE),)), origin=ToolOrigin('builtin')))
    async def scenario():
        task = asyncio.create_task(deps.tools_mgr.execute('blocking_write', {}, deps=deps, agent=agent))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            await asyncio.sleep(0)
            assert deps.process_mgr.workspace_lock.writer
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
        assert not deps.process_mgr.workspace_lock.writer
    asyncio.run(scenario())


def test_parallel_results_keep_individual_budget_and_match_events(runtime):
    from src.events.types import ToolCallCompleted
    deps, fake = runtime
    agent = object.__new__(Agent)
    agent.deps, agent.uuid, agent.llm = deps, fake.uuid, fake.llm
    agent.agent_type, agent.mode, agent.tools = 'main', RunMode.EXECUTE, None
    agent.history = []
    agent._reminder_mgr = ReminderMgr()
    events = []
    class Bus:
        async def emit(self, event):
            events.append(event)
    deps.event_bus = Bus()
    class Args(BaseModel):
        pass
    async def evidence():
        return 'x' * 45000
    deps.tools_mgr.register(ToolEntry('evidence', evidence, Args, '', Args.model_json_schema(), policy=ToolPolicy(AccessKind.INTERNAL, DataFlow.LOCAL), origin=ToolOrigin('builtin'), parallel=True))
    calls = {i: {'id': str(i), 'name': 'evidence', 'arguments': '{}'} for i in range(8)}
    ctx = RunContext(messages=agent.history, response=LLMResponse(content='', tool_calls=calls))
    asyncio.run(agent._on_execute_tools(ctx))
    completed = {event.tool_call_id: event for event in events if isinstance(event, ToolCallCompleted)}
    for message in agent.history:
        size = len(message['content'].encode())
        assert 39000 <= size <= 40000
        event = completed[message['tool_call_id']]
        assert event.returned_bytes == size
        assert event.returned_tokens_estimate == (size + 3) // 4
