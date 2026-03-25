# MCP Client Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable the AI Agent to connect to external MCP Servers, discover their tools, and use them alongside existing local tools.

**Architecture:** Add an `src/mcp/` package with config loading (`config.py`) and connection management (`manager.py`). MCPManager uses `AsyncExitStack` to manage MCP SDK context managers. MCP tools are prefixed with `mcp_{server}_{tool}` and merged into the existing tool list. ToolExecutor routes `mcp_`-prefixed calls to MCPManager.

**Tech Stack:** Python 3.13, `mcp` SDK 1.26.0 (stdio_client, streamablehttp_client, ClientSession)

**Spec:** `docs/superpowers/specs/2026-03-25-mcp-client-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/mcp/__init__.py` | Create | Package init, re-exports |
| `src/mcp/config.py` | Create | `MCPServerConfig` dataclass + `load_mcp_config()` |
| `src/mcp/manager.py` | Create | `MCPManager` class — connect/disconnect/tool discovery/call routing |
| `tests/test_mcp_config.py` | Create | Unit tests for config loading |
| `tests/test_mcp_manager.py` | Create | Unit tests for MCPManager (name conversion, schema conversion) |
| `src/tools/tool_executor.py` | Modify | Add `mcp_manager` attribute + MCP routing in `execute()` |
| `config.py` | Modify | Add `MCP_CONFIG_PATH` constant |
| `main.py` | Modify | Initialize MCPManager, inject into tool_executor, merge tool lists |
| `mcp_servers.json` | Create | Example configuration file |

---

### Task 1: MCP Config Module

**Files:**
- Create: `src/mcp/__init__.py`
- Create: `src/mcp/config.py`
- Create: `tests/test_mcp_config.py`

- [ ] **Step 1: Create package init**

```python
# src/mcp/__init__.py
from .config import MCPServerConfig, load_mcp_config
from .manager import MCPManager

__all__ = ["MCPServerConfig", "load_mcp_config", "MCPManager"]
```

Note: This will fail to import until manager.py exists. Create it as a placeholder first:

```python
# src/mcp/manager.py (placeholder)
class MCPManager:
    pass
```

- [ ] **Step 2: Write failing tests for config loading**

```python
# tests/test_mcp_config.py
import json
import tempfile
import os
import pytest
from src.mcp.config import MCPServerConfig, load_mcp_config


def test_load_stdio_config(tmp_path):
    """stdio server config is loaded correctly."""
    config_file = tmp_path / "mcp_servers.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "filesystem": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                "env": {"DEBUG": "1"}
            }
        }
    }))
    configs = load_mcp_config(str(config_file))
    assert len(configs) == 1
    c = configs[0]
    assert c.name == "filesystem"
    assert c.transport == "stdio"
    assert c.command == "npx"
    assert c.args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    assert c.env == {"DEBUG": "1"}
    assert c.enabled is True
    assert c.timeout == 30.0


def test_load_http_config(tmp_path):
    """HTTP server config is loaded correctly with defaults for missing fields."""
    config_file = tmp_path / "mcp_servers.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "my-api": {
                "transport": "http",
                "url": "http://localhost:8080/mcp"
            }
        }
    }))
    configs = load_mcp_config(str(config_file))
    assert len(configs) == 1
    c = configs[0]
    assert c.name == "my-api"
    assert c.transport == "http"
    assert c.url == "http://localhost:8080/mcp"
    assert c.command is None
    assert c.args == []
    assert c.env == {}


def test_load_missing_file_returns_empty():
    """Missing config file returns empty list without error."""
    configs = load_mcp_config("/nonexistent/path.json")
    assert configs == []


def test_load_disabled_server_skipped(tmp_path):
    """Disabled servers are excluded from results."""
    config_file = tmp_path / "mcp_servers.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "disabled-one": {
                "transport": "stdio",
                "command": "echo",
                "enabled": False
            },
            "active-one": {
                "transport": "stdio",
                "command": "echo"
            }
        }
    }))
    configs = load_mcp_config(str(config_file))
    assert len(configs) == 1
    assert configs[0].name == "active-one"


def test_load_stdio_without_command_skipped(tmp_path):
    """stdio server without command is skipped with warning."""
    config_file = tmp_path / "mcp_servers.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "bad": {
                "transport": "stdio"
            }
        }
    }))
    configs = load_mcp_config(str(config_file))
    assert configs == []


def test_load_http_without_url_skipped(tmp_path):
    """HTTP server without url is skipped with warning."""
    config_file = tmp_path / "mcp_servers.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "bad": {
                "transport": "http"
            }
        }
    }))
    configs = load_mcp_config(str(config_file))
    assert configs == []


def test_load_custom_timeout(tmp_path):
    """Custom timeout is loaded from config."""
    config_file = tmp_path / "mcp_servers.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "slow-server": {
                "transport": "stdio",
                "command": "slow-cmd",
                "timeout": 120.0
            }
        }
    }))
    configs = load_mcp_config(str(config_file))
    assert configs[0].timeout == 120.0
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_config.py -v`
Expected: FAIL (module not found)

