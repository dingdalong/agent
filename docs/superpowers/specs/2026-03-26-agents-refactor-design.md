# Agents 模块全面重写设计

> 日期：2026-03-26
> 状态：待实现
> 方案：混合架构 — 图引擎 + Agent Runner

## 1. 背景与目标

### 现状问题

现有 `src/agents/` 是学习阶段的产物，存在以下局限：

- `MultiAgentFlow` 基于 FSM，只能表达线性 handoff 链，无法并行执行
- `AgentDef` 定义过于简单，不支持 guardrails、hooks、动态 instructions
- `transfer_to_agent` 工具 schema 硬编码在 registry 中
- orchestrator 逻辑（LLM 调用、skill 激活、handoff 检测）全部塞在一个 `on_enter_orchestrating` 回调里
- FSM 体系（`python-statemachine` + `FSMRunner`）与多智能体编排是两套独立机制

### 目标

1. **统一执行模型**：用图引擎替代 FSM，所有流程（多智能体、对话、表单、规划）统一驱动
2. **生产级 Agent 定义**：支持 guardrails、lifecycle hooks、动态 instructions、声明式 handoff
3. **灵活的协作拓扑**：支持顺序、并行、条件路由、动态 handoff
4. **可观测性**：内置 tracing 和事件系统
5. **与现有模块无缝集成**：通过 RunContext 注入 ToolRouter、Memory 等

### 参考框架

融合多家优点，自己实现：
- **OpenAI Agents SDK**：Agent 声明式定义、handoff 机制、Runner 模式
- **LangGraph**：图定义 API、条件边、并行节点、状态管理
- **CrewAI**：任务驱动执行的理念

### 架构决策

- **移除 FSM 依赖**：删除 `src/core/fsm.py`、`src/flows/` 目录，移除 `python-statemachine` 依赖
- **不引入外部框架**：自实现轻量图引擎，保持完全可控
- **分两层**：图引擎负责拓扑编排，AgentRunner 负责单 agent 执行

## 2. 架构概览

```
用户输入
  │
  ▼
main.py → RunContext 构建 → GraphEngine.run(compiled_graph, context)
  │
  ▼
GraphEngine（外层：拓扑编排）
  │
  ├─ AgentNode → AgentRunner（内层：工具调用循环）
  │                ├─ call_model
  │                ├─ tool execution
  │                ├─ handoff detection
  │                ├─ guardrails check
  │                └─ hooks callback
  │
  ├─ FunctionNode → 普通 async 函数
  │
  ├─ 并行分叉 → asyncio.gather
  │
  └─ 条件路由 → edge.condition(context)
  │
  ▼
GraphResult → main.py → 输出 + 保存记忆
```

## 3. 模块结构

```
src/agents/
├── __init__.py              # 公共 API 导出
├── agent.py                 # Agent 数据模型
├── registry.py              # AgentRegistry 注册表
├── runner.py                # AgentRunner — 单 agent 执行引擎
├── context.py               # RunContext — 运行时上下文
├── hooks.py                 # AgentHooks + GraphHooks
├── guardrails.py            # Guardrail + GuardrailResult
├── graph/
│   ├── __init__.py
│   ├── types.py             # GraphNode / Edge / NodeResult / CompiledGraph
│   ├── builder.py           # GraphBuilder — 声明式图构建 API
│   └── engine.py            # GraphEngine — 图执行引擎

删除：
- src/core/fsm.py
- src/flows/（整个目录）
- pyproject.toml 中 python-statemachine 依赖
```

`src/core/io.py`（`agent_input` / `agent_output`）保留，图引擎中的节点通过它与用户交互。

## 4. 核心类型设计

### 4.1 Agent — 声明式定义

```python
@dataclass
class Agent:
    """Agent 定义 — 描述一个 agent 是什么、能做什么。"""

    name: str
    description: str                                  # 一句话描述，用于 handoff 工具生成
    instructions: str | Callable[[RunContext], str]   # 静态或动态系统提示
    tools: list[str] = field(default_factory=list)    # 允许使用的工具名
    handoffs: list[str] = field(default_factory=list) # 可 handoff 到的 agent 名
    output_model: type[BaseModel] | None = None       # 结构化输出模型
    input_guardrails: list[Guardrail] = field(default_factory=list)
    output_guardrails: list[Guardrail] = field(default_factory=list)
    hooks: AgentHooks | None = None
```

