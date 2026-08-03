# MCP 集成与 Hooks

两个外部扩展机制：**MCP**（Model Context Protocol）把外部 server 的工具接入框架；**Hooks** 在生命周期节点执行 shell 命令以观察/干预流程。分别由 `McpMgr`（`src/mgr/mcp_mgr.py`）与 `HooksMgr`（`src/mgr/hooks_mgr.py`）管理。

## MCP 集成

### 配置来源与三层合并

MCP server 连接配置写在 `mcp_servers.json`，格式为 `{"mcpServers": {"<name>": {<spec>}}}`。`McpMgr.start()`（`mcp_mgr.py:122-174`）按低→高优先级合并三层，同名 server 后者覆盖：

| 层 | 来源 | 说明 |
|---|---|---|
| 角色 | `role_mgr.mcp_servers_path()` | 激活角色目录下的 `mcp_servers.json` |
| 全局 + 项目 | `config_mgr.load_mcp_servers()` | `~/.agent/mcp_servers.json` → `<项目>/.agent/mcp_servers.json`（project 覆盖 global） |

合并后经 `_apply_server_policy()` 过滤（见下）。若没有任何 server 或未安装 `mcp` 包，整体跳过且不影响主流程。项目层和项目角色层 server 只有通过 `ProjectTrustGate` 才会加载。

### 连接模型

每个 server 在**专属常驻 asyncio 任务**里打开连接（`_serve`，`mcp_mgr.py:229-254`）：transport + `ClientSession` 的 async 上下文在同一任务进入并退出，规避 anyio cancel-scope 跨任务错误；工具调用从其他任务复用同一 session 的消息流。单 server 连接超时 `_CONNECT_TIMEOUT = 30.0s`，连接失败仅记日志并跳过。`stop()`（`mcp_mgr.py:215-227`）置停止事件，等待各任务清退（`_CLOSE_TIMEOUT = 5.0s`），超时强制取消。所有方法只 `await` 真异步原语，满足[异步/阻塞契约](architecture.md)。

### 三种 transport

`_open_session()`（`mcp_mgr.py:256-297`）按 `spec.transport`（默认 `stdio`）分派：

| transport | 别名 | 必需字段 | 可选字段 | 说明 |
|---|---|---|---|---|
| `stdio` | — | `command` | `args`、`env` | 启动子进程；使用 DataGuard 安全环境并叠加可信配置显式 env；子进程 stderr → DEVNULL |
| `streamable-http` | `http`、`streamable_http` | `url` | `headers` | 经 `httpx.AsyncClient` 连接 streamable HTTP server |
| `sse` | — | `url` | `headers` | Server-Sent Events |

其他值抛 `不支持的 MCP transport`。

**`env` 值路径占位符**：`stdio` server 的 `env` 各值在传给子进程前，由 `_interpolate_env()`（`mcp_mgr.py`）做占位符替换——`${workdir}` 展开为当前工作目录（`--workdir` 指定的目标仓库）的绝对路径，开头的 `~` 展开为家目录，其余原样保留。因子进程 env 原样透传、且进程不 `chdir` 到 workdir，需要随仓库变化的路径（如把索引写进被分析项目而非全局缓存）必须借此写成绝对路径。例：onboard 角色用 `"CBM_CACHE_DIR": "${workdir}/.agent/codebase-memory"` 把 `codebase-memory` 的索引落到被分析仓库的 `.agent/` 下。

### 工具注册与命名

`_register_tool()`（`mcp_mgr.py:299-335`）为每个上游工具注册一个 `ToolEntry`：

- **工具名** = `_safe_tool_name("mcp__<server>__<tool>")`：非 `[A-Za-z0-9_-]` 字符替换为 `_`，截断到 `_MAX_TOOL_NAME = 64`（对齐 Anthropic 工具名约束）。
- **参数模型** = `_PassThroughArgs`（`extra="allow"`，原样透传全部入参，不做字段级校验）；`parameters_schema` 用上游 `inputSchema`。
- **授权策略**：无条件 `REVIEW + EXTERNAL`，`origin=ToolOrigin("mcp", server)`；上游 annotation（含 `readOnlyHint`）不能提升权限。
- 结果经 `_format_result()`（`mcp_mgr.py:70-94`）拼接文本块；`isError` 为真时加 `错误：` 前缀以命中 ToolsMgr 的错误判定。

### server 级开关（`settings.json` 的 `mcp` 段）

`_apply_server_policy()`（`mcp_mgr.py:176-205`）在连接前硬过滤：

| 键 | 语义 |
|---|---|
| `mcp.enabledServers` | 非空时作**白名单**，只连其中的 server |
| `mcp.disabledServers` | 始终剔除（优先于白名单） |

这是连接前的硬开关：被禁用的 server 不连接、其工具不注册、不进 LLM schema。它只控制连接，不改变工具授权；所有已连接 MCP 工具仍逐次进入判官。

当前 `src/roles/mijia/mcp_servers.json` 示例：

```json
{
  "mcpServers": {
    "mijia": {
      "transport": "stdio",
      "command": "uv",
      "args": ["--directory", "/path/to/mijia-mcp", "run", "mijia-mcp", "serve"]
    }
  }
}
```

