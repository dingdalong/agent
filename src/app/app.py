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

        await self.deps.ui.output("Agent 已启动，输入 'exit' 退出。\n")
        try:
            prompt = "你是一个有用的助手，你的名字叫小糖果"
            agent = Agent(
                name = "总控",
                description = "入口",
                prompt = prompt,
                deps = self.deps,
            )
            history = []
            while True:
                user_input = await self.deps.ui.input("\n\n你: ")
                if user_input.strip().lower() in ("exit", "quit"):
                    break

                await agent.run(user_input, history)
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
            if self.deps.event_bus:
                self.deps.event_bus.close()
            if consumer_task:
                await consumer_task
        pass

    async def shutdown(self):
        pass
