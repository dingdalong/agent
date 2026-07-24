# 工具系统

本文档面向开发者与运维者，说明本框架的工具层：`@tool` 装饰器、异步/阻塞契约、`ToolEntry` 与 `ToolPermission` 元数据、`ToolsMgr.execute` 执行流水线，以及逐个内置工具与安全要点。

相关文档：权限规则与 6 步检查见 [permissions.md](permissions.md)；MCP 上游工具的注册与包装见 [mcp-and-hooks.md](mcp-and-hooks.md)；工具事件（`ToolCallStarted`/`ToolCallCompleted`）见 [events-and-ui.md](events-and-ui.md)；分页所依赖的 `page_token_budget` 见 [llm.md](llm.md)；`ToolsMgr` 在依赖组装中的位置见 [managers.md](managers.md)；配置键 `tool.page_token_rate` 见 [configuration-reference.md](configuration-reference.md)。

---

## 1. `@tool` 装饰器

工具通过 `@tool` 装饰器（`src/tools/decorator.py:132`）注册。签名参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `model` | `type[BaseModel]` | Pydantic 参数模型，用于校验参数并生成 schema。 |
| `description` | `str` | 工具描述，发送给 LLM。 |
| `name` | `str \| None` | 工具名，默认用函数名。 |
| `permission` | `ToolPermission \| None` | 权限元数据，`None` 表示无特殊声明。 |
| `raw_output` | `bool` | `True` 时跳过结果分页截断。 |
| `subagent` | `bool \| None` | 子 agent 可见性三态（见第 3 节）。 |
| `feature` | `str \| None` | 所属可插拔 feature 名（见第 3 节）。 |

装饰器行为（`src/tools/decorator.py:155`）：

- **注册**：构造 `ToolEntry` 并 append 进全局 `_registry`（`src/tools/decorator.py:130`）。`ToolsMgr` 初始化时遍历 `_registry` 逐个 `register`。
- **schema 提取**：调 `model.model_json_schema()`；若顶层 `type == "object"` 则直接用作参数 schema，否则包一层 `{"type":"object","properties":{"input": <schema>},"required":["input"]}`。随后 pop 掉顶层 `description`。
- **context 注入**：调用时按函数签名的参数名从 context 注入（`agent` / `deps` / `current_tool_call_id` 等），这些注入参数不进入 LLM 看到的 schema（`ToolEntry.__call__` 的 `inject` 逻辑，`src/tools/decorator.py:96`）。
- **参数校验错误格式化**：Pydantic `ValidationError` 时最多取前 3 条错误，格式化为 `loc: msg` 拼接，超过 3 条追加「... 等N个错误」，整体返回 `参数验证失败: ...`（`src/tools/decorator.py:84`）。工具函数内部抛异常则返回 `工具执行出错: <type>: <msg>`（截断到 200 字符）。

`format_tool_tips(tips, tool_input, fallback)`（`src/tools/decorator.py:111`）把 `ToolPermission.tips` 模板用工具参数格式化，失败时回退，用于事件的 `detail` 展示。

---

## 2. 异步/阻塞契约

装饰器是框架**唯一的线程卸载点**（`src/tools/decorator.py:99`）：

```python
if inspect.iscoroutinefunction(self.func):
    result = await self.func(...)          # 协程：直接 await
else:
    result = await asyncio.to_thread(self.func, ...)  # 普通 def：卸载到线程
```

据此，工具作者的规则（呼应根 CLAUDE.md 的异步/阻塞契约）：

- **叶子工具做同步 I/O / CPU 工作** → 声明为**普通 `def`**，装饰器自动经 `asyncio.to_thread` 卸载，不冻结事件循环。例：`file.py` 各工具、`plan_write_file` / `plan_edit_file`、`web_search`、`web_fetch`。
- **真异步工具**（函数体只 `await` 真正的异步原语，如 `asyncio.create_subprocess_shell`、事件总线 `request_input`）→ 保持 `async def`。例：`shell`、`ask_user`、`task_*`、`task_delegator`。
- **禁止** 在 `async def` 里直接跑同步阻塞工作而不 `await`；无需层层手写 `to_thread`（装饰器是唯一卸载点）。

