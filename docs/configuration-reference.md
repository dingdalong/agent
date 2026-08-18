# 配置总参考（Configuration Reference）

本文档面向**运维者与开发者**，是本框架所有配置项的权威参考。全部默认值、键名以源码为准：运行配置见 `src/config.yaml`，合并逻辑见 `src/mgr/config_mgr.py`，路径解析见 `src/mgr/paths.py`，各键的消费点在本文逐一标注。

术语约定：

- **三层配置合并** — 配置来源分为内置（`src/`）→ 全局（`~/.agent/`）→ 项目（`{workdir}/.agent/`）三层，后层覆盖前层。详见「配置体系总览」。
- **feature 门控** — 角色在 `role.md` frontmatter 声明启用的 feature 集，据此决定 `MemoryMgr`/`PlanMgr` 等可插拔 Manager 是否实例化（未启用注入 `None`）。详见 [managers.md](managers.md) 与 [roles-subagents-skills.md](roles-subagents-skills.md)。

相关文档：目录路径与运行时结构见 [architecture.md](architecture.md)；统一授权边界见 [permissions.md](permissions.md)；MCP 与 Hooks 详解见 [mcp-and-hooks.md](mcp-and-hooks.md)。

---

## 1. 配置体系总览

框架有三类配置文件，加上 `.env` 环境变量，各司其职：

| 文件 | 职责 | 加载入口 |
|------|------|----------|
| `config.yaml` | **运行配置** — LLM provider、角色模型槽位、压缩、激活角色、事件级别 | `ConfigManager.load_config()` |
| `settings.json` | **运行时开关与 Hooks** — MCP server 开关、生命周期 Hook | `ConfigManager.load_user_settings()` + `HooksMgr._load_hooks()` |
| `mcp_servers.json` | **MCP 连接** — 各 MCP server 的传输方式与连接参数 | `ConfigManager.load_mcp_servers()` + `McpMgr.start()`（角色层） |
| `.env` | **环境变量** — API key / API URL 等敏感值，覆盖 provider 字段 | `ConfigManager.load_config()` 内 `dotenv_values()` |

### 三层来源

每类配置从低到高优先级叠加三层（`src/mgr/config_mgr.py`、`src/mgr/paths.py`）：

1. **内置** — `src/`（`builtin_root()`，即安装后的包目录）。仅 `config.yaml` 有内置层。
2. **全局** — `~/.agent/`（`global_data_dir()`）。可用环境变量 `$AGENT_HOME` 改写此目录（`paths.py:42`）。
3. **项目** — `{workdir}/.agent/`（`project_data_dir()`）。`workdir` 由 `--workdir` / `$AGENT_WORKDIR` / `cwd` 决定（`paths.py:59`）。

### 各文件三层路径与合并规则一览

| 文件 | 内置层 | 全局层 | 项目层 | 合并规则 | 写入目标 | `/clear` 重载？ |
|------|--------|--------|--------|----------|----------|-----------------|
| `config.yaml` | `src/config.yaml` | `~/.agent/config.yaml` | `{workdir}/.agent/config.yaml` | 深合并，后层覆盖叶子值 | `ConfigManager.set_config()` 可回写全局或项目层；内置层只读 | 是（`ConfigManager.reload()`） |
| `settings.json` | 无 | `~/.agent/settings.json` | `{workdir}/.agent/settings.json` | 深合并，项目覆盖全局 | 只读（框架不自动写） | 是 |
| `mcp_servers.json` | 角色层 `src/roles/<role>/mcp_servers.json` | `~/.agent/mcp_servers.json` | `{workdir}/.agent/mcp_servers.json` | 按 server 名深合并，项目覆盖全局；角色层最低优先级 | 只读（框架不写回） | 是，`/clear` 重新检查信任后重连 |
| `.env` | 无 | `~/.agent/.env` | `{workdir}/.env` 与 `{workdir}/.agent/.env` | `dotenv_values()` 读入私有有效环境，后覆盖前，不修改 `os.environ` | 全局：手工或首次 Provider 向导 / `ConfigManager.set_global_env`；项目层：手工 | 是（`load_config()` 重跑） |

