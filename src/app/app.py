import asyncio, logging
from dataclasses import dataclass, field
from src.agent import Agent, AgentDeps
from src.interfaces import InputInterrupted

logger = logging.getLogger(__name__)

@dataclass
class AgentApp:
    deps: AgentDeps = field(repr=False)

    async def run(self) -> None:
        consumer_task = None
        async def _consume():
            async for event in self.deps.event_bus.subscribe():
                await self.deps.ui.on_event(event)
        consumer_task = asyncio.create_task(_consume())
        await asyncio.sleep(0)

        await self.deps.event_bus.request_output("Agent 已启动，输入 'exit' 退出。\n")
        try:
            agent = Agent(
                agent_type = "总控",
                description = "入口",
                deps = self.deps,
            )
            history = []
            while True:
                try:
                    user_input = await self.deps.event_bus.request_input("\n\n你: ")
                except (InputInterrupted, asyncio.CancelledError, KeyboardInterrupt):
                    break
                if user_input.strip().lower() in ("exit", "quit"):
                    break

                work_task = asyncio.create_task(agent.run(user_input, history))
                try:
                    await work_task
                except (InputInterrupted, asyncio.CancelledError, KeyboardInterrupt):
                    if not work_task.done():
                        work_task.cancel()
                    await asyncio.gather(work_task, return_exceptions=True)
                    await self.deps.event_bus.request_output("\n已中断当前任务。\n")
                    continue
        finally:
            if self.deps.event_bus:
                self.deps.event_bus.close()
            if consumer_task:
                consumer_task.cancel()
                await asyncio.gather(consumer_task, return_exceptions=True)
        pass

    async def shutdown(self):
        pass
