# MCP 按需连接重构

## 背景

当前 MCP 模块在应用启动时通过 `MCPManager.connect_all()` 一次性连接所有配置的 MCP Server，无论本次会话是否用到。这导致：

- 启动延迟：每个 server 连接需要数秒
- 资源浪费：未使用的 server 占用进程和连接
- 脆弱性：某个 server 不可用会拖慢整体启动

## 目标

- 启动时零 MCP 连接，配置只加载不连接
- 当 tool agent 被激活且其工具包含 MCP 工具时，才连接对应的 server
- 分类时（`classify.py`）缓存工具描述到 `tool_categories.json`，运行时无需连接即可完成 orchestrator 路由
- 不考虑向后兼容，直接替换

## 设计

### 两层信息分离

| 层级 | 需要的信息 | 来源 | 是否需要 MCP 连接 |
|------|-----------|------|------------------|
| Orchestrator 路由 | category description + tool description | `tool_categories.json` | 否 |
| Tool Agent 执行 | 完整 schema（参数定义） | MCP server `list_tools()` | 是（按需） |

### 1. `tool_categories.json` 格式变更

`tools` 字段从 `list[str]` 改为 `dict[str, str]`（工具名 → 一行描述）。分类 LLM 同时生成更详尽的 category description。

```json
{
  "version": 2,
  "max_tools_per_category": 8,
  "categories": {
    "file_operations": {
      "description": "文件与目录管理：读取、写入、创建、移动文件和目录，获取文件元信息",
      "tools": {
        "mcp_desktop_commander_read_file": "Read complete contents of a file at the specified path",
        "mcp_desktop_commander_write_file": "Create or overwrite a file with new content",
        "mcp_desktop_commander_create_directory": "Create a new directory at the specified path",
        "mcp_desktop_commander_list_directory": "List contents of a directory",
        "mcp_desktop_commander_move_file": "Move or rename a file or directory",
        "mcp_desktop_commander_get_file_info": "Get metadata about a file (size, timestamps, etc.)"
      }
    }
  }
}
```

### 2. MCPManager 重构

#### 新的初始化方式

构造函数接收 configs 列表，存储但不连接：

```python
class MCPManager:
    def __init__(self, configs: list[MCPServerConfig] | None = None):
        self._exit_stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tool_map: dict[str, tuple[str, str]] = {}
        self._timeouts: dict[str, float] = {}
        self._tools_schemas: list[dict] = []
        # 新增：配置存储（safe_name → config）
        self._configs: dict[str, MCPServerConfig] = {}
        if configs:
            for cfg in configs:
                safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", cfg.name)
                self._configs[safe_name] = cfg
```

#### API 变更

| 方法 | 行为 |
|------|------|
| `__init__(configs)` | 存储配置，不连接 |
| `connect_server(server_name)` | 连接单个 server（幂等：已连接则跳过） |
| `ensure_servers_for_tools(tool_names)` | 从工具名提取 server 名，连接未连接的 server |
| `connect_all(connect_timeout)` | 连接所有已配置的 server（保留给 `classify.py`） |
| `call_tool(tool_name, arguments)` | 不变，路由到已连接 session |
| `disconnect_all()` | 不变，清理所有连接 |
| `get_tools_schemas()` | 不变，返回已连接 server 的 schema |

#### `ensure_servers_for_tools` 实现逻辑

从工具名 `mcp_{safe_server}_{tool}` 中提取 `safe_server` 部分，与 `_configs` 中的 key 匹配。匹配策略：遍历 `_configs` 的 key（按长度降序，确保最长前缀优先匹配），找到工具名以 `mcp_{key}_` 开头的 config。对于未连接的 server，调用 `connect_server()`。

```python
async def ensure_servers_for_tools(self, tool_names: list[str]) -> None:
    needed: set[str] = set()
    # 按 key 长度降序排列，确保最长前缀优先匹配
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

### 3. MCPToolProvider 变更

无 API 变化。`can_handle()` 仍检查 `mcp_` 前缀，`execute()` 仍委托 `MCPManager.call_tool()`，`get_schemas()` 仍返回 `MCPManager.get_tools_schemas()`。

由于 `get_schemas()` 只返回已连接 server 的 schema，在 server 连接前返回空列表。这不影响 orchestrator 路由（orchestrator 看的是 delegate tool schema，不是 MCP tool schema）。当 tool agent 运行时，`ensure_servers_for_tools` 已被调用，schema 可用。

### 4. Bootstrap 变更

```python
# 旧代码
mcp_manager = MCPManager()
await mcp_manager.connect_all(load_mcp_config(mcp_config_path))
if mcp_manager.get_tools_schemas():
    tool_router.add_provider(MCPToolProvider(mcp_manager))

