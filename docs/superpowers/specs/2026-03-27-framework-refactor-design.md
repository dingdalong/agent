# 框架全面重构设计

> 日期：2026-03-27
> 状态：设计阶段
> 范围：项目结构、模块划分、依赖关系、可插拔架构

## 1. 背景与目标

### 当前问题

1. **`config.py` 上帝配置** — 根目录 config.py 实例化 LLM client、持有所有子系统常量，被几乎所有模块直接 import，形成全局耦合源头
2. **`src/agents/` 职责过载** — 同时包含 agent 抽象层、通用图执行引擎、具体 agent 定义，三种不同层次的关注点混在一起
3. **`src/core/` 杂物间** — 5 个毫无关联的模块（LLM调用、I/O、守护、结构化输出、性能计时）被扔在同一个包里
4. **双重 I/O 系统** — `core/io.py` 全局可变函数与 `interfaces/UserInterface` 协议并存，中间件仍依赖旧系统
5. **可插拔性为零** — MemoryStore 直接绑定 ChromaDB、EmbeddingClient 直接绑定 Ollama、call_model() 直接绑定 OpenAI SDK，无接口隔离
6. **`agents ↔ plan` 循环依赖** — definitions.py 用 lazy import 绕过，是模块边界划分有问题的信号
7. **`AgentDeps` 全是 `Any`** — 失去类型安全

### 重构目标

- 全面重审模块划分、命名、依赖、流程设计、职责边界
- 不考虑兼容性和系统过渡，一步到位
- 为未来第三方组件替换（Mem0、向量数据库、embedding实现、LLM provider）提供可插拔架构
- 为未来 Workflow 编排能力预留扩展点

### 设计约束

- 自研框架，基于 AI SDK 实现，不引入 LangChain 等第三方 agent 框架
- 完全自主可控

## 2. 目标目录结构

```
aitest/
├── main.py                     # 薄入口
├── config.yaml                 # 用户/开发者配置文件（新增）
├── mcp_servers.json
├── pyproject.toml
├── .env                        # 只保留 secrets（API keys）
├── workspace/
├── chroma_data/
├── skills/                     # skill 定义文件
│
├── src/
│   ├── config.py               # 配置加载器：读 config.yaml + .env → dict
│   │
│   ├── llm/                    # 【新】LLM 抽象层
│   │   ├── base.py             # LLMProvider Protocol
│   │   ├── types.py            # LLMResponse, ToolCallData, StreamChunk
│   │   ├── openai.py           # OpenAIProvider(LLMProvider) — 当前实现
│   │   └── structured.py       # build_output_schema, parse_output
│   │
│   ├── graph/                  # 【从 agents/graph/ 提升】通用图执行引擎
│   │   ├── types.py            # GraphNode Protocol, Edge, CompiledGraph, NodeResult, ParallelGroup
│   │   ├── builder.py          # GraphBuilder
│   │   ├── engine.py           # GraphEngine
│   │   └── hooks.py            # GraphHooks
│   │
│   ├── agents/                 # Agent 抽象 + 执行
│   │   ├── agent.py            # Agent, AgentResult, HandoffRequest
│   │   ├── node.py             # AgentNode(GraphNode) — 从 graph/types.py 拆出
│   │   ├── runner.py           # AgentRunner — tool-calling 循环
│   │   ├── context.py          # RunContext, TraceEvent（精简）
│   │   ├── deps.py             # AgentDeps — 类型化
│   │   ├── registry.py         # AgentRegistry
│   │   └── hooks.py            # AgentHooks（仅 agent 相关）
│   │
│   ├── guardrails/             # 【从 core/ + agents/ 合并提升】
│   │   ├── base.py             # Guardrail Protocol
│   │   ├── input.py            # InputGuardrail（正则检查等）
│   │   └── output.py           # OutputGuardrail
│   │
│   ├── memory/                 # 记忆系统（可插拔）
│   │   ├── base.py             # MemoryProvider Protocol
│   │   ├── types.py            # MemoryType, MemoryRecord
│   │   ├── decay.py            # calculate_importance()
│   │   ├── extractor.py        # FactExtractor（依赖 LLMProvider）
│   │   ├── buffer.py           # ConversationBuffer
│   │   └── chroma/             # ChromaDB 实现
│   │       ├── store.py        # ChromaMemoryStore(MemoryProvider)
│   │       ├── embeddings.py   # OllamaEmbedding
│   │       └── utils.py        # build_collection_name
│   │
│   ├── tools/                  # 工具系统（结构基本不变）
│   │   ├── schemas.py
│   │   ├── registry.py
│   │   ├── decorator.py
│   │   ├── discovery.py
│   │   ├── executor.py
│   │   ├── middleware.py       # 移除对 core/io.py 的依赖，改用注入的 UserInterface
│   │   ├── router.py
│   │   ├── tool_call.py
│   │   └── builtin/
│   │       ├── calculator.py
│   │       └── file.py
│   │
│   ├── plan/                   # 计划系统（依赖 graph/ 而非 agents/graph/）
│   │   ├── models.py
│   │   ├── planner.py          # 依赖 LLMProvider 而非 call_model
│   │   ├── compiler.py         # 依赖 src/graph/ 而非 src/agents/graph/
│   │   ├── flow.py
│   │   └── exceptions.py
│   │
│   ├── mcp/                    # MCP 集成（不变）
│   │   ├── config.py
│   │   ├── manager.py
│   │   └── provider.py
│   │
│   ├── skills/                 # 技能系统（保持纯文本指令定位）
│   │   ├── models.py
│   │   ├── parser.py
│   │   ├── manager.py
│   │   └── provider.py
│   │
│   ├── interfaces/             # UI 抽象
│   │   ├── base.py             # UserInterface Protocol
│   │   └── cli.py              # CLIInterface
│   │
│   ├── app/                    # 【新】应用组装层
│   │   ├── bootstrap.py        # create_app(): 读配置 → 创建组件 → 注入依赖
│   │   ├── app.py              # AgentApp: 消息路由 + REPL
│   │   └── presets.py          # 具体 agent 定义（weather/calendar/email/orchestrator）
│   │
│   └── utils/
│       ├── text.py             # extract_json()
│       └── performance.py      # 计时装饰器（从 core/ 移来）
│
└── tests/                      # 镜像 src/ 结构
```

