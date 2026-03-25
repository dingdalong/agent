import inspect
import asyncio
from typing import Dict, Any, Optional
from pydantic import ValidationError
from src.core.io import agent_output, agent_input

class ToolExecutor:
    def __init__(self, registry: Dict[str, Dict[str, Any]]):
        """
        registry: 工具注册表，格式为 {name: {"func": callable, "model": BaseModel, "sensitive": bool, ...}}
        """
        self.registry = registry

    def is_sensitive(self, tool_name: str) -> bool:
        """检查工具是否为敏感工具"""
        info = self.registry.get(tool_name)
        return info.get("sensitive", False) if info else False

    async def execute(self, tool_name: str, arguments: Dict[str, Any], skip_confirm: bool = False) -> str:
        """异步执行工具，返回结果字符串（错误信息也以字符串返回）

        Args:
            skip_confirm: 为 True 时跳过敏感工具确认（用于已预先确认的场景）
        """
        if tool_name not in self.registry:
            return f"错误：未知工具 '{tool_name}'"

        info = self.registry[tool_name]
        func = info["func"]
        model = info["model"]
        sensitive = info.get("sensitive", False)

        # 敏感工具确认（异步方式）
        if sensitive and not skip_confirm:
            confirmed = await self._confirm_sensitive(tool_name, arguments)
            if not confirmed:
                return "用户取消了操作"

        # 使用 Pydantic 模型验证参数
        try:
            validated_args = model(**arguments).model_dump()  # 转换为字典，也可直接用模型实例
        except ValidationError as e:
            # 格式化验证错误，返回给模型
            error_msg = self._format_validation_error(e)
            return f"参数验证失败: {error_msg}"

        # 执行工具函数（支持同步或异步）
        try:
            result = await self._run_func(func, validated_args)
            result_str = str(result)
            # 限制结果长度
            if len(result_str) > 2000:
                result_str = result_str[:2000] + "...(结果已截断)"
            return result_str
        except Exception as e:
            # 精简错误信息：只保留异常类型和简短描述，不包含堆栈
            error_msg = f"{type(e).__name__}: {str(e)}"
            if len(error_msg) > 200:
                error_msg = error_msg[:200] + "..."
            return f"工具执行出错: {error_msg}"

    async def _run_func(self, func, validated_args)->str:
        if inspect.iscoroutinefunction(func):
            result = await func(**validated_args)
        else:
            # 同步函数放在线程池执行，避免阻塞事件循环
            result = await asyncio.to_thread(func, **validated_args)
        return str(result)

    def _build_confirm_prompt(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """根据工具名和参数生成自然语言确认提示，模板来自工具注册时的 confirm_template"""
        info = self.registry.get(tool_name)
        template = info.get("confirm_template") if info else None
        if template:
            try:
                desc = template.format(**arguments)
                return f"是否允许{desc}？"
            except KeyError:
                pass
        # 未知工具的兜底提示
        return f"是否允许执行 '{tool_name}'？"

    async def _confirm_sensitive(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> bool:
        """异步询问用户确认，使用自然语言提示"""
        prompt = self._build_confirm_prompt(tool_name, arguments or {})
        await agent_output(f"\n⚠️  {prompt}\n")
        answer = await agent_input("(y/n): ")
        return answer.strip().lower() == 'y'

    def _format_validation_error(self, error: ValidationError) -> str:
        messages = []
        for err in error.errors()[:3]:  # 最多显示前3个错误
            loc = ".".join(str(x) for x in err["loc"])
            msg = err["msg"]
            messages.append(f"{loc}: {msg}")
        result = "; ".join(messages)
        if len(error.errors()) > 3:
            result += f"... 等{len(error.errors())}个错误"
        return result
