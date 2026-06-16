# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在此仓库中工作时的指导文件。

## 开发命令

```bash
uv sync                              # 安装依赖
uv run python main.py                # 运行应用
uv run python main.py --workdir /path  # 指定工作目录运行
uv run pytest                        # 运行全部测试
uv run pytest -k "test_name"         # 运行匹配名称的测试
uv run pytest tests/test_foo.py      # 运行单个测试文件
```

项目要求 Python >= 3.13，使用 `uv` 管理依赖。无 lint/format 工具配置。

## 架构概述

本项目是一个自研的 AI Agent CLI 框架，采用 Python asyncio 构建，分为四层：

**入口与组装层** — `main.py` 解析 CLI 参数，`src/app/bootstrap.py` 中的 `create_app()` 是唯一的依赖组装点，手动构造所有 Manager 并注入 `AgentDeps` dataclass。

**应用主循环层** — `src/app/app.py` 中的 `AgentApp` 管理外层 REPL：启动 UI → 消费事件 → 驱动 Agent 轮次 → 处理中断 → 会话 Hook → 关闭。

**Agent 状态机层** — `src/agent/agent.py` 中的 `Agent` 是有限状态机，由 `_handlers: dict[AgentState, Callable]` 驱动。状态流转：
```
REQUEST_INPUT → CHECK_COMPACT → [COMPACT →] LLM_CALL → PROCESS_RESPONSE
→ [EXECUTE_TOOLS → POST_ROUND → CHECK_COMPACT] → CHECK_STOP → DONE
```
`RunContext` 持有每轮的可变状态，避免线程/异步冲突。

**Manager 服务层** — `src/mgr/` 下的各 Manager 类各司其职：`LLMMgr`（模型管理）、`ToolsMgr`（工具注册与执行）、`PermissionManager`（6 步权限检查）、`CompactMgr`（上下文压缩）、`PromptMgr`（系统提示词构建）、`SubAgentMgr`（子智能体调度）、`SkillMgr`（技能加载）等。

### 事件驱动 I/O

所有输出和输入通过 `EventBus`（`src/events/bus.py`）以类型化事件流转，不直接调用 UI。事件类型定义在 `src/events/types.py`。

### 配置系统

3 层合并，后者覆盖前者：内置 `src/config.yaml` → 全局 `~/.agent/config.yaml` → 项目 `.agent/config.yaml`。环境变量通过 `.env` 文件加载（全局 `~/.agent/.env`、项目 `.agent/.env`）。

## 关键模式

**工具注册** — 使用 `@tool` 装饰器（`src/tools/decorator.py`）+ Pydantic 参数模型，自动注册到全局 `_registry`。工具实现在 `src/tools/builtin/`。新增工具时须确认是否需要自动注入给子智能体（见 `subagent_mgr._AUTO_INJECT_TOOLS`）。

**子智能体** — 定义为 `src/agent/agents/*.md`，带 YAML frontmatter 声明 `agent_type`、`tools`、`model`、`memory` 等。主 Agent 通过 `task_delegator` 工具调度子智能体，每个子智能体是共享 `AgentDeps` 的完整 `Agent` 实例。

**技能系统** — 5 层加载 `SKILL.md` 文件，通过 `load_skill` 工具按需注入系统提示词。

**Hooks** — 8 种生命周期钩子事件（`PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`Stop`、`SessionStart`、`SessionEnd`、`SubagentStart`、`SubagentStop`），通过 shell 命令执行，支持 JSON stdin/stdout 协议。

**`reload()` 协议** — 有状态的 Manager 实现 `reload()` 方法，`/clear` 重置会话时通过 `hasattr` 发现并统一调用。

## 函数设计、注释与命名规范

在新增或修改函数时，必须遵守以下规则：

1. **避免重复逻辑**：如果两个或多个函数使用了相似的逻辑，应合并成一个通用函数，避免代码重复。

2. **避免过度抽象**：不要为了"统一"而强行拆分或封装函数。例如：若 `on_enter()`、`on_exit()`、`on_fire()` 等函数内部仅仅是调用 `on_event(event_type, xxx)`，则应直接使用 `on_event` 或最多封装一个通用的 `on_event` 函数，而不必保留多个仅转发调用的函数。优先保持代码直观、扁平，减少不必要的间接层。

3. **添加注释**：为每个新增或修改的函数添加注释，注释中需清晰说明该函数的功能。

4. **命名规范**：函数命名必须与其实际用途强相关，做到"见名知义"。

5. **参数类型**：函数参数需明确指定具体类型。

6. **注释完整**：对每个传入参数和返回值都要编写注释，说明其含义。

7. **全面检索引用**：调整或重构函数时，必须检索所有调用或引用该函数的地方，确保修改没有遗漏，避免因部分更新导致不一致或错误。

添加或修改管理器、工具的时候，如果涉及工作流的变化，需要同步添加、更新提示词，用以提示llm如何工作。

提示词不要有模糊描述，需要确定性的指导，可落地的。

不要为流优化而优化：除非优化明确有效，否则不要优化。
