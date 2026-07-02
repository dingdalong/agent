# 配置总参考（Configuration Reference）

本文档面向**运维者与开发者**，是本框架所有配置项的权威参考。全部默认值、键名以源码为准：运行配置见 `src/config.yaml`，合并逻辑见 `src/mgr/config_mgr.py`，路径解析见 `src/mgr/paths.py`，各键的消费点在本文逐一标注。

术语约定：

- **三层配置合并** — 配置来源分为内置（`src/`）→ 全局（`~/.agent/`）→ 项目（`{workdir}/.agent/`）三层，后层覆盖前层。详见「配置体系总览」。
- **feature 门控** — 角色在 `role.md` frontmatter 声明启用的 feature 集，据此决定 `MemoryMgr`/`PlanMgr` 等可插拔 Manager 是否实例化（未启用注入 `None`）。详见 [managers.md](managers.md) 与 [roles-subagents-skills.md](roles-subagents-skills.md)。

相关文档：目录路径与运行时结构见 [architecture.md](architecture.md)；权限规则格式见 [permissions.md](permissions.md)；MCP 与 hooks 详解见 [mcp-and-hooks.md](mcp-and-hooks.md)；LLM 模型与别名见 [llm.md](llm.md)；事件级别见 [events-and-ui.md](events-and-ui.md)；各 Manager 见 [managers.md](managers.md)。

---

## 1. 配置体系总览

框架有三类配置文件，加上 `.env` 环境变量，各司其职：

| 文件 | 职责 | 加载入口 |
|------|------|----------|
| `config.yaml` | **运行配置** — LLM provider、模型别名、压缩、激活角色、事件级别 | `ConfigManager.load_config()` |
| `settings.json` | **权限与 hooks** — 权限规则、默认模式、MCP server 开关、生命周期 hooks | `ConfigManager.load_user_settings()`（权限）+ `HooksMgr._load_hooks()`（hooks） |
| `mcp_servers.json` | **MCP 连接** — 各 MCP server 的传输方式与连接参数、server 级只读权限块 | `ConfigManager.load_mcp_servers()` + `McpMgr.start()`（角色层） |
| `.env` | **环境变量** — API key / API URL 等敏感值，覆盖 provider 字段 | `ConfigManager.load_config()` 内 `load_dotenv()` |

### 三层来源

每类配置从低到高优先级叠加三层（`src/mgr/config_mgr.py`、`src/mgr/paths.py`）：

1. **内置** — `src/`（`builtin_root()`，即安装后的包目录）。仅 `config.yaml` 有内置层。
2. **全局** — `~/.agent/`（`global_data_dir()`）。可用环境变量 `$AGENT_HOME` 改写此目录（`paths.py:42`）。
3. **项目** — `{workdir}/.agent/`（`project_data_dir()`）。`workdir` 由 `--workdir` / `$AGENT_WORKDIR` / `cwd` 决定（`paths.py:59`）。

### 各文件三层路径与合并规则一览

| 文件 | 内置层 | 全局层 | 项目层 | 合并规则 | 写入目标 | `/clear` 重载？ |
|------|--------|--------|--------|----------|----------|-----------------|
| `config.yaml` | `src/config.yaml` | `~/.agent/config.yaml` | `{workdir}/.agent/config.yaml` | 深合并，后层覆盖叶子值 | 只读（框架不写） | 是（`ConfigManager.reload()`） |
| `settings.json` | 无 | `~/.agent/settings.json` | `{workdir}/.agent/settings.json` | `permissions.allow`/`permissions.deny` 列表并集去重，其余键深合并项目覆盖全局 | **仅项目层**（`append_permission_list` 原子写） | 权限规则随 `/clear` 重载（`PermissionManager.reload()`） |
| `mcp_servers.json` | 角色层 `src/roles/<role>/mcp_servers.json` | `~/.agent/mcp_servers.json` | `{workdir}/.agent/mcp_servers.json` | 按 server 名深合并，项目覆盖全局；角色层由 `McpMgr` 额外并入（最低优先级） | 只读（框架永不写回） | **否，需重启**（`McpMgr` 无 `reload()`，`/clear` 不重连 server） |
| `.env` | 无 | `~/.agent/.env` | `{workdir}/.env` 与 `{workdir}/.agent/.env` | `load_dotenv(override=True)` 逐个加载，后覆盖前 | 只读 | 是（`load_config()` 重跑） |

