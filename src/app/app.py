import asyncio, logging
from src.singleton import ui, event_bus, llm
from src.agent import Agent

logger = logging.getLogger(__name__)

class AgentApp:
    def __init__(
        self,
    ):
        pass

    async def run(self) -> None:
        consumer_task = None
        async def _consume():
            async for event in event_bus.subscribe():
                await ui.on_event(event)
        consumer_task = asyncio.create_task(_consume())

        await ui.output("Agent 已启动，输入 'exit' 退出。\n")
        try:
            agent = Agent("总控", "入口", "你是一个有用的助手")
            while True:
                user_input = await ui.input("\n\n你: ")
                if user_input.strip().lower() in ("exit", "quit"):
                    break
                await agent.run(user_input)
        finally:
            if event_bus:
                event_bus.close()
            if consumer_task:
                await consumer_task
        pass

    async def shutdown(self):
        pass