---

## 3. `ToolEntry` 与可见性/门控

`ToolEntry`（`src/tools/decorator.py:45`）承载工具完整元数据：

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | `str` | 工具名。 |
| `func` | `Callable` | 实现函数。 |
| `model` | `type[BaseModel]` | 参数模型。 |
| `description` | `str` | 发给 LLM 的描述。 |
| `parameters_schema` | `dict` | OpenAI 格式参数 schema。 |
| `permission` | `ToolPermission \| None` | 权限元数据。 |
| `raw_output` | `bool` | 是否跳过分页。 |
| `subagent` | `bool \| None` | 子 agent 可见性三态。 |
| `feature` | `str \| None` | 所属 feature。 |

### `subagent` 三态

由 `ToolsMgr.resolve_subagent_tools`（`src/mgr/tools_mgr.py:85`）解释：

- `True` = **自动注入** — 即使子 agent 定义未列出，也追加进其工具集（如 `task_*`、`read_tool_result`、`compact`）。
- `False` = **强制排除** — 即使子 agent 定义为全量，也从其工具集移除（如四个 plan 工具、`task_delegator`、`load_skill`、`ask_user`）。
- `None` = **随 agent 的 tools 列表决定**（如 `shell`、`web_search`）。

### `feature` 门控

由 `ToolsMgr.excluded_tool_names`（`src/mgr/tools_mgr.py:71`）解释：

- `feature=None` = 无归属，**恒可用**。
- `feature` 非 `None` = 仅当该 feature 被角色启用时才注入，否则从 schema 排除并在调用时拒绝。合法 feature 名与门控规则见 [roles-subagents-skills.md](roles-subagents-skills.md) 与根 CLAUDE.md 的 feature 门控一节。

---

## 4. `ToolPermission` 字段

`ToolPermission`（`src/tools/decorator.py:20`）声明工具的权限元数据，供 `PermissionManager` 消费（各字段对权限判定的完整影响见 [permissions.md](permissions.md)）：

| 字段 | 默认 | 影响 |
|---|---|---|
| `kind` | `None` | `"readonly"`=只读（所有模式自动放行且始终可见）；`"edit"`=文件编辑（acceptEdits 模式自动放行）；`None`=其他（如 shell）。 |
| `plan_visible` | `False` | 非只读工具默认在 plan 模式隐藏；`True` 使其在 plan 模式仍可见（plan 专用文件工具用）。 |
| `specifier_arg` | `None` | 内容级规则匹配的参数名；`check()` 提取该参数值做 fnmatch 匹配，也用于构建 "always allow" 会话规则。 |
| `tips` | `None` | 权限提示模板（如 `写入文件：{path}`、`task_delegator` 的 `委托 {agent_type}`），用工具参数格式化后作为事件 `detail`；缺省时回退工具描述。 |
| `check_permissions` | `None` | 工具自身安全逻辑检查函数 `(tool_input, ctx) -> PermissionCheckResult`；只处理工具特有安全逻辑（shell 危险命令、file 敏感路径），不做规则匹配。 |
| `mcp_server` | `None` | 该工具所属的 MCP server 名（仅 MCP 工具非空），供 ask 弹窗提供「信任整个 server」选项。 |

---

## 5. `ToolsMgr.execute` 执行流水线

`ToolsMgr.execute`（`src/mgr/tools_mgr.py:243`）是工具执行的统一入口，完整流程：