特点：
- `instructions` 支持 `Callable[[RunContext], str]`，可根据共享状态动态生成提示
- `handoffs` 声明式，AgentRunner 自动生成对应的工具 schema
- guardrails 和 hooks 按 agent 粒度配置

### 4.2 AgentResult / HandoffRequest — 执行结果

```python
@dataclass
class AgentResult:
    """单个 agent 的执行结果。"""
    text: str                                       # 文本回复
    data: dict = field(default_factory=dict)        # 结构化数据
    handoff: HandoffRequest | None = None           # handoff 请求（如有）

@dataclass
class HandoffRequest:
    """Agent 请求将任务交接到另一个 agent。"""
    target: str     # 目标 agent 名
    task: str       # 传递的任务描述
```

### 4.3 RunContext — 运行时上下文（泛型：类型安全 + 依赖注入）

```python
StateT = TypeVar("StateT", bound=BaseModel)
DepsT = TypeVar("DepsT", bound=BaseModel)

@dataclass
class RunContext(Generic[StateT, DepsT]):
    """贯穿整个图执行的共享上下文。

    两个泛型参数：
    - StateT: 共享状态结构，节点间传递数据的唯一通道
    - DepsT: 外部依赖（工具路由、记忆、数据库等），由使用者定义，框架只负责传递
    """

    # 输入
    input: str

    # 共享状态 — 类型安全，由 StateT 约束
    state: StateT

    # 外部依赖 — 类型安全，由 DepsT 约束
    deps: DepsT

    # 执行追踪
    trace: list[TraceEvent] = field(default_factory=list)

    # 当前执行信息
    current_agent: str = ""
    depth: int = 0                  # handoff 深度
```

使用示例：

```python
# 1. 定义状态结构
class AgentState(BaseModel):
    weather: WeatherResult | None = None
    calendar: CalendarResult | None = None
    final_response: str = ""

# 2. 定义依赖（替代硬编码的 tool_router/memory/store）
class AgentDeps(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool_router: ToolRouter
    memory: ConversationBuffer
    store: MemoryStore
    # 自由扩展 — 加新依赖不用改框架代码
    # db: Database
    # http_client: HttpClient

# 3. 创建 context
context = RunContext[AgentState, AgentDeps](
    input=user_input,
    state=AgentState(),
    deps=AgentDeps(tool_router=router, memory=buffer, store=store),
)

# 4. 节点中使用 — 全部类型安全
context.state.weather.temp_c     # ✅ IDE 补全
context.deps.tool_router         # ✅ IDE 补全
context.deps.db                  # ❌ 没定义则立即报错
```

对于不需要严格类型的简单场景，提供默认类型：

```python
class DictState(BaseModel):
    """默认的宽松状态，允许任意 key-value。"""
    model_config = ConfigDict(extra="allow")

class EmptyDeps(BaseModel):
    """无外部依赖时使用。"""
    pass

# 最简用法
context = RunContext[DictState, EmptyDeps](
    input="...", state=DictState(), deps=EmptyDeps()
)
```

### 4.4 TraceEvent — 可观测性

```python
@dataclass
class TraceEvent:
    """一次执行事件的记录。"""
    node: str                       # 节点名
    event: str                      # "start" | "end" | "tool_call" | "handoff" | "error"
    timestamp: float
    data: dict = field(default_factory=dict)
```

## 5. AgentRunner — 单 Agent 执行引擎