> `~/.agent/settings.json` 全局层只读；框架写权限规则（`/mode` 的「always」持久化、`resolve_ask` 的「信任整个 server」）只落项目层 `{workdir}/.agent/settings.json`（`config_mgr.py:99`、`config_mgr.py:277` `append_permission_list`）。

---

## 2. 合并规则细节

全部逻辑在 `src/mgr/config_mgr.py`。

### 2.1 `config.yaml` — 深合并

`_deep_merge(base, override)`（`config_mgr.py:21`）递归合并：两边同名键都是 dict 时递归下沉，否则 override 的值直接覆盖 base。加载顺序（`load_config()`，`config_mgr.py:111`）：

```
内置 src/config.yaml → 全局 ~/.agent/config.yaml → 项目 {workdir}/.agent/config.yaml
```

因此项目层只需写想覆盖的叶子键，其余继承低层。

### 2.2 `settings.json` — 权限列表并集，其余深合并

`load_user_settings()`（`config_mgr.py:162`）：先 `_deep_merge` 全局与项目，再对 `permissions.allow` 与 `permissions.deny` 两个列表做**并集去重**（全局条目在前、项目条目在后，保持顺序，`config_mgr.py:186`）。`permissions.ask`、`permissions.defaultMode`、`mcp`、`hooks` 等其余键走深合并，项目覆盖全局。

> 注意：只有 `allow`/`deny` 做列表并集；`ask` 列表走普通深合并（项目层若提供 `ask` 会整体覆盖全局层同键，而非并集）。

### 2.3 `mcp_servers.json` — 按 server 名深合并 + 角色层并入

- `load_mcp_servers()`（`config_mgr.py:145`）只读顶层 `mcpServers` 字段，`_deep_merge(全局, 项目)`，同名 server 由项目层覆盖全局层。
- `McpMgr.start()`（`mcp_mgr.py:122`）先取**角色层** `src/roles/<role>/mcp_servers.json`（`role_mgr.mcp_servers_path()`），再 `servers.update(config_mgr.load_mcp_servers())`。因此优先级为 **角色 < 全局 < 项目**，角色层是最低优先级、被全局/项目同名 key 覆盖。

### 2.4 `.env` 加载顺序

`load_config()`（`config_mgr.py:117`）按以下顺序调用 `load_dotenv(path, override=True)`，后加载者覆盖同名变量：

```
~/.agent/.env  →  {workdir}/.env  →  {workdir}/.agent/.env
```

仓库根 `{workdir}/.env` 是最常见的放置位置，特意纳入以免配置读不到。

### 2.5 环境变量覆盖 provider 字段

`.env` 加载后，`load_config()`（`config_mgr.py:135`）对 `llm_provider` 下每个 provider 名 `<name>` 检查两个环境变量并覆盖对应字段：

| 环境变量 | 覆盖字段 | 说明 |
|----------|----------|------|
| `{NAME_UPPER}_API_KEY` | `llm_provider.<name>.api_key` | 如 `DEEPSEEK_API_KEY` → `llm_provider.deepseek.api_key` |
| `{NAME_UPPER}_API_URL` | `llm_provider.<name>.base_url` | 如 `OPENAI_API_URL` → `llm_provider.openai.base_url` |

`<name>` 取自 `config.yaml` 中 `llm_provider` 的键，转大写后拼接后缀。这样 API key 无需写进 `config.yaml`，只放 `.env` 即可。

---

## 3. `config.yaml` 逐键参考

以 `src/config.yaml` 为权威默认值来源。下列各表标注了消费该键的源码位置。

### 3.1 `llm_provider.<name>` — LLM provider 连接与推理配置

`LLMMgr` 在 `load_models()`（`llm_mgr.py:51`）和 `_create_provider()`（`llm_mgr.py:161`）消费。`<name>` 必须是框架已知的 provider（由 `get_provider(name)` 解析，见 [llm.md](llm.md)）。

