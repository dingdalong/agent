# 项目文档体系 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立完整的项目文档体系，服务三类受众：使用者、开发者、AI 工具。

**Architecture:** 分层文档目录：README.md（门面）+ CLAUDE.md（AI 指令）+ docs/guide/（使用指南）+ docs/architecture/（架构文档）+ docs/contributing.md（贡献指南）+ 关键代码 docstring 增强。

**Tech Stack:** Markdown, Python docstrings

---

## File Structure

**新建文件：**
- `README.md`（重写）
- `CLAUDE.md`
- `docs/guide/getting-started.md`
- `docs/guide/configuration.md`
- `docs/guide/skills-and-tools.md`
- `docs/architecture/overview.md`
- `docs/architecture/llm.md`
- `docs/architecture/agents.md`
- `docs/architecture/memory.md`
- `docs/architecture/tools.md`
- `docs/architecture/graph.md`
- `docs/architecture/plan.md`
- `docs/contributing.md`

**修改文件（添加 docstring）：**
- `src/llm/base.py` — LLMProvider（已有 docstring，检查是否需要补充）
- `src/memory/base.py` — MemoryProvider
- `src/tools/router.py` — ToolProvider
- `src/graph/types.py` — GraphNode
- `src/interfaces/base.py` — UserInterface（已有 docstring）
- `src/app/bootstrap.py` — create_app()
- `src/app/app.py` — AgentApp
- `src/graph/engine.py` — GraphEngine
- `src/plan/compiler.py` — PlanCompiler
- `src/memory/extractor.py` — FactExtractor
- `src/memory/decay.py` — calculate_importance()

---

### Task 1: README.md

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 重写 README.md**

```markdown
# AI Agent Framework

从零构建的 Python AI Agent 框架，支持多智能体协作、长期记忆和动态规划。

## 特性

- **多智能体系统** — 编排器 + 专家智能体，支持 handoff 协作
- **向量记忆系统** — ChromaDB 存储，自动事实提取，重要性衰减
- **图执行引擎** — 顺序/并行节点执行，handoff 路由，执行追踪
- **动态规划系统** — LLM 驱动的多步骤计划生成、编译与执行
- **MCP 协议集成** — 通过 Model Context Protocol 接入外部工具
- **技能系统** — Markdown 定义技能，斜杠命令激活
- **输入安全守卫** — 正则匹配拦截危险输入
- **端口-适配器架构** — Protocol 接口定义，可扩展替换任意组件

## 快速开始

### 环境要求

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) — Python 包管理工具
- [Ollama](https://ollama.com/) — 本地 embedding 服务

### 安装

```bash
git clone <repo-url>
cd agent
uv sync
```

### 配置

1. 创建 `.env` 文件，填入 API Key：

```bash
OPENAI_API_KEY=your-api-key-here
```

2. 编辑 `config.yaml` 配置 LLM 和 embedding 服务（默认使用 DeepSeek + Ollama）

3. 启动 Ollama 并拉取 embedding 模型：

```bash
ollama pull qwen3-embedding:0.6b
```

### 运行

```bash
uv run python main.py
```

## 项目结构

```
main.py                  # 入口
config.yaml              # 用户配置
src/
├── config.py            # 配置加载器
├── interfaces/          # 用户交互协议（CLI）
├── llm/                 # LLM 抽象层（OpenAI 兼容）
├── memory/              # 记忆系统（ChromaDB + 事实提取 + 衰减）
├── agents/              # 智能体模型、运行器、注册表
├── graph/               # 图执行引擎
├── plan/                # 动态规划系统
├── tools/               # 工具注册、执行、中间件
├── mcp/                 # MCP 协议客户端
├── skills/              # 技能系统
├── guardrails/          # 输入安全守卫
├── utils/               # 工具函数
└── app/                 # 应用组装与 REPL
tests/                   # 测试（镜像 src/ 结构）
docs/                    # 文档
```

## 文档

- [快速入门](docs/guide/getting-started.md)
- [配置详解](docs/guide/configuration.md)
- [技能与工具](docs/guide/skills-and-tools.md)
- [架构总览](docs/architecture/overview.md)
- [贡献指南](docs/contributing.md)

## License

MIT
```

- [ ] **Step 2: 提交**

```bash
git add README.md
git commit -m "docs: rewrite README with project overview and quick start"
```

---

### Task 2: CLAUDE.md

**Files:**
- Create: `CLAUDE.md`

- [ ] **Step 1: 创建 CLAUDE.md**

