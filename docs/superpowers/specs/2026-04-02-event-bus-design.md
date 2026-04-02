# EventBus — Agent 思考过程展示系统

## 概述

当前 agent 执行过程对用户完全不可见：用户确认计划后，只能等待最终结果，无法看到当前执行到哪个步骤、调用了什么工具、agent 在想什么。

本设计引入 EventBus 事件系统，通过 `UserInterface` 协议将 agent 的思考过程实时展示给用户，支持 CLI 和未来的 WebSocket 终端。

## 设计目标

1. **粒度可控** — 三级（PROGRESS / DETAIL / TRACE）+ 事件类型过滤
2. **多终端适配** — 通过 `UserInterface.on_event()` 抽象，CLI 和 WebSocket 各自实现
3. **方便调试** — TRACE 级别展示完整思维链和 LLM 请求
4. **职责清晰** — EventBus（用户看）、logger（开发者看）、trace（事后审计）三通道不重叠

## 信息通道定位

| 通道 | 受众 | 内容 | 举例 |
|------|------|------|------|
| **EventBus** | 终端用户 | agent 思考过程、执行进度 | "正在调用 get_weather"、"weather_agent 完成" |
| **context.trace** | 事后分析 | 结构化执行轨迹，完整记录 | `{node: "weather", event: "tool_call", data: {...}}` |
| **logger** | 框架开发者 | 框架内部运维日志 | "MCP 连接失败"、"并行宽度超限" |

## 模块结构

在 Layer 0 新建 `src/events/` 模块：

```
src/events/
├── __init__.py
├── types.py      # 事件 dataclass 定义
├── levels.py     # PROGRESS / DETAIL / TRACE 级别枚举
└── bus.py        # EventBus 实现
```

## 事件级别

```python
class EventLevel(IntEnum):
    PROGRESS = 1   # 节点开始/结束、图开始/结束、错误
    DETAIL = 2     # + 工具调用、工具结果、handoff、agent 开始/结束
    TRACE = 3      # + thinking、token_delta、llm_request
```

配置方式：

```yaml
# config.yaml
events:
  level: progress    # progress | detail | trace
```

## 事件类型定义

所有事件继承自 `Event` 基类，使用 `Literal` type 字段作为 discriminator：

```python
@dataclass
class Event:
    timestamp: float
    source: str          # 节点/agent 名称
    level: EventLevel
```

### PROGRESS 级别事件

| 事件类 | type 字段 | 字段 | 说明 |
|--------|----------|------|------|
| `GraphStarted` | `"graph_started"` | — | 图执行开始 |
| `GraphEnded` | `"graph_ended"` | `output: Any` | 图执行结束 |
| `NodeStarted` | `"node_started"` | `node_type: str` | 节点开始执行 |
| `NodeEnded` | `"node_ended"` | `output_summary: str` | 节点执行完成 |
| `ErrorOccurred` | `"error"` | `error: str` | 执行出错 |

### DETAIL 级别事件

| 事件类 | type 字段 | 字段 | 说明 |
|--------|----------|------|------|
| `AgentStarted` | `"agent_started"` | `agent_name: str` | Agent 开始 |
| `AgentEnded` | `"agent_ended"` | `agent_name: str` | Agent 结束 |
| `ToolCalled` | `"tool_called"` | `tool_name: str, args: dict` | 工具被调用 |
| `ToolResult` | `"tool_result"` | `tool_name: str, result: str` | 工具返回结果 |
| `Handoff` | `"handoff"` | `from_agent: str, to_agent: str, task: str` | Agent 切换 |

### TRACE 级别事件

| 事件类 | type 字段 | 字段 | 说明 |
|--------|----------|------|------|
| `TokenDelta` | `"token_delta"` | `delta: str` | LLM 流式 token |
| `ThinkingDelta` | `"thinking_delta"` | `content: str` | LLM thinking 块内容 |

## EventBus 实现

```python
class EventBus:
    def __init__(self, level: EventLevel = EventLevel.PROGRESS):
        self._level = level
        self._subscribers: list[_Subscription] = []

    async def emit(self, event: Event) -> None:
        """广播事件到所有匹配的订阅者（非阻塞）。"""
        for sub in self._subscribers:
            if event.level.value <= self._level.value and sub.accepts(event):
                sub.queue.put_nowait(event)

    def subscribe(
        self,
        event_types: set[type[Event]] | None = None,
    ) -> AsyncIterator[Event]:
        """返回 async iterator。event_types=None 表示订阅全部。"""
        ...

    def set_level(self, level: EventLevel) -> None:
        """运行时动态调整级别。"""
        self._level = level
```