| 键 | 类型 | 默认值 | 可选值 | 效果 |
|----|------|--------|--------|------|
| `llm_provider.<name>.base_url` | str | 见下方各 provider | 任意 URL | provider API 端点；`_create_provider` 直接读取（必填，缺失会 KeyError）。可被 `{NAME}_API_URL` 覆盖 |
| `llm_provider.<name>.api_key` | str | 无（通常来自 `.env`） | — | API key；`_create_provider` 读取，默认 `""`。通常经 `{NAME}_API_KEY` 注入 |
| `llm_provider.<name>.reasoning_effort` | str | 见下方 | provider 相关（如 `low`/`high`/`max`/`xhigh`） | 推理力度，传给 provider；缺省回退 `"max"`（`llm_mgr.py:168`） |
| `llm_provider.<name>.context_limit` | int | 见下方 | 正整数 | 上下文窗口 token 上限；缺省 `0`（`llm_mgr.py:172`）。**压缩阈值由此换算**（见 3.4） |
| `llm_provider.<name>.preserve_thinking` | bool | `ollama` 为 `true`，其余无 | `true`/`false` | 是否在历史中保留 reasoning 内容；缺省 `false`（`llm_mgr.py:169`）。Qwen 类 agent 场景需保留 |
| `llm_provider.<name>.models` | list[str] | `openai` 为 `[gpt-5.5]`，其余无 | 模型名列表 | provider API 拉取模型列表失败时的回退清单（`llm_mgr.py:62`） |

`src/config.yaml` 现有的四个 provider 默认值：

| provider | base_url | reasoning_effort | context_limit | 其他 |
|----------|----------|------------------|---------------|------|
| `deepseek` | `https://api.deepseek.com` | `max` | `400000` | — |
| `openai` | `https://api.openai.com/v1` | `xhigh` | `200000` | `models: [gpt-5.5]` |
| `anthropic` | `https://api.anthropic.com` | `high` | `200000` | — |
| `ollama` | `http://127.0.0.1:8001/v1` | `high` | `100000` | `preserve_thinking: true` |

### 3.2 `llm` — 模型别名与调用参数

`LLMMgr.__post_init__`（`llm_mgr.py:45`）、`resolve_model`（`llm_mgr.py:80`）、`ensure_default_available`（`llm_mgr.py:118`）消费。

| 键 | 类型 | 默认值 | 可选值 | 效果 |
|----|------|--------|--------|------|
| `llm.default` | str | `deepseek-v4-flash`（本仓库） | 任一可用模型名 | **必填**。默认模型别名 `default` 解析目标；子 agent `model: inherit` 或省略时用它。启动前经 `ensure_default_available()` 校验，不可用则报 `ModelUnavailableError` 退出 |
| `llm.best` | str | 不填时回退 `default` | 任一可用模型名 | 别名 `best` 的解析目标（Claude Code 别名 `opus` 映射到此，`llm_mgr.py:17`） |
| `llm.fast` | str | 不填时回退 `default` | 任一可用模型名 | 别名 `fast` 的解析目标（Claude Code 别名 `haiku` 映射到此） |
| `llm.concurrency` | int | `5` | 正整数 | provider 并发上限，传给每个 provider 实例（`llm_mgr.py:47`、`:170`） |
| `llm.max_retries` | int | `3` | 非负整数 | provider 调用重试次数（`llm_mgr.py:48`、`:171`） |

**别名体系**：`default`/`best`/`fast` 是框架三个通用别名，子 agent 在其 `*.md` frontmatter 的 `model:` 字段通过这些别名引用（`model: inherit` 表示继承父 agent 已解析的真实模型 ID）。`resolve_model`（`llm_mgr.py:80`）解析顺序：`None → "default" → Claude Code 兼容映射（opus/sonnet/haiku → best/default/fast）→ 配置别名 → 精确匹配 → 模糊匹配 → 回退默认`。别名细节与模型加载见 [llm.md](llm.md)。

### 3.3 `tool` — 工具结果分页