项目 `.env`、Provider/LLM 配置、项目 Hook 和项目 MCP 只有通过 `ProjectTrustGate` 后才加载；项目信任确认被拒绝、取消、失败或运行于非 TTY 时进入受限模式。此时项目层 `llm_provider`、`llm` 会整体剥离；`role.default` 仍可选择已发现的非项目角色，但每个 `role.<角色>.model` 与 `role.<角色>.reasoning_effort` 会剥离，模型与 effort 只能来自内置或全局层。`/models` 因固定写项目层而直接拒绝执行，不写配置也不热切模型。

首次启动无显式 Provider 配置时（判定与流程见 [architecture.md](architecture.md)「首次 LLM Provider 配置向导」），向导自动把 `{PROVIDER}_API_URL` / `{PROVIDER}_API_KEY` 写入全局 `.env`，并把 `role.<有效角色>.model` 的 `default`/`fast` mapping 写入全局 `config.yaml`；配置角色不存在时与 `RoleMgr` 一样回退 `coding`。项目层 `.env` 始终手工维护。写入均为单文件原子更新，`.env` 只改目标变量并保留其他原文。

---

## 2. 合并规则细节

全部逻辑在 `src/mgr/config_mgr.py`。

### 2.1 `config.yaml` — 深合并

`_deep_merge(base, override)`（`config_mgr.py:21`）递归合并：两边同名键都是 dict 时递归下沉，否则 override 的值直接覆盖 base。加载顺序（`load_config()`，`config_mgr.py:111`）：

```
内置 src/config.yaml → 全局 ~/.agent/config.yaml → 项目 {workdir}/.agent/config.yaml
```

因此项目层只需写想覆盖的叶子键，其余继承低层。`ConfigManager.set_config(key, value, scope)` 只更新指定的 `global` 或 `project` 源文件中的点路径，不会把合并结果写回；写后需调用 `reload()` 或重启才会生效。回写使用规范化 YAML 输出，不保留原有注释和空白格式；源文件 YAML 无效或顶层不是对象时会拒绝写入。

### 2.2 `settings.json` — 深合并

`load_user_settings()` 使用 `_deep_merge` 合并全局与项目设置。受限模式完全忽略项目层，避免未信任 Hook 或连接开关生效。授权策略不从 settings 加载。

### 2.3 `mcp_servers.json` — 按 server 名深合并 + 角色层并入

- `load_mcp_servers()`（`config_mgr.py:145`）只读顶层 `mcpServers` 字段，`_deep_merge(全局, 项目)`，同名 server 由项目层覆盖全局层。
- `McpMgr.start()`（`mcp_mgr.py:122`）先取**角色层** `src/roles/<role>/mcp_servers.json`（`role_mgr.mcp_servers_path()`），再 `servers.update(config_mgr.load_mcp_servers())`。因此优先级为 **角色 < 全局 < 项目**，角色层是最低优先级、被全局/项目同名 key 覆盖。

### 2.4 `.env` 加载顺序

`load_config()` 按以下顺序用 `dotenv_values()` 构造私有有效环境，后加载者覆盖同名变量，不修改进程全局环境：

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

### 3.1 `llm_provider.<name>` — LLM provider 连接配置

`LLMMgr.load_models()` 与 `_create_provider()` 消费。`<name>` 必须是框架已知 provider；provider 顶层和每个条目都必须是 mapping，名称与 `base_url` 必须是非空字符串。`models` 必须是 `list[str]`，元素必须非空，加载时按首次出现顺序去重。

| 键 | 类型 | 默认值 | 可选值 | 效果 |
|----|------|--------|--------|------|
| `llm_provider.<name>.base_url` | str | 见 `src/config.yaml` | 非空字符串 | provider API 端点；可被 `{NAME}_API_URL` 覆盖 |
| `llm_provider.<name>.api_key` | str | 无（通常来自 `.env`） | — | API key；通常经 `{NAME}_API_KEY` 注入 |
| `llm_provider.<name>.web` | str | `local` | `local` \| `provider` | `web_search` 与 `web_fetch` 的路由；`provider` 只在原生能力不支持时回退本地，其他错误不回退 |
| `llm_provider.<name>.context_limit` | int | 缺键时 `0` | 正整数 | 上下文窗口 token 上限；压缩阈值由此换算 |
| `llm_provider.<name>.preserve_thinking` | bool | 缺键时 `false` | `true`/`false` | 是否在历史中保留 reasoning 内容 |
| `llm_provider.anthropic.max_pause_turn_continuations` | int | `5` | 非 bool 正整数 | Anthropic 单个响应恢复链允许的 `pause_turn` 自动续接次数；其他 provider 归一为 `0` |
| `llm_provider.<name>.models` | list[str] | 缺键时 `[]` | 模型 ID 列表（可为空） | provider API 拉取失败时的静态回退清单；空列表表示发现失败后不注册模型 |

