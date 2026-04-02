"""sensitive_confirm_middleware 测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.tools.middleware import sensitive_confirm_middleware, build_pipeline
from src.tools.registry import ToolRegistry, ToolEntry


def _make_registry_with_sensitive_tool() -> ToolRegistry:
    """构建包含一个 sensitive=True 工具的 registry。"""
    registry = ToolRegistry()
    entry = ToolEntry(
        name="delete_file",
        func=AsyncMock(),
        description="删除文件",
        parameters_schema={},
        model=None,
        sensitive=True,
        confirm_template="删除文件 {path}",
    )
    registry._entries["delete_file"] = entry
    return registry


@pytest.fixture
def mock_interaction():
    interaction = AsyncMock()
    interaction.confirm = AsyncMock(return_value=True)
    return interaction


class TestSensitiveConfirmMiddleware:
    @pytest.mark.asyncio
    async def test_confirm_approved_calls_next(self, mock_interaction):
        """用户确认后应继续执行下游。"""
        registry = _make_registry_with_sensitive_tool()
        mock_interaction.confirm = AsyncMock(return_value=True)
        mw = sensitive_confirm_middleware(registry, mock_interaction)

        next_fn = AsyncMock(return_value="deleted")
        result = await mw("delete_file", {"path": "/tmp/test"}, next_fn)

        assert result == "deleted"
        next_fn.assert_called_once()
        mock_interaction.confirm.assert_called_once()

    @pytest.mark.asyncio
    async def test_confirm_rejected_returns_cancel(self, mock_interaction):
        """用户拒绝后应返回取消消息，不调用下游。"""
        registry = _make_registry_with_sensitive_tool()
        mock_interaction.confirm = AsyncMock(return_value=False)
        mw = sensitive_confirm_middleware(registry, mock_interaction)

        next_fn = AsyncMock(return_value="deleted")
        result = await mw("delete_file", {"path": "/tmp/test"}, next_fn)

        assert "取消" in result
        next_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_sensitive_tool_skips_confirm(self, mock_interaction):
        """非敏感工具应直接执行，不触发确认。"""
        registry = ToolRegistry()
        entry = ToolEntry(
            name="get_weather",
            func=AsyncMock(),
            description="获取天气",
            parameters_schema={},
            model=None,
            sensitive=False,
            confirm_template=None,
        )
        registry._entries["get_weather"] = entry
        mw = sensitive_confirm_middleware(registry, mock_interaction)

        next_fn = AsyncMock(return_value="sunny")
        result = await mw("get_weather", {}, next_fn)

        assert result == "sunny"
        mock_interaction.confirm.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirm_message_uses_template(self, mock_interaction):
        """确认消息应使用 confirm_template 格式化。"""
        registry = _make_registry_with_sensitive_tool()
        mock_interaction.confirm = AsyncMock(return_value=True)
        mw = sensitive_confirm_middleware(registry, mock_interaction)

        next_fn = AsyncMock(return_value="ok")
        await mw("delete_file", {"path": "/tmp/test"}, next_fn)

        confirm_arg = mock_interaction.confirm.call_args[0][0]
        assert "删除文件 /tmp/test" in confirm_arg
