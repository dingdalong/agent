#!/usr/bin/env python3
"""Integration-style tests for the main memory wiring."""

import importlib
import sys
import types
import unittest
import asyncio
from unittest.mock import Mock, AsyncMock


class TestMainMemoryIntegration(unittest.TestCase):
    def _load_main_with_stubs(self, user_id="User 42"):
        sys.modules.pop("main", None)

        memory_store_instance = Mock()
        memory_store_cls = Mock(return_value=memory_store_instance)
        conversation_buffer_cls = Mock()

        # Stub both the submodules and the src.memory package itself
        memory_namespace = types.SimpleNamespace(
            ConversationBuffer=conversation_buffer_cls,
            MemoryStore=memory_store_cls,
        )

        stub_modules = {
            "src.tools": types.SimpleNamespace(tools=[], tool_executor=Mock()),
            "src.core.async_api": types.SimpleNamespace(
                call_model=AsyncMock(return_value=("stub", {}, "stop")),
            ),
            "src.core.io": types.SimpleNamespace(
                agent_input=AsyncMock(return_value=""),
                agent_output=AsyncMock(),
            ),
            "src.core.fsm": types.SimpleNamespace(FSMRunner=Mock()),
            "src.core.guardrails": types.SimpleNamespace(
                InputGuardrail=Mock(return_value=Mock(check=Mock(return_value=(True, "")))),
            ),
            "src.memory": memory_namespace,
            "src.memory.buffer": types.SimpleNamespace(
                ConversationBuffer=conversation_buffer_cls,
                summarize_conversation=Mock(),
            ),
            "src.memory.store": types.SimpleNamespace(MemoryStore=memory_store_cls),
            "src.flows": types.SimpleNamespace(detect_flow=Mock(return_value=None)),
            "src.flows.planning": types.SimpleNamespace(PlanningFlow=Mock()),
            "src.agents": types.SimpleNamespace(
                agent_registry=Mock(),
                MultiAgentFlow=Mock(),
            ),
            "config": types.SimpleNamespace(USER_ID=user_id, MCP_CONFIG_PATH="mcp_servers.json", SKILLS_DIRS=["skills/"]),
            "src.mcp.config": types.SimpleNamespace(load_mcp_config=Mock(return_value={})),
            "src.mcp.manager": types.SimpleNamespace(MCPManager=Mock()),
            "src.skills": types.SimpleNamespace(SkillManager=Mock()),
        }

        original_modules = {name: sys.modules.get(name) for name in stub_modules}
        sys.modules.update(stub_modules)
        try:
            main_module = importlib.import_module("main")
        finally:
            for name, original in original_modules.items():
                if original is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original

        return main_module, memory_store_cls, memory_store_instance

    def test_collection_name_includes_sanitized_user_id(self):
        main_module, memory_store_cls, _ = self._load_main_with_stubs(user_id="User/ABC 123")

        self.assertEqual(main_module._build_collection_name("user_facts", "User/ABC 123"), "user_facts_user_abc_123")
        self.assertEqual(memory_store_cls.call_args.kwargs["collection_name"], "memories_user_abc_123")

    # test_run_agent_merges_fact_and_summary_context 已移除
    # run_agent 已重构为 ChatFlow，相关测试见 tests/flows/test_chat.py


class TestHandleInputNewAPI(unittest.TestCase):
    def _load_main_with_stubs(self, user_id="test_user"):
        sys.modules.pop("main", None)

        memory_store_instance = Mock()
        memory_store_cls = Mock(return_value=memory_store_instance)
        conversation_buffer_cls = Mock()

        memory_namespace = types.SimpleNamespace(
            ConversationBuffer=conversation_buffer_cls,
            MemoryStore=memory_store_cls,
        )

        stub_modules = {
            "src.tools": types.SimpleNamespace(tools=[], tool_executor=Mock()),
            "src.core.async_api": types.SimpleNamespace(
                call_model=AsyncMock(return_value=("stub", {}, "stop")),
            ),
            "src.core.io": types.SimpleNamespace(
                agent_input=AsyncMock(return_value=""),
                agent_output=AsyncMock(),
            ),
            "src.core.fsm": types.SimpleNamespace(FSMRunner=Mock()),
            "src.core.guardrails": types.SimpleNamespace(
                InputGuardrail=Mock(return_value=Mock(check=Mock(return_value=(True, "")))),
            ),
            "src.memory": memory_namespace,
            "src.memory.buffer": types.SimpleNamespace(
                ConversationBuffer=conversation_buffer_cls,
                summarize_conversation=Mock(),
            ),
            "src.memory.store": types.SimpleNamespace(MemoryStore=memory_store_cls),
            "src.flows": types.SimpleNamespace(detect_flow=Mock(return_value=None)),
            "src.flows.planning": types.SimpleNamespace(PlanningFlow=Mock()),
            "src.agents": types.SimpleNamespace(
                agent_registry=Mock(),
                MultiAgentFlow=Mock(),
            ),
            "config": types.SimpleNamespace(USER_ID=user_id, MCP_CONFIG_PATH="mcp_servers.json", SKILLS_DIRS=["skills/"]),
            "src.mcp.config": types.SimpleNamespace(load_mcp_config=Mock(return_value={})),
            "src.mcp.manager": types.SimpleNamespace(MCPManager=Mock()),
            "src.skills": types.SimpleNamespace(SkillManager=Mock()),
        }

        original_modules = {name: sys.modules.get(name) for name in stub_modules}
        sys.modules.update(stub_modules)
        try:
            main_module = importlib.import_module("main")
        finally:
            for name, original in original_modules.items():
                if original is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original

        return main_module

    def test_normal_conversation_uses_store(self):
        """handle_input section 4 should pass buffer and store (not old vars)."""
        main_module = self._load_main_with_stubs()
        self.assertTrue(hasattr(main_module, 'store'))
        self.assertTrue(hasattr(main_module, 'buffer'))
        self.assertFalse(hasattr(main_module, 'user_facts'))
        self.assertFalse(hasattr(main_module, 'conversation_summaries'))


if __name__ == "__main__":
    unittest.main()