角色级 `role.<角色名>.reasoning_effort` 是 Agent 调用时的单值覆盖，default/fast 两个槽位共用；它缺失时，主 agent 可使用 `role.md` 的合法 effort，再缺失才使用 Provider 类的内部默认值 `max`。Provider effort 不从配置读取。子 agent 自身 frontmatter effort 与继承规则见 [roles-subagents-skills.md](roles-subagents-skills.md)。

### 3.2 `llm` — 调用参数

`LLMMgr.__post_init__` 消费本段。`llm` 与 `llm.retry` 都必须是 mapping；模型槽位不再位于本段。`max_attempts` 必须是非 bool 正整数，timeout 和两项延迟必须是非 bool 的有限正数，且最大延迟不得小于基础延迟；`RetryConfig` 构造时还会执行同样的独立校验。

| 键 | 类型 | 默认值 | 可选值 | 效果 |
|----|------|--------|--------|------|
| `llm.concurrency` | int | `5` | `>= 1` 的整数 | provider 并发上限 |
| `llm.timeout_seconds` | int \| float | `120` | 有限正数 | 单次 provider LLM 请求的 SDK 超时秒数；不影响模型发现 |
| `llm.retry.max_attempts` | int | `10` | `>= 1` 的整数 | 最大尝试次数，包含首次调用；`1` 表示不自动重试 |
| `llm.retry.base_delay_seconds` | int \| float | `2` | 有限正数 | 无有效等待响应头时的指数退避基础秒数 |
| `llm.retry.max_delay_seconds` | int \| float | `300` | 有限正数，且不小于基础延迟 | 相邻尝试之间的单次退避等待封顶秒数 |
| `llm.user_agent` | str | `claude-cli/2.1.201 (external, cli)` | 任意字符串 | 非空时作为 provider 及模型发现请求的自定义 User-Agent；空串沿用 SDK 默认值 |

模型必须配置在 `role.<角色名>.model.default/fast`。模型解析只接受两个槽位别名、Claude Code 兼容别名和完整模型 ID：`opus`/`sonnet` → `default`，`haiku` → `fast`；无法精确解析时直接报 `ModelUnavailableError`。

### 3.3 `tool` — 工具结果分页

| 键 | 类型 | 默认值 | 可选值 | 效果 |
|----|------|--------|--------|------|
| `tool.page_token_rate` | float | `0.03` | `0`~`1` | 单个工具调用结果每页最多占上下文窗口的比例；`LLMMgr.__post_init__` 读取（`llm_mgr.py:119`）后传给每个 provider（`llm_mgr.py:384`）用于分页。分页机制见 [llm.md](llm.md) |

### 3.4 `compact` — 上下文压缩

`Agent.__post_init__`（`agent.py:202-212`）读取整个 `compact` 段，按 provider 的 `context_limit` 换算为**绝对 token 数**后构造 `CompactMgr`：

| 键 | 类型 | 默认值 | 可选值 | 效果 |
|----|------|--------|--------|------|
| `compact.auto_compact_rate` | float | `0.8` | `0`~`1` | 输入估算超过 `context_limit × auto_compact_rate` 时触发自动压缩。换算为 `auto_compact_size = int(context_limit * auto_compact_rate)`（`agent.py:209`）；`context_limit <= 0` 时自动压缩禁用 |
| `compact.keep_recent_user_turns` | int | `3` | 非负整数 | 定义优先保留原文的最近 N 个用户轮次范围；直接传给 `CompactMgr`（`agent.py:210`），不覆盖近期原文硬预算 |
| `compact.keep_recent_messages_token_rate` | float | `0.25` | `0`~`1` | 近期原文的硬预算比例；换算为 `recent_messages_token_limit = int(context_limit * keep_recent_messages_token_rate)`（`agent.py:211`）。优先轮次超限时会在其内部按 assistant/tool 原子块移动切分点 |

