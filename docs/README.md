# 技术文档索引

本目录是本框架**面向人类开发者与运维者**的系统技术参考：既讲清每个模块/Manager 的职责与关键方法（开发者视角），又讲清每个配置项能填什么、有什么效果（运维视角）。所有事实以源码为准，涉及源码处给出 `文件:行` 引用。

> 与根目录文档的分工：`README.md`（面向使用者的简介）、`CLAUDE.md`（面向 AI 助手的精简工作指引）、本 `docs/`（面向人的完整参考）。

## 快速开始

```bash
uv sync                                 # 安装依赖（要求 Python >= 3.13）
uv run python main.py                    # 运行应用（默认工作目录为当前目录）
uv run python main.py --workdir /path    # 指定工作目录运行
uv run python main.py --debug            # 启用 asyncio 慢回调告警（>0.1s 即告警），排查阻塞事件循环的代码
uv run pytest                            # 运行全部测试
```

入口链路：`main.py`（解析 `--workdir`/`--debug`）→ `app.bootstrap.create_app()`（唯一依赖组装点）→ `AgentApp.run()`。默认模型不可用时打印可操作提示并以非零码退出（`ModelUnavailableError`）。

## 四层架构一图

```
┌─────────────────────────────────────────────────────────────┐
│ 入口与组装层   main.py → bootstrap.create_app() → AgentDeps   │  见 architecture.md
├─────────────────────────────────────────────────────────────┤
│ 应用主循环层   AgentApp：启动 UI→消费事件→驱动轮次→中断→关闭  │  见 agent-runtime.md
├─────────────────────────────────────────────────────────────┤
│ Agent 状态机层  Agent（_handlers: dict[AgentState, Callable]）│  见 agent-runtime.md
├─────────────────────────────────────────────────────────────┤
│ Manager 服务层  RoleMgr/LLMMgr/ToolsMgr/PermissionManager/... │  见 managers.md
└─────────────────────────────────────────────────────────────┘
       ↕ 所有 I/O 经 EventBus 以类型化事件流转（见 events-and-ui.md）
```

**角色（Role）是框架的顶层组织单位**——一套角色决定主 agent 的身份提示词、可用子 agent、技能、MCP server 与启用的 feature 集（见 roles-subagents-skills.md）。

## 文档导航

| 文档 | 内容 |
|---|---|
| [architecture.md](architecture.md) | 四层架构、`create_app()` 装配顺序、`AgentDeps` 字段、feature 门控总览、`reload()` 协议、路径解析（`paths.py`） |
| [agent-runtime.md](agent-runtime.md) | `AgentApp` 外层 REPL、`AgentState` 全枚举与边缘状态、`RunContext`、逐个 handler、`Agent.from_manifest`、`PermissionModeController` |
| [managers.md](managers.md) | Manager 服务层参考：逐个 Manager 的职责/公共方法/消费配置/reload/feature 门控 |
| [llm.md](llm.md) | `LLMProvider` 基类与 `LLMResponse`、`chat()` 并发与重试、分页、四个 provider（Anthropic/OpenAI/DeepSeek/Ollama）差异 |
| [tools.md](tools.md) | `@tool` 装饰器、`ToolEntry`/`ToolPermission`、`subagent`/`feature` 门控、`ToolsMgr.execute` 流水线、内置工具清单 |
| [roles-subagents-skills.md](roles-subagents-skills.md) | 角色系统（三层发现、`role.md`、`common/`）、子智能体（四层扫描、frontmatter、委派）、技能系统、内置 agent 清单 |
| [permissions.md](permissions.md) | 6 种权限模式、6 步评估顺序、`PermissionRule` 规则格式、`resolve_ask` 选项、按 agent 独立模式、工具可见性 |
| [mcp-and-hooks.md](mcp-and-hooks.md) | MCP 三层合并/三种 transport/工具命名/per-server 权限/server 开关；Hooks 8 事件/配置格式/JSON 协议/退出码 |
| [events-and-ui.md](events-and-ui.md) | `EventBus` API、`EventLevel` 门控、事件目录表、`UserInterface`/`InlineInterface`、`OutputRouter` 多 agent 路由、Markdown 流式渲染、斜杠补全 |
| [configuration-reference.md](configuration-reference.md) | 配置总参考：三层合并规则、`config.yaml`/`settings.json`/`mcp_servers.json` 完整 schema、环境变量、目录布局 |

## 按主题跳转

- **想改配置**：先看 [configuration-reference.md](configuration-reference.md)（`config.yaml` 逐键）+ [permissions.md](permissions.md)（`settings.json` 权限）+ [mcp-and-hooks.md](mcp-and-hooks.md)（`mcp_servers.json`）。
- **想加工具**：[tools.md](tools.md)（`@tool` + 异步/阻塞契约）+ [managers.md](managers.md)（`ToolsMgr`）。
- **想加角色/子 agent/技能**：[roles-subagents-skills.md](roles-subagents-skills.md)。
- **想懂运行流程**：[agent-runtime.md](agent-runtime.md)（状态机）+ [architecture.md](architecture.md)（装配）+ [events-and-ui.md](events-and-ui.md)（I/O）。
- **想接外部 server / 生命周期钩子**：[mcp-and-hooks.md](mcp-and-hooks.md)。
