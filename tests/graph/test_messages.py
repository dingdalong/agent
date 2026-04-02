import json
import pytest
from src.graph.messages import (
    AgentMessage,
    AgentResponse,
    ResponseStatus,
    format_for_receiver,
    build_message_schema,
)


class TestAgentMessage:
    def test_required_fields(self):
        msg = AgentMessage(objective="部署应用", task="检查服务器状态")
        assert msg.objective == "部署应用"
        assert msg.task == "检查服务器状态"
        assert msg.context == ""
        assert msg.expected_result is None
        assert msg.sender is None
        assert len(msg.message_id) == 12

    def test_all_fields(self):
        msg = AgentMessage(
            objective="部署应用",
            task="检查服务器状态",
            context={"server": "prod-01"},
            expected_result="返回服务器 CPU 和内存使用率",
            sender="orchestrator",
        )
        assert msg.context == {"server": "prod-01"}
        assert msg.expected_result == "返回服务器 CPU 和内存使用率"
        assert msg.sender == "orchestrator"

    def test_message_id_unique(self):
        msg1 = AgentMessage(objective="a", task="b")
        msg2 = AgentMessage(objective="a", task="b")
        assert msg1.message_id != msg2.message_id

    def test_context_accepts_string(self):
        msg = AgentMessage(objective="a", task="b", context="一些上下文")
        assert msg.context == "一些上下文"


class TestAgentResponse:
    def test_defaults(self):
        resp = AgentResponse(text="完成")
        assert resp.status == ResponseStatus.COMPLETED
        assert resp.data == {}
        assert resp.sender is None
        assert resp.message_id == ""

    def test_with_message_id(self):
        resp = AgentResponse(text="ok", message_id="abc123")
        assert resp.message_id == "abc123"

    def test_from_graph_result_with_agent_response(self):
        original = AgentResponse(text="hello", data={"k": "v"}, message_id="id1")
        result_mock = type("GR", (), {"output": original})()
        converted = AgentResponse.from_graph_result(result_mock)
        assert converted is original

    def test_from_graph_result_with_dict(self):
        result_mock = type("GR", (), {"output": {"text": "hi", "data": {"a": 1}}})()
        converted = AgentResponse.from_graph_result(result_mock)
        assert converted.text == "hi"
        assert converted.data == {"a": 1}

    def test_from_graph_result_with_empty_dict(self):
        result_mock = type("GR", (), {"output": {}})()
        converted = AgentResponse.from_graph_result(result_mock)
        assert converted.text == ""
        assert converted.data == {}


class TestFormatForReceiver:
    def test_all_fields(self):
        msg = AgentMessage(
            objective="部署应用",
            task="检查服务器状态",
            context="生产环境",
            expected_result="CPU 和内存",
        )
        text = format_for_receiver(msg)
        assert "最终目标：部署应用" in text
        assert "具体任务：检查服务器状态" in text
        assert "相关上下文：生产环境" in text
        assert "期望结果：CPU 和内存" in text

    def test_optional_fields_omitted(self):
        msg = AgentMessage(objective="a", task="b")
        text = format_for_receiver(msg)
        assert "相关上下文" not in text
        assert "期望结果" not in text

    def test_dict_context_serialized(self):
        msg = AgentMessage(objective="a", task="b", context={"key": "val"})
        text = format_for_receiver(msg)
        assert '"key"' in text
        assert '"val"' in text


class TestBuildMessageSchema:
    def test_schema_structure(self):
        schema = build_message_schema()
        assert schema["type"] == "object"
        props = schema["properties"]
        assert "objective" in props
        assert "task" in props
        assert "context" in props
        assert "expected_result" in props
        assert schema["required"] == ["objective", "task"]

    def test_all_properties_are_strings(self):
        schema = build_message_schema()
        for prop in schema["properties"].values():
            assert prop["type"] == "string"