```python
class AgentRunner:
    """驱动单个 Agent 完成任务的循环。

    职责边界：只负责一个 agent 的工具调用循环和 handoff 检测。
    不关心图的拓扑 — 图引擎决定收到 HandoffRequest 后怎么做。
    """

    def __init__(self, max_tool_rounds: int = 10, max_result_length: int = 4000):
        self.max_tool_rounds = max_tool_rounds
        self.max_result_length = max_result_length

    async def run(self, agent: Agent, context: RunContext) -> AgentResult:
        # 1. 触发 hooks.on_start
        # 2. 执行 input_guardrails
        # 3. 构建 instructions（静态字符串或调用函数生成）
        # 4. 构建 messages: [system, user(context.input 或 task)]
        # 5. 构建工具列表：从 context.tool_router 过滤 agent.tools + 生成 handoff 工具
        # 6. 工具调用循环（最多 max_tool_rounds 轮）：
        #      call_model(messages, tools) →
        #        tool_call 且是 handoff → 返回 AgentResult(handoff=HandoffRequest(...))
        #        tool_call 且是普通工具 → 执行工具，追加消息，触发 hooks.on_tool_call，继续
        #        无 tool_call → 跳出循环
        # 7. 执行 output_guardrails
        # 8. 如果有 output_model → 提取结构化数据
        # 9. 触发 hooks.on_end
        # 10. 返回 AgentResult
        ...
```

### Handoff 工具自动生成

AgentRunner 根据 `agent.handoffs` 自动生成工具 schema：

```python
def _build_handoff_tools(self, agent: Agent, registry: AgentRegistry) -> list[dict]:
    """为 agent.handoffs 中的每个目标生成一个 transfer_to_<name> 工具。"""
    tools = []
    for target_name in agent.handoffs:
        target = registry.get(target_name)
        if target:
            tools.append({
                "type": "function",
                "function": {
                    "name": f"transfer_to_{target_name}",
                    "description": f"将任务交接给 {target_name}: {target.description}",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "交接给目标 agent 的任务描述",
                            }
                        },
                        "required": ["task"],
                    },
                },
            })
    return tools
```

## 6. 图引擎设计

### 6.1 图的类型

```python
class GraphNode(Protocol):
    """图节点协议 — agent 和普通函数都能做节点。"""
    name: str
    async def execute(self, context: RunContext) -> NodeResult: ...

class AgentNode:
    """包装一个 Agent，内部用 AgentRunner 驱动。"""
    def __init__(self, agent: Agent, runner: AgentRunner | None = None):
        self.name = agent.name
        self.agent = agent
        self.runner = runner or AgentRunner()

    async def execute(self, context: RunContext) -> NodeResult:
        context.current_agent = self.name
        result = await self.runner.run(self.agent, context)
        return NodeResult(
            output={"text": result.text, "data": result.data},
            handoff=result.handoff,
        )

class FunctionNode:
    """包装一个普通 async 函数。"""
    def __init__(self, name: str, fn: Callable[[RunContext], Awaitable[NodeResult]]):
        self.name = name
        self.fn = fn

    async def execute(self, context: RunContext) -> NodeResult:
        return await self.fn(context)

@dataclass
class NodeResult:
    """节点执行结果。"""
    output: Any                                 # 写入 context.state[node.name]
    next: str | list[str] | None = None         # 显式指定下一个节点（覆盖默认边）
    handoff: HandoffRequest | None = None       # AgentNode 的 handoff 请求

@dataclass
class Edge:
    """节点间的连接。"""
    source: str
    target: str
    condition: Callable[[RunContext], bool] | None = None  # None = 无条件

@dataclass
class CompiledGraph:
    """编译后的图，不可变，可复用。"""
    nodes: dict[str, GraphNode]
    edges: list[Edge]
    entry: str
    parallel_groups: list[ParallelGroup]   # 并行执行组

@dataclass
class ParallelGroup:
    """一组需要并行执行的节点。"""
    nodes: list[str]        # 并行节点名
    then: str               # 汇聚节点名
```

### 6.2 GraphBuilder — 声明式构建 API

