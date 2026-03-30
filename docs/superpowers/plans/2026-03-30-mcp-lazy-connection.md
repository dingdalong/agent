# MCP 按需连接重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 MCP 模块从启动时全量连接改为按需懒连接，消除启动延迟和资源浪费。

**Architecture:** 分类时缓存工具描述到 `tool_categories.json`（tools 字段从 `list[str]` 改为 `dict[str, str]`）。MCPManager 启动时只存配置不连接，当 DelegateToolProvider 激活 tool agent 时，根据工具名反推所需 server 并按需连接。

**Tech Stack:** Python 3.13, asyncio, MCP SDK (`mcp` package)

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `src/mcp/manager.py` | MCPManager: 新增 configs 存储、`connect_server`、`ensure_servers_for_tools` |
| Modify | `src/mcp/__init__.py` | 更新 exports |
| Modify | `src/tools/categories.py` | `CategoryEntry.tools` 改为 `dict[str, str]`，适配所有消费方法 |
| Modify | `src/tools/classifier.py` | LLM prompt/parse 输出 tools dict 格式 |
| Modify | `src/tools/classify.py` | `_build_output` 输出 tool descriptions，`_collect_tools`/`detect_changes` 适配 dict |
| Modify | `src/tools/delegate.py` | 新增 `_mcp_manager` 参数，execute 中按需连接 |
| Modify | `src/app/bootstrap.py` | 不再 `connect_all`，传 configs 给 MCPManager，传 mcp_manager 给 DelegateToolProvider |
| Modify | `src/agents/registry.py` | `agent.tools = list(cat["tools"].keys())` |
| Modify | `tests/mcp/test_manager.py` | 新增 configs 存储、`connect_server`、`ensure_servers_for_tools` 测试 |
| Modify | `tests/tools/test_categories.py` | 所有 fixture 和断言适配 dict 格式 |
| Modify | `tests/tools/test_classifier.py` | prompt/parse 测试适配新 tools dict 格式 |
| Modify | `tests/tools/test_classify_cli.py` | `_build_output`/`detect_changes` 测试适配 dict 格式 |
| Modify | `tests/tools/test_delegate.py` | fixture 适配 dict 格式，新增 MCP 按需连接测试 |

---

### Task 1: MCPManager — configs 存储与 `connect_server`

**Files:**
- Modify: `src/mcp/manager.py:19-28` (init) and add `connect_server` method
- Test: `tests/mcp/test_manager.py`

- [ ] **Step 1: Write failing tests for configs storage and `connect_server`**

在 `tests/mcp/test_manager.py` 末尾追加：

```python
from src.mcp.config import MCPServerConfig


def test_init_with_configs_stores_by_safe_name():
    """构造时传入 configs，按 safe_name 存储，不连接。"""
    configs = [
        MCPServerConfig(name="desktop-commander", transport="stdio", command="npx"),
        MCPServerConfig(name="my.api", transport="http", url="http://localhost:8080"),
    ]
    mgr = MCPManager(configs=configs)
    assert "desktop_commander" in mgr._configs
    assert "my_api" in mgr._configs
    assert mgr._sessions == {}  # 未连接


def test_init_without_configs():
    """无参构造仍然可用。"""
    mgr = MCPManager()
    assert mgr._configs == {}
    assert mgr._sessions == {}


@pytest.mark.asyncio
async def test_connect_server_idempotent():
    """已连接的 server 再次调用 connect_server 不会重复连接。"""
    configs = [
        MCPServerConfig(name="test-server", transport="stdio", command="echo"),
    ]
    mgr = MCPManager(configs=configs)
    # 手动注入一个 fake session 来模拟已连接状态
    mgr._sessions["test_server"] = "fake_session"
    await mgr.connect_server("test_server")
    # session 不变，说明没有重新连接
    assert mgr._sessions["test_server"] == "fake_session"


def test_connect_server_unknown_name_raises():
    """传入未知 server name 应报错。"""
    mgr = MCPManager()
    import asyncio
    with pytest.raises(KeyError):
        asyncio.get_event_loop().run_until_complete(mgr.connect_server("nonexistent"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/mcp/test_manager.py::test_init_with_configs_stores_by_safe_name tests/mcp/test_manager.py::test_init_without_configs tests/mcp/test_manager.py::test_connect_server_idempotent tests/mcp/test_manager.py::test_connect_server_unknown_name_raises -v`
Expected: FAIL — `MCPManager()` 不接受 `configs` 参数

- [ ] **Step 3: Implement configs storage and `connect_server`**

修改 `src/mcp/manager.py`。`__init__` 改为接受 `configs` 参数，新增 `connect_server` 方法：

```python
class MCPManager:
    """Manages MCP Server connections, tool discovery, and tool call routing."""

    def __init__(self, configs: list[MCPServerConfig] | None = None):
        self._exit_stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tool_map: dict[str, tuple[str, str]] = {}  # full_name -> (server_name, original_name)
        self._timeouts: dict[str, float] = {}
        self._tools_schemas: list[dict] = []
        # 配置存储（safe_name -> config），不立即连接
        self._configs: dict[str, MCPServerConfig] = {}
        if configs:
            for cfg in configs:
                safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", cfg.name)
                self._configs[safe_name] = cfg

    async def connect_server(self, safe_name: str) -> None:
        """按需连接单个 MCP Server（幂等：已连接则跳过）。

        Args:
            safe_name: server 的 safe name（与 _configs 的 key 一致）。

        Raises:
            KeyError: safe_name 不在 _configs 中。
        """
        if safe_name in self._sessions:
            return
        config = self._configs[safe_name]
        await self._connect_one(config)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mcp/test_manager.py::test_init_with_configs_stores_by_safe_name tests/mcp/test_manager.py::test_init_without_configs tests/mcp/test_manager.py::test_connect_server_idempotent tests/mcp/test_manager.py::test_connect_server_unknown_name_raises -v`
Expected: PASS

- [ ] **Step 5: Run all existing MCPManager tests to verify no regression**

Run: `uv run pytest tests/mcp/test_manager.py -v`
Expected: All PASS（现有测试中 `MCPManager()` 无参构造仍兼容）