## 3. 配置系统

### 设计原则

- **模块自带默认值**（硬编码在代码中）
- **config.yaml 可选覆盖**（不写则用模块默认值）
- **分层分类**：用户层配置（换 LLM）简单直接，开发者调优参数有合理默认
- **.env 只保留 secrets**（API keys）

### config.yaml 示例

```yaml
# ===== 用户配置（换 provider 只改这里）=====
llm:
  provider: deepseek
  model: deepseek-chat
  base_url: https://api.deepseek.com/v1

embedding:
  provider: ollama
  model: qwen3-embedding:0.6b
  base_url: http://127.0.0.1:11434

memory:
  provider: chroma
  path: ./chroma_data

user:
  id: user_001

# ===== 开发者调优（全部可选，有默认值）=====
# plan:
#   max_adjustments: 3
#   max_clarification_rounds: 3
# agents:
#   max_handoffs: 3
#   max_tool_rounds: 3
# llm:
#   concurrency: 5
#   max_retries: 3
# tools:
#   max_output_length: 2000
```

### src/config.py

职责极简 — 只是加载器：

```python
import yaml
from pathlib import Path

def load_config(path: str = "config.yaml") -> dict:
    """加载配置文件，返回原始 dict。文件不存在返回空 dict。"""
    config_path = Path(path)
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}
```

各模块在自己的构造函数中定义默认值，bootstrap 传入 config.yaml 中对应的覆盖值。

## 4. 核心 Protocol 定义

各协议就近放在各模块的 `base.py` 中。

### src/llm/base.py — LLMProvider

```python
from typing import Protocol, AsyncIterator
from src.llm.types import LLMResponse, StreamChunk

class LLMProvider(Protocol):
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse: ...

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamChunk]: ...
```

### src/memory/base.py — MemoryProvider