| 键 | 类型 | 默认值 | 可选值 | 效果 |
|----|------|--------|--------|------|
| `tool.page_token_rate` | float | `0.03` | `0`~`1` | 单个工具调用结果每页最多占上下文窗口的比例；`LLMMgr.__post_init__` 读取（`llm_mgr.py:49`）后传给每个 provider（`llm_mgr.py:173`）用于分页。分页机制见 [llm.md](llm.md) |

### 3.4 `compact` — 上下文压缩

`Agent.__post_init__`（`agent.py:126`）读取整个 `compact` 段，按 provider 的 `context_limit` 换算为**绝对 token 数**后构造 `CompactMgr`：

| 键 | 类型 | 默认值 | 可选值 | 效果 |
|----|------|--------|--------|------|
| `compact.auto_compact_rate` | float | `0.8` | `0`~`1` | 达到 `context_limit × auto_compact_rate` token 时触发自动压缩。换算为 `auto_compact_size = int(context_limit * auto_compact_rate)`（`agent.py:131`） |
| `compact.keep_recent_user_turns` | int | `3` | 正整数 | 压缩时保留最近 N 次用户输入及其后完整原始对话；直接传给 `CompactMgr`（`agent.py:132`） |
| `compact.keep_recent_messages_token_rate` | float | `0.25` | `0`~`1` | 最近原始对话最多保留上下文窗口的比例；换算为 `recent_messages_token_limit = int(context_limit * keep_recent_messages_token_rate)`（`agent.py:133`）。低于最小轮次时仍保留最小轮次 |

> **换算说明**：`config.yaml` 里存的是**比例**，`Agent` 在构造 `CompactMgr` 时用当前 agent 所用模型的 `context_limit`（`self.llm.context_limit`，`agent.py:130`）乘以比例得到绝对 token 数。不同 agent 若用不同 `context_limit` 的模型，绝对阈值也不同。`CompactMgr` 细节见 [managers.md](managers.md)。

### 3.5 `role` — 激活角色

| 键 | 类型 | 默认值 | 可选值 | 效果 |
|----|------|--------|--------|------|
| `role` | str | 缺省回退 `coding`（本仓库设为 `mijia`） | 已发现的角色名 | 指定激活角色；`RoleMgr._resolve()` 读取（`role_mgr.py:228`）。未指定或角色不存在时回退 `coding`（`role_mgr.py:28`、`:234`）。角色决定主 agent 身份提示词、可用子 agent、技能、MCP server 与 feature 集 |

角色发现与结构见 [roles-subagents-skills.md](roles-subagents-skills.md) 与 [architecture.md](architecture.md)。

### 3.6 `events` — 事件级别

| 键 | 类型 | 默认值 | 可选值 | 效果 |
|----|------|--------|--------|------|
| `events.level` | str | `detail`（本仓库；`bootstrap` 内 `.get` 回退为 `progress`） | `progress` \| `detail` \| `trace` | 事件总线的输出详细度；`bootstrap.create_app()` 读取（`bootstrap.py:33`）经 `EventLevel.from_str` 构造 `EventBus`。`detail` 及以上会展示模型思考过程等更细粒度事件 |

> `src/config.yaml` 内置值为 `detail`；`bootstrap` 读取时 `config_mgr.get_config("events").get("level", "progress")` 的 `"progress"` 只在整个 `events.level` 键缺失时才生效。事件级别语义见 [events-and-ui.md](events-and-ui.md)。

### 3.7 完整注释版 `config.yaml` 示例