- [ ] **Step 6: Commit**

```bash
git add src/mcp/manager.py tests/mcp/test_manager.py
git commit -m "feat(mcp): MCPManager 支持 configs 存储和 connect_server 按需连接"
```

---

### Task 2: MCPManager — `ensure_servers_for_tools`

**Files:**
- Modify: `src/mcp/manager.py` (add `ensure_servers_for_tools`)
- Test: `tests/mcp/test_manager.py`

- [ ] **Step 1: Write failing tests for `ensure_servers_for_tools`**

在 `tests/mcp/test_manager.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_ensure_servers_for_tools_connects_needed():
    """ensure_servers_for_tools 根据工具名前缀，连接所需但未连接的 server。"""
    configs = [
        MCPServerConfig(name="desktop-commander", transport="stdio", command="npx"),
        MCPServerConfig(name="another-server", transport="stdio", command="echo"),
    ]
    mgr = MCPManager(configs=configs)
    # 模拟 connect_server 避免真实连接
    connected: list[str] = []

    async def fake_connect(safe_name: str) -> None:
        if safe_name not in mgr._configs:
            raise KeyError(safe_name)
        mgr._sessions[safe_name] = "fake"
        connected.append(safe_name)

    mgr.connect_server = fake_connect  # type: ignore[assignment]

    await mgr.ensure_servers_for_tools([
        "mcp_desktop_commander_read_file",
        "mcp_desktop_commander_write_file",
        "calculator",  # 非 MCP 工具，应忽略
    ])
    assert connected == ["desktop_commander"]
    assert "another_server" not in connected


@pytest.mark.asyncio
async def test_ensure_servers_for_tools_skips_connected():
    """已连接的 server 不会重复连接。"""
    configs = [
        MCPServerConfig(name="desktop-commander", transport="stdio", command="npx"),
    ]
    mgr = MCPManager(configs=configs)
    mgr._sessions["desktop_commander"] = "already_connected"

    connected: list[str] = []

    async def fake_connect(safe_name: str) -> None:
        connected.append(safe_name)

    mgr.connect_server = fake_connect  # type: ignore[assignment]

    await mgr.ensure_servers_for_tools(["mcp_desktop_commander_read_file"])
    assert connected == []  # 不应连接


@pytest.mark.asyncio
async def test_ensure_servers_for_tools_longest_prefix_match():
    """当存在 server name 前缀包含关系时，使用最长前缀匹配。"""
    configs = [
        MCPServerConfig(name="foo", transport="stdio", command="echo"),
        MCPServerConfig(name="foo-bar", transport="stdio", command="echo"),
    ]
    mgr = MCPManager(configs=configs)
    connected: list[str] = []

    async def fake_connect(safe_name: str) -> None:
        if safe_name not in mgr._configs:
            raise KeyError(safe_name)
        mgr._sessions[safe_name] = "fake"
        connected.append(safe_name)

    mgr.connect_server = fake_connect  # type: ignore[assignment]

    # mcp_foo_bar_some_tool 应匹配 foo_bar（长）而非 foo（短）
    await mgr.ensure_servers_for_tools(["mcp_foo_bar_some_tool"])
    assert connected == ["foo_bar"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/mcp/test_manager.py::test_ensure_servers_for_tools_connects_needed tests/mcp/test_manager.py::test_ensure_servers_for_tools_skips_connected tests/mcp/test_manager.py::test_ensure_servers_for_tools_longest_prefix_match -v`
Expected: FAIL — `ensure_servers_for_tools` 不存在

- [ ] **Step 3: Implement `ensure_servers_for_tools`**

在 `src/mcp/manager.py` 的 `MCPManager` 类中，`connect_server` 方法之后添加：

```python
    async def ensure_servers_for_tools(self, tool_names: list[str]) -> None:
        """根据工具名前缀，按需连接所需的 MCP Server。

        从工具名 mcp_{safe_server}_{tool} 中提取 safe_server，
        与 _configs 的 key 匹配（最长前缀优先），连接未连接的 server。
        """
        needed: set[str] = set()
        sorted_keys = sorted(self._configs.keys(), key=len, reverse=True)
        for tool_name in tool_names:
            if not tool_name.startswith("mcp_"):
                continue
            for safe_name in sorted_keys:
                if tool_name.startswith(f"mcp_{safe_name}_"):
                    if safe_name not in self._sessions:
                        needed.add(safe_name)
                    break
        for safe_name in needed:
            await self.connect_server(safe_name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mcp/test_manager.py::test_ensure_servers_for_tools_connects_needed tests/mcp/test_manager.py::test_ensure_servers_for_tools_skips_connected tests/mcp/test_manager.py::test_ensure_servers_for_tools_longest_prefix_match -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mcp/manager.py tests/mcp/test_manager.py
git commit -m "feat(mcp): 新增 ensure_servers_for_tools 按需连接"
```

---

### Task 3: MCPManager — 重构 `connect_all` 使用 configs

**Files:**
- Modify: `src/mcp/manager.py:73-81` (`connect_all` method)
- Test: `tests/mcp/test_manager.py`

- [ ] **Step 1: Write failing test for new `connect_all` behavior**

在 `tests/mcp/test_manager.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_connect_all_uses_stored_configs():
    """connect_all 不再需要 configs 参数，使用构造时存储的配置。"""
    configs = [
        MCPServerConfig(name="server-a", transport="stdio", command="echo"),
        MCPServerConfig(name="server-b", transport="stdio", command="echo"),
    ]
    mgr = MCPManager(configs=configs)
    connected: list[str] = []

    async def fake_connect_one(config: MCPServerConfig) -> None:
        safe = re.sub(r"[^a-zA-Z0-9_]", "_", config.name)
        mgr._sessions[safe] = "fake"
        connected.append(config.name)

    mgr._connect_one = fake_connect_one  # type: ignore[assignment]

    await mgr.connect_all()
    assert set(connected) == {"server-a", "server-b"}
```