```python
from typing import Protocol
from src.memory.types import MemoryRecord, MemoryType

class MemoryProvider(Protocol):
    async def add(self, record: MemoryRecord) -> None: ...
    async def search(
        self, query: str, limit: int = 5,
        memory_type: MemoryType | None = None,
    ) -> list[MemoryRecord]: ...
    async def cleanup(self, threshold: float = 0.1) -> int: ...
    async def recalculate_importance(self) -> None: ...
```

### src/guardrails/base.py — Guardrail

```python
from typing import Protocol
from dataclasses import dataclass

@dataclass
class GuardrailResult:
    passed: bool
    message: str = ""

class Guardrail(Protocol):
    name: str
    async def check(self, input: str) -> GuardrailResult: ...
```

### 已有协议（保持不变）

- `src/interfaces/base.py` — `UserInterface` Protocol
- `src/tools/router.py` — `ToolProvider` Protocol
- `src/graph/types.py` — `GraphNode` Protocol

## 5. 依赖流向

单向依赖，无循环：

```
层级 0（无依赖）:
  src/config.py, src/utils/, src/interfaces/, src/graph/, src/guardrails/

层级 1（依赖层级 0）:
  src/llm/          ← 依赖: 无外部模块依赖
  src/tools/        ← 依赖: interfaces/base (UserInterface Protocol)

层级 2（依赖层级 0-1）:
  src/memory/       ← 依赖: llm/base (LLMProvider Protocol)
  src/agents/       ← 依赖: llm/base, graph/, tools/, guardrails/
  src/plan/         ← 依赖: llm/base, graph/ （不再依赖 agents/）
  src/mcp/          ← 依赖: tools/router (ToolProvider Protocol)
  src/skills/       ← 依赖: tools/router (ToolProvider Protocol)

层级 3（应用层，依赖所有）:
  src/app/          ← 依赖: 所有模块（唯一知道具体实现的地方）
  main.py           ← 依赖: src/app/
```

**关键变化：`plan/` 不再依赖 `agents/`，循环依赖彻底消除。**

## 6. 应用组装层

### src/app/bootstrap.py

唯一知道所有具体实现的地方，负责读配置、创建实例、注入依赖：

```python
from src.config import load_config
from src.llm.openai import OpenAIProvider       # 具体实现
from src.memory.chroma.store import ChromaMemoryStore  # 具体实现
from src.interfaces.cli import CLIInterface      # 具体实现
# ... 其余所有模块只 import Protocol

async def create_app(config_path: str = "config.yaml") -> AgentApp:
    raw_config = load_config(config_path)

    # 1. 基础设施
    llm = OpenAIProvider(**raw_config.get("llm", {}))
    ui = CLIInterface()

    # 2. 记忆系统
    memory = ChromaMemoryStore(**raw_config.get("memory", {}))
    extractor = FactExtractor(llm=llm)
    buffer = ConversationBuffer(memory=memory)

    # 3. 工具系统
    tool_registry = discover_tools(...)
    tool_executor = ToolExecutor(tool_registry)
    pipeline = build_pipeline(tool_executor.execute, middlewares, ui=ui)
    tool_router = ToolRouter()
    tool_router.add(LocalToolProvider(tool_registry, pipeline))

    # 4. MCP + Skills
    mcp_manager = await setup_mcp(raw_config)
    if mcp_manager:
        tool_router.add(MCPToolProvider(mcp_manager))
    skill_manager = SkillManager(raw_config.get("skills", {}))
    if skill_manager.skills:
        tool_router.add(SkillToolProvider(skill_manager))

    # 5. Agent 系统
    agent_registry = AgentRegistry()
    register_default_agents(agent_registry, llm=llm, ...)
    graph = build_default_graph(agent_registry)
    engine = GraphEngine()

    # 6. 组装
    deps = AgentDeps(
        llm=llm, tool_router=tool_router,
        agent_registry=agent_registry, engine=engine,
        ui=ui, memory=memory,
    )
    return AgentApp(deps=deps, ui=ui)
```

### src/app/presets.py

从 `agents/definitions.py` 移来的具体 agent 定义（weather, calendar, email, orchestrator, planner），属于"应用配置"而非"框架代码"。

### src/app/app.py

AgentApp 精简为消息路由 + REPL，不再负责组件创建。

## 7. 文件迁移清单