### 过滤机制（两层）

1. **全局级别门控** — `event.level <= bus.level` 才广播（设置 DETAIL 则 TRACE 事件不发送）
2. **订阅者类型过滤** — 每个订阅者可选只关注特定事件类型子集

## 集成点

### 生产者（emit 事件的位置）

| 组件 | 位置 | emit 的事件 |
|------|------|------------|
| `GraphEngine.run()` | 图开始/结束 | `GraphStarted`, `GraphEnded` |
| `GraphEngine._execute_node()` | 节点前后 | `NodeStarted`, `NodeEnded` |
| `AgentRunner.run()` | agent 开始/结束 | `AgentStarted`, `AgentEnded` |
| `AgentRunner.run()` | 工具调用前后 | `ToolCalled`, `ToolResult` |
| `AgentRunner.run()` | handoff 检测时 | `Handoff` |
| `AgentRunner.run()` | 出错时 | `ErrorOccurred` |
| `OpenAIProvider._parse_stream()` | 流式 token | `TokenDelta` |
| `OpenAIProvider._parse_stream()` | thinking 块 | `ThinkingDelta` |

### 消费者（UserInterface 扩展）

`UserInterface` 协议新增 `on_event()` 方法：

```python
@runtime_checkable
class UserInterface(Protocol):
    async def prompt(self, message: str) -> str: ...
    async def display(self, message: str) -> None: ...
    async def confirm(self, message: str) -> bool: ...
    async def on_event(self, event: Event) -> None: ...   # 新增
```

`CLIInterface.on_event()` 实现按级别格式化输出：

```
[PROGRESS] 正在执行: weather_agent
[DETAIL]   ⚙ 调用工具: get_weather({"city": "北京"})
[DETAIL]   ✅ 工具结果: {"temp": 25, "condition": "晴"}
[TRACE]    💭 thinking: 用户想知道北京天气...
[PROGRESS] ✅ weather_agent 完成
```

### bootstrap 组装

```python
# bootstrap.py
bus = EventBus(level=EventLevel.from_str(raw.get("events", {}).get("level", "progress")))

engine = GraphEngine(event_bus=bus, ...)
runner = AgentRunner(event_bus=bus, ...)
llm = OpenAIProvider(event_bus=bus, ...)

# UI 订阅 bus，后台消费
async def _ui_consumer():
    async for event in bus.subscribe():
        await ui.on_event(event)
```

## 删除的内容

| 删除项 | 原位置 |
|--------|--------|
| `GraphHooks` 类 | `src/graph/hooks.py` |
| `AgentHooks` 类 | `src/agents/hooks.py` |
| `Agent.hooks` 字段 | `src/agents/agent.py` |
| `OpenAIProvider.on_chunk` 参数 | `src/llm/openai.py` |

## 受影响文件

| 文件 | 改动 |
|------|------|
| `src/graph/engine.py` | `hooks: GraphHooks` → `event_bus: EventBus`，`self.hooks.on_*()` → `self.event_bus.emit(...)` |
| `src/agents/runner.py` | 移除 `agent.hooks` 调用，注入 `event_bus`，在工具调用/handoff 处 emit |
| `src/agents/agent.py` | 删除 `hooks: Optional[Any]` 字段 |
| `src/llm/openai.py` | `on_chunk` → `event_bus`，流式解析中 emit `TokenDelta`；`silent` 保留，控制是否 emit TRACE 级事件 |
| `src/interfaces/base.py` | 新增 `on_event()` 方法 |
| `src/interfaces/cli.py` | 实现 `on_event()`，按级别格式化输出 |
| `src/app/bootstrap.py` | 创建 `EventBus`，注入各组件，启动 UI 消费协程 |
| `config.yaml` | 新增 `events.level` 配置项 |

## context.trace 保留

`context.trace` 继续积累 `TraceEvent`，不受 EventBus 影响。EventBus 是实时展示通道，trace 是事后审计记录，两者职责不同。

## 测试策略

- 新增 `tests/events/` 目录
- `test_types.py` — 事件构造、级别判断
- `test_bus.py` — emit/subscribe、级别过滤、类型过滤、多订阅者
- 修改 `tests/graph/test_engine.py` — 用 EventBus 替代 GraphHooks 的断言
- 修改 `tests/agents/test_runner.py` — 验证工具调用/handoff 事件被 emit