> **换算说明**：`config.yaml` 里存的是**比例**，`Agent` 在构造 `CompactMgr` 时用当前 agent 所用模型的 `context_limit`（`self.llm.context_limit`，`agent.py:203`）乘以比例得到绝对 token 数。不同 agent 若用不同 `context_limit` 的模型，绝对阈值也不同；窗口未知（非正数）时不会自动压缩。`CompactMgr` 细节见 [managers.md](managers.md)。

### 3.5 `role` — 激活角色、模型槽位与推理力度

`role` 必须是 mapping。`role.default` 先确定实际激活角色；缺省、空值或未发现的角色回退 `coding`。角色目录名作为 mapping key 原样使用，允许 Unicode、点号和长名称；`common` 与 `default` 是保留名，不会进入角色发现结果。

| 键 | 类型 | 默认值 | 可选值 | 效果 |
|----|------|--------|--------|------|
| `role.default` | str | 缺省或空值回退 `coding` | 合法且已发现的角色名 | 指定激活角色；连 `coding` 都不存在时无角色激活 |
| `role.<角色名>.model` | mapping | 无 | 必须同时含 `default`、`fast` | 当前角色的模型槽位；非 mapping 值会在启动时报错 |
| `role.<角色名>.model.default` | str | 无 | 已加载的完整模型 ID | 主 agent 恒用此槽位；省略 `model` 或声明 `default`/`opus`/`sonnet` 的子 agent 也解析到此槽位 |
| `role.<角色名>.model.fast` | str | 无 | 已加载的完整模型 ID | 声明 `fast`/`haiku` 的子 agent 及智能权限裁决使用此槽位 |
| `role.<角色名>.reasoning_effort` | str | `role.md` 的合法值，否则 Provider 类默认 `max` | `low` \| `medium` \| `high` \| `xhigh` \| `max` | 角色级单值，default/fast 两个槽位共用；规范化大小写和首尾空白，非法值告警并忽略 |

每个被激活角色都必须配置两个模型槽位，且两者必须精确指向已加载模型；内置 `src/config.yaml` 不提供模型兜底，缺任一键、值为空或模型不可用都会阻止启动。`role.md` 不再允许 `model` 字段，残留该键会在角色解析期报 `LLMConfigurationError`；主 agent 恒以 `default` 槽位构造。

`/models` 将 `model` mapping 与 `reasoning_effort` 一次写入可信项目层。只改 fast 时当前主 agent 不热切；新建子 agent 和智能权限会立即现读新槽位。default 或 effort 变化时，当前主 agent 原地切换并保留会话历史。角色与子 agent 细节见 [roles-subagents-skills.md](roles-subagents-skills.md)。

### 3.6 `events` — 事件级别

| 键 | 类型 | 默认值 | 可选值 | 效果 |
|----|------|--------|--------|------|
| `events.level` | str | `detail`（本仓库；`bootstrap` 内 `.get` 回退为 `progress`） | `progress` \| `detail` \| `trace` | 事件总线的输出详细度；`bootstrap.create_app()` 读取（`bootstrap.py:36`）经 `EventLevel.from_str` 构造 `EventBus`。`detail` 及以上会展示模型思考过程等更细粒度事件 |

> `src/config.yaml` 内置值为 `detail`；`bootstrap` 读取时 `config_mgr.get_config("events").get("level", "progress")` 的 `"progress"` 只在整个 `events.level` 键缺失时才生效。事件级别语义见 [events-and-ui.md](events-and-ui.md)。

### 3.7 `logging` — 运行日志级别

| 键 | 类型 | 默认值 | 可选值 | 效果 |
|----|------|--------|--------|------|
| `logging.level` | str | `info`（本仓库；`bootstrap` 内 `.get` 回退同） | `debug` \| `info` \| `warning` \| `error` | 根 logger 级别，作用于写入 `{workdir}/.agent/logs/agent.log` 的文件 handler（`bootstrap.create_app()`）。`info` 下记录所有授权拒绝与非确定性放行；`debug` 额外记录 `source=policy` 的确定性放行（见 [permissions.md](permissions.md)「授权日志」） |

