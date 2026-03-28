# 项目文档体系设计

## 背景

agent 是一个从零构建的 Python AI Agent 框架，定位为潜在开源项目。目前代码架构完整，但缺少面向用户的文档。需要建立一套完整的文档体系，服务三类受众：使用者、开发者、AI 工具（如 Claude Code）。

## 设计决策

- **语言**：中文为主
- **现有文档**：`docs/superpowers/` 保留原样，新文档独立存放
- **包管理**：使用 uv，不直接使用 python
- **AI 文档策略**：CLAUDE.md 提供全局上下文 + 关键代码 docstring 提供局部细节

## 文件结构

```
README.md                           ← 项目首页（重写）
CLAUDE.md                           ← AI 工具项目指令（新建）
docs/
├── guide/                          ← 面向使用者（新建）
│   ├── getting-started.md
│   ├── configuration.md
│   └── skills-and-tools.md
├── architecture/                   ← 面向开发者（新建）
│   ├── overview.md
│   ├── llm.md
│   ├── agents.md
│   ├── memory.md
│   ├── tools.md
│   ├── graph.md
│   └── plan.md
├── contributing.md                 ← 贡献指南（新建）
└── superpowers/                    ← 已有设计文档（保留不动）
    ├── specs/
    └── plans/
```

---

## 一、README.md（项目首页）

重写现有 README.md，作为项目门面。

### 结构

1. **标题 + 一句话定位**：从零构建的 Python AI Agent 框架，支持多智能体协作、长期记忆和动态规划
2. **特性亮点**：
   - 多智能体系统（编排器 + 专家智能体，支持 handoff）
   - 向量记忆系统（ChromaDB，事实提取，重要性衰减）
   - 图执行引擎（支持顺序/并行节点、handoff 路由）
   - 动态规划系统（LLM 驱动的多步骤计划生成与执行）
   - MCP 协议集成
   - 技能系统（Markdown 定义，斜杠命令激活）
   - 输入安全守卫
   - 端口-适配器架构，Protocol 接口可扩展
3. **快速开始**：
   - 环境要求（Python 3.13+, uv, Ollama）
   - 安装步骤：`uv sync`
   - 配置 `.env` 和 `config.yaml`
   - 运行：`uv run python main.py`
4. **项目结构**：简要目录树 + 一行说明
5. **配置说明**：指向 `docs/guide/configuration.md`
6. **文档导航**：
   - 使用指南 → `docs/guide/`
   - 架构文档 → `docs/architecture/`
   - 贡献指南 → `docs/contributing.md`
7. **License**

---

## 二、CLAUDE.md（AI 工具项目指令）

放在项目根目录，Claude Code 每次对话自动加载。

### 内容

1. **项目概述**：从零构建的 Python AI Agent 框架，端口-适配器架构
2. **技术栈**：Python 3.13, uv 管理, DeepSeek (OpenAI 兼容), ChromaDB + Ollama embedding, asyncio 全链路
3. **架构分层**：
   - Layer 0: config, utils, interfaces, graph, guardrails（无外部依赖）
   - Layer 1: llm, tools（依赖 Layer 0）
   - Layer 2: memory, agents, plan, mcp, skills（依赖 Layer 0-1）
   - Layer 3: src/app/（应用层，组装所有具体实现）
4. **关键约定**：
   - 所有可插拔组件使用 Protocol 接口（LLMProvider, MemoryProvider, ToolProvider 等）
   - 具体实现只在 `src/app/bootstrap.py` 中实例化
   - 工具通过 `@tool` 装饰器注册，支持中间件管道
   - 配置集中在 `config.yaml` + `.env`
5. **开发规范**：
   - 每次完成任务后，如有必要，同步更新相关文档和测试用例
   - 类型安全：所有函数签名必须有类型注解，使用 Protocol 而非具体类型，善用 TypedDict/dataclass/Pydantic model 定义数据结构
6. **常用命令**：
   - `uv sync` — 安装依赖
   - `uv run python main.py` — 启动 agent
   - `uv run pytest` — 运行测试
7. **当前状态**：
   - weather/calendar/email 专家智能体的工具是占位符
   - 参见 TODO 文件了解未来计划
8. **文件导航**：
   - 入口: `main.py` → `src/app/bootstrap.py` → `src/app/app.py`
   - 核心接口: `src/interfaces/`, `src/llm/base.py`, `src/memory/base.py`, `src/tools/router.py`
   - 配置: `config.yaml`, `.env`