即：以 stdio 启动 `mijia-mcp` 子进程；发现的每个工具仍按 `REVIEW + EXTERNAL` 授权。

## Hooks

Hooks 在 8 个生命周期事件上执行 shell 命令，通过 JSON stdin/stdout 协议观察或干预流程。

### 8 种事件

`HOOK_EVENTS`（`hooks_mgr.py:17-26`）：

| 事件 | 触发时机 | `pre_tool` |
|---|---|---|
| `PreToolUse` | 工具执行前（可改入参、可拦截） | ✓ |
| `PostToolUse` | 工具执行后 | |
| `UserPromptSubmit` | 用户提交输入后 | |
| `Stop` | 主 agent 一轮结束 | |
| `SessionStart` | 会话开始 | |
| `SessionEnd` | 会话结束 | |
| `SubagentStart` | 子 agent 启动前（`subagent_mgr.task_delegator`） | |
| `SubagentStop` | 子 agent 结束后 | |

### 配置格式与两层加载

Hooks 写在 `settings.json` 的 `hooks` 段（也可来自插件的 `hooks/hooks.json`）。`_load_hooks()`（`hooks_mgr.py:88-117`）按顺序**全部追加、不覆盖**：全局 plugins → 全局 `settings.json` → 项目 plugins → 项目 `settings.json`。

配置结构（`_load_hook_file`，`hooks_mgr.py:119-167`）：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "write_file|edit_file_lines",
        "hooks": [
          { "type": "command", "command": "./check.sh", "timeout": 60, "async": false }
        ]
      }
    ]
  }
}
```

- `matcher`：匹配值（对工具类事件为工具名）。`_matches()`（`hooks_mgr.py:204-214`）：空或 `*` 匹配全部；纯 `[\w|]+` 按 `|` 分隔精确匹配（如 `a|b|c`）；否则按正则 `re.fullmatch`。
- `type` 必须为 `command`（其他忽略）；`command` 为 shell 命令串。
- `timeout`：秒，默认 60，下限 0.1。
- `async`：`true` 时 fire-and-forget（`_schedule_async`，不阻塞、不收集输出）。
- 未知事件名告警忽略。

`HooksMgr.reload()`（`hooks_mgr.py:82-84`）重新加载——**`settings.json` 的 hook 编辑随 `/clear` 生效**。

### 执行与 JSON 协议

`run_event()`（`hooks_mgr.py:171-199`）向匹配的每个 hook 的 stdin 传入 JSON payload：

```json
{ "hook_event_name": "...", "session_id": "...", "agent_id": "...",
  "agent_type": "...", "cwd": "...", "<extra>": "..." }
```

同步 hook 串行执行，其 `updatedInput` 会传递给后续 hook 的 `tool_input`。插件 hook 额外注入 `CLAUDE_PLUGIN_ROOT`/`AGENT_PLUGIN_ROOT` 环境变量。

**退出码语义**（`_run_hook`，`hooks_mgr.py:264-276`）：

| 退出码 | 含义 |
|---|---|
| `0` | 成功；stdout 非空则解析为结果（见下），空则无操作 |
| `2` | **阻止**：`blocked=True`，`block_reason` 取 stderr |
| 其他 | 非阻止错误：记日志并继续 |

超时或 `OSError` → `_error_result`：记为 error；PreToolUse 的执行错误会使工具调用拒绝或进入统一授权的保守路径。

**stdout JSON 解析**（`_parse_output`，`hooks_mgr.py:286-323`）——非 JSON 文本整体作为 `additionalContext`；JSON dict 可含（顶层或 `hookSpecificOutput` 内）：

| 字段 | 类型 | 效果（映射到 `HookRunResult`） |
|---|---|---|
| `additionalContext` | str / list | 追加上下文 |
| `permissionDecision` | `deny`/`ask`/`defer` | deny 可直接阻止；其余值不能绕过统一授权 |
| `permissionDecisionReason` / `reason` | str | 决策原因 |
| `updatedInput` | dict | 替换工具入参 |
| `decision: "block"` + `reason` | | `blocked=True`，`block_reason=reason` |

### `HookRunResult` 字段（`hooks_mgr.py:41-58`）

| 字段 | 类型 | 含义 |
|---|---|---|
| `additional_context` | list[str] | 注入给 LLM 的附加上下文 |
| `permission_decisions` | list[(str,str)] | Hook 决策列表；deny 可阻止，任何值都不能直接放行工具 |
| `updated_input` | dict / None | 被 hook 改写的工具入参 |
| `blocked` | bool | 是否拦截 |
| `block_reason` | str / None | 拦截原因 |
| `errors` | list[str] | 执行错误 |

`merge()` 把多个 hook 的结果合并（`updated_input` 后者覆盖；任一 `blocked` 即拦截）。子 agent 场景下 `SubagentStop` 的 `blocked`/`additional_context` 会覆盖或追加到子 agent 返回结果（见 [roles-subagents-skills.md](roles-subagents-skills.md)）。
