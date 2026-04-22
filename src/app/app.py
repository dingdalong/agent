import asyncio, logging
from src.singleton import ui, event_bus, llm
from src.agent import Agent
from src.compact import CompactState

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
            prompt = "你是一个有用的助手，你的名字叫小糖果"
            agent = Agent("总控", "入口", prompt)
            compact_state = CompactState()
            history = []
            while True:
                user_input = await ui.input("\n\n你: ")
                if user_input.strip().lower() in ("exit", "quit"):
                    break

                await agent.run(user_input, history, compact_state)
                #final_text = extract_text(history[-1]["content"])
                #if final_text:
                #    print(final_text)
                #print()
                #from workflow.plan import build_graph, PlanExecuteState
                #from src.graph import GraphEngine, RunContext
                #plan_graph = await build_graph()
                #state = PlanExecuteState(user_goal=user_input.strip())
                #result = await GraphEngine().run(plan_graph, RunContext(input=user_input.strip(), state=state))
                #await ui.output(result.output)
        finally:
            if event_bus:
                event_bus.close()
            if consumer_task:
                await consumer_task
        pass

    async def shutdown(self):
        pass