1. **查表**：未知工具名返回 `错误：未知工具 '<name>'`。
2. **PreToolUse hook**（`src/mgr/tools_mgr.py:281`）：若 hook `blocked` 或返回 `deny` 决策 → 返回 `权限拒绝：...`；若返回 `updated_input` 则替换参数。
3. **权限检查**（`src/mgr/tools_mgr.py:298`）：取 `agent.permission_mode`（缺省回退 `permission_mgr.default_mode`），调 `permission_mgr.check(tool_name, arguments, mode)`：
   - `deny` → 通知并返回 `权限拒绝：...`；
   - `ask`（或 hook 给出 ask）→ `permission_mgr.resolve_ask(...)` 弹窗，解析为 `deny` 则拒绝；
   - 否则（`allow` / `auto_allow`）→ `notify_decision`。
4. 发 **`ToolCallStarted`** 事件（`detail` 为 `format_tool_tips` 格式化后的提示）。
5. **调用工具**：`await tool(context, **arguments)`，context 注入 `current_tool_call_id` / `deps` / `agent`。
6. **PostToolUse hook**（`src/mgr/tools_mgr.py:326`）：`blocked` 则结果改写为拒绝；有 `additional_context` 则追加到结果末尾。
7. 发 **`ToolCallCompleted`** 事件（含 `status`、耗时、`result_preview`）。`_result_status` 按结果前缀（`错误：`/`参数验证失败:`/`工具执行出错:`/`权限拒绝：`）判为 `error`，否则 `success`。
8. **结果分页**（`src/mgr/tools_mgr.py:347`）：`raw_output=True` 或无 `tool_call_id` 或无 `llm` 时直接返回；否则 `_truncate` —— 若结果 `estimate_tokens` 超过 `llm.page_token_budget`，用 `llm.split_page` 切页缓存到 `_result_store`，返回首页并提示可用 `read_tool_result` 读后续页。

### schema 可见性过滤（重要）

`PermissionManager` **没有** `get_schemas()` 方法。schema 可见性过滤由 **`ToolsMgr.get_schemas(tool_names, permission_mgr, mode)`**（`src/mgr/tools_mgr.py:109`）实现：当同时传入 `permission_mgr` 与 `mode` 时，对每个工具回调 `permission_mgr.is_tool_visible(tool, mode)` 决定是否纳入 schema。工具按 `_tool_sort_key`（只读优先、非只读次之、无权限元数据最后）排序后返回 OpenAI function-calling 格式。

### 分页翻页

分页结果由 `read_tool_result` 工具翻页，内部调 `ToolsMgr.get_page(tool_call_id, page)`（`src/mgr/tools_mgr.py:144`），返回带「总页数 / 当前页 / 继续读取提示」的格式化内容。

---

## 6. 内置工具一览

下表逐个列出 `src/tools/builtin/*.py` 中的工具（标记以各文件 `@tool` 实参为准）。`kind` 列 `-` 表示 `ToolPermission.kind=None`；`subagent`/`feature` 列 `-` 表示未声明（即 `None`）。

