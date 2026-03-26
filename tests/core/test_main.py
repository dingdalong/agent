"""Tests for main.py module wiring."""
import importlib
import sys
import types
from unittest.mock import Mock, AsyncMock


def _load_main_with_stubs(user_id="test_user"):
    """Load main module with all external dependencies stubbed."""
    sys.modules.pop("main", None)

    memory_store_instance = Mock()
    memory_store_cls = Mock(return_value=memory_store_instance)
    conversation_buffer_cls = Mock()

    memory_namespace = types.SimpleNamespace(
        ConversationBuffer=conversation_buffer_cls,
        MemoryStore=memory_store_cls,
    )

    stub_modules = {
        "src.tools": types.SimpleNamespace(
            get_registry=Mock(return_value=Mock()),
            discover_tools=Mock(),
            ToolExecutor=Mock(),
            ToolRouter=Mock(),
            LocalToolProvider=Mock(),
            sensitive_confirm_middleware=Mock(),
            truncate_middleware=Mock(),
            error_handler_middleware=Mock(),
        ),
        "src.mcp.provider": types.SimpleNamespace(MCPToolProvider=Mock()),
        "src.skills.provider": types.SimpleNamespace(SkillToolProvider=Mock()),
        "src.core.io": types.SimpleNamespace(
            agent_input=AsyncMock(return_value=""),
            agent_output=AsyncMock(),
        ),
        "src.core.guardrails": types.SimpleNamespace(
            InputGuardrail=Mock(return_value=Mock(check=Mock(return_value=(True, "")))),
        ),
        "src.memory": memory_namespace,
        "src.memory.buffer": types.SimpleNamespace(
            ConversationBuffer=conversation_buffer_cls,
            summarize_conversation=Mock(),
        ),
        "src.memory.store": types.SimpleNamespace(MemoryStore=memory_store_cls),
        "src.agents": types.SimpleNamespace(
            Agent=Mock(),
            AgentRegistry=Mock(return_value=Mock()),
            GraphBuilder=Mock(return_value=Mock(
                add_agent=Mock(return_value=Mock(
                    set_entry=Mock(return_value=Mock(
                        compile=Mock(return_value=Mock()),
                    )),
                )),
            )),
            GraphEngine=Mock(),
            RunContext=Mock(),
            DictState=Mock(),
        ),
        "config": types.SimpleNamespace(
            USER_ID=user_id, MCP_CONFIG_PATH="mcp_servers.json", SKILLS_DIRS=["skills/"],
        ),
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


class TestMainModuleAttributes:

    def test_main_has_store_and_buffer(self):
        main_module = _load_main_with_stubs()
        assert hasattr(main_module, "store")
        assert hasattr(main_module, "buffer")

    def test_main_no_old_variables(self):
        main_module = _load_main_with_stubs()
        assert not hasattr(main_module, "user_facts")
        assert not hasattr(main_module, "conversation_summaries")
