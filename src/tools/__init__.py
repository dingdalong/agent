import importlib
from pathlib import Path
from typing import Dict, Any, Callable, Optional, Type, TypedDict
from pydantic import BaseModel
import logging
from .tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

# 全局工具注册表
_TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}

def tool(
    model: Type[BaseModel],           # 必选，Pydantic 模型类
    name: Optional[str] = None,
    description: Optional[str] = None,
    sensitive: bool = False,
    confirm_template: Optional[str] = None
):
    """装饰器：注册工具函数，参数由 Pydantic 模型定义
    Args:
        model: Pydantic 模型类，用于生成参数 schema 和验证输入
        name: 工具名称，默认使用函数名
        description: 工具描述，默认使用函数文档
        sensitive: 是否敏感工具
        confirm_template: 敏感工具的确认提示模板，支持 {参数名} 占位符
    """
    def decorator(func: Callable):
        tool_name = name or func.__name__
        if tool_name in _TOOL_REGISTRY:
            logger.warning(f"repeated tool. name:{tool_name}")
            return func

        # 从模型生成参数 schema
        # 确保模型生成的是 object 类型的 schema，否则包装
        model_schema = model.model_json_schema()
        if model_schema.get("type") == "object":
            parameters_schema = model_schema
        else:
            # 极少数情况：模型直接返回基本类型，包装为 input 属性
            parameters_schema = {
                "type": "object",
                "properties": {
                    "input": model_schema
                },
                "required": ["input"]
            }
        parameters_schema.pop("description", None)

        tool_info = {
            "name": tool_name,
            "func": func,
            "model": model,                     # 保存模型类，用于执行时实例化
            "description": description or func.__doc__ or "",
            "parameters_schema": parameters_schema,
            "sensitive": sensitive,
            "confirm_template": confirm_template,
        }

        _TOOL_REGISTRY[tool_name] = tool_info
        logger.debug(f"registry tool. name:{tool_name} with model {model.__name__}")

        return func
    return decorator

def get_tools_schemas() -> list:
    """生成 OpenAI 格式的工具列表"""
    schemas = []
    for name, info in _TOOL_REGISTRY.items():
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": info["description"],
                "parameters": info["parameters_schema"],
            }
        })
    return schemas


class ToolDict(TypedDict):
    function: Dict[str, Any]


# 非工具模块，自动发现时跳过
_SKIP_MODULES = {"__init__", "tool_call", "tool_executor"}

# 自动发现所有工具模块
package_dir = Path(__file__).parent
for item in sorted(package_dir.glob("*.py")):
    module_name = item.stem
    if module_name in _SKIP_MODULES:
        continue
    module = importlib.import_module(f".{module_name}", package=__package__)

tools = get_tools_schemas()
tool_executor = ToolExecutor(_TOOL_REGISTRY)

__all__ = ["tools", "tool_executor", "tool", "ToolDict"]