```yaml
# ── LLM provider 连接与推理配置 ──────────────────────────
# <name> 须为框架已知 provider；api_key 通常经 .env 的 {NAME}_API_KEY 注入。
llm_provider:
  deepseek:
    base_url: https://api.deepseek.com   # API 端点，可被 DEEPSEEK_API_URL 覆盖
    reasoning_effort: max                # 推理力度
    context_limit: 400000                # 上下文窗口 token 上限（压缩阈值据此换算）
    # api_key: 通常放 .env：DEEPSEEK_API_KEY=sk-...
  openai:
    models:                              # API 拉取失败时的回退模型清单
      - gpt-5.5
    base_url: https://api.openai.com/v1
    reasoning_effort: xhigh
    context_limit: 200000
  anthropic:
    base_url: https://api.anthropic.com
    reasoning_effort: high
    context_limit: 200000
  ollama:
    base_url: http://127.0.0.1:8001/v1
    reasoning_effort: high
    preserve_thinking: true              # 保留历史 reasoning（Qwen 类 agent 场景）
    context_limit: 100000

# ── 模型别名与调用参数 ──────────────────────────────────
llm:
  default: deepseek-v4-flash             # 必填：默认模型，子 agent 通过别名 default 引用
  # best: ...                            # 可选：最强模型（别名 best / Claude Code opus）
  # fast: ...                            # 可选：最快/最省模型（别名 fast / Claude Code haiku）
  concurrency: 5                         # provider 并发上限
  max_retries: 3                         # provider 调用重试次数

# ── 工具结果分页 ────────────────────────────────────────
tool:
  page_token_rate: 0.03                  # 单页工具结果最多占上下文窗口比例

# ── 上下文压缩（比例，运行时按 context_limit 换算为绝对 token）──
compact:
  auto_compact_rate: 0.8                 # 用量达 context_limit×0.8 时自动压缩
  keep_recent_user_turns: 3              # 保留最近 N 次用户输入及其后原始对话
  keep_recent_messages_token_rate: 0.25  # 最近原始对话最多占上下文窗口比例

# ── 激活角色 ────────────────────────────────────────────
role: mijia                              # 缺省回退 coding

# ── 事件级别 ────────────────────────────────────────────
events:
  level: detail                          # progress | detail | trace
```

---

## 4. `settings.json` 完整 schema

承载**权限与 hooks 策略**。权限段由 `PermissionManager._load_config()` 消费（`permission_mgr.py:312`，经 `config_mgr.get_user_setting("permissions")`）；`mcp` 段由 `McpMgr._apply_server_policy()` 消费（`mcp_mgr.py:190`）；`hooks` 段由 `HooksMgr._load_hooks()` 消费（`hooks_mgr.py:88`）。

| 键 | 类型 | 默认值 | 效果 | 消费点 |
|----|------|--------|------|--------|
| `permissions.allow` | list[str] | `[]` | allow 规则列表；全局+项目并集去重 | `permission_mgr.py:328` |
| `permissions.deny` | list[str] | `[]` | deny 规则列表（优先级最高）；全局+项目并集去重 | `permission_mgr.py:326` |
| `permissions.ask` | list[str] | `[]` | ask 规则列表（触发确认弹窗）；深合并（非并集） | `permission_mgr.py:327` |
| `permissions.defaultMode` | str | 缺省 `default`（`DEFAULT_MODE`） | 入口主 agent 的默认权限模式 | `permission_mgr.py:318` |
| `mcp.enabledServers` | list[str] | 无（视为不启用白名单） | 非空时作**白名单**，只连接其中的 server | `mcp_mgr.py:193` |
| `mcp.disabledServers` | list[str] | 无 | 始终从待连接集合剔除（连接前硬开关） | `mcp_mgr.py:194` |
| `hooks.<Event>` | list[group] | 无 | 生命周期 hook 定义（见下） | `hooks_mgr.py:135` |

**规则文本格式**：`工具名` 或 `工具名(specifier)`，`specifier` 走 fnmatch，工具名段也支持 `*`/`?` 通配（如 `mcp__github__*`、`mcp__github__get_*`）。`deny` 优先于 `allow`。完整规则语法、6 步评估顺序、复合命令逐段匹配详见 [permissions.md](permissions.md)。

**`defaultMode` 合法值**（`permission_mgr.py:94`~`117`，`parse_permission_mode` 接受编号或名字）：`default`、`acceptEdits`、`plan`、`bypassPermissions`、`auto`、`dontAsk`。语义见 [permissions.md](permissions.md)。