### 3.8 完整注释版 `config.yaml` 示例

```yaml
# ── LLM provider 连接配置 ────────────────────────────────
# <name> 须为框架已知 provider；api_key 通常经 .env 的 {NAME}_API_KEY 注入。
llm_provider:
  deepseek:
    web: provider                         # provider 优先原生能力，不支持时回退本地
    models:                               # API 拉取失败时的静态回退清单
      - deepseek-v4-pro
      - deepseek-v4-flash
    base_url: https://api.deepseek.com
  openai:
    web: provider
    models:
      - gpt-5.6-sol
      - gpt-5.6-terra
      - gpt-5.6-luna
    base_url: https://api.openai.com/v1
  anthropic:
    web: provider
    models:
      - claude-opus-5
      - claude-sonnet-5
    base_url: https://api.anthropic.com
    max_pause_turn_continuations: 5       # pause_turn 协议终态的单轮最大自动续接次数
  ollama:
    web: local
    base_url: http://127.0.0.1:8001/v1
    preserve_thinking: true               # 保留历史 reasoning（Qwen 类 agent 场景）
    context_limit: 200000
  moonshot:
    web: local
    models:
      - k3
    base_url: https://api.moonshot.cn/v1

# ── LLM 调用参数；模型不在此段配置 ───────────────────────
llm:
  concurrency: 5
  timeout_seconds: 120
  retry:
    max_attempts: 10
    base_delay_seconds: 2
    max_delay_seconds: 300
  user_agent: "claude-cli/2.1.201 (external, cli)"

# ── 工具结果分页 ────────────────────────────────────────
tool:
  page_token_rate: 0.03

# ── 上下文压缩（比例，运行时按 context_limit 换算为绝对 token）──
compact:
  auto_compact_rate: 0.8
  keep_recent_user_turns: 3
  keep_recent_messages_token_rate: 0.25

# ── 激活角色、角色模型双槽位与共享推理力度 ───────────────
role:
  default: coding                         # 缺省或角色不存在时回退 coding
  coding:
    model:
      default: claude-opus-5              # 主 agent；model: default/opus/sonnet 的子 agent
      fast: deepseek-v4-flash             # model: fast/haiku 的子 agent；智能权限裁决
    reasoning_effort: max                 # 角色级单值，两个槽位共用

# ── 事件与日志级别 ──────────────────────────────────────
events:
  level: detail
logging:
  level: info
```

内置 `src/config.yaml` 只声明 `role.default` 与角色级 `reasoning_effort`，不提供任何角色模型槽位兜底；上例的 `role.coding.model` 必须写入全局或可信项目配置。

---

## 4. `settings.json` 完整 schema

承载 MCP 连接开关和生命周期 Hook。统一授权不读取任何用户设置。

| 键 | 类型 | 默认值 | 效果 | 消费点 |
|----|------|--------|------|--------|
| `mcp.enabledServers` | list[str] | 无（视为不启用白名单） | 非空时作**白名单**，只连接其中的 server | `mcp_mgr.py:193` |
| `mcp.disabledServers` | list[str] | 无 | 始终从待连接集合剔除（连接前硬开关） | `mcp_mgr.py:194` |
| `hooks.<Event>` | list[group] | 无 | 生命周期 hook 定义（见下） | `hooks_mgr.py:135` |