- [ ] **Step 4: Implement config module**

```python
# src/mcp/config.py
import json
import logging
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP Server connection."""
    name: str
    transport: Literal["stdio", "http"]
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    enabled: bool = True
    timeout: float = 30.0


def load_mcp_config(path: str) -> list[MCPServerConfig]:
    """Load MCP server configurations from a JSON file.

    Returns an empty list if the file doesn't exist or can't be parsed.
    Skips invalid or disabled server entries with a warning.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.debug(f"MCP 配置文件不存在: {path}，跳过 MCP 初始化")
        return []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"MCP 配置文件读取失败: {path}, {e}")
        return []

    servers_data = data.get("mcpServers", {})
    configs = []

    for name, server_dict in servers_data.items():
        transport = server_dict.get("transport")
        enabled = server_dict.get("enabled", True)

        if not enabled:
            logger.debug(f"MCP Server '{name}' 已禁用，跳过")
            continue

        if transport not in ("stdio", "http"):
            logger.warning(f"MCP Server '{name}' transport 无效: {transport}，跳过")
            continue

        if transport == "stdio" and not server_dict.get("command"):
            logger.warning(f"MCP Server '{name}' (stdio) 缺少 command，跳过")
            continue

        if transport == "http" and not server_dict.get("url"):
            logger.warning(f"MCP Server '{name}' (http) 缺少 url，跳过")
            continue

        config = MCPServerConfig(
            name=name,
            transport=transport,
            command=server_dict.get("command"),
            args=server_dict.get("args", []),
            env=server_dict.get("env", {}),
            url=server_dict.get("url"),
            enabled=enabled,
            timeout=server_dict.get("timeout", 30.0),
        )
        configs.append(config)

    return configs
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_config.py -v`
Expected: All 7 tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/mcp/__init__.py src/mcp/config.py src/mcp/manager.py tests/test_mcp_config.py
git commit -m "feat(mcp): add config module with MCPServerConfig and load_mcp_config"
```

---

### Task 2: MCPManager — Name Utilities and Schema Conversion

**Files:**
- Modify: `src/mcp/manager.py`
- Create: `tests/test_mcp_manager.py`

These are pure functions that don't need real MCP connections to test.

- [ ] **Step 1: Write failing tests for name utilities and schema conversion**

```python
# tests/test_mcp_manager.py
import pytest
from src.mcp.manager import MCPManager


def test_make_tool_name_simple():
    """Simple server and tool names are joined with mcp_ prefix."""
    mgr = MCPManager()
    assert mgr._make_tool_name("filesystem", "read_file") == "mcp_filesystem_read_file"


def test_make_tool_name_hyphen_to_underscore():
    """Hyphens in server name are converted to underscores."""
    mgr = MCPManager()
    assert mgr._make_tool_name("my-remote-api", "query") == "mcp_my_remote_api_query"


