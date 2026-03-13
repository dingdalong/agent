"""
工具包入口文件，自动发现所有工具模块并构建 tools 列表和 tool_executor 字典。
每个工具模块应定义：
    - ToolModel : Pydantic 模型类
    - execute   : 可调用函数
    - (可选) TOOL_NAME : 工具名称，默认使用模块名
    - (可选) TOOL_DESCRIPTION : 工具描述，默认使用 ToolModel.__doc__
"""

import importlib
from pathlib import Path

# 获取当前目录的路径
package_dir = Path(__file__).parent

# 存储最终结果
tools = []
tool_executor = {}

# 遍历当前目录下所有 .py 文件（排除 __init__.py）
for item in sorted(package_dir.glob("*.py")):
    if item.name == "__init__.py":
        continue

    module_name = item.stem  # 例如 "weather"
    # 动态导入模块（使用相对导入）
    module = importlib.import_module(f".{module_name}", package=__package__)

    # 检查模块是否包含必要属性
    if not hasattr(module, "ToolModel") or not hasattr(module, "execute"):
        # 若缺少核心元素，跳过（可在此打印警告，便于调试）
        continue

    model = module.ToolModel
    func = module.execute

    # 工具名称：优先使用模块内定义的 TOOL_NAME，否则用模块名
    tool_name = getattr(module, "TOOL_NAME", module_name)
    # 工具描述：优先使用 TOOL_DESCRIPTION，否则使用 model 的文档字符串
    tool_description = getattr(module, "TOOL_DESCRIPTION", model.__doc__)

    # 构建 OpenAI 工具格式
    tool_def = {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": tool_description,
            "parameters": model.model_json_schema(),
        }
    }
    tools.append(tool_def)

    # 添加到执行器字典
    tool_executor[tool_name] = func

# 可选：导出 __all__，明确哪些是公开的
__all__ = ["tools", "tool_executor"]