```python
class GraphBuilder:
    """声明式图构建器。"""

    def add_agent(self, name: str, agent: Agent) -> "GraphBuilder":
        """添加一个 agent 节点。"""
        ...

    def add_function(self, name: str, fn: Callable) -> "GraphBuilder":
        """添加一个函数节点。"""
        ...

    def set_entry(self, name: str) -> "GraphBuilder":
        """设置入口节点。"""
        ...

    def add_edge(self, source: str, target: str,
                 condition: Callable[[RunContext], bool] | None = None) -> "GraphBuilder":
        """添加一条边（可选条件）。"""
        ...

    def add_parallel(self, nodes: list[str], then: str) -> "GraphBuilder":
        """声明一组并行执行的节点，完成后汇聚到 then 节点。"""
        ...

    def compile(self) -> CompiledGraph:
        """编译图：验证合法性（环检测、入口可达性、悬挂节点检查）。"""
        ...
```

### 6.3 GraphEngine — 执行引擎

```python
class GraphEngine:
    """异步图执行器。"""

    def __init__(self, registry: AgentRegistry, hooks: GraphHooks | None = None):
        self.registry = registry
        self.hooks = hooks

    async def run(self, graph: CompiledGraph, context: RunContext[StateT]) -> GraphResult[StateT]:
        """执行编译后的图。

        算法：
        1. 触发 hooks.on_graph_start
        2. pending = [graph.entry]
        3. 循环直到 pending 为空：
           a. 检查 pending 中是否有并行组 → asyncio.gather 并行执行
           b. 否则逐个执行
           c. 每个节点执行后：
              - 触发 hooks.on_node_end
              - 将 result.output 写入 context.state[node.name]
              - 记录 TraceEvent
              - 如果有 handoff → 解析目标节点加入 pending
              - 如果有 result.next → 覆盖默认路由
              - 否则按 edges 的 condition 选择下一批节点
           d. 无下一节点 → 结束
        4. 触发 hooks.on_graph_end
        5. 返回 GraphResult
        """
        ...
```

### Handoff 与图的交互

AgentNode 内部的 `HandoffRequest` 被图引擎拦截后：

1. **handoff 目标是图中已有节点** → 直接跳转到该节点执行
2. **handoff 目标不在图中但在 registry 中** → 动态创建临时 AgentNode 执行
3. **handoff 目标不存在** → 记录错误事件，继续执行后续边

这样既支持预定义拓扑，也支持 LLM 动态决策。

### GraphResult

```python
@dataclass
class GraphResult(Generic[StateT]):
    """图执行的最终结果。"""
    output: Any                          # 最后一个节点的输出
    state: StateT                        # 完整的共享状态（类型安全）
    trace: list[TraceEvent]              # 完整的执行追踪
```

## 7. Hooks — 生命周期钩子

```python
@dataclass
class AgentHooks:
    """Agent 级钩子 — 挂在单个 agent 上。"""
    on_start:     Callable[[Agent, RunContext], Awaitable[None]] | None = None
    on_end:       Callable[[Agent, RunContext, AgentResult], Awaitable[None]] | None = None
    on_tool_call: Callable[[Agent, RunContext, str, dict], Awaitable[None]] | None = None
    on_handoff:   Callable[[Agent, RunContext, HandoffRequest], Awaitable[None]] | None = None
    on_error:     Callable[[Agent, RunContext, Exception], Awaitable[None]] | None = None

@dataclass
class GraphHooks:
    """图级钩子 — 挂在整个图执行上。"""
    on_graph_start: Callable[[RunContext], Awaitable[None]] | None = None
    on_graph_end:   Callable[[RunContext, GraphResult], Awaitable[None]] | None = None
    on_node_start:  Callable[[str, RunContext], Awaitable[None]] | None = None
    on_node_end:    Callable[[str, RunContext, NodeResult], Awaitable[None]] | None = None
```

用途示例：
- `AgentHooks.on_tool_call` → 记录日志、敏感操作确认
- `GraphHooks.on_graph_end` → 保存记忆、统计耗时
- `GraphHooks.on_node_start` → 向用户输出「正在调用 weather_agent...」

## 8. Guardrails

```python
@dataclass
class Guardrail:
    """输入/输出护栏。"""
    name: str
    check: Callable[[RunContext, str], Awaitable[GuardrailResult]]

@dataclass
class GuardrailResult:
    passed: bool
    message: str = ""
    action: str = "block"    # "block" | "warn" | "rewrite"
```