def test_convert_tool_schema():
    """MCP Tool is converted to OpenAI function format with server tag in description."""
    mgr = MCPManager()
    # Simulate an MCP Tool object using a dict-like mock
    class MockTool:
        name = "read_file"
        description = "Read a file from the filesystem"
        inputSchema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
        annotations = None

    schema = mgr._convert_tool_schema("filesystem", MockTool())
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "mcp_filesystem_read_file"
    assert schema["function"]["description"] == "[filesystem] Read a file from the filesystem"
    assert schema["function"]["parameters"] == MockTool.inputSchema


def test_convert_result_text():
    """Text content is extracted from CallToolResult."""
    mgr = MCPManager()

    class MockTextContent:
        type = "text"
        text = "file contents here"

    class MockResult:
        isError = False
        content = [MockTextContent()]
        structuredContent = None

    result = mgr._convert_result(MockResult())
    assert result == "file contents here"


def test_convert_result_error():
    """Error results are prefixed with error message."""
    mgr = MCPManager()

    class MockTextContent:
        type = "text"
        text = "File not found"

    class MockResult:
        isError = True
        content = [MockTextContent()]
        structuredContent = None

    result = mgr._convert_result(MockResult())
    assert "工具执行出错" in result
    assert "File not found" in result


def test_convert_result_truncation():
    """Long results are truncated at 2000 chars."""
    mgr = MCPManager()

    class MockTextContent:
        type = "text"
        text = "x" * 3000

    class MockResult:
        isError = False
        content = [MockTextContent()]
        structuredContent = None

    result = mgr._convert_result(MockResult())
    assert result.endswith("...(结果已截断)")
    assert len(result) == 2000 + len("...(结果已截断)")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_manager.py -v`
Expected: FAIL (methods not found)

- [ ] **Step 3: Implement MCPManager with utilities (no connect/disconnect yet)**

```python
# src/mcp/manager.py
import logging
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.streamable_http import streamablehttp_client

from .config import MCPServerConfig

logger = logging.getLogger(__name__)