**`hooks` 事件**（`hooks_mgr.py:17` `HOOK_EVENTS`，共 8 种）：`PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`Stop`、`SessionStart`、`SessionEnd`、`SubagentStart`、`SubagentStop`。每个事件下是 group 列表，group 含可选 `matcher` 与 `hooks` 命令数组，命令支持 JSON stdin/stdout 协议、`timeout`（默认 60s）、`async`（默认 false）。格式详解见 [mcp-and-hooks.md](mcp-and-hooks.md)。

> **写入目标**：框架只写项目层 `{workdir}/.agent/settings.json`（`append_permission_list` 原子写，`config_mgr.py:277`）。全局 `~/.agent/settings.json` 只读，需手工编辑。权限编辑随 `/clear` 重载（`PermissionManager.reload()`，`permission_mgr.py:414`）；hooks 亦随 `/clear` 重载（`HooksMgr.reload()`，`hooks_mgr.py:82`）。

### 示例 `settings.json`

```json
{
  "permissions": {
    "defaultMode": "default",
    "allow": [
      "shell(git status)",
      "shell(npm *)",
      "mcp__mijia__get_*"
    ],
    "deny": [
      "shell(rm -rf *)"
    ],
    "ask": [
      "shell(git push:*)"
    ]
  },
  "mcp": {
    "disabledServers": ["some-noisy-server"]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "shell",
        "hooks": [
          { "type": "command", "command": "./scripts/audit.sh", "timeout": 10 }
        ]
      }
    ]
  }
}
```

---

## 5. `mcp_servers.json` 完整 schema

配置各 MCP server 的连接方式与就近权限。顶层唯一字段 `mcpServers`，值为 `{server名: spec}`。`spec` 由 `McpMgr._open_session()` 消费（`mcp_mgr.py:256`），`permissions` 块由 `McpMgr.server_permissions()`（`mcp_mgr.py:207`）暴露给 `PermissionManager`。

| 字段 | 类型 | 适用 transport | 效果 |
|------|------|----------------|------|
| `transport` | str | 全部 | `stdio`（默认）/ `http` / `streamable-http` / `streamable_http` / `sse`（`mcp_mgr.py:268`） |
| `command` | str | `stdio` | 启动命令（必填） |
| `args` | list[str] | `stdio` | 命令参数（默认 `[]`） |
| `env` | dict | `stdio` | 追加环境变量，叠加在默认环境（含 PATH）之上（`mcp_mgr.py:275`） |
| `url` | str | `http`/`sse` | server 端点 URL（必填） |
| `headers` | dict | `http`/`sse` | HTTP 请求头 |
| `permissions` | dict | 全部 | **只读**的 server 级权限块 `{allow,deny,ask: [...]}`；条目是相对该 server 的上游工具名通配（如 `get_*`、`*`），展开为 `mcp__<server>__<entry>` 规则，作为最低优先级层生效 |

`permissions` 块是最低优先级权限层，仅在 `settings.json` 对该次调用无任何规则命中时才落到它；`settings.json` 的 `allow` 能覆盖此处的 `deny`。**编辑该块需重启生效**（`McpMgr` 无 `reload()`）。规则展开与分层评估详见 [mcp-and-hooks.md](mcp-and-hooks.md) 与 [permissions.md](permissions.md)。

### 示例（本仓库 `mijia` 角色）

`src/roles/mijia/mcp_servers.json`：

```json
{
  "mcpServers": {
    "mijia": {
      "transport": "stdio",
      "command": "uv",
      "args": ["--directory", "/Users/dingdalong/github/mijia-mcp", "run", "mijia-mcp", "serve"],
      "permissions": {
        "allow": ["*"]
      }
    }
  }
}
```

`http`/`sse` 传输示例：

```json
{
  "mcpServers": {
    "github": {
      "transport": "streamable-http",
      "url": "https://api.example.com/mcp",
      "headers": { "Authorization": "Bearer ${TOKEN}" },
      "permissions": {
        "allow": ["get_*", "list_*"],
        "deny": ["delete_*"]
      }
    }
  }
}
```

---

## 6. `.env` / 环境变量

`.env` 文件按 `~/.agent/.env → {workdir}/.env → {workdir}/.agent/.env` 顺序加载，后覆盖前（`config_mgr.py:117`）。除任意进程环境变量外，框架专门识别以下几个：

| 变量 | 作用 | 消费点 |
|------|------|--------|
| `{NAME_UPPER}_API_KEY` | 覆盖 `llm_provider.<name>.api_key`（如 `DEEPSEEK_API_KEY`） | `config_mgr.py:137` |
| `{NAME_UPPER}_API_URL` | 覆盖 `llm_provider.<name>.base_url`（如 `OPENAI_API_URL`） | `config_mgr.py:137` |
| `$AGENT_HOME` | 改写全局配置目录（默认 `~/.agent/`） | `paths.py:42` `global_data_dir()` |
| `$AGENT_WORKDIR` | 指定工作目录（优先级：`--workdir` > `$AGENT_WORKDIR` > `cwd`） | `paths.py:72` `workdir()` |

`$AGENT_HOME` / `$AGENT_WORKDIR` 与目录路径的关系见 [architecture.md](architecture.md)。

示例 `.env`（放仓库根 `{workdir}/.env`）：

```env
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
OPENAI_API_KEY=sk-yyyyyyyyyyyyyyyy
# OPENAI_API_URL=https://proxy.example.com/v1   # 可选：覆盖 base_url
```

---

## 7. 目录布局速查表

以下为各类落盘产物的**实际写入路径**（以对应 Manager 源码为准）。

### 全局 `~/.agent/`（可经 `$AGENT_HOME` 改写）

| 路径 | 内容 | 来源 |
|------|------|------|
| `~/.agent/config.yaml` | 全局运行配置 | 手工 |
| `~/.agent/settings.json` | 全局权限与 hooks（**只读**，框架不写此层） | 手工 |
| `~/.agent/mcp_servers.json` | 全局 MCP 连接 | 手工 |
| `~/.agent/.env` | 全局环境变量 | 手工 |
| `~/.agent/roles/` | 全局角色 | 手工 |
| `~/.agent/agents/` | 全局子 agent 定义 | 手工 |
| `~/.agent/skills/` | 全局技能 | 手工 |
| `~/.agent/plugins/` | 全局插件 | 手工 |
| `~/.agent/sessions/` | 会话历史与元数据 | `SessionMgr`（`session_mgr.py:50`，`global_dir / "sessions"`） |
| `~/.agent/tasks/{session_id}/` | 主 agent 任务持久化（每 task 一个 JSON + highwatermark） | `TaskManager`，仅主 agent（`agent.py:151`，`global_dir / "tasks" / session_id`） |

### 项目 `{workdir}/.agent/`

| 路径 | 内容 | 来源 |
|------|------|------|
| `{workdir}/.agent/config.yaml` | 项目运行配置 | 手工 |
| `{workdir}/.agent/settings.json` | 项目权限与 hooks（**框架写入目标**） | 手工 + `append_permission_list` |
| `{workdir}/.agent/mcp_servers.json` | 项目 MCP 连接 | 手工 |
| `{workdir}/.agent/.env` | 项目环境变量（最高优先级） | 手工 |
| `{workdir}/.agent/roles/`、`agents/`、`skills/`、`plugins/` | 项目级角色/子 agent/技能/插件 | 手工 |
| `{workdir}/.agent/memory/` | 记忆文件（`*.md`） | `MemoryMgr`（`memory_mgr.py:35`，`workdir / ".agent" / "memory"`） |
| `{workdir}/.agent/plans/` | plan 文件 | `PlanMgr`（`plan_mgr.py:70`，`workdir / ".agent" / "plans"`） |

### 运行产物

| 路径 | 内容 | 来源 |
|------|------|------|
| `{workdir}/.transcripts/transcript_<ts>.jsonl` | 压缩前完整对话备份 | `CompactMgr.write_transcript`（`compact_mgr.py:58`，`workdir / ".transcripts"`） |

> 落盘目录要点：会话（`sessions/`）与主 agent 任务（`tasks/`）在**全局** `~/.agent/` 下；记忆（`memory/`）、plan（`plans/`）在**项目** `{workdir}/.agent/` 下；transcript 在 `{workdir}/.transcripts/`。子 agent 的 `TaskManager` 为纯内存模式（`tasks_dir=None`），不落盘（`agent.py:150`、`task_mgr.py:82`）。目录路径体系见 [architecture.md](architecture.md)。

---

## 8. CLI 参数

`main.py`（`parse_args`）：

| 参数 | 类型 | 默认 | 效果 |
|------|------|------|------|
| `--workdir <path>` | str | `None`（用 `cwd`） | 指定工作目录，传给 `create_app(workdir_override=...)`（`main.py:38`）。优先级高于 `$AGENT_WORKDIR` |
| `--debug` | flag | 关闭 | 启用 asyncio 调试模式：事件循环被任一回调占用超过 0.1s 即打印慢回调告警，用于排查在 `async` 中误跑同步阻塞工作的代码（`main.py:50`，`asyncio.run(..., debug=True)`） |

运行时结构与阻塞契约见 [architecture.md](architecture.md) 与 [agent-runtime.md](agent-runtime.md)。

---

## 附：`.claude/settings.local.json` 说明

仓库中的 `.claude/settings.local.json` 是 **Claude Code（claude.ai/code）编辑器自身的本地权限文件**，**不属于本框架**。本框架读取的是 `.agent/settings.json`（注意目录是 `.agent/` 而非 `.claude/`）。`.claude/settings.local.json` 里的 `permissions.allow` 条目（如 `Bash(...)`、`WebFetch(...)`）遵循 Claude Code 的规则语法，与本框架的 `PermissionRule`（`工具名(specifier)`）无关，也不会被 `ConfigManager` 或 `PermissionManager` 加载。

---

## 9. 「我想要 X 效果，改哪个键」速查表

| 想要的效果 | 改哪里 | 怎么改 |
|------------|--------|--------|
| 换主 agent 默认模型 | `config.yaml` `llm.default` | 设为目标模型名 |
| 指定「最强 / 最快」别名指向 | `config.yaml` `llm.best` / `llm.fast` | 设为对应模型名（子 agent 通过 `best`/`fast` 引用） |
| 更早触发上下文压缩 | `config.yaml` `compact.auto_compact_rate` | 调小（如 `0.6`） |
| 压缩后多保留近期对话 | `config.yaml` `compact.keep_recent_user_turns` / `keep_recent_messages_token_rate` | 调大 |
| 加大 / 减小模型上下文窗口 | `config.yaml` `llm_provider.<name>.context_limit` | 设为目标 token 数（同时影响压缩绝对阈值） |
| 提高 provider 并发 | `config.yaml` `llm.concurrency` | 调大 |
| 换激活角色 | `config.yaml` `role` | 设为目标角色名 |
| 看模型思考过程 | `config.yaml` `events.level` | 设为 `detail`（或 `trace`） |
| 减少权限弹窗 | `settings.json` `permissions.defaultMode` 或 `permissions.allow` | 设更宽松模式（如 `acceptEdits`/`auto`）或加 allow 规则 |
| 禁止某类命令 | `settings.json` `permissions.deny` | 加 deny 规则（优先级最高） |
| 某操作每次都确认 | `settings.json` `permissions.ask` | 加 ask 规则 |
| 禁用某个 MCP server | `settings.json` `mcp.disabledServers` | 加入该 server 名（连接前剔除） |
| 只启用部分 MCP server | `settings.json` `mcp.enabledServers` | 列出白名单（非空即生效） |
| 收窄某 MCP server 的工具权限 | `mcp_servers.json` server 的 `permissions` 块 | 写 `allow`/`deny`/`ask` 通配（**改后需重启**） |
| 在工具调用 / 会话事件时跑脚本 | `settings.json` `hooks.<Event>` | 加 hook 命令（见 [mcp-and-hooks.md](mcp-and-hooks.md)） |
| 换 API key / 端点 | `.env` `{NAME}_API_KEY` / `{NAME}_API_URL` | 设对应 provider 的变量 |
| 换全局配置目录 | 环境变量 `$AGENT_HOME` | 指向新目录 |
| 换工作目录 | `--workdir` 或 `$AGENT_WORKDIR` | 指定路径 |
| 排查 UI 卡顿 / 阻塞事件循环 | CLI `--debug` | 加 `--debug` 启动看慢回调告警 |