| 原位置 | 新位置 | 动作 |
|---|---|---|
| `config.py`（根目录） | `src/config.py` + `config.yaml` | 重写 |
| `src/app.py` | `src/app/app.py` | 精简 |
| — | `src/app/bootstrap.py` | 新建 |
| `src/agents/definitions.py` | `src/app/presets.py` | 移动+解耦 |
| `src/agents/graph/types.py` | `src/graph/types.py` | 移动（AgentNode 拆到 agents/node.py） |
| `src/agents/graph/builder.py` | `src/graph/builder.py` | 移动 |
| `src/agents/graph/engine.py` | `src/graph/engine.py` | 移动 |
| `src/agents/hooks.py` 中 `GraphHooks` | `src/graph/hooks.py` | 拆出 |
| `src/agents/guardrails.py` | `src/guardrails/` | 合并 |
| `src/core/async_api.py` | `src/llm/openai.py` | 移动+改造为 LLMProvider 实现 |
| `src/core/structured_output.py` | `src/llm/structured.py` | 移动 |
| `src/core/guardrails.py` | `src/guardrails/input.py` + `output.py` | 拆分 |
| `src/core/io.py` | — | **删除** |
| `src/core/performance.py` | `src/utils/performance.py` | 移动 |
| `src/memory/store.py` | `src/memory/chroma/store.py` | 移动 |
| `src/memory/embeddings.py` | `src/memory/chroma/embeddings.py` | 移动 |
| `src/memory/utils.py` | `src/memory/chroma/utils.py` | 移动 |
| — | `src/llm/base.py` | 新建 |
| — | `src/llm/types.py` | 新建 |
| — | `src/memory/base.py` | 新建 |
| — | `src/guardrails/base.py` | 新建 |

### 删除的文件/目录

- `config.py`（根目录）— 被 `src/config.py` + `config.yaml` 替代
- `src/core/` — 整个目录取消
- `src/core/io.py` — 删除，统一用 `UserInterface`
- `src/agents/graph/` — 移到 `src/graph/`
- `src/agents/definitions.py` — 移到 `src/app/presets.py`
- `src/agents/context.py` 中的 `DictState` — 移到 `src/graph/` 或 `src/utils/`，`EmptyDeps` 删除

### 不变的模块

- `src/tools/` — 结构不变，仅修改 middleware 中 I/O 依赖
- `src/mcp/` — 不变
- `src/interfaces/` — 不变
- `src/plan/models.py`、`src/plan/exceptions.py` — 不变
- `src/skills/` — 保持纯文本指令定位，结构不变
- `main.py` — 入口微调，调用 `create_app()` 而非直接 `AgentApp()`

## 8. 未来扩展点

### Workflow 编排（本次不实现）

Workflow 是比 skill 更高层次的编排概念，未来作为独立顶级模块：

```
src/workflow/          # 未来新增
  models.py            # WorkflowDefinition, WorkflowStep
  parser.py            # 解析 workflow 定义文件
  compiler.py          # WorkflowDefinition → CompiledGraph
  runner.py            # 执行入口
```

本次确保的架构支撑：
- `src/graph/` 与 Agent 无关，workflow 可直接使用图引擎
- `src/skills/` 保持纯文本指令，workflow 可调用 skill 作为子能力
- `Agent` 是纯数据类，支持运行时动态创建
- `src/app/` 消息路由可扩展新的命令入口

### 新增 LLM Provider

```python
# src/llm/claude.py（示例）
class ClaudeProvider(LLMProvider):
    async def chat(self, messages, tools=None, tool_choice=None) -> LLMResponse: ...
    async def chat_stream(self, messages, tools=None) -> AsyncIterator[StreamChunk]: ...
```

config.yaml 中 `llm.provider: claude`，bootstrap 中根据 provider 字段选择实现类。

### 新增 Memory Provider

```python
# src/memory/mem0/store.py（示例）
class Mem0MemoryStore(MemoryProvider):
    async def add(self, record) -> None: ...
    async def search(self, query, limit=5, memory_type=None) -> list[MemoryRecord]: ...
```

config.yaml 中 `memory.provider: mem0`，bootstrap 中根据 provider 字段选择实现类。
