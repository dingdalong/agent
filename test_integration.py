#!/usr/bin/env python3
"""Integration-style tests for the main memory wiring."""

import importlib
import sys
import types
import unittest
from unittest.mock import Mock


class TestMainMemoryIntegration(unittest.TestCase):
    def _load_main_with_stubs(self, user_id="User 42"):
        sys.modules.pop("main", None)

        user_facts_instance = Mock()
        conversation_summaries_instance = Mock()
        vector_memory_cls = Mock(side_effect=[user_facts_instance, conversation_summaries_instance])
        conversation_buffer_cls = Mock()

        stub_modules = {
            "src.tools": types.SimpleNamespace(tools=[], tool_executor={}),
            "src.core.api": types.SimpleNamespace(
                call_model_with_retry=Mock(return_value=("stub", {}, "stop")),
                execute_tool_calls=Mock(return_value=[]),
            ),
            "src.memory.memory": types.SimpleNamespace(
                ConversationBuffer=conversation_buffer_cls,
                VectorMemory=vector_memory_cls,
            ),
            "config": types.SimpleNamespace(USER_ID=user_id),
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

    def test_run_agent_merges_fact_and_summary_context(self):
        main_module, _, user_facts, conversation_summaries = self._load_main_with_stubs()
        user_facts.search.return_value = [{"fact": "用户叫大龙"}]
        conversation_summaries.search.return_value = [{"fact": "之前讨论过记忆系统"}]
        user_facts.add_conversation = Mock()

        memory = Mock()
        memory.get_messages_for_api.return_value = [{"role": "user", "content": "你好"}]
        memory.should_compress.return_value = False

        mock_call = Mock(return_value=("最终回复", {}, "stop"))
        main_module.call_model_with_retry = mock_call

        response = main_module.run_agent("你好", memory, "系统提示")

        self.assertEqual(response, "最终回复")
        sent_messages = mock_call.call_args.args[0]
        system_content = sent_messages[0]["content"]
        self.assertIn("用户叫大龙", system_content)
        self.assertIn("之前讨论过记忆系统", system_content)
        self.assertIn("系统提示", system_content)
        user_facts.add_conversation.assert_called_once_with("你好", "最终回复")


if __name__ == "__main__":
    unittest.main()
