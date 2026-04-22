from src.tools.decorator import tool
from pydantic import BaseModel, Field

class Compact(BaseModel):
    focus: str = Field(..., description="后续需要专注的内容")

@tool(model=Compact, description="Summarize earlier conversation so work can continue in a smaller context.")
async def compact(focus: str):
    pass