注意需要在测试文件顶部加 `import re`。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp/test_manager.py::test_connect_all_uses_stored_configs -v`
Expected: FAIL — `connect_all()` 仍需要 `configs` 参数

- [ ] **Step 3: Refactor `connect_all` to use stored configs**

修改 `src/mcp/manager.py` 中的 `connect_all`：

```python
    async def connect_all(self, connect_timeout: float = 30.0) -> None:
        """连接所有已配置的 MCP Server。主要供 classify.py 等离线工具使用。

        失败的连接会被记录并跳过，不会中断其他 server 的连接。
        """
        for config in self._configs.values():
            try:
                await asyncio.wait_for(self._connect_one(config), timeout=connect_timeout)
            except asyncio.TimeoutError:
                logger.warning(f"MCP Server '{config.name}' 连接超时 ({connect_timeout}s)，跳过")
            except Exception as e:
                logger.warning(f"MCP Server '{config.name}' 连接失败: {e}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp/test_manager.py::test_connect_all_uses_stored_configs -v`
Expected: PASS

- [ ] **Step 5: Run all MCPManager tests**

Run: `uv run pytest tests/mcp/test_manager.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/mcp/manager.py tests/mcp/test_manager.py
git commit -m "refactor(mcp): connect_all 使用存储的 configs，不再接受参数"
```

---

### Task 4: `tool_categories.json` 格式变更 — `CategoryEntry.tools` 改为 `dict[str, str]`

**Files:**
- Modify: `src/tools/categories.py:18-20` (CategoryEntry), `:26-88` (load/flatten/validate), `:143-190` (CategoryResolver)
- Test: `tests/tools/test_categories.py`

- [ ] **Step 1: Update test fixtures to use dict format**

修改 `tests/tools/test_categories.py`，所有 fixture 和测试中的 `"tools": [...]` 改为 `"tools": {...}`：

```python
"""tool_categories.json 配置加载与校验测试。"""
import json
import pytest
from pathlib import Path


@pytest.fixture
def valid_config(tmp_path: Path) -> Path:
    config = {
        "version": 2,
        "max_tools_per_category": 8,
        "categories": {
            "terminal": {
                "description": "终端操作",
                "tools": {
                    "execute_command": "Execute a shell command",
                    "read_output": "Read command output",
                },
            },
            "calculation": {
                "description": "数学计算",
                "tools": {"calculate": "Perform math calculations"},
            },
        },
    }
    p = tmp_path / "tool_categories.json"
    p.write_text(json.dumps(config), encoding="utf-8")
    return p


@pytest.fixture
def nested_config(tmp_path: Path) -> Path:
    config = {
        "version": 2,
        "max_tools_per_category": 8,
        "categories": {
            "text_editing": {
                "description": "文本编辑",
                "subcategories": {
                    "code_editing": {
                        "description": "代码编辑",
                        "tools": {
                            "edit_block": "Edit a code block",
                            "search_code": "Search code content",
                        },
                    },
                    "document_editing": {
                        "description": "文档编辑",
                        "tools": {"find_replace": "Find and replace text"},
                    },
                },
            },
        },
    }
    p = tmp_path / "tool_categories.json"
    p.write_text(json.dumps(config), encoding="utf-8")
    return p


def test_load_categories_valid(valid_config: Path):
    from src.tools.categories import load_categories
    result = load_categories(str(valid_config))
    assert "tool_terminal" in result
    assert result["tool_terminal"]["description"] == "终端操作"
    assert result["tool_terminal"]["tools"] == {
        "execute_command": "Execute a shell command",
        "read_output": "Read command output",
    }
    assert "tool_calculation" in result


def test_load_categories_nested(nested_config: Path):
    from src.tools.categories import load_categories
    result = load_categories(str(nested_config))
    assert "tool_text_editing" not in result
    assert "tool_text_editing_code_editing" in result
    assert result["tool_text_editing_code_editing"]["tools"] == {
        "edit_block": "Edit a code block",
        "search_code": "Search code content",
    }
    assert "tool_text_editing_document_editing" in result


def test_load_categories_missing_file():
    from src.tools.categories import load_categories
    result = load_categories("/nonexistent/path.json")
    assert result == {}


def test_validate_categories_all_tools_covered():
    from src.tools.categories import validate_categories
    categories = {
        "tool_a": {"description": "A", "tools": {"t1": "desc1", "t2": "desc2"}},
        "tool_b": {"description": "B", "tools": {"t3": "desc3"}},
    }
    errors = validate_categories(categories, {"t1", "t2", "t3"})
    assert errors == []


def test_validate_categories_missing_tools():
    from src.tools.categories import validate_categories
    categories = {"tool_a": {"description": "A", "tools": {"t1": "desc1"}}}
    errors = validate_categories(categories, {"t1", "t2"})
    assert any("t2" in e for e in errors)


def test_validate_categories_duplicate_tools():
    from src.tools.categories import validate_categories
    categories = {
        "tool_a": {"description": "A", "tools": {"t1": "desc1", "t2": "desc2"}},
        "tool_b": {"description": "B", "tools": {"t2": "desc2"}},
    }
    errors = validate_categories(categories, {"t1", "t2"})
    assert any("t2" in e for e in errors)


def test_validate_categories_unknown_tools():
    from src.tools.categories import validate_categories
    categories = {"tool_a": {"description": "A", "tools": {"t1": "desc1", "unknown": "desc"}}}
    errors = validate_categories(categories, {"t1"})
    assert any("unknown" in e for e in errors)


def test_flatten_categories_instructions_passthrough(tmp_path: Path):
    """instructions 字段应完整透传到叶子节点条目。"""
    import json
    from src.tools.categories import load_categories

    config = {
        "categories": {
            "terminal": {
                "description": "终端操作",
                "tools": {"execute_command": "Execute command"},
                "instructions": "只在必要时使用",
            }
        }
    }
    p = tmp_path / "tool_categories.json"
    p.write_text(json.dumps(config), encoding="utf-8")

    result = load_categories(p)
    assert "tool_terminal" in result
    assert result["tool_terminal"].get("instructions") == "只在必要时使用"


def test_validate_categories_invalid_snake_case_name():
    """类别名包含大写字母或连字符时，应产生校验错误。"""
    from src.tools.categories import validate_categories

    categories = {
        "tool_Bad-Name": {"description": "错误命名示例", "tools": {"t1": "desc"}},
    }
    errors = validate_categories(categories, {"t1"})
    assert any("Bad-Name" in e or "tool_Bad-Name" in e for e in errors)


# ---------------------------------------------------------------------------
# CategoryResolver 测试
# ---------------------------------------------------------------------------


def test_category_resolver_can_resolve():
    """can_resolve 对已知类别返回 True，未知类别返回 False。"""
    from src.tools.categories import CategoryResolver

    cats = {"tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute", "read": "Read"}}}
    resolver = CategoryResolver(cats)
    assert resolver.can_resolve("tool_terminal") is True
    assert resolver.can_resolve("tool_unknown") is False


def test_category_resolver_get_category():
    """get_category 返回原始 CategoryEntry，不存在时返回 None。"""
    from src.tools.categories import CategoryResolver

    cats = {"tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute", "read": "Read"}}}
    resolver = CategoryResolver(cats)

    cat = resolver.get_category("tool_terminal")
    assert cat is not None
    assert cat["description"] == "终端操作"
    assert cat["tools"] == {"exec": "Execute", "read": "Read"}

    assert resolver.get_category("tool_unknown") is None


def test_category_resolver_build_instructions_default():
    """无自定义 instructions 时，使用模板自动生成。"""
    from src.tools.categories import CategoryResolver

    cats = {"tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute", "read": "Read"}}}
    resolver = CategoryResolver(cats)
    instructions = resolver.build_instructions("tool_terminal")

    assert "终端操作" in instructions
    assert "exec" in instructions
    assert "read" in instructions


def test_category_resolver_build_instructions_custom():
    """有自定义 instructions 时，直接使用而非模板。"""
    from src.tools.categories import CategoryResolver

    cats = {
        "tool_terminal": {
            "description": "终端操作",
            "tools": {"exec": "Execute"},
            "instructions": "自定义指令",
        }
    }
    resolver = CategoryResolver(cats)
    assert resolver.build_instructions("tool_terminal") == "自定义指令"


def test_category_resolver_build_instructions_unknown_raises():
    """对未知类别调用 build_instructions 应抛出 KeyError。"""
    from src.tools.categories import CategoryResolver

    resolver = CategoryResolver({})
    with pytest.raises(KeyError):
        resolver.build_instructions("tool_nonexistent")


def test_category_resolver_get_all_summaries():
    """get_all_summaries 返回所有类别的 name 和 description。"""
    from src.tools.categories import CategoryResolver

    cats = {
        "tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute"}},
        "tool_calc": {"description": "计算", "tools": {"calc": "Calculate"}},
    }
    resolver = CategoryResolver(cats)
    summaries = resolver.get_all_summaries()

    assert len(summaries) == 2
    names = {s["name"] for s in summaries}
    assert names == {"tool_terminal", "tool_calc"}
    descs = {s["description"] for s in summaries}
    assert descs == {"终端操作", "计算"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/tools/test_categories.py -v`
Expected: FAIL — `CategoryEntry` 和相关函数仍期望 `list[str]`

- [ ] **Step 3: Update `CategoryEntry` type and all methods in `categories.py`**

修改 `src/tools/categories.py`：

1. `CategoryEntry.tools` 从 `list[str]` 改为 `dict[str, str]`：

```python
class CategoryEntry(TypedDict, total=False):
    """叶子类别条目，包含 description、tools（name -> description），以及可选的 instructions。"""

    description: Required[str]
    tools: Required[dict[str, str]]
    instructions: str
```

2. `_flatten_categories` 中叶子节点构建改为 dict：

```python
            # 叶子节点
            entry: CategoryEntry = {
                "description": cat["description"],
                "tools": dict(cat["tools"]),
            }
```

3. `validate_categories` 中遍历改为 `.keys()`：

```python
        for tool_name in cat.get("tools", {}).keys():
```

和全覆盖校验：

```python
    missing = all_tool_names - categorized_tools
```

4. `CategoryResolver.build_instructions` 中 `tool_names` 取 keys：

```python
    def build_instructions(self, agent_name: str) -> str:
        cat = self._categories[agent_name]
        return cat.get("instructions") or _TOOL_AGENT_INSTRUCTIONS_TEMPLATE.format(
            description=cat["description"],
            tool_names="、".join(cat["tools"].keys()),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/tools/test_categories.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/tools/categories.py tests/tools/test_categories.py
git commit -m "refactor(categories): tools 字段从 list[str] 改为 dict[str, str]"
```

---

### Task 5: AgentRegistry 适配 dict tools

**Files:**
- Modify: `src/agents/registry.py:57-64`
- Test: `tests/tools/test_delegate.py` (已依赖 registry 懒加载)

- [ ] **Step 1: Update delegate test fixture to dict format**

修改 `tests/tools/test_delegate.py` 中 `resolver` fixture：

```python
@pytest.fixture
def resolver():
    cats = {
        "tool_terminal": {"description": "终端操作", "tools": {"exec": "Execute command"}},
        "tool_calc": {"description": "数学计算", "tools": {"calc": "Calculate math"}},
    }
    return CategoryResolver(cats)
```

- [ ] **Step 2: Run delegate tests to verify they fail**

Run: `uv run pytest tests/tools/test_delegate.py -v`
Expected: FAIL — `AgentRegistry.get()` 调用 `list(cat["tools"])` 拿到的是 dict keys 的列表（实际上 `list(dict)` 返回 keys，所以可能碰巧通过）。但让我们确认行为正确。

- [ ] **Step 3: Update `AgentRegistry.get()` to use dict keys**

修改 `src/agents/registry.py:61`：

```python
            agent = Agent(
                name=name,
                description=cat["description"],
                instructions=instructions,
                tools=list(cat["tools"].keys()),
                handoffs=[],
            )
```

注意：`list(dict)` 和 `list(dict.keys())` 结果相同，但显式使用 `.keys()` 更清晰表达意图。

- [ ] **Step 4: Run delegate tests to verify they pass**

Run: `uv run pytest tests/tools/test_delegate.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/agents/registry.py tests/tools/test_delegate.py
git commit -m "refactor(registry): 适配 CategoryEntry.tools dict 格式"
```

---

### Task 6: 分类流水线适配 — classifier.py

**Files:**
- Modify: `src/tools/classifier.py:55-86` (build_classify_prompt), `:93-116` (parse_classify_response), `:123-145` (build_split_prompt), `:152-172` (parse_split_response)
- Test: `tests/tools/test_classifier.py`

- [ ] **Step 1: Update classifier tests for dict tools format**

重写 `tests/tools/test_classifier.py`：

```python
"""工具分类流水线测试。"""
import json
import pytest
from unittest.mock import AsyncMock


def test_extract_category_hints():
    from src.tools.classifier import extract_category_hints
    schemas = [
        {"function": {"name": "t1", "description": "[Filesystem] Read a file", "parameters": {}}},
        {"function": {"name": "t2", "description": "[Terminal] Run command", "parameters": {}}},
        {"function": {"name": "t3", "description": "Calculate math", "parameters": {}}},
    ]
    hints = extract_category_hints(schemas)
    assert hints["t1"] == "Filesystem"
    assert hints["t2"] == "Terminal"
    assert "t3" not in hints


def test_build_classify_prompt():
    from src.tools.classifier import build_classify_prompt
    schemas = [
        {"function": {"name": "read_file", "description": "读取文件", "parameters": {}}},
        {"function": {"name": "calculate", "description": "数学计算", "parameters": {}}},
    ]
    hints = {"read_file": "Filesystem"}
    prompt = build_classify_prompt(schemas, hints, max_per_category=8)
    assert "read_file" in prompt
    assert "calculate" in prompt
    assert "Filesystem" in prompt
    assert "8" in prompt


def test_parse_classify_response_valid():
    from src.tools.classifier import parse_classify_response
    raw = json.dumps({
        "categories": [
            {
                "name": "filesystem",
                "description": "文件操作",
                "tools": {"read_file": "Read a file", "write_file": "Write a file"},
            },
            {
                "name": "calculation",
                "description": "计算",
                "tools": {"calculate": "Do math"},
            },
        ]
    })
    result = parse_classify_response(raw)
    assert "tool_filesystem" in result
    assert result["tool_filesystem"]["tools"] == {"read_file": "Read a file", "write_file": "Write a file"}
    assert "tool_calculation" in result


def test_parse_classify_response_code_block():
    from src.tools.classifier import parse_classify_response
    raw = '```json\n{"categories": [{"name": "test", "description": "d", "tools": {"t1": "Tool 1"}}]}\n```'
    result = parse_classify_response(raw)
    assert "tool_test" in result


def test_parse_classify_response_invalid_json():
    from src.tools.classifier import parse_classify_response
    with pytest.raises(ValueError, match="JSON"):
        parse_classify_response("not json")


def test_build_split_prompt():
    from src.tools.classifier import build_split_prompt
    category = {
        "description": "文件操作",
        "tools": {f"t{i}": f"Tool {i}" for i in range(10)},
    }
    prompt = build_split_prompt("filesystem", category, max_per_category=8)
    assert "filesystem" in prompt
    assert "t0" in prompt
    assert "8" in prompt


def test_parse_split_response_valid():
    from src.tools.classifier import parse_split_response
    raw = json.dumps({
        "subcategories": [
            {"name": "group_a", "description": "A 组", "tools": {"t0": "Tool 0", "t1": "Tool 1"}},
            {"name": "group_b", "description": "B 组", "tools": {"t2": "Tool 2"}},
        ]
    })
    result = parse_split_response(raw)
    assert "group_a" in result
    assert result["group_a"]["tools"] == {"t0": "Tool 0", "t1": "Tool 1"}


@pytest.mark.asyncio
async def test_classify_tools_pipeline():
    from src.tools.classifier import classify_tools
    from src.llm.types import LLMResponse

    schemas = [
        {"function": {"name": "read_file", "description": "读取文件", "parameters": {}}},
        {"function": {"name": "write_file", "description": "写入文件", "parameters": {}}},
        {"function": {"name": "calculate", "description": "数学计算", "parameters": {}}},
    ]
    llm_response = json.dumps({
        "categories": [
            {
                "name": "filesystem",
                "description": "文件操作",
                "tools": {"read_file": "读取文件", "write_file": "写入文件"},
            },
            {
                "name": "calculation",
                "description": "计算",
                "tools": {"calculate": "数学计算"},
            },
        ]
    })
    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock(return_value=LLMResponse(content=llm_response, finish_reason="stop"))
    result = await classify_tools(schemas, mock_llm, max_per_category=8)
    assert "tool_filesystem" in result
    assert "tool_calculation" in result
    assert result["tool_filesystem"]["tools"] == {"read_file": "读取文件", "write_file": "写入文件"}


@pytest.mark.asyncio
async def test_classify_tools_with_overflow_split():
    from src.tools.classifier import classify_tools
    from src.llm.types import LLMResponse

    schemas = [{"function": {"name": f"t{i}", "description": f"Tool {i}", "parameters": {}}} for i in range(10)]
    first_response = json.dumps({
        "categories": [
            {
                "name": "big_group",
                "description": "所有工具",
                "tools": {f"t{i}": f"Tool {i}" for i in range(10)},
            },
        ]
    })
    split_response = json.dumps({
        "subcategories": [
            {"name": "group_a", "description": "A 组", "tools": {f"t{i}": f"Tool {i}" for i in range(5)}},
            {"name": "group_b", "description": "B 组", "tools": {f"t{i}": f"Tool {i}" for i in range(5, 10)}},
        ]
    })
    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock(side_effect=[
        LLMResponse(content=first_response, finish_reason="stop"),
        LLMResponse(content=split_response, finish_reason="stop"),
    ])
    result = await classify_tools(schemas, mock_llm, max_per_category=5)
    assert "tool_big_group" not in result
    assert "tool_big_group_group_a" in result
    assert "tool_big_group_group_b" in result
    assert len(result["tool_big_group_group_a"]["tools"]) == 5


@pytest.mark.asyncio
async def test_classify_tools_empty_schemas():
    from src.tools.classifier import classify_tools
    mock_llm = AsyncMock()
    result = await classify_tools([], mock_llm)
    assert result == {}
    mock_llm.chat.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/tools/test_classifier.py -v`
Expected: FAIL — parse 函数仍期望 tools 是 list

- [ ] **Step 3: Update classifier.py**

修改 `src/tools/classifier.py`：

1. `build_classify_prompt` — 更新输出格式说明，要求 LLM 输出 tools 为 dict：

```python
def build_classify_prompt(
    schemas: list[dict[str, Any]],
    hints: dict[str, str],
    max_per_category: int,
) -> str:
    """构建发送给 LLM 的工具分类 prompt。"""
    tool_lines: list[str] = []
    for schema in schemas:
        func = schema.get("function", {})
        name = func.get("name", "")
        description = func.get("description", "")
        hint = hints.get(name)
        hint_str = f" (hint: {hint})" if hint else ""
        tool_lines.append(f"- {name}: {description}{hint_str}")

    tools_block = "\n".join(tool_lines)

    return (
        "你是一个工具分类专家。请将以下工具分成若干类别。\n\n"
        f"工具列表：\n{tools_block}\n\n"
        "约束：\n"
        f"- 每个类别最多包含 {max_per_category} 个工具\n"
        "- 合并功能相似的工具到同一类别\n"
        "- 类别名使用 snake_case\n"
        "- 每个工具只能属于一个类别\n"
        "- 类别描述要详细，概述该类别包含的所有功能\n\n"
        "请以 JSON 格式输出，格式如下：\n"
        '{"categories": [{"name": "...", "description": "详细的类别描述", '
        '"tools": {"tool_name": "tool_description", ...}}]}\n\n'
        "tools 字段是一个对象，key 是工具名，value 是工具描述。\n"
        "只输出 JSON，不要输出其他内容。"
    )
```

2. `parse_classify_response` — tools 改为 dict：

```python
def parse_classify_response(raw: str) -> dict[str, dict[str, Any]]:
    """解析 LLM 的分类响应 JSON。"""
    data = _extract_json(raw)

    if "categories" not in data:
        raise ValueError("JSON 缺少 'categories' 字段")

    result: dict[str, dict[str, Any]] = {}
    for cat in data["categories"]:
        name = cat["name"]
        key = f"tool_{name}"
        result[key] = {
            "description": cat["description"],
            "tools": dict(cat["tools"]),
        }
    return result
```

3. `build_split_prompt` — 输出 tools 列表用于可读性，要求返回 dict：

```python
def build_split_prompt(
    category_name: str,
    category: dict[str, Any],
    max_per_category: int,
) -> str:
    """构建拆分超额类别的 prompt。"""
    tools_str = ", ".join(
        f"{name}: {desc}" for name, desc in category["tools"].items()
    )
    return (
        f"类别 \"{category_name}\" 包含太多工具，请将其拆分为更小的子类别。\n\n"
        f"类别描述：{category['description']}\n"
        f"工具列表：{tools_str}\n\n"
        "约束：\n"
        f"- 每个子类别最多包含 {max_per_category} 个工具\n"
        "- 子类别名使用 snake_case\n"
        "- 每个工具只能属于一个子类别\n"
        "- 子类别描述要详细\n\n"
        "请以 JSON 格式输出，格式如下：\n"
        '{"subcategories": [{"name": "...", "description": "...", '
        '"tools": {"tool_name": "tool_description", ...}}]}\n\n'
        "只输出 JSON，不要输出其他内容。"
    )
```

4. `parse_split_response` — tools 改为 dict：

```python
def parse_split_response(raw: str) -> dict[str, dict[str, Any]]:
    """解析拆分响应 JSON。"""
    data = _extract_json(raw)

    if "subcategories" not in data:
        raise ValueError("JSON 缺少 'subcategories' 字段")

    result: dict[str, dict[str, Any]] = {}
    for sub in data["subcategories"]:
        name = sub["name"]
        result[name] = {
            "description": sub["description"],
            "tools": dict(sub["tools"]),
        }
    return result
```

5. `classify_tools` 中溢出检测改为 `len(cat["tools"])`（dict 的 len 返回 key 数量，不需要改）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/tools/test_classifier.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/tools/classifier.py tests/tools/test_classifier.py
git commit -m "refactor(classifier): LLM 分类输出 tools 为 dict 格式"
```

---

### Task 7: 分类 CLI 适配 — classify.py

**Files:**
- Modify: `src/tools/classify.py:28-72` (_collect_tools, _build_output, detect_changes)
- Test: `tests/tools/test_classify_cli.py`

- [ ] **Step 1: Update classify CLI tests for dict format**

重写 `tests/tools/test_classify_cli.py`：

```python
"""CLI 分类入口测试。"""
import json
import pytest
from pathlib import Path


def test_detect_changes_no_existing_config():
    from src.tools.classify import detect_changes
    changed, added, removed = detect_changes({"t1", "t2", "t3"}, None)
    assert changed is True
    assert added == {"t1", "t2", "t3"}
    assert removed == set()


def test_detect_changes_no_change(tmp_path: Path):
    from src.tools.classify import detect_changes
    config = {
        "version": 2,
        "max_tools_per_category": 8,
        "categories": {
            "cat_a": {"description": "A", "tools": {"t1": "desc1", "t2": "desc2"}},
            "cat_b": {"description": "B", "tools": {"t3": "desc3"}},
        },
    }
    config_path = tmp_path / "tool_categories.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    changed, added, removed = detect_changes({"t1", "t2", "t3"}, str(config_path))
    assert changed is False
    assert added == set()
    assert removed == set()


def test_detect_changes_new_tools(tmp_path: Path):
    from src.tools.classify import detect_changes
    config = {
        "version": 2,
        "max_tools_per_category": 8,
        "categories": {"cat_a": {"description": "A", "tools": {"t1": "desc1"}}},
    }
    config_path = tmp_path / "tool_categories.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    changed, added, removed = detect_changes({"t1", "t2"}, str(config_path))
    assert changed is True
    assert added == {"t2"}


def test_detect_changes_removed_tools(tmp_path: Path):
    from src.tools.classify import detect_changes
    config = {
        "version": 2,
        "max_tools_per_category": 8,
        "categories": {"cat_a": {"description": "A", "tools": {"t1": "desc1", "t2": "desc2"}}},
    }
    config_path = tmp_path / "tool_categories.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    changed, added, removed = detect_changes({"t1"}, str(config_path))
    assert changed is True
    assert removed == {"t2"}


def test_detect_changes_with_nested_config(tmp_path: Path):
    from src.tools.classify import detect_changes
    config = {
        "version": 2,
        "max_tools_per_category": 8,
        "categories": {
            "text_editing": {
                "description": "编辑",
                "subcategories": {
                    "code": {"description": "代码编辑", "tools": {"t1": "desc1"}},
                    "doc": {"description": "文档编辑", "tools": {"t2": "desc2"}},
                },
            },
        },
    }
    config_path = tmp_path / "tool_categories.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    changed, added, removed = detect_changes({"t1", "t2"}, str(config_path))
    assert changed is False


def test_build_output():
    from src.tools.classify import _build_output
    categories = {
        "tool_terminal": {"description": "终端", "tools": {"exec": "Execute", "read": "Read output"}},
        "tool_calc": {"description": "计算", "tools": {"calc": "Calculate"}},
    }
    output = _build_output(categories, max_per_category=8)
    assert output["version"] == 2
    assert output["max_tools_per_category"] == 8
    assert "terminal" in output["categories"]
    assert "calc" in output["categories"]
    assert output["categories"]["terminal"]["tools"] == {"exec": "Execute", "read": "Read output"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/tools/test_classify_cli.py -v`
Expected: FAIL — `_collect_tools` 和 `_build_output` 仍使用 list 格式

- [ ] **Step 3: Update classify.py**

修改 `src/tools/classify.py`：

1. `_collect_tools` — 适配 dict（取 keys）：

```python
def _collect_tools(categories: dict, out: set[str]) -> None:
    """递归收集配置中所有工具名。"""
    for cat in categories.values():
        if "tools" in cat:
            tools = cat["tools"]
            if isinstance(tools, dict):
                out.update(tools.keys())
            else:
                out.update(tools)
        if "subcategories" in cat:
            _collect_tools(cat["subcategories"], out)
```

2. `_build_output` — 输出 dict 格式，version 改为 2：

```python
def _build_output(
    categories: dict[str, dict[str, Any]],
    max_per_category: int,
) -> dict[str, Any]:
    """将叶子映射转为输出 JSON 格式。"""
    raw_categories: dict[str, Any] = {}
    for agent_name, cat in sorted(categories.items()):
        path = agent_name.removeprefix("tool_")
        raw_categories[path] = {
            "description": cat["description"],
            "tools": dict(cat["tools"]),
        }
    return {
        "version": 2,
        "max_tools_per_category": max_per_category,
        "categories": raw_categories,
    }
```

3. `run_classify` 中 MCP 连接部分改为使用新的 MCPManager 初始化方式：

```python
    # 2. MCP tools
    mcp_schemas: list[dict] = []
    mcp_manager = None
    try:
        from src.mcp.config import load_mcp_config
        from src.mcp.manager import MCPManager

        mcp_config_path = raw.get("mcp", {}).get("config_path", "mcp_servers.json")
        mcp_configs = load_mcp_config(mcp_config_path)
        mcp_manager = MCPManager(configs=mcp_configs)
        await mcp_manager.connect_all()
        mcp_schemas = mcp_manager.get_tools_schemas()
    except Exception:
        logger.warning("MCP 连接失败，仅使用本地工具", exc_info=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/tools/test_classify_cli.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/tools/classify.py tests/tools/test_classify_cli.py
git commit -m "refactor(classify): 适配 dict tools 格式，version 升至 2"
```

---

### Task 8: DelegateToolProvider — 注入 MCPManager 并按需连接

**Files:**
- Modify: `src/tools/delegate.py:32-91`
- Test: `tests/tools/test_delegate.py`

- [ ] **Step 1: Write failing test for MCP lazy connection in delegate**

在 `tests/tools/test_delegate.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_execute_ensures_mcp_connection():
    """execute 应在运行 agent 前确保 MCP server 已连接。"""
    from src.tools.delegate import DelegateToolProvider
    from src.agents.agent import AgentResult

    cats = {
        "tool_files": {
            "description": "文件操作",
            "tools": {
                "mcp_desktop_commander_read_file": "Read file",
                "mcp_desktop_commander_write_file": "Write file",
            },
        },
    }
    test_resolver = CategoryResolver(cats)

    test_registry = AgentRegistry()
    test_registry.set_category_resolver(test_resolver)

    test_runner = AsyncMock()
    test_runner.run = AsyncMock(return_value=AgentResult(text="done"))

    test_deps = AgentDeps()

    mock_mcp = AsyncMock()
    mock_mcp.ensure_servers_for_tools = AsyncMock()

    provider = DelegateToolProvider(
        resolver=test_resolver,
        runner=test_runner,
        registry=test_registry,
        deps=test_deps,
        mcp_manager=mock_mcp,
    )
    await provider.execute("delegate_tool_files", {"task": "read something"})
    mock_mcp.ensure_servers_for_tools.assert_called_once_with([
        "mcp_desktop_commander_read_file",
        "mcp_desktop_commander_write_file",
    ])


@pytest.mark.asyncio
async def test_execute_no_mcp_manager_still_works():
    """没有 mcp_manager 时（纯本地工具）execute 仍正常工作。"""
    from src.tools.delegate import DelegateToolProvider
    from src.agents.agent import AgentResult

    cats = {
        "tool_calc": {"description": "计算", "tools": {"calc": "Calculate"}},
    }
    test_resolver = CategoryResolver(cats)
    test_registry = AgentRegistry()
    test_registry.set_category_resolver(test_resolver)
    test_runner = AsyncMock()
    test_runner.run = AsyncMock(return_value=AgentResult(text="42"))
    test_deps = AgentDeps()

    provider = DelegateToolProvider(
        resolver=test_resolver,
        runner=test_runner,
        registry=test_registry,
        deps=test_deps,
    )
    result = await provider.execute("delegate_tool_calc", {"task": "1+1"})
    assert result == "42"
```

同时需要在文件顶部追加导入：

```python
from src.agents.registry import AgentRegistry
from src.agents.deps import AgentDeps
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/tools/test_delegate.py::test_execute_ensures_mcp_connection tests/tools/test_delegate.py::test_execute_no_mcp_manager_still_works -v`
Expected: FAIL — `DelegateToolProvider.__init__` 不接受 `mcp_manager` 参数

- [ ] **Step 3: Update DelegateToolProvider**

修改 `src/tools/delegate.py`：

1. 添加 TYPE_CHECKING import：

```python
if TYPE_CHECKING:
    from src.agents.deps import AgentDeps
    from src.agents.registry import AgentRegistry
    from src.agents.runner import AgentRunner
    from src.mcp.manager import MCPManager
    from src.tools.categories import CategoryResolver
```

2. `__init__` 新增 `mcp_manager` 参数：

```python
    def __init__(
        self,
        resolver: CategoryResolver,
        runner: AgentRunner,
        registry: AgentRegistry,
        deps: AgentDeps,
        mcp_manager: MCPManager | None = None,
    ) -> None:
        self._resolver = resolver
        self._runner = runner
        self._registry = registry
        self._deps = deps
        self._mcp_manager = mcp_manager
```

3. `execute` 中添加按需连接逻辑：

```python
    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """委派执行：按需连接 MCP server，创建子 RunContext 并驱动 AgentRunner。"""
        from src.agents.context import DynamicState, RunContext

        agent_name = tool_name[len(DELEGATE_PREFIX):]
        agent = self._registry.get(agent_name)
        if agent is None:
            return f"错误：找不到 agent {agent_name}"

        # 按需连接该 agent 所需的 MCP server
        if self._mcp_manager:
            mcp_tools = [t for t in agent.tools if t.startswith("mcp_")]
            if mcp_tools:
                await self._mcp_manager.ensure_servers_for_tools(mcp_tools)

        sub_ctx: RunContext = RunContext(
            input=arguments.get("task", ""),
            state=DynamicState(),
            deps=self._deps,
        )
        result = await self._runner.run(agent, sub_ctx)
        return result.text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/tools/test_delegate.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/tools/delegate.py tests/tools/test_delegate.py
git commit -m "feat(delegate): 注入 MCPManager，execute 中按需连接 MCP server"
```

---

### Task 9: Bootstrap 适配 — 移除启动连接

**Files:**
- Modify: `src/app/bootstrap.py:76-81` (MCP section), `:169-179` (delegate provider section)

- [ ] **Step 1: Update bootstrap MCP section**

修改 `src/app/bootstrap.py` 中 MCP 部分（第 76-81 行）：

```python
    # 3. MCP — 只加载配置，不连接。连接在 DelegateToolProvider.execute 中按需触发
    mcp_config_path = raw.get("mcp", {}).get("config_path", "mcp_servers.json")
    mcp_configs = load_mcp_config(mcp_config_path)
    mcp_manager = MCPManager(configs=mcp_configs)
    if mcp_configs:
        tool_router.add_provider(MCPToolProvider(mcp_manager))
```

- [ ] **Step 2: Update bootstrap DelegateToolProvider section**

修改 `src/app/bootstrap.py` 第 172-179 行，传入 `mcp_manager`：

```python
    if category_resolver:
        delegate_provider = DelegateToolProvider(
            resolver=category_resolver,
            runner=runner,
            registry=agent_registry,
            deps=deps,
            mcp_manager=mcp_manager,
        )
        tool_router.add_provider(delegate_provider)
```

- [ ] **Step 3: Run full test suite to verify no regression**

Run: `uv run pytest -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/app/bootstrap.py
git commit -m "feat(bootstrap): MCP 启动时零连接，配置传递给 DelegateToolProvider"
```

---

### Task 10: 更新现有 `tool_categories.json` 并运行全量测试

**Files:**
- Modify: `tool_categories.json` (update to version 2 dict format)

- [ ] **Step 1: Delete existing `tool_categories.json`**

现有文件使用旧格式（tools 为 list），需要重新分类生成新格式。删除旧文件：

```bash
rm tool_categories.json
```

- [ ] **Step 2: Run classification to regenerate**

```bash
uv run python -m src.tools.classify --force
```

Expected: 生成新的 `tool_categories.json`，version 为 2，tools 为 dict 格式。

如果 MCP server 不可用，可以手动创建一个最小版本用于测试：

```json
{
  "version": 2,
  "max_tools_per_category": 8,
  "categories": {
    "calculation": {
      "description": "Basic mathematical calculation tool",
      "tools": {
        "calculator": "Perform basic math calculations"
      }
    }
  }
}
```

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add tool_categories.json
git commit -m "chore: 重新生成 tool_categories.json（version 2，dict 格式）"
```

---

### Task 11: 更新文档

**Files:**
- Modify: `docs/superpowers/specs/2026-03-28-tool-classification-design.md` (if it references old format)

- [ ] **Step 1: Check and update any docs referencing old tools list format**

检查 `docs/` 下是否有引用 `tool_categories.json` 旧格式的文档，更新描述。特别关注：
- `CLAUDE.md` 中的常用命令部分（无需改动，命令不变）
- `docs/superpowers/specs/2026-03-28-tool-classification-design.md` 如果引用了 tools 格式

- [ ] **Step 2: Commit**

```bash
git add docs/
git commit -m "docs: 更新文档反映 MCP 按需连接和 tool_categories v2 格式"
```

---

### Task 12: 最终验证

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest -v
```

Expected: All PASS

- [ ] **Step 2: Quick smoke test — start app**

```bash
uv run python main.py
```

Expected: 启动无 MCP 连接日志，输入触发 MCP 工具时才看到连接日志。

- [ ] **Step 3: Verify MCP lazy connection works**

在 app 中输入一个需要 MCP 工具的请求（如文件操作），确认：
1. 此时才出现 MCP 连接日志
2. 工具调用成功返回结果
3. 后续调用同一 server 不重复连接