| 工具名 | 参数（名:类型=默认） | subagent | kind | feature | 作用 |
|---|---|---|---|---|---|
| `list_directory` | `path:str\|None=None`, `max_depth:int=3` | - | readonly | file | 列出目录树形结构。 |
| `glob` | `pattern:str`, `path:str\|None=None` | - | readonly | file | rg 按 glob 查找文件（遵守 .gitignore，不含目录）。 |
| `grep` | `pattern:str`, `path:str\|None=None` | - | readonly | file | rg 正则搜索文件内容，返回文件、行号、匹配行。 |
| `get_file_info` | `path:str` | - | readonly | file | 获取文件/目录元数据（大小、行数、时间、权限）。 |
| `read_file` | `path:str`, `start_line:int\|None=None`, `end_line:int\|None=None` | - | readonly | file | 读取文件内容并附行号，可限定行范围。 |
| `create_directory` | `path:str` | - | edit | file | 创建目录（支持多级）。 |
| `move_file` | `source:str`, `destination:str` | - | edit | file | 移动/重命名文件或目录。 |
| `write_file` | `path:str`, `content:str`, `append:bool=False`, `chunk_index:int\|None=None`, `total_chunks:int\|None=None` | - | edit | file | 新建/覆盖/追加写入，支持分块写大文件。 |
| `edit_file_lines` | `file_path:str`, `start_line:int`, `new_text:str=""`, `end_line:int\|None=None` | - | edit | file | 按行号替换/插入/删除。 |
| `replace_all_in_file` | `file_path:str`, `old_text:str`, `new_text:str` | - | edit | file | 全局替换文件中所有匹配文本。 |
| `shell` | `command:str`, `timeout:int=300` | - | - | - | 执行 shell 命令（`asyncio.create_subprocess_shell`，真异步）。 |
| `web_search` | `query:str`, `max_results:int=5` | - | readonly | - | 联网搜索（`ddgs`，同步库，普通 def）。 |
| `web_fetch` | `url:str` | - | readonly | - | 访问 URL 返回网页正文（`urllib`，普通 def）。 |
| `task_create` | `subject:str`, `description:str`, `active_form:str\|None=None`, `metadata:dict\|None=None` | True | readonly | task | 创建任务（状态 pending）。 |
| `task_update` | `task_id:str`, `subject/description/active_form/status/owner/add_blocks/add_blocked_by/metadata`（均可选） | True | readonly | task | 更新任务字段；`status="deleted"` 触发删除。 |
| `task_list` | （无参数） | True | readonly | task | 列出所有任务摘要。 |
| `task_get` | `task_id:str` | True | readonly | task | 查看任务完整详情。 |
| `task_delegator` | `description:str`, `agent_type:str`, `prompt:str`, `task_id:str\|None=None` | False | readonly | subagent | 委派任务给子智能体并返回结果。 |
| `save_memory` | `title:str`, `description:str`, `type:MemoryType`, `body:str` | - | readonly | memory | 保存长期项目记忆（同标题覆盖）。 |
| `read_memory` | `title:str` | - | readonly | memory | 读取一条记忆的完整内容。 |
| `enter_plan_mode` | （无参数） | False | readonly | plan | 切换到计划模式并刷新工具可见性。 |
| `exit_plan_mode` | `file_path:str` | False | readonly（`plan_visible=True`） | plan | 提交计划供用户审核（自动/手动执行或修改意见）。 |
| `plan_write_file` | `name:str`, `content:str` | False | readonly（`plan_visible=True`） | plan | 全量写入计划文件（按计划名生成路径）。普通 def。 |
| `plan_edit_file` | `file_path:str`, `start_line:int`, `new_text:str=""`, `end_line:int\|None=None` | False | readonly（`plan_visible=True`） | plan | 按行号增量编辑计划文件。普通 def。 |
| `load_skill` | `name:str` | False | readonly | skill | 将指定技能全文加载进当前上下文。 |
| `calculator` | `expression:str` | - | readonly | - | AST 安全求值数学表达式：算术运算 + 数学函数（sqrt/log/sin/factorial/comb/mean 等）+ 常量（pi/e/tau）。普通 def。 |
| `random` | `operation:str`, `low/high:int\|None`, `items:list[str]\|None`, `count:int=1`, `length:int\|None`, `sides:int=6`, `num_dice:int=1` | - | readonly | - | 生成真随机值（int/float/choice/sample/shuffle/uuid/password/token_hex/dice/coin）。普通 def。 |
| `datetime` | `operation:str`, `timezone_name:str\|None`, `date1/date2:str\|None`, `amount:int\|None`, `unit:str\|None`, `timestamp:float\|None` | - | readonly | - | 当前时间与日期运算（now/diff/add/weekday/to_timestamp/from_timestamp）。普通 def。 |
| `encode` | `operation:str`, `text:str` | - | readonly | - | 文本编解码与哈希（base64/hex/url 编解码，md5/sha1/sha256）。普通 def。 |
| `text_stats` | `operation:str`, `text:str`, `substring:str\|None` | - | readonly | - | 精确文本统计（summary/char_count/byte_count/word_count/line_count/count_substring/reverse）。普通 def。 |
| `ask_user` | `questions:list[Question]`（`Question`=`question:str`+`header:str`+`options:list[Option]\|None=None`+`multi_select:bool=False`，1–3 项；`Option`=`label:str`+`description:str=""`） | False | readonly | - | 向用户提问，一次至多 3 个各自独立的问题。用户在单屏标签页向导内作答：←→ 切标签（顶部标签栏各题前带答题状态 ☑/☐、显示 header 简介、末尾恒有「提交」标签）、↑↓ 移动答案行、空格勾选多选项、数字直选、每题末尾恒有自定义输入行、Enter 确认并推进；选项的 description 以浅色副行展示在该选项下方供参考；底部常驻讨论栏（Tab 切入）随答案回传为「讨论：…」。 |
| `read_tool_result` | `tool_call_id:str`, `page:int=2` | True | readonly | - | 读取分页工具结果的后续页。`raw_output=True`。 |
| `compact` | `focus:str` | True | readonly | - | 触发对话历史压缩（信号工具，返回固定提示）。 |