---

## 三、docs/guide/（使用指南）

面向使用者，3 个文件。

### docs/guide/getting-started.md

1. **环境准备**：Python 3.13+, uv（包管理）, Ollama（本地 embedding 服务）
2. **安装**：git clone + `uv sync`，启动 Ollama 并拉取 embedding 模型
3. **配置**：`.env` 文件（API Key）、`config.yaml` 基本字段说明
4. **第一次运行**：`uv run python main.py`，示例对话演示
5. **常见问题**：Ollama 连接失败、API Key 无效、模型不支持

### docs/guide/configuration.md

1. **config.yaml 完整字段说明**：llm, embedding, memory, user 配置
2. **.env 环境变量**：OPENAI_API_KEY 用途和获取方式
3. **切换 LLM 提供商**：DeepSeek / OpenAI / 其他兼容接口示例

### docs/guide/skills-and-tools.md

1. **内置工具**：计算器（calculator）、文件读写（file）
2. **MCP 工具集成**：mcp_servers.json 配置说明、如何添加新 MCP 服务器
3. **技能系统**：技能文件格式、如何创建自定义技能、斜杠命令使用方式
4. **规划系统**：`/plan` 命令的使用流程（澄清 → 生成 → 确认 → 执行）

---

## 四、docs/architecture/（架构文档）

面向开发者，按模块拆分。每个文件覆盖：职责、核心类/Protocol、数据流、扩展方式。

### docs/architecture/overview.md

1. **设计哲学**：从零构建、端口-适配器架构、Protocol 定义接口、具体实现只在 bootstrap.py 组装
2. **分层架构图**（Layer 0-3）：依赖关系图 + 每层职责
3. **请求处理流程**：用户输入 → 守卫检查 → 路由（普通/plan/skill）→ 图执行 → 响应
4. **模块依赖关系图**

### 各模块文档

| 文件 | 覆盖内容 |
|------|----------|
| `llm.md` | LLMProvider Protocol、OpenAIProvider 实现、流式输出、重试机制、结构化输出 |
| `agents.md` | Agent 模型、AgentRunner 工具循环、handoff 机制、守卫系统、AgentDeps 依赖注入 |
| `memory.md` | MemoryProvider Protocol、ChromaDB 实现、事实提取器、对话缓冲区、重要性衰减算法 |
| `tools.md` | @tool 装饰器、注册表、执行器、中间件管道、ToolProvider/ToolRouter、MCP 集成 |
| `graph.md` | GraphNode Protocol、GraphBuilder、GraphEngine、顺序/并行执行、handoff 路由、trace |
| `plan.md` | 规划流程（5 阶段）、Step/Plan 模型、PlanCompiler 拓扑排序、变量引用 |

### docs/contributing.md

1. **开发环境搭建**：`uv sync` + 配置 `.env`
2. **代码规范**：Protocol 优先、分层依赖（低层不导入高层）、异步优先、类型安全（所有函数签名必须有类型注解，优先使用 Protocol/TypedDict/dataclass/Pydantic model）
3. **添加新模块的步骤**：定义 Protocol → 实现 → bootstrap.py 注册 → 测试 → 更新文档
4. **测试**：`uv run pytest`，测试目录结构镜像 `src/`
5. **提交规范**：feat/fix/refactor/test/docs 前缀

---

## 五、代码内 docstring 增强

配合 CLAUDE.md，在关键入口添加 docstring。

### 增强范围

| 位置 | 增强内容 |
|------|----------|
| Protocol 类（`LLMProvider`, `MemoryProvider`, `ToolProvider`, `GraphNode`, `UserInterface`） | 类 docstring：职责 + 使用方式 |
| `src/app/bootstrap.py` 的 `create_app()` | 组装流程说明 |
| `src/app/app.py` 的 `AgentApp` | 路由逻辑说明 |
| `src/graph/engine.py` 的 `GraphEngine` | 执行模型说明 |
| `src/plan/compiler.py` 的 `PlanCompiler` | 编译流程说明 |
| `src/memory/extractor.py` 的 `FactExtractor` | 提取策略说明 |
| `src/memory/decay.py` 的 `calculate_importance()` | 算法公式说明 |

### 不做的事

- 不给每个函数加 docstring
- 不加冗余的参数说明（类型注解已足够）
- 不改动业务逻辑
