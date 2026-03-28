# 工具统一分类与动态 Agent 生成设计

> 日期：2026-03-28
> 状态：设计阶段
> 范围：工具分类流水线、分类配置、动态 Agent 生成、复合工具调用

## 1. 背景与目标

### 当前问题

1. **MCP 工具无分类** — MCP server（如 desktop-commander）可能提供几十个工具，全部暴露给 orchestrator 会导致 LLM 工具选择不准确
2. **Agent-工具绑定是静态的** — `presets.py` 中手动定义每个 agent 持有哪些工具，新增 MCP server 需要手动调整
3. **Local 和 MCP 工具割裂** — 两者本质相同（都是 ToolProvider 提供的能力），但分类和管理方式不同
4. **跨 MCP server 功能重叠** — 不同 MCP server 可能提供相似功能（如多个 server 都有文件操作），缺乏统一归类

### 目标

- 对所有工具（Local + MCP）统一分类，按功能语义分组
- 每个分类不超过 5-8 个工具，保证 LLM 选择准确性
- 分类由 LLM 辅助生成初稿，开发者 review 后确认，写入配置文件持久化
- 运行时按需为每个分类动态创建 Tool Agent
- 业务 Agent 可通过 handoff 或复合工具两种方式调用 Tool Agent

### 设计约束

- 分类是离线操作，不嵌入 main 运行时
- 分类结果存储在独立配置文件中，纳入 git 管理
- 现有 weather_agent / calendar_agent / email_agent 预设将被移除（工具为占位符，未来由对应 MCP 替代）
- 业务 Agent 由开发者手动创建，可 handoff 到 Tool Agent 或通过复合工具调用

## 2. 分类配置文件

### 文件：`tool_categories.json`（项目根目录）

```json
{
  "version": 1,
  "max_tools_per_category": 8,
  "categories": {
    "terminal": {
      "description": "执行命令、管理进程、读取终端输出",
      "tools": [
        "mcp_desktop_commander_execute_command",
        "mcp_desktop_commander_read_output",
        "mcp_desktop_commander_list_processes",
        "mcp_desktop_commander_kill_process"
      ]
    },
    "filesystem": {
      "description": "文件和目录的读写、搜索、管理",
      "tools": [
        "mcp_desktop_commander_read_file",
        "mcp_desktop_commander_write_file",
        "mcp_desktop_commander_list_directory",
        "mcp_desktop_commander_search_files",
        "mcp_desktop_commander_get_file_info"
      ]
    },
    "text_editing": {
      "description": "文本内容的编辑和替换操作",
      "subcategories": {
        "code_editing": {
          "description": "代码文件的精确编辑",
          "tools": [
            "mcp_desktop_commander_edit_block",
            "mcp_desktop_commander_search_code"
          ]
        },
        "document_editing": {
          "description": "文档和配置文件的批量编辑",
          "tools": [
            "mcp_desktop_commander_find_replace",
            "mcp_desktop_commander_patch_file"
          ]
        }
      }
    },
    "calculation": {
      "description": "数学计算",
      "tools": ["calculate"]
    }
  }
}
```

### 格式规则

- **`tools` 与 `subcategories` 互斥**：叶子类别有 `tools`，中间节点有 `subcategories`，不能同时存在
- **工具名全局唯一**：Local 工具用原名（如 `calculate`），MCP 工具用 `mcp_{server}_{tool}` 格式
- **每个工具只属于一个类别**：全覆盖、无重复
- **`description`**：作为对应 Agent 的职责描述，也是 orchestrator 选择 handoff 目标的依据
- **`version`**：配置格式版本号，用于未来升级
- **可选 `instructions` 字段**：开发者可为某个类别自定义 Agent 的 system prompt，覆盖自动生成的模板

## 3. 分类流水线

### 模块：`src/tools/classifier.py`

独立于 agent 运行时的离线脚本，纯 Python 标准库 + 复用框架组件（`MCPManager`、`LLMProvider`、`ToolRegistry`）。

### 流程

```
收集所有工具 schema → 提取 MCP server 自带分类 hint → LLM 扁平分类 → 校验 → 溢出拆分 → 写入配置
```

#### Step 1：收集工具 schema

从两个来源合并为统一列表：

- **Local 工具**：调用 `discover_tools()` 触发 `@tool` 注册，从 `ToolRegistry` 获取 schema
- **MCP 工具**：通过 `MCPManager.connect_all()` 连接所有 MCP server，调用 `get_tools_schemas()` 获取

每个工具提取 `name`、`description`、`parameters` 供 LLM 判断。

