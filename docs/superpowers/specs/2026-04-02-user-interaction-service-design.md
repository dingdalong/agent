# UserInteractionService + UserInputToolProvider 设计文档

## 概述

为框架新增统一的用户交互能力：让 agent 在工具循环中主动向用户提问，同时统一框架内所有用户交互（包括敏感工具确认）的并行安全和展示一致性。

## 背景

当前框架中，`AgentRunner` 的工具循环没有 agent 主动向用户提问的机制。唯一的用户交互点是 `sensitive_confirm_middleware`，它直接持有 `ui` 引用，没有并行保护，且与新的 `ask_user` 工具机制完全割裂。

在多智能体并行执行（`parallel_delegate`）场景下，多个 agent 可能同时需要用户输入，必须有统一的串行化保护。

## 设计目标

1. 让所有 agent 能在工具循环中主动向用户提问
2. 统一框架内所有用户交互的并行安全（共享 Lock）
3. 保持一致的用户交互展示格式
4. 为未来的选项/按钮交互留好扩展接口
5. 最小化改动，不影响现有架构

## 架构分层

```
Layer 0: UserInteractionService (src/utils/interaction.py)
         └─ 依赖: UserInterface 协议 (src/interfaces/base.py)

Layer 1: UserInputToolProvider (src/tools/user_input.py)
         └─ 依赖: UserInteractionService, ToolDict (Layer 0)

         sensitive_confirm_middleware (src/tools/middleware.py) [已有，小改]
         └─ 依赖: UserInteractionService, ToolRegistry (Layer 0)

Layer 3: bootstrap.py (src/app/bootstrap.py) [组装]
         └─ 创建 UserInteractionService，注入到 middleware 和 provider
```

## 组件设计

### 1. UserInteractionService

**文件**: `src/utils/interaction.py`

**职责**: 统一的用户交互入口，封装 `asyncio.Lock` 保证同一时刻只有一个交互在进行。

**接口**:

```python
class UserInteractionService:
    """统一的用户交互服务。

    所有需要向用户提问的组件（工具确认、agent 提问等）
    都通过此服务交互，保证并行安全和展示一致性。
    """

    def __init__(self, ui: UserInterface) -> None:
        self._ui = ui
        self._lock = asyncio.Lock()

    async def ask(self, question: str, source: str = "") -> str:
        """向用户提出开放式问题，返回用户的自由文本回答。

        Args:
            question: 要向用户提出的问题
            source: 提问者标识（如 agent 名称），用于展示
        """
        async with self._lock:
            label = f"[{source}] " if source else ""
            await self._ui.display(f"\n🤖 {label}提问: {question}")
            return await self._ui.prompt("你的回答: ")

    async def confirm(self, message: str) -> bool:
        """向用户请求是/否确认。

        Args:
            message: 确认提示信息
        """
        async with self._lock:
            await self._ui.display(f"\n⚠️  是否允许{message}？")
            return await self._ui.confirm("")
```

**未来扩展**（不在本次实现范围）:

```python
    async def select(self, question: str, options: list[str], source: str = "") -> str:
        """从选项中选择。CLI 用数字选择，Web/App 渲染按钮。"""
```

### 2. UserInputToolProvider

**文件**: `src/tools/user_input.py`

**职责**: 实现 `ToolProvider` 协议，暴露 `ask_user` 工具供 LLM 在工具循环中调用。

**接口**:

```python
class UserInputToolProvider:
    """让 agent 能主动向用户提问的 ToolProvider。

    实现 ToolProvider 协议，注册到 ToolRouter 后，
    agent 可通过调用 ask_user 工具向用户提出问题。
    """

    def __init__(self, interaction: UserInteractionService) -> None:
        self._interaction = interaction

    def can_handle(self, tool_name: str) -> bool:
        return tool_name == "ask_user"

    def get_schemas(self) -> list[ToolDict]:
        return [{
            "type": "function",
            "function": {
                "name": "ask_user",
                "description": (
                    "当你需要用户提供额外信息、做出选择或确认时调用此工具。"
                    "请确保问题清晰具体，避免模糊的提问。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "要向用户提出的问题",
                        },
                    },
                    "required": ["question"],
                },
            },
        }]

    async def execute(
        self, tool_name: str, arguments: dict, context: Any = None,
    ) -> str:
        question = arguments.get("question", "")
        if not question:
            return "错误：question 参数不能为空"
        source = ""
        if context is not None:
            source = getattr(context, "current_agent", "")
        return await self._interaction.ask(question, source=source)
```

### 3. sensitive_confirm_middleware 改造

**文件**: `src/tools/middleware.py`

**改动**: 将参数 `ui` 改为 `interaction: UserInteractionService`，内部调用 `interaction.confirm()` 代替直接调用 `ui.display()` + `ui.confirm()`。

**改前**:
```python
def sensitive_confirm_middleware(registry: ToolRegistry, ui) -> Middleware:
    async def middleware(name: str, args: dict, next_fn: NextFn) -> str:
        entry = registry.get(name)
        if entry and entry.sensitive:
            # ... 构建 msg ...
            await ui.display(f"\n⚠️  是否允许{msg}？\n")
            confirmed = await ui.confirm("")
            if not confirmed:
                return "用户取消了操作"
        return await next_fn(name, args)
    return middleware
```

**改后**:
```python
def sensitive_confirm_middleware(
    registry: ToolRegistry, interaction: UserInteractionService,
) -> Middleware:
    async def middleware(name: str, args: dict, next_fn: NextFn) -> str:
        entry = registry.get(name)
        if entry and entry.sensitive:
            # ... 构建 msg ...
            confirmed = await interaction.confirm(msg)
            if not confirmed:
                return "用户取消了操作"
        return await next_fn(name, args)
    return middleware
```

