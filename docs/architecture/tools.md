# 工具系统

## 职责

提供统一的工具注册、发现、执行和路由机制。通过中间件管道支持错误处理、敏感操作确认、输出截断等横切关注点。

## 核心组件

### ToolProvider Protocol（`src/tools/router.py`）

```python
class ToolProvider(Protocol):
    def can_handle(self, tool_name: str) -> bool: ...
    async def execute(self, tool_name: str, arguments: dict) -> str: ...
    def get_schemas(self) -> list[ToolDict]: ...
```

四种实现：
- `LocalToolProvider` — 本地 @tool 装饰器注册的工具
- `MCPToolProvider`（`src/mcp/provider.py`）— MCP 服务器提供的工具
- `SkillToolProvider`（`src/skills/provider.py`）— 技能提供的工具
- `DelegateToolProvider`（`src/tools/delegate.py`）— 将 Tool Agent 包装为可调用工具，支持 `delegate_<agent_name>(task=...)` 形式的复合调用

### ToolRouter（`src/tools/router.py`）

按注册顺序查询 provider，找到第一个 `can_handle` 返回 `True` 的执行。聚合所有 provider 的 schema 供 LLM 使用。

### @tool 装饰器（`src/tools/decorator.py`）

```python
@tool(model=CalculateInput, description="数学计算")
async def calculate(args: CalculateInput) -> str:
    ...
```

自动从 Pydantic model 生成 JSON Schema，注册到全局 `ToolRegistry`。

### 中间件管道（`src/tools/middleware.py`）

按顺序执行的中间件链：
1. `error_handler_middleware` — 捕获异常，返回错误信息
2. `sensitive_confirm_middleware` — 标记为 sensitive 的工具执行前请求用户确认
3. `truncate_middleware` — 截断超长输出

### ToolExecutor（`src/tools/executor.py`）

用 Pydantic model 验证参数，然后调用工具函数。

### 工具发现（`src/tools/discovery.py`）

启动时自动扫描指定路径，导入所有工具模块，触发 @tool 装饰器注册。

## 内置工具

| 工具 | 文件 | 功能 |
|------|------|------|
| `calculate` | `src/tools/builtin/calculator.py` | AST 安全数学计算 |

文件操作（读写、编辑、搜索等）由 MCP 服务 `desktop-commander` 提供，不再作为内置工具。

## 工具分类系统

### 配置格式（`tool_categories.json`）

存储每个工具的分类结果，结构为工具名到类别信息的映射：

```json
{
  "calculate": { "category": "math", "description": "数学计算" },
  "delegate_weather": { "category": "weather", "description": "天气查询" }
}
```

文件由分类流水线生成，运行时由 CategoryResolver 读取。

### 分类流水线（`src/tools/classifier.py`）

使用 LLM 对所有已注册工具按功能分组，输出写入 `tool_categories.json`。通过 CLI 触发：

```bash
uv run python -m src.tools.classify          # 增量分类（跳过已有条目）
uv run python -m src.tools.classify --force  # 强制重分类所有工具
```

### CategoryResolver（`src/tools/categories.py`）

启动时读取 `tool_categories.json`，按类别将工具分组。当某类别被实际调用时，按需创建对应的 Tool Agent（懒加载），避免预先实例化所有 Agent。

### DelegateToolProvider（`src/tools/delegate.py`）

将 Tool Agent 包装为标准 ToolProvider，对外暴露 `delegate_<agent_name>(task=...)` 形式的工具。两种调用方式：

- **handoff**：主 Agent 将控制权完全移交给 Tool Agent，适合长流程子任务
- **delegate tool**：主 Agent 通过工具调用方式委托，Tool Agent 返回结果后主 Agent 继续

## 数据流

```
LLM 返回 tool_calls
  → AgentRunner 解析
  → ToolRouter.route(name, args)
    → provider.can_handle(name)?
      → LocalToolProvider: middleware → executor → 工具函数
      → MCPToolProvider: MCP 协议调用
      → DelegateToolProvider: 创建子 RunContext → AgentRunner 驱动子 Agent
  → 结果返回 AgentRunner → 加入消息 → 继续 LLM 对话
```