#### Step 2：提取 MCP server 自带分类 hint

部分 MCP server 的工具 description 中包含 category 信息（如 `[Configuration]`、`[Filesystem]` 前缀）。用简单规则提取这些 hint，作为 LLM 的参考输入，优先参考但不强制采用。

#### Step 3：LLM 扁平分类

单次 LLM 调用，prompt 要求：

- 按功能语义对所有工具分组
- 每个类别最多 `max_tools_per_category` 个工具
- 来自不同来源但功能相似的工具合并到同一类别
- 参考工具的 `source_hint`（如有）
- 输出 JSON 格式：每个类别包含 `name`（snake_case）、`description`、`tools` 列表

使用 structured output（`response_format=json`）确保格式可靠。

#### Step 4：校验

- 每个工具必须且只能出现在一个类别中
- 类别名合法（英文 snake_case）
- `description` 非空
- 校验失败报错退出，不写入配置

#### Step 5：溢出拆分

遍历分类结果，对工具数 > `max_tools_per_category` 的类别，发起第二次 LLM 调用，将该类别拆分为子类别。拆分结果替换原类别，变为 `subcategories` 结构。

上限为 8 个工具，拆分后每个子类别通常 2-5 个，一次拆分即可，不需要递归。

#### Step 6：写入 `tool_categories.json`

格式化写入，按类别名排序，便于 diff 和 review。

### 增量策略

采用**全量重分类**：LLM 调用成本低（工具列表通常不大），简单可靠。开发者通过 git diff review 变更。暂不实现增量分类。

## 4. 动态 Agent 生成（懒加载）

### 核心原则

**启动时不创建 Tool Agent 实例**。只加载配置、解析类别元数据（name + description），注入 orchestrator 的 instructions。Agent 在首次被 handoff 到时按需创建并缓存。

### CategoryResolver

```python
class CategoryResolver:
    """从 tool_categories.json 解析类别，按需创建 Tool Agent"""

    def __init__(self, categories: dict, tool_router: ToolRouter):
        self._categories = categories   # 叶子类别映射：name → {description, tools}
        self._tool_router = tool_router

    def can_resolve(self, agent_name: str) -> bool:
        """判断是否为已知的工具类别 agent"""
        return agent_name in self._categories

    def create_agent(self, agent_name: str) -> Agent:
        """按需创建 Tool Agent 实例"""
        category = self._categories[agent_name]
        return Agent(
            name=agent_name,
            description=category["description"],
            instructions=build_instructions(category),
            tools=category["tools"],
            handoffs=[],
        )

    def get_all_summaries(self) -> list[dict[str, str]]:
        """返回所有类别的 name + description，供 orchestrator instructions 使用"""
        return [{"name": k, "description": v["description"]}
                for k, v in self._categories.items()]
```

### AgentRegistry 改造

```python
class AgentRegistry:
    def __init__(self, category_resolver: CategoryResolver | None = None):
        self._agents: dict[str, Agent] = {}
        self._category_resolver = category_resolver

    def get(self, name: str) -> Agent:
        if self.has(name):
            return self._agents[name]
        # 懒加载：尝试从分类配置创建
        if self._category_resolver and self._category_resolver.can_resolve(name):
            agent = self._category_resolver.create_agent(name)
            self.register(agent)  # 缓存
            return agent
        raise AgentNotFoundError(name)
```

### Agent 命名与层级

- 叶子类别 → `tool_{category_path}`，用下划线连接层级路径
- 统一 `tool_` 前缀与业务 agent 区分
- 中间节点（有 `subcategories`）不生成 agent
- 示例：
  - `terminal` → `tool_terminal`
  - `text_editing/code_editing` → `tool_text_editing_code_editing`

### Agent instructions 模板

自动生成，包含类别描述 + 工具列表概述。开发者可通过配置中的 `instructions` 字段覆盖。

## 5. 复合工具调用（Delegate Tool）

### 场景

业务 Agent 需要在一次对话中连续调用多个工具类别，且保持自身上下文控制。例如"部署 agent"：先用 terminal 构建 → filesystem 检查产物 → terminal 部署。handoff 会断裂上下文，复合工具调用让业务 agent 像调普通工具一样委派子任务。

### DelegateToolProvider

新增 `ToolProvider` 实现，为每个叶子类别生成一个 `delegate_tool_{name}` 工具：

