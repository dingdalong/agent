import asyncio, logging
from dataclasses import dataclass, field
from src.agent import Agent, AgentDeps

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
                user_input = await self.deps.event_bus.request_input("\n\n你: ")
                if user_input.strip().lower() in ("exit", "quit"):
                    break

                await agent.run(user_input, history)
        finally:
            if self.deps.event_bus:
                await self.deps.event_bus.join()
                self.deps.event_bus.close()
            if consumer_task:
                await consumer_task
        pass

    async def shutdown(self):
        pass
