from src.tools.decorator import tool
from pydantic import BaseModel, Field

class Compact(BaseModel):
    focus: str = Field(..., description="后续需要专注的内容")

@tool(model=Compact, description="总结之前的对话，以便在更小的上下文中继续工作。")
async def compact(focus: str):
    pass