说明：

- 五个 file 只读工具（`list_directory`/`glob`/`grep`/`get_file_info`/`read_file`）用 `check_permissions=check_file_read_permissions`（工作目录外路径 ask）；五个 file 编辑工具用 `check_file_edit_permissions`（敏感路径 ask，`bypass_immune=True`），`move_file` 用 `check_file_move_permissions`（源与目标都查）。它们都声明 `specifier_arg`（`path` 或 `file_path` / `source`）。
- `task_*` 虽为写任务列表的操作，但 `kind` 标为 `readonly`（不触碰文件系统/外部状态），且 `subagent=True` 自动注入到子 agent。
- `read_tool_result` 与 `compact` 标 `subagent=True`；`read_tool_result` 额外 `raw_output=True`（其内容本身就是分页结果，不再二次分页）。
- `MemoryType` 为 `save_memory` 的枚举参数类型，定义在 `src/mgr/memory_mgr.py`（见 [managers.md](managers.md) 的 MemoryMgr）。
- `random`/`datetime`/`encode`/`text_stats` 集中实现在 `src/tools/builtin/utility.py`，与 `calculator` 同属"确定性工具"：补齐 LLM 无法可靠完成的操作（真随机、当前时间/日期运算、编解码哈希、精确计数）。四者均 `readonly`、无 feature 门控（恒可用）、纯 stdlib、普通 def；每个工具用 `operation` 参数区分子操作。
- `calculator` 通过 AST 白名单（`SAFE_OPERATORS`/`SAFE_FUNCTIONS`/`SAFE_NAMES`）安全求值单条表达式：除算术运算外，放行白名单数学函数调用与常量，拒绝属性访问、关键字/星号参数及非白名单名称（挡住 `__import__`、`().__class__` 等）；`factorial` 带参数上限防挂死。

---

## 7. 工具安全要点

三个高风险工具在 `check_permissions` 中内置了独立安全逻辑，与 `PermissionManager` 的规则匹配正交（详见 [permissions.md](permissions.md)）。

### shell（`src/tools/builtin/shell.py`）

`check_shell_permissions`（`src/tools/builtin/shell.py:937`）的评估顺序：**危险 → deny，敏感路径 → ask，acceptEdits/auto 文件操作 → allow，只读 → allow，其余 → passthrough**。

