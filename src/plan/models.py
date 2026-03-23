from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Literal

class Step(BaseModel):
    """单个步骤"""
    id: str = Field(description="步骤唯一标识，如 step1")
    description: str = Field(description="步骤描述，用于展示")
    action: Literal["tool", "subtask", "user_input"] = Field(description="动作类型：'tool', 'subtask', 'user_input'")
    tool_name: Optional[str] = Field(default=None, description="工具名称（当 action=tool）")
    tool_args: Optional[Dict[str, Any]] = Field(default=None, description="工具参数")
    subtask_prompt: Optional[str] = Field(default=None, description="子任务描述（当 action=subtask）")
    depends_on: List[str] = Field(default_factory=list, description="依赖的步骤id列表")

class Plan(BaseModel):
    """完整计划"""
    steps: List[Step] = Field(description="步骤列表")
    context: Dict[str, Any] = Field(default_factory=dict, description="执行过程中的上下文变量")