```markdown
# AI Agent Framework

从零构建的 Python AI Agent 框架，端口-适配器架构。

## 技术栈

- Python 3.13，uv 管理依赖
- LLM：DeepSeek（OpenAI 兼容接口），通过 LLMProvider Protocol 可替换
- 向量存储：ChromaDB + Ollama embedding（qwen3-embedding:0.6b）
- 异步：asyncio 全链路

## 架构分层

- **Layer 0**（无外部依赖）：`src/config.py`, `src/utils/`, `src/interfaces/`, `src/graph/`, `src/guardrails/`
- **Layer 1**（依赖 Layer 0）：`src/llm/`, `src/tools/`
- **Layer 2**（依赖 Layer 0-1）：`src/memory/`, `src/agents/`, `src/plan/`, `src/mcp/`, `src/skills/`
- **Layer 3**（应用层，组装所有具体实现）：`src/app/`

低层模块不得导入高层模块。

## 关键约定

- 所有可插拔组件使用 Protocol 接口：`LLMProvider`, `MemoryProvider`, `ToolProvider`, `GraphNode`, `UserInterface`
- 具体实现只在 `src/app/bootstrap.py` 中实例化
- 工具通过 `@tool` 装饰器注册，支持中间件管道（错误处理、敏感确认、截断）
- 配置集中在 `config.yaml`（用户配置）+ `.env`（密钥）

## 开发规范

- 每次完成任务后，如有必要，同步更新相关文档和测试用例
- 类型安全：所有函数签名必须有类型注解，使用 Protocol 而非具体类型，善用 TypedDict / dataclass / Pydantic model 定义数据结构

## 常用命令

```bash
uv sync                    # 安装依赖
uv run python main.py      # 启动 agent
uv run pytest              # 运行测试
uv run pytest -m slow      # 运行慢速/集成测试
```

## 当前状态

- weather / calendar / email 专家智能体的工具是占位符（`get_weather`, `create_event`, `send_email` 未注册）
- 参见 `TODO` 文件了解未来计划

## 文件导航

- 入口：`main.py` → `src/app/bootstrap.py` → `src/app/app.py`
- 核心接口：`src/llm/base.py`（LLMProvider）、`src/memory/base.py`（MemoryProvider）、`src/tools/router.py`（ToolProvider）、`src/graph/types.py`（GraphNode）、`src/interfaces/base.py`（UserInterface）
- 智能体：`src/agents/agent.py`（Agent 模型）、`src/agents/runner.py`（工具循环）、`src/app/presets.py`（预设定义）
- 图引擎：`src/graph/engine.py`（GraphEngine）、`src/graph/builder.py`（GraphBuilder）
- 规划：`src/plan/flow.py`（PlanFlow 5 阶段）、`src/plan/compiler.py`（Plan → CompiledGraph）
- 记忆：`src/memory/chroma/store.py`（ChromaDB 存储）、`src/memory/extractor.py`（事实提取）、`src/memory/decay.py`（重要性衰减）
- 配置：`config.yaml`、`.env`
```

- [ ] **Step 2: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: add CLAUDE.md for AI tool context"
```

---

### Task 3: docs/guide/getting-started.md

**Files:**
- Create: `docs/guide/getting-started.md`

- [ ] **Step 1: 创建使用指南目录和入门文档**

```bash
mkdir -p docs/guide
```

```markdown
# 快速入门

## 环境准备

