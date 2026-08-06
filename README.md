# Agent

基于 Python asyncio 的 AI Agent CLI 框架。以**角色（Role）**为顶层组织单位，由有限状态机驱动 Agent 运行，支持子智能体调度、技能编排、MCP 工具接入和多 LLM 提供商切换，默认提供 Textual TUI 界面。

## 特性

- **角色系统** — 角色是顶层组织单位，一套角色决定主 agent 的身份提示词、子 agent、技能、MCP server 与启用的 feature 集；内置 `coding`（默认）、`mijia`、`onboard` 等，支持自定义
- **多 LLM 提供商** — 支持 DeepSeek、OpenAI、Anthropic、Ollama、Moonshot，可通过配置自由切换；部分提供商支持原生搜索等能力
- **Agent 状态机** — 有限状态机驱动 Agent 生命周期，支持子智能体调度与任务委派、上下文溢出与长度重试等边缘处理
- **内置工具集** — 文件操作、Shell 执行、Web 搜索与抓取、任务管理、计划（Plan）、记忆、计算器等
- **技能系统** — 通过 SKILL.md 定义可复用工作流，按需加载到上下文中
- **权限管理** — 声明式工具策略、代码级高危拦截、智能权限（LLM 裁决 deny/pass/ask）与一次性人工确认
- **MCP 集成** — 接入 MCP server 扩展工具，支持白名单/黑名单开关
- **Hooks** — 8 种生命周期钩子（PreToolUse、PostToolUse、UserPromptSubmit、Stop、SessionStart/End、SubagentStart/Stop），通过 shell 命令执行
- **事件驱动 I/O** — 类型化事件总线解耦 Agent 逻辑与 UI 渲染，支持流式 Markdown 输出
- **3 层配置** — 内置默认 → 全局 `~/.agent/` → 项目 `.agent/`，逐层覆盖合并
- **插件系统** — 角色级、全局和项目级插件目录，可扩展工具、子智能体、技能
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

# 启用 asyncio 慢回调告警（排查事件循环阻塞）
uv run python main.py --debug
```

### 打包分发

```bash
# 构建（同时产出在线 wheel 和离线安装包）
make build

# 清理构建产物
make clean
```

用户安装：

```bash
# 在线安装（自动从 PyPI 拉取依赖）
pip install ./agent-0.1.0-py3-none-any.whl

# 离线安装（无需网络）
tar xzf agent-0.1.0-offline.tar.gz
pip install --no-index --find-links deps/ agent-0.1.0-py3-none-any.whl
```

安装后直接运行 `agent` 命令即可。

### 配置 API Key

通过环境变量或 `.env` 文件设置 LLM 提供商的 API Key：

```bash
# 全局配置（所有项目生效）
# ~/.agent/.env
DEEPSEEK_API_KEY=your-key
OPENAI_API_KEY=your-key
ANTHROPIC_API_KEY=your-key
MOONSHOT_API_KEY=your-key

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
Manager 服务层  Role / LLM / Tools / Permission / Compact / Prompt / ... — 各司其职
```

Agent 状态流转（happy path）：

```
REQUEST_INPUT → CHECK_COMPACT → [COMPACT →] LLM_CALL → PROCESS_RESPONSE
→ [EXECUTE_TOOLS → POST_ROUND → CHECK_COMPACT] → CHECK_STOP → DONE
```

另有边缘/退出状态：`LENGTH_RETRY`（长度截断重试）、`CONTEXT_OVERFLOW`（上下文溢出）、`SUMMARIZE_EXIT`（退出前总结）。

## 配置

3 层配置合并，后者覆盖前者：

1. 内置默认 — `src/config.yaml`
2. 全局配置 — `~/.agent/config.yaml`
3. 项目配置 — `.agent/config.yaml`

`config.yaml` 的 `role.default` 选定激活角色（缺省回退 `coding`）；`role.<角色名>.model` 和 `role.<角色名>.reasoning_effort` 可覆盖该主角色 `role.md` 的同名字段。未覆盖时分别保留 `role.md` 的值，并最终由 `llm.default` 和 provider 推理强度兜底。MCP 连接开关和 Hooks 通过 `settings.json`（全局 + 项目两层）配置；MCP server 连接配置在独立的 `mcp_servers.json`（角色 → 全局 → 项目三层）。工具授权使用内置声明式策略、代码级安全规则和逐次智能权限裁决，不从用户配置提升权限。

环境变量 `{PROVIDER}_API_KEY` 和 `{PROVIDER}_API_URL` 可覆盖对应提供商的配置。

## 开发

```bash
# 安装依赖（含 pytest 等开发工具）
uv sync --extra dev

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
│   ├── agent/              # Agent 状态机
│   ├── app/                # 应用主循环 + 依赖组装
│   ├── events/             # 事件总线 + 事件类型
│   ├── interfaces/         # TUI/CLI 界面 + Markdown 渲染
│   ├── llm/                # LLM 提供商实现
│   ├── mgr/                # 各 Manager 服务
│   ├── roles/              # 内置角色（含共享 common/）
│   ├── tools/              # 工具注册器 + 内置工具
│   └── web/                # Web 搜索/抓取后端
└── tests/                  # 测试
```

## 深入参考

完整的面向人的技术文档见 [`docs/`](docs/README.md)（架构、运行时、配置参考、权限、角色/子智能体/技能、MCP 与 Hooks 等）。
