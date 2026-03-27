from pydantic import BaseModel, Field, model_validator
from typing import Optional


class Step(BaseModel):
    """计划中的单个步骤。

    类型由字段值决定：
    - tool_name 有值 → 工具调用步骤
    - agent_name 有值 → Agent 委托步骤
    """

    id: str = Field(description="步骤唯一标识")
    description: str = Field(description="步骤描述，用于展示")
    tool_name: Optional[str] = Field(default=None, description="工具名称（工具步骤）")
    tool_args: dict = Field(default_factory=dict, description="工具参数，支持 $step_id.field 变量引用")
    agent_name: Optional[str] = Field(default=None, description="Agent 名称（委托步骤）")
    agent_prompt: Optional[str] = Field(default=None, description="Agent 指令")
    depends_on: list[str] = Field(default_factory=list, description="依赖的步骤 ID 列表")

    @model_validator(mode="after")
    def validate_step_type(self):
        has_tool = self.tool_name is not None
        has_agent = self.agent_name is not None
        if has_tool and has_agent:
            raise ValueError("Step cannot have both tool_name and agent_name")
        if not has_tool and not has_agent:
            raise ValueError("Step must have either tool_name or agent_name")
        return self


class Plan(BaseModel):
    """完整计划"""

    steps: list[Step] = Field(description="步骤列表")
    context: dict = Field(default_factory=dict, description="初始上下文")
