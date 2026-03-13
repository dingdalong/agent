from pydantic import BaseModel, Field

class GetWeather(BaseModel):
    """获取指定城市的天气预报。"""
    city: str = Field(description="城市名称")

def get_weather(city: str) -> str:
    """模拟天气查询"""
    weather_data = {
        "北京": "晴，气温25℃",
        "上海": "多云，气温28℃",
        "东京": "小雨，气温22℃"
    }
    return weather_data.get(city, f"未找到 {city} 的天气信息")

# 为了满足自动发现约定，添加别名
ToolModel = GetWeather
execute = get_weather