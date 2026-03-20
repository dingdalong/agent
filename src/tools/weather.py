from . import tool
from pydantic import BaseModel, Field
import asyncio

class GetWeather(BaseModel):
    """获取指定城市的天气预报。"""
    city: str = Field(description="城市名称")

@tool(name="get_weather", description="模拟天气查询")
async def get_weather(city: str) -> str:
    await asyncio.sleep(0.1)  # 模拟异步操作
    weather_data = {
        "北京": "晴，气温25℃",
        "上海": "多云，气温28℃",
        "东京": "小雨，气温22℃"
    }
    return weather_data.get(city, f"未找到 {city} 的天气信息")
