from __future__ import annotations
from typing import TYPE_CHECKING

from src.tools.decorator import PermissionRule, ToolPermission, tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent import Agent

class TaskDelegator(BaseModel):
    name: str = Field(..., description="要加载的子智能体")
    prompt: str = Field(..., description="简短的任务需求描述")

@tool(model=TaskDelegator, description="委托一个任务给子智能体",
      permission=ToolPermission(rules=[PermissionRule(permission="allow")]))
async def task_delegator(name: str, prompt: str, agent: Agent) -> str:
    return await agent._subagent_mgr.task_delegator(name, prompt)

class TaskDelegatorTemplate(BaseModel):
    name: str = Field(..., description="要加载的子智能体")
    template_path: str = Field(..., description="模板文件的绝对路径")
    variables: dict[str, str] = Field(..., description="模板变量，key 为占位符名，value 为替换值")

@tool(model=TaskDelegatorTemplate, description="读取模板文件、填充变量后作为任务委托给子智能体",
      permission=ToolPermission(rules=[PermissionRule(permission="allow")]))
async def task_delegator_template(name: str, template_path: str, variables: dict[str, str], agent: Agent) -> str:
    template = agent._file_mgr.safe_path(template_path).read_text()
    prompt = template
    for key, value in variables.items():
        prompt = prompt.replace(f"{{{key}}}", value)
    return await agent._subagent_mgr.task_delegator(name, prompt)
