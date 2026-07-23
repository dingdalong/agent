# Agent

基于 Python asyncio 的 AI Agent CLI 框架。通过状态机驱动 Agent 运行，支持子智能体调度、技能编排和多 LLM 提供商切换。

## 特性

- **多 LLM 提供商** — 支持 DeepSeek、OpenAI、Anthropic、Ollama，可通过配置自由切换
- **Agent 状态机** — 有限状态机驱动 Agent 生命周期，支持子智能体调度与任务委派
- **内置工具集** — 文件操作、Shell 执行、Web 搜索与抓取、任务管理、计算器等
- **技能系统** — 通过 SKILL.md 定义可复用工作流，按需加载到上下文中
- **权限管理** — 6 种权限模式 + 基于规则的权限引擎，支持生命周期 Hooks
- **事件驱动 I/O** — 类型化事件总线解耦 Agent 逻辑与 UI 渲染，支持流式 Markdown 输出
- **3 层配置** — 内置默认 → 全局 `~/.agent/` → 项目 `.agent/`，逐层覆盖合并
- **插件系统** — 全局和项目级插件目录，可扩展工具、子智能体、技能
- **会话管理** — 会话持久化到磁盘，支持恢复历史会话
- **上下文压缩** — 自动检测上下文用量并压缩历史，保留近期对话完整性
- **记忆系统** — 项目级长期记忆，跨会话持久化关键信息

## 快速开始

### 环境要求

- Python >= 3.13
- [uv](https://github.com/astral-sh/uv) 包管理器

### 安装与运行

```bash
# 安装依赖
uv sync

# 运行
uv run python main.py

# 指定工作目录运行
uv run python main.py --workdir /path/to/project
```

### 配置 API Key

通过环境变量或 `.env` 文件设置 LLM 提供商的 API Key：

```bash
# 全局配置（所有项目生效）
# ~/.agent/.env
DEEPSEEK_API_KEY=your-key
OPENAI_API_KEY=your-key
ANTHROPIC_API_KEY=your-key

# 项目级配置（仅当前项目生效）
# .agent/.env
DEEPSEEK_API_KEY=your-key
```

## 架构

四层架构设计：

```
入口与组装层    main.py / bootstrap.py — CLI 参数解析，依赖组装
      ↓
应用主循环层    AgentApp — REPL 循环，事件消费，中断处理
      ↓
Agent 状态机层  Agent — 有限状态机驱动每轮对话
      ↓
Manager 服务层  LLM / Tools / Permission / Compact / Prompt / ... — 各司其职
```

Agent 状态流转：

```
REQUEST_INPUT → CHECK_COMPACT → [COMPACT →] LLM_CALL → PROCESS_RESPONSE
→ [EXECUTE_TOOLS → POST_ROUND → CHECK_COMPACT] → CHECK_STOP → DONE
```

## 配置

3 层配置合并，后者覆盖前者：

1. 内置默认 — `src/config.yaml`
2. 全局配置 — `~/.agent/config.yaml`
3. 项目配置 — `.agent/config.yaml`

权限规则和 Hooks 通过 `settings.json` 配置（同样支持全局和项目两级）。

环境变量 `{PROVIDER}_API_KEY` 和 `{PROVIDER}_API_URL` 可覆盖对应提供商的配置。

## 开发

```bash
uv run pytest                        # 运行全部测试
uv run pytest -k "test_name"         # 运行匹配名称的测试
uv run pytest tests/test_foo.py      # 运行单个测试文件
```

### 项目结构

```
├── AGENTS.md               # 项目级 Agent 行为指令
├── main.py                 # CLI 入口
├── src/
│   ├── config.yaml         # 内置默认配置
│   ├── agent/              # Agent 状态机 + 子智能体定义
│   ├── app/                # 应用主循环 + 依赖组装
│   ├── events/             # 事件总线 + 事件类型
│   ├── interfaces/         # CLI 界面 + Markdown 渲染
│   ├── llm/                # LLM 提供商实现
│   ├── mgr/                # 各 Manager 服务
│   ├── skills/             # 内置技能
│   └── tools/              # 工具注册器 + 内置工具
└── tests/                  # 测试
```
