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

        user_facts_instance = Mock()
        conversation_summaries_instance = Mock()
        vector_memory_cls = Mock(side_effect=[user_facts_instance, conversation_summaries_instance])
        conversation_buffer_cls = Mock()

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
            "src.memory.memory": types.SimpleNamespace(
                ConversationBuffer=conversation_buffer_cls,
                VectorMemory=vector_memory_cls,
            ),
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

        return main_module, vector_memory_cls, user_facts_instance, conversation_summaries_instance

    def test_collection_name_includes_sanitized_user_id(self):
        main_module, vector_memory_cls, _, _ = self._load_main_with_stubs(user_id="User/ABC 123")

        self.assertEqual(main_module._build_collection_name("user_facts", "User/ABC 123"), "user_facts_user_abc_123")
        self.assertEqual(vector_memory_cls.call_args_list[0].kwargs["collection_name"], "user_facts_user_abc_123")
        self.assertEqual(vector_memory_cls.call_args_list[1].kwargs["collection_name"], "conversation_summaries_user_abc_123")

    # test_run_agent_merges_fact_and_summary_context 已移除
    # run_agent 已重构为 ChatFlow，相关测试见 tests/flows/test_chat.py


if __name__ == "__main__":
    unittest.main()
