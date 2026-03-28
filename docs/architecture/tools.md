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

三种实现：
- `LocalToolProvider` — 本地 @tool 装饰器注册的工具
- `MCPToolProvider`（`src/mcp/provider.py`）— MCP 服务器提供的工具
- `SkillToolProvider`（`src/skills/provider.py`）— 技能提供的工具

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

## 数据流

```
LLM 返回 tool_calls
  → AgentRunner 解析
  → ToolRouter.route(name, args)
    → provider.can_handle(name)?
      → LocalToolProvider: middleware → executor → 工具函数
      → MCPToolProvider: MCP 协议调用
  → 结果返回 AgentRunner → 加入消息 → 继续 LLM 对话
```
