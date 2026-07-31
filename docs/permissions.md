# 权限系统

权限系统决定"某次工具调用是放行、拒绝，还是弹窗让用户确认"。它由两部分组成：**权限模式**（粗粒度策略）与**规则引擎**（细粒度 allow/deny/ask 规则）。核心实现在 `src/mgr/permission_mgr.py` 的 `PermissionManager`。

工具的权限元数据（`ToolPermission`：`kind`/`specifier_arg`/`check_permissions`/`tips`/`mcp_server` 等）来自各工具的 `@tool` 声明，参见 [tools.md](tools.md)。配置入口（`settings.json` 的 `permissions.*`、`mcp_servers.json` 的 per-server `permissions`）参见 [configuration-reference.md](configuration-reference.md) 与 [mcp-and-hooks.md](mcp-and-hooks.md)。

## 权限模式按 agent 独立

可变的权限模式持有在**每个 `Agent` 实例**上（`agent.permission_mode`，以及 plan 模式的 `_pre_plan_mode`）。`PermissionManager` 只保留全局共享的规则字典、`session_allow` 和不可变的 `default_mode`。`check()` 接收调用方 agent 的 `mode` 参数。

`default_mode` 的**解析优先级**（`_load_config`，`permission_mgr.py:313-330`）：激活角色 `role.md` 的 `permissionMode` →（未声明）`settings.json` 的 `permissions.defaultMode` →（未声明）内置 `DEFAULT_MODE`。role 值由 `bootstrap.create_app()` 经构造参数 `role_default_mode=role_mgr.manifest.permission_mode` 注入，`_load_config` 在读完 `settings.json` 后最后套用它（存快照于 `_role_default_mode`，不受 `settings.json` 是否有 `permissions` 块影响）。该 `default_mode` 用于：主 agent 初始模式、未声明 `permissionMode` 的子 agent 回退值、`/clear` 重置目标、绑定前状态栏显示值。

语义：
- 用户的模式设置（`/mode`、Shift+Tab、`/plan`、`/resume` 恢复）**只作用于入口主 agent**（总控），见 [agent-runtime.md](agent-runtime.md) 的 `PermissionModeController`。
- 每个子 agent 在构造时从自身 frontmatter 的 `permissionMode` 取一次值（缺省回退到 `default_mode`），整个生命周期固定不变——子 agent 无 plan 能力（四个 plan 工具标记 `subagent=False` 强制排除），故并发子 agent 互不干扰。参见 [roles-subagents-skills.md](roles-subagents-skills.md)。

## 六种权限模式

模式常量定义在 `permission_mgr.py:94-117`。

| 模式 (`value`) | 常量 | 只读工具 | 编辑工具 (`kind="edit"`) | shell / 其他 |
|---|---|---|---|---|
| `default` | `DEFAULT_MODE` | 自动放行 | 询问（可被 allow 规则放行） | 询问 |
| `acceptEdits` | `ACCEPT_EDITS_MODE` | 自动放行 | 自动放行 | 询问 |
| `plan` | `PLAN_MODE` | 自动放行 | 询问 | 询问 |
| `bypassPermissions` | `BYPASS_MODE` | 自动放行 | 自动放行 | 自动放行（但 `deny`/`ask` 规则、含 mcp 层仍生效） |
| `auto` | `AUTO_MODE` | 自动放行 | 自动放行（安全关键文件除外→人工） | 只读命令放行、安全文件操作放行、安全关键写入→人工；其余模糊操作交 **LLM 判官**（见「auto 模式 LLM 判官」） |
| `dontAsk` | `DONT_ASK_MODE` | 自动放行 | 拒绝 | 从不弹窗：一律拒绝 |

模式默认策略的实现是 `_mode_default()`（`permission_mgr.py:590-622`），按工具的 `kind`（`"readonly"`/`"edit"`/`None`）分派。