class MCPManager:
    """Manages MCP Server connections, tool discovery, and tool call routing."""

    def __init__(self):
        self._exit_stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tool_map: dict[str, tuple[str, str]] = {}  # full_name → (server_name, original_name)
        self._timeouts: dict[str, float] = {}
        self._tools_schemas: list[dict] = []

    def _make_tool_name(self, server_name: str, tool_name: str) -> str:
        """Create prefixed tool name: mcp_{server}_{tool}. Hyphens → underscores."""
        safe_server = server_name.replace("-", "_")
        return f"mcp_{safe_server}_{tool_name}"

    def _convert_tool_schema(self, server_name: str, tool) -> dict:
        """Convert an MCP Tool to OpenAI function-calling format."""
        full_name = self._make_tool_name(server_name, tool.name)
        description = tool.description or ""
        return {
            "type": "function",
            "function": {
                "name": full_name,
                "description": f"[{server_name}] {description}",
                "parameters": tool.inputSchema,
            }
        }

    def _convert_result(self, result) -> str:
        """Convert a CallToolResult to a plain string."""
        if result.isError:
            texts = [c.text for c in result.content if hasattr(c, "text")]
            return f"工具执行出错: {''.join(texts)}"

        texts = []
        for item in result.content:
            if hasattr(item, "text"):
                texts.append(item.text)
            elif hasattr(item, "data"):
                mime = getattr(item, "mimeType", "binary")
                texts.append(f"[{mime} content, {len(item.data)} bytes]")

        output = "\n".join(texts)
        if len(output) > 2000:
            output = output[:2000] + "...(结果已截断)"
        return output

    def get_tools_schemas(self) -> list[dict]:
        """Return all discovered MCP tools in OpenAI format."""
        return list(self._tools_schemas)

    async def connect_all(self, configs: list[MCPServerConfig]) -> None:
        """Connect to all configured MCP Servers. Failures are logged and skipped."""
        for config in configs:
            try:
                await self._connect_one(config)
            except Exception as e:
                logger.warning(f"MCP Server '{config.name}' 连接失败: {e}")

    async def _connect_one(self, config: MCPServerConfig) -> None:
        """Connect to a single MCP Server and discover its tools."""
        logger.info(f"正在连接 MCP Server: {config.name} ({config.transport})")

        if config.transport == "stdio":
            server_params = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=config.env if config.env else None,
            )
            transport = stdio_client(server_params)
            read_stream, write_stream = await self._exit_stack.enter_async_context(transport)
        else:
            transport = streamablehttp_client(url=config.url)
            # streamablehttp_client yields 3 values: (read, write, get_session_id)
            read_stream, write_stream, _ = await self._exit_stack.enter_async_context(transport)

        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()

        # Discover tools (with pagination)
        tools = []
        cursor = None
        while True:
            result = await session.list_tools(cursor=cursor)
            tools.extend(result.tools)
            if not result.nextCursor:
                break
            cursor = result.nextCursor

        # Register tools
        registered_count = 0
        for tool in tools:
            full_name = self._make_tool_name(config.name, tool.name)
            if full_name in self._tool_map:
                logger.warning(f"MCP 工具名冲突: {full_name}，跳过来自 '{config.name}' 的 '{tool.name}'")
                continue
            self._tool_map[full_name] = (config.name, tool.name)
            self._tools_schemas.append(self._convert_tool_schema(config.name, tool))
            registered_count += 1

        self._sessions[config.name] = session
        self._timeouts[config.name] = config.timeout
        logger.info(f"MCP Server '{config.name}' 已连接，发现 {registered_count} 个工具")

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Route a tool call to the appropriate MCP Server session."""
        if tool_name not in self._tool_map:
            return f"错误：未知 MCP 工具 '{tool_name}'"

        server_name, original_name = self._tool_map[tool_name]
        session = self._sessions.get(server_name)
        if not session:
            return f"错误：MCP Server '{server_name}' 未连接"

        timeout = self._timeouts.get(server_name, 30.0)
        try:
            result = await session.call_tool(
                original_name,
                arguments,
                read_timeout_seconds=timedelta(seconds=timeout),
            )
            return self._convert_result(result)
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            if len(error_msg) > 200:
                error_msg = error_msg[:200] + "..."
            return f"MCP 工具执行出错: {error_msg}"

    async def disconnect_all(self) -> None:
        """Close all MCP Server connections."""
        try:
            await self._exit_stack.aclose()
        except Exception as e:
            logger.warning(f"MCP 断开连接时出错: {e}")
        self._sessions.clear()
        self._tool_map.clear()
        self._tools_schemas.clear()
        logger.info("所有 MCP Server 已断开连接")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_manager.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/mcp/manager.py tests/test_mcp_manager.py
git commit -m "feat(mcp): implement MCPManager with tool discovery and call routing"
```

---

### Task 3: ToolExecutor MCP Routing

**Files:**
- Modify: `src/tools/tool_executor.py` (lines 7-8, 19, 25-26)

- [ ] **Step 1: Write failing test for MCP routing**

Add to `tests/test_mcp_manager.py`:

```python
import pytest
from unittest.mock import AsyncMock
from src.tools.tool_executor import ToolExecutor


@pytest.mark.asyncio
async def test_tool_executor_routes_mcp_tools():
    """ToolExecutor routes mcp_-prefixed tools to mcp_manager."""
    mock_mcp = AsyncMock()
    mock_mcp.call_tool.return_value = "mcp result"

    executor = ToolExecutor({}, mcp_manager=mock_mcp)
    result = await executor.execute("mcp_filesystem_read_file", {"path": "/tmp/test"})

    mock_mcp.call_tool.assert_called_once_with("mcp_filesystem_read_file", {"path": "/tmp/test"})
    assert result == "mcp result"


@pytest.mark.asyncio
async def test_tool_executor_no_mcp_for_local_tools():
    """Non-mcp_ tools go through normal local execution, not MCP."""
    mock_mcp = AsyncMock()
    executor = ToolExecutor({}, mcp_manager=mock_mcp)
    result = await executor.execute("calculator", {})
    # Should NOT call mcp_manager since tool name has no "mcp_" prefix
    mock_mcp.call_tool.assert_not_called()
    assert "未知工具" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_manager.py::test_tool_executor_routes_mcp_tools -v`
Expected: FAIL (ToolExecutor doesn't accept mcp_manager)

- [ ] **Step 3: Modify ToolExecutor to support MCP routing**

In `src/tools/tool_executor.py`, make these changes:

Change the `__init__` method to accept `mcp_manager`:
```python
class ToolExecutor:
    def __init__(self, registry: Dict[str, Dict[str, Any]], mcp_manager=None):
        """
        registry: 工具注册表，格式为 {name: {"func": callable, "model": BaseModel, "sensitive": bool, ...}}
        mcp_manager: MCPManager 实例，用于路由 MCP 工具调用
        """
        self.registry = registry
        self.mcp_manager = mcp_manager
```

Add MCP routing at the top of the `execute` method, before the existing `if tool_name not in self.registry:` check:
```python
    async def execute(self, tool_name: str, arguments: Dict[str, Any], skip_confirm: bool = False) -> str:
        # MCP 工具路由
        if tool_name.startswith("mcp_") and self.mcp_manager:
            return await self.mcp_manager.call_tool(tool_name, arguments)

        if tool_name not in self.registry:
            return f"错误：未知工具 '{tool_name}'"
        # ... rest unchanged
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/tools/tool_executor.py tests/test_mcp_manager.py
git commit -m "feat(mcp): add MCP routing to ToolExecutor"
```

---

### Task 4: config.py and main.py Integration

**Files:**
- Modify: `config.py` (add MCP_CONFIG_PATH)
- Modify: `main.py` (add MCP initialization and cleanup)

**IMPORTANT:** `main.py` 已实现多智能体协作（MultiAgentFlow），不再使用 ChatFlow。
当前 `main.py` 导入了 `from src.agents import agent_registry, MultiAgentFlow`，
普通对话分支使用 `MultiAgentFlow`，参数名为 `all_tools`。

- [ ] **Step 1: Add MCP_CONFIG_PATH to config.py**

Add after the multi-agent config section (after `SPECIALIST_MAX_RESULT_LENGTH = 500`, line 43):

```python
# MCP 配置
MCP_CONFIG_PATH = os.getenv("MCP_CONFIG_PATH", "mcp_servers.json")
```

- [ ] **Step 2: Modify main.py to initialize MCPManager**

Add new imports at the top (after existing imports):

```python
from src.mcp.config import load_mcp_config
from src.mcp.manager import MCPManager
from config import USER_ID, MCP_CONFIG_PATH
```

(Remove the old `from config import USER_ID` line since it's now combined.)

Replace `async def main()`:

```python
async def main():
    # 初始化 MCP
    mcp_manager = MCPManager()
    await mcp_manager.connect_all(load_mcp_config(MCP_CONFIG_PATH))

    # 注入 MCP 到现有 tool_executor
    tool_executor.mcp_manager = mcp_manager

    # 合并工具列表（本地 tools + MCP tools）
    all_tools = tools + mcp_manager.get_tools_schemas()

    print("Agent 已启动，输入 'exit' 退出。")
    if mcp_manager.get_tools_schemas():
        print(f"已加载 {len(mcp_manager.get_tools_schemas())} 个 MCP 工具")

    try:
        while True:
            user_input = await agent_input("\n你: ")
            if user_input.lower() in ["exit", "quit"]:
                break
            await handle_input(user_input, all_tools)
            await agent_output("\n")
    finally:
        await mcp_manager.disconnect_all()
```

- [ ] **Step 3: Update handle_input to accept all_tools parameter**

Change `handle_input` signature. The function currently uses `tools` in two places:
PlanningFlow (`available_tools=tools`) and MultiAgentFlow (`all_tools=tools`).
Both need to use the merged tool list.

```python
async def handle_input(user_input: str, all_tools=None):
    """统一入口：护栏 → Flow 路由 → 执行"""
    effective_tools = all_tools or tools

    # 1. 护栏检查（unchanged）
    passed, reason = input_guard.check(user_input)
    if not passed:
        await agent_output(f"\n[安全拦截] {reason}\n")
        return

    # 2. 关键词触发的特殊 Flow（如 /book）（unchanged）
    flow = detect_flow(user_input, tool_executor=tool_executor)
    if flow:
        runner = FSMRunner(flow)
        await runner.run()
        return

    # 3. 复杂请求 → PlanningFlow
    if await is_complex_request(user_input):
        planning_flow = PlanningFlow(
            available_tools=effective_tools,
            tool_executor=tool_executor,
        )
        planning_flow.model.data["original_request"] = user_input
        runner = FSMRunner(planning_flow)
        result = await runner.run()
        if result is not None:
            return

    # 4. 普通对话 → MultiAgentFlow（总控 + 专业 Agent）
    multi_agent_flow = MultiAgentFlow(
        registry=agent_registry,
        memory=memory,
        user_facts=user_facts,
        conversation_summaries=conversation_summaries,
        all_tools=effective_tools,
        tool_executor=tool_executor,
    )
    multi_agent_flow.model.data["user_input"] = user_input
    runner = FSMRunner(multi_agent_flow)
    await runner.run()
```

**Important:** Preserve the existing `if __name__ == "__main__": asyncio.run(main())` at the bottom of `main.py`. Only replace the `main()` and `handle_input()` function bodies.

- [ ] **Step 4: Verify the code runs without MCP config (graceful degradation)**

Run: `python main.py`
Expected: Agent starts normally with message "Agent 已启动，输入 'exit' 退出。" (no MCP errors since mcp_servers.json doesn't exist yet)
Type `exit` to quit.

- [ ] **Step 5: Commit**

```bash
git add config.py main.py
git commit -m "feat(mcp): integrate MCPManager into main.py startup and shutdown"
```

---

### Task 5: Example Config and Integration Test

**Files:**
- Create: `mcp_servers.json`

- [ ] **Step 1: Create example mcp_servers.json**

```json
{
  "mcpServers": {
    "filesystem": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "env": {}
    }
  }
}
```

- [ ] **Step 2: Test MCP connection with filesystem server**

Run: `python main.py`
Expected output should include: "已加载 X 个 MCP 工具" (where X is the number of tools from filesystem server)

Ask the agent: "用 MCP 工具读取 /tmp 目录下有什么文件"
Expected: Agent calls `mcp_filesystem_list_directory` or similar tool and returns results.

Type `exit` to quit.
Expected: Clean shutdown, no errors.

- [ ] **Step 3: Test error case — bad server config**

Temporarily edit `mcp_servers.json` to add a broken server:
```json
{
  "mcpServers": {
    "filesystem": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    },
    "broken": {
      "transport": "stdio",
      "command": "nonexistent_command_12345"
    }
  }
}
```

Run: `python main.py`
Expected: Warning about "broken" server connection failure, but filesystem server works normally.

Remove the "broken" entry after testing.

- [ ] **Step 4: Run all unit tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_servers.json
git commit -m "feat(mcp): add example mcp_servers.json config"
```

---

### Task 6: Update __init__.py exports

**Files:**
- Modify: `src/mcp/__init__.py`

- [ ] **Step 1: Finalize __init__.py with proper exports**

The placeholder from Task 1 should already be correct. Verify it imports cleanly:

```python
# src/mcp/__init__.py
from .config import MCPServerConfig, load_mcp_config
from .manager import MCPManager

__all__ = ["MCPServerConfig", "load_mcp_config", "MCPManager"]
```

- [ ] **Step 2: Verify imports work**

Run: `python -c "from src.mcp import MCPManager, load_mcp_config; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Run full test suite one final time**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 4: Commit if any changes**

```bash
git add src/mcp/__init__.py
git commit -m "chore(mcp): finalize package exports"
```