```python
class DelegateToolProvider(ToolProvider):
    """将 Tool Agent 包装为可调用工具"""

    def can_handle(self, tool_name: str) -> bool:
        if not tool_name.startswith("delegate_"):
            return False
        agent_name = tool_name.removeprefix("delegate_")
        return self._resolver.can_resolve(agent_name)

    def get_schemas(self) -> list[ToolDict]:
        return [build_delegate_schema(name, desc)
                for name, desc in self._resolver.get_all_summaries()]

    async def execute(self, tool_name: str, arguments: dict) -> str:
        agent_name = tool_name.removeprefix("delegate_")
        agent = self._registry.get(agent_name)  # 触发懒加载
        sub_context = self._context.create_child(input=arguments["task"], agent=agent_name)
        result = await self._runner.run(agent, sub_context)
        return result.output
```

每个 delegate 工具的 schema：

```json
{
  "name": "delegate_tool_terminal",
  "description": "委派任务给终端操作专家：执行命令、管理进程、读取终端输出",
  "parameters": {
    "type": "object",
    "properties": {
      "task": {"type": "string", "description": "需要完成的具体任务描述"}
    },
    "required": ["task"]
  }
}
```

### 两种调用方式对比

| 维度 | handoff | delegate tool |
|------|---------|---------------|
| 上下文 | 切换到目标 agent，原 agent 暂停 | 原 agent 保持控制，结果作为 tool result 返回 |
| 适用场景 | 单一任务委派 | 多步编排，步骤间需保持推理 |
| 声明方式 | `handoffs=["tool_terminal"]` | `tools=["delegate_tool_terminal"]` |

业务 Agent 的定义决定使用哪种方式，两者可混用。

## 6. CLI 入口

### 命令

```bash
# 全量分类
uv run python -m src.tools.classify

# 强制重分类（忽略现有配置）
uv run python -m src.tools.classify --force
```

### 执行流程

1. `load_config()` + 加载 `.env`
2. `discover_tools()` → 收集 Local 工具 schema
3. `MCPManager.connect_all()` → 收集 MCP 工具 schema
4. 检查现有 `tool_categories.json`
   - 无 `--force` 且文件存在 → 对比工具列表，无变化则退出
   - 有变化或 `--force` → 继续
5. LLM 分类 → 校验 → 溢出拆分
6. 写入 `tool_categories.json`
7. 打印 diff 摘要，提示开发者 review
8. `MCPManager.disconnect_all()`

### 变化检测

对比配置中所有 `tools` 的并集与当前发现的工具列表，新增或删除工具都算变化。

## 7. Bootstrap 集成

### `bootstrap.py` 改动

```python
async def create_app():
    # ... 现有逻辑 ...

    # 加载分类配置（如果存在）
    category_resolver = load_category_resolver("tool_categories.json", tool_router)

    # AgentRegistry 注入 CategoryResolver，支持懒加载
    agent_registry = AgentRegistry(category_resolver=category_resolver)

    # DelegateToolProvider 加入 ToolRouter
    delegate_provider = DelegateToolProvider(category_resolver, agent_runner, agent_registry)
    tool_router.add_provider(delegate_provider)

    # orchestrator 动态获取所有可 handoff 的目标
    orchestrator = build_orchestrator(
        category_summaries=category_resolver.get_all_summaries(),
        business_agents=business_agent_names,
    )
```

### 现有预设调整

- 移除 `weather_agent`、`calendar_agent`、`email_agent`（占位符）
- `orchestrator` 保留，`handoffs` 改为动态生成
- `planner` 保留，不受分类影响

## 8. 文件结构

```
src/tools/
  classifier.py      # 分类流水线（LLM 调用、校验、溢出拆分）
  classify.py         # CLI 入口（python -m src.tools.classify），包含 main() + if __name__
  categories.py       # CategoryResolver + 配置加载/解析
  delegate.py         # DelegateToolProvider
  router.py           # 现有 ToolRouter（如需新增 add_provider 方法）
  registry.py         # 现有 ToolRegistry
  executor.py         # 现有 ToolExecutor
  middleware.py       # 现有中间件
  decorator.py        # 现有 @tool
  discovery.py        # 现有 discover_tools
  schemas.py          # 现有 ToolDict
  builtin/            # 现有本地工具

tool_categories.json  # 分类配置（项目根目录，纳入 git）
```

## 9. 容错

- **`tool_categories.json` 不存在** → 启动时 warning，所有工具仍通过 ToolRouter 直接使用，只是没有动态 Agent 和 delegate 工具
- **配置引用不存在的工具名** → 启动时 warning 并跳过该工具，不阻断
- **LLM 分类失败** → CLI 报错退出，不覆盖现有配置
- **MCP server 连接失败** → CLI 中跳过该 server 的工具，warning 提示
