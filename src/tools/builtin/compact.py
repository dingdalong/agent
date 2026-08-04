from src.tools.policy import AccessKind, DataFlow, ToolPolicy
from src.tools.decorator import tool
from pydantic import BaseModel, Field

class Compact(BaseModel):
    focus: str = Field(..., description="给压缩摘要模型的保留提示：说明压缩早期上下文时必须重点保留的必要上下文，包括但不限于用户要求、设计决策、当前进度、已完成内容、阶段性结果、实现状态、文件、命令、错误、风险或待办。")

@tool(model=Compact, description="总结之前的对话，以便在更小的上下文中继续工作。",
      policy=ToolPolicy(AccessKind.INTERNAL, DataFlow.LOCAL, plan_safe=True), subagent=True)
async def compact(focus: str):
    return "对话历史已压缩。请继续基于当前压缩后的上下文工作。"