| 依赖 | 版本 | 用途 |
|------|------|------|
| Python | 3.13+ | 运行环境 |
| [uv](https://docs.astral.sh/uv/) | 最新 | 包管理 |
| [Ollama](https://ollama.com/) | 最新 | 本地 embedding 服务 |

## 安装

### 1. 克隆项目

```bash
git clone <repo-url>
cd agent
```

### 2. 安装依赖

```bash
uv sync
```

### 3. 启动 Ollama 并拉取 embedding 模型

```bash
# 启动 Ollama 服务（如果还没有运行）
ollama serve

# 拉取 embedding 模型
ollama pull qwen3-embedding:0.6b
```

### 4. 配置 API Key

创建 `.env` 文件：

```bash
OPENAI_API_KEY=your-api-key-here
```

项目默认使用 DeepSeek API。如需切换，参见 [配置详解](configuration.md)。

### 5. 运行

```bash
uv run python main.py
```

## 示例对话

```
Agent 已启动，输入 'exit' 退出。

你: 帮我算一下 123 * 456
助手: 123 × 456 = 56,088

你: 今天天气怎么样？
助手: [orchestrator 会将请求转交给 weather_agent 处理]

你: /plan 查询北京天气然后发邮件告诉同事
助手: [启动规划流程：澄清 → 生成计划 → 确认 → 执行]

你: exit
```

## 常见问题

### Ollama 连接失败

```
ConnectionError: Cannot connect to http://127.0.0.1:11434
```

确保 Ollama 服务已启动：`ollama serve`

### API Key 无效

```
AuthenticationError: Invalid API key
```

检查 `.env` 文件中的 `OPENAI_API_KEY` 是否正确。DeepSeek 的 API Key 从 [DeepSeek Platform](https://platform.deepseek.com/) 获取。

### 模型不支持

如果使用的 LLM 不支持 function calling（工具调用），规划系统和事实提取器将无法工作。推荐使用 DeepSeek-Chat 或 OpenAI GPT-4 系列。
```

- [ ] **Step 2: 提交**

```bash
git add docs/guide/getting-started.md
git commit -m "docs: add getting started guide"
```

---

### Task 4: docs/guide/configuration.md

**Files:**
- Create: `docs/guide/configuration.md`

- [ ] **Step 1: 创建配置文档**

```markdown
# 配置详解

项目使用两个配置文件：`config.yaml`（用户配置）和 `.env`（密钥）。

## config.yaml

### LLM 配置

```yaml
llm:
  provider: deepseek          # 提供商标识（目前仅用于注释，实际通过 base_url 区分）
  model: deepseek-chat         # 模型名称
  base_url: https://api.deepseek.com/v1  # API 端点
  # concurrency: 5             # 并发请求上限（默认 5）
  # max_retries: 3             # 失败重试次数（默认 3）
```

### Embedding 配置

```yaml
embedding:
  provider: ollama
  model: qwen3-embedding:0.6b
  base_url: http://127.0.0.1:11434
```

Embedding 用于记忆系统的向量化。默认使用本地 Ollama 服务。

### 记忆配置

```yaml
memory:
  provider: chroma             # 存储后端（目前仅支持 chroma）
  path: ./chroma_data          # ChromaDB 持久化目录
```

### 用户配置

```yaml
user:
  id: user_001                 # 用户标识，用于记忆隔离
```

### 高级参数（可选）

```yaml
# 规划系统
plan:
  max_adjustments: 3           # 计划调整最大次数
  max_clarification_rounds: 3  # 澄清对话最大轮次

# 智能体
agents:
  max_handoffs: 3              # handoff 最大深度
  max_tool_rounds: 3           # 工具调用最大轮次

# 工具
tools:
  max_output_length: 2000      # 工具输出最大长度（字符）

# MCP
mcp:
  config_path: mcp_servers.json  # MCP 服务器配置文件路径

# 技能
skills:
  dirs:                        # 技能搜索目录
    - skills/
    - .agents/skills/
```

## .env 环境变量

| 变量 | 用途 | 示例 |
|------|------|------|
| `OPENAI_API_KEY` | LLM API 密钥 | `sk-...` |
| `OPENAI_BASE_URL` | LLM API 端点（备用，优先使用 config.yaml） | `https://api.openai.com/v1` |
| `OPENAI_MODEL` | 模型名称（备用） | `gpt-4` |
| `OPENAI_MODEL_EMBEDDING` | Embedding 模型名称（备用） | `text-embedding-3-small` |
| `OPENAI_MODEL_EMBEDDING_URL` | Embedding API 端点（备用） | `https://api.openai.com/v1` |
| `USER_ID` | 用户标识（备用） | `user_001` |

`.env` 中的值仅在 `config.yaml` 对应字段为空时生效。

## 切换 LLM 提供商

项目使用 OpenAI 兼容接口，任何支持该接口的提供商都可以使用：

### DeepSeek（默认）

```yaml
llm:
  provider: deepseek
  model: deepseek-chat
  base_url: https://api.deepseek.com/v1
```

### OpenAI

```yaml
llm:
  provider: openai
  model: gpt-4o
  base_url: https://api.openai.com/v1
```

### 本地模型（通过 Ollama）

```yaml
llm:
  provider: ollama
  model: qwen2.5:7b
  base_url: http://127.0.0.1:11434/v1
```

注意：本地模型需要支持 function calling 才能使用规划系统和事实提取功能。
```

- [ ] **Step 2: 提交**

```bash
git add docs/guide/configuration.md
git commit -m "docs: add configuration guide"
```

---

### Task 5: docs/guide/skills-and-tools.md

**Files:**
- Create: `docs/guide/skills-and-tools.md`

- [ ] **Step 1: 创建技能与工具文档**

```markdown
# 技能与工具

## 内置工具

### 计算器（calculator）

安全的数学表达式计算，基于 AST 解析（不使用 eval）。

```
你: 帮我算一下 (123 + 456) * 789
```

### 文件读写（file）

沙箱化的文件操作，限制在 `workspace/` 目录内。

```
你: 读取 workspace/notes.txt 的内容
你: 把这段内容写入 workspace/output.txt
```

## MCP 工具集成

通过 [Model Context Protocol](https://modelcontextprotocol.io/) 接入外部工具服务。

### 配置

编辑 `mcp_servers.json`：

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

每个 MCP 服务器需要指定：
- `transport`：通信方式（`stdio` 或 `http`）
- `command`：启动命令（stdio 模式）
- `args`：命令参数
- `env`：环境变量

### 添加新的 MCP 服务器

在 `mcpServers` 下添加新条目即可。启动时框架会自动连接所有配置的服务器并发现其工具。

## 技能系统

技能是通过 Markdown 文件定义的可激活指令集，为智能体注入特定领域的行为。

### 技能文件格式

在 `skills/` 目录下创建文件夹，包含 `SKILL.md`：

```
skills/
└── my-skill/
    └── SKILL.md
```

`SKILL.md` 格式：

```markdown
---
name: my-skill
description: 简短描述这个技能的用途
---

这里是技能的指令内容，会注入到智能体的 system prompt 中。
```

### 使用技能

通过斜杠命令激活：

```
你: /my-skill 请按照技能要求执行任务
```

技能激活后，框架会创建一个独立的智能体图，将技能指令注入编排器，然后执行用户请求。

### 内置技能示例

- `/code-review` — 代码审查
- `/translate` — 翻译

## 规划系统

处理需要多步骤的复杂任务。

### 使用方式

```
你: /plan 查询北京天气，然后把结果发邮件给同事
```

### 执行流程

1. **澄清阶段** — 如果请求不够清楚，系统会提问澄清
2. **计划生成** — LLM 生成包含多个步骤的执行计划
3. **确认/调整** — 用户确认计划或要求调整
4. **编译** — 计划编译为可执行的图（支持并行步骤）
5. **执行** — 图引擎按拓扑顺序执行各步骤
```

- [ ] **Step 2: 提交**

```bash
git add docs/guide/skills-and-tools.md
git commit -m "docs: add skills and tools guide"
```

---

### Task 6: docs/architecture/overview.md

**Files:**
- Create: `docs/architecture/overview.md`

- [ ] **Step 1: 创建架构目录和总览文档**

```bash
mkdir -p docs/architecture
```

```markdown
# 架构总览

## 设计哲学

- **从零构建** — 不依赖 LangChain、CrewAI 等框架，所有核心功能自研
- **端口-适配器架构** — 通过 Protocol 定义接口，具体实现可替换
- **集中组装** — 所有具体类的实例化集中在 `src/app/bootstrap.py`，业务代码只依赖 Protocol
- **异步优先** — 全链路 asyncio，支持并发执行

## 分层架构

```
Layer 3: src/app/          应用层 — 组装组件、REPL、消息路由
         ↓ 依赖所有层
Layer 2: src/memory/       记忆系统 — 向量存储、事实提取、衰减
         src/agents/       智能体 — 模型定义、运行器、注册表
         src/plan/         规划系统 — 计划生成、编译、执行
         src/mcp/          MCP 客户端 — 外部工具集成
         src/skills/       技能系统 — 发现、激活、注入
         ↓ 依赖 Layer 0-1
Layer 1: src/llm/          LLM 抽象层 — Provider、流式输出、结构化输出
         src/tools/        工具系统 — 注册、执行、中间件、路由
         ↓ 依赖 Layer 0
Layer 0: src/config.py     配置加载器
         src/utils/        工具函数
         src/interfaces/   用户交互协议
         src/graph/        图执行引擎
         src/guardrails/   输入安全守卫
         （无外部依赖）
```

**依赖规则：低层模块不得导入高层模块。**

## 请求处理流程

```
用户输入
  │
  ├─ 守卫检查（InputGuardrail）
  │   └─ 拦截危险输入 → 返回安全提示
  │
  ├─ /plan 命令 → PlanFlow
  │   └─ 澄清 → 生成 → 确认 → 编译 → GraphEngine 执行
  │
  ├─ /skill-name 命令 → SkillManager
  │   └─ 激活技能 → 构建独立图 → GraphEngine 执行
  │
  └─ 普通消息 → 默认图
      └─ orchestrator → 直接回答 / handoff 到专家智能体
```

## 核心接口（Protocol）

| Protocol | 文件 | 职责 |
|----------|------|------|
| `LLMProvider` | `src/llm/base.py` | LLM 调用抽象 |
| `MemoryProvider` | `src/memory/base.py` | 记忆存储抽象 |
| `ToolProvider` | `src/tools/router.py` | 工具来源抽象 |
| `GraphNode` | `src/graph/types.py` | 图节点抽象 |
| `UserInterface` | `src/interfaces/base.py` | 用户交互抽象 |

## 模块文档

- [LLM 层](llm.md)
- [智能体系统](agents.md)
- [记忆系统](memory.md)
- [工具系统](tools.md)
- [图引擎](graph.md)
- [规划系统](plan.md)
```

- [ ] **Step 2: 提交**

```bash
git add docs/architecture/overview.md
git commit -m "docs: add architecture overview"
```

---

### Task 7: docs/architecture/ 模块文档（6 个文件）

**Files:**
- Create: `docs/architecture/llm.md`
- Create: `docs/architecture/agents.md`
- Create: `docs/architecture/memory.md`
- Create: `docs/architecture/tools.md`
- Create: `docs/architecture/graph.md`
- Create: `docs/architecture/plan.md`

- [ ] **Step 1: 创建 llm.md**

```markdown
# LLM 层

## 职责

封装 LLM 调用细节，提供统一的 Protocol 接口。上层模块（AgentRunner、PlanFlow、FactExtractor）只依赖 `LLMProvider`，不关心底层是哪个模型。

## 核心组件

### LLMProvider Protocol（`src/llm/base.py`）

```python
class LLMProvider(Protocol):
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 1.0,
        tool_choice: str | None = None,
        silent: bool = False,
    ) -> LLMResponse: ...
```

### LLMResponse（`src/llm/types.py`）

返回值包含：
- `content: str` — 文本响应
- `tool_calls: dict` — 工具调用请求
- `finish_reason: str` — 结束原因

### OpenAIProvider（`src/llm/openai.py`）

唯一的具体实现，特性：
- 流式输出（`on_chunk` 回调实时显示）
- 并发控制（`asyncio.Semaphore`）
- 指数退避重试
- 支持所有 OpenAI 兼容接口（DeepSeek、Ollama 等）

### 结构化输出（`src/llm/structured.py`）

通过 function calling 技巧，强制 LLM 返回符合 Pydantic model 的 JSON：
- `build_output_schema(name, desc, model)` — Pydantic model → tool schema
- `parse_output(tool_calls, name, model)` — tool_calls → Pydantic 实例

## 扩展方式

实现 `LLMProvider` Protocol，然后在 `bootstrap.py` 中替换 `OpenAIProvider`。
```

- [ ] **Step 2: 创建 agents.md**

```markdown
# 智能体系统

## 职责

定义智能体模型，驱动工具调用循环，管理智能体间的 handoff 协作。

## 核心组件

### Agent 模型（`src/agents/agent.py`）

```python
@dataclass
class Agent:
    name: str
    description: str = ""
    instructions: str = ""
    tools: list[str] = field(default_factory=list)
    handoffs: list[str] = field(default_factory=list)
    output_model: type[BaseModel] | None = None
    guardrails: list = field(default_factory=list)
    hooks: AgentHooks | None = None
```

### AgentRunner（`src/agents/runner.py`）

驱动单个智能体的工具调用循环：
1. 发送消息给 LLM（包含工具 schema）
2. 如果 LLM 返回工具调用 → 执行工具 → 将结果加入消息 → 回到 1
3. 如果检测到 `transfer_to_<name>` 调用 → 返回 `HandoffRequest`
4. 如果 LLM 返回文本 → 运行输出守卫 → 返回结果
5. 达到 `max_tool_rounds` → 强制结束

### AgentRegistry（`src/agents/registry.py`）

名称 → Agent 的映射表。AgentRunner 通过 registry 查找 handoff 目标。

### AgentNode（`src/agents/node.py`）

将 Agent + AgentRunner 适配为 `GraphNode` Protocol，使智能体可以作为图节点参与图执行。

### RunContext（`src/agents/context.py`）

执行上下文，贯穿整个调用链：
- `input: str` — 当前输入
- `state: DictState` — 可写状态（Pydantic `extra='allow'`）
- `deps: AgentDeps` — 依赖注入容器
- `trace: list` — 执行追踪记录

### AgentDeps（`src/agents/deps.py`）

依赖注入容器，持有所有共享组件的引用：
- `llm`, `tool_router`, `agent_registry`, `graph_engine`, `ui`, `memory`（可选）

## 预设智能体（`src/app/presets.py`）

| 智能体 | 职责 |
|--------|------|
| `orchestrator` | 总控，根据用户请求路由到专家或直接回答 |
| `weather_agent` | 天气查询（工具占位） |
| `calendar_agent` | 日历管理（工具占位） |
| `email_agent` | 邮件发送（工具占位） |
| `planner` | 转发到 PlanFlow 执行多步骤任务 |

## 数据流

```
用户输入 → RunContext → GraphEngine
  → orchestrator (AgentNode)
    → AgentRunner.run()
      → LLM chat（带工具 schema）
      → 工具调用循环
      → HandoffRequest / 最终回复
  → handoff 目标智能体（如果有）
  → 最终输出
```
```

- [ ] **Step 3: 创建 memory.md**

```markdown
# 记忆系统

## 职责

提供长期记忆能力：从对话中自动提取事实，向量化存储，支持语义检索，带时间衰减的重要性评估。

## 核心组件

### MemoryProvider Protocol（`src/memory/base.py`）

```python
class MemoryProvider(Protocol):
    def add(self, record: MemoryRecord) -> str: ...
    def search(self, query: str, n: int = 5, ...) -> list[MemoryRecord]: ...
    def cleanup(self, min_importance: float = 0.1) -> int: ...
    def recalculate_importance(self) -> None: ...
```

### MemoryRecord（`src/memory/types.py`）

统一的记忆记录模型（Pydantic）：
- `memory_type`: FACT 或 SUMMARY
- `content`, `type_tag`, `attribute`
- `confidence`, `importance`, `access_count`
- `version`, `is_active`, `last_accessed`
- 支持 ChromaDB 序列化/反序列化

### ChromaMemoryStore（`src/memory/chroma/store.py`）

MemoryProvider 的 ChromaDB 实现：
- 单 collection 存储所有记忆类型
- 版本控制：更新时旧版本标记为 `is_active=False`
- 访问统计：搜索时自动增加 `access_count`
- 批量重新计算重要性

### FactExtractor（`src/memory/extractor.py`）

通过 LLM 从对话中提取结构化事实：
- 支持 5 大类约 25 种事实类型（user, assistant, world, conversation, interaction）
- 使用 function calling（`submit_facts` 工具）确保输出格式
- 置信度调整：模糊词降低、强确定词提高
- 过滤：敏感信息、不合理事实、低置信度

### ConversationBuffer（`src/memory/buffer.py`）

短期记忆的 token 计数滑动窗口：
- 按轮次维护对话历史
- 超出 token 上限时压缩最老的一半（通过 LLM 生成摘要，存入 ChromaDB）

### 重要性衰减（`src/memory/decay.py`）

```
importance = confidence × recency_weight × frequency_weight
```

- `recency_weight = exp(-λ × days_since_last_access)`，半衰期约 70 天
- `frequency_weight = min(1.0, log(access_count + 1) / log(20))`
- SUMMARY 类型不衰减，始终返回 1.0

### Embedding（`src/memory/chroma/embeddings.py`）

调用 Ollama API 生成向量，支持连接复用。

```

- [ ] **Step 4: 创建 tools.md**

```markdown
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
| `read_file` / `write_file` | `src/tools/builtin/file.py` | 沙箱文件读写 |

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
```

- [ ] **Step 5: 创建 graph.md**

```markdown
# 图执行引擎

## 职责

提供与智能体无关的异步图执行能力。将工作流定义（节点、边、并行组）编译为不可变的图结构，然后按拓扑顺序执行。

## 核心组件

### GraphNode Protocol（`src/graph/types.py`）

```python
class GraphNode(Protocol):
    name: str
    async def execute(self, context: Any) -> NodeResult: ...
```

`NodeResult` 包含：
- `output: Any` — 节点执行结果
- `next: str | list[str] | None` — 显式指定下一个节点
- `handoff: Any` — handoff 请求（交给其他智能体）

### FunctionNode（`src/graph/types.py`）

将普通 async 函数包装为 GraphNode。用于 PlanCompiler 将 Step 转为节点。

### GraphBuilder（`src/graph/builder.py`）

声明式、链式的图构建 API：
- `add_node(node)` — 添加 GraphNode
- `add_function(name, fn)` — 添加函数节点
- `add_edge(source, target)` — 添加边
- `add_parallel(nodes, then)` — 添加并行组
- `set_entry(name)` — 设置入口节点
- `compile()` → `CompiledGraph`（带验证）

### CompiledGraph（`src/graph/types.py`）

编译后的不可变图结构：`nodes`, `edges`, `entry`, `parallel_groups`。

### GraphEngine（`src/graph/engine.py`）

执行 CompiledGraph：
1. 从 `entry` 节点开始
2. 检查当前 pending 列表是否匹配 ParallelGroup → 并行执行
3. 否则顺序执行，将输出写入 context.state
4. 处理 handoff：更新 context.input，切换到目标节点
5. 处理显式 next 或按边路由
6. 支持 hooks（`on_graph_start/end`, `on_node_start/end`）
7. 深度限制防止无限 handoff

## 数据流

```
CompiledGraph + RunContext
  → GraphEngine.run()
    → pending = [entry]
    → while pending:
        → 并行组？ → asyncio.gather → 写入 state
        → 单节点？ → execute → 处理 handoff/next/edge
    → GraphResult(output, state, trace)
```
```

- [ ] **Step 6: 创建 plan.md**

```markdown
# 规划系统

## 职责

处理需要多步骤的复杂任务：通过 LLM 生成执行计划，编译为可执行的图，然后通过图引擎执行。

## 核心组件

### Step / Plan 模型（`src/plan/models.py`）

```python
@dataclass
class Step:
    id: str
    description: str
    tool_name: str | None = None    # 工具步骤
    tool_args: dict = field(...)
    agent_name: str | None = None   # 智能体步骤
    agent_prompt: str | None = None
    depends_on: list[str] = field(...)  # 依赖的步骤 ID

@dataclass
class Plan:
    goal: str
    steps: list[Step]
```

每个 Step 要么是工具步骤（`tool_name`），要么是智能体步骤（`agent_name`）。

### PlanFlow（`src/plan/flow.py`）

5 阶段编排流程：
1. **澄清** — `check_clarification_needed()` 判断是否需要补充信息，循环提问直到清晰
2. **生成** — `generate_plan()` 通过 LLM + `submit_plan` 工具生成 Plan
3. **确认** — 展示计划给用户，用户可以确认、调整或取消
4. **编译** — `PlanCompiler.compile()` 将 Plan 转为 CompiledGraph
5. **执行** — `GraphEngine.run()` 执行编译后的图

### PlanCompiler（`src/plan/compiler.py`）

将 Plan 编译为 CompiledGraph：
1. 验证（ID 唯一性、agent 存在性）
2. 分层拓扑排序 — 同层步骤无互相依赖，可并行
3. 每个 Step → FunctionNode（工具闭包或智能体闭包）
4. 单步骤层 → 顺序边；多步骤层 → ParallelGroup + merge 节点
5. 支持 `$step_id.field` 变量引用（步骤间传递数据）

### 变量引用

步骤间通过 `$step_id.field` 语法传递数据：

```yaml
- id: step1
  tool_name: get_weather
  tool_args: { city: "北京" }

- id: step2
  agent_name: email_agent
  agent_prompt: "发送天气信息: $step1.result"
  depends_on: [step1]
```

`resolve_variables()` 在执行时从 context.state 中解析变量值。

## 数据流

```
用户请求
  → PlanFlow.run()
    → 澄清循环（可选）
    → LLM 生成 Plan
    → 用户确认/调整循环
    → PlanCompiler.compile() → CompiledGraph
    → GraphEngine.run() → 结果
```
```

- [ ] **Step 7: 提交所有架构文档**

```bash
git add docs/architecture/
git commit -m "docs: add architecture documentation for all modules"
```

---

### Task 8: docs/contributing.md

**Files:**
- Create: `docs/contributing.md`

- [ ] **Step 1: 创建贡献指南**

```markdown
# 贡献指南

## 开发环境搭建

```bash
git clone <repo-url>
cd agent
uv sync
```

配置 `.env` 文件（参见 [配置详解](guide/configuration.md)）。

## 代码规范

### 架构原则

- **Protocol 优先** — 新功能先定义 Protocol 接口，再实现具体类
- **分层依赖** — 低层模块不得导入高层模块（Layer 0 → 1 → 2 → 3）
- **异步优先** — 优先使用 async/await
- **集中组装** — 具体实现只在 `src/app/bootstrap.py` 中实例化
- **类型安全** — 所有函数签名必须有类型注解，优先使用 Protocol / TypedDict / dataclass / Pydantic model 定义数据结构

### 添加新模块

1. 在对应层定义 Protocol（如 `src/xxx/base.py`）
2. 实现具体类
3. 在 `src/app/bootstrap.py` 中实例化并注入
4. 编写测试
5. 更新相关文档

### 添加新工具

```python
# src/tools/builtin/my_tool.py
from pydantic import BaseModel
from src.tools.decorator import tool

class MyToolInput(BaseModel):
    param: str

@tool(model=MyToolInput, description="工具描述")
async def my_tool(args: MyToolInput) -> str:
    return f"结果: {args.param}"
```

工具会在启动时被 `discover_tools()` 自动发现和注册。

## 测试

```bash
uv run pytest              # 运行所有测试（跳过慢速测试）
uv run pytest -m slow      # 运行慢速/集成测试
uv run pytest -v           # 详细输出
```

测试目录结构镜像 `src/`：

```
tests/
├── llm/
├── agents/
├── memory/
├── tools/
├── graph/
├── plan/
├── mcp/
├── skills/
├── guardrails/
├── interfaces/
└── test_app.py
```

## 提交规范

使用语义化前缀：

| 前缀 | 用途 |
|------|------|
| `feat:` | 新功能 |
| `fix:` | Bug 修复 |
| `refactor:` | 重构（不改变行为） |
| `test:` | 测试相关 |
| `docs:` | 文档相关 |
```

- [ ] **Step 2: 提交**

```bash
git add docs/contributing.md
git commit -m "docs: add contributing guide"
```

---

### Task 9: 关键代码 docstring 增强

**Files:**
- Modify: `src/memory/base.py:8-12`
- Modify: `src/tools/router.py:14-18`
- Modify: `src/graph/types.py:25-28`
- Modify: `src/app/bootstrap.py:32-33`
- Modify: `src/app/app.py:14`
- Modify: `src/graph/engine.py:27-28`
- Modify: `src/plan/compiler.py:79-80`
- Modify: `src/memory/extractor.py:145-146`
- Modify: `src/memory/decay.py:18-29`

已有良好 docstring 的文件跳过：`src/llm/base.py`（LLMProvider 已有完整 docstring）、`src/interfaces/base.py`（UserInterface 已有 docstring）。

- [ ] **Step 1: MemoryProvider docstring**

在 `src/memory/base.py` 中，将：

```python
class MemoryProvider(Protocol):
    """所有记忆存储实现必须满足的协议。"""
```

替换为：

```python
class MemoryProvider(Protocol):
    """记忆存储的抽象接口。

    提供记忆的增删改查能力。实现者需要处理向量化存储和语义检索。
    当前实现：ChromaMemoryStore（src/memory/chroma/store.py）。

    使用方式：
        store = ChromaMemoryStore(...)
        record_id = store.add(record)
        results = store.search("用户喜欢什么", n=5)
    """
```

- [ ] **Step 2: ToolProvider docstring**

在 `src/tools/router.py` 中，将：

```python
class ToolProvider(Protocol):
    """所有工具来源的统一接口"""
```

替换为：

```python
class ToolProvider(Protocol):
    """工具来源的统一接口。

    ToolRouter 通过此协议查询和执行工具。每个 provider 管理一组工具，
    通过 can_handle 判断是否能处理某个工具名，通过 get_schemas 暴露
    工具的 JSON Schema 供 LLM 选择。

    实现者：LocalToolProvider（本地 @tool）、MCPToolProvider（MCP）、SkillToolProvider（技能）。
    """
```

- [ ] **Step 3: GraphNode docstring**

在 `src/graph/types.py` 中，将：

```python
class GraphNode(Protocol):
    """图节点协议。"""
```

替换为：

```python
class GraphNode(Protocol):
    """图节点协议 — 所有可在 GraphEngine 中执行的节点必须实现此接口。

    实现者通过 execute() 接收 RunContext，返回 NodeResult 控制执行流：
    - output: 节点计算结果，写入 context.state
    - next: 显式指定下一个节点（覆盖边路由）
    - handoff: 请求切换到另一个智能体

    实现者：AgentNode（智能体）、FunctionNode（普通函数）。
    """
```

- [ ] **Step 4: create_app() docstring**

在 `src/app/bootstrap.py` 中，将：

```python
async def create_app(config_path: str = "config.yaml") -> AgentApp:
    """读配置 → 创建所有组件 → 注入依赖 → 返回 AgentApp。"""
```

替换为：

```python
async def create_app(config_path: str = "config.yaml") -> AgentApp:
    """应用组装入口 — 整个框架唯一的具体实现实例化点。

    组装流程：
    1. 加载 config.yaml + .env
    2. 创建 LLM provider（OpenAIProvider）
    3. 发现并注册本地工具，构建中间件管道
    4. 连接 MCP 服务器，注册外部工具
    5. 发现技能
    6. 注册预设智能体，构建默认图
    7. 组装 AgentDeps 依赖容器
    8. 返回 AgentApp 实例
    """
```

- [ ] **Step 5: AgentApp docstring**

在 `src/app/app.py` 中，将：

```python
class AgentApp:
    """应用核心：消息路由 + REPL。组件由 bootstrap 注入。"""
```

替换为：

```python
class AgentApp:
    """应用核心 — 消息路由 + REPL 循环。

    所有组件由 bootstrap.py 注入，AgentApp 不创建任何具体实现。
    消息路由逻辑：
    - 所有输入先经过 InputGuardrail 安全检查
    - /plan 命令 → PlanFlow 多步骤规划执行
    - /skill-name → SkillManager 激活技能，构建独立图执行
    - 普通消息 → 默认图（orchestrator → 专家智能体）
    """
```

- [ ] **Step 6: GraphEngine docstring**

在 `src/graph/engine.py` 中，将：

```python
class GraphEngine:
    """异步图执行器。Agent 无关 — 只负责图的遍历和执行。"""
```

替换为：

```python
class GraphEngine:
    """异步图执行器 — 与智能体无关的通用图遍历引擎。

    执行模型：
    1. 维护 pending 节点列表，从 entry 开始
    2. 检查 pending 是否匹配 ParallelGroup → asyncio.gather 并行执行
    3. 否则顺序执行单节点
    4. 每个节点的 output 写入 context.state（可通过 $node_name 引用）
    5. 根据 NodeResult 的 handoff / next / edges 决定下一步
    6. max_handoff_depth 防止无限循环
    """
```

- [ ] **Step 7: PlanCompiler docstring**

在 `src/plan/compiler.py` 中，将：

```python
class PlanCompiler:
    """将 Plan 编译为 CompiledGraph。"""
```

替换为：

```python
class PlanCompiler:
    """将 Plan 编译为 CompiledGraph。

    编译流程：
    1. 验证：检查 Step ID 唯一性、agent 存在性
    2. 分层拓扑排序：同层步骤无互相依赖，可并行执行
    3. 每个 Step 生成 FunctionNode 闭包（工具调用或智能体调用）
    4. 单步骤层 → 顺序 Edge；多步骤层 → ParallelGroup + merge 节点
    5. 支持 $step_id.field 变量引用，运行时从 context.state 解析
    """
```

- [ ] **Step 8: FactExtractor docstring**

在 `src/memory/extractor.py` 中，将：

```python
class FactExtractor:
    """从对话中提取结构化记忆事实。"""
```

替换为：

```python
class FactExtractor:
    """从对话中提取结构化记忆事实。

    提取策略：
    1. 通过 LLM + submit_facts 工具调用，强制输出结构化 JSON
    2. 支持 5 大类约 25 种事实类型（user, assistant, world, conversation, interaction）
    3. 置信度调整：检测模糊词（"可能""也许"）降低、强确定词（"肯定""一定"）提高
    4. 多重过滤：敏感信息、不合理事实（is_plausible=False）、低置信度、无 attribute
    5. include_types 参数可限制只提取特定类型的事实
    """
```

- [ ] **Step 9: calculate_importance docstring**

`src/memory/decay.py` 中的 `calculate_importance()` 已有完整 docstring，无需修改。

- [ ] **Step 10: 提交 docstring 增强**

```bash
git add src/memory/base.py src/tools/router.py src/graph/types.py src/app/bootstrap.py src/app/app.py src/graph/engine.py src/plan/compiler.py src/memory/extractor.py
git commit -m "docs: enhance docstrings for key protocols and components"
```
