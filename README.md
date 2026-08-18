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

# 核对随包资源与内置工具/命令注册是否完好（主要用于验证打包产物）
uv run python main.py --self-check
```

### 打包分发

分发物是**解压即用的可执行包**，使用者机器无需 Python、无需装依赖，首次启动也不需要联网。

```bash
# 构建当前平台的可执行包
make build

# 复用已预热的 tiktoken 缓存重建（改依赖或换 tiktoken 版本后请用 make build）
make rebuild

# 对构建产物跑冻结态冒烟测试
make check

# 构建并把产物安装到 ~/.local/bin
make install

# 清理构建产物
make clean
```

产物为 `dist/agent-{版本}-{os}-{arch}.tar.gz`（Windows 为 `.zip`），压缩包内含安装脚本。

**PyInstaller 无法交叉编译**：`make build` 只产出当前平台的包，名字如实标注平台。三平台
发布由 `.github/workflows/release.yml` 在各自 runner 上跑同一个脚本完成，push tag 触发。

#### 使用者侧

一行安装，装完在任意目录敲 `agent` 即可，工作目录就是启动时所在目录：

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/dingdalong/agent/main/scripts/install.sh | sh
```

```bat
:: Windows
curl -fsSL https://raw.githubusercontent.com/dingdalong/agent/main/scripts/install.bat -o "%TEMP%\agent-install.bat" && "%TEMP%\agent-install.bat"
```

也可以下载 release 里的压缩包，解压后运行包内的 `install.sh`（Windows 为 `install.bat`）。

安装器会改动这些位置：

| 位置 | 说明 |
| --- | --- |
| `~/.local/share/agent/<版本>/` | 产物本体。Windows 为 `%LOCALAPPDATA%\Programs\agent\<版本>\` |
| `~/.local/bin/agent` | 指向上面那个目录的符号链接。Windows 为 `bin\agent.bat` 转发脚本 |
| shell 配置 | 仅当 `~/.local/bin` 不在 PATH 上时，追加一段带 `agent installer` 标记的 PATH 导出；幂等，重复安装不重复写。Windows 改写用户 PATH 环境变量 |

产物是 onedir 形态，`agent` 必须与同级 `_internal/` 在一起，所以不能只把二进制拷进
`~/.local/bin`——安装脚本做的就是"落到固定位置再建链接"这件事。

常用选项：`--from <解压目录>` 跳过下载、`--version <tag>` 指定版本、`--keep-old` 保留旧版本、
`--verify` 装完跑自检、`--uninstall` 卸载（同时移除 shell 配置里那段，并留下 `.agent.bak` 备份）。

macOS 上的隔离标记由安装器自动去除（产物未签名，否则首次运行会被 Gatekeeper 拦下）。
Windows 上未签名的 exe 可能被 SmartScreen 或 Defender 提示，属预期。

`agent --self-check` 会核对随包资源、内置工具/命令注册与离线编码，输出 JSON 报告——
打包引入的失效大多是「能启动但不干活」，用它可以直接判定产物是否完好。

**已知限制**：用户自定义 slash 命令（`~/.agent/commands/*.py`）在可执行包里只能 import
已被打包的模块，import 额外的第三方库会失败。需要这类扩展时请从源码运行。

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

`config.yaml` 的 `role.default` 选定激活角色（缺省回退 `coding`）；每个角色必须配置 `role.<角色名>.model.default/fast` 两个模型槽位，主 agent 恒用 default，轻量子 agent 与智能权限裁决可用 fast。`role.<角色名>.reasoning_effort` 是两个槽位共用的调用级覆盖。MCP 连接开关和 Hooks 通过 `settings.json`（全局 + 项目两层）配置；MCP server 连接配置在独立的 `mcp_servers.json`（角色 → 全局 → 项目三层）。工具授权使用内置声明式策略、代码级安全规则和逐次智能权限裁决，不从用户配置提升权限。

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