**模式切换集合**（`permission_mgr.py:120-128`）：
- `CAROUSEL_MODES`（Shift+Tab 轮转）：`default → acceptEdits → plan → default`。
- `MENU_MODES`（`/mode` 菜单，全部六种）：`default`、`acceptEdits`、`plan`、`bypassPermissions`、`auto`、`dontAsk`。
- `parse_permission_mode(text)`（`:134`）支持按编号（1–6）或模式名解析。

## 六步评估顺序

`check(tool_name, tool_input, mode)`（`permission_mgr.py:446-559`）按固定顺序求值，命中即返回 `(decision, reason)`，`decision ∈ {allow, deny, ask, auto_allow}`：

| 步骤 | 内容 | 命中结果 |
|---|---|---|
| **Step 1** | `deny_rules` 匹配（`specifier` 见下）。shell 复合命令另有 Step 1.5 逐段检查 `_check_compound_against_deny_rules` | `deny` |
| **Step 2** | `ask_rules` 匹配。**bypass-immune**：任何模式（含 bypass）都不跳过 | `ask` |
| **Step 3** | 工具自检 `check_permissions(tool_input, ctx)`：`deny`→拒绝；`ask`+`bypass_immune`→询问（`dontAsk` 模式转 `deny`）；`ask`+非 immune→记录原因后**穿透**到 Step 4；`allow`→放行 | 见描述 |
| **Step 4** | `allow_rules` + `session_allow` 匹配。shell 复合命令须**每一段**都被 allow 覆盖（`_check_compound_against_allow_rules`）；简单命令额外尝试剥离安全包装后的候选 | `allow` |
| **Step 4.5** | 处理 Step 3 记录的非 immune ask：`dontAsk`→`deny`；`bypassPermissions`→穿透继续；其他模式→`ask` | 见描述 |
| **Step 4.7** | `mcp_servers.json` 声明的 server 级规则（**最低优先级层**）：`mcp_deny → mcp_ask → mcp_allow`。置于 bypass 之前，故 BYPASS 模式下 mcp 的 `deny`/`ask` 仍生效 | `deny`/`ask`/`allow` |
| **Step 5** | `bypassPermissions` 模式 | `auto_allow` |
| **Step 6** | 模式默认策略 `_mode_default()`（按 `kind`） | `allow`/`ask`/`deny` |

**关键含义**：`settings.json` 的规则（Step 1–4）先于 `mcp_servers.json` 的 server 级规则（Step 4.7）评估。只要 `settings.json` 有规则命中即由它决定，否则才落到 `mcp_servers.json`。因此 `settings.json` 的 `allow`（含"信任整个 server"写入 `session_allow` 的 `mcp__<server>__*`）能**覆盖** `mcp_servers.json` 的 `deny`。两层共用同一 `PermissionRule` 表示与匹配引擎，仅优先级不同（`permission_mgr.py:1-11` 模块文档）。

## auto 模式 LLM 判官

同步 `check()` 引擎跑**确定性快路径**（不调 LLM）；**仅在 auto 模式**下，当 `check()` 判为 `ask`（且非 hook 触发的 ask）时，`ToolsMgr.execute` 在**已是 async 的执行链**上插入 LLM 判官（`auto_judge`），替代人工审批对模糊操作做放行/拒绝/交人工三选一。其余模式（default/acceptEdits/plan/bypass/dontAsk）完全不经判官，行为不变。

### 分层（对齐 Claude Code auto mode）

判官只承接下表最后一层「其余模糊操作」；前面几层由 `check()` 与工具自检确定性处理、不调 LLM：

| 层 | 内容 | 处理 | 调 LLM |
|---|---|---|---|
| 硬底线 | `deny` 规则、`_is_dangerous_command`、`ask` 规则 | `deny` / `ask` | 否 |
| Tier 1 | 只读工具/命令、显式 `allow`/`session_allow` 规则 | `allow` | 否 |
| Tier 2 | 工作区/可信目录内文件编辑、`_is_accept_edits_command`（`mkdir`/`touch`/`cp`/`mv`/`sed` 等） | `allow` | 否 |
| 安全关键 | 两根 `.agent/{settings.json,mcp_servers.json,config.yaml}`、`.env`/凭证/shell 配置/`.gitconfig`、`.git` 内部、`.vscode`/`.idea`（`is_security_critical_path`） | 强制人工确认（`bypass_immune` ask，判官不得静默放行） | 否 |
| Tier 3 | 其余会 `ask` 的模糊动作：状态变更 shell、工作区外操作、MCP、网络等 | **LLM 判官** → `allow` / `ask`（人工）/ `deny`（回落 agent 重试） | 是 |

