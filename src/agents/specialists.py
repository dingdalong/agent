"""专业 Agent 定义与注册。

注册三个专业 Agent：weather_agent, calendar_agent, email_agent。
工具名称与 src/tools/ 中注册的函数名保持一致。
"""

from pydantic import BaseModel

from src.agents.registry import AgentDef, AgentRegistry


class WeatherResult(BaseModel):
    """天气查询结构化结果，供后续 Agent 通过 shared_context 使用。"""

    city: str
    condition: str
    temp_c: float
    description: str


def setup_agents(registry: AgentRegistry) -> None:
    """向注册表注册所有专业 Agent。"""

    registry.register(AgentDef(
        name="weather_agent",
        description="处理天气查询，返回指定城市的当前天气状况",
        tool_names=["get_weather"],
        system_prompt=(
            "你是天气查询专家，专注于提供准确的天气信息。"
            "查询后请直接报告天气结果，包括城市、天气状况、温度等信息。"
        ),
        output_model=WeatherResult,
    ))

    registry.register(AgentDef(
        name="calendar_agent",
        description="管理日历事件，创建日程安排",
        tool_names=["create_event"],
        system_prompt=(
            "你是日历管理专家，负责按照要求创建日程事件。"
            "创建完成后请确认事件详情。"
        ),
    ))

    registry.register(AgentDef(
        name="email_agent",
        description="发送邮件，需要明确指定收件人、主题和正文",
        tool_names=["send_email"],
        system_prompt=(
            "你是邮件助手，负责按照要求发送邮件。"
            "发送前请确认收件人、主题和内容无误，然后执行发送。"
        ),
    ))
