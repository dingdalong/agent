from __future__ import annotations
from typing import TYPE_CHECKING

from src.tools.decorator import tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import Agent


class FillSkillTemplate(BaseModel):
    skill_name: str = Field(..., description="技能名称")
    file_name: str = Field(..., description="附属文件名称")
    variables: dict[str, str] = Field(..., description="模板变量，key 为占位符名，value 为替换值")


@tool(model=FillSkillTemplate, description="填充技能附属文件中的模板变量，返回替换后的文本")
async def fill_skill_template(skill_name: str, file_name: str, variables: dict[str, str], agent: Agent) -> str:
    text = agent._skill_mgr.get_companion(skill_name, file_name)
    if text is None:
        return f"错误：未找到技能 '{skill_name}' 的附属文件 '{file_name}'"
    for key, value in variables.items():
        text = text.replace(f"{{{key}}}", value)
    return text