### 判官流程 `auto_judge`

`auto_judge(tool_name, tool_input, mode, deps, agent)`（`permission_mgr.py`）返回 `(decision, reason)`，处理顺序：

1. **安全关键自守**：先用 `_judge_security_critical_hit` 扫描目标路径（文件工具取 `path`/`file_path`/`source`/`destination`；shell 复用 `_check_security_critical_paths` 分词扫描）。命中即直接返回 `("ask", …)`、**绝不调 LLM**——防提示注入诱导判官静默放行两根 `.agent` 核心配置写入（自我提权）。
2. **升级阈值**：累计拒绝达 `maxTotalDenials`（缺省 20，会话级硬底线，不重置）→ 转人工；连续拒绝达 `maxConsecutiveDenials`（缺省 3）→ 清零连续计数并转人工（交人工后判官恢复）。
3. **会话缓存**：键为 `(tool_name, 规范化参数)`（`_judge_cache_key`：优先 `specifier`，否则 `json.dumps(sort_keys=True)`）。命中直接复用，不二次调 LLM。
4. **调判官**：`_call_judge` 用快模型（`autoJudge.model`，缺省别名 `fast`，未配回落 `default`）以 `temperature=0.0`、`enable_thinking=False`、`reasoning_effort_override="low"` 调用；用**强制工具调用** `record_verdict(decision, reason)` 取回结构化裁决（无 JSON-mode helper），`_parse_verdict` 解析，解析失败回落 `ask`。
5. **计数更新**：裁决 `deny` 自增连续与累计计数；`allow` 清零连续计数。

**失败即回落人工**：判官出错/超时/不可用/无效裁决一律返回 `("ask", …)`，**绝不 error→allow**（`_call_judge` 的异常由 `auto_judge` 以 `except Exception` 兜底；`CancelledError`/`KeyboardInterrupt` 属 `BaseException`，正常向上传播）。

### execute 侧分流

`ToolsMgr.execute` 在同步 `check()` 之后、`resolve_ask` 之前插入分流（仅当 `decision == "ask" and not hook_has_ask and mode is AUTO_MODE and judge_enabled`）：

- 判官 `allow` → 置 `decision="allow"`，**静默执行**（不弹窗、不额外通知，工具面板已展示调用）。
- 判官 `deny` → 经 `notify_decision(..., "deny")` 透明告知，`return f"判官拦截：{reason}。请改用更安全的做法后重试。"` **回落 agent**（作为工具结果返回，agent 可换更安全做法重试）。
- 判官 `ask`（不确定或已达升级阈值）→ 保持 `decision=="ask"`，落入下方 `resolve_ask` 人工确认。

### 配置与 reload

判官配置就近放 `settings.json` 的 `permissions.autoJudge`（`_load_judge_config` 消费，schema 见 [configuration-reference.md](configuration-reference.md)）：`enabled`（缺省 `true`）、`model`（缺省 `fast`）、`maxConsecutiveDenials`（缺省 3）、`maxTotalDenials`（缺省 20）；`settings.json` 无内置层，缺省在代码兜底。`reload()`（`/clear`）把这四项重置为缺省、清空 `_judge_cache` 与两个拒绝计数器，再 `_load_config` 按最新 `settings.json` 覆盖——故**编辑 `autoJudge` 随 `/clear` 生效**。

## 规则格式 `PermissionRule`

规则文本形如 `工具名` 或 `工具名(specifier)`（`parse_rule`，`permission_mgr.py:232`；正则 `_RULE_PATTERN` 见 `:152`）。