### 4. bootstrap.py 组装变更

```python
from src.utils.interaction import UserInteractionService
from src.tools.user_input import UserInputToolProvider

# 创建统一交互服务
interaction = UserInteractionService(ui)

# middleware 传 interaction 而非 ui
middlewares = [
    error_handler_middleware(),
    sensitive_confirm_middleware(registry, interaction),
    truncate_middleware(raw.get("tools", {}).get("max_output_length", 2000)),
]

# 注册 UserInputToolProvider
tool_router.add_provider(UserInputToolProvider(interaction))
```

## 执行流程

### Agent 主动提问

```
AgentRunner.run() 工具循环
  → LLM 返回 tool_call: ask_user(question="你想要什么格式？")
  → ToolRouter.route("ask_user", args, context)
  → UserInputToolProvider.execute()
    → interaction.ask("你想要什么格式？", source="orchestrator")
      → 获取 Lock
      → ui.display("🤖 [orchestrator] 提问: 你想要什么格式？")
      → answer = ui.prompt("你的回答: ")
      → 释放 Lock
      → return answer
  → 答案作为 tool result 回到 messages
  → LLM 继续推理
```

### 敏感工具确认

```
AgentRunner.run() 工具循环
  → LLM 返回 tool_call: delete_file(path="/data/important.csv")
  → ToolRouter.route("delete_file", args, context)
  → LocalToolProvider.execute() → pipeline
    → sensitive_confirm_middleware
      → interaction.confirm("执行敏感操作: delete_file")
        → 获取 Lock（如果此时有 ask_user 在进行，会等待）
        → ui.display("⚠️  是否允许执行敏感操作: delete_file？")
        → confirmed = ui.confirm("")
        → 释放 Lock
      → 用户确认 → 继续执行
      → 用户拒绝 → return "用户取消了操作"
```

### 并行场景

```
parallel_delegate 同时执行 agent_a 和 agent_b
  → agent_a 调用 ask_user("问题A")
    → interaction.ask() 获取 Lock ✅
    → 用户回答
    → 释放 Lock
  → agent_b 调用 ask_user("问题B")（同时发起）
    → interaction.ask() 等待 Lock... ⏳
    → agent_a 释放后获取 Lock ✅
    → 用户回答
    → 释放 Lock
```

### 5. ask_user 工具的可见性

**问题**: `AgentRunner._build_tools()` 按 `agent.tools` 白名单过滤工具 schema。如果 agent 的 `tools` 列表不含 `"ask_user"`，agent 就看不到此工具。

**当前 agent 工具来源**:
- **CategoryResolver 懒加载的 agent**（`registry.py:66`）: `tools = list(cat["tools"].keys()) + delegate_names`
- **预设 orchestrator**（`presets.py:82`）: `tools=[]`（空，只用 handoff）
- **planner**（`presets.py:23`）: `tools=[]`

**方案**: 在 `AgentRunner._build_tools()` 中引入「系统工具」概念 — 无论 agent.tools 白名单如何，始终包含 `ask_user`。

**改动**（`src/agents/runner.py`）:

```python
# _build_tools 中，在白名单过滤后追加系统工具
SYSTEM_TOOLS = {"ask_user"}

def _build_tools(self, agent: Agent, context: RunContext) -> list[dict]:
    tool_router = getattr(context.deps, "tool_router", None)
    if not tool_router:
        return []
    all_schemas = tool_router.get_all_schemas()

    if not agent.tools:
        # agent 没有声明工具时，只返回系统工具
        return [s for s in all_schemas if s["function"]["name"] in SYSTEM_TOOLS]

    allowed = set(agent.tools) | SYSTEM_TOOLS
    if context.delegate_depth >= 1:
        allowed = {name for name in allowed if not name.startswith("delegate_")}
    return [s for s in all_schemas if s["function"]["name"] in allowed]
```

这样所有 agent（包括无工具声明的 orchestrator 和 planner）都能使用 `ask_user`，未来新增系统工具只需改 `SYSTEM_TOOLS` 集合。

## 文件变更清单

| 文件 | 动作 | 说明 |
|------|------|------|
| `src/utils/interaction.py` | **新建** | UserInteractionService |
| `src/tools/user_input.py` | **新建** | UserInputToolProvider |
| `src/tools/middleware.py` | **小改** | sensitive_confirm 改用 interaction |
| `src/app/bootstrap.py` | **小改** | 创建 service，注册 provider |
| `src/agents/runner.py` | **小改** | _build_tools 支持系统工具 |

## 不在范围内

- 不改 `UserInterface` 协议（现有 `prompt`/`display`/`confirm` 足够）
- 不改 `ToolRouter`、`ToolProvider` 协议
- 不改 `@tool` 装饰器或 `AgentDeps`
- 不实现选项/按钮交互（接口已预留 `select()` 方法）
- 不实现 EventBus 事件发送（后续可按需添加 `UserAsked`/`UserAnswered` 事件）

## 未来扩展路径

1. **选项交互**: `UserInteractionService` 添加 `select()` 方法，`ask_user` Schema 添加 `options` 字段，`UserInterface` 协议添加 `select()` 方法
2. **Web/App UI**: 实现新的 `UserInterface`（WebSocket 推送问题，接收回答），`UserInteractionService` 无需改动
3. **EventBus 集成**: 发送 `UserInteractionRequested` / `UserInteractionCompleted` 事件，供 UI 层监听
4. **超时机制**: `ask()` / `confirm()` 添加 `timeout` 参数，超时返回默认值或抛异常