- 每个 Agent 可配置独立的 `input_guardrails` 和 `output_guardrails`
- `action` 支持三种策略：
  - `block`：直接阻断，返回错误信息
  - `warn`：记录警告，继续执行
  - `rewrite`：自动改写内容后继续

## 9. main.py 集成

### 两种使用模式

**模式一：Orchestrator 动态分派**（默认）

```python
orchestrator = Agent(
    name="orchestrator",
    instructions="你是总控 Agent，根据用户需求协调专业 Agent。",
    handoffs=["weather_agent", "calendar_agent", "email_agent"],
)

graph = GraphBuilder()
graph.add_agent("orchestrator", orchestrator)
graph.set_entry("orchestrator")
compiled = graph.compile()
# handoff 目标不在图中 → 图引擎从 registry 动态创建节点
```

**模式二：预定义拓扑**（复杂工作流）

```python
graph = GraphBuilder()
graph.add_agent("weather", weather_agent)
graph.add_agent("calendar", calendar_agent)
graph.add_function("merge", merge_fn)
graph.set_entry("weather")
graph.add_parallel(["weather", "calendar"], then="merge")
compiled = graph.compile()
```

### 启动流程

```python
# 1. 注册 agents
registry = AgentRegistry()
registry.register(weather_agent)
registry.register(calendar_agent)
registry.register(email_agent)
registry.register(orchestrator)

# 2. 构建图引擎
engine = GraphEngine(registry=registry, hooks=graph_hooks)

# 3. 编译默认图
default_graph = build_default_graph(registry)

# 4. 初始化依赖（启动时创建一次，所有请求复用）
deps = AgentDeps(tool_router=router, memory=buffer, store=store)

# 5. 每次请求
async def handle_input(user_input: str):
    context = RunContext[AgentState, AgentDeps](
        input=user_input,
        state=AgentState(),
        deps=deps,
    )
    result = await engine.run(default_graph, context)
    return result.output
```

## 10. 原有流程迁移

移除 FSM 后，原有流程迁移为图引擎驱动：

| 原流程 | 迁移方式 |
|--------|---------|
| `ChatFlow` | 单 AgentNode 图，orchestrator agent 直接处理 |
| `MultiAgentFlow` | orchestrator + handoff 动态分派（默认图模式） |
| `MeetingBookingFlow` | 多个 FunctionNode 串联（收集参数 → 确认 → 提交） |
| `PlanningFlow` | Agent + FunctionNode 混合图（澄清 → 生成 → 确认 → 执行） |

## 11. 删除清单

| 文件/目录 | 原因 |
|-----------|------|
| `src/core/fsm.py` | FSM 基础设施，被图引擎替代 |
| `src/flows/` | 所有 FSM Flow，迁移为图引擎驱动 |
| `src/agents/orchestrator.py` | MultiAgentFlow，被 GraphEngine 替代 |
| `src/agents/specialist_runner.py` | run_specialist，被 AgentRunner 替代 |
| `src/agents/specialists.py` | 硬编码的 agent 定义，改为独立注册 |
| `pyproject.toml` 中 `python-statemachine` | FSM 库依赖 |
| `tests/core/test_fsm.py` | FSM 测试 |
| `tests/flows/` | Flow 测试，随流程迁移重写 |

## 12. 测试策略

```
tests/agents/
├── test_agent.py            # Agent 数据模型测试
├── test_registry.py         # AgentRegistry 测试
├── test_runner.py           # AgentRunner 测试（mock LLM）
├── test_context.py          # RunContext 测试
├── test_guardrails.py       # Guardrail 测试
├── test_hooks.py            # Hooks 测试
├── graph/
│   ├── test_types.py        # 节点、边类型测试
│   ├── test_builder.py      # GraphBuilder 编译、验证测试
│   └── test_engine.py       # GraphEngine 执行测试（顺序、并行、条件、handoff）
└── integration/
    └── test_graph_flow.py   # 端到端集成测试
```
