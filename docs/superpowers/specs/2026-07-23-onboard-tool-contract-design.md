# Onboard 子 Agent 工具契约修复设计

## 目标

使 `src/roles/onboard/agents/*.md` 中声明的每个工具，都能在当前 Agent 框架的运行时注册表中获得，并保留现有可用的本地文件工具。

## 现状与边界

`glob` 和 `grep` 是 `src/tools/builtin/file.py` 中以 `@tool` 注册的本地 `file` feature 工具，不属于 shell 命令声明问题，保持不变。

对 `codebase-memory` MCP 的真实 `list_tools` 握手只注册以下工具：

- `get_architecture`
- `get_code_snippet`
- `get_graph_schema`
- `index_repository`
- `query_graph`
- `search_code`
- `search_graph`
- `trace_path`

因此下列声明在当前框架中不可调用：

- `repository-map`：`index_status`、`list_projects`
- `module-analyst`、`dimension-classifier`（runtime-flow 与 guardrails 维度）：`trace_call_path`

## 方案

1. 从上述四份子 Agent frontmatter 中删除不可注册的 MCP 工具名。
2. 只使用已注册工具改写相关提示词：
   - `trace_call_path` 的调用图检查统一使用 `trace_path`，必要时配合 `search_graph`、`query_graph` 与 `search_code`。
   - `repository-map` 不再要求 `index_status`；改为记录 `index_repository` 的返回结果，并使用 `get_architecture` 与 `get_graph_schema` 核验可分析的结构和覆盖缺口。
3. 扩展 `tests/test_onboard_role.py`：解析每份 onboard 子 Agent manifest，断言其非 MCP 工具均由 `ToolsMgr` 注册；同时以当前已握手的 `codebase-memory` MCP 工具契约校验声明的 MCP 名称。

## 验收

- 每个 onboard 子 Agent 的每个声明工具均属于对应的内置或 MCP 注册集合。
- 角色提示词不再引用 `trace_call_path` 或用于工具调用的 `index_status`/`list_projects`。
- `uv run pytest tests/test_onboard_role.py` 通过；随后运行完整测试集。

