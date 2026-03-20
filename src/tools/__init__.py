import importlib
from pathlib import Path
from typing import Dict, Any, Callable, Optional
import inspect
from pydantic import TypeAdapter, BaseModel
import logging
logger = logging.getLogger(__name__)

# 全局工具注册表
_TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}

def tool(name: Optional[str] = None, description: Optional[str] = None, sensitive: bool = False):
    """装饰器：注册工具函数"""
    def decorator(func: Callable):
        tool_name = name or func.__name__
        if tool_name in _TOOL_REGISTRY:
            print("1重复的工具："+tool_name)
            return

        # 从函数签名自动生成参数 schema
        sig = inspect.signature(func)
        parameters = {}
        required = []
        for param_name, param in sig.parameters.items():
            # 利用 TypeAdapter 从类型注解生成 schema
            if param.annotation != inspect.Parameter.empty:
                adapter = TypeAdapter(param.annotation)
                param_schema = adapter.json_schema()
            else:
                param_schema = {"type": "string"}  # 默认
            parameters[param_name] = param_schema
            if param.default == inspect.Parameter.empty:
                required.append(param_name)
        parameters_schema = {
            "type": "object",
            "properties": parameters,
        }
        if required:
            parameters_schema["required"] = required

        tool_info = {
            "name": tool_name,
            "func": func,
            "description": description or func.__doc__ or "",
            "parameters_schema": parameters_schema,
            "sensitive": sensitive,
        }

        if tool_name in _TOOL_REGISTRY:
            logger.warning(f"repeated tool. name:{tool_name}")
            return

        _TOOL_REGISTRY[tool_name] = tool_info
        logger.debug(f"registry tool. name:{tool_name}")

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

# 自动发现所有工具模块
package_dir = Path(__file__).parent
for item in sorted(package_dir.glob("*.py")):
    if item.name == "__init__.py":
        continue
    module_name = item.stem
    module = importlib.import_module(f".{module_name}", package=__package__)

tools = get_tools_schemas()
tool_executor = {name: info["func"] for name, info in _TOOL_REGISTRY.items()}

__all__ = ["tools", "tool_executor", "tool"]