- **只读命令自动放行**：命令的每个段的基础命令名在 `READONLY_COMMANDS` 白名单（`src/tools/builtin/shell.py:62`，含 `ls`/`cat`/`grep`/`rg`/`wc`/`ps`/`date` 等）内即视为只读。`git` 走 `GIT_READONLY_SUBCOMMANDS`（`status`/`log`/`diff`/`show` 等，对 `branch`/`tag`/`stash`/`config`/`worktree`/`remote` 做破坏性参数检测）。`sed`（无 `-i`）、`curl`（无 `-o/-O`）、`find`（无 `-delete`/危险 `-exec`）、`xargs`（递归检查被执行命令）单独判定；含反引号 `` ` `` 或写入普通文件的输出重定向即判非只读。
- **危险命令拦截（deny，`bypass_immune=True`）**：`_is_dangerous_command` / `_is_shell_deny_segment`（`src/tools/builtin/shell.py:455`）拦截：特权命令 `sudo`/`su`/`doas`；递归 `rm -r`；`chmod`/`chown` 带递归标志；`dd of=/dev/...`；`mkfs*`；`diskutil` 含 `erase`；`git clean -f`（非 dry-run）；危险 `find -delete`/`-exec`；`curl|sh` / `wget|sh` 管道到解释器；以及含反引号命令替换。
- **敏感路径 ask（`bypass_immune=True`）**：`_check_sensitive_paths`（`src/tools/builtin/shell.py:854`）提取命令中的路径候选（含重定向目标、`bash -c` 内部命令递归），命中 `is_sensitive_path` 即 ask。
- **acceptEdits/auto 放行**：`_is_accept_edits_command` 仅放行 `ACCEPT_EDITS_COMMANDS`（`mkdir`/`touch`/`rm`/`rmdir`/`mv`/`cp`/`sed`，且 rm 非递归）。

### file（`src/tools/builtin/file.py`）

- **敏感文件名 `SENSITIVE_NAMES`**（`src/tools/builtin/file.py:14`）：`.env` 系列、`credentials.json`、`.npmrc`、`.pypirc`、shell 配置（`.bashrc`/`.zshrc`/...）、`.gitconfig`。
- **敏感目录 `SENSITIVE_DIRS`**：`/.agent/`、`/.vscode/`、`/.idea/`。
- **`.git` 目录**：路径含 `/.git/` 或以 `/.git` 结尾（大小写不敏感）视为敏感。
- **越界检查 `is_outside_workspace`**（`src/tools/builtin/file.py:27`）：路径解析后不在 `workdir` 及 `extra_trusted`（如全局配置目录）内即视为工作区外。
- `is_sensitive_path`（`src/tools/builtin/file.py:53`）综合以上：编辑工具命中敏感路径 → ask（`bypass_immune=True`）；只读工具仅对工作区外路径 → ask（`bypass_immune=False`）。

### web_fetch SSRF 防护（`src/tools/builtin/web_fetch.py`）

- **仅 http/https**：`validate_http_url`（`src/tools/builtin/web_fetch.py:113`）拒绝其他 scheme 与缺主机名的 URL。
- **禁私网/环回/云元数据**：`_is_private_host`（`src/tools/builtin/web_fetch.py:152`）拒绝 `localhost`、`metadata.google.internal`，以及 private / loopback / link-local / multicast / reserved / unspecified 地址；`resolve_public_ips` 对 DNS 解析出的每个地址复检。
- **重定向后复检**：拿到 `response.geturl()` 后再次 `validate_public_url`（`src/tools/builtin/web_fetch.py:264`），阻断重定向到内网。
- **大小上限**：`DEFAULT_MAX_BYTES = 1_000_000`（约 1MB），按 `Content-Length` 预判并读取 `MAX+1` 字节兜底；附件（`Content-Disposition: attachment`）与不可读 `Content-Type` 直接跳过。
- **敏感 query 参数脱敏**：`redact_url`（`src/tools/builtin/web_fetch.py:170`）把 `token`/`api_key`/`password`/`secret`/`signature` 等 `SENSITIVE_QUERY_KEYS` 中的参数值替换为 `[REDACTED]` 再回显。