- **工具名段**支持 `*`/`?` 通配（`_RULE_PATTERN` 允许 `[\w*?-]`）。`_get_rules()`（`:426`）在调用期先取精确键，再对含通配的键做 `fnmatch`。故可写 `mcp__github__*` 一次性覆盖整个 server，或 `mcp__github__get_*` 按前缀放行。
- **specifier** 三种匹配类型（`PermissionRule.rule_type`，`:175`）：
  | 类型 | 写法 | 匹配语义 |
  |---|---|---|
  | `exact` | `git status` | 精确相等 |
  | `prefix` | `git commit:*`（以 `:*` 结尾） | 前缀后须是空格或结尾（词边界），如匹配 `git commit -m x` |
  | `wildcard` | `npm *` / `*`（含 `*`/`?`） | `fnmatch` 通配；`*` 匹配该工具全部调用 |
- specifier 值从 `tool_input` 中按工具的 `specifier_arg` 提取（`_extract_specifier`，`:574`）；未声明 `specifier_arg` 的工具按空串处理（即只能用 `工具名` 无参形式匹配）。
- 示例：`shell(git commit:*)`、`write_file`、`mcp__mijia__*`、`shell(npm *)`。三类规则字典按 `deny`/`ask`/`allow` 分开维护（`deny_rules`/`ask_rules`/`allow_rules`，`:272-274`）。

## `resolve_ask` — 弹窗与"记住选择"

当 `check()` 返回 `ask` 时，`ToolsMgr.execute` 调用 `resolve_ask()`（`permission_mgr.py:624-685`）经 `event_bus.request_permission(...)` 向 UI 请求确认。用户回答（归一化小写）与效果：

| 回答 | 效果 |
|---|---|
| `y` / `yes` | 本次放行 |
| `session` | 加入 `session_allow`（仅本会话内存，不落盘） |
| `always` | 加入 `session_allow` **并**持久化到 `settings.json`（`_persist_allow_rule` → `config_mgr.append_permission_list("allow", ...)`) |
| `session_server` | 信任整个 MCP server：把 `mcp__<server>__*` 加入 `session_allow`（仅当该工具有 `mcp_server`） |
| `always_server` | 信任整个 server 并持久化到 `settings.json` |
| 其他 | 拒绝 |

要点：
- 建议规则由 `_build_session_rule`（`:687`）智能生成——shell 优先生成前缀规则（如 `git commit:*`），减少后续同类弹窗；复合命令由 `_build_compound_session_rules`（`:719`）为每段生成前缀规则。
- **持久化只落 `settings.json`**；框架永不写回 `mcp_servers.json`。
- MCP 工具的 server 名经 `ToolPermission.mcp_server` 透传到 `_mcp_servers`（`:309`），据此提供 `mcp__<server>__*` 的"信任整个 server"选项（`:653-658`）。

## 其他方法与 reload

- `notify_decision()`（`:847`）：对 `deny`/`auto_allow` 决策经 `event_bus.notify_permission` 发通知（`allow` 不通知）。auto 判官放行走 `allow` 分支故静默，判官拦截走 `deny` 分支发通知。
- `reload()`：`/clear` 时把 `default_mode` 重置为 `DEFAULT_MODE`，清空 `session_allow` 与全部规则字典，并把 auto 判官的四项配置重置为缺省、清空 `_judge_cache` 与两个拒绝计数器，再 `_load_config()` 重新加载——`_load_config` 会重放 `settings.json`（含 `autoJudge`）与 role（`_role_default_mode` 快照不清空），故 `/clear` 后 `default_mode` 仍保持上述优先级（role 值不丢）。因此**编辑 `settings.json` 的权限规则与 `autoJudge` 可随 `/clear` 生效**；而 `mcp_servers.json` 的 per-server 规则由 `McpMgr` 在启动时抽取，`McpMgr` 无 `reload`，故**编辑需重启**（见 [mcp-and-hooks.md](mcp-and-hooks.md)）。role.md 的 `permissionMode` 在 `create_app()` 构造时注入一次，故**编辑 role.md 需重启**才刷新。