# 新代码
mcp_configs = load_mcp_config(mcp_config_path)
mcp_manager = MCPManager(configs=mcp_configs)
if mcp_configs:
    tool_router.add_provider(MCPToolProvider(mcp_manager))
```

MCPToolProvider 在有 MCP 配置时始终注册，不再依赖 schema 发现结果。

### 5. DelegateToolProvider 变更

新增 `_mcp_manager` 依赖。在 `execute()` 中，运行 tool agent 前确保 MCP 连接：

```python
class DelegateToolProvider:
    def __init__(self, resolver, runner, registry, deps, mcp_manager=None):
        # ... 现有字段 ...
        self._mcp_manager = mcp_manager

    async def execute(self, tool_name, arguments):
        agent_name = tool_name[len(DELEGATE_PREFIX):]
        agent = self._registry.get(agent_name)
        if agent is None:
            return f"错误：找不到 agent {agent_name}"

        # 按需连接 MCP server
        if self._mcp_manager:
            mcp_tools = [t for t in agent.tools if t.startswith("mcp_")]
            if mcp_tools:
                await self._mcp_manager.ensure_servers_for_tools(mcp_tools)

        sub_ctx = RunContext(input=arguments.get("task", ""), ...)
        result = await self._runner.run(agent, sub_ctx)
        return result.text
```

Bootstrap 中传入 `mcp_manager`：

```python
delegate_provider = DelegateToolProvider(
    resolver=category_resolver,
    runner=runner,
    registry=agent_registry,
    deps=deps,
    mcp_manager=mcp_manager,
)
```

### 6. 分类流程变更（classify.py + classifier.py）

#### classify.py

- `_build_output()` 接收 `all_schemas` 参数，将每个 tool 的 description 写入输出
- `_collect_tools()` 适配 `dict` 格式（取 keys）
- `detect_changes()` 适配 `dict` 格式

#### classifier.py

- `build_classify_prompt()` 要求 LLM 在输出中同时包含每个工具的 description
- 输出格式变为：

```json
{
  "categories": [{
    "name": "file_operations",
    "description": "详细的类别描述...",
    "tools": {
      "mcp_desktop_commander_read_file": "Read file contents at path",
      "mcp_desktop_commander_write_file": "Write content to file"
    }
  }]
}
```

- `parse_classify_response()` 和 `parse_split_response()` 适配新格式

### 7. CategoryResolver 及相关类型变更

```python
class CategoryEntry(TypedDict, total=False):
    description: Required[str]
    tools: Required[dict[str, str]]  # 从 list[str] 改为 dict[str, str]
    instructions: str
```

消费者适配：

| 位置 | 变化 |
|------|------|
| `CategoryResolver.build_instructions()` | `tool_names` 从 `cat["tools"]` 改为 `cat["tools"].keys()` |
| `AgentRegistry.get()` | `tools=list(cat["tools"].keys())` |
| `validate_categories()` | 遍历 `cat["tools"].keys()` 而非 `cat["tools"]` |
| `_flatten_categories()` | `"tools": list(cat["tools"])` 改为 `"tools": dict(cat["tools"])` |

## 运行时流程

```
App 启动
  └→ MCPManager(configs=[...])  # 存配置，零连接
  └→ ToolRouter 注册 MCPToolProvider（空 schema）
  └→ load_categories() 加载 tool_categories.json

用户: "读取 X 文件"
  └→ Orchestrator 看到 delegate_tool_file_operations（category description 路由）
  └→ DelegateToolProvider.execute("delegate_tool_file_operations", {task: "..."})
      └→ agent.tools = ["mcp_desktop_commander_read_file", ...]
      └→ ensure_servers_for_tools(["mcp_desktop_commander_read_file", ...])
          └→ 提取 safe_name = "desktop_commander"
          └→ connect_server("desktop_commander") → 连接 + 发现 schema
      └→ AgentRunner.run()
          └→ _build_tools() → tool_router.get_all_schemas() → 包含已连接 server 的 schema
          └→ LLM 选择 mcp_desktop_commander_read_file
          └→ tool_router.route() → MCPToolProvider.execute() → MCPManager.call_tool()
```

## 不在范围内

- 空闲超时断开：保持简单，连接后复用直到 app shutdown
- Schema 缓存到磁盘：不需要，按需连接时自然获得 schema
- MCP server 热重载：配置变更需重启或重新分类
