import asyncio, logging
from src.agent import AgentDeps
from src.events import EventBus

logger = logging.getLogger(__name__)

class AgentApp:
    def __init__(
        self,
        deps: AgentDeps,
        event_bus: EventBus,
    ):
        self.deps = deps
        self.event_bus = event_bus

    async def process(self, user_input: str) -> None:
        response = await self.deps.llm.chat(
            [
                {"role": "system", "content": ""},
                {"role": "user", "content": user_input},
            ],
        )

    async def run(self) -> None:
        consumer_task = None
        async def _consume():
            async for event in self.event_bus.subscribe():
                await self.deps.ui.on_event(event)
        consumer_task = asyncio.create_task(_consume())

        await self.deps.ui.output("Agent 已启动，输入 'exit' 退出。\n")
        try:
            while True:
                user_input = await self.deps.ui.input("\n\n你: ")
                if user_input.strip().lower() in ("exit", "quit"):
                    break
                await self.process(user_input)
        finally:
            if self.event_bus:
                self.event_bus.close()
            if consumer_task:
                await consumer_task
        pass

    async def shutdown(self):
        pass
