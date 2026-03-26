"""
测试 src/flows/__init__.py — Flow 注册表与路由
"""
import pytest
from unittest.mock import MagicMock

from statemachine import StateMachine, State

from src.flows import register_flow, detect_flow, _FLOW_REGISTRY


class DummyFlow(StateMachine):
    init = State(initial=True)
    done = State(final=True)
    proceed = init.to(done)


def _dummy_factory(**kwargs):
    return DummyFlow()


class TestFlowRegistry:

    def setup_method(self):
        """每个测试前保存并清空注册表"""
        self._backup = dict(_FLOW_REGISTRY)
        _FLOW_REGISTRY.clear()

    def teardown_method(self):
        """每个测试后恢复注册表"""
        _FLOW_REGISTRY.clear()
        _FLOW_REGISTRY.update(self._backup)

    def test_register_and_detect(self):
        """测试注册后能检测到 flow"""
        register_flow("/test", _dummy_factory)
        flow = detect_flow("/test something")
        assert flow is not None
        assert isinstance(flow, DummyFlow)

    def test_detect_no_match(self):
        """测试无匹配时返回 None"""
        register_flow("/test", _dummy_factory)
        flow = detect_flow("普通对话")
        assert flow is None

    def test_case_insensitive_trigger(self):
        """测试触发词大小写不敏感"""
        register_flow("/Book", _dummy_factory)
        flow = detect_flow("/book a room")
        assert flow is not None

    def test_case_insensitive_input(self):
        """测试用户输入大小写不敏感"""
        register_flow("/test", _dummy_factory)
        flow = detect_flow("/TEST hello")
        assert flow is not None

    def test_chinese_trigger(self):
        """测试中文触发词"""
        register_flow("预订会议室", _dummy_factory)
        flow = detect_flow("预订会议室 明天下午")
        assert flow is not None

    def test_whitespace_stripped(self):
        """测试输入前后空格被去除"""
        register_flow("/test", _dummy_factory)
        flow = detect_flow("  /test hello  ")
        assert flow is not None

    def test_kwargs_passed_to_factory(self):
        """测试 kwargs 被传递给工厂函数"""
        mock_factory = MagicMock(return_value=DummyFlow())
        register_flow("/kw", mock_factory)
        detect_flow("/kw test", tool_executor="mock_exec")
        mock_factory.assert_called_once_with(tool_executor="mock_exec")

    def test_multiple_registrations(self):
        """测试多个触发词注册"""
        factory1 = MagicMock(return_value=DummyFlow())
        factory2 = MagicMock(return_value=DummyFlow())

        register_flow("/a", factory1)
        register_flow("/b", factory2)

        detect_flow("/a test")
        factory1.assert_called_once()
        factory2.assert_not_called()

        factory1.reset_mock()
        detect_flow("/b test")
        factory2.assert_called_once()
        factory1.assert_not_called()

    def test_exact_prefix_match(self):
        """测试前缀匹配而非完全匹配"""
        register_flow("/book", _dummy_factory)
        # "/booking" 以 "/book" 开头，应该匹配
        flow = detect_flow("/booking")
        assert flow is not None

    def test_builtin_meeting_booking_registered(self):
        """测试 meeting_booking 模块的触发词已自动注册"""
        # 恢复原始注册表来测试
        _FLOW_REGISTRY.clear()
        _FLOW_REGISTRY.update(self._backup)

        flow = detect_flow("/book")
        assert flow is not None

        flow2 = detect_flow("预订会议室")
        assert flow2 is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