**`hooks` 事件**（`hooks_mgr.py:17` `HOOK_EVENTS`，共 8 种）：`PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`Stop`、`SessionStart`、`SessionEnd`、`SubagentStart`、`SubagentStop`。每个事件下是 group 列表，group 含可选 `matcher` 与 `hooks` 命令数组，命令支持 JSON stdin/stdout 协议、`timeout`（默认 60s）、`async`（默认 false）。格式详解见 [mcp-and-hooks.md](mcp-and-hooks.md)。

项目层设置只有通过项目启动信任后才合并。`/clear` 重新检查工作目录是否已信任，并按信任结果重载 Hook 与 MCP。

### 示例 `settings.json`

```json
{
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

配置各 MCP server 的连接方式。顶层唯一字段 `mcpServers`，值为 `{server名: spec}`。`spec` 由 `McpMgr._open_session()` 消费。

| 字段 | 类型 | 适用 transport | 效果 |
|------|------|----------------|------|
| `transport` | str | 全部 | `stdio`（默认）/ `http` / `streamable-http` / `streamable_http` / `sse`（`mcp_mgr.py:268`） |
| `command` | str | `stdio` | 启动命令（必填） |
| `args` | list[str] | `stdio` | 命令参数（默认 `[]`） |
| `env` | dict | `stdio` | 追加可信配置显式环境变量；基础环境由 DataGuard 去除秘密后构造 |
| `url` | str | `http`/`sse` | server 端点 URL（必填） |
| `headers` | dict | `http`/`sse` | HTTP 请求头 |

### 示例（本仓库 `mijia` 角色）

`src/roles/mijia/mcp_servers.json`：

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

`http`/`sse` 传输示例：

```json
{
  "mcpServers": {
    "github": {
      "transport": "streamable-http",
      "url": "https://api.example.com/mcp",
      "headers": { "Authorization": "Bearer ${TOKEN}" }
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
| `~/.agent/config.yaml` | 全局运行配置 | 手工或 `ConfigManager.set_config(..., "global")` |
| `~/.agent/settings.json` | 全局 MCP 开关与 Hooks | 手工 |
| `~/.agent/mcp_servers.json` | 全局 MCP 连接 | 手工 |
| `~/.agent/.env` | 全局环境变量 | 手工或首次 Provider 向导 / `ConfigManager.set_global_env` |
| `~/.agent/roles/` | 全局角色 | 手工 |
| `~/.agent/agents/` | 全局子 agent 定义 | 手工 |
| `~/.agent/skills/` | 全局技能 | 手工 |
| `~/.agent/plugins/` | 全局插件 | 手工 |
| `~/.agent/sessions/` | 会话历史与元数据 | `SessionMgr`（`session_mgr.py:50`，`global_dir / "sessions"`） |
| `~/.agent/tasks/{session_id}/` | 主 agent 任务持久化（每 task 一个 JSON + highwatermark） | `TaskManager`，仅主 agent（`agent.py:151`，`global_dir / "tasks" / session_id`） |
| `~/.agent/logs/tui.jsonl` | TUI 生命周期、降级与渲染诊断；2 MiB 轮转并保留两个备份 | `TuiDiagnostics`；生产装配注入 `global_dir / "logs"` |

### 项目 `{workdir}/.agent/`

| 路径 | 内容 | 来源 |
|------|------|------|
| `{workdir}/.agent/config.yaml` | 项目运行配置 | 手工或 `ConfigManager.set_config(..., "project")` |
| `{workdir}/.agent/settings.json` | 项目 MCP 开关与 Hooks（需项目信任） | 手工 |
| `{workdir}/.agent/mcp_servers.json` | 项目 MCP 连接 | 手工 |
| `{workdir}/.agent/.env` | 项目环境变量（最高优先级） | 手工 |
| `{workdir}/.agent/roles/`、`agents/`、`skills/`、`plugins/` | 项目级角色/子 agent/技能/插件 | 手工 |
| `{workdir}/.agent/memory/` | 记忆文件（`*.md`） | `MemoryMgr`（`memory_mgr.py:35`，`workdir / ".agent" / "memory"`） |
| `{workdir}/.agent/plans/` | plan 文件 | `PlanMgr`（`plan_mgr.py:70`，`workdir / ".agent" / "plans"`） |
| `{workdir}/.agent/logs/agent.log` | 运行日志，含逐次授权裁决（`授权 …` / `judge 裁决 …` / `转人工确认 …`）；2 MiB 轮转保留两个备份，目录 `0700`、文件 `0600` | `bootstrap.create_app` 配 `RotatingFileHandler`，级别见 `logging.level` |

### 运行产物

| 路径 | 内容 | 来源 |
|------|------|------|
| `{workdir}/.agent/transcripts/transcript_<time_ns>.jsonl` | 压缩前完整 Unicode 对话备份 | `CompactMgr.write_transcript`（`compact_mgr.py:279`，`project_data_dir(workdir) / "transcripts"`） |

TUI 诊断路径随 `$AGENT_HOME` 改写，不提供独立配置项。`tui.jsonl` 及 `.1`、`.2` 总上限约 6 MiB；日志不包含对话正文、Markdown、用户输入或工具参数，异常文本和 traceback 在写入前经运行时 `DataGuard` 脱敏。

> 落盘目录要点：会话（`sessions/`）、主 agent 任务（`tasks/`）和 TUI 诊断（`logs/`）在**全局** `~/.agent/` 下；记忆（`memory/`）、plan（`plans/`）、transcript（`transcripts/`）和运行日志（`logs/`）在**项目** `{workdir}/.agent/` 下。子 agent 的 `TaskManager` 为纯内存模式（`tasks_dir=None`），不落盘（`agent.py:150`、`task_mgr.py:82`）。目录路径体系见 [architecture.md](architecture.md)。

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

仓库中的 `.claude/settings.local.json` 是 Claude Code 编辑器自身的本地授权文件，不属于本框架。框架不会把其中内容加载为工具策略。

---

## 9. 「我想要 X 效果，改哪个键」速查表

| 想要的效果 | 改哪里 | 怎么改 |
|------------|--------|--------|
| 换主 agent 模型 | `config.yaml` `role.<角色名>.model.default` | 设为已加载的完整模型 ID |
| 换轻量子 agent / 智能权限模型 | `config.yaml` `role.<角色名>.model.fast` | 设为已加载的完整模型 ID；不会热切当前主 agent |
| 调整角色推理力度 | `config.yaml` `role.<角色名>.reasoning_effort` | 设为 `low`、`medium`、`high`、`xhigh` 或 `max`；两个槽位共用 |
| 更早触发上下文压缩 | `config.yaml` `compact.auto_compact_rate` | 调小（如 `0.6`） |
| 压缩后多保留近期对话 | `config.yaml` `compact.keep_recent_user_turns` / `keep_recent_messages_token_rate` | 调大 |
| 加大 / 减小模型上下文窗口 | `config.yaml` `llm_provider.<name>.context_limit` | 设为目标 token 数（同时影响压缩绝对阈值） |
| 提高 provider 并发 | `config.yaml` `llm.concurrency` | 调大 |
| 调整单次 LLM 请求超时 | `config.yaml` `llm.timeout_seconds` | 设为有限正秒数；模型发现固定为 3 秒，不可配置 |
| 调整自动尝试次数 | `config.yaml` `llm.retry.max_attempts` | 设为包含首次的正整数 |
| 调整重试等待 | `config.yaml` `llm.retry.base_delay_seconds` / `max_delay_seconds` | 设为有限正秒数，且最大值不小于基础值 |
| 调整 Anthropic 协议续接上限 | `config.yaml` `llm_provider.anthropic.max_pause_turn_continuations` | 设为非 bool 正整数；网络重试次数仍由 `llm.retry.max_attempts` 独立控制 |
| 换激活角色 | `config.yaml` `role.default` | 设为合法且已发现的角色名 |
| 看模型思考过程 | `config.yaml` `events.level` | 设为 `detail`（或 `trace`） |
| 看每次授权的完整裁决日志 | `config.yaml` `logging.level` | 设为 `debug`（`info` 已含拒绝与非确定性放行） |
| 禁用某个 MCP server | `settings.json` `mcp.disabledServers` | 加入该 server 名（连接前剔除） |
| 只启用部分 MCP server | `settings.json` `mcp.enabledServers` | 列出白名单（非空即生效） |
| 在工具调用 / 会话事件时跑脚本 | `settings.json` `hooks.<Event>` | 加 hook 命令（见 [mcp-and-hooks.md](mcp-and-hooks.md)） |
| 换 API key / 端点 | `.env` `{NAME}_API_KEY` / `{NAME}_API_URL` | 设对应 provider 的变量 |
| 换全局配置目录 | 环境变量 `$AGENT_HOME` | 指向新目录 |
| 换工作目录 | `--workdir` 或 `$AGENT_WORKDIR` | 指定路径 |
| 排查 UI 卡顿 / 阻塞事件循环 | CLI `--debug` | 加 `--debug` 启动看慢回调告警 |